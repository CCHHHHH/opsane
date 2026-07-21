# Shell Agent 知识、记忆与服务画像设计

- 版本：v1.0
- 日期：2026-07-13
- 状态：已完成交互设计确认，待用户评审文档

## 1. 背景

Shell Agent 已具备三类上下文能力：

- 会话上下文：保存当前聊天中的用户输入、命令、结果和摘要。
- 全局记忆：保存跨会话可复用的信息，当前存储在 SQLite 的 `global_memories` 表中。
- 服务画像：保存服务与服务器、目录、端口、健康检查和运维命令的结构化关系，当前存储在 `config/inventory.yaml` 中。

全局记忆和服务画像会记录部分相似事实，但可靠性和用途不同。如果直接合并，LLM 推断可能被误认为正式配置；如果完全割裂，又会出现重复维护和冲突。因此采用“分层管理、候选晋升”的设计。

## 2. 目标与非目标

### 2.1 目标

1. 明确会话上下文、全局记忆和服务画像的职责边界。
2. 从成功任务中自动发现可复用知识，但不让 LLM 静默修改正式画像。
3. 允许用户确认、编辑或拒绝画像更新候选。
4. 为每条知识保留来源、证据、置信度和时效信息。
5. 在 LLM 调用前只检索与当前问题相关的知识，减少上下文噪声。
6. 发生冲突或目标不确定时阻止错误写操作。
7. 永远不把密码、Token、私钥等秘密写入记忆、画像或 LLM 上下文。

### 2.2 非目标

- 本阶段不建设完整企业 CMDB。
- 不引入向量数据库；先使用现有 SQLite 和结构化检索完成闭环。
- 不自动扫描所有服务器建立画像。
- 不允许未经用户确认的画像字段覆盖。

## 3. 核心设计

### 3.1 三层知识模型

```text
当前会话上下文
    ↓ 任务完成后提取有证据的知识
全局记忆
    ↓ 稳定服务事实生成画像候选
画像更新候选
    ↓ 用户确认或编辑
服务画像
```

各层职责如下：

| 层级 | 主要内容 | 生命周期 | 是否可直接驱动写操作 |
|---|---|---|---|
| 会话上下文 | 本轮消息、命令、结果、临时指代 | 单会话 | 仅在目标明确时 |
| 全局记忆 | 跨会话事实、经验、用户偏好 | 可过期 | 目标不明确时不可 |
| 画像候选 | 服务画像字段的结构化增量 | 待审核 | 不可 |
| 服务画像 | 正式服务配置和运维入口 | 长期、可验证 | 可以 |

### 3.2 读取优先级

解析同一信息时采用以下优先级：

1. 用户在当前请求中明确指定的信息。
2. 已确认且未标记冲突的服务画像。
3. 有成功执行证据且未过期的全局记忆。
4. 当前会话上下文中的临时推断。
5. LLM 自身推断。

成功命令输出可以在本次任务中作为最新证据，但不能静默覆盖服务画像。它与画像冲突时必须产生冲突记录或画像更新候选。

## 4. 数据模型

### 4.1 全局记忆

在现有 `global_memories` 表上增加以下字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | TEXT | `fact`、`procedure`、`preference`、`observation` |
| `status` | TEXT | `inferred`、`confirmed`、`promoted`、`stale`、`conflicted` |
| `source_task_id` | TEXT | 来源任务 |
| `source_event_id` | TEXT | 来源事件或命令结果 |
| `observed_at` | TEXT | 事实观察时间 |
| `expires_at` | TEXT | 失效时间，可为空 |
| `evidence_summary` | TEXT | 不含秘密的证据摘要 |
| `fingerprint` | TEXT | 用于去重和冲突检测 |

保留现有 `subject`、`predicate`、`value`、`target`、`confidence`、`source_session_id` 和 `source` 字段。

`fingerprint` 由规范化后的 `type + subject + predicate + target` 组成，不包含 `value`。相同指纹但不同值表示潜在冲突。

### 4.2 画像更新候选

