"""slack-bridge command line.

  slack-bridge run                 the daemon (what the LaunchAgent runs)
  slack-bridge say [--thread <note-id> | --topic <key>] [--project <key>]
                   [--image <png|jpeg>]... [-|text...]
                                   post to the captain's DM (stdin with -);
                                   --topic posts in that topic's thread,
                                   opening one on first use; --project posts
                                   (and opens new topics) in the project's
                                   channel instead of the DM; --image attaches
                                   images in the same thread
  slack-bridge shot <url-or-html-file> [--width N] [--height N] [--viewport-only]
                    [--out file.png]
                                   render a page to a PNG with a headless
                                   browser; prints the PNG's path
  slack-bridge link [--force] <key> <note-id>
                                   make a note's Slack thread the topic's thread
  slack-bridge topics              list topic key -> thread (no message text)
  slack-bridge channel create <project-key> <channel-name>
                                   create the project's private channel,
                                   invite the captain, print the channel id
  slack-bridge channel link [--force] <project-key> <channel-id>
                                   map a project to a private channel the
                                   bot is already in
  slack-bridge channel list        list project key -> channel
  slack-bridge fresh-session [--wait]
                                   queue a fresh Firstmate session; --wait
                                   does it inline instead of via the daemon
  slack-bridge doctor              check setup without printing secrets

Global options (before or after the subcommand):
  --pane <id>        pin the Herdr pane instead of discovering Firstmate's
  --herdr <path>     herdr binary (tests point this at a fake)
  --state-dir <dir>  state directory
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import sys

from . import config as C
from .state import State

log = logging.getLogger("slack_bridge")


def _setup_logging(cfg: C.Config, to_stderr: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(cfg.log_file, maxBytes=2_000_000,
                                                  backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        to_stderr = True
    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pane", default=argparse.SUPPRESS)
    common.add_argument("--herdr", default=argparse.SUPPRESS)
    common.add_argument("--state-dir", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(prog="slack-bridge", parents=[common],
                                description="Private Slack bridge for Firstmate.")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", parents=[common], help="run the daemon")
    r.add_argument("--no-keeper", action="store_true")
    s = sub.add_parser("say", parents=[common], help="post to the captain's DM")
    s.add_argument("--thread", metavar="NOTE_ID",
                   help="reply in the Slack thread of this inbox note")
    s.add_argument("--topic", metavar="KEY",
                   help="post in this topic's thread; the first post opens it")
    s.add_argument("--project", metavar="KEY",
                   help="post in this project's channel; new topics open there")
    s.add_argument("--image", metavar="PATH", action="append",
                   help="attach a PNG or JPEG in the message's thread (repeatable)")
    s.add_argument("text", nargs="*", help="message text, or - for stdin")
    lk = sub.add_parser("link", parents=[common],
                        help="link a topic key to the Slack thread of an inbox note")
    lk.add_argument("--force", action="store_true",
                    help="relink a key that already points at another thread")
    lk.add_argument("key")
    lk.add_argument("note_id")
    sub.add_parser("topics", parents=[common], help="list topic key -> thread")
    ch = sub.add_parser("channel", parents=[common], help="project channels")
    chs = ch.add_subparsers(dest="channel_cmd", required=True)
    cc = chs.add_parser("create", parents=[common],
                        help="create a project's private channel and invite the captain")
    cc.add_argument("key")
    cc.add_argument("name")
    cl = chs.add_parser("link", parents=[common],
                        help="map a project to a private channel the bot is in")
    cl.add_argument("--force", action="store_true",
                    help="relink a project that already has another channel")
    cl.add_argument("key")
    cl.add_argument("channel_id")
    chs.add_parser("list", parents=[common], help="list project key -> channel")
    f = sub.add_parser("fresh-session", parents=[common],
                       help="start a fresh Firstmate session when it is idle")
    f.add_argument("--wait", action="store_true",
                   help="do it now in this process (blocks until idle or timeout)")
    f.add_argument("--timeout", type=float, default=None)
    sub.add_parser("doctor", parents=[common], help="check setup")
    sh = sub.add_parser("shot", parents=[common],
                        help="render a page to a PNG for say --image")
    sh.add_argument("target", help="http(s)/file URL or an HTML file")
    sh.add_argument("--width", type=int, default=1280, help="viewport width (default 1280)")
    sh.add_argument("--height", type=int, default=800, help="viewport height (default 800)")
    sh.add_argument("--viewport-only", action="store_true",
                    help="capture only the viewport, not the full page")
    sh.add_argument("--out", metavar="FILE.png", help="output path (default: a temp file)")
    return p


def _config(args) -> C.Config:
    cfg = C.Config.from_env()
    if getattr(args, "pane", None):
        cfg.target_pane = args.pane
    if getattr(args, "herdr", None):
        cfg.herdr_bin = args.herdr
    if getattr(args, "state_dir", None):
        from pathlib import Path

        cfg.state_dir = Path(args.state_dir)
    return cfg


def cmd_run(cfg: C.Config, args) -> int:
    if args.no_keeper:
        cfg.keeper_enabled = False
    from .daemon import run

    secrets = C.load_secrets(need_app_token=True)
    run(cfg, secrets)
    return 0


def cmd_say(cfg: C.Config, args) -> int:
    from . import images as I
    from .outbound import say
    from .slack_api import WebSlack

    # Checked before reading stdin or posting anything.
    attached = I.load_all(args.image)
    if args.text in ([], ["-"]):
        body = sys.stdin.read()
    else:
        body = " ".join(args.text)
    if not body.strip():
        print("slack-bridge say: empty message", file=sys.stderr)
        return 2
    secrets = C.load_secrets(need_app_token=False)
    state = State(cfg.state_dir)
    slack = WebSlack(secrets.bot_token, team_id=cfg.slack_team_id)
    n, where = say(slack, state, lambda: slack.dm_channel(secrets.captain_id, state), body,
                   thread=args.thread, topic=args.topic, project=args.project,
                   images=attached)
    log.info("say: posted %d message(s) to %s", n, where)
    print(f"posted {n} message(s) to {where}")
    return 0


def cmd_link(cfg: C.Config, args) -> int:
    from .outbound import link

    print(link(State(cfg.state_dir), args.key, args.note_id, force=args.force))
    return 0


def cmd_topics(cfg: C.Config, args) -> int:
    from .outbound import topics

    for key, channel, ts in topics(State(cfg.state_dir)):
        print(f"{key}\t{channel}\t{ts}")
    return 0


def cmd_channel(cfg: C.Config, args) -> int:
    from . import projects

    state = State(cfg.state_dir)
    if args.channel_cmd == "list":
        for key, channel, name in projects.channels(state):
            print(f"{key}\t{channel}\t{name}")
        return 0
    if args.channel_cmd == "link":
        print(projects.link(state, args.key, args.channel_id, force=args.force))
        return 0
    from .slack_api import WebSlack

    secrets = C.load_secrets(need_app_token=False)
    channel, msg = projects.create(WebSlack(secrets.bot_token, team_id=cfg.slack_team_id), state, secrets.captain_id,
                                   args.key, args.name)
    log.info("channel create: project=%s channel=%s", args.key, channel)
    print(f"{channel}\t{msg}")
    return 0


def cmd_fresh_session(cfg: C.Config, args) -> int:
    from .keeper import Herdr, Keeper

    state = State(cfg.state_dir)

    def notify(text, channel=None, thread_ts=None):
        print(text)

    keeper = Keeper(cfg, state, Herdr(cfg.herdr_bin, cfg.herdr_session), notify)
    if args.wait:
        ok, msg = keeper.fresh_session(args.timeout)
        print(msg)
        return 0 if ok else 1
    req = keeper.request_fresh_session(source="cli")
    print(f"queued fresh-session request {req}; the bridge daemon types /clear "
          "once Firstmate is idle and reports the result in Slack")
    return 0


def cmd_shot(cfg: C.Config, args) -> int:
    from .shot import render

    out = render(args.target, args.out, width=args.width, height=args.height,
                 full_page=not args.viewport_only, playwright=cfg.playwright_bin)
    print(out)
    return 0


def cmd_doctor(cfg: C.Config, args) -> int:
    from .keeper import Herdr, HerdrError, Keeper

    ok = True

    def line(good: bool, what: str) -> None:
        nonlocal ok
        ok = ok and good
        print(("ok   " if good else "FAIL ") + what)

    for svc in (C.KEYCHAIN_BOT_TOKEN, C.KEYCHAIN_APP_TOKEN, C.KEYCHAIN_CAPTAIN_ID):
        try:
            C.keychain_read(svc)
            line(True, f"Keychain item {svc} present")
        except C.ConfigError as exc:
            line(False, str(exc))
    line(os.access(cfg.fm_inbox, os.X_OK), f"fm-inbox.sh executable at {cfg.fm_inbox}")
    line(shutil.which(cfg.herdr_bin) is not None or os.access(cfg.herdr_bin, os.X_OK),
         f"herdr found ({cfg.herdr_bin})")
    import importlib.util

    line(importlib.util.find_spec("slack_bolt") is not None,
         "slack_bolt installed (scripts/install.sh installs it)")
    keeper = Keeper(cfg, State(cfg.state_dir), Herdr(cfg.herdr_bin, cfg.herdr_session),
                    lambda *a, **k: None)
    try:
        pane, reason = keeper.find_pane()
        if pane:
            line(True, f"Firstmate pane {pane.pane_id} agent={pane.agent or '-'} "
                       f"status={pane.agent_status}")
        else:
            line(False, reason)
    except HerdrError as exc:
        line(False, str(exc))
    print(f"state dir: {cfg.state_dir}\nlog file:  {cfg.log_file}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = _config(args)
    _setup_logging(cfg, to_stderr=args.cmd == "run" and sys.stderr.isatty())
    from .outbound import OutboundError
    from .shot import ShotError

    handlers = {"run": cmd_run, "say": cmd_say, "fresh-session": cmd_fresh_session,
                "doctor": cmd_doctor, "link": cmd_link, "topics": cmd_topics, "channel": cmd_channel,
                "shot": cmd_shot}
    try:
        return handlers[args.cmd](cfg, args)
    except C.ConfigError as exc:
        print(f"slack-bridge: {exc}", file=sys.stderr)
        return 2
    except (OutboundError, ShotError) as exc:
        print(f"slack-bridge {args.cmd}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
