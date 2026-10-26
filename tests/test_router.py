import json
import logging

from slack_bridge.router import Router
from slack_bridge.suggest import Advice

from .helpers import CAPTAIN, DM, FakeClock, FakeHerdr, TempCase, pane

SECRET_TEXT = "launch codes 1234"


def dm(text=SECRET_TEXT, user=CAPTAIN, ts="1700000000.000100", **extra):
    ev = {"type": "message", "channel_type": "im", "channel": DM, "user": user,
          "ts": ts, "text": text}
    ev.update(extra)
    return ev


class RouterTest(TempCase):
    def setUp(self):
        super().setUp()
        self.herdr = FakeHerdr([pane(cwd=str(self.fm_home))])
        self.router = Router(CAPTAIN, self.slack, self.inbox, self.state,
                             self.keeper(self.herdr), self.make_handoffs())

    def send(self, ev):
        """Route one event, then run the hand-off loop once."""
        outcome = self.router.handle_message(ev)
        self.router.handoffs.tick()
        return outcome

    # Sender allowlist -----------------------------------------------------

    def test_drops_everything_but_the_captains_dm(self):
        cases = {
            "sender-not-allowed": dm(user="USOMEONE"),
            "channel-type-channel": dm(channel_type="channel"),
            "channel-type-mpim": dm(channel_type="mpim"),
            "from-bot": dm(bot_id="B1"),
            "subtype-message_changed": dm(subtype="message_changed"),
            "not-a-message": dict(dm(), type="app_mention"),
        }
        with self.assertLogs("slack_bridge.router", level="INFO") as logs:
            for reason, ev in cases.items():
                self.assertEqual(self.router.handle_message(ev), f"dropped:{reason}")
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.slack.posts, [])  # never answers a stranger
        self.assertEqual(self.slack.reactions, [])
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    def test_missing_user_is_dropped(self):
        ev = dm()
        del ev["user"]
        self.assertEqual(self.router.handle_message(ev), "dropped:sender-not-allowed")

    def test_queued_note_logs_no_body(self):
        with self.assertLogs("slack_bridge", level="INFO") as logs:
            self.assertEqual(self.send(dm()), "accepted")
        self.assertEqual(len(self.notes()), 1)
        self.assertNotIn(SECRET_TEXT, "\n".join(logs.output))

    # Idempotent note --------------------------------------------------------

    def test_note_request_id_and_retry_are_idempotent(self):
        self.assertEqual(self.send(dm()), "accepted")
        self.assertEqual(self.send(dm()), "accepted")  # Slack retry: replayed
        notes = self.notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["request_id"], f"slack-{DM}-1700000000.000100")
        self.assertEqual(notes[0]["body"], SECRET_TEXT)
        self.assertIn((DM, "1700000000.000100", "eyes"), self.slack.reactions)
        calls = [json.loads(line) for line in (self.fm_home / "calls.log").read_text().splitlines()]
        self.assertEqual([c for c in calls if c[0] == "note"][0],
                         ["note", "--request-id", f"slack-{DM}-1700000000.000100", "--json", "-"])

    def test_inbound_text_is_unescaped(self):
        self.send(dm(text="a &lt;b&gt; &amp; c"))
        self.assertEqual(self.notes()[0]["body"], "a <b> & c")

    def test_thread_mapping_uses_thread_root(self):
        self.send(dm())
        self.send(dm(ts="1700000001.000200", thread_ts="1700000000.000100"))
        threads = self.state.read()["threads"]
        self.assertEqual(threads["n0001"]["ts"], "1700000000.000100")
        self.assertEqual(threads["n0002"]["ts"], "1700000000.000100")
        self.assertEqual(threads["n0002"]["channel"], DM)

    def test_note_failure_is_reported_and_kept_for_retry(self):
        (self.fm_home / "fail-note").touch()
        with self.assertLogs("slack_bridge.handoff", level="ERROR"):
            self.assertEqual(self.send(dm()), "accepted")
        self.assertEqual(self.slack.reactions, [(DM, "1700000000.000100", "eyes"),
                                                (DM, "1700000000.000100", "hourglass_flowing_sand")])
        self.assertIn("keeps retrying", self.slack.posts[0]["text"])
        self.assertNotIn("resend", self.slack.posts[0]["text"].replace("no need to resend", ""))
        self.assertEqual(self.state.read()["threads"], {})
        self.assertIn(f"slack-{DM}-1700000000.000100", self.state.read()["handoffs"])

    def test_saved_but_unannounced_warns(self):
        (self.fm_home / "unannounced").touch()
        self.send(dm())
        self.assertIn("could not be woken", self.slack.posts[0]["text"])
        self.assertIn("n0001", self.state.read()["threads"])

    # Commands ----------------------------------------------------------------

    def test_new_session_is_not_sent_to_the_inbox(self):
        for text in ("new session", "  New   Session! ", "/new-session"):
            ts = f"17000000{len(self.slack.posts):02d}.000"
            self.assertEqual(self.router.handle_message(dm(text=text, ts=ts)), "new-session")
        self.assertEqual(self.notes(), [])
        queue = self.state.read()["fresh_requests"]
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["channel"], DM)
        self.assertEqual(queue[0]["source"], "slack")
        self.assertEqual(self.herdr.runs, [])  # nothing typed from the Slack thread

    def test_new_session_retry_is_deduplicated(self):
        self.router.handle_message(dm(text="new session"))
        self.assertEqual(self.router.handle_message(dm(text="new session")), "duplicate")
        self.assertEqual(len(self.state.read()["fresh_requests"]), 1)

    def test_phrase_inside_a_sentence_is_a_normal_note(self):
        self.send(dm(text="should we start a new session tomorrow?"))
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.state.read()["fresh_requests"], [])

    def test_status_answers_without_waking_firstmate(self):
        (self.fm_home / "status.txt").write_text("primary: idle\n<2 notes>")
        self.assertEqual(self.router.handle_message(dm(text="Status")), "status")
        self.assertEqual(self.notes(), [])
        text = self.slack.posts[0]["text"]
        self.assertTrue(text.startswith("```\n"))
        self.assertIn("&lt;2 notes&gt;", text)
        self.assertEqual(self.slack.posts[0]["thread_ts"], "1700000000.000100")

    def test_disabled_keeper_answers_new_session(self):
        router = Router(CAPTAIN, self.slack, self.inbox, self.state, None,
                        self.make_handoffs())
        self.assertEqual(router.handle_message(dm(text="new session")), "new-session-disabled")


