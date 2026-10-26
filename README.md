# x402-guard

**The payment-path seatbelt for x402 buyer agents.**

Every x402 buyer has the same quiet fear: the endpoint it is about
to pay changed its terms overnight — a new `payTo`, a doubled price,
a listing that no longer matches what it sells — or it never
delivered for anyone in the first place. Checking that by hand
before every payment is exactly the kind of work that stops
happening by week two.

`x402-guard` is the check, installed once, inside the payment path
itself. Wrap your buyer client (Python) or your fetch (TypeScript)
and every payment is pre-flighted through Verdict Row **before
anything is signed**:

| # | Stage | Cost | What it does |
|---|---|---|---|
| 1 | **Settled pre-flight** | free | Asks [settled.tools](https://settled.tools) what its scout's test purchases found about the endpoint. Hard veto **only** on a failed delivery or a not-payable verdict. A verified delivery corroborates; `nothing_to_compare` is noted, never a veto; Settled down or silent = pass-through. |
| 2 | **Drift check** ([StallRow Recheck](https://github.com/QuincyGB/stall-row)) | $0.005, cached | On a first look (or after the TTL, default 24h) fingerprints the endpoint's live payment terms and diffs them against your local store. `payTo` / asset changed → **hard block**. Price moved > 20% (configurable) → block. Schema / description / other changes → warn. Inside the TTL: cached, costs zero. |
| 3 | **RouteVerdict gate** ([RouteVerdict](https://github.com/QuincyGB/route-verdict)) | $0.05, cached | A first-ever payment ≥ $0.10 (configurable) requires a TRUSTED verdict ≥ 80 (configurable) for the target. |
| 4 | **Your spend caps** | free | Per-payment, per-endpoint-daily, and global-daily caps from your own config, checked against the guard's local ledger *before* signing. It refuses first — never signs and discovers later. |

Stage 2 drift checks can spend from a StallRow recheck pack
(config `recheck_pack_id` / `recheckPackId`): $0.004 per uncached check, no per-check
settlement, and the guard warns at 25 and 10 credits remaining
(balance reads are free). Without a pack, drift checks pay $0.005
per uncached check as before.

A block in any stage short-circuits the rest (the guard never spends
check money after a veto), raises/throws `GuardBlocked` with the
full reason chain, and — allowed or not — every decision is written
to a local audit log (JSONL) you can read. Bypassing the guard later
means rewriting your own safety policy, which is rather the point.

## Packages

| Language | Package | Status |
|---|---|---|
| Python | [`x402-guard`](python/) (PyPI) | built + tested; publish pending the owner's token |
| TypeScript | [`@quincygb/x402-guard`](typescript/) (npm) | built + tested; publish pending the owner's token |

Both packages implement the same pipeline with the same semantics
(the Python one is the reference; its Settled stage is a port of
verdict-loop's pre-flight, semantics fixed in that repo's commit
`eae6b4c`). Local state lives on the buyer's machine
(`~/.x402-guard/`: cache + spend ledger + audit log) and is never
transmitted anywhere.

```python
# Python
from x402_guard import GuardConfig, GuardedClient
guard = GuardedClient(account=account, config=GuardConfig(global_daily_cap_units=1_000_000))
result = guard.pay("https://some-service.example/v1/thing", {"input": 1})
```

```ts
// TypeScript
import { createGuardedFetch } from "@quincygb/x402-guard";
const fetchGuarded = createGuardedFetch({ signer, config: { globalDailyCapUnits: 1_000_000 } });
const res = await fetchGuarded("https://some-service.example/v1/thing", { method: "POST", body: "{}" });
```

## Dogfooded, in public

[verdict-loop](https://github.com/QuincyGB/verdict-loop) — Verdict
Row's reference buyer agent, which runs live every morning and
publishes its log — makes **all** of its payments through the Python
guard. Its daily cap is handed to the guard as an external budget,
so the guard's check calls and the loop's service calls share the
one cap. Watch it work (or watch it refuse) in
[the live log](https://github.com/QuincyGB/verdict-loop/blob/main/docs/live-log.md).

## What it cannot do

It judges **pre-payment signals only**: the inspector's delivery
record, the endpoint's current terms vs. its own history, a trust
verdict, and your caps. It cannot judge the paid payload — whether
the answer you bought was *good* is your application's problem. It
also cannot help if an endpoint's terms change between the drift
check and your payment inside the same TTL window; shorten the TTL
if that window matters to you.

## The other shovel

Sellers get the mirror image: [listsmith-ci](https://github.com/QuincyGB/listsmith-ci)
checks *your* listing in *your* CI pipeline — the traps buyers'
guards and inspectors catch, flagged at build time instead.

## License

MIT
