"""Outbound posts from Firstmate, and topic threads.

A topic key (main uses its task ids, e.g. ``example-build``)
names one Slack thread, so every update about a task lands in the same
thread. A key is linked either to the thread of an inbox note (``link``) or
to a new top-level message that ``say --topic`` posts the first time: in the
DM, or with ``--project`` in that project's private channel.

Images (``say --image``) go into the thread the text landed in; for a
top-level post that is the thread of its first message.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Callable

from .router import SlackPort, post_long
from .state import State

if TYPE_CHECKING:
    from .images import Image

TOPIC_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class OutboundError(RuntimeError):
    pass


def check_key(key: str) -> str:
    if not TOPIC_KEY.match(key or ""):
        raise OutboundError(f"invalid topic key {key!r}: use letters, digits, '.', '_', '-'")
    return key


def say(slack: SlackPort, state: State, dm_channel: Callable[[], str], body: str,
        thread: str | None = None, topic: str | None = None,
        project: str | None = None, images: list[Image] | None = None) -> tuple[int, str]:
    """Post ``body``, then ``images`` in the same thread; returns (messages posted, where)."""
    n, where, channel, thread_ts = _say_text(slack, state, dm_channel, body, thread, topic,
                                             project)
    if images:
        try:
            slack.upload_images(channel, images, thread_ts=thread_ts)
        except Exception as exc:
            raise OutboundError(f"posted {n} message(s) to {where}, but the "
                                f"image upload failed: {exc}") from None
        where += f" with {len(images)} image(s)"
    return n, where


def _say_text(slack, state, dm_channel, body, thread, topic, project):
    # Returns (messages posted, where, channel, ts of the thread they are in).
    if thread and topic:
        raise OutboundError("pass --thread or --topic, not both")
    if thread and project:
        raise OutboundError("--thread already names its thread; drop --project")
    if project:
        check_key(project)
        proj = state.read()["projects"].get(project)
        if proj is None:
            raise OutboundError(f"no channel recorded for project {project}; "
                                "run: slack-bridge channel create <key> <name>")
        home, home_name = (lambda: proj["channel"]), f"project {project} channel"
    else:
        home, home_name = dm_channel, "DM"
    if topic:
        return _say_topic(slack, state, home, body, check_key(topic))
    if thread:
        target = state.thread_for(thread)
        if target is None:
            channel = dm_channel()
            chunks = post_long(slack, channel, body)
            return (len(chunks), f"DM (no Slack thread recorded for note {thread})",
                    channel, chunks[0] if chunks else None)
        n = len(post_long(slack, target["channel"], body, thread_ts=target["ts"]))
        return n, f"thread of note {thread}", target["channel"], target["ts"]
    channel = home()
    chunks = post_long(slack, channel, body)
    return len(chunks), home_name, channel, chunks[0] if chunks else None


def _say_topic(slack, state, home, body, key):
    # Resolved before taking the lock: resolving may cache into the same state.
    channel = home()
    # Held across the post so two concurrent first posts for one key cannot
    # open two threads. A key that already has a thread keeps it, wherever
    # it is; ``home`` only decides where a new one opens.
    with state.update() as st:
        target = st["topics"].get(key)
        if target:
            n = len(post_long(slack, target["channel"], body, thread_ts=target["ts"]))
            return n, f"topic {key} thread", target["channel"], target["ts"]
        chunks = post_long(slack, channel, body, thread_rest=True)
        if not chunks or not chunks[0]:
            raise OutboundError("Slack returned no message ts; topic not linked")
        st["topics"][key] = {"channel": channel, "ts": chunks[0], "at": int(time.time())}
        return len(chunks), f"new topic {key} thread", channel, chunks[0]


def link(state: State, key: str, note_id: str, force: bool = False) -> str:
    """Link ``key`` to the Slack thread of inbox note ``note_id``."""
    check_key(key)
    target = state.thread_for(note_id)
    if target is None:
        raise OutboundError(f"no Slack thread recorded for note {note_id}")
    with state.update() as st:
        current = st["topics"].get(key)
        if current and (current["channel"], current["ts"]) == (target["channel"], target["ts"]):
            return f"topic {key} already linked to note {note_id}'s thread"
        if current and not force:
            raise OutboundError(f"topic {key} is already linked to another thread "
                                f"(ts {current['ts']}); pass --force to relink")
        st["topics"][key] = {"channel": target["channel"], "ts": target["ts"],
                             "at": int(time.time()), "note": note_id}
    return f"{'relinked' if current else 'linked'} topic {key} to note {note_id}'s thread"


def topics(state: State) -> list[tuple[str, str, str]]:
    """(key, channel, thread ts) for every topic, sorted by key."""
    return sorted((k, v["channel"], v["ts"]) for k, v in state.read()["topics"].items())
