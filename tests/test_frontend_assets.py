"""Checks for the built Vue workbench shipped by the Python package."""

from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = PROJECT_ROOT / "shell_agent" / "web"


def test_vue_workbench_build_references_existing_assets() -> None:
    static_root = WEB_ROOT / "static"
    index = static_root / "next" / "index.html"

    assert index.is_file()
    html = index.read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="(/next/[^"]+)"', html)

    assert references
    for reference in references:
        assert (static_root / reference.removeprefix("/")).is_file(), reference


def test_vue_components_use_transport_modules_instead_of_browser_globals() -> None:
    component_roots = [
        WEB_ROOT / "frontend" / "src" / "pages",
        WEB_ROOT / "frontend" / "src" / "components",
    ]

    for root in component_roots:
        for component in root.rglob("*.vue"):
            source = component.read_text(encoding="utf-8")
            assert "fetch(" not in source, component
            assert "new WebSocket" not in source, component
            assert "window." not in source, component


def test_legacy_chat_full_access_mode_has_visual_warning_contract() -> None:
    html = (WEB_ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="chat-full-access-warning"' in html
    assert "Opsane 当前拥有最大权限" in html
    assert ".chat-composer.full-access" in html
    assert ".chat-composer.full-access:focus-within" in html
    assert "function syncChatAccessMode()" in html
    assert "mode?.value === 'full_access'" in html
    assert "chatConfirmMode.addEventListener('change', syncChatAccessMode)" in html
