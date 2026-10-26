import io
import json
import logging
import os
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from slack_bridge import __main__ as cli
from slack_bridge import images
from slack_bridge.config import Secrets
from slack_bridge.images import Image, ImageError
from slack_bridge.outbound import OutboundError, say
from slack_bridge.shot import ShotError, page_url, render
from slack_bridge.slack_api import SlackCallError, WebSlack

from .helpers import CAPTAIN, DM, FAKES, TempCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
KEY = "example-build"


class FakeFilesClient:
    """slack_sdk WebClient stand-in for the external upload methods."""

    class ApiError(Exception):
        def __init__(self, code):
            super().__init__("upload_url=https://files.slack.com/secret")
            self.response = {"ok": False, "error": code}

    def __init__(self):
        self.calls = []
        self.errors = {}

    def files_getUploadURLExternal(self, **kw):
        self.calls.append(("getUploadURLExternal", kw))
        if "get" in self.errors:
            raise self.ApiError(self.errors["get"])
        n = sum(1 for c in self.calls if c[0] == "getUploadURLExternal")
        return {"ok": True, "upload_url": f"https://files.example/u{n}", "file_id": f"F{n}"}

    def files_completeUploadExternal(self, **kw):
        self.calls.append(("completeUploadExternal", kw))
        if "complete" in self.errors:
            raise self.ApiError(self.errors["complete"])
        return {"ok": True}


class WebSlackUploadTest(TempCase):
    def setUp(self):
        super().setUp()
        self.client = FakeFilesClient()
        self.posted = []
        self.status = 200

        def upload_bytes(url, data):
            self.posted.append((url, data))
            return self.status

        self.web = WebSlack("unused", client=self.client, upload_bytes=upload_bytes)
        self.imgs = [Image("board.png", PNG, "png"), Image("plan.jpg", JPEG, "jpeg")]

    def test_upload_sequence_shares_all_files_in_one_thread_message(self):
        self.web.upload_images("C0042", self.imgs, thread_ts="91.000")
        self.assertEqual(self.client.calls, [
            ("getUploadURLExternal", {"filename": "board.png", "length": len(PNG)}),
            ("getUploadURLExternal", {"filename": "plan.jpg", "length": len(JPEG)}),
            ("completeUploadExternal", {
                "files": [{"id": "F1", "title": "board.png"}, {"id": "F2", "title": "plan.jpg"}],
                "channel_id": "C0042", "thread_ts": "91.000"}),
        ])
        self.assertEqual(self.posted, [("https://files.example/u1", PNG),
                                       ("https://files.example/u2", JPEG)])

    def test_top_level_upload_sends_no_thread_ts(self):
        self.web.upload_images(DM, self.imgs[:1])
        self.assertNotIn("thread_ts", self.client.calls[-1][1])

    def test_failed_byte_upload_stops_before_complete_and_hides_the_url(self):
        self.status = 500
        with self.assertRaises(RuntimeError) as err:
            self.web.upload_images(DM, self.imgs)
        self.assertIn("board.png", str(err.exception))
        self.assertNotIn("files.example", str(err.exception))
        self.assertEqual([c[0] for c in self.client.calls], ["getUploadURLExternal"])

    def test_api_errors_keep_only_the_code(self):
        self.client.errors["get"] = "missing_scope"
        with self.assertRaises(SlackCallError) as err:
            self.web.upload_images(DM, self.imgs)
        self.assertEqual(err.exception.code, "missing_scope")
        self.assertNotIn("secret", str(err.exception))


class SayImagesTest(TempCase):
    def setUp(self):
        super().setUp()
        self.imgs = [Image("board.png", PNG, "png")]

    def say(self, body, **kw):
        return say(self.slack, self.state, lambda: DM, body, images=self.imgs, **kw)

    def test_dm_post_puts_images_in_the_first_messages_thread(self):
        n, where = self.say("board")
        self.assertEqual((n, where), (1, "DM with 1 image(s)"))
        self.assertEqual(self.slack.uploads, [{"channel": DM, "thread_ts": "91.000",
                                               "names": ["board.png"]}])

    def test_new_and_existing_topic_threads_get_the_images(self):
        self.say("started", topic=KEY)
        self.say("board", topic=KEY)
        self.assertEqual([u["thread_ts"] for u in self.slack.uploads], ["91.000", "91.000"])
        self.assertEqual(self.slack.posts[1]["thread_ts"], "91.000")

    def test_note_thread_and_project_channel(self):
        self.state.remember_thread("n0007", "C0042", "1700.000100")
        self.say("reply", thread="n0007")
        with self.state.update() as st:
            st["projects"]["example-project"] = {"channel": "C0099", "name": "example-project"}
        self.say("board", topic=KEY, project="example-project")
        self.assertEqual([(u["channel"], u["thread_ts"]) for u in self.slack.uploads],
                         [("C0042", "1700.000100"), ("C0099", "92.000")])

    def test_upload_failure_after_text_names_what_was_posted(self):
        def boom(*a, **k):
            raise SlackCallError("files.completeUploadExternal", "not_in_channel")

        self.slack.upload_images = boom
        with self.assertRaises(OutboundError) as err:
            self.say("board", topic=KEY)
        self.assertIn("posted 1 message(s)", str(err.exception))
        self.assertIn("not_in_channel", str(err.exception))
        self.assertIn(KEY, self.state.read()["topics"])  # the text's thread stays linked


