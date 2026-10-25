/**
 * The guard's local store: check cache + spend ledger (a JSON file).
 *
 * Everything here lives on the BUYER's machine, in the buyer's own
 * file — their data, never sent anywhere. Three collections:
 * checks (last Recheck look per endpoint, with its payment
 * fingerprint), routeVerdicts (last gate verdict per endpoint), and
 * spend (an append-only ledger of settled payments, feeding the
 * daily caps).
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export interface StoredCheck {
    /** epoch milliseconds */
    checkedAt: number;
    status: string;
    fingerprint: Record<string, unknown>;
}

export interface StoredVerdict {
    checkedAt: number;
    verdict: string;
    score: number;
}

export interface SpendEntry {
    date: string;
    url: string;
    units: number;
    ts: number;
}

interface StoreData {
    checks: Record<string, StoredCheck>;
    routeVerdicts: Record<string, StoredVerdict>;
    spend: SpendEntry[];
}

export function defaultStorePath(): string {
    return join(homedir(), ".x402-guard", "guard.json");
}

function emptyData(): StoreData {
    return { checks: {}, routeVerdicts: {}, spend: [] };
}

export class JsonStore {
    readonly path: string;
    private data: StoreData;

    constructor(path?: string | null) {
        this.path = path ?? defaultStorePath();
        this.data = this.load();
    }

    private load(): StoreData {
        try {
            if (existsSync(this.path)) {
                const parsed = JSON.parse(readFileSync(this.path, "utf-8")) as StoreData;
                if (parsed && typeof parsed === "object") {
                    return {
                        checks: parsed.checks ?? {},
                        routeVerdicts: parsed.routeVerdicts ?? {},
                        spend: parsed.spend ?? [],
                    };
                }
            }
        } catch {
            // a corrupt store starts clean rather than bricking payments
        }
        return emptyData();
    }

    private save(): void {
        mkdirSync(dirname(this.path), { recursive: true });
        writeFileSync(this.path, JSON.stringify(this.data, null, 2), "utf-8");
    }

    getCheck(url: string): StoredCheck | null {
        return this.data.checks[url] ?? null;
    }

    saveCheck(url: string, status: string, fingerprint: Record<string, unknown>,
              nowMs: number): void {
        this.data.checks[url] = { checkedAt: nowMs, status, fingerprint };
        this.save();
    }

    getRouteVerdict(url: string): StoredVerdict | null {
        return this.data.routeVerdicts[url] ?? null;
    }

    saveRouteVerdict(url: string, verdict: string, score: number, nowMs: number): void {
        this.data.routeVerdicts[url] = { checkedAt: nowMs, verdict, score };
        this.save();
    }

    recordSpend(url: string, units: number, nowMs: number): void {
        if (units <= 0) return;
        this.data.spend.push({
            date: new Date(nowMs).toISOString().slice(0, 10),
            url,
            units,
            ts: nowMs,
        });
        this.save();
    }

    /** Total settled units on the UTC day containing `nowMs`,
     * globally or for one paid URL. */
    spentToday(nowMs: number, url?: string): number {
        const date = new Date(nowMs).toISOString().slice(0, 10);
        let total = 0;
        for (const entry of this.data.spend) {
            if (entry.date === date && (url === undefined || entry.url === url)) {
                total += entry.units;
            }
        }
        return total;
    }
}
