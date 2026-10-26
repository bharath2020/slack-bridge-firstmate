"""Hand Slack messages to Firstmate without ever dropping one.

A message is recorded in the state file and acknowledged with a reaction at
once, on the Slack listener thread; ``fm-inbox.sh note`` runs only on the
daemon's hand-off loop, which ``submit`` wakes, so a slow hand-off never holds
a listener worker that ``load``, ``shed`` or ``restart`` needs. The record
stays until Firstmate has been woken for it. A hand-off that times out or
fails, or a note that was saved but not announced, is retried with backoff.
``note --request-id`` is idempotent, so a retry of a note that was in fact
saved replays it and re-appends the wake if it is still missing. The pending
record keeps the message text only until the note is saved; the state
directory is private (0700, file 0600).

A message with files attached keeps their references in its record until
the loop's first attempt saves the pictures (``attachments``) and appends
their local paths to the text; a picture that could not be fetched is
named in the note and in the captain's thread, and the text still goes.

The loop's first tick also sweeps ``receipts --all-pending`` once and wakes
Firstmate for any Slack note that is saved but unannounced and that this
bridge does not track (for example one left behind by an older bridge).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import text as T
from .attachments import Attachments, reason_text
from .inbox import FmInbox, InboxError, NoteResult
from .state import State

log = logging.getLogger("slack_bridge.handoff")

QUEUED_REACTION = "eyes"
DELAYED_REACTION = "hourglass_flowing_sand"
REQUEST_PREFIX = "slack-"

RETRY_BASE_SECONDS = 15.0
RETRY_MAX_SECONDS = 300.0
CLEARED = {"queued:created", "queued:replay"}


def request_id(channel: str, ts: str) -> str:
    return f"{REQUEST_PREFIX}{channel}-{ts}"


def parse_request_id(rid: str) -> tuple[str, str] | None:
    """(channel, ts) of a bridge request id, or None for any other id."""
    if not rid or not rid.startswith(REQUEST_PREFIX):
        return None
    channel, sep, ts = rid[len(REQUEST_PREFIX):].partition("-")
    return (channel, ts) if sep and channel and ts else None


def backoff(attempts: int) -> float:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1))


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


class Handoffs:
    def __init__(self, slack, inbox: FmInbox, state: State, attachments: Attachments,
                 clock: Callable[[], float] = time.time):
        self.slack = slack
        self.inbox = inbox
        self.state = state
        self.clock = clock
        self.attachments = attachments
        self.wake = threading.Event()  # set by submit; the hand-off loop waits on it
        self._swept = False

    # Entry from the router ----------------------------------------------------

    def submit(self, channel: str, ts: str, thread_ts: str, text: str,
               files: list[dict] | None = None) -> str:
        """Record one hand-off, react and wake the hand-off loop; never waits
        for ``fm-inbox.sh`` or a download."""
        rid = request_id(channel, ts)
        now = self.clock()
        with self.state.update() as st:
            if rid not in st["handoffs"]:
                st["handoffs"][rid] = {
                    "channel": channel, "ts": ts, "thread_ts": thread_ts, "text": text,
                    "at": int(now), "attempts": 0, "next_at": 0, "delayed": False,
                }
                if files:
                    st["handoffs"][rid]["files"] = files
        self._react(channel, ts, QUEUED_REACTION)
        self.wake.set()
        return "accepted"

    # Daemon loop -------------------------------------------------------------

    def tick(self) -> int:
        """Attempt every due hand-off (sweeping first, once); returns how many
        cleared."""
        cleared = 0
        if not self._swept:
            self._swept = True
            cleared += self.sweep()
        now = self.clock()
        for rid, rec in list(self.state.read()["handoffs"].items()):
            if rec.get("next_at", 0) <= now and self._attempt(rid) in CLEARED:
                cleared += 1
        return cleared

    def sweep(self) -> int:
        """Wake Firstmate for Slack notes that are saved but were never announced."""
        try:
            data = self.inbox.receipts(after=None, all_pending=True)
        except InboxError as exc:
            log.warning("sweep skipped: %s", exc)
            return 0
        tracked = set(self.state.read()["handoffs"])
        woken = 0
        for note in data.get("pending", []):
            rid = note.get("request_id") or ""
            where = parse_request_id(rid)
            if where is None or rid in tracked:
                continue
            if note.get("announced") is not False or note.get("acknowledged"):
                continue
            try:
                ok = self.inbox.announce(note["id"])
            except InboxError as exc:
                log.warning("sweep announce failed: note=%s err=%s", note["id"], exc)
                continue
            if not ok:
                log.warning("sweep announce refused: note=%s", note["id"])
                continue
            woken += 1
            channel, ts = where
            target = self.state.thread_for(note["id"])
            if target is None:
                self.state.remember_thread(note["id"], channel, ts)
                target = {"channel": channel, "ts": ts}
            log.info("sweep: woke Firstmate for note=%s request_id=%s", note["id"], rid)
            self._post(target["channel"], ":warning: This message was saved for Firstmate "
                       "but it was never woken for it; it has been woken now.",
                       target["ts"])
        return woken

    # One attempt -------------------------------------------------------------

    def _attempt(self, rid: str) -> str:
        rec = self.state.read()["handoffs"].get(rid)
        if rec is None:
            return "gone"
        channel, thread_ts = rec["channel"], rec["thread_ts"]
        if rec.get("files") and not rec.get("note_id"):
            rec = self._save_files(rid, rec)
        attempts = rec.get("attempts", 0) + 1
        try:
            if rec.get("note_id"):
                result = NoteResult(rec["note_id"], "replay",
                                    self.inbox.announce(rec["note_id"]), False)
            else:
                result = self.inbox.note(rid, rec["text"])
        except InboxError as exc:
            return self._failed(rid, rec, attempts, str(exc))

        if rec.get("note_id") is None:
            self.state.remember_thread(result.note_id, channel, thread_ts)
        log.info("queued: request_id=%s note=%s outcome=%s announced=%s attempt=%d", rid,
                 result.note_id, result.outcome, result.announced, attempts)
        if result.announced is False and not result.acknowledged:
            first_warning = not rec.get("delayed")
            with self.state.update() as st:
                cur = st["handoffs"].get(rid)
                if cur is not None:
                    cur.update(note_id=result.note_id, attempts=attempts, delayed=True,
                               next_at=int(self.clock() + backoff(attempts)))
                    cur.pop("text", None)  # saved: the body now lives only in Firstmate
            if first_warning:
                self._post(channel, ":warning: Saved for Firstmate, but it could not be "
                           "woken yet; the bridge keeps retrying.", thread_ts)
            return "queued:unannounced"

        with self.state.update() as st:
            st["handoffs"].pop(rid, None)
        if rec.get("delayed"):
            self._post(channel, ":white_check_mark: Firstmate now has this message "
                       f"({_ago(self.clock() - rec['at'])} late).", thread_ts)
        return f"queued:{result.outcome}"

    def _save_files(self, rid: str, rec: dict) -> dict:
        """Save the message's pictures once, folding their paths into its text."""
        saved = self.attachments.save(rid, rec["files"])
        text = "\n\n".join(p for p in (rec["text"], "\n".join(saved.lines)) if p)
        with self.state.update() as st:
            cur = st["handoffs"].get(rid)
            if cur is not None:
                cur["text"] = text
                cur.pop("files", None)
        rec = dict(rec, text=text)
        rec.pop("files", None)
        if saved.failed:
            skipped = "\n".join(f"• {T.escape(name)}: {reason_text(reason)}"
                                 for name, reason in saved.failed)
            these = "this picture" if len(saved.failed) == 1 else "these pictures"
            self._post(rec["channel"], f":warning: Firstmate gets this message, but not "
                       f"{these}:\n{skipped}", rec["thread_ts"])
        return rec

    def _failed(self, rid: str, rec: dict, attempts: int, err: str) -> str:
        log.error("note hand-off failed: request_id=%s attempt=%d err=%s", rid, attempts, err)
        with self.state.update() as st:
            cur = st["handoffs"].get(rid)
            if cur is not None:
                cur.update(attempts=attempts, delayed=True,
                           next_at=int(self.clock() + backoff(attempts)))
        if not rec.get("delayed"):
            self._react(rec["channel"], rec["ts"], DELAYED_REACTION)
            self._post(rec["channel"], f"Firstmate is slow to take this ({T.escape(err)}). "
                       "The bridge keeps retrying; no need to resend. Send `load` to see "
                       "what is busy on the Mac.", rec["thread_ts"])
        return "note-delayed"

    # Slack -------------------------------------------------------------------

    def _post(self, channel: str, text: str, thread_ts: str | None) -> None:
        try:
            self.slack.post(channel, text, thread_ts=thread_ts)
        except Exception as exc:  # never let a Slack hiccup lose the hand-off
            log.warning("hand-off notice not posted: %s", exc.__class__.__name__)

    def _react(self, channel: str, ts: str, name: str) -> None:
        try:
            self.slack.react(channel, ts, name)
        except Exception as exc:  # already_reacted on a retry, or scope missing
            log.info("reaction %s skipped: %s", name, exc.__class__.__name__)
