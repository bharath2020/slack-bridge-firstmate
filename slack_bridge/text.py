"""Slack mrkdwn escaping and long-message splitting."""

from __future__ import annotations

import re

# Slack accepts up to 40k characters but renders messages over ~4k poorly and
# truncates blocks; stay well under that so every chunk reads as one message.
DEFAULT_LIMIT = 3500

FENCE = "```"


def escape(text: str) -> str:
    """Escape the three characters Slack treats as control sequences.

    Everything else (``*bold*``, ``_italic_``, code fences) is left alone so
    Firstmate's formatting still renders.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    """Reverse Slack's escaping on inbound message text."""
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


_WS = re.compile(r"\s+")


def normalize_command(text: str) -> str:
    """Lower-case, trim and collapse whitespace; strip trailing punctuation."""
    return _WS.sub(" ", text).strip().rstrip(".!?").strip().lower()


def split(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split text into chunks of at most ``limit`` characters.

    Prefers paragraph, then line, then word boundaries. A chunk that ends
    inside a code fence is closed and the next chunk reopens it, so code
    blocks render on both sides of the cut.
    """
    if limit < 20:
        raise ValueError("limit too small")
    text = text.strip("\n")
    if not text:
        return []
    budget = limit - len(FENCE) - 1  # room to close a fence we cut through
    chunks: list[str] = []
    rest = text
    reopen = False
    while rest:
        if reopen:
            rest = FENCE + "\n" + rest
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = _cut_point(rest, budget)
        head, rest = rest[:cut].rstrip("\n"), rest[cut:].lstrip("\n")
        open_fence = head.count(FENCE) % 2 == 1
        if open_fence:
            head += "\n" + FENCE
        chunks.append(head)
        reopen = open_fence
    return [c for c in chunks if c.strip()]


def _cut_point(text: str, budget: int) -> int:
    window = text[:budget]
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx > budget // 2:
            return idx + len(sep)
    return budget