新增 `service_profile_candidates` 表：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT | 候选 ID |
| `service_id` | TEXT | 目标服务，可为空，表示建议新建服务 |
| `service_name` | TEXT | 服务名称 |
| `proposed_changes` | TEXT/JSON | 字段级变更 |
| `before_snapshot` | TEXT/JSON | 生成候选时的画像快照 |
| `evidence` | TEXT/JSON | 来源任务、命令、目标和摘要 |
| `confidence` | REAL | 候选置信度 |
| `fingerprint` | TEXT | 候选去重键 |
| `status` | TEXT | `pending`、`accepted`、`rejected`、`expired` |
| `source_memory_ids` | TEXT/JSON | 关联记忆 ID |
| `source_task_id` | TEXT | 来源任务 |
| `created_at` | TEXT | 创建时间 |
| `reviewed_at` | TEXT | 审核时间 |
| `reviewed_by` | TEXT | 审核者，当前可固定为 `local-user` |

拒绝的候选保留记录和指纹。相同证据不重复生成候选；只有值变化、来源任务变化或证据显著增强时才允许重新提出。

### 4.3 服务画像扩展

现有 `ServiceProfile` 增加：

| 字段 | 类型 | 说明 |
|---|---|---|
| `config_paths` | list[str] | 常用配置文件 |
| `runtime` | str | systemd、docker、tomcat、standalone 等 |
| `version` | str | 最近确认版本 |
| `last_verified_at` | str | 最后验证时间 |
| `verification_status` | str | `verified`、`stale`、`conflicted`、`unknown` |
| `source_task_id` | str | 最近一次更新来源 |
| `revision` | int | 乐观并发版本号 |

画像仍保存在 `config/inventory.yaml`，保持可读、可备份和便于手工维护。SQLite 只保存记忆、候选、审核状态和来源证据。

## 5. 自动学习流程

### 5.1 触发时机

只在一个完整 Shell Agent 任务进入终态后运行知识提取：

- `completed`：允许提取成功步骤中有证据的事实；任务包含失败步骤时，只提取明确成功且不依赖失败步骤的事实。
- `failed`、`canceled`、`blocked`、`timeout`：默认不提取服务事实；可以提取非敏感故障经验，但置信度较低。

方案生成、命令预览和 LLM 解释本身不能作为事实来源。

### 5.2 提取管线

1. `TaskEvidenceCollector` 收集用户原始目标、实际目标服务器、成功命令结果和最终总结。
2. `SecretRedactor` 清理密码、Token、私钥、连接串敏感参数和临时口令。
3. `KnowledgeExtractor` 调用 LLM 输出结构化知识候选，不输出自由文本。
4. `KnowledgePolicy` 校验类型、证据、置信度、目标服务器和字段合法性。
5. `MemoryRepository` 去重、合并、标记冲突并写入全局记忆。
6. `ProfileCandidateBuilder` 将稳定服务事实转换为画像字段变更。
7. 页面显示“学习到 N 条信息，生成 M 条待确认画像更新”。

### 5.3 可提取与禁止提取内容

允许提取：

- 服务与服务器绑定关系。
- 部署目录、日志目录、配置文件、端口、健康检查地址。
- 服务版本、运行方式和经过验证的启停命令。
- 已验证的故障现象和处理经验。
- 用户明确表达的长期操作偏好。

禁止提取：

- 密码、Token、私钥、会话 Cookie、临时口令。
- 未执行方案中的路径、版本和命令。
- LLM 猜测但没有命令结果或用户陈述支持的事实。
- 无长期价值的 PID、瞬时 CPU、瞬时磁盘值和任务运行状态。

## 6. 画像候选审核流程

候选详情必须展示：

- 服务名称和明确目标服务器。
- 修改前与修改后的字段差异。
- 来源任务、执行时间和证据摘要。
- 置信度和潜在冲突。

用户可以：

- 确认写入：应用原始变更。
- 编辑后写入：修改字段后再次校验并应用。
- 忽略：标记为 `rejected`，阻止相同证据重复提示。

