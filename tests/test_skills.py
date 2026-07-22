import sys
from pathlib import Path

import pytest

from shell_agent.skills.engine import match_template_skill
from shell_agent.skills.loader import load_template_skills, parse_template_skill_data


def test_loads_template_skills_from_yaml() -> None:
    skills = load_template_skills()

    names = {skill.name for skill in skills}
    assert "resource_summary" in names
    assert "java_processes" in names
    assert "list_directory" in names


def test_matches_directory_skill_and_renders_command() -> None:
    match = match_template_skill(
        "看下 dev-01 /data/app 目录下的内容",
        server_aliases=["dev-01", "dev-02"],
        default_target="dev-02",
    )

    assert match is not None
    assert match.skill.name == "list_directory"
    assert match.params["target"] == "dev-01"
    assert match.params["path"] == "/data/app"
    assert match.steps[0]["command"] == "ssh dev-01 'ls -la /data/app'"


def test_uses_default_target_when_user_does_not_mention_alias() -> None:
    match = match_template_skill(
        "看下 /opt/app 目录内容",
        server_aliases=["dev-01"],
        default_target="dev-01",
    )

    assert match is not None
    assert match.params["target"] == "dev-01"
    assert match.steps[0]["command"] == "ssh dev-01 'ls -la /opt/app'"


def test_template_skill_expands_multiple_targets() -> None:
    match = match_template_skill(
        "看下dev-01和dev-02上面运行了哪些java程序",
        server_aliases=["dev-01", "dev-02"],
        default_target="dev-01",
    )

    assert match is not None
    assert match.skill.name == "java_processes"
    assert [step["command"] for step in match.steps] == [
        "ssh dev-01 'ps -ef | grep java | grep -v grep'",
        "ssh dev-02 'ps -ef | grep java | grep -v grep'",
    ]


def test_reports_missing_required_path_for_directory_skill() -> None:
    match = match_template_skill(
        "看下目录内容",
        server_aliases=["dev-01"],
        default_target="dev-01",
    )

    assert match is not None
    assert match.skill.name == "list_directory"
    assert match.missing_params == ["path"]
    assert match.steps == []


def test_invalid_skill_file_is_skipped(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "bad.yaml").write_text("name: bad\nsteps: []\n", encoding="utf-8")

    assert load_template_skills(skill_dir) == []


def test_default_skill_path_falls_back_to_install_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_prefix = tmp_path / "venv"
    installed_skills = install_prefix / "skills" / "templates"
    installed_skills.mkdir(parents=True)
    (installed_skills / "installed.yaml").write_text(
        "name: installed\nsteps:\n  - command: uptime\n",
        encoding="utf-8",
    )
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(sys, "prefix", str(install_prefix))

    skills = load_template_skills()

    assert [skill.name for skill in skills] == ["installed"]
    assert skills[0].source_path == installed_skills / "installed.yaml"


def test_default_skill_path_prefers_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skills = tmp_path / "working" / "skills" / "templates"
    source_skills.mkdir(parents=True)
    (source_skills / "source.yaml").write_text(
        "name: source\nsteps:\n  - command: uptime\n",
        encoding="utf-8",
    )
    install_prefix = tmp_path / "venv"
    installed_skills = install_prefix / "skills" / "templates"
    installed_skills.mkdir(parents=True)
    (installed_skills / "installed.yaml").write_text(
        "name: installed\nsteps:\n  - command: uptime\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path / "working")
    monkeypatch.setattr(sys, "prefix", str(install_prefix))

    skills = load_template_skills()

    assert [skill.name for skill in skills] == ["source"]
    assert skills[0].source_path == Path("skills/templates/source.yaml")


def test_explicit_missing_skill_path_does_not_fall_back_to_install_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_prefix = tmp_path / "venv"
    installed_skills = install_prefix / "skills" / "templates"
    installed_skills.mkdir(parents=True)
    (installed_skills / "installed.yaml").write_text(
        "name: installed\nsteps:\n  - command: uptime\n",
        encoding="utf-8",
    )
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.chdir(working_dir)
    monkeypatch.setattr(sys, "prefix", str(install_prefix))

    assert load_template_skills(tmp_path / "missing") == []


def test_skill_v2_renders_typed_shell_argument_and_step_policy(tmp_path: Path) -> None:
    skill = parse_template_skill_data(
        {
            "name": "service_status",
            "version": "3",
            "triggers": ["服务状态"],
            "params": [
                {
                    "name": "service",
                    "type": "shell_arg",
                    "required": True,
                    "extract": r"服务状态\s+(.+)$",
                }
            ],
            "steps": [
                {
                    "name": "查询服务",
                    "command": "systemctl status {{service}}",
                    "confirm": True,
                    "timeout_seconds": 17,
                    "on_failure": "continue",
                }
            ],
            "safety": {"default_confirm_mode": "auto_safe"},
        },
        tmp_path / "service_status.yaml",
    )

    match = match_template_skill(
        "服务状态 app; touch /tmp/pwned",
        server_aliases=[],
        skills=[skill],
    )

    assert match is not None
    assert match.steps[0]["command"] == "systemctl status 'app; touch /tmp/pwned'"
    assert match.steps[0]["confirm"] is True
    assert match.steps[0]["timeout_seconds"] == 17
    assert match.steps[0]["on_failure"] == "continue"
    assert match.steps[0]["skill_version"] == "3"
    assert len(match.steps[0]["skill_hash"]) == 64


def test_skill_rejects_untyped_string_in_command(tmp_path: Path) -> None:
    skill = parse_template_skill_data(
        {
            "name": "unsafe",
            "triggers": ["运行"],
            "params": [
                {"name": "value", "type": "string", "required": True, "extract": r"运行\s+(.+)$"}
            ],
            "steps": [{"command": "echo {{value}}"}],
        },
        tmp_path / "unsafe.yaml",
    )

    with pytest.raises(ValueError, match="不能直接插入命令"):
        match_template_skill("运行 hello", server_aliases=[], skills=[skill])
