"""GuardBlocked: the guard refused a payment before anything was signed.

Carries the full GuardDecision so the caller can show the buyer
exactly which stage blocked and why (the reason chain), not just
that something somewhere said no.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a cycle: client imports this module
    from .client import GuardDecision


class GuardBlocked(Exception):
    """A policy stage blocked the payment. Nothing was signed."""

    def __init__(self, decision: "GuardDecision"):
        self.decision = decision
        reasons = decision.reasons or ["blocked by guard policy"]
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)
