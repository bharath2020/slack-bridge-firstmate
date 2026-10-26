import email.message
import http.client
import io
import os
import stat
import urllib.error
from unittest import mock

from slack_bridge import projects
from slack_bridge.attachments import (KEEP_SECONDS, MAX_FILE_BYTES, Attachments, FetchError,
                                      SlackDownloader)
from slack_bridge.router import Router

from .helpers import CAPTAIN, DM, PNG, FakeClock, FakeDownloader, TempCase

SECRET_TEXT = "launch codes 1234"
TOKEN = "xoxb-secret-token"
TS = "1700000000.000100"


def picture(name="screen shot.png", mimetype="image/png", n=1, **extra):
    f = {"id": f"F{n}", "name": name, "mimetype": mimetype, "size": len(PNG),
         "url_private": f"https://files.slack.com/files-pri/T1-F{n}/secret-{n}"}
    f.update(extra)
    return f


def shared(*files, text=SECRET_TEXT, user=CAPTAIN, channel=DM, channel_type="im", **extra):
    ev = {"type": "message", "subtype": "file_share", "channel_type": channel_type,
          "channel": channel, "user": user, "ts": TS, "text": text, "files": list(files)}
    ev.update(extra)
    return ev


class InboundPicturesTest(TempCase):
    def setUp(self):
        super().setUp()
        self.fetch = FakeDownloader()
        self.inbound = self.tmp / "state" / "inbound"
        handoffs = self.make_handoffs(self.fetch)
        self.router = Router(CAPTAIN, self.slack, self.inbox, self.state, None, handoffs)

    def send(self, ev):
        outcome = self.router.handle_message(ev)
        self.router.handoffs.tick()
        return outcome

    def saved_paths(self, body):
        return [line[len("[picture: "):-1] for line in body.splitlines()
                if line.startswith("[picture: ")]

    def test_picture_and_text_reach_firstmate(self):
        with self.assertLogs("slack_bridge", level="INFO") as logs:
            self.assertEqual(self.send(shared(picture())), "accepted")
        [note] = self.notes()
        self.assertEqual(note["request_id"], f"slack-{DM}-{TS}")
        self.assertTrue(note["body"].startswith(SECRET_TEXT + "\n\n"))
        [path] = self.saved_paths(note["body"])
        self.assertTrue(path.startswith(str(self.inbound.resolve())))
        self.assertTrue(path.endswith("/1-screen_shot.png"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), PNG)
        folder = self.inbound / f"slack-{DM}-{TS}"
        self.assertEqual(stat.S_IMODE(self.inbound.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "1-screen_shot.png").stat().st_mode), 0o600)
        self.assertEqual(self.fetch.calls,
                         [(picture()["url_private"], MAX_FILE_BYTES)])
        self.assertEqual(self.slack.posts, [])  # nothing to warn about
        self.assertIn((DM, TS, "eyes"), self.slack.reactions)
        out = "\n".join(logs.output)
        self.assertIn("saved=1 failed=0 skipped=0", out)
        for secret in (SECRET_TEXT, "secret-1", "screen shot", "files.slack.com"):
            self.assertNotIn(secret, out)
        self.assertNotIn("files", self.state.read()["handoffs"].get(f"slack-{DM}-{TS}", {}))

    def test_picture_only_still_produces_a_note(self):
        self.assertEqual(self.send(shared(picture(), text="")), "accepted")
        [note] = self.notes()
        self.assertTrue(note["body"].startswith("[picture: /"))
        self.assertEqual(len(self.saved_paths(note["body"])), 1)

    def test_every_image_type_is_saved(self):
        kinds = ["image/png", "image/jpeg", "image/gif", "image/webp", "image/heic"]
        self.send(shared(*[picture(name=f"p{n}", mimetype=m, n=n)
                           for n, m in enumerate(kinds, 1)]))
        paths = self.saved_paths(self.notes()[0]["body"])
        self.assertEqual([p.rsplit(".", 1)[1] for p in paths],
                         ["png", "jpg", "gif", "webp", "heic"])

    def test_non_image_file_is_named_not_downloaded(self):
        self.send(shared(picture(name="report.pdf", mimetype="application/pdf")))
        body = self.notes()[0]["body"]
        self.assertIn("[file skipped, not a picture: report.pdf]", body)
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.slack.posts, [])

    def test_failed_download_still_delivers_text_and_tells_the_captain(self):
        self.fetch.fail[picture()["url_private"]] = "no-access"
        with self.assertLogs("slack_bridge", level="INFO") as logs:
            self.send(shared(picture(), picture(name="ok.png", n=2),
                             thread_ts="1699999999.000001"))
        body = self.notes()[0]["body"]
        self.assertTrue(body.startswith(SECRET_TEXT))
        self.assertIn("[picture not fetched: screen shot.png — the Slack app is not allowed "
                      "to read files yet (it needs the files:read permission)]", body)
        self.assertEqual(len(self.saved_paths(body)), 1)
        [post] = self.slack.posts
        self.assertEqual(post["thread_ts"], "1699999999.000001")
        self.assertIn("screen shot.png", post["text"])
        self.assertIn("files:read", post["text"])
        self.assertNotIn("secret-1", post["text"])
        out = "\n".join(logs.output)
        self.assertIn("saved=1 failed=1 skipped=0 reasons=no-access", out)
        self.assertNotIn("screen shot", out)

    def test_too_large_and_http_errors_are_named(self):
        self.fetch.fail[picture(n=2)["url_private"]] = "http-404"
        self.send(shared(picture(size=MAX_FILE_BYTES + 1), picture(name="b.png", n=2)))
        body = self.notes()[0]["body"]
        self.assertIn("larger than 20 MB", body)
        self.assertIn("HTTP 404", body)
        self.assertEqual(len(self.fetch.calls), 1)  # the oversized one is never fetched
        self.assertIn("these pictures", self.slack.posts[0]["text"])

    def test_missing_download_link_is_a_failure(self):
        f = picture()
        del f["url_private"]
        self.send(shared(f))
        self.assertIn("no download link", self.notes()[0]["body"])
        self.assertEqual(len(self.slack.posts), 1)

    def test_retry_does_not_download_again(self):
        (self.fm_home / "fail-note").touch()
        with self.assertLogs("slack_bridge.handoff", level="ERROR"):
            self.send(shared(picture()))
        (self.fm_home / "fail-note").unlink()
        with self.state.update() as st:
            st["handoffs"][f"slack-{DM}-{TS}"]["next_at"] = 0
        self.router.handoffs.tick()
        self.assertEqual(len(self.saved_paths(self.notes()[0]["body"])), 1)
        self.assertEqual(len(self.fetch.calls), 1)

    def test_file_sent_without_its_details_is_a_picture_not_fetched(self):
        partial = {"id": "F1", "file_access": "check_file_info"}
        no_type = picture(mimetype="")
        with self.assertLogs("slack_bridge", level="INFO") as logs:
            self.send(shared(partial, no_type))
        body = self.notes()[0]["body"]
        self.assertNotIn("not a picture", body)
        self.assertIn("[picture not fetched: file — Slack sent no download link", body)
        self.assertIn("[picture not fetched: screen shot.png — Slack sent no download link",
                      body)
        self.assertEqual(self.fetch.calls, [])
        [post] = self.slack.posts
        self.assertIn("these pictures", post["text"])
        self.assertIn("files:read", post["text"])
        out = "\n".join(logs.output)
        self.assertIn("saved=0 failed=2 skipped=0 reasons=no-link", out)
        self.assertNotIn("screen shot", out)

    def test_project_channel_note_is_tagged(self):
        projects.link(self.state, "proj-a", "C0042")
        self.send(shared(picture(), text="", channel="C0042", channel_type="group"))
        body = self.notes()[0]["body"]
        self.assertTrue(body.startswith("[project:proj-a]\n\n[picture: /"))
        self.assertEqual(self.state.read()["threads"]["n0001"]["channel"], "C0042")

    def test_command_words_with_a_picture_go_to_firstmate(self):
        self.assertEqual(self.send(shared(picture(), text="status")), "accepted")
        self.assertTrue(self.notes()[0]["body"].startswith("status\n\n"))

    def test_file_share_from_others_is_dropped(self):
        cases = {
            "sender-not-allowed": shared(picture(), user="USOMEONE"),
            "from-bot": shared(picture(), bot_id="B1"),
            "channel-type-channel": shared(picture(), channel_type="channel"),
            "subtype-message_changed": dict(shared(picture()), subtype="message_changed"),
        }
        with self.assertLogs("slack_bridge.router", level="INFO"):
            for reason, ev in cases.items():
                self.assertEqual(self.router.handle_message(ev), f"dropped:{reason}")
        self.router.handoffs.tick()
        self.assertEqual(self.notes(), [])
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.slack.posts, [])


