# WebSocket 协议契约（迁移冻结版）

本文冻结当前 `GET /ws/chat` 的 JSON 消息协议，供 Web API 拆分和 Vue 前端迁移使用。迁移阶段允许调整内部模块，不应无意改变这里列出的事件名称、字段或流程语义。

## 基本约定

- 传输：WebSocket JSON object；客户端一条请求对应零到多条服务端事件。
- `channel` 只有 `chat`、`command` 两种取值。
- `confirm_mode` 取值为 `interactive`、`auto_safe`、`full_access`、`dry_run`，缺省为 `interactive`。
- 客户端可传 `session_id`；缺省时服务端为当前连接生成 `ws_HHMMSS`。服务端在处理请求期间发出的事件会携带该 `session_id`。
- 每个服务端事件都包含 `type` 和本地时间格式的 `timestamp`（`HH:MM:SS`）。存在当前聊天轮次时还会包含 `turn_id`。
- 当前实现没有单独的请求确认（ack）。未知 `type` 被忽略；空聊天消息不产生事件；无效 JSON 或无法转为整数的 `complete.cursor` 会结束当前连接。

## 客户端入站消息

除 `type` 外，表中标有缺省值的字段均可省略。当前实现不做 Pydantic 校验，调用方应严格发送表中类型。

| `type` | 字段 |
|---|---|
| `chat`（也是缺省类型） | `message: string = ""`、`target: string = ""`、`confirm_mode`、`session_id` |
| `command` | `command: string = ""`、`target: string = ""`、`cwd: string = ""`、`confirm_mode`、`session_id` |
| `confirm` | `confirmed: boolean = false`、`channel: string = "chat"`、`task_id: string = ""`、`secondary_confirm_value: string = ""`、`session_id` |
| `cancel` | `channel: string = "command"`、`session_id` |
| `complete` | `command: string = ""`、`cursor: integer = 0`、`target: string = ""`、`cwd: string = ""`、`request_id: string = ""`、`input_id: string = ""`、`session_id` |
| `plan_confirm` | `plan_id: string = ""`、`confirmed: boolean = false`、`session_id` |
| `plan_adjust` | `plan_id: string = ""`、`instruction: string = ""`、`session_id` |

`confirm.task_id` 用于在同一会话内精确选择待确认任务；`secondary_confirm_value` 用于生产环境二次确认。`complete.request_id` 和 `input_id` 由服务端原样回传，用于丢弃过期补全响应。

## 服务端通用文本事件

| `type` | 稳定字段 | 说明 |
|---|---|---|
| `user_message` | `content: string` | 回显已接受的用户消息或方案调整请求 |
| `agent` | `content: string` | Agent 文本回复、意图或分析结论 |
| `system` | `content: string`，`channel?` | 状态说明、阻断原因或错误；部分聊天事件不带 `channel` |
| `command_error` | `content: string`、`channel: "command"` | 直接命令解析失败 |
| `confirm_prompt` | `content: string`、`channel` | 命令已进入人工确认状态 |

## 服务端结构化事件

所有下表事件还包含通用的 `type`、`timestamp`，通常也包含 `session_id`；带 `turn_id` 的事件应按轮次归并展示。

### `turn_state`

字段：`turn_id: string`、`channel: "chat"`、`status: string`、`label: string`、`active: boolean`。

当前状态值包括 `thinking`、`planning`、`waiting_confirm`、`executing`、`completed`、`failed`、`canceled`、`blocked`、`timeout`。终态的 `active` 为 `false`。

### `operation_plan`

字段：

- 标识与状态：`session_id`、`turn_id`、`channel: "chat"`、`plan_id: string`、`active: boolean`
- 文本：`intent`、`title`、`goal`、`recommended_approach`
- 列表：`impact: string[]`、`risks: string[]`、`rollback: string[]`、`verification: string[]`
- `steps: object[]`；每步包含 `command`、`intent`、`explanation`，可选 `target`

### `command_preview`

字段：

- 标识：`session_id`、`task_id`、`turn_id`、`channel`
- 命令：`command`、`target`、`cwd`、`intent`、`explanation`、`confirm_mode`
- 风险：`risk_level`（`safe|caution|dangerous|critical`）、`risk_reasons: string[]`、`risk_rules: string[]`
- 策略：`policy_blocked: boolean`、`policy_block_reason: string`
- 二次确认：`requires_secondary_confirm: boolean`、`secondary_confirm_expected`、`secondary_confirm_label`、`secondary_confirm_reason`

命令频道中的 `turn_id` 当前为空字符串；聊天频道中它与 `task_id` 相同。

### `task_step`

字段：`task_id`、`turn_id`、`channel: "chat"`、`step_index: integer`、`total_steps: integer`、`status`、`content`、`intent`、`command`、`target`。

当前状态值包括 `pending`、`success`、`partial`、`failed`、`timeout`、`complete`。该事件只用于多步骤或工作流式聊天任务。

### `execution_status`

字段：`channel`、`status`、`content`。当前状态值包括 `running`、`stopping`、`success`、`partial`、`failed`、`timeout`、`canceled`。

### `execution_result`

字段：`channel`、`task_id`、`turn_id`、`success: boolean`、`partial_success: boolean`、`output: string`、`exit_code: integer`、`timed_out: boolean`、`command`、`target`、`cwd`。

`output` 合并标准输出与标准错误；二者同时存在时使用 `\n[stderr]\n` 分隔。`exit_code` 缺失时当前实现发送 `1`。

### `completion_result`

字段：`channel: "command"`、`request_id`、`input_id`、`kind: "command"|"path"`、`start: integer`、`end: integer`、`prefix: string`、`candidates: string[]`、`common_prefix: string`。候选最多 80 项。

## 关键事件顺序

- 交互式直接命令：`command_preview` → `confirm_prompt`；确认后 `execution_status(running)` → `execution_status(终态)` → `execution_result`。
- `dry_run`：`command_preview` → `system`，不发送执行结果。
- `auto_safe` 安全命令：`command_preview` → `system` → `execution_status(running)` → 终态状态与结果。
- 聊天命令会穿插 `turn_state`；前端应按 `type` 和 `turn_id` 更新状态，不应依赖所有流程拥有完全相同的文本事件序列。
- 操作方案：`turn_state(planning)` → `operation_plan(active=true)`；确认、取消或调整由对应入站事件继续驱动。

## 兼容性规则

迁移期间新增可选字段或新增事件类型是向后兼容的；删除/重命名字段、改变字段类型、改变既有状态含义或关键事件顺序均需显式升级协议与前端解析器。前端必须忽略未知出站事件和未知附加字段。
