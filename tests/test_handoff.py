import json

from slack_bridge import handoff as H
from slack_bridge.handoff import Handoffs
from slack_bridge.inbox import FmInbox

from .helpers import DM, FAKES, FakeClock, TempCase

TS = "1700000000.000100"
RID = f"slack-{DM}-{TS}"


class HandoffTest(TempCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.handoffs = self.make_handoffs(clock=self.clock)

    def calls(self):
        log = self.fm_home / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def texts(self):
        return [p["text"] for p in self.slack.posts]

    def test_request_id_round_trip(self):
        self.assertEqual(H.parse_request_id(H.request_id(DM, TS)), (DM, TS))
        self.assertIsNone(H.parse_request_id("cli-123"))
        self.assertIsNone(H.parse_request_id("slack-"))
        self.assertEqual([H.backoff(n) for n in (1, 2, 3, 6, 20)], [15, 30, 60, 300, 300])

    def test_submit_never_waits_for_fm_inbox(self):
        # The listener thread only records, reacts and wakes the hand-off loop.
        (self.fm_home / "hang-after-save").touch()
        self.assertEqual(self.handoffs.submit(DM, TS, TS, "hello"), "accepted")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.slack.reactions, [(DM, TS, "eyes")])
        self.assertTrue(self.handoffs.wake.is_set())
        self.assertEqual(self.state.read()["handoffs"][RID]["text"], "hello")

    def test_clean_hand_off_leaves_nothing_pending(self):
        self.handoffs.submit(DM, TS, TS, "hello")
        self.assertEqual(self.handoffs.tick(), 1)
        self.assertEqual(self.state.read()["handoffs"], {})
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.slack.posts, [])

    def test_timeout_after_save_is_retried_and_announced(self):
        # The 2026-10-24 incident: the note is saved, then the hand-off times
        # out before Firstmate is woken.
        self.handoffs.inbox = FmInbox(str(FAKES / "fm-inbox.sh"), str(self.fm_home),
                                      timeout=1.0)
        (self.fm_home / "hang-after-save").touch()
        self.handoffs.submit(DM, TS, TS, "hello")
        with self.assertLogs("slack_bridge.handoff", level="ERROR"):
            self.assertEqual(self.handoffs.tick(), 0)
        self.assertEqual(len(self.notes()), 1)
        self.assertFalse(self.notes()[0]["announced"])
        rec = self.state.read()["handoffs"][RID]
        self.assertEqual(rec["text"], "hello")
        self.assertEqual(rec["attempts"], 1)
        self.assertIn("TimeoutExpired", self.texts()[0])
        self.assertIn("no need to resend", self.texts()[0])

        (self.fm_home / "hang-after-save").unlink()
        self.assertEqual(self.handoffs.tick(), 0)  # not due yet
        self.clock.now += 15
        self.assertEqual(self.handoffs.tick(), 1)
        self.assertEqual(len(self.notes()), 1)  # replayed, not duplicated
        self.assertTrue(self.notes()[0]["announced"])
        self.assertEqual(self.state.read()["handoffs"], {})
        self.assertEqual(self.state.thread_for("n0001"), {"channel": DM, "ts": TS,
                                                          "at": self.state.thread_for("n0001")["at"]})
        self.assertIn("now has this message (15s late)", self.texts()[-1])
        notes_calls = [c for c in self.calls() if c[0] == "note"]
        self.assertEqual({c[2] for c in notes_calls}, {RID})

    def test_failure_backs_off_and_warns_once(self):
        (self.fm_home / "fail-note").touch()
        self.handoffs.submit(DM, TS, TS, "hello")
        with self.assertLogs("slack_bridge.handoff", level="ERROR"):
            self.handoffs.tick()
            for step in (15, 30):
                self.clock.now += step - 1
                self.handoffs.tick()
                attempts = self.state.read()["handoffs"][RID]["attempts"]
                self.clock.now += 1
                self.handoffs.tick()
                self.assertEqual(self.state.read()["handoffs"][RID]["attempts"], attempts + 1)
        self.assertEqual(len(self.slack.posts), 1)  # one delay notice, not one per retry
        (self.fm_home / "fail-note").unlink()
        self.clock.now += 60
        self.handoffs.tick()
        self.assertEqual(self.state.read()["handoffs"], {})
        self.assertIn("late", self.texts()[-1])

    def test_unannounced_note_keeps_only_its_id_and_is_woken_later(self):
        (self.fm_home / "unannounced").touch()
        self.handoffs.submit(DM, TS, TS, "hello")
        self.assertEqual(self.handoffs.tick(), 0)
        rec = self.state.read()["handoffs"][RID]
        self.assertEqual(rec["note_id"], "n0001")
        self.assertNotIn("text", rec)  # saved: the body is no longer kept here
        self.assertIn("n0001", self.state.read()["threads"])
        (self.fm_home / "unannounced").unlink()
        self.clock.now += 15
        self.assertEqual(self.handoffs.tick(), 1)
        self.assertIn(["announce", "n0001"], self.calls())
        self.assertEqual(self.state.read()["handoffs"], {})
        self.assertIn("now has this message", self.texts()[-1])

    def test_sweep_wakes_untracked_slack_notes_only(self):
        notes = self.fm_home / "notes"
        notes.mkdir()
        rows = {
            "n0001": {"request_id": RID, "announced": False},           # lost by an old bridge
            "n0002": {"request_id": "cli-9", "announced": False},       # not from Slack
            "n0003": {"request_id": f"slack-{DM}-2.0", "announced": True},
        }
        for nid, rec in rows.items():
            (notes / f"{nid}.json").write_text(json.dumps(dict(rec, body="x")))
        self.assertEqual(self.handoffs.sweep(), 1)
        self.assertEqual([c for c in self.calls() if c[0] == "announce"], [["announce", "n0001"]])
        self.assertEqual(self.state.thread_for("n0001")["ts"], TS)
        self.assertEqual(self.slack.posts[0]["thread_ts"], TS)
        self.assertIn("never woken", self.texts()[0])
        self.assertEqual(self.handoffs.sweep(), 0)  # now announced

    def test_sweep_runs_once_on_the_first_tick(self):
        notes = self.fm_home / "notes"
        notes.mkdir()
        (notes / "n0001.json").write_text(json.dumps(
            {"request_id": RID, "announced": False, "body": "x"}))
        self.assertEqual(self.handoffs.tick(), 1)
        (notes / "n0002.json").write_text(json.dumps(
            {"request_id": f"slack-{DM}-2.0", "announced": False, "body": "x"}))
        self.assertEqual(self.handoffs.tick(), 0)
        self.assertEqual([c for c in self.calls() if c[0] in ("receipts", "announce")],
                         [["receipts", "--all-replies", "--all-pending"], ["announce", "n0001"]])

    def test_sweep_leaves_tracked_hand_offs_to_the_retry(self):
        (self.fm_home / "unannounced").touch()
        self.handoffs.submit(DM, TS, TS, "hello")
        self.handoffs.tick()
        self.assertEqual(self.handoffs.sweep(), 0)
        # Only the hand-off's own immediate repair, never a second one from the sweep.
        self.assertEqual(self.calls().count(["announce", "n0001"]), 1)
