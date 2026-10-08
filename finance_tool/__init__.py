"""The backend of a personal budgeting tool: a recurring plan and a reconciled ledger.

Layering (§6): ``store`` and ``engine`` import no Qt, ever. ``hooks`` sits between the
engine and the UI and is also Qt-free. ``tests/test_layering.py`` enforces this by importing
every module in a subprocess with PyQt6 blocked, and by grepping the sources for the string.

**There is no frontend here.** It was removed on 2026-10-08 to be redesigned, so nothing in
this package imports Qt any more and nothing can be run. What is left is the model — the
store, the engine that computes every figure, and the hooks a UI reads — plus its tests.
``BACKEND.md`` is the contract for whoever writes the next one.
"""

__version__ = "0.1.0"
