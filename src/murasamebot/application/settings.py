from dataclasses import dataclass, field
from murasamebot.application.autonomy import AutonomySettings


@dataclass(frozen=True)
class Behaviour:
    agent_id: str = "murasame"
    owner_person_id: str = "owner"
    platform: str = "bilibili"
    account_id: str = ""
    timezone: str = "Asia/Hong_Kong"
    draft: bool = True
    model_review: bool = False
    review_strategy: str = "inline"
    text_language: str = "zh"
    categories: tuple[str, ...] = ()
    windows: tuple[tuple[str, str], ...] = (("19:00", "21:00"),)
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    browsing_enabled: bool = False
    allow_metadata_video: bool = False
    comment_videos: bool = True
    reply_comments: bool = True
    recommend_owner: bool = True
    video_like: bool = False
    video_coin: bool = False
    dynamic_like: bool = False
    interval_minutes: int = 60
    videos_per_round: int = 2
    candidate_limit: int = 6
    interests: tuple[str, ...] = ()
    owner_interests: tuple[str, ...] = ()
    daily_comments: int = 20
    daily_recommendations: int = 3
    min_send_gap_seconds: int = 10
    recommendation_gap_minutes: int = 120
    action_ttl_minutes: int = 30
    request_expiry_days: int = 7
    max_task_attempts: int = 3
    max_event_attempts: int = 3
    event_retry_seconds: int = 60
    max_comment_characters: int = 250
    max_private_characters: int = 400
    max_recommendation_characters: int = 80
    private_messages_enabled: bool = True
    autonomy: AutonomySettings = field(default_factory=AutonomySettings)
