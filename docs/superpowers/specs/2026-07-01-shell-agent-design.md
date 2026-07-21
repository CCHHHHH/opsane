# Shell Agent 设计文档

- **版本**: v1.0
- **日期**: 2026-07-01
- **状态**: 设计已确认，待实现规划
- **作者**: chenhao

---

## 目录

1. [整体架构](#1-整体架构)
2. [接入层与会话调度器](#2-接入层与会话调度器)
3. [Skill 引擎（模板 + LLM 双路）](#3-skill-引擎模板--llm-双路)
4. [确认工作流与安全控制](#4-确认工作流与安全控制)
5. [执行器与可插拔扩展](#5-执行器与可插拔扩展)
6. [资源管理子系统](#6-资源管理子系统)
7. [LLM 适配层](#7-llm-适配层)
8. [项目目录结构与启动流程](#8-项目目录结构与启动流程)
9. [错误处理与可观测性](#9-错误处理与可观测性)
10. [实现优先级与里程碑](#10-实现优先级与里程碑)
11. [风险评估与缓解](#11-风险评估与缓解)

---

## 背景与目标

### 背景

研发工程师日常工作涉及数据库操作、服务更新、问题排查等，但对各类服务器命令不熟悉，需频繁查阅手册。希望构建一个 Shell Agent 工具，能够：
1. 维护服务器资源
2. 通过自然语言描述执行相关命令
3. 根据日常工作总结成 Skill，自动触发（如发版服务、资源监控等）

### 核心目标

- **自然语言驱动**：用自然语言描述意图，由 LLM 转换为可执行命令
- **人工确认机制**：命令清单和执行结果展示给用户，确认后才执行
- **经验沉淀**：日常操作可总结成 Skill，未来自动触发
- **生产可用**：多层安全防护，可信任地操作生产环境

### 关键设计决策

| 维度 | 决策 |
|------|------|
| 交互形态 | 长驻服务 + 多入口（CLI/MCP/HTTP/Webhook） |
| 服务器接入 | SSH + 可插拔扩展 |
| 安全控制 | 多层防护体系 |
| Skill 机制 | 模板 + LLM 双路 |
| 资源管理 | 服务器清单 + 监控告警 + 审计日志 + 配置版本管理 |
| LLM 接入 | 云 + 本地可切换 |
| 架构方案 | 混合架构：长驻服务做核心 + MCP 作为标准接入层 |

---

## 1. 整体架构

### 1.1 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      接入层 (Entry Points)                    │
│                                                             │
│   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐    │
│   │ CLI     │   │ MCP     │   │ HTTP    │   │ Webhook │    │
│   │ (日常)  │   │ (Trae)  │   │ API     │   │ (定时)  │    │
│   └────┬────┘   └────┬────┘   └────┬────┘   └────┬────┘    │
│        └─────────────┴─────────────┴─────────────┘          │
│                          ↓                                  │
└──────────────────────────┬──────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                    核心层 (Agent Service)                    │
│                                                             │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  会话调度器 (Session Router)                         │  │
│   │  - 多轮对话上下文 / 多入口会话隔离                    │  │
│   │  - 路由：直接命令 / 模板 Skill / LLM Skill            │  │
│   └────┬─────────────────────────────────────────────┬────┘  │
│        ↓                                             ↓       │
│   ┌─────────────────┐                       ┌─────────────┐  │
│   │ Skill 引擎      │ ← 双路执行 ←          │ LLM 适配层  │  │
│   │ (模板 + LLM)    │   ← 参数提取          │ (云/本地    │  │
│   │                 │                       │  可切换)    │  │
│   └────────┬────────┘                       └─────────────┘  │
│            ↓                                                │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  确认工作流 (Confirmation Workflow)                  │  │
│   │  - 命令预览 / 危险命令分类 / 二次确认 / 审计落盘       │  │
│   └────────┬─────────────────────────────────────────────┘  │
│            ↓                                                │
│   ┌────────────┬────────────┬────────────┬─────────────┐    │
│   │ SSH        │ MCP        │ 内置       │ 扩展插件    │    │
│   │ Executor   │ Tools      │ Actions    │ (K8s/Ans..) │    │
│   │            │ (MySQL等)  │ (文件/进程)│             │    │
│   └────────────┴────────────┴────────────┴─────────────┘    │
│                                                             │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  资源管理子系统                                      │  │
│   │  - 服务器清单 / 监控调度 / 审计日志 / 配置版本        │  │
│   └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│                  存储层 (Storage)                            │
│   SQLite (元数据/审计/会话) + 文件 (Skill/凭证)              │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 核心要点

- **接入层多入口**：CLI 日常用、MCP 给 Trae/IDE、HTTP API 给外部系统、Webhook 给定时任务
- **核心层是长驻服务**：自己实现对话循环和调度，不依赖 MCP 协议做主流程
- **MCP 双重身份**：既是 Agent 调用已有 MCP（MySQL）的消费者，也把自己的核心能力暴露为 MCP 供 Trae 使用
- **Skill 双路**：模板 Skill 由 Skill 引擎直接执行，LLM Skill 经 LLM 适配层做意图理解 + 多步拆解
- **确认工作流是所有执行路径的必经环节**：无论直接命令、模板、LLM Skill 生成的命令，都要走"预览→分类→确认→执行→审计"
- **资源管理子系统**：四项能力（清单/监控/审计/版本）作为独立子系统

---

## 2. 接入层与会话调度器

### 2.1 四个入口的职责划分

| 入口 | 协议 | 用途 | 是否长连接 | 触发方式 |
|------|------|------|----------|----------|
| **CLI** | 本地 stdin/stdout（可加 `--json` 输出供脚本消费） | 日常人机交互、排查问题、临时执行 | 单次或 REPL | 人工 |
| **MCP** | MCP 协议（stdio 或 HTTP） | Trae/IDE 内直接调用，复用 IDE 内置 LLM 能力 | 长连接 | IDE 触发 |
| **HTTP API** | REST + SSE（流式输出） | 外部系统调用、CI/CD 集成、自定义前端 | 请求-响应 | 程序触发 |
| **Webhook** | HTTP POST + 定时调度 | 定时任务（每分钟采集、凌晨发版、定期巡检） | 一次性 | Cron 调度 |

**关键约束**：CLI 和 MCP 走人工确认流程（交互式确认）；HTTP API 和 Webhook 必须在请求里预先指定 `auto_confirm=false`（默认）或 `auto_confirm=true`（仅限配了白名单的非危险操作），避免无人值守时执行危险命令。

### 2.2 统一请求模型

四个入口最终都转成同一个内部 `AgentRequest`：

```python
{
  "session_id": "sess_xxx",         # 会话 ID（CLI 每次新建 / REPL 持续 / MCP 用客户端 ID）
  "input": "查一下生产 order 服务的慢查询",  # 自然语言 或 命令
  "input_type": "auto",             # auto | command | natural
  "target": "prod-order-01",       # 可选：指定目标服务器/别名
  "skill_hint": null,              # 可选：直接指定要跑的 skill
  "confirm_mode": "interactive",   # interactive | auto_safe | dry_run
  "context": {                     # 入口附加上下文
    "source": "cli" | "mcp" | "http" | "webhook",
    "caller": "chenhao",
    "timestamp": ...
  }
}
```

`input_type=auto` 时，调度器先用轻量规则判断（以 `ssh`/`mysql`/`kubectl` 等开头且无空格分词歧义 → command；否则 → natural），不确定时回退到 LLM 分类。

### 2.3 会话调度器（Session Router）

**① 路由决策**：

```
input
  │
  ├─ input_type=command  ─→ 直接命令路径（绕过 Skill，但走确认工作流）
  │
  ├─ skill_hint 指定     ─→ 直接调该 Skill
  │
  └─ natural language   ─→ Skill 匹配器
                              │
                              ├─ 命中模板 Skill（关键词/正则匹配）─→ 模板路径
                              │
                              └─ 未命中 ─→ LLM 路径
```

**② 多轮上下文管理**：
- 每个会话维护最近 N 轮对话 + 最近执行过的命令及其结果
- 后续可引用"上一步的输出"、"刚才那台机器"、"再查一下相关的"等指代
- 上下文存储在 SQLite，会话超时（默认 30 分钟无活动）自动归档

**③ 入口隔离**：
- 同一用户的 CLI 会话和 MCP 会话**默认隔离**（避免 IDE 里正在排查问题时被 CLI 输入干扰）
- 可通过 `session_id` 显式串联（例如 CLI 里跑了一半，到 IDE 里继续）

### 2.4 会话生命周期

```
[创建] → [活跃] ←→ [挂起等待确认] → [执行中] → [完成] → [归档]
            │                                  ↑
            └──── 超时归档 ────────────────────┘
```

- **挂起等待确认**：命令已生成、预览已展示、等用户回复 y/n/edit
- **执行中**：已发送到执行器，等结果
- 挂起超过 5 分钟未确认 → 自动取消，记录到审计

### 2.5 confirm_mode 三档定义

**`interactive`（默认，交互式确认）**
- 命令生成后暂停执行，展示完整命令 + 风险等级 + 目标机器给用户
- 用户回复 `y` 执行、`n` 取消、`e` 进入编辑模式改命令后再执行
- 适用于：CLI / MCP 入口的人工操作场景

**`auto_safe`（自动执行安全命令，危险命令仍需确认）**
- 命令生成后先做静态分析：
  - 判定为"只读/安全"（如 `ps`、`df`、`SELECT`、`tail`）→ 直接执行
  - 判定为"写操作/危险"（如 `restart`、`DROP`、`rm`）→ 降级回 `interactive`
- 适用于：HTTP API 调用、Webhook 定时任务、或 CLI 加 `--auto-safe` 加速日常只读操作

**`dry_run`（只生成不执行）**
- 命令生成后只展示，永远不执行，不连服务器
- 用途：验证 LLM 翻译、复制命令手动执行、调试新 Skill
- 适用于：CLI `--dry-run` 标志、新 Skill 上线前的试运行

---

## 3. Skill 引擎（模板 + LLM 双路）

### 3.1 Skill 定义

Skill = 一段可复用的运维能力封装，包含：
- 元信息（名称、描述、触发词、所属类目）
- 输入参数 schema
- 执行逻辑（模板 Skill 是脚本/命令模板；LLM Skill 是 prompt + 工具集 + 约束）
- 安全配置（默认 `confirm_mode`、允许的目标环境、危险等级）
- 版本与变更记录

### 3.2 模板 Skill

适合**高频、确定性强、步骤固定**的任务。典型场景：发版服务、查看 Nginx 日志、清理临时文件、重启服务、拉取数据库备份。

**示例（YAML 格式）**：

```yaml
name: deploy_service
description: 发版服务到指定环境
triggers: ["发版", "deploy", "上线"]
category: release
params:
  - name: service
    type: string
    required: true
    description: 服务名（如 order, payment, user）
    enum: [order, payment, user, inventory]
  - name: env
    type: string
    required: true
    description: 目标环境
    enum: [dev, test, prod]
  - name: version
    type: string
    required: true
    description: 要发布的版本号
    pattern: '^v\d+\.\d+\.\d+$'
    example: v1.2.3

steps:
  - name: 预检查
    command: 'ssh {{env}}-{{service}}-01 "df -h /opt && systemctl status {{service}}-service"'
    confirm: true
  - name: 拉取镜像
    command: 'ssh {{env}}-{{service}}-01 "docker pull registry.example.com/{{service}}:{{version}}"'
    confirm: true
  - name: 滚动重启
    command: 'ssh {{env}}-{{service}}-01 "docker compose up -d --no-deps {{service}}"'
    confirm: true
  - name: 健康检查
    command: 'ssh {{env}}-{{service}}-01 "curl -fs http://localhost:8080/health"'
    confirm: false   # 只读, 可跳过确认

safety:
  default_confirm_mode: interactive
  forbidden_envs_when_prod: []
```

**模板路径执行流程**：
1. Skill 匹配器命中模板
2. LLM 仅做"参数提取"（轻量、便宜、快）
3. 参数校验（类型、enum、pattern、required）
4. 渲染 steps（参数填入模板）
5. 按 steps 顺序进入确认工作流
6. 整体执行结果汇总 + 审计落盘

**关键点**：模板路径的 LLM 只做参数提取，不做命令生成，所以**成本低、可预测、可审计**。

### 3.3 LLM Skill

适合**探索性、多步推理、需要根据中间结果调整**的任务。典型场景：排查 CPU 飙高、分析慢查询根因、根据日志推断故障节点。

**示例（Markdown + Front Matter 格式）**：

```markdown
---
name: troubleshoot_high_cpu
description: 排查目标机器 CPU 飙高问题
triggers: ["CPU高", "cpu 飙", "卡顿", "高负载"]
category: troubleshoot
tools:
  - ssh_executor
  - mysql_query
safety:
  default_confirm_mode: interactive
  allowed_actions: [read_only]
---

# 排查 CPU 飙高

你是一个运维专家。用户报告目标机器 CPU 异常，请按以下思路排查：

## 排查步骤（每步根据上一步结果决定下一步）

1. **总体负载**：执行 `top -bn1 | head -20` 查看负载和高 CPU 进程
2. **进程详情**：对高 CPU 进程执行 `ps -p <pid> -o pid,ppid,cmd,%cpu,etime`
3. **线程级**：如果是 Java 进程，执行 `top -Hp <pid> -bn1 | head -20`
4. **关联日志**：根据进程和时间点，执行 `tail -n 200 /var/log/<service>/app.log`
5. **数据库层**：如果怀疑是 DB 瓶颈，用 mysql_query 工具查 `SHOW PROCESSLIST` 和慢日志

## 约束

- 只允许只读命令（不能 kill、restart、修改配置）
- 每一步都要把上一步输出纳入上下文，决定是否需要继续
- 命令仍需走确认工作流，但可以建议用户切到 auto_safe 加速
```

**LLM 路径执行流程**：
1. Skill 匹配器未命中模板 → 走 LLM 路径
2. 加载 LLM Skill 或使用默认 Agent Prompt
3. LLM 在每一步：思考 → 生成命令 → 走确认工作流 → 拿到结果 → 决定下一步
4. LLM 收敛后输出最终结论
5. 全程审计落盘

**关键点**：LLM 路径**每条命令仍走确认工作流**，安全不松绑。LLM 的自由度体现在"决定下一步做什么"，而不是"绕过确认"。

### 3.4 Skill 匹配器

```
用户输入
  │
  ├─ 1. 显式指定 skill_hint        → 直接走该 Skill
  ├─ 2. 触发词精确匹配             → 模板 Skill
  ├─ 3. 关键词/正则启发式匹配       → 模板 Skill
  ├─ 4. LLM 路由判定（输入模糊时） → LLM 选择
  └─ 5. 都不命中                   → 默认 LLM Agent
```

匹配器自己有缓存（高频输入直接命中），模糊输入才调 LLM 做一次轻量分类。

### 3.5 Skill 生命周期

```
[创建] → [试运行 dry_run] → [启用] ←→ [禁用] → [归档]
                              │
                              └─ [版本更新] → 新版本试运行 → 替换/回滚
```

- **试运行**：新 Skill 必须先用 `dry_run` 跑几次真实输入
- **版本管理**：每次修改生成新版本，老版本保留可回滚
- **自动总结**：用户日常操作如果未命中任何 Skill 且重复多次类似输入，系统提示"是否要把这个流程沉淀成 Skill？"

### 3.6 Skill 存储结构

```
skills/
├── templates/                  # 模板 Skill
│   ├── deploy_service.yaml
│   ├── query_slow_log.yaml
│   └── restart_service.yaml
├── llm/                        # LLM Skill
│   ├── troubleshoot_high_cpu.md
│   └── analyze_db_health.md
└── registry.json               # 注册表（索引、版本、启用状态）
```

### 3.7 Skill 步骤失败处理

```yaml
steps:
  - name: 拉取镜像
    command: 'docker pull ...'
    confirm: true
    on_failure:
      action: abort          # abort（默认）| continue | retry(3)
      message: "镜像拉取失败，发版中止"
      
  - name: 健康检查
    command: 'curl ...'
    confirm: false
    on_failure:
      action: retry
      max_retries: 3
      interval: 5
```

- `abort`：终止整个 Skill，前面已执行的步骤不回滚（但记录到审计）
- `continue`：继续下一步
- `retry`：重试 N 次

---

## 4. 确认工作流与安全控制

所有执行路径（直接命令 / 模板 Skill / LLM Skill）生成的命令都必须经过这一层。

### 4.1 工作流总览

```
            命令生成（来自任意路径）
                    │
                    ↓
        ┌──────────────────────┐
        │  ① 命令规范化         │  统一封装、提取目标机器
        └──────────┬───────────┘
                   ↓
        ┌──────────────────────┐
        │  ② 静态分类器         │  判定 safe/medium/dangerous/forbidden
        └──────────┬───────────┘
                   ↓
        ┌──────────────────────┐
        │  ③ 环境校验           │  生产环境额外限制
        └──────────┬───────────┘
                   ↓
        ┌──────────────────────┐
        │  ④ 预览生成           │  展示给用户
        └──────────┬───────────┘
                   ↓
              决策分支
                   │
   ┌───────────────┼───────────────┐
   ↓               ↓               ↓
confirm_mode    confirm_mode    confirm_mode
interactive     auto_safe       dry_run
   │               │               │
   ↓               ↓               ↓
 等用户确认     safe→自动执行     永不执行
               其他→降级 interactive  仅展示
   │                               │
   └───────────────┬───────────────┘
                   ↓
        ┌──────────────────────┐
        │  ⑤ 执行               │  调用执行器
        └──────────┬───────────┘
                   ↓
        ┌──────────────────────┐
        │  ⑥ 审计落盘           │  记录谁/何时/何地/什么/结果
        └──────────────────────┘
```

### 4.2 命令规范化

统一封装成 `PendingCommand`：

```python
{
  "id": "cmd_xxx",
  "raw": "ssh prod-order-01 'systemctl restart order-service'",
  "target": "prod-order-01",
  "target_env": "prod",
  "executor": "ssh",
  "actual_command": "systemctl restart order-service",
  "source": "template_step",
  "skill_name": "deploy_service",
  "step_name": "滚动重启",
}
```

### 4.3 静态分类器

四级分类：

| 等级 | 含义 | 示例 | auto_safe 下行为 |
|------|------|------|------------------|
| `safe` | 只读，无副作用 | `ps`, `df`, `top`, `SELECT`, `tail`, `cat /etc/xxx` | 自动执行 |
| `medium` | 有写操作但可控 | `systemctl restart`, `docker pull`, 带 WHERE 的 UPDATE | 降级 interactive |
| `dangerous` | 高风险，影响范围大 | `DROP TABLE`, `DELETE` 无 WHERE, `kill -9`, `rm -rf /var/log/*` | 降级 interactive，加额外警示 |
| `forbidden` | 黑名单，永不执行 | `rm -rf /`, `dd if=/dev/zero of=/dev/sda` | 直接拒绝，记录到审计 |

**分类器三层判定**：

```
命令
  │
  ├─ Layer 1: 精确黑名单（forbidden）
  │     - 命中即拒绝，不可绕过
  │
  ├─ Layer 2: 精确白名单（safe）
  │     - 已知只读命令清单
  │     - 命中且无重定向/管道到危险命令 → safe
  │
  └─ Layer 3: 启发式规则（medium / dangerous）
        - rm + -rf + 通配符 → dangerous
        - DROP / TRUNCATE → dangerous
        - DELETE/UPDATE 无 WHERE → dangerous
        - kill -9 → dangerous
        - systemctl restart/stop → medium
        - 其他写操作 → medium
        - 默认（无法判定）→ medium（保守）
```

**关键原则**：无法判定时保守归为 medium，即 `auto_safe` 下不会自动执行。

### 4.4 环境校验

```yaml
# config/safety/env_policies.yaml
prod:
  require_secondary_confirm: true       # 危险命令需二次确认
  forbidden_executors: []
  time_window:                          # 可选：危险操作只允许在指定时间窗
    dangerous_allowed: ["10:00-18:00"]

test:
  require_secondary_confirm: false
  time_window: null

dev:
  require_secondary_confirm: false
  time_window: null
```

**生产环境 + dangerous 命令的二次确认流程**：

```
[预览] 目标: prod-order-01 (生产)  ⚠⚠⚠ 危险操作
       命令: DELETE FROM order_log WHERE created_at < '2025-01-01'
       风险: 危险 - 生产环境删除数据
       
⚠ 此操作不可逆。请输入目标机器名 "prod-order-01" 确认:
> prod-order-01
✓ 确认通过，执行中...
```

### 4.5 预览格式

```
─── safe ───────────────────────────────
✓ 目标: prod-order-01
  命令: top -bn1 | head -20
  来源: LLM Skill [troubleshoot_high_cpu] 步骤 1

─── medium ─────────────────────────────
⚠ 目标: prod-order-01 (生产)
  命令: systemctl restart order-service
  来源: 模板 Skill [deploy_service] 步骤 "滚动重启"
  影响: 服务重启, 预计中断 <5s

─── dangerous ──────────────────────────
⚠⚠⚠ 目标: prod-order-01 (生产)  危险操作
  命令: DELETE FROM order_log WHERE created_at < '2025-01-01'
  来源: 直接命令
  影响: 删除 2025-01-01 前的订单日志, 不可逆
  
确认执行? [y/n/e]:
  y - 执行
  n - 取消
  e - 编辑命令后执行
```

`e`（编辑）会打开 `$EDITOR`，用户改完再走一遍分类器（防止改成更危险的）。

### 4.6 执行与超时

- 每条命令有默认超时（SSH 60s、MySQL 30s），可在 Skill step 里覆盖
- 超时自动取消，记录到审计
- 长输出截断（默认保留前 200 行 + 后 50 行 + 总行数）

### 4.7 审计日志

每条命令（无论执行与否）都落盘：

```python
{
  "audit_id": "aud_xxx",
  "session_id": "sess_xxx",
  "caller": "chenhao",
  "source": "cli",
  "skill_name": "deploy_service",
  "step_name": "滚动重启",
  "target": "prod-order-01",
  "target_env": "prod",
  "command": "systemctl restart order-service",
  "classification": "medium",
  "confirm_mode": "interactive",
  "user_confirmed": true,
  "executed": true,
  "exit_code": 0,
  "duration_ms": 1234,
  "truncated": false,
  "timestamp": "2026-07-01T10:30:00+08:00"
}
```

**未执行也要记录**（用户拒绝、超时未确认、被 forbidden 拦截）。

### 4.8 安全配置文件

```
config/
├── safety/
│   ├── forbidden_patterns.yaml    # 黑名单正则
│   ├── safe_commands.yaml         # 白名单命令清单
│   ├── heuristic_rules.yaml       # 启发式规则
│   └── env_policies.yaml          # 各环境策略
```

所有规则文件支持热加载，但**改动本身也记审计**（防止有人偷偷改黑名单）。

---

## 5. 执行器与可插拔扩展

### 5.1 执行器接口

```python
class Executor(Protocol):
    name: str                          # 执行器标识
    
    def normalize(self, raw_command: str) -> PendingCommand:
        """从原始命令解析出 target/executor/actual_command"""
        ...
    
    def execute(self, command: PendingCommand, timeout: int) -> ExecutionResult:
        """实际执行命令，返回统一结果"""
        ...
    
    def health_check(self) -> bool:
        """执行器是否可用"""
        ...
```

### 5.2 内置执行器清单

| 执行器 | name | 用途 | 实现库 |
|--------|------|------|--------|
| SSH | `ssh` | 远程 shell 命令 | asyncssh |
| MySQL | `mysql` | SQL 执行 | aiomysql |
| PostgreSQL | `postgres` | SQL 执行 | asyncpg |
| Redis | `redis` | KV 操作 | redis-py (async) |
| Kafka | `kafka` | 消息查询、消费 lag | aiokafka |
| MQTT | `mqtt` | 发布/订阅 | asyncio-mqtt |
| MongoDB | `mongodb` | 文档查询 | motor |
| HTTP | `http` | 调用 HTTP 接口 | aiohttp |
| 文件 | `action:file` | 本地文件操作 | 内置 |
| 进程 | `action:process` | 本地进程操作 | 内置 |

**关键定位**：Shell Agent 内部自实现所有组件执行器，**不依赖 MCP**。MCP 只作为对外给 Trae 的接入层。

### 5.3 SSH 执行器详细设计

**① 凭证管理（支持三种模式）**：

```yaml
# config/credentials.yaml
credentials:
  # 模式 1: 直接存密码（方便起步）
  - id: prod_ssh_direct
    type: ssh_password
    username: chenhao
    password: "实际密码"             # 直接明文存（文件权限 600）
    
  # 模式 2: 存引用到环境变量
  - id: prod_ssh_env
    type: ssh_password
    username: chenhao
    password_env: PROD_SSH_PASS
    
  # 模式 3: 引用外部 Secret Manager
  - id: prod_ssh_vault
    type: ssh_key
    username: deploy
    vault_ref:
      path: secret/ssh/prod
      key_field: private_key
```

**加密存储支持**：

```yaml
credentials:
  - id: prod_ssh_direct
    type: ssh_password
    username: chenhao
    password_encrypted: "AES256:base64...."   # 密文
    password_env_master: AGENT_MASTER_KEY
```

**安全策略**：
- 配置文件权限强制 `chmod 600`，启动时检查
- 凭证加载后绝不输出到日志/审计/LLM 上下文，审计里只记"使用了 credential_id"
- 提供 `opsane credentials encrypt` 命令加密明文

**② 连接管理**：

```python
class SSHExecutor:
    def __init__(self):
        self.pool = ConnectionPool(
            max_per_host=3,           # 每台机器最多 3 个并发连接
            idle_timeout=300,         # 空闲 5 分钟回收
            total_max=50,             # 全局连接上限
        )
```

**③ 命令执行**：

```python
async def execute(self, command: PendingCommand, timeout: int) -> ExecutionResult:
    conn = await self.pool.get(command.target)
    try:
        wrapped = f"bash -lc {shlex.quote(command.actual_command)}"
        stdin, stdout, stderr = await conn.exec_command(wrapped, timeout=timeout)
        ...
    finally:
        await self.pool.release(command.target, conn)
```

**④ 输出截断**：

```python
def process_output(stdout: str, limit: int = 10000) -> tuple[str, bool]:
    if len(stdout) <= limit:
        return stdout, False
    head = stdout[:limit // 2]
    tail = stdout[-limit // 2:]
    skipped = len(stdout) - limit
    return f"{head}\n\n... [{skipped} chars truncated] ...\n\n{tail}", True
```

### 5.4 插件机制（多版本并存）

**插件结构**：

```
plugins/
├── redis/
│   ├── manifest.json
│   ├── versions/
│   │   ├── v6/                       # 适配 Redis 6.x
│   │   │   ├── executor.py
│   │   │   ├── commands.yaml
│   │   │   └── skills/
│   │   └── v7/                       # 适配 Redis 7.x
│   │       ├── executor.py
│   │       ├── commands.yaml
│   │       └── skills/
│   └── manifest.py
├── kafka/
│   ├── manifest.json
│   └── versions/{v2,v3}/...
```

**版本自动识别机制**：

```
实例首次连接
  │
  ├─ 1. 检查 config 是否显式指定 plugin_version
  │     └─ 是 → 直接用，跳过识别（手动覆盖优先）
  │
  ├─ 2. 否则调用插件提供的 version_probe()
  │     └─ 执行插件特定的探测命令获取版本
  │
  ├─ 3. 匹配到对应版本的执行器
  │     └─ 找到 → 绑定并缓存
  │
  ├─ 4. 找不到对应版本
  │     └─ 回退到 default_version，记 warning
  │
  └─ 5. 探测失败
        └─ 回退到 default_version，标记 "version_unknown"
```

**各插件探测实现**：

| 插件 | 探测命令 | 解析方式 |
|------|---------|---------|
| Redis | `INFO server` | 解析 `redis_version:7.0.5` → `v7` |
| MySQL | `SELECT VERSION()` | 解析 `8.0.35` → `v8` |
| Kafka | AdminClient `describe_cluster()` | 解析 broker API version |
| MongoDB | `db.runCommand({buildInfo:1})` | 解析 `version: 5.0.8` → `v5` |
| PostgreSQL | `SELECT version()` | 解析 `PostgreSQL 14.2` → `v14` |

**版本优先级**：

```
显式 plugin_version + version_lock: true   （最高，永不自动识别）
显式 plugin_version（无 version_lock）       （启动用此版本，定期探测校验）
未指定 plugin_version                       （启动时自动探测，缓存结果）
探测失败                                     （回退 default_version + 标记）
```

**启动时全量并发探测**：`asyncio.gather()`，单实例超时 10s。

**定期刷新**：默认每天一次，可用 `opsane probe-versions` 手动触发。

### 5.5 配置统一管理

```
config/
├── credentials.yaml        # 所有凭证
├── inventory.yaml          # 服务器清单（见第 6 节）
├── mysql.yaml / redis.yaml / kafka.yaml / ...   # 各组件实例
```

每份配置文件遵循相同结构：

```yaml
instances:
  - alias: <别名>              # LLM 和用户都用别名引用
    host: <host>
    port: <port>
    credential_ref: <credential_id>
    plugin_version: <version>   # 可选，不写则自动探测
    pool:
      max_connections: 10
      idle_timeout: 300
    timeouts:
      query: 30
      connect: 5
```

**关键原则**：LLM 永远用别名引用，看不到真实 host/密码。

---

## 6. 资源管理子系统

### 6.1 子系统总览

```
┌─────────────────────────────────────────────────────────────┐
│                  资源管理子系统                              │
│                                                             │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│   │ 服务器清单  │  │ 监控调度器  │  │ 配置版本库  │        │
│   │ Inventory   │  │ Monitor     │  │ ConfigStore │        │
│   └──────┬──────┘  └──────┬──────┘  └──────┬──────┘        │
│          │                │                │                │
│          ↓                ↓                ↓                │
│   ┌──────────────────────────────────────────────────────┐  │
│   │              统一存储 (SQLite + 文件)                │  │
│   └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 服务器清单（Inventory）

```yaml
# config/inventory.yaml
servers:
  - alias: prod-order-01
    host: 10.0.1.10
    port: 22
    env: prod
    role: app
    services: [order-service, order-worker]
    ssh_credential: prod_ssh_direct
    related_db: prod_order_db
    related_cache: prod_cache
    tags: [critical, order-domain]
```

**反向索引**（注入 LLM 上下文）：

```python
by_env: {prod: [prod-order-01, prod-mq-01], test: [test-order-01]}
by_role: {app: [prod-order-01, test-order-01], mq: [prod-mq-01]}
by_service: {order-service: [prod-order-01, test-order-01]}
by_tag: {critical: [prod-order-01, prod-mq-01]}
```

### 6.3 监控调度器

**采集架构**：

```
┌──────────────────────────────────────────────────┐
│  监控调度器                                       │
│  ┌────────────────┐    ┌────────────────────┐   │
│  │ 调度器         │───→│ 采集任务队列       │   │
│  │ (APScheduler)  │    │ (并发执行, 限流)    │   │
│  └────────────────┘    └─────────┬──────────┘   │
│                                  ↓                │
│  ┌────────────────────────────────────────────┐ │
│  │  采集器 Registry                           │ │
│  │  - ssh_metrics  (CPU/mem/disk/proc)        │ │
│  │  - mysql_metrics (processlist/slow log)    │ │
│  │  - redis_metrics (info/slowlog)            │ │
│  │  - kafka_metrics (lag/consumer group)     │ │
│  └────────────────────────────────────────────┘ │
│                                  ↓                │
│  ┌────────────────────────────────────────────┐ │
│  │  告警引擎                                  │ │
│  │  - 阈值规则                                │ │
│  │  - 触发动作: 通知 / 调用 Skill / 记录       │ │
│  └────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

**内置采集器**：

| 采集器 | 采集目标 | 指标 | 默认间隔 |
|--------|---------|------|---------|
| `ssh_metrics` | Inventory 里的服务器 | CPU、内存、磁盘、负载、关键进程状态 | 60s |
| `mysql_metrics` | MySQL 实例 | 连接数、慢查询数、QPS、表大小 TOP N | 60s |
| `redis_metrics` | Redis 实例 | 内存、连接、hit rate、慢日志 | 60s |
| `kafka_metrics` | Kafka 实例 | consumer group lag、topic 大小 | 30s |

**采集结果存储**（SQLite）：

```sql
CREATE TABLE metrics (
    id INTEGER PRIMARY KEY,
    target TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value REAL,
    labels TEXT,
    collected_at TIMESTAMP NOT NULL,
    INDEX idx_target_time (target, collected_at)
);
```

**保留策略**：原始数据保留 7 天，聚合（小时/日均值）保留 1 年。

**告警规则**：

```yaml
# config/monitor_rules.yaml
rules:
  - name: high_cpu
    target: "*"
    metric: ssh.cpu_usage
    condition: "> 80"
    duration: "5m"
    severity: warning
    actions:
      - notify: console
      - skill: troubleshoot_high_cpu
      
  - name: mysql_slow_query_spike
    target: "prod_*_db"
    metric: mysql.slow_queries_per_min
    condition: "> 10"
    duration: "1m"
    severity: critical
    actions:
      - notify: webhook
      - skill: analyze_slow_query
```

**告警触发 Skill 的安全约束**：
- 纯只读分析 Skill（如 `troubleshoot_high_cpu`、`analyze_slow_query`）默认走 `auto_safe`
- 写操作仍降级 `interactive` 等人工确认
- 想让告警自动执行修复命令，必须在规则里显式写 `auto_execute: true` 且配白名单命令

### 6.4 操作审计

第 4.7 节已定义审计日志结构。补充查询接口：

```python
class AuditLog:
    def query(self, caller=None, target=None, skill=None, env=None,
              start_time=None, end_time=None, executed_only=False,
              danger_level=None) -> list[AuditRecord]:
        ...
    
    def replay(self, audit_id: str) -> str:
        """回放某次审计记录对应的命令（dry_run）"""
        ...
```

CLI 命令：`opsane audit query --target prod-order-01 --last 1h`

### 6.5 配置与变更版本管理（Git）

所有配置文件（inventory/credentials/各组件实例/监控规则/safety 规则/Skill 文件）都纳入 Git 版本管理。

```
shell-agent-config/             # 独立的 Git 仓库
├── config/
├── skills/
└── CHANGELOG.md
```

**提交分类**：
- 人工改动（vim 编辑后）→ 用户自己 git commit
- CLI/API 改动 → 自动 commit，message 带 `[auto]` 前缀
- 发版前 snapshot → `git tag config-snapshot-<timestamp>-<label>`
- 凌晨定时任务 → 合并当天的 `[auto]` 提交为一条

**版本快照机制**：

```python
class ConfigStore:
    def snapshot(self, label: str) -> str:
        """生成配置快照，返回快照 ID"""
        ...
    
    def diff(self, snapshot_id: str) -> str:
        """对比当前与快照的差异"""
        ...
    
    def rollback(self, snapshot_id: str) -> bool:
        """回滚到指定快照"""
        ...
```

**与发版 Skill 集成**：发版 Skill 执行前自动 `snapshot(label="before_deploy_v1.2.3")`，失败时可 `rollback`。

### 6.6 数据存储总览

```
data/
├── shell_agent.db                # SQLite
│   ├── sessions
│   ├── audit_logs
│   ├── metrics
│   └── version_cache
├── skills/                       # Skill 文件（Git 管理）
└── config/                       # 配置文件（Git 管理）
```

---

## 7. LLM 适配层

### 7.1 职责定位

1. 抽象不同 LLM 提供商（云 / 本地可切换）
2. 构造合适的提示词
3. 管理调用模式（参数提取 / 路由分类 / 多步推理 / 自然语言转 SQL）

### 7.2 LLM Provider 抽象

```python
class LLMProvider(Protocol):
    name: str
    
    async def chat(self, request: ChatRequest) -> ChatResponse: ...
    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[ChatDelta]: ...
    async def embed(self, text: str) -> list[float]: ...
    def supports_tools(self) -> bool: ...
```

### 7.3 内置 Provider 清单

| Provider | name | 是否支持 tools |
|---------|------|--------------|
| OpenAI | `openai` | 是 |
| Anthropic | `anthropic` | 是 |
| 通义千问 | `qwen` | 是 |
| GLM | `glm` | 是 |
| DeepSeek | `deepseek` | 是 |
| Ollama | `ollama` | 部分 |
| vLLM | `vllm` | 部分 |

### 7.4 任务类型与模型路由

任务类型由**调用方静态指定**（不靠 LLM 自己判断）：

```
任务类型              调用时机                              推荐模型
─────────────────────────────────────────────────────────────────
param_extraction     模板 Skill 参数提取                    gpt-4o-mini（便宜快）
skill_matching       Skill 匹配器 LLM fallback              gpt-4o-mini
llm_skill_reasoning  LLM Skill 多步推理                     claude-sonnet-4（强）
free_form_agent      默认 Agent                            claude-sonnet-4
nl_to_sql           自然语言转 SQL                          gpt-4o（强，SQL 要准）
```

**配置**：

```yaml
# config/llm.yaml
default_provider: openai

providers:
  openai:
    api_key_env: OPENAI_API_KEY
    default_model: gpt-4o
    timeout: 60
  anthropic:
    api_key_env: ANTHROPIC_API_KEY
    default_model: claude-sonnet-4-20250514
    timeout: 60
  ollama:
    base_url: http://localhost:11434
    default_model: qwen2.5:14b
    timeout: 120

routing:
  param_extraction:
    provider: openai
    model: gpt-4o-mini
    temperature: 0.1
  skill_matching:
    provider: openai
    model: gpt-4o-mini
    temperature: 0.2
  llm_skill_reasoning:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.3
  free_form_agent:
    provider: anthropic
    model: claude-sonnet-4-20250514
    temperature: 0.3
  nl_to_sql:
    provider: openai
    model: gpt-4o
    temperature: 0.2
```

### 7.5 提示词模板

**① 系统提示词（所有调用共享）**：

```python
SYSTEM_PROMPT = """你是 Shell Agent 的核心智能层，协助运维工程师操作生产服务器。

## 你的能力边界
- 只能通过提供的工具（tools）执行操作，不能直接生成 shell 命令绕过工具
- 生成的所有命令必须经过确认工作流，由用户确认后才执行
- 仅使用提供的执行器（SSH/MySQL/Redis 等），目标实例必须用别名引用

## 可用实例
{injection_instances}    # 注入 Inventory + 版本信息

## 安全约束
- 危险命令必须明确警示用户
- 生产环境的写操作需要二次确认
- 永远不要执行你不理解的命令

## 输出风格
- 简洁直接
- 命令生成后简要说明意图，等用户确认
"""
```

**② 参数提取提示词**：

```python
PARAM_EXTRACTION_PROMPT = """从用户输入提取 Skill 参数。

Skill: {skill_name}
描述: {skill_description}
参数 schema: {params_schema}

用户输入: {user_input}

返回 JSON: {{"param_name": "value", ...}}
只返回 JSON，不要解释。缺失的必填参数用 null。
"""
```

**③ 路由分类提示词**：

```python
ROUTING_PROMPT = """判断用户输入应该走哪个 Skill。

可用 Skill: {skill_list}

用户输入: {user_input}

返回 JSON:
{{"matched_skill": "skill_name 或 null", "reason": "简短理由"}}
"""
```

**④ LLM Skill 推理提示词**：

```python
LLM_SKILL_PROMPT = """{skill_markdown_content}

## 当前任务上下文
- 目标: {target}
- 已执行步骤及结果: {step_history}

## 工具
你有以下工具可用: {tools_description}

下一步: 思考要做什么 → 如果需要执行命令，调用对应工具 → 拿到结果后继续。
"""
```

**⑤ 自然语言转 SQL 提示词**：

```python
NL_TO_SQL_PROMPT = """你是 SQL 专家。将用户的自然语言查询转换成 SQL。

## 数据库信息
- 实例: {instance_alias} ({db_type} {version})
- 当前库: {database}
- 表结构: {schema_info}

## 用户查询
{user_input}

## 约束
1. 只生成 SELECT 语句（除非用户明确说要修改数据）
2. 大表查询必须带 LIMIT（默认 100）
3. 时间字段用标准格式
4. 字段名/表名用反引号（MySQL）或双引号（PostgreSQL）

## 输出格式（JSON）
{{
  "sql": "SELECT ...",
  "explanation": "这条 SQL 做了什么",
  "warnings": ["如果有性能风险等"],
  "estimated_rows": "预计返回行数"
}}
"""
```

### 7.6 上下文管理（压缩摘要，非直接截断）

```
每次需要构造 LLM messages 时:
  │
  ├─ 1. 计算当前上下文 token 数
  │
  ├─ 2. 未超过阈值 → 直接用完整历史
  │
  └─ 3. 超过阈值
        ├─ 保留: 系统提示词 + 实例信息（永不压缩）
        ├─ 保留: 最近 K 轮工具调用 + 结果（默认 3 轮）
        └─ 压缩: 更老的步骤成摘要
              ↓
        摘要模板:
        "已完成步骤:
         1. 执行 ps -ef，发现高 CPU 进程 PID 1234
         2. 查 PID 1234 日志，发现 OOM 警告
         关键发现: 内存泄漏，非 CPU 问题
         已排除方向: CPU/磁盘 IO
         待验证方向: 内存、JVM 堆"
              ↓
        用 param_extraction 配置的便宜模型生成
              ↓
        组装: [system] + [summary] + [recent K rounds] + [current input]
```

**摘要要求**：
- 保留：命令、关键发现、排除的方向、用户明确确认过的结论
- 丢弃：冗长输出、重复信息

**摘要缓存**：已有摘要且覆盖的步骤没变 → 复用，避免重复压缩。

**阈值**：按模型上下文窗口动态计算：`模型窗口 * 0.6 - 预留输出空间`

### 7.7 错误处理与降级

```python
class LLMAdapter:
    async def chat_with_fallback(self, request: ChatRequest) -> ChatResponse:
        providers = [self.primary, self.fallback]
        for provider in providers:
            try:
                return await provider.chat(request)
            except (TimeoutError, RateLimitError):
                continue
        raise AllProvidersFailedError()
```

**降级策略**：
- 主 provider 超时/限流 → 自动切到 fallback
- 所有 provider 失败 → 模板 Skill 仍可工作（参数提取失败时回退到"让用户手动填参数"）
- LLM Skill 推理失败 → 提示用户改用模板 Skill 或手动执行

### 7.8 成本监控

```python
{
    "session_id": "sess_xxx",
    "task_type": "llm_skill_reasoning",
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "input_tokens": 2345,
    "output_tokens": 567,
    "cost_usd": 0.0123,
    "latency_ms": 2340,
    "timestamp": "2026-07-01T10:30:00+08:00"
}
```

CLI：`opsane llm usage --today`

---

## 8. 项目目录结构与启动流程

### 8.1 项目目录结构

```
AI-Shell/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
│
├── shell_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   │
│   ├── core/
│   │   ├── session.py                # 会话调度器
│   │   ├── request.py                # AgentRequest 统一模型
│   │   ├── router.py                 # 路由决策
│   │   └── context.py                # 上下文管理
│   │
│   ├── skills/
│   │   ├── engine.py
│   │   ├── matcher.py
│   │   ├── template_skill.py
│   │   ├── llm_skill.py
│   │   ├── registry.py
│   │   └── lifecycle.py
│   │
│   ├── safety/
│   │   ├── workflow.py
│   │   ├── classifier.py
│   │   ├── env_policy.py
│   │   ├── preview.py
│   │   ├── confirm.py
│   │   └── audit.py
│   │
│   ├── executors/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── ssh.py
│   │   ├── mysql.py
│   │   ├── postgres.py
│   │   ├── redis.py
│   │   ├── kafka.py
│   │   ├── mqtt.py
│   │   ├── mongodb.py
│   │   ├── http.py
│   │   ├── file_action.py
│   │   ├── process_action.py
│   │   └── pool.py
│   │
│   ├── plugins/
│   │   ├── loader.py
│   │   ├── version_probe.py
│   │   └── builtin/
│   │       ├── redis/versions/{v6,v7}/...
│   │       ├── kafka/versions/{v2,v3}/...
│   │       └── ...
│   │
│   ├── llm/
│   │   ├── adapter.py
│   │   ├── providers/
│   │   │   ├── base.py
│   │   │   ├── openai.py
│   │   │   ├── anthropic.py
│   │   │   ├── qwen.py
│   │   │   ├── glm.py
│   │   │   ├── deepseek.py
│   │   │   ├── ollama.py
│   │   │   └── vllm.py
│   │   ├── prompts.py
│   │   ├── routing.py
│   │   ├── context_manager.py
│   │   └── cost_tracker.py
│   │
│   ├── resources/
│   │   ├── inventory.py
│   │   ├── monitor/
│   │   │   ├── scheduler.py
│   │   │   ├── collectors/
│   │   │   ├── alerting.py
│   │   │   └── rules.py
│   │   ├── audit_log.py
│   │   └── config_store.py
│   │
│   ├── entries/
│   │   ├── cli_entry.py
│   │   ├── mcp_entry.py
│   │   ├── http_entry.py
│   │   └── webhook_entry.py
│   │
│   ├── storage/
│   │   ├── database.py
│   │   ├── models.py
│   │   └── migrations/
│   │
│   └── utils/
│       ├── config.py
│       ├── crypto.py
│       ├── output.py
│       └── logging.py
│
├── config/
│   ├── agent.yaml
│   ├── credentials.yaml
│   ├── inventory.yaml
│   ├── llm.yaml
│   ├── mysql.yaml / redis.yaml / ...
│   ├── monitor_rules.yaml
│   └── safety/
│
├── skills/
│   ├── templates/
│   └── llm/
│
├── data/                             # .gitignore
│   ├── shell_agent.db
│   └── logs/
│
├── plugins/                          # 用户自定义插件
│
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

### 8.2 技术栈选型

| 层 | 选型 | 理由 |
|----|------|------|
| 语言 | Python 3.11+ | 运维生态成熟、异步支持好、LLM SDK 丰富 |
| 异步框架 | asyncio + anyio | 原生异步 |
| CLI | Click + Rich | 命令组织清晰 + 终端美化 |
| HTTP 服务 | FastAPI + Uvicorn | 异步原生、自动 OpenAPI |
| MCP 服务端 | mcp Python SDK | 官方 SDK |
| SSH | asyncssh | 纯异步、连接池友好 |
| MySQL | aiomysql | 异步、社区活跃 |
| PostgreSQL | asyncpg | 性能最优 |
| Redis | redis-py (async) | 官方支持 |
| Kafka | aiokafka | 异步 |
| MQTT | asyncio-mqtt | 简洁 |
| MongoDB | motor | 官方异步驱动 |
| 调度 | APScheduler | 轻量、支持 cron/interval |
| 存储 | SQLite + aiosqlite | 单机够用、无外部依赖 |
| 配置 | PyYAML + pydantic | YAML 易读 + schema 校验 |
| 加密 | cryptography | AES256 |
| Git | GitPython | 程序化 git |
| 日志 | loguru | 开箱即用 |
| 测试 | pytest + pytest-asyncio | 标准 |

### 8.3 启动流程

```
opsane serve
        │
        ↓
Phase 1: 加载配置
  ├─ 1.1 读取 config/agent.yaml
  ├─ 1.2 加载 credentials.yaml（解密敏感字段）
  ├─ 1.3 加载 inventory.yaml
  ├─ 1.4 加载各组件实例配置
  ├─ 1.5 加载 safety 规则
  ├─ 1.6 加载 monitor_rules.yaml
  ├─ 1.7 加载 llm.yaml
  └─ 1.8 校验配置完整性
        ↓
Phase 2: 初始化存储
  ├─ 2.1 打开 SQLite
  ├─ 2.2 执行 migrations
  └─ 2.3 清理过期会话
        ↓
Phase 3: 初始化执行器
  ├─ 3.1 注册内置执行器
  ├─ 3.2 扫描 plugins/ 目录
  ├─ 3.3 加载插件 manifest
  └─ 3.4 注册插件执行器
        ↓
Phase 4: 版本自动探测（全量并发）
  ├─ 4.1 收集所有实例
  ├─ 4.2 并发执行 version_probe()（单实例超时 10s）
  ├─ 4.3 匹配到插件版本
  ├─ 4.4 未匹配回退 default_version
  └─ 4.5 缓存结果
        ↓
Phase 5: 初始化 Skill 引擎
  ├─ 5.1 扫描 skills/ 目录
  ├─ 5.2 加载到 Registry
  ├─ 5.3 校验执行器引用
  └─ 5.4 校验参数 schema
        ↓
Phase 6: 初始化 LLM 适配层
  ├─ 6.1 初始化各 provider
  ├─ 6.2 建立路由表
  ├─ 6.3 主备健康检查
  └─ 6.4 失败的标记不可用
        ↓
Phase 6.5: 启动监控调度器
  ├─ 6.5.1 注册采集器
  ├─ 6.5.2 配置调度
  └─ 6.5.3 启动告警引擎
        ↓
Phase 7: 启动接入层
  ├─ 7.1 启动 HTTP API
  ├─ 7.2 启动 Webhook
  ├─ 7.3 启动 MCP 服务端
  └─ 7.4 输出启动摘要
        ↓
     [就绪, 接受请求]
```

### 8.4 CLI 命令总览

```bash
# 服务管理
opsane serve [--port 8000] [--mcp] [--no-monitor]
opsane status
opsane stop
opsane health

# 日常交互
opsane run "查 prod-order-01 CPU 情况"
opsane shell                            # REPL
opsane exec "ssh prod-order-01 'df -h'"

# 资源管理
opsane servers list [--env prod]
opsane servers add ...
opsane instances list
opsane probe-versions

# Skill 管理
opsane skills list
opsane skills show deploy_service
opsane skills run deploy_service --params '...'
opsane skills test deploy_service --dry-run '...'
opsane skills create --template

# 审计
opsane audit query --target prod-order-01 --last 1h
opsane audit replay <audit_id>

# 配置
opsane config get inventory
opsane config set llm.default_provider anthropic
opsane config diff <snapshot_id>
opsane config rollback <snapshot_id>

# 监控
opsane monitor status
opsane monitor metrics <target> [--last 1h]
opsane monitor alerts

# LLM
opsane llm providers
opsane llm usage [--today|--week]
```

### 8.5 进程模型

**单进程多协程**：

```
opsane serve 启动一个进程，内部:
  ├─ 主事件循环（asyncio）
  ├─ HTTP API 协程（FastAPI/Uvicorn）
  ├─ MCP 协程
  ├─ 监控调度器协程（APScheduler async）
  ├─ 各执行器的连接池协程
  └─ 配置热加载 watcher（watchdog）
```

**扩展点**：预留 `RuntimeBackend` 抽象，未来切多进程不改业务代码：

```python
class RuntimeBackend(Protocol):
    async def submit_task(self, task: Task) -> TaskHandle: ...

class InProcessBackend:    # 初期默认
    ...
class ProcessPoolBackend:  # 未来扩展
    ...
```

### 8.6 配置热加载

```python
class ConfigWatcher:
    async def on_file_changed(self, path: str):
        if path.endswith('inventory.yaml'):
            await inventory.reload()
        elif path.endswith('credentials.yaml'):
            await credentials.reload()    # 活跃连接不重连，新连接用新凭证
        elif path.endswith('safety/'):
            await classifier.reload_rules()
        elif path.endswith('monitor_rules.yaml'):
            await monitor.reload_rules()
        await audit.log_config_change(path)
```

- safety 规则改动本身记审计
- credentials 热加载不影响已建立的连接
- llm.yaml 改动需手动 `opsane llm reload`

### 8.7 依赖与版本约束

```toml
[project]
name = "shell-agent"
version = "0.1.0"
requires-python = ">=3.11"

dependencies = [
    "click>=8.1", "rich>=13.0", "fastapi>=0.110", "uvicorn[standard]>=0.29",
    "anyio>=4.0", "aiosqlite>=0.20", "pydantic>=2.5", "pyyaml>=6.0",
    "loguru>=0.7", "asyncssh>=2.14", "aiomysql>=0.2", "asyncpg>=0.29",
    "redis[hiredis]>=5.0", "aiokafka>=0.11", "asyncio-mqtt>=0.16",
    "motor>=3.3", "aiohttp>=3.9", "apscheduler>=3.10",
    "cryptography>=42.0", "gitpython>=3.1", "watchdog>=4.0",
]

[project.optional-dependencies]
mcp = ["mcp>=0.1"]
llm-openai = ["openai>=1.30"]
llm-anthropic = ["anthropic>=0.25"]
llm-qwen = ["dashscope>=1.20"]
llm-glm = ["zhipuai>=2.0"]
llm-deepseek = ["openai>=1.30"]
llm-all = ["openai>=1.30", "anthropic>=0.25", "dashscope>=1.20", "zhipuai>=2.0"]

[project.scripts]
opsane = "shell_agent.cli:main"
```

---

## 9. 错误处理与可观测性

### 9.1 错误分级

| 等级 | 含义 | 处理方式 |
|------|------|---------|
| `fatal` | 系统无法启动 | 启动中止，输出明确错误 |
| `error` | 当前请求失败 | 返回错误给用户，记录日志 |
| `warning` | 可恢复的异常 | 降级继续，记录 warning |
| `info` | 正常事件 | 记录 info 日志 |
| `debug` | 调试信息 | 仅 debug 模式输出 |

### 9.2 错误分类与处理策略

**执行器错误**：

```
执行命令失败
  │
  ├─ 网络类（连接超时/断开）
  │    └─ 重试 3 次，间隔 1s/2s/4s（指数退避）
  │    └─ 仍失败 → 返回 error，建议检查网络/凭证
  │
  ├─ 认证类（密码错/key 拒绝）
  │    └─ 不重试 → 返回 error
  │
  ├─ 语法类（SQL 错/命令不存在）
  │    └─ 不重试 → 返回 error + 详情
  │    └─ LLM 生成的 SQL → 可选触发"修正模式"重试一次
  │
  ├─ 超时类
  │    └─ 不重试 → 返回 timeout
  │
  └─ 未知类
       └─ 不重试 → 返回 error + 异常栈（debug 模式）
```

**LLM 错误**：

```
LLM 调用失败
  │
  ├─ 超时/限流
  │    └─ 切到 fallback provider
  │    └─ 仍失败 → 当前任务降级:
  │          ├─ param_extraction 失败 → 提示用户手动填参数
  │          ├─ skill_matching 失败 → 走 free_form_agent
  │          ├─ llm_skill_reasoning 失败 → 提示改用模板 Skill
  │          └─ nl_to_sql 失败 → 提示手写 SQL
  │
  ├─ 认证错（API key 无效）
  │    └─ 不切 fallback → 直接报错
  │
  └─ 模型不可用
       └─ 不切 fallback → 报错
```

### 9.3 日志体系

三处输出：
1. **控制台**（启动 + 关键事件，Rich 着色）
2. **文件**（全量日志，按天滚动，`data/logs/shell-agent-YYYY-MM-DD.log`）
3. **SQLite**（结构化事件，便于查询）

### 9.4 指标采集（Prometheus 兼容）

```python
# GET /metrics (Prometheus 格式)
# shell_agent_requests_total{entry="cli",source="human"} 1234
# shell_agent_command_duration_seconds_bucket{executor="ssh",le="1.0"} 456
# shell_agent_llm_tokens_total{provider="openai",task_type="param_extraction"} 12300
# shell_agent_llm_cost_usd_total{provider="openai"} 0.45
# shell_agent_active_sessions 12
# shell_agent_executor_health{executor="ssh"} 1
# shell_agent_skill_invocations_total{skill="deploy_service",result="success"} 8
```

### 9.5 健康检查端点

```python
# GET /health
{
    "status": "healthy",          # healthy / degraded / unhealthy
    "uptime_seconds": 86400,
    "components": {
        "ssh_executor": {"healthy": true, "active_connections": 3},
        "mysql_executor": {"healthy": true, "instances": 4},
        "llm_openai": {"healthy": true, "latency_ms": 234},
        "monitor": {"healthy": true, "active_collectors": 6}
    },
    "stats": {
        "active_sessions": 3,
        "commands_today": 45,
        "errors_today": 1
    }
}
```

### 9.6 调试模式

```bash
opsane run "..." --debug    # 命令分类细节、LLM prompt/response、Skill 匹配过程
opsane run "..." --trace   # 每个 span 的耗时
```

### 9.7 关键错误场景的兜底

| 场景 | 兜底策略 |
|------|---------|
| SQLite 文件被删/损坏 | 启动时检测，损坏则备份后重建；审计改写文件日志 |
| 所有 LLM provider 挂了 | 模板 Skill 仍可用；直接命令路径不受影响 |
| 凭证文件被改坏 | schema 校验失败 → 拒绝加载该凭证 → 标记实例不可用 → 启动继续但告警 |
| 监控调度器崩溃 | 独立协程，watchdog 检测后重启 |
| 配置热加载出错 | 加载失败回滚到上一份配置 + 告警 |
| 磁盘满 | 主动告警 + 拒绝新请求 + 提示清理 |

### 9.8 进程级守护

用 systemd/supervisor 守护进程（外部工具）：

```ini
# /etc/systemd/system/opsane.service
[Service]
ExecStart=/usr/bin/opsane serve
Restart=always
RestartSec=5
```

---

## 10. 实现优先级与里程碑

### 10.1 阶段总览

```
阶段 1: MVP 核心              阶段 2: 安全完善          阶段 3: 智能增强          阶段 4: 全功能
(能跑起来 + 基本闭环)         (生产可用基础)            (Skill 与 LLM 双路)       (监控 + 多组件)

┌────────────────┐         ┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│ CLI + SSH      │         │ 分类器四级    │         │ 模板 Skill   │         │ 监控调度器    │
│ 直接命令路径    │  ──→    │ 环境策略      │  ──→    │ LLM Skill    │  ──→    │ 告警引擎      │
│ 简单确认流程    │         │ 审计日志      │         │ Skill 匹配器 │         │ 配置版本管理  │
│ 单 LLM provider│         │ MCP 接入     │         │ 上下文压缩   │         │ 多组件执行器  │
│ Inventory 基础 │         │ 配置热加载    │         │ 任务类型路由 │         │ 插件机制      │
└────────────────┘         └──────────────┘         └──────────────┘         └──────────────┘
```

### 10.2 阶段 1：MVP 核心

**目标**：从零搭建项目，能通过 CLI 用自然语言或直接命令在 SSH 服务器上执行命令。

**交付物**：
- 项目骨架（pyproject.toml、目录结构、CI）
- CLI 入口（`opsane run` / `opsane exec`）
- Inventory 加载（YAML → 别名解析）
- SSH 执行器（asyncssh、连接池）
- 单 LLM provider
- 简单确认流程（预览 → y/n）
- SQLite 存储
- 基础日志

**验收标准**：
```bash
opsane exec "ssh prod-order-01 'df -h'"
opsane run "查看 prod-order-01 磁盘使用情况"
```

### 10.3 阶段 2：安全完善

**目标**：补齐安全控制与审计，达到生产可用基础。

**交付物**：
- 静态分类器（四级判定）
- 环境策略（生产环境额外限制、二次确认）
- 完整确认工作流（interactive / auto_safe / dry_run）
- 审计日志 + CLI 查询
- MCP 接入层
- 配置热加载 + 变更审计
- 错误处理与重试策略
- 健康检查端点
- 凭证加密存储

**验收标准**：
```bash
# 危险命令被拦截
opsane exec "ssh prod-order-01 'rm -rf /tmp/*'"

# auto_safe 模式只放行只读
opsane run "查 prod-order-01 CPU" --auto-safe

# Trae 里能用 MCP 工具
```

### 10.4 阶段 3：智能增强

**目标**：Skill 引擎落地，模板 + LLM 双路。

**交付物**：
- Skill 引擎（模板加载、参数提取、步骤执行）
- Skill 匹配器（5 级 fallback）
- LLM Skill（多步推理、工具调用）
- Skill 生命周期
- LLM 任务类型路由（5 种任务类型）
- 上下文压缩与摘要
- 主备 LLM provider + 降级
- 5 个示例 Skill
- "自动总结"提示入口

**验收标准**：
```bash
# 模板 Skill
opsane run "发版 order 服务到测试 v1.2.3"

# LLM Skill
opsane run "prod-order-01 CPU 一直 90%, 帮我排查"

# 自然语言转 SQL
opsane run "查 prod_order_db 最近一周下单失败的订单"
```

### 10.5 阶段 4：全功能

**目标**：监控告警、多组件、插件机制、配置版本管理。

**交付物**：
- 监控调度器（APScheduler）
- 4 个内置采集器
- 告警引擎 + 规则配置
- 告警触发 Skill
- 配置版本管理（Git）
- 多组件执行器（MySQL/Redis/Kafka/MQTT/MongoDB/PostgreSQL/HTTP）
- 插件机制（多版本、自动探测）
- HTTP API + Webhook 入口
- Prometheus 指标端点
- Trace 追踪

**验收标准**：
```bash
# 监控自动触发
# CPU 持续 > 80% 5 分钟 → 自动触发 troubleshoot_high_cpu (auto_safe)

# 多组件
opsane run "查 prod_cache 的连接数和 hit rate"

# 配置回滚
opsane config rollback config-snapshot-20260701-1015-before-deploy
```

### 10.6 阶段 1 内部实现顺序

```
1. 项目骨架 + pyproject.toml
2. 配置加载（PyYAML + pydantic）
3. Inventory 模块
4. SSH 执行器
5. SQLite 存储层
6. 简单确认流程
7. LLM 适配层（单 provider）
8. CLI 入口
9. 会话调度器
10. 审计落盘
11. 端到端测试
```

### 10.7 测试策略

| 层 | 测试方式 | 覆盖重点 |
|----|---------|---------|
| 单元测试 | pytest | 分类器规则、参数提取、配置解析、上下文压缩 |
| 集成测试 | pytest + mock | 执行器与确认工作流联动、Skill 端到端 |
| E2E 测试 | Docker 容器作 target | 真实 SSH 到容器 |
| 安全测试 | 专项用例 | 黑名单命中、二次确认、生产环境拦截 |
| 性能测试 | locust | 并发会话、连接池上限 |

---

## 11. 风险评估与缓解

### 11.1 安全风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| LLM 生成危险命令误执行 | 高 | 所有命令走确认工作流；分类器兜底；生产环境二次确认 |
| 凭证泄露 | 高 | 文件权限 600；加密存储；日志/审计不输出明文；内存保护 |
| SQL 注入 | 中 | 参数化查询；LLM 生成 SQL 经分类器审查；用户确认 |
| SSH 跳板被绕过 | 中 | 凭证按机器绑定；Inventory 限制可达目标 |
| 配置被恶意修改 | 中 | 配置变更记审计；safety 规则改动需二次确认 |

### 11.2 可用性风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| LLM 误理解意图 | 高 | 确认工作流兜底；模板 Skill 优先 |
| LLM 生成命令语法错 | 中 | 执行器返回错误详情；可选 LLM 修正模式重试一次 |
| 连接池耗尽 | 中 | 单机上限 50；超限排队；监控告警 |
| 上下文压缩丢失关键信息 | 中 | 摘要保留关键发现/排除方向/待验证 |
| Skill 匹配错误 | 低 | 5 级 fallback；用户可显式 skill_hint |

### 11.3 性能风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| LLM 延迟高 | 中 | 任务类型路由；流式输出；超时降级 |
| 监控采集压目标 | 中 | 采集间隔 60s 起；连接池复用 |
| SQLite 并发瓶颈 | 低 | aiosqlite + WAL 模式；写入量不大 |
| 大量长输出 | 低 | 默认截断；LLM 上下文压缩 |

### 11.4 运维风险

| 风险 | 等级 | 缓解措施 |
|------|------|---------|
| Agent 自己挂了 | 中 | systemd 守护；健康检查；自动重启 |
| 版本升级破坏兼容 | 中 | 配置 schema 版本化；迁移脚本 |
| 磁盘日志爆满 | 中 | 按天滚动 + 保留 N 天；监控磁盘 |
| LLM 成本失控 | 中 | 任务类型路由省钱；成本监控 |

### 11.5 关键风险 Top 3 与对策

**① LLM 误执行危险命令（最高风险）**
- 多层防护：分类器 + 确认工作流 + 生产二次确认 + 审计
- 永远不"完全信任 LLM"，所有命令都经过人审
- 测试覆盖：所有 forbidden_patterns 有专项测试

**② 凭证泄露**
- 加密存储 + 文件权限 + 日志脱敏
- 不在 LLM 上下文里暴露真实 host/密码（只用别名）
- 定期审计凭证访问记录

**③ 范围失控**
- 四阶段严格交付，每阶段独立可用
- 阶段 1 能用就先用，用着才知道真正需要什么
- 避免一次性把所有执行器/Skill/监控都做完

---

## 附录 A：设计决策记录

| 维度 | 决策 | 理由 |
|------|------|------|
| 交互形态 | 长驻服务 + 多入口 | 覆盖日常交互、IDE 集成、外部调用、定时任务 |
| 服务器接入 | SSH + 可插拔扩展 | SSH 覆盖大部分场景，插件保留扩展 |
| 安全控制 | 多层防护体系 | 生产环境运维工具的标准要求 |
| Skill 机制 | 模板 + LLM 双路 | 高频任务可靠（模板）+ 复杂任务灵活（LLM） |
| 资源管理 | 四项全要 | 完整运维平台能力 |
| LLM 接入 | 云 + 本地可切换 | 灵活性 + 数据合规 |
| 架构方案 | 混合架构 | 兼顾能力、可扩展、Trae 生态融合 |
| 凭证管理 | 三种模式 + 加密 | 覆盖不同安全需求 |
| MySQL 路径 | 内置直连，不依赖 MCP | 性能 + 精细控制 |
| 插件版本 | 多版本并存 + 自动探测 | 适应不同版本服务端 |
| 上下文管理 | 压缩摘要（非截断） | 保留关键信息 |
| LLM 路由 | 按任务类型路由 | 成本可控 |
| 配置版本 | Git | 与文本配置文件天然契合 |
| 进程模型 | 单进程多协程 | 短中期不需要多进程 |
| 部署守护 | systemd/supervisor | 进程级守护 |

---

## 附录 B：术语表

| 术语 | 含义 |
|------|------|
| AgentRequest | 四个入口统一转换的内部请求模型 |
| PendingCommand | 命令规范化后的对象，含 target/executor/actual_command |
| Skill | 可复用的运维能力封装，分模板和 LLM 两种 |
| 分类器 | 静态判定命令危险等级的组件，四级（safe/medium/dangerous/forbidden） |
| 确认工作流 | 所有命令必经的预览→分类→确认→执行→审计流程 |
| Inventory | 服务器清单，提供别名到真实 host 的映射 |
| version_probe | 插件提供的版本探测函数 |
| 任务类型 | LLM 调用的分类（5 种），决定用哪个模型 |
| 快照 | 配置版本管理的某个时间点标签 |
