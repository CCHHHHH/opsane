"""Application configuration REST endpoints."""
from __future__ import annotations

import yaml
from fastapi import APIRouter

from shell_agent.web.runtime import get_runtime
from shell_agent.web.schemas import ConfigUpdate


router = APIRouter()


@router.get("/api/config")
async def get_config() -> dict:
    rt = get_runtime()
    llm_config = rt.config.llm.model_dump()
    llm_config["api_key_set"] = bool(llm_config.get("api_key"))
    llm_config["api_key"] = ""
    return {
        "llm": llm_config,
        "ssh": rt.config.ssh.model_dump(),
        "session": rt.config.session.model_dump(),
        "context": rt.config.context.model_dump(),
    }


@router.put("/api/config")
async def update_config(update: ConfigUpdate) -> dict:
    rt = get_runtime()
    try:
        if update.section == "llm":
            for key, value in update.data.items():
                if key == "api_key" and not value:
                    continue
                if key == "api_key" and value == "********":
                    continue
                if hasattr(rt.config.llm, key):
                    setattr(rt.config.llm, key, value)
        elif update.section == "ssh":
            for key, value in update.data.items():
                if hasattr(rt.config.ssh, key):
                    setattr(rt.config.ssh, key, value)
        elif update.section == "session":
            for key, value in update.data.items():
                if hasattr(rt.config.session, key):
                    setattr(rt.config.session, key, value)
        elif update.section == "context":
            for key, value in update.data.items():
                if hasattr(rt.config.context, key):
                    setattr(rt.config.context, key, value)
        data = rt.config.model_dump()
        with open(rt.config_path, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
        await rt.reload()
        return {"ok": True}
    except Exception as exc:
        return {"error": str(exc)}


@router.post("/api/config/test-llm")
async def test_llm(data: dict) -> dict:
    from openai import AsyncOpenAI

    try:
        api_key = data.get("api_key", "")
        if not api_key:
            rt = get_runtime()
            api_key = rt.config.llm.api_key
        base_url = data.get("base_url", "")
        if not base_url:
            rt = get_runtime()
            base_url = rt.config.llm.base_url
        model = data.get("model", "gpt-4o-mini")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncOpenAI(**kwargs)
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=10,
            timeout=30,
        )
        return {"ok": True, "response": response.choices[0].message.content}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
