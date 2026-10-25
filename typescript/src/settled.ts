/**
 * Settled pre-flight: ask the inspector before paying.
 *
 * Ported from the fleet's Python implementation (verdict-loop's
 * settled_check.py, semantics fixed in its commit eae6b4c; the Python
 * x402-guard package carries the same code). Settled
 * (https://settled.tools) grades endpoints by actually *buying* from
 * them with its scout and recording whether the paid call delivered
 * what was advertised.
 *
 * The consultation is advisory with one narrow veto:
 * - a hard negative (a delivery receipt with delivered=false, or an
 *   explicit not-payable verdict) -> BLOCK. A receipt's comparison
 *   verdict (as_advertised, partial_match, nothing_to_compare, ...)
 *   is NEVER a veto on its own — those grade the listing's sample,
 *   not the delivery.
 * - delivery_verified=true -> corroboration, nothing more.
 * - Settled silent or unreachable -> pass-through, never a raise.
 *
 * Wire protocol: Settled's free MCP endpoint — JSON-RPC initialize
 * -> initialized -> tools/call "settled_check" {url}. No credentials.
 */

import { SETTLED_MCP_URL } from "./config";

const MCP_PROTOCOL_VERSION = "2025-06-18";
const SUPPORTED_PROTOCOLS = new Set(["2025-11-25", "2025-06-18", "2025-03-26"]);

/** Payability verdicts that are NOT hard negatives. */
const NON_NEGATIVE_PAYABILITY = new Set(["payable", "delivered", "unknown"]);

export interface SettledReport {
    url: string;
    available: boolean;
    found: boolean;
    status: string | null;
    payabilityVerdict: string | null;
    qualityTotal: number | null;
    deliveryVerified: boolean | null;
    hasReceipt: boolean;
    receiptDelivered: boolean | null;
    receiptVerdict: string | null;
    page: string | null;
    warning: string;
}

export function settledUnavailable(url: string, warning: string): SettledReport {
    return {
        url,
        available: false,
        found: false,
        status: null,
        payabilityVerdict: null,
        qualityTotal: null,
        deliveryVerified: null,
        hasReceipt: false,
        receiptDelivered: null,
        receiptVerdict: null,
        page: null,
        warning,
    };
}

export function settledFromPayload(url: string, payload: Record<string, unknown>): SettledReport {
    const quality = payload.quality as Record<string, unknown> | undefined;
    const qualityTotal =
        quality && quality.total !== undefined && quality.total !== null
            ? Number(quality.total)
            : null;
    const payability = payload.payability as Record<string, unknown> | undefined;
    const payabilityVerdict =
        payability && payability.verdict !== undefined && payability.verdict !== null
            ? String(payability.verdict)
            : null;
    const receipt = payload.delivery_receipt as Record<string, unknown> | undefined;
    const hasReceipt = !!receipt && typeof receipt === "object";
    let receiptDelivered: boolean | null = null;
    let receiptVerdict: string | null = null;
    if (hasReceipt) {
        if (receipt.delivered !== undefined && receipt.delivered !== null) {
            receiptDelivered = Boolean(receipt.delivered);
        }
        const checks = receipt.checks as Record<string, unknown> | undefined;
        if (checks && checks.verdict !== undefined && checks.verdict !== null) {
            receiptVerdict = String(checks.verdict);
        }
    }
    const deliveryVerified =
        payload.delivery_verified !== undefined && payload.delivery_verified !== null
            ? Boolean(payload.delivery_verified)
            : null;
    const status =
        payload.status !== undefined && payload.status !== null
            ? String(payload.status)
            : null;
    const found =
        status === "live" ||
        (payabilityVerdict !== null && payabilityVerdict !== "unknown") ||
        deliveryVerified !== null ||
        hasReceipt;
    return {
        url,
        available: true,
        found,
        status,
        payabilityVerdict,
        qualityTotal,
        deliveryVerified,
        hasReceipt,
        receiptDelivered,
        receiptVerdict,
        page: (payload.page as string) ?? null,
        warning: "",
    };
}

/** Settled's scout paid this endpoint and the delivery verified. */
export function settledCorroborated(report: SettledReport): boolean {
    return report.available && report.found && report.deliveryVerified === true;
}

/** The latest receipt could not be compared against the listing (no
 * usable sample). A listing gap, not a delivery failure — never a veto. */
export function settledNothingToCompare(report: SettledReport): boolean {
    return report.hasReceipt && report.receiptVerdict === "nothing_to_compare";
}

/**
 * A veto reason, or null. Deliberately narrow: only a failed delivery
 * (delivered=false) or an explicit not-payable verdict vetoes.
 */
export function settledHardNegative(report: SettledReport): string | null {
    if (!report.available || !report.found) return null;
    if (report.hasReceipt && report.receiptDelivered === false) {
        return (
            "Settled's delivery receipt for this endpoint records " +
            "delivered=false (its test purchase did not deliver)"
        );
    }
    if (report.payabilityVerdict && !NON_NEGATIVE_PAYABILITY.has(report.payabilityVerdict)) {
        return (
            `Settled reports payability verdict '${report.payabilityVerdict}' ` +
            "for this endpoint (not payable)"
        );
    }
    return null;
}

