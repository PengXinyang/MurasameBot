"""Platform composition boundaries. Import concrete protocols only when enabled."""
from dataclasses import dataclass, replace

from murasamebot.domain.models import stable_id


@dataclass
class PlatformModule:
    name: str
    adapter: object
    capabilities: frozenset[str] = frozenset()
    resources: tuple = ()
    history: object = None
    history_reader: object = None
    artwork: object = None
    monitor: object = None
    poll_seconds: int = 15
    private_policy: dict | None = None
    private_content_provider: object = None

    @property
    def account_id(self):
        return str(self.adapter.account_id)

    def poll(self, runtime, now):
        """Optional polling hook; push transports use Runtime.ingest instead."""

    def poll_private(self, runtime, now):
        pass

    def normalize_event(self, event):
        # New connectors namespace protocol IDs before entering shared storage.
        return replace(event, event_id=stable_id("inbound", self.name, event.account_id, event.event_id),
                       conversation_id=stable_id("conversation", self.name, event.account_id, event.conversation_id))

    def background(self, runtime, now):
        pass

    def guard(self, config, action, now):
        return ""

    def status(self, runtime):
        return {"enabled": True, "configured": bool(self.adapter.configured),
                "capabilities": sorted(self.capabilities),
                "paused": runtime.engine.paused(self.name)}


def default_factories():
    # Importing the composition root must not load any platform SDK/client.
    def bilibili(config, store):
        from murasamebot.bootstrap.platforms.bilibili import build_module
        return build_module(config, store)
    def qq(config, store):
        from murasamebot.bootstrap.platforms.qq import build_module
        return build_module(config, store)
    return {"bilibili": bilibili, "qq": qq}


def prepare_platform_state(config, store):
    # Compatibility checkpoint belongs to the Bilibili module, not shared state.
    if "bilibili" in config.enabled_platforms:
        from murasamebot.bootstrap.platforms.bilibili import prepare_state
        prepare_state(config, store)


def build_modules(config, store, factories=None):
    factories = default_factories() if factories is None else dict(factories)
    missing = set(config.enabled_platforms) - factories.keys()
    if missing:
        raise ValueError("启用的平台尚无连接器：" + ", ".join(sorted(missing)))
    modules = {}
    try:
        for name in config.enabled_platforms:
            module = factories[name](config, store)
            modules[name] = module
            if module.name != name:
                raise ValueError("平台工厂名称与注册名称不一致")
        return modules
    except BaseException:
        for module in modules.values():
            close_module(module)
        raise


def module_resources(module):
    resources = [module.adapter, *module.resources]
    if module.artwork:
        resources.extend(module.artwork.sources.values())
    return resources


def close_resources(resources):
    seen = set()
    for resource in resources:
        if resource is not None and id(resource) not in seen:
            seen.add(id(resource))
            resource.close()


def close_module(module):
    close_resources(module_resources(module))
