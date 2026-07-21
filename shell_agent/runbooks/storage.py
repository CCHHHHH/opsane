"""SQLite persistence for deployment runs, steps, events and service locks."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Iterable
from uuid import uuid4

import aiosqlite

from .models import (
    DeploymentPlan,
    ExecutionResult,
    INTERRUPTIBLE_RUN_STATUSES,
    RunStatus,
    StepStatus,
    transition_allowed,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployment_runs (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    service_key TEXT NOT NULL,
    target TEXT NOT NULL,
    environment TEXT NOT NULL,
    runbook_id TEXT NOT NULL,
    runbook_version TEXT NOT NULL,
    status TEXT NOT NULL,
    profile_revision INTEGER NOT NULL,
    profile_snapshot TEXT NOT NULL,
    artifact_snapshot TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    confirmed_plan_hash TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    rollback_confirmed_by TEXT,
    rollback_confirmed_at TEXT,
    mutation_started INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(session_id, request_id)
);

CREATE TABLE IF NOT EXISTS deployment_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    phase TEXT NOT NULL,
    action TEXT NOT NULL,
    name TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    mutates_live INTEGER NOT NULL DEFAULT 0,
    arguments TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt INTEGER NOT NULL DEFAULT 0,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    details TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(run_id, step_id),
    FOREIGN KEY(run_id) REFERENCES deployment_runs(id)
);

CREATE TABLE IF NOT EXISTS deployment_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    type TEXT NOT NULL,
    status TEXT,
    step_id TEXT,
    message TEXT,
    payload TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence),
    FOREIGN KEY(run_id) REFERENCES deployment_runs(id)
);

CREATE TABLE IF NOT EXISTS deployment_locks (
    service_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    target TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    metadata TEXT,
    FOREIGN KEY(run_id) REFERENCES deployment_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_deployment_runs_session_time
ON deployment_runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_deployment_runs_status
ON deployment_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_deployment_steps_run_order
ON deployment_steps(run_id, step_index);
CREATE INDEX IF NOT EXISTS idx_deployment_events_run_order
ON deployment_events(run_id, sequence);

CREATE TRIGGER IF NOT EXISTS trg_deployment_plan_immutable
BEFORE UPDATE OF plan_json, plan_hash, profile_snapshot, artifact_snapshot,
                 runbook_id, runbook_version, profile_revision, session_id,
                 service_id, service_key, target, environment
ON deployment_runs
WHEN OLD.plan_json <> NEW.plan_json
  OR OLD.plan_hash <> NEW.plan_hash
  OR OLD.profile_snapshot <> NEW.profile_snapshot
  OR OLD.artifact_snapshot <> NEW.artifact_snapshot
  OR OLD.runbook_id <> NEW.runbook_id
  OR OLD.runbook_version <> NEW.runbook_version
  OR OLD.profile_revision <> NEW.profile_revision
  OR OLD.session_id <> NEW.session_id
  OR OLD.service_id <> NEW.service_id
  OR OLD.service_key <> NEW.service_key
  OR OLD.target <> NEW.target
  OR OLD.environment <> NEW.environment
BEGIN
    SELECT RAISE(ABORT, 'deployment plan is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_deployment_success_requires_postcheck
BEFORE UPDATE OF status ON deployment_runs
WHEN NEW.status IN ('succeeded', 'completed')
 AND (
    NOT EXISTS (
        SELECT 1 FROM deployment_steps
        WHERE run_id = OLD.id AND phase = 'postcheck'
    )
    OR EXISTS (
        SELECT 1 FROM deployment_steps
        WHERE run_id = OLD.id AND phase = 'postcheck' AND status <> 'success'
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'deployment success requires completed postchecks');
END;

CREATE TRIGGER IF NOT EXISTS trg_deployment_rollback_requires_postcheck
BEFORE UPDATE OF status ON deployment_runs
WHEN NEW.status = 'rolled_back'
 AND (
    NOT EXISTS (
        SELECT 1 FROM deployment_steps
        WHERE run_id = OLD.id AND phase = 'rollback_postcheck'
    )
    OR EXISTS (
        SELECT 1 FROM deployment_steps
        WHERE run_id = OLD.id AND phase = 'rollback_postcheck' AND status <> 'success'
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'rollback success requires completed postchecks');
END;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RunbookStorage:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def initialize(self) -> None:
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(_SCHEMA)
        await self.db.commit()

    async def create_run(
        self,
        *,
        request_id: str,
        session_id: str,
        plan: DeploymentPlan,
    ) -> tuple[dict[str, Any], bool]:
        now = _now_iso()
        service = plan.service
        service_key = f"{service.environment}:{service.target}:{service.service_id}"
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO deployment_runs (
                id, request_id, session_id, service_id, service_key, target,
                environment, runbook_id, runbook_version, status,
                profile_revision, profile_snapshot, artifact_snapshot,
                plan_json, plan_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.run_id,
                request_id,
                session_id,
                service.service_id,
                service_key,
                service.target,
                service.environment,
                plan.runbook_id,
                plan.runbook_version,
                RunStatus.CREATED.value,
                service.revision,
                _json(service.to_dict()),
                _json(plan.artifact.to_dict()),
                plan.canonical_json(),
                plan.plan_hash,
                now,
                now,
            ),
        )
        created = cursor.rowcount == 1
        if created:
            for index, step in enumerate(plan.steps):
                await self.db.execute(
                    """
                    INSERT INTO deployment_steps (
                        id, run_id, step_id, step_index, phase, action, name,
                        risk_level, mutates_live, arguments, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{plan.run_id}:{step.id}",
                        plan.run_id,
                        step.id,
                        index,
                        step.phase.value,
                        step.action,
                        step.name,
                        step.risk_level.value,
                        int(step.mutates_live),
                        _json(dict(step.arguments)),
                        StepStatus.PENDING.value,
                    ),
                )
            await self._append_event_no_commit(
                plan.run_id,
                event_type="run_created",
                status=RunStatus.CREATED,
                message="部署任务已创建，执行计划已冻结",
                payload={"plan_hash": plan.plan_hash},
            )
        await self.db.commit()
        if created:
            record = await self.get_run(plan.run_id)
        else:
            record = await self.get_run_by_request(session_id, request_id)
        if record is None:  # pragma: no cover - database invariant
            raise RuntimeError("部署任务创建失败")
        return record, created

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            "SELECT * FROM deployment_runs WHERE id = ?", (run_id,)
        )
        row = await cursor.fetchone()
        return self._decode_run(row)

    async def get_run_by_request(
        self, session_id: str, request_id: str
    ) -> dict[str, Any] | None:
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            """
            SELECT * FROM deployment_runs
            WHERE session_id = ? AND request_id = ?
            """,
            (session_id, request_id),
        )
        return self._decode_run(await cursor.fetchone())

    async def list_runs(
        self,
        *,
        session_id: str | None = None,
        statuses: Iterable[RunStatus] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        status_values = [status.value for status in (statuses or [])]
        if status_values:
            placeholders = ",".join("?" for _ in status_values)
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_values)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            f"""
            SELECT * FROM deployment_runs
            {where}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            params,
        )
        return [self._decode_run(row) for row in await cursor.fetchall()]

    @staticmethod
    def _decode_run(row: aiosqlite.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("profile_snapshot", "artifact_snapshot", "plan_json"):
            value[key] = json.loads(value[key])
        value["mutation_started"] = bool(value["mutation_started"])
        return value

    async def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            """
            SELECT * FROM deployment_steps
            WHERE run_id = ? ORDER BY step_index
            """,
            (run_id,),
        )
        rows = []
        for row in await cursor.fetchall():
            value = dict(row)
            value["arguments"] = json.loads(value["arguments"])
            value["details"] = json.loads(value["details"] or "{}")
            value["mutates_live"] = bool(value["mutates_live"])
            rows.append(value)
        return rows

    async def list_events(self, run_id: str) -> list[dict[str, Any]]:
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            """
            SELECT * FROM deployment_events
            WHERE run_id = ? ORDER BY sequence
            """,
            (run_id,),
        )
        result = []
        for row in await cursor.fetchall():
            value = dict(row)
            value["payload"] = json.loads(value["payload"] or "{}")
            result.append(value)
        return result

    async def transition(
        self,
        run_id: str,
        *,
        expected: RunStatus | Iterable[RunStatus],
        new: RunStatus,
        message: str = "",
        error: str = "",
        result_summary: str = "",
        completed: bool = False,
    ) -> bool:
        expected_values = (
            [expected] if isinstance(expected, RunStatus) else list(expected)
        )
        if not expected_values:
            raise ValueError("expected 状态不能为空")
        invalid = [state for state in expected_values if not transition_allowed(state, new)]
        if invalid:
            raise ValueError(
                f"非法部署状态迁移: {','.join(s.value for s in invalid)} -> {new.value}"
            )
        now = _now_iso()
        placeholders = ",".join("?" for _ in expected_values)
        cursor = await self.db.execute(
            f"""
            UPDATE deployment_runs
            SET status = ?, error = ?, result_summary = ?, updated_at = ?,
                completed_at = CASE WHEN ? THEN ? ELSE completed_at END
            WHERE id = ? AND status IN ({placeholders})
            """,
            (
                new.value,
                error[:4000],
                result_summary[:4000],
                now,
                int(completed),
                now,
                run_id,
                *(state.value for state in expected_values),
            ),
        )
        changed = cursor.rowcount == 1
        if changed:
            await self._append_event_no_commit(
                run_id,
                event_type="status_changed",
                status=new,
                message=message,
                payload={"error": error[:1000]} if error else {},
            )
        await self.db.commit()
        return changed

    async def confirm_plan(
        self, run_id: str, *, plan_hash: str, confirmed_by: str
    ) -> bool:
        """CAS confirmation: the exact frozen plan can only be approved once."""
        now = _now_iso()
        cursor = await self.db.execute(
            """
            UPDATE deployment_runs
            SET status = ?, confirmed_plan_hash = ?, confirmed_by = ?,
                confirmed_at = ?, updated_at = ?
            WHERE id = ? AND status = ? AND plan_hash = ?
              AND confirmed_plan_hash IS NULL
            """,
            (
                RunStatus.CONFIRMED.value,
                plan_hash,
                confirmed_by,
                now,
                now,
                run_id,
                RunStatus.WAITING_PLAN_CONFIRM.value,
                plan_hash,
            ),
        )
        changed = cursor.rowcount == 1
        if changed:
            await self._append_event_no_commit(
                run_id,
                event_type="plan_confirmed",
                status=RunStatus.CONFIRMED,
                message="用户已确认冻结的部署方案",
                payload={"plan_hash": plan_hash, "confirmed_by": confirmed_by},
            )
        await self.db.commit()
        return changed

    async def confirm_rollback(self, run_id: str, *, confirmed_by: str) -> bool:
        now = _now_iso()
        cursor = await self.db.execute(
            """
            UPDATE deployment_runs
            SET status = ?, rollback_confirmed_by = ?, rollback_confirmed_at = ?,
                updated_at = ?
            WHERE id = ? AND status = ? AND rollback_confirmed_at IS NULL
            """,
            (
                RunStatus.ROLLBACK_CONFIRMED.value,
                confirmed_by,
                now,
                now,
                run_id,
                RunStatus.ROLLBACK_REQUIRED.value,
            ),
        )
        changed = cursor.rowcount == 1
        if changed:
            await self._append_event_no_commit(
                run_id,
                event_type="rollback_confirmed",
                status=RunStatus.ROLLBACK_CONFIRMED,
                message="用户已确认回滚",
                payload={"confirmed_by": confirmed_by},
            )
        await self.db.commit()
        return changed

    async def mark_mutation_started(self, run_id: str) -> None:
        await self.db.execute(
            """
            UPDATE deployment_runs SET mutation_started = 1, updated_at = ?
            WHERE id = ?
            """,
            (_now_iso(), run_id),
        )
        await self.db.commit()

    async def start_step(self, run_id: str, step_id: str) -> bool:
        now = _now_iso()
        cursor = await self.db.execute(
            """
            UPDATE deployment_steps
            SET status = ?, attempt = attempt + 1, started_at = ?,
                completed_at = NULL, error = NULL
            WHERE run_id = ? AND step_id = ? AND status = ?
            """,
            (
                StepStatus.RUNNING.value,
                now,
                run_id,
                step_id,
                StepStatus.PENDING.value,
            ),
        )
        changed = cursor.rowcount == 1
        if changed:
            await self._append_event_no_commit(
                run_id,
                event_type="step_started",
                step_id=step_id,
                message="步骤开始执行",
            )
        await self.db.commit()
        return changed

    async def finish_step(
        self,
        run_id: str,
        step_id: str,
        *,
        result: ExecutionResult | None = None,
        error: str = "",
    ) -> bool:
        success = bool(result and result.success and not error)
        now = _now_iso()
        cursor = await self.db.execute(
            """
            UPDATE deployment_steps
            SET status = ?, exit_code = ?, stdout = ?, stderr = ?, details = ?,
                error = ?, completed_at = ?
            WHERE run_id = ? AND step_id = ? AND status = ?
            """,
            (
                StepStatus.SUCCESS.value if success else StepStatus.FAILED.value,
                result.exit_code if result else None,
                (result.stdout if result else "")[:100000],
                (result.stderr if result else "")[:100000],
                _json(dict(result.details) if result else {}),
                error[:4000] or ((result.stderr if result and not result.success else "")[:4000]),
                now,
                run_id,
                step_id,
                StepStatus.RUNNING.value,
            ),
        )
        changed = cursor.rowcount == 1
        if changed:
            await self._append_event_no_commit(
                run_id,
                event_type="step_finished",
                step_id=step_id,
                message="步骤执行成功" if success else "步骤执行失败",
                payload={"success": success, "exit_code": result.exit_code if result else None},
            )
        await self.db.commit()
        return changed

    async def acquire_service_lock(
        self,
        *,
        service_key: str,
        run_id: str,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO deployment_locks (
                service_key, run_id, target, acquired_at, metadata
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (service_key, run_id, target, _now_iso(), _json(metadata or {})),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def release_service_lock(self, *, service_key: str, run_id: str) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM deployment_locks WHERE service_key = ? AND run_id = ?",
            (service_key, run_id),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def get_service_lock(self, service_key: str) -> dict[str, Any] | None:
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            "SELECT * FROM deployment_locks WHERE service_key = ?", (service_key,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        value = dict(row)
        value["metadata"] = json.loads(value["metadata"] or "{}")
        return value

    async def reconcile_interrupted_runs(self) -> list[str]:
        """Mark possibly executing work UNKNOWN without releasing its lock."""
        statuses = sorted(state.value for state in INTERRUPTIBLE_RUN_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        self.db.row_factory = aiosqlite.Row
        cursor = await self.db.execute(
            f"SELECT id FROM deployment_runs WHERE status IN ({placeholders})",
            statuses,
        )
        run_ids = [str(row["id"]) for row in await cursor.fetchall()]
        if not run_ids:
            return []
        now = _now_iso()
        id_placeholders = ",".join("?" for _ in run_ids)
        await self.db.execute(
            f"""
            UPDATE deployment_runs
            SET status = ?, error = ?, updated_at = ?
            WHERE id IN ({id_placeholders}) AND status IN ({placeholders})
            """,
            (
                RunStatus.UNKNOWN.value,
                "Shell Agent 进程中断，远端执行结果未知，需人工核对",
                now,
                *run_ids,
                *statuses,
            ),
        )
        await self.db.execute(
            f"""
            UPDATE deployment_steps
            SET status = ?, error = ?, completed_at = ?
            WHERE run_id IN ({id_placeholders}) AND status = ?
            """,
            (
                StepStatus.UNKNOWN.value,
                "进程中断，步骤结果未知",
                now,
                *run_ids,
                StepStatus.RUNNING.value,
            ),
        )
        for run_id in run_ids:
            await self._append_event_no_commit(
                run_id,
                event_type="startup_reconciled",
                status=RunStatus.UNKNOWN,
                message="进程重启后将执行中任务收口为未知状态",
            )
        await self.db.commit()
        return run_ids

    async def _append_event_no_commit(
        self,
        run_id: str,
        *,
        event_type: str,
        status: RunStatus | None = None,
        step_id: str | None = None,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        cursor = await self.db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM deployment_events WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        sequence = int(row[0])
        await self.db.execute(
            """
            INSERT INTO deployment_events (
                id, run_id, sequence, type, status, step_id, message, payload, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"depev_{uuid4().hex[:20]}",
                run_id,
                sequence,
                event_type,
                status.value if status else None,
                step_id,
                message,
                _json(payload or {}),
                _now_iso(),
            ),
        )
