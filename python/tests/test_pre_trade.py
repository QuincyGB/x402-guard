"""Pre-Trade Guard stage (stage 4, opt-in) — fully offline.

One MockTransport plays the world; the StallRow host routes by path
(/v1/recheck for drift, /v1/pre-trade-guard for this stage). Every
paid fake answers 402 until a PAYMENT-SIGNATURE header arrives, then
returns its scripted payload and counts the paid call — so tests can
prove an ABORT blocked BEFORE the target was ever signed for, and
that the stage costs exactly one $0.005 check when it runs.
"""

import base64
import json

import httpx
import pytest
from eth_account import Account

from x402_guard import GuardBlocked, GuardConfig, GuardedClient

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F"
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


def fingerprint(pay_to=PAY_TO):
    return {
        "status_code": 402,
        "amount": "200000",
        "asset": USDC,
        "network": "eip155:8453",
        "payTo": pay_to,
        "description_hash": "d" * 64,
        "schema_hash": "s" * 64,
    }


def recheck_baseline():
    return {
        "watch_id": "w1",
        "url": TARGET,
        "kind": "x402",
        "status": "BASELINE",
        "changes": [],
        "current": fingerprint(),
        "checked_at": "2026-10-27T00:00:00Z",
        "summary": "status BASELINE",
    }


def pretrade_payload(verdict, risk=0, reasons=None):
    return {
        "pay_to": PAY_TO,
        "verdict": verdict,
        "risk_score": risk,
        "reasons": reasons or [],
        "index_history": {"status": "ok"},
        "wallet_snapshot": {"status": "ok"},
        "sanctions": {"status": "no_match"},
        "limits": "test",
    }


class FakePaidEndpoint:
    def __init__(self, url, amount, payloads, *, fail=False):
        self.url = url
        self.challenge_header = challenge(url, amount)
        self.payloads = list(payloads)
        self.fail = fail
        self.paid_calls = 0
        self.unpaid_calls = 0
        self.bodies = []

    def __call__(self, request):
        if request.headers.get("PAYMENT-SIGNATURE"):
            self.paid_calls += 1
            self.bodies.append(json.loads(request.content or b"{}"))
            if self.fail:
                return httpx.Response(500, text="upstream exploded")
            idx = min(self.paid_calls - 1, len(self.payloads) - 1)
            receipt = base64.b64encode(
                json.dumps({"success": True, "transaction": "0xfeed"}).encode()
            ).decode()
            return httpx.Response(
                200, json=self.payloads[idx], headers={"payment-response": receipt}
            )
        self.unpaid_calls += 1
        return httpx.Response(402, headers={"payment-required": self.challenge_header})


class World:
    def __init__(self, *, pretrade_payloads=None, pretrade_fail=False,
                 target_amount="200000"):
        self.target = FakePaidEndpoint(TARGET, target_amount, [{"ok": True}])
        self.recheck = FakePaidEndpoint(
            "https://stall-row-production.up.railway.app/v1/recheck",
            "5000", [recheck_baseline()],
        )
        self.pretrade = FakePaidEndpoint(
            "https://stall-row-production.up.railway.app/v1/pre-trade-guard",
            "5000",
            pretrade_payloads or [pretrade_payload("PROCEED", 0)],
            fail=pretrade_fail,
        )
        self.route_verdict = FakePaidEndpoint(
            CHECK_URL, "50000", [{"verdict": "TRUSTED", "score": 90.0}]
        )

    def __call__(self, request):
        host = request.url.host
        if host == "stall-row-production.up.railway.app":
            if request.url.path == "/v1/pre-trade-guard":
                return self.pretrade(request)
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


def make_guard(tmp_path, world, *, account="live", config=None, clock=None):
    http = httpx.Client(transport=httpx.MockTransport(world))
    cfg = config or GuardConfig(settled_enabled=False, pre_trade_enabled=True)
    cfg.store_path = str(tmp_path / "guard.db")
    cfg.audit_log_path = str(tmp_path / "audit.jsonl")
    acct = Account.create() if account == "live" else None
    return GuardedClient(account=acct, config=cfg, http=http, now=clock or Clock())


def stage(decision, name):
    return next(s for s in decision.stages if s.name == name)


# --- the stage ---------------------------------------------------------------

def test_disabled_by_default(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world, config=GuardConfig(settled_enabled=False))
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    assert stage(result.decision, "pre_trade").outcome == "skipped"
    assert "disabled" in stage(result.decision, "pre_trade").detail
    assert world.pretrade.paid_calls == 0


