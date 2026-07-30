$ErrorActionPreference = "Stop"

$BundleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDir = if ($env:OPSANE_PORTABLE_HOME) {
    $env:OPSANE_PORTABLE_HOME
} else {
    Join-Path $BundleDir "data"
}
$Port = if ($env:OPSANE_PORT) { [int]$env:OPSANE_PORT } else { 8010 }
$OpsaneExe = Join-Path $BundleDir "Opsane.exe"
$PidFile = Join-Path $DataDir "run\opsane.pid"
$OutLog = Join-Path $DataDir "data\logs\opsane-console.log"
$ErrLog = Join-Path $DataDir "data\logs\opsane-error.log"
$HealthUrl = "http://127.0.0.1:$Port/api/state"
$AppUrl = "http://127.0.0.1:$Port/next/#/chat"

function Copy-IfMissing([string]$Source, [string]$Target) {
    if (-not (Test-Path $Target)) {
        Copy-Item $Source $Target
    }
}

function Initialize-PortableData {
    @(
        (Join-Path $DataDir "config\safety"),
        (Join-Path $DataDir "data\logs"),
        (Join-Path $DataDir "data\session_files"),
        (Join-Path $DataDir "skills\templates"),
        (Join-Path $DataDir "run")
    ) | ForEach-Object {
        New-Item -ItemType Directory -Force -Path $_ | Out-Null
    }

    $TemplateConfig = Join-Path $BundleDir "templates\config"
    Copy-IfMissing (Join-Path $TemplateConfig "agent.yaml") (Join-Path $DataDir "config\agent.yaml")
    Copy-IfMissing (Join-Path $TemplateConfig "credentials.yaml") (Join-Path $DataDir "config\credentials.yaml")
    Copy-IfMissing (Join-Path $TemplateConfig "inventory.yaml") (Join-Path $DataDir "config\inventory.yaml")
    Copy-IfMissing (Join-Path $TemplateConfig "safety\env_policies.yaml") (Join-Path $DataDir "config\safety\env_policies.yaml")
    Copy-IfMissing (Join-Path $TemplateConfig "safety\safe_commands.yaml") (Join-Path $DataDir "config\safety\safe_commands.yaml")
    Copy-IfMissing (Join-Path $TemplateConfig "safety\forbidden_patterns.yaml") (Join-Path $DataDir "config\safety\forbidden_patterns.yaml")

    Get-ChildItem (Join-Path $BundleDir "templates\skills") -Filter "*.yaml" | ForEach-Object {
        Copy-IfMissing $_.FullName (Join-Path $DataDir ("skills\templates\" + $_.Name))
    }
}

function Test-Health {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 1 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Show-FailureLogs {
    if ($Process -and $Process.HasExited) {
        Write-Host "Opsane process exited with code $($Process.ExitCode)."
    }
    foreach ($LogPath in @($OutLog, $ErrLog)) {
        if (Test-Path $LogPath) {
            Write-Host ""
            Write-Host "===== $LogPath ====="
            Get-Content $LogPath -Tail 100
        }
    }
}

if (-not (Test-Path $OpsaneExe)) {
    throw "Opsane.exe is missing. Extract the complete portable ZIP before starting."
}

Initialize-PortableData

if (Test-Path $PidFile) {
    $ExistingPid = [int](Get-Content $PidFile -ErrorAction SilentlyContinue)
    $ExistingProcess = Get-Process -Id $ExistingPid -ErrorAction SilentlyContinue
    if ($ExistingProcess) {
        if (Test-Health) {
            Write-Host "Opsane is already running: $AppUrl"
            if ($env:OPSANE_NO_BROWSER -ne "1") { Start-Process $AppUrl }
            exit 0
        }
        throw "Process $ExistingPid is running, but the Opsane health check failed."
    }
    Remove-Item $PidFile -Force
}

$Process = Start-Process -FilePath $OpsaneExe `
    -ArgumentList @("serve", "--config", "config/agent.yaml", "--host", "127.0.0.1", "--port", "$Port") `
    -WorkingDirectory $DataDir `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -WindowStyle Hidden `
    -PassThru
Set-Content -Path $PidFile -Value $Process.Id -Encoding ascii

for ($Attempt = 0; $Attempt -lt 60; $Attempt++) {
    if (Test-Health) {
        Write-Host "Opsane started: $AppUrl"
        Write-Host "Data directory: $DataDir"
        if ($env:OPSANE_NO_BROWSER -ne "1") { Start-Process $AppUrl }
        exit 0
    }
    $Process.Refresh()
    if ($Process.HasExited) { break }
    Start-Sleep -Milliseconds 500
}

Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
Show-FailureLogs
throw "Opsane failed to start. Check $OutLog and $ErrLog."
