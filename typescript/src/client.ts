/**
 * X402Guard: every payment pre-flighted, before anything is signed.
 *
 * The pipeline (same semantics as the Python package):
 *
 *   1. Settled pre-flight (FREE) — the inspector's delivery record.
 *      Hard veto ONLY on a failed delivery or a not-payable verdict;
 *      a verified delivery corroborates; nothing_to_compare is
 *      noted, never a veto; Settled down or silent = pass-through.
 *   2. Drift check (StallRow Recheck, $0.005, cached) — payTo or
 *      asset changed = hard block; price moved more than the
 *      configured % (default 20%) = block; schema/description/other
 *      changes = warn. Inside the TTL (default 24h) the cached
 *      result is used and the stage costs zero. With `recheckPackId`
 *      configured, the check instead spends one credit from a prepaid
 *      StallRow recheck pack ($1.00 = 250 checks on one endpoint):
 *      no per-check settlement, and an exhausted or expired pack
 *      fails the stage — it never falls back to paying per call.
 *   3. RouteVerdict gate (optional, $0.05, cached) — a first-ever
 *      payment at or above the threshold (default $0.10) requires
 *      TRUSTED >= the trust floor (default 80).
 *   4. Spend caps — per-payment, per-endpoint-daily, global-daily,
 *      checked against the local spend ledger BEFORE signing.
 *
 * A block short-circuits the remaining stages (recorded as skipped)
 * — the guard never spends check money after a veto. Every decision
 * is written to the audit log, allowed or not.
 *
 * Modes: with a signer, `fetch` pays through @x402/fetch and a
 * blocked payment throws GuardBlockedError before signing. Without
 * a signer the guard is an observer: the pipeline runs what it can
 * for free, paid stages are noted as skipped, and the raw 402
 * response is returned unpaid.
 */

import { ExactEvmScheme, type ClientEvmSigner } from "@x402/evm";
import { wrapFetchWithPaymentFromConfig, type PaymentPolicy } from "@x402/fetch";
import { AuditLog } from "./audit";
import {
    resolveConfig,
    type GuardConfig,
    type GuardConfigOptions,
} from "./config";
import { GuardBlockedError } from "./errors";
import {
    fetchSettledReport,
    settledDescribe,
    settledHardNegative,
    type SettledReport,
} from "./settled";
import { JsonStore } from "./store";
import { BASE_NETWORK, formatUsd, parsePaymentRequired, selectRequirement } from "./x402";

export interface StageResult {
    name: string; // settled | drift | route_verdict | caps
    outcome: "pass" | "warn" | "block" | "skipped";
    detail: string;
    costUnits: number;
}

export interface GuardDecision {
    /** The endpoint being vetted (the payment's target). */
    url: string;
    /** The URL actually being paid (== url for fetch()). */
    payUrl: string;
    amountUnits: number | null;
    stages: StageResult[];
    settledReport: SettledReport | null;
    checkCostUnits: number;
    blocked: boolean;
    reasons: string[];
    warnings: string[];
    settledVeto: string | null;
    label: "allow" | "warn" | "block";
}

export function decisionSummary(decision: GuardDecision): string {
    const bits = decision.stages.map((s) => `${s.name} ${s.outcome}`).join(", ");
    let line = `guard ${decision.label} (${bits})`;
    if (decision.checkCostUnits > 0) {
        line += `; checks cost ${formatUsd(decision.checkCostUnits)}`;
    }
    for (const reason of decision.reasons) line += `; BLOCKED: ${reason}`;
    return line;
}

export interface X402GuardOptions {
    /** The buyer's x402 signer (@x402/evm ClientEvmSigner). Omit for
     * observe/dry-run mode: the guard then never pays. */
    signer?: ClientEvmSigner;
    config?: GuardConfigOptions;
    /** fetch implementation used for EVERY request (inject a fake in
     * tests). Defaults to the global fetch. */
    fetchImpl?: typeof fetch;
    store?: JsonStore;
    audit?: AuditLog;
    /** Clock injection for tests (epoch milliseconds). */
    now?: () => number;
}

