"""Guard configuration: one small explicit object, no magic.

Every knob the policy pipeline consults lives here. Amounts are USDC
base units (6 decimals) unless the name says otherwise. Build one
directly, or use `GuardConfig.from_env()` to read `X402_GUARD_*`
environment variables (USD-denominated where a human would type them).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .settled import SETTLED_MCP_URL

DEFAULT_RECHECK_URL = "https://stall-row-production.up.railway.app/v1/recheck"
DEFAULT_ROUTE_VERDICT_URL = "https://route-verdict-production.up.railway.app/v1/check"

USDC_UNITS_PER_DOLLAR = 1_000_000

_TRUE_WORDS = {"1", "true", "yes", "on"}
_FALSE_WORDS = {"0", "false", "no", "off"}


@dataclass
class GuardConfig:
    """Policy + wiring for GuardedClient.

    Stage toggles and thresholds:
      settled_enabled / settled_mcp_url / settled_timeout
          The free Settled pre-flight (stage 1).
      drift_enabled / recheck_url / recheck_pack_id /
      drift_ttl_seconds / price_change_block_pct / drift_failure
          The StallRow Recheck drift check (stage 2). Results are
          cached for drift_ttl_seconds; a RouteVerdict gate verdict is
          cached for the same TTL. When recheck_pack_id is set, drift
          checks spend credits from that StallRow recheck pack
          (POST /v1/recheck/pack: $1.00 = 250 checks on one endpoint)
          instead of paying $0.005 per uncached check — the pack id is
          the credential, no per-check settlement occurs, and an
          exhausted (402) or expired (410) pack fails the stage under
          drift_failure WITHOUT falling back to per-call payment.
          drift_failure is what happens when
          the Recheck call itself fails: "warn" (pass through with a
          warning) or "block".
      route_verdict_enabled / route_verdict_url /
      route_verdict_threshold_units / route_verdict_min_trust /
      route_verdict_failure
          The RouteVerdict gate (stage 3): first-ever payments at or
          above the threshold require TRUSTED >= min trust.
          route_verdict_failure is the on-error policy, same words as
          drift_failure.

    Spend caps (stage 4), all optional (None = no cap):
      max_payment_units            one payment may never exceed this
      per_endpoint_daily_cap_units per paid-URL per UTC day
      global_daily_cap_units       everything per UTC day (payments
                                   AND the guard's own check calls)

    Local state:
      store_path      sqlite file for the check cache + spend ledger
                      (default: ~/.x402-guard/guard.db)
      audit_log_path  JSONL audit log (default: audit.jsonl beside
                      the store file)
    """

    settled_enabled: bool = True
    settled_mcp_url: str = SETTLED_MCP_URL
    settled_timeout: float = 10.0

    drift_enabled: bool = True
    recheck_url: str = DEFAULT_RECHECK_URL
    recheck_pack_id: str | None = None
    drift_ttl_seconds: float = 86_400.0
    price_change_block_pct: float = 20.0
    drift_failure: str = "warn"

    route_verdict_enabled: bool = True
    route_verdict_url: str = DEFAULT_ROUTE_VERDICT_URL
    route_verdict_threshold_units: int = 100_000  # $0.10
    route_verdict_min_trust: float = 80.0
    route_verdict_failure: str = "warn"

    max_payment_units: int | None = None
    per_endpoint_daily_cap_units: int | None = None
    global_daily_cap_units: int | None = None

    store_path: str | None = None
    audit_log_path: str | None = None

    def __post_init__(self) -> None:
        if self.drift_failure not in ("warn", "block"):
            raise ValueError('drift_failure must be "warn" or "block"')
        if self.route_verdict_failure not in ("warn", "block"):
            raise ValueError('route_verdict_failure must be "warn" or "block"')
        if self.drift_ttl_seconds < 0:
            raise ValueError("drift_ttl_seconds cannot be negative")
        if self.price_change_block_pct < 0:
            raise ValueError("price_change_block_pct cannot be negative")

    @classmethod
    def from_env(cls, env: dict | None = None, prefix: str = "X402_GUARD_") -> "GuardConfig":
        """Read configuration from environment variables.

        Recognized (all optional):
          X402_GUARD_SETTLED_ENABLED, X402_GUARD_DRIFT_ENABLED,
          X402_GUARD_ROUTE_VERDICT_ENABLED   booleans (1/0, true/false…)
          X402_GUARD_RECHECK_URL, X402_GUARD_ROUTE_VERDICT_URL,
          X402_GUARD_SETTLED_MCP_URL         URLs
          X402_GUARD_RECHECK_PACK_ID         StallRow recheck pack id
                                             (spend pack credits for
                                             drift checks, no per-call
                                             payment, no fallback)
          X402_GUARD_DRIFT_TTL_SECONDS       seconds (float)
          X402_GUARD_PRICE_CHANGE_BLOCK_PCT  percent (float)
          X402_GUARD_DRIFT_FAILURE, X402_GUARD_ROUTE_VERDICT_FAILURE
                                             "warn" | "block"
          X402_GUARD_ROUTE_VERDICT_THRESHOLD_USD,
          X402_GUARD_ROUTE_VERDICT_MIN_TRUST,
          X402_GUARD_MAX_PAYMENT_USD,
          X402_GUARD_PER_ENDPOINT_DAILY_CAP_USD,
          X402_GUARD_GLOBAL_DAILY_CAP_USD    USD amounts -> units
          X402_GUARD_STORE_PATH, X402_GUARD_AUDIT_LOG_PATH
        """
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = env.get(prefix + name)
            return value.strip() if isinstance(value, str) and value.strip() else None

        def get_bool(name: str, default: bool) -> bool:
            raw = get(name)
            if raw is None:
                return default
            low = raw.lower()
            if low in _TRUE_WORDS:
                return True
            if low in _FALSE_WORDS:
                return False
            raise ValueError(f"{prefix}{name}: not a boolean: {raw!r}")

        def get_float(name: str, default: float) -> float:
            raw = get(name)
            return default if raw is None else float(raw)

        def get_usd_units(name: str) -> int | None:
            raw = get(name)
            if raw is None:
                return None
            return int(round(float(raw) * USDC_UNITS_PER_DOLLAR))

        defaults = cls()
        return cls(
            settled_enabled=get_bool("SETTLED_ENABLED", defaults.settled_enabled),
            settled_mcp_url=get("SETTLED_MCP_URL") or defaults.settled_mcp_url,
            drift_enabled=get_bool("DRIFT_ENABLED", defaults.drift_enabled),
            recheck_url=get("RECHECK_URL") or defaults.recheck_url,
            recheck_pack_id=get("RECHECK_PACK_ID") or defaults.recheck_pack_id,
            drift_ttl_seconds=get_float("DRIFT_TTL_SECONDS", defaults.drift_ttl_seconds),
            price_change_block_pct=get_float(
                "PRICE_CHANGE_BLOCK_PCT", defaults.price_change_block_pct
            ),
            drift_failure=(get("DRIFT_FAILURE") or defaults.drift_failure).lower(),
            route_verdict_enabled=get_bool(
                "ROUTE_VERDICT_ENABLED", defaults.route_verdict_enabled
            ),
            route_verdict_url=get("ROUTE_VERDICT_URL") or defaults.route_verdict_url,
            route_verdict_threshold_units=(
                get_usd_units("ROUTE_VERDICT_THRESHOLD_USD")
                if get("ROUTE_VERDICT_THRESHOLD_USD") is not None
                else defaults.route_verdict_threshold_units
            ),
            route_verdict_min_trust=get_float(
                "ROUTE_VERDICT_MIN_TRUST", defaults.route_verdict_min_trust
            ),
            route_verdict_failure=(
                get("ROUTE_VERDICT_FAILURE") or defaults.route_verdict_failure
            ).lower(),
            max_payment_units=get_usd_units("MAX_PAYMENT_USD"),
            per_endpoint_daily_cap_units=get_usd_units("PER_ENDPOINT_DAILY_CAP_USD"),
            global_daily_cap_units=get_usd_units("GLOBAL_DAILY_CAP_USD"),
            store_path=get("STORE_PATH"),
            audit_log_path=get("AUDIT_LOG_PATH"),
        )
