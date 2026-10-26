import json
import os

from slack_bridge.keeper import Herdr

from .helpers import FAKES, FakeClock, FakeHerdr, TempCase, pane

HOUR = 3600


class DiscoveryTest(TempCase):
    def test_picks_the_firstmate_pane_by_cwd_and_label(self):
        fm = str(self.fm_home)
        herdr = FakeHerdr([
            pane("t:p1", cwd=fm, label="Mobile Concepts"),
            pane("t:p2", cwd="/elsewhere", label="Firstmate"),
            pane("t:p3", cwd=fm, label="🧭 Firstmate"),
        ])
        found, _ = self.keeper(herdr).find_pane()
        self.assertEqual(found.pane_id, "t:p3")

    def test_ambiguous_discovery_refuses(self):
        fm = str(self.fm_home)
        herdr = FakeHerdr([pane("t:p1", cwd=fm), pane("t:p2", cwd=fm)])
        found, reason = self.keeper(herdr).find_pane()
        self.assertIsNone(found)
        self.assertIn("more than one", reason)

    def test_pinned_pane_ignores_discovery(self):
        # A pane that looks exactly like the live Firstmate is present, but a
        # pinned target must never resolve to it.
        live_like = pane("w1M:p1", cwd=str(self.fm_home), label="Firstmate")
        scratch = pane("t:scratch", cwd="/tmp", label="scratch")
        self.cfg.target_pane = "t:scratch"
        k = self.keeper(FakeHerdr([live_like, scratch]))
        self.assertEqual(k.find_pane()[0].pane_id, "t:scratch")
        self.cfg.target_pane = "t:gone"
        self.assertIsNone(k.find_pane()[0])


