# 项目记忆索引

索引更新：2026-10-30。各专题的代码核对日期见其正文。这些文档帮助后续开发恢复上下文；机器人运行时的角色与用户记忆不从这里加载。

| 文件 | 用途 |
| --- | --- |
| [STATUS.md](STATUS.md) | 当前已实现、未实现、已知问题、已修复事项及验收边界 |
| [../AGENTS.md](../AGENTS.md) | 每次任务的入口、依赖约束、行为规则、记忆维护义务 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 当前技术栈、目录、核心模块、调用和数据边界 |
| [DECISIONS.md](DECISIONS.md) | 已确定的技术与产品决策、依据、未采用方案 |
| [DEVELOPMENT_LOG.md](DEVELOPMENT_LOG.md) | 已完成、验证程度、已知问题及下一步；开始开发前必读 |
| [OPERATIONS.md](OPERATIONS.md) | 启动、配置、登录、审核、请求控制、排障与验收 |
| [RUNTIME_MEMORY.md](RUNTIME_MEMORY.md) | 用户记忆/委托/观察的区别、持久化、遗忘及尚存缺口 |
| [SOCIAL_STATES.md](SOCIAL_STATES.md) | 带来源社交记录、短期角色状态、公开感受、跟进与管理 |
| [VIDEO_VISION.md](VIDEO_VISION.md) | 视频快照配置、裁图/时间映射、视觉证据及降级边界 |
| [ROADMAP.md](ROADMAP.md) | 跨平台、Docker、语音与视频投稿等尚未实现的范围 |
| [QQ_INTEGRATION.md](QQ_INTEGRATION.md) | QQ功能范围、模块化单体选择、规划基线与Q0～Q4验收；Q0已编码、QQ未接入 |
| [PLATFORM_MODULES.md](PLATFORM_MODULES.md) | 已编码的Q0平台模块、惰性装配、启停、路由/队列保护、兼容入口与后续边界 |
| [QQ_PRIVATE_CHAT.md](QQ_PRIVATE_CHAT.md) | QQ主人文字/图片私聊、网关安装、签名上报、静默合并、分条回复、共享私人记忆与大肥鱼参考 |
| [QQ_BILIBILI_TASKS.md](QQ_BILIBILI_TASKS.md) | QQ视频委托、B站查找、QQ回传/B站提及、持久路由与取消竞态 |
| [PROVENANCE.md](PROVENANCE.md) | 上一级资料如何提炼、哪些材料留在本机及历史结论的取舍 |
| [VALIDATION.md](VALIDATION.md) | 本次推送对应的验证记录及证据限制 |

阅读顺序：AGENTS → ARCHITECTURE → DECISIONS → DEVELOPMENT_LOG → 任务相关专题 → 代码与测试。

发生冲突时先区分“必须满足的约束”“当前代码行为”“历史快照”。不能默默把缺陷当决策，也不能让旧文档覆盖用户的新要求。新增重要功能后更新日志；改变架构/长期行为后追加决策记录，保留被取代的结论及原因。
