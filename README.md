# Opsane

Opsane 是一个自然语言驱动的智能运维工作台。推荐以**本地单用户 Web 应用**运行：
Opsane 服务只启动在使用者自己的电脑上，通过浏览器完成模型配置、服务器管理、
自然语言排障、文件传输、部署确认和审计查询。

```text
浏览器
  -> http://127.0.0.1:8010
  -> 本机 Opsane Web 服务
  -> SSH / SFTP
  -> 已配置的服务器
```

当前版本适合个人或小范围同事试用。每位使用者运行自己的 Opsane 实例，配置、SSH
凭证、会话文件和审计记录保存在本机。由于尚未提供 Web 登录和 RBAC，请勿直接将
服务暴露到公网或共享办公网络。

## 本地 Web 快速开始

### 1. 环境要求

- Python 3.11 或更高版本。
- Chrome、Edge、Safari 等现代浏览器。
- 本机能够访问所使用的模型 API。
- 本机能够通过 SSH 连接需要管理的服务器。
- 日常使用不需要安装 Node.js；只有修改或重新构建前端时才需要 Node.js。

先确认 Python 版本：

```bash
python3 --version
```

### 2. 安装 Opsane

在 macOS 或 Linux 上执行：

```bash
cd /path/to/AI-Shell
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

在 Windows PowerShell 中执行：

```powershell
cd C:\path\to\AI-Shell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

项目中的正式配置文件缺失时，从示例初始化。已有配置不要覆盖：

```bash
cp -n config/agent.yaml.example config/agent.yaml
cp -n config/credentials.yaml.example config/credentials.yaml
cp -n config/inventory.yaml.example config/inventory.yaml
cp -n config/safety/env_policies.yaml.example config/safety/env_policies.yaml
cp -n config/safety/safe_commands.yaml.example config/safety/safe_commands.yaml
cp -n config/safety/forbidden_patterns.yaml.example config/safety/forbidden_patterns.yaml
```

Windows PowerShell 使用下面的等价命令：

```powershell
if (!(Test-Path config\agent.yaml)) { Copy-Item config\agent.yaml.example config\agent.yaml }
if (!(Test-Path config\credentials.yaml)) { Copy-Item config\credentials.yaml.example config\credentials.yaml }
if (!(Test-Path config\inventory.yaml)) { Copy-Item config\inventory.yaml.example config\inventory.yaml }
if (!(Test-Path config\safety\env_policies.yaml)) { Copy-Item config\safety\env_policies.yaml.example config\safety\env_policies.yaml }
if (!(Test-Path config\safety\safe_commands.yaml)) { Copy-Item config\safety\safe_commands.yaml.example config\safety\safe_commands.yaml }
if (!(Test-Path config\safety\forbidden_patterns.yaml)) { Copy-Item config\safety\forbidden_patterns.yaml.example config\safety\forbidden_patterns.yaml }
```

### 3. 启动 Web 服务

始终从项目根目录启动，避免相对配置路径指向错误位置：

```bash
cd /path/to/AI-Shell
./.venv/bin/opsane serve --host 127.0.0.1 --port 8010
```

Windows PowerShell：

```powershell
cd C:\path\to\AI-Shell
.\.venv\Scripts\opsane.exe serve --host 127.0.0.1 --port 8010
```

启动后在浏览器打开：

- `http://127.0.0.1:8010/`：Opsane 工作台。
- `http://127.0.0.1:8010/next/#/chat`：直接进入聊天页。

服务运行期间请保留启动终端。按 `Ctrl+C` 停止 Opsane；再次执行相同启动命令即可
恢复使用，会话、配置和审计记录不会因为正常重启而丢失。

### 4. 首次使用

首次进入 Web 页面后，按照页面的新手引导完成：

1. 打开“配置 > LLM”，填写模型、API Key 和 Base URL，然后测试连接。
2. 打开“资源 > SSH 凭证”，新增密码或私钥凭证。
3. 打开“资源 > 服务器”，新增服务器并绑定 SSH 凭证。
4. 在“配置 > 安全策略”检查不同环境的执行限制。
5. 进入“聊天”，使用安全自动模式发送第一条只读请求。

推荐从以下任务开始验证：

```text
查看 dev-01 的 CPU、内存和磁盘使用情况
```

```text
检查 dev-01 上 nginx 是否安装并正在运行
```

模型生成的命令仍会经过目标校验、风险分类、环境策略和确认流程。首次试用不要直接
从生产环境写操作开始。

## 本地数据与备份

当前版本的数据保存在项目目录中：

| 路径 | 内容 |
|---|---|
| `config/agent.yaml` | 模型、SSH 和会话配置 |
| `config/credentials.yaml` | SSH 凭证，包含敏感信息 |
| `config/inventory.yaml` | 服务器与服务画像 |
| `config/safety/` | 风险规则和环境策略 |
| `data/shell_agent.db` | 会话、任务、记忆和审计记录 |
| `data/session_files/` | 用户上传到会话的文件 |
| `data/logs/` | Opsane 本地运行日志 |
| `skills/templates/` | Skill 模板 |

升级、迁移或重新安装前，至少备份 `config/`、`data/` 和自定义的
`skills/templates/`。不要把包含 API Key、SSH 密码或私钥的配置提交到代码仓库，
也不要将整个项目目录发送给其他人。

## 安全边界

