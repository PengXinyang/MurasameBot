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
    video_frames_enabled: bool = True
    video_frame_count: int = 6
    video_frame_max_parts: int = 3


class VisionService:
    """Reusable image evidence acquisition, independent of social platforms."""

    def __init__(self, settings: VisionSettings, reader: ImageReader, model: ModelGateway, store: StateStore, *, model_name: str):
        self.settings, self.reader, self.model, self.store, self.model_name = settings, reader, model, store, model_name

    def needs_refresh(self, document: Document, now: float) -> bool:
        if (document.ref.kind != "video" or document.topic_class == "sensitive" or
                not self.settings.enabled or not self.settings.video_frames_enabled):
            return False
        frames = document.frame_context
        return (frames is None or frames.status == "disabled" or
                frames.requested_frames != min(self.settings.video_frame_count, self.settings.max_images) or
                frames.max_parts != self.settings.video_frame_max_parts or
                now >= frames.created_at + self.settings.cache_minutes * 60 or
                (frames.status != "complete" and now >= frames.retry_after))

    @staticmethod
    def _base_level(document: Document) -> str:
        if document.evidence_level.startswith("transcript"):
            return "transcript"
        if document.ref.kind == "video":
            return "metadata"
        return "text" if document.text else "metadata"

    def enrich(self, document: Document, now: float, previous: Document | None = None) -> Document:
        level = self._base_level(document)
        is_frames = document.ref.kind == "video" and document.frame_context is not None
        if is_frames and (not self.settings.enabled or not self.settings.video_frames_enabled):
            return replace(document, images=(), evidence_level=level, visual=VisualEvidence("disabled"),
                           frame_context=replace(document.frame_context, status="disabled"))
        if not document.images:
            if is_frames:
                if not previous or previous.frame_context != document.frame_context:
                    self.store.audit("video_frames_unavailable", document.ref.key + "：未取得预览画面，保留文字分析；" +
                                     "；".join(document.frame_context.limitations)[:600], now)
                return replace(document, evidence_level=level, visual=VisualEvidence("unavailable",
                               limitations=document.frame_context.limitations))
            return document
        total = len(document.images)
        visual_level = "transcript_visual" if level == "transcript" else "text_visual" if document.text else "visual"
        signature = stable_id(document.ref.key, document.title, document.text, level,
                              [asdict(image) for image in document.images],
                              (document.frame_context.status, document.frame_context.total_parts,
                               document.frame_context.sampled_parts, document.frame_context.limitations) if is_frames else None,
                              asdict(self.settings), self.model_name)
        old = previous.visual if previous else None
        if not self.settings.enabled:
            return replace(document, evidence_level=level, visual=VisualEvidence("disabled", total_images=total))
        if old and old.source_signature == signature and now < old.created_at + self.settings.cache_minutes * 60:
            if old.status == "complete" or now < old.retry_after:
                return replace(document, visual=old, evidence_level=visual_level if old.indices else level)
        inputs, indices, hashes, limits = [], [], [], []
        if is_frames:
            limits.extend(document.frame_context.limitations)
        remaining = self.settings.max_total_bytes
        samples = []
        with self.reader.batch(self.settings.max_total_bytes):
            for ref in document.images[:self.settings.max_images]:
                if remaining <= 0:
                    limits.append("已达到本次图片总大小上限")
                    break
                try:
                    image = self.reader.load(ref, min(self.settings.max_image_bytes, remaining))
                except MediaError as exc:
                    limits.append(f"第{ref.index}张图片读取失败：{exc}")
                    continue
                label = f"第{ref.index}张图片"
                if is_frames:
                    label = f"视频分P{ref.part}，{ref.timestamp_seconds}秒，预览帧{ref.index}，{image.width}×{image.height}"
                    samples.append({"index": ref.index, "part": ref.part, "timestamp_seconds": ref.timestamp_seconds,
                                    "width": image.width, "height": image.height})
                inputs.append({"index": ref.index, "data_url": image.data_url, "detail": self.settings.detail, "label": label})
                indices.append(ref.index)
                hashes.append(image.sha256)
                remaining -= image.size_bytes
                if image.animated:
                    limits.append(f"第{ref.index}张为动态图，不保证理解所有帧")
        if total > self.settings.max_images:
            limits.append(f"共{total}张，本次最多读取前{self.settings.max_images}张")
        downloaded = len(inputs)
        summary, failure_stage = "", ""
        if inputs:
            failure_stage = "模型调用或JSON解析"
            try:
                result = self.model.decide("describe_images", {"title": document.title, "post_text": document.text,
                    "image_indices": indices, "total_images": total, "_image_inputs": inputs,
                    "media_kind": "video_frames" if is_frames else "images", "frame_samples": samples,
                    "coverage": list(document.frame_context.limitations) if is_frames else []})
                failure_stage = "视觉结果字段校验"
                descriptions = result.get("images")
                if (not isinstance(descriptions, list) or len(descriptions) != len(indices) or
                        any(not isinstance(row, dict) or type(row.get("index")) is not int for row in descriptions) or
                        {row["index"] for row in descriptions} != set(indices)):
                    raise ModelError("视觉输出缺少对应的逐图说明")
                if any(not isinstance(row.get("description"), str) or not row["description"].strip() for row in descriptions):
                    raise ModelError("视觉输出缺少逐图描述")
                if not isinstance(result.get("summary"), str) or not result["summary"].strip():
                    raise ModelError("视觉输出缺少概括")
                labels = {row["index"]: row["label"] for row in inputs}
                summary = result["summary"][:1800] + "\n" + "\n".join(f"{labels[row['index']]}：{row['description'][:600]}" for row in descriptions)
                uncertainty = result.get("uncertainties", [])
                if isinstance(uncertainty, list):
                    limits.extend(str(item)[:200] for item in uncertainty[:9])
                failure_stage = ""
            except (ModelError, ValueError, TypeError, KeyError):
                summary = ""
                indices, hashes = [], []
                limits.append(f"图片已下载{downloaded}/{total}张，但{failure_stage}失败；视觉分析未完成，不能据此描述图片")
        elif not inputs:
            failure_stage = "图片下载"
        status = "complete" if len(indices) == total else "partial" if indices else "failed"
        visual = VisualEvidence(status, summary, tuple(indices), total, self.model_name, signature, now,
                                now + self.settings.failure_retry_minutes * 60, tuple(limits), tuple(hashes))
        detail = "；" + "、".join(f"P{s['part']}@{s['timestamp_seconds']}秒 {s['width']}×{s['height']}"
                                 for s in samples if s["index"] in indices) if is_frames and indices else ""
        if is_frames and document.frame_context.status != "complete":
            detail += "；快照覆盖：" + document.frame_context.status + "；" + "；".join(document.frame_context.limitations)[:500]
        stage = f"；失败阶段：{failure_stage}" if failure_stage else ""
        self.store.audit("vision_result", f"{document.ref.key}: {status}，图片下载{downloaded}/{total}张；有效识别{len(indices)}/{total}张" + stage + detail, now)
        return replace(document, visual=visual, evidence_level=visual_level if indices else level)
