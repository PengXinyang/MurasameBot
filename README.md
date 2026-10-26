# MurasameBot

丛雨的跨平台社交内核，首个实际适配目标为 B 站。当前版本 `0.1.0`，采用 **Python 3.12+**。

Python 更适合当前单人迭代：角色配置、模型接口、内容处理与既有 B 站实现都容易接合；数据库和调度首期用标准库即可。Java 更适合已有 Java 团队或统一 JVM 部署的场景，本项目目前没有这些前提。模块之间使用端口接口，未来 QQ、微信适配器复用同一套内核。

## 当前实现

- 视频及动态评论区的 @ / 回复通知读取、首次基线和重复事件识别。
- 获取评论线程、生成中文角色回复、排队发送到对应楼层。
- 监测主人新视频和新动态，生成主动评论。
- 独立转发主人新视频对应的投稿动态与新动态，评论和转发分别开关、分别去重。
- 按时刻搜索 Pixiv/X 的游戏角色插画，筛选后生成丛雨配文；仅对配置授权范围内的作品发布图片，附画师和原作链接。
- 统一对外代理，默认 `http://127.0.0.1:7890`，可切换 SOCKS5；管理台仍使用 8766。
- 按星期、时区、时间窗口和分区发现视频，读取字幕并决定旁观、评论、接话或推荐。
- 使用实际 UID 到昵称映射编码 @ 主人，记录回执和提及是否确认。
- 统一人物绑定、范围隔离的偏好记忆、可取消/修改的持久找视频委托。
- SQLite 持久化、发送额度、并发领取控制、重启恢复、结果未知时不重发。
- 丛雨文字人设导入、本地管理台、命令行和无网络模拟演示。

**这些是代码实现范围，不代表真实账号端到端验收已经通过。** 当前未提供模型配置和机器人登录态，未向 B 站发送任何真实评论。QQ、微信的真实适配器尚未实现；跨平台需求可通过本地统一事件输入验证。

## 启动

在本目录执行，不必激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,login]"
.\.venv\Scripts\python.exe run.py doctor
.\.venv\Scripts\python.exe run.py demo
```

本次开发环境已在 `.venv` 安装本地命令入口及依赖。`demo` 使用固定场景模型和假平台，不联网、不计费、不改变正式账号数据库；它展示“模拟微信需求 → 保存任务 → 查找视频 → 模拟 B 站提及 → 重启去重”。

## 填写模型和账号

把 `.env.example` 复制成 `.env`，在本机填写：

```dotenv
MURASAME_MODEL_BASE_URL=https://your-service.example/v1
MURASAME_MODEL_NAME=your-model-name
MURASAME_MODEL_API_KEY=your-private-key
MURASAME_OWNER_UID=your-bilibili-uid
```

服务需支持 `/chat/completions`、`response_format: {"type":"json_object"}` 和结构化 JSON 文本输出。地址填写到 `/v1` 等基础路径，程序追加 `/chat/completions`。本地无密钥服务可以留空密钥。修改 `.env` 后重启工作进程。

主人 UID 从本地 `.env` 的 `MURASAME_OWNER_UID` 读取，也可在被 Git 忽略的 `config.local.yml` 中覆盖 `bindings`。公开示例不含个人 UID；没有填写就不建立主人绑定。请使用你准备作为 AI 的账号扫码：

```powershell
.\.venv\Scripts\python.exe run.py login
```

用 B 站 App 扫描生成的 `.secrets/login-qr.png` 并确认。凭证写入 `.secrets/bilibili.json`，不会输出 Cookie。`bilibili.robot_uid` 可填预期机器人 UID，用来防止误登账号。头像由你在平台手动设置。

随后进行只读登录检查：

```powershell
.\.venv\Scripts\python.exe run.py doctor --online
```

Cookie 失效时程序暂停相应出站；重新扫码并 `resume`。首版没有自动 Cookie 刷新，也不会处理验证码或切换其他账号继续操作。

网络默认走本机 7890 代理；代理服务尚未启动时，在 `config.local.yml` 设置 `network: {proxy_enabled: false}`。代理协议、定时美图、授权名单与转发配置见 [新增功能配置](docs/新增功能配置.md)。

## 管理台与工作循环

```powershell
# 仅管理台：不会启动平台轮询、模型调用或发布
.\.venv\Scripts\python.exe run.py serve

# 填好模型、登录后，启动管理台与工作循环
.\.venv\Scripts\python.exe run.py serve --with-worker

# 也可以仅运行后台工作循环
.\.venv\Scripts\python.exe run.py run
```

打开终端打印的本机管理链接。该链接含管理令牌，只留在本机；服务只绑定 `127.0.0.1`。面板可设置浏览时段、星期、分区、兴趣、每轮数量、发布模式，查看草稿、记忆、委托和活动记录，并暂停或恢复任务。配置改动下一轮生效。

默认 `mode: draft`，生成草稿后在面板批准进入发送队列；已批准动作由运行中的工作循环发送。切换为 `auto` 后，新动作按策略自动发送，无需逐条确认。切回草稿模式会把尚未执行的自动动作转回待审核，已开始的网络请求可能无法撤回。

## 浏览与推荐

`config.yml` 中配置：

```yaml
browsing:
  enabled: true
  weekdays: [0, 1, 2, 3, 4, 5, 6]
  windows:
    - start: "19:00"
      end: "21:00"
  categories: ["4", "188"]
  interval_minutes: 60
  videos_per_round: 2
  candidate_limit: 6
  interests: [游戏开发, 人工智能]
  owner_interests: []
