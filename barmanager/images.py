"""Downloading, validating and storing product photos.

All images are re-encoded with Pillow to WebP, which both shrinks them and makes sure
only real raster images end up in the image directory (no SVG/HTML).
"""

import io
import ipaddress
import logging
import socket
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps

log = logging.getLogger("barmanager.images")

Image.MAX_IMAGE_PIXELS = 40_000_000
MAX_SIDE = 1000
MIN_SIDE = 80


class ImageError(Exception):
    pass


def is_public_url(url: str) -> bool:
    """Only http(s) URLs that resolve to public addresses (no access to the LAN)."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except (OSError, ValueError, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return True


def fetch(client: httpx.Client, url: str, max_bytes: int) -> bytes:
    """GET a public URL, following redirects manually so every hop is checked."""
    for _ in range(5):
        if not is_public_url(url):
            raise ImageError("adres niet toegestaan")
        with client.stream("GET", url, follow_redirects=False) as r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("location", ""))
                continue
            if r.status_code != 200:
                raise ImageError(f"HTTP {r.status_code}")
            ctype = r.headers.get("content-type", "").lower()
            if ctype and not (ctype.startswith("image/") or ctype.startswith("application/octet-stream")
                              or ctype.startswith("binary/")):
                raise ImageError(f"geen afbeelding ({ctype})")
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ImageError("afbeelding te groot")
            return bytes(buf)
    raise ImageError("te veel doorverwijzingen")


def store(data: bytes, image_dir: str) -> str:
    """Validate + re-encode image bytes, return the stored filename."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            if min(img.size) < MIN_SIDE:
                raise ImageError("afbeelding te klein")
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            name = uuid.uuid4().hex + ".webp"
            Path(image_dir).mkdir(parents=True, exist_ok=True)
            img.save(Path(image_dir) / name, "WEBP", quality=85, method=4)
            return name
    except ImageError:
        raise
    except Exception as exc:  # Pillow raises many different exception types
        raise ImageError(f"ongeldige afbeelding: {exc}") from exc


def remove(filename: str, image_dir: str) -> None:
    try:
        (Path(image_dir) / Path(filename).name).unlink(missing_ok=True)
    except OSError:
        log.warning("kon afbeelding %s niet verwijderen", filename)
