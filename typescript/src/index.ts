/**
 * @quincygb/x402-guard — the payment-path seatbelt for x402 buyer
 * agents.
 *
 * Install the guarded fetch once inside your paying code; from then
 * on every x402 payment is pre-flighted before anything is signed:
 *
 *   1. Settled pre-flight (free)   — the inspector's delivery record
 *   2. Drift check (Recheck $0.005, cached daily)
 *                                  — the endpoint's terms haven't moved
 *   3. RouteVerdict gate ($0.05)   — big first payments need TRUSTED
 *   4. Pre-Trade Guard ($0.005)    — opt-in payTo screen (ABORT blocks)
 *   5. Your own spend caps         — enforced before signing
 *
 * Blocks throw GuardBlockedError with the full reason chain; every
 * decision lands in a local audit log you can read.
 */

export {
    X402Guard,
    createGuardedFetch,
    decisionSummary,
    type GuardDecision,
    type StageResult,
    type X402GuardOptions,
} from "./client";
export {
    configFromEnv,
    resolveConfig,
    SETTLED_MCP_URL,
    DEFAULT_RECHECK_URL,
    DEFAULT_ROUTE_VERDICT_URL,
    DEFAULT_PRE_TRADE_URL,
    type FailurePolicy,
    type GuardConfig,
    type GuardConfigOptions,
} from "./config";
export { GuardBlockedError } from "./errors";
export {
    fetchSettledReport,
    settledCorroborated,
    settledDescribe,
    settledFromPayload,
    settledHardNegative,
    settledNothingToCompare,
    settledUnavailable,
    type SettledReport,
} from "./settled";
export { JsonStore, type SpendEntry, type StoredCheck, type StoredVerdict } from "./store";
export {
    BASE_NETWORK,
    formatUsd,
    parsePaymentRequired,
    selectRequirement,
    type PaymentChallenge,
    type PaymentRequirement,
} from "./x402";
