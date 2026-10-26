# slack-bridge

Connect Slack DMs and private project channels to Firstmate, an agent inbox on your Mac. Send a note from Slack and receive replies and outcomes in the same thread. The bridge runs as a macOS user LaunchAgent and connects out over Socket Mode, with no public server or inbound port.

```text
phone (Slack DM) ──► Slack ◄──Socket Mode── bridge ──► fm-inbox.sh note ──► Firstmate
                                              ▲                                 │
                                              └── fm-inbox.sh receipts ◄── fm-inbox.sh reply
```

## Setup

Start with the [setup guide](SETUP.md): prerequisites, one-command install, each wizard prompt, verification, updates, uninstall, and troubleshooting.

From a clone:

```sh
./scripts/install.sh
```

Once this repository is published, the same wizard can start without a clone:

```sh
curl -fsSL https://raw.githubusercontent.com/bharath2020/slack-bridge-firstmate/main/scripts/install.sh | bash
```

The installer clones or updates the managed copy, creates a virtual environment, guides you through your Slack workspace and Firstmate folder, stores tokens in your login Keychain, starts the service, and sends a test DM. One macOS login account supports one workspace. Review [the publish audit](docs/publish-audit.md) before publishing; visibility and license decisions are pending.

## Slack CLI project

This repo is a Slack CLI project for Bolt for Python:

| File | Role |
| --- | --- |
| `manifest.json` | the app manifest (`.slack/config.json` sets `manifest.source` to `local`) |
| `.slack/hooks.json` | `get-hooks` from the official `slack-cli-hooks` package, plus a `deploy` hook |
| `scripts/install.sh` | entry point from a clone or remote shell; safely clones/updates then starts the wizard |
| `scripts/setup.sh` | interactive wizard; runs `slack login`, `slack deploy` and `scripts/install-service.sh` |
| `scripts/install-service.sh` | installs dependencies and renders/loads the LaunchAgent |
| `scripts/slack-deploy-hook.sh` | run by `slack deploy` with the app's tokens; stores them in the Keychain and runs `scripts/install-service.sh` |
| `app.py` | `slack run` entry point for a drill only (keeper off, separate state dir) |
| `.venv/` | the project venv the CLI's Python hooks and the LaunchAgent both use (gitignored) |

The always-on bridge is the LaunchAgent, not `slack run`; see "What the Slack
CLI cannot do here" in SETUP.md.

## What it does

