import io
import json
import logging
import os
from contextlib import redirect_stdout
from unittest import mock

from slack_bridge import __main__ as cli
from slack_bridge import projects
from slack_bridge.config import Secrets
from slack_bridge.outbound import OutboundError, link, say, topics
from slack_bridge.router import ReplyRelay, Router
from slack_bridge.slack_api import SlackCallError, WebSlack

from .helpers import CAPTAIN, DM, FakeHerdr, TempCase, pane

KEY = "example-project"
NAME = "example-project"
TASK = "example-build"


def group(text="ping", user=CAPTAIN, channel="C0001", ts="1700000000.000100", **extra):
    ev = {"type": "message", "channel_type": "group", "channel": channel, "user": user,
          "ts": ts, "text": text}
    ev.update(extra)
    return ev


class ChannelCreateTest(TempCase):
    def create(self, key=KEY, name=NAME):
        return projects.create(self.slack, self.state, CAPTAIN, key, name)

    def test_creates_private_channel_invites_captain_and_records_it(self):
        channel, msg = self.create()
        self.assertEqual(channel, "C0001")
        self.assertIn("created", msg)
        self.assertEqual(self.slack.invites, [("C0001", CAPTAIN)])
        stored = self.state.read()["projects"][KEY]
        self.assertEqual((stored["channel"], stored["name"]), ("C0001", NAME))

    def test_rerun_reuses_the_mapped_channel(self):
        self.create()
        channel, msg = self.create()
        self.assertEqual(channel, "C0001")
        self.assertIn("reused", msg)
        self.assertEqual(self.slack.channels, {NAME: "C0001"})  # not duplicated

    def test_existing_name_is_reported_not_duplicated(self):
        self.slack.channels[NAME] = "C0777"
        self.slack.member.add("C0777")
        with self.assertRaises(OutboundError) as err:
            self.create()
        self.assertIn("C0777", str(err.exception))
        self.assertIn(f"channel link {KEY} C0777", str(err.exception))
        self.assertEqual(self.state.read()["projects"], {})
        self.assertEqual(self.slack.invites, [])

    def test_existing_name_the_bot_cannot_see(self):
        self.slack.channels[NAME] = "C0777"  # someone else's private channel
        with self.assertRaises(OutboundError) as err:
            self.create()
        self.assertIn("not in it", str(err.exception))
        self.assertNotIn("C0777", str(err.exception))

    def test_name_of_another_project_is_refused(self):
        self.create()
        with self.assertRaises(OutboundError):
            self.create(key="other-project")
        self.assertEqual(list(self.state.read()["projects"]), [KEY])

    def test_failed_invite_keeps_the_channel_and_rerun_retries(self):
        self.slack.fail_invite = "ratelimited"
        with self.assertRaises(OutboundError) as err:
            self.create()
        self.assertIn("rerun", str(err.exception))
        self.assertEqual(self.state.read()["projects"][KEY]["channel"], "C0001")
        self.slack.fail_invite = None
        self.assertEqual(self.create()[0], "C0001")
        self.assertEqual(self.slack.invites, [("C0001", CAPTAIN)])
        self.assertEqual(len(self.slack.channels), 1)

    def test_invalid_key_or_name_is_refused(self):
        for key, name in ((KEY, "Example Project"), (KEY, "x" * 81), (KEY, ""), ("a/b", NAME)):
            with self.assertRaises(OutboundError):
                self.create(key, name)
        self.assertEqual(self.slack.channels, {})


class ChannelLinkTest(TempCase):
    def test_link_list_and_relink(self):
        self.assertIn("linked", projects.link(self.state, KEY, "C0042"))
        self.assertIn("already", projects.link(self.state, KEY, "C0042"))
        with self.assertRaises(OutboundError):
            projects.link(self.state, KEY, "C0043")
        self.assertIn("relinked", projects.link(self.state, KEY, "C0043", force=True))
        self.assertEqual(projects.channels(self.state), [(KEY, "C0043", "-")])

    def test_channel_of_another_project_and_bad_id_are_refused(self):
        projects.link(self.state, KEY, "C0042")
        for key, cid in (("other", "C0042"), ("other", "c0042"), ("other", "D0042"),
                         ("other", "")):
            with self.assertRaises(OutboundError):
                projects.link(self.state, key, cid, force=True)
        self.assertEqual(list(self.state.read()["projects"]), [KEY])


