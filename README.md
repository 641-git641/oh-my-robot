# oh-my-robot

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![GitHub REST API](https://img.shields.io/badge/GitHub-REST%20API-181717?logo=github&logoColor=white)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite&logoColor=white)
![DeepSeek](https://img.shields.io/badge/AI-DeepSeek-4D6BFE)

面向 GitHub Pull Request 的 AI Review 服务：接收 Webhook，在 PR 评论区发布评审结果。默认只读；接入 roboomp 后，可为 Issue/PR 对话提供授权的修复流程。

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
- `./review-rules.md:/app/review-rules.md:ro`：基础评审规则；
- `./review-rules.toml:/app/review-rules.toml:ro`：仓库和路径评审规则。

## 部署方式

### 本地开发部署

适合调试 Webhook、Review Prompt 和 SQLite 状态：

```bash
python -m pip install -e ".[dev]"
uvicorn reviewbot.main:create_app --factory --host 127.0.0.1 --port 8090
```

本地服务默认只监听 `127.0.0.1`，不会自动暴露到公网。需要接收 GitHub Webhook 时，使用反向代理或隧道将公网 HTTPS 请求转发到：

```text
POST http://127.0.0.1:8090/webhook/github
```

### Docker Compose 部署

```bash
docker compose up --build -d
docker compose logs -f github-review-bot
```

停止服务：

```bash
docker compose down
```

Compose 会将服务运行在容器内，将 `./data` 作为持久化目录，并以只读方式挂载 `review-rules.md` 和 `review-rules.toml`。

### 公网生产部署

推荐结构：

```text
GitHub Webhook
  -> HTTPS Reverse Proxy
  -> /webhook/github
  -> oh-my-robot:8090
```

部署要求：

- Webhook 公网入口必须使用 HTTPS；
- 只公开 `/webhook/github`；
- `/healthz`、`/readyz` 和 `/admin/*` 建议限制为内网或可信来源；
- `GITHUB_WEBHOOK_SECRET`、`GITHUB_TOKEN`、`DEEPSEEK_API_KEY` 不提交到 Git；
- 持久化 `data/`，避免容器重启丢失队列和 Review 记录；
- 使用 fine-grained GitHub Token，只授予目标仓库所需权限；
- Admin 控制面必须配置独立的 `ROBOT_ADMIN_TOKEN`。

生产部署前，建议创建一个无敏感信息的测试 PR，确认 Webhook 返回 `202`，并确认 PR 评论只出现一条。

## 功能

### Pull Request Review

- 创建或更新 Pull Request 后，机器人在 PR 评论区发布 AI Review；
- 评论给出总体结论、问题等级、文件位置、影响、修改建议和测试建议；
- 可以在具体代码行看到对应的问题评论；
- PR 页面可以看到 `oh-my-robot review` Check Run；
- 同一个提交不会重复发布相同 Review；
- PR 在评审期间产生新提交时，只发布最新提交的结论；
- Draft、关闭的 PR 或不在仓库白名单中的 PR 不会发布 Review；
- 超大 Diff、二进制文件和无法读取的文件会在评论中明确说明，避免让用户误以为已完整检查。

### Deep Review（可选）

- 在普通 Review 之外，Review 可以结合 PR 相关仓库上下文，分析跨文件影响；
- 适合需要跨文件理解的 PR；
- Deep Review 仍然只给出评审意见，不修改代码、不 Push、不创建 PR；
- OMP 不可用或超时时，可以继续使用默认 Fast Review。

### Issue / PR 对话与授权修复（可选）

配置 roboomp 后，用户可以在 Issue 或 PR 评论中：

- 请求解释问题或继续跟进评审；
- 获取 Issue/PR 的对话式回复；
- 在获得维护者授权后请求复现、测试和修复；
- 查看机器人创建的 Draft PR。

授权修复由 roboomp 独立执行；未配置 roboomp 时，oh-my-robot 只处理 Pull Request Review。

### 管理员操作（可选）

配置 Admin Token 后，管理员可以：

- 查看 Webhook、Review 和处理结果；
- 重试失败任务；
- 手动触发当前提交的 Review；
- 查看服务处理指标。

## 共存部署：oh-my-robot + roboomp

当前仓库保留两个职责清晰的服务：

```text
oh-my-robot
  -> Pull Request Review

roboomp
  -> Issue/PR 对话
  -> 维护者授权的复现、测试、修复和 Draft PR
```

GitHub Webhook 统一发送到 oh-my-robot。配置 `ROBOT_ROBOOMP_WEBHOOK_URL` 后，Issue、PR 评论和 PR Review 事件会转发给 roboomp；不配置时，这些事件会被跳过。

```text
ROBOT_ROBOOMP_WEBHOOK_URL=http://127.0.0.1:6543/webhook/github
ROBOT_ROBOOMP_TIMEOUT_SECONDS=15
```

roboomp 源码已复制到当前仓库的 `src/robomp`，后续 Agent 能力在此基础上二开。roboomp 需要在具备 `omp`、Git 和 Linux 进程隔离能力的运行环境中单独启动；oh-my-robot 不转发 GitHub Token。

## Agent 能力现状

### 已接入

- PR Review：Fast Review 和可选 Deep Review；
- Issue/PR 评论转发；
- roboomp 对话、维护者授权修复和 Draft PR 能力的共存入口。

### 完整 Agent 使用前提

要让用户实际使用 Issue/PR 对话和修复能力，还需要：

1. 使用 `.env.roboomp.example` 准备 roboomp 配置；
2. 从当前仓库源码启动 `roboomp serve`；
3. 将 `ROBOT_ROBOOMP_WEBHOOK_URL` 指向 roboomp 的 Webhook；
4. 在 GitHub Webhook 中启用 Pull Requests、Issue comments、Issues 和 Pull request reviews；
5. 使用同一个 Webhook Secret，并在 roboomp 侧配置维护者授权和代码写入权限。

```bash
PYTHONPATH=src python -m robomp serve
```

完成后，用户可以在 Issue 或 PR 评论中请求解释、继续跟进、复现和测试；获得授权后，机器人才能修改代码并创建 Draft PR。未完成上述配置时，oh-my-robot 仍可独立提供 PR Review，但不会提供修复 Agent。

## 可选模式配置

### Deep Review

默认使用 Fast Review。需要跨文件理解时，可以安装 Deep Review 依赖并配置：

```bash
python -m pip install -e ".[dev,deep]"
```

```text
ROBOT_REVIEW_MODE=deep
ROBOT_OMP_COMMAND=omp
ROBOT_OMP_MODEL=你的模型 ID
ROBOT_OMP_SANDBOXED=true
```

Deep Review 只提供评审意见，不修改代码。配置不完整或 OMP 不可用时，使用默认 Fast Review。

## Webhook 配置

在 GitHub 仓库 `Settings -> Webhooks` 中配置：

- Payload URL：`https://<your-host>/webhook/github`
- Content type：`application/json`
- Secret：与 `GITHUB_WEBHOOK_SECRET` 相同
- Events：选择 `Pull requests`、`Issue comments`、`Issues` 和 `Pull request reviews`

建议使用限定仓库的 fine-grained token。Token 需要能够读取目标仓库的 Pull Request 信息和 Diff，并创建 PR Issue Comment；roboomp 的写操作权限按其部署文档单独配置。

## 评审范围定制

部署者可以为所有 PR、指定仓库或指定路径增加评审要求。效果会体现在 Review 评论中的问题和建议；规则文件由服务端受信任地挂载，不读取 PR 分支中的规则文件。

配置文件：`review-rules.toml`

```toml
rules = "全局规则"

[paths."src/auth/**"]
rules = ["检查授权", "检查敏感信息"]

[repositories."owner/repo"]
rules = "保持该仓库的公开 API 兼容"
```

## 安全边界

- GitHub Token、Webhook Secret、Admin Token 和 DeepSeek Key 只存在服务端环境；
- GitHub Token 不进入 URL 查询参数；
- PR 描述、代码、README、注释和 Diff 都按不可信数据处理；
- Fast/Deep Review 不修改代码、不 Push、不批准、不拒绝、不合并；
- 默认只发布评审评论，不自动阻断合并；
- 错误日志、数据库错误字段和 Admin 响应进行敏感信息脱敏；
- OMP 深度模式只读；roboomp 写操作必须经过自身授权、工作区和 Git gate。

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
ROBOT_REVIEW_PATH_RULE_FILE=review-rules.toml
ROBOT_MAX_DIFF_BYTES=200000
ROBOT_MAX_REVIEW_BYTES=50000
ROBOT_REQUEST_TIMEOUT_SECONDS=90
ROBOT_MAX_RETRIES=2
ROBOT_MAX_CONCURRENCY=4
ROBOT_SHUTDOWN_DRAIN_SECONDS=25
ROBOT_ADMIN_TOKEN=
ROBOT_REVIEW_MODE=fast
ROBOT_OMP_COMMAND=omp
ROBOT_OMP_MODEL=
ROBOT_DEEP_REVIEW_TIMEOUT_SECONDS=300
ROBOT_OMP_SANDBOXED=false
ROBOT_ROBOOMP_WEBHOOK_URL=
ROBOT_ROBOOMP_TIMEOUT_SECONDS=15
ROBOT_REVIEW_ENABLED=true
```

## 验证

```bash
PYTHONPATH=src python -m pytest -q
python -m ruff check src tests
```

当前本地验证结果：

```text
72 passed
All checks passed
```

测试使用 `httpx.MockTransport` 和 FastAPI ASGI transport，不访问真实 GitHub 或 DeepSeek。
