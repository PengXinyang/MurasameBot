# Docker与NapCat部署

2026-11-01。已交付构建/编排/初始化配置；后续排障中Engine已可访问，成功拉取Python基础镜像、构建bot镜像，并通过无网络/无私密挂载的Linux非root初始化检查。管理台真实启动、QQ扫码/收发和长期运行仍未由这些检查验收；早期“Engine未启动”的记录保留为历史。

## 组件和边界

- Dockerfile基于Python 3.12 slim，安装项目及扫码依赖，以10001非root用户运行。构建上下文采用.dockerignore允许列表，不包含本机.env、config.local.yml、凭证、数据库、聊天记录或备份。两个角色项目文件及三张已批准的管理台装饰图随镜像提供；其他可选图片按需迁入卷，不整包复制角色衣装目录。不要把私人追加配置放入公开人设文件。
- compose.yml运行单个bot，持久目录/workspace使用bot-state命名卷，存配置、角色、SQLite/WAL、凭证及管理token。首次只初始化缺失文件；重建镜像不覆盖已有配置或人设，不自动迁移本机数据。
- 默认仅启动管理台，两平台在首次生成的config.local.yml中均关闭；工作循环由管理台人工开启。无人值守启动可在本机Compose覆盖文件将bot.command设为[serve, --with-worker]，它会按已配置的平台开关启动唯一协调器，不应再同时执行run或第二个worker容器。
- MURASAME_CONTAINER_BIND=1只在镜像内使管理台监听容器0.0.0.0，原Host/Origin/token校验仍保留。宿主仅绑定127.0.0.1:8766，不直接开放公网；浏览器用localhost/127.0.0.1。检查健康只代表管理台HTTP可用，不证明模型、工作循环或QQ已登录。
- NapCat使用独立容器与独立QQ/配置/插件命名卷，通过network_mode: service:bot共用网络命名空间，双方127.0.0.1可互通。QQ API仍为本机3000、签名回调仍为本机8767，不放宽本项目QQ URL校验，不发布3000/8767。NapCat WebUI通过bot的宿主回环6099访问；bot没有挂Docker socket。