type FetchInput = string | URL | Request;

export class X402Guard {
    private readonly config: GuardConfig;
    private readonly signer?: ClientEvmSigner;
    private readonly rawFetch: typeof fetch;
    private readonly payingFetch: typeof fetch | null;
    private readonly store: JsonStore;
    private readonly audit: AuditLog;
    private readonly nowFn: () => number;

    constructor(options: X402GuardOptions = {}) {
        this.config = resolveConfig(options.config ?? {});
        this.signer = options.signer;
        this.rawFetch = options.fetchImpl ?? globalThis.fetch;
        this.nowFn = options.now ?? (() => Date.now());
        this.store = options.store ?? new JsonStore(this.config.storePath);
        this.audit =
            options.audit ??
            new AuditLog(
                this.config.auditLogPath ??
                    this.store.path.replace(/[^/]+$/, "audit.jsonl"),
            );
        if (this.signer) {
            const policies: PaymentPolicy[] = [];
            if (this.config.maxPaymentUnits !== null) {
                const cap = BigInt(this.config.maxPaymentUnits);
                policies.push((_version, requirements) =>
                    requirements.filter((r) => BigInt(r.amount) <= cap),
                );
            }
            this.payingFetch = wrapFetchWithPaymentFromConfig(this.rawFetch, {
                schemes: [
                    {
                        network: BASE_NETWORK,
                        client: new ExactEvmScheme(this.signer),
                    },
                ],
                policies,
            });
        } else {
            this.payingFetch = null;
        }
    }

    /** Run the policy pipeline for a target without paying it. */
    async evaluate(targetUrl: string, amountUnits?: number): Promise<GuardDecision> {
        const decision = await this.runPipeline(targetUrl, amountUnits ?? null, targetUrl);
        this.writeAudit(decision, "evaluated");
        return decision;
    }

    /**
     * A fetch with the seatbelt on: probes the target unpaid, runs
     * the pipeline, and only then lets @x402/fetch sign and pay.
     * Throws GuardBlockedError on a block (signer mode); in observe
     * mode returns the raw 402 response unpaid.
     */
    async fetch(input: FetchInput, init?: RequestInit): Promise<Response> {
        const url = input instanceof Request ? input.url : String(input);
        const probeInput = input instanceof Request ? input.clone() : input;
        const probe = await this.rawFetch(probeInput as string, init);
        if (probe.status !== 402) return probe;
        let amountUnits: number;
        try {
            const challenge = await parsePaymentRequired(probe);
            amountUnits = Number(selectRequirement(challenge).amount);
        } catch {
            // A 402 we cannot parse cannot be vetted; hand it back raw
            // rather than guessing at someone else's money.
            return probe;
        }
        const decision = await this.runPipeline(url, amountUnits, url);
        if (decision.blocked) {
            this.writeAudit(decision, "blocked");
            if (this.signer) throw new GuardBlockedError(decision);
            return probe;
        }
        if (!this.signer || !this.payingFetch) {
            this.writeAudit(decision, "dry_run");
            return probe;
        }
        const response = await this.payingFetch(input as string, init);
        if (response.status === 200) {
            this.store.recordSpend(url, amountUnits, this.nowFn());
            this.writeAudit(decision, "paid");
        } else {
            this.writeAudit(decision, `http_${response.status}`);
        }
        return response;
    }

    // ------------------------------------------------------------------
    // The pipeline
    // ------------------------------------------------------------------