- 本地试用固定使用 `--host 127.0.0.1`，不要改成 `0.0.0.0`。
- 当前 Web 页面没有登录和多用户权限隔离，不适合作为多人共享服务直接部署。
- API Key、SSH 凭证、会话文件和审计数据都保存在本机，应限制项目目录访问权限。
- 安全自动模式只自动执行满足策略的低风险操作，其他操作仍需要人工确认。
- 文件传输、部署、删除、服务启停和数据库写操作必须核对目标、影响和验证方式。
- 当前主要执行器为 SSH/SFTP，适合本机或受控内网中的单实例使用。

## 会话文件与服务器传输

聊天输入框支持点击 `＋`、粘贴或拖拽文件。文件加入会话后只保存在本机，不会自动
连接服务器、执行命令或部署。

可以通过自然语言发起传输：

```text
把 bedcare-mock.jar 上传到 dev-01 的 /tmp/shell-agent-uploads 目录
```

只有会话文件、服务器别名和远端绝对目录都明确时，系统才会生成传输预览。用户确认
后，Opsane 使用 SFTP 暂存文件，校验文件大小和 SHA-256，再原子发布到目标路径。
完全访问模式也不能跳过文件传输确认。

Office 文件预览可选依赖本机 LibreOffice：

```bash
# macOS
brew install --cask libreoffice

# Debian / Ubuntu
sudo apt-get install libreoffice
```

缺少 LibreOffice 不影响原文件上传、下载和服务器传输，只会降低旧版 Office 文件的
版式预览能力。

## 会话 JAR 部署

聊天会话上传 `.jar` 后，可以从文件面板发起部署。系统只匹配名称唯一、已经验证的
`dev/test` 单机服务画像；匹配不唯一时会阻止执行，不会让模型猜目标。

部署流程固定为：

```text
只读预检
  -> 部署方案确认
  -> SFTP 暂存与校验
  -> 备份当前制品
  -> 停止服务
  -> 原子替换
  -> 启动服务
  -> 状态、健康和制品校验
```

验证失败后必须再次确认才会回滚。首版不支持生产环境、多主机滚动发布或无人值守
自动回滚。

## 常见问题

### 页面无法访问

确认启动终端没有报错，并检查访问地址和启动端口是否一致。如果 `8010` 已被占用，
可以改用其他本地端口：

```bash
./.venv/bin/opsane serve --host 127.0.0.1 --port 8011
```

### 重启后配置或会话不存在

确认每次都从同一个项目根目录启动。当前版本的 `config/`、`data/` 和 Skill 路径以
启动工作目录为基准。

### 无法连接服务器

先确认本机网络能够到达目标服务器，并核对服务器地址、SSH 端口、用户名、密码或
私钥。首次连接未知主机时，根据实际安全要求配置主机指纹信任策略。

### 模型调用失败

在“配置 > LLM”重新测试 API Key、Base URL 和模型名称，并查看 `data/logs/` 中的
本地日志。

## 生成同事试用包

不要把整个源码目录直接发送给同事。执行下面的命令生成本地 Web 发行包：

```bash
./scripts/build_local_release.sh
```

构建结果：

```text
release/Opsane-<version>-local.zip
release/Opsane-<version>-local.zip.sha256
```

只需要发送 ZIP；SHA-256 文件可用于接收方校验。发行包包含 Python wheel、macOS/Linux
与 Windows 的安装和启停脚本、空白配置及 Skill 模板，不包含源码开发依赖、`.venv`、
`node_modules`、本机会话数据、真实模型密钥或 SSH 凭证。

同事解压后阅读包内 `README.md`，首次运行安装脚本，之后通过启动脚本打开本地 Web
页面。首次安装需要 Python 3.11 或更高版本，并需要访问 Python 软件源下载运行依赖。
用户数据独立保存在 `~/.opsane`，安装新版时不会覆盖已有配置和会话。

## 开发者说明

日常 Web 使用不需要下面的命令。本节仅用于开发、调试和自动化测试。

### CLI

命令行入口为 `opsane`：

```bash
opsane run "查看 dev-01 的磁盘使用情况"
opsane exec "ssh dev-01 'df -h'"
opsane shell
opsane audit query --target dev-01
```

### 前端开发

```bash
cd shell_agent/web/frontend
npm install
npm run dev
```

Vite 开发服务器负责热更新，并把 `/api` 和 `/ws` 代理到本地 Opsane 服务。发布构建
使用 `npm run build`，产物写入 `shell_agent/web/static/next/` 并随 Python wheel
分发，因此普通用户运行 Web 版本不需要 Node.js。

### 测试

安装开发依赖并执行完整质量门禁：

```bash
python -m pip install -e ".[dev]"
bash scripts/quality.sh
```

浏览器测试使用隔离的 HTTP/WebSocket 服务，不读取真实服务器清单、不连接 SSH，也
不调用 LLM。首次运行需要安装测试浏览器：

```bash
npm --prefix shell_agent/web/frontend run test:e2e:install
```

## 产品与设计文档

- [产品说明书](docs/shell-agent-product-manual.md)：功能、使用流程、安全模式和当前边界。
- [设计文档](docs/superpowers/specs/2026-07-01-shell-agent-design.md)：项目早期架构与设计背景。

## 当前实现概览

- 六个工作区：聊天、终端、资源、配置、记忆、审计。
- 多会话自然语言任务、连续工作流、多服务器采集、操作方案和最终结论。
- 四种聊天权限模式、四级风险分类、环境策略、二次确认和灾难性命令硬阻断。
- 服务器、SSH 凭证、服务画像、全局记忆和画像候选管理。
- 会话文件、SFTP 校验传输和单机 Java JAR 部署 Runbook。
- SQLite 状态持久化、页面刷新恢复、任务终止和审计记录。

完整产品边界以产品说明书为准。
