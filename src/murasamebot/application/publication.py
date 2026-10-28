"""Read-only publication checks, independent of platform write/retry state."""
import time
from typing import Mapping

from murasamebot.domain.models import PlatformError, PolicyError, PublicationCheck
from murasamebot.ports import PublicationProbe, StateStore


class PublicationService:
    def __init__(self, store: StateStore, probes: Mapping[str, PublicationProbe]):
        self.store, self.probes = store, probes

    def check(self, action_id: str) -> PublicationCheck:
        action, external_id = self.store.publication_target(action_id)
        probe = self.probes.get(action.platform)
        if probe is None:
            raise PolicyError("该平台尚未接入发布状态核查")
        lease = "publication_check:" + action_id
        if not self.store.acquire_lease(lease, time.time(), 120):
            raise PolicyError("该条内容正在核查，请稍后刷新")
        try:
            try:
                result = probe.check_publication(action, external_id)
            except PlatformError as exc:
                if exc.pause:
                    self.store.set_setting("platform_paused:" + action.platform, True)
                result = PublicationCheck("unavailable", "查询失败或被平台限制，本次无法判断可见性；未重发", time.time())
            self.store.record_publication_check(action_id, external_id, result)
            return result
        finally:
            self.store.release_lease(lease)
