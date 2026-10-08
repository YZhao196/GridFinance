"""``data/secrets.json`` — the sync credential and nothing else (§13).

Rules this module exists to enforce:

* Never logged, never included in an export or a bug report.
* Stored as entered, shown only behind a password-echo field.
* Revoking is a delete: a removed credential leaves no residue.
* The bank password is never stored here or anywhere — the CDR consent flow happens on
  the bank's own page (§23).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finance_tool import paths

SECRET_KEYS = ("simplefin_access_url",)

REDACTED = "••••••••"


@dataclass
class Secrets:
    """The credential store. ``simplefin_access_url`` embeds Basic Auth (§22)."""

    simplefin_access_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in SECRET_KEYS:
            value = getattr(self, key, None)
            if value:
                payload[key] = value
        payload.update({k: v for k, v in self.extra.items() if v})
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Secrets":
        known = {key: data.get(key) for key in SECRET_KEYS}
        extra = {k: v for k, v in data.items() if k not in SECRET_KEYS}
        return cls(extra=extra, **known)

    def has(self, key: str) -> bool:
        return bool(getattr(self, key, None))

    def set(self, key: str, value: str | None) -> None:
        if key not in SECRET_KEYS:
            raise KeyError(f"{key!r} is not a known secret")
        setattr(self, key, value or None)

    def revoke(self, key: str) -> None:
        """Revoking is a delete — no residue, no tombstone (§13)."""
        self.set(key, None)

    def __repr__(self) -> str:  # never let a credential reach a log
        present = [key for key in SECRET_KEYS if self.has(key)]
        return f"Secrets({', '.join(present) if present else 'empty'})"


def load_secrets(path: Path | None = None) -> Secrets:
    target = Path(path) if path is not None else paths.SECRETS_PATH
    if not target.exists():
        return Secrets()
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # A secrets file we cannot read is simply not a credential. Never log its body.
        return Secrets()
    return Secrets.from_dict(data if isinstance(data, dict) else {})


def save_secrets(secrets: Secrets, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else paths.SECRETS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = secrets.to_dict()
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())

    if payload:
        _restrict(tmp)
    elif target.exists():
        # Revoked everything: leave no file behind at all.
        os.replace(tmp, target)
        target.unlink(missing_ok=True)
        return target

    os.replace(tmp, target)
    if payload:
        _restrict(target)
    return target


def _restrict(path: Path) -> None:
    """Best-effort 0600. Windows ACLs make this approximate; POSIX gets it exactly."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def redact(value: str | None) -> str:
    """What a credential looks like when it has to appear somewhere."""
    return REDACTED if value else ""