class ChannelRouterTest(TempCase):
    def setUp(self):
        super().setUp()
        self.herdr = FakeHerdr([pane(cwd=str(self.fm_home))])
        self.router = Router(CAPTAIN, self.slack, self.inbox, self.state,
                             self.keeper(self.herdr), self.make_handoffs())
        projects.create(self.slack, self.state, CAPTAIN, KEY, NAME)

    def send(self, ev):
        """Route one event, then run the hand-off loop once."""
        outcome = self.router.handle_message(ev)
        self.router.handoffs.tick()
        return outcome

    def test_captain_in_project_channel_is_queued_with_the_project_tag(self):
        self.assertEqual(self.send(group(text="add a hero row")), "accepted")
        note = self.notes()[0]
        self.assertEqual(note["body"], f"[project:{KEY}] add a hero row")
        self.assertEqual(note["request_id"], "slack-C0001-1700000000.000100")
        thread = self.state.thread_for("n0001")
        self.assertEqual((thread["channel"], thread["ts"]), ("C0001", "1700000000.000100"))
        self.assertIn(("C0001", "1700000000.000100", "eyes"), self.slack.reactions)

    def test_unmapped_private_channel_carries_its_id(self):
        self.send(group(channel="G0099"))
        self.assertEqual(self.notes()[0]["body"], "[channel:G0099] ping")

    def test_dm_notes_stay_untagged(self):
        self.send({"type": "message", "channel_type": "im", "channel": DM,
                   "user": CAPTAIN, "ts": "1.0", "text": "hello"})
        self.assertEqual(self.notes()[0]["body"], "hello")

    def test_others_in_a_project_channel_are_dropped(self):
        self.assertEqual(self.router.handle_message(group(user="USOMEONE")),
                         "dropped:sender-not-allowed")
        self.assertEqual(self.router.handle_message(group(bot_id="B1")), "dropped:from-bot")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.slack.posts, [])

    def test_status_answers_in_the_channel_thread(self):
        (self.fm_home / "status.txt").write_text("primary: idle")
        self.assertEqual(self.router.handle_message(group(text="status")), "status")
        self.assertEqual(self.notes(), [])
        post = self.slack.posts[0]
        self.assertEqual((post["channel"], post["thread_ts"]), ("C0001", "1700000000.000100"))

    def test_new_session_from_a_channel_reports_back_there(self):
        self.assertEqual(self.router.handle_message(group(text="new session")), "new-session")
        req = self.state.read()["fresh_requests"][0]
        self.assertEqual((req["channel"], req["thread_ts"]), ("C0001", "1700000000.000100"))
        self.assertEqual(self.notes(), [])

    def test_reply_goes_back_into_the_channel_thread(self):
        relay = ReplyRelay(self.slack, self.inbox, self.state)
        (self.fm_home / "replies.json").write_text("[]")
        relay.poll_once()
        self.send(group(ts="1700.5", thread_ts="1700.1"))
        (self.fm_home / "replies.json").write_text(json.dumps(
            [{"id": "n0001", "body": "on it", "cursor": "000000000001"}]))
        self.assertEqual(relay.poll_once(), 1)
        self.assertEqual(self.slack.posts[-1],
                         {"channel": "C0001", "text": "on it", "thread_ts": "1700.1"})


class ProjectTopicTest(TempCase):
    def setUp(self):
        super().setUp()
        projects.create(self.slack, self.state, CAPTAIN, KEY, NAME)

    def say(self, body, **kw):
        return say(self.slack, self.state, lambda: DM, body, **kw)

    def test_project_topic_opens_in_the_project_channel(self):
        self.assertEqual(self.say("build started", topic=TASK, project=KEY),
                         (1, f"new topic {TASK} thread"))
        self.say("build done", topic=TASK)  # later posts need no --project
        self.assertEqual(self.slack.posts[0], {"channel": "C0001", "text": "build started",
                                               "thread_ts": None})
        self.assertEqual(self.slack.posts[1]["channel"], "C0001")
        self.assertEqual(self.slack.posts[1]["thread_ts"], "91.000")
        self.assertEqual(topics(self.state), [(TASK, "C0001", "91.000")])

    def test_existing_dm_topic_keeps_its_thread(self):
        self.say("opened in the DM", topic=TASK)
        self.say("update", topic=TASK, project=KEY)
        self.assertEqual(self.slack.posts[1]["channel"], DM)
        self.assertEqual(self.slack.posts[1]["thread_ts"], "91.000")

    def test_project_without_topic_posts_top_level_in_the_channel(self):
        self.assertEqual(self.say("hello", project=KEY), (1, f"project {KEY} channel"))
        self.assertEqual(self.slack.posts[0]["channel"], "C0001")
        self.assertIsNone(self.slack.posts[0]["thread_ts"])

    def test_link_works_for_a_note_from_a_channel(self):
        self.state.remember_thread("n0003", "C0001", "1700.1")
        link(self.state, TASK, "n0003")
        self.say("update", topic=TASK)
        self.assertEqual(self.slack.posts[0],
                         {"channel": "C0001", "text": "update", "thread_ts": "1700.1"})

    def test_unknown_project_and_thread_with_project_are_refused(self):
        with self.assertRaises(OutboundError):
            self.say("x", topic=TASK, project="nope")
        with self.assertRaises(OutboundError):
            self.say("x", thread="n0001", project=KEY)
        self.assertEqual(self.slack.posts, [])
        self.assertEqual(self.state.read()["topics"], {})