```

面板设置写入 `config.local.yml`，优先于 `config.yml`。`categories` 显示适配器的分区参考表；这些是社区接口兼容 ID，需要核对实际返回内容，不能视为网站当前导航的官方完整目录。

分区留空时不做一般探索，但有效的明确找视频委托仍可搜索。关闭浏览总开关时，委托和一般浏览都不主动读取新候选；通知回复、主人新内容监测独立运行。每轮到期委托优先于一般探索。跨午夜窗口归属开始的星期。

当前内容理解基于可取得的字幕和动态正文。无字幕视频不进行主动评价或推荐；被动回复只能依据已取得的简介、评论等信息。尚未接入 ASR、视频画面和动态图片理解。长视频字幕最多读取前 3 个分 P，文本有长度预算，因此不会声称完整观看全部内容。

首轮通知和主人内容列表只建立基线，不评论历史积压。通知按配置页数和时间范围读取，积压超过上限会提示调整，不承诺无限历史回补。平台接口并非正式开放授权保证，兼容性须用自己的可用账号逐项验收。

## 记忆、委托与后续 QQ / 微信

`character/settings.yml` 保留原样，`character/text_profile.yml` 保存从来源提炼的社交文字角色资料。公开资料和主人称谓分开使用，原始人设中的私人姓名、关系和秘密不整段发送到公开模型上下文。人设版本更新不会清空数据库。

所有文字只用中文；日语仅为将来 QQ / 微信语音生成预留，本版没有翻译或 TTS。服装、图片和自动头像不属于首版。

绑定自己的后续平台身份后，可导入统一事件测试意图识别：

```powershell
.\.venv\Scripts\python.exe run.py bind wechat your-verified-user-id
.\.venv\Scripts\python.exe run.py ingest examples/owner_request.json --process
```

将示例中的 `actor_id` 改为你已核验绑定的标识，时间改为当前时间。`--process` 会调用真实已配置模型，可能计费，但此命令不发送消息。真实微信/QQ消息读取需要后续实现对应适配器；昵称相同不建立身份关系。

明确请求成为一次性任务；兴趣不会自动变成周期任务。明确更正或取消会使旧任务和待发动作失效，删除来源记忆会阻止旧事件重放恢复该需求。明确要求“每天/每周/持续推荐”才建立周期任务。公开推荐不携带源私聊原文；敏感主题会停止公开投递，供本地处理。

## 常用命令

```powershell
.\.venv\Scripts\python.exe run.py inspect tasks
.\.venv\Scripts\python.exe run.py inspect actions
.\.venv\Scripts\python.exe run.py inspect memories
.\.venv\Scripts\python.exe run.py pause
.\.venv\Scripts\python.exe run.py resume
.\.venv\Scripts\python.exe run.py cancel TASK_ID
.\.venv\Scripts\python.exe run.py forget MEMORY_ID
.\.venv\Scripts\python.exe run.py approve ACTION_ID
.\.venv\Scripts\python.exe run.py reject ACTION_ID
```

超时或进程中断可能意味着评论已经发出。状态 `unknown` 不自动重试；先到平台核对，再记录实际结果：

```powershell
.\.venv\Scripts\python.exe run.py resolve ACTION_ID --state accepted --external-id ACTUAL_COMMENT_ID
# 或确认没有发出后，记录失败（仍不会自动重发）
.\.venv\Scripts\python.exe run.py resolve ACTION_ID --state failed
```

平台“受理”不证明公开可见。提及字段未得到确认时也保留待核对状态。所有发布统一经过队列，额度在事务内预留。

## 验证与代码结构

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py
```

测试包括跨平台需求场景、身份与范围隔离、取消/删除/更正、重启、并发领取、未知发送、模拟 HTTP 契约和管理台接口。测试不调用真实模型或 B 站；管理台测试会访问本机随机端口。受限沙箱需允许本机回环访问。

2026-10-26 本地结果：**56 项通过，0 失败，0 跳过**；依赖检查通过，管理台已由独立浏览器渲染检查，原始 settings.yml 哈希保持一致。明细见 [验证记录](docs/validation.json)、[测试结果](docs/test-results.xml) 和 [管理台预览](docs/dashboard-preview.png)。这些结果不包含真实模型或账号读写验证。可使用 requirements.lock 重建此次测试依赖版本。

`domain` 为纯数据对象，`application` 为通用业务，`ports` 为能力接口，`adapters` 实现平台/模型/数据库/角色格式，`bootstrap` 装配应用。测试会检查核心导入边界，并在禁用第三方包的 Python 进程中导入核心。首期使用 SQL 范围过滤和结构化记忆，没有向量检索或训练功能。

更多设计见 `开发顺序与功能流程.md`、`架构与跨平台记忆设计.md`。第三方参考来源见 `THIRD_PARTY_NOTICES.md`。
