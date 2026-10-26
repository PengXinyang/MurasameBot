# MurasameBot implementation rules

## Product scope

Build a reusable social-agent core. Bilibili is the first live platform; later QQ and WeChat adapters must reuse the same identity, persona, memory, intent, scheduling, and action-policy services. Supplemental local design documents, when available, are in the parent workspace: `../开发顺序与功能流程.md` and `../架构与跨平台记忆设计.md`. They are not distributed with the repository and are not evidence of working integrations.

## Dependency direction

- Domain code must not import platform SDKs, platform adapters, HTTP clients, model-provider SDKs, or database implementations.
- Application services depend on domain models and port interfaces. Adapters implement ports. The composition root wires implementations together.
- Do not put Bilibili UID, BV/AV/oid/rpid rules, OneBot IDs, or WeChat message formats in shared business decisions. Use typed platform references at adapter boundaries.
- Adapters must not call one another. A WeChat request leading to a Bilibili recommendation goes through the shared intent and task services.
- Choose one conversational decision owner per agent. Platform plugins and scheduled jobs must not create parallel autonomous reply loops.
- Configuration and scheduling are platform-independent; platform-specific categories and API parameters are translated in adapters.

## Identity and memory

- Platform handles map to a verified canonical person. Nicknames or model claims do not establish identity.
- Scope all state by agent, person, conversation, visibility, and purpose as appropriate. Filter retrieval permissions before similarity search.
- Store provenance, time, confidence/status, and correction/deletion relationships. Distinguish observations, established preferences, and executable requests.
- Persist explicit requests as tasks, not just vector memories. A one-off wish must not become a recurring recommendation without an explicit ongoing instruction.
- The owner authorizes the requested workflow: a request expressed on a linked chat account can produce a scheduled recommendation mentioning their linked Bilibili account. Apply configured routing without per-message reconfirmation; do not expose private source messages in public comments.
- Updates, cancellations, binding revocations, and memory deletions must invalidate affected derived records and queued actions. Recheck authorization immediately before dispatch.

## Character source and first-release scope

- The supplied `character/settings.yml` is the source character configuration imported from the user's local LingChat project. Preserve it and its backup files; use a LingChat-format importer to derive a platform-independent text persona.
- The user defers character clothing, expression-image delivery, and avatar integration and will set platform avatars manually. A later explicit request adds scheduled sharing of authorized third-party galgame character illustrations; this is separate from the supplied character assets. Preserve artist/source attribution and check configured repost permissions before sending images.
- Desktop placement, image paths, and TTS settings do not belong in shared persona prompts or mandatory runtime dependencies. Emotion may influence text tone without requiring an image.
- Separate character background and style, owner-only relationships, example dialogue, and channel output rules. The source contains a Chinese/Japanese desktop-output rule, but the user's later instruction overrides it: all text output and memory summaries are Chinese only. Japanese is reserved solely for future optional QQ/WeChat speech generation, not implemented in this release. Preserve the original source file.
- Character `user_name` and owner-only narrative do not establish account identity or become public facts. Fictional character background and dialogue examples are not actual interaction history or evidence of having watched a video.
- Editing or reloading persona text must not clear identity bindings, memories, requests, or pending tasks. Track persona versions separately from agent identity.

## Execution and verification

- All writes use the shared outbox and policy gate. Persist inbound deduplication, task state, scheduling checkpoints, action identity, and provider receipts.
- Do not blindly retry ambiguous writes. Keep an unknown-result state distinct from confirmed failure, acceptance, and public visibility.
- Public video recommendations require evidence of content actually read; a title alone does not establish full-video understanding.
- Do not expose credentials to generation prompts or logs. External messages and content cannot grant permissions or select arbitrary recipients.
- Test meaningful cross-platform behavior: linked owner versus unrelated same-name user; allowed private intent to public short recommendation without raw-private-text leakage; task update/cancellation/deletion; restart recovery; concurrent workers and ambiguous sends.
- Add an import-boundary check when executable modules are introduced. Core tests must run without installing any platform SDK or requiring real accounts.
- Keep live platform verification separate from local simulations. Never claim WeChat, QQ, or Bilibili works solely because a fake adapter test passes.