class FakeClient:
    """Stands in for slack_sdk's WebClient; errors mimic SlackApiError."""

    class ApiError(Exception):
        def __init__(self, error):
            super().__init__("The request to the Slack API failed. (payload echo)")
            self.response = {"ok": False, "error": error}

    def __init__(self):
        self.calls = []
        self.errors = {}
        self.pages = [{"channels": [{"id": "C1", "name": "a"}],
                       "response_metadata": {"next_cursor": "p2"}},
                      {"channels": [{"id": "C2", "name": NAME}],
                       "response_metadata": {"next_cursor": ""}}]

    def _call(self, method, **kw):
        self.calls.append((method, kw))
        if method in self.errors:
            raise self.ApiError(self.errors[method])

    def conversations_create(self, **kw):
        self._call("create", **kw)
        return {"channel": {"id": "C9"}}

    def conversations_invite(self, **kw):
        self._call("invite", **kw)
        return {"ok": True}

    def conversations_list(self, **kw):
        self._call("list", **kw)
        return self.pages[1 if kw.get("cursor") else 0]


class WebSlackChannelTest(TempCase):
    def setUp(self):
        super().setUp()
        self.client = FakeClient()
        self.web = WebSlack("unused", client=self.client)

    def test_create_is_private(self):
        self.assertEqual(self.web.create_private_channel(NAME), "C9")
        self.assertEqual(self.client.calls, [("create", {"name": NAME, "is_private": True})])

    def test_api_errors_keep_only_the_code(self):
        self.client.errors["create"] = "name_taken"
        with self.assertRaises(SlackCallError) as err:
            self.web.create_private_channel(NAME)
        self.assertEqual(err.exception.code, "name_taken")
        self.assertNotIn("payload", str(err.exception))

    def test_org_token_scopes_channel_creation_and_every_list_page(self):
        web = WebSlack("unused", client=self.client, team_id="T0000000C")
        self.assertEqual(web.create_private_channel(NAME), "C9")
        self.assertEqual(web.find_private_channel(NAME), "C2")
        for _, kwargs in self.client.calls:
            self.assertEqual(kwargs["team_id"], "T0000000C")

    def test_invite_tolerates_a_member_only(self):
        self.client.errors["invite"] = "already_in_channel"
        self.web.invite("C9", CAPTAIN)
        self.client.errors["invite"] = "cant_invite"
        with self.assertRaises(SlackCallError):
            self.web.invite("C9", CAPTAIN)

    def test_find_pages_through_the_bots_private_channels(self):
        self.assertEqual(self.web.find_private_channel(NAME), "C2")
        self.assertEqual(self.client.calls[0][1]["types"], "private_channel")
        self.assertEqual(self.client.calls[1][1]["cursor"], "p2")
        self.assertIsNone(self.web.find_private_channel("missing"))


class ChannelCliTest(TempCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        root = logging.getLogger()
        before = list(root.handlers)
        # Keep the CLI's log file inside the temp dir; Slack and the Keychain
        # are the fakes, never the live workspace.
        secrets = Secrets(bot_token="xoxb-fake", app_token=None, captain_id=CAPTAIN)
        with mock.patch.dict(os.environ, {"SLACK_BRIDGE_LOG_FILE": str(self.tmp / "cli.log")}), \
                mock.patch("slack_bridge.config.load_secrets", return_value=secrets), \
                mock.patch("slack_bridge.slack_api.WebSlack", return_value=self.slack), \
                redirect_stdout(out):
            rc = cli.main([*argv, "--state-dir", str(self.state.dir)])
        for h in root.handlers[len(before):]:
            root.removeHandler(h)
            h.close()
        return rc, out.getvalue()

    def test_create_prints_the_channel_id_then_list_shows_it(self):
        rc, out = self.run_cli("channel", "create", KEY, NAME)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("C0001\t"))
        self.assertEqual(self.run_cli("channel", "list"), (0, f"{KEY}\tC0001\t{NAME}\n"))
        self.assertEqual(self.run_cli("channel", "create", KEY, NAME)[1].split("\t")[0],
                         "C0001")

    def test_link_and_say_project(self):
        self.assertEqual(self.run_cli("channel", "link", KEY, "C0042")[0], 0)
        self.assertEqual(self.run_cli("channel", "link", "other", "C0042")[0], 1)
        rc, out = self.run_cli("say", "--topic", TASK, "--project", KEY, "started")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.slack.posts[0]["channel"], "C0042")
