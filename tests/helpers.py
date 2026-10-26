"""Shared fakes. Nothing here can reach Slack or the live Herdr session."""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from slack_bridge.attachments import Attachments, FetchError
from slack_bridge.config import Config
from slack_bridge.handoff import Handoffs
from slack_bridge.inbox import FmInbox
from slack_bridge.keeper import Keeper
from slack_bridge.slack_api import SlackCallError
from slack_bridge.state import State

FAKES = Path(__file__).parent / "fakes"

# Expected warnings (alarms, retries) stay out of the test output;
# assertLogs still sees them.
logging.getLogger("slack_bridge").addHandler(logging.NullHandler())
CAPTAIN = "UTESTUSER1"
DM = "DTESTDM01"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class FakeSlack:
    def __init__(self):
        self.posts: list[dict] = []
        self.reactions: list[tuple] = []
        self.uploads: list[dict] = []
        # Project channels: every private channel in the fake workspace
        # (name -> id), and the ones the bot is a member of.
        self.channels: dict[str, str] = {}
        self.member: set[str] = set()
        self.invites: list[tuple] = []
        self.fail_invite: str | None = None

    def post(self, channel, text, thread_ts=None):
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return f"9{len(self.posts)}.000"

    def react(self, channel, ts, name):
        self.reactions.append((channel, ts, name))

    def upload_images(self, channel, images, thread_ts=None):
        self.uploads.append({"channel": channel, "thread_ts": thread_ts,
                             "names": [i.name for i in images]})

    def create_private_channel(self, name):
        if name in self.channels:
            raise SlackCallError("conversations.create", "name_taken")
        self.channels[name] = f"C{len(self.channels) + 1:04d}"
        self.member.add(self.channels[name])
        return self.channels[name]

    def invite(self, channel, user):
        if self.fail_invite:
            raise SlackCallError("conversations.invite", self.fail_invite)
        self.invites.append((channel, user))

    def find_private_channel(self, name):
        cid = self.channels.get(name)
        return cid if cid in self.member else None


class FakeDownloader:
    def __init__(self, data=PNG):
        self.data = data
        self.calls: list[tuple[str, int]] = []
        self.fail: dict[str, str] = {}  # url -> reason

    def __call__(self, url, max_bytes):
        self.calls.append((url, max_bytes))
        if url in self.fail:
            raise FetchError(self.fail[url])
        return self.data


class FakeHerdr:
    """In-process herdr: a mutable pane list plus a record of typed commands."""

    def __init__(self, panes):
        self.panes = panes
        self.runs: list[tuple[str, str]] = []
        self.keys: list[tuple[str, str]] = []
        self.on_keys = None  # called with the key, e.g. to make the pane idle
        self.fg = {"foreground_process_group_id": 7, "shell_pid": 7,
                   "foreground_processes": [{"name": "zsh", "pid": 7}]}

    def list_panes(self):
        return [dict(p) for p in self.panes]

    def process_info(self, pane_id):
        return self.fg

    def run(self, pane_id, command):
        self.runs.append((pane_id, command))

    def send_keys(self, pane_id, key):
        self.keys.append((pane_id, key))
        if self.on_keys:
            self.on_keys(key)


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self.now)


def pane(pane_id="t:p1", cwd=None, label="Firstmate", agent="claude", status="idle"):
    p = {"pane_id": pane_id, "cwd": cwd, "label": label, "agent_status": status}
    if agent:
        p["agent"] = agent
    return p


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fm_home = self.tmp / "fm-home"
        self.fm_home.mkdir()
        self.state = State(self.tmp / "state")
        # A config that can only ever point at fakes: a temp FM home, a fake
        # herdr binary and the "test" Herdr session.
        self.cfg = Config(fm_home=str(self.fm_home), state_dir=self.tmp / "state",
                          log_file=self.tmp / "bridge.log",
                          herdr_bin=str(FAKES / "herdr"), herdr_session="test")
        self.inbox = FmInbox(str(FAKES / "fm-inbox.sh"), str(self.fm_home))
        self.slack = FakeSlack()

    def tearDown(self):
        self._tmp.cleanup()

    def make_handoffs(self, fetch=None, **kw):
        attachments = Attachments(self.tmp / "state" / "inbound", fetch or FakeDownloader())
        return Handoffs(self.slack, self.inbox, self.state, attachments, **kw)

    def keeper(self, herdr, clock=None):
        clock = clock or FakeClock()
        self.notified = []
        k = Keeper(self.cfg, self.state, herdr,
                   lambda text, channel=None, thread_ts=None:
                   self.notified.append((text, channel, thread_ts)),
                   clock=clock, sleep=clock.sleep)
        return k

    def notes(self):
        import json

        return [json.loads(p.read_text()) for p in sorted((self.fm_home / "notes").glob("*.json"))]
