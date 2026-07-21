# Shell Agent 增量语义上下文摘要设计

- 日期：2026-07-13
- 状态：已确认并实现

## 目标

将原有字符截断升级为大模型语义总结，同时保留最近事件原文、会话隔离、精确目标信息和失败降级能力。

## 数据流

1. SQLite `session_messages` 继续作为原始历史来源。
2. 未覆盖的关键事件超过配置阈值时，保留最近事件，将较早事件与已有摘要交给摘要模型增量合并。
3. 摘要前后执行敏感信息清理。
4. 新摘要及覆盖的消息数量写回 `sessions`。
5. LLM 上下文由跨会话记忆、持久化语义摘要、最近事件和当前输入组成。
6. 摘要调用失败或结果无效时继续使用原有规则压缩，不阻塞任务。

## 摘要内容

摘要保留服务器别名、实际命令、路径、端口、版本、退出码、关键发现、失败与排除方向、待验证事项以及用户约束。摘要不得编造事实或包含凭证。

## 配置

- `llm.summary_model`：可选；留空复用主模型。
- `context.semantic_summary_enabled`：是否启用。
- `context.summary_trigger_events`：事件数阈值。
- `context.summary_trigger_chars`：字符数阈值。
- `context.recent_events`：保留原文的最近事件数。
- `context.summary_max_chars`：持久化摘要最大字符数。
- `context.summary_max_tokens`：单次摘要输出预算。
