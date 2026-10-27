/**
 * Pre-Trade Guard stage (stage 4, opt-in) — fully offline, mirroring
 * the Python package's test_pre_trade.py.
 *
 * One fake fetch plays the world; the StallRow host routes by path
 * (/v1/recheck for drift, /v1/pre-trade-guard for this stage). Every
 * paid fake answers 402 until a payment-signature header arrives,
 * then returns its scripted payload and counts the paid call.
 */

import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ClientEvmSigner } from "@x402/evm";
import { describe, expect, it } from "vitest";
import { X402Guard } from "./client";
import { configFromEnv, resolveConfig } from "./config";
import { GuardBlockedError } from "./errors";
import { JsonStore } from "./store";

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F";
const TARGET = "https://api.example.com/v1/thing";
const SIGNATURE = `0x${"11".repeat(65)}` as const;

const signer = {
    address: "0x1111111111111111111111111111111111111111",
    signTypedData: async () => SIGNATURE,
} as unknown as ClientEvmSigner;

function challengeB64(url: string, amount: string): string {
    const payload = {
        x402Version: 2,
        error: "Payment required",
        resource: { url, description: "thing", mimeType: "application/json" },
        accepts: [
            {
                scheme: "exact",
                network: "eip155:8453",
                amount,
                asset: USDC,
                payTo: PAY_TO,
                maxTimeoutSeconds: 300,
                extra: { name: "USD Coin", version: "2" },
            },
        ],
        extensions: {},
    };
    return Buffer.from(JSON.stringify(payload)).toString("base64");
}

function fingerprint(payTo: string = PAY_TO) {
    return {
        status_code: 402,
        amount: "200000",
        asset: USDC,
        network: "eip155:8453",
        payTo,
        description_hash: "d".repeat(64),
        schema_hash: "s".repeat(64),
    };
}

function recheckBaseline() {
    return {
        watch_id: "w1",
        url: TARGET,
        kind: "x402",
        status: "BASELINE",
        changes: [],
        current: fingerprint(),
        checked_at: "2026-10-27T00:00:00Z",
        summary: "status BASELINE",
    };
}

function pretradePayload(verdict: string, risk = 0, reasons: string[] = []) {
    return {
        pay_to: PAY_TO,
        verdict,
        risk_score: risk,
        reasons,
        index_history: { status: "ok" },
        wallet_snapshot: { status: "ok" },
        sanctions: { status: "no_match" },
        limits: "test",
    };
}

class FakePaid {
    paidCalls = 0;
    bodies: Record<string, unknown>[] = [];
    constructor(
        private readonly url: string,
        private readonly amount: string,
        private readonly payloads: unknown[],
        private readonly fail = false,
    ) {}

    async handle(req: Request): Promise<Response> {
        if (req.headers.get("payment-signature")) {
            this.paidCalls += 1;
            try {
                this.bodies.push(JSON.parse(await req.text()));
            } catch {
                this.bodies.push({});
            }
            if (this.fail) return new Response("upstream exploded", { status: 500 });
            const payload =
                this.payloads[Math.min(this.paidCalls - 1, this.payloads.length - 1)];
            const receipt = Buffer.from(
                JSON.stringify({ success: true, transaction: "0xfeed" }),
            ).toString("base64");
            return Response.json(payload, {
                headers: { "payment-response": receipt },
            });
        }
        return new Response("{}", {
            status: 402,
            headers: { "payment-required": challengeB64(this.url, this.amount) },
        });
    }
}

interface WorldOptions {
    pretradePayloads?: unknown[];
    pretradeFail?: boolean;
    targetAmount?: string;
}

