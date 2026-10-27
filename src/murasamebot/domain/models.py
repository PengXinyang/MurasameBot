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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        data = dict(data)
        if isinstance(data.get("content"), dict):
            data["content"] = ContentRef(**data["content"])
        if isinstance(data.get("share_content"), dict):
            data["share_content"] = ContentRef(**data["share_content"])
        return cls(**data)


@dataclass(frozen=True)
class ImageRef:
    url: str
    index: int


@dataclass(frozen=True)
class ImageInput:
    data_url: str
    sha256: str
    size_bytes: int
    animated: bool = False


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

    def public_context(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Document:
        values = dict(data)
        values["ref"] = ContentRef(**values["ref"])
        values["comments"] = tuple(values.get("comments", ()))
        values["images"] = tuple(ImageRef(**image) for image in values.get("images", ()))
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

    def prompt(self, owner_addressed: bool = False) -> str:
        parts = [self.public_background, self.style, "表达示例：", *self.examples]
        if owner_addressed:
            parts.append(self.owner_style)
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
    context: ContentRef | None = None
    amount: int = 0
    proactive: bool = False
    emojis: tuple[Emoji, ...] = ()
    memory_epoch: int = -1  # recheck derived-memory invalidation immediately before dispatch

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        data = dict(data)
        if data.get("content") is not None:
            data["content"] = ContentRef(**data["content"])
        data["mentions"] = tuple(data.get("mentions", ()))
        data["emojis"] = tuple(Emoji(**item) for item in data.get("emojis", ()))
        if isinstance(data.get("context"), dict):
            data["context"] = ContentRef(**data["context"])
        return cls(**data)


@dataclass(frozen=True)
class Receipt:
    state: str  # accepted, failed, unknown
    external_id: str = ""
    detail: str = ""
    mention_resolved: bool = True
    pause: bool = False


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
    def __init__(self, message: str, *, code: str = "platform_error", pause: bool = False, platform: str = ""):
        super().__init__(message)
        self.code = code
        self.pause = pause
        self.platform = platform


class ModelError(RuntimeError):
    pass


class MediaError(RuntimeError):
    pass


class PolicyError(ValueError):
    pass
