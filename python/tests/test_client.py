"""GuardedClient policy pipeline — fully offline.

One MockTransport plays the whole world:

  api.example.com                          the target the buyer pays
  settled.tools                            Settled's free MCP endpoint
  stall-row-production.up.railway.app      StallRow /v1/recheck
  route-verdict-production.up.railway.app  RouteVerdict /v1/check

Every paid fake answers 402 until a PAYMENT-SIGNATURE header arrives,
then returns its scripted payload and counts the paid call — so tests
can prove a block happened BEFORE anything was signed.
"""

import base64
import json

import httpx
import pytest
from eth_account import Account

from x402_guard import GuardBlocked, GuardConfig, GuardedClient

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
PAY_TO_MOVED = "0x2222222222222222222222222222222222222222"

TARGET = "https://api.example.com/v1/thing"
CHECK_URL = "https://route-verdict-production.up.railway.app/v1/check"


def challenge(url: str, amount: str, pay_to: str = PAY_TO) -> str:
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": url, "description": "thing", "mimeType": "application/json"},
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": amount,
                "asset": USDC,
                "payTo": pay_to,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
        "extensions": {},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def fingerprint(amount="20000", pay_to=PAY_TO, asset=USDC):
    return {
        "status_code": 402,
        "amount": amount,
        "asset": asset,
        "network": "eip155:8453",
        "payTo": pay_to,
        "description_hash": "d" * 64,
        "schema_hash": "s" * 64,
    }


def recheck_response(status, *, changes=None, current=None):
    return {
        "watch_id": "w1",
        "url": TARGET,
        "kind": "x402",
        "status": status,
        "changes": changes or [],
        "current": current if current is not None else fingerprint(),
        "checked_at": "2026-10-25T00:00:00Z",
        "summary": f"status {status}",
    }


# --- Settled payloads ---------------------------------------------------

def settled_unknown(url):
    return {
        "url": url,
        "status": "unknown",
        "payability": {"verdict": "unknown"},
        "delivery_verified": None,
        "quality": {"total": 10},
    }


def settled_failed_delivery(url):
    return {
        "url": url,
        "status": "live",
        "payability": {"verdict": "payable"},
        "delivery_verified": False,
        "quality": {"total": 45},
        "delivery_receipt": {
            "delivered": False,
            "checks": {"verdict": "fields_missing"},
            "paid_usd": 0.02,
        },
    }


def settled_nothing_to_compare(url):
    return {
        "url": url,
        "status": "live",
        "payability": {"verdict": "delivered"},
        "delivery_verified": True,
        "quality": {"total": 82},
        "delivery_receipt": {
            "delivered": True,
            "checks": {"verdict": "nothing_to_compare"},
            "paid_usd": 0.02,
        },
    }


def settled_not_payable(url):
    payload = settled_unknown(url)
    payload["payability"] = {"verdict": "not_payable"}
    payload["status"] = "live"
    return payload


# --- the fake world ------------------------------------------------------

class FakePaidEndpoint:
    """402 until paid; then a scripted JSON payload. Counts paid calls."""

    def __init__(self, url, amount, payloads):
        self.url = url
        self.amount = amount
        self.payloads = list(payloads)  # one per paid call; last repeats
        self.challenge_header = challenge(url, amount)
        self.paid_calls = 0
        self.unpaid_calls = 0

    def __call__(self, request):
        if request.headers.get("PAYMENT-SIGNATURE"):
            self.paid_calls += 1
            idx = min(self.paid_calls - 1, len(self.payloads) - 1)
            receipt = base64.b64encode(
                json.dumps({"success": True, "transaction": "0xfeed"}).encode()
            ).decode()
            return httpx.Response(
                200, json=self.payloads[idx], headers={"payment-response": receipt}
            )
        self.unpaid_calls += 1
        return httpx.Response(402, headers={"payment-required": self.challenge_header})


class FakeSettledMcp:
    def __init__(self, payload_for, *, down=False):
        self.payload_for = payload_for
        self.down = down
        self.tool_calls = 0

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("settled unreachable", request=request)
        body = json.loads(request.content)
        method = body.get("method")
        if method == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": "2025-06-18",
                           "capabilities": {"tools": {}}},
            })
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            self.tool_calls += 1
            payload = self.payload_for(body["params"]["arguments"]["url"])
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"content": [{"type": "text", "text": json.dumps(payload)}],
                           "structuredContent": payload, "isError": False},
            })
        raise AssertionError(f"unexpected MCP method: {method}")


