"""Images ``say --image`` attaches: PNG or JPEG files, checked before posting.

The check reads the file's leading bytes, not its name, so a renamed text
file is refused like any other non-image.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .outbound import OutboundError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 10

_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"))


class ImageError(OutboundError):
    pass


@dataclass(frozen=True)
class Image:
    name: str
    data: bytes
    kind: str  # "png" or "jpeg"

    def __repr__(self) -> str:
        return f"Image({self.name!r}, {self.kind}, {len(self.data)} bytes)"


def load(path: str | Path) -> Image:
    """Read ``path`` as an attachable image, or raise ImageError."""
    p = Path(path)
    if not p.is_file():
        raise ImageError(f"image {str(p)!r}: no such file")
    size = p.stat().st_size
    if size == 0:
        raise ImageError(f"image {str(p)!r} is empty")
    if size > MAX_IMAGE_BYTES:
        raise ImageError(f"image {str(p)!r} is {size / 1024 / 1024:.1f} MB; "
                         f"the limit is {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    data = p.read_bytes()
    for magic, kind in _MAGIC:
        if data.startswith(magic):
            return Image(p.name, data, kind)
    raise ImageError(f"{str(p)!r} is not a PNG or JPEG image")


def load_all(paths: list[str] | None) -> list[Image]:
    paths = paths or []
    if len(paths) > MAX_IMAGES:
        raise ImageError(f"{len(paths)} images; attach at most {MAX_IMAGES} per message")
    return [load(p) for p in paths]
