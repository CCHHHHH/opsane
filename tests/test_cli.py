from types import SimpleNamespace

from click.testing import CliRunner

from shell_agent import cli


def test_serve_bootstrap_output_is_ascii_safe(monkeypatch, tmp_path) -> None:
    config = SimpleNamespace(
        logging=SimpleNamespace(level="INFO", dir=str(tmp_path / "logs"))
    )
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(cli, "setup_logging", lambda _level, _dir: None)

    import shell_agent.web.app as web_app
    import uvicorn

    monkeypatch.setattr(web_app, "create_app", lambda config_path: object())
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        cli.main,
        ["serve", "--host", "127.0.0.1", "--port", "8010"],
    )

    assert result.exit_code == 0
    assert result.output.isascii()
    assert "Opsane Web service starting: http://127.0.0.1:8010" in result.output
