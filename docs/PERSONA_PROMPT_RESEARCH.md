# 角色对话、提示词与去套话调研

日期：2026-10-31。MurasameBot调研基线为f64c10a；本轮已做下文第一阶段的有限风格修改，其余均为建议。没有调用真实模型或测试真实社交发布，不把结构回归当作自然度验收。

## 结论与本轮实现

优先改表达示例和普通聊天习惯，不再叠加长篇“必须可爱、必须配合”的指令，也不追加第二次模型润色。角色的语气来自具体回应、节奏和少量用词，不能只靠反复自称或每条都提角色背景。

已在chat_completions.py的八类正文生成操作共用一份简短表达规则：允许一句接话、不强凑句数或每次追问、减少固定口癖、普通闲聊不无故结束、不反复插入AI身份声明；被直接询问身份时如实说明。不确定时简短说明或询问，不能回答的私人请求只回应合适部分。公开需要skip时继续skip，不因为“继续聊天”额外发言。原有同次can_send、话题、互关、权限、隐私、来源、语言和输出格式要求保留。

text_profile.yml改了四条通用示例，保留破刀抗议，减少“唔”和“资料不足”套句；语言改为服从程序当前渠道规则，消除“一律中文”与已实现的QQ主人私聊多语言策略冲突。settings.yml、角色背景、主人专属内容和记忆未重写。源文件哈希核对机制保留，派生profile改动照常改变角色版本。

本轮不加入威胁、惩罚或无条件服从提示，不改变模型服务边界；不实现R18输出或相关规避模板。非露骨的亲昵、撒娇、吃醋仍按原有授权与角色状态规则表达。

## 当前链路与可能的套话来源

代码入口：

- [角色导入器](../src/murasamebot/adapters/characters/lingchat.py)：读settings.yml的角色名和来源哈希，运行时文字取自经范围区分的text_profile.yml，不原样注入来源system_prompt。
- [Persona.prompt](../src/murasamebot/domain/models.py)：公开背景、风格、通用示例；经身份验证后才加入主人语气，只有主人私聊可加入专属背景。
- [ChatCompletionsModel](../src/murasamebot/adapters/models/chat_completions.py)：操作要求在system消息，人设、真实消息和授权上下文在有界JSON user消息；同次返回正文、表情和发送判断。
- [共享记忆](RUNTIME_MEMORY.md)与[短期角色状态](SOCIAL_STATES.md)：提供真实来源，不能把示例、风格要求或虚构互动写成真实经历。

基线中private_chat已明确不写客服套话，但五条通用示例中两条以“唔”开头，另有固定的“资料不够”声明。重复模式可能被模仿，这是提示词层面的风险判断，并非真实模型对照实验结论。资料缺失、身份范围和任务状态仍要真实保留，不能为了口语化删掉事实限制。

基线最终system指令按Python字符数计，private_chat为3852、reply为4156、evaluate_video为2714，另有人设与上下文JSON；这不是token数。它说明仅继续加长风格禁令会增加负担，不能证明长提示词一定导致八股。未来应按职责组织并测量，而非直接删去安全/来源约束。

## 可用skill与插件

先查skills.sh目录，再核对上游SKILL.md、仓库热度与许可证。下列为2026-10-31查询快照，目录安装量与GitHub指标会变化，不是效果证明。

