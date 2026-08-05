# Paper-BibChecker Web Demo —— 通用容器镜像（Render / HF Spaces / 本地）
#
# 本地构建运行：
#   docker build -f web/Dockerfile -t bibchecker-web .
#   docker run -p 7860:7860 bibchecker-web
# 然后打开 http://localhost:7860

FROM python:3.11-slim

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY pyproject.toml ./
COPY web/requirements.txt web/requirements.txt
COPY bibchecker ./bibchecker
RUN pip install --no-cache-dir -e . \
    && pip install --no-cache-dir -r web/requirements.txt

# 再拷贝 web 代码
COPY web ./web

# 公网部署：开启加固（移除 SSRF provider、限流、限条目数、清理缓存）
ENV PUBLIC_MODE=1 \
    MAX_ENTRIES=60 \
    MAX_CONCURRENT=2 \
    MAX_BATCH_SIZE=8 \
    MAX_UPLOAD_MB=5 \
    PYTHONUNBUFFERED=1

# 监听端口：Render 等平台通过 $PORT 注入；本地未设置时回退 7860
ENV PORT=7860
EXPOSE 7860

# 生产启动：非 reload，单 worker（并发由应用内的信号量控制）。
# 用 shell 形式让 $PORT 在运行时展开。
CMD ["sh", "-c", "uvicorn web.server:app --host 0.0.0.0 --port ${PORT:-7860}"]
