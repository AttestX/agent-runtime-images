"""Trade on Hyperliquid after an analysis.

At most one order per finished analysis, on the coin's perpetual:
  * a Buy (or Overweight) rating on a crypto ticker (BTC-USD, ETH-USDT, ...)
    buys the owner's order size, in US dollars;
  * a Sell (or Underweight) rating closes a long position, reduce-only.
    No shorts are opened and leverage is never changed.

Every order is a limit order (IOC, 5% through the mid price, as the SDK's
market orders are), so its value is stated: price times size.

An order is, in this order:
  1. asked of the owner's ClawUp mandate (CLAWUP_MANDATE, with CLAWUP_API_KEY):
     it is placed only on a signed "allow", with the parameters trusted-rail's
     Hyperliquid adapter states for the same order. No mandate, no trading;
  2. signed by the owner's Privy wallet, through the Privy agent-wallet CLI.
     Hyperliquid has only a hash of the action signed, so the request carries
     the action itself (header `x-hyperliquid-action`): the proxy can then check that
     the action hashes to what is signed, and decide it (trusted-rail);
  3. posted to Hyperliquid's /exchange.

Runs in its own venv with hyperliquid-python-sdk (/opt/hl); the shim calls
`python hl_trade.py <command> ...` and reads one JSON object from stdout.
The helpers above `main` use the standard library only, so they are tested
without the SDK.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from decimal import ROUND_CEILING, Decimal

# TA_HL_NETWORK=testnet trades on Hyperliquid's testnet (free test USDC) and
# signs as testnet; anything else is mainnet.
MAINNET = os.environ.get("TA_HL_NETWORK", "mainnet").strip().lower() != "testnet"
EXCHANGE_HOST = "api.hyperliquid.xyz" if MAINNET else "api.hyperliquid-testnet.xyz"
BASE_URL = f"https://{EXCHANGE_HOST}"
SLIPPAGE = 0.05
# Absolute by default: the provisioner sets the container's PATH itself.
_PRIVY_IN_IMAGE = "/opt/privy/bin/privy-agent-wallet"
PRIVY_CLI = os.environ.get("PRIVY_CLI") or (
    _PRIVY_IN_IMAGE if os.path.exists(_PRIVY_IN_IMAGE) else "privy-agent-wallet"
)
CLAWUP_API_URL = os.environ.get("CLAWUP_API_URL", "https://api.clawup.org").rstrip("/")
CRYPTO_QUOTES = ("-USD", "-USDT", "-USDC")
STATE_DIR = os.environ.get("TA_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".tradingagents")
WALLET_HOME = os.path.join(STATE_DIR, "wallet")
HOST_PRELOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "privy_host.cjs")


def privy_env() -> dict:
    """The Privy CLI's environment: its session (~/.privy) lives in the state
    dir, so it is backed up and restored with the agent, and is encrypted
    under this agent's own id rather than the container's hostname."""
    os.makedirs(WALLET_HOME, mode=0o700, exist_ok=True)
    id_path = os.path.join(WALLET_HOME, "host-id")
    try:
        with open(id_path) as f:
            host = f.read().strip()
    except FileNotFoundError:
        host = ""
    if not host:
        host = "ta-" + os.urandom(16).hex()
        fd = os.open(id_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(host)
    env = dict(os.environ)
    env["HOME"] = WALLET_HOME
    env["PRIVY_SESSION_HOST"] = host
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + f" --require {HOST_PRELOAD}").strip()
    return env


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


def limit_price(mid: float, is_buy: bool, sz_decimals: int) -> float:
    """As the SDK prices a market order: 5% through the mid, five significant
    figures, at most 6 - szDecimals decimals (perpetuals)."""
    px = mid * (1 + SLIPPAGE) if is_buy else mid * (1 - SLIPPAGE)
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def order_size(usd: float, mid: float, sz_decimals: int) -> float:
    """The coin size for `usd` at `mid`, rounded down to the coin's step."""
    step = 10**sz_decimals
    return math.floor(usd / mid * step) / step


