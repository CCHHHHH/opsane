"""配置加载：YAML + 环境变量"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    summary_model: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout: int = 60
    api_key: str = ""
    base_url: str = ""
    image_analysis_enabled: bool = False
    vision_model: str = ""
    vision_max_bytes: int = 12 * 1024 * 1024


class SSHConfig(BaseModel):
    max_per_host: int = 3
    idle_timeout: int = 300
    total_max: int = 50
    default_timeout: int = 60
    trust_unknown_hosts: bool = True


class StorageConfig(BaseModel):
    sqlite_path: str = "data/shell_agent.db"


class SessionConfig(BaseModel):
    idle_timeout_minutes: int = 30
    pending_confirm_timeout_seconds: int = 300


class ContextConfig(BaseModel):
    semantic_summary_enabled: bool = True
    summary_timeout_seconds: float = 15.0
    summary_trigger_events: int = 16
    summary_trigger_chars: int = 12000
    recent_events: int = 8
    summary_max_chars: int = 3200
    summary_max_tokens: int = 1200


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str = "data/logs"


class AgentConfig(BaseModel):
    name: str = "shell-agent"
    version: str = "0.1.0"


class AppConfig(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ssh: SSHConfig = Field(default_factory=SSHConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(config_path: str = "config/agent.yaml") -> AppConfig:
    """加载 agent.yaml 配置，环境变量优先"""
    data: dict[str, Any] = {}
    path = Path(config_path)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig(**data)

    # 环境变量覆盖
    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        config.llm.api_key = api_key
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        config.llm.base_url = base_url
    vision_model = os.getenv("OPENAI_VISION_MODEL")
    if vision_model:
        config.llm.vision_model = vision_model
    image_analysis_enabled = os.getenv("OPSANE_IMAGE_ANALYSIS_ENABLED")
    if image_analysis_enabled:
        config.llm.image_analysis_enabled = image_analysis_enabled.strip().lower() not in {
            "0", "false", "no", "off",
        }

    return config


class Credential(BaseModel):
    id: str
    type: str  # password | key
    username: str
    password: str | None = None
    private_key: str | None = None
    passphrase: str | None = None


class ServerEntry(BaseModel):
    alias: str
    host: str
    port: int = 22
    env: str = "dev"
    role: str = ""
    ssh_credential: str
    tags: list[str] = Field(default_factory=list)


class ServiceProfile(BaseModel):
    id: str
    name: str
    env: str = "dev"
    owners: list[str] = Field(default_factory=list)
    servers: list[str] = Field(default_factory=list)
    deploy_dir: str = ""
    artifact_path: str = ""
    backup_dir: str = ""
    artifact_type: str = "jar"
    startup_timeout_seconds: int = 60
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


def load_credentials(path: str = "config/credentials.yaml") -> dict[str, Credential]:
    """加载 SSH 凭证"""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    creds = {}
    for item in data.get("credentials", []):
        c = Credential(**item)
        creds[c.id] = c
    return creds


def load_inventory(path: str = "config/inventory.yaml") -> dict[str, ServerEntry]:
    """加载服务器清单"""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = {}
    for item in data.get("servers", []):
        s = ServerEntry(**item)
        servers[s.alias] = s
    return servers


def load_services(path: str = "config/inventory.yaml") -> dict[str, ServiceProfile]:
    """加载服务画像。"""
    if not Path(path).exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    services = {}
    for item in data.get("services", []):
        service = ServiceProfile(**item)
        services[service.id] = service
    return services
