"""GuardedClient: every payment pre-flighted, before anything is signed.

The pipeline (same semantics in the TypeScript package):

  1. Settled pre-flight (FREE) — ask settled.tools what its scout's
     test purchases found about the target endpoint. Hard veto ONLY
     on a failed delivery or a not-payable verdict; a verified
     delivery corroborates; nothing_to_compare is noted, never a
     veto; Settled down or silent = pass-through.
  2. Drift check (StallRow Recheck, $0.005, cached) — if the target
     is new to this buyer, or the cached look is older than the TTL
     (default 24h), pay Recheck to fingerprint the target's live
     payment terms and diff them against the guard's local store.
     With `recheck_pack_id` configured, the check instead spends one
     credit from a prepaid StallRow recheck pack ($1.00 = 250 checks
     on one endpoint): no per-check settlement, and an exhausted or
     expired pack fails the stage — it never falls back to paying
     per call.
     payTo or asset changed = hard block. Price moved more than the
     configured % (default 20%) = block. Schema/description/other
     changes = warn. Inside the TTL the cached result is used and
     the stage costs nothing.
  3. RouteVerdict gate (optional, $0.05, cached) — a first-ever
     payment at or above the configured amount threshold (default
     $0.10) requires a TRUSTED verdict at or above the configured
     trust score (default 80) for the target.
  4. Spend caps — per-payment, per-endpoint-daily, and global-daily
     caps from the buyer's own config, checked against the guard's
     local spend ledger BEFORE signing. Refusal here never signs
     first and discovers later.

A block in any stage short-circuits the rest (later stages are
recorded as skipped) — the guard never spends check money after a
veto. Every completed evaluation is written to the audit log,
allowed or not.

Modes: with no signer (account=None) the guard is a dry-run — the
free Settled stage and any fresh cache still run, paid stages are
noted as skipped, blocks are reported on the returned decision
(`decision.blocked`) instead of raised, and the payment outcome is
the unpaid probe (exactly what a live run would pay). With a signer,
a blocked payment raises GuardBlocked BEFORE anything is signed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .audit import AuditLog
from .config import GuardConfig
from .errors import GuardBlocked
from .settled import SettledReport, fetch_settled_report
from .store import GuardStore
from .x402client import CallOutcome, ServiceError, call_paid_endpoint, format_usd

__all__ = [
    "GuardedClient",
    "GuardDecision",
    "GuardResult",
    "StageResult",
    "GuardBlocked",
]


@dataclass
class StageResult:
    name: str  # settled | drift | route_verdict | caps
    outcome: str  # pass | warn | block | skipped
    detail: str
    cost_units: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "cost_units": self.cost_units,
        }


@dataclass
class GuardDecision:
    """The full record of one pipeline evaluation."""

    url: str  # the endpoint being vetted (the payment's target)
    pay_url: str  # the URL actually being paid (== url for pay())
    amount_units: int | None
    stages: list[StageResult] = field(default_factory=list)
    settled_report: SettledReport | None = None

    @property
    def check_cost_units(self) -> int:
        return sum(s.cost_units for s in self.stages)

    @property
    def blocked(self) -> bool:
        return any(s.outcome == "block" for s in self.stages)

    @property
    def reasons(self) -> list[str]:
        return [s.detail for s in self.stages if s.outcome == "block"]

    @property
    def warnings(self) -> list[str]:
        return [s.detail for s in self.stages if s.outcome == "warn"]

    @property
    def settled_veto(self) -> str | None:
        if self.settled_report is None:
            return None
        return self.settled_report.hard_negative

    @property
    def label(self) -> str:
        if self.blocked:
            return "block"
        if self.warnings:
            return "warn"
        return "allow"

    def summary(self) -> str:
        bits = ", ".join(f"{s.name} {s.outcome}" for s in self.stages)
        line = f"guard {self.label} ({bits})"
        if self.check_cost_units:
            line += f"; checks cost {format_usd(self.check_cost_units)}"
        for reason in self.reasons:
            line += f"; BLOCKED: {reason}"
        return line

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "pay_url": self.pay_url,
            "amount_units": self.amount_units,
            "decision": self.label,
            "blocked": self.blocked,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "check_cost_units": self.check_cost_units,
            "stages": [s.to_dict() for s in self.stages],
        }


@dataclass
class GuardResult:
    outcome: CallOutcome
    decision: GuardDecision


class GuardedClient:
    """A buyer client with the Verdict Row seatbelt bolted on.

    account: an eth_account LocalAccount, or None for dry-run.
    budget:  an optional external spend-cap object with
             authorize(units) / record(units, label=) — the shape of
             verdict-loop's DailySpendCap. Every settled payment the
             guard makes (targets AND check calls) is authorized and
             recorded against it, so an embedding app keeps ONE cap.
    http:    an httpx.Client used for EVERY request (target, Settled,
             Recheck, RouteVerdict) — inject a MockTransport client in
             tests. Created (and owned) when not supplied.
    store / audit: injectable for tests; default to the configured
             paths (~/.x402-guard/ out of the box).
    """

    def __init__(
        self,
        *,
        account=None,
        config: GuardConfig | None = None,
        http: httpx.Client | None = None,
        budget=None,
        store: GuardStore | None = None,
        audit: AuditLog | None = None,
        now=None,
    ):
        self.config = config or GuardConfig()
        self.account = account
        self.budget = budget
        self._owns_http = http is None
        self.http = http if http is not None else httpx.Client(timeout=30.0)
        self.store = store if store is not None else GuardStore(self.config.store_path)
        if audit is not None:
            self.audit = audit
        else:
            audit_path = self.config.audit_log_path or str(
                Path(self.store.path).parent / "audit.jsonl"
            )
            self.audit = AuditLog(audit_path)
        self._now = now or time.time

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
        self.store.close()

    def __enter__(self) -> "GuardedClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, target_url: str, amount_units: int | None = None) -> GuardDecision:
        """Run the policy pipeline for a target without paying it.

        With amount_units=None the amount-dependent stages (RouteVerdict
        gate threshold, caps) are skipped as "price not supplied".
        Writes an audit entry with outcome "evaluated".
        """
        decision = self._pipeline(target_url, amount_units, spend_url=target_url)
        self._write_audit(decision, outcome_mode="evaluated", dry_run=self.account is None)
        return decision

    def pay(self, url: str, body: dict | None = None) -> GuardResult:
        """Pay `url` (an x402 endpoint) through the guard.

        Raises GuardBlocked (live mode) before signing when a stage
        blocks. In dry-run (no signer) returns the probe outcome with
        the decision attached — decision.blocked says what a live run
        would have refused.
        """
        return self._guarded(target_url=url, pay_url=url, body=body or {})

    def pay_on_behalf(
        self, target_url: str, pay_url: str, body: dict | None = None
    ) -> GuardResult:
        """Vet `target_url` through the pipeline, then pay `pay_url`.

        For flows where the thing being vetted and the thing being
        paid differ — e.g. paying RouteVerdict to check a third-party
        service: the pipeline vets the service (target), the payment
        goes to RouteVerdict (pay_url).
        """
        return self._guarded(target_url=target_url, pay_url=pay_url, body=body or {})

    # ------------------------------------------------------------------
    # The guarded payment
    # ------------------------------------------------------------------

    def _guarded(self, *, target_url: str, pay_url: str, body: dict) -> GuardResult:
        probe = call_paid_endpoint(self.http, pay_url, body)
        if probe.mode == "paid":
            # Answered 200 without demanding payment — nothing to vet,
            # and never pretend a payment (or a check) occurred.
            decision = GuardDecision(
                url=target_url,
                pay_url=pay_url,
                amount_units=0,
                stages=[
                    StageResult(
                        "settled", "skipped",
                        "endpoint answered 200 without demanding payment; "
                        "pipeline not run",
                    )
                ],
            )
            self._write_audit(decision, outcome_mode="paid_free",
                              dry_run=self.account is None)
            return GuardResult(probe, decision)

        amount_units = probe.price_units
        decision = self._pipeline(target_url, amount_units, spend_url=pay_url)

        if decision.blocked:
            self._write_audit(decision, outcome_mode="blocked",
                              dry_run=self.account is None)
            if self.account is not None:
                raise GuardBlocked(decision)
            return GuardResult(probe, decision)

        if self.account is None:
            self._write_audit(decision, outcome_mode="dry_run", dry_run=True)
            return GuardResult(probe, decision)

        try:
            outcome = call_paid_endpoint(
                self.http, pay_url, body, account=self.account, budget=self.budget
            )
        except ServiceError:
            self._write_audit(decision, outcome_mode="error", dry_run=False)
            raise
        if outcome.mode == "paid" and outcome.price_units > 0:
            self.store.record_spend(pay_url, outcome.price_units, now=self._now())
        self._write_audit(decision, outcome_mode=outcome.mode, dry_run=False)
        return GuardResult(outcome, decision)

    # ------------------------------------------------------------------
    # The pipeline
    # ------------------------------------------------------------------

    def _pipeline(
        self, target_url: str, amount_units: int | None, *, spend_url: str
    ) -> GuardDecision:
        cfg = self.config
        stages: list[StageResult] = []
        settled_report: SettledReport | None = None

        # --- 1. Settled pre-flight (free) ------------------------------
        if cfg.settled_enabled:
            settled_report = fetch_settled_report(
                self.http,
                target_url,
                endpoint=cfg.settled_mcp_url,
                timeout=cfg.settled_timeout,
            )
            veto = settled_report.hard_negative
            if veto:
                stages.append(StageResult("settled", "block", veto))
            else:
                stages.append(
                    StageResult("settled", "pass", settled_report.describe())
                )
        else:
            stages.append(StageResult("settled", "skipped", "disabled in config"))

        # --- 2. Drift check (Recheck, cached) ---------------------------
        if _blocked(stages):
            stages.append(
                StageResult("drift", "skipped", "not run — an earlier stage blocked")
            )
        else:
            stages.append(self._drift_stage(target_url))

        # --- 3. RouteVerdict gate ----------------------------------------
        if _blocked(stages):
            stages.append(
                StageResult(
                    "route_verdict", "skipped", "not run — an earlier stage blocked"
                )
            )
        else:
            stages.append(self._route_verdict_stage(target_url, amount_units))

        # --- 4. Spend caps -------------------------------------------------
        if _blocked(stages):
            stages.append(
                StageResult("caps", "skipped", "not run — an earlier stage blocked")
            )
        else:
            stages.append(self._caps_stage(amount_units, spend_url))

        return GuardDecision(
            url=target_url,
            pay_url=spend_url,
            amount_units=amount_units,
            stages=stages,
            settled_report=settled_report,
        )

    # ------------------------------------------------------------------
    # Stage 2: drift
    # ------------------------------------------------------------------

    def _drift_stage(self, target_url: str) -> StageResult:
        cfg = self.config
        if not cfg.drift_enabled:
            return StageResult("drift", "skipped", "disabled in config")
        now = self._now()
        cached = self.store.get_check(target_url)
        if cached is not None and now - cached["checked_at"] < cfg.drift_ttl_seconds:
            age = now - cached["checked_at"]
            return StageResult(
                "drift",
                "pass",
                f"cached Recheck result {age:.0f}s old "
                f"(TTL {cfg.drift_ttl_seconds:.0f}s), status {cached['status']} "
                f"— no new Recheck call",
            )
        if self.account is None:
            note = (
                "no cached Recheck result"
                if cached is None
                else "cached Recheck result is stale"
            )
            return StageResult(
                "drift", "skipped", f"{note}; dry-run never pays for checks"
            )
        if cfg.recheck_pack_id:
            # Pack path: the pack id is the credential. StallRow's
            # middleware lets a pack-bearing request through unpaid and
            # the handler spends one credit — no settlement, no budget
            # authorization, cost_units 0. An exhausted/expired/unknown
            # pack fails the stage under drift_failure; the guard NEVER
            # falls back to per-call payment silently.
            try:
                resp = self._call_recheck_with_pack(target_url)
            except ServiceError as exc:
                return self._failure(
                    "drift", cfg.drift_failure,
                    f"Recheck pack spend failed: {exc} — no per-call "
                    "fallback was attempted",
                )
            result = self._drift_result(resp, 0, now, target_url)
            note = self._pack_balance_note()
            if note:
                result = StageResult(
                    result.name, result.outcome,
                    f"{result.detail}; {note}", cost_units=result.cost_units,
                )
            return result
        try:
            outcome = call_paid_endpoint(
                self.http,
                cfg.recheck_url,
                {"url": target_url},
                account=self.account,
                budget=self.budget,
            )
        except ServiceError as exc:
            return self._failure("drift", cfg.drift_failure,
                                 f"Recheck call failed: {exc}")
        if outcome.mode == "blocked":
            return StageResult(
                "drift", "block",
                f"Recheck check refused by the spend budget: {outcome.note}",
            )
        if outcome.mode != "paid" or not isinstance(outcome.response, dict):
            return self._failure("drift", cfg.drift_failure,
                                 "Recheck returned no usable result")
        return self._drift_result(outcome.response, outcome.price_units, now, target_url)

    def _call_recheck_with_pack(self, target_url: str) -> dict:
        """One pack-credit Recheck: POST with pack_id, no payment.

        Raises ServiceError on any non-200 (402 exhausted, 410 expired,
        403 wrong endpoint, 404 unknown pack) — the caller turns that
        into the stage's failure policy, never a paid retry.
        """
        cfg = self.config
        response = self.http.post(
            cfg.recheck_url,
            json={"url": target_url, "pack_id": cfg.recheck_pack_id},
        )
        if response.status_code != 200:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise ServiceError(response.status_code, detail)
        data = response.json()
        if not isinstance(data, dict):
            raise ServiceError(200, "Recheck pack response was not an object")
        return data

    def _pack_balance_note(self) -> str:
        """Free pack-balance read -> a status/low-balance note, or "".

        Balance reads are free (POST /v1/recheck/pack-balance). Warns
        at <=25 credits and again at <=10. Best-effort: a failed read
        never fails the drift stage.
        """
        cfg = self.config
        balance_url = cfg.recheck_url.rstrip("/") + "/pack-balance"
        try:
            response = self.http.post(
                balance_url, json={"pack_id": cfg.recheck_pack_id}
            )
            if response.status_code != 200:
                return ""
            data = response.json()
        except Exception:  # noqa: BLE001 — advisory read only
            return ""
        if not isinstance(data, dict):
            return ""
        remaining = data.get("credits_remaining")
        total = data.get("credits_total", 250)
        if not isinstance(remaining, int):
            return ""
        note = f"paid with a recheck pack credit ({remaining} of {total} left)"
        if remaining <= 10:
            note += (
                " — PACK LOW: 10 or fewer credits remain; renewal is a "
                "new $1.00 pack (POST /v1/recheck/pack) — this guard "
                "never auto-buys and never falls back to per-call billing"
            )
        elif remaining <= 25:
            note += (
                " — pack low: 25 or fewer credits remain; renewal is a "
                "new $1.00 pack (POST /v1/recheck/pack)"
            )
        return note

    def _drift_result(self, resp: dict, cost: int, now: float, target_url: str) -> StageResult:
        cfg = self.config
        if cost:
            self.store.record_spend(cfg.recheck_url, cost, now=now)
        status = resp.get("status")
        fingerprint = resp.get("current")
        if not isinstance(fingerprint, dict):
            fingerprint = {}
        if status == "BASELINE":
            self.store.save_check(target_url, status, fingerprint, now=now)
            return StageResult(
                "drift", "pass",
                "Recheck: BASELINE — first sighting recorded; "
                "nothing to compare yet",
                cost_units=cost,
            )
        if status == "UNCHANGED":
            self.store.save_check(target_url, status, fingerprint, now=now)
            return StageResult(
                "drift", "pass",
                "Recheck: UNCHANGED since the previous check",
                cost_units=cost,
            )
        if status == "CHANGED":
            self.store.save_check(target_url, status, fingerprint, now=now)
            blocks: list[str] = []
            warns: list[str] = []
            for change in resp.get("changes") or []:
                field_name = change.get("field")
                before, after = change.get("before"), change.get("after")
                if field_name in ("payTo", "asset"):
                    blocks.append(f"{field_name} changed ({before} -> {after})")
                elif field_name == "amount":
                    pct = _pct_change(before, after)
                    if pct is not None and pct > cfg.price_change_block_pct:
                        blocks.append(
                            f"price changed {pct:.1f}% ({before} -> {after}), "
                            f"over the {cfg.price_change_block_pct:g}% "
                            f"block threshold"
                        )
                    else:
                        suffix = f" ({pct:.1f}%)" if pct is not None else ""
                        warns.append(f"price changed{suffix} ({before} -> {after})")
                else:
                    warns.append(f"{field_name} changed")
            detail = "Recheck: CHANGED"
            if blocks or warns:
                detail += " — " + "; ".join(blocks + warns)
            if blocks:
                return StageResult("drift", "block", detail, cost_units=cost)
            return StageResult("drift", "warn", detail, cost_units=cost)
        return self._failure(
            "drift", cfg.drift_failure,
            f"Recheck answered with an unrecognized status: {status!r}",
        )

    # ------------------------------------------------------------------
    # Stage 3: RouteVerdict gate
    # ------------------------------------------------------------------

    def _route_verdict_stage(
        self, target_url: str, amount_units: int | None
    ) -> StageResult:
        cfg = self.config
        if not cfg.route_verdict_enabled:
            return StageResult("route_verdict", "skipped", "disabled in config")
        if amount_units is None:
            return StageResult(
                "route_verdict", "skipped", "payment amount not supplied"
            )
        if amount_units < cfg.route_verdict_threshold_units:
            return StageResult(
                "route_verdict",
                "skipped",
                f"payment {format_usd(amount_units)} is below the "
                f"{format_usd(cfg.route_verdict_threshold_units)} gate threshold",
            )
        now = self._now()
        cached = self.store.get_route_verdict(target_url)
        if cached is not None and now - cached["checked_at"] < cfg.drift_ttl_seconds:
            age = now - cached["checked_at"]
            return self._rv_evaluate(
                cached["verdict"], cached["score"],
                source=f"cached RouteVerdict verdict {age:.0f}s old",
            )
        if self.account is None:
            return StageResult(
                "route_verdict", "skipped",
                "no fresh cached verdict; dry-run never pays for checks",
            )
        try:
            outcome = call_paid_endpoint(
                self.http,
                cfg.route_verdict_url,
                {"resource_url": target_url},
                account=self.account,
                budget=self.budget,
            )
        except ServiceError as exc:
            return self._failure("route_verdict", cfg.route_verdict_failure,
                                 f"RouteVerdict call failed: {exc}")
        if outcome.mode == "blocked":
            return StageResult(
                "route_verdict", "block",
                f"RouteVerdict check refused by the spend budget: {outcome.note}",
            )
        resp = outcome.response
        if (
            outcome.mode != "paid"
            or not isinstance(resp, dict)
            or "verdict" not in resp
        ):
            return self._failure(
                "route_verdict", cfg.route_verdict_failure,
                "RouteVerdict returned no usable verdict",
            )
        cost = outcome.price_units
        if cost:
            self.store.record_spend(cfg.route_verdict_url, cost, now=now)
        try:
            score = float(resp.get("score", 0))
        except (TypeError, ValueError):
            return self._failure(
                "route_verdict", cfg.route_verdict_failure,
                f"RouteVerdict score was not numeric: {resp.get('score')!r}",
            )
        verdict = str(resp["verdict"])
        self.store.save_route_verdict(target_url, verdict, score, now=now)
        return self._rv_evaluate(
            verdict, score, source="fresh RouteVerdict check", cost_units=cost
        )

    def _rv_evaluate(
        self, verdict: str, score: float, *, source: str, cost_units: int = 0
    ) -> StageResult:
        cfg = self.config
        if verdict == "TRUSTED" and score >= cfg.route_verdict_min_trust:
            return StageResult(
                "route_verdict", "pass",
                f"{source}: TRUSTED {score:g} >= {cfg.route_verdict_min_trust:g} "
                f"required",
                cost_units=cost_units,
            )
        return StageResult(
            "route_verdict", "block",
            f"{source}: verdict {verdict} trust {score:g} — gate requires "
            f"TRUSTED >= {cfg.route_verdict_min_trust:g}",
            cost_units=cost_units,
        )

    # ------------------------------------------------------------------
    # Stage 4: caps
    # ------------------------------------------------------------------

    def _caps_stage(self, amount_units: int | None, spend_url: str) -> StageResult:
        cfg = self.config
        if amount_units is None:
            return StageResult("caps", "skipped", "payment amount not supplied")
        now = self._now()
        problems: list[str] = []
        configured = False
        if cfg.max_payment_units is not None:
            configured = True
            if amount_units > cfg.max_payment_units:
                problems.append(
                    f"payment {format_usd(amount_units)} exceeds the per-payment "
                    f"cap {format_usd(cfg.max_payment_units)}"
                )
        if cfg.per_endpoint_daily_cap_units is not None:
            configured = True
            spent = self.store.spent_today(now=now, url=spend_url)
            if spent + amount_units > cfg.per_endpoint_daily_cap_units:
                problems.append(
                    f"per-endpoint daily cap: {format_usd(spent)} already spent "
                    f"today on {spend_url}; paying {format_usd(amount_units)} "
                    f"would exceed {format_usd(cfg.per_endpoint_daily_cap_units)}"
                )
        if cfg.global_daily_cap_units is not None:
            configured = True
            spent = self.store.spent_today(now=now)
            if spent + amount_units > cfg.global_daily_cap_units:
                problems.append(
                    f"global daily cap: {format_usd(spent)} already spent today; "
                    f"paying {format_usd(amount_units)} would exceed "
                    f"{format_usd(cfg.global_daily_cap_units)}"
                )
        if problems:
            return StageResult("caps", "block", "; ".join(problems))
        if configured:
            return StageResult("caps", "pass", "within the configured caps")
        return StageResult("caps", "skipped", "no caps configured")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _failure(stage: str, policy: str, detail: str) -> StageResult:
        if policy == "block":
            return StageResult(stage, "block", detail + " (failure policy: block)")
        return StageResult(stage, "warn", detail + " (failure policy: warn)")

    def _write_audit(
        self, decision: GuardDecision, *, outcome_mode: str, dry_run: bool
    ) -> None:
        entry = decision.to_dict()
        entry.update(
            {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outcome": outcome_mode,
                "dry_run": dry_run,
                "amount_usd": (
                    decision.amount_units / 1_000_000
                    if decision.amount_units is not None
                    else None
                ),
            }
        )
        self.audit.write(entry)


def _blocked(stages: list[StageResult]) -> bool:
    return any(s.outcome == "block" for s in stages)


def _pct_change(before, after) -> float | None:
    """Percent change between two USDC-unit amounts (strings or ints)."""
    try:
        b = float(before)
        a = float(after)
    except (TypeError, ValueError):
        return None
    if b == 0:
        return None
    return abs(a - b) / b * 100.0
