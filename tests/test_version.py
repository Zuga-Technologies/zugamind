"""Guards against zugamind/__init__.py's __version__ drifting from
pyproject.toml -- it silently sat at 0.1.0 across 4 releases while
pyproject.toml advanced to 0.5.0 before this test existed."""
import pathlib
import re

import zugamind


def test_version_matches_pyproject():
    pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
    text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m is not None
    assert m.group(1) == zugamind.__version__