class FakeRescue:
    def __init__(self):
        self.calls = []
        self.fail_restart = False
        self.offer_for = None  # (channel, thread_ts) of a pending `shed` list

    def load_report(self):
        self.calls.append("load")
        return "*Mac load:* load 50.0 / 45.0 / 30.0"

    def offer(self, channel, thread_ts):
        self.calls.append(("offer", channel, thread_ts))
        return "*Heaviest processes `shed` can act on:*\n• qemu-system-aarch64 Pixel_8 (pid 801)"

    def current_offer(self, channel, thread_ts):
        return {"action": "quit"} if (channel, thread_ts) == self.offer_for else None

    def shed(self, action, channel, thread_ts, top_level=False):
        self.calls.append(("shed", action, channel, thread_ts, top_level))
        return "*Shed (quit):*\n• quit qemu-system-aarch64 Pixel_8 (pid 801)"

    def resume(self):
        self.calls.append("resume")
        return "*Resume:*\n• resumed qemu-system-aarch64 Pixel_8 (pid 801)"

    def restart_bridge(self):
        self.calls.append("restart-bridge")
        if self.fail_restart:
            raise OSError("this bridge is not running as the LaunchAgent")


class EmergencyCommandTest(TempCase):
    """load, shed and restart are answered by the bridge, never by Firstmate."""

    def setUp(self):
        super().setUp()
        self.herdr = FakeHerdr([pane(cwd=str(self.fm_home))])
        self.rescue = FakeRescue()
        self.router = Router(CAPTAIN, self.slack, self.inbox, self.state,
                             self.keeper(self.herdr), self.make_handoffs(), rescue=self.rescue)
        # Firstmate is stuck: any hand-off would fail.
        (self.fm_home / "fail-note").touch()

    def test_load_and_shed_answer_in_thread_without_firstmate(self):
        self.assertEqual(self.router.handle_message(dm(text="Load")), "load")
        self.assertEqual(self.router.handle_message(dm(text="shed", ts="2.0")), "shed")
        self.assertEqual(self.rescue.calls, ["load", ("offer", DM, "2.0")])
        self.assertIn("*Mac load:*", self.slack.posts[0]["text"])  # mrkdwn kept
        self.assertIn("Pixel_8", self.slack.posts[1]["text"])
        self.assertEqual(self.slack.posts[1]["thread_ts"], "2.0")
        self.assertFalse((self.fm_home / "calls.log").exists())
        self.assertEqual(self.state.read()["handoffs"], {})

    def test_strangers_cannot_shed(self):
        self.assertEqual(self.router.handle_message(dm(text="shed", user="USOMEONE")),
                         "dropped:sender-not-allowed")
        self.assertEqual(self.rescue.calls, [])

    def test_shed_retry_is_deduplicated(self):
        self.router.handle_message(dm(text="shed quit"))
        self.assertEqual(self.router.handle_message(dm(text="shed quit")), "duplicate")
        self.assertEqual(self.rescue.calls, [("shed", "quit", DM, "1700000000.000100", True)])

    def test_shed_pause_and_quit_act_on_the_list(self):
        self.assertEqual(self.router.handle_message(dm(text="shed pause", ts="2.0")),
                         "shed-pause")
        self.assertEqual(self.router.handle_message(dm(text="Shed quit!", ts="3.0")),
                         "shed-quit")
        self.assertEqual(self.router.handle_message(dm(text="shed quit", ts="4.0",
                                                       thread_ts="2.0")), "shed-quit")
        self.assertEqual(self.rescue.calls, [("shed", "pause", DM, "2.0", True),
                                             ("shed", "quit", DM, "3.0", True),
                                             ("shed", "quit", DM, "2.0", False)])
        self.assertIn("quit qemu", self.slack.posts[-1]["text"])

    def test_yes_accepts_only_as_a_reply_in_the_shed_list_thread(self):
        self.assertEqual(self.router.handle_message(dm(text="yes")), "accepted")  # Firstmate's
        self.rescue.offer_for = (DM, "5.0")
        # Top level, or an answer to Firstmate in another thread: ordinary text.
        self.assertEqual(self.router.handle_message(dm(text="Ok.", ts="6.0")), "accepted")
        self.assertEqual(self.router.handle_message(dm(text="ok", ts="7.0", thread_ts="4.0")),
                         "accepted")
        self.assertEqual(self.rescue.calls, [])
        self.assertEqual(len(self.state.read()["handoffs"]), 3)
        self.assertEqual(self.router.handle_message(dm(text="Yes", ts="8.0", thread_ts="5.0")),
                         "shed-accept")
        self.assertEqual(self.rescue.calls, [("shed", None, DM, "5.0", False)])

    def test_resume_only_while_something_is_paused(self):
        self.assertEqual(self.router.handle_message(dm(text="resume")), "accepted")
        with self.state.update() as st:
            st["paused"] = [{"pid": 801, "command": "qemu", "label": "qemu"}]
        self.assertEqual(self.router.handle_message(dm(text="resume", ts="2.0")), "resume")
        self.assertEqual(self.rescue.calls, ["resume"])

    def test_restart_uses_the_fresh_session_path(self):
        self.assertEqual(self.router.handle_message(dm(text="restart")), "new-session")
        self.assertEqual(len(self.state.read()["fresh_requests"]), 1)
        self.assertFalse(self.state.read()["fresh_requests"][0]["force"])
        self.assertEqual(self.herdr.runs, [])
        self.assertNotIn("restart force", self.slack.posts[-1]["text"])

    def test_restart_force_queues_an_interrupting_fresh_session(self):
        self.assertEqual(self.router.handle_message(dm(text="restart force")),
                         "new-session-force")
        self.assertTrue(self.state.read()["fresh_requests"][0]["force"])
        self.assertEqual(self.herdr.keys, [])  # nothing typed from the Slack thread

    def test_restart_while_busy_carries_the_model_suggestion(self):
        class FakeAdvisor:
            def restart(self, status, busy_seconds, rule):
                self.asked = (status, busy_seconds, rule.choice)
                return Advice("force", "No output for a while", by="claude")

        clock = FakeClock()
        self.herdr.panes[0]["agent_status"] = "working"
        keeper = self.keeper(self.herdr, clock)
        advisor = FakeAdvisor()
        router = Router(CAPTAIN, self.slack, self.inbox, self.state, keeper,
                        self.make_handoffs(), rescue=self.rescue, advisor=advisor)
        keeper.check()
        clock.now += 120
        self.assertEqual(router.handle_message(dm(text="restart")), "new-session")
        self.assertEqual(advisor.asked, ("working", 120, "wait"))
        self.assertIn("Suggested by claude: `restart force` (No output for a while)",
                      self.slack.posts[-1]["text"])
        self.assertFalse(self.state.read()["fresh_requests"][0]["force"])  # only a suggestion

    def test_restart_suggests_force_when_firstmate_is_stuck(self):
        clock = FakeClock()
        self.herdr.panes[0]["agent_status"] = "working"
        keeper = self.keeper(self.herdr, clock)
        router = Router(CAPTAIN, self.slack, self.inbox, self.state, keeper,
                        self.make_handoffs(), rescue=self.rescue)
        keeper.check()
        clock.now += 600
        self.assertEqual(router.handle_message(dm(text="restart")), "new-session")
        self.assertIn("working for ~10 min", self.slack.posts[-1]["text"])
        self.assertIn("`restart force`", self.slack.posts[-1]["text"])

    def test_restart_bridge_records_where_to_answer(self):
        self.assertEqual(self.router.handle_message(dm(text="restart bridge")),
                         "restart-bridge")
        self.assertEqual(self.rescue.calls, ["restart-bridge"])
        self.assertEqual(self.state.read()["bridge_restart"],
                         {"channel": DM, "thread_ts": "1700000000.000100"})
        from slack_bridge.daemon import announce_restart

        sent = []
        announce_restart(self.state, lambda text, channel=None, thread_ts=None:
                         sent.append((text, channel, thread_ts)))
        self.assertEqual(sent, [(":white_check_mark: The bridge is back up.", DM,
                                 "1700000000.000100")])
        self.assertIsNone(self.state.read()["bridge_restart"])

    def test_restart_bridge_failure_is_reported(self):
        self.rescue.fail_restart = True
        self.assertEqual(self.router.handle_message(dm(text="restart bridge")),
                         "restart-bridge-failed")
        self.assertIn("not running as the LaunchAgent", self.slack.posts[-1]["text"])
        self.assertIsNone(self.state.read()["bridge_restart"])

    def test_without_rescue_emergency_commands_say_so(self):
        router = Router(CAPTAIN, self.slack, self.inbox, self.state, None,
                        self.make_handoffs())
        self.assertEqual(router.handle_message(dm(text="load")), "rescue-disabled")


