from dataclasses import dataclass


@dataclass(frozen=True)
class RequestSettings:
    read_gap_seconds: float = 1.0
    read_max_wait_seconds: float = 5.0
    account_cache_seconds: int = 60
    backoff_seconds: int = 60
    backoff_max_seconds: int = 900
    quiet_before_seconds: int = 60
    quiet_after_seconds: int = 15
    log_retention_days: int = 7
    log_max_rows: int = 20000
