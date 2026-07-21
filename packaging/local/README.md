# Opsane 本地版

这是供个人电脑运行的 Opsane Web 版本。程序只监听 `127.0.0.1`，浏览器、配置、
SSH 凭证、会话文件和审计记录都保存在当前用户电脑上。

本安装包不包含打包者的模型密钥、SSH 凭证、服务器清单、会话历史或上传文件。

## 环境要求

- Python 3.11 或更高版本。
- 首次安装能够访问 Python 软件源以下载运行依赖。
- 本机能够访问模型 API，并能通过 SSH 连接目标服务器。
- 日常使用不需要 Node.js。

## macOS / Linux

1. 首次使用运行 `install.command`。
2. 安装完成后运行 `start.command`。
3. 浏览器会自动打开 `http://127.0.0.1:8010/next/#/chat`。
4. 使用 `status.command` 查看状态，使用 `stop.command` 停止服务。

也可以在终端执行：

```bash
./install.command
./start.command
./status.command
./stop.command
```

macOS 首次运行下载得到的脚本时，可在 Finder 中右键脚本并选择“打开”。

## Windows PowerShell

在当前 PowerShell 窗口允许本地脚本后执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
.\start.ps1
.\status.ps1
.\stop.ps1
```

## 首次进入 Opsane

1. 在“配置 > LLM”填写模型、API Key 和 Base URL，并测试连接。
2. 在“资源 > SSH 凭证”添加自己的凭证。
3. 在“资源 > 服务器”添加服务器并绑定凭证。
4. 使用“安全自动”模式执行第一条只读查询。

## 数据位置

默认用户数据目录：

- macOS / Linux：`~/.opsane`
- Windows：`%USERPROFILE%\.opsane`

重新运行新版安装包中的安装脚本会升级程序，但不会覆盖已有配置、会话和审计数据。
备份或迁移时复制整个 `.opsane` 目录。

## 安全说明

- 不要把启动地址改为 `0.0.0.0`。
- 当前版本没有 Web 登录和多用户 RBAC，不要将它作为共享公网服务。
- `.opsane/config` 中包含 API Key 和 SSH 凭证，不要发送给其他人。
- 文件传输、部署、删除和服务启停等操作仍需核对目标与风险确认。