确认写入使用 `revision` 做乐观并发检查。如果画像在候选生成后被修改，接口返回冲突，页面要求用户根据最新画像重新审核。

写入成功后：

1. 原子更新 `config/inventory.yaml`。
2. 重新加载 Runtime 中的服务画像。
3. 将来源记忆标记为 `promoted`。
4. 将候选标记为 `accepted`。
5. 写入包含修改前后快照的审计日志。

任一步骤失败时，不允许出现“画像已更新但候选仍待确认”的半完成状态。文件写入采用临时文件校验后替换；SQLite 状态只在画像更新成功后提交。

## 7. KnowledgeResolver

新增统一的 `KnowledgeResolver`，聊天、Skill 和任务工作流只能通过它读取长期知识，禁止各模块直接拼接全量记忆或服务画像。

### 7.1 输入

```text
session_id
user_message
explicit_target
detected_entities
operation_type       read | write | destructive
```

### 7.2 输出

```text
resolved_target
target_source        user | profile | memory | session | unresolved
matched_profiles
matched_memories
session_context
conflicts
requires_target_confirmation
llm_context
```

`llm_context` 必须是精简且带来源标签的文本，不包含完整数据库记录，也不包含真实 SSH 凭证。

### 7.3 检索策略

1. 从用户输入识别服务器别名、服务名、路径和操作意图。
2. 服务画像按 ID、名称、标签和绑定服务器精确匹配。
3. 全局记忆使用现有关键词检索，并按目标、状态、置信度和时间重新排序。
4. 读取当前会话摘要及最近相关任务，不加载完整长输出。
5. 合并并执行冲突、过期和目标确定性检查。
6. 最多注入与当前请求相关的少量画像和记忆，避免全量系统提示词持续膨胀。

## 8. 冲突与目标安全

### 8.1 冲突规则

- 服务画像和普通记忆冲突：画像作为默认值，记忆标记为 `conflicted`。
- 当前成功命令结果和画像冲突：本次任务可使用执行结果，但画像标记为待验证并生成更新候选。
- 两条记忆指纹相同、值不同：比较证据等级、观察时间和置信度；不能可靠判断时两条都标记为冲突。
- 用户明确纠正：当前请求立即采用用户值，并生成待确认的画像变更或已确认记忆。

### 8.2 目标确定性

- 所有方案、任务、步骤、命令预览和结果必须显示目标服务器别名。
- 写操作仅凭普通记忆推断目标时，必须先确认目标。
- 只读操作可以对少量候选服务器执行安全探测，再确定目标。
- “完全访问”只改变命令风险确认策略，不改变目标确定性要求。
- 出现多个同名服务且无法区分环境时，禁止自动选择。

## 9. 时效策略

默认策略如下，后续可配置：

| 信息 | 默认策略 |
|---|---|
| CPU、内存、PID、运行状态 | 不保存或 1 小时过期 |
| 服务版本、监听端口 | 30 天后标记 `stale` |
| 部署目录、日志目录、配置路径 | 90 天后提示重新验证 |
| 服务与服务器绑定 | 长期有效，相关命令失败时立即待验证 |
| 用户偏好 | 长期有效，直到用户修改或删除 |
| 密码、Token、私钥 | 永不保存 |

过期不等于删除。过期知识可以辅助生成验证命令，但不能单独作为高风险写操作的目标依据。

## 10. API 设计

### 10.1 记忆

- `GET /api/memories`：支持 `type`、`status`、`target` 和关键词筛选。
- `POST /api/memories`：用户手工创建已确认记忆。
- `PUT /api/memories/{id}`：编辑、确认或标记过期。
- `DELETE /api/memories/{id}`：软删除。

### 10.2 画像候选

- `GET /api/service-profile-candidates`
- `GET /api/service-profile-candidates/{id}`
- `POST /api/service-profile-candidates/{id}/accept`
- `POST /api/service-profile-candidates/{id}/reject`
- `POST /api/service-profile-candidates/{id}/accept-edited`

审核接口返回更新后的服务画像、候选状态和 Runtime 刷新结果。

### 10.3 冲突与验证

