"""Make the four src roots importable so `pytest` works without a preset PYTHONPATH."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_ROOTS = [
    "apps/api/src",
    "services/agent-worker/src",
    "services/learning-worker/src",
    "packages/core/src",
]
for rel in SRC_ROOTS:
    path = str(ROOT / rel)
    if path not in sys.path:
        sys.path.insert(0, path)
