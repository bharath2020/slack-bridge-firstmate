"""Load report, shed, bridge restart and the load guard, against fakes only.

ps, sysctl, taskpolicy and launchctl are fake scripts; signals go to an
in-process fake kill. Nothing here can touch a real process or LaunchAgent.
"""

import json
import os
import signal
from unittest import mock

from slack_bridge import sysload as S
from slack_bridge.config import Config
from slack_bridge.guard import FirstmateRoots, LoadGuard, Rescue
from slack_bridge.suggest import CLAUDE_ARGS, CODEX_ARGS, Advice, Advisor

from .helpers import DM, FAKES, FakeClock, FakeHerdr, TempCase, pane

ME = os.getpid()
FM_SHELL = 500

# pid ppid %cpu rss(kB) command
PS = f"""\
  1     0   0.5   10000 /sbin/launchd
{ME}    1   0.1   50000 /usr/bin/python3 -m slack_bridge run
  610 {ME}  0.0    2000 adb -s emulator-5554 logcat
  {FM_SHELL}  1   0.0    3000 -zsh
  501   {FM_SHELL}  12.0  400000 claude
  502   501   0.0    2000 adb -s emulator-5554 shell am instrument -w com.app.test
  700     1  30.0  600000 claude --resume worker
  710     1   2.0  300000 claude remote-control
  800     1   1.0  100000 /test-home/Library/Android/sdk/emulator/emulator -avd Pixel_8
  801   800 180.0 4000000 /test-home/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64 -avd Pixel_8 -grpc 8554
  900     1 150.0 4100000 qemu-system-aarch64-headless -avd Tablet_API_35
  950   700   5.0  800000 java -jar gradle-wrapper.jar connectedDebugAndroidTest
  960     1  90.0  200000 /System/Library/Frameworks/CoreServices.framework/mds_stores
  970     1  40.0  300000 /usr/libexec/photoanalysisd
  980     1   3.0  900000 /Applications/Safari.app/Contents/MacOS/Safari
"""


class SysCase(TempCase):
    def setUp(self):
        super().setUp()
        self.sys = self.tmp / "sys"
        self.sys.mkdir()
        (self.sys / "ps.txt").write_text(PS)
        env = mock.patch.dict(os.environ, {"FAKE_SYS_DIR": str(self.sys)})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = Config(fm_home=str(self.fm_home), state_dir=self.tmp / "state",
                          ps_bin=str(FAKES / "ps"), sysctl_bin=str(FAKES / "sysctl"),
                          taskpolicy_bin=str(FAKES / "taskpolicy"),
                          launchctl_bin=str(FAKES / "launchctl"),
                          launchd_label="test.slack-bridge")
        self.clock = FakeClock()
        self.load = (10.0, 10.0, 10.0)
        self.killed: list[tuple[int, int]] = []
        self.dead: set[int] = set()
        self.roots_known = True

    def rescue(self):
        return Rescue(self.cfg, lambda: ({FM_SHELL}, True) if self.roots_known else (set(), False),
                      self.state, clock=self.clock, sleep=self.clock.sleep, loadavg=lambda: self.load,
                      cores=14, kill=lambda pid, sig: self.killed.append((pid, sig)),
                      alive=lambda pid: pid not in self.dead)

    def calls(self, tool=None):
        log = self.sys / "calls.log"
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [r for r in rows if tool is None or r[0] == tool]

    def cpu_bound(self):
        (self.sys / "sysctl.txt").write_text(
            "1\ntotal = 2048.00M  used = 0.00M  free = 2048.00M\n51539607552")


class SysloadTest(SysCase):
    def test_protected_trees_and_system_processes(self):
        procs = S.parse_ps(PS)
        keep = S.protected_pids(procs, {ME, FM_SHELL})
        self.assertTrue({ME, 610, FM_SHELL, 501, 502, 710, 1, 960, 970} <= keep)
        self.assertFalse({700, 800, 801, 900, 950, 980} & keep)

    def test_suggestion_follows_memory_or_cpu(self):
        self.assertEqual(S.suggest(S.Pressure("warning", 0.0, 48))[0], S.QUIT)
        self.assertEqual(S.suggest(S.Pressure("normal", 9000.0, 48))[0], S.QUIT)  # heavy swap
        self.assertEqual(S.suggest(S.Pressure("normal", 100.0, 48))[0], S.PAUSE)