- `GET /api/knowledge/conflicts`
- `POST /api/services/{id}/verify`：创建只读验证任务，不在请求线程内直接执行 SSH。

## 11. 页面设计

“记忆”页面包含三个页签：

1. 已确认记忆：搜索、筛选、编辑、删除和查看来源。
2. 画像候选：查看字段差异、证据，确认、编辑或忽略。
3. 冲突与过期：查看冲突值、来源、最后验证时间并发起验证。

“服务画像”详情增加：

- 最后验证时间。
- 验证状态。
- 最近来源任务。
- 当前 revision。
- 相关全局记忆和待审核候选数量。

聊天区域只显示折叠式学习摘要，不逐条插入知识卡片，避免干扰任务结果阅读。

## 12. 错误处理

- LLM 知识提取超时：不影响原任务终态和最终总结，只记录后台学习失败。
- LLM 返回非法结构：丢弃本次提取并记录审计，不保存自由文本。
- 敏感信息检测命中：删除该知识候选并记录不含原值的安全事件。
- 画像 YAML 校验失败：不替换原文件，不改变候选和记忆状态。
- Runtime 刷新失败：恢复原画像文件，候选保持 `pending`。
- 候选 revision 冲突：返回 HTTP 409，要求基于最新画像重新审核。
- 服务或服务器已删除：候选标记为 `expired`，不得写入悬空引用。

## 13. 迁移与兼容

1. 通过 SQLite migration 为 `global_memories` 增加可空字段并填充默认值。
2. 现有记忆迁移为 `type=fact`、`status=confirmed`，保留原数据。
3. 创建 `service_profile_candidates` 表和索引。
4. 为现有服务画像补充默认的 `revision=1`、`verification_status=unknown`。
5. 保留当前记忆 API 的基础行为，新字段均提供默认值。
6. KnowledgeResolver 上线后移除全量服务画像系统提示词，仅保留产品级规则提示。

## 14. 测试与验收

### 14.1 单元测试

- 记忆指纹、去重、状态和过期计算。
- 同指纹不同值的冲突检测。
- 敏感信息过滤，包括密码参数、Token、私钥和连接串。
- 画像候选字段白名单和服务器引用校验。
- KnowledgeResolver 优先级和目标确认判断。
- 服务画像 revision 并发控制。

### 14.2 集成测试

- 成功安装服务后生成记忆和画像候选。
- 用户确认候选后 YAML、Runtime、记忆、候选和审计状态一致。
- 拒绝候选后相同证据不重复提示。
- 画像与最新执行结果冲突时生成更新候选，不静默覆盖。
- LLM 提取失败不影响任务完成状态和最终总结。
- 写操作仅由普通记忆推断目标时要求用户确认。
- “完全访问”模式仍不能绕过目标不确定检查。

### 14.3 前端验收

- 三个记忆页签可正常筛选和刷新。
- 候选卡片明确显示服务、目标服务器、修改前后和证据。
- 用户可编辑后确认，错误字段能在提交前提示。
- 聊天任务完成后只显示简洁学习摘要。
- 页面刷新后候选、冲突和审核状态保持一致。

## 15. 实施顺序

1. 数据库迁移、扩展数据模型和 Repository。
2. SecretRedactor、TaskEvidenceCollector 和 KnowledgePolicy。
3. KnowledgeExtractor 与画像候选生成。
4. 候选审核 API、画像原子写入和 Runtime 刷新。
5. KnowledgeResolver 及聊天、Skill、任务工作流接入。
6. 记忆页面和服务画像详情调整。
7. 回归测试、旧数据迁移和文档更新。

## 16. 验收结论

设计完成后，Shell Agent 应满足以下核心行为：

- 能从成功任务中学习，但不会把 LLM 猜测当成正式配置。
- 能跨会话定位服务，同时明确说明目标服务器和信息来源。
- 稳定事实只有经过用户确认才进入服务画像。
- 冲突、过期和目标不确定不会被权限模式绕过。
- 记忆学习失败不会污染任务状态或阻止用户获得最终结论。
