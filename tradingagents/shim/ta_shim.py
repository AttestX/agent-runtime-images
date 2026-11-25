#!/usr/bin/env python3
"""ClawUp gateway shim for TradingAgents.

Adapts ClawUp's runtime gateway contract to the TradingAgents CLI:

  * listens on :18789 (the port the provisioner hardcodes for every runtime)
  * GET  /ready                  -> {"ready": true}
  * POST /v1/chat/completions    -> OpenAI-compatible, Bearer-authenticated
  * GET  /v1/ta/status           -> machine-readable run status (same auth)

Design rules:

1. **Single-flight, decoupled from connections.** One analysis per bot at a
   time. The runner is a daemon thread + subprocess; if the SSE client (or
   the backend's 120s idle timeout) drops the connection, the analysis keeps
   running and the next chat turn picks up progress or the result.
2. **Drive the documented non-interactive CLI**, not the Python API:
   `tradingagents --ticker T --date D --save --no-show --no-html`.  Config is
   passed through the documented TRADINGAGENTS_* environment variables only,
   so the shim survives upstream Python-API churn.
3. **Stdlib only.**  The image already carries TradingAgents' heavy
   dependency tree; the shim adds nothing to it.  Trading (optional, off
   unless TA_TRADE_HYPERLIQUID is set) runs in hl_trade.py, in its own venv
   with the Hyperliquid SDK, signed by the owner's Privy wallet.

Configuration arrives as TA_* env vars exported by the provisioner's
bootstrap script (see runtime-provisioner/scripts.go, buildTradingAgentsBootstrap).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config from the environment (injected by the provisioner bootstrap)
# ---------------------------------------------------------------------------

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "18789"))
GATEWAY_TOKEN = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

# Data root (memory log, checkpoints, reports).  The provisioner points
# TA_STATE_DIR at the bot's state dir (same path the OSS backup/restore
# scripts tar), falling back to $HOME/.tradingagents for standalone runs.
DATA_ROOT = os.environ.get("TA_STATE_DIR") or os.path.join(
    os.environ.get("HOME", "/root"), ".tradingagents"
)

HEARTBEAT_SECONDS = float(os.environ.get("TA_SHIM_HEARTBEAT_SECONDS", "30"))
REPORT_MAX_CHARS = int(os.environ.get("TA_SHIM_REPORT_MAX_CHARS", "4000"))
REPORT_TAIL_CHARS = int(os.environ.get("TA_SHIM_REPORT_TAIL_CHARS", "3000"))
LOG_TAIL_LINES = 200
MODEL_LABEL = "tradingagents-shim"

# Trading on Hyperliquid after an analysis (hl_trade.py). Off by default.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hl_trade  # noqa: E402  (stdlib at import; the SDK runs in HL_PYTHON)

TRADE_ENABLED = os.environ.get("TA_TRADE_HYPERLIQUID", "").strip().lower() in ("1", "true", "yes")
try:
    TRADE_ORDER_USD = float(os.environ.get("TA_TRADE_ORDER_USD", "0") or 0)
except ValueError:
    TRADE_ORDER_USD = 0.0
HL_PYTHON = os.environ.get("TA_HL_PYTHON", "/opt/hl/bin/python")
TRADE_TIMEOUT_SECONDS = 120


def ta_config() -> dict:
    """Read the TA_* bootstrap vars.

    Deliberately read at call time (not import time): the provisioner's
    bootstrap exports them before exec'ing the shim, but reading lazily
    keeps the module importable in unit tests and resilient to env being
    settled a moment late.
    """
    return {
        "mode": os.environ.get("TA_MODE", "native"),  # native | compatible
        "llm_provider": os.environ.get("TA_LLM_PROVIDER", ""),
        "quick_model": os.environ.get("TA_QUICK_MODEL", ""),
        "deep_model": os.environ.get("TA_DEEP_MODEL", ""),
        "backend_url": os.environ.get("TA_BACKEND_URL", ""),
        "api_key": os.environ.get("TA_API_KEY_RAW", ""),
        "output_language": os.environ.get("TA_OUTPUT_LANGUAGE", ""),
        "max_debate_rounds": os.environ.get("TA_MAX_DEBATE_ROUNDS", ""),
        "max_risk_rounds": os.environ.get("TA_MAX_RISK_ROUNDS", ""),
        "analysts": os.environ.get("TA_ANALYSTS", ""),
        "bin": _resolve_ta_bin(),
    }


def _resolve_ta_bin() -> str:
    """Locate the TradingAgents CLI.

    Prefer the absolute venv path: the container is started via a login
    shell (`sh -lc`), and /etc/profile resets PATH before our bootstrap's
    re-export runs in some environments — a bare-name execve would then
    ENOENT (and the CLI's shebang points at /opt/venv/bin/python, so the
    absolute path also pins the interpreter).  TA_BIN env overrides for
    tests.
    """
    override = os.environ.get("TA_BIN", "").strip()
    if override:
        return override
    preferred = "/opt/venv/bin/tradingagents"
    if os.path.isfile(preferred) and os.access(preferred, os.X_OK):
        return preferred
    return "tradingagents"

log_lock = threading.Lock()


def _log(msg: str) -> None:
    with log_lock:
        print(f"[ta-shim] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Request parsing (ticker + date)
# ---------------------------------------------------------------------------

STOPWORDS = {
    "ANALYZE", "ANALYSIS", "STOCK", "MARKET", "TRADING", "TRADE", "REPORT",
    "PLEASE", "WANT", "NEED", "GIVE", "SHOW", "WHAT", "WHICH", "WHEN", "HOW",
    "RUN", "START", "THE", "A", "AN", "USD", "CNY", "HOLD", "BUY", "SELL",
    "TODAY", "YESTERDAY", "DATE", "TIME", "CHECK", "HELP", "STATUS", "ME",
    "OK", "AND", "FOR", "WITH", "THIS", "THAT", "IT", "IS", "BE", "GO",
}

EXPLICIT_TICKER_RE = re.compile(
    r"(?:ticker|symbol|代码|股票代码|股票)\s*[:：=]?\s*([A-Za-z][A-Za-z0-9.\-]{0,15})",
    re.I,
)
EXPLICIT_DATE_RE = re.compile(
    r"(?:date|日期|交易日)\s*[:：=]?\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.I
)
BARE_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
# Exchange-suffixed tickers are unambiguous, so the base may be long
# (India name-based NSE/BSE tickers: RELIANCE.NS, TATAMOTORS.NS).
SUFFIXED_TICKER_RE = re.compile(
    r"\b[A-Z0-9]{1,12}\.(?:HK|SS|SZ|NS|BO|TO|AX|L|T)\b|\b[A-Z]{2,5}-USD\b"
)
# Bare all-caps tokens: classic 1-5 char US/EU/JP style (AAPL, SPY, AZN).
# Longer bare tokens are NOT guessed — use the explicit form instead.
BARE_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
DECISION_MARKER_RE = re.compile(r"^\s*[-*]?\s*(?:Final\s+Decision|Decision|Rating):\s*(.+?)\s*$", re.M | re.I)


def parse_request(text: str) -> dict | None:
    """Extract {ticker, date} from a chat message, or None when undecipherable."""
    text = text or ""
    ticker: str | None = None
    date: str | None = None

    m = EXPLICIT_TICKER_RE.search(text)
    if m:
        ticker = m.group(1).upper().strip(".")
    else:
        # Exchange-suffixed first (unambiguous, any base length), then
        # conservative bare 1-5 char tokens with a stopword filter.
        m = SUFFIXED_TICKER_RE.search(text)
        if m:
            ticker = m.group(0)
        else:
            for cand in BARE_TICKER_RE.findall(text):
                if cand in STOPWORDS:
                    continue
                ticker = cand
                break

    m = EXPLICIT_DATE_RE.search(text) or BARE_DATE_RE.search(text)
    if m:
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    else:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not ticker or len(ticker) > 16:
        return None
    return {"ticker": ticker, "date": date}


# "re-run it" style intent — overrides the cached-result shortcut below.
REANALYZE_RE = re.compile(
    r"\b(re-?run|re-?analyze|reanalysis|refresh|again)\b|重新|再来|再(跑|分析|来)", re.I
)


def _explicit_date_in(text: str) -> str | None:
    m = EXPLICIT_DATE_RE.search(text) or BARE_DATE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def resolve_turn(text: str, parsed: dict | None, current: "Run | None") -> tuple:
    """Decide what a chat turn does (pure function; testable without HTTP).

    Returns (action, target).  action is one of:
      "help"   — nothing to do: no ticker and no run to report on
      "status" — a run is in flight; report its progress
      "result" — show the finished run's result (or its error)
      "start"  — start a new run
    target is the {ticker, date} to start (only for "start"): the parsed one,
    or the previous run's when a bare re-run command ("re-run", "again",
    "重新分析") names no ticker.  None otherwise.

    Follow-up policy (review finding: "show the report for NVDA" must NOT
    burn another run): after a finished run, a message naming the same
    ticker without an explicit *different* date returns the cached result;
    an explicit different date or a re-run intent starts a fresh analysis.
    """
    if current is None:
        return ("help", None) if parsed is None else ("start", parsed)
    if current.status == "running":
        return "status", None
    reanalyze = bool(REANALYZE_RE.search(text))
    if parsed is None:
        if reanalyze:
            # Bare "re-run" / "again" / "重新分析" — re-run the last
            # ticker/date instead of replaying the stale result.
            return "start", {"ticker": current.ticker, "date": current.date}
        return "result", None
    if parsed["ticker"].lower() != current.ticker.lower():
        return "start", parsed
    if reanalyze:
        return "start", parsed
    explicit = _explicit_date_in(text)
    if explicit is not None and explicit != current.date:
        return "start", parsed
    return "result", None


def last_user_text(messages) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            if parts:
                return "\n".join(parts)
    return ""


HELP_TEXT = (
    "Please tell me which ticker and date to analyze, e.g.:\n"
    "  • Analyze NVDA on 2026-11-20\n"
    "  • ticker: 600519.SS  date: 2026-11-19\n"
    "  • How is BTC-USD doing today?\n"
    "Any market Yahoo Finance covers works (US, HK .HK, A-shares .SS/.SZ, crypto -USD, …).\n"
    "An analysis runs the full multi-agent pipeline (analyst team → bull/bear debate → "
    "trader → risk team → portfolio manager) and typically takes 10-40 minutes. "
    "You can ask me for progress at any time.\n"
    "After a run finishes, asking about the same ticker returns its saved result — "
    "to run it again, name an explicit date or say 're-run'.\n"
    "Trading on Hyperliquid (when the owner has turned it on): 'connect wallet' signs in "
    "your Privy wallet, 'wallet' shows it, 'disconnect wallet' signs out."
)


# ---------------------------------------------------------------------------
# Run lifecycle (single-flight)
# ---------------------------------------------------------------------------


class Run:
    __slots__ = (
        "ticker", "date", "started_at", "finished_at", "status",
        "proc", "log", "error", "decision", "report_path", "report_head",
        "report_tail", "workspace_path", "trade",
    )

    def __init__(self, ticker: str, date: str) -> None:
        self.ticker = ticker
        self.date = date
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.status = "running"  # running | done | error
        self.proc: subprocess.Popen | None = None
        self.log: deque = deque(maxlen=LOG_TAIL_LINES)
        self.error: str | None = None
        self.decision: str | None = None
        self.report_path: str | None = None
        self.report_head: str | None = None
        self.report_tail: str | None = None
        self.workspace_path: str | None = None
        self.trade: str | None = None

    def elapsed(self) -> str:
        end = self.finished_at or time.time()
        s = int(end - self.started_at)
        return f"{s // 60:02d}:{s % 60:02d}"

    def status_text(self) -> str:
        return f"{self.ticker} {self.date} analysis in progress, running for {self.elapsed()}."


class RunSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.run: Run | None = None

    def with_lock(self):
        return self._lock

    def get(self) -> Run | None:
        with self._lock:
            return self.run

    def claim(self, ticker: str, date: str) -> tuple[Run, bool]:
        """Return (run, started_now). Refuses while a run is in flight."""
        with self._lock:
            if self.run is not None and self.run.status == "running":
                return self.run, False
            run = Run(ticker, date)
            self.run = run
            return run, True


SLOT = RunSlot()


def build_cli_env() -> dict:
    """Translate TA_* bootstrap vars into the documented TRADINGAGENTS_* env.

    The CLI refuses to start without a terminal unless EVERY setup question
    is answered (cli/selections.py unattended_gaps): language, both round
    counts, provider, and at least one model.  Defaults are applied here so
    the shim is self-sufficient; TA_* bootstrap vars override them.
    """
    cfg = ta_config()
    env = dict(os.environ)
    env["TRADINGAGENTS_IN_DOCKER"] = "1"
    env["TRADINGAGENTS_NO_UPDATE_CHECK"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("SEC_EDGAR_USER_AGENT", "ClawUp platform (managed TradingAgents runtime)")

    if cfg["mode"] == "compatible":
        env["TRADINGAGENTS_LLM_PROVIDER"] = "openai_compatible"
        if cfg["backend_url"]:
            env["TRADINGAGENTS_LLM_BACKEND_URL"] = cfg["backend_url"]
        if cfg["api_key"]:
            env["OPENAI_COMPATIBLE_API_KEY"] = cfg["api_key"]
        else:
            env.pop("OPENAI_COMPATIBLE_API_KEY", None)
    else:
        # Native provider: TradingAgents reads the provider's own standard
        # key env (ANTHROPIC_API_KEY, OPENAI_API_KEY, …), which the backend
        # already injects into the container env.
        env["TRADINGAGENTS_LLM_PROVIDER"] = cfg["llm_provider"]
        # ClawUp names the Google key GEMINI_API_KEY, but TradingAgents'
        # native "google" provider reads GOOGLE_API_KEY — alias it.
        if (
            cfg["llm_provider"] == "google"
            and not env.get("GOOGLE_API_KEY")
            and env.get("GEMINI_API_KEY")
        ):
            env["GOOGLE_API_KEY"] = env["GEMINI_API_KEY"]

    if cfg["quick_model"]:
        env["TRADINGAGENTS_QUICK_THINK_LLM"] = cfg["quick_model"]
    if cfg["deep_model"]:
        env["TRADINGAGENTS_DEEP_THINK_LLM"] = cfg["deep_model"]
    env["TRADINGAGENTS_OUTPUT_LANGUAGE"] = cfg["output_language"] or "English"
    env["TRADINGAGENTS_MAX_DEBATE_ROUNDS"] = cfg["max_debate_rounds"] or "1"
    env["TRADINGAGENTS_MAX_RISK_ROUNDS"] = cfg["max_risk_rounds"] or "1"
    # Point the CLI's data root (hardwired to $HOME/.tradingagents upstream)
    # at the shim's state dir, whatever its layout — see _cli_home.
    env["HOME"] = _cli_home()
    return env


# Crypto asset types have no fundamentals (no filings); the CLI rejects the
# rest-of-the-set default for them.  Suffixes per upstream cli/prompts.py.
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def _cli_home() -> str:
    """A HOME whose ~/.tradingagents IS the shim's DATA_ROOT.

    Upstream computes its whole data root from Path.home() at import time —
    results_dir, data_cache_dir and memory_log_path all under
    $HOME/.tradingagents — and exposes no single env override for the root.
    When DATA_ROOT already sits at $HOME/.tradingagents nothing is needed;
    otherwise build a tiny bridge home containing a symlink so results,
    checkpoints and the memory log land inside the backed-up state dir for
    ANY deploy account (default, root, custom) instead of silently escaping
    it.
    """
    home = os.environ.get("HOME", "")
    d = os.path.realpath(DATA_ROOT)
    if home and os.path.realpath(os.path.join(home, ".tradingagents")) == d:
        return home
    bridge = os.path.join(DATA_ROOT, ".clawup-home")
    link = os.path.join(bridge, ".tradingagents")
    os.makedirs(bridge, exist_ok=True)
    if not os.path.islink(link):
        try:
            os.symlink(d, link)
        except FileExistsError:
            pass  # someone already put a real dir there; leave it be
    return bridge


def default_analysts(ticker: str) -> str:
    """The 'all the asset type allows' --analysts default, per ticker."""
    if ticker.upper().endswith(CRYPTO_SUFFIXES):
        return "market,sentiment,news"
    return "market,sentiment,news,fundamentals"


def _pump(run: Run, proc: subprocess.Popen) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        with log_lock:
            run.log.append(line.rstrip("\n"))
    rc = proc.wait()
    tail = "\n".join(list(run.log)[-40:])
    if rc == 0:
        # Collect the results BEFORE publishing "done": other handler
        # threads read run.status concurrently, and a momentary
        # done-without-report state is exactly what makes the SSE reply
        # say "analysis complete" with no result attached.
        _collect_report(run)
        _mirror_reports_to_workspace(run)
        _extract_decision(run)
        run.trade = _trade(run)
        run.status = "done"
        _log(f"run {run.ticker} {run.date} finished ok (report={run.report_path})")
    else:
        run.error = (
            f"TradingAgents CLI exited with code {rc}.\n"
            f"--- last {min(40, len(list(run.log)))} lines ---\n{tail}"
        )
        run.status = "error"
        _log(f"run {run.ticker} {run.date} failed rc={rc}")
    run.finished_at = time.time()


def _collect_report(run: Run) -> None:
    """Find the newest report markdown written by this run under DATA_ROOT.

    v0.7.1 layout (verified in e2e):
      $DATA/logs/reports/<TICKER>_<timestamp>/complete_report.md   (one-page full report)
      $DATA/logs/reports/<TICKER>_<timestamp>/{1_analysts,2_research,
          3_trading,4_risk,5_portfolio}/*.md
      $DATA/logs/<TICKER>/<date>/reports/*.md                      (per-role)
      $DATA/memory/trading_memory.md                               (memory log)
    complete_report.md is preferred; the heuristic below is the fallback.
    """
    candidates = []
    for dirpath, _dirnames, filenames in os.walk(DATA_ROOT):
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime >= run.started_at - 10:
                candidates.append((mtime, path))
    if not candidates:
        return
    # Scope candidates to THIS run with EXACT path-component matches.
    # Substring checks are unsound: a one-letter ticker matches inside any
    # path containing that letter ("v" in "nvda_..."), and a shared date
    # matches another ticker's per-role tree (logs/NVDA/<same date>/).
    # Accepted shapes:
    #   logs/<TICKER>/<DATE>/...                (per-role; ticker AND date exact)
    #   logs/reports/<TICKER>_<YYYYMMDD_HHMMSS>/...  (consolidated; the ts is
    #   run-start time, so same-ticker runs are separated by the mtime window)
    ticker_lc = run.ticker.lower()
    date_lc = run.date.lower()

    def scope(path: str) -> int:
        parts = [p.lower() for p in os.path.relpath(path, DATA_ROOT).split(os.sep)]
        for i, part in enumerate(parts[:-1]):  # directories only
            if part == ticker_lc:
                if date_lc in parts[i + 1:]:
                    return 0
                return 3  # same ticker, different date -> earlier run
            if part.startswith(ticker_lc + "_"):
                return 0
        return 3

    scoped = [c for c in candidates if scope(c[1]) == 0]
    if not scoped:
        _log(f"no report file belongs to run {run.ticker} {run.date}; not adopting a foreign report")
        return
    scoped.sort()

    def rank(path: str) -> int:
        p = path.lower()
        if "complete_report" in p:
            return 0  # one-page full report
        if "final" in p:
            return 1  # final_trade_decision.md etc.
        if "report" in p:
            return 2  # per-role reports
        return 3

    # Best rank wins; within a rank, newest mtime.
    best_rank = min(rank(p) for _m, p in scoped)
    _mtime, run.report_path = max(c for c in scoped if rank(c[1]) == best_rank)
    try:
        with open(run.report_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # Head = the chat excerpt (report top, where the header Rating lives);
        # tail = where the PM's final-decision section sits.  Both are bounded
        # so a huge report never bloats a chat message or the decision scan.
        run.report_head = text[:REPORT_MAX_CHARS]
        run.report_tail = text[-REPORT_TAIL_CHARS:]
    except OSError as exc:
        _log(f"failed to read report {run.report_path}: {exc}")


def _mirror_reports_to_workspace(run: Run) -> None:
    """Copy the run's report tree into the Files-API-visible workspace.

    The Files API scopes to $STATE_DIR/workspace, while TradingAgents
    writes reports under $STATE_DIR/logs — without this mirror the full
    reports (per-analyst sections, risk debate, PM decision) are invisible
    to the user, who only sees the chat excerpt.
    """
    if not run.report_path:
        return
    src_dir = os.path.dirname(run.report_path)
    if src_dir == DATA_ROOT:
        return
    dest = os.path.join(DATA_ROOT, "workspace", "reports", os.path.basename(src_dir))
    try:
        shutil.copytree(src_dir, dest, dirs_exist_ok=True)
        run.workspace_path = dest
    except OSError as exc:
        _log(f"report mirror to workspace failed (non-fatal): {exc}")


def _extract_decision(run: Run) -> None:
    """Extract the FINAL decision from authoritative lines only.

    The report body is full of per-agent rating words (the bull researcher
    says Buy, the bear says Sell, …), so "last rating word in the text"
    reverses the verdict whenever the excerpt is truncated mid-report.
    Trusted sources, in order:
      1. the report header's "Rating: <X>" line (written from the
         portfolio manager's verdict at the top of complete_report.md);
      2. an explicit "Final Decision:" / "Decision:" line — report tail
         first (the PM section is last), then the CLI log.
    If neither yields a value, decision stays None and the result omits
    the line — an absent answer beats a reversed one.
    """
    m = DECISION_MARKER_RE.search(run.report_head or "")
    if m and m.group(1).strip():
        run.decision = m.group(1).strip()
        return
    for text in (run.report_tail, "\n".join(list(run.log)[-100:])):
        matches = DECISION_MARKER_RE.findall(text or "")
        if matches:
            run.decision = matches[-1].strip()
            return


def _trade(run: Run) -> str | None:
    """At most one Hyperliquid order for a finished analysis (hl_trade.py).
    None when trading is off or the ticker is not crypto quoted in dollars."""
    if not TRADE_ENABLED or hl_trade.coin_of(run.ticker) is None:
        return None
    if hl_trade.side_of(run.decision) is None:
        return f"no order: the rating is {run.decision or 'unknown'}"
    if TRADE_ORDER_USD <= 0:
        return "no order: the order size (TA_TRADE_ORDER_USD) is not set"
    cmd = [
        HL_PYTHON, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hl_trade.py"),
        "order", "--ticker", run.ticker, "--rating", run.decision or "", "--usd", str(TRADE_ORDER_USD),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TRADE_TIMEOUT_SECONDS)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        result = json.loads(lines[-1]) if lines else {}
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        result = {"ok": False, "message": f"the trade did not run ({type(e).__name__})"}
    message = str(result.get("message") or "the trade returned nothing")
    _log(f"trade {run.ticker} {run.decision}: {message}")
    return message if result.get("ok") else f"not traded: {message}"


# ---------------------------------------------------------------------------
# The owner's Privy wallet (chat commands)
# ---------------------------------------------------------------------------

WALLET_COMMANDS = {
    "status": ("wallet", "wallet status", "my wallet", "钱包", "钱包状态", "我的钱包"),
    "connect": ("connect wallet", "connect my wallet", "wallet login", "login wallet",
                "连接钱包", "登录钱包", "钱包登录"),
    "disconnect": ("disconnect wallet", "disconnect my wallet", "wallet logout", "logout wallet",
                   "断开钱包", "退出钱包", "钱包退出"),
}


def wallet_command(text: str) -> str | None:
    """'connect wallet' -> "connect", ...; None for anything else."""
    t = re.sub(r"[\s.!?。！？]+", " ", (text or "").strip().lower()).strip()
    for command, phrases in WALLET_COMMANDS.items():
        if t in phrases:
            return command
    return None


class Login:
    """One Privy device login at a time. The CLI prints a URL and a code,
    then waits until the owner approves; the wait outlives the chat turn."""

    URL_RE = re.compile(r"https://\S+")
    CODE_RE = re.compile(r"Your code:\s*(\S+)")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.code: str | None = None
        self.output: deque = deque(maxlen=40)
        self.result: str | None = None  # "connected" | an error, once the CLI exits

    def start(self) -> str:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None and self.url:
                return self._prompt()
            self.url = self.code = self.result = None
            self.output.clear()
            ready = threading.Event()
            try:
                self.proc = subprocess.Popen(
                    [hl_trade.PRIVY_CLI, "login"], env=hl_trade.privy_env(), text=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                )
            except OSError as e:
                return f"The Privy CLI is not available here ({e})."
            threading.Thread(target=self._read, args=(self.proc, ready), daemon=True).start()
        ready.wait(30)
        with self._lock:
            if self.url and self.code:
                return self._prompt()
            tail = " ".join(self.output).strip()
            if "Already logged in" in tail:
                return "Your wallet is already connected. Say 'wallet' to see it."
            return f"Privy did not start a login: {tail[-300:] or 'no answer'}"

    def _prompt(self) -> str:
        return (
            f"Open {self.url} and approve with code {self.code}. "
            "Privy holds the key; this agent only asks it to sign. "
            "Say 'wallet' afterwards to check."
        )

    def _read(self, proc: subprocess.Popen, ready: threading.Event) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            with self._lock:
                self.output.append(line)
                if self.url is None and (m := self.URL_RE.search(line)):
                    self.url = m.group(0)
                if (m := self.CODE_RE.search(line)):
                    self.code = m.group(1)
                if self.url and self.code:
                    ready.set()
        rc = proc.wait()
        with self._lock:
            self.result = "connected" if rc == 0 else (" ".join(self.output)[-300:] or f"exit {rc}")
        ready.set()

    def pending(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None


LOGIN = Login()


def wallet_reply(command: str) -> str:
    if command == "connect":
        return LOGIN.start()
    if command == "disconnect":
        try:
            hl_trade.privy(["logout"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
            return f"Could not disconnect: {e}"
        return "Wallet disconnected. This agent can no longer sign."
    if LOGIN.pending():
        return "Waiting for you to approve the login in Privy. " + LOGIN._prompt()
    try:
        address = hl_trade.wallet_address()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        return f"No wallet connected ({e}). Say 'connect wallet' to sign in to Privy."
    if not TRADE_ENABLED:
        trading = "Trading is off for this agent."
    elif TRADE_ORDER_USD <= 0:
        trading = "Trading is on, but no order size is set, so nothing is traded."
    else:
        trading = (
            f"After a Buy rating on a crypto ticker this agent buys ${TRADE_ORDER_USD:g} of it on "
            "Hyperliquid; after a Sell it closes the long. Each order is first asked of your "
            "ClawUp mandate."
        )
    return f"Wallet: {address} (Privy). {trading}"


def _start_proc(run: Run) -> None:
    cfg = ta_config()
    analysts = cfg["analysts"] or default_analysts(run.ticker)
    cmd = [cfg["bin"], "--ticker", run.ticker, "--date", run.date,
           "--save", "--no-show", "--no-html", "--analysts", analysts]
    _log(f"starting: {' '.join(cmd)}")
    os.makedirs(DATA_ROOT, exist_ok=True)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=build_cli_env(),
            text=True,
            bufsize=1,
            cwd=DATA_ROOT,
        )
    except OSError as exc:
        run.status = "error"
        run.error = f"failed to start TradingAgents CLI: {exc}"
        run.finished_at = time.time()
        return
    run.proc = proc
    threading.Thread(target=_pump, args=(run, proc), daemon=True).start()


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _completion_id() -> str:
    return f"chatcmpl-shim-{uuid.uuid4().hex[:12]}"


def _sse_chunk(handler: BaseHTTPRequestHandler, delta: dict, finish: str | None) -> bool:
    chunk = {
        "id": _completion_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [{"index": 0, "delta": delta}],
    }
    if finish is not None:
        chunk["choices"][0]["finish_reason"] = finish
    payload = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    try:
        handler.wfile.write(payload.encode("utf-8"))
        handler.wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _sse_done(handler: BaseHTTPRequestHandler) -> None:
    try:
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _send_json(handler: BaseHTTPRequestHandler, status: int, obj: dict) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _send_chat_completion(handler: BaseHTTPRequestHandler, content: str) -> None:
    _send_json(handler, 200, {
        "id": _completion_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


def _send_sse_start(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    # SSE has no Content-Length, so the stream end can only be signalled by
    # closing the connection — a keep-alive response would leave the client
    # (and the backend's reqwest reader) blocking forever for EOF.
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()


def _result_text(run: Run) -> str:
    if run.status == "error":
        return f"Analysis failed.\n{run.error or 'unknown error'}"
    lines = [f"{run.ticker} {run.date} analysis complete (took {run.elapsed()})."]
    if run.decision:
        lines.append(f"Final decision: {run.decision}")
    if run.trade:
        lines.append(f"Hyperliquid: {run.trade}")
    if run.workspace_path or run.report_path:
        # Prefer the workspace path: that is what the Files API exposes.
        lines.append(f"Report saved to: {run.workspace_path or run.report_path}")
    if run.report_head:
        lines.append("\n--- Report excerpt ---\n" + run.report_head)
    return "\n".join(lines)


def _handle_chat(handler: BaseHTTPRequestHandler, body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        _send_json(handler, 400, {"error": {"message": "invalid JSON body", "type": "invalid_request"}})
        return

    stream = bool(payload.get("stream"))
    text = last_user_text(payload.get("messages"))

    if not text.strip():
        content = HELP_TEXT
        if stream:
            _send_sse_start(handler)
            _sse_chunk(handler, {"role": "assistant", "content": content}, None)
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
        else:
            _send_chat_completion(handler, content)
        return

    command = wallet_command(text)
    if command is not None:
        content = wallet_reply(command)
        if stream:
            _send_sse_start(handler)
            _sse_chunk(handler, {"role": "assistant", "content": content}, None)
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
        else:
            _send_chat_completion(handler, content)
        return

    parsed = parse_request(text)
    current = SLOT.get()
    action, target = resolve_turn(text, parsed, current)

    if action == "help":
        content = "I couldn't tell which ticker to analyze.\n" + HELP_TEXT
        if stream:
            _send_sse_start(handler)
            _sse_chunk(handler, {"role": "assistant", "content": content}, None)
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
        else:
            _send_chat_completion(handler, content)
        return

    if action in ("status", "result"):
        run, started_now = current, False
    else:  # start
        run, started_now = SLOT.claim(target["ticker"], target["date"])
        if started_now:
            _start_proc(run)

    # From here `run` is the live/finished run this turn is bound to.
    if run is None:  # pragma: no cover
        _send_chat_completion(handler, HELP_TEXT)
        return

    if not stream:
        # Non-streaming must never block on a long run (backend hard-caps at
        # 120s).  Report status; the user polls with a follow-up message.
        if run.status == "running":
            if started_now:
                content = (
                    f"Started {run.ticker} {run.date} analysis. "
                    "This usually takes 10-40 minutes — send any message (e.g. 'progress') "
                    "later to check on it."
                )
            else:
                content = run.status_text()
        else:
            content = _result_text(run)
        _send_chat_completion(handler, content)
        return

    # ---- streaming path ----
    _send_sse_start(handler)
    if started_now:
        intro = (
            f"Starting analysis of {run.ticker} (date {run.date}). "
            "Pipeline: analyst team → bull/bear researcher debate → trader → "
            "risk team → portfolio manager. This usually takes 10-40 minutes; "
            "I'll report progress as it runs, and you can ask 'progress' anytime."
        )
        if not _sse_chunk(handler, {"role": "assistant", "content": intro + "\n"}, None):
            return  # client gone; run continues in background
    else:
        if run.status == "running":
            if not _sse_chunk(handler, {"role": "assistant", "content": run.status_text() + "\n"}, None):
                return
            _sse_chunk(handler, {}, "stop")
            _sse_done(handler)
            return
        if not _sse_chunk(handler, {"role": "assistant", "content": _result_text(run) + "\n"}, None):
            return
        _sse_chunk(handler, {}, "stop")
        _sse_done(handler)
        return

    # We own this run: heartbeat until it finishes (or the client drops).
    # Sleep in chunks no larger than the heartbeat interval so a heartbeat
    # actually fires at ~HEARTBEAT_SECONDS, not only at multiples of 5.
    chunk = min(5.0, max(0.5, HEARTBEAT_SECONDS))
    last_beat = time.time()
    while run.status == "running":
        time.sleep(chunk)
        if run.status != "running":
            break
        if time.time() - last_beat >= HEARTBEAT_SECONDS:
            if not _sse_chunk(handler, {"role": "assistant", "content": f"\nrunning for {run.elapsed()}\n"}, None):
                return  # client gone; run continues in background
            last_beat = time.time()

    final = _result_text(run)
    _sse_chunk(handler, {"role": "assistant", "content": "\n" + final + "\n"}, None)
    _sse_chunk(handler, {}, "stop")
    _sse_done(handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ta-shim/1.0"

    def log_message(self, fmt: str, *args) -> None:  # route stdlib access logs to _log
        try:
            _log(fmt % args)
        except (TypeError, ValueError):
            _log(fmt)

    def _authorized(self) -> bool:
        if not GATEWAY_TOKEN:
            return True  # local dev without a gateway token
        return self.headers.get("Authorization", "") == f"Bearer {GATEWAY_TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/ready", "/healthz"):
            # Compact JSON on purpose: the provisioner's wget/grep readiness
            # fallbacks look for the literal `"ready":true` (no space).
            body = b'{"ready":true,"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return
        if path == "/v1/ta/status":
            if not self._authorized():
                _send_json(self, 401, {"error": {"message": "unauthorized"}})
                return
            run = SLOT.get()
            obj: dict = {"status": "idle"}
            if run is not None:
                obj = {
                    "status": run.status,
                    "ticker": run.ticker,
                    "date": run.date,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "elapsed": run.elapsed(),
                    "decision": run.decision,
                    "report_path": run.report_path,
                    "error": run.error,
                    "trade": run.trade,
                }
            _send_json(self, 200, obj)
            return
        _send_json(self, 404, {"error": {"message": f"no such endpoint: {path}"}})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            _send_json(self, 404, {"error": {"message": f"no such endpoint: {path}"}})
            return
        if not self._authorized():
            _send_json(self, 401, {"error": {"message": "invalid or missing gateway token", "type": "unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        _handle_chat(self, body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _sigterm_handler(signum, frame) -> None:
    _log(f"received signal {signum}; shutting down")
    run = SLOT.get()
    if run is not None and run.proc is not None and run.status == "running":
        try:
            run.proc.terminate()
        except OSError:
            pass
    # shutdown() must run on a DIFFERENT thread: this handler fires on the
    # main thread that owns serve_forever, and calling shutdown() there
    # deadlocks (it waits on the loop it is blocking).
    threading.Thread(target=server.shutdown, daemon=True).start()


def main() -> None:
    global server
    os.makedirs(DATA_ROOT, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", GATEWAY_PORT), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)
    _log(
        f"listening on :{GATEWAY_PORT} "
        f"(mode={ta_config()['mode']} provider={ta_config()['llm_provider'] or '-'} "
        f"quick={ta_config()['quick_model'] or '-'} deep={ta_config()['deep_model'] or '-'} "
        f"data_root={DATA_ROOT})"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
