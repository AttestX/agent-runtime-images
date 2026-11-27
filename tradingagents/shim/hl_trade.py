"""Trade on Hyperliquid after an analysis.

At most one order per finished analysis, on the coin's perpetual:
  * a Buy (or Overweight) rating on a crypto ticker (BTC-USD, ETH-USDT, ...)
    buys the owner's order size, in US dollars;
  * a Sell (or Underweight) rating closes a long position, reduce-only.
    No shorts are opened and leverage is never changed.

Orders are the official SDK's (hyperliquid-python-sdk's Exchange:
market_open and market_close, IOC limits 5% through the mid), so their value
is stated: price times size. The SDK signs with a local key; this agent holds
none. Its wallet is a server wallet ClawUp keeps for it, and the SDK is given
a RemoteSigner that asks ClawUp to sign (POST /api/v1/runtime/wallet/sign,
with the agent's gateway token). Hyperliquid has only a hash of the action
signed, so the request carries the action itself; ClawUp checks that it
hashes to what is signed, asks every mandate bound to the agent, and signs
only if all of them allow it. Then the SDK posts it to /exchange. Anything
else the SDK would sign (withdrawals, transfers) is refused by the signer, and
by ClawUp. Funds move only by the owner's approval, one exact request at a
time, from the agent's panel (below `RemoteSigner`'s helpers: `usd_send`,
`usd_class_transfer`, `ask_approval`).

Runs in its own venv with the SDK (/opt/hl); the shim calls
`python hl_trade.py <command> ...` and reads one JSON object from stdout.
The helpers above `RemoteSigner` use the standard library only, so they are
tested without the SDK.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request

EXCHANGE_HOST = "api.hyperliquid.xyz"
BASE_URL = f"https://{EXCHANGE_HOST}"
SLIPPAGE = 0.05
CLAWUP_API_URL = os.environ.get("CLAWUP_API_URL", "https://api.clawup.org").rstrip("/")
CRYPTO_QUOTES = ("-USD", "-USDT", "-USDC")
# The Hyperliquid account traded for. Empty: the agent's wallet's own account
# (the owner deposits into it). Or the owner's own address, after approving
# the agent's wallet as an API wallet of it in Hyperliquid: it then signs
# orders for that account, and cannot withdraw or transfer from it.
ACCOUNT = os.environ.get("TA_HL_ACCOUNT", "").strip()


class Refused(Exception):
    """Not signed: a mandate refused, or ClawUp did not sign. ``code`` is
    ClawUp's error code when it answered with one (e.g. WALLET_NOT_CREATED)."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def coin_of(ticker: str) -> str | None:
    """BTC-USD -> BTC. Only crypto tickers quoted in dollars trade here."""
    t = (ticker or "").strip().upper()
    for quote in CRYPTO_QUOTES:
        if t.endswith(quote) and len(t) > len(quote):
            return t[: -len(quote)]
    return None


def side_of(rating: str | None) -> str | None:
    """The portfolio manager's rating -> "buy", "sell" or None (hold, unknown)."""
    words = re.findall(r"[a-z]+", (rating or "").lower())
    if not words:
        return None
    if words[0] in ("buy", "overweight"):
        return "buy"
    if words[0] in ("sell", "underweight"):
        return "sell"
    return None


def order_size(usd: float, mid: float, sz_decimals: int) -> float:
    """The coin size for `usd` at `mid`, rounded down to the coin's step."""
    step = 10**sz_decimals
    return math.floor(usd / mid * step) / step


def payload(action: dict, nonce: int, vault: str | None = None, expires_after: int | None = None) -> str:
    """What the signature is a hash of: the action as the `/exchange` body
    carries it, without the signature, JSON in base64. Key order matters for
    the hash, and json.dumps keeps it."""
    shown = {"action": action, "nonce": nonce, "vaultAddress": vault}
    if expires_after is not None:
        shown["expiresAfter"] = expires_after
    return base64.b64encode(json.dumps(shown).encode()).decode()


def split_signature(sig: str) -> dict:
    """A 65-byte hex signature -> Hyperliquid's {r, s, v}."""
    raw = bytes.fromhex(sig[2:] if sig.startswith("0x") else sig)
    if len(raw) != 65:
        raise ValueError("a signature that is not 65 bytes")
    v = raw[64]
    if v < 27:
        v += 27
    return {"r": "0x" + raw[:32].hex(), "s": "0x" + raw[32:64].hex(), "v": v}


