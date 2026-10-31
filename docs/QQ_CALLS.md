# QQ语音/视频通话：分析、方案与接入

2026-10-31，群学习任务bbe0fb7已推送后开始本任务。本文件先形成设计，再实施；微信仅保留未来适配边界。

## 可借鉴部分与限制

核对[ohwhale-deploy](https://github.com/chauko0423/ohwhale-deploy)固定提交8988070a1102bebb195f622c995d77207b183672的scripts/call_agent.py、scripts/call_loop.py、QUICKSTART及manifest。它用Windows UIA找QQ通话接听按钮，系统音频采集→ASR→模型→GPT-SoVITS→VB-CABLE送入QQ麦克风；不是OneBot标准通话API。它的电话默认直接调用模型，不能据此认定复用了跨天记忆；视频虚拟摄像头存在尚未解决的客户端兼容问题。

借鉴音频分层、VAD/打断、明确设备、可选画面；不复制角色、权重或脚本，不替换现用NapCat，不把“找到按钮”当作对端身份核验，也不声称修好了QQ虚拟摄像头。当前采用手动在QQ接通主人语音/视频，再在管理台绑定本次窗口并开启媒体会话，不自动接听陌生来电或主动拨号。

## 本项目链路

保留QQ客户端负责原生呼叫/视频传输。管理台启动一个本机媒体收发服务，选定QQ.exe的通话窗口、输入/输出设备和已核验主人；它只做VAD/ASR/画面采集及播放，不含第二AI循环。转写按真实主人来源入同库事件，仍交唯一WorkerCoordinator、共同MemoryService/模型/审核/outbox。专用call_reply只送当前通话设备，不能退回普通私聊文本。新语音开始即推进会话修订，取消旧生成/播放；停止、断窗、失去绑定/好友、配置或记忆变化后拒绝旧回复，不在重启时续播。

ASR采用可配置的OpenAI兼容/audio/transcriptions端点；GPT-SoVITS按官方api_v2.py的POST /tts接口，返回PCM WAV，地址/令牌/服务器参考音频路径全部留空。语音段最长12秒，短暂静默后识别；首版整段HTTP，不承诺流式低延迟。原始音频只在内存，不写录音文件；转写/已实际完成播放的文字按私聊用途归档，部分播放或未知结果不算完整发言，不自动重播。

视频模式只截取操作者指定且校验属于QQ.exe的通话窗口，复用VisionService与现有视觉模型；不截取整个桌面，不把窗口帧当成看完整段视频。可选把本地角色图通过pyvirtualcam送到已安装的虚拟摄像头；仍须在QQ手动选择该设备，不能把本机成功发送帧当作对方看到。

## 需要的本地条件

- Windows QQ客户端、已登录NapCat及QQ主人好友绑定；原管理台工作循环保持运行。
- 可选依赖sounddevice；视频输出另需pyvirtualcam及兼容虚拟摄像头驱动，按官方安装。程序不自动安装驱动、不修改系统默认设备、不切换现有QQ套件。
- 建议用两个隔离虚拟音频通道：QQ扬声器→输入线A→本程序采集；本程序TTS→输出线B→QQ麦克风。避免TTS被再次识别形成回声，不从默认整机混音偷选设备。
- 当前通话窗口由操作者在QQ确认对端确为主人后明确绑定，程序的好友核验不能单独证明屏幕上这一通电话的对端身份。窗口关闭即停止；QQ仍负责真正挂断/切换摄像头。

配置与操作见下文。离线模拟、原生QQ接通、真实音频回程、远程服务、画面理解及对端视频可见分别验收；当前远程地址留空，不进行真实通话测试。

参考：[GPT-SoVITS官方API](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)、[sounddevice Raw Streams](https://python-sounddevice.readthedocs.io/en/0.5.1/api/raw-streams.html)、[Pillow窗口截图](https://pillow.readthedocs.io/en/stable/reference/ImageGrab.html)。

## 填写配置

在本地config.local.yml加入以下内容（不要提交该文件）：

```yaml
qq_calls:
  enabled: false
  video_enabled: false
  capture_device: ""       # 输入线A的录音端点，设备完整名或索引字符串
  playback_device: ""      # 输出线B的播放端点，设备完整名或索引字符串
  asr_url: ""              # 兼容语音识别服务的完整 /v1/audio/transcriptions 地址
  asr_key: ""              # 识别服务要求时填写，与TTS密钥独立
  asr_model: ""            # 识别服务实际提供的模型名
  tts_url: ""              # 远程GPT-SoVITS完整 /tts 地址
  tts_key: ""              # 反向代理要求Bearer认证时填写；原生服务可能无需此项
  ref_audio_path: ""       # 远程服务器上的参考WAV绝对路径，不是本机路径
  prompt_text: ""          # 参考WAV对应的准确文字
  prompt_lang: zh
  text_lang: zh
  avatar_image: ""         # 可选：本机角色图绝对路径，不填不启用虚拟摄像头输出
  silence_ms: 800
  vad_threshold: 500
  max_utterance_seconds: 12
  session_minutes: 30
```

例如服务器通过HTTPS代理提供服务时，tts_url可填`https://voice.example.com/tts`，ref_audio_path填`/srv/gpt-sovits/reference/murasame.wav`。本机只调用推理接口，模型权重与参考音频均由远程服务预先加载/保存；不会上传本机参考音频或切换服务器权重。服务器运行GPT-SoVITS api_v2.py；若只有原始9880端口，可用安全隧道映射到本机后填`http://127.0.0.1:9880/tts`。不要把访问令牌放进URL；地址字段拒绝内嵌凭据/查询参数。

ASR是独立需求，GPT-SoVITS只负责合成。asr_url填语音识别服务的完整兼容端点、asr_model填该服务的真实模型名；POST multipart包含speech.wav和model，响应需为`{"text":"转写"}`。未配ASR时不能开始双向语音通话。text_lang按合成服务实际支持的语言填写，prompt_lang须与参考音频一致。两个服务可在同一远程服务器部署，但分别填各自地址和认证。

管理台只允许修改enabled/video_enabled，不允许通过公开配置接口改地址、密钥或设备。ASR/TTS密钥在管理接口显示时脱敏；HTTP客户端不读取系统代理认证、不跨服务复用密钥、不跟随重定向。

## 安装与一次通话

1. 在项目虚拟环境安装可选依赖：语音用`.venv/Scripts/python.exe -m pip install -e ".[voice]"`；视频用`.venv/Scripts/python.exe -m pip install -e ".[voice,call-video]"`。不会自动安装任何设备驱动或下载人物权重。
2. 安装并手动配置隔离音频线。管理台「QQ语音 / 视频通话 → 查看音频设备」列出输入/输出能力及索引；将选择的索引以字符串填入上方配置。QQ扬声器指向输入线A的播放端，本程序capture_device选对应录音端；本程序playback_device选输出线B的播放端，QQ麦克风选对应录音端。两端不能选同一个设备，不修改系统默认音频设置。
3. 启动管理台，在管理台启动QQ平台与共同工作循环。保持NapCat在线，先用QQ手动接通与主人的原生语音/视频通话。
4. 打开QQ页「QQ语音 / 视频通话」，保存通话开关；视频还需保存video_enabled。点击刷新通话窗口，选择机器人QQ对应窗口，选择语音或视频，核对对端并勾选本次确认，再点启用本次会话。窗口必须来自QQ.exe且标题含通话；这不代替操作者对实际对端/机器人进程的确认。
5. 说话后，短暂静默会触发识别与回复。原审核模式、总额度和全局发送间隔仍有效，首版不绕过原10秒默认发送间隔。输入使用16kHz单声道PCM，TTS需返回16位PCM WAV；播放与设备采样率必须兼容，否则会停止/报告失败而不选择其他设备。
6. 视频模式每段有效转写后获取一次所选窗口快照，5秒内可供当前回合视觉服务使用；过期/失效画面不提供。不是连续视频理解。可选角色图通过pyvirtualcam以1280×720/10fps送虚拟摄像头，需按其官方要求安装OBS等兼容设备，并在QQ手动选择。当前是静态图，不含口型动画，必须由对端确认看到的设备/内容。
7. 停止AI媒体或在QQ挂断。切换通话对象、窗口或机器人账号前先停止并重新绑定；程序无法从群名片/窗口昵称证明对端身份。关闭窗口、绑定/配置/记忆变化、暂停、设备不再提供数据或30分钟到期会停止；每会话最多处理100个识别段，避免噪声导致无限调用。

原始声音和窗口图像不落盘；只归档实际转写与完整写入设备的回复文字，按同一QQ私人会话加入记忆/日记/相处回顾。声卡写入成功仍不代表对端听到。新语音开始会递增会话修订并取消旧回复，最多两段待识别缓存，旧ASR结果被拒绝。部分播放记unknown且不归档完整回复、不自动重播；未产生音频的失败不会降级成普通QQ文字。识别服务网络请求无法强制中断，但迟到结果不进入会话。

停止只停止本项目媒体线程，真正挂断/接听/开启摄像头仍由QQ客户端执行。微信尚未接入，媒体适配与应用会话分开保留未来扩展边界。远程服务/音频设备/QQ客户端/虚拟摄像头需分别实机验收，本次没有连接真实设备。
