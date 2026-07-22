"""HTTP API request schemas.

Keeping request validation in one module makes route modules independent from
the WebSocket workflow that still lives in :mod:`shell_agent.web.api` during
the incremental migration.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class CommandRequest(BaseModel):
    command: str
    session_id: Optional[str] = None
    auto_confirm: bool = False


class ConfirmRequest(BaseModel):
    session_id: str
    confirmed: bool
    channel: str = "chat"
    task_id: str = ""
    operation_id: str = ""
    request_id: str = ""


class ServerCreate(BaseModel):
    alias: str
    host: str
    port: int = 22
    env: str = "dev"
    role: str = ""
    ssh_credential: str
    tags: list[str] = Field(default_factory=list)


class ServiceProfileUpsert(BaseModel):
    id: str = ""
    name: str
    env: str = "dev"
    owners: list[str] = Field(default_factory=list)
    servers: list[str] = Field(default_factory=list)
    deploy_dir: str = ""
    artifact_path: str = ""
    backup_dir: str = ""
    artifact_type: str = "jar"
    startup_timeout_seconds: int = Field(default=60, ge=1, le=900)
    log_dir: str = ""
    health_url: str = ""
    ports: list[int] = Field(default_factory=list)
    start_cmd: str = ""
    stop_cmd: str = ""
    restart_cmd: str = ""
    status_cmd: str = ""
    config_paths: list[str] = Field(default_factory=list)
    runtime: str = ""
    version: str = ""
    last_verified_at: str = ""
    verification_status: str = "unknown"
    source_task_id: str = ""
    revision: int = 1
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class CredentialUpsert(BaseModel):
    id: str
    type: str = "password"  # password | key
    username: str
    password: str = ""
    private_key: str = ""
    passphrase: str = ""


class ConfigUpdate(BaseModel):
    section: str  # llm | ssh | session
    data: dict


class SafetyConfigUpdate(BaseModel):
    environments: dict = Field(default_factory=dict)
    safe_patterns: list[str] = Field(default_factory=list)
    forbidden_patterns: list[dict] = Field(default_factory=list)


class SafetyClassifyRequest(BaseModel):
    command: str
    target: str = ""
    env: str = "dev"
    executor: str = "ssh"


class SkillYamlUpdate(BaseModel):
    yaml: str


class SkillPreviewRequest(SkillYamlUpdate):
    input: str
    target: str = ""


class SkillCandidateScanRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)
    min_occurrences: int = Field(default=3, ge=2, le=20)
    semantic: bool = True


class MemoryCreate(BaseModel):
    subject: str
    predicate: str = "note"
    value: str
    target: str = ""
    type: str = "fact"
    status: str = "confirmed"
    confidence: float = 1.0
    expires_at: str = ""
    evidence_summary: str = ""


class MemoryUpdate(BaseModel):
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    target: str | None = None
    type: str | None = None
    status: str | None = None
    confidence: float | None = None
    expires_at: str | None = None
    evidence_summary: str | None = None


class ProfileCandidateDecision(BaseModel):
    proposed_changes: dict | None = None
    expected_revision: int | None = None


class SessionCreate(BaseModel):
    type: str = "chat"  # chat | command
    title: str = ""


class SessionUpdate(BaseModel):
    title: str


class SessionPinUpdate(BaseModel):
    pinned: bool


class SessionFileTransferCreate(BaseModel):
    target: str = Field(min_length=1, max_length=100)
    remote_dir: str = Field(default="/tmp/shell-agent-uploads", max_length=1024)
    remote_name: str = Field(default="", max_length=240)
    overwrite: bool = False
    request_id: str = Field(default="", max_length=128)


class SessionFileTransferConfirm(BaseModel):
    confirmed: bool
    request_id: str = Field(default="", max_length=128)


class DeploymentRunCreate(BaseModel):
    """Create a frozen registered plan for one session-scoped deployment."""

    session_id: str = Field(min_length=1, max_length=128)
    service_id: str = Field(min_length=1, max_length=128)
    file_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(default="", max_length=128)
    # Kept in the API so clients can send their current composer mode.  The
    # server deliberately never uses it to skip the runbook plan confirmation.
    confirm_mode: str = Field(default="interactive", max_length=32)


class DeploymentPlanConfirm(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")


class DeploymentRollbackConfirm(BaseModel):
    confirmed: bool = True
