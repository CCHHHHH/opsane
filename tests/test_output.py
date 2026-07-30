from shell_agent.utils.output import console


def test_console_avoids_legacy_windows_renderer() -> None:
    assert console.legacy_windows is False