function makeWorld(opts: WorldOptions = {}) {
    const target = new FakePaid(TARGET, opts.targetAmount ?? "200000", [{ ok: true }]);
    const recheck = new FakePaid(
        "https://stall-row-production.up.railway.app/v1/recheck",
        "5000",
        [recheckBaseline()],
    );
    const pretrade = new FakePaid(
        "https://stall-row-production.up.railway.app/v1/pre-trade-guard",
        "5000",
        opts.pretradePayloads ?? [pretradePayload("PROCEED", 0)],
        opts.pretradeFail ?? false,
    );
    const routeVerdict = new FakePaid(
        "https://route-verdict-production.up.railway.app/v1/check",
        "50000",
        [{ verdict: "TRUSTED", score: 90 }],
    );
    const fetchImpl = (async (input: unknown, init?: RequestInit) => {
        const req = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(req.url);
        if (url.host === "stall-row-production.up.railway.app") {
            return url.pathname === "/v1/pre-trade-guard"
                ? pretrade.handle(req)
                : recheck.handle(req);
        }
        if (url.host === "route-verdict-production.up.railway.app") {
            return routeVerdict.handle(req);
        }
        if (url.host === "api.example.com") return target.handle(req);
        throw new Error(`unexpected host: ${url.host}`);
    }) as unknown as typeof fetch;
    return { fetchImpl, target, recheck, pretrade, routeVerdict };
}

class Clock {
    nowMs = 1_700_000_000_000;
    readonly now = () => this.nowMs;
}

function makeGuard(
    world: ReturnType<typeof makeWorld>,
    opts: {
        config?: Record<string, unknown>;
        clock?: Clock;
        signer?: unknown;
        store?: JsonStore;
    } = {},
) {
    const dir = mkdtempSync(join(tmpdir(), "x402-guard-pt-"));
    const clock = opts.clock ?? new Clock();
    const guard = new X402Guard({
        signer: (opts.signer === undefined ? signer : opts.signer) as ClientEvmSigner,
        fetchImpl: world.fetchImpl,
        now: clock.now,
        store: opts.store,
        config: {
            settledEnabled: false,
            preTradeEnabled: true,
            storePath: join(dir, "guard.json"),
            auditLogPath: join(dir, "audit.jsonl"),
            ...(opts.config ?? {}),
        },
    });
    return { guard, dir, clock };
}

const post = (body: unknown = { x: 1 }): RequestInit => ({
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
});

function stageOf(decision: { stages: { name: string; outcome: string; detail: string; costUnits: number }[] }, name: string) {
    const s = decision.stages.find((x) => x.name === name);
    if (!s) throw new Error(`no stage ${name}`);
    return s;
}

