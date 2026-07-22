from types import SimpleNamespace

import pytest
import yaml

from shell_agent.executors.ssh import parse_ssh_command
from shell_agent.skills.discovery import discover_skill_candidates
from shell_agent.skills.engine import match_template_skill
from shell_agent.skills.loader import parse_template_skill_data
from shell_agent.storage.database import connect, init_db
from shell_agent.storage.sessions import ensure_session
from shell_agent.storage.skill_candidates import (
    expire_skill_candidates,
    get_skill_candidate,
    list_skill_candidates,
    review_skill_candidate,
)
from shell_agent.storage.tasks import add_task_event, create_task, update_task
from shell_agent.web.routes import skill_candidates as skill_candidate_routes
from shell_agent.web.schemas import SkillCandidateScanRequest


async def _successful_task(
    db,
    index: int,
    command: str,
    *,
    snapshot=None,
    title: str = "检查 Java 进程",
) -> str:
    session_id = f"sess_discovery_{index}"
    await ensure_session(db, session_id, session_type="chat", title=f"检查 Java 进程 {index}")
    task = await create_task(db, session_id, "chat", title=title)
    await update_task(
        db,
        task["id"],
        status="completed",
        workflow_snapshot=snapshot,
        completed=True,
    )
    await add_task_event(
        db,
        task["id"],
        session_id,
        "chat",
        "execution_result",
        status="success",
        step_index=1,
        content="ok",
        payload={
            "command": command,
            "target": f"dev-0{index}",
            "exit_code": 0,
            "output": "ok",
        },
    )
    return task["id"]


class FakeSemanticLLM:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or {"groups": []}
        self.error = error
        self.calls: list[tuple[list[dict], int]] = []

    async def cluster_skill_flows(self, flows, *, min_occurrences=3):
        self.calls.append((flows, min_occurrences))
        if self.error:
            raise self.error
        return self.response


