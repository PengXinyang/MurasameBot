# 管理台内的平台服务启停

更新：2026-10-31。`ReceiverManager`负责本进程的平台收件服务生命周期；QQ回调不再必须单独启动进程。NapCat仍是独立协议网关。

## 使用

```powershell
.\.venv\Scripts\python.exe run.py serve
```

打开本机管理台，在“运行概况 → 平台服务”分别点击“启动QQ”“启动B站”。只启动QQ时，即使配置里B站为开启，也不会将B站加入本进程的工作循环。启动QQ会自动监听原`qq.callback_port`和`/onebot/qq`，并运行共用协调器；无需再开`qq-listen`终端。QQ原有签名、主人好友核验、30秒静默合并、视觉、共享记忆、审核与outbox保持不变。NapCat须已登录并按[QQ私聊说明](QQ_PRIVATE_CHAT.md)配置上报。

如需启动时自动运行配置中启用的全部平台：

```powershell
.\.venv\Scripts\python.exe run.py serve --with-worker
```

`serve`默认只开管理台，等待点击；`--with-worker`自动选择`platforms.*.enabled: true`的平台。启停按钮将该平台开关保存到本地覆盖文件，平台设置页保存开关也会同步启停。管理台退出关闭自己的接收端口及工作循环，但保留配置中的开关，供下次自动启动使用。关闭全部平台后，共用线程空闲等待，不再调度平台工作；共同记忆维护也等待下一次启动。

初次切换前先停止旧的独立`run`和`qq-listen`进程；不需要关闭NapCat。此版本中，相同数据库和agent的工作循环使用操作系统文件锁，已有worker仍占有锁时新worker拒绝启动，管理台显示异常。锁随进程退出释放，残留锁文件无需删除。回调端口被旧接收器占用时明确报错，不自动抢占或杀进程。

“服务运行中”表示本进程的线程/监听状态，不代表QQ/B站已登录、模型可用或消息送达。QQ账号与主人好友继续通过独立只读核验确认；原认证/风控暂停不会被启动按钮解除，必要时排障后按原管理台恢复流程处理。

## 停止与在途工作

停止平台先关闭其配置开关并移出当前选择，再关闭相应接收服务。后续领取、工作任务重新装配和最终发送检查均使用当前选择；停止QQ不关闭B站，也不取消持久委托、删除记忆或清空待合并消息。重新启动后按原时段、期限、去重及权限规则继续，过期缓冲按原24小时规则处理。已经发往平台的HTTP请求无法撤回，unknown仍不自动重发；已经开始的生成/读取可能完成，但发送前会复验。

QQ找B站视频仍需启用并启动B站内容模块。只启动QQ可以私聊，但不会为了找视频隐式启动B站。停止目标投递平台后，相关outbox继续等待原有效期和调度规则。

## 模块边界与扩展

```text
管理台serve
  ReceiverManager
    QQ: HTTPReceiver → 原签名处理器 → 持久chat_inbox
    B站: polling标记 → 原PlatformModule.poll/poll_private
    唯一worker_loop → WorkerCoordinator → 原共享内核/outbox
```

`bootstrap/receivers.py`中的`ReceiverSpec`注册传输模式与可选长期接收器工厂。B站轮询继续由现有协调器驱动，不新增轮询器或AI决策循环；QQ工厂惰性导入原接收器。新增推送平台时，注册具备`start()`、`is_alive()`、`close()`的生命周期对象，并在`bootstrap/platforms/`注册其业务模块和配置校验。接收器只收件持久化，不调用模型或直接发送。未来WebSocket重连属于对应传输实现；本轮没有实现微信、WebSocket或同平台多账号。

`worker_config`按“本地配置开启 ∩ 本进程已选择”构造运行配置；同一加载器传给轮询、协调器的消息/审核/发送/维护任务及Runtime最终策略，避免任务重新装配时重新带起未选择平台。模型、记忆、视觉和全局串行sender仍只有一套，不引入微服务。

鉴权接口：`GET /api/receivers`返回本进程状态；`POST /api/receivers`仅接受`platform`和`operation: start|stop`。沿用本机管理token、Host/Origin检查，不接受进程命令或凭证参数。状态不返回密钥或原始异常。

验证边界见[开发日志](DEVELOPMENT_LOG.md)和[验证记录](VALIDATION.md)：使用隔离配置/数据库、模拟模型/平台及本机签名HTTP，未将正式平台常驻启用或执行真实发送作为开发测试。
