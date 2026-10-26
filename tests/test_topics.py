import io
import json
import logging
import os
from contextlib import redirect_stdout
from unittest import mock

from slack_bridge import __main__ as cli
from slack_bridge.outbound import OutboundError, link, say, topics

from .helpers import DM, TempCase

KEY = "example-build"


class TopicTest(TempCase):
    def say(self, body, **kw):
        return say(self.slack, self.state, lambda: DM, body, **kw)

    def test_first_topic_post_opens_a_thread_then_reuses_it(self):
        n, where = self.say("build started", topic=KEY)
        self.assertEqual((n, where), (1, f"new topic {KEY} thread"))
        root = self.slack.posts[0]
        self.assertIsNone(root["thread_ts"])  # top-level DM message
        self.say("build done", topic=KEY)
        self.say("shipped", topic=KEY)
        root_ts = "91.000"  # FakeSlack's ts for the first post
        self.assertEqual([p["thread_ts"] for p in self.slack.posts[1:]], [root_ts, root_ts])
        self.assertEqual(topics(self.state), [(KEY, DM, root_ts)])

    def test_long_first_post_keeps_the_rest_in_its_thread(self):
        body = "\n\n".join("p%d " % i + "x" * 900 for i in range(8))
        n, _ = self.say(body, topic=KEY)
        self.assertGreater(n, 1)
        self.assertIsNone(self.slack.posts[0]["thread_ts"])
        self.assertTrue(all(p["thread_ts"] == "91.000" for p in self.slack.posts[1:]))

    def test_link_makes_a_notes_thread_the_topic(self):
        self.state.remember_thread("n0007", DM, "1700.000100")
        self.assertIn("linked", link(self.state, KEY, "n0007"))
        self.assertIn("already linked", link(self.state, KEY, "n0007"))  # idempotent
        self.say("update", topic=KEY)
        self.assertEqual(self.slack.posts[-1]["thread_ts"], "1700.000100")
        self.assertEqual(topics(self.state), [(KEY, DM, "1700.000100")])

    def test_relink_needs_force(self):
        self.state.remember_thread("n0001", DM, "1.0")
        self.state.remember_thread("n0002", DM, "2.0")
        link(self.state, KEY, "n0001")
        with self.assertRaises(OutboundError):
            link(self.state, KEY, "n0002")
        self.assertIn("relinked", link(self.state, KEY, "n0002", force=True))
        self.assertEqual(topics(self.state)[0][2], "2.0")

    def test_link_unknown_note_and_bad_key_are_refused(self):
        with self.assertRaises(OutboundError):
            link(self.state, KEY, "n9999")
        self.state.remember_thread("n0001", DM, "1.0")
        for bad in ("", "has space", "-leading", "x" * 101, "a/b"):
            with self.assertRaises(OutboundError):
                link(self.state, bad, "n0001")
        self.assertEqual(self.state.read()["topics"], {})

    def test_thread_and_topic_together_are_refused(self):
        with self.assertRaises(OutboundError):
            self.say("x", thread="n0001", topic=KEY)
        self.assertEqual(self.slack.posts, [])

    def test_unknown_note_thread_still_falls_back_to_dm(self):
        n, where = self.say("hi", thread="n0404")
        self.assertEqual(n, 1)
        self.assertIn("DM", where)
        self.assertIsNone(self.slack.posts[0]["thread_ts"])

    def test_failed_first_post_links_nothing(self):
        def boom(*a, **k):
            raise RuntimeError("slack down")

        self.slack.post = boom
        with self.assertRaises(RuntimeError):
            self.say("x", topic=KEY)
        self.assertEqual(self.state.read()["topics"], {})


class TopicCliTest(TempCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        root = logging.getLogger()
        before = list(root.handlers)
        # Keep the CLI's log file inside the temp dir, never ~/Library/Logs.
        with mock.patch.dict(os.environ, {"SLACK_BRIDGE_LOG_FILE": str(self.tmp / "cli.log")}), \
                redirect_stdout(out):
            rc = cli.main([*argv, "--state-dir", str(self.state.dir)])
        for h in root.handlers[len(before):]:
            root.removeHandler(h)
            h.close()
        return rc, out.getvalue()

    def test_link_and_topics_commands(self):
        self.state.remember_thread("n0001", DM, "1.0")
        self.assertEqual(self.run_cli("link", KEY, "n0001")[0], 0)
        rc, out = self.run_cli("topics")
        self.assertEqual((rc, out), (0, f"{KEY}\t{DM}\t1.0\n"))
        self.assertEqual(self.run_cli("link", KEY, "n0404")[0], 1)
        stored = json.loads(self.state.path.read_text())["topics"][KEY]
        self.assertEqual(stored["note"], "n0001")
