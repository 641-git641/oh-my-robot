# oh-my-robot

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![GitHub REST API](https://img.shields.io/badge/GitHub-REST%20API-181717?logo=github&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![DeepSeek](https://img.shields.io/badge/AI-DeepSeek-4D6BFE)

面向 GitHub Pull Request 的只读 AI Review 服务：接收 Webhook，获取 PR Diff，调用 DeepSeek 生成结构化评审结果，并将 Markdown 总结发布到 PR 评论区。

## 快速启动

### 方式一：Python 本地启动

要求：Python 3.11+。

```bash
cd oh-my-robot
python -m venv .venv
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

macOS/Linux：

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

编辑 `.env`，至少填写：

```text
GITHUB_TOKEN=服务器端 GitHub Token
GITHUB_WEBHOOK_SECRET=Webhook 共享密钥
GITHUB_REPO_ALLOWLIST=owner/repo
DEEPSEEK_API_KEY=服务器端 DeepSeek Key
DEEPSEEK_MODEL=deepseek-chat
```

启动服务：

```bash
uvicorn reviewbot.main:create_app --factory --host 127.0.0.1 --port 8090
```

健康检查：

```text
GET http://127.0.0.1:8090/healthz
GET http://127.0.0.1:8090/readyz
```

### 方式二：Docker Compose

```bash
cd oh-my-robot
copy .env.example .env  # Windows
# macOS/Linux 使用 cp .env.example .env
docker compose up --build -d
```

Docker 服务默认监听 `8090`，并持久化：

- `./data:/app/data`：SQLite 数据库；
- `./review-rules.md:/app/review-rules.md:ro`：评审规则。

## 目前已实现功能

### GitHub 接入

- GitHub Pull Request Webhook 验签：`X-Hub-Signature-256`；
- 支持 `opened`、`reopened`、`synchronize`、`ready_for_review`；
- GitHub REST API 分页获取 PR 文件、Diff 和评论；
- 使用 Bearer Token、`application/vnd.github+json` 和 API 版本请求头；
- 按仓库白名单过滤事件；
- `X-GitHub-Delivery` 幂等去重。

### Review 执行

- DeepSeek Chat Completions JSON 模式；
- Pydantic 校验结构化评审结果；
- 校验文件路径和 Diff 新增行，丢弃无法定位的意见；
- 支持 P0～P3 优先级、置信度和测试建议；
- 评论使用 `<!-- oh-my-robot-review:<headSha> -->` marker；
- 同一个仓库、PR、head SHA 只发布一次；
- patchless 文件和超出限制的文件明确标记为不可评审。

### 可靠性与队列

- SQLite 持久化事件队列；
- `queued / running / succeeded / failed / skipped / superseded` 状态；
- 旧 SQLite Schema 无损迁移；
- 模型评审期间 PR 更新时，旧 head 不发布评论；
- 自动创建最新 head 的 refresh 任务；
- refresh 任务支持 force-push 回弹和幂等重排队；
- GitHub 限流、429、5xx 和网络错误按策略重试；
- 支持 `ROBOT_MAX_CONCURRENCY`；
- 不同 PR 并发，同一 PR 数据库级串行；
- 关停 drain 超时后自动恢复未完成任务。

### Admin 控制面

配置 `ROBOT_ADMIN_TOKEN` 后启用；未配置时不会注册 `/admin/*` 路由。

```text
GET  /admin/events
GET  /admin/events/{delivery_id}
GET  /admin/reviews
GET  /admin/metrics
POST /admin/events/{delivery_id}/replay
POST /admin/repos/{owner}/{repo}/pulls/{number}/review
```

请求鉴权：

```http
Authorization: Bearer <ROBOT_ADMIN_TOKEN>
```

- 事件、Review 查询支持筛选和 cursor 分页；
- Replay 只允许 `failed`、`skipped`、`superseded`；
- Replay 和人工触发支持 `Idempotency-Key`；
- Manual Review 会重新读取当前 head SHA；
- 支持含 `/` 的 delivery ID 详情和 Replay；
- Admin 不返回 Token、Secret、完整 Diff 或模型原始响应。

### 可观测性

- JSON 结构化日志；
- Webhook、Job、GitHub 请求、模型请求、评论发布和 Admin 操作事件；
- GitHub/model 错误分类；
- 模型输入/输出 Token 统计；
- GitHub、模型耗时统计；
- Diff 文件数、Diff 字节数、不可评审文件数；
- finding 数量、Review 等级分布；
- `/admin/metrics` 聚合队列、重试、错误和 Review 指标。

## Webhook 配置

在 GitHub 仓库 `Settings -> Webhooks` 中配置：

- Payload URL：`https://<your-host>/webhook/github`
- Content type：`application/json`
- Secret：与 `GITHUB_WEBHOOK_SECRET` 相同
- Events：选择 `Pull requests`

建议使用限定仓库的 fine-grained token。Token 需要能够读取目标仓库的 Pull Request 信息和 Diff，并创建 PR Issue Comment。

## 技术链路

```text
GitHub Pull Request Webhook
  -> FastAPI 验签、过滤和入队
  -> SQLite durable queue
  -> WorkerPool 并发调度
  -> GitHub REST API 获取 PR 和 Diff
  -> DeepSeek 结构化评审
  -> Pydantic 校验和新增行定位
  -> GitHub Issue Comment 发布总结
  -> SQLite 记录 Review、指标和终态
```

## 安全边界

- GitHub Token、Webhook Secret、Admin Token 和 DeepSeek Key 只存在服务端环境；
- GitHub Token 不进入 URL 查询参数；
- PR 描述、代码、README、注释和 Diff 都按不可信数据处理；
- 不 Clone、不执行、不修改 PR 代码；
- 不 Push、不批准、不拒绝、不合并；
- 默认只发布评审评论，不自动阻断合并；
- 错误日志、SQLite 错误字段和 Admin 响应进行敏感信息脱敏；
- 只有明确的后续阶段设计才允许引入 OMP RPC 和自动修复。

## 自定义评审规则

默认读取 `ROBOT_REVIEW_RULE_FILE`，默认值为 `review-rules.md`。模型只接收受大小限制的 PR 元信息、评审规则和 Diff。

## 配置参考

```text
GITHUB_API_BASE_URL=https://api.github.com
GITHUB_TOKEN=
GITHUB_WEBHOOK_SECRET=
GITHUB_REPO_ALLOWLIST=owner/repo

DEEPSEEK_API_BASE_URL=https://api.deepseek.com
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_THINKING_ENABLED=false
DEEPSEEK_REASONING_EFFORT=high

ROBOT_BIND_HOST=0.0.0.0
ROBOT_BIND_PORT=8090
ROBOT_DATABASE_PATH=data/review-bot.sqlite3
ROBOT_REVIEW_RULE_FILE=review-rules.md
ROBOT_MAX_DIFF_BYTES=200000
ROBOT_MAX_REVIEW_BYTES=50000
ROBOT_REQUEST_TIMEOUT_SECONDS=90
ROBOT_MAX_RETRIES=2
ROBOT_MAX_CONCURRENCY=4
ROBOT_SHUTDOWN_DRAIN_SECONDS=25
ROBOT_ADMIN_TOKEN=
ROBOT_REVIEW_ENABLED=true
```

## 验证

```bash
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
```

当前本地验证结果：

```text
55 passed
All checks passed
```

测试使用 `httpx.MockTransport` 和 FastAPI ASGI transport，不访问真实 GitHub 或 DeepSeek。
