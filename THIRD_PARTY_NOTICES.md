# Reference and dependency notices

MurasameBot's Python implementation was written for this project. No source files from an existing bot framework were vendored. Platform endpoint information and concepts were checked against these public projects on 2026-10-26:

- https://github.com/chenluQwQ/bilibili-ai-bot — MIT; account-login, notification and proactive-interaction references.
- https://github.com/Janson20/BiliCommentBot — MIT; comment-thread and monitoring references.
- https://github.com/kukemc/bili-cli — MIT; real mention encoding, current nickname resolution and write-result handling references.
- https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot — AGPL-3.0; architectural and endpoint reference only. No implementation code copied into this project.
- https://github.com/YOYOLnFu/dsh-bili-pond — MIT; DeepSeek Harness Bilibili plugin, metadata/subtitle, memory and result-accounting reference only. No implementation code copied.

The supplied character/settings.yml and original artwork are user-provided assets from the local LingChat installation. They remain unchanged and are not relicensed here. LingChat's local program LICENSE declares AGPL-3.0; this does not establish a separate grant for every character image or fictional character. No LingChat program source was copied into this implementation. Selected supplied character PNGs are displayed unchanged as local dashboard decoration only; they are not used as platform avatars or message attachments.

Additional protocol references checked on 2026-10-26 (no implementation source copied):

- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/message/private_msg.md — private session/history schemas and WBI-signed web text message fields. No implementation code copied.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/user/relation.md — authenticated relationship query and mutual-follow attribute semantics.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/video/action.md — video like and coin request/state fields.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/dynamic/action.md — explicit dynamic like operation (up=1), not toggling state.
- https://api-docs.deepseek.com/guides/vision/ — official multimodal image input format and provider limits. Runtime uses lower configurable image budgets.
- https://api-docs.deepseek.com/quick_start/rate_limit/ — official account-level request concurrency semantics; multiple keys on one account do not increase its concurrency quota. Checked 2026-10-27.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/emoji/list.md — authenticated user emoticon panels and catalog fields; checked against the current account with read-only requests.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/opus/rich_text_nodes.md — native emoji rich-text node type 9. No emoji artwork was copied into this repository.

- https://www.python-httpx.org/advanced/proxies/ — official HTTPX HTTP/HTTPS CONNECT and SOCKS configuration.
- https://docs.x.com/x-api/posts/search-recent-posts — official X recent search and expansion fields. Requires the operator's own API credentials and access.
- https://github.com/FreeNowOrg/PixivNow/blob/master/docs/pixiv-web-api.md — author's web AJAX endpoint observations; not an official supported Pixiv API contract.
- https://github.com/melon-444/bilibili-API-collect-fork/blob/master/docs/dynamic/publish.md — community observations of dynamic creation and image upload schemas.
- https://greasyfork.org/en/scripts/456890/code — original author's observation of scene=4 and web_repost_src for native dynamic reposts; endpoint fields only, no script source copied.

Selected supplied character PNGs are used only as local dashboard decoration and remain unmodified. Scheduled third-party illustration sharing is a later, separate user-requested feature: artwork licenses are not inherited from source repositories, API access, discovery, or attribution. Image reposts require operator-configured permission and preserve artist/source attribution.

Runtime dependencies retain their own licenses: HTTPX with socksio, PyYAML, tzdata, Pillow, and optional qrcode. pytest is a development dependency. This project does not depend on the archived bilibili-api-python package. The existence of an endpoint implementation or third-party license does not establish platform authorization.

Memory design reference checked on 2026-10-27:

- https://github.com/chenkezhen480/dsh-world — root MIT, examined at commit `402a211ea92cd675d8775ec1346f12d490b4fa34` (README version 0.5.3). Architectural reference for durable archive/checkpoints, recent history, context compaction, daily reflection and versioned self-description. The Python implementation here was written independently; no TypeScript implementation source, templates or runtime dependencies were copied. The repository's Windows WeChat native engine has a separate noncommercial license and is not included or used.
