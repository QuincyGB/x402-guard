"""Settled pre-flight: ask the inspector before paying.

Ported from verdict-loop's ``verdict_loop/settled_check.py`` (the
fleet's reference implementation, semantics fixed in its commit
eae6b4c). Settled (https://settled.tools) grades endpoints by actually
*buying* from them with its scout and recording whether the paid call
delivered what was advertised. Before the guard lets a payment be
signed, it can ask Settled — for free — what those test purchases
found.

The consultation is advisory with one narrow veto:

- Settled shows a hard negative for the endpoint (a delivery receipt
  with delivered=false, or an explicit not-payable verdict) -> the
  guard BLOCKS. A receipt's comparison verdict (as_advertised,
  partial_match, nothing_to_compare, ...) is NEVER a veto on its own:
  those grade the listing's sample, not the delivery. In particular
  nothing_to_compare — Settled had no usable sample in the listing it
  holds, so it could not compare fields — says nothing bad about an
  endpoint whose delivery verified, and the report says so out loud
  (see SettledReport.describe) instead of quietly passing.
- Settled shows delivery_verified=true -> corroboration. That is all:
  it never loosens any other stage's bar.
- Settled does not know the endpoint, or Settled is unreachable ->
  pass-through. A Settled outage must never crash or block a payment,
  so every failure here degrades to an "unavailable" report, never
  a raise.

Wire protocol: Settled's free MCP endpoint
(https://settled.tools/mcp/free) — JSON-RPC initialize -> initialized
-> tools/call "settled_check" {url}. No credentials, no payment.
Requests go through the caller's httpx client (so tests drive it with
a MockTransport).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

SETTLED_MCP_URL = "https://settled.tools/mcp/free"
MCP_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOLS = {"2025-11-25", "2025-06-18", "2025-03-26"}
DEFAULT_TIMEOUT = 10.0

# Payability verdicts that are NOT hard negatives: the endpoint can be
# paid, it has been paid and delivered, or Settled has no verdict yet.
_NON_NEGATIVE_PAYABILITY = {"payable", "delivered", "unknown"}


class _SettledError(Exception):
    """Transport/protocol failure talking to Settled (always degraded)."""


@dataclass
class SettledReport:
    """What Settled knows about one endpoint URL (or that it is silent).

    available=False means the lookup itself failed (network, protocol,
    tool error): `warning` says why and the caller proceeds without
    Settled. found=False means Settled answered but holds no
    payability verdict and no delivery record for the endpoint yet.
    """

    url: str
    available: bool
    found: bool = False
    status: str | None = None
    payability_verdict: str | None = None
    quality_total: float | None = None
    delivery_verified: bool | None = None
    has_receipt: bool = False
    receipt_delivered: bool | None = None
    receipt_verdict: str | None = None
    page: str | None = None
    warning: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def unavailable(cls, url: str, warning: str) -> "SettledReport":
        return cls(url=url, available=False, warning=warning)

    @classmethod
    def from_payload(cls, url: str, payload: dict) -> "SettledReport":
        quality = payload.get("quality")
        quality_total = None
        if isinstance(quality, dict) and quality.get("total") is not None:
            quality_total = float(quality["total"])
        payability = payload.get("payability")
        payability_verdict = (
            payability.get("verdict") if isinstance(payability, dict) else None
        )
        receipt = payload.get("delivery_receipt")
        has_receipt = isinstance(receipt, dict)
        receipt_delivered = None
        receipt_verdict = None
        if has_receipt:
            if receipt.get("delivered") is not None:
                receipt_delivered = bool(receipt["delivered"])
            checks = receipt.get("checks")
            if isinstance(checks, dict) and checks.get("verdict") is not None:
                receipt_verdict = str(checks["verdict"])
        delivery_verified = payload.get("delivery_verified")
        if delivery_verified is not None:
            delivery_verified = bool(delivery_verified)
        status = payload.get("status")
        found = (
            status == "live"
            or payability_verdict not in (None, "unknown")
            or delivery_verified is not None
            or has_receipt
        )
        return cls(
            url=url,
            available=True,
            found=found,
            status=str(status) if status is not None else None,
            payability_verdict=(
                str(payability_verdict) if payability_verdict is not None else None
            ),
            quality_total=quality_total,
            delivery_verified=delivery_verified,
            has_receipt=has_receipt,
            receipt_delivered=receipt_delivered,
            receipt_verdict=receipt_verdict,
            page=payload.get("page"),
            raw=payload,
        )

    @property
    def corroborated(self) -> bool:
        """Settled's scout paid this endpoint and the delivery verified."""
        return self.available and self.found and self.delivery_verified is True

    @property
    def nothing_to_compare(self) -> bool:
        """The latest receipt Settled holds could not be compared
        against the listing (it publishes no usable sample in the copy
        Settled ingested). A gap in the listing, not a delivery
        failure — reported distinctly, never a veto."""
        return self.has_receipt and self.receipt_verdict == "nothing_to_compare"

    @property
    def hard_negative(self) -> str | None:
        """A veto reason, or None. Deliberately narrow: only a failed
        delivery (delivered=false) or an explicit not-payable verdict
        vetoes — an absent record, an untested endpoint
        (delivery_verified null), quality scores, and receipt
        comparison verdicts (nothing_to_compare, partial_match, ...)
        never do."""
        if not self.available or not self.found:
            return None
        if self.has_receipt and self.receipt_delivered is False:
            return (
                "Settled's delivery receipt for this endpoint records "
                "delivered=false (its test purchase did not deliver)"
            )
        if (
            self.payability_verdict
            and self.payability_verdict not in _NON_NEGATIVE_PAYABILITY
        ):
            return (
                f"Settled reports payability verdict "
                f"{self.payability_verdict!r} for this endpoint (not payable)"
            )
        return None

    def describe(self) -> str:
        """One-line summary for reports and the audit log."""
        if not self.available:
            return f"Settled pre-flight unavailable ({self.warning})"
        if not self.found:
            return (
                "Settled holds no payability verdict or delivery record "
                "for this endpoint yet"
            )
        parts = ["Settled knows this endpoint"]
        if self.status:
            parts.append(f"status {self.status}")
        if self.quality_total is not None:
            parts.append(f"quality {self.quality_total:g}/100")
        if self.corroborated:
            parts.append("delivery VERIFIED by a Settled test purchase")
        elif self.has_receipt:
            parts.append("a Settled test purchase is on record")
        else:
            parts.append("no delivery record yet")
        if self.nothing_to_compare:
            if self.corroborated:
                parts.append(
                    "latest receipt had nothing to compare "
                    "(no usable sample in Settled's listing) — "
                    "proceeding on verified delivery"
                )
            else:
                parts.append(
                    "latest receipt had nothing to compare "
                    "(no usable sample in Settled's listing) — "
                    "not a delivery failure, no veto"
                )
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# MCP wire (free tier, no credentials)
# ---------------------------------------------------------------------------

