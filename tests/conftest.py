"""Make the zugamind package importable two ways for the test suite:

  - package-form:  `from zugamind.scanners.world import hackernews`
  - bare-form:     `from cognition...`, `from gates...`, `from foundation...`

Bare-form imports mirror the internal convention used throughout the
zugamind package itself (every module does `from foundation.config import
X`, not `from zugamind.foundation.config import X`), so both the repo root
and the zugamind/ package dir go on sys.path.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ZUGAMIND_PKG_DIR = os.path.join(_REPO_ROOT, "zugamind")

for _p in (_REPO_ROOT, _ZUGAMIND_PKG_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
