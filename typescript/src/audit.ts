/**
 * The audit log: one JSON object per guard decision, append-only.
 *
 * Every payment the guard allows, warns on, or blocks leaves a record
 * the buyer can read: what was about to be paid, which stages ran,
 * what each one found, what the checks themselves cost, and the
 * final decision with its reasons.
 */

import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export class AuditLog {
    readonly path: string;

    constructor(path: string) {
        this.path = path;
        mkdirSync(dirname(this.path), { recursive: true });
    }

    write(entry: Record<string, unknown>): void {
        appendFileSync(this.path, JSON.stringify(entry) + "\n", "utf-8");
    }
}
