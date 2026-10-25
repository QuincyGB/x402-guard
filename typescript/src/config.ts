/**
 * Guard configuration: one small explicit object, no magic.
 *
 * Mirrors the Python package's GuardConfig field for field. Amounts
 * are USDC base units (6 decimals) unless the name says otherwise.
 */

export const SETTLED_MCP_URL = "https://settled.tools/mcp/free";
export const DEFAULT_RECHECK_URL = "https://stall-row-production.up.railway.app/v1/recheck";
export const DEFAULT_ROUTE_VERDICT_URL =
    "https://route-verdict-production.up.railway.app/v1/check";

const USDC_UNITS_PER_DOLLAR = 1_000_000;

export type FailurePolicy = "warn" | "block";

export interface GuardConfigOptions {
    /** Stage 1: the free Settled pre-flight. */
    settledEnabled?: boolean;
    settledMcpUrl?: string;
    settledTimeoutMs?: number;
    /** Stage 2: StallRow Recheck drift check, cached for the TTL. */
    driftEnabled?: boolean;
    recheckUrl?: string;
    driftTtlSeconds?: number;
    priceChangeBlockPct?: number;
    /** What happens when the Recheck call itself fails. */
    driftFailure?: FailurePolicy;
    /** Stage 3: RouteVerdict gate for big first payments. */
    routeVerdictEnabled?: boolean;
    routeVerdictUrl?: string;
    routeVerdictThresholdUnits?: number;
    routeVerdictMinTrust?: number;
    routeVerdictFailure?: FailurePolicy;
    /** Stage 4: spend caps (null = no cap). */
    maxPaymentUnits?: number | null;
    perEndpointDailyCapUnits?: number | null;
    globalDailyCapUnits?: number | null;
    /** Local state: JSON store + JSONL audit log paths. */
    storePath?: string;
    auditLogPath?: string;
}

export interface GuardConfig {
    settledEnabled: boolean;
    settledMcpUrl: string;
    settledTimeoutMs: number;
    driftEnabled: boolean;
    recheckUrl: string;
    driftTtlSeconds: number;
    priceChangeBlockPct: number;
    driftFailure: FailurePolicy;
    routeVerdictEnabled: boolean;
    routeVerdictUrl: string;
    routeVerdictThresholdUnits: number;
    routeVerdictMinTrust: number;
    routeVerdictFailure: FailurePolicy;
    maxPaymentUnits: number | null;
    perEndpointDailyCapUnits: number | null;
    globalDailyCapUnits: number | null;
    storePath: string | null;
    auditLogPath: string | null;
}

export function resolveConfig(options: GuardConfigOptions = {}): GuardConfig {
    const config: GuardConfig = {
        settledEnabled: options.settledEnabled ?? true,
        settledMcpUrl: options.settledMcpUrl ?? SETTLED_MCP_URL,
        settledTimeoutMs: options.settledTimeoutMs ?? 10_000,
        driftEnabled: options.driftEnabled ?? true,
        recheckUrl: options.recheckUrl ?? DEFAULT_RECHECK_URL,
        driftTtlSeconds: options.driftTtlSeconds ?? 86_400,
        priceChangeBlockPct: options.priceChangeBlockPct ?? 20,
        driftFailure: options.driftFailure ?? "warn",
        routeVerdictEnabled: options.routeVerdictEnabled ?? true,
        routeVerdictUrl: options.routeVerdictUrl ?? DEFAULT_ROUTE_VERDICT_URL,
        routeVerdictThresholdUnits: options.routeVerdictThresholdUnits ?? 100_000,
        routeVerdictMinTrust: options.routeVerdictMinTrust ?? 80,
        routeVerdictFailure: options.routeVerdictFailure ?? "warn",
        maxPaymentUnits: options.maxPaymentUnits ?? null,
        perEndpointDailyCapUnits: options.perEndpointDailyCapUnits ?? null,
        globalDailyCapUnits: options.globalDailyCapUnits ?? null,
        storePath: options.storePath ?? null,
        auditLogPath: options.auditLogPath ?? null,
    };
    if (config.driftFailure !== "warn" && config.driftFailure !== "block") {
        throw new Error('driftFailure must be "warn" or "block"');
    }
    if (config.routeVerdictFailure !== "warn" && config.routeVerdictFailure !== "block") {
        throw new Error('routeVerdictFailure must be "warn" or "block"');
    }
    return config;
}

const TRUE_WORDS = new Set(["1", "true", "yes", "on"]);
const FALSE_WORDS = new Set(["0", "false", "no", "off"]);

/**
 * Read configuration from X402_GUARD_* environment variables — the
 * same names the Python package reads (USD amounts converted to
 * units). Explicit options passed to the constructor win over env.
 */
export function configFromEnv(env: Record<string, string | undefined> = process.env,
                              prefix = "X402_GUARD_"): GuardConfigOptions {
    const get = (name: string): string | undefined => {
        const value = env[prefix + name];
        return value !== undefined && value.trim() !== "" ? value.trim() : undefined;
    };
    const getBool = (name: string): boolean | undefined => {
        const raw = get(name);
        if (raw === undefined) return undefined;
        const low = raw.toLowerCase();
        if (TRUE_WORDS.has(low)) return true;
        if (FALSE_WORDS.has(low)) return false;
        throw new Error(`${prefix}${name}: not a boolean: ${raw}`);
    };
    const getFloat = (name: string): number | undefined => {
        const raw = get(name);
        return raw === undefined ? undefined : Number(raw);
    };
    const getUsdUnits = (name: string): number | undefined => {
        const raw = get(name);
        return raw === undefined ? undefined : Math.round(Number(raw) * USDC_UNITS_PER_DOLLAR);
    };
    const failure = (name: string): FailurePolicy | undefined => {
        const raw = get(name);
        return raw === undefined ? undefined : (raw.toLowerCase() as FailurePolicy);
    };

    const options: GuardConfigOptions = {
        settledEnabled: getBool("SETTLED_ENABLED"),
        settledMcpUrl: get("SETTLED_MCP_URL"),
        driftEnabled: getBool("DRIFT_ENABLED"),
        recheckUrl: get("RECHECK_URL"),
        driftTtlSeconds: getFloat("DRIFT_TTL_SECONDS"),
        priceChangeBlockPct: getFloat("PRICE_CHANGE_BLOCK_PCT"),
        driftFailure: failure("DRIFT_FAILURE"),
        routeVerdictEnabled: getBool("ROUTE_VERDICT_ENABLED"),
        routeVerdictUrl: get("ROUTE_VERDICT_URL"),
        routeVerdictThresholdUnits: getUsdUnits("ROUTE_VERDICT_THRESHOLD_USD"),
        routeVerdictMinTrust: getFloat("ROUTE_VERDICT_MIN_TRUST"),
        routeVerdictFailure: failure("ROUTE_VERDICT_FAILURE"),
        maxPaymentUnits: getUsdUnits("MAX_PAYMENT_USD"),
        perEndpointDailyCapUnits: getUsdUnits("PER_ENDPOINT_DAILY_CAP_USD"),
        globalDailyCapUnits: getUsdUnits("GLOBAL_DAILY_CAP_USD"),
        storePath: get("STORE_PATH"),
        auditLogPath: get("AUDIT_LOG_PATH"),
    };
    for (const key of Object.keys(options) as (keyof GuardConfigOptions)[]) {
        if (options[key] === undefined) delete options[key];
    }
    return options;
}
