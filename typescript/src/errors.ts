/**
 * GuardBlockedError: the guard refused a payment before anything
 * was signed. Carries the full decision so the caller can show the
 * buyer exactly which stage blocked and why (the reason chain).
 */

import type { GuardDecision } from "./client";

export class GuardBlockedError extends Error {
    readonly decision: GuardDecision;
    readonly reasons: string[];

    constructor(decision: GuardDecision) {
        const reasons = decision.reasons.length > 0
            ? decision.reasons
            : ["blocked by guard policy"];
        super(reasons.join("; "));
        this.name = "GuardBlockedError";
        this.decision = decision;
        this.reasons = reasons;
    }
}
