#!/usr/bin/env python3
"""Root-level launcher for the ZugaMind stream runner.

Thin shim so `python runner.py --once` works from a fresh clone without
installing the package or changing directories. The real implementation
lives in zugamind/stream/runner.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "zugamind"))

from stream.runner import main

if __name__ == "__main__":
    sys.exit(main())
