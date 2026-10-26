"""Route inbound Slack events and relay Firstmate replies back to Slack.

Independent of the Slack SDK: Slack is reached through a small ``SlackPort``
so the tests can drive the whole flow with a fake.
"""

from __future__ import annotations

import logging
from typing import Protocol

from . import text as T
from .attachments import file_refs
from .guard import Rescue
from .handoff import Handoffs, parse_request_id, request_id
from .inbox import FmInbox, InboxError
from .keeper import Keeper
from .state import State
from .suggest import Advice, Advisor

# DMs with the bot, and private channels the bot is a member of.
ALLOWED_CHANNEL_TYPES = {"im", "group"}
# A message with files attached; every other subtype (edits, deletes, joins)
# is dropped.
ALLOWED_SUBTYPES = {"file_share"}

log = logging.getLogger("slack_bridge.router")

NEW_SESSION_PHRASES = {"new session", "/new-session", "new-session"}
STATUS_PHRASES = {"status", "/status"}
# Emergency commands: answered by the bridge itself, never by Firstmate.
LOAD_PHRASES = {"load", "/load"}
SHED_PHRASES = {"shed", "/shed"}
SHED_ACTIONS = {"shed pause": "pause", "/shed pause": "pause",
                "shed quit": "quit", "/shed quit": "quit"}
RESTART_PHRASES = {"restart", "/restart", "restart firstmate"}
RESTART_FORCE_PHRASES = {"restart force", "/restart force"}
RESTART_BRIDGE_PHRASES = {"restart bridge", "/restart bridge", "restart-bridge"}
COMMANDS = (NEW_SESSION_PHRASES | STATUS_PHRASES | LOAD_PHRASES | SHED_PHRASES
            | set(SHED_ACTIONS) | RESTART_PHRASES | RESTART_FORCE_PHRASES
            | RESTART_BRIDGE_PHRASES)
# Commands only while they have something to act on; otherwise they are
# ordinary messages for Firstmate.
ACCEPT_PHRASES = {"yes", "ok"}  # in a fresh `shed` list's thread: takes its suggestion
RESUME_PHRASES = {"resume", "/resume"}  # while `shed pause` has paused processes


class SlackPort(Protocol):
    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str | None: ...
    def react(self, channel: str, ts: str, name: str) -> None: ...
    # Used only by outbound ``say --image``; ``images`` are images.Image.
    def upload_images(self, channel: str, images, thread_ts: str | None = None) -> None: ...


def post_long(slack: SlackPort, channel: str, body: str, thread_ts: str | None = None,
              escape: bool = True, thread_rest: bool = False) -> list[str | None]:
    """Post ``body`` as one or more mrkdwn-safe messages; returns their ts.

    With ``thread_rest`` and no ``thread_ts``, the first chunk is a top-level
    message and the rest go into its thread.
    """
    chunks = T.split(T.escape(body) if escape else body)
    posted = []
    for chunk in chunks:
        ts = slack.post(channel, chunk, thread_ts=thread_ts)
        posted.append(ts)
        if thread_rest and thread_ts is None:
            thread_ts = ts
    return posted


