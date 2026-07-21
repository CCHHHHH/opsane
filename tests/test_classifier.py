from shell_agent.safety.classifier import RiskLevel, classify_command


def test_configured_safe_pattern_can_mark_custom_read_command_safe(tmp_path, monkeypatch) -> None:
    safety_dir = tmp_path / "config" / "safety"
    safety_dir.mkdir(parents=True)
    (safety_dir / "safe_commands.yaml").write_text(
        "patterns:\n  - '^\\s*redis-cli\\s+info\\b'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = classify_command("redis-cli info memory")

    assert result.level == RiskLevel.SAFE
    assert result.rules == ["known_read_only"]


def test_configured_safe_pattern_cannot_bypass_compound_command_guard(tmp_path, monkeypatch) -> None:
    safety_dir = tmp_path / "config" / "safety"
    safety_dir.mkdir(parents=True)
    (safety_dir / "safe_commands.yaml").write_text(
        "patterns:\n  - '^\\s*df\\b'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = classify_command("df -h; useradd backdoor")

    assert result.level == RiskLevel.CAUTION
    assert result.rules == ["compound_shell_command"]


def test_configured_risk_pattern_can_mark_custom_command_critical(tmp_path, monkeypatch) -> None:
    safety_dir = tmp_path / "config" / "safety"
    safety_dir.mkdir(parents=True)
    (safety_dir / "forbidden_patterns.yaml").write_text(
        "\n".join(
            [
                "patterns:",
                "  - name: custom_cache_flush",
                "    level: critical",
                "    pattern: '\\bcustomctl\\s+flush-cache\\b'",
                "    reason: '自定义缓存清理需要高风险确认'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = classify_command("customctl flush-cache all")

    assert result.level == RiskLevel.CRITICAL
    assert "custom_cache_flush" in result.rules


def test_classifies_known_read_only_command_as_safe() -> None:
    result = classify_command("df -h")

    assert result.level == RiskLevel.SAFE
    assert result.rules == ["known_read_only"]


def test_classifies_tail_latest_log_command_as_safe() -> None:
    result = classify_command(
        'tail -n 200 "$(ls -t /data/app/app/logs/*.log /data/app/app/logs/*.out 2>/dev/null | head -n 1)"'
    )

    assert result.level == RiskLevel.SAFE
    assert result.rules == ["known_read_only"]


def test_classifies_shell_location_commands_as_safe() -> None:
    assert classify_command("pwd").level == RiskLevel.SAFE
    assert classify_command("cd /data/app && pwd").level == RiskLevel.SAFE
    assert classify_command("cd '/data/app logs' && pwd").level == RiskLevel.SAFE


def test_classifies_builtin_read_only_compound_commands_as_safe() -> None:
    commands = [
        "ps -ef | grep java | grep -v grep",
        'uptime && echo "---" && free -h && echo "---" && df -h',
        'tail -n 200 "$(ls -t /data/app/logs/*.log /data/app/logs/*.out 2>/dev/null | head -n 1)"',
    ]

    for command in commands:
        result = classify_command(command)
        assert result.level == RiskLevel.SAFE, command
        assert result.rules == ["known_read_only"], command


def test_classifies_unapproved_compound_commands_as_caution() -> None:
    commands = [
        "df -h; useradd backdoor",
        "ls -la && touch /tmp/changed",
        "uptime; sed -i s/a/b/ /etc/example",
        "df -h $(useradd backdoor)",
        "df -h & useradd backdoor",
        "df -h < <(useradd backdoor)",
        "cd /tmp; useradd backdoor && pwd",
        "ps -ef | grep java | useradd backdoor",
        'uptime && echo "---" && free -h && useradd backdoor',
    ]

    for command in commands:
        result = classify_command(command)
        assert result.level == RiskLevel.CAUTION, command
        assert result.rules == ["compound_shell_command"], command


def test_compound_operators_inside_single_quotes_are_not_shell_syntax() -> None:
    result = classify_command("grep 'ready|healthy' /var/log/app.log")

    assert result.level == RiskLevel.SAFE
    assert result.rules == ["known_read_only"]


def test_known_dangerous_rule_still_wins_for_compound_command() -> None:
    result = classify_command("df -h; rm app.log")

    assert result.level == RiskLevel.DANGEROUS
    assert "rm_delete" in result.rules


def test_classifies_recursive_forced_remove_as_critical() -> None:
    result = classify_command("rm -rf /var/lib/app/cache")

    assert result.level == RiskLevel.CRITICAL
    assert "rm_recursive" in result.rules


def test_classifies_service_restart_as_dangerous() -> None:
    result = classify_command("systemctl restart order-service")

    assert result.level == RiskLevel.DANGEROUS
    assert "service_mutation" in result.rules


def test_classifies_delete_without_where_as_critical() -> None:
    result = classify_command("mysql -e \"DELETE FROM orders\"")

    assert result.level == RiskLevel.CRITICAL
    assert "sql_delete_without_where" in result.rules


def test_classifies_update_with_where_as_dangerous() -> None:
    result = classify_command("mysql -e \"UPDATE orders SET status='closed' WHERE id=1\"")

    assert result.level == RiskLevel.DANGEROUS
    assert "sql_update_with_where" in result.rules


def test_classifies_unknown_command_as_caution() -> None:
    result = classify_command("custom-tool inspect production")

    assert result.level == RiskLevel.CAUTION
    assert result.rules == ["unknown_command"]