上游镜像和持久目录依据[NapCat官方部署说明](https://napneko.github.io/guide/boot/Shell)与[NapCat-Docker](https://github.com/NapNeko/NapCat-Docker)。Compose命名空间和端口规则见[Docker服务配置](https://docs.docker.com/reference/compose-file/services/)。latest用于首次示例，正式验收后在.env的NAPCAT_IMAGE锁定验证过的版本或digest；未虚构一个已验证版本。

## 首次启动

Windows当前本机已填好deploy/local时，直接双击根目录Start-Docker.cmd。公共逻辑在deploy/start-docker.ps1：仅校验可用-ValidateOnly，正常启动尝试唤起Docker Desktop、确认Linux Engine、使用三份Compose和私密env构建/启动，等两页可用后打开带token的机器人管理台与NapCat。-NoBrowser保留控制台链接而不打开浏览器。缺配置/Engine不兼容/构建失败即停止，不生成新账号密钥、不覆盖本地配置、不启动AI worker；首次扫码与管理台启动平台仍需人工完成。关闭命令窗口不会停止已在后台运行的容器。

在仓库根目录操作，先启动Docker Desktop的Linux containers/Engine。Linux服务器使用Docker Engine及Compose插件；不需在宿主安装Python或Node。

1. 参考deploy/docker.env.example，把模型、主人UID等填进本机.env；已有.env请合并，不覆盖。只启用B站时可不填NapCat项；启用NapCat时生成自己的WEBUI_TOKEN。这个密码和OneBot API token、回调签名密钥是三种不同用途。
2. 检查并构建：

```sh
docker compose config --quiet
docker compose build
docker compose up -d
docker compose logs bot
```

日志包含本机管理台token链接，请保存在本机。默认地址http://127.0.0.1:8766。卷内web.port保持8766。改宿主端口时用MURASAME_WEB_PORT；日志内默认端口仍是容器8766，浏览器改用发布的宿主端口并保留token片段。

3. 开关、QQ账号与密钥等保存在卷内/workspace/config.local.yml。可复制出来编辑再放回；只用本机忽略目录保存：

```sh
mkdir -p deploy/local
docker compose cp bot:/workspace/config.local.yml deploy/local/config.local.yml
# 编辑本机文件后：
docker compose cp deploy/local/config.local.yml bot:/workspace/config.local.yml
docker compose restart bot
```

Windows PowerShell用New-Item -ItemType Directory -Force deploy/local替代mkdir -p。从主机cp回去后确认容器10001用户仍可读取/更新配置；若Linux主机产生root所有权，使用一次有界修复：docker compose exec --user root bot chown 10001:10001 /workspace/config.local.yml。不要递归放开整个卷权限。

初始两个平台关闭，不能只点启动而不填平台身份。B站需将platforms.bilibili.enabled改true，配置绑定，再通过管理台扫码AI账号；主人历史使用另一个授权入口。容器扫码可行：二维码展示在浏览器，由手机确认，凭证写卷，容器不需要桌面。只读doctor可用docker compose exec bot python /opt/murasame/run.py --config /workspace/config.yml doctor；加--online会进行只读平台核验，不代表发送验收。

## NapCat与QQ

在.env填写NAPCAT_WEBUI_TOKEN后：

```sh
docker compose -f compose.yml -f compose.napcat.yml config --quiet
docker compose -f compose.yml -f compose.napcat.yml up -d --build
```

打开http://127.0.0.1:6099，输入WebUI token，由用户本人扫码登录AI的QQ。MODE=http作为初始化提示；已有NapCat配置优先，必须在实际WebUI确认以下两项，不假设环境变量自动覆盖旧配置：

| NapCat设置 | 值 |
| --- | --- |
| OneBot HTTP服务端 | 启用；监听127.0.0.1或0.0.0.0，端口3000；access token与本项目qq.access_token相同 |
| HTTP事件上报客户端 | http://127.0.0.1:8767/onebot/qq；Array消息；关闭自身发言上报；token与本项目qq.callback_secret相同，用于X-Signature HMAC-SHA1 |

卷内config.local.yml填入以下内容并保留其他已有设置（示例不是实际账号/密钥）：

```yaml
platforms:
  bilibili: {enabled: false}
  qq: {enabled: true}
qq:
  api_url: http://127.0.0.1:3000
  robot_id: '机器人QQ号'
  access_token: '自己生成的API密钥'
  callback_secret: '自己生成的回调签名密钥'
bindings:
  - person_id: owner
    platform: qq
    external_id: '主人QQ号'
    display_name: ''
    purpose: owner_video_recommendation
```

替换数字占位符后重启bot。只读核验：docker compose exec bot python /opt/murasame/run.py --config /workspace/config.yml qq-check。确认机器人身份和主人好友，再从管理台启动QQ；这个入口同时管理接收器与唯一worker，不另开qq-listen。群聊、主动私信、QQ空间等仍受各自开关/白名单/审核控制，不因容器化自动开启。重建bot容器后，NapCat必须重建以加入新的网络命名空间；升级请对同一组Compose文件执行down（不加-v）再up -d --build，期间存在停机时间。

桌面NapCat与容器NapCat不要同时登录同一机器人。若保留Windows桌面NapCat，当前localhost限定接口不支持直接填host.docker.internal，本配置不采用放开QQ地址的捷径；应继续让整套bot在宿主运行，或使用本方案一并容器化。不要为此公开3000端口。

## 7890、模型服务与音视频

容器127.0.0.1是容器自身。宿主代理或模型服务用host.docker.internal；Compose为Linux添加host-gateway，Docker Desktop也提供该域名。网络概念见[Docker Desktop文档](https://docs.docker.com/desktop/features/networking/)。宿主服务必须允许来自容器的连接；代理仅监听宿主回环时可能仍不可达，需调整代理监听/防火墙，不能仅靠改域名保证连通。

初始关闭代理。需要时在卷内network开启proxy_enabled、地址http://host.docker.internal:7890，保留proxy_mode: gfwlist和国内直连规则。不要给NapCat全局设置代理，不使用network all模式替代原选择性路由。镜像拉取、pip构建下载使用Docker Engine/构建器自己的代理设置，与bot运行时分流是两条链路。

本配置只部署文字/图片、平台API与视频预览等无桌面能力。当前QQ通话依赖Windows原生QQ窗口、声卡和可选虚拟摄像头；默认qq_calls关闭，镜像不装sounddevice/pyvirtualcam、不挂声卡/摄像头，不声称Linux容器能够运行这条桌面通话链。要通话仍在Windows宿主运行bot，后续需要设计有鉴权的独立媒体桥。ASR/GPT-SoVITS服务可独立部署，但只配置网址不等于音频线路或QQ接通已完成。

## 更新、数据与故障检查

- 更换镜像后已有/workspace配置/人设不会自动覆盖；需要新默认值时对照版本手动合并。不要用更新镜像清空记忆。
- SQLite与WAL必须在同一持久卷，不使用网络盘共享数据库。bot保持一个副本，不scale worker；本机旧进程与容器不能同时使用同一份数据库。
- 迁移本机数据前先停旧worker并做一致性备份；不能只复制运行中的sqlite3而丢失WAL。将凭证、设备缓存、管理token和相对路径一起核对，Windows绝对路径不能直接带到Linux。此配置不自动迁移正式数据。
- down保留卷；down -v会删除记忆/凭证以及NapCat登录态，本轮不执行。备份所有相关卷时先停止写入，备份属于私密材料。需要无停机备份的专门工具仍未实现。
- 管理台健康但无消息时检查平台开关、接收服务、工作循环、登录/好友、暂停及回调签名。健康检查不检查NapCat或模型，不因不健康状态自动重放POST。docker compose logs可能含管理token或上游NapCat会话信息，不直接公开日志。

本次已验证配置、幂等初始化、真实镜像构建及无网络Linux初始化；实际管理台启动、首次扫码、升级卷保留、容器互通、QQ收发、真实代理和Linux长期运行仍待实机验收。

## 基础镜像OAuth证书错误

若FROM python:3.12-slim阶段访问auth.docker.io出现x509: certificate signed by unknown authority，失败发生在依赖镜像解析，修改应用Cookie或在应用镜像内装证书不能修复发生在FROM之前的请求。先区分Engine与宿主/BuildKit路径，不将成功下载另一个镜像视为所有请求路径都正常。

本机一次排障中：Windows直连认证站点收到不受信任的证书链，具体拦截/签发来源未确认；Engine的docker pull python:3.12-slim却成功，Docker Hub Secure=true。预拉取后Compose构建完整成功。启动脚本现在检查基础镜像缓存，缺失时先通过Engine正常拉取，拉取失败即停止；不使用insecure-registry、不关闭TLS验证、不导入未知根证书。此顺序是已验证的构建路径调整，不代表Windows信任库已修复或所有环境都可恢复。

遇到持续证书问题，应核对网络/代理和可信CA来源；如果确实需要组织代理CA，按[Docker官方证书说明](https://docs.docker.com/engine/network/ca-certs/)由操作者确认来源后处理。本项目不自动信任从失败握手中取得的证书。
