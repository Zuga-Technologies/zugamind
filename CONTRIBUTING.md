# Contributing to ZugaMind

Thanks for your interest in contributing. A few ground rules:

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

`pytest` is the only development dependency the project requires — there is
no other test framework or fixture library to install.

## The stdlib-only constraint

The core `zugamind` package is stdlib-only by design (see `README.md` for
why). **PRs must not introduce pip runtime dependencies to
the core package.** `pytest` (and only `pytest`) is allowed as a *dev*
dependency for running the test suite — it is never imported by the core
package itself. If your change needs a third-party library, discuss it in an
issue first; the answer is very likely "use `urllib`/`json`/`dataclasses`
instead."

## Code style

- Type hints on function signatures (parameters and return types).
- A docstring on every public module, class, and function explaining what it
  does and why, not just restating the signature.
- Prefer small, focused modules over large ones — see how `foundation/` and
  `cognition/models/` are organized for the expected grain size.
- Keep fail-closed behavior fail-closed: gates, budget checks, and validation
  should default to "don't act" on error, never "proceed."

## Pull requests

- Keep PRs scoped to one change. Explain the "why," not just the "what."
- Add or update tests for any behavior change.
- Do not add pip runtime dependencies to the core package (see above).