class ReplyRelayTest(TempCase):
    def setUp(self):
        super().setUp()
        from slack_bridge.router import ReplyRelay

        self.relay = ReplyRelay(self.slack, self.inbox, self.state)

    def replies(self, rows):
        (self.fm_home / "replies.json").write_text(json.dumps(rows))

    def test_first_poll_starts_at_present(self):
        self.state.remember_thread("n0001", DM, "1.0")
        self.replies([{"id": "n0001", "body": "old", "cursor": "000000000001"}])
        self.assertEqual(self.relay.poll_once(), 0)
        self.assertEqual(self.state.read()["receipts_cursor"], "000000000001")
        self.assertEqual(self.slack.posts, [])

    def test_replies_go_to_their_threads_once(self):
        self.replies([])
        self.relay.poll_once()  # initialize at empty history
        self.state.remember_thread("n0001", DM, "1.0")
        self.state.remember_thread("n0002", DM, "2.0")
        self.replies([
            {"id": "n0002", "body": "second <ok>", "cursor": "000000000002"},
            {"id": "n0001", "body": "first", "cursor": "000000000001"},
            {"id": "other", "body": "not from slack", "cursor": "000000000003"},
        ])
        self.assertEqual(self.relay.poll_once(), 2)
        self.assertEqual([(p["thread_ts"], p["text"]) for p in self.slack.posts],
                         [("1.0", "first"), ("2.0", "second &lt;ok&gt;")])
        self.assertEqual(self.state.read()["receipts_cursor"], "000000000003")
        self.assertEqual(self.relay.poll_once(), 0)
        self.assertEqual(len(self.slack.posts), 2)

    def test_long_reply_is_split(self):
        self.replies([])
        self.relay.poll_once()
        self.state.remember_thread("n0001", DM, "1.0")
        body = "\n\n".join(f"paragraph {i} " + "x" * 900 for i in range(10))
        self.replies([{"id": "n0001", "body": body, "cursor": "000000000001"}])
        self.relay.poll_once()
        self.assertGreater(len(self.slack.posts), 1)
        self.assertTrue(all(len(p["text"]) <= 3500 for p in self.slack.posts))
        self.assertTrue(all(p["thread_ts"] == "1.0" for p in self.slack.posts))


logging.getLogger("slack_bridge").setLevel(logging.INFO)


class LateMappingTest(TempCase):
    def test_reply_to_a_note_whose_hand_off_timed_out_still_finds_its_thread(self):
        from slack_bridge.router import ReplyRelay

        relay = ReplyRelay(self.slack, self.inbox, self.state)
        relay.poll_once()  # initialize at empty history
        # Saved by fm-inbox, but the bridge timed out before learning the id.
        (self.fm_home / "notes").mkdir(exist_ok=True)
        (self.fm_home / "notes" / "n0001.json").write_text(json.dumps(
            {"request_id": f"slack-{DM}-5.0", "body": "x", "announced": True}))
        with self.state.update() as st:
            st["handoffs"][f"slack-{DM}-5.0"] = {"channel": DM, "ts": "5.0",
                                                 "thread_ts": "4.0", "at": 0}
        (self.fm_home / "replies.json").write_text(json.dumps(
            [{"id": "n0001", "body": "answer", "cursor": "000000000001"}]))
        self.assertEqual(relay.poll_once(), 1)
        self.assertEqual(self.slack.posts[-1]["thread_ts"], "4.0")
        self.assertEqual(self.state.thread_for("n0001")["ts"], "4.0")
