$ErrorActionPreference = "Stop"

$OpsaneHome = if ($env:OPSANE_HOME) { $env:OPSANE_HOME } else { Join-Path $HOME ".opsane" }
$PidFile = Join-Path $OpsaneHome "run\opsane.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "Opsane 当前未运行。"
    exit 0
}

$ProcessId = [int](Get-Content $PidFile)
$Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item $PidFile -Force
    Write-Host "Opsane 当前未运行，已清理过期状态。"
    exit 0
}

Stop-Process -Id $ProcessId
$Process.WaitForExit(10000)
if (-not $Process.HasExited) {
    throw "Opsane 未在 10 秒内退出，请检查进程 $ProcessId。"
}

Remove-Item $PidFile -Force
Write-Host "Opsane 已停止。"
