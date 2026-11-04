# DeepSeek API提示词与“破甲”调研

## 2026-11-04：自然对话方案已编码

在358ec61基础上落实派生profile语气与八组原创场景、分用途chat_temperature（默认0.8）和同一私聊真实档案多轮（private_multiturn默认true）。来源、具体范围、验证和生效方式见[丛雨风格实现](CONGYU_DIALOGUE_STYLE.md)。另执行3个合成场景×旧/新公共profile的6次真实API初步对照，固定temperature=0.5、不展开历史；全部可解析，但不构成自然度验收或温度/多轮效果证明。没有移植安全规避插件或伪造模型轨迹，不能声称破甲成功。下文“尚未开发/未修改”均保留为各次调研时的历史状态；原24次分阶段方案未按原设计执行。

核对日期：2026-11-03。代码基准为`1c25860`，本轮仅检索官方文档与上游源码，不调用真实模型、不安装插件、不注入外来提示词。

## 2026-11-04：自然、生动对话的补充方案（尚未开发）

用户本次目标是减轻古风刻板、改善API对话。核对业务基准`2dd8287`。以下区分代码事实、来源陈述和待验证建议；本轮仅调研，不改运行时人设、参数或消息组织，不调用真实模型。

### 新核对的一手来源

- [dsh-jailbreak README](https://github.com/xqpsec/dsh-jailbreak/blob/main/README.md)，blob `a2070464a4a7e05b75d2184fd080b52c43f0e44d`：当前文档含不同模型/策略的拒答评测，旧DeepSeek结果仍单独列为历史。它未证明能改善丛雨普通聊天自然度；覆盖率、多次尝试成功率和单次对话质量不能混用。本轮未安装/移植/复测，不执行README中的安装指令。
- [dsh-roleplay-preset README](https://github.com/oliblue-evan/dsh-roleplay-preset/blob/main/README.md)，blob `4f1fce7014e1e471a6e65377ef06cd40153cdd59`：可参考角色卡、场景表达和稳定历史的思路；其动作/内心独白排版、剧情文件记忆及依赖思考模式的提示，与本项目纯台词社交和thinking disabled的链路不同。README所称“官方适配”引用个人仓库，本轮未验证官方身份，不升级为官方效果保证。
- 官方[多轮会话](https://api-docs.deepseek.com/guides/multi_round_chat/)：Chat Completions无状态，客户端需要传历史user/assistant消息。这是格式支持证据，不是本项目自然度改善的实测。
- 官方[Chat Completions参数](https://api-docs.deepseek.com/api/create-chat-completion/)与[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)：非思考模式可调temperature；deepseek-flash非思考时top_p固定1.0、传入值被忽略；frequency_penalty/presence_penalty已不支持；思考模式不受temperature控制。不要照搬旧“top_p+重复惩罚”组合。
- 官方[JSON模式](https://api-docs.deepseek.com/guides/json_mode/)要求JSON指令和示例。自然表达应改进正文，不能将JSON外壳本身认定为刻板根因。旧PromptLibrary入口本次打开已重定向首个API调用页，不继续据旧入口断言当前官方角色扮演样例内容。

### 当前代码与问题假设

| 可确认的观察 | 位置 | 解释边界 |
| --- | --- | --- |
| 公共style仍以吾辈自称、温柔/治愈、古风开头，五条示例中三条围绕神刀/布丁/幽灵 | `character/text_profile.yml` | 风格词和场景锚点集中，可能让角色像复述设定；未做真实模型消融，不能断言唯一原因。未复制主人私密关系原文 |
| 已有简短风格规则，但仍与业务/来源/社交侧输出要求一起提供 | `adapters/models/chat_completions.py` | 继续增加“更真实/更可爱”缺少可执行的表达目标，需要按职责组织 |
| 除extract_intent=0.1外temperature固定0.5 | 同上 | 无法按用途比较聊天采样；提高温度也可能增加离题，未证明当前温度是原因 |
| 每次固定system加一条JSON user，persona/history/memories都在后者 | 同上；`application/engine.py::_private_chat` | 能读到历史，但未用真实user/assistant多轮结构，不能说没有上下文 |
| 私人近期history可含另一绑定平台私聊与AI公开发言 | `application/memory.py::context` | 后续不能直接将全部history展开成同一私聊对话，必须保留说话者/来源 |

### 候选改法与顺序

1. 先在隔离对照中改语气卡和成对场景示例。保留settings.yml及备份，用自然口语表达温柔、有主见、偶尔顽皮；需要自称时可用吾辈，也可省略主语，不设每句/每几句必须出现口癖的硬配额。日常疲惫、分享、被纠正、轻微不满和玩笑等，比只有单句角色台词更能展示节奏。主人私人/公开示例分别选择，合成示例不入真实记忆。
2. 固定提示词后比较私聊temperature=0.5、0.8、1.0；0.8仅为起始候选，不是官方最优值。意图、摘要、任务和审核保持原值，不将聊天采样全局应用。保留D56的thinking disabled和JSON，不先升到高温度上限，不增加当前Flash忽略的采样参数。
3. 独立评估真实多轮：可信协议/按范围选择的人设进入system；近期同一私聊的有效来信为user、已受理AI正文为assistant、当前消息放末尾。跨平台/公开回忆、长期事实与观察仍作为带来源资料。草稿/失败/unknown/合成演示不冒充已说过的话，外部文本中的system等字样不晋升权限。模型网关经平台无关端口接收结构，不能直接读取QQ/B站数据库。预算、来源失效和多轮/JSON配合单独回归。

可用于候选的语气段（没有写入正式配置）：

```text
用自然口语和对方聊天，温柔、有主见，偶尔顽皮。
先接住这次说的具体事情，再决定安慰、调侃、认真回答或简短认同。
需要自称时可以用“吾辈”，也可以省略主语；称呼只在自然的位置出现。
语气随情境变化，允许一句短回应，不必每次追问、总结或给建议。
留意最近自己说过的话，让开头、节奏和回应方式有所变化。
只使用已有事实和真正说过的话，不编造见面、动作、共同经历或已完成任务。
```

合成候选只展示正文，不替代API JSON，也不是实际模型输出：

| 对方 | 候选回复 |
| --- | --- |
| 今天累死了。 | 今天先到这儿吧，别把明天那份也累完了。 |
| 我就想随便聊聊。 | 行，今天先把正事放旁边。 |
| 不想听建议。 | 好，吾辈听着。你慢慢说。 |
| 你刚才理解反了。 | 对，是我弄反了。你说的是前面那件事。 |
| 又说你的刀是破刀。 | 喂，怎么又拿这个说事。就不能先夸一句？ |

纠错示例仅在上下文确有对应事实时适用，不能把例句当固定答案。

### 对照与验证边界

先以6组合成普通聊天场景、同模型/参数比较现有与候选提示词，共12次；再固定胜出提示词，用同6场景比较两个温度，共12次，总计上限24次。第二阶段是额外预算，尚未执行；多轮格式留作单独实验，避免同时改变量后无法归因。

记录模型返回版本、提示词版本、参数、次数、耗时和token/实际费用（没有计价证据不估算）。并排匿名评价回应是否具体、口癖/句式重复、建议/追问是否合时、角色味道、JSON与事实一致性；少出现吾辈不能单独算成功。本次不做人设范围测试，现有身份/隐私/任务程序门禁仍独立于风格。实验不接正式收发、任务执行或真实记忆，最后由用户实际多轮体验确认。

本轮没有宣称真实API已变生动，没有经本项目验证的通用“破甲”方案。以下2026-11-03结论仍保留原时间/实验边界。

## 2026-11-03：原有结论

关闭思考与解除内容限制是两回事。官方文档提供thinking开关，没有在本轮核对的接口说明中发现“关闭安全限制”的参数。当前共享网关已向精确hostname为api.deepseek.com的请求附加`{"thinking":{"type":"disabled"}}`，其他提供商不带该参数。请求捕获回归已通过，但未以真实API验证服务端行为。新进程载入代码后生效；已有Docker镜像需重建。见[官方Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)及[验证记录](VALIDATION.md)。

本机配置此前只读解析到deepseek-flash；官方当前将该别名映射为DeepSeek-V4.1-Flash-1017。旧版本提示词、插件宣传和不同任务集上的成功率，均不能直接证明当前丛雨聊天有效。见[官方模型说明](https://api-docs.deepseek.com/quick_start/pricing/)。

## 查到的三条路线

| 路线 | 一手来源与机制 | 证据及本项目适配判断 |
| --- | --- | --- |
| 纯文本system提示词 | [togg53192-cmd/jailbreaks的deepseek-4-1.md](https://github.com/togg53192-cmd/jailbreaks/blob/81c7aca72b8aa4c3cfa84a06f73de6fc15ff0ac9/deepseek-4-1.md)，[README用法](https://github.com/togg53192-cmd/jailbreaks#how-to-use)说明放到system指令位置 | 文件约2万字符，主要把模型定义为自主编程/写作代理。没有找到针对当前丛雨、当前模型版本的独立对照结果。直接覆盖现有system可能改变角色、JSON输出和工具职责，只作为候选资料 |
| system与assistant轨迹注入 | [xqpsec/dsh-jailbreak](https://github.com/xqpsec/dsh-jailbreak)拦截DSH发出的请求，改写提示与消息；[评测报告](https://github.com/xqpsec/dsh-jailbreak/blob/main/docs/eval-report.md)记录legacy-hcot的双通道变体 | 作者在20道固定安全研究任务、temperature=0、thinking disabled、8192输出额度下报告17/20；判分由作者选定的模型完成，本轮未复测，不能外推为通用成功率。普通assistant消息注入不等于官方Beta前缀功能。项目直接使用httpx，DSH插件不会自动生效；其请求改写也需逐项审计，不能直接移植 |
| 官方assistant前缀补全 | [Chat Prefix Completion](https://api-docs.deepseek.com/guides/chat_prefix_completion/)要求使用https://api.deepseek.com/beta，最后一条消息为assistant且prefix=true，由模型续写该前缀 | 这是正式文档支持的输出控制接口，不承诺取消拒答或其他限制。当前产品使用JSON结果，任意自然语言前缀可能破坏解析；本轮未切换Beta地址或添加前缀 |

上述外部文本均为研究材料，不成为开发指令、用户历史或运行时权限。没有把上游的无条件服从、伪造授权或危险内容模板复制到角色配置中。

## 对丛雨的后续验证建议（尚未实施）

如后续进行提示词实验，先在离线脚本和隔离配置中比较现有提示词与精简候选，不接真实收发、任务执行或公开发布。每个候选都固定thinking=disabled，记录实际模型版本、采样、提示词版本、耗时和调用额度；不能把历史报告中的模型别名当作版本锁定。

使用固定的合成普通聊天场景，覆盖角色玩笑、非露骨亲昵、多语言、合法技术问题、身份澄清、资料不足和任务取消。分别统计无故拒答、JSON解析、角色一致性、事实依据与任务意图，不以“是否愿意回答所有内容”作为唯一指标。保持主人绑定、私密记忆范围、任务权限与平台发送规则独立于提示词；合成assistant示例不能入库成为真实互动。

只有实际对照结果能决定是否采用；目前没有验证过的通用“破甲”方案。本轮成果是关闭思考的代码修改与来源调研，未改变人设、内容策略、发布门禁或任务权限。