    private async runPipeline(
        targetUrl: string,
        amountUnits: number | null,
        spendUrl: string,
    ): Promise<GuardDecision> {
        const cfg = this.config;
        const stages: StageResult[] = [];
        let settledReport: SettledReport | null = null;

        // 1. Settled pre-flight (free)
        if (cfg.settledEnabled) {
            settledReport = await fetchSettledReport(this.rawFetch, targetUrl, {
                endpoint: cfg.settledMcpUrl,
                timeoutMs: cfg.settledTimeoutMs,
            });
            const veto = settledHardNegative(settledReport);
            stages.push(
                veto
                    ? stage("settled", "block", veto)
                    : stage("settled", "pass", settledDescribe(settledReport)),
            );
        } else {
            stages.push(stage("settled", "skipped", "disabled in config"));
        }

        // 2. Drift check (Recheck, cached)
        stages.push(
            isBlocked(stages)
                ? stage("drift", "skipped", "not run — an earlier stage blocked")
                : await this.driftStage(targetUrl),
        );

        // 3. RouteVerdict gate
        stages.push(
            isBlocked(stages)
                ? stage("route_verdict", "skipped", "not run — an earlier stage blocked")
                : await this.routeVerdictStage(targetUrl, amountUnits),
        );

        // 4. Spend caps
        stages.push(
            isBlocked(stages)
                ? stage("caps", "skipped", "not run — an earlier stage blocked")
                : this.capsStage(amountUnits, spendUrl),
        );

        const reasons = stages.filter((s) => s.outcome === "block").map((s) => s.detail);
        const warnings = stages.filter((s) => s.outcome === "warn").map((s) => s.detail);
        return {
            url: targetUrl,
            payUrl: spendUrl,
            amountUnits,
            stages,
            settledReport,
            checkCostUnits: stages.reduce((sum, s) => sum + s.costUnits, 0),
            blocked: reasons.length > 0,
            reasons,
            warnings,
            settledVeto: settledReport ? settledHardNegative(settledReport) : null,
            label: reasons.length > 0 ? "block" : warnings.length > 0 ? "warn" : "allow",
        };
    }

    // ------------------------------------------------------------------
    // Stage 2: drift
    // ------------------------------------------------------------------

    private async driftStage(targetUrl: string): Promise<StageResult> {
        const cfg = this.config;
        if (!cfg.driftEnabled) return stage("drift", "skipped", "disabled in config");
        const now = this.nowFn();
        const ttlMs = cfg.driftTtlSeconds * 1000;
        const cached = this.store.getCheck(targetUrl);
        if (cached && now - cached.checkedAt < ttlMs) {
            const age = Math.floor((now - cached.checkedAt) / 1000);
            return stage(
                "drift",
                "pass",
                `cached Recheck result ${age}s old (TTL ${cfg.driftTtlSeconds}s), ` +
                    `status ${cached.status} — no new Recheck call`,
            );
        }
        if (!this.signer) {
            const note = cached ? "cached Recheck result is stale" : "no cached Recheck result";
            return stage("drift", "skipped", `${note}; no signer — observe mode never pays for checks`);
        }
        if (cfg.recheckPackId) {
            // Pack path: the pack id is the credential — StallRow
            // spends one credit, no settlement occurs, cost is 0.
            // An exhausted/expired pack fails the stage; the guard
            // NEVER falls back to per-call payment silently.
            let payload: Record<string, unknown>;
            try {
                payload = await this.packRecheckCall(targetUrl);
            } catch (exc) {
                return this.failure(
                    "drift",
                    cfg.driftFailure,
                    `Recheck pack spend failed: ${errMsg(exc)} — no per-call fallback was attempted`,
                );
            }
            const result = this.driftResult(payload, 0, now, targetUrl);
            const note = await this.packBalanceNote();
            return note
                ? { ...result, detail: `${result.detail}; ${note}` }
                : result;
        }
        let payload: Record<string, unknown>;
        let cost: number;
        try {
            ({ payload, priceUnits: cost } = await this.paidCheckCall(cfg.recheckUrl, {
                url: targetUrl,
            }));
        } catch (exc) {
            return this.failure("drift", cfg.driftFailure, `Recheck call failed: ${errMsg(exc)}`);
        }
        return this.driftResult(payload, cost, now, targetUrl);
    }

