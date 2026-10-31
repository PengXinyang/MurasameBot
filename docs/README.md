# 项目记忆索引

索引更新：2026-10-31。各专题的代码核对日期见其正文。这些文档帮助后续开发恢复上下文；机器人运行时的角色与用户记忆不从这里加载。

| 文件 | 用途 |
| --- | --- |
| [STATUS.md](STATUS.md) | 当前已实现、未实现、已知问题、已修复事项及验收边界 |
| [../AGENTS.md](../AGENTS.md) | 每次任务的入口、依赖约束、行为规则、记忆维护义务 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 当前技术栈、目录、核心模块、调用和数据边界 |
| [DECISIONS.md](DECISIONS.md) | 已确定的技术与产品决策、依据、未采用方案 |
| [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) | 已完成、验证程度、已知问题及下一步；开始开发前必读 |
| [OPERATIONS.md](OPERATIONS.md) | 启动、配置、登录、审核、请求控制、排障与验收 |
| [RUNTIME_MEMORY.md](RUNTIME_MEMORY.md) | 用户记忆/委托/观察的区别、持久化、遗忘及尚存缺口 |
| [DASHBOARD_MODULES.md](DASHBOARD_MODULES.md) | QQ/B站平台管理页、任务归属、共同设置及QQ/B站共同记忆/日记/相处回顾 |
| [SOCIAL_STATES.md](SOCIAL_STATES.md) | 带来源社交记录、短期角色状态、公开感受、跟进与管理 |
| [PERSONA_PROMPT_RESEARCH.md](PERSONA_PROMPT_RESEARCH.md) | 去套话skill核验、LingChat提示词/历史链路、本轮普通聊天风格规则及后续对照验收 |
| [VIDEO_VISION.md](VIDEO_VISION.md) | 视频快照配置、裁图/时间映射、视觉证据及降级边界 |
| [VIDEO_UNDERSTANDING_PLAN.md](VIDEO_UNDERSTANDING_PLAN.md) | 视频理解规划（未开发）：分镜总览、高清细看、分析资产、上游分支对比、阶段与验收 |
| [ROADMAP.md](ROADMAP.md) | 跨平台、Docker、语音与视频投稿等尚未实现的范围 |
| [QQ_INTEGRATION.md](QQ_INTEGRATION.md) | QQ功能范围、模块化单体选择、规划基线与Q0～Q4验收；Q0已编码、QQ未接入 |
| [PLATFORM_MODULES.md](PLATFORM_MODULES.md) | 已编码的Q0平台模块、惰性装配、启停、路由/队列保护、兼容入口与后续边界 |
| [PLATFORM_SERVICES.md](PLATFORM_SERVICES.md) | 管理台内QQ/B站独立启停、ReceiverManager、唯一worker与接收服务扩展 |
| [QQ_PRIVATE_CHAT.md](QQ_PRIVATE_CHAT.md) | QQ主人文字/图片私聊、网关安装、签名上报、静默合并、分条回复、共享私人记忆与大肥鱼参考 |
| [QQ_AUTONOMY.md](QQ_AUTONOMY.md) | QQ主人主动文字私聊、上下文择时、独立策略/预算、合并输入与计划取消 |
| [QQ_SPACE.md](QQ_SPACE.md) | 空间自主说说、动态和私聊统一记忆、好友可见发布及验证边界 |
| [QQ_CALLS.md](QQ_CALLS.md) | QQ语音/视频媒体、上游分析、GPT-SoVITS/ASR空配置、设备与验收 |
| [QQ_GROUP_LEARNING.md](QQ_GROUP_LEARNING.md) | 群节奏/wait、来源学习、候选/可用与人工管理 |
| [QQ_GROUP_PARTICIPATION.md](QQ_GROUP_PARTICIPATION.md) | 已编码：有界群窗口、自主接话/旁观、AI选择回答对象与最终失效保护 |
| [QQ_GROUP_CHAT.md](QQ_GROUP_CHAT.md) | 已编码：指定群被动文字接话、对象识别、近期上下文、配置与验收边界 |
| [QQ_GROUP_PLAN.md](QQ_GROUP_PLAN.md) | 后续规划：自主群聊节奏、表达/黑话学习；首期被动接话状态与阶段边界 |
| [QQ_BILIBILI_TASKS.md](QQ_BILIBILI_TASKS.md) | QQ视频委托、B站查找、QQ回传/B站提及、持久路由与取消竞态 |
| [PROVENANCE.md](PROVENANCE.md) | 上一级资料如何提炼、哪些材料留在本机及历史结论的取舍 |
| [VALIDATION.md](VALIDATION.md) | 本次推送对应的验证记录及证据限制 |

阅读顺序：AGENTS → ARCHITECTURE → DECISIONS → DEVELOPMENT_LOG → 任务相关专题 → 代码与测试。

发生冲突时先区分“必须满足的约束”“当前代码行为”“历史快照”。不能默默把缺陷当决策，也不能让旧文档覆盖用户的新要求。新增重要功能后更新日志；改变架构/长期行为后追加决策记录，保留被取代的结论及原因。
