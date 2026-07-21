"""Redact secrets before operational context is sent to an LLM or persisted."""
from __future__ import annotations

import re


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\n]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|authorization|cookie)"
    r"(\s*(?:=|:)\s*)([^\s,;]+)",
)
_FLAG_SECRET_RE = re.compile(
    r"(?i)(--(?:password|passwd|token|api-key|api_key|secret)\s+)([^\s]+)",
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_OPENAI_STYLE_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_URL_CREDENTIAL_RE = re.compile(r"(://[^\s:/@]+:)([^\s/@]+)(@)")


def redact_context_secrets(text: str) -> str:
    """Best-effort removal of common credentials from context text."""
    value = text or ""
    value = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
    value = _BEARER_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    value = _FLAG_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    value = _OPENAI_STYLE_KEY_RE.sub("[REDACTED API KEY]", value)
    value = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]\3", value)
    return value
