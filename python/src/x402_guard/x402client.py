"""x402 v2 payment plumbing (the guard's internal buyer client).

The 402 -> sign -> retry flow below is adapted field-for-field from
the fleet's own proven client (verdict-loop's ``verdict_loop/x402.py``,
itself adapted from QuincyGB/token-verdict-tools), which mirrors the
live cataloging payment made with @x402/fetch + @x402/evm
`ExactEvmScheme`:

1. POST the endpoint unpaid -> HTTP 402 with a base64 JSON challenge
   in the `payment-required` response header.
2. Pick the `exact` / `eip155:8453` entry from `accepts`.
3. Build an EIP-3009 `TransferWithAuthorization`
   {from, to: payTo, value: amount, validAfter: "0",
    validBefore: now + maxTimeoutSeconds, nonce: 32 random bytes}.
4. Sign it as EIP-712 typed data with domain
   {name: accepts.extra.name, version: accepts.extra.version,
    chainId: 8453, verifyingContract: accepts.asset}.
5. Retry the identical POST with the payment payload in the
   `PAYMENT-SIGNATURE` header (base64 JSON). The service's facilitator
   verifies and settles USDC on Base server-side.

Safety contract: with no signer configured this module NEVER pays.
The caller gets a `CallOutcome` in dry-run mode carrying the parsed
challenge and the exact price it *would* pay — never a fabricated
response body.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE_NETWORK = "eip155:8453"
USDC_DECIMALS = 6

TRANSFER_WITH_AUTHORIZATION_TYPES = {
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


class ServiceError(Exception):
    """Non-payment failure from a service (carries HTTP status + body)."""

    def __init__(self, status: int, detail: Any):
        super().__init__(f"request failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SpendCapExceeded(Exception):
    """A spend cap (the caller's budget object) refused this payment
    before anything was signed."""


# ---------------------------------------------------------------------------
# Challenge parsing
# ---------------------------------------------------------------------------

def parse_payment_required(response: httpx.Response) -> dict:
    """Parse the x402 v2 challenge from a 402 response.

    The challenge travels base64-encoded in the `payment-required`
    header; some stacks also put it in the JSON body. Raises ValueError
    when neither carries a usable challenge.
    """
    header = response.headers.get("payment-required")
    if header:
        try:
            decoded = json.loads(base64.b64decode(header).decode("utf-8"))
            if isinstance(decoded, dict) and decoded.get("accepts"):
                return decoded
        except (ValueError, UnicodeDecodeError):
            pass
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("accepts"):
        return body
    raise ValueError("402 response carried no parseable x402 payment challenge")


def select_requirement(
    challenge: dict, network: str = BASE_NETWORK, scheme: str = "exact"
) -> dict:
    """Pick the accepts entry this client can settle (exact EVM on Base)."""
    for entry in challenge.get("accepts") or []:
        if entry.get("scheme") == scheme and entry.get("network") == network:
            return entry
    raise ValueError(
        f"challenge offers no {scheme} requirement on {network}: "
        f"{[(a.get('scheme'), a.get('network')) for a in challenge.get('accepts') or []]}"
    )


def units_to_usd(units: int) -> float:
    """USDC base units (6 decimals) -> dollars."""
    return units / (10 ** USDC_DECIMALS)


def format_usd(units: int) -> str:
    """20000 -> '$0.02', 50000 -> '$0.05', 100000 -> '$1.00'."""
    s = f"{units_to_usd(units):.6f}".rstrip("0")
    if s.endswith("."):
        s += "00"
    elif len(s.split(".")[1]) == 1:
        s += "0"
    return "$" + s


# ---------------------------------------------------------------------------
# Payment construction (EIP-3009 / EIP-712) — live mode only
# ---------------------------------------------------------------------------

def build_authorization(
    from_address: str,
    requirement: dict,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    """EIP-3009 authorization, string fields exactly as @x402/evm builds."""
    now = int(time.time()) if now is None else now
    return {
        "from": from_address,
        "to": requirement["payTo"],
        "value": str(requirement["amount"]),
        "validAfter": "0",
        "validBefore": str(now + int(requirement.get("maxTimeoutSeconds", 300))),
        "nonce": nonce or ("0x" + secrets.token_hex(32)),
    }


def sign_authorization(account, requirement: dict, authorization: dict) -> str:
    """EIP-712 sign the TransferWithAuthorization against the USDC domain
    the challenge advertises in accepts.extra (name/version).

    `account` is an eth_account LocalAccount (imported lazily by the
    caller so dry-run and the test-suite never need a signer stack).
    """
    extra = requirement.get("extra") or {}
    if not extra.get("name") or not extra.get("version"):
        raise ValueError(
            "payment requirement is missing EIP-712 domain parameters "
            f"(extra.name/extra.version) for asset {requirement.get('asset')}"
        )
    network = requirement.get("network", BASE_NETWORK)
    if not network.startswith("eip155:"):
        raise ValueError(f"unsupported network for EVM signing: {network}")
    domain = {
        "name": extra["name"],
        "version": extra["version"],
        "chainId": int(network.split(":", 1)[1]),
        "verifyingContract": requirement["asset"],
    }
    message = {
        "from": authorization["from"],
        "to": authorization["to"],
        "value": int(authorization["value"]),
        "validAfter": int(authorization["validAfter"]),
        "validBefore": int(authorization["validBefore"]),
        "nonce": bytes.fromhex(authorization["nonce"].removeprefix("0x")),
    }
    signed = account.sign_typed_data(
        domain_data=domain,
        message_types=TRANSFER_WITH_AUTHORIZATION_TYPES,
        message_data=message,
    )
    hex_sig = signed.signature.hex()
    return hex_sig if hex_sig.startswith("0x") else "0x" + hex_sig


def build_payment_payload(
    challenge: dict, requirement: dict, authorization: dict, signature: str
) -> dict:
    """The x402 v2 PaymentPayload the paid retry carries."""
    return {
        "x402Version": challenge.get("x402Version", 2),
        "resource": challenge.get("resource"),
        "accepted": requirement,
        "payload": {"authorization": authorization, "signature": signature},
    }


def encode_payment_header(payload: dict) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def decode_receipt(response: httpx.Response) -> dict | None:
    """Decode the base64 `payment-response` settlement receipt, if any."""
    raw = response.headers.get("payment-response")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def load_account_from_env(env: dict | None = None):
    """Build the signer from the operator's OWN environment.

    AGENT_PRIVATE_KEY: a 0x-prefixed EVM private key.
    AGENT_KEY_FILE:    path to a file whose contents are that key.
    Returns None when neither is set — callers then never pay.
    eth_account is imported lazily so dry-run works without it.
    """
    from eth_account import Account

    env = os.environ if env is None else env
    key = (env.get("AGENT_PRIVATE_KEY") or "").strip()
    if not key:
        key_file = (env.get("AGENT_KEY_FILE") or "").strip()
        if key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
    if not key:
        return None
    if not key.startswith("0x"):
        key = "0x" + key
    return Account.from_key(key)


# ---------------------------------------------------------------------------
# The one paid call the guard makes (targets and check services alike)
# ---------------------------------------------------------------------------

@dataclass
class CallOutcome:
    """Result of one (attempted) paid call.

    mode == "dry_run": nothing was paid; `challenge` + `price_units`
        describe exactly what a live run would pay. `response` is None —
        a dry run NEVER invents a service response.
    mode == "paid": the payment settled; `response` is the real service
        JSON and `receipt` the decoded settlement receipt.
    mode == "blocked": live mode, but the budget refused the payment
        before signing. `response` is None.
    """

    url: str
    mode: str  # "dry_run" | "paid" | "blocked"
    price_units: int
    challenge: dict | None = None
    requirement: dict | None = None
    response: dict | None = None
    receipt: dict | None = None
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def price_usd(self) -> float:
        return units_to_usd(self.price_units)


def call_paid_endpoint(
    http: httpx.Client,
    url: str,
    body: dict,
    *,
    account=None,
    budget=None,
) -> CallOutcome:
    """POST `url` with `body`, handling the x402 challenge.

    - No account -> dry-run outcome (challenge parsed, price reported).
    - Account + budget -> the budget must authorize the exact price
      BEFORE anything is signed; a settled payment is recorded against
      the budget only after the service answers 200.
    """
    response = http.post(url, json=body)
    if response.status_code == 200:
        # Free answer (shouldn't happen for paid routes, but never
        # pretend a payment occurred).
        return CallOutcome(
            url=url, mode="paid", price_units=0,
            response=_safe_json(response), receipt=decode_receipt(response),
            note="endpoint answered 200 without demanding payment",
        )
    if response.status_code != 402:
        raise ServiceError(response.status_code, _safe_json(response))

    challenge = parse_payment_required(response)
    requirement = select_requirement(challenge)
    price_units = int(requirement["amount"])

    if account is None:
        return CallOutcome(
            url=url, mode="dry_run", price_units=price_units,
            challenge=challenge, requirement=requirement,
            note="no signer configured — nothing was paid",
        )

    if budget is not None:
        try:
            budget.authorize(price_units)
        except SpendCapExceeded as exc:
            return CallOutcome(
                url=url, mode="blocked", price_units=price_units,
                challenge=challenge, requirement=requirement, note=str(exc),
            )

    authorization = build_authorization(account.address, requirement)
    signature = sign_authorization(account, requirement, authorization)
    payload = build_payment_payload(challenge, requirement, authorization, signature)
    paid = http.post(
        url, json=body,
        headers={"PAYMENT-SIGNATURE": encode_payment_header(payload)},
    )
    if paid.status_code == 200:
        if budget is not None:
            budget.record(price_units, label=url)
        return CallOutcome(
            url=url, mode="paid", price_units=price_units,
            challenge=challenge, requirement=requirement,
            response=paid.json(), receipt=decode_receipt(paid),
        )
    if paid.status_code == 402:
        # Settlement rejected — nothing was recorded against the budget.
        raise ServiceError(402, "paid retry still returned 402 (settlement rejected)")
    raise ServiceError(paid.status_code, _safe_json(paid))


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text[:500]
