"""Run the AI Finance Manager migration from a checkout.

    python scripts/migrate_afm.py --afm-db "../AI Finance Manager/data/finance.db" --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finance_tool.migrate_afm import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
