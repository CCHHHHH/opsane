"""Secret redaction before knowledge reaches storage or the LLM."""
from __future__ import annotations

import re
from typing import Any, Iterable


REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", re.I),
    re.compile(r"(?i)\b(identified\s+by\s+)(['\"])(.*?)(\2)"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|secret|passphrase)\b(\s*[:=]\s*|\s+)(['\"]?)([^\s,;]+)"),
    re.compile(r"(?i)(mysqladmin\b[^\n]*?\s-p)(?:\s*|=)(['\"]?)([^\s'\"]+)(\2)"),
    re.compile(r"(?i)(https?://[^:/\s]+:)([^@/\s]+)(@)"),
)

_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "private_key",
    "passphrase",
    "credential",
}


class SecretRedactor:
    def __init__(self, secret_values: Iterable[str] | None = None) -> None:
        values = {str(value) for value in (secret_values or []) if len(str(value)) >= 4}
        self.secret_values = sorted(values, key=len, reverse=True)

    def redact_text(self, value: str) -> str:
        text = str(value or "")
        for secret in self.secret_values:
            text = text.replace(secret, REDACTED)
        text = _PATTERNS[0].sub(REDACTED, text)
        text = _PATTERNS[1].sub(lambda match: f"{match.group(1)}'{REDACTED}'", text)
        text = _PATTERNS[2].sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)
        text = _PATTERNS[3].sub(lambda match: f"{match.group(1)}{REDACTED}", text)
        text = _PATTERNS[4].sub(lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}", text)
        return text

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact(item) for item in value)
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                output[key] = REDACTED if normalized in _SENSITIVE_KEYS else self.redact(item)
            return output
        return value


def is_sensitive_predicate(predicate: str) -> bool:
    normalized = (predicate or "").strip().lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(key in normalized for key in _SENSITIVE_KEYS)
