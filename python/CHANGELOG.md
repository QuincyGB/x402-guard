# Changelog

## 0.2.0 — 2026-10-27

- New opt-in pipeline stage 4, **Pre-Trade Guard screen**
  (`pre_trade_enabled`, default off): payments at or above
  `pre_trade_threshold_units` (default $0.10) are screened by
  StallRow's Pre-Trade Guard ($0.005 per fresh call) against the
  payTo itself — Verdict Index listing history and rotations, a
  wallet snapshot, and the bundled OFAC SDN snapshot. ABORT blocks;
  CAUTION warns-and-logs by default (`pre_trade_caution="warn"`) or
  blocks (`"block"`); PROCEED passes. Call failures follow
  `pre_trade_failure` ("warn" | "block"). The payTo comes from the
  payment challenge in `pay()`/`pay_on_behalf()`, or from the cached
  drift fingerprint in `evaluate()`; with neither known the stage
  skips. Dry-run never pays for the screen. Spend caps are now
  stage 5. Env: `X402_GUARD_PRE_TRADE_ENABLED`,
  `X402_GUARD_PRE_TRADE_URL`, `X402_GUARD_PRE_TRADE_THRESHOLD_USD`,
  `X402_GUARD_PRE_TRADE_CAUTION`, `X402_GUARD_PRE_TRADE_FAILURE`.
  14 new tests (42 total).

## 0.1.1 — 2026-10-26

- Recheck pack support for drift checks: `recheck_pack_id` spends
  pack credits, warns at 25/10 credits remaining, never falls back
  to per-call payment.
- Published via PyPI Trusted Publishing (OIDC), validated by a real
  publish.

## 0.1.0 — 2026-10-25

- First release: the payment-path seatbelt — Settled pre-flight,
  StallRow Recheck drift checks, RouteVerdict trust gate, and spend
  caps, all enforced before any payment is signed.
