"""Tests for the owner's panel (a chat request with a `panel` object; ClawUp's
design/agent-panel.md): what it shows, the trading settings, and funds moved
only by the owner's approval. Stdlib only: a stand-in ClawUp and a stand-in
Hyperliquid on localhost, so nothing reaches the network."""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SHIM_DIR = Path(__file__).resolve().parent.parent / "shim"
sys.path.insert(0, str(SHIM_DIR))
import hl_trade as t  # noqa: E402

_spec = importlib.util.spec_from_file_location("ta_shim", SHIM_DIR / "ta_shim.py")
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)

FAKE_CLI = Path(__file__).resolve().parent / "fake_tradingagents.py"
ADDRESS = "0x" + "ab" * 20
OWNER = "0x" + "Cd" * 20
SIGNATURE = "0x" + "11" * 64 + "1b"
APPROVAL = "0123456789abcdef0123456789abcdef"


class StandIns:
    """ClawUp's runtime wallet routes and Hyperliquid's /info and /exchange,
    on one localhost server. What the agent sends is recorded; the answers
    are set per test."""

    def __init__(self):
        self.seen = []
        self.wallet = (200, {"address": ADDRESS})
        self.decision = {"status": "pending", "expires_at": int(time.time()) + 600}
        self.perps = {"marginSummary": {"accountValue": "12.5"}, "withdrawable": "10.25",
                      "assetPositions": [{"position": {"coin": "BTC", "szi": "0.001"}},
                                         {"position": {"coin": "ETH", "szi": "0.0"}}]}
        self.spot = {"balances": [{"coin": "USDC", "total": "3.5", "hold": "0.5"},
                                  {"coin": "HYPE", "total": "2.0", "hold": "0"},
                                  {"coin": "PURR", "total": "0.0", "hold": "0"}]}
        self.exchange = {"status": "ok", "response": {"type": "default"}}
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
                if self.path == "/api/v1/runtime/wallet":
                    self.answer(*outer.wallet)
                elif self.path == f"/api/v1/runtime/wallet/approvals/{APPROVAL}":
                    if outer.decision is None:
                        self.answer(404, {"error": "APPROVAL_NOT_FOUND", "message": "No such approval"})
                    else:
                        self.answer(200, outer.decision)
                else:
                    self.answer(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.seen.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path == "/api/v1/runtime/wallet/approvals":
                    self.answer(201, {"id": APPROVAL, "expires_at": int(time.time()) + 600})
                elif self.path == "/info":
                    self.answer(200, outer.perps if body["type"] == "clearinghouseState" else outer.spot)
                elif self.path == "/exchange":
                    self.answer(200, outer.exchange)
                else:
                    self.answer(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def posted(self, path):
        return [body for method, p, _auth, body in self.seen if method == "POST" and p == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ta-panel-test-")
        self.stand = StandIns()
        self.old = (t.CLAWUP_API_URL, t.BASE_URL, t.ACCOUNT, os.environ.get("OPENCLAW_GATEWAY_TOKEN"),
                    shim.DATA_ROOT, shim.APPROVAL_POLL_SECONDS, shim.TRANSFERS,
                    shim.TRADE_ENABLED, shim.TRADE_ORDER_USD)
        t.CLAWUP_API_URL = t.BASE_URL = self.stand.url
        t.ACCOUNT = ""
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = "gw-token"
        shim.DATA_ROOT = self.tmp
        shim.APPROVAL_POLL_SECONDS = 0.05
        shim.TRANSFERS = shim.Transfers()
        shim.TRADE_ENABLED, shim.TRADE_ORDER_USD = False, 0.0

    def tearDown(self):
        shim.TRANSFERS.stop()
        self.stand.close()
        (t.CLAWUP_API_URL, t.BASE_URL, t.ACCOUNT, token, shim.DATA_ROOT, shim.APPROVAL_POLL_SECONDS,
         shim.TRANSFERS, shim.TRADE_ENABLED, shim.TRADE_ORDER_USD) = self.old
        if token is None:
            os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)
        else:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = token
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def items(panel):
        return [item for section in panel["sections"] for item in section["items"]]

    def wait_for(self, status, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if shim.TRANSFERS.latest and shim.TRANSFERS.latest["status"] == status:
                return
            time.sleep(0.02)
        self.fail(f"the transfer is {shim.TRANSFERS.latest}, not {status}")


class Shows(PanelCase):
    def test_the_wallet_its_funds_and_the_settings(self):
        panel = shim.panel_view("en")
        self.assertEqual(panel["title"], "Trading")
        items = self.items(panel)
        self.assertIn({"type": "copy", "label": "Address", "value": ADDRESS}, items)
        perps = next(i for i in items if i.get("label") == "Perps")
        self.assertEqual(perps["value"], "12.5 USDC")
        self.assertIn("10.25 USDC withdrawable", perps["help"])
        self.assertEqual(next(i for i in items if i.get("label") == "Spot")["value"], "3 USDC")  # 0.5 on hold
        self.assertEqual(next(i for i in items if i.get("label") == "Positions")["value"], "BTC 0.001")
        self.assertEqual(next(i for i in items if i.get("label") == "Other spot")["value"], "HYPE 2.0")
        toggle = next(i for i in items if i["type"] == "toggle")
        self.assertEqual((toggle["id"], toggle["value"]), ("trade", False))
        actions = [i["action"] for i in items if i["type"] == "button"]
        self.assertEqual(actions, ["save", "withdraw", "to_perps", "to_spot"])
        # Balances were read for the wallet's own account.
        users = {b["user"] for b in self.stand.posted("/info")}
        self.assertEqual(users, {ADDRESS})

    def test_in_the_owners_language(self):
        panel = shim.panel_view("zh")
        self.assertEqual(panel["title"], "交易")
        self.assertEqual(shim.panel_view("fr")["title"], "Trading")
        for key, texts in shim.PANEL_TEXT.items():
            self.assertEqual(len(texts), 3, key)
            for locale in shim.LOCALES:
                shim.tr(locale, key, reason="r", amount="1", to="0x", account="0x", what="w")

    def test_no_wallet_yet(self):
        self.stand.wallet = (409, {"error": "WALLET_NOT_CREATED", "message": "no wallet yet"})
        panel = shim.panel_view("en")
        texts = [i["text"] for i in self.items(panel) if i["type"] == "text"]
        self.assertTrue(any("no wallet yet" in x for x in texts), texts)
        self.assertEqual([s["title"] for s in panel["sections"]], ["Wallet on Hyperliquid", "Trading"])

    def test_clawup_unreachable_is_not_a_missing_wallet(self):
        t.CLAWUP_API_URL = "http://127.0.0.1:9"
        texts = [i for i in self.items(shim.panel_view("en")) if i["type"] == "text"]
        self.assertEqual(texts[0]["tone"], "error")
        self.assertIn("unreachable", texts[0]["text"])

    def test_trading_for_another_account_moves_nothing_from_here(self):
        t.ACCOUNT = OWNER
        panel = shim.panel_view("en")
        self.assertEqual([s["title"] for s in panel["sections"]], ["Wallet on Hyperliquid", "Trading"])
        self.assertEqual({b["user"] for b in self.stand.posted("/info")}, {OWNER})
        status, answer = shim.panel_act("withdraw", {"amount": "1", "to": OWNER}, "en")
        self.assertEqual(status, 409)
        self.assertEqual(self.stand.posted("/api/v1/runtime/wallet/approvals"), [])


class Settings(PanelCase):
    def test_saved_in_the_state_dir_and_used_for_trading(self):
        self.assertEqual(shim.trade_settings(), (False, 0.0))
        status, answer = shim.panel_act("save", {"trade": True, "order_usd": "25"}, "en")
        self.assertEqual((status, answer["tone"]), (200, "ok"))
        with open(os.path.join(self.tmp, shim.SETTINGS_FILE)) as fh:
            self.assertEqual(json.load(fh), {"trade": True, "order_usd": 25.0})
        self.assertEqual(shim.trade_settings(), (True, 25.0))
        self.assertIn("$25", shim.wallet_reply("status"))
        items = self.items(shim.panel_view("en"))
        self.assertTrue(next(i for i in items if i["type"] == "toggle")["value"])
        self.assertEqual(next(i for i in items if i.get("id") == "order_usd")["value"], "25")

    def test_the_defaults_until_saved(self):
        shim.TRADE_ENABLED, shim.TRADE_ORDER_USD = True, 10.0
        self.assertEqual(shim.trade_settings(), (True, 10.0))
        shim.panel_act("save", {"trade": False, "order_usd": ""}, "en")
        self.assertEqual(shim.trade_settings(), (False, 0.0))

    def test_refused(self):
        for inputs in ({"trade": True, "order_usd": "0"}, {"order_usd": "-1"}, {"order_usd": "nan"},
                       {"order_usd": "1e9"}, {"order_usd": "ten"}, {"order_usd": "9.99"},
                       {"order_usd": "10001"}):
            status, answer = shim.panel_act("save", inputs, "en")
            self.assertEqual(status, 400, inputs)
            self.assertTrue(answer["message"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, shim.SETTINGS_FILE)))
        # "trade" must be the toggle's bool, not a truthy string.
        shim.panel_act("save", {"trade": "yes", "order_usd": "15"}, "en")
        self.assertEqual(shim.trade_settings(), (False, 15.0))


class MovesFundsByApproval(PanelCase):
    def withdraw(self, amount="5.50", to=OWNER):
        return shim.panel_act("withdraw", {"amount": amount, "to": to}, "en")

    def test_asks_the_owner_then_sends_what_was_approved(self):
        status, answer = self.withdraw()
        self.assertEqual((status, answer["approval"]), (200, APPROVAL))
        (asked,) = self.stand.posted("/api/v1/runtime/wallet/approvals")
        typed = asked["typed_data"]
        self.assertEqual(typed["primaryType"], "HyperliquidTransaction:UsdSend")
        self.assertEqual(typed["message"]["destination"], OWNER.lower())
        self.assertEqual(typed["message"]["amount"], "5.5")
        self.assertEqual(typed["message"]["hyperliquidChain"], "Mainnet")
        self.assertIn("5.5 USDC", asked["summary"])
        self.assertIn(OWNER.lower(), asked["summary"])
        # Nothing is sent while it waits.
        time.sleep(0.2)
        self.assertEqual(self.stand.posted("/exchange"), [])
        texts = [i["text"] for i in self.items(shim.panel_view("en")) if i["type"] == "text"]
        self.assertTrue(any(x.startswith("Waiting for your approval") for x in texts), texts)

        self.stand.decision = {"status": "approved", "signature": SIGNATURE, "expires_at": 0}
        self.wait_for("sent")
        (sent,) = self.stand.posted("/exchange")
        self.assertEqual(sent["action"]["type"], "usdSend")
        self.assertEqual({k: sent["action"][k] for k in typed["message"]}, typed["message"])
        self.assertEqual(sent["nonce"], typed["message"]["time"])
        self.assertEqual(sent["signature"], t.split_signature(SIGNATURE))
        texts = [i["text"] for i in self.items(shim.panel_view("en")) if i["type"] == "text"]
        self.assertTrue(any(x.startswith("Done: Send 5.5 USDC") for x in texts), texts)

    def test_declined_or_expired_sends_nothing(self):
        self.withdraw()
        self.stand.decision = {"status": "declined", "expires_at": 0}
        self.wait_for("declined")
        self.withdraw()
        self.stand.decision = None  # ClawUp has forgotten it
        self.wait_for("expired")
        self.assertEqual(self.stand.posted("/exchange"), [])

    def test_hyperliquids_refusal_is_shown(self):
        self.stand.exchange = {"status": "err", "response": "Insufficient balance"}
        self.withdraw()
        self.stand.decision = {"status": "approved", "signature": SIGNATURE, "expires_at": 0}
        self.wait_for("failed")
        self.assertIn("Insufficient balance", shim.TRANSFERS.latest["reason"])

    def test_refused_before_asking(self):
        for amount, to in (("10.26", OWNER), ("0", OWNER), ("1.1234567", OWNER), ("-1", OWNER),
                           ("abc", OWNER), ("1", "0x12"), ("1", ""), ("1", ADDRESS.upper().replace("0X", "0x"))):
            status, answer = self.withdraw(amount, to)
            self.assertEqual(status, 400, (amount, to))
            self.assertTrue(answer["message"])
        self.assertIn("10.25", self.withdraw("11")[1]["message"])
        self.assertEqual(self.stand.posted("/api/v1/runtime/wallet/approvals"), [])
        self.assertEqual(shim.panel_act("burn", {}, "en")[0], 400)

    def test_between_spot_and_perps(self):
        # Spot holds 3.5 USDC, 0.5 of it on hold.
        self.assertEqual(shim.panel_act("to_perps", {"move_amount": "3.01"}, "en")[0], 400)
        status, answer = shim.panel_act("to_perps", {"move_amount": "3"}, "en")
        self.assertEqual(status, 200)
        (asked,) = self.stand.posted("/api/v1/runtime/wallet/approvals")
        self.assertEqual(asked["typed_data"]["primaryType"], "HyperliquidTransaction:UsdClassTransfer")
        self.assertEqual(asked["typed_data"]["message"]["toPerp"], True)
        self.assertEqual(asked["typed_data"]["message"]["amount"], "3")
        status, _ = shim.panel_act("to_spot", {"move_amount": "10.25"}, "en")
        self.assertEqual(status, 200)
        self.assertEqual(self.stand.posted("/api/v1/runtime/wallet/approvals")[1]["typed_data"]["message"]["toPerp"], False)

    def test_amounts(self):
        self.assertEqual(t.usdc("005.500"), "5.5")
        self.assertEqual(t.usdc("7"), "7")
        self.assertEqual(t.usdc("0.000001"), "0.000001")
        for bad in ("0", "0.0", "", " ", "1.", ".5", "1e3", "1,5", "-2", "1.0000001"):
            self.assertIsNone(t.usdc(bad), bad)
        self.assertEqual(t.micro("12.3456789"), 12_345_678)
        self.assertEqual(t.micro("-0.5"), -500_000)
        self.assertEqual(t.micro(None), 0)
        self.assertEqual(t.shown(12_500_000), "12.5")
        self.assertEqual(t.shown(3_000_000), "3")
        self.assertEqual(t.shown(0), "0")


class OverChat(PanelCase):
    """The panel arrives on /v1/chat/completions, and is answered even while
    an analysis runs."""

    def setUp(self):
        super().setUp()
        os.environ["TA_BIN"] = str(FAKE_CLI)
        os.environ["TA_FAKE_SECONDS"] = "3"
        self.old_shim = (shim.SLOT, shim.GATEWAY_TOKEN)
        shim.SLOT, shim.GATEWAY_TOKEN = shim.RunSlot(), "test-token"
        self.server = shim.ThreadingHTTPServer(("127.0.0.1", 0), shim.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        run = shim.SLOT.get()
        if run is not None and run.proc is not None and run.proc.poll() is None:
            run.proc.kill()
        self.server.shutdown()
        self.server.server_close()
        shim.SLOT, shim.GATEWAY_TOKEN = self.old_shim
        for key in ("TA_BIN", "TA_FAKE_SECONDS"):
            os.environ.pop(key, None)
        super().tearDown()

    def post(self, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_answered_during_an_analysis_and_never_a_message(self):
        status, _ = self.post({"messages": [{"role": "user", "content": "Analyze BTC-USD on 2026-11-20"}]})
        self.assertEqual(status, 200)
        self.assertEqual(shim.SLOT.get().status, "running")
        started = time.time()
        status, panel = self.post({"model": "panel", "messages": [], "stream": False, "panel": {"locale": "es"}})
        self.assertLess(time.time() - started, 2)
        self.assertEqual(status, 200)
        self.assertEqual(panel["title"], "Trading")
        self.assertNotIn("choices", panel)
        self.assertEqual(shim.SLOT.get().ticker, "BTC-USD")  # the panel started nothing
        status, answer = self.post({"messages": [], "panel": {"action": "save", "locale": "zh",
                                                              "inputs": {"trade": True, "order_usd": "12"}}})
        self.assertEqual((status, answer["message"]), (200, "已保存。"))
        status, answer = self.post({"messages": [], "panel": {"action": "save", "inputs": {"order_usd": "x"}}})
        self.assertEqual(status, 400)
        self.assertIn("order size", answer["message"])

    def test_a_panel_cannot_hide_a_chat(self):
        for body in ({"messages": [{"role": "user", "content": "Analyze NVDA"}], "panel": {}},
                     {"messages": [], "stream": True, "panel": {}},
                     {"messages": [], "panel": "view"}):
            status, _ = self.post(body)
            self.assertEqual(status, 400, body)
        self.assertIsNone(shim.SLOT.get())


try:
    import hyperliquid  # noqa: F401
    HAVE_SDK = True
except ImportError:
    HAVE_SDK = False


@unittest.skipUnless(HAVE_SDK, "needs hyperliquid-python-sdk (run with /opt/hl/bin/python)")
class AsTheSdkSigns(unittest.TestCase):
    """The typed data asked for approval signs what the SDK's would. The SDK
    puts the whole action in `message`; EIP-712 signs only the fields its
    types name, and those are all this shows the owner."""

    def same(self, typed, sdk):
        from eth_account.messages import encode_typed_data
        self.assertEqual(typed["domain"], sdk["domain"])
        self.assertEqual(typed["types"], sdk["types"])
        self.assertEqual(typed["message"], {k: sdk["message"][k] for k in typed["message"]})
        self.assertEqual(encode_typed_data(full_message=typed), encode_typed_data(full_message=sdk))

    def test_usd_send(self):
        from hyperliquid.utils.signing import USD_SEND_SIGN_TYPES, user_signed_payload
        action, typed = t.usd_send(OWNER, "5.5", 1732000000000)
        self.same(typed, user_signed_payload("HyperliquidTransaction:UsdSend", USD_SEND_SIGN_TYPES, dict(action)))

    def test_usd_class_transfer(self):
        from hyperliquid.utils.signing import USD_CLASS_TRANSFER_SIGN_TYPES, user_signed_payload
        action, typed = t.usd_class_transfer("3", True, 1732000000000)
        self.same(typed, user_signed_payload(
            "HyperliquidTransaction:UsdClassTransfer", USD_CLASS_TRANSFER_SIGN_TYPES, dict(action)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
