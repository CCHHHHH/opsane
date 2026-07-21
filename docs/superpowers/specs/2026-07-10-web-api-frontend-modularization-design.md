# Web API 与前端模块化设计

日期：2026-07-10

## 1. 目标

将当前约 4 千行的 `shell_agent/web/api.py` 和约 6.5 千行的
`shell_agent/web/static/index.html` 拆成职责清晰、可独立测试的后端模块与
Vue 3 前端工程，同时保持现有 HTTP/WebSocket 协议和产品行为不变。

本次采用渐进式替换，不进行前后端一次性重写。迁移期间旧版页面继续可用，
新版页面先挂载到 `/next/`，完成协议对齐和功能验收后再切换根页面。

## 2. 约束

- 使用 Vite、TypeScript、Vue 3、Pinia 和 Vue Router。
- Vue Router 使用 hash 模式，避免增加 FastAPI SPA fallback 路由。
- `/api/*`、`/ws/chat`、WebSocket 事件名称、字段和发送顺序保持兼容。
- 同一阶段不同时修改前端 WebSocket 消费逻辑和后端 WebSocket 协议。
- Python wheel 携带已构建的前端，运行环境不依赖 Node.js。
- `Runtime` 及其 pending、running task、session context 保持单实例。
- 不借本次拆分调整鉴权、凭证模型、业务规则或 UI 视觉设计。

## 3. 方案选择

### 3.1 采用：渐进式替换

先冻结协议并拆分低耦合 REST 路由，再搭建 Vue 新入口并迁移页面；聊天和命令
终端完成后，最后拆分后端 WebSocket 状态机。每个阶段都可以独立回归和回退。

### 3.2 未采用：一次性重写

一次性重写能更快得到整齐目录，但聊天确认、任务恢复、终端补全和并发任务状态
具有较多隐含协议，行为漂移风险过高。

### 3.3 未采用：机械文件切割

只搬运函数不会消除全局状态、DOM 直连和循环调用，文件数量增加但维护性改善有限。

## 4. 后端目标结构

```text
shell_agent/web/
├── app.py
├── api.py
├── schemas.py
├── runtime.py
├── routes/
│   ├── state.py
│   ├── sessions.py
│   ├── artifacts.py
│   ├── inventory.py
│   ├── credentials.py
│   ├── config.py
│   ├── memories.py
│   ├── skills.py
│   ├── safety.py
│   └── audit.py
├── services/
│   ├── config_files.py
│   └── session_state.py
└── ws/
    ├── router.py
    ├── transport.py
    ├── chat.py
    ├── plans.py
    ├── commands.py
    └── execution.py
```

### 4.1 模块职责

- `app.py`：FastAPI 生命周期、路由注册和静态资源挂载。
- `api.py`：聚合各子 router；迁移期保留必要的兼容包装器，最终控制在 150 行内。
- `schemas.py`：集中 Pydantic HTTP 请求与响应模型。
- `routes/*`：只处理 HTTP 输入输出，不承载跨流程编排。
- `services/config_files.py`：配置文件读写、脱敏、验证和路径解析。
- `services/session_state.py`：pending command、task、context、cwd 和持久化辅助。
- `ws/router.py`：接收 WebSocket 消息并按 `type` 分派。
- `ws/transport.py`：唯一的 `ConnectionManager`、ContextVar 和统一发送函数。
- `ws/chat.py`：聊天入口、Skill、记忆和 LLM 分流。
- `ws/plans.py`：操作方案的生成、调整和确认。
- `ws/commands.py`：命令预览、确认、补全、安全策略和 pending 状态。
- `ws/execution.py`：后台执行、结果分析、连续排查和任务收尾。

依赖方向固定为：

```text
app/api → routes/ws → services → runtime/storage/core
```

任何子模块都不得反向导入 `web.api`。`_SEND_SESSION_ID`、`_SEND_TURN_ID` 和
ConnectionManager 只能在 `ws/transport.py` 定义一份。

## 5. 前端目标结构

```text
shell_agent/web/frontend/
├── package.json
├── vite.config.ts
├── tsconfig.json
└── src/
    ├── main.ts
    ├── App.vue
    ├── api/
    │   ├── http.ts
    │   ├── websocket.ts
    │   └── protocol.ts
    ├── stores/
    │   ├── app.ts
    │   ├── sessions.ts
    │   ├── chat.ts
    │   ├── terminal.ts
    │   └── inventory.ts
    ├── pages/
    │   ├── ChatPage.vue
    │   ├── TerminalPage.vue
    │   ├── ServersPage.vue
    │   ├── ConfigPage.vue
    │   ├── MemoriesPage.vue
    │   └── AuditPage.vue
    ├── components/
    │   ├── sessions/
    │   ├── chat/
    │   ├── terminal/
    │   ├── operation/
    │   └── common/
    └── styles/
        ├── tokens.css
        ├── base.css
        └── components.css
```

Vite 在迁移阶段输出到 `shell_agent/web/static/next/`。切换完成后改为输出到
`shell_agent/web/static/` 根目录，并删除旧版单文件页面。

## 6. 前端数据流