def _decode_rpc_message(payload: bytes, request_id: int) -> dict:
    try:
        message = json.loads(payload)
    except ValueError:
        raise _SettledError("MCP response was not valid JSON") from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise _SettledError("MCP response was not a JSON-RPC message")
    if message.get("id") != request_id:
        raise _SettledError("MCP response carried the wrong request id")
    if "error" in message:
        raise _SettledError("MCP returned a JSON-RPC error")
    result = message.get("result")
    if not isinstance(result, dict):
        raise _SettledError("MCP response carried no result object")
    return result


def _read_rpc_result(response: httpx.Response, request_id: int) -> dict:
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if content_type == "application/json":
        return _decode_rpc_message(response.content, request_id)
    if content_type == "text/event-stream":
        data: list[str] = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                try:
                    return _decode_rpc_message("\n".join(data).encode(), request_id)
                except _SettledError:
                    data.clear()  # not our message (or an error frame); keep reading
        if data:
            return _decode_rpc_message("\n".join(data).encode(), request_id)
        raise _SettledError("MCP event stream ended before the result")
    raise _SettledError(f"MCP returned an unsupported content type: {content_type!r}")


def _extract_tool_payload(result: dict) -> dict:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text") or "")
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise _SettledError("settled_check result carried no endpoint payload")


def fetch_settled_report(
    http: httpx.Client,
    url: str,
    *,
    endpoint: str = SETTLED_MCP_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> SettledReport:
    """Ask Settled's free MCP what it knows about `url`. Never raises.

    Any failure — network, timeout, protocol, tool error — comes back
    as an unavailable report so the caller proceeds on other signals.
    The lookup is read-only and free; it never spends.
    """
    session: str | None = None
    sequence = 0

    def call(method: str, params: dict, *, notification: bool = False) -> dict | None:
        nonlocal session, sequence
        sequence += 1
        request_id = sequence
        body: dict = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        response = http.post(endpoint, json=body, headers=headers, timeout=timeout)
        new_session = response.headers.get("Mcp-Session-Id")
        if new_session:
            session = new_session
        if notification:
            if response.status_code not in (200, 202, 204):
                raise _SettledError(
                    f"MCP initialization notification got HTTP {response.status_code}"
                )
            return None
        if response.status_code != 200:
            raise _SettledError(f"MCP call {method} got HTTP {response.status_code}")
        return _read_rpc_result(response, request_id)

    try:
        init = call(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "x402-guard", "version": "1"},
            },
        )
        protocol = (init or {}).get("protocolVersion")
        capabilities = (init or {}).get("capabilities")
        if (
            protocol not in _SUPPORTED_PROTOCOLS
            or not isinstance(capabilities, dict)
            or not isinstance(capabilities.get("tools"), dict)
        ):
            raise _SettledError("MCP server did not negotiate a supported protocol")
        call("notifications/initialized", {}, notification=True)
        result = call("tools/call", {"name": "settled_check", "arguments": {"url": url}})
        if result is None or result.get("isError"):
            raise _SettledError("settled_check returned an error result")
        return SettledReport.from_payload(url, _extract_tool_payload(result))
    except (httpx.HTTPError, _SettledError, ValueError) as exc:
        return SettledReport.unavailable(url, str(exc) or exc.__class__.__name__)
