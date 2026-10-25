"""GuardConfig.from_env + GuardStore round-trips."""

import time

from x402_guard import GuardConfig, GuardStore


def test_from_env_defaults():
    cfg = GuardConfig.from_env({})
    assert cfg.settled_enabled is True
    assert cfg.drift_enabled is True
    assert cfg.drift_ttl_seconds == 86_400.0
    assert cfg.route_verdict_threshold_units == 100_000
    assert cfg.max_payment_units is None


def test_from_env_overrides():
    cfg = GuardConfig.from_env({
        "X402_GUARD_SETTLED_ENABLED": "0",
        "X402_GUARD_DRIFT_TTL_SECONDS": "3600",
        "X402_GUARD_PRICE_CHANGE_BLOCK_PCT": "5",
        "X402_GUARD_DRIFT_FAILURE": "block",
        "X402_GUARD_MAX_PAYMENT_USD": "0.25",
        "X402_GUARD_GLOBAL_DAILY_CAP_USD": "2.50",
        "X402_GUARD_ROUTE_VERDICT_THRESHOLD_USD": "0.50",
        "X402_GUARD_STORE_PATH": "/tmp/x/guard.db",
    })
    assert cfg.settled_enabled is False
    assert cfg.drift_ttl_seconds == 3600.0
    assert cfg.price_change_block_pct == 5.0
    assert cfg.drift_failure == "block"
    assert cfg.max_payment_units == 250_000
    assert cfg.global_daily_cap_units == 2_500_000
    assert cfg.route_verdict_threshold_units == 500_000
    assert cfg.store_path == "/tmp/x/guard.db"


def test_store_roundtrip(tmp_path):
    store = GuardStore(tmp_path / "g.db")
    now = time.time()
    assert store.get_check("https://x.example") is None
    store.save_check("https://x.example", "BASELINE", {"payTo": "0x1"}, now=now)
    got = store.get_check("https://x.example")
    assert got["status"] == "BASELINE"
    assert got["fingerprint"] == {"payTo": "0x1"}
    assert got["checked_at"] == now

    assert store.get_route_verdict("https://x.example") is None
    store.save_route_verdict("https://x.example", "TRUSTED", 91.5, now=now)
    rv = store.get_route_verdict("https://x.example")
    assert rv == {"checked_at": now, "verdict": "TRUSTED", "score": 91.5}

    store.record_spend("https://x.example", 2_000, now=now)
    store.record_spend("https://y.example", 3_000, now=now)
    store.record_spend("https://x.example", 0, now=now)  # zero is a no-op
    assert store.spent_today(now=now) == 5_000
    assert store.spent_today(now=now, url="https://x.example") == 2_000
    store.close()
