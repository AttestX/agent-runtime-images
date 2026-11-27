"""Tests for trading on Hyperliquid after an analysis (shim/hl_trade.py) and
the wallet chat commands. Stdlib only: the SDK and the Privy CLI are not
needed, and nothing reaches the network."""

import importlib.util
import json
import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
import tempfile
import unittest
from pathlib import Path

SHIM_DIR = Path(__file__).resolve().parent.parent / "shim"
sys.path.insert(0, str(SHIM_DIR))
import hl_trade as t  # noqa: E402

_spec = importlib.util.spec_from_file_location("ta_shim", SHIM_DIR / "ta_shim.py")
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)


class Ratings(unittest.TestCase):
    def test_only_dollar_crypto_trades(self):
        self.assertEqual(t.coin_of("BTC-USD"), "BTC")
        self.assertEqual(t.coin_of("eth-usdt"), "ETH")
        self.assertEqual(t.coin_of("SOL-USDC"), "SOL")
        for ticker in ("NVDA", "ETH-BTC", "600519.SS", "-USD", ""):
            self.assertIsNone(t.coin_of(ticker), ticker)

    def test_the_rating_sets_the_side(self):
        self.assertEqual(t.side_of("Buy"), "buy")
        self.assertEqual(t.side_of("**Overweight** (conviction: high)"), "buy")
        self.assertEqual(t.side_of("SELL"), "sell")
        self.assertEqual(t.side_of("Underweight"), "sell")
        for rating in ("Hold", None, "", "Not a buy"):
            self.assertIsNone(t.side_of(rating), rating)


