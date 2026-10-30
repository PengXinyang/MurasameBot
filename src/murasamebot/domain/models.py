from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any


def stable_id(*parts: object) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ContentRef:
    provider: str
    kind: str
    external_id: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.kind}:{self.external_id}"

    @classmethod
    def parse(cls, key: str) -> ContentRef:
        return cls(*key.split(":", 2))


@dataclass(frozen=True)
class Event:
    event_id: str
    platform: str
    account_id: str
    actor_id: str
    conversation_id: str
    kind: str
    text: str
    occurred_at: float
    visibility: str = "public"
    content: ContentRef | None = None
    reply_to: str = ""
    thread_root: str = ""
    actor_name: str = ""
    share_content: ContentRef | None = None
    sequence: int = 0
    images: tuple[ImageRef, ...] = ()
    reply_limit: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        data = dict(data)
        data["images"] = tuple(ImageRef(**i) if isinstance(i, dict) else i for i in data.get("images", ()))
        if isinstance(data.get("content"), dict):
            data["content"] = ContentRef(**data["content"])
        if isinstance(data.get("share_content"), dict):
            data["share_content"] = ContentRef(**data["share_content"])
        return cls(**data)


@dataclass(frozen=True)
class ImageRef:
    url: str
    index: int
    crop: tuple[int, ...] = ()  # left, top, width, height in the source image
    timestamp_seconds: float | None = None
    part: int = 0  # one-based video part; zero for ordinary images


@dataclass(frozen=True)
class ImageInput:
    data_url: str
    sha256: str
    size_bytes: int
    animated: bool = False
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class FrameContext:
    status: str
    requested_frames: int = 6
    max_parts: int = 3
    total_parts: int = 1
    sampled_parts: tuple[int, ...] = ()
    limitations: tuple[str, ...] = ()
    created_at: float = 0
    retry_after: float = 0
    source: str = "player_preview"


@dataclass(frozen=True)
class VisualEvidence:
    status: str
    summary: str = ""
    indices: tuple[int, ...] = ()
    total_images: int = 0
    model: str = ""
    source_signature: str = ""
    created_at: float = 0
    retry_after: float = 0
    limitations: tuple[str, ...] = ()
    image_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommentContext:
    target_id: str
    root_id: str
    status: str
    missing_ids: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    requests: int = 0