class Router:
    def __init__(self, captain_id: str, slack: SlackPort, inbox: FmInbox, state: State,
                 keeper: Keeper | None, handoffs: Handoffs,
                 rescue: Rescue | None = None, advisor: Advisor | None = None):
        if not captain_id:
            raise ValueError("captain_id is required")
        self.captain_id = captain_id
        self.slack = slack
        self.inbox = inbox
        self.state = state
        self.keeper = keeper
        self.handoffs = handoffs
        self.rescue = rescue
        self.advisor = advisor

    # Allowlist -------------------------------------------------------------

    def accept(self, event: dict) -> tuple[bool, str]:
        """Return (accepted, drop_reason). Never inspects message content."""
        if event.get("type") != "message":
            return False, "not-a-message"
        if event.get("subtype") and event.get("subtype") not in ALLOWED_SUBTYPES:
            return False, f"subtype-{event.get('subtype')}"
        if event.get("bot_id") or event.get("bot_profile"):
            return False, "from-bot"
        if event.get("channel_type") not in ALLOWED_CHANNEL_TYPES:
            return False, f"channel-type-{event.get('channel_type') or 'none'}"
        if event.get("user") != self.captain_id:
            return False, "sender-not-allowed"
        if not event.get("channel") or not event.get("ts"):
            return False, "malformed"
        return True, ""

    # Inbound ---------------------------------------------------------------

    def handle_message(self, event: dict) -> str:
        """Handle one Slack message event; returns an outcome tag."""
        ok, reason = self.accept(event)
        if not ok:
            # Content is never logged, only who/where and why it was dropped.
            log.info("drop: reason=%s user=%s channel=%s", reason, event.get("user"),
                     event.get("channel"))
            return f"dropped:{reason}"

        channel, ts = event["channel"], event["ts"]
        thread_ts = event.get("thread_ts") or ts
        body = T.unescape(event.get("text") or "").strip()
        files = file_refs(event)
        if not body and not files:
            return "dropped:empty"
        command = T.normalize_command(body)

        # A message with files always goes to Firstmate, never as a command.
        if not files and (command in COMMANDS
                          or self._live_command(command, channel, thread_ts)):
            if not self.state.mark_command_seen(request_id(channel, ts)):
                return "duplicate"
            return self._command(command, channel, ts, thread_ts)
        tag = ""
        if event["channel_type"] == "group":
            # Tells Firstmate which project a channel message is about.
            key = self.state.project_for_channel(channel)
            tag = f"[project:{key}]" if key else f"[channel:{channel}]"
        return self.handoffs.submit(channel, ts, thread_ts, f"{tag} {body}".strip(), files)

    def _live_command(self, command: str, channel: str, thread_ts: str) -> bool:
        if self.rescue is None:
            return False
        if command in ACCEPT_PHRASES:
            return self.rescue.current_offer(channel, thread_ts) is not None
        return command in RESUME_PHRASES and bool(self.state.read()["paused"])

    def _command(self, command: str, channel: str, ts: str, thread_ts: str) -> str:
        if command in NEW_SESSION_PHRASES or command in RESTART_PHRASES:
            return self._new_session(channel, ts, thread_ts)
        if command in RESTART_FORCE_PHRASES:
            return self._new_session(channel, ts, thread_ts, force=True)
        if command in STATUS_PHRASES:
            return self._status(channel, thread_ts)
        if self.rescue is None:
            self.slack.post(channel, "Emergency commands are not enabled on this bridge.",
                            thread_ts=thread_ts)
            return "rescue-disabled"
        if command in LOAD_PHRASES:
            post_long(self.slack, channel, self.rescue.load_report(), thread_ts=thread_ts,
                      escape=False)
            return "load"
        if command in SHED_PHRASES:
            post_long(self.slack, channel, self.rescue.offer(channel, thread_ts),
                      thread_ts=thread_ts, escape=False)
            return "shed"
        if command in SHED_ACTIONS or command in ACCEPT_PHRASES:
            self._react(channel, ts, "hourglass_flowing_sand")
            action = SHED_ACTIONS.get(command)
            post_long(self.slack, channel,
                      self.rescue.shed(action, channel, thread_ts, top_level=ts == thread_ts),
                      thread_ts=thread_ts, escape=False)
            return f"shed-{action or 'accept'}"
        if command in RESUME_PHRASES:
            post_long(self.slack, channel, self.rescue.resume(), thread_ts=thread_ts,
                      escape=False)
            return "resume"
        return self._restart_bridge(channel, thread_ts)

    def _restart_bridge(self, channel: str, thread_ts: str) -> str:
        with self.state.update() as st:
            st["bridge_restart"] = {"channel": channel, "thread_ts": thread_ts}
        self.slack.post(channel, "Restarting the bridge (launchd restarts it in a few "
                        "seconds)…", thread_ts=thread_ts)
        try:
            self.rescue.restart_bridge()
        except OSError as exc:
            with self.state.update() as st:
                st["bridge_restart"] = None
            self.slack.post(channel, f"Bridge restart failed: {T.escape(str(exc))}.",
                            thread_ts=thread_ts)
            return "restart-bridge-failed"
        return "restart-bridge"

    def _new_session(self, channel: str, ts: str, thread_ts: str,
                     force: bool = False) -> str:
        if self.keeper is None:
            self.slack.post(channel, "The keeper is disabled on this bridge.",
                            thread_ts=thread_ts)
            return "new-session-disabled"
        self._react(channel, ts, "hourglass_flowing_sand")
        if force:
            text = ("Interrupting Firstmate's current turn (Escape; nothing is killed), "
                    "then starting a fresh session once it is idle…")
        else:
            text = ("Starting a fresh Firstmate session once it is idle "
                    "(a working turn is never interrupted)…")
        self.slack.post(channel, text, thread_ts=thread_ts)
        # Queued, not run here: the keeper loop serializes every fresh-session
        # request (Slack or hook) so two can never type /clear at once.
        self.keeper.request_fresh_session("slack", channel=channel, thread_ts=thread_ts,
                                          force=force)
        busy = None if force else self.keeper.busy()
        if busy:
            self.slack.post(channel, self._restart_advice(*busy), thread_ts=thread_ts)
        return "new-session-force" if force else "new-session"

    def _restart_advice(self, status: str, busy_seconds: float) -> str:
        """Wait or force, for a Firstmate that is busy; only a suggestion."""
        stuck = busy_seconds >= self.keeper.cfg.stuck_seconds
        advice = Advice("force" if stuck else "wait",
                        f"Firstmate has been {status} for ~{round(busy_seconds / 60)} min"
                        + (" with no change" if stuck else ""))
        if self.advisor is not None:
            advice = self.advisor.restart(status, busy_seconds, advice)
        if advice.choice == "force":
            return (f"{advice.label}: `restart force` ({advice.why}).{advice.note} Send "
                    "`restart force` to interrupt that turn; meanwhile a fresh session "
                    "starts if it goes idle on its own.")
        return (f"{advice.label}: wait ({advice.why}).{advice.note} The fresh session "
                "starts when the turn ends; send `restart force` to interrupt it instead.")

    def _status(self, channel: str, thread_ts: str) -> str:
        try:
            out = self.inbox.status().strip() or "(no status)"
        except InboxError as exc:
            self.slack.post(channel, f"Status unavailable: {T.escape(str(exc))}",
                            thread_ts=thread_ts)
            return "status-failed"
        for chunk in T.split(T.escape(out), limit=T.DEFAULT_LIMIT - 8):
            self.slack.post(channel, f"```\n{chunk}\n```", thread_ts=thread_ts)
        return "status"

    def _react(self, channel: str, ts: str, name: str) -> None:
        try:
            self.slack.react(channel, ts, name)
        except Exception as exc:  # already_reacted on a retry, or scope missing
            log.info("reaction %s skipped: %s", name, exc.__class__.__name__)