    /** One pack-credit Recheck: POST with pack_id, no payment. Throws
     * on any non-200 (402 exhausted, 410 expired, 403 wrong endpoint,
     * 404 unknown pack) — the caller applies the failure policy. */
    private async packRecheckCall(
        targetUrl: string,
    ): Promise<Record<string, unknown>> {
        const cfg = this.config;
        const res = await this.rawFetch(cfg.recheckUrl, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ url: targetUrl, pack_id: cfg.recheckPackId }),
        });
        if (res.status !== 200) {
            throw new Error(`pack spend failed: HTTP ${res.status}`);
        }
        return (await res.json()) as Record<string, unknown>;
    }

    /** Free pack-balance read -> a status/low-balance note, or "".
     * Warns at <=25 credits and again at <=10. Best-effort: a failed
     * read never fails the drift stage. */
    private async packBalanceNote(): Promise<string> {
        const cfg = this.config;
        try {
            const res = await this.rawFetch(`${cfg.recheckUrl}/pack-balance`, {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ pack_id: cfg.recheckPackId }),
            });
            if (res.status !== 200) return "";
            const data = (await res.json()) as Record<string, unknown>;
            const remaining = data.credits_remaining;
            const total = data.credits_total ?? 250;
            if (typeof remaining !== "number") return "";
            let note = `paid with a recheck pack credit (${remaining} of ${total} left)`;
            if (remaining <= 10) {
                note +=
                    " — PACK LOW: 10 or fewer credits remain; renewal is a new $1.00 pack (POST /v1/recheck/pack) — this guard never auto-buys and never falls back to per-call billing";
            } else if (remaining <= 25) {
                note +=
                    " — pack low: 25 or fewer credits remain; renewal is a new $1.00 pack (POST /v1/recheck/pack)";
            }
            return note;
        } catch {
            return "";
        }
    }

    private driftResult(
        payload: Record<string, unknown>,
        cost: number,
        now: number,
        targetUrl: string,
    ): StageResult {
        const cfg = this.config;
        if (cost > 0) this.store.recordSpend(cfg.recheckUrl, cost, now);
        const status = payload.status as string | undefined;
        const fingerprint =
            payload.current && typeof payload.current === "object"
                ? (payload.current as Record<string, unknown>)
                : {};
        if (status === "BASELINE") {
            this.store.saveCheck(targetUrl, status, fingerprint, now);
            return stage(
                "drift",
                "pass",
                "Recheck: BASELINE — first sighting recorded; nothing to compare yet",
                cost,
            );
        }
        if (status === "UNCHANGED") {
            this.store.saveCheck(targetUrl, status, fingerprint, now);
            return stage("drift", "pass", "Recheck: UNCHANGED since the previous check", cost);
        }
        if (status === "CHANGED") {
            this.store.saveCheck(targetUrl, status, fingerprint, now);
            const blocks: string[] = [];
            const warns: string[] = [];
            for (const change of (payload.changes as Array<Record<string, unknown>>) ?? []) {
                const field = String(change.field);
                const before = change.before;
                const after = change.after;
                if (field === "payTo" || field === "asset") {
                    blocks.push(`${field} changed (${String(before)} -> ${String(after)})`);
                } else if (field === "amount") {
                    const pct = pctChange(before, after);
                    if (pct !== null && pct > cfg.priceChangeBlockPct) {
                        blocks.push(
                            `price changed ${pct.toFixed(1)}% (${String(before)} -> ${String(after)}), ` +
                                `over the ${cfg.priceChangeBlockPct}% block threshold`,
                        );
                    } else {
                        warns.push(
                            `price changed${pct !== null ? ` (${pct.toFixed(1)}%)` : ""} ` +
                                `(${String(before)} -> ${String(after)})`,
                        );
                    }
                } else {
                    warns.push(`${field} changed`);
                }
            }
            let detail = "Recheck: CHANGED";
            if (blocks.length + warns.length > 0) {
                detail += " — " + [...blocks, ...warns].join("; ");
            }
            return blocks.length > 0
                ? stage("drift", "block", detail, cost)
                : stage("drift", "warn", detail, cost);
        }
        return this.failure(
            "drift",
            cfg.driftFailure,
            `Recheck answered with an unrecognized status: ${String(status)}`,
        );
    }

    // ------------------------------------------------------------------
    // Stage 3: RouteVerdict gate
    // ------------------------------------------------------------------

    private async routeVerdictStage(
        targetUrl: string,
        amountUnits: number | null,
    ): Promise<StageResult> {
        const cfg = this.config;
        if (!cfg.routeVerdictEnabled) {
            return stage("route_verdict", "skipped", "disabled in config");
        }
        if (amountUnits === null) {
            return stage("route_verdict", "skipped", "payment amount not supplied");
        }
        if (amountUnits < cfg.routeVerdictThresholdUnits) {
            return stage(
                "route_verdict",
                "skipped",
                `payment ${formatUsd(amountUnits)} is below the ` +
                    `${formatUsd(cfg.routeVerdictThresholdUnits)} gate threshold`,
            );
        }
        const now = this.nowFn();
        const cached = this.store.getRouteVerdict(targetUrl);
        if (cached && now - cached.checkedAt < cfg.driftTtlSeconds * 1000) {
            const age = Math.floor((now - cached.checkedAt) / 1000);
            return this.rvEvaluate(
                cached.verdict,
                cached.score,
                `cached RouteVerdict verdict ${age}s old`,
            );
        }
        if (!this.signer) {
            return stage(
                "route_verdict",
                "skipped",
                "no fresh cached verdict; no signer — observe mode never pays for checks",
            );
        }
        let payload: Record<string, unknown>;
        let cost: number;
        try {
            ({ payload, priceUnits: cost } = await this.paidCheckCall(cfg.routeVerdictUrl, {
                resource_url: targetUrl,
            }));
        } catch (exc) {
            return this.failure(
                "route_verdict",
                cfg.routeVerdictFailure,
                `RouteVerdict call failed: ${errMsg(exc)}`,
            );
        }
        if (typeof payload.verdict !== "string") {
            return this.failure(
                "route_verdict",
                cfg.routeVerdictFailure,
                "RouteVerdict returned no usable verdict",
            );
        }
        if (cost > 0) this.store.recordSpend(cfg.routeVerdictUrl, cost, now);
        const verdict = payload.verdict;
        const score = Number(payload.score ?? 0);
        if (!Number.isFinite(score)) {
            return this.failure(
                "route_verdict",
                cfg.routeVerdictFailure,
                `RouteVerdict score was not numeric: ${String(payload.score)}`,
            );
        }
        this.store.saveRouteVerdict(targetUrl, verdict, score, now);
        return this.rvEvaluate(verdict, score, "fresh RouteVerdict check", cost);
    }

    private rvEvaluate(
        verdict: string,
        score: number,
        source: string,
        costUnits = 0,
    ): StageResult {
        const cfg = this.config;
        if (verdict === "TRUSTED" && score >= cfg.routeVerdictMinTrust) {
            return stage(
                "route_verdict",
                "pass",
                `${source}: TRUSTED ${score} >= ${cfg.routeVerdictMinTrust} required`,
                costUnits,
            );
        }
        return stage(
            "route_verdict",
            "block",
            `${source}: verdict ${verdict} trust ${score} — gate requires ` +
                `TRUSTED >= ${cfg.routeVerdictMinTrust}`,
            costUnits,
        );
    }

    // ------------------------------------------------------------------
    // Stage 4: caps
    // ------------------------------------------------------------------

    private capsStage(amountUnits: number | null, spendUrl: string): StageResult {
        const cfg = this.config;
        if (amountUnits === null) {
            return stage("caps", "skipped", "payment amount not supplied");
        }
        const now = this.nowFn();
        const problems: string[] = [];
        let configured = false;
        if (cfg.maxPaymentUnits !== null) {
            configured = true;
            if (amountUnits > cfg.maxPaymentUnits) {
                problems.push(
                    `payment ${formatUsd(amountUnits)} exceeds the per-payment cap ` +
                        formatUsd(cfg.maxPaymentUnits),
                );
            }
        }
        if (cfg.perEndpointDailyCapUnits !== null) {
            configured = true;
            const spent = this.store.spentToday(now, spendUrl);
            if (spent + amountUnits > cfg.perEndpointDailyCapUnits) {
                problems.push(
                    `per-endpoint daily cap: ${formatUsd(spent)} already spent today on ` +
                        `${spendUrl}; paying ${formatUsd(amountUnits)} would exceed ` +
                        formatUsd(cfg.perEndpointDailyCapUnits),
                );
            }
        }
        if (cfg.globalDailyCapUnits !== null) {
            configured = true;
            const spent = this.store.spentToday(now);
            if (spent + amountUnits > cfg.globalDailyCapUnits) {
                problems.push(
                    `global daily cap: ${formatUsd(spent)} already spent today; paying ` +
                        `${formatUsd(amountUnits)} would exceed ` +
                        formatUsd(cfg.globalDailyCapUnits),
                );
            }
        }
        if (problems.length > 0) return stage("caps", "block", problems.join("; "));
        if (configured) return stage("caps", "pass", "within the configured caps");
        return stage("caps", "skipped", "no caps configured");
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /** A paid call to one of the fleet's check services (Recheck /
     * RouteVerdict), through the same paying fetch as target payments. */
    private async paidCheckCall(
        url: string,
        body: Record<string, unknown>,
    ): Promise<{ payload: Record<string, unknown>; priceUnits: number }> {
        if (!this.payingFetch) throw new Error("guard check calls require a signer");
        const init: RequestInit = {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
        };
        const probe = await this.rawFetch(url, init);
        if (probe.status === 200) {
            return {
                payload: (await probe.json()) as Record<string, unknown>,
                priceUnits: 0,
            };
        }
        if (probe.status !== 402) {
            throw new Error(`check call to ${url} failed: HTTP ${probe.status}`);
        }
        const challenge = await parsePaymentRequired(probe);
        const priceUnits = Number(selectRequirement(challenge).amount);
        const paid = await this.payingFetch(url, init);
        if (paid.status !== 200) {
            throw new Error(`check call to ${url} failed after payment: HTTP ${paid.status}`);
        }
        return {
            payload: (await paid.json()) as Record<string, unknown>,
            priceUnits,
        };
    }

    private failure(
        name: string,
        policy: "warn" | "block",
        detail: string,
    ): StageResult {
        return policy === "block"
            ? stage(name, "block", `${detail} (failure policy: block)`)
            : stage(name, "warn", `${detail} (failure policy: warn)`);
    }

    private writeAudit(decision: GuardDecision, outcomeMode: string): void {
        this.audit.write({
            ts: new Date(this.nowFn()).toISOString(),
            targetUrl: decision.url,
            payUrl: decision.payUrl,
            amountUnits: decision.amountUnits,
            amountUsd:
                decision.amountUnits !== null ? decision.amountUnits / 1_000_000 : null,
            decision: decision.label,
            blocked: decision.blocked,
            dryRun: !this.signer,
            outcome: outcomeMode,
            reasons: decision.reasons,
            warnings: decision.warnings,
            checkCostUnits: decision.checkCostUnits,
            stages: decision.stages,
        });
    }
}

/** Build a fetch with the seatbelt on (the drop-in form). */
export function createGuardedFetch(options: X402GuardOptions = {}): typeof fetch {
    const guard = new X402Guard(options);
    return ((input: FetchInput, init?: RequestInit) =>
        guard.fetch(input, init)) as typeof fetch;
}

function stage(
    name: string,
    outcome: StageResult["outcome"],
    detail: string,
    costUnits = 0,
): StageResult {
    return { name, outcome, detail, costUnits };
}

function isBlocked(stages: StageResult[]): boolean {
    return stages.some((s) => s.outcome === "block");
}

function errMsg(exc: unknown): string {
    return exc instanceof Error ? exc.message : String(exc);
}

function pctChange(before: unknown, after: unknown): number | null {
    const b = Number(before);
    const a = Number(after);
    if (!Number.isFinite(b) || !Number.isFinite(a) || b === 0) return null;
    return (Math.abs(a - b) / b) * 100;
}