class LoadImageTest(TempCase):
    def write(self, name, data):
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_png_and_jpeg_by_content(self):
        self.assertEqual(images.load(self.write("a.png", PNG)).kind, "png")
        self.assertEqual(images.load(self.write("b.jpeg", JPEG)).kind, "jpeg")
        self.assertEqual(images.load(self.write("no-ext", PNG)).name, "no-ext")

    def test_refuses_non_images_empty_missing_oversize_and_too_many(self):
        for p in (self.write("fake.png", b"<html>not a png</html>"),
                  self.write("x.gif", b"GIF89a" + b"\0" * 10),
                  self.write("empty.png", b""),
                  self.tmp / "missing.png"):
            with self.assertRaises(ImageError, msg=p.name):
                images.load(p)
        big = self.write("big.png", PNG + b"\0" * images.MAX_IMAGE_BYTES)
        with self.assertRaises(ImageError) as err:
            images.load(big)
        self.assertIn("limit", str(err.exception))
        ok = str(self.write("ok.png", PNG))
        with self.assertRaises(ImageError):
            images.load_all([ok] * (images.MAX_IMAGES + 1))
        self.assertEqual(len(images.load_all([ok] * images.MAX_IMAGES)), images.MAX_IMAGES)


class CliCase(TempCase):
    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        before = list(root.handlers)
        secrets = Secrets(bot_token="xoxb-fake", app_token=None, captain_id=CAPTAIN)
        self.slack.dm_channel = lambda captain, state: DM
        environ = {"SLACK_BRIDGE_LOG_FILE": str(self.tmp / "cli.log"), **(env or {})}
        with mock.patch.dict(os.environ, environ), \
                mock.patch("slack_bridge.config.load_secrets", return_value=secrets), \
                mock.patch("slack_bridge.slack_api.WebSlack", return_value=self.slack), \
                redirect_stdout(out), redirect_stderr(err):
            rc = cli.main([*argv, "--state-dir", str(self.state.dir)])
        for h in root.handlers[len(before):]:
            root.removeHandler(h)
            h.close()
        return rc, out.getvalue(), err.getvalue()


class SayImageCliTest(CliCase):
    def test_repeatable_image_flag(self):
        a, b = self.tmp / "a.png", self.tmp / "b.jpg"
        a.write_bytes(PNG)
        b.write_bytes(JPEG)
        rc, out, err = self.run_cli("say", "--topic", KEY, "--image", str(a), "--image", str(b),
                                    "board ready")
        self.assertEqual(rc, 0, err)
        self.assertIn("with 2 image(s)", out)
        self.assertEqual(self.slack.uploads[0]["names"], ["a.png", "b.jpg"])
        self.assertEqual(self.slack.uploads[0]["thread_ts"], "91.000")

    def test_bad_image_is_refused_before_anything_posts(self):
        bad = self.tmp / "notes.png"
        bad.write_text("plain text")
        rc, _, err = self.run_cli("say", "--image", str(bad), "hello")
        self.assertEqual(rc, 1)
        self.assertIn("not a PNG or JPEG", err)
        self.assertEqual((self.slack.posts, self.slack.uploads), ([], []))