describe("pre-trade stage", () => {
    it("is disabled by default", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world, { config: { preTradeEnabled: false } });
        const res = await guard.fetch(TARGET, post());
        expect(res.status).toBe(200);
        expect(world.pretrade.paidCalls).toBe(0);
    });

    it("runs after route_verdict and before caps", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world);
        const decision = await guard.evaluate(TARGET, 200_000);
        expect(decision.stages.map((s) => s.name)).toEqual([
            "settled",
            "drift",
            "route_verdict",
            "pre_trade",
            "caps",
        ]);
    });

    it("PROCEED passes, costs one $0.005 check, and asks about the challenge payTo", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world);
        const res = await guard.fetch(TARGET, post());
        expect(res.status).toBe(200);
        expect(world.pretrade.paidCalls).toBe(1);
        expect(world.pretrade.bodies[0].pay_to).toBe(PAY_TO);
        expect(world.pretrade.bodies[0].amount).toBe("200000");
        expect(world.pretrade.bodies[0].resource).toBe(TARGET);
    });

    it("ABORT blocks before the target is signed for", async () => {
        const world = makeWorld({
            pretradePayloads: [
                pretradePayload("ABORT", 100, ["payTo is an exact match on the OFAC SDN snapshot"]),
            ],
        });
        const { guard } = makeGuard(world);
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(
            GuardBlockedError,
        );
        expect(world.target.paidCalls).toBe(0);
        expect(world.pretrade.paidCalls).toBe(1);
    });

    it("CAUTION warns-and-logs by default", async () => {
        const world = makeWorld({
            pretradePayloads: [pretradePayload("CAUTION", 40, ["payTo rotated recently"])],
        });
        const { guard } = makeGuard(world);
        const res = await guard.fetch(TARGET, post());
        expect(res.status).toBe(200);
        expect(world.target.paidCalls).toBe(1);
    });

    it("CAUTION blocks when configured", async () => {
        const world = makeWorld({
            pretradePayloads: [pretradePayload("CAUTION", 40, ["payTo rotated recently"])],
        });
        const { guard } = makeGuard(world, { config: { preTradeCaution: "block" } });
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(
            GuardBlockedError,
        );
        expect(world.target.paidCalls).toBe(0);
    });

    it("skips below the threshold without spending", async () => {
        const world = makeWorld({ targetAmount: "20000" });
        const { guard } = makeGuard(world);
        const res = await guard.fetch(TARGET, post());
        expect(res.status).toBe(200);
        expect(world.pretrade.paidCalls).toBe(0);
    });

    it("observe mode never pays for the screen", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world, { signer: null });
        const res = await guard.fetch(TARGET, post());
        expect(res.status).toBe(402); // handed back unpaid
        expect(world.pretrade.paidCalls).toBe(0);
    });

    it("failure policy warn passes through, block refuses", async () => {
        const warnWorld = makeWorld({ pretradeFail: true });
        const { guard: warnGuard } = makeGuard(warnWorld);
        const res = await warnGuard.fetch(TARGET, post());
        expect(res.status).toBe(200);

        const blockWorld = makeWorld({ pretradeFail: true });
        const { guard: blockGuard } = makeGuard(blockWorld, {
            config: { preTradeFailure: "block" },
        });
        await expect(blockGuard.fetch(TARGET, post())).rejects.toBeInstanceOf(
            GuardBlockedError,
        );
        expect(blockWorld.target.paidCalls).toBe(0);
    });

    it("evaluate() takes the payTo from the cached drift fingerprint", async () => {
        const world = makeWorld({
            pretradePayloads: [pretradePayload("ABORT", 95, ["sanctions match"])],
        });
        const clock = new Clock();
        const dir = mkdtempSync(join(tmpdir(), "x402-guard-pt-store-"));
        const store = new JsonStore(join(dir, "guard.json"));
        store.saveCheck(TARGET, "BASELINE", fingerprint(), clock.nowMs);
        const { guard } = makeGuard(world, { clock, store });
        const decision = await guard.evaluate(TARGET, 200_000);
        expect(stageOf(decision, "pre_trade").outcome).toBe("block");
        expect(world.pretrade.paidCalls).toBe(1);
        expect(world.pretrade.bodies[0].pay_to).toBe(PAY_TO);
    });

    it("evaluate() without any payTo skips the stage", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world);
        const decision = await guard.evaluate(TARGET, 200_000);
        const s = stageOf(decision, "pre_trade");
        expect(s.outcome).toBe("skipped");
        expect(s.detail).toContain("no payTo known");
        expect(world.pretrade.paidCalls).toBe(0);
    });
});

describe("pre-trade config", () => {
    it("reads env and defaults", () => {
        const cfg = resolveConfig(
            configFromEnv({
                X402_GUARD_PRE_TRADE_ENABLED: "1",
                X402_GUARD_PRE_TRADE_THRESHOLD_USD: "0.25",
                X402_GUARD_PRE_TRADE_CAUTION: "block",
                X402_GUARD_PRE_TRADE_FAILURE: "block",
            }),
        );
        expect(cfg.preTradeEnabled).toBe(true);
        expect(cfg.preTradeThresholdUnits).toBe(250_000);
        expect(cfg.preTradeCaution).toBe("block");
        expect(cfg.preTradeFailure).toBe("block");
        const defaults = resolveConfig({});
        expect(defaults.preTradeEnabled).toBe(false);
        expect(defaults.preTradeThresholdUnits).toBe(100_000);
    });

    it("rejects bad policy words", () => {
        expect(() => resolveConfig({ preTradeCaution: "shrug" as never })).toThrow();
        expect(() => resolveConfig({ preTradeFailure: "shrug" as never })).toThrow();
    });
});
