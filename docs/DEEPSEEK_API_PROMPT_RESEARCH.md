# DeepSeek API提示词与“破甲”调研

核对日期：2026-11-03。代码基准为`1c25860`，本轮仅检索官方文档与上游源码，不调用真实模型、不安装插件、不注入外来提示词。

## 当前结论

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