class FreshSessionTest(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg.target_pane = "t:p1"
        self.cfg.fresh_wait_seconds = 60

    def test_idle_pane_gets_clear(self):
        herdr = FakeHerdr([pane(status="idle")])
        ok, msg = self.keeper(herdr).fresh_session()
        self.assertTrue(ok)
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])

    def test_waits_for_working_turn_to_finish(self):
        herdr = FakeHerdr([pane(status="working")])
        clock = FakeClock()
        start = clock.now

        def finish(now):
            if now - start >= 20:
                herdr.panes[0]["agent_status"] = "idle"
            else:
                self.assertEqual(herdr.runs, [])  # nothing typed mid-turn

        clock.on_sleep = finish
        ok, _ = self.keeper(herdr, clock).fresh_session()
        self.assertTrue(ok)
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])
        self.assertGreaterEqual(clock.now - start, 20)

    def test_never_interrupts_until_timeout(self):
        for status in ("working", "blocked", "unknown"):
            herdr = FakeHerdr([pane(status=status)])
            ok, msg = self.keeper(herdr).fresh_session()
            self.assertFalse(ok)
            self.assertIn(status, msg)
            self.assertIn("`restart force`", msg)
            self.assertEqual(herdr.runs, [])
            self.assertEqual(herdr.keys, [])

    def test_force_interrupts_with_escape_then_clears_when_idle(self):
        herdr = FakeHerdr([pane(status="working")])
        herdr.on_keys = lambda key: herdr.panes[0].update(agent_status="idle")
        ok, msg = self.keeper(herdr).fresh_session(force=True)
        self.assertTrue(ok)
        self.assertIn("after interrupting its turn", msg)
        self.assertEqual(herdr.keys, [("t:p1", "esc")])
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])

    def test_force_sends_one_escape_and_still_waits_for_idle(self):
        herdr = FakeHerdr([pane(status="working")])
        ok, msg = self.keeper(herdr).fresh_session(force=True)
        self.assertFalse(ok)
        self.assertIn("after the interrupt", msg)
        self.assertEqual(herdr.keys, [("t:p1", "esc")])
        self.assertEqual(herdr.runs, [])

    def test_force_leaves_an_idle_pane_uninterrupted(self):
        herdr = FakeHerdr([pane(status="idle")])
        self.assertTrue(self.keeper(herdr).fresh_session(force=True)[0])
        self.assertEqual(herdr.keys, [])

    def test_a_forced_request_forces_the_collapsed_clear(self):
        herdr = FakeHerdr([pane(status="working")])
        herdr.on_keys = lambda key: herdr.panes[0].update(agent_status="idle")
        k = self.keeper(herdr)
        k.request_fresh_session("slack", channel="D1", thread_ts="1.0")
        k.request_fresh_session("slack", channel="D1", thread_ts="2.0", force=True)
        self.assertEqual(k.process_fresh_requests(), 2)
        self.assertEqual(herdr.keys, [("t:p1", "esc")])
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])

    def test_a_late_restart_force_gets_a_full_wait_after_its_escape(self):
        herdr = FakeHerdr([pane(status="working")])
        clock = FakeClock()
        start = clock.now
        k = self.keeper(herdr, clock)
        k.request_fresh_session("slack", channel="D1", thread_ts="1.0")

        def on_sleep(now):
            if now - start >= 55 and not herdr.keys and not self.state.read()["fresh_requests"]:
                k.request_fresh_session("slack", channel="D1", thread_ts="2.0", force=True)
            if herdr.keys and now - self.escaped_at >= 30:
                herdr.panes[0]["agent_status"] = "idle"  # Firstmate needs 30 s to stop

        herdr.on_keys = lambda key: setattr(self, "escaped_at", clock.now)
        clock.on_sleep = on_sleep
        self.assertEqual(k.process_fresh_requests(), 2)
        self.assertEqual(herdr.keys, [("t:p1", "esc")])
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])
        self.assertTrue(all(text.startswith("✅") for text, _, _ in self.notified))

    def test_busy_counts_one_unchanged_busy_status(self):
        herdr = FakeHerdr([pane(status="working")])
        clock = FakeClock()
        k = self.keeper(herdr, clock)
        k.find_pane()
        clock.now += 599
        k.find_pane()
        self.assertEqual(k.busy(), ("working", 599))
        herdr.panes[0]["agent_status"] = "blocked"  # a change is progress
        k.find_pane()
        self.assertEqual(k.busy(), ("blocked", 0))
        herdr.panes[0]["agent_status"] = "idle"
        clock.now += 900
        k.find_pane()
        self.assertIsNone(k.busy())

    def test_restart_force_during_an_ordinary_wait_interrupts_at_once(self):
        herdr = FakeHerdr([pane(status="working")])
        herdr.on_keys = lambda key: herdr.panes[0].update(agent_status="idle")
        clock = FakeClock()
        start = clock.now
        k = self.keeper(herdr, clock)
        k.request_fresh_session("slack", channel="D1", thread_ts="1.0")

        def captain_sends_restart_force(now):
            if not self.state.read()["fresh_requests"] and not herdr.keys:
                k.request_fresh_session("slack", channel="D1", thread_ts="2.0", force=True)

        clock.on_sleep = captain_sends_restart_force
        self.assertEqual(k.process_fresh_requests(), 2)
        self.assertLess(clock.now - start, 30)
        self.assertEqual(herdr.keys, [("t:p1", "esc")])
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])
        self.assertEqual([(c, t) for _, c, t in self.notified], [("D1", "1.0"), ("D1", "2.0")])
        self.assertTrue(all(text.startswith("✅") for text, _, _ in self.notified))
        self.assertEqual(self.state.read()["fresh_requests"], [])

    def test_no_agent_is_not_cleared(self):
        herdr = FakeHerdr([pane(agent=None, status="unknown")])
        ok, msg = self.keeper(herdr).fresh_session()
        self.assertFalse(ok)
        self.assertIn("no agent", msg)
        self.assertEqual(herdr.runs, [])

    def test_queued_requests_collapse_into_one_clear(self):
        herdr = FakeHerdr([pane(status="idle")])
        k = self.keeper(herdr)
        k.request_fresh_session("slack", channel="D1", thread_ts="1.0")
        k.request_fresh_session("cli")
        self.assertEqual(k.process_fresh_requests(), 2)
        self.assertEqual(herdr.runs, [("t:p1", "/clear")])
        self.assertEqual([(c, t) for _, c, t in self.notified], [("D1", "1.0"), (None, None)])
        self.assertTrue(self.notified[0][0].startswith("✅"))
        self.assertEqual(self.state.read()["fresh_requests"], [])
        self.assertEqual(k.process_fresh_requests(), 0)


