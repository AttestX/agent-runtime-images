"""Tests for trading on Hyperliquid after an analysis (shim/hl_trade.py) and
the wallet chat commands. Stdlib only: the SDK and the Privy CLI are not
needed, and nothing reaches the network."""

import importlib.util
import json
import os
import shutil
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
    def test_prices_as_the_sdk_prices_a_market_order(self):
        # 5% through, five significant figures, 6 - szDecimals decimals.
        self.assertEqual(t.limit_price(97000.5, True, 5), 101850.0)
        self.assertEqual(t.limit_price(97000.5, False, 5), 92150.0)
        self.assertEqual(t.limit_price(1.23456, True, 0), 1.2963)

    def test_size_rounds_down_to_the_step(self):
        self.assertEqual(t.order_size(100, 97000.5, 5), 0.00103)
        self.assertEqual(t.order_size(5, 97000.5, 3), 0.0)

    def test_parameters_are_what_trusted_rail_states(self):
        # trusted-rail's exchange/hyperliquid.json types this order (buy 0.0125
        # BTC at 97000.5) as exactly these parameters; see its tests/signing.rs.
        self.assertEqual(t.mandate_parameters("BTC", "buy", "97000.5", "0.0125"), {
            "account": "main", "amount_micro": 1212506250, "currency": "usdc",
            "limit_price_micro": 97000500000, "order_type": "limit", "quantity_micro": 12500,
            "rail": "hyperliquid", "side": "buy", "symbol": "btc", "venue": "api.hyperliquid.xyz",
        })

    def test_the_account_traded_for(self):
        old = t.ACCOUNT
        try:
            t.ACCOUNT = "0x00000000000000000000000000000000000000Aa"
            self.assertEqual(t.trading_account(), "0x00000000000000000000000000000000000000Aa")
            t.ACCOUNT = "not-an-address"
            with self.assertRaises(RuntimeError):
                t.trading_account()
        finally:
            t.ACCOUNT = old

    def test_testnet_is_a_switch(self):
        import subprocess
        code = "import hl_trade as t; print(t.MAINNET, t.EXCHANGE_HOST)"
        def host(value):
            env = dict(os.environ, TA_HL_NETWORK=value)
            return subprocess.run([sys.executable, "-c", code], cwd=SHIM_DIR, env=env,
                                  capture_output=True, text=True).stdout.strip()
        self.assertEqual(host("testnet"), "False api.hyperliquid-testnet.xyz")
        self.assertEqual(host(""), "True api.hyperliquid.xyz")
        self.assertEqual(host("TESTNET "), "False api.hyperliquid-testnet.xyz")

    def test_the_privy_body_is_only_what_privy_takes(self):
        typed = {"domain": {"chainId": 1337, "name": "Exchange"}, "types": {}, "primaryType": "Agent",
                 "message": {"source": "a", "connectionId": "0x" + "00" * 32}}
        body = t.privy_body(typed)
        # Privy refuses unknown keys ("Unrecognized key(s) in object", seen live).
        self.assertEqual(list(body), ["method", "params"])
        self.assertEqual(body["method"], "eth_signTypedData_v4")
        self.assertEqual(body["params"]["typed_data"]["primary_type"], "Agent")

    def test_the_action_header_keeps_the_key_order(self):
        import base64
        action = {"type": "order", "orders": [{"a": 0, "b": True, "p": "1", "s": "1", "r": False,
                                               "t": {"limit": {"tif": "Ioc"}}}], "grouping": "na"}
        shown = json.loads(base64.b64decode(t.action_header(action, 7)))
        self.assertEqual(shown, {"action": action, "nonce": 7, "vaultAddress": None})
        self.assertEqual(list(shown["action"]), ["type", "orders", "grouping"])
        self.assertEqual(list(shown["action"]["orders"][0]), ["a", "b", "p", "s", "r", "t"])

    def test_signature(self):
        sig = "0x" + "11" * 32 + "22" * 32 + "1b"
        self.assertEqual(t.split_signature(sig), {"r": "0x" + "11" * 32, "s": "0x" + "22" * 32, "v": 27})
        self.assertEqual(t.split_signature("11" * 64 + "01")["v"], 28)
        with self.assertRaises(ValueError):
            t.split_signature("0x1234")

    def test_no_mandate_no_trade(self):
        old = {k: os.environ.pop(k, None) for k in ("CLAWUP_API_KEY", "CLAWUP_MANDATE")}
        try:
            allowed, why = t.ask_mandate("hl-1", t.mandate_parameters("BTC", "buy", "1", "1"))
        finally:
            os.environ.update({k: v for k, v in old.items() if v is not None})
        self.assertFalse(allowed)
        self.assertIn("no ClawUp mandate", why)