def clawup(method: str, path: str, body: dict | None = None) -> dict:
    """A call to ClawUp as this agent, by its gateway token."""
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        raise Refused("this agent has no gateway token, so ClawUp cannot sign for it")
    req = urllib.request.Request(
        f"{CLAWUP_API_URL}{path}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            answer = json.load(e)
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        code = answer.get("error") if isinstance(answer.get("error"), str) else None
        raise Refused(answer.get("message") or f"ClawUp answered {e.code}", code) from None
    except (urllib.error.URLError, OSError) as e:
        raise Refused(f"ClawUp is unreachable: {e}") from None


def wallet_address() -> str:
    """The agent's wallet, once its owner has opened it in the console."""
    address = clawup("GET", "/api/v1/runtime/wallet").get("address", "")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        raise Refused("ClawUp answered without a wallet address")
    return address


def trading_account() -> str:
    """The account whose positions are read: TA_HL_ACCOUNT, or the wallet's own."""
    if ACCOUNT:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", ACCOUNT):
            raise Refused("TA_HL_ACCOUNT is not an address")
        return ACCOUNT
    return wallet_address()


# ---------------------------------------------------------------------------
# The wallet's funds, and moving them by its owner's approval (the panel)
# ---------------------------------------------------------------------------

# The chain a user-signed action is signed under, as the SDK signs it; the
# action names it again in `signatureChainId`.
SIGNATURE_CHAIN_ID = "0x66eee"
USDC_RE = re.compile(r"\d{1,12}(?:\.\d{1,6})?")


def hyperliquid(path: str, body: dict, timeout: float = 15):
    """POST to Hyperliquid's API (`/info` or `/exchange`); the JSON answer."""
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace").strip()
        raise Refused(f"Hyperliquid answered {e.code}: {detail}" if detail else f"Hyperliquid answered {e.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Refused(f"Hyperliquid is unreachable: {e}") from None


def usdc(amount: str) -> str | None:
    """A USDC amount as Hyperliquid takes it ("12.5", "3"), or None when it is
    not a positive decimal of at most 6 places."""
    amount = (amount or "").strip()
    if not USDC_RE.fullmatch(amount):
        return None
    whole, _, frac = amount.partition(".")
    whole, frac = whole.lstrip("0") or "0", frac.rstrip("0")
    if whole == "0" and not frac:
        return None
    return f"{whole}.{frac}" if frac else whole


def micro(amount) -> int:
    """USDC in millionths, from Hyperliquid's decimal string; 0 if unreadable.
    Digits past the sixth place are dropped."""
    text = str(amount or "0").strip()
    neg = text.startswith("-")
    whole, _, frac = text.lstrip("-").partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()):
        return 0
    value = int(whole) * 1_000_000 + int((frac + "000000")[:6])
    return -value if neg else value


def shown(micro_usdc: int) -> str:
    """Millionths of USDC as a decimal string: 12500000 -> "12.5"."""
    sign = "-" if micro_usdc < 0 else ""
    whole, frac = divmod(abs(micro_usdc), 1_000_000)
    return f"{sign}{whole}.{frac:06d}".rstrip("0").rstrip(".")


def _nonzero(size) -> bool:
    try:
        return float(size) != 0.0
    except (TypeError, ValueError):
        return False


def holdings(address: str) -> dict:
    """What `address` holds on Hyperliquid. USDC in millionths: perps (the
    account value), withdrawable (of perps: the rest is margin), spot_usdc
    (not on hold); positions [(coin, size)]; spot [(coin, total)] for the
    other coins."""
    perps = hyperliquid("/info", {"type": "clearinghouseState", "user": address})
    spot = hyperliquid("/info", {"type": "spotClearinghouseState", "user": address})
    if not isinstance(perps, dict) or not isinstance(spot, dict):
        raise Refused("Hyperliquid answered without a balance")
    positions = []
    for p in perps.get("assetPositions") or []:
        pos = (p or {}).get("position") or {}
        if pos.get("coin") and _nonzero(pos.get("szi")):
            positions.append((str(pos["coin"]), str(pos["szi"])))
    spot_usdc, other = 0, []
    for b in spot.get("balances") or []:
        b = b or {}
        coin = str(b.get("coin") or "")
        if coin == "USDC":
            spot_usdc = max(0, micro(b.get("total")) - micro(b.get("hold")))
        elif coin and _nonzero(b.get("total")):
            other.append((coin, str(b["total"])))
    return {
        "perps": micro((perps.get("marginSummary") or {}).get("accountValue")),
        "withdrawable": micro(perps.get("withdrawable")),
        "spot_usdc": spot_usdc,
        "positions": positions,
        "spot": other,
    }


def user_signed(primary_type: str, fields: list, action: dict) -> dict:
    """The EIP-712 typed data of a user-signed action, as the SDK builds it
    (hyperliquid.utils.signing.user_signed_payload)."""
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": int(SIGNATURE_CHAIN_ID, 16),
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            primary_type: fields,
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": primary_type,
        "message": {f["name"]: action[f["name"]] for f in fields},
    }


