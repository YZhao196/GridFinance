"""Where things live on disk (§8).

One ``data/`` directory beside the app, created on first run.

**Frozen, "beside the app" means beside the executable.** A one-file build unpacks the
program into a temporary directory and deletes it when the process exits, so an app that
worked out its own location from `__file__` would put the store in there and throw the
user's data away every time they closed the window. That is the single worst thing this
program could do, so the writable location and the read-only one are separate questions
with separate answers.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """Whether this is a built executable rather than a checkout run from source."""
    return bool(getattr(sys, "frozen", False))


def _writable_root() -> Path:
    """Where the store lives. The executable's own folder when built."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _read_only_root() -> Path:
    """Where files that ship *with* the program live — the bundled fonts.

    A one-file build unpacks these somewhere temporary, which is fine: they are read, never
    written. From source they sit beside the package.
    """
    unpacked = getattr(sys, "_MEIPASS", None)
    return Path(unpacked) if unpacked else Path(__file__).resolve().parent.parent


APP_ROOT = _writable_root()
RESOURCE_ROOT = _read_only_root()

DATA_DIR = APP_ROOT / "data"
SECRETS_PATH = DATA_DIR / "secrets.json"
SHOTS_DIR = APP_ROOT / "shots"

# Where to look for a bundled face, in order. Absolute, because a built app's working
# directory is wherever the user happened to launch it from.
FONT_DIRS = tuple(RESOURCE_ROOT / part for part in ("assets/fonts", "assets", "fonts"))
