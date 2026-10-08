"""The dependency rule (§6), enforced rather than hoped for.

"Enforced by a test that imports every engine module in a process without PyQt
available." The import runs in a subprocess so that a module already cached by another
test cannot make the check pass vacuously.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
QT_FREE_PACKAGES = ("engine", "store", "hooks")


def _modules_under(package: str) -> list[str]:
    directory = ROOT / "finance_tool" / package
    if not directory.exists():
        return []
    found = []
    for path in sorted(directory.rglob("*.py")):
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        found.append(".".join(parts))
    return [m for m in found if m]


BLOCK_QT_SCRIPT = r"""
import sys

class NoQtImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "PyQt6" or fullname.startswith("PyQt6."):
            raise ImportError("PyQt6 is deliberately unavailable in this process")
        return None

sys.meta_path.insert(0, NoQtImport())

import importlib

for name in sys.argv[1:]:
    importlib.import_module(name)

leaked = [m for m in sys.modules if m == "PyQt6" or m.startswith("PyQt6.")]
if leaked:
    sys.stderr.write("Qt leaked into: " + ", ".join(leaked) + "\n")
    raise SystemExit(3)
print("ok")
"""


def _qt_free_modules() -> list[str]:
    modules = ["finance_tool"]
    for package in QT_FREE_PACKAGES:
        modules.extend(_modules_under(package))
    return modules


def test_there_is_something_to_check():
    assert _modules_under("store"), "expected store modules to be discovered"


def test_engine_store_and_hooks_import_without_qt_available():
    modules = _qt_free_modules()
    result = subprocess.run(
        [sys.executable, "-c", BLOCK_QT_SCRIPT, *modules],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"importing {modules} without Qt failed:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("package", QT_FREE_PACKAGES)
def test_no_qt_import_appears_in_qt_free_packages(package):
    """A second, fast check: even an import guarded by try/except is a smell here."""
    directory = ROOT / "finance_tool" / package
    if not directory.exists():
        pytest.skip(f"{package} does not exist yet")
    offenders = []
    for path in directory.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "PyQt6" in stripped or "PySide" in stripped:
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {stripped}")
    assert not offenders, "Qt referenced in a Qt-free package:\n" + "\n".join(offenders)