def usd_send(destination: str, amount: str, time_ms: int) -> tuple[dict, dict]:
    """(action, typed data) sending `amount` USDC from the wallet's perps to
    `destination` on Hyperliquid (the SDK's usd_transfer)."""
    action = {
        "type": "usdSend", "signatureChainId": SIGNATURE_CHAIN_ID, "hyperliquidChain": "Mainnet",
        "destination": destination.lower(), "amount": amount, "time": time_ms,
    }
    return action, user_signed("HyperliquidTransaction:UsdSend", [
        {"name": "hyperliquidChain", "type": "string"},
        {"name": "destination", "type": "string"},
        {"name": "amount", "type": "string"},
        {"name": "time", "type": "uint64"},
    ], action)


def usd_class_transfer(amount: str, to_perp: bool, nonce: int) -> tuple[dict, dict]:
    """(action, typed data) moving `amount` USDC between the wallet's spot and
    perps (the SDK's usd_class_transfer)."""
    action = {
        "type": "usdClassTransfer", "signatureChainId": SIGNATURE_CHAIN_ID, "hyperliquidChain": "Mainnet",
        "amount": amount, "toPerp": to_perp, "nonce": nonce,
    }
    return action, user_signed("HyperliquidTransaction:UsdClassTransfer", [
        {"name": "hyperliquidChain", "type": "string"},
        {"name": "amount", "type": "string"},
        {"name": "toPerp", "type": "bool"},
        {"name": "nonce", "type": "uint64"},
    ], action)


def post_signed(action: dict, nonce: int, signature: str) -> None:
    """Posts a signed user action to `/exchange`; Refused with Hyperliquid's
    reason unless it answers ok."""
    answer = hyperliquid("/exchange", {
        "action": action, "nonce": nonce, "signature": split_signature(signature), "vaultAddress": None,
    })
    if not isinstance(answer, dict) or answer.get("status") != "ok":
        reason = answer.get("response") if isinstance(answer, dict) else answer
        raise Refused(f"Hyperliquid refused it: {reason}"[:300])


def ask_approval(typed_data: dict, summary: str) -> str:
    """Asks the agent's owner, through ClawUp, to approve signing
    `typed_data`; the approval's id. The console shows the owner ClawUp's
    record of it, not this agent's words alone."""
    answer = clawup("POST", "/api/v1/runtime/wallet/approvals", {"typed_data": typed_data, "summary": summary})
    approval_id = answer.get("id")
    if not isinstance(approval_id, str) or not re.fullmatch(r"[0-9a-f]{32}", approval_id):
        raise Refused("ClawUp answered without an approval")
    return approval_id


def approval(approval_id: str) -> dict:
    """{status: pending | approved | declined, signature?, expires_at}, or
    {status: expired} once ClawUp no longer has it."""
    try:
        return clawup("GET", f"/api/v1/runtime/wallet/approvals/{approval_id}")
    except Refused as e:
        if e.code == "APPROVAL_NOT_FOUND":
            return {"status": "expired"}
        raise


def allowed_by(answer: dict) -> str:
    names = [d.get("mandate") for d in answer.get("decisions") or [] if d.get("mandate")]
    return "allowed by " + ", ".join(names) if names else "allowed"


