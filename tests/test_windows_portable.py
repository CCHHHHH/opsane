from pathlib import Path
import tomllib

from shell_agent import __version__


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_DIR = ROOT / "packaging" / "windows"


def test_cli_version_matches_release_version() -> None:
    with open(ROOT / "pyproject.toml", "rb") as stream:
        project_version = tomllib.load(stream)["project"]["version"]

    assert __version__ == project_version


def test_portable_runtime_has_no_target_python_install_step() -> None:
    start_script = (WINDOWS_DIR / "runtime" / "start.ps1").read_text()
    lowered = start_script.lower()

    assert "opsane.exe" in lowered
    assert "127.0.0.1" in lowered
    assert "python -m" not in lowered
    assert "pip install" not in lowered
    assert "venv" not in lowered


def test_portable_build_uses_blank_templates_and_smoke_test() -> None:
    build_script = (WINDOWS_DIR / "build_portable.ps1").read_text()

    assert "packaging\\local\\templates\\config\\credentials.yaml" in build_script
    assert 'Join-Path $Root "config\\credentials.yaml"' not in build_script
    assert "Invoke-WebRequest" in build_script
    assert "Compress-Archive" in build_script
    assert "Get-FileHash -Algorithm SHA256" in build_script


def test_pyinstaller_bundle_collects_web_and_dynamic_runtime_modules() -> None:
    spec = (WINDOWS_DIR / "Opsane.spec").read_text()

    assert "shell_agent/web/static" in spec
    assert 'for package_dir in ("domains", "runbooks")' in spec
    assert 'collect_submodules("shell_agent")' in spec
    assert '"uvicorn.protocols.http.auto"' in spec
    assert 'name="Opsane"' in spec


def test_windows_workflow_builds_on_native_x64_runner() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-portable.yml").read_text()

    assert "runs-on: windows-latest" in workflow
    assert 'architecture: "x64"' in workflow
    assert "build_portable.ps1" in workflow
    assert "actions/upload-artifact@v4" in workflow


def test_runtime_launchers_are_ascii_for_windows_powershell_compatibility() -> None:
    launchers = sorted((WINDOWS_DIR / "runtime").glob("*.*"))
    assert launchers

    for launcher in launchers:
        data = launcher.read_bytes()
        assert all(byte < 128 for byte in data), launcher.name
