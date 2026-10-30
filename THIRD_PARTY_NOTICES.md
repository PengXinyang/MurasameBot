# Reference and dependency notices

Prompt-style research (2026-10-31) consulted op7418/Humanizer-zh, blader/humanizer and hardikpandya/stop-slop (MIT), plus LingChat at ef8f79141f724fee1f0c78fc863b63bc2be42126 (AGPL-3.0). No upstream source or full prompt/skill templates were vendored. The project retains its own scoped persona, structured review and delivery protocols; sources, limits and independently written style changes are described in [the research note](docs/PERSONA_PROMPT_RESEARCH.md).

MurasameBot's Python implementation was written for this project. No source files from an existing bot framework were vendored. Platform endpoint information and concepts were checked against these public projects on 2026-10-26:

- https://github.com/chenluQwQ/bilibili-ai-bot — MIT; account-login, notification and proactive-interaction references.
- https://github.com/Janson20/BiliCommentBot — MIT; comment-thread and monitoring references.
- https://github.com/kukemc/bili-cli — MIT; real mention encoding, current nickname resolution and write-result handling references.
- On 2026-10-28, its `comment.py`, `runtime.py` and credential handling were additionally checked at commit `e17ab4da33a5b32a5fb4706e0a6dbb9ed994085a`. Web request/header conventions and bounded comment-status checks were adapted into this project's own synchronous adapter. The CLI and its SDK are not runtime dependencies. The upstream MIT notice is reproduced below for the adapted conventions.
- https://github.com/chenluQwQ/astrbot_plugin_bilibili_ai_bot — AGPL-3.0; architectural and endpoint reference only. No implementation code copied into this project.
- https://github.com/YOYOLnFu/dsh-bili-pond — MIT; DeepSeek Harness Bilibili plugin, metadata/subtitle, memory and result-accounting reference only. No implementation code copied.

The supplied character/settings.yml and original artwork are user-provided assets from the local LingChat installation. They remain unchanged and are not relicensed here. LingChat's local program LICENSE declares AGPL-3.0; this does not establish a separate grant for every character image or fictional character. No LingChat program source was copied into this implementation. Selected supplied character PNGs are displayed unchanged as local dashboard decoration only; they are not used as platform avatars or message attachments.

Additional protocol references checked on 2026-10-26 (no implementation source copied):

- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/historytoview/history.md — rechecked 2026-10-28 for authenticated history/cursor, cursor pagination, view_at, progress and video metadata. The owner read-only client and observation workflow were implemented independently; this is a community protocol reference, not an official supported API contract.

- https://github.com/bilibili-plugins/bilibili-api-collect/blob/master/docs/comment/list.md — rechecked 2026-10-28 for reply/reply pagination and reply/dialog/cursor schemas; bounded parent-chain traversal was implemented independently. The deprecated reply/info endpoint is not used.

- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/message/private_msg.md — private session/history schemas and WBI-signed web text message fields. No implementation code copied.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/user/relation.md — authenticated relationship query and mutual-follow attribute semantics.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/video/action.md — video like and coin request/state fields.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/dynamic/action.md — explicit dynamic like operation (up=1), not toggling state.
- https://api-docs.deepseek.com/guides/vision/ — official multimodal image input format and provider limits. Runtime uses lower configurable image budgets.
- https://api-docs.deepseek.com/quick_start/rate_limit/ — official account-level request concurrency semantics; multiple keys on one account do not increase its concurrency quota. Checked 2026-10-27.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/emoji/list.md — authenticated user emoticon panels and catalog fields; checked against the current account with read-only requests.
- https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/opus/rich_text_nodes.md — native emoji rich-text node type 9. No emoji artwork was copied into this repository.

- https://www.python-httpx.org/advanced/proxies/ — official HTTPX HTTP/HTTPS CONNECT and SOCKS configuration.
- https://www.python-httpx.org/advanced/transports/ — official custom transport interface used for per-request proxy routing; checked 2026-10-28.
- https://github.com/gfwlist/gfwlist — user-selected AutoProxy routing data, fetched from the upstream Base64 text and cached locally rather than vendored. Upstream header declares LGPL-2.1; downloaded rule data retains its original header/license reference and is not relicensed by this project. The parser/transport implementation was independently written. Checked 2026-10-28.
- https://docs.x.com/x-api/posts/search-recent-posts — official X recent search and expansion fields. Requires the operator's own API credentials and access.
- https://github.com/FreeNowOrg/PixivNow/blob/master/docs/pixiv-web-api.md — author's web AJAX endpoint observations; not an official supported Pixiv API contract.
- https://github.com/melon-444/bilibili-API-collect-fork/blob/master/docs/dynamic/publish.md — community observations of dynamic creation and image upload schemas.
- https://greasyfork.org/en/scripts/456890/code — original author's observation of scene=4 and web_repost_src for native dynamic reposts; endpoint fields only, no script source copied.

Selected supplied character PNGs are used only as local dashboard decoration and remain unmodified. Scheduled third-party illustration sharing is a later, separate user-requested feature: artwork licenses are not inherited from source repositories, API access, discovery, or attribution. Image reposts require operator-configured permission and preserve artist/source attribution.

On 2026-10-28 the user additionally supplied two JPEG illustrations and one transparent PNG for dashboard decoration. They are preserved byte-for-byte as `character/avatar/dashboard-smile.jpg`, `dashboard-letter.jpg` and `dashboard-kimono.png`. Layout fitting uses CSS only; embedded image markings remain in the original files. These supplied artworks are not relicensed as program source.

Runtime dependencies retain their own licenses: HTTPX with socksio, PyYAML, tzdata, Pillow, and optional qrcode. pytest is a development dependency. This project does not depend on the archived bilibili-api-python package. The existence of an endpoint implementation or third-party license does not establish platform authorization.

Memory design reference checked on 2026-10-27:

- https://github.com/chenkezhen480/dsh-world — root MIT, examined at commit `402a211ea92cd675d8775ec1346f12d490b4fa34` (README version 0.5.3). Architectural reference for durable archive/checkpoints, recent history, context compaction, daily reflection and versioned self-description. The Python implementation here was written independently; no TypeScript implementation source, templates or runtime dependencies were copied. The repository's Windows WeChat native engine has a separate noncommercial license and is not included or used.


Autonomous initiative references checked on 2026-10-28: `chenkezhen480/dsh-world` (README 0.6.0 and life alarm/notification modules), `YOYOLnFu/dsh-bili-pond` (bounded cadence, fixed-recipient messaging and result accounting), and `kdcadmin/dsh_douyin_tools` (public README describing video-creation/publication workflows). These were architectural references only. No DSH runtime, TypeScript implementation, browser automation engine or native messaging bridge was copied or installed; this release implements its own Python text-post and owner-message service.

## bili-cli MIT notice

Copyright (c) 2026 bili-cli contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


Video-preview design references checked on 2026-10-29: `YZz-S/dsh-bili-summary` at `81b7a0e2991e311b74de22b5414dcdbdac5648aa` (videoshot sprites, crops and fallback), `realysy/bili-apis/docs/video/snapshot.md` (community endpoint/time-index fields), and `Alterssss/dsh-bili-whale` at `8b0009f8ce2ebdc421afcb2e727b19ac18912ec6` (temporal coverage). The Python/Pillow sampler and crop pipeline were independently implemented; no upstream implementation source, Node/sharp/DSH runtime, FFmpeg binary or video downloader is copied or required. See `docs/VIDEO_VISION.md`.
