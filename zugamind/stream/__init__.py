"""ZugaMind stream package — the always-on cognition loop.

Ties scanners (perception) -> the GWT workspace (attention) -> the
fail-closed action gate -> act.command_actuator (harness wake) into a
single runnable loop. See runner.py for the CLI:

    python -m stream.runner --once

`StreamRunner` is exposed lazily: an eager `from .runner import ...` here
made `python -m stream.runner` import the module twice (runpy's
"found in sys.modules after import of package" warning).
"""


def __getattr__(name):  # PEP 562
    if name == "StreamRunner":
        from .runner import StreamRunner
        return StreamRunner
    raise AttributeError(name)


__all__ = ["StreamRunner"]
