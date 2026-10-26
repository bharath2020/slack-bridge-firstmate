"""SlackPort backed by slack_sdk's WebClient (imported lazily)."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from .state import State

log = logging.getLogger("slack_bridge.slack")


class SlackCallError(RuntimeError):
    """A Web API call Slack answered with ``ok: false``; carries only the code."""

    def __init__(self, method: str, code: str):
        super().__init__(f"{method} failed: {code}")
        self.method = method
        self.code = code


def _call(method: str, fn, **kwargs):
    # slack_sdk's SlackApiError keeps the API response on ``.response``; keep
    # its error code and drop the rest, which can echo request content.
    try:
        return fn(**kwargs)
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = response.get("error") if hasattr(response, "get") else None
        if not code:
            raise
        raise SlackCallError(method, str(code)) from None


def _upload_bytes(url: str, data: bytes) -> int:
    """POST raw bytes to a files.getUploadURLExternal URL; returns the HTTP status."""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class WebSlack:
    def __init__(self, bot_token: str, client=None, upload_bytes=_upload_bytes):
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=bot_token)
        self.client = client
        self.upload_bytes = upload_bytes

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str | None:
        kwargs = {"channel": channel, "text": text, "mrkdwn": True,
                  "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = self.client.chat_postMessage(**kwargs)
        return resp.get("ts")

    def react(self, channel: str, ts: str, name: str) -> None:
        self.client.reactions_add(channel=channel, timestamp=ts, name=name)

    def upload_images(self, channel: str, images, thread_ts: str | None = None) -> None:
        """Share ``images`` in ``channel`` (in ``thread_ts``'s thread) as one message.

        Slack's external upload flow: files.getUploadURLExternal per file, a
        POST of its bytes to the returned URL, then one
        files.completeUploadExternal that shares them all.
        """
        files = []
        for image in images:
            resp = _call("files.getUploadURLExternal", self.client.files_getUploadURLExternal,
                         filename=image.name, length=len(image.data))
            # The upload URL is a capability; errors name the file, never the URL.
            try:
                status = self.upload_bytes(resp["upload_url"], image.data)
            except OSError as exc:
                raise RuntimeError(f"upload of {image.name} failed: "
                                   f"{exc.__class__.__name__}") from None
            if status != 200:
                raise RuntimeError(f"upload of {image.name} failed: HTTP {status}")
            files.append({"id": resp["file_id"], "title": image.name})
        kwargs = {"files": files, "channel_id": channel}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        _call("files.completeUploadExternal", self.client.files_completeUploadExternal, **kwargs)

    def dm_channel(self, captain_id: str, state: State) -> str:
        """The captain's DM channel with the bot, cached in state."""
        cached = state.read().get("dm_channel")
        if cached:
            return cached
        resp = self.client.conversations_open(users=captain_id)
        channel = resp["channel"]["id"]
        with state.update() as st:
            st["dm_channel"] = channel
        return channel

    def create_private_channel(self, name: str) -> str:
        """Create a private channel the bot is a member of; returns its id."""
        resp = _call("conversations.create", self.client.conversations_create,
                     name=name, is_private=True)
        return resp["channel"]["id"]

    def invite(self, channel: str, user: str) -> None:
        """Invite ``user``; a user already in the channel is not an error."""
        try:
            _call("conversations.invite", self.client.conversations_invite,
                  channel=channel, users=user)
        except SlackCallError as exc:
            if exc.code != "already_in_channel":
                raise

    def find_private_channel(self, name: str) -> str | None:
        """The id of the private channel ``name`` among the bot's own, if any."""
        cursor = None
        while True:
            kwargs = {"types": "private_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = _call("conversations.list", self.client.conversations_list, **kwargs)
            for ch in resp.get("channels") or []:
                if ch.get("name") == name:
                    return ch["id"]
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None
