"""Pytest discovers the repo root from this file and puts it on ``sys.path``.

The frontend was removed on 2026-10-08 to be redesigned, so there is no Qt here any more and
no application object to build. What is left is the Qt-free half — ``store``, ``engine`` and
``hooks`` — and its tests, which is why the whole suite now runs in a few seconds with no
display and no window.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