class ReplyRelay:
    """Poll ``fm-inbox.sh receipts`` and post new replies into their threads."""

    def __init__(self, slack: SlackPort, inbox: FmInbox, state: State):
        self.slack = slack
        self.inbox = inbox
        self.state = state

    def poll_once(self) -> int:
        """Post every reply after the stored cursor; returns how many posted."""
        cursor = self.state.read().get("receipts_cursor")
        if cursor is None:
            # First run: start at the present, never replay history into Slack.
            data = self.inbox.receipts(after=None)
            with self.state.update() as st:
                st["receipts_cursor"] = data.get("reply_cursor") or ""
            log.info("receipts cursor initialized")
            return 0
        data = self.inbox.receipts(after=cursor or None)
        posted = 0
        for reply in sorted(data.get("replies", []), key=lambda r: r["cursor"]):
            if cursor and reply["cursor"] <= cursor:
                continue
            target = self.state.thread_for(reply["id"]) or self._thread_from_request(
                data, reply["id"])
            if target:
                post_long(self.slack, target["channel"], reply.get("body") or "",
                          thread_ts=target["ts"])
                posted += 1
                log.info("reply posted: note=%s cursor=%s", reply["id"], reply["cursor"])
            # Advance per reply so a crash re-posts at most the one in flight.
            cursor = reply["cursor"]
            with self.state.update() as st:
                st["receipts_cursor"] = cursor
        return posted

    def _thread_from_request(self, data: dict, note_id: str) -> dict | None:
        """Thread of a Slack note whose hand-off timed out before it was mapped."""
        for note in data.get("pending", []) + data.get("handled", []):
            if note.get("id") != note_id:
                continue
            rid = note.get("request_id") or ""
            where = parse_request_id(rid)
            if where is None:
                return None
            pending = self.state.read()["handoffs"].get(rid) or {}
            thread_ts = pending.get("thread_ts") or where[1]
            self.state.remember_thread(note_id, where[0], thread_ts)
            return {"channel": where[0], "ts": thread_ts}
        return None
