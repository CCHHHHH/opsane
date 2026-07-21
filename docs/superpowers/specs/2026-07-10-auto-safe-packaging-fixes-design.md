# Auto-safe 与发布包修复设计

日期：2026-07-10

## 目标

本次只修复两个问题：

1. 防止以只读命令开头的复合 Shell 命令被错误判定为 `safe`，进而被 `auto_safe` 自动执行。
2. 确保 wheel 包含 Web 静态页面、内置 Skill 和配置示例，并让 wheel 安装后的 Skill 加载能够找到内置模板。

不调整 `interactive`、`dry_run`、`full_access` 的现有语义，不引入新的运行时依赖，也不扩展鉴权、凭证或其他安全功能。

## Auto-safe 设计

### 分类顺序

命令分类保持“高风险优先”的原则：

1. 先执行现有 critical、dangerous、caution 和自定义风险规则。
2. 未命中风险规则时，检查命令是否包含复合 Shell 语法。
3. 普通复合命令返回 `caution`，规则名为 `compound_shell_command`，因此不会被 `auto_safe` 自动执行。
4. 只有明确匹配内置只读组合的命令可以继续判为 `safe`。
5. 最后才应用普通只读命令和配置化安全命令规则。

这样可以保留现有危险等级，同时防止安全前缀掩盖后续未知命令。

### 复合语法边界

门禁覆盖以下可能改变控制流或产生副作用的语法：

- `;`、换行、`&`、`&&`、`||`
- 管道 `|`
- 输出重定向 `>`、`>>`、`2>` 等
- 命令替换 `$()`、反引号和进程替换 `<()` / `>()`

配置文件中的安全正则不能绕过该门禁；需要自动执行的复合命令必须进入代码中的明确只读组合清单。

### 明确放行的内置只读组合

为保持现有内置 Skill 和目录状态功能可用，初始只放行以下结构：

- `cd <path> && pwd`
- Java 进程查询：`ps ... | grep java | grep -v grep`
- 资源概览：`uptime && echo ... && free ... && echo ... && df ...`
- 最新日志查询：`tail ... "$(ls -t ... 2>/dev/null | head -n 1)"`

匹配使用完整命令结构，不使用“仅匹配开头”的方式。未明确列出的只读复合命令也会降级为 `caution`，由用户确认执行。

`/dev/null` 重定向本身不作为文件写入风险，但仍属于复合语法；只有上述完整的最新日志查询结构可以因此被自动放行。

## 发布包设计

### Web 静态文件

通过 setuptools package data 将 `shell_agent/web/static/index.html` 放入 wheel。FastAPI 继续通过模块目录定位静态文件，无需修改运行时代码。

### Skill 与配置示例

通过 setuptools data files 将以下资源放入安装前缀：

- `skills/templates/*.yaml` → `<prefix>/skills/templates/`
- `config/*.yaml.example` → `<prefix>/share/shell-agent/config/`
- `config/safety/*.yaml.example` → `<prefix>/share/shell-agent/config/safety/`

Skill 加载器按以下顺序解析默认目录：

1. 当前工作目录下存在 `skills/templates` 时使用它，保持源码运行和用户自定义行为不变。
2. 否则使用 `sys.prefix/skills/templates`，支持 wheel 安装后的内置模板。

显式传给 `load_template_skills(path)` 的路径不做替换。

## 错误处理与兼容性

- 风险判断不确定时返回 `caution`，不会静默回退为 `safe`。
- 已命中的 critical 或 dangerous 规则不会被复合命令门禁降低等级。
- 缺少安装版 Skill 目录时返回空列表，保持当前容错行为。
- 源码目录的 Skill 仍具有最高优先级，避免改变现有 Web Skill 编辑路径。
- wheel 的配置示例只作为分发资源，不写入或覆盖用户当前工作目录。

## 测试与验收

### 分类器回归

至少覆盖：

- `df -h; useradd backdoor` 为 `caution`。
- `ls -la && touch /tmp/changed` 为 `caution`。
- `uptime; sed -i ...` 为 `caution`。
- 已知 dangerous/critical 复合命令保持原等级。
- 四类内置只读组合保持 `safe`。
- 配置化安全前缀不能绕过复合命令门禁。

### Auto-safe 工作流

增加工作流测试，确认复合命令会进入人工确认路径，拒绝时执行器调用次数为零。

### 发布包验收

1. 构建 wheel 成功。
2. wheel 内容包含 `index.html`、4 个内置 Skill 和全部配置示例。
3. 将 wheel 安装到临时虚拟环境后，从非项目目录执行 Skill 加载，能够读取 4 个内置 Skill。
4. 运行完整 pytest 测试集，除已知的时间窗口测试隔离问题外无新增失败；本次若触及该测试环境，将同时消除其时间依赖。
