"""The built image, end to end: a Telegram user asks for an analysis and gets the result.

Runs only when TA_IMAGE names a built image (CI builds one); needs Docker and host networking
(Linux). The real shim and the embedded channel-gateway run as in production; the Telegram Bot
API and the TradingAgents CLI are stand-ins (fake_telegram.py, fake_tradingagents.py).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_telegram import FakeTelegram  # noqa: E402

IMAGE = os.environ.get("TA_IMAGE", "")
TOKEN = "123456:TEST-token"
OWNER, STRANGER = 4242, 9999
HERE = Path(__file__).resolve().parent


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, seconds, what):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


@unittest.skipUnless(IMAGE, "set TA_IMAGE to a built image to run")
class TelegramToTheShim(unittest.TestCase):
    def setUp(self):
        self.telegram = FakeTelegram(TOKEN)
        self.state = Path(tempfile.mkdtemp(prefix="ta-state-"))
        channels = self.state / "channels"
        channels.mkdir(mode=0o700)
        # What ClawUp's provisioner writes when the owner connects Telegram.
        (channels / ".env").write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USERS={OWNER}\n")
        os.chmod(channels / ".env", 0o600)
        (channels / "config.extra.json").write_text(
            json.dumps({"platforms": {"telegram": {"extra": {"base_url": self.telegram.base_url}}}}))
        self.name = "ta-image-test-" + uuid.uuid4().hex[:8]
        docker("run", "-d", "--name", self.name, "--network", "host",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "-v", f"{self.state}:/state", "-v", f"{HERE}:/opt/test:ro",
               "-e", "HOME=/tmp", "-e", "TA_STATE_DIR=/state",
               "-e", f"GATEWAY_PORT={free_port()}",
               "-e", "OPENCLAW_GATEWAY_TOKEN=gateway-" + uuid.uuid4().hex,
               "-e", "TA_BIN=/opt/test/fake_tradingagents.py", "-e", "TA_FAKE_SECONDS=8",
               "-e", "TA_SHIM_HEARTBEAT_SECONDS=1", "-e", "CHANNEL_GATEWAY_POLL_SECONDS=1",
               "-e", "OPENAI_API_KEY=sk-must-not-leak",
               IMAGE, "/opt/venv/bin/python", "/opt/ta-shim/ta_shim.py")

    def tearDown(self):
        docker("rm", "-f", self.name, check=False)
        self.telegram.close()
        shutil.rmtree(self.state, ignore_errors=True)

    def logs(self) -> str:
        r = docker("logs", self.name, check=False)
        return r.stdout + r.stderr

    def test_an_allowed_user_gets_the_analysis_and_no_heartbeats(self):
        wait_for(lambda: "getUpdates" in self.telegram.calls, 120, "the gateway to poll Telegram\n" + self.logs())
        self.telegram.message_from(STRANGER, "Analyze NVDA on 2026-11-20")
        self.telegram.message_from(OWNER, "Analyze NVDA on 2026-11-20")

        def answered():
            return any("Final decision: Buy" in t for t in self.telegram.texts_to(OWNER))
        try:
            wait_for(answered, 120, "the analysis in Telegram")
        except AssertionError:
            raise AssertionError(f"sent: {self.telegram.sent}\n{self.logs()}") from None

        owner = "\n".join(self.telegram.texts_to(OWNER))
        self.assertIn("I'll send the result here", owner)
        self.assertNotIn("running for", owner)  # heartbeats are SSE comments, never chat text
        self.assertNotIn("progress", owner)
        self.assertEqual(self.telegram.texts_to(STRANGER), [])
        self.assertEqual(self.logs().count("--ticker NVDA"), 1)  # the stranger started nothing
        self.assertNotIn("sk-must-not-leak", self.logs())

        start = time.time()
        docker("stop", "-t", "30", self.name)
        self.assertLess(time.time() - start, 25)
        r = docker("inspect", "-f", "{{.State.ExitCode}}", self.name)
        self.assertEqual(r.stdout.strip(), "0", self.logs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