| 工具 | 核验情况 | 本项目用途与取舍 |
| --- | --- | --- |
| [op7418/Humanizer-zh](https://github.com/op7418/Humanizer-zh/blob/main/SKILL.md) | 社区项目，MIT；GitHub约2.2万Stars，目录约5.6万总安装；本机已有humanizer-zh | 首选开发时检查中文示例与提示词；保留事实、语气和确定程度，不靠虚构经历制造自然感 |
| [blader/humanizer](https://github.com/blader/humanizer/blob/main/SKILL.md) | 社区项目，MIT；GitHub约6.3万Stars；本次目录页未可靠提取安装量，不补造数字 | 参考结构性套话与声音校准；偏散文编辑，不把全部规则搬进短聊天 |
| [hardikpandya/stop-slop](https://github.com/hardikpandya/stop-slop/blob/main/SKILL.md) | 社区项目，MIT；GitHub约1.8万Stars，目录约1.9万安装 | 参考去掉空铺垫和机械节奏；其一律删副词/禁某些标点等规则不适合照搬中文角色聊天 |

目录入口：[Humanizer-zh](https://www.skills.sh/op7418/humanizer-zh)、[Humanizer](https://www.skills.sh/blader/humanizer/humanizer)、[Stop Slop](https://www.skills.sh/hardikpandya/stop-slop/stop-slop)。npx skills find humanizer尝试因本机npm访问/缓存权限EACCES失败，改用网页目录与GitHub源码完成核验；未将CLI搜索记为成功，未安装或升级任何skill。

当前插件目录以creative writing character dialogue和humanizer检索，没有发现值得为本任务新增的匹配插件；不表示所有插件市场都没有。已有GitHub工具足够读上游。开发助手的skill不会自动成为MurasameBot运行时能力，本轮也没有把本机skill路径变成项目依赖。

## LingChat实际逻辑与可借鉴范围

只读核对本机LingChat提交ef8f79141f724fee1f0c78fc863b63bc2be42126，下列四个关键源码文件相对HEAD未发现差异。它是当前Rust/Tauri实现，不能把早期Python版本或网络介绍当成这份代码的事实。

| 位置 | 已核对行为 | 本项目取舍 |
| --- | --- | --- |
| [utils/prompt.rs](https://github.com/SlimeBoyOwO/LingChat/blob/ef8f79141f724fee1f0c78fc863b63bc2be42126/src-tauri/src/utils/prompt.rs) | 按角色设置组装背景、环境说明、台词格式及示例；可选双语；检测部分旧格式时直接返回旧prompt | 借鉴分层和表达示例；不直接复制格式，避免情绪标签、动作旁白、日语译文进入B站正文。旧格式分支意味着并非所有角色都会走相同拼接 |
| [game_system/memory_builder.rs](https://github.com/SlimeBoyOwO/LingChat/blob/ef8f79141f724fee1f0c78fc863b63bc2be42126/src-tauri/src/ai_service/game_system/memory_builder.rs) | 按目标角色整理自身assistant台词、其他上下文及user发言；保留当前角色system，处理重复与工具记录 | 值得研究真实多轮消息组织；不能把公开评论、其他用户私聊和主人私密材料混为同一历史 |
| [message_system/generator.rs](https://github.com/SlimeBoyOwO/LingChat/blob/ef8f79141f724fee1f0c78fc863b63bc2be42126/src-tauri/src/ai_service/message_system/generator.rs) | 获取当前角色memory，执行流式管道，分段处理情绪/翻译/TTS并写回角色台词；区分普通聊天、主动等调用来源 | 可借鉴调用来源与展示分段；本项目继续完整生成、审核后发送，不把未审流式片段逐条公开 |
| [llm/providers/genai_provider.rs](https://github.com/SlimeBoyOwO/LingChat/blob/ef8f79141f724fee1f0c78fc863b63bc2be42126/src-tauri/src/ai_service/llm/providers/genai_provider.rs) | 保留system/user/assistant/tool角色，配置temperature/top_p，归一化停止原因，包括content_filter | 说明角色效果受消息组织、模型与采样共同影响；源码里有指令不等于服务保证执行，更不代表能解除服务端限制 |

上游prompt还有强制服从和以终止程序施压的文字。本轮只记录存在这类写法，不复制模板，也没有实测其效果。重复命令不是可证明的权限机制。原背景中的年幼外观也不会因设定寿命而自动改变。

LingChat本机LICENSE为AGPL-3.0。本轮仅研究架构，独立编写表达规则，没有复制其程序、提示词大段原文或引入运行依赖；素材权利继续按现有第三方声明处理。

## 后续优化顺序与验收

第一阶段已编码：上述共享表达规则、有限示例调整、渠道语言冲突修正。先让操作者重启后体验，比较连续几轮能否接住具体内容，是否仍重复口癖。普通闲聊继续对话不等于必须回应所有请求。

第二阶段尚未开发：把表达样本按普通朋友、绑定主人私聊、公开内容回复分类，样本覆盖简短认同、轻松抗议、安慰、真实纠错与必要澄清。示例要标为合成表达参考，不能作为用户历史；主人专属样本不得进入普通用户或公开生成。保留“可以不用吾辈/表情”的弹性，不制造另一套固定句式。

第三阶段尚未开发：评估将已授权的真实近期对话保留为有界多轮消息，而非把所有资料都扁平放入一条JSON。先验证角色、目标、可见性、记忆来源、预算与末尾当前消息的边界；历史中冒充system的文字不得晋升权限。保持现有结构化结果和同次审核，不复制LingChat桌面消息协议。

第四阶段尚未开发：建立固定的合成场景集，对同一模型/设置做新旧盲测。建议至少覆盖短问候、疲惫、夸奖、角色玩笑、不同意、澄清、标题不足判断、QQ多语言、普通用户冒充主人、公开敏感话题、正常结束对话和真实身份询问。记录模型版本、采样、开场/结尾重复、空泛安慰、重复问题、角色一致性、事实/身份错误、skip与审核边界及实际耗时/调用量。

自然度需人工比较，不能用关键词测试、AI检测分或“测试全过”代替。不要靠删除不确定性、冒充真人、增加无依据共同经历或第二次改写未再审核的正文来提高观感。正常聊天结束、主人要求安静、主动无话可说、超预算或不允许互动时，仍允许停止或skip。
