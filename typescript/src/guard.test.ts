/**
 * X402Guard policy pipeline — fully offline.
 *
 * One fake fetch plays the whole world:
 *   api.example.com                          the target the buyer pays
 *   settled.tools                            Settled's free MCP endpoint
 *   stall-row-production.up.railway.app      StallRow /v1/recheck
 *   route-verdict-production.up.railway.app  RouteVerdict /v1/check
 *
 * Every paid fake answers 402 until a payment-signature header
 * arrives (that's @x402/fetch's paid retry), then returns its
 * scripted payload and counts the paid call — so tests can prove a
 * block happened BEFORE anything was signed.
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ClientEvmSigner } from "@x402/evm";
import { describe, expect, it } from "vitest";
import { X402Guard } from "./client";
import { GuardBlockedError } from "./errors";

const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const PAY_TO = "0x097f9685D1e0752bF8B02d61E5Ce9be7eA0FBA7F";
const PAY_TO_MOVED = "0x2222222222222222222222222222222222222222";
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

function fingerprint(amount = "20000", payTo: string = PAY_TO) {
    return {
        status_code: 402,
        amount,
        asset: USDC,
        network: "eip155:8453",
        payTo: payTo,
        description_hash: "d".repeat(64),
        schema_hash: "s".repeat(64),
    };
}

function recheckResponse(
    status: string,
    opts: { changes?: unknown[]; current?: Record<string, unknown> } = {},
) {
    return {
        watch_id: "w1",
        url: TARGET,
        kind: "x402",
        status,
        changes: opts.changes ?? [],
        current: opts.current ?? fingerprint(),
        checked_at: "2026-10-25T00:00:00Z",
        summary: `status ${status}`,
    };
}

// --- Settled payloads -------------------------------------------------------

function settledUnknown(url: string) {
    return {
        url,
        status: "unknown",
        payability: { verdict: "unknown" },
        delivery_verified: null,
        quality: { total: 10 },
    };
}

function settledFailedDelivery(url: string) {
    return {
        url,
        status: "live",
        payability: { verdict: "payable" },
        delivery_verified: false,
        quality: { total: 45 },
        delivery_receipt: {
            delivered: false,
            checks: { verdict: "fields_missing" },
            paid_usd: 0.02,
        },
    };
}

function settledNothingToCompare(url: string) {
    return {
        url,
        status: "live",
        payability: { verdict: "delivered" },
        delivery_verified: true,
        quality: { total: 82 },
        delivery_receipt: {
            delivered: true,
            checks: { verdict: "nothing_to_compare" },
            paid_usd: 0.02,
        },
    };
}

// --- the fake world -----------------------------------------------------------

class FakePaid {
    paidCalls = 0;

    constructor(
        private readonly url: string,
        private readonly amount: string,
        private readonly payloads: unknown[],
    ) {}

    handle(req: Request): Response {
        if (req.headers.get("payment-signature")) {
            this.paidCalls += 1;
            const payload = this.payloads[
                Math.min(this.paidCalls - 1, this.payloads.length - 1)
            ];
            const receipt = Buffer.from(
                JSON.stringify({ success: true, transaction: "0xfeed" }),
            ).toString("base64");
            return Response.json(payload, { headers: { "payment-response": receipt } });
        }
        return new Response("{}", {
            status: 402,
            headers: { "payment-required": challengeB64(this.url, this.amount) },
        });
    }
}

interface WorldOptions {
    settledFor?: (url: string) => unknown;
    settledDown?: boolean;
    recheckPayloads?: unknown[];
    rvPayloads?: unknown[];
}

function makeWorld(opts: WorldOptions = {}) {
    const target = new FakePaid(TARGET, "20000", [{ ok: true }]);
    const recheck = new FakePaid(
        "https://stall-row-production.up.railway.app/v1/recheck",
        "5000",
        opts.recheckPayloads ?? [recheckResponse("BASELINE")],
    );
    const routeVerdict = new FakePaid(
        "https://route-verdict-production.up.railway.app/v1/check",
        "50000",
        opts.rvPayloads ?? [{ verdict: "TRUSTED", score: 90 }],
    );
    const settledFor = opts.settledFor ?? settledUnknown;

    const settledFake = async (req: Request): Promise<Response> => {
        if (opts.settledDown) throw new TypeError("fetch failed");
        const body = JSON.parse(await req.text()) as {
            id?: number;
            method: string;
            params?: { arguments?: { url: string } };
        };
        if (body.method === "initialize") {
            return Response.json({
                jsonrpc: "2.0",
                id: body.id,
                result: { protocolVersion: "2025-06-18", capabilities: { tools: {} } },
            });
        }
        if (body.method === "notifications/initialized") {
            return new Response(null, { status: 202 });
        }
        if (body.method === "tools/call") {
            const payload = settledFor(body.params?.arguments?.url ?? "");
            return Response.json({
                jsonrpc: "2.0",
                id: body.id,
                result: {
                    content: [{ type: "text", text: JSON.stringify(payload) }],
                    structuredContent: payload,
                    isError: false,
                },
            });
        }
        throw new Error(`unexpected MCP method: ${body.method}`);
    };

    const fetchImpl = (async (input: unknown, init?: RequestInit) => {
        const req = input instanceof Request ? input : new Request(String(input), init);
        const host = new URL(req.url).host;
        if (host === "settled.tools") return settledFake(req);
        if (host === "stall-row-production.up.railway.app") return recheck.handle(req);
        if (host === "route-verdict-production.up.railway.app") {
            return routeVerdict.handle(req);
        }
        if (host === "api.example.com") return target.handle(req);
        throw new Error(`unexpected host: ${host}`);
    }) as unknown as typeof fetch;

    return { fetchImpl, target, recheck, routeVerdict };
}

class Clock {
    nowMs = 1_700_000_000_000;
    readonly now = () => this.nowMs;
    advance(seconds: number) {
        this.nowMs += seconds * 1000;
    }
}

function makeGuard(
    world: ReturnType<typeof makeWorld>,
    opts: { config?: Record<string, unknown>; clock?: Clock; signer?: unknown } = {},
) {
    const dir = mkdtempSync(join(tmpdir(), "x402-guard-test-"));
    const clock = opts.clock ?? new Clock();
    const guard = new X402Guard({
        signer: (opts.signer === undefined ? signer : opts.signer) as ClientEvmSigner,
        fetchImpl: world.fetchImpl,
        now: clock.now,
        config: {
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

// --- Settled stage -------------------------------------------------------------

describe("settled stage", () => {
    it("veto blocks before any signing", async () => {
        const world = makeWorld({ settledFor: settledFailedDelivery });
        const { guard } = makeGuard(world);
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(GuardBlockedError);
        expect(world.target.paidCalls).toBe(0);
        expect(world.recheck.paidCalls).toBe(0); // veto short-circuits paid stages
    });

    it("settled outage passes through", async () => {
        const world = makeWorld({ settledDown: true });
        const { guard } = makeGuard(world);
        const decision = await guard.evaluate(TARGET, 20_000);
        expect(decision.stages[0].outcome).toBe("pass");
        expect(decision.stages[0].detail).toContain("unavailable");
        const response = await guard.fetch(TARGET, post());
        expect(response.status).toBe(200);
        expect(world.target.paidCalls).toBe(1);
    });

    it("nothing_to_compare is noted, not a veto", async () => {
        const world = makeWorld({ settledFor: settledNothingToCompare });
        const { guard } = makeGuard(world);
        const decision = await guard.evaluate(TARGET, 20_000);
        expect(decision.blocked).toBe(false);
        expect(decision.stages[0].outcome).toBe("pass");
        expect(decision.stages[0].detail).toContain("nothing to compare");
        expect(decision.settledReport?.deliveryVerified).toBe(true);
    });
});

// --- drift stage -----------------------------------------------------------------

describe("drift stage", () => {
    it("records a baseline, then serves the TTL from cache", async () => {
        const clock = new Clock();
        const world = makeWorld();
        const { guard } = makeGuard(world, { clock });
        await guard.fetch(TARGET, post());
        expect(world.recheck.paidCalls).toBe(1);
        await guard.fetch(TARGET, post());
        clock.advance(3_600); // inside the 24h TTL
        const decision = await guard.evaluate(TARGET, 20_000);
        expect(world.recheck.paidCalls).toBe(1);
        expect(decision.stages[1].outcome).toBe("pass");
        expect(decision.stages[1].detail).toContain("cached");
        expect(decision.stages[1].costUnits).toBe(0);
        clock.advance(86_401); // past the TTL: checked again
        await guard.fetch(TARGET, post());
        expect(world.recheck.paidCalls).toBe(2);
    });

    it("payTo change after the TTL blocks the payment", async () => {
        const clock = new Clock();
        const world = makeWorld({
            recheckPayloads: [
                recheckResponse("BASELINE"),
                recheckResponse("CHANGED", {
                    changes: [{ field: "payTo", before: PAY_TO, after: PAY_TO_MOVED }],
                    current: fingerprint("20000", PAY_TO_MOVED),
                }),
            ],
        });
        const { guard } = makeGuard(world, { clock });
        await guard.fetch(TARGET, post());
        expect(world.target.paidCalls).toBe(1);
        clock.advance(86_401);
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(GuardBlockedError);
        expect(world.target.paidCalls).toBe(1);
    });

    it("price spike blocks; a small move only warns", async () => {
        const clock = new Clock();
        const small = makeWorld({
            recheckPayloads: [
                recheckResponse("BASELINE"),
                recheckResponse("CHANGED", {
                    changes: [{ field: "amount", before: "20000", after: "21000" }],
                    current: fingerprint("21000"),
                }),
            ],
        });
        const g1 = makeGuard(small, { clock });
        await g1.guard.fetch(TARGET, post());
        clock.advance(86_401);
        const decision = await g1.guard.evaluate(TARGET, 20_000);
        expect(decision.stages[1].outcome).toBe("warn");
        expect(decision.blocked).toBe(false);

        const clock2 = new Clock();
        const spike = makeWorld({
            recheckPayloads: [
                recheckResponse("BASELINE"),
                recheckResponse("CHANGED", {
                    changes: [{ field: "amount", before: "20000", after: "30000" }],
                    current: fingerprint("30000"),
                }),
            ],
        });
        const g2 = makeGuard(spike, { clock: clock2 });
        await g2.guard.fetch(TARGET, post());
        clock2.advance(86_401);
        const blocked = await g2.guard.evaluate(TARGET, 20_000);
        expect(blocked.stages[1].outcome).toBe("block");
        expect(blocked.stages[1].detail).toContain("50.0%");
    });
});

// --- RouteVerdict gate --------------------------------------------------------------

describe("route verdict gate", () => {
    const gateConfig = { routeVerdictThresholdUnits: 10_000 }; // $0.01 < $0.02 target

    it("blocks an untrusted first big payment", async () => {
        const world = makeWorld({ rvPayloads: [{ verdict: "CAUTION", score: 50 }] });
        const { guard } = makeGuard(world, { config: gateConfig });
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(GuardBlockedError);
        expect(world.target.paidCalls).toBe(0);
        expect(world.routeVerdict.paidCalls).toBe(1);
    });

    it("passes TRUSTED and caches the verdict", async () => {
        const world = makeWorld({ rvPayloads: [{ verdict: "TRUSTED", score: 90 }] });
        const { guard } = makeGuard(world, { config: gateConfig });
        const response = await guard.fetch(TARGET, post());
        expect(response.status).toBe(200);
        await guard.fetch(TARGET, post());
        expect(world.routeVerdict.paidCalls).toBe(1);
    });

    it("is skipped below the threshold", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world); // default threshold $0.10 > $0.02
        const decision = await guard.evaluate(TARGET, 20_000);
        expect(decision.stages[2].outcome).toBe("skipped");
        expect(world.routeVerdict.paidCalls).toBe(0);
    });
});

// --- caps + observe mode + audit -------------------------------------------------------

describe("caps, observe mode, audit", () => {
    it("per-payment cap blocks before signing", async () => {
        const world = makeWorld();
        const { guard } = makeGuard(world, {
            config: { maxPaymentUnits: 10_000, driftEnabled: false, routeVerdictEnabled: false },
        });
        await expect(guard.fetch(TARGET, post())).rejects.toBeInstanceOf(GuardBlockedError);
        expect(world.target.paidCalls).toBe(0);
    });

    it("observe mode (no signer) returns the 402 unpaid and reports the veto", async () => {
        const world = makeWorld({ settledFor: settledFailedDelivery });
        const { guard } = makeGuard(world, { signer: null });
        const response = await guard.fetch(TARGET, post());
        expect(response.status).toBe(402);
        expect(world.target.paidCalls).toBe(0);
        const decision = await guard.evaluate(TARGET, 20_000);
        expect(decision.blocked).toBe(true);
        expect(decision.settledVeto).toContain("delivered=false");
    });

    it("writes the audit log with stages and check costs", async () => {
        const world = makeWorld();
        const { guard, dir } = makeGuard(world);
        await guard.fetch(TARGET, post());
        const lines = readFileSync(join(dir, "audit.jsonl"), "utf-8")
            .trim()
            .split("\n")
            .map((line) => JSON.parse(line));
        expect(lines).toHaveLength(1);
        const entry = lines[0];
        expect(entry.decision).toBe("allow");
        expect(entry.outcome).toBe("paid");
        expect(entry.checkCostUnits).toBe(5000);
        expect(entry.stages.map((s: { name: string }) => s.name)).toEqual([
            "settled",
            "drift",
            "route_verdict",
            "caps",
        ]);
    });
});