@pytest.mark.asyncio
async def test_discovers_repeated_successful_flow_as_disabled_skill(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(
                db,
                index,
                f"ssh dev-0{index} 'ps -ef | grep java | grep -v grep'",
            )

        result = await discover_skill_candidates(db, days=30, min_occurrences=3)

        assert result["scanned_tasks"] == 3
        assert result["repeated_groups"] == 1
        assert len(result["created"]) == 1
        candidate = result["created"][0]
        assert candidate["status"] == "pending"
        assert candidate["occurrence_count"] == 3
        draft = yaml.safe_load(candidate["draft_yaml"])
        assert draft["enabled"] is False
        assert draft["params"][0]["type"] == "server_alias"
        assert draft["steps"][0]["command"] == "ssh {{target}} 'ps -ef | grep java | grep -v grep'"
        parse_template_skill_data(draft, tmp_path / "candidate.yaml")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_discovery_uses_structured_target_and_preserves_quoted_actual_command(
    tmp_path,
) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        command = "grep 'error' /var/log/app.log"
        for index in range(1, 4):
            await _successful_task(db, index, command, title="检查错误日志")

        result = await discover_skill_candidates(db, days=30, min_occurrences=3)

        draft = yaml.safe_load(result["created"][0]["draft_yaml"])
        assert draft["params"][0]["name"] == "target"
        skill = parse_template_skill_data(draft, tmp_path / "quoted.yaml")
        matched = match_template_skill(
            "检查错误日志 dev-01",
            server_aliases=["dev-01"],
            skills=[skill],
        )
        assert matched is not None
        assert matched.steps[0]["command"].startswith("ssh dev-01 ")
        assert parse_ssh_command(matched.steps[0]["command"]) == (
            "dev-01",
            command,
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_discovery_excludes_existing_skill_and_failed_flows(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(
                db,
                index,
                f"ssh dev-0{index} 'uptime'",
                snapshot={"source": "skill", "skill_name": "resource_summary"},
            )
        result = await discover_skill_candidates(db, days=30, min_occurrences=3)
        assert result["scanned_tasks"] == 0
        assert result["created"] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_discovery_drops_commands_containing_redacted_secrets(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(
                db,
                index,
                f"ssh dev-0{index} 'curl -H token=private-token http://127.0.0.1/health'",
            )
        result = await discover_skill_candidates(
            db,
            days=30,
            min_occurrences=3,
            secret_values=["private-token"],
        )
        assert result["scanned_tasks"] == 0
        assert result["created"] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_candidate_review_is_single_use(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(db, index, f"ssh dev-0{index} 'uptime'")
        result = await discover_skill_candidates(db, days=30, min_occurrences=3)
        candidate_id = result["created"][0]["id"]

        accepted = await review_skill_candidate(
            db, candidate_id, "accepted", published_skill_name="learned_test"
        )
        duplicate = await review_skill_candidate(db, candidate_id, "rejected")

        assert accepted is not None
        assert accepted["status"] == "accepted"
        assert duplicate is None
        assert await list_skill_candidates(db, status="pending") == []
        assert (await get_skill_candidate(db, candidate_id))["published_skill_name"] == "learned_test"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pending_candidate_expires_after_retention_window(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(db, index, f"ssh dev-0{index} 'uptime'")
        result = await discover_skill_candidates(db, days=30, min_occurrences=3)
        candidate_id = result["created"][0]["id"]
        await db.execute(
            "UPDATE skill_candidates SET expires_at = '2000-01-01T00:00:00' WHERE id = ?",
            (candidate_id,),
        )
        await db.commit()

        assert await expire_skill_candidates(db) == 1
        assert (await get_skill_candidate(db, candidate_id))["status"] == "expired"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_semantic_group_compiles_only_bounded_path_and_integer_params(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        task_ids = []
        for index, (lines, path) in enumerate(
            ((100, "/var/log/app.log"), (200, "/opt/log/app.log"), (300, "/srv/log/app.log")),
            start=1,
        ):
            task_ids.append(
                await _successful_task(
                    db,
                    index,
                    f"ssh dev-0{index} 'tail -n {lines} {path}'",
                    title=f"查看 {path} 最近 {lines} 行",
                )
            )
        llm = FakeSemanticLLM(
            {
                "groups": [
                    {
                        "task_ids": task_ids,
                        "label": "查看应用日志",
                        "description": "查看指定应用日志的最近若干行",
                        "rationale": "步骤目标与顺序一致",
                        "commands": ["rm -rf /"],
                        "yaml": "enabled: true",
                    }
                ]
            }
        )

        result = await discover_skill_candidates(
            db,
            days=30,
            min_occurrences=3,
            semantic=True,
            llm=llm,
        )

        assert result["exact_groups"] == 0
        assert result["semantic_groups"] == 1
        assert result["semantic"]["status"] == "completed"
        candidate = result["created"][0]
        assert candidate["evidence"]["grouping_mode"] == "semantic"
        assert candidate["evidence"]["parameterized_fields"] == ["path", "number"]
        draft = yaml.safe_load(candidate["draft_yaml"])
        assert draft["enabled"] is False
        assert [item["name"] for item in draft["params"]] == ["target", "path", "number"]
        assert draft["steps"][0]["command"] == "ssh {{target}} 'tail -n {{number}} {{path}}'"
        assert "rm -rf" not in candidate["draft_yaml"]
        skill = parse_template_skill_data(draft, tmp_path / "semantic.yaml")
        matched = match_template_skill(
            "查看 /opt/log/app.log 最近 200 行",
            server_aliases=["dev-01"],
            default_target="dev-01",
            skills=[skill],
        )
        assert matched is not None
        assert matched.missing_params == []
        assert matched.steps[0]["command"] == "ssh dev-01 'tail -n 200 /opt/log/app.log'"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_semantic_group_rejects_unknown_ids_and_uncompilable_structure(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        task_ids = []
        for index, command in enumerate(
            ("systemctl status alpha", "journalctl -u beta", "ps -ef | grep gamma"),
            start=1,
        ):
            task_ids.append(
                await _successful_task(db, index, f"ssh dev-0{index} '{command}'")
            )
        llm = FakeSemanticLLM(
            {
                "groups": [
                    {"task_ids": [*task_ids[:2], "unknown-task"], "label": "未知 ID"},
                    {"task_ids": task_ids, "label": "结构不同"},
                ]
            }
        )

        result = await discover_skill_candidates(
            db, semantic=True, llm=llm, min_occurrences=3
        )

        assert result["created"] == []
        assert result["semantic_groups"] == 0
        assert result["semantic"]["rejected_groups"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_semantic_failure_keeps_exact_candidates_and_redacts_model_input(tmp_path) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        for index in range(1, 4):
            await _successful_task(db, index, f"ssh exact-{index} 'uptime'")
        for index, path in enumerate(
            (("/var/log/a.log"), ("/opt/log/b.log"), ("/srv/log/c.log")),
            start=11,
        ):
            await _successful_task(
                db,
                index,
                f"ssh semantic-{index} 'tail {path}'",
                title=f"private-token 查看 {path}",
            )
        llm = FakeSemanticLLM(error=TimeoutError("provider detail must not leak"))

        result = await discover_skill_candidates(
            db,
            semantic=True,
            llm=llm,
            min_occurrences=3,
            secret_values=["private-token"],
        )

        assert len(result["created"]) == 1
        assert result["exact_groups"] == 1
        assert result["semantic"]["status"] == "failed"
        assert result["semantic"]["error_type"] == "TimeoutError"
        serialized = str(llm.calls)
        assert "private-token" not in serialized
        assert "provider detail" not in str(result)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scan_api_passes_explicit_semantic_mode_to_configured_llm(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "db.sqlite"
    await init_db(str(db_path))
    db = await connect(str(db_path))
    try:
        task_ids = []
        for index, path in enumerate(
            ("/var/log/a.log", "/opt/log/b.log", "/srv/log/c.log"), start=1
        ):
            task_ids.append(
                await _successful_task(
                    db,
                    index,
                    f"ssh dev-0{index} 'tail {path}'",
                    title=f"查看 {path}",
                )
            )
        llm = FakeSemanticLLM(
            {"groups": [{"task_ids": task_ids, "label": "查看指定日志"}]}
        )
        runtime = SimpleNamespace(db=db, llm=llm, secret_values=lambda: [])
        monkeypatch.setattr(skill_candidate_routes, "get_runtime", lambda: runtime)

        response = await skill_candidate_routes.scan_skill_candidates(
            SkillCandidateScanRequest(days=30, min_occurrences=3, semantic=True)
        )

        assert response["ok"] is True
        assert response["semantic"]["status"] == "completed"
        assert response["semantic_groups"] == 1
        assert llm.calls[0][1] == 3
    finally:
        await db.close()
