# Reference and dependency notices

MurasameBot's Python implementation was written for this project. No source files from an existing bot framework were vendored. Platform endpoint information and concepts were checked against these public projects on 2026-10-26:

- https://github.com/chenluQwQ/bilibili-ai-bot — MIT; account-login, notification and proactive-interaction references.
- https://github.com/Janson20/BiliCommentBot — MIT; comment-thread and monitoring references.
- https://github.com/kukemc/bili-cli — MIT; real mention encoding, current nickname resolution and write-result handling references.
- https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot — AGPL-3.0; architectural and endpoint reference only. No implementation code copied into this project.

The supplied character/settings.yml and original artwork are user-provided assets from the local LingChat installation. They remain unchanged and are not relicensed here. LingChat's local program LICENSE declares AGPL-3.0; this does not establish a separate grant for every character image or fictional character. No LingChat program source was copied into this implementation. Supplied clothing and character expression artwork are not integrated.

Additional protocol references checked on 2026-10-26 (no implementation source copied):

- https://www.python-httpx.org/advanced/proxies/ — official HTTPX HTTP/HTTPS CONNECT and SOCKS configuration.
- https://docs.x.com/x-api/posts/search-recent-posts — official X recent search and expansion fields. Requires the operator's own API credentials and access.
- https://github.com/FreeNowOrg/PixivNow/blob/master/docs/pixiv-web-api.md — author's web AJAX endpoint observations; not an official supported Pixiv API contract.
- https://github.com/melon-444/bilibili-API-collect-fork/blob/master/docs/dynamic/publish.md — community observations of dynamic creation and image upload schemas.
- https://greasyfork.org/en/scripts/456890/code — original author's observation of scene=4 and web_repost_src for native dynamic reposts; endpoint fields only, no script source copied.

Supplied character expression images remain unused. Scheduled third-party illustration sharing is a later, separate user-requested feature: artwork licenses are not inherited from source repositories, API access, discovery, or attribution. Image reposts require operator-configured permission and preserve artist/source attribution.

Runtime dependencies retain their own licenses: HTTPX with socksio, PyYAML, tzdata, Pillow, and optional qrcode. pytest is a development dependency. This project does not depend on the archived bilibili-api-python package. The existence of an endpoint implementation or third-party license does not establish platform authorization.
