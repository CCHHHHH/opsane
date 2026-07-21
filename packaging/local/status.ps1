$OpsaneHome = if ($env:OPSANE_HOME) { $env:OPSANE_HOME } else { Join-Path $HOME ".opsane" }
$Port = if ($env:OPSANE_PORT) { [int]$env:OPSANE_PORT } else { 8010 }
$PidFile = Join-Path $OpsaneHome "run\opsane.pid"

if (Test-Path $PidFile) {
    $ProcessId = [int](Get-Content $PidFile -ErrorAction SilentlyContinue)
    if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        Write-Host "Opsane 正在运行：PID $ProcessId，http://127.0.0.1:$Port"
        exit 0
    }
}

Write-Host "Opsane 当前未运行。"
exit 1
