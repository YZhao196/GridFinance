"""``data/secrets.json`` — credentials, encrypted at rest (§13).

Rules this module exists to enforce:

* Never logged, never included in an export or a bug report.
* Encrypted with Windows DPAPI, which ties the ciphertext to the current Windows
  account: copying the data folder elsewhere leaves the key undecryptable rather than
  readable, and the app reports it as unconfigured instead of failing.
* Revoking is a delete: a removed credential leaves no residue.
* The bank password is never stored here or anywhere — the CDR consent flow happens on
  the bank's own page (§23).

The non-Windows fallback is base64, which is obfuscation, not encryption. It exists so
the test suite and development runs work elsewhere; a built app is a Windows app.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from finance_tool import paths

SECRET_KEYS = (
    "simplefin_access_url",
    "llm_anthropic",
    "llm_openai",
    "llm_deepseek",
    "llm_openrouter",
    "feed_redbark",
    "feed_banksync",
    "app_pin_hash",
)

REDACTED = "••••••••"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """Encrypt or decrypt with the current Windows user's DPAPI key."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        raise OSError("DPAPI is only available on Windows")

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    blob_out = _DataBlob()
    call = windll.crypt32.CryptProtectData if protect else windll.crypt32.CryptUnprotectData
    ok = call(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        windll.kernel32.LocalFree(blob_out.pbData)


def _encode(value: str) -> str:
    raw = value.encode("utf-8")
    try:
        return "dpapi:" + base64.b64encode(_dpapi(raw, protect=True)).decode("ascii")
    except OSError:
        return "plain:" + base64.b64encode(raw).decode("ascii")


def _decode(stored: str) -> str | None:
    """The plaintext behind a stored value, or ``None`` if it cannot be recovered.

    A value with no prefix is a file written before encryption existed; it is read as it
    is. Anything prefixed that fails to decode is treated as absent — the ciphertext may
    belong to another Windows account, and guessing would be worse than asking again.
    """
    try:
        if stored.startswith("dpapi:"):
            return _dpapi(base64.b64decode(stored[6:]), protect=False).decode("utf-8")
        if stored.startswith("plain:"):
            return base64.b64decode(stored[6:]).decode("utf-8")
    except (OSError, ValueError):
        return None
    return stored


@dataclass
class Secrets:
    """The credential store. Values are plaintext in memory, encrypted in the file."""

    simplefin_access_url: str | None = None
    llm_anthropic: str | None = None
    llm_openai: str | None = None
    llm_deepseek: str | None = None
    llm_openrouter: str | None = None
    feed_redbark: str | None = None
    feed_banksync: str | None = None
    app_pin_hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The on-disk shape: known credentials encrypted, extras untouched."""
        payload: dict[str, Any] = {}
        for key in SECRET_KEYS:
            value = getattr(self, key, None)
            if value:
                payload[key] = _encode(value)
        payload.update({k: v for k, v in self.extra.items() if v})
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Secrets":
        known = {}
        for key in SECRET_KEYS:
            stored = data.get(key)
            known[key] = _decode(stored) if isinstance(stored, str) and stored else None
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


def decode_stored(stored: str | None) -> str | None:
    """The plaintext behind a stored value, for a migration from another app."""
    return _decode(stored) if stored else None


def encode_stored(value: str) -> str:
    """The encrypted on-disk form, for a migration into this one."""
    return _encode(value)