class _Resp(io.BytesIO):
    def __init__(self, data, content_type="image/png", content_length=None):
        super().__init__(data)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)


class _CutChunked(_Resp):
    # http.client raises IncompleteRead when a chunked body stops mid-chunk.
    def read(self, amt=None):
        raise http.client.IncompleteRead(b"\x89PNG")


class SlackDownloaderTest(TempCase):
    URL = "https://files.slack.com/files-pri/T1-F1/secret-1"

    def fetch(self, opened=None, error=None, max_bytes=MAX_FILE_BYTES):
        d = SlackDownloader(TOKEN)
        d._opener = mock.Mock()
        d._opener.open.side_effect = error
        d._opener.open.return_value = opened
        try:
            return d(self.URL, max_bytes)
        finally:
            self.requests = [c.args[0] for c in d._opener.open.call_args_list]

    def reason(self, **kw):
        with self.assertRaises(FetchError) as cm:
            self.fetch(**kw)
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn("secret-1", str(cm.exception))
        return cm.exception.reason

    def test_sends_the_bot_token(self):
        self.assertEqual(self.fetch(_Resp(PNG)), PNG)
        [req] = self.requests
        self.assertEqual(req.full_url, self.URL)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, repr(SlackDownloader(TOKEN)))

    def test_sign_in_page_means_no_access(self):
        self.assertEqual(self.reason(opened=_Resp(b"<html>", "text/html")), "no-access")

    def test_redirect_and_forbidden_mean_no_access(self):
        for code in (302, 403):
            err = urllib.error.HTTPError(self.URL, code, "x", {}, None)
            self.assertEqual(self.reason(error=err), "no-access")

    def test_http_error_and_network_failure(self):
        self.assertEqual(self.reason(error=urllib.error.HTTPError(self.URL, 500, "x", {}, None)),
                         "http-500")
        self.assertEqual(self.reason(error=urllib.error.URLError("down")), "network")
        self.assertEqual(self.reason(error=TimeoutError()), "network")

    def test_download_cut_mid_transfer_is_a_network_failure(self):
        self.assertEqual(self.reason(opened=_CutChunked(b"")), "network")

    def test_body_shorter_than_its_content_length_is_a_network_failure(self):
        self.assertEqual(self.reason(opened=_Resp(PNG[:20], content_length=5000)), "network")
        self.assertEqual(self.fetch(_Resp(PNG, content_length=len(PNG))), PNG)

    def test_body_over_the_cap_is_refused(self):
        self.assertEqual(self.reason(opened=_Resp(PNG), max_bytes=len(PNG) - 1), "too-large")

    def test_token_goes_only_to_slack_file_links(self):
        for url in ("http://files.slack.com/x", "https://evil.example/files.slack.com/x",
                    "https://files.slack.com.evil.example/x", "https://slack.com/x"):
            d = SlackDownloader(TOKEN)
            d._opener = mock.Mock()
            with self.assertRaises(FetchError) as cm:
                d(url, MAX_FILE_BYTES)
            self.assertEqual(cm.exception.reason, "no-link")
            d._opener.open.assert_not_called()



