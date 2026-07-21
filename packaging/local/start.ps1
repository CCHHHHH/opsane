$ErrorActionPreference = "Stop"

$OpsaneHome = if ($env:OPSANE_HOME) { $env:OPSANE_HOME } else { Join-Path $HOME ".opsane" }
$Port = if ($env:OPSANE_PORT) { [int]$env:OPSANE_PORT } else { 8010 }
$OpsaneExe = Join-Path $OpsaneHome ".venv\Scripts\opsane.exe"
$PidFile = Join-Path $OpsaneHome "run\opsane.pid"
$OutLog = Join-Path $OpsaneHome "data\logs\opsane-console.log"
$ErrLog = Join-Path $OpsaneHome "data\logs\opsane-error.log"
$HealthUrl = "http://127.0.0.1:$Port/api/state"
$AppUrl = "http://127.0.0.1:$Port/next/#/chat"

function Test-Health {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 1 | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-Path $OpsaneExe)) {
    throw "Opsane 尚未安装，请先运行 install.ps1。"
}

New-Item -ItemType Directory -Force -Path (Split-Path $PidFile), (Split-Path $OutLog) | Out-Null

if (Test-Path $PidFile) {
    $ExistingPid = [int](Get-Content $PidFile -ErrorAction SilentlyContinue)
    $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
    if ($ExistingProcess) {
        if (Test-Health) {
            Write-Host "Opsane 已在运行：$AppUrl"
            if ($env:OPSANE_NO_BROWSER -ne "1") { Start-Process $AppUrl }
            exit 0
        }
        throw "Opsane 进程 $ExistingPid 仍在运行，但健康检查未通过。"
    }
    Remove-Item $PidFile -Force
}

$Process = Start-Process -FilePath $OpsaneExe `
    -ArgumentList @("serve", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $OpsaneHome `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru
Set-Content -Path $PidFile -Value $Process.Id

for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
    if (Test-Health) {
        Write-Host "Opsane 已启动：$AppUrl"
        if ($env:OPSANE_NO_BROWSER -ne "1") { Start-Process $AppUrl }
        exit 0
    }
    if ($Process.HasExited) { break }
    Start-Sleep -Milliseconds 500
}

throw "Opsane 启动失败，请查看 $OutLog 和 $ErrLog。"
