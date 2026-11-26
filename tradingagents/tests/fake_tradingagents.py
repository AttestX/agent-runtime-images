#!/usr/bin/env python3
"""A stand-in for the TradingAgents CLI: waits TA_FAKE_SECONDS, then writes a report as v0.7.1 does.

The shim runs it through TA_BIN, with the arguments it gives the real CLI
(--ticker T --date D ...) and HOME set so that ~/.tradingagents is its data root.
"""
import argparse
import os
import time
from datetime import datetime

parser = argparse.ArgumentParser()
parser.add_argument("--ticker", required=True)
parser.add_argument("--date", required=True)
args, _ = parser.parse_known_args()

time.sleep(float(os.environ.get("TA_FAKE_SECONDS", "2")))
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
out = os.path.join(os.path.expanduser("~"), ".tradingagents", "logs", "reports", f"{args.ticker}_{stamp}")
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "complete_report.md"), "w", encoding="utf-8") as fh:
    fh.write(f"# Trading Analysis Report: {args.ticker}\n\nRating: Buy\n\nThe stand-in's report.\n")
print("analysis complete", flush=True)
