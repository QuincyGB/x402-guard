# x402-guard (Python)

The payment-path seatbelt for x402 buyer agents. Install
`GuardedClient` once inside your paying code; from then on, every
x402 payment you make is pre-flighted through Verdict Row **before
anything is signed**:

1. **Settled pre-flight (free)** — settled.tools' scout has actually
   bought from most endpoints worth paying. The guard asks what its
   test purchases found. Hard veto *only* on a failed delivery or a
   not-payable verdict; a verified delivery corroborates;
   `nothing_to_compare` is noted, never a veto; Settled being down or
   silent is a pass-through, never a block.
2. **Drift check (StallRow Recheck, $0.005, cached)** — if the
   endpoint is new to you, or your last look is older than the TTL
   (default 24h), the guard pays Recheck to fingerprint the
   endpoint's live payment terms and diffs them against its local
   store. `payTo` or asset changed → **hard block**. Price moved
   more than the configured % (default 20%) → block. Schema /
   description / other changes → warn. Inside the TTL the cached
   result is used and this stage costs **zero**.
3. **RouteVerdict gate (optional, $0.05, cached)** — a first-ever
   payment at or above a threshold (default $0.10) requires a
   TRUSTED verdict ≥ your trust floor (default 80).
4. **Pre-Trade Guard screen (opt-in, $0.005)** — off by default
   (`pre_trade_enabled=True` turns it on). Payments at or above a
   threshold (default $0.10) are screened against the **payTo
   itself**: StallRow's Pre-Trade Guard reads the payTo's listing
   history and rotations from the Verdict Index, a wallet snapshot,
   and the OFAC SDN snapshot. `ABORT` → block. `CAUTION` →
   warn-and-log (or block with `pre_trade_caution="block"`).
   `PROCEED` → pass.
5. **Your spend caps** — per-payment, per-endpoint-daily, and
   global-daily caps from your own config, checked against the
   guard's local ledger *before* signing. The guard refuses first;
   it never signs and discovers later.

Stage 2 drift checks can spend from a StallRow recheck pack
(config `recheck_pack_id`): $0.004 per uncached check, no per-check
settlement, and the guard warns at 25 and 10 credits remaining
(balance reads are free). Without a pack, drift checks pay $0.005
per uncached check as before.

Every decision — allow, warn, block, with the reason chain and what
the checks cost — is appended to a local audit log (JSONL) you can
read. Blocks raise `GuardBlocked` carrying the full decision.

```python
from eth_account import Account
from x402_guard import GuardConfig, GuardedClient

account = Account.from_key("0x...")  # your key, your env — never logged
guard = GuardedClient(account=account, config=GuardConfig(
    global_daily_cap_units=1_000_000,  # $1.00/day across everything
))

result = guard.pay("https://some-service.example/v1/thing", {"input": 1})
print(result.outcome.response)          # the service's real answer
print(result.decision.summary())        # what was consulted, what it cost
```

No signer (`account=None`) = dry-run: the pipeline runs what it can
for free, paid stages are noted as skipped, would-be blocks land on
`result.decision.blocked`, and nothing is ever paid.

## What it costs you

| Stage | Cost | When |
|---|---|---|
| Settled pre-flight | free | every payment |
| Recheck drift check | $0.005 | first look per endpoint per TTL (24h default) |
| RouteVerdict gate | $0.05 | first big payment (≥ $0.10 default) per TTL |
| Pre-Trade Guard screen | $0.005 | opt-in; payments ≥ $0.10 default |
| The guard itself | $0 | it's a library |

## What it cannot do

It judges **pre-payment signals only**: the inspector's delivery
record, the endpoint's current terms vs. its own history, a trust
verdict, and your caps. It cannot judge the paid payload — whether
the answer you bought was *good* is your application's problem. It
also cannot help if an endpoint's terms change between the drift
check and your payment inside the same TTL window; shorten the TTL
if that window matters to you.

## Configuration

`GuardConfig` in code, or environment variables via
`GuardConfig.from_env()` (`X402_GUARD_*`: stage toggles, URLs, TTL,
price-change %, failure policies — `"warn"` or `"block"` when a check
service itself errors — USD caps, store/audit paths). Local state
lives in `~/.x402-guard/` (sqlite cache + spend ledger, JSONL audit
log) — your machine, your data, never transmitted.

An external budget object (anything with `authorize(units)` /
`record(units, label=)` — e.g. verdict-loop's `DailySpendCap`) can be
passed as `budget=`; every settled payment the guard makes, checks
included, is authorized and recorded against it, so an embedding app
keeps exactly one cap.

Part of Verdict Row: [Token Verdict](https://github.com/QuincyGB/token-verdict) ·
[RouteVerdict](https://github.com/QuincyGB/route-verdict) ·
[ListSmith](https://github.com/QuincyGB/list-smith) ·
[StallRow](https://github.com/QuincyGB/stall-row) ·
[listsmith-ci](https://github.com/QuincyGB/listsmith-ci) (the seller-side shovel)
