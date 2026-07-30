$ErrorActionPreference = "Stop"

$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = if ($env:OPSANE_PORTABLE_HOME) {
    $env:OPSANE_PORTABLE_HOME
} else {
    Join-Path $BundleDir "data"
}
$PidFile = Join-Path $DataDir "run\opsane.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "Opsane is not running."
    exit 0
}

$ProcessId = [int](Get-Content $PidFile)
$Process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
if (-not $Process) {
    Remove-Item $PidFile -Force
    Write-Host "Opsane is not running. Removed a stale PID file."
    exit 0
}

Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
$Stopped = $false
for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        $Stopped = $true
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $Stopped) {
    throw "Opsane process $ProcessId did not stop within 10 seconds."
}

Remove-Item $PidFile -Force
Write-Host "Opsane stopped."
