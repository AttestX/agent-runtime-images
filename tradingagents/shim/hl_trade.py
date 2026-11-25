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
by ClawUp.

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
    """Not signed: a mandate refused, or ClawUp did not sign."""


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
        raise Refused(answer.get("message") or f"ClawUp answered {e.code}") from None
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


def allowed_by(answer: dict) -> str:
    names = [d.get("mandate") for d in answer.get("decisions") or [] if d.get("mandate")]
    return "allowed by " + ", ".join(names) if names else "allowed"


class RemoteSigner:
    """The wallet the SDK's Exchange is given. The key is ClawUp's, never
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
