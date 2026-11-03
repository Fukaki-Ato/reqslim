"""Shared pytest setup.

The suite stays runnable as plain scripts (``python tests/test_zcode.py``),
which is why each file also bootstraps ``sys.path`` on its own. This exists so
``pytest`` works regardless of the directory it is invoked from, and so the
scripts could drop their bootstrap later without breaking collection.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