@dataclass(frozen=True)
class Document:
    ref: ContentRef
    title: str
    text: str
    evidence_level: str
    author_id: str = ""
    category_id: str = ""
    url: str = ""
    comments: tuple[dict[str, str], ...] = ()
    images: tuple[ImageRef, ...] = ()
    visual: VisualEvidence | None = None
    parent: ContentRef | None = None
    comment_context: CommentContext | None = None
    topic_class: str = "unknown"
    frame_context: FrameContext | None = None

    def public_context(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        values = dict(data)
        values["ref"] = ContentRef(**values["ref"])
        values["comments"] = tuple(values.get("comments", ()))
        values["images"] = tuple(ImageRef(**{**image, "crop": tuple(image.get("crop", ()))}) for image in values.get("images", ()))
        if isinstance(values.get("frame_context"), dict):
            frames = dict(values["frame_context"])
            for name in ("sampled_parts", "limitations"):
                frames[name] = tuple(frames.get(name, ()))
            values["frame_context"] = FrameContext(**frames)
        if isinstance(values.get("comment_context"), dict):
            coverage = dict(values["comment_context"])
            for name in ("missing_ids", "issues"):
                coverage[name] = tuple(coverage.get(name, ()))
            values["comment_context"] = CommentContext(**coverage)
        if isinstance(values.get("parent"), dict):
            values["parent"] = ContentRef(**values["parent"])
        if isinstance(values.get("visual"), dict):
            visual = dict(values["visual"])
            for field_name in ("indices", "limitations", "image_hashes"):
                visual[field_name] = tuple(visual.get(field_name, ()))
            values["visual"] = VisualEvidence(**visual)
        return cls(**values)


@dataclass(frozen=True)
class Persona:
    name: str
    public_background: str
    style: str
    owner_style: str
    examples: tuple[str, ...]
    version: str
    source_changed: bool = False
    private_owner_context: str = ""

    def prompt(self, owner_addressed: bool = False, *, private: bool = False) -> str:
        parts = [self.public_background, self.style, "表达示例：", *self.examples]
        if owner_addressed:
            parts.append(self.owner_style)
            if private and self.private_owner_context:
                parts.append(self.private_owner_context)
        return "\n".join(parts)


@dataclass(frozen=True)
class Emoji:
    key: str
    token: str
    label: str
    kind: str = "native"


@dataclass(frozen=True)
class Action:
    action_id: str
    agent_id: str
    platform: str
    account_id: str
    content: ContentRef | None
    text: str
    created_at: float
    expires_at: float
    kind: str = "reply"
    reply_to: str = ""
    thread_root: str = ""
    mentions: tuple[str, ...] = ()  # canonical person IDs, resolved only by the sink
    event_id: str = ""
    task_id: str = ""
    task_version: int = 0
    binding_version: int = 0
    evidence_level: str = "metadata"
    conversation_id: str = ""
    decision_reason: str = ""
    dedup_key: str = ""
    approved_manually: bool = False
    subject_person_id: str = ""  # authorization subject; does not imply an @
    artwork: dict[str, Any] = field(default_factory=dict)
    source_author_id: str = ""
    source_kind: str = ""
    recipient_id: str = ""  # trusted inbound sender, never selected by the model
    visibility: str = "public"
    source_sequence: int = 0
    review_state: str = ""
    review_reason: str = ""
    review_method: str = ""  # inline or independent; empty preserves legacy independent approvals
    context: ContentRef | None = None
    amount: int = 0
    proactive: bool = False
    emojis: tuple[Emoji, ...] = ()
    memory_epoch: int = -1  # recheck derived-memory invalidation immediately before dispatch
    not_before: float = 0  # Persisted earliest delivery time; never a sleeping worker.
    autonomy_version: str = ""
    owner_activity_version: str = ""  # Observation grant/policy revision, independent of refreshes.
    owner_activity_sources: tuple[str, ...] = ()  # Referenced observations; never raw history.
    manual_attempts: int = 0
    topic_class: str = "unknown"
    retry_at: float = 0
    social_refs: tuple[dict[str, Any], ...] = ()
    social_followups: tuple[str, ...] = ()
    social_version: str = ""
    social_gap_seconds: int = 7200

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        data = dict(data)
        if data.get("content") is not None:
            data["content"] = ContentRef(**data["content"])
        data["mentions"] = tuple(data.get("mentions", ()))
        data["owner_activity_sources"] = tuple(data.get("owner_activity_sources", ()))
        data["social_refs"] = tuple(data.get("social_refs", ()))
        data["social_followups"] = tuple(data.get("social_followups", ()))
        data["emojis"] = tuple(Emoji(**item) for item in data.get("emojis", ()))
        if isinstance(data.get("context"), dict):
            data["context"] = ContentRef(**data["context"])
        return cls(**data)


@dataclass(frozen=True)
class PublicationCheck:
    status: str
    detail: str
    checked_at: float
    platform_state: int | None = None
    checked_pages: int = 0


@dataclass(frozen=True)
class Receipt:
    state: str  # accepted, failed, unknown
    external_id: str = ""
    detail: str = ""
    mention_resolved: bool = True
    pause: bool = False
    publication_check: PublicationCheck | None = None


@dataclass(frozen=True)
class PrivateInbox:
    events: tuple[Event, ...] = ()
    cursors: dict[str, int] = field(default_factory=dict)
    denied_peers: int = 0


@dataclass(frozen=True)
class Artwork:
    ref: ContentRef
    title: str
    artist_id: str
    artist_name: str
    source_url: str
    image_url: str
    tags: tuple[str, ...] = ()
    description: str = ""
    safe: bool = False
    is_ai: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PlatformError(RuntimeError):
    def __init__(self, message: str, *, code: str = "platform_error", pause: bool = False, platform: str = "", retry_at: float = 0):
        super().__init__(message)
        self.code = code
        self.pause = pause
        self.platform = platform
        self.retry_at = retry_at


class ModelError(RuntimeError):
    pass


class MediaError(RuntimeError):
    pass


class PolicyError(ValueError):
    pass
