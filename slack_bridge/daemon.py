"""The long-running bridge: Socket Mode listener, reply relay, keeper,
hand-off retries, picture expiry and the load guard.

Socket Mode keeps one outbound WebSocket to Slack; nothing listens on a
local port and no public URL exists. Slack SDK imports happen here only.
"""

from __future__ import annotations

import logging
import threading
import time

from .attachments import Attachments, SlackDownloader
from .config import Config, Secrets
from .guard import FirstmateRoots, LoadGuard, Rescue
from .handoff import Handoffs
from .inbox import FmInbox
from .keeper import Herdr, Keeper
from .router import ReplyRelay, Router, post_long
from .slack_api import WebSlack
from .state import State
from .suggest import Advisor

log = logging.getLogger("slack_bridge.daemon")

FRESH_QUEUE_POLL_SECONDS = 2.0
PICTURE_EXPIRY_POLL_SECONDS = 60 * 60


def _loop(name: str, interval: float, fn, stop: threading.Event,
          wake: threading.Event | None = None) -> threading.Thread:
    """Run ``fn`` every ``interval`` s, or as soon as ``wake`` is set."""
    def run() -> None:
        while not stop.is_set():
            if wake is not None:
                wake.clear()
            try:
                fn()
            except Exception:
                # Log the type and traceback location only; exceptions here can
                # carry Slack payloads, so no str(exc) of an API response.
                log.exception("%s loop iteration failed", name)
            (wake or stop).wait(interval)

    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


def build(cfg: Config, secrets: Secrets):
    state = State(cfg.state_dir)
    slack = WebSlack(secrets.bot_token, team_id=cfg.slack_team_id)
    inbox = FmInbox(cfg.fm_inbox, cfg.fm_home, timeout=cfg.handoff_timeout_seconds)

    def notify(text: str, channel: str | None = None,
               thread_ts: str | None = None) -> tuple[str, str | None]:
        """Post; returns (channel, ts of the first message)."""
        channel = channel or slack.dm_channel(secrets.captain_id, state)
        posted = post_long(slack, channel, text, thread_ts=thread_ts, escape=False)
        return channel, posted[0] if posted else None

    keeper = None
    if cfg.keeper_enabled:
        keeper = Keeper(cfg, state, Herdr(cfg.herdr_bin, cfg.herdr_session), notify)
    attachments = Attachments(cfg.state_dir / "inbound", SlackDownloader(secrets.bot_token))
    handoffs = Handoffs(slack, inbox, state, attachments)
    advisor = Advisor(cfg) if cfg.suggest_enabled else None
    rescue = Rescue(cfg, FirstmateRoots(keeper), state, advisor=advisor)
    guard = LoadGuard(cfg, rescue, state, notify) if cfg.load_alarm_enabled else None
    router = Router(secrets.captain_id, slack, inbox, state, keeper, handoffs, rescue,
                    advisor)
    relay = ReplyRelay(slack, inbox, state)
    return state, slack, router, relay, keeper, handoffs, guard, notify


def announce_restart(state, notify) -> None:
    """Answer a `restart bridge` request once the new process is up."""
    with state.update() as st:
        req, st["bridge_restart"] = st.get("bridge_restart"), None
    if req:
        notify(":white_check_mark: The bridge is back up.", channel=req.get("channel"),
               thread_ts=req.get("thread_ts"))


def run(cfg: Config, secrets: Secrets) -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    # Bolt and the SDK log request/response bodies at DEBUG; keep them quiet.
    for name in ("slack_bolt", "slack_sdk"):
        logging.getLogger(name).setLevel(logging.WARNING)

    state, slack, router, relay, keeper, handoffs, guard, notify = build(cfg, secrets)
    app = App(token=secrets.bot_token, logger=logging.getLogger("slack_bolt"))
    slack.client = app.client

    @app.event("message")
    def on_message(event, logger):  # noqa: ARG001 - bolt injects by name
        router.handle_message(event)

    stop = threading.Event()
    _loop("receipts", cfg.receipts_poll_seconds, relay.poll_once, stop)
    # Hands each message to Firstmate as soon as the listener records it, and
    # retries ones that timed out or were never announced; its first tick
    # also sweeps once for saved-but-unannounced Slack notes.
    _loop("handoff", cfg.handoff_poll_seconds, handoffs.tick, stop, wake=handoffs.wake)
    # Deletes saved pictures older than 7 days: now, then hourly.
    _loop("picture-expiry", PICTURE_EXPIRY_POLL_SECONDS, handoffs.attachments.expire, stop)
    if guard is not None:
        _loop("load-guard", cfg.load_poll_seconds, guard.tick, stop)
    if keeper is not None:
        _loop("fresh-session", FRESH_QUEUE_POLL_SECONDS, keeper.process_fresh_requests, stop)
        _loop("keeper", cfg.keeper_poll_seconds, keeper.check, stop)

    log.info("bridge starting (keeper=%s, pane=%s)", keeper is not None,
             cfg.target_pane or "auto")
    try:
        announce_restart(state, notify)  # Web API only; no socket needed
    except Exception:
        log.exception("restart notice failed")
    handler = SocketModeHandler(app, secrets.app_token)
    try:
        handler.start()  # blocks; reconnects on its own
    finally:
        stop.set()
        time.sleep(0.1)
