$ErrorActionPreference = "Stop"

$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$OpsaneHome = if ($env:OPSANE_HOME) { $env:OPSANE_HOME } else { Join-Path $HOME ".opsane" }
$VenvDir = Join-Path $OpsaneHome ".venv"

function Copy-IfMissing([string]$Source, [string]$Target) {
    if (-not (Test-Path $Target)) {
        Copy-Item $Source $Target
    }
}

function Test-Python([string]$Command, [string[]]$PrefixArgs) {
    try {
        & $Command @PrefixArgs -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($Version in @("-3.12", "-3.11", "-3.13")) {
        if (Test-Python "py" @($Version)) {
            $PythonCommand = "py"
            $PythonPrefix = @($Version)
            break
        }
    }
}
if (-not $PythonCommand -and (Get-Command python -ErrorAction SilentlyContinue)) {
    if (Test-Python "python" @()) {
        $PythonCommand = "python"
    }
}
if (-not $PythonCommand) {
    throw "需要安装 Python 3.11 或更高版本。"
}

$Wheel = Get-ChildItem (Join-Path $BundleDir "packages") -Filter "*.whl" | Select-Object -First 1
if (-not $Wheel) {
    throw "安装包中没有找到 Opsane wheel。"
}

Write-Host "Opsane 本地安装"
Write-Host "数据目录: $OpsaneHome"

@(
    (Join-Path $OpsaneHome "config\safety"),
    (Join-Path $OpsaneHome "data\logs"),
    (Join-Path $OpsaneHome "data\session_files"),
    (Join-Path $OpsaneHome "skills\templates"),
    (Join-Path $OpsaneHome "run")
) | ForEach-Object { New-Item -ItemType Directory -Force -Path $_ | Out-Null }

$TemplateConfig = Join-Path $BundleDir "templates\config"
Copy-IfMissing (Join-Path $TemplateConfig "agent.yaml") (Join-Path $OpsaneHome "config\agent.yaml")
Copy-IfMissing (Join-Path $TemplateConfig "credentials.yaml") (Join-Path $OpsaneHome "config\credentials.yaml")
Copy-IfMissing (Join-Path $TemplateConfig "inventory.yaml") (Join-Path $OpsaneHome "config\inventory.yaml")
Copy-IfMissing (Join-Path $TemplateConfig "safety\env_policies.yaml") (Join-Path $OpsaneHome "config\safety\env_policies.yaml")
Copy-IfMissing (Join-Path $TemplateConfig "safety\safe_commands.yaml") (Join-Path $OpsaneHome "config\safety\safe_commands.yaml")
Copy-IfMissing (Join-Path $TemplateConfig "safety\forbidden_patterns.yaml") (Join-Path $OpsaneHome "config\safety\forbidden_patterns.yaml")

Get-ChildItem (Join-Path $BundleDir "templates\skills") -Filter "*.yaml" | ForEach-Object {
    Copy-IfMissing $_.FullName (Join-Path $OpsaneHome ("skills\templates\" + $_.Name))
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "正在创建独立 Python 环境..."
    & $PythonCommand @PythonPrefix -m venv $VenvDir
}

Write-Host "正在安装 Opsane 及运行依赖，首次安装需要访问 Python 软件源..."
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
& $VenvPython -m pip install --upgrade $Wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "Opsane 安装失败。"
}

Copy-Item (Join-Path $BundleDir "VERSION") (Join-Path $OpsaneHome "VERSION") -Force
Write-Host "安装完成。运行 .\start.ps1 启动 Opsane。"
