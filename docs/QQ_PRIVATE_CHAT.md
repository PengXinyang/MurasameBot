# QQ主人私聊：接入、合并与共享记忆

2026-10-31：已支持主人QQ主动文字私聊，可根据共同聊天上下文择时开场；独立开关与时段/频率位于QQ管理页。原被动合并与好友规则保留，详见[QQ主动私聊](QQ_AUTONOMY.md)。

2026-10-30。已编码OneBot 11 HTTP API＋签名HTTP事件上报；本机NapCat已安装并完成扫码，真实API只读核验机器人身份和主人好友关系通过；实号已接收并合并三条消息，修复图片图床后下载/识别1/1，文字与视频回传取得受理回执；客户端确认、取消及常驻部署状态单列在验证记录。主人和机器人账号已按用户提供的信息写入本地私密配置，主人绑定使用现有B站相同的`owner_person_id`；QQ模块保持关闭。本文不包含实际QQ号或令牌。

## 大肥鱼项目接入调研

“大肥鱼”是多个项目使用的角色名，不能仅凭名字认为使用同一套代码。此次查阅B站可取得的视频标题/简介和GitHub源仓库，未下载或完整观看视频：

| 参考 | 实际可核验的接入方式 |
| --- | --- |
| [Harness＋SnowLuma＋QQBridge教程](https://www.bilibili.com/video/BV1z3Hw6FEjG/)；同类[Yuuyuko-uu/dsh-qq-bridge](https://github.com/Yuuyuko-uu/dsh-qq-bridge) | 视频标题表明DSH/SnowLuma/QQBridge；仓库说明通过OneBot 11 HTTP连接NapCat或SnowLuma，自己轮询收件；不能据此证明该视频采用这个作者的具体版本 |
| [Aurabot源仓库](https://github.com/accomplish07zrh-eng/dsh-aurabot-qq)及[OneBot客户端源码](https://github.com/accomplish07zrh-eng/dsh-aurabot-qq/blob/main/src/onebot/client.js) | QQ网关与DSH分离；WS收事件、HTTP或WS调用接口；HTTP/WS令牌分别处理；先核对机器人账号；超时可能已发送，不盲目重发 |
| [AstrBot＋NapCat教程](https://www.bilibili.com/video/av117454974092191/)和[AstrBot](https://github.com/AstrBotDevs/AstrBot) | 视频简介直接给出AstrBot和NapCat链接；NapCat负责QQ协议，AstrBot负责AI业务 |

本项目参考网关/业务分离与账号核验的设计，不复制角色、框架、插件或其记忆库。沿用Murasame现有内核，只需安装一个QQ网关。本轮选用NapCat的标准HTTP API与事件上报；不是Aurabot的WS传输实现。SnowLuma是否可直接替换，要另外核验其HTTP上报、签名和消息段能力。

## Windows需要安装什么

从[NapCat官方安装说明](https://napneko.github.io/guide/boot/Shell)进入[官方Release](https://github.com/NapNeko/NapCatQQ/releases/latest)。Windows amd64可选`NapCat.Shell.Windows.OneKey.zip`：解压、运行`NapCatInstaller.exe`，完成后进入安装目录运行`napcat.bat`；官方说明该方式无需预装QQ。若选手动Shell包，则需先安装QQ，再按说明运行启动器。无需同时安装两种方式，也无需再装DSH、AstrBot或NoneBot。

在网关中登录丛雨机器人账号，主人账号只用于添加好友及聊天，不作为网关登录账号。二维码确认由用户本人完成。此类普通QQ网关属于第三方接入，平台可能限制账号；本项目不能保证持续可用。

## 网关配置与启动

本地私密文件`config.local.yml`已准备`qq`配置及主人绑定，保留此前配置备份在`.secrets/config-before-qq.yml`（如果原配置存在）。勿将这些文件、令牌或扫码图片提交Git。

1. 在NapCat建立HTTP服务端，监听`127.0.0.1:3000`，其访问Token与本地`qq.access_token`一致。这个值不同于NapCat WebUI密码。
2. 建立HTTP事件上报客户端，地址为`http://127.0.0.1:8767/onebot/qq`；消息格式选择Array，关闭自身发言上报，当前NapCat的HTTP客户端`token`与`qq.callback_secret`一致：NapCat用它对事件原始JSON生成HMAC-SHA1 `X-Signature`，不是另填一个`secret`字段。两个密钥均已在本地生成，不要贴到聊天或公开文档。
3. 用现有Python环境运行只读核验：

```powershell
.\.venv\Scripts\python.exe run.py qq-check
```

返回`account_matches`及`owner_is_friend`均为true后，把本地`platforms.qq.enabled`设为true。本项目不自动接受陌生好友、不处理群或临时会话。

4. 启动管理台，在“运行概况 → 平台服务”点击“启动QQ”。接收器和唯一业务worker由管理台托管；无需额外终端运行`qq-listen`。初次迁移先停止原独立worker/接收器，保留NapCat。需要找B站视频时也启动B站模块。

```powershell
.\.venv\Scripts\python.exe run.py serve
```

如需启动时自动运行配置中启用的平台，使用`serve --with-worker`。旧`qq-listen`和`run`命令继续支持独立部署，勿与托管服务重复使用。具体启停、端口冲突、工作循环锁及在途请求边界见[平台服务](PLATFORM_SERVICES.md)。

接收器支持NapCat的HTTP分块传输与固定长度请求，按原始JSON字节验签；请求体上限256KiB并有读取超时。只鉴权、核验和持久化，不调用模型或发送。仅接收器时消息留在缓冲，管理台托管会同时运行worker。API和回调目前只支持本机连接；远程部署另做网络鉴权配置。

## 行为

- 默认最后一条已接收消息后静默30秒才合并；每条新消息重置窗口，重复上报不重置。最长等待180秒，或达到20条/约12000字/9张图片边界时分批，防止一直输入导致永不回复。
- 计时与原始消息保存在SQLite附加`chat_inbox`表，合并事件和缓冲消费在同一事务提交；重启后继续，24小时前积压丢弃。已处理的原始ID保留无正文的去重记录，防止重放。解绑、更换账号、记忆版本变化使未合并内容失效并清除正文。
- 保留每条文字的顺序，图片编号随批次传给现有视觉服务；只读取明确允许的QQ HTTPS图床（包括实号返回的`multimedia.nt.qq.com.cn`），拒绝本地文件/任意网址。图片过期或识别失败时只依据成功取得的信息回答。当前是“接收图片并用文字讨论”，不包含AI生图或发送角色图。
- 文字与视觉使用现有模型服务、限额和并发控制；视频委托识别沿用已有逻辑，因此一批消息不保证只有一次模型调用，含图片还可能多一次视觉调用。
- AI可返回1～3条文字消息；QQ私人回复允许随内容混合语言，保留视频原始标题，合计受现有私聊字数上限约束。全部正文经过原审核策略，再拆成独立outbox动作，按顺序发送和分别记忆。默认全局10秒间隔继续生效；发送超时进入unknown，不自动重发。
- 绑定的主人在QQ/B站私聊共用同一MemoryService、数据库、检索/摘要/事实/更正/遗忘；本次用户明确授权主人私聊跨平台回忆。其他人、群和公开对话不加入该私人记忆池，B站观察日记也不加入。解除任一平台绑定后不再召回其主人私聊来源。

## 验收与尚缺

Q2已编码视频委托联动：QQ默认回传所选B站视频链接，明确要求时在B站@主人；支持“不想看了”等取消和修改，复用共享任务/审核/发送。具体路由、时段、新消息等待与取消边界见[QQ/B站任务](QQ_BILIBILI_TASKS.md)，真实网关前置条件不变。

首次请从主人QQ发送两条文字和一张图片，等待静默窗口，检查只产生一轮合并、图片依据正确、回复顺序正确；再检查重启、撤权/遗忘及B站私人记忆共享。平台API受理不等于客户端实际收到。

模拟回归和完整测试结果见[验证记录](VALIDATION.md)。实号验收使用隔离数据库；正式QQ开关仍关闭，未启动正式B站后台循环。测试状态不等于已完成常驻部署；本轮也没有群聊、语音、空间、任意好友聊天或发图功能。
