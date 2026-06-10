# OpenDeck

OpenDeck 是一个面向本地 **OpenCode Server** 的轻量 Web 控制台。它不替代 OpenCode，也不内置模型；通过 HTTP API 连接已运行的 `opencode serve`，用于监控 session、发送一次性任务、运行带巡检的 Harness 任务。

## 功能

- **Session 监控** — 按项目目录分组展示 session，同步 `busy` / `idle` / `retry` 状态（结合 OpenCode status API、SSE 与消息探测）
- **Task** — 向新 workspace 或已有 session 发送一次性 prompt（`POST /api/dispatch`）
- **Harness** — 按 YAML/Markdown 规范创建长任务，定时检查进度并记录 check log
- **Recent Workspaces** — 最近使用的工作区，可删除
- **Session 抽屉** — 查看消息、diff、元数据；列表行支持 Archive / Dispatch

## 环境要求

| 组件 | 要求 |
|------|------|
| Python | ≥ 3.11 |
| Node.js | ≥ 18（仅构建前端时需要） |
| OpenCode CLI | 能运行 `opencode serve` |
| 操作系统 | macOS / Linux；原生文件夹选择器在 macOS 上体验最佳 |

### Python 依赖

通过 `pip install -e .` 安装：FastAPI、Uvicorn、httpx、PyYAML。

### 运行时依赖

必须先启动 OpenCode Server，且 OpenDeck 能访问其 URL（默认 `http://127.0.0.1:14096`）。

## 快速开始

### 1. 安装

```bash
git clone <repo-url> opencode-deck
cd opencode-deck

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# 编辑 .env，填入 OpenCode 用户名和密码
```

### 2. 构建前端

`frontend/dist/` 不会随 git 检出（需本地构建），否则只能看到 API 提示：

```bash
cd frontend
npm install
npm run build
cd ..
```

### 3. 启动 OpenCode Server

```bash
OPENCODE_SERVER_USERNAME=opencode \
OPENCODE_SERVER_PASSWORD=your-password \
opencode serve --hostname 127.0.0.1 --port 14096
```

### 4. 启动 OpenDeck

```bash
set -a
source .env
set +a

uvicorn app.main:app --host "${OPENDECK_HOST:-127.0.0.1}" --port "${OPENDECK_PORT:-55413}"
```

或使用入口命令（开发模式默认 `--reload`）：

```bash
opendeck
```

浏览器打开：**http://127.0.0.1:55413**

## 配置

通过环境变量配置（见 `.env.example`）：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENCODE_SERVER_URL` | OpenCode Server 地址 | `http://127.0.0.1:14096` |
| `OPENCODE_SERVER_USERNAME` | Basic Auth 用户名 | `opencode` |
| `OPENCODE_SERVER_PASSWORD` | Basic Auth 密码 | — |
| `OPENDECK_HOST` | OpenDeck 监听地址 | `127.0.0.1` |
| `OPENDECK_PORT` | OpenDeck 监听端口 | `55413` |
| `OPENDECK_DATABASE` | SQLite 路径（Harness、Recent Workspaces） | `.opendeck/opendeck.sqlite3` |
| `OPENDECK_ALLOWED_ROOTS` | 允许的工作区根目录，冒号分隔 | 空（不限制） |
| `OPENDECK_STRICT_ROOTS` | 设为 `true` 时强制白名单 | `false` |

凭据只通过环境变量注入，不要写入代码或提交到 git。

## 开发模式

前后端分离，前端热更新：

```bash
# 终端 1：后端
source .venv/bin/activate
set -a && source .env && set +a
uvicorn app.main:app --host 127.0.0.1 --port 55413 --reload

# 终端 2：前端
cd frontend && npm run dev
```

访问 **http://127.0.0.1:5173**（Vite 将 `/api/*` 代理到 `55413`）。

修改 UI 后若使用生产模式，需重新执行 `npm run build`。

## Task 与 Harness

### Task（一次性派发）

`POST /api/dispatch` — 快速操作：新建 workspace session 或向已有 session 发送 prompt。OpenDeck 不额外持久化任务状态。

UI 中 **Task** 面板支持：

- **New workspace** — 指定目录创建/使用 session
- **Existing session** — 先选 Project，再选 Session（两级下拉）

Session 列表行的 **Dispatch** 会切到 Task 面板并预选对应 session。

### Harness（长任务 + 巡检）

`POST /api/tasks` — 提交 YAML/Markdown 规范。OpenDeck 会：

1. 在目标 workspace 创建 OpenCode session
2. 发送由 spec 生成的 bootstrap prompt
3. 按间隔轮询 session 状态与最近消息
4. 更新任务状态（`pending → running → completed/failed/archived`）
5. 追加 check log，可在 UI 查看

示例见 `examples/harness-task.yaml`。Agent 回复中可使用 `STEP DONE: <n>` 与 `TASK COMPLETE` 便于进度识别。

## 分发与部署

当前推荐方式：**源码克隆 + 本地构建 + 自托管**。

```text
使用者机器
├── opencode serve     # 保持私有，默认只监听本机
└── OpenDeck (55413)   # 提供 Web UI，连接上面的 OpenCode
```

要点：

1. **克隆后必须 `npm run build`**，否则没有 Web UI
2. **`pip install` 不会打包前端静态文件**，需单独构建 `frontend/dist/`
3. **OpenDeck 无内置登录**；若暴露到内网/公网，请在前面加 VPN、反向代理或鉴权
4. **OpenCode 不建议直接公网暴露**；只暴露 OpenDeck 并限制访问更安全
5. 本地数据在 `.opendeck/`（SQLite），随部署目录迁移

## 项目结构

```text
opencode-deck/
├── app/                  # FastAPI 后端
│   ├── main.py           # 路由、静态资源、生命周期
│   ├── opencode_client.py
│   ├── session_status.py # busy/idle 解析与消息探测
│   ├── status_stream.py  # OpenCode SSE 订阅
│   ├── harness.py        # Harness 轮询执行
│   └── store.py          # SQLite 持久化
├── frontend/             # Svelte 5 + Vite
│   └── src/App.svelte
├── examples/
│   └── harness-task.yaml
├── pyproject.toml
├── .env.example
└── README.md
```

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/state` | 面板状态（sessions、tasks、status map） |
| `POST` | `/api/dispatch` | Task 派发 |
| `POST` | `/api/tasks` | 创建 Harness 任务 |
| `POST` | `/api/tasks/{id}/pause` | 暂停 Harness |
| `POST` | `/api/tasks/{id}/resume` | 恢复 Harness |
| `POST` | `/api/sessions/{id}/archive` | 归档 session |
| `DELETE` | `/api/recent-workspaces` | 从最近工作区移除 |

## 许可证

尚未指定；使用前请与仓库维护者确认。
