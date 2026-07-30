[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$OutputDir = "",
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutputDir) {
    $OutputDir = Join-Path $Root "release"
}
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)

$Version = (& $Python -c "import tomllib; print(tomllib.load(open(r'$Root\pyproject.toml', 'rb'))['project']['version'])").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) {
    throw "Unable to read the Opsane version with $Python."
}

$BuildRoot = Join-Path $Root ".build\windows-portable"
$VenvDir = Join-Path $BuildRoot "venv"
$DistDir = Join-Path $BuildRoot "dist"
$WorkDir = Join-Path $BuildRoot "work"
$PackageName = "Opsane-$Version-windows-x64"
$StageDir = Join-Path $OutputDir $PackageName
$ArchivePath = Join-Path $OutputDir "$PackageName.zip"
$HashPath = "$ArchivePath.sha256"

Remove-Item $BuildRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $StageDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $ArchivePath, $HashPath -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $BuildRoot, $OutputDir | Out-Null

Write-Host "Creating isolated Windows build environment..."
& $Python -m venv $VenvDir
if ($LASTEXITCODE -ne 0) { throw "Failed to create the build virtual environment." }

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }
& $VenvPython -m pip install --disable-pip-version-check $Root "pyinstaller>=6.10,<7" "pillow>=10"
if ($LASTEXITCODE -ne 0) { throw "Failed to install portable build dependencies." }

$FrontendIndex = Join-Path $Root "shell_agent\web\static\next\index.html"
if (-not (Test-Path $FrontendIndex)) {
    throw "The built Web frontend is missing. Run npm ci and npm run build first."
}

Write-Host "Building Opsane.exe with PyInstaller..."
$env:PYINSTALLER_CONFIG_DIR = Join-Path $BuildRoot "pyinstaller-cache"
& $VenvPython -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $DistDir `
    --workpath $WorkDir `
    (Join-Path $PSScriptRoot "Opsane.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$BuiltApp = Join-Path $DistDir "Opsane"
$BuiltExe = Join-Path $BuiltApp "Opsane.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "PyInstaller did not create Opsane.exe."
}

New-Item -ItemType Directory -Force -Path `
    $StageDir, `
    (Join-Path $StageDir "templates\config\safety"), `
    (Join-Path $StageDir "templates\skills") | Out-Null
Copy-Item (Join-Path $BuiltApp "*") $StageDir -Recurse -Force
Copy-Item (Join-Path $PSScriptRoot "runtime\*") $StageDir -Recurse -Force
Copy-Item (Join-Path $PSScriptRoot "README-WINDOWS.md") (Join-Path $StageDir "README.md")

Copy-Item (Join-Path $Root "config\agent.yaml.example") (Join-Path $StageDir "templates\config\agent.yaml")
Copy-Item (Join-Path $Root "packaging\local\templates\config\credentials.yaml") (Join-Path $StageDir "templates\config\credentials.yaml")
Copy-Item (Join-Path $Root "packaging\local\templates\config\inventory.yaml") (Join-Path $StageDir "templates\config\inventory.yaml")
Copy-Item (Join-Path $Root "config\safety\env_policies.yaml.example") (Join-Path $StageDir "templates\config\safety\env_policies.yaml")
Copy-Item (Join-Path $Root "config\safety\safe_commands.yaml.example") (Join-Path $StageDir "templates\config\safety\safe_commands.yaml")
Copy-Item (Join-Path $Root "config\safety\forbidden_patterns.yaml.example") (Join-Path $StageDir "templates\config\safety\forbidden_patterns.yaml")
Copy-Item (Join-Path $Root "skills\templates\*.yaml") (Join-Path $StageDir "templates\skills") -Force
Set-Content -Path (Join-Path $StageDir "VERSION") -Value $Version -Encoding ascii

$GitCommit = (& git -C $Root rev-parse --short HEAD 2>$null)
$BuildInfo = [ordered]@{
    product = "Opsane"
    version = $Version
    platform = "windows-x64"
    python = (& $VenvPython -c "import sys; print(sys.version.split()[0])").Trim()
    commit = if ($GitCommit) { $GitCommit.Trim() } else { "" }
    built_at_utc = [DateTime]::UtcNow.ToString("o")
}
$BuildInfo | ConvertTo-Json | Set-Content (Join-Path $StageDir "PORTABLE_BUILD.json") -Encoding utf8

& $BuiltExe --version
if ($LASTEXITCODE -ne 0) { throw "The packaged Opsane.exe version check failed." }

if (-not $SkipSmokeTest) {
    Write-Host "Running packaged Web startup smoke test..."
    $SmokeHome = Join-Path $BuildRoot "smoke-home"
    $env:OPSANE_PORTABLE_HOME = $SmokeHome
    $env:OPSANE_PORT = "18010"
    $env:OPSANE_NO_BROWSER = "1"
    try {
        & (Join-Path $StageDir "start.ps1")
        if ($LASTEXITCODE -ne 0) { throw "Portable start.ps1 failed." }
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:18010/api/state" -TimeoutSec 5 | Out-Null
    } finally {
        & (Join-Path $StageDir "stop.ps1")
        Remove-Item Env:\OPSANE_PORTABLE_HOME -ErrorAction SilentlyContinue
        Remove-Item Env:\OPSANE_PORT -ErrorAction SilentlyContinue
        Remove-Item Env:\OPSANE_NO_BROWSER -ErrorAction SilentlyContinue
    }
}

Write-Host "Creating portable archive..."
Compress-Archive -Path $StageDir -DestinationPath $ArchivePath -CompressionLevel Optimal
$Hash = (Get-FileHash -Algorithm SHA256 $ArchivePath).Hash.ToLowerInvariant()
"$Hash  $([System.IO.Path]::GetFileName($ArchivePath))" |
    Set-Content -Path $HashPath -Encoding ascii

Write-Host "Created $ArchivePath"
Write-Host "SHA-256 $Hash"