| Captain sends (DM or project channel) | Bridge does |
| --- | --- |
| any text | reacts 👀 at once, then `fm-inbox.sh note --request-id slack-<channel>-<ts> -` (body on stdin) |
| `status` | answers from `fm-inbox.sh status` (read-only, Firstmate is not woken) |
| `new session` (or `/new-session`) | queues a fresh session: `/clear` is typed into the Firstmate pane once it is idle; result posted in the thread |
| `restart` | same as `new session`; while Firstmate is busy it also suggests waiting or `restart force` |
| `restart force` | interrupts Firstmate's turn with one Escape, then starts the fresh session once it is idle |
| `load` | what is loading the Mac, answered by the bridge itself (see [Emergency commands](#emergency-commands)) |
| `shed` | lists the heaviest processes it could act on, with a suggested action; touches nothing |
| `shed pause` / `shed quit` | pauses (SIGSTOP) or quits (SIGTERM) the processes of that list |
| `yes` / `ok` | as a reply in the thread of a `shed` list or load alarm, within 10 min: takes its suggestion |
| `resume` | while `shed pause` has paused processes: continues them (SIGCONT) |
| `restart bridge` | restarts the bridge's own LaunchAgent; it says so in the thread when it is back |

Only the configured member id is accepted, in the DM with the bot or in a
private channel the bot is a member of. Anything else — other people, public
channels, group DMs, bots, edits — is dropped without a reply and logged with
the reason only, never the message text. The bridge never reads channel
history; it sees only new messages Slack sends it.

A message with pictures attached (Slack subtype `file_share`) passes the
same checks. The bridge downloads each PNG, JPEG, GIF, WebP or HEIC picture
(by Slack's mimetype, at most 20 MB each) with the bot token, which needs
the `files:read` scope, and saves it under
`~/Library/Application Support/slack-bridge/inbound/<request-id>/` (0700,
files 0600). The note gets one `[picture: <absolute path>]` line per saved
picture after the text, so Firstmate can open it; a picture alone still
makes a note. Other files are not downloaded: the note names each as
skipped. When a picture cannot be fetched (no `files:read`, an HTTP error,
too large, or a file Slack sent without its details or with a link off
`files.slack.com`), the text still goes, the note names the picture, and the
bridge says in the thread which picture was skipped and why. The download
runs on the hand-off loop, once per message; logs carry only counts and
reasons, never file names, links or contents. A message with a file is
never taken as a command. Received pictures are kept for 7 days, then
deleted.

A note from a private channel starts with the project it belongs to,
`[project:<key>] `, so Firstmate knows the context (`[channel:<id>] ` for a
private channel the bot is in that is not mapped to a project). DM notes carry
no prefix. Every command works in a channel too, and answers in
that message's thread. Slack retries are safe:
the request id makes a repeat return the original note.

### Never losing a message

The 2026-10-24 incident: at load ~50 on 14 cores, `fm-inbox.sh note` took
longer than the bridge's 60 s limit; the notes were saved but Firstmate was
never woken, and the bridge gave up. Now:

- Each message is recorded in the state file and gets its 👀 at once; the
  Slack listener never waits for `note`. The daemon's hand-off loop, woken
  by the new record, runs `note`, so a slow hand-off never delays `load`,
  `shed` or `restart`. The record stays until Firstmate has been woken for
  it. The text is kept only until the note is saved (the state directory is
  private, 0700).
- The hand-off limit is 180 s (`SLACK_BRIDGE_HANDOFF_TIMEOUT`).
- On a timeout or failure the bridge reacts ⏳ and says in the thread that
  it keeps retrying (no need to resend). A note saved but not announced
  gets a ⚠️ note instead. The daemon retries every pending hand-off with the
  same request id, backing off 15 s, 30 s, 60 s … up to 5 min, until
  Firstmate is woken: `note` replays a saved note and re-appends its missing
  wake, and `announce <id>` wakes a note whose id is already known.
- When a late message finally gets through, the thread says how late it was.
- Once at start the bridge sweeps `fm-inbox.sh receipts --all-pending` for
  Slack notes (request id `slack-…`) that are saved but were never
  announced and that it is not already retrying (for example ones an older
  bridge gave up on), wakes Firstmate for each and says so in its thread.
- A reply to a note whose hand-off timed out before the bridge learned its id
  still reaches the right thread: the relay maps it from the note's request
  id.

Every reply Firstmate publishes with `fm-inbox.sh reply <note-id>` for a
Slack-originated note is posted back into that message's thread. The bridge
polls `fm-inbox.sh receipts --after <cursor>` every 5 s and stores the cursor
and the note id → thread mapping in
`~/Library/Application Support/slack-bridge/state.json`. On its very first
run it starts at the present cursor rather than replaying history.

## For Firstmate

```sh
# Post an outcome or decision to the captain unprompted (stdin or args).
bin/slack-bridge say - <<'EOF'
*Done:* the example-task lane merged.
EOF

# Answer in the Slack thread of a note that came from Slack.
bin/slack-bridge say --thread <note-id> "Follow-up on this"

# Topic threads: one Slack thread per task, keyed by the task id.
bin/slack-bridge link example-build <note-id>   # captain's message becomes the thread
bin/slack-bridge say --topic example-build "Build passed"
bin/slack-bridge topics          # key<TAB>channel<TAB>thread ts, no message text

# Project channels: one private channel per project, created by the bot.
bin/slack-bridge channel create example-project example-project   # prints <channel-id><TAB>what it did
bin/slack-bridge channel link example-project C0123ABC       # map a channel the bot is already in
bin/slack-bridge channel list    # key<TAB>channel id<TAB>name
bin/slack-bridge say --topic example-build --project example-project "Build started"

# Boards and reports: Slack never renders HTML, so send a screenshot plus the link.
# Render the report HTML file, or a URL you want to share.
png=$(bin/slack-bridge shot path/to/board.html)   # or an http(s) URL
bin/slack-bridge say --topic example-build --image "$png" \
  "Board: https://example.com/report"

# Ask for a fresh session (e.g. from a Claude Code hook). Queued for the
# daemon, which waits until the pane is idle, so a hook running inside the
# Firstmate turn never blocks on itself. --wait does it inline instead.
bin/slack-bridge fresh-session
```

`say --topic <key>` posts in the key's thread; for a key not linked yet it
posts a new top-level DM message and links the key to it, so a task the
captain did not start from Slack still gets one thread. With
`--project <key>` that new thread opens in the project's channel instead of
the DM (and `say --project <key>` without `--topic` posts there top-level);
a key that already has a thread keeps it, wherever it is. `link` is
idempotent, works for notes from channels as well as the DM, and refuses to move a key to a different thread without
`--force`. Topic links live in the same state file as the note threads, and
the captain's replies inside a topic thread are queued like any other DM,
with Firstmate's answers coming back into that thread.

`channel create <project-key> <channel-name>` creates a private channel
(`conversations.create`, `is_private`), invites the captain, records
project key → channel id in the state file and prints the id. It is
idempotent: a project that already has a channel reuses it (re-inviting the
captain, which also retries an invite that failed), and a channel name that
already exists is reported, with its id when the bot is in it (that
lookup, `conversations.list`, is the only use of `groups:read`), never
created twice. `channel link` maps a project to a private channel someone
else made; invite the bot there first (`/invite @Firstmate`). Relinking a
project needs `--force`, and one channel belongs to one project.

`say --image <path>` (repeatable, up to 10) attaches PNG or JPEG files of at
most 10 MB each, checked by their content before anything is posted; any
other file is refused. The text posts first, then the images go into the
same thread as one message: the topic's or note's thread, or for a
top-level post the thread of its first message. Uploads use Slack's
external upload flow (`files.getUploadURLExternal`, a POST of the bytes,
then one `files.completeUploadExternal` with `channel_id` and `thread_ts`)
and need the `files:write` scope. If the upload fails after the text was
posted, `say` exits 1 and says what was posted.

`shot <url-or-html-file> [--width N] [--height N] [--viewport-only] [--out file.png]`
renders a page to a PNG on this Mac and prints its path (a temp file unless
`--out`). It uses the Playwright CLI already on PATH driving the installed
Google Chrome (`playwright screenshot --channel chrome --full-page`; no
browser download). For a report editor, pass the exported HTML file to capture the report itself rather than the editor controls. If Playwright
is missing, fails, times out or writes no usable PNG, `shot` exits 1 and
names the failure. Width defaults to 1280, the viewport height to 800; a
full-page shot is as tall as the page, so use `--viewport-only` for a very
long one.

Text is escaped for Slack mrkdwn (`&`, `<`, `>`), keeps `*bold*`/`_italic_`/
code fences, and is split into ≤3500-character messages; a split through a
code block closes and reopens the fence.

## Keeper

The daemon also keeps the main Firstmate pane alive, through Herdr pane-level
commands only (`pane list`, `pane process-info`, `pane run`,
`pane send-keys`). It never runs Herdr session/server lifecycle commands and
never creates or closes panes.

- **Target:** the single pane in Herdr session `default` whose cwd is the
  Firstmate home and whose label contains "Firstmate". Zero or several
  matches means no target (it alerts instead). `--pane <id>` /
  `SLACK_BRIDGE_PANE` pins a pane and skips discovery.
- **Fresh session:** typed only while the pane's agent is `idle`/`done`;
  waits up to 5 min (`SLACK_BRIDGE_FRESH_WAIT`) and gives up rather than
  interrupting a working turn or a permission prompt. Queued requests
  collapse into one `/clear`. Only `restart force` interrupts: it sends one
  Escape (`herdr pane send-keys <pane> esc`, no process is killed), then
  waits for `idle` the same way.
- **Restart:** checked every 60 s. If the pane reports no agent on two
  consecutive checks *and* its foreground process is its own shell, the
  keeper runs `cd <cwd> && claude` in it and tells the captain in Slack. At
  most 3 restarts per hour (persisted across daemon restarts); past that it
  only alerts, once an hour.

## Emergency commands

`load`, `shed`, `resume`, `restart` and `restart bridge` are handled inside
the bridge process. They never depend on Firstmate, so they work while
Firstmate is stuck, and like every command they are accepted only from the
configured member. `shed`, the load alarm and `restart` may ask a model for
their suggestion (see [Suggestions](#suggestions)); that call is optional,
capped at 25 s, and falls back to the rules. The LaunchAgent runs at `ProcessType` `Interactive` so
the bridge stays responsive on a loaded Mac.

**Protected processes** are never paused, quit or demoted: the bridge, the
main Firstmate session (the Firstmate pane's whole process tree), Claude
Remote Control (any process whose command line mentions `remote-control`),
each with its ancestors and descendants, and macOS system processes
(anything under `/System`, `/usr/libexec`, `/usr/sbin` or `/sbin`, plus
`launchd`, `WindowServer` and `kernel_task`). If the Firstmate pane cannot be
found through Herdr, every agent session's tree is protected too.

- **`load`** reads `os.getloadavg`, one `sysctl` (memory pressure, swap,
  RAM) and one `ps`, nothing heavier. It answers with the load averages
  against the core count and the alarm threshold, memory pressure, and the
  top 5 processes by CPU and by memory, whatever they are. Processes are
  shown by executable name only, never their command lines.
- **`shed`** lists the heaviest unprotected processes, whatever they are,
  with a suggested action (see [Suggestions](#suggestions)). The rules
  suggest `quit` when memory is the problem (pressure `warning`/`critical`,
  or 8 GB of swap or more), listing up to 5 of the largest by memory (1 GB or
  more), and otherwise `pause`, listing up to 5 of the busiest by CPU (10% of
  a core or more). When nothing qualifies for the suggested action it
  switches to the other one, so it never says there is nothing to shed while
  a heavy unprotected process runs. It touches nothing. The list, with each
  pid's command line, is kept in the state file for 10 minutes under the
  thread it was posted in; up to 5 lists (yours and the load alarm's) are
  kept at once, each expiring on its own, so a newer list never replaces
  another.
- **`shed pause`** and **`shed quit`** act on the list of the thread they
  are sent in; sent top-level, they act on the newest list. In a thread
  without a current list (none, used up or expired) they never act on
  another thread's list. **`yes`**/**`ok`** take the
  suggested action of that thread's list, and only as a reply in a list's
  thread (a top-level `yes`, or one in any other thread, goes to Firstmate
  as usual). Each pid is checked again first and left alone if it
  exited, is now a different command, or became protected. `pause` sends
  SIGSTOP and records the process; `quit` sends SIGTERM and reports anything
  still running 10 s later. The reply names every process paused, quit or
  left alone. Without a current list, `shed pause`/`shed quit` touch nothing
  and post a fresh list.
- **`resume`** sends SIGCONT to every process `shed pause` stopped (when
  nothing is paused, `resume` is an ordinary message for Firstmate).
- **`restart`** is `new session`: the keeper's fresh-session path, which
  never interrupts a working turn. While Firstmate is busy, a second reply
  suggests waiting or `restart force` (see [Suggestions](#suggestions)); the
  rules suggest `restart force` once it has shown the same busy status for
  10 min (`SLACK_BRIDGE_STUCK_AFTER`). Either way it keeps waiting until the
  captain sends `restart force`.
- **`restart force`** interrupts the current turn with one Escape through
  Herdr (nothing is killed), waits for the pane to report idle, then types
  `/clear` as `new session` does.
- **`restart bridge`** runs `launchctl kickstart -k gui/<uid>/com.firstmate.slack-bridge`
  detached, so launchd restarts the bridge; the new process answers
  "The bridge is back up" in the same thread. The `slack run` drill refuses
  it because it is not the LaunchAgent.

### Suggestions

The `shed` and `restart` suggestions come from one `claude -p` call (with
`--safe-mode --restricted --strict-mcp-config --no-session-persistence`), or
`codex exec --sandbox read-only` when `claude` is not installed, using the
CLI's existing login. It runs under `taskpolicy -b` so it does not add to the
overload, and is killed after 25 s (`SLACK_BRIDGE_SUGGEST_TIMEOUT`). The
prompt holds only executable names, CPU, memory, memory pressure, swap, load
and Firstmate's status and busy time, never a command line or argument.

The answer is only a suggestion: it may pick among the listed unprotected
pids and `pause`/`quit` (or `wait`/`force` for `restart`); anything else is
ignored, protected processes can never be listed, and nothing happens until
the captain replies. The reply says which CLI made the suggestion. If no CLI
is found, it times out, fails or gives no usable answer, the rules above
decide and the reply says why ("Rule-based: …"). `SLACK_BRIDGE_SUGGEST=0`
turns the call off.

## Load alarm and fleet guard

Every 30 s the daemon samples the load. When the 5-minute load average stays
above the threshold (default 2 × the core count, 28 on 14 cores) for 5
minutes it posts one DM alert with the top 3 processes by CPU and by memory
and the same list and suggestion `shed` gives, so a one-word `yes` in the
alert's thread acts on it. The alert's list is kept alongside any list the
captain asked for, never in place of it. Alerts are at least 30 min apart; an overload that starts inside
that window is announced as soon as the 30 min have passed. When the 5-minute
average drops below 80% of the threshold it posts a recovery note.

The fleet guard starts as soon as the 1-minute load average crosses the
threshold, before any alert: it moves every heavy unprotected process (10% of
a core or 1 GB of memory) and all of its descendants, such as the build
tools an agent worker runs, to background priority
(`taskpolicy -b -p <pid>`), including ones that become heavy later. It
restores them (`taskpolicy -B`) when the alarm clears, or, if the alarm never
went off, once both the 1- and 5-minute averages are below 80% of the
threshold. The demoted pids are kept in the state file so a bridge restart
still restores them. The protected processes above are never demoted.

## Configuration

Secrets come only from the login Keychain (service names
`slack-bridge.bot-token`, `slack-bridge.app-token`,
`slack-bridge.captain-user-id`, account `$USER`). Everything else has a
default and an environment override:

| Variable | Default |
| --- | --- |
| `SLACK_BRIDGE_FM_HOME` | the folder `scripts/setup.sh` saved in `<state dir>/fm-home` (or in the default state dir, so the `slack run` drill finds it too), else `~/Documents/firstmate` |
| `SLACK_BRIDGE_STATE_DIR` | `~/Library/Application Support/slack-bridge` |
| `SLACK_BRIDGE_LOG_FILE` | `~/Library/Logs/slack-bridge/bridge.log` |
| `SLACK_BRIDGE_HERDR` / `SLACK_BRIDGE_HERDR_SESSION` | `herdr` / `default` |
| `SLACK_BRIDGE_PANE` | unset (discover) |
| `SLACK_BRIDGE_AGENT_CMD` | `claude` |
| `SLACK_BRIDGE_KEEPER` | `1` (`0` disables the keeper) |
| `SLACK_BRIDGE_RESTART_LIMIT` | `3` per hour |
| `SLACK_BRIDGE_FRESH_WAIT` | `300` s a fresh session waits for Firstmate to be idle |
| `SLACK_BRIDGE_STUCK_AFTER` | `600` s of one busy status before `restart` suggests `restart force` |
| `SLACK_BRIDGE_PLAYWRIGHT` | `playwright` (for `shot`) |
| `SLACK_BRIDGE_HANDOFF_TIMEOUT` | `180` s for each `fm-inbox.sh` call |
| `SLACK_BRIDGE_LOAD_ALARM` | `1` (`0` turns off the load alarm and the guard) |
| `SLACK_BRIDGE_GUARD` | `1` (`0` keeps the alarm but never changes priorities) |
| `SLACK_BRIDGE_LOAD_THRESHOLD` | unset: `SLACK_BRIDGE_LOAD_FACTOR` × cores |
| `SLACK_BRIDGE_LOAD_FACTOR` | `2.0` |
| `SLACK_BRIDGE_LOAD_SUSTAIN` | `300` s above the threshold before alerting |
| `SLACK_BRIDGE_LOAD_POLL` | `30` s between samples |
| `SLACK_BRIDGE_LOAD_REALERT` | `1800` s between alerts |
| `SLACK_BRIDGE_LAUNCHD_LABEL` | `com.firstmate.slack-bridge` (what `restart bridge` restarts) |
| `SLACK_BRIDGE_SUGGEST` | `1` (`0`: suggestions from the rules only, no model call) |
| `SLACK_BRIDGE_SUGGEST_CLI` / `SLACK_BRIDGE_SUGGEST_CODEX` | `claude` / `codex` (looked up on PATH) |
| `SLACK_BRIDGE_SUGGEST_TIMEOUT` | `25` s for the suggestion call |

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

The suite is offline and standard-library only: a fake `fm-inbox.sh` and a
fake `herdr` (plus an in-process fake) stand in for Firstmate and Herdr, a
fake Slack client records the upload calls, a fake browser stands in for
Playwright, and every keeper test pins a fake pane, so nothing can reach
Slack or the live Firstmate pane. Fake `ps`, `sysctl`, `taskpolicy` and
`launchctl` scripts and an in-process fake `kill` cover `load`, `shed`,
`resume`, `restart bridge`, the guard and the LaunchAgent reload in
`scripts/install-service.sh`, so no test signals or
reprioritizes a real process or restarts a LaunchAgent. `scripts/setup.sh`
runs end to end against a fake `slack` CLI, a fake `security`, the fake
`launchctl`, fake `install-service.sh` and `bin/slack-bridge` and a fake app-level
token check, in a temp HOME. Fake `claude` and
`codex` scripts stand in for the suggestion CLI, so no test calls a model. The live Slack check is step 7 of `scripts/setup.sh`.
