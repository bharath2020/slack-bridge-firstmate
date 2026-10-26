"""Project channels: one private Slack channel per Firstmate project.

The bot creates the channel itself (``conversations.create`` with
``is_private``), invites the captain, and records project key -> channel in
the bridge state. The captain's messages there are queued like DMs, tagged
with the project (``[project:<key>]``), and ``say --project`` opens topic threads
in the channel instead of the DM.
"""

from __future__ import annotations

import re
import time
from typing import Protocol

from .outbound import OutboundError, check_key
from .slack_api import SlackCallError
from .state import State

# Slack's own rule for channel names: lowercase letters, digits, '-' and '_',
# at most 80 characters.
CHANNEL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{2,}$")


class ChannelPort(Protocol):
    def create_private_channel(self, name: str) -> str: ...
    def invite(self, channel: str, user: str) -> None: ...
    def find_private_channel(self, name: str) -> str | None: ...


def _check_name(name: str) -> str:
    if not CHANNEL_NAME.match(name or ""):
        raise OutboundError(f"invalid channel name {name!r}: use lowercase letters, digits, "
                            "'-' and '_' (at most 80)")
    return name


def create(slack: ChannelPort, state: State, captain_id: str, key: str,
           name: str) -> tuple[str, str]:
    """Create (or reuse) ``key``'s private channel; returns (channel id, message)."""
    check_key(key)
    _check_name(name)
    projects = state.read()["projects"]
    current = projects.get(key)
    if current:
        # Re-inviting is a no-op for a member and heals an invite that failed.
        _invite(slack, current["channel"], captain_id, current.get("name") or name)
        shown = f"#{current['name']}" if current.get("name") else current["channel"]
        return current["channel"], f"project {key} already has channel {shown}; reused it"
    for other, proj in projects.items():
        if proj.get("name") == name:
            raise OutboundError(f"channel #{name} is already project {other}'s channel")
    try:
        channel = slack.create_private_channel(name)
    except SlackCallError as exc:
        if exc.code != "name_taken":
            raise OutboundError(str(exc))
        raise OutboundError(_name_taken(slack, key, name))
    with state.update() as st:
        st["projects"][key] = {"channel": channel, "name": name, "at": int(time.time())}
    # Recorded before the invite, so a failed invite never orphans the
    # channel: rerunning the same command retries only the invite.
    _invite(slack, channel, captain_id, name)
    return channel, f"created private channel #{name} for project {key}; invited the captain"


def _name_taken(slack: ChannelPort, key: str, name: str) -> str:
    try:
        found = slack.find_private_channel(name)
    except SlackCallError:
        found = None
    if found:
        return (f"a channel named #{name} already exists ({found}) and was not duplicated; "
                f"to use it: slack-bridge channel link {key} {found}")
    return (f"a channel named #{name} already exists and the bot is not in it; "
            "pick another name, or invite the bot there and use channel link")


def _invite(slack: ChannelPort, channel: str, captain_id: str, name: str) -> None:
    try:
        slack.invite(channel, captain_id)
    except SlackCallError as exc:
        raise OutboundError(f"channel #{name} ({channel}) exists but inviting the captain "
                            f"failed ({exc.code}); rerun the same command to retry")


def link(state: State, key: str, channel: str, force: bool = False) -> str:
    """Map ``key`` to an existing private channel the bot is already in."""
    check_key(key)
    if not CHANNEL_ID.match(channel or ""):
        raise OutboundError(f"invalid channel id {channel!r}")
    with state.update() as st:
        projects = st["projects"]
        owner = next((k for k, v in projects.items() if v["channel"] == channel), None)
        if owner == key:
            return f"project {key} already linked to {channel}"
        if owner:
            raise OutboundError(f"{channel} is already project {owner}'s channel")
        current = projects.get(key)
        if current and not force:
            raise OutboundError(f"project {key} is already linked to {current['channel']}; "
                                "pass --force to relink")
        projects[key] = {"channel": channel, "name": None, "at": int(time.time())}
    return f"{'relinked' if current else 'linked'} project {key} to {channel}"


def channels(state: State) -> list[tuple[str, str, str]]:
    """(key, channel id, name or '-') for every project, sorted by key."""
    return sorted((k, v["channel"], v.get("name") or "-")
                  for k, v in state.read()["projects"].items())
