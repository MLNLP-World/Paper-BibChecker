"""Paper-BibChecker 本地 Web Demo 后端。

这是一层薄薄的 FastAPI 封装，直接复用 ``bibchecker`` 的核心函数，
不改动 ``bibchecker/`` 内的任何代码。检查是阻塞式网络 I/O，因此接口用
同步 ``def`` 定义，由 Starlette 放进线程池执行；结果以 NDJSON 流式返回，
前端可实时展示进度。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
from pathlib import Path
import shutil
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from bibchecker import (
    check_entry,
    default_providers,
    parse_bib,
)

# 与 bibchecker/cli.py 一致的状态标签。直接在此定义，避免为一个常量导入整个
# cli 模块（其顶层类型注解需要 Python 3.10+）。
STATUS_LABELS = {
    "validated": "✅ 通过",
    "needs_review": "⚠️ 信息需核对",
    "likely_hallucination": "❌ 疑似幻觉",
    "unconfirmed": "❓ 无法确认",
}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 公网加固开关（本地默认关闭，线上通过环境变量开启）。
#   PUBLIC_MODE=1        一键开启下面所有面向公网的保护
#   MAX_ENTRIES=60       单次最多检查的条目数（0 表示不限）
#   MAX_CONCURRENT=2     同时运行的检查任务数上限
#   MAX_BATCH_SIZE=8     允许的最大并行数量（防止有人传一个巨大的 batch_size）
#   MAX_UPLOAD_MB=5      单个上传文件大小上限
# ---------------------------------------------------------------------------
PUBLIC_MODE = _env_bool("PUBLIC_MODE", False)
# 公网模式下移除会抓取任意 URL 的 provider（SSRF 防护）。
DROP_URL_PROVIDER = _env_bool("DROP_URL_PROVIDER", PUBLIC_MODE)
MAX_ENTRIES = _env_int("MAX_ENTRIES", 60 if PUBLIC_MODE else 0)
MAX_CONCURRENT = _env_int("MAX_CONCURRENT", 2 if PUBLIC_MODE else 8)
MAX_BATCH_SIZE = _env_int("MAX_BATCH_SIZE", 8)
MAX_UPLOAD_BYTES = _env_int("MAX_UPLOAD_MB", 5) * 1024 * 1024

# 限制同时运行的检查任务数，超出直接拒绝，保护服务不被拖垮。
_slots = threading.BoundedSemaphore(max(1, MAX_CONCURRENT))

HERE = Path(__file__).resolve().parent

app = FastAPI(title="Paper-BibChecker Web Demo")

# 允许 GitHub Pages 前端跨域调用。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_providers(timeout: float):
    providers = default_providers(timeout=timeout)
    if DROP_URL_PROVIDER:
        # DirectURLProvider.name == "url"，它会抓取 Bib 中任意 URL（SSRF 风险）。
        providers = [p for p in providers if getattr(p, "name", "") != "url"]
    return providers


def _clear_http_cache() -> None:
    """清空 provider 的进程级 HTTP 缓存，避免长时间运行内存无限增长。"""
    try:
        from bibchecker.providers import _HTTPProvider

        with _HTTPProvider._cache_lock:
            _HTTPProvider._cache.clear()
            _HTTPProvider._request_locks.clear()
    except Exception:
        pass


@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "public_mode": PUBLIC_MODE,
        "max_concurrent": MAX_CONCURRENT,
        "max_entries": MAX_ENTRIES,
        "max_batch_size": MAX_BATCH_SIZE,
    }


@app.post("/api/check")
def check(
    bib: UploadFile = File(...),
    timeout: float = Form(10.0),
    batch_size: int = Form(4),
) -> StreamingResponse:
    if batch_size < 1:
        raise HTTPException(status_code=400, detail="batch_size 必须至少为 1")
    if timeout <= 0:
        raise HTTPException(status_code=400, detail="timeout 必须大于 0")
    # 夹住 batch_size，防止有人传一个巨大的值制造大量并发外连。
    batch_size = min(batch_size, MAX_BATCH_SIZE)

    # 并发闸门：同时运行的检查任务超过上限就拒绝，保护服务。
    if not _slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503, detail="服务繁忙，请稍后再试（并发已达上限）"
        )
    slot_released = False

    def release_slot() -> None:
        nonlocal slot_released
        if not slot_released:
            slot_released = True
            _slots.release()

    try:
        workdir = Path(tempfile.mkdtemp(prefix="bibchecker_web_"))
        bib_path = workdir / "input.bib"
        _save_upload(bib, bib_path)

        # 镜像 CLI 的选键逻辑（bibchecker/cli.py _run）。
        try:
            entries = parse_bib(bib_path)
        except Exception as error:  # 解析失败要给前端明确原因，而不是 500。
            shutil.rmtree(workdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"解析 Bib 失败：{error}")

        keys = sorted(set(entries))
        truncated = 0
        if MAX_ENTRIES and len(keys) > MAX_ENTRIES:
            truncated = len(keys) - MAX_ENTRIES
            keys = keys[:MAX_ENTRIES]
        providers = _build_providers(timeout)
    except HTTPException:
        release_slot()
        raise
    except Exception:
        release_slot()
        raise

    def generate():
        try:
            yield _line(
                {
                    "type": "start",
                    "total": len(keys),
                    "truncated": truncated,
                    "max_entries": MAX_ENTRIES,
                }
            )
            counts: dict[str, int] = {}
            completed = 0
            # 分批并行检查，复刻 cli._check_batches 的并发模型
            # （ThreadPoolExecutor(max_workers=batch_size)，逐条产出）。
            for start in range(0, len(keys), batch_size):
                batch_keys = keys[start : start + batch_size]
                with ThreadPoolExecutor(max_workers=batch_size) as executor:
                    futures = {
                        key: executor.submit(check_entry, entries[key], providers)
                        for key in batch_keys
                    }
                    # 保持批内顺序，输出更可读。
                    for key in batch_keys:
                        result = futures[key].result()
                        completed += 1
                        status = result.status
                        counts[status] = counts.get(status, 0) + 1
                        yield _line(
                            {
                                "type": "result",
                                "index": completed,
                                "key": key,
                                "status": status,
                                "label": STATUS_LABELS.get(status, status),
                                "result": result.as_dict(),
                            }
                        )
            yield _line({"type": "summary", "counts": counts})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            if PUBLIC_MODE:
                _clear_http_cache()
            release_slot()

    return StreamingResponse(
        generate(), media_type="application/x-ndjson"
    )


class CheckEntryRequest(BaseModel):
    """单条检查请求：带上字段字典就够。"""
    fields: dict[str, str]
    entry_type: str = "article"
    key: str = ""
    timeout: float = 10.0


@app.post("/api/check-entry")
def check_entry_api(body: CheckEntryRequest) -> dict:
    """对单条 Bib 条目做深度检查（不限量、调用全部 provider）。

    GitHub Pages 前端先用自己的 OpenAlex+Crossref 快速查一遍，
    结论为 unconfirmed 的条目缓存到此接口做二次深度复查。
    """
    from bibchecker.models import BibEntry
    from bibchecker.checker import check_entry as _check_entry

    entry = BibEntry(
        key=body.key or "entry",
        entry_type=body.entry_type,
        fields=body.fields,
    )
    providers = _build_providers(timeout=body.timeout)
    result = _check_entry(entry, providers)
    return result.as_dict()


def _line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _save_upload(upload: UploadFile, destination: Path) -> None:
    size = 0
    with destination.open("wb") as handle:
        while True:
            chunk = upload.file.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                handle.close()
                raise HTTPException(
                    status_code=413,
                    detail=f"文件过大，上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
                )
            handle.write(chunk)
