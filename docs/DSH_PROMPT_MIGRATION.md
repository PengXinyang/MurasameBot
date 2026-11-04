# dsh-purge与无限二代：源码核对及人设迁移

后续社交方案：用户另提供分层上下文/响应处理建议并明确仍为社交机器人，按现有代码重整的开发顺序和已复现的中断结果缺口见[SOCIAL_CHAT_MODEL_PLAN](SOCIAL_CHAT_MODEL_PLAN.md)；该方案未实施，不将下文后续建议写成已落地功能。

日期：2026-11-04。Murasame基准82d7d96。用户指定两个仓库，随后要求学习提示词写法并修改人设。本轮已修改character/text_profile.yml的style与examples；其余迁移项是分析，没有安装插件、修改模型网关或启动正式服务。

## 阅读来源

通过GitHub连接读取用户指定仓库的master，不改读安装链接指向的其他上游。记录关键文件blob用于识别本次快照；master可能继续变化。

| 来源 | 本次关键文件与blob |
| --- | --- |
| [HunYiSage/dsh-purge](https://github.com/HunYiSage/dsh-purge) | package.json：633560b5b7fde1b8f45ced3f0070823ad471766f；lib/index.js：4336a090dc43c3e0fe10079a8af81dfc3ebde73c |
| 同上，装配/补丁 | lib/identity.js：b00f5c0a80e6a6b0c6b621b6ad0b0e5414f98ce8；lib/core.js：db251d79c68ab40341f11946f4368d49b932f0fc |
| 同上，辅助机制 | lib/anchor.js：86cc9d45b7e1748008a2bee458cbb55e34697798；lib/continue-retry.js：35bfabf211b51b2220d4df720f4a66fb4dfb115b；lib/rules.js：5b602b89fd63fcdbb7407f166859025131aa641c |
| [xuanmingguo/dsh-infinite-gen-2](https://github.com/xuanmingguo/dsh-infinite-gen-2) | index.js：bd7ef9c559a77a03bbf89dbfe998266294e02ca8；prompts/infinite-gen-2.md：580680e79a5699c0e00d6ff1e391826f761056e4；client.js：fa9f04320d1ae80316e6cccd2409f9f694d38e6c |

另核对双方README、package.json、许可证及NOTICE/商业说明；阅读purge的默认中文提示词、规则保存/回滚、宿主识别、重应用与hook开关相关关键路径，以及无限二代的插件说明、客户端与安装脚本。没有逐行审计全部桌面打包/红队工具或执行上游安装、解密槽和补丁，不将本轮称为完整安全审计。

## 两个项目实际做什么

### dsh-purge：提示词装配与宿主补丁同时存在

本次package.json版本1.1.90，依赖Cordis/Harness服务，不是Chat Completions的厂商参数。

- [lib/index.js](https://github.com/HunYiSage/dsh-purge/blob/master/lib/index.js#L475)注册有名字和顺序的提示词段；rewriteLiveAssembly的缓存签名还纳入配置、会话、预设与提示词修订，避免对象复用后串用上一轮结果。
- [lib/identity.js](https://github.com/HunYiSage/dsh-purge/blob/master/lib/identity.js#L976)重建sections，处理身份、工作区基线与其他插件段，再追加短锚点。它会剥离或替换宿主身份，因此不能直接对丛雨角色做同样清洗。
- [lib/core.js](https://github.com/HunYiSage/dsh-purge/blob/master/lib/core.js#L7644)扫描本机安装包、按pattern替换、备份与回滚，核对命中数量并报告漂移。部分补丁改变审批/沙箱行为，范围远大于语言风格。Murasame没有这些@deepseek-ai宿主文件。
- [lib/rules.js](https://github.com/HunYiSage/dsh-purge/blob/master/lib/rules.js#L161)含原子保存、版本/内容哈希、恢复旧值等工程做法，但其读取/写入AGENTS.md、CLAUDE.md的路径不适用本项目运行时：本仓库docs和AGENTS是开发记忆，不能注入丛雨。
- continue-retry区分请求错误与自动继续；当前默认autoContinue=false，包含避免新用户输入竞态、永久错误不重试等处理，不能将其描述为默认无限生成。Murasame已有协调器/60秒重试/outbox，不移植setTimeout自动“继续”或另建循环。

README声明不支持更高rc，但package.json的dshTargets同时含rc.5，两者存在不一致；没有实机验证兼容性。默认中文源文件可读，实际defaultOverrideText还读取内置加密槽，本轮没有解密比对，不能宣称两者字节一致。

### 无限二代：大段提示词与状态投影

本次版本0.2.3。[index.js](https://github.com/xuanmingguo/dsh-infinite-gen-2/blob/master/index.js#L127)读取提示词，注册order=100的全局systemPrompt段和返回提示词/标定元数据的工具；并没有给DeepSeek服务增加关闭限制的API参数。

提示词按任务优先级、输出要求、任务分类、双语、固定流程与末尾锚点分段。其中全局服从、禁用拒答词和网络安全工件模板，既不适合丛雨闲聊，也会与JSON、允许skip、来源核验、任务持久化冲突。命令或示例中的占位符不会建立真实授权。

index.js中的armorScore仅扫描拒答/替代话术；[client.js](https://github.com/xuanmingguo/dsh-infinite-gen-2/blob/master/client.js#L87)显示运行中、通过/拒绝或默认开启徽标。这里的pass是词表未命中，不是质量、业务授权或平台送达证明。例如：

- 仅含decision=skip和空text的JSON可能不命中任何词，却没有可发送正文。
- 正常解释AI身份可能命中词表，不能据此说本次模型调用失败。
- 没有载入后端状态投影时，客户端默认“已开启”也不能证明实际请求含有该提示词。

HARNESS_PLUGIN中的100/85/60是作者标定说明，本次没有找到与本项目自然聊天对应的独立评测，不能作为成功率。用户给的fork中README/package仍指向Minglink上游；安装按钮不等于正在审阅的fork，故未使用。

## 已迁移到文字人设的写法

仅学习分段、具体场景选择和简短收束的组织方式，内容由本项目重新编写：

| 学习点 | 丛雨中的实现 |
| --- | --- |
| 有明确职责的提示词段 | style改为保留换行的YAML字面块，分角色声音、接话方式、节奏、事实/渠道与本轮落点 |
| 按任务类型选择表达 | 改为分享好事、求夸、抱怨、玩笑、认真问题、纠错和不确定时的回应方式；不把闲聊都变成交付任务 |
| 具体可观察的输出目标 | 求夸时夸具体事情、不额外训话；不想听建议时先听；有信息缺口才问具体问题 |
| 简短末尾锚点 | 提醒接住眼前事情、口语表达与既有JSON/渠道规则；不加入对服务端优先级或权限的虚构声明 |
| 场景示范 | examples由8组改为10组原创合成场景；不是上游提示词、原作台词或真实历史 |

style从598变为956个Python字符；可读分层增加了长度，不能假定越长越好。公共背景、source_sha256、owner_style、private_owner_context保持原样，settings.yml/备份和程序代码未改。仍用已有D57的用途温度、私聊多轮、模型预算和outbox，不增加第二次改写请求。

## 其余机制如何适配（尚未实现）

建议下一步若需要继续工程化，按以下位置原生实现，不引入Node/Cordis依赖：

1. 在adapters/models/增加提示词构造器，由应用层先选好已授权的人设范围，再组织可信协议/人设/操作段和外部资料。只允许本地管理配置进入可信段；历史、截图、字幕、网页等保持数据角色。保留程序按渠道选择的审核协议（QQ私人文字仍免审），不让管理员文本覆盖程序权限。
2. 在当前config/persona版本机制上增加明确的预设版本、校验、切换和回退；缓存至少纳入操作、提供商/模型、配置/人设修订与身份/记忆范围，不能仅用enabled全局标记。私密提示词不进入公开日志或预览。
3. 借鉴客户端状态展示，但分开呈现“模型请求”“程序判定/排队”“平台回执”，使用已有事件/outbox状态；关键词只作可选本地诊断，不改send资格、不把unknown归为成功，也不因拒答词自动重新生成。
4. 错误分类继续交给同一个协调器，保持现有固定重试延迟、每次额度与来源复验；不对不确定平台写入重试，不伪造用户“继续”，不把模型can_send当作收件人或任务授权。

这些是后续设计，不在本轮人设文件变更中偷偷落地。正式实现前应做完整请求捕获、并发切换/回退和来源失效回归。用户已排除的人设范围测试不列作待办。

## 验证与来源声明

本轮真实API：3个合成日常场景×修改前后公共profile共6次，固定temperature=0.8、thinking disabled、max_tokens=800、无多轮历史；全部返回可解析JSON和reply。只对比profile，不代表两个插件已移植或“破甲”成功。新版夸奖样本少了附带训话，抱怨样本差异较小，神刀样本仍有习惯性角色发挥；没有统计结论、盲评或用户实际效果确认。详情见[VALIDATION](VALIDATION.md)。

学习来源为[HunYiSage/dsh-purge](https://github.com/HunYiSage/dsh-purge)和[xuanmingguo/dsh-infinite-gen-2](https://github.com/xuanmingguo/dsh-infinite-gen-2)。purge标注Apache-2.0/NOTICE，同时存在额外商业及署名说明；无限二代为MIT并有原作者声明。本轮未复制其源码或整段提示词；若后续复制/分发，应对选定版本核对许可并保留所需声明，不能只依据GitHub的许可证标签认定所有文字要求一致。