class Orders(unittest.TestCase):
    def test_size_rounds_down_to_the_step(self):
        self.assertEqual(t.order_size(100, 97000.5, 5), 0.00103)
        self.assertEqual(t.order_size(5, 97000.5, 3), 0.0)

    def test_the_payload_keeps_the_key_order(self):
        import base64
        action = {"type": "order", "orders": [{"a": 0, "b": True, "p": "1", "s": "1", "r": False,
                                               "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}
        shown = json.loads(base64.b64decode(t.payload(action, 7)))
        self.assertEqual(shown, {"action": action, "nonce": 7, "vaultAddress": None})
        timed = json.loads(base64.b64decode(t.payload(action, 7, None, 99)))
        self.assertEqual(timed["expiresAfter"], 99)
        self.assertEqual(list(shown["action"]), ["type", "orders", "grouping"])
        self.assertEqual(list(shown["action"]["orders"][0]), ["a", "b", "p", "s", "r", "t"])

    def test_signature(self):
        sig = "0x" + "11" * 32 + "22" * 32 + "1b"
        self.assertEqual(t.split_signature(sig), {"r": "0x" + "11" * 32, "s": "0x" + "22" * 32, "v": 27})
        self.assertEqual(t.split_signature("11" * 64 + "01")["v"], 28)
        with self.assertRaises(ValueError):
            t.split_signature("0x1234")

    def test_the_mandates_that_allowed_it(self):
        self.assertEqual(t.allowed_by({"decisions": [{"mandate": "a"}, {"mandate": "b"}]}), "allowed by a, b")
        self.assertEqual(t.allowed_by({}), "allowed")


ADDRESS = "0x" + "ab" * 20
SIGNATURE = "0x" + "11" * 64 + "1b"


class StandInClawUp:
    """ClawUp's runtime wallet routes, on localhost: what the agent sends is
    recorded, and the answers are set per test."""

    def __init__(self):
        self.seen = []
        self.wallet = (200, {"address": ADDRESS, "provider": "mock"})
        self.sign = (200, {"signature": SIGNATURE, "decisions": [{"mandate": "trade"}]})
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.seen.append(("GET", self.path, self.headers.get("Authorization"), None))
                self.answer(*outer.wallet)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
                self.answer(*outer.sign)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClawUpCase(unittest.TestCase):
    """A stand-in ClawUp, and this agent's gateway token."""

    def setUp(self):
        self.clawup = StandInClawUp()
        self.old = (t.CLAWUP_API_URL, os.environ.get("OPENCLAW_GATEWAY_TOKEN"), t.ACCOUNT)
        t.CLAWUP_API_URL = self.clawup.url
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = "gw-token"

    def tearDown(self):
        self.clawup.close()
        t.CLAWUP_API_URL, token, t.ACCOUNT = self.old
        if token is None:
            os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)
        else:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = token



class TalksToClawUp(ClawUpCase):
    def test_the_wallet_is_asked_for_with_the_gateway_token(self):
        self.assertEqual(t.wallet_address(), ADDRESS)
        self.assertEqual(self.clawup.seen[0][:3], ("GET", "/api/v1/runtime/wallet", "Bearer gw-token"))

    def test_no_wallet_yet_says_why(self):
        self.clawup.wallet = (409, {"error": "WALLET_NOT_CREATED", "message": "This agent has no wallet yet"})
        with self.assertRaisesRegex(t.Refused, "no wallet yet"):
            t.wallet_address()
        self.assertIn("No wallet yet", shim.wallet_reply("status"))
        self.assertIn("console", shim.wallet_reply("status"))

    def test_a_failure_is_not_called_a_missing_wallet(self):
        self.clawup.wallet = (401, {"error": "AUTH_401", "message": "Send the agent's gateway token"})
        with self.assertRaises(t.Refused) as caught:
            t.wallet_address()
        self.assertEqual(caught.exception.code, "AUTH_401")
        reply = shim.wallet_reply("status")
        self.assertNotIn("No wallet yet", reply)
        self.assertIn("gateway token", reply)
        t.CLAWUP_API_URL = "http://127.0.0.1:9"  # nothing listens
        reply = shim.wallet_reply("status")
        self.assertNotIn("No wallet yet", reply)
        self.assertIn("unreachable", reply)

    def test_without_a_gateway_token_nothing_is_asked(self):
        del os.environ["OPENCLAW_GATEWAY_TOKEN"]
        with self.assertRaisesRegex(t.Refused, "gateway token"):
            t.wallet_address()
        self.assertEqual(self.clawup.seen, [])

    def test_the_account_traded_for(self):
        self.assertEqual(t.trading_account(), ADDRESS)
        t.ACCOUNT = "0x00000000000000000000000000000000000000Aa"
        self.assertEqual(t.trading_account(), "0x00000000000000000000000000000000000000Aa")
        t.ACCOUNT = "not-an-address"
        with self.assertRaises(t.Refused):
            t.trading_account()

    def test_the_wallet_reply(self):
        old = (shim.TRADE_ENABLED, shim.TRADE_ORDER_USD)
        try:
            shim.TRADE_ENABLED, shim.TRADE_ORDER_USD = True, 25.0
            reply = shim.wallet_reply("status")
        finally:
            shim.TRADE_ENABLED, shim.TRADE_ORDER_USD = old
        self.assertIn(ADDRESS, reply)
        self.assertIn("$25", reply)
        self.assertIn("mandate", reply)

    def test_wallet_commands(self):
        self.assertEqual(shim.wallet_command("wallet"), "status")
        self.assertEqual(shim.wallet_command("钱包？"), "status")
        self.assertEqual(shim.wallet_command("  My wallet! "), "status")
        for text in ("analyze BTC-USD", "what is a wallet", "wallet BTC", "connect wallet"):
            self.assertIsNone(shim.wallet_command(text), text)


class AfterAnalysis(unittest.TestCase):
    def setUp(self):
        self.old = (shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON, shim.DATA_ROOT)
        shim.DATA_ROOT = tempfile.mkdtemp(prefix="ta-trade-test-")  # no saved panel settings

    def tearDown(self):
        shutil.rmtree(shim.DATA_ROOT, ignore_errors=True)
        shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON, shim.DATA_ROOT = self.old

    def run_of(self, ticker, decision):
        run = shim.Run(ticker, "2026-11-25")
        run.decision = decision
        return run

    def test_off_by_default_and_only_for_crypto(self):
        shim.TRADE_ENABLED = False
        self.assertIsNone(shim._trade(self.run_of("BTC-USD", "Buy")))
        shim.TRADE_ENABLED = True
        self.assertIsNone(shim._trade(self.run_of("NVDA", "Buy")))
        self.assertIn("rating is Hold", shim._trade(self.run_of("BTC-USD", "Hold")))
        shim.TRADE_ORDER_USD = 0
        self.assertIn("no order size", shim._trade(self.run_of("BTC-USD", "Buy")))

    def test_one_order_through_hl_trade(self):
        # A stand-in for the SDK venv: prints what hl_trade.py would.
        tmp = tempfile.mkdtemp(prefix="hl-python-")
        try:
            fake = os.path.join(tmp, "python")
            with open(fake, "w") as f:
                f.write('#!/bin/sh\necho \'{"ok": false, "message": "refused by the mandate \\"trade\\""}\'\n')
            os.chmod(fake, 0o755)
            shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON = True, 25.0, fake
            run = self.run_of("ETH-USD", "Sell")
            run.trade = shim._trade(run)
            self.assertEqual(run.trade, "not traded: refused by the mandate \"trade\"")
            run.status = "done"
            self.assertIn("Hyperliquid: not traded", shim._result_text(run))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


try:
    import hyperliquid  # noqa: F401
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "needs hyperliquid-python-sdk (run with /opt/hl/bin/python)")
class SignsForTheSdk(ClawUpCase):
    """The SDK's Exchange signs through RemoteSigner: ClawUp is asked to sign,
    with the action it hashes; nothing else is signed."""

    # A buy of 0.0125 BTC at 97000.5, and its connectionId (SDK 0.24.1).
    ACTION = {"type": "order", "orders": [{"a": 0, "b": True, "p": "97000.5", "s": "0.0125", "r": False,
                                           "t": {"limit": {"tif": "Gtc"}}}], "grouping": "na"}
    NONCE = 1732000000000
    HASH = "0xb2c52a2c3da723ee415cf4b41740dae1eefa8587e2051c1e6997d4c7304b3a13"

    def setUp(self):
        super().setUp()
        self.signer = t.RemoteSigner(ADDRESS)

    def test_an_order_is_signed_by_clawup_with_its_action(self):
        import base64
        sig = self.signer.sign_l1_action(self.signer, self.ACTION, None, self.NONCE, None, True)
        self.assertEqual(sig["v"], 27)
        self.assertEqual(self.signer.why, "allowed by trade")
        (method, path, auth, body), = self.clawup.seen
        self.assertEqual((method, path, auth), ("POST", "/api/v1/runtime/wallet/sign", "Bearer gw-token"))
        self.assertEqual(body["typed_data"]["message"], {"source": "a", "connectionId": self.HASH})
        self.assertEqual(body["typed_data"]["domain"]["name"], "Exchange")
        self.assertEqual(json.loads(base64.b64decode(body["payload"])),
                         {"action": self.ACTION, "nonce": self.NONCE, "vaultAddress": None})

    def test_refused_by_a_mandate_says_why(self):
        self.clawup.sign = (403, {"error": "MANDATE_DENIED", "message": "refused by the mandate \"trade\""})
        with self.assertRaisesRegex(t.Refused, "refused by the mandate"):
            self.signer.sign_l1_action(self.signer, self.ACTION, None, self.NONCE, None, True)

    def test_only_one_order_is_signed(self):
        for action in ({"type": "cancel", "cancels": []},
                       {**self.ACTION, "orders": self.ACTION["orders"] * 2}):
            with self.assertRaises(t.Refused):
                self.signer.sign_l1_action(self.signer, action, None, self.NONCE, None, True)
        # A withdrawal or transfer comes to sign_message.
        with self.assertRaises(t.Refused):
            self.signer.sign_message(object())
        self.assertEqual(self.clawup.seen, [])

    def test_the_sdk_signs_through_the_signer(self):
        # The SDK's own order path, without Hyperliquid: _post_action is
        # stubbed, so what it would post is what is checked.
        import hyperliquid.exchange as sdk
        meta = {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}
        spot = {"universe": [], "tokens": []}
        ex = sdk.Exchange(self.signer, "https://api.hyperliquid.xyz", meta=meta, spot_meta=spot, perp_dexs=[""])
        posted = []
        ex._post_action = lambda action, signature, nonce: posted.append((action, signature)) or {"status": "ok"}
        saved, sdk.sign_l1_action = sdk.sign_l1_action, self.signer.sign_l1_action
        try:
            ex.order("BTC", True, 0.0125, 97000.5, {"limit": {"tif": "Gtc"}})
        finally:
            sdk.sign_l1_action = saved
        (action, signature), = posted
        self.assertEqual(action["orders"][0]["p"], "97000.5")
        self.assertEqual(signature["v"], 27)
        self.assertEqual(len(self.clawup.seen), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
