"""ZugaMind configuration — paths, model endpoints, budget envelope.

Pure constants and the budget-cap resolver. No business logic. Imported by
the cognitive workspace and any module that needs to know where runtime
state lives, what model to call, or what the monthly spend cap is.

Resolution rules:
  - All paths are derived from `ZUGAMIND_DIR` (the package root) and are
    overridable via `ZUGAMIND_DATA_DIR`. There is no OS-specific branching —
    this package targets any platform Python 3.10+ runs on.
  - The budget cap is a simple, self-contained monthly ceiling
    (`ZUGAMIND_MONTHLY_BUDGET_USD`, default $10.00/month). In the private
    origin repo this value was read LIVE from a shared, fleet-wide budget
    manager service that does not exist in this repo. This OSS version is a
    standalone cap by design — integrators who run a shared accounting
    system across multiple agents should replace `monthly_cap()` with their
    own resolver.
  - All other tunables (POLL_INTERVAL, timeouts, dedupe windows) are simple
    env-overridable constants — change them via env var, or edit the default
    here and restart.
"""

import logging
import math
import os
from pathlib import Path

logger = logging.getLogger("zugamind.config")

# --- Package root ------------------------------------------------------------

# zugamind/ is the parent of foundation/, which is the parent of this file.
ZUGAMIND_DIR = Path(__file__).resolve().parent.parent

# --- Data directory (gitignored runtime artifacts) ---------------------------

def _dir_from_env(name: str, default: Path) -> Path:
    """A path from the environment, or the default.

    An EMPTY value is treated as unset. `Path("")` is `Path(".")`, so
    `ZUGAMIND_DATA_DIR=` in an env file silently relocated the whole runtime
    -- including the budget ledger -- to a path relative to the current
    working directory, which makes the monthly cap per-CWD: two shells, two
    ledgers, twice the spend (audit 2026-08-29). expanduser so `~/zugadata`
    means what it looks like; resolve so the answer cannot change when
    something calls os.chdir.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default.resolve()
    return Path(raw).expanduser().resolve()


DATA_DIR = _dir_from_env("ZUGAMIND_DATA_DIR", ZUGAMIND_DIR / "data")

ENGINE_DIR = DATA_DIR / "engine"
EVENT_LOG = ENGINE_DIR / "events.jsonl"
STATE_FILE = ENGINE_DIR / "state.json"
BUDGET_FILE = ENGINE_DIR / "budget.json"
TRIGGERS_FILE = ENGINE_DIR / "triggers.json"

# Kill-switch: presence of this file halts the cognitive cycle. Lives at the
# package root (not under ENGINE_DIR) so it's easy to find and touch/remove
# by hand — `touch PAUSE` / `rm PAUSE`.
PAUSE_FILE = ZUGAMIND_DIR / "PAUSE"
# Cooperative stop request. On Windows every external stop is TerminateProcess
# (taskkill /F, os.kill): a Python signal handler never runs, so `zugamind stop`
# writes this file and the daemon loop polls it once a second (2026-08-29).
STOP_FILE = ENGINE_DIR / "stop.request"

# --- Local model endpoint (Ollama) -------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Generic, reasonable default — swap for whatever instruction-tuned model you
# have loaded locally. Used for the fast/free Sentinel tier.
LOCAL_MODEL = os.environ.get("ZUGAMIND_LOCAL_MODEL", "qwen2.5:14b-instruct")

# Largest context window `ollama_query` will ask for (tokens). The window is
# sized per call from the prompt; without one Ollama used the model default
# (2048-4096) and silently dropped the FRONT of longer prompts — the system
# message (measured 2026-08-28). Past this cap it still truncates, loudly.
# Bigger windows cost RAM and load time on the local box.
OLLAMA_MAX_CTX = int(os.environ.get("ZUGAMIND_OLLAMA_MAX_CTX", "16384"))

# --- Timeouts (seconds) -------------------------------------------------------

SENTINEL_TIMEOUT = int(os.environ.get("ZUGAMIND_SENTINEL_TIMEOUT", "90"))
REASONING_TIMEOUT = int(os.environ.get("ZUGAMIND_REASONING_TIMEOUT", "180"))

# --- Budget envelope ----------------------------------------------------------

# Approximate per-call costs (heuristics for the local budget ledger, not
# billing-grade figures — actual provider invoices are the source of truth).
def _money_from_env(name: str, default: float) -> float:
    """A dollar amount from the environment. Never negative, never infinite.

    These were bare float() calls. Three ways that went wrong (audit
    2026-08-29): a typo raised ValueError during import and took the whole
    package down with a traceback that never named the variable; "inf" or
    "1e999" parsed cleanly into an INFINITE cap; and a 0 for a paid tier made
    can_spend treat it as the free local tier and stop gating it entirely.
    A misconfigured dial degrades to the default -- it does not disable the
    only spending limit there is.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — using %s", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0:
        logger.warning("%s=%r is not a usable amount — using %s",
                       name, raw, default)
        return default
    return value


HAIKU_COST = _money_from_env("ZUGAMIND_HAIKU_COST", 0.005)
SONNET_COST = _money_from_env("ZUGAMIND_SONNET_COST", 0.05)
OPUS_COST = _money_from_env("ZUGAMIND_OPUS_COST", 0.50)

# Simple, self-contained monthly cap. In the private origin repo this value
# was read LIVE from a shared fleet-wide budget manager (a cross-repo
# dependency that does not exist in this repo). The OSS version intentionally
# drops that dependency: `monthly_cap()` just returns this constant.
# Integrators with their own accounting/budget system should replace
# `monthly_cap()` with a call into it.
ZUGAMIND_MONTHLY_BUDGET_USD = _money_from_env("ZUGAMIND_MONTHLY_BUDGET_USD", 10.00)


def monthly_cap() -> float:
    """Return the monthly ($/30d) spend cap for paid-tier model calls.

    Standalone by design (see module docstring). Replace this function if you
    have a shared budget/accounting system across multiple agents.
    """
    return ZUGAMIND_MONTHLY_BUDGET_USD


# --- Service maps (empty by design) ------------------------------------------

# A deployer fills these in with their own service map. In the private origin
# repo these held a real, private port map / health-check inventory that must
# not ship in an OSS release. Downstream code should treat empty dicts/lists
# here as "no services configured" and skip health-checking gracefully.
LOCAL_SERVICES: dict = {}
PRODUCTION_ENDPOINTS: list = []

# --- Timing / tunables --------------------------------------------------------

POLL_INTERVAL = int(os.environ.get("ZUGAMIND_POLL_INTERVAL", "180"))  # seconds between cycles

# --- Habituation tunables ------------------------------------------------------

SEEN_TRIGGERS_FILE = ENGINE_DIR / "seen_triggers.json"
HABITUATION_HOURS = int(os.environ.get("ZUGAMIND_HABITUATION_HOURS", "6"))  # ignore a repeat trigger for N hours
