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

import os
from pathlib import Path

# --- Package root ------------------------------------------------------------

# zugamind/ is the parent of foundation/, which is the parent of this file.
ZUGAMIND_DIR = Path(__file__).resolve().parent.parent

# --- Data directory (gitignored runtime artifacts) ---------------------------

DATA_DIR = Path(os.environ.get("ZUGAMIND_DATA_DIR", str(ZUGAMIND_DIR / "data")))

ENGINE_DIR = DATA_DIR / "engine"
EVENT_LOG = ENGINE_DIR / "events.jsonl"
STATE_FILE = ENGINE_DIR / "state.json"
BUDGET_FILE = ENGINE_DIR / "budget.json"
TRIGGERS_FILE = ENGINE_DIR / "triggers.json"

# Kill-switch: presence of this file halts the cognitive cycle. Lives at the
# package root (not under ENGINE_DIR) so it's easy to find and touch/remove
# by hand — `touch PAUSE` / `rm PAUSE`.
PAUSE_FILE = ZUGAMIND_DIR / "PAUSE"

# --- Local model endpoint (Ollama) -------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Generic, reasonable default — swap for whatever instruction-tuned model you
# have loaded locally. Used for the fast/free Sentinel tier.
LOCAL_MODEL = os.environ.get("ZUGAMIND_LOCAL_MODEL", "qwen2.5:14b-instruct")

# --- Timeouts (seconds) -------------------------------------------------------

SENTINEL_TIMEOUT = int(os.environ.get("ZUGAMIND_SENTINEL_TIMEOUT", "90"))
REASONING_TIMEOUT = int(os.environ.get("ZUGAMIND_REASONING_TIMEOUT", "180"))

# --- Budget envelope ----------------------------------------------------------

# Approximate per-call costs (heuristics for the local budget ledger, not
# billing-grade figures — actual provider invoices are the source of truth).
HAIKU_COST = float(os.environ.get("ZUGAMIND_HAIKU_COST", "0.005"))
SONNET_COST = float(os.environ.get("ZUGAMIND_SONNET_COST", "0.05"))
OPUS_COST = float(os.environ.get("ZUGAMIND_OPUS_COST", "0.50"))

# Simple, self-contained monthly cap. In the private origin repo this value
# was read LIVE from a shared fleet-wide budget manager (a cross-repo
# dependency that does not exist in this repo). The OSS version intentionally
# drops that dependency: `monthly_cap()` just returns this constant.
# Integrators with their own accounting/budget system should replace
# `monthly_cap()` with a call into it.
ZUGAMIND_MONTHLY_BUDGET_USD = float(os.environ.get("ZUGAMIND_MONTHLY_BUDGET_USD", "10.00"))


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
