"""The engine: every number the app shows, produced by plain-Python functions.

Four contracts hold everywhere in this package (§15):

1. **Pure.** A function of its arguments and the store snapshot. No clocks, no globals,
   no I/O outside ``importers`` and ``sync``. "Today" is always a parameter.
2. **Read-only over the store.** A query never mutates.
3. **No Qt.** Enforced by ``tests/test_layering.py``.
4. **Total.** Degenerate input returns an empty or zero result, never raises.
"""
