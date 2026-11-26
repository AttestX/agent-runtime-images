"""Behavior tests for the TradingAgents gateway shim.

Stdlib-only (the shim is stdlib-only); run with:
    python3 -m unittest discover -s tradingagents/tests -v

Covers the review-finding regressions:
  - final-decision reversal (last rating word in truncated text)
  - report adoption from another ticker's run
  - custom state dirs escaping the backed-up data root (CLI home bridge)
  - google key alias, unattended env defaults
  - follow-up turns ("show the report for NVDA") re-launching analyses
"""

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SHIM_PATH = Path(__file__).resolve().parent.parent / "shim" / "ta_shim.py"

_spec = importlib.util.spec_from_file_location("ta_shim", SHIM_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_ENV_KEYS = (
    "HOME", "TA_MODE", "TA_LLM_PROVIDER", "TA_QUICK_MODEL", "TA_DEEP_MODEL",
    "TA_BACKEND_URL", "TA_API_KEY_RAW", "TA_OUTPUT_LANGUAGE",
    "TA_MAX_DEBATE_ROUNDS", "TA_MAX_RISK_ROUNDS", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "TA_BIN",
)


def date_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class ShimTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ta-shim-test-")
        self._old_data_root = m.DATA_ROOT
        self._old_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        m.DATA_ROOT = self._tmp

    def tearDown(self):
        m.DATA_ROOT = self._old_data_root
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _touch(self, rel: str, content: str = "x") -> str:
        """Create a file (mtime = now, i.e. inside this run's window)."""
        path = os.path.join(self._tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def _run(self, ticker="AAPL", date=None) -> m.Run:
        run = m.Run(ticker, date or date_today())
        run.started_at = time.time()
        return run


# -- request parsing ------------------------------------------------------

class TestParseRequest(ShimTestCase):
    def test_bare_and_suffixed(self):
        self.assertEqual(m.parse_request("Analyze NVDA on 2026-11-20"),
                         {"ticker": "NVDA", "date": "2026-11-20"})
        self.assertEqual(m.parse_request("TATAMOTORS.NS please"),
                         {"ticker": "TATAMOTORS.NS", "date": date_today()})
        self.assertEqual(m.parse_request("600519.SS 2026-11-01"),
                         {"ticker": "600519.SS", "date": "2026-11-01"})
        self.assertEqual(m.parse_request("BTC-USD"),
                         {"ticker": "BTC-USD", "date": date_today()})

    def test_no_ticker(self):
        self.assertIsNone(m.parse_request("hello there"))
        self.assertIsNone(m.parse_request("THE"))
        self.assertIsNone(m.parse_request("fine, it.to go"))

    def test_tickers_inside_chinese_text(self):
        # A CJK character is a word character, so \b found no ticker here.
        self.assertEqual(m.parse_request("如何交易BTC吗？")["ticker"], "BTC-USD")
        self.assertEqual(m.parse_request("分析NVDA 2026-11-20"),
                         {"ticker": "NVDA", "date": "2026-11-20"})
        self.assertEqual(m.parse_request("0700.HK怎么样")["ticker"], "0700.HK")
        self.assertEqual(m.parse_request("日期:2026-11-19分析AAPL"),
                         {"ticker": "AAPL", "date": "2026-11-19"})

    def test_a_bare_coin_is_the_coin_in_dollars(self):
        for text in ("BTC", "analyze btc", "比特币怎么样", "Bitcoin today", "ticker: btc",
                     "I HOLD BTC", "分析 btc-usd"):
            self.assertEqual(m.parse_request(text)["ticker"], "BTC-USD", text)
        self.assertEqual(m.parse_request("以太坊")["ticker"], "ETH-USD")
        # A stock named first stays the stock.
        self.assertEqual(m.parse_request("NVDA vs BTC")["ticker"], "NVDA")


# -- decision extraction (regression: the reversal) ------------------------

class TestDecision(ShimTestCase):
    def test_header_rating_wins_over_body_words(self):
        """Header says Sell, the body is full of per-agent 'Buy' words —
        truncation or ordering must not flip the verdict."""
        body = ("- Rating: Sell\n\n" + "Buy BUY — moon. Final Decision: BUY\n" * 200)
        self._touch("logs/reports/AAPL_1/complete_report.md", body)
        run = self._run()
        m._collect_report(run)
        m._extract_decision(run)
        self.assertEqual(run.decision, "Sell")

    def test_no_authoritative_marker_means_no_decision(self):
        self._touch("logs/reports/AAPL_1/complete_report.md", "notes, no markers at all")
        run = self._run()
        m._collect_report(run)
        m._extract_decision(run)
        self.assertIsNone(run.decision)

    def test_pm_decision_line_at_tail(self):
        body = ("analysts disagree\n" * 300 + "\n## V. Portfolio Manager\n\nDecision: Hold\n")
        self._touch("logs/reports/AAPL_1/complete_report.md", body)
        run = self._run()
        m._collect_report(run)
        m._extract_decision(run)
        self.assertEqual(run.decision, "Hold")


# -- report selection (regression: foreign-ticker adoption) ----------------

class TestReportSelection(ShimTestCase):
    def test_foreign_ticker_report_is_rejected(self):
        """An AAPL run whose own report failed to save must NOT return the
        earlier NVDA report (its mtime is inside the grace window)."""
        self._touch("logs/reports/NVDA_20261118_000000/complete_report.md", "- Rating: Buy\n")
        self._touch("logs/AAPL/2026-11-19/reports/market_report.md", "market notes")
        run = self._run("AAPL", "2026-11-19")
        m._collect_report(run)
        # The NVDA report must be ignored; the run's own per-role report may
        # be used (exact ticker + date components).
        if run.report_path is not None:
            self.assertIn("AAPL", run.report_path)

    def test_same_date_other_ticker_is_rejected(self):
        """Reviewer repro: AAPL run on 2026-11-19 must not adopt NVDA's
        per-role tree written for the SAME date."""
        self._touch("logs/NVDA/2026-11-19/reports/final_trade_decision.md", "NVDA decision")
        run = self._run("AAPL", "2026-11-19")
        m._collect_report(run)
        self.assertIsNone(run.report_path)

    def test_one_letter_ticker_does_not_match_substrings(self):
        """Reviewer repro: ticker 'V' must not match paths merely containing
        the letter v (nvda_...)."""
        self._touch("logs/reports/NVDA_20261119_000000/complete_report.md", "- Rating: Buy\n")
        self._touch("logs/V/2026-11-19/reports/market_report.md", "V market notes")
        run = self._run("V", "2026-11-19")
        m._collect_report(run)
        self.assertIsNotNone(run.report_path)
        self.assertIn("/V/", run.report_path)

    def test_same_ticker_other_date_is_rejected(self):
        self._touch("logs/NVDA/2026-11-18/reports/market_report.md", "old date notes")
        run = self._run("NVDA", "2026-11-19")
        m._collect_report(run)
        self.assertIsNone(run.report_path)

    def test_only_foreign_reports_means_no_report(self):
        self._touch("logs/reports/NVDA_1/complete_report.md", "- Rating: Buy\n")
        run = self._run("AAPL")
        m._collect_report(run)
        self.assertIsNone(run.report_path)

    def test_same_ticker_complete_report_preferred(self):
        self._touch("logs/reports/AAPL_1/complete_report.md", "full")
        self._touch("logs/reports/AAPL_1/1_analysts/market.md", "market")
        run = self._run()
        m._collect_report(run)
        self.assertIsNotNone(run.report_path)
        self.assertIn("complete_report", run.report_path)


# -- CLI home bridge (regression: custom state dirs escape the backup) -----

class TestCliHome(ShimTestCase):
    def test_passthrough_when_default_layout(self):
        home = self._tmp
        os.makedirs(os.path.join(home, ".tradingagents"), exist_ok=True)
        m.DATA_ROOT = os.path.join(home, ".tradingagents")
        os.environ["HOME"] = home
        self.assertEqual(m._cli_home(), home)

    def test_bridge_for_custom_state_dir(self):
        custom = os.path.join(self._tmp, "custom-state")  # e.g. root-account /root/.openclaw
        os.makedirs(custom, exist_ok=True)
        m.DATA_ROOT = custom
        os.environ["HOME"] = "/does-not-matter"
        home = m._cli_home()
        self.assertNotEqual(home, "/does-not-matter")
        resolved = os.path.realpath(os.path.join(home, ".tradingagents"))
        self.assertEqual(resolved, os.path.realpath(custom))
        # The CLI writing through the bridge lands inside the state dir.
        probe = os.path.join(home, ".tradingagents", "memory", "probe.md")
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as fh:
            fh.write("x")
        self.assertTrue(os.path.isfile(os.path.join(custom, "memory", "probe.md")))


# -- provider env translation ----------------------------------------------

class TestCliEnv(ShimTestCase):
    def test_google_key_alias(self):
        os.environ.update({"TA_MODE": "native", "TA_LLM_PROVIDER": "google",
                           "GEMINI_API_KEY": "gm-key", "TA_QUICK_MODEL": "g", "TA_DEEP_MODEL": "g"})
        os.environ.pop("GOOGLE_API_KEY", None)
        env = m.build_cli_env()
        self.assertEqual(env["GOOGLE_API_KEY"], "gm-key")
        self.assertEqual(env["TRADINGAGENTS_LLM_PROVIDER"], "google")

    def test_compatible_mode(self):
        os.environ.update({"TA_MODE": "compatible", "TA_LLM_PROVIDER": "",
                           "TA_BACKEND_URL": "http://x/v1", "TA_API_KEY_RAW": "k",
                           "TA_QUICK_MODEL": "q", "TA_DEEP_MODEL": "d"})
        env = m.build_cli_env()
        self.assertEqual(env["TRADINGAGENTS_LLM_PROVIDER"], "openai_compatible")
        self.assertEqual(env["TRADINGAGENTS_LLM_BACKEND_URL"], "http://x/v1")
        self.assertEqual(env["OPENAI_COMPATIBLE_API_KEY"], "k")

    def test_unattended_defaults(self):
        os.environ.update({"TA_MODE": "native", "TA_LLM_PROVIDER": "openai",
                           "TA_QUICK_MODEL": "gpt", "TA_DEEP_MODEL": "gpt"})
        env = m.build_cli_env()
        self.assertEqual(env["TRADINGAGENTS_OUTPUT_LANGUAGE"], "English")
        self.assertEqual(env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"], "1")
        self.assertEqual(env["TRADINGAGENTS_MAX_RISK_ROUNDS"], "1")
        self.assertEqual(env["TRADINGAGENTS_NO_UPDATE_CHECK"], "1")


# -- follow-up turn resolution (regression: report queries re-running) -----

class TestResolveTurn(ShimTestCase):
    def test_matrix(self):
        P = m.parse_request

        # no current run
        self.assertEqual(m.resolve_turn("hello", P("hello"), None), ("help", None))
        self.assertEqual(m.resolve_turn("analyze NVDA", P("analyze NVDA"), None),
                         ("start", P("analyze NVDA")))

        # running run: always status, no second run
        running = self._run()
        running.status = "running"
        self.assertEqual(m.resolve_turn("progress", P("progress"), running), ("status", None))
        self.assertEqual(m.resolve_turn("analyze TSLA", P("analyze TSLA"), running), ("status", None))

        # finished run: the reviewer's scenario — same ticker, no date,
        # must NOT start another analysis
        done = self._run("NVDA", "2026-11-19")
        done.status = "done"
        self.assertEqual(m.resolve_turn("show the report for NVDA",
                                        P("show the report for NVDA"), done), ("result", None))
        self.assertEqual(m.resolve_turn("NVDA?", P("NVDA?"), done), ("result", None))
        self.assertEqual(m.resolve_turn("结果", P("结果"), done), ("result", None))

        # same ticker + same explicit date -> result
        self.assertEqual(m.resolve_turn("NVDA 2026-11-19 report",
                                        P("NVDA 2026-11-19 report"), done), ("result", None))

        # same ticker + different explicit date -> fresh run
        self.assertEqual(m.resolve_turn("analyze NVDA on 2026-11-18",
                                        P("analyze NVDA on 2026-11-18"), done),
                         ("start", P("analyze NVDA on 2026-11-18")))

        # re-run keywords with a ticker -> fresh run
        self.assertEqual(m.resolve_turn("re-run NVDA", P("re-run NVDA"), done),
                         ("start", P("re-run NVDA")))
        self.assertEqual(m.resolve_turn("重新分析 NVDA", P("重新分析 NVDA"), done),
                         ("start", P("重新分析 NVDA")))

        # reviewer repro: bare re-run commands (no ticker) must re-run the
        # LAST ticker/date, not replay the stale result
        self.assertEqual(m.resolve_turn("re-run", P("re-run"), done),
                         ("start", {"ticker": "NVDA", "date": "2026-11-19"}))
        self.assertEqual(m.resolve_turn("again", P("again"), done),
                         ("start", {"ticker": "NVDA", "date": "2026-11-19"}))
        self.assertEqual(m.resolve_turn("重新分析", P("重新分析"), done),
                         ("start", {"ticker": "NVDA", "date": "2026-11-19"}))

        # different ticker -> fresh run
        self.assertEqual(m.resolve_turn("analyze TSLA", P("analyze TSLA"), done),
                         ("start", P("analyze TSLA")))


# -- the stream: heartbeats a console shows, and ones a chat app must not --

FAKE_CLI = Path(__file__).resolve().parent / "fake_tradingagents.py"


class TestStream(ShimTestCase):
    def setUp(self):
        super().setUp()
        os.environ["TA_BIN"] = str(FAKE_CLI)
        os.environ["TA_FAKE_SECONDS"] = "2.5"
        self._old = (m.SLOT, m.HEARTBEAT_SECONDS, m.GATEWAY_TOKEN)
        m.SLOT, m.HEARTBEAT_SECONDS, m.GATEWAY_TOKEN = m.RunSlot(), 0.5, "test-token"
        self.server = m.ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        m.SLOT, m.HEARTBEAT_SECONDS, m.GATEWAY_TOKEN = self._old
        os.environ.pop("TA_FAKE_SECONDS", None)
        super().tearDown()

    def chat(self, text, headers=None):
        """(content shown to the user, SSE comment lines) of one streamed turn."""
        body = json.dumps({"stream": True, "messages": [{"role": "user", "content": text}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions", data=body,
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json", **(headers or {})})
        content, comments = "", []
        with urllib.request.urlopen(req, timeout=30) as resp:
            for raw in resp:
                line = raw.decode().rstrip("\n")
                if line.startswith(":"):
                    comments.append(line)
                elif line.startswith("data: ") and line != "data: [DONE]":
                    delta = json.loads(line[6:])["choices"][0]["delta"]
                    content += delta.get("content", "")
        return content, comments

    def test_console_sees_progress(self):
        content, comments = self.chat("Analyze NVDA on 2026-11-20")
        self.assertIn("running for", content)
        self.assertIn("ask 'progress'", content)
        self.assertIn("Final decision: Buy", content)
        self.assertEqual(comments, [])

    def test_a_chat_app_sees_only_the_answer(self):
        content, comments = self.chat("Analyze NVDA on 2026-11-20", {"X-Hermes-Session-Id": "telegram:42"})
        self.assertNotIn("running for", content)
        self.assertNotIn("progress", content)
        self.assertIn("I'll send the result here", content)
        self.assertIn("Final decision: Buy", content)
        self.assertTrue(comments and all(c.startswith(": running for") for c in comments), comments)


# -- the channel gateway beside the shim ----------------------------------

def _lines(path: str) -> list[str]:
    try:
        with open(path) as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


def _wait(predicate, seconds: float = 10) -> None:
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.1)


class TestChannelGateway(ShimTestCase):
    def fake(self, body: str) -> str:
        path = os.path.join(self._tmp, "channel-gateway")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_absent_from_the_image_means_not_started(self):
        gw = m.ChannelGateway(os.path.join(self._tmp, "missing"), self._tmp, 18789)
        self.assertFalse(gw.start())

    def test_started_with_its_directory_and_this_shim_as_the_agent_then_stopped(self):
        seen = os.path.join(self._tmp, "seen")
        fake = self.fake(f"echo \"$CHANNEL_GATEWAY_HOME $CHANNEL_GATEWAY_AGENT_URL\" >> {seen}\n"
                         "trap 'exit 0' TERM\nwhile :; do sleep 0.1; done\n")
        home = os.path.join(self._tmp, "channels")
        gw = m.ChannelGateway(fake, home, 18795)
        self.assertTrue(gw.start())
        _wait(lambda: _lines(seen))
        self.assertEqual(_lines(seen), [f"{home} http://127.0.0.1:18795"])
        gw.stop()
        gw.wait(5)
        self.assertIsNotNone(gw.proc.returncode)
        time.sleep(1.5)
        self.assertEqual(len(_lines(seen)), 1)  # not restarted once stopped

    def test_restarted_when_it_exits(self):
        seen = os.path.join(self._tmp, "seen")
        gw = m.ChannelGateway(self.fake(f"echo started >> {seen}\nexit 3\n"), self._tmp, 18789)
        gw.start()
        _wait(lambda: len(_lines(seen)) >= 2)
        gw.stop()
        self.assertGreaterEqual(len(_lines(seen)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
