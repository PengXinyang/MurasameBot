from dataclasses import dataclass


@dataclass(frozen=True)
class RequestSettings:
    read_gap_seconds: float = 3.0
    read_max_wait_seconds: float = 10.0
    account_cache_seconds: int = 180
    backoff_seconds: int = 300
    backoff_max_seconds: int = 1800
    quiet_before_seconds: int = 120
    quiet_after_seconds: int = 60
    log_retention_days: int = 7
    log_max_rows: int = 20000