class RemoteSigner:
    """The wallet the SDK's Exchange is given. The key is held in Privy, never
    here: the SDK reads only `.address`, and it signs every L1 action through
    `hyperliquid.exchange.sign_l1_action`, which `place` points at
    `sign_l1_action` below. Anything signed another way (withdrawals,
    transfers) comes to `sign_message`, and is refused."""

    def __init__(self, address: str):
        self.address = address
        self.why = ""

    def sign_message(self, *_args, **_kwargs):
        raise Refused("only orders are signed for this agent")

    def sign_l1_action(self, wallet, action, active_pool, nonce, expires_after, is_mainnet):
        from hyperliquid.utils.signing import action_hash, construct_phantom_agent, l1_payload

        orders = action.get("orders") or []
        if wallet is not self or action.get("type") != "order" or len(orders) != 1:
            raise Refused("only one order at a time is signed for this agent")
        typed = l1_payload(construct_phantom_agent(action_hash(action, active_pool, nonce, expires_after), is_mainnet))
        typed["message"]["connectionId"] = "0x" + typed["message"]["connectionId"].hex()
        answer = clawup("POST", "/api/v1/runtime/wallet/sign", {
            "typed_data": typed,
            "payload": payload(action, nonce, active_pool, expires_after),
        })
        sig = answer.get("signature")
        if not sig:
            raise Refused("ClawUp returned no signature")
        self.why = allowed_by(answer)
        return split_signature(sig)


def place(ticker: str, rating: str, usd: float) -> dict:
    import hyperliquid.exchange as sdk_exchange
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    coin, side = coin_of(ticker), side_of(rating)
    if coin is None:
        return {"ok": False, "message": f"{ticker} does not trade on Hyperliquid (crypto quoted in USD only)"}
    if side is None:
        return {"ok": True, "message": f"no order: the rating is {rating or 'unknown'}"}
    info = Info(BASE_URL, skip_ws=True)
    meta = info.meta()
    asset = next((a for a in meta["universe"] if a["name"] == coin and not a.get("isDelisted")), None)
    if asset is None:
        return {"ok": False, "message": f"{coin} has no perpetual on Hyperliquid"}
    try:
        signer = RemoteSigner(wallet_address())
    except Refused as e:
        return {"ok": False, "message": str(e)}
    exchange = Exchange(signer, BASE_URL, meta=meta, account_address=ACCOUNT or None)
    signing, sdk_exchange.sign_l1_action = sdk_exchange.sign_l1_action, signer.sign_l1_action
    try:
        if side == "buy":
            mid = float(info.all_mids()[coin])
            size = order_size(usd, mid, asset["szDecimals"])
            if size <= 0:
                return {"ok": False, "message": f"${usd:g} is less than one {coin} size step at {mid:g}"}
            sent = exchange.market_open(coin, True, size, None, SLIPPAGE)
        else:
            positions = info.user_state(trading_account()).get("assetPositions", [])
            held = next((float(p["position"]["szi"]) for p in positions if p["position"]["coin"] == coin), 0.0)
            if held <= 0:
                return {"ok": True, "message": f"no order: Sell, but there is no {coin} long to close (shorts are not opened)"}
            sent = exchange.market_close(coin, None, None, SLIPPAGE)
    except Refused as e:
        return {"ok": False, "message": str(e)}
    finally:
        sdk_exchange.sign_l1_action = signing
    statuses = (((sent or {}).get("response") or {}).get("data") or {}).get("statuses") or []
    if (sent or {}).get("status") != "ok" or not statuses or "error" in statuses[0]:
        detail = statuses[0].get("error") if statuses else (sent or {}).get("response")
        return {"ok": False, "message": f"Hyperliquid did not fill the order: {detail}"}
    verb = "bought" if side == "buy" else "sold"
    filled = statuses[0].get("filled") or {}
    return {
        "ok": True,
        "message": f"{verb} {filled.get('totalSz', '?')} {coin} at {filled.get('avgPx', '?')} ({signer.why})",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("wallet")
    order = sub.add_parser("order")
    order.add_argument("--ticker", required=True)
    order.add_argument("--rating", required=True)
    order.add_argument("--usd", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "wallet":
            result = {"ok": True, "address": wallet_address(), "account": trading_account()}
        else:
            result = place(args.ticker, args.rating, args.usd)
    except Exception as e:  # one JSON line, whatever happens
        result = {"ok": False, "message": f"{type(e).__name__}: {e}"[:500]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
