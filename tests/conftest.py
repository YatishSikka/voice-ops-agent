"""Put the repo root on sys.path so tests import `agent` and `config` directly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
