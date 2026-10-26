"""Render a page to a PNG on this Mac, for ``say --image``.

Slack never renders HTML, so a board or report goes to the captain as a
screenshot plus its link. Uses what is already installed: the Playwright CLI
driving the installed Google Chrome (``--channel chrome``). Nothing is
downloaded, and a Playwright failure is an error, never a lesser capture.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import images

TIMEOUT_SECONDS = 90
WIDTH_RANGE = (320, 3840)
HEIGHT_RANGE = (200, 4000)
_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")


class ShotError(RuntimeError):
    pass


def page_url(target: str) -> str:
    """``target`` as a URL: http(s)/file URLs pass through, a file path becomes file://."""
    m = _SCHEME.match(target)
    if m and m.group(1).lower() in ("http", "https", "file"):
        return target
    path = Path(target).expanduser()
    if path.is_file():
        return path.resolve().as_uri()
    if m and "://" in target:
        raise ShotError(f"unsupported URL scheme {m.group(1)!r}: use http, https or file")
    raise ShotError(f"{target!r}: no such file (pass an http(s) URL or an HTML file)")


def _playwright_cmd(bin_, url, out, width, height, full_page):
    cmd = [bin_, "screenshot", "--channel", "chrome", "--viewport-size", f"{width},{height}",
           "--wait-for-timeout", "500"]
    if full_page:
        cmd.append("--full-page")
    return cmd + [url, str(out)]


def _run(cmd) -> str | None:
    """Run ``cmd``; None on success, else a short reason."""
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return f"timed out after {TIMEOUT_SECONDS}s"
    except OSError as exc:
        return exc.__class__.__name__
    return None if proc.returncode == 0 else f"exit {proc.returncode}"


def render(target: str, out: str | Path | None = None, width: int = 1280, height: int = 800,
           full_page: bool = True, playwright: str = "playwright") -> Path:
    """Screenshot ``target`` to ``out`` (a temp file if None); returns its path."""
    for name, value, (lo, hi) in (("width", width, WIDTH_RANGE), ("height", height, HEIGHT_RANGE)):
        if not lo <= value <= hi:
            raise ShotError(f"--{name} {value} is out of range {lo}-{hi}")
    url = page_url(target)
    if out is not None:
        out = Path(out).expanduser().resolve()
        if out.suffix.lower() != ".png":
            raise ShotError(f"--out {str(out)!r} must end in .png")
        out.parent.mkdir(parents=True, exist_ok=True)
        return _render(url, out, width, height, full_page, playwright)
    fd, tmp = tempfile.mkstemp(prefix="slack-bridge-shot-", suffix=".png")
    os.close(fd)
    try:
        return _render(url, Path(tmp), width, height, full_page, playwright)
    except ShotError:
        Path(tmp).unlink(missing_ok=True)
        raise


def _render(url, out, width, height, full_page, playwright):
    pw = shutil.which(playwright)
    if not pw:
        raise ShotError(f"playwright CLI {playwright!r} not found: put it on PATH or "
                        "set SLACK_BRIDGE_PLAYWRIGHT")
    why = _run(_playwright_cmd(pw, url, out, width, height, full_page))
    if why is not None:
        raise ShotError(f"playwright screenshot failed ({why})")
    return _checked(out)


def _checked(out: Path) -> Path:
    try:
        image = images.load(out)
    except images.ImageError as exc:
        if out.is_file() and out.stat().st_size > images.MAX_IMAGE_BYTES:
            raise ShotError(f"{exc}; try --viewport-only or a smaller --width") from None
        raise ShotError(f"browser wrote no usable PNG: {exc}") from None
    if image.kind != "png":
        raise ShotError(f"browser wrote {image.kind}, not PNG, to {str(out)!r}")
    return out
