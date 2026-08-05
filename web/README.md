# Paper-BibChecker Web Demo

一个最小的本地网站：上传 `.bib`（可选 `.tex`）→ 实时查看每条引用的核验结果 → 下载 JSON 报告。

后端是一层薄薄的 FastAPI 封装，直接复用 `bibchecker` 的核心函数，**不改动** `bibchecker/` 里的任何代码。前端是单个 `index.html`（原生 HTML/JS，无构建步骤）。

## 运行

在仓库根目录执行：

```bash
# 1. 确保核心包已安装（README 根目录已说明）
python -m pip install -e .

# 2. 安装 Web 依赖
python -m pip install -r web/requirements.txt

# 3. 启动
uvicorn web.server:app --port 8000
# 开发时可加 --reload
```

打开浏览器访问 http://localhost:8000

用仓库自带的 `examples/example_ref.bib` 即可试跑（32 条引用，含多条 desk-reject 幻觉文献）。

## 说明

- 检查是网络阻塞型任务，单条需数秒；页面会实时显示进度与逐条结果。
- 上传文件写入临时目录，检查完成后自动删除。
- 结果分四类展示：✅ 通过 / ⚠️ 信息需核对 / ❓ 无法确认 / ❌ 疑似幻觉。
- 可选环境变量（都不是必需的，用于提高数据源配额）：
  `OPENALEX_EMAIL`、`CROSSREF_EMAIL`、`GITHUB_TOKEN`。

## 公网部署

面向公网时，通过环境变量一键开启加固（本地默认不开）：

| 环境变量 | 默认（本地） | 公网建议 | 作用 |
| --- | --- | --- | --- |
| `PUBLIC_MODE` | `0` | `1` | 一键开启下列所有保护 |
| `DROP_URL_PROVIDER` | 跟随 `PUBLIC_MODE` | `1` | 移除会抓取 Bib 中任意 URL 的 provider（**SSRF 防护**） |
| `MAX_ENTRIES` | `0`（不限） | `60` | 单次最多检查条目数，超出部分跳过并提示 |
| `MAX_CONCURRENT` | `8` | `2` | 同时运行的检查任务数上限，超出返回 503 |
| `MAX_BATCH_SIZE` | `8` | `8` | 允许的最大并行数量 |
| `MAX_UPLOAD_MB` | `5` | `5` | 单个上传文件大小上限 |

开启 `PUBLIC_MODE` 后还会在每次检查结束时清理进程级 HTTP 缓存，避免长时间运行内存无限增长。健康检查端点：`GET /healthz`。

### 用 Docker 部署（推荐）

`web/Dockerfile` 已内置 `PUBLIC_MODE=1` 及各项上限，监听 7860 端口。

```bash
# 在仓库根目录构建（注意 -f 指向 web/Dockerfile，上下文是根目录）
docker build -f web/Dockerfile -t bibchecker-web .
docker run -p 7860:7860 bibchecker-web
# 打开 http://localhost:7860
```

### 部署到 Render（免费，推荐）

仓库根目录已备好 `render.yaml` 蓝图，已内置全部加固环境变量，无需自己填。

1. 把本仓库推到 GitHub。
2. 登录 https://render.com（可用 GitHub 账号登录）→ **New → Blueprint**。
3. 选中本仓库，Render 会自动读取 `render.yaml` 创建一个 Docker Web 服务并构建。
4. 构建完成后得到一个公网地址（形如 `https://paper-bibchecker.onrender.com`），
   分享给同学即可。**无需域名、无需备案。**
5. 之后每次 `git push` 到默认分支，Render 会自动重新部署。

> **免费版特性**：无人访问约 15 分钟后实例会休眠，下次访问需冷启动
> ~30–50 秒（访客会看到加载），之后恢复正常。对 Demo 完全够用。
> Render 的 Web 服务支持流式响应，本应用逐条产出的 NDJSON 不会被超时掐断。

> **速度提示**：Render 免费区在海外——访问 arXiv/OpenAlex 等数据源更快，
> 但国内用户访问平台本身可能偏慢。若日后要照顾国内速度、且不想被休眠，
> 可迁到国内轻量应用服务器（首年约 ¥100–190，含域名，但域名需备案）。

可选：在 Render 服务的 **Environment** 面板填入 `OPENALEX_EMAIL`、
`CROSSREF_EMAIL`、`GITHUB_TOKEN` 提高数据源配额（非必需）。

## 安全说明（公网必读）

- **SSRF**：`.bib` 中的 URL 会被服务器抓取（`DirectURLProvider`）。公网务必设
  `DROP_URL_PROVIDER=1`（`PUBLIC_MODE=1` 已默认包含）。
- 请求限流、上传大小与条目数上限由上表的环境变量控制。
- HTTP 缓存 `_HTTPProvider._cache` 是进程级的；`PUBLIC_MODE` 下每次检查后自动清理。