/** One-line summary for reports and the audit log. */
export function settledDescribe(report: SettledReport): string {
    if (!report.available) return `Settled pre-flight unavailable (${report.warning})`;
    if (!report.found) {
        return "Settled holds no payability verdict or delivery record for this endpoint yet";
    }
    const parts = ["Settled knows this endpoint"];
    if (report.status) parts.push(`status ${report.status}`);
    if (report.qualityTotal !== null) parts.push(`quality ${report.qualityTotal}/100`);
    if (settledCorroborated(report)) {
        parts.push("delivery VERIFIED by a Settled test purchase");
    } else if (report.hasReceipt) {
        parts.push("a Settled test purchase is on record");
    } else {
        parts.push("no delivery record yet");
    }
    if (settledNothingToCompare(report)) {
        parts.push(
            settledCorroborated(report)
                ? "latest receipt had nothing to compare (no usable sample in Settled's listing) — proceeding on verified delivery"
                : "latest receipt had nothing to compare (no usable sample in Settled's listing) — not a delivery failure, no veto",
        );
    }
    return parts.join(", ");
}

// ---------------------------------------------------------------------------
// MCP wire (free tier, no credentials)
// ---------------------------------------------------------------------------

class SettledError extends Error {}

function decodeRpcMessage(payload: string, requestId: number): Record<string, unknown> {
    let message: Record<string, unknown>;
    try {
        message = JSON.parse(payload) as Record<string, unknown>;
    } catch {
        throw new SettledError("MCP response was not valid JSON");
    }
    if (!message || message.jsonrpc !== "2.0") {
        throw new SettledError("MCP response was not a JSON-RPC message");
    }
    if (message.id !== requestId) {
        throw new SettledError("MCP response carried the wrong request id");
    }
    if ("error" in message) throw new SettledError("MCP returned a JSON-RPC error");
    const result = message.result;
    if (!result || typeof result !== "object") {
        throw new SettledError("MCP response carried no result object");
    }
    return result as Record<string, unknown>;
}

async function readRpcResult(response: Response, requestId: number): Promise<Record<string, unknown>> {
    const contentType = (response.headers.get("content-type") ?? "").split(";")[0].trim();
    if (contentType === "application/json") {
        return decodeRpcMessage(await response.text(), requestId);
    }
    if (contentType === "text/event-stream") {
        const text = await response.text();
        let data: string[] = [];
        for (const line of text.split("\n")) {
            if (line.startsWith("data:")) {
                data.push(line.slice(5).replace(/^ /, ""));
            } else if (line.trim() === "" && data.length > 0) {
                try {
                    return decodeRpcMessage(data.join("\n"), requestId);
                } catch {
                    data = []; // not our message; keep reading
                }
            }
        }
        if (data.length > 0) return decodeRpcMessage(data.join("\n"), requestId);
        throw new SettledError("MCP event stream ended before the result");
    }
    throw new SettledError(`MCP returned an unsupported content type: '${contentType}'`);
}

function extractToolPayload(result: Record<string, unknown>): Record<string, unknown> {
    const structured = result.structuredContent;
    if (structured && typeof structured === "object") {
        return structured as Record<string, unknown>;
    }
    for (const item of (result.content as Array<Record<string, unknown>>) ?? []) {
        if (item && item.type === "text" && typeof item.text === "string") {
            try {
                const parsed = JSON.parse(item.text) as unknown;
                if (parsed && typeof parsed === "object") {
                    return parsed as Record<string, unknown>;
                }
            } catch {
                continue;
            }
        }
    }
    throw new SettledError("settled_check result carried no endpoint payload");
}

/**
 * Ask Settled's free MCP what it knows about `url`. Never throws:
 * any failure comes back as an unavailable report (pass-through).
 */
export async function fetchSettledReport(
    fetchImpl: typeof fetch,
    url: string,
    options: { endpoint?: string; timeoutMs?: number } = {},
): Promise<SettledReport> {
    const endpoint = options.endpoint ?? SETTLED_MCP_URL;
    const timeoutMs = options.timeoutMs ?? 10_000;
    let session: string | null = null;
    let sequence = 0;

    async function call(
        method: string,
        params: Record<string, unknown>,
        notification = false,
    ): Promise<Record<string, unknown> | null> {
        sequence += 1;
        const requestId = sequence;
        const body: Record<string, unknown> = { jsonrpc: "2.0", method, params };
        if (!notification) body.id = requestId;
        const headers: Record<string, string> = {
            "content-type": "application/json",
            accept: "application/json, text/event-stream",
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        };
        if (session) headers["mcp-session-id"] = session;
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        let response: Response;
        try {
            response = await fetchImpl(endpoint, {
                method: "POST",
                headers,
                body: JSON.stringify(body),
                signal: controller.signal,
            });
        } finally {
            clearTimeout(timer);
        }
        const newSession = response.headers.get("mcp-session-id");
        if (newSession) session = newSession;
        if (notification) {
            if (![200, 202, 204].includes(response.status)) {
                throw new SettledError(
                    `MCP initialization notification got HTTP ${response.status}`,
                );
            }
            return null;
        }
        if (response.status !== 200) {
            throw new SettledError(`MCP call ${method} got HTTP ${response.status}`);
        }
        return readRpcResult(response, requestId);
    }

    try {
        const init = await call("initialize", {
            protocolVersion: MCP_PROTOCOL_VERSION,
            capabilities: {},
            clientInfo: { name: "x402-guard", version: "1" },
        });
        const protocol = init?.protocolVersion as string | undefined;
        const capabilities = init?.capabilities as Record<string, unknown> | undefined;
        if (
            !protocol ||
            !SUPPORTED_PROTOCOLS.has(protocol) ||
            !capabilities ||
            typeof capabilities.tools !== "object" ||
            capabilities.tools === null
        ) {
            throw new SettledError("MCP server did not negotiate a supported protocol");
        }
        await call("notifications/initialized", {}, true);
        const result = await call("tools/call", {
            name: "settled_check",
            arguments: { url },
        });
        if (!result || result.isError) {
            throw new SettledError("settled_check returned an error result");
        }
        return settledFromPayload(url, extractToolPayload(result));
    } catch (exc) {
        return settledUnavailable(url, exc instanceof Error ? exc.message : String(exc));
    }
}