class World:
    def __init__(self, *, settled_for=None, settled_down=False,
                 recheck_payloads=None, rv_payloads=None, target_amount="20000"):
        self.target = FakePaidEndpoint(TARGET, target_amount, [{"ok": True}])
        self.recheck = FakePaidEndpoint(
            "https://stall-row-production.up.railway.app/v1/recheck",
            "5000",
            recheck_payloads or [recheck_response("BASELINE")],
        )
        self.route_verdict = FakePaidEndpoint(
            CHECK_URL, "50000",
            rv_payloads or [{"verdict": "TRUSTED", "score": 90.0}],
        )
        self.settled = FakeSettledMcp(settled_for or settled_unknown, down=settled_down)

    def __call__(self, request):
        host = request.url.host
        if host == "settled.tools":
            return self.settled(request)
        if host == "stall-row-production.up.railway.app":
            return self.recheck(request)
        if host == "route-verdict-production.up.railway.app":
            return self.route_verdict(request)
        if host == "api.example.com":
            return self.target(request)
        raise AssertionError(f"unexpected host: {host}")


class Clock:
    def __init__(self, start=1_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_guard(tmp_path, world, *, account="live", config=None, clock=None):
    http = httpx.Client(transport=httpx.MockTransport(world))
    cfg = config or GuardConfig()
    cfg.store_path = str(tmp_path / "guard.db")
    cfg.audit_log_path = str(tmp_path / "audit.jsonl")
    acct = Account.create() if account == "live" else None
    return GuardedClient(
        account=acct, config=cfg, http=http, now=clock or Clock()
    )


def read_audit(tmp_path):
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- Settled stage ---------------------------------------------------------

def test_settled_veto_blocks_before_any_signing(tmp_path):
    world = World(settled_for=settled_failed_delivery)
    guard = make_guard(tmp_path, world)
    with pytest.raises(GuardBlocked) as excinfo:
        guard.pay(TARGET, {"x": 1})
    assert "delivered=false" in str(excinfo.value)
    assert world.target.paid_calls == 0
    assert world.recheck.paid_calls == 0  # veto short-circuits paid stages
    entries = read_audit(tmp_path)
    assert entries[-1]["decision"] == "block"
    assert entries[-1]["outcome"] == "blocked"


def test_settled_not_payable_vetoes(tmp_path):
    world = World(settled_for=settled_not_payable)
    guard = make_guard(tmp_path, world)
    with pytest.raises(GuardBlocked):
        guard.pay(TARGET, {})
    assert world.target.paid_calls == 0


def test_settled_outage_passes_through(tmp_path):
    world = World(settled_down=True)
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    assert world.target.paid_calls == 1
    settled_stage = result.decision.stages[0]
    assert settled_stage.outcome == "pass"
    assert "unavailable" in settled_stage.detail


def test_nothing_to_compare_is_noted_not_a_veto(tmp_path):
    world = World(settled_for=settled_nothing_to_compare)
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    settled_stage = result.decision.stages[0]
    assert settled_stage.outcome == "pass"
    assert "nothing to compare" in settled_stage.detail
    assert result.decision.settled_report.corroborated


# --- drift stage -------------------------------------------------------------

def test_first_payment_records_baseline(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    drift = result.decision.stages[1]
    assert drift.outcome == "pass" and "BASELINE" in drift.detail
    assert drift.cost_units == 5000
    assert world.recheck.paid_calls == 1


def test_drift_payto_change_blocks(tmp_path):
    clock = Clock()
    world = World(recheck_payloads=[
        recheck_response("BASELINE"),
        recheck_response(
            "CHANGED",
            changes=[{"field": "payTo", "before": PAY_TO, "after": PAY_TO_MOVED}],
            current=fingerprint(pay_to=PAY_TO_MOVED),
        ),
    ])
    guard = make_guard(tmp_path, world, clock=clock)
    guard.pay(TARGET, {})
    assert world.target.paid_calls == 1
    clock.advance(86_401)  # past the 24h TTL: the next pay re-checks
    with pytest.raises(GuardBlocked) as excinfo:
        guard.pay(TARGET, {})
    assert "payTo changed" in str(excinfo.value)
    assert world.target.paid_calls == 1  # the second payment never signed


def test_drift_price_spike_blocks_small_move_warns(tmp_path):
    clock = Clock()
    world = World(recheck_payloads=[
        recheck_response("BASELINE"),
        recheck_response(
            "CHANGED",
            changes=[{"field": "amount", "before": "20000", "after": "21000"}],
            current=fingerprint(amount="21000"),
        ),
    ])
    guard = make_guard(tmp_path, world, clock=clock)
    guard.pay(TARGET, {})
    clock.advance(86_401)
    result = guard.pay(TARGET, {})  # +5% < 20% threshold: warn, proceed
    drift = result.decision.stages[1]
    assert drift.outcome == "warn"
    assert result.outcome.mode == "paid"

    clock2 = Clock()
    world2 = World(recheck_payloads=[
        recheck_response("BASELINE"),
        recheck_response(
            "CHANGED",
            changes=[{"field": "amount", "before": "20000", "after": "30000"}],
            current=fingerprint(amount="30000"),
        ),
    ])
    guard2 = make_guard(tmp_path / "b", world2, clock=clock2)
    guard2.pay(TARGET, {})
    clock2.advance(86_401)
    with pytest.raises(GuardBlocked) as excinfo:
        guard2.pay(TARGET, {})  # +50% > 20%: block
    assert "price changed 50.0%" in str(excinfo.value)


def test_description_only_change_warns(tmp_path):
    clock = Clock()
    world = World(recheck_payloads=[
        recheck_response("BASELINE"),
        recheck_response(
            "CHANGED",
            changes=[{"field": "description_hash", "before": "d" * 64, "after": "e" * 64}],
        ),
    ])
    guard = make_guard(tmp_path, world, clock=clock)
    guard.pay(TARGET, {})
    clock.advance(86_401)
    result = guard.pay(TARGET, {})
    drift = result.decision.stages[1]
    assert drift.outcome == "warn"
    assert result.outcome.mode == "paid"


def test_cache_ttl_avoids_second_recheck_call(tmp_path):
    clock = Clock()
    world = World()
    guard = make_guard(tmp_path, world, clock=clock)
    guard.pay(TARGET, {})
    guard.pay(TARGET, {})
    clock.advance(3_600)  # 1h later: still inside the 24h TTL
    result = guard.pay(TARGET, {})
    assert world.recheck.paid_calls == 1
    drift = result.decision.stages[1]
    assert drift.outcome == "pass" and "cached" in drift.detail
    assert drift.cost_units == 0
    clock.advance(86_401)
    guard.pay(TARGET, {})
    assert world.recheck.paid_calls == 2  # TTL expired: checked again


def test_recheck_failure_warns_by_default_and_can_block(tmp_path):
    class BrokenRecheck:
        def __call__(self, request):
            if request.url.host == "stall-row-production.up.railway.app":
                return httpx.Response(500, json={"error": "boom"})
            return world(request)

    world = World()
    guard = make_guard(tmp_path, BrokenRecheck())
    result = guard.pay(TARGET, {})
    drift = result.decision.stages[1]
    assert drift.outcome == "warn"
    assert result.outcome.mode == "paid"

    cfg = GuardConfig(drift_failure="block")
    guard2 = make_guard(tmp_path / "c", BrokenRecheck(), config=cfg)
    with pytest.raises(GuardBlocked):
        guard2.pay(TARGET, {})


# --- RouteVerdict gate --------------------------------------------------------

def gate_config():
    # $0.01 threshold: the $0.02 target payment trips the gate.
    return GuardConfig(route_verdict_threshold_units=10_000)


def test_gate_blocks_untrusted_first_big_payment(tmp_path):
    world = World(rv_payloads=[{"verdict": "CAUTION", "score": 50.0}])
    guard = make_guard(tmp_path, world, config=gate_config())
    with pytest.raises(GuardBlocked) as excinfo:
        guard.pay(TARGET, {})
    assert "CAUTION" in str(excinfo.value)
    assert world.target.paid_calls == 0
    assert world.route_verdict.paid_calls == 1


def test_gate_passes_trusted_and_caches(tmp_path):
    world = World(rv_payloads=[{"verdict": "TRUSTED", "score": 90.0}])
    guard = make_guard(tmp_path, world, config=gate_config())
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    rv = result.decision.stages[2]
    assert rv.outcome == "pass" and rv.cost_units == 50_000
    guard.pay(TARGET, {})
    assert world.route_verdict.paid_calls == 1  # second pay used the cache


def test_gate_skipped_below_threshold(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)  # default threshold $0.10 > $0.02
    result = guard.pay(TARGET, {})
    rv = result.decision.stages[2]
    assert rv.outcome == "skipped"
    assert world.route_verdict.paid_calls == 0


# --- caps ----------------------------------------------------------------------

def test_per_payment_cap_blocks(tmp_path):
    world = World()
    cfg = GuardConfig(max_payment_units=10_000, drift_enabled=False,
                      route_verdict_enabled=False)
    guard = make_guard(tmp_path, world, config=cfg)
    with pytest.raises(GuardBlocked) as excinfo:
        guard.pay(TARGET, {})
    assert "per-payment cap" in str(excinfo.value)
    assert world.target.paid_calls == 0


def test_per_endpoint_daily_cap_blocks_second_payment(tmp_path):
    world = World()
    cfg = GuardConfig(per_endpoint_daily_cap_units=30_000, drift_enabled=False,
                      route_verdict_enabled=False)
    guard = make_guard(tmp_path, world, config=cfg)
    guard.pay(TARGET, {})  # $0.02 settles
    with pytest.raises(GuardBlocked):
        guard.pay(TARGET, {})  # another $0.02 would make $0.04 > $0.03 cap
    assert world.target.paid_calls == 1


def test_global_cap_counts_check_spend(tmp_path):
    world = World()
    cfg = GuardConfig(global_daily_cap_units=25_000)
    guard = make_guard(tmp_path, world, config=cfg)
    result = guard.pay(TARGET, {})  # $0.005 Recheck + $0.02 target = cap exactly
    assert result.outcome.mode == "paid"
    with pytest.raises(GuardBlocked):
        guard.pay(TARGET, {})  # any further spend today breaks the global cap
    assert world.target.paid_calls == 1


# --- dry-run ---------------------------------------------------------------------

def test_dry_run_reports_veto_without_raising_or_paying(tmp_path):
    world = World(settled_for=settled_failed_delivery)
    guard = make_guard(tmp_path, world, account=None)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "dry_run"
    assert result.outcome.price_units == 20_000
    assert result.decision.blocked
    assert result.decision.settled_veto is not None
    assert world.target.paid_calls == 0
    assert world.recheck.paid_calls == 0


def test_dry_run_clean_target_would_pay(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world, account=None)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "dry_run"
    assert not result.decision.blocked
    drift = result.decision.stages[1]
    assert drift.outcome == "skipped"  # paid stages never run in dry-run


# --- pay_on_behalf -----------------------------------------------------------------

def test_pay_on_behalf_vets_target_pays_checker(tmp_path):
    world = World(settled_for=settled_failed_delivery)
    guard = make_guard(tmp_path, world)
    with pytest.raises(GuardBlocked):
        guard.pay_on_behalf(TARGET, CHECK_URL, {"resource_url": TARGET})
    assert world.route_verdict.paid_calls == 0

    world2 = World()
    guard2 = make_guard(tmp_path / "d", world2)
    result = guard2.pay_on_behalf(TARGET, CHECK_URL, {"resource_url": TARGET})
    assert result.outcome.mode == "paid"
    assert result.outcome.response == {"verdict": "TRUSTED", "score": 90.0}
    assert world2.route_verdict.paid_calls == 1


# --- audit log -----------------------------------------------------------------------

def test_audit_log_records_decisions_and_costs(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)
    guard.pay(TARGET, {})
    entries = read_audit(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["decision"] == "allow"
    assert entry["outcome"] == "paid"
    assert entry["check_cost_units"] == 5000
    assert [s["name"] for s in entry["stages"]] == [
        "settled", "drift", "route_verdict", "caps",
    ]
    assert entry["amount_units"] == 20_000
    assert entry["dry_run"] is False


# --- Recheck pack spending (drift stage) -------------------------------------

class PackWorld:
    """A pack-aware fake world. The StallRow fake answers /v1/recheck
    with a scripted payload when the body carries a known pack_id (no
    payment header ever), an error status for an exhausted pack, and
    serves pack-balance reads. It records every request so tests can
    prove no PAYMENT-SIGNATURE was ever sent for a pack check."""

    def __init__(self, *, balance=249, pack_status="BASELINE"):
        self.settled = FakeSettledMcp(settled_unknown)
        self.balance = balance
        self.pack_status = pack_status
        self.pack_spends = 0
        self.balance_reads = 0
        self.signed_requests = 0

    def __call__(self, request):
        if request.headers.get("PAYMENT-SIGNATURE"):
            self.signed_requests += 1
        host = request.url.host
        path = request.url.path
        if host == "settled.tools":
            return self.settled(request)
        if host == "stall-row-production.up.railway.app":
            body = json.loads(request.content or b"{}")
            if path == "/v1/recheck/pack-balance":
                self.balance_reads += 1
                return httpx.Response(200, json={
                    "pack_id": body.get("pack_id"),
                    "credits_total": 250,
                    "credits_remaining": self.balance,
                    "expires_at": "2027-01-23T00:00:00Z",
                    "expired": False,
                })
            if path == "/v1/recheck":
                pack_id = body.get("pack_id")
                if pack_id == "pack_test":
                    self.pack_spends += 1
                    return httpx.Response(200, json=recheck_response(self.pack_status))
                if pack_id == "pack_empty":
                    return httpx.Response(402, json={
                        "error": "pack exhausted", "code": "pack_exhausted",
                    })
                return httpx.Response(404, json={"error": "unknown pack_id"})
        raise AssertionError(f"unexpected request: {host}{path}")


def make_pack_guard(tmp_path, world, *, account="live", pack_id="pack_test"):
    http = httpx.Client(transport=httpx.MockTransport(world))
    cfg = GuardConfig(recheck_pack_id=pack_id)
    cfg.store_path = str(tmp_path / "guard.db")
    cfg.audit_log_path = str(tmp_path / "audit.jsonl")
    acct = Account.create() if account == "live" else None
    return GuardedClient(account=acct, config=cfg, http=http, now=Clock())


def test_pack_spend_uses_credit_not_payment(tmp_path):
    world = PackWorld(balance=249)
    guard = make_pack_guard(tmp_path, world)
    decision = guard.evaluate(TARGET, 20_000)
    drift = [s for s in decision.stages if s.name == "drift"][0]
    assert drift.outcome == "pass"
    assert drift.cost_units == 0
    assert "pack credit" in drift.detail
    assert "249 of 250" in drift.detail
    assert world.pack_spends == 1
    assert world.balance_reads == 1
    assert world.signed_requests == 0  # nothing was signed or settled


def test_pack_low_balance_warns(tmp_path):
    world = PackWorld(balance=10)
    guard = make_pack_guard(tmp_path, world)
    decision = guard.evaluate(TARGET, 20_000)
    drift = [s for s in decision.stages if s.name == "drift"][0]
    assert "PACK LOW" in drift.detail
    assert "never falls back" in drift.detail


def test_pack_exhausted_fails_without_per_call_fallback(tmp_path):
    world = PackWorld()
    guard = make_pack_guard(tmp_path, world, pack_id="pack_empty")
    decision = guard.evaluate(TARGET, 20_000)
    drift = [s for s in decision.stages if s.name == "drift"][0]
    assert drift.outcome == "warn"  # default drift_failure policy
    assert "no per-call" in drift.detail and "fallback" in drift.detail
    assert world.pack_spends == 0
    assert world.signed_requests == 0  # the guard did NOT pay per-call instead


def test_pack_not_spent_in_dry_run(tmp_path):
    world = PackWorld()
    guard = make_pack_guard(tmp_path, world, account=None)
    decision = guard.evaluate(TARGET, 20_000)
    drift = [s for s in decision.stages if s.name == "drift"][0]
    assert drift.outcome == "skipped"
    assert world.pack_spends == 0


def test_recheck_pack_id_from_env():
    cfg = GuardConfig.from_env({"X402_GUARD_RECHECK_PACK_ID": "pack_abc"})
    assert cfg.recheck_pack_id == "pack_abc"
    assert GuardConfig.from_env({}).recheck_pack_id is None