def test_stage_order_includes_pre_trade_before_caps(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert [s.name for s in result.decision.stages] == [
        "settled", "drift", "route_verdict", "pre_trade", "caps",
    ]


def test_proceed_passes_and_costs_one_check(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    s = stage(result.decision, "pre_trade")
    assert s.outcome == "pass"
    assert s.cost_units == 5000
    assert world.pretrade.paid_calls == 1
    # The screen was asked about the challenge's payTo and amount.
    body = world.pretrade.bodies[0]
    assert body["pay_to"] == PAY_TO
    assert body["amount"] == "200000"
    assert body["resource"] == TARGET


def test_abort_blocks_before_signing(tmp_path):
    world = World(pretrade_payloads=[
        pretrade_payload("ABORT", 100, ["payTo is an exact match on the OFAC SDN snapshot"])
    ])
    guard = make_guard(tmp_path, world)
    with pytest.raises(GuardBlocked) as excinfo:
        guard.pay(TARGET, {})
    assert "ABORT" in str(excinfo.value)
    assert world.target.paid_calls == 0
    assert world.pretrade.paid_calls == 1


def test_caution_warns_by_default(tmp_path):
    world = World(pretrade_payloads=[
        pretrade_payload("CAUTION", 40, ["payTo rotated recently"])
    ])
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    assert result.decision.label == "warn"
    assert stage(result.decision, "pre_trade").outcome == "warn"


def test_caution_blocks_when_configured(tmp_path):
    world = World(pretrade_payloads=[
        pretrade_payload("CAUTION", 40, ["payTo rotated recently"])
    ])
    cfg = GuardConfig(
        settled_enabled=False, pre_trade_enabled=True, pre_trade_caution="block"
    )
    guard = make_guard(tmp_path, world, config=cfg)
    with pytest.raises(GuardBlocked):
        guard.pay(TARGET, {})
    assert world.target.paid_calls == 0


def test_below_threshold_skips_without_spending(tmp_path):
    world = World(target_amount="20000")  # $0.02 < $0.10 threshold
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    assert stage(result.decision, "pre_trade").outcome == "skipped"
    assert "below" in stage(result.decision, "pre_trade").detail
    assert world.pretrade.paid_calls == 0


def test_dry_run_never_pays_for_the_screen(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world, account=None)
    result = guard.pay(TARGET, {})
    s = stage(result.decision, "pre_trade")
    assert s.outcome == "skipped"
    assert "dry-run" in s.detail
    assert world.pretrade.paid_calls == 0


def test_failure_policy_warn_passes_through(tmp_path):
    world = World(pretrade_fail=True)
    guard = make_guard(tmp_path, world)
    result = guard.pay(TARGET, {})
    assert result.outcome.mode == "paid"
    assert stage(result.decision, "pre_trade").outcome == "warn"


def test_failure_policy_block_refuses(tmp_path):
    world = World(pretrade_fail=True)
    cfg = GuardConfig(
        settled_enabled=False, pre_trade_enabled=True, pre_trade_failure="block"
    )
    guard = make_guard(tmp_path, world, config=cfg)
    with pytest.raises(GuardBlocked):
        guard.pay(TARGET, {})
    assert world.target.paid_calls == 0


def test_evaluate_uses_cached_fingerprint_payto(tmp_path):
    world = World(pretrade_payloads=[
        pretrade_payload("ABORT", 95, ["sanctions match"])
    ])
    clock = Clock()
    guard = make_guard(tmp_path, world, clock=clock)
    guard.store.save_check(TARGET, "BASELINE", fingerprint(), now=clock())
    decision = guard.evaluate(TARGET, 200_000)
    s = stage(decision, "pre_trade")
    assert s.outcome == "block"
    assert world.pretrade.paid_calls == 1
    assert world.pretrade.bodies[0]["pay_to"] == PAY_TO


def test_evaluate_without_any_payto_skips(tmp_path):
    world = World()
    guard = make_guard(tmp_path, world)
    decision = guard.evaluate(TARGET, 200_000)
    s = stage(decision, "pre_trade")
    assert s.outcome == "skipped"
    assert "no payTo known" in s.detail
    assert world.pretrade.paid_calls == 0


# --- config --------------------------------------------------------------------

def test_config_from_env_pre_trade():
    cfg = GuardConfig.from_env({
        "X402_GUARD_PRE_TRADE_ENABLED": "1",
        "X402_GUARD_PRE_TRADE_THRESHOLD_USD": "0.25",
        "X402_GUARD_PRE_TRADE_CAUTION": "block",
        "X402_GUARD_PRE_TRADE_FAILURE": "block",
    })
    assert cfg.pre_trade_enabled is True
    assert cfg.pre_trade_threshold_units == 250_000
    assert cfg.pre_trade_caution == "block"
    assert cfg.pre_trade_failure == "block"
    defaults = GuardConfig.from_env({})
    assert defaults.pre_trade_enabled is False
    assert defaults.pre_trade_threshold_units == 100_000


def test_config_rejects_bad_pre_trade_words():
    with pytest.raises(ValueError):
        GuardConfig(pre_trade_caution="shrug")
    with pytest.raises(ValueError):
        GuardConfig(pre_trade_failure="shrug")