```text
Vue page/component
    → Pinia action
    → HTTP client / WebSocket client
    → FastAPI route / WebSocket router
    → service / workflow
    → Runtime / Storage / Executor / LLM
```

- Vue 组件不得直接调用 `fetch` 或创建 WebSocket。
- `api/protocol.ts` 使用可辨识联合类型定义全部 WebSocket 入站和出站事件。
- `api/websocket.ts` 负责连接、重连、消息解析、未知事件和连接状态。
- Pinia Store 负责业务状态、事件归并、重试和用户 action。
- 组件只通过 props、emits 和 Store 渲染状态，不直接拼接全局 DOM。
- 不再向 `window` 挂载页面函数，也不使用 HTML 内联 `onclick`。

## 7. 迁移阶段

### 阶段 0：冻结协议

- 整理现有 HTTP endpoint、请求和响应字段。
- 整理所有 WebSocket 消息类型、字段及关键事件顺序。
- 为聊天、确认、执行、取消、方案、补全和恢复建立协议回归测试。

### 阶段 1：拆分 REST 后端

1. 提取 `schemas.py`。
2. 依次迁移 audit、memories、safety、skills、credentials/config、inventory、
   artifacts、state/sessions。
3. 每迁移一个 router 就运行对应测试和完整 `tests/test_web.py`。
4. 暂时保留 `api.py` 兼容包装器；测试迁移后再删除。

### 阶段 2：建立 Vue 新入口

- 初始化 Vite、TypeScript、Vue 3、Pinia、Vue Router、Vitest。
- 建立基础布局、协议类型、HTTP/WS client 和公共 Store。
- 设置 Vite `base=/next/`，构建目录为 `shell_agent/web/static/next/`。
- 同步扩展 setuptools package data，使 wheel 在迁移期也包含 `/next/` 的全部构建资源。
- 在 `/next/` 提供新版入口，不改变旧版根页面。

### 阶段 3：迁移低状态页面

按 Audit、Memories、Config/Skills、Servers/Services/Credentials 顺序迁移。
这些页面以 REST 为主，先用于验证组件、Store 和构建部署链。

### 阶段 4：迁移聊天页

迁移会话列表、历史分页、任务步骤、操作方案、确认卡片、结果展示和断线恢复。
此阶段继续使用原 WebSocket 后端实现。

### 阶段 5：迁移命令终端

最后迁移多服务器缓冲区、cwd、命令历史、滚屏、补全、执行取消和结果恢复。

### 阶段 6：拆分 WebSocket 后端

按 transport/session state、chat、plans、commands、execution 顺序拆分。
一次只迁移一个工作流，不改变前端协议，同时保留新旧两套 UI 做回归。

### 阶段 7：切换与清理

- 通过完整验收后将 Vue 构建产物切换为根页面。
- 删除旧版 `index.html`、内联 CSS/JS、兼容包装器和废弃测试入口。
- 更新 wheel package data 和产品文档。

## 8. 错误处理

- 迁移期间保留现有 HTTP 状态码及 `{error: ...}` 格式。
- HTTP client 将网络错误、协议错误和业务错误转换为统一前端错误对象。
- WebSocket 未知事件只记录并忽略，不导致连接崩溃。
- WebSocket 重连后由 Store 根据 session/task 接口恢复权威状态。
- 后端 workflow 的异常必须在边界转换成当前协议事件，并完成任务状态和审计收尾。
- 任何不确定迁移都优先保持旧行为，不在重构中顺手改变业务语义。

## 9. 测试策略

### Python

- 保留并持续运行现有 pytest 测试。
- REST 路由优先通过 TestClient 公共接口测试，减少对私有函数的导入。
- WebSocket 使用 FakeWebSocket 验证事件类型、字段和顺序。
- 每个 workflow 使用 fake Runtime、Executor、LLM 和 Storage 做独立测试。

### TypeScript/Vue

- Vitest 测试协议解析、HTTP client、WebSocket client、Store 和工具函数。
- Vue Test Utils 测试确认卡片、任务步骤、会话恢复、终端状态组件。
- Playwright 覆盖创建会话、安全命令、危险命令拒绝、历史恢复和服务器配置。

### 构建与发布

CI 顺序固定为：

```text
npm ci
→ npm test
→ npm run build
→ pytest
→ wheel build
→ wheel 静态资源检查
```

wheel 验证必须确认不安装 Node.js 也能启动 FastAPI 并打开新版页面。

## 10. 验收标准

- `web/api.py` 最终少于 150 行，只负责聚合和必要兼容。
- 原始单文件 `index.html` 被删除，HTML/CSS/JS 由 Vite 生成。
- Vue 组件中不存在直接 fetch、直接 WebSocket、内联事件或 `window.xxx`。
- 后端模块不存在对 `web.api` 的反向导入。
- `_SEND_SESSION_ID`、`_SEND_TURN_ID` 和 ConnectionManager 均只有一个实例来源。
- 所有现有 Python 测试继续通过。
- 关键 HTTP/WS 协议在新旧页面之间一致。
- wheel 包含完整 Vue 构建产物，Python 运行环境不依赖 Node.js。
- 新版根页面验收通过后才允许删除旧版页面。
