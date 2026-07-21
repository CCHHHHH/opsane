"""SQLite 数据库初始化与连接"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

# 建表 SQL
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    type TEXT DEFAULT 'chat',
    caller TEXT,
    source TEXT,
    status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    context_summary TEXT,
    context_summary_message_count INTEGER DEFAULT 0,
    context_summary_updated_at TEXT,
    pinned_at TEXT,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS session_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT,
    payload TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_files (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    media_type TEXT,
    extension TEXT,
    kind TEXT NOT NULL DEFAULT 'other',
    preview_type TEXT NOT NULL DEFAULT 'none',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT,
    extracted_text TEXT,
    metadata TEXT,
    layout_preview_status TEXT NOT NULL DEFAULT 'none',
    layout_preview_path TEXT,
    layout_preview_error TEXT,
    layout_preview_size INTEGER NOT NULL DEFAULT 0,
    layout_preview_source_sha256 TEXT,
    layout_preview_claim_id TEXT,
    layout_preview_updated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_file_transfers (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    target TEXT NOT NULL,
    target_env TEXT,
    target_fingerprint TEXT,
    remote_dir TEXT NOT NULL,
    remote_name TEXT NOT NULL,
    remote_path TEXT NOT NULL,
    overwrite INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL,
    remote_size INTEGER,
    remote_sha256 TEXT,
    error TEXT,
    source TEXT NOT NULL DEFAULT 'web',
    turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(session_id, request_id),
    FOREIGN KEY(session_id) REFERENCES sessions(id),
    FOREIGN KEY(file_id) REFERENCES session_files(id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    caller TEXT,
    source TEXT,
    target TEXT,
    target_env TEXT,
    executor TEXT,
    command TEXT NOT NULL,
    executed INTEGER NOT NULL,
    user_confirmed INTEGER,
    exit_code INTEGER,
    duration_ms INTEGER,
    stdout TEXT,
    stderr TEXT,
    truncated INTEGER,
    timed_out INTEGER,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    current_step INTEGER DEFAULT 0,
    total_steps INTEGER DEFAULT 1,
    pending_command TEXT,
    pending_target TEXT,
    confirm_mode TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS agent_task_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT,
    step_index INTEGER,
    content TEXT,
    payload TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES agent_tasks(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS global_memories (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    target TEXT,
    type TEXT DEFAULT 'fact',
    status TEXT DEFAULT 'confirmed',
    confidence REAL DEFAULT 1.0,
    source_session_id TEXT,
    source_task_id TEXT,
    source_event_id TEXT,
    source TEXT,
    observed_at TEXT,
    expires_at TEXT,
    evidence_summary TEXT,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS service_profile_candidates (
    id TEXT PRIMARY KEY,
    service_id TEXT,
    service_name TEXT NOT NULL,
    proposed_changes TEXT NOT NULL,
    before_snapshot TEXT,
    evidence TEXT,
    confidence REAL DEFAULT 1.0,
    fingerprint TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    source_memory_ids TEXT,
    source_task_id TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_target_time ON audit_logs(target, timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_logs(session_id);
CREATE INDEX IF NOT EXISTS idx_session_messages_session_time ON session_messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_session_files_session_time ON session_files(session_id, deleted_at, created_at);
CREATE INDEX IF NOT EXISTS idx_session_file_transfers_session_time ON session_file_transfers(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_session_file_transfers_file_status ON session_file_transfers(file_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_status ON agent_tasks(session_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_task_events_task_time ON agent_task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_global_memories_subject ON global_memories(subject, deleted_at);
CREATE INDEX IF NOT EXISTS idx_global_memories_target ON global_memories(target, deleted_at);
CREATE INDEX IF NOT EXISTS idx_profile_candidates_status ON service_profile_candidates(status, created_at);
CREATE INDEX IF NOT EXISTS idx_profile_candidates_fingerprint ON service_profile_candidates(fingerprint, status);
"""

_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN title TEXT",
    "ALTER TABLE sessions ADD COLUMN type TEXT DEFAULT 'chat'",
    "ALTER TABLE sessions ADD COLUMN deleted_at TEXT",
    "ALTER TABLE sessions ADD COLUMN context_summary TEXT",
    "ALTER TABLE sessions ADD COLUMN context_summary_message_count INTEGER DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN context_summary_updated_at TEXT",
    "ALTER TABLE sessions ADD COLUMN pinned_at TEXT",
    "ALTER TABLE global_memories ADD COLUMN type TEXT DEFAULT 'fact'",
    "ALTER TABLE global_memories ADD COLUMN status TEXT DEFAULT 'confirmed'",
    "ALTER TABLE global_memories ADD COLUMN source_task_id TEXT",
    "ALTER TABLE global_memories ADD COLUMN source_event_id TEXT",
    "ALTER TABLE global_memories ADD COLUMN observed_at TEXT",
    "ALTER TABLE global_memories ADD COLUMN expires_at TEXT",
    "ALTER TABLE global_memories ADD COLUMN evidence_summary TEXT",
    "ALTER TABLE global_memories ADD COLUMN fingerprint TEXT",
    "ALTER TABLE session_file_transfers ADD COLUMN source TEXT NOT NULL DEFAULT 'web'",
    "ALTER TABLE session_file_transfers ADD COLUMN turn_id TEXT",
    "ALTER TABLE session_file_transfers ADD COLUMN target_env TEXT",
    "ALTER TABLE session_file_transfers ADD COLUMN target_fingerprint TEXT",
    "ALTER TABLE session_files ADD COLUMN layout_preview_status TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE session_files ADD COLUMN layout_preview_path TEXT",
    "ALTER TABLE session_files ADD COLUMN layout_preview_error TEXT",
    "ALTER TABLE session_files ADD COLUMN layout_preview_size INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE session_files ADD COLUMN layout_preview_source_sha256 TEXT",
    "ALTER TABLE session_files ADD COLUMN layout_preview_claim_id TEXT",
    "ALTER TABLE session_files ADD COLUMN layout_preview_updated_at TEXT",
]

_POST_MIGRATION_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_sessions_type_updated ON sessions(type, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_type_pinned_updated ON sessions(type, pinned_at, updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_global_memories_status ON global_memories(status, deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_global_memories_fingerprint ON global_memories(fingerprint, deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_profile_candidates_status ON service_profile_candidates(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_profile_candidates_fingerprint ON service_profile_candidates(fingerprint, status)",
    "UPDATE global_memories SET type = COALESCE(NULLIF(type, ''), 'fact'), status = COALESCE(NULLIF(status, ''), 'confirmed'), observed_at = COALESCE(observed_at, created_at)",
]


async def init_db(sqlite_path: str) -> None:
    """初始化数据库（建表）"""
    path = Path(sqlite_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(sqlite_path) as db:
        await db.executescript(_SCHEMA)
        for sql in _MIGRATIONS:
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        for sql in _POST_MIGRATION_SQL:
            await db.execute(sql)
        await db.commit()


async def connect(sqlite_path: str) -> aiosqlite.Connection:
    """获取数据库连接（WAL 模式）"""
    db = await aiosqlite.connect(sqlite_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db
