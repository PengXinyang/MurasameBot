from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from murasamebot.domain.models import Document, MediaError, ModelError, VisualEvidence, stable_id
from murasamebot.ports import ImageReader, ModelGateway, StateStore


@dataclass(frozen=True)
class VisionSettings:
    enabled: bool = True
    max_images: int = 9
    max_image_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 24 * 1024 * 1024
    cache_minutes: int = 1440
    failure_retry_minutes: int = 10
    detail: str = "auto"


class VisionService:
    """Reusable image evidence acquisition, independent of social platforms."""

    def __init__(self, settings: VisionSettings, reader: ImageReader, model: ModelGateway, store: StateStore, *, model_name: str):
        self.settings, self.reader, self.model, self.store, self.model_name = settings, reader, model, store, model_name

    def enrich(self, document: Document, now: float, previous: Document | None = None) -> Document:
        if not document.images:
            return document
        level = "text" if document.text else "metadata"
        total = len(document.images)
        signature = stable_id(document.ref.key, document.text, [image.url for image in document.images], asdict(self.settings), self.model_name)
        old = previous.visual if previous else None
        if not self.settings.enabled:
            return replace(document, evidence_level=level, visual=VisualEvidence("disabled", total_images=total))
        if old and old.source_signature == signature and now < old.created_at + self.settings.cache_minutes * 60:
            if old.status == "complete" or now < old.retry_after:
                return replace(document, visual=old, evidence_level=("text_visual" if document.text else "visual") if old.indices else level)
        inputs, indices, hashes, limits = [], [], [], []
        remaining = self.settings.max_total_bytes
        for ref in document.images[:self.settings.max_images]:
            if remaining <= 0:
                limits.append("已达到本次图片总大小上限")
                break
            try:
                image = self.reader.load(ref, min(self.settings.max_image_bytes, remaining))
            except MediaError as exc:
                limits.append(f"第{ref.index}张图片读取失败：{exc}")
                continue
            inputs.append({"index": ref.index, "data_url": image.data_url, "detail": self.settings.detail})
            indices.append(ref.index)
            hashes.append(image.sha256)
            remaining -= image.size_bytes
            if image.animated:
                limits.append(f"第{ref.index}张为动态图，不保证理解所有帧")
        if total > self.settings.max_images:
            limits.append(f"共{total}张，本次最多读取前{self.settings.max_images}张")
        summary = ""
        if inputs:
            try:
                result = self.model.decide("describe_images", {"title": document.title, "post_text": document.text,
                    "image_indices": indices, "total_images": total, "_image_inputs": inputs})
                descriptions = result.get("images")
                if (not isinstance(descriptions, list) or len(descriptions) != len(indices) or
                        any(not isinstance(row, dict) or type(row.get("index")) is not int for row in descriptions) or
                        {row["index"] for row in descriptions} != set(indices)):
                    raise ModelError("视觉输出缺少对应的逐图说明")
                if any(not isinstance(row.get("description"), str) or not row["description"].strip() for row in descriptions):
                    raise ModelError("视觉输出缺少逐图描述")
                if not isinstance(result.get("summary"), str) or not result["summary"].strip():
                    raise ModelError("视觉输出缺少概括")
                summary = result["summary"][:1800] + "\n" + "\n".join(f"图片{row['index']}：{row['description'][:600]}" for row in descriptions)
                uncertainty = result.get("uncertainties", [])
                if isinstance(uncertainty, list):
                    limits.extend(str(item)[:200] for item in uncertainty[:9])
            except (ModelError, ValueError, TypeError, KeyError):
                indices, hashes = [], []
                limits.append("视觉分析未完成，不能据此描述图片")
        status = "complete" if len(indices) == total else "partial" if indices else "failed"
        visual = VisualEvidence(status, summary, tuple(indices), total, self.model_name, signature, now,
                                now + self.settings.failure_retry_minutes * 60, tuple(limits), tuple(hashes))
        self.store.audit("vision_result", f"{document.ref.key}: {status}，读取{len(indices)}/{total}张", now)
        return replace(document, visual=visual, evidence_level=("text_visual" if document.text else "visual") if indices else level)
