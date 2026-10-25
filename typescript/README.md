# @quincygb/x402-guard

The payment-path seatbelt for x402 buyer agents. Wrap your fetch
once; from then on, every x402 payment you make is pre-flighted
through Verdict Row **before anything is signed**:

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
4. **Your spend caps** — per-payment, per-endpoint-daily, and
   global-daily caps from your own config, checked against the
   guard's local ledger *before* signing. The guard refuses first;
   it never signs and discovers later.

Every decision — allow, warn, block, with the reason chain and what
the checks cost — is appended to a local audit log (JSONL) you can
read. Blocks throw `GuardBlockedError` carrying the full decision.

```ts
import { toClientEvmSigner } from "@x402/evm";
import { privateKeyToAccount } from "viem/accounts";
import { createGuardedFetch, GuardBlockedError } from "@quincygb/x402-guard";

// Any @x402/evm ClientEvmSigner works; this is the viem-account form.
const signer = toClientEvmSigner(privateKeyToAccount("0x..."));
const fetchGuarded = createGuardedFetch({
  signer,
  config: { globalDailyCapUnits: 1_000_000 }, // $1.00/day across everything
});

try {
  const res = await fetchGuarded("https://some-service.example/v1/thing", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ input: 1 }),
  });
  console.log(await res.json());
} catch (err) {
  if (err instanceof GuardBlockedError) {
    console.error("payment refused:", err.reasons);
  }
}
```

> The guard never sees key material beyond what your signer exposes
> to the standard x402 client — signing stays inside `@x402/fetch` +
> `@x402/evm`, exactly as in an unguarded buyer.

No signer = observe mode: the pipeline runs what it can for free,
paid stages are noted as skipped, and the raw 402 response comes
back unpaid. Use `new X402Guard({...}).evaluate(url, amountUnits)`
to inspect a full decision (stages, warnings, the Settled report)
without paying anything.

## What it costs you

| Stage | Cost | When |
|---|---|---|
| Settled pre-flight | free | every payment |
| Recheck drift check | $0.005 | first look per endpoint per TTL (24h default) |
| RouteVerdict gate | $0.05 | first big payment (≥ $0.10 default) per TTL |
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

Pass a config object, or build one from the environment with
`configFromEnv()` (`X402_GUARD_*`: stage toggles, URLs, TTL,
price-change %, failure policies — `"warn"` or `"block"` when a check
service itself errors — USD caps, store/audit paths). Local state
lives in `~/.x402-guard/` (JSON cache + spend ledger, JSONL audit
log) — your machine, your data, never transmitted.

Part of Verdict Row: [Token Verdict](https://github.com/QuincyGB/token-verdict) ·
[RouteVerdict](https://github.com/QuincyGB/route-verdict) ·
[ListSmith](https://github.com/QuincyGB/list-smith) ·
[StallRow](https://github.com/QuincyGB/stall-row) ·
[listsmith-ci](https://github.com/QuincyGB/listsmith-ci) (the seller-side shovel) ·
[x402-guard on PyPI](https://pypi.org/project/x402-guard/) (the Python package, same semantics)
