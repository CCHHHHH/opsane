import shlex

import pytest

from shell_agent.core.models import AgentRequest, InputType
from shell_agent.executors.ssh import (
    SSHExecutor,
    expand_common_shell_aliases,
    parse_ssh_command,
    truncate_output,
)
from shell_agent.llm.adapter import LLMAdapter
from shell_agent.utils.config import Credential, LLMConfig, ServerEntry, SSHConfig


def test_parse_ssh_command_with_quotes() -> None:
    assert parse_ssh_command("ssh prod-order-01 'df -h'") == (
        "prod-order-01",
        "df -h",
    )
    assert parse_ssh_command('ssh prod-order-01 "tail -n 100 /var/log/app.log"') == (
        "prod-order-01",
        "tail -n 100 /var/log/app.log",
    )
    quoted = "grep 'error' /var/log/app.log"
    assert parse_ssh_command(
        f"ssh prod-order-01 {shlex.quote(quoted)}"
    ) == ("prod-order-01", quoted)


def test_parse_ssh_command_without_quotes() -> None:
    assert parse_ssh_command("ssh prod-order-01 uptime") == (
        "prod-order-01",
        "uptime",
    )


def test_parse_ssh_command_rejects_non_ssh() -> None:
    assert parse_ssh_command("df -h") is None


def test_ssh_executor_normalize_uses_configured_alias_case() -> None:
    executor = SSHExecutor(
        servers={
            "dev-01": ServerEntry(
                alias="dev-01",
                host="127.0.0.1",
                ssh_credential="default",
            )
        },
        credentials={},
    )

    command = executor.normalize("ssh DEV-01 'df -h'")

    assert command.target == "dev-01"
    assert command.actual_command == "df -h"


def test_expand_common_shell_aliases() -> None:
    assert expand_common_shell_aliases("ll") == "ls -l"
    assert expand_common_shell_aliases("ll -s") == "ls -l -s"
    assert expand_common_shell_aliases("ll /data/app | head") == "ls -l /data/app | head"
    assert expand_common_shell_aliases("la /tmp") == "ls -la /tmp"
    assert expand_common_shell_aliases("l&&pwd") == "ls -CF&&pwd"
    assert expand_common_shell_aliases("llama") == "llama"


def test_ssh_executor_normalize_expands_common_shell_aliases() -> None:
    executor = SSHExecutor(
        servers={
            "dev-01": ServerEntry(
                alias="dev-01",
                host="127.0.0.1",
                ssh_credential="default",
            )
        },
        credentials={},
    )

    command = executor.normalize("ssh dev-01 'll /data/app'")

    assert command.actual_command == "ls -l /data/app"


def test_ssh_executor_can_trust_unknown_hosts() -> None:
    executor = SSHExecutor(
        servers={},
        credentials={},
        trust_unknown_hosts=True,
    )

    assert executor.trust_unknown_hosts is True


def test_ssh_config_trusts_unknown_hosts_by_default() -> None:
    assert SSHConfig().trust_unknown_hosts is True


@pytest.mark.asyncio
async def test_ssh_executor_passes_known_hosts_none_when_enabled(monkeypatch) -> None:
    captured = {}

    class FakeConnection:
        def is_closed(self) -> bool:
            return False

        def close(self) -> None:
            pass

    async def fake_connect(host, port, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["kwargs"] = kwargs
        return FakeConnection()

    monkeypatch.setattr("shell_agent.executors.ssh.asyncssh.connect", fake_connect)
    executor = SSHExecutor(
        servers={
            "dev-03": ServerEntry(
                alias="dev-03",
                host="10.0.100.14",
                ssh_credential="dev-03",
            )
        },
        credentials={
            "dev-03": Credential(
                id="dev-03",
                type="password",
                username="root",
                password="secret",
            )
        },
        trust_unknown_hosts=True,
    )

    await executor._get_connection("dev-03")

    assert captured["host"] == "10.0.100.14"
    assert captured["kwargs"]["known_hosts"] is None


def test_agent_request_detects_command_or_natural_language() -> None:
    assert (
        AgentRequest(input="ssh prod-order-01 'df -h'").detect_input_type()
        == InputType.COMMAND
    )
    assert (
        AgentRequest(input="查看 prod-order-01 磁盘使用情况").detect_input_type()
        == InputType.NATURAL
    )


def test_llm_response_parser_accepts_json_block_and_text() -> None:
    parser = LLMAdapter(LLMConfig())._parse_response

    parsed = parser(
        '```json\n{"command": "ssh prod-order-01 df -h", "intent": "check disk"}\n```'
    )
    assert isinstance(parsed, dict)
    assert parsed["command"] == "ssh prod-order-01 df -h"
    assert parsed["intent"] == "check disk"

    assert parser("需要更多信息") == "需要更多信息"


def test_truncate_output_keeps_head_and_tail() -> None:
    text = "a" * 20 + "b" * 20
    truncated, was_truncated = truncate_output(text, limit=20)
    assert was_truncated is True
    assert truncated.startswith("a" * 10)
    assert truncated.endswith("b" * 10)
    assert "truncated" in truncated