def amount_micro(price_wire: str, size_wire: str) -> int:
    """Price times size in millionths, rounded up: as trusted-rail states it."""
    value = Decimal(price_wire) * Decimal(size_wire) * 1_000_000
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def micro(text: str) -> int:
    return int((Decimal(text) * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def mandate_parameters(coin: str, side: str, price_wire: str, size_wire: str) -> dict:
    """What trusted-rail's exchange/hyperliquid.json writes for this order."""
    return {
        "symbol": coin.lower(),
        "side": side,
        "order_type": "limit",
        "amount_micro": amount_micro(price_wire, size_wire),
        "quantity_micro": micro(size_wire),
        "limit_price_micro": micro(price_wire),
        "currency": "usdc",
        "account": "main",
        "venue": EXCHANGE_HOST,
        "rail": "hyperliquid",
    }


def privy_body(typed: dict) -> dict:
    """The Privy RPC body: sign the typed data. Privy refuses any other field."""
    return {
        "method": "eth_signTypedData_v4",
        "params": {
            "typed_data": {
                "domain": typed["domain"],
                "types": typed["types"],
                "primary_type": typed["primaryType"],
                "message": typed["message"],
            }
        },
    }


def action_header(action: dict, nonce: int) -> str:
    """The x-hyperliquid-action header: the action as the `/exchange` body
    carries it, without the signature, JSON in base64. Key order matters for
    the hash, and json.dumps keeps it."""
    shown = {"action": action, "nonce": nonce, "vaultAddress": None}
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


def privy(args: list[str], stdin: str | None = None, hyperliquid_action: str | None = None) -> str:
    env = privy_env()
    if hyperliquid_action:
        env["PRIVY_HYPERLIQUID_ACTION"] = hyperliquid_action  # sent as a header (privy_host.cjs)
    proc = subprocess.run(
        [PRIVY_CLI, *args], input=stdin, capture_output=True, text=True, timeout=60, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[-400:] or "the Privy CLI failed")
    return proc.stdout


def wallet_address() -> str:
    out = privy(["list-wallets"])
    m = re.search(r"ethereum:\s*(0x[0-9a-fA-F]{40})", out)
    if not m:
        raise RuntimeError("no Ethereum wallet: connect one first ('connect wallet')")
    return m.group(1)


def ask_mandate(action_id: str, parameters: dict) -> tuple[bool, str]:
    key = os.environ.get("CLAWUP_API_KEY", "")
    mandate = os.environ.get("CLAWUP_MANDATE", "")
    if not key or not mandate:
        return False, "no ClawUp mandate is configured (CLAWUP_MANDATE, CLAWUP_API_KEY), so nothing is traded"
    body = json.dumps({
        "action_id": action_id,
        "action": {"operation": "order.place", "resource": parameters["symbol"], "parameters": parameters},
    }).encode()
    req = urllib.request.Request(
        f"{CLAWUP_API_URL}/api/v1/mandates/agents/{urllib.request.quote(mandate, safe='')}/decide",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            answer = json.load(r)
    except urllib.error.HTTPError as e:
        return False, f"the mandate check failed (HTTP {e.code})"
    except (OSError, ValueError) as e:
        return False, f"the mandate check failed ({e})"
    if answer.get("decision") == "allow":
        return True, "allowed by the mandate"
    return False, "refused by the mandate: " + ", ".join(answer.get("reason_codes") or ["no reason given"])


def place(ticker: str, rating: str, usd: float) -> dict:
    from hyperliquid.api import API
    from hyperliquid.info import Info
    from hyperliquid.utils.signing import (
        action_hash,
        construct_phantom_agent,
        get_timestamp_ms,
        l1_payload,
        order_request_to_order_wire,
        order_wires_to_order_action,
    )

    coin, side = coin_of(ticker), side_of(rating)
    if coin is None:
        return {"ok": False, "message": f"{ticker} does not trade on Hyperliquid (crypto quoted in USD only)"}
    if side is None:
        return {"ok": True, "message": f"no order: the rating is {rating or 'unknown'}"}
    info = Info(BASE_URL, skip_ws=True)
    universe = info.meta()["universe"]
    index = next((i for i, a in enumerate(universe) if a["name"] == coin and not a.get("isDelisted")), None)
    if index is None:
        return {"ok": False, "message": f"{coin} has no perpetual on Hyperliquid"}
    sz_decimals = universe[index]["szDecimals"]
    mid = float(info.all_mids()[coin])
    is_buy = side == "buy"
    if is_buy:
        size = order_size(usd, mid, sz_decimals)
        reduce_only = False
    else:
        address = wallet_address()
        positions = info.user_state(address).get("assetPositions", [])
        held = next((float(p["position"]["szi"]) for p in positions if p["position"]["coin"] == coin), 0.0)
        if held <= 0:
            return {"ok": True, "message": f"no order: Sell, but there is no {coin} long to close (shorts are not opened)"}
        size, reduce_only = held, True
    if size <= 0:
        return {"ok": False, "message": f"${usd:g} is less than one {coin} size step at {mid:g}"}
    order = {
        "coin": coin, "is_buy": is_buy, "sz": size,
        "limit_px": limit_price(mid, is_buy, sz_decimals),
        "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": reduce_only,
    }
    action = order_wires_to_order_action([order_request_to_order_wire(order, index)])
    wire = action["orders"][0]
    nonce = get_timestamp_ms()

    allowed, why = ask_mandate(f"hl-{nonce}", mandate_parameters(coin, side, wire["p"], wire["s"]))
    if not allowed:
        return {"ok": False, "message": why}

    typed = l1_payload(construct_phantom_agent(action_hash(action, None, nonce, None), MAINNET))
    typed["message"]["connectionId"] = "0x" + typed["message"]["connectionId"].hex()
    answer = json.loads(privy(
        ["rpc"], stdin=json.dumps(privy_body(typed)), hyperliquid_action=action_header(action, nonce)
    ))
    sig = (answer.get("data") or {}).get("signature") or answer.get("signature")
    if not sig:
        return {"ok": False, "message": "Privy returned no signature"}
    sent = API(BASE_URL).post("/exchange", {
        "action": action, "nonce": nonce, "signature": split_signature(sig),
        "vaultAddress": None, "expiresAfter": None,
    })
    statuses = ((sent.get("response") or {}).get("data") or {}).get("statuses") or []
    if sent.get("status") != "ok" or not statuses or "error" in statuses[0]:
        detail = statuses[0].get("error") if statuses else sent.get("response")
        return {"ok": False, "message": f"Hyperliquid did not fill the order: {detail}"}
    verb = "bought" if is_buy else "sold"
    filled = statuses[0].get("filled") or {}
    return {
        "ok": True,
        "message": f"{verb} {filled.get('totalSz', wire['s'])} {coin} at {filled.get('avgPx', wire['p'])} (limit {wire['p']}, {why})",
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
            result = {"ok": True, "address": wallet_address()}
        else:
            result = place(args.ticker, args.rating, args.usd)
    except Exception as e:  # one JSON line, whatever happens
        result = {"ok": False, "message": f"{type(e).__name__}: {e}"[:500]}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
