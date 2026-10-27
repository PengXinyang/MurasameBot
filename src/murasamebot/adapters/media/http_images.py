from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
from urllib.parse import urlparse
import warnings

import httpx
from PIL import Image

from murasamebot.adapters.network import client as network_client
from murasamebot.domain.models import ImageInput, ImageRef, MediaError


class HTTPImageReader:
    """Cookie-free, bounded image reads; no redirects and no arbitrary hosts."""

    def __init__(self, *, hosts: tuple[str, ...], proxy_url="", client: httpx.Client | None = None):
        self.hosts = frozenset(hosts)
        self.client = client or network_client(proxy_url=proxy_url, timeout=25)

    def load(self, ref: ImageRef, max_bytes: int) -> ImageInput:
        try:
            url = urlparse(ref.url)
            allowed = len(ref.url) <= 8192 and url.scheme == "https" and url.hostname in self.hosts and not url.username and url.port in {None, 443}
        except ValueError:
            allowed = False
        if not allowed:
            raise MediaError("image_host_denied")
        try:
            self.client.cookies.clear()
            with self.client.stream("GET", ref.url, headers={"Referer": "https://www.bilibili.com/"}) as response:
                if response.status_code != 200:
                    raise MediaError(f"image_http_{response.status_code}")
                if int(response.headers.get("content-length") or 0) > max_bytes:
                    raise MediaError("image_too_large")
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise MediaError("image_too_large")
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    formats = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
                    if image.format not in formats or max(image.size) > 8192 or image.width * image.height > 40_000_000:
                        raise MediaError("image_format_or_dimensions_denied")
                    mime = formats[image.format]
                    animated = getattr(image, "n_frames", 1) > 1
                    image.verify()
            return ImageInput("data:image/" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
                              sha256(data).hexdigest(), len(data), animated)
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise MediaError("image_download_or_decode_failed") from None

    def close(self):
        self.client.close()