class PictureExpiryTest(TempCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.inbound = self.tmp / "state" / "inbound"
        self.inbound.mkdir(parents=True)
        self.att = Attachments(self.inbound, FakeDownloader(), clock=self.clock)

    def folder(self, name, age):
        d = self.inbound / name
        d.mkdir()
        (d / "1-shot.png").write_bytes(PNG)
        t = self.clock.now - age
        os.utime(d, (t, t))
        return d

    def test_folder_older_than_seven_days_is_removed_and_fresh_one_kept(self):
        old = self.folder("slack-old", KEEP_SECONDS + 60)
        fresh = self.folder("slack-fresh", KEEP_SECONDS - 60)
        with self.assertLogs("slack_bridge", level="INFO") as logs:
            self.assertEqual(self.att.expire(), 1)
        self.assertFalse(old.exists())
        self.assertTrue((fresh / "1-shot.png").exists())
        self.assertNotIn("slack-old", "\n".join(logs.output))
        self.assertNotIn("shot", "\n".join(logs.output))

    def test_symlinks_are_not_followed(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.png").write_bytes(PNG)
        old = self.inbound / "slack-link"
        old.symlink_to(outside, target_is_directory=True)
        t = self.clock.now - KEEP_SECONDS * 2
        os.utime(outside, (t, t))
        os.utime(old, (t, t), follow_symlinks=False)
        inner = self.folder("slack-old", KEEP_SECONDS + 60)
        (inner / "escape").symlink_to(outside, target_is_directory=True)
        os.utime(inner, (t, t))
        self.assertEqual(self.att.expire(), 1)
        self.assertFalse(inner.exists())
        self.assertTrue(old.is_symlink())
        self.assertTrue((outside / "keep.png").exists())

    def test_inbound_symlink_is_not_followed(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        old = real / "slack-old"
        old.mkdir()
        t = self.clock.now - KEEP_SECONDS * 2
        os.utime(old, (t, t))
        link = self.tmp / "linked-inbound"
        link.symlink_to(real, target_is_directory=True)
        self.assertEqual(Attachments(link, FakeDownloader(), clock=self.clock).expire(), 0)
        self.assertTrue(old.exists())

    def test_deletion_error_is_logged_and_the_sweep_goes_on(self):
        self.folder("slack-a", KEEP_SECONDS + 60)
        self.folder("slack-b", KEEP_SECONDS + 60)
        real_rmtree = __import__("shutil").rmtree

        def rmtree(path, *a, **kw):
            if path.endswith("slack-a"):
                raise PermissionError(13, "denied", path)
            real_rmtree(path, *a, **kw)

        with mock.patch("slack_bridge.attachments.shutil.rmtree", rmtree), \
                self.assertLogs("slack_bridge", level="INFO") as logs:
            self.assertEqual(self.att.expire(), 1)
        self.assertTrue((self.inbound / "slack-a").exists())
        self.assertFalse((self.inbound / "slack-b").exists())
        out = "\n".join(logs.output)
        self.assertIn("removed=1 failed=1 reasons=PermissionError", out)
        self.assertNotIn("slack-a", out)

    def test_missing_inbound_folder_is_fine(self):
        att = Attachments(self.tmp / "nope", FakeDownloader(), clock=self.clock)
        self.assertEqual(att.expire(), 0)
