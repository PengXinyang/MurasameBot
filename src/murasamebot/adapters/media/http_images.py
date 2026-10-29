from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
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
        self._batch = ContextVar("image_batch", default=None)

    @contextmanager
    def batch(self, max_bytes: int):
        # Per-decision and per-thread: never retain sprites or private image bytes
        # in a process-wide cache. Shared sprites count once against source bytes.
        token = self._batch.set({"remaining": max_bytes, "sources": {}})
        try:
            yield
        finally:
            self._batch.reset(token)

    def load(self, ref: ImageRef, max_bytes: int) -> ImageInput:
        try:
            url = urlparse(ref.url)
            allowed = len(ref.url) <= 8192 and url.scheme == "https" and url.hostname in self.hosts and not url.username and url.port in {None, 443}
        except ValueError:
            allowed = False
        if not allowed:
            raise MediaError("image_host_denied")
        if ref.crop and (len(ref.crop) != 4 or any(type(n) is not int for n in ref.crop) or
                         min(ref.crop[:2]) < 0 or min(ref.crop[2:]) < 1):
            raise MediaError("image_crop_invalid")
        try:
            batch = self._batch.get()
            data = batch["sources"].get(ref.url) if batch else None
            if data is None:
                source_limit = min(max_bytes, batch["remaining"]) if batch else max_bytes
                if source_limit <= 0:
                    raise MediaError("image_batch_too_large")
                self.client.cookies.clear()
                with self.client.stream("GET", ref.url, headers={"Referer": "https://www.bilibili.com/"}, follow_redirects=False) as response:
                    if response.status_code != 200:
                        raise MediaError(f"image_http_{response.status_code}")
                    if int(response.headers.get("content-length") or 0) > source_limit:
                        raise MediaError("image_too_large")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > source_limit:
                            raise MediaError("image_too_large")
                data = bytes(data)
                if batch is not None:
                    batch["remaining"] -= len(data)
                    batch["sources"][ref.url] = data
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    formats = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}
                    if image.format not in formats or max(image.size) > 8192 or image.width * image.height > 40_000_000:
                        raise MediaError("image_format_or_dimensions_denied")
                    mime = formats[image.format]
                    animated = getattr(image, "n_frames", 1) > 1
                    width, height = image.size
                    image.verify()
                if ref.crop:
                    x, y, width, height = ref.crop
                    with Image.open(BytesIO(data)) as image:
                        if x + width > image.width or y + height > image.height:
                            raise MediaError("image_crop_out_of_bounds")
                        output = BytesIO()
                        image.crop((x, y, x + width, y + height)).convert("RGB").save(output, format="JPEG", quality=90)
                        data, mime, animated = output.getvalue(), "jpeg", False
            if len(data) > max_bytes:
                raise MediaError("image_too_large")
            return ImageInput("data:image/" + mime + ";base64," + base64.b64encode(data).decode("ascii"),
                              sha256(data).hexdigest(), len(data), animated, width, height)
        except (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise MediaError("image_download_or_decode_failed") from None

    def close(self):
        self.client.close()
