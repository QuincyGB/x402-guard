/**
 * x402 v2 challenge parsing helpers — the small pieces of the wire
 * the guard needs before a payment exists (the actual signing is
 * delegated to @x402/fetch + @x402/evm, like the fleet's other TS
 * packages).
 */

export const BASE_NETWORK = "eip155:8453";
export const USDC_DECIMALS = 6;

export interface PaymentRequirement {
    scheme: string;
    network: string;
    amount: string;
    asset: string;
    payTo: string;
    maxTimeoutSeconds?: number;
    extra?: { name?: string; version?: string; [key: string]: unknown };
    [key: string]: unknown;
}

export interface PaymentChallenge {
    x402Version?: number;
    resource?: { url?: string; description?: string; mimeType?: string };
    accepts?: PaymentRequirement[];
    extensions?: Record<string, unknown>;
    [key: string]: unknown;
}

/** Parse the x402 v2 challenge from a 402 Response (header first,
 * JSON body second — the same order the fleet's Python client uses). */
export async function parsePaymentRequired(
    response: Response,
): Promise<PaymentChallenge> {
    const header = response.headers.get("payment-required");
    if (header) {
        try {
            const decoded = JSON.parse(
                Buffer.from(header, "base64").toString("utf-8"),
            ) as PaymentChallenge;
            if (decoded && Array.isArray(decoded.accepts)) return decoded;
        } catch {
            // fall through to the body
        }
    }
    try {
        const body = (await response.clone().json()) as PaymentChallenge;
        if (body && Array.isArray(body.accepts)) return body;
    } catch {
        // no usable body either
    }
    throw new Error("402 response carried no parseable x402 payment challenge");
}

/** Pick the accepts entry this client can settle (exact EVM on Base). */
export function selectRequirement(
    challenge: PaymentChallenge,
    network: string = BASE_NETWORK,
    scheme = "exact",
): PaymentRequirement {
    for (const entry of challenge.accepts ?? []) {
        if (entry.scheme === scheme && entry.network === network) return entry;
    }
    throw new Error(
        `challenge offers no ${scheme} requirement on ${network}: ` +
            JSON.stringify((challenge.accepts ?? []).map((a) => [a.scheme, a.network])),
    );
}

/** 20000 -> "$0.02", 50000 -> "$0.05", 100000 -> "$1.00". */
export function formatUsd(units: number): string {
    const s = (units / 10 ** USDC_DECIMALS).toFixed(6).replace(/0+$/, "");
    const trimmed = s.endsWith(".") ? s + "00" : s;
    const frac = trimmed.split(".")[1] ?? "";
    return "$" + (frac.length === 1 ? trimmed + "0" : trimmed);
}
