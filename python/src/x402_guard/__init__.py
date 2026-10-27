"""x402-guard — the payment-path seatbelt for x402 buyer agents.

Install GuardedClient once inside your paying code; from then on
every x402 payment is pre-flighted before anything is signed:

  1. Settled pre-flight (free)   — the inspector's delivery record
  2. Drift check (Recheck $0.005, cached daily)
                                 — the endpoint's terms haven't moved
  3. RouteVerdict gate ($0.05)   — big first payments need TRUSTED
  4. Pre-Trade Guard ($0.005)    — opt-in payTo screen (ABORT blocks)
  5. Your own spend caps         — enforced before signing, never after

Blocks raise GuardBlocked with the full reason chain; every decision
lands in a local audit log you can read.
"""

from .client import GuardDecision, GuardResult, GuardedClient, StageResult
from .config import GuardConfig
from .errors import GuardBlocked
from .settled import SettledReport, fetch_settled_report
from .store import GuardStore
from .x402client import (
    CallOutcome,
    ServiceError,
    SpendCapExceeded,
    call_paid_endpoint,
    format_usd,
    load_account_from_env,
)

__version__ = "0.1.1"

__all__ = [
    "GuardedClient",
    "GuardConfig",
    "GuardDecision",
    "GuardResult",
    "StageResult",
    "GuardBlocked",
    "GuardStore",
    "SettledReport",
    "fetch_settled_report",
    "CallOutcome",
    "ServiceError",
    "SpendCapExceeded",
    "call_paid_endpoint",
    "format_usd",
    "load_account_from_env",
    "__version__",
]