class RescueTest(SysCase):
    def offered(self, thread_ts="1.0"):
        return [i["pid"] for i in self.state.read()["shed_offers"][f"{DM} {thread_ts}"]["procs"]]

    def test_load_report_is_fast_and_names_offenders(self):
        def roots():
            raise AssertionError("load must not look up the Firstmate pane")

        r = self.rescue()
        r.roots = roots
        self.load = (48.0, 45.5, 30.0)
        text = r.load_report()
        self.assertIn("load 48.0 / 45.5 / 30.0", text)
        self.assertIn("on 14 cores; alarm above 28", text)
        self.assertIn("pressure warning, swap used 1.5 GB, 48 GB RAM", text)
        self.assertIn("Top CPU: qemu-system-aarch64 180%, qemu-system-aarch64-headless 150%",
                      text.replace("*", ""))
        self.assertIn("Top memory: qemu-system-aarch64-headless 3.9 GB", text.replace("*", ""))
        self.assertIn("Reply `shed`", text)
        for detail in ("By kind", "emulators", "Tablet_API_35", "-grpc"):  # names only
            self.assertNotIn(detail, text)
        self.assertEqual({c[0] for c in self.calls()}, {"ps"})
        self.assertEqual(self.state.read()["shed_offers"], {})

    def test_shed_lists_with_a_suggestion_and_touches_nothing(self):
        text = self.rescue().offer(DM, "1.0")
        self.assertIn("qemu-system-aarch64-headless (pid 900)", text)
        self.assertIn("qemu-system-aarch64 (pid 801)", text)
        self.assertIn("Suggested: `shed quit` (memory pressure is warning)", text)
        self.assertIn("Reply `yes`", text)
        self.assertEqual(self.offered(), [900, 801])  # memory: heaviest by RSS
        for pid in (ME, 610, 501, 502, 710, 1, 960, 970):  # bridge, Firstmate, RC, macOS
            self.assertNotIn(f"pid {pid})", text)
        self.assertEqual(self.killed, [])

    def test_cpu_bound_suggests_pause_of_the_busiest(self):
        self.cpu_bound()
        text = self.rescue().offer(DM, "1.0")
        self.assertIn("Suggested: `shed pause`", text)
        self.assertEqual(self.offered(), [801, 900, 700])

    def test_memory_suggestion_with_only_cpu_heavy_processes_pauses_them(self):
        (self.sys / "sysctl.txt").write_text(
            "1\ntotal = 12288.00M  used = 9216.00M  free = 3072.00M\n51539607552")
        (self.sys / "ps.txt").write_text(f"{ME} 1 0.1 50000 python3\n"
                                         "1200 1 100.0 600000 swift-frontend -c a.swift\n"
                                         "1201 1 95.0 400000 clang -c b.c\n"
                                         "1202 1 2.0 100000 node idle.js\n")
        text = self.rescue().offer(DM, "1.0")
        self.assertNotIn("Nothing to shed", text)
        self.assertIn("Suggested: `shed pause` (9.0 GB of swap is in use, but nothing "
                      "unprotected holds 1.0 GB or more)", text)
        self.assertEqual(self.offered(), [1200, 1201])

    def test_unknown_firstmate_pane_protects_every_agent_session(self):
        self.roots_known = False
        self.cpu_bound()
        self.rescue().offer(DM, "1.0")
        self.assertEqual(self.offered(), [801, 900])

    def test_quit_acts_only_on_listed_processes_that_are_still_the_same(self):
        r = self.rescue()
        r.offer(DM, "1.0")
        # pid 900 exited and was reused by another program before the reply.
        (self.sys / "ps.txt").write_text(PS.replace(
            "qemu-system-aarch64-headless -avd Tablet_API_35", "/Applications/Notes.app/Notes"))
        self.dead = {801}
        text = r.shed("quit", DM, "1.0")
        self.assertEqual(self.killed, [(801, signal.SIGTERM)])
        self.assertIn("quit qemu-system-aarch64 (pid 801)", text)
        self.assertIn("left qemu-system-aarch64-headless (pid 900) alone", text)
        self.assertEqual(self.state.read()["shed_offers"], {})  # used up
        self.assertEqual(self.calls("adb"), [])

    def test_quit_reports_what_is_still_running(self):
        r = self.rescue()
        r.offer(DM, "1.0")
        text = r.shed("quit", DM, "1.0")
        self.assertEqual(sorted(self.killed), [(801, signal.SIGTERM), (900, signal.SIGTERM)])
        self.assertIn("(pid 900) is still running 10s after SIGTERM", text)

    def test_yes_pauses_then_resume_continues(self):
        self.cpu_bound()
        r = self.rescue()
        r.offer(DM, "1.0")
        text = r.shed(None, DM, "1.0")  # one-word accept of the suggestion
        self.assertEqual(self.killed, [(801, signal.SIGSTOP), (900, signal.SIGSTOP),
                                       (700, signal.SIGSTOP)])
        self.assertIn("paused claude (pid 700)", text)
        self.assertIn("Reply `resume`", text)
        self.assertEqual([i["pid"] for i in self.state.read()["paused"]], [801, 900, 700])
        self.killed.clear()
        text = r.resume()
        self.assertEqual(self.killed, [(801, signal.SIGCONT), (900, signal.SIGCONT),
                                       (700, signal.SIGCONT)])
        self.assertIn("resumed qemu-system-aarch64 (pid 801)", text)
        self.assertEqual(self.state.read()["paused"], [])
        self.assertIn("Nothing to resume", r.resume())

    def test_yes_counts_only_in_the_offer_thread_and_expires(self):
        r = self.rescue()
        r.offer(DM, "1.0")
        self.assertIsNotNone(r.current_offer(DM, "1.0"))
        self.assertIsNone(r.current_offer(DM, "2.0"))  # another thread
        self.assertIsNone(r.current_offer("C0001", "1.0"))
        self.assertIn("No current `shed` list here", r.shed(None, DM, "2.0"))
        self.assertEqual(self.killed, [])
        self.clock.now += 601
        self.assertIsNone(r.current_offer(DM, "1.0"))
        text = r.shed("quit", DM, "1.0")
        self.assertIn("nothing was touched", text)
        self.assertIn("Suggested:", text)  # a fresh list to act on
        self.assertEqual(self.killed, [])

    def test_explicit_action_top_level_uses_the_newest_list(self):
        r = self.rescue()
        r.offer(DM, "1.0")
        self.cpu_bound()
        self.clock.now += 5
        r.offer(DM, "2.0")
        r.shed("pause", DM, "9.0", top_level=True)
        self.assertEqual(self.killed, [(801, signal.SIGSTOP), (900, signal.SIGSTOP),
                                       (700, signal.SIGSTOP)])
        self.assertEqual(list(self.state.read()["shed_offers"]), [f"{DM} 1.0"])

    def two_lists(self):
        """The captain's quit list A in thread 1.0, then the alarm's pause list B in 77.0."""
        r = self.rescue()
        r.offer(DM, "1.0")
        self.cpu_bound()
        self.clock.now += 120
        r.keep_offer(DM, "77.0", r.draft_offer()[1])
        return r

    def test_explicit_action_in_a_used_alarm_thread_never_takes_another_list(self):
        r = self.two_lists()
        r.shed(None, DM, "77.0")  # `yes` in B pauses B's processes
        self.killed.clear()
        text = r.shed("quit", DM, "77.0")
        self.assertIn("No current `shed` list here, so nothing was touched", text)
        self.assertEqual(self.killed, [])
        self.assertEqual(self.offered("1.0"), [900, 801])  # A is untouched
        self.assertEqual(self.offered("77.0"), [801, 900, 700])  # a fresh list in B

    def test_explicit_action_in_a_used_captain_thread_never_takes_another_list(self):
        r = self.two_lists()
        r.shed(None, DM, "1.0")  # `yes` in A quits A's processes
        self.killed.clear()
        text = r.shed("pause", DM, "1.0")
        self.assertIn("No current `shed` list here, so nothing was touched", text)
        self.assertEqual(self.killed, [])
        self.assertIn(f"{DM} 77.0", self.state.read()["shed_offers"])  # B is untouched
        self.assertEqual(self.offered("1.0"), [801, 900, 700])  # a fresh list in A

    def test_alarm_list_never_replaces_one_the_captain_asked_for(self):
        r = self.rescue()
        r.offer(DM, "1.0")  # the captain's `shed` (quit: memory pressure)
        self.cpu_bound()
        self.clock.now += 120
        text, offer = r.draft_offer()  # the alarm's list, kept only once posted
        self.assertIn("Suggested: `shed pause`", text)
        self.assertIsNone(r.current_offer(DM, "77.0"))
        r.keep_offer(DM, "77.0", offer)
        self.assertEqual(self.offered("1.0"), [900, 801])
        self.assertEqual(self.offered("77.0"), [801, 900, 700])
        r.shed(None, DM, "1.0")  # `yes` in the captain's thread: its own list
        self.assertEqual(sorted(self.killed), [(801, signal.SIGTERM), (900, signal.SIGTERM)])
        self.killed.clear()
        r.shed(None, DM, "77.0")  # `yes` in the alert's thread
        self.assertEqual(self.killed, [(801, signal.SIGSTOP), (900, signal.SIGSTOP),
                                       (700, signal.SIGSTOP)])
        self.assertEqual(self.state.read()["shed_offers"], {})

    def test_lists_expire_on_their_own_and_are_capped(self):
        r = self.rescue()
        for i in range(7):
            r.offer(DM, f"{i}.0")
            self.clock.now += 100
        offers = self.state.read()["shed_offers"]
        self.assertEqual(list(offers), [f"{DM} {i}.0" for i in range(2, 7)])
        self.clock.now += 101
        self.assertIsNone(r.current_offer(DM, "2.0"))  # over 600 s old: expired
        self.assertIsNotNone(r.current_offer(DM, "6.0"))

    def test_old_single_list_state_is_dropped(self):
        self.state.path.parent.mkdir(parents=True, exist_ok=True)
        self.state.path.write_text(json.dumps({"shed_offer": {
            "at": int(self.clock.now), "channel": DM, "thread_ts": "1.0", "action": "quit",
            "procs": [{"pid": 900, "command": "x", "label": "x"}]}}))
        r = self.rescue()
        self.assertIsNone(r.current_offer(DM, "1.0"))
        r.offer(DM, "2.0")
        self.assertNotIn("shed_offer", json.loads(self.state.path.read_text()))

    def test_shed_with_nothing_heavy(self):
        (self.sys / "ps.txt").write_text(f"{ME} 1 0.1 50000 python3\n700 1 1.0 1000 claude\n")
        self.assertIn("Nothing to shed", self.rescue().offer(DM, "1.0"))
        self.assertEqual(self.state.read()["shed_offers"], {})
        self.assertEqual(self.killed, [])

    def test_restart_bridge_kickstarts_its_own_label(self):
        r = self.rescue()
        r.restart_bridge()
        r._kickstart.wait(timeout=10)
        self.assertEqual(self.calls("launchctl"),
                         [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/test.slack-bridge"]])

    def test_restart_bridge_refuses_without_a_label(self):
        self.cfg.launchd_label = ""
        with self.assertRaises(OSError):
            self.rescue().restart_bridge()


class LoadGuardTest(SysCase):
    def setUp(self):
        super().setUp()
        self.notified = []

        def notify(text, channel=None, thread_ts=None):
            self.notified.append(text)
            return DM, f"{len(self.notified)}.5"

        self.guard = LoadGuard(self.cfg, self.rescue(), self.state, notify, clock=self.clock)

    def tick(self, seconds=30):
        self.clock.now += seconds
        return self.guard.tick()

    def policy(self):
        return [(c[1], int(c[3])) for c in self.calls("taskpolicy")]

    def test_demotion_starts_on_the_1_minute_average(self):
        self.load = (50.0, 20.0, 10.0)  # 1-min above 28, 5-min not yet
        self.assertEqual(self.tick(), "ok")
        # Heavy unprotected processes and their children (java under the worker).
        self.assertEqual(self.policy(), [("-b", 700), ("-b", 801), ("-b", 900), ("-b", 950)])
        self.assertEqual(self.notified, [])
        self.load = (10.0, 10.0, 10.0)
        self.assertEqual(self.tick(), "ok")
        self.assertEqual(self.policy()[4:], [("-B", 700), ("-B", 801), ("-B", 900),
                                             ("-B", 950)])
        self.assertEqual(self.state.read()["guard"]["demoted"], [])
        self.assertEqual(self.notified, [])

    def test_alarm_after_sustained_load_then_recovery(self):
        self.load = (50.0, 45.0, 30.0)  # threshold 28 on 14 cores
        self.assertEqual(self.guard.tick(), "high")
        self.assertEqual(len(self.policy()), 4)  # demoted before the alarm
        for _ in range(9):
            self.assertEqual(self.tick(), "high")
        self.assertEqual(self.notified, [])
        self.assertEqual(self.tick(), "raised")  # 300 s above the threshold
        self.assertEqual(len(self.notified), 1)
        alert = self.notified[0]
        self.assertIn("The Mac is overloaded", alert)
        self.assertIn("Moved 4 heavy processes and their children to background priority",
                      alert)
        self.assertIn("Suggested: `shed quit`", alert)
        self.assertIn("Reply `yes`", alert)
        self.assertNotIn("emulators are running", alert)
        self.assertIn("Top memory:", alert)
        self.assertEqual(list(self.state.read()["shed_offers"]), [f"{DM} 1.5"])  # the alert
        self.assertEqual(self.tick(), "active")
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(len(self.policy()), 4)  # nothing demoted twice

        self.load = (20.0, 23.0, 25.0)  # below the threshold, not yet clear
        self.assertEqual(self.tick(), "active")
        self.load = (10.0, 20.0, 25.0)  # under 80% of it
        self.assertEqual(self.tick(), "cleared")
        self.assertEqual(sorted(self.policy()[4:]), [("-B", 700), ("-B", 801), ("-B", 900),
                                                     ("-B", 950)])
        self.assertIn("back to normal", self.notified[-1])
        self.assertIn("Restored 4 processes", self.notified[-1])
        self.assertEqual(self.state.read()["guard"]["demoted"], [])

    def test_processes_demoted_on_the_clearing_tick_are_restored(self):
        self.load = (50.0, 45.0, 30.0)
        for _ in range(11):
            self.tick()
        self.assertTrue(self.state.read()["guard"]["active"])
        # A new build starts as the episode winds down.
        (self.sys / "ps.txt").write_text(PS + "  990   700  60.0  500000 java GradleWorkerMain\n")
        self.load = (29.0, 22.0, 25.0)  # 1-min above 28, 5-min under 80% of it
        self.assertEqual(self.tick(), "cleared")
        demoted = {p for flag, p in self.policy() if flag == "-b"}
        restored = {p for flag, p in self.policy() if flag == "-B"}
        self.assertLessEqual(demoted, restored)
        self.assertEqual(self.state.read()["guard"]["demoted"], [])

    def test_repeat_overload_inside_the_realert_window_is_announced_later(self):
        self.load = (50.0, 45.0, 30.0)
        for _ in range(11):
            self.tick()
        self.load = (5.0, 5.0, 5.0)
        self.tick()
        self.assertEqual(len(self.notified), 2)  # alert + recovery
        self.load = (50.0, 45.0, 30.0)
        outcomes = [self.tick() for _ in range(11)]
        self.assertEqual(outcomes[-1], "raised-quiet")  # within 30 min of the last alert
        self.assertEqual(len(self.notified), 2)
        for _ in range(80):  # the overload goes on for up to 40 more minutes
            outcome = self.tick()
            if len(self.notified) > 2:
                break
        self.assertEqual(outcome, "alerted")
        self.assertIn("The Mac is overloaded", self.notified[2])
        self.assertEqual(self.tick(), "active")  # once per episode
        self.load = (5.0, 5.0, 5.0)
        self.tick()
        self.assertIn("back to normal", self.notified[-1])

    def test_unknown_firstmate_pane_leaves_agent_trees_alone(self):
        self.roots_known = False
        self.load = (50.0, 45.0, 30.0)
        for _ in range(11):
            self.tick()
        self.assertEqual(sorted(p for _, p in self.policy()), [801, 900])

    def test_guard_off_still_alerts_but_never_demotes(self):
        self.cfg.guard_enabled = False
        self.load = (50.0, 45.0, 30.0)
        for _ in range(11):
            self.tick()
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.policy(), [])


class SuggestTest(SysCase):
    """The model CLI only narrows and words the suggestion; fake claude/codex."""

    def setUp(self):
        super().setUp()
        self.cfg.suggest_cli = str(FAKES / "claude")
        self.cfg.suggest_fallback_cli = str(FAKES / "codex")
        self.advisor = Advisor(self.cfg)

    def answer(self, text):
        (self.sys / "answer.txt").write_text(text)

    def runs(self):
        log = self.sys / "model.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def offer(self):
        r = self.rescue()
        r.advisor = self.advisor
        text = r.offer(DM, "1.0")
        offer = self.state.read()["shed_offers"].get(f"{DM} 1.0")
        return text, offer and [i["pid"] for i in offer["procs"]]

    def test_model_picks_among_unprotected_pids_from_names_and_numbers_only(self):
        self.answer('Sure. {"action": "pause", "pids": [900, 710, 501, 4242], '
                    '"why": "The <headless> emulator burns CPU"}')
        text, pids = self.offer()
        self.assertEqual(pids, [900])  # protected (710, 501) and unknown pids dropped
        self.assertIn("Suggested by claude: `shed pause` (The &lt;headless&gt; emulator burns "
                      "CPU)", text)
        self.assertEqual(self.killed, [])  # the captain still has to reply
        run, = self.runs()
        self.assertEqual(run["argv"], ["claude", *CLAUDE_ARGS])
        self.assertEqual(self.calls("taskpolicy")[0][:3],
                         ["taskpolicy", "-b", str(FAKES / "claude")])
        # The emitted prompt: names and numbers of the unprotected candidates only.
        facts = json.loads(run["prompt"].splitlines()[-1])
        self.assertEqual([(p["pid"], p["name"]) for p in facts["processes"]],
                         [(900, "qemu-system-aarch64-headless"), (801, "qemu-system-aarch64"),
                          (700, "claude")])
        self.assertEqual(facts["memory_pressure"], "warning")
        for secret in ("Tablet_API_35", "-grpc", "/test-home", "--resume", "worker"):
            self.assertNotIn(secret, run["prompt"])

    def test_unusable_answers_fall_back_to_the_rules_and_say_so(self):
        for answer in ('{"action": "kill", "pids": [900]}',
                       '{"action": "quit", "pids": [710, 1]}',  # protected only
                       "I would quit the emulators."):
            self.answer(answer)
            text, pids = self.offer()
            self.assertEqual(pids, [900, 801])
            self.assertIn("Suggested: `shed quit` (memory pressure is warning). Rule-based: "
                          "claude gave no", text)

    def test_timeout_falls_back_to_the_rules(self):
        self.cfg.suggest_timeout_seconds = 1
        (self.sys / "hang").touch()
        text, pids = self.offer()
        self.assertEqual(pids, [900, 801])
        self.assertIn("Rule-based: claude gave no answer within 1s.", text)

    def test_codex_when_claude_is_not_installed(self):
        self.cfg.suggest_cli = str(self.tmp / "no-claude")
        self.answer('{"action": "quit", "pids": [801]}')
        text, pids = self.offer()
        self.assertEqual(pids, [801])
        self.assertIn("Suggested by codex: `shed quit` (memory pressure is warning)", text)
        self.assertEqual(self.runs()[0]["argv"], ["codex", *CODEX_ARGS])

    def test_no_cli_at_all(self):
        self.cfg.suggest_cli = str(self.tmp / "no-claude")
        self.cfg.suggest_fallback_cli = ""
        text, _ = self.offer()
        self.assertIn("Rule-based: no claude or codex CLI found.", text)

    def test_restart_advice_is_wait_or_force_only(self):
        rule = Advice("wait", "Firstmate has been working for ~5 min")
        self.answer('{"choice": "force", "why": "Five quiet minutes"}')
        advice = self.advisor.restart("working", 300, rule)
        self.assertEqual((advice.choice, advice.why, advice.by), ("force", "Five quiet minutes",
                                                                  "claude"))
        facts = json.loads(self.runs()[0]["prompt"].splitlines()[-1])
        self.assertEqual((facts["firstmate_status"], facts["busy_minutes"]), ("working", 5))
        self.answer('{"choice": "kill -9"}')
        advice = self.advisor.restart("working", 300, rule)
        self.assertEqual((advice.choice, advice.by), ("wait", None))
        self.assertEqual(advice.fallback, "claude gave no usable answer")


class FirstmateRootsTest(TempCase):
    def test_roots_come_from_the_pinned_pane(self):
        self.cfg.target_pane = "t:p1"
        herdr = FakeHerdr([pane(cwd=str(self.fm_home))])
        herdr.fg = {"shell_pid": 4242}
        roots = FirstmateRoots(self.keeper(herdr))
        self.assertEqual(roots(), ({4242}, True))
        self.assertEqual(FirstmateRoots(None)(), (set(), False))


class ConfigTest(TempCase):
    def test_defaults_and_overrides(self):
        cfg = Config.from_env({})
        self.assertEqual((cfg.handoff_timeout_seconds, cfg.load_threshold, cfg.load_factor,
                          cfg.load_sustain_seconds, cfg.guard_enabled, cfg.load_alarm_enabled,
                          cfg.launchd_label, cfg.stuck_seconds, cfg.suggest_enabled,
                          cfg.suggest_cli, cfg.suggest_fallback_cli, cfg.suggest_timeout_seconds),
                         (180.0, None, 2.0, 300.0, True, True, "com.firstmate.slack-bridge",
                          600.0, True, "claude", "codex", 25.0))
        cfg = Config.from_env({"SLACK_BRIDGE_HANDOFF_TIMEOUT": "240",
                               "SLACK_BRIDGE_LOAD_THRESHOLD": "30", "SLACK_BRIDGE_GUARD": "0",
                               "SLACK_BRIDGE_LOAD_ALARM": "0", "SLACK_BRIDGE_LAUNCHD_LABEL": "",
                               "SLACK_BRIDGE_STUCK_AFTER": "900", "SLACK_BRIDGE_SUGGEST": "0",
                               "SLACK_BRIDGE_SUGGEST_CLI": "/opt/claude",
                               "SLACK_BRIDGE_SUGGEST_CODEX": "/opt/codex",
                               "SLACK_BRIDGE_SUGGEST_TIMEOUT": "20"})
        self.assertEqual((cfg.handoff_timeout_seconds, cfg.load_threshold, cfg.guard_enabled,
                          cfg.load_alarm_enabled, cfg.launchd_label, cfg.stuck_seconds,
                          cfg.suggest_enabled, cfg.suggest_cli, cfg.suggest_fallback_cli,
                          cfg.suggest_timeout_seconds),
                         (240.0, 30.0, False, False, "", 900.0, False, "/opt/claude",
                          "/opt/codex", 20.0))