class Wallet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hl-wallet-")
        self.old = t.WALLET_HOME
        t.WALLET_HOME = os.path.join(self.tmp, "wallet")

    def tearDown(self):
        t.WALLET_HOME = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_session_lives_in_the_state_dir_under_a_stable_id(self):
        first = t.privy_env()
        second = t.privy_env()
        self.assertEqual(first["HOME"], t.WALLET_HOME)
        self.assertTrue(first["PRIVY_SESSION_HOST"].startswith("ta-"))
        self.assertEqual(first["PRIVY_SESSION_HOST"], second["PRIVY_SESSION_HOST"])
        self.assertIn("--require", first["NODE_OPTIONS"])
        self.assertTrue(first["NODE_OPTIONS"].endswith("privy_host.cjs"))
        self.assertEqual(os.stat(t.WALLET_HOME).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(t.WALLET_HOME, "host-id")).st_mode & 0o777, 0o600)

    def test_wallet_commands(self):
        self.assertEqual(shim.wallet_command("connect wallet"), "connect")
        self.assertEqual(shim.wallet_command("  Connect my wallet! "), "connect")
        self.assertEqual(shim.wallet_command("连接钱包"), "connect")
        self.assertEqual(shim.wallet_command("wallet"), "status")
        self.assertEqual(shim.wallet_command("钱包？"), "status")
        self.assertEqual(shim.wallet_command("disconnect wallet"), "disconnect")
        for text in ("analyze BTC-USD", "what is a wallet", "wallet BTC"):
            self.assertIsNone(shim.wallet_command(text), text)


class ConnectWallet(unittest.TestCase):
    """The login outlives the chat turn: the URL and code come back at once,
    and the CLI keeps waiting for the owner's approval."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hl-login-")
        self.old = (t.PRIVY_CLI, t.WALLET_HOME)
        t.WALLET_HOME = os.path.join(self.tmp, "wallet")
        t.PRIVY_CLI = os.path.join(self.tmp, "privy")
        self.marker = os.path.join(self.tmp, "approved")
        # What the real CLI prints (0.3.1, `login`), then it polls until approved.
        with open(t.PRIVY_CLI, "w") as f:
            f.write(
                "#!/bin/sh\n"
                "printf '\\nOpen this URL to authorize:\\n\\n  https://auth.privy.io/device?code=ABCD-EFGH\\n\\n'\n"
                "printf 'Your code: ABCD-EFGH\\n\\nWaiting for approval...\\n'\n"
                f"while [ ! -f {self.marker} ]; do sleep 0.05; done\n"
                "printf '\\nLogged in successfully.\\n'\n"
            )
        os.chmod(t.PRIVY_CLI, 0o755)

    def tearDown(self):
        t.PRIVY_CLI, t.WALLET_HOME = self.old
        open(self.marker, "w").close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_url_and_code_come_back_while_the_cli_waits(self):
        login = shim.Login()
        reply = login.start()
        self.assertIn("https://auth.privy.io/device?code=ABCD-EFGH", reply)
        self.assertIn("code ABCD-EFGH", reply)
        self.assertTrue(login.pending())
        # Asking again shows the same login, not a second one.
        self.assertEqual(login.start(), reply)
        open(self.marker, "w").close()
        login.proc.wait(5)
        for _ in range(100):
            if login.result:
                break
            import time; time.sleep(0.02)
        self.assertEqual(login.result, "connected")
        self.assertFalse(login.pending())


class AfterAnalysis(unittest.TestCase):
    def setUp(self):
        self.old = (shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON)

    def tearDown(self):
        shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON = self.old

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
        self.assertIn("TA_TRADE_ORDER_USD", shim._trade(self.run_of("BTC-USD", "Buy")))

    def test_one_order_through_hl_trade(self):
        # A stand-in for the SDK venv: prints what hl_trade.py would.
        tmp = tempfile.mkdtemp(prefix="hl-python-")
        try:
            fake = os.path.join(tmp, "python")
            with open(fake, "w") as f:
                f.write('#!/bin/sh\necho \'{"ok": false, "message": "refused by the mandate: over_limit"}\'\n')
            os.chmod(fake, 0o755)
            shim.TRADE_ENABLED, shim.TRADE_ORDER_USD, shim.HL_PYTHON = True, 25.0, fake
            run = self.run_of("ETH-USD", "Sell")
            run.trade = shim._trade(run)
            self.assertEqual(run.trade, "not traded: refused by the mandate: over_limit")
            run.status = "done"
            self.assertIn("Hyperliquid: not traded", shim._result_text(run))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