class RestartTest(TempCase):
    def setUp(self):
        super().setUp()
        self.cfg.target_pane = "t:p1"
        self.herdr = FakeHerdr([pane(cwd="/fm home", agent=None, status="unknown")])
        self.clock = FakeClock()
        self.k = self.keeper(self.herdr, self.clock)

    def tick(self, n=1):
        return [self.k.check() for _ in range(n)]

    def test_running_agent_is_left_alone(self):
        self.herdr.panes[0].update(agent="claude", agent_status="working")
        self.assertEqual(self.tick(3), ["ok"] * 3)
        self.assertEqual(self.herdr.runs, [])

    def test_restart_after_two_missing_checks(self):
        self.assertEqual(self.tick(), ["agent-missing-once"])
        self.assertEqual(self.herdr.runs, [])
        self.assertEqual(self.tick(), ["restarted"])
        self.assertEqual(self.herdr.runs, [("t:p1", "cd '/fm home' && claude")])
        self.assertIn("started `claude`", self.notified[-1][0])

    def test_transient_report_does_not_restart(self):
        self.tick()
        self.herdr.panes[0].update(agent="claude", agent_status="idle")
        self.tick()
        self.herdr.panes[0].pop("agent")
        self.assertEqual(self.tick(), ["agent-missing-once"])
        self.assertEqual(self.herdr.runs, [])

    def test_rate_limit_then_alert_once(self):
        outcomes = []
        for _ in range(5):
            outcomes += self.tick(2)
            self.clock.now += 60
        self.assertEqual(outcomes.count("restarted"), 3)
        self.assertEqual(outcomes[-1], "rate-limited")
        self.assertEqual(len(self.herdr.runs), 3)
        alerts = [t for t, _, _ in self.notified if "restarts in the last hour" in t]
        self.assertEqual(len(alerts), 1)
        # Once the window passes, restarts are allowed again.
        self.clock.now += HOUR
        self.assertIn("restarted", self.tick(2))
        self.assertEqual(len(self.herdr.runs), 4)

    def test_rate_limit_survives_a_daemon_restart(self):
        self.tick(6)
        fresh = self.keeper(self.herdr, self.clock)  # new process, same state file
        self.assertEqual([fresh.check(), fresh.check()], ["agent-missing-once", "rate-limited"])
        self.assertEqual(len(self.herdr.runs), 3)

    def test_busy_foreground_is_not_typed_into(self):
        self.herdr.fg = {"foreground_process_group_id": 9, "shell_pid": 7,
                         "foreground_processes": [{"name": "vim", "pid": 9}]}
        self.assertEqual(self.tick(2), ["agent-missing-once", "fg-busy"])
        self.assertEqual(self.herdr.runs, [])
        self.assertIn("vim", self.notified[-1][0])

    def test_missing_pane_alerts_once_and_never_creates(self):
        self.herdr.panes = []
        self.assertEqual(self.tick(3), ["no-pane"] * 3)
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.herdr.runs, [])


class HerdrCliTest(TempCase):
    """The real Herdr wrapper against the fake herdr binary: exact argv."""

    def test_argv_is_pane_level_only(self):
        d = self.tmp / "herdr"
        d.mkdir()
        (d / "panes.json").write_text(json.dumps([pane("t:p1")]))
        os.environ["FAKE_HERDR_DIR"] = str(d)
        try:
            h = Herdr(str(FAKES / "herdr"), "test")
            self.assertEqual(h.list_panes()[0]["pane_id"], "t:p1")
            h.process_info("t:p1")
            h.run("t:p1", "/clear")
            h.send_keys("t:p1", "esc")
        finally:
            del os.environ["FAKE_HERDR_DIR"]
        calls = [json.loads(line) for line in (d / "calls.log").read_text().splitlines()]
        self.assertEqual(calls, [
            ["--session", "test", "pane", "list"],
            ["--session", "test", "pane", "process-info", "--pane", "t:p1"],
            ["--session", "test", "pane", "run", "t:p1", "/clear"],
            ["--session", "test", "pane", "send-keys", "t:p1", "esc"],
        ])
        for c in calls:
            self.assertEqual(c[2], "pane")
