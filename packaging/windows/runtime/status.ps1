$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = if ($env:OPSANE_PORTABLE_HOME) {
    $env:OPSANE_PORTABLE_HOME
} else {
    Join-Path $BundleDir "data"
}
$Port = if ($env:OPSANE_PORT) { [int]$env:OPSANE_PORT } else { 8010 }
$PidFile = Join-Path $DataDir "run\opsane.pid"
$HealthUrl = "http://127.0.0.1:$Port/api/state"

if (Test-Path $PidFile) {
    $ProcessId = [int](Get-Content $PidFile -ErrorAction SilentlyContinue)
    $Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($Process) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 1 | Out-Null
            Write-Host "Opsane is running: PID $ProcessId, http://127.0.0.1:$Port"
            exit 0
        } catch {
            Write-Host "Process $ProcessId exists, but the health check failed."
            exit 2
        }
    }
}

Write-Host "Opsane is not running."
exit 1