class ShotTest(TempCase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"FAKE_BROWSER_DIR": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.page = self.tmp / "board.html"
        self.page.write_text("<h1>board</h1>")
        self.fake = str(FAKES / "browser")

    def calls(self):
        log = self.tmp / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_html_file_becomes_a_file_url_and_urls_pass_through(self):
        self.assertEqual(page_url(str(self.page)), self.page.resolve().as_uri())
        self.assertEqual(page_url("http://127.0.0.1:4387/session/x"),
                         "http://127.0.0.1:4387/session/x")
        for bad in ("javascript:alert(1)", "ftp://host/x", str(self.tmp / "missing.html")):
            with self.assertRaises(ShotError, msg=bad):
                page_url(bad)

    def test_playwright_full_page_with_installed_chrome(self):
        out = self.tmp / "out" / "board.png"
        path = render(str(self.page), out, width=1024, playwright=self.fake)
        self.assertEqual(path, out.resolve())
        argv = self.calls()[0]
        self.assertEqual(argv[1:6], ["screenshot", "--channel", "chrome", "--viewport-size",
                                     "1024,800"])
        self.assertIn("--full-page", argv)
        self.assertEqual(argv[-2:], [self.page.resolve().as_uri(), str(out.resolve())])

    def test_viewport_only_to_a_temp_file(self):
        path = render(str(self.page), None, playwright=self.fake, full_page=False)
        self.addCleanup(path.unlink)
        self.assertTrue(path.name.startswith("slack-bridge-shot-"))
        self.assertNotIn("--full-page", self.calls()[0])

    def test_playwright_failure_is_an_error_not_a_lesser_capture(self):
        with mock.patch.dict(os.environ, {"FAKE_BROWSER_FAIL": "1"}):
            with self.assertRaises(ShotError) as err:
                render(str(self.page), self.tmp / "x.png", playwright=self.fake)
        self.assertIn("playwright screenshot failed (exit 1)", str(err.exception))
        self.assertEqual(len(self.calls()), 1)
        with mock.patch("slack_bridge.shot.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("playwright", 90)):
            with self.assertRaises(ShotError) as err:
                render(str(self.page), playwright=self.fake)
        self.assertIn("timed out", str(err.exception))
        with mock.patch("slack_bridge.shot.subprocess.run", side_effect=PermissionError):
            with self.assertRaises(ShotError) as err:
                render(str(self.page), playwright=self.fake)
        self.assertIn("PermissionError", str(err.exception))

    def test_refuses_bad_sizes_no_playwright_and_non_png_output(self):
        with self.assertRaises(ShotError):
            render(str(self.page), width=10, playwright=self.fake)
        with self.assertRaises(ShotError):
            render(str(self.page), self.tmp / "x.jpg", playwright=self.fake)
        with self.assertRaises(ShotError) as err:
            render(str(self.page), playwright="no-such-playwright")
        self.assertIn("playwright CLI 'no-such-playwright' not found", str(err.exception))
        with mock.patch.dict(os.environ, {"FAKE_BROWSER_WRITE": "text"}):
            with self.assertRaises(ShotError) as err:
                render(str(self.page), self.tmp / "y.png", playwright=self.fake)
        self.assertIn("no usable PNG", str(err.exception))


class ShotCliTest(CliCase):
    def test_prints_only_the_png_path(self):
        page = self.tmp / "board.html"
        page.write_text("<h1>board</h1>")
        out = self.tmp / "shot.png"
        env = {"FAKE_BROWSER_DIR": str(self.tmp), "SLACK_BRIDGE_PLAYWRIGHT": str(FAKES / "browser")}
        rc, stdout, err = self.run_cli("shot", str(page), "--out", str(out), env=env)
        self.assertEqual((rc, stdout), (0, f"{out.resolve()}\n"), err)
        rc, _, err = self.run_cli("shot", "javascript:alert(1)", env=env)
        self.assertEqual(rc, 1)
        self.assertIn("slack-bridge shot:", err)

    def test_playwright_failure_exits_nonzero_and_names_it(self):
        page = self.tmp / "board.html"
        page.write_text("<h1>board</h1>")
        env = {"FAKE_BROWSER_DIR": str(self.tmp), "FAKE_BROWSER_FAIL": "1",
               "SLACK_BRIDGE_PLAYWRIGHT": str(FAKES / "browser")}
        rc, stdout, err = self.run_cli("shot", str(page), "--out", str(self.tmp / "s.png"),
                                       env=env)
        self.assertEqual((rc, stdout), (1, ""))
        self.assertIn("playwright screenshot failed (exit 1)", err)
        rc, stdout, err = self.run_cli("shot", str(page), env={
            **env, "SLACK_BRIDGE_PLAYWRIGHT": "no-such-playwright"})
        self.assertEqual((rc, stdout), (1, ""))
        self.assertIn("playwright CLI 'no-such-playwright' not found", err)
