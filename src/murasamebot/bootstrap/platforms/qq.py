from murasamebot.bootstrap.platforms import PlatformModule


DEFAULTS = {"api_url":"http://127.0.0.1:3000", "robot_id":"", "access_token":"",
    "callback_secret":"", "quiet_seconds":30, "max_wait_seconds":180, "max_reply_messages":3,
    "callback_port":8767}


def validate(settings):
    from urllib.parse import urlparse
    if not isinstance(settings,dict) or set(settings)-DEFAULTS.keys():
        raise ValueError("未知QQ配置")
    p = urlparse(settings["api_url"])
    if p.scheme!="http" or p.hostname not in {"127.0.0.1","localhost","::1"} or p.username or p.password or p.path not in {"","/"} or p.query or p.fragment:
        raise ValueError("QQ API需使用本机HTTP地址")
    for key,low,high in (("quiet_seconds",1,120),("max_wait_seconds",30,600),("max_reply_messages",1,5),("callback_port",1024,65535)):
        if type(settings[key]) is not int or not low<=settings[key]<=high:
            raise ValueError("QQ参数超出范围："+key)
    if settings["max_wait_seconds"]<settings["quiet_seconds"]:
        raise ValueError("QQ最长等待不能短于静默窗口")
    for key in ("robot_id","access_token","callback_secret"):
        if not isinstance(settings[key],str):
            raise ValueError("QQ账号和凭证必须为字符串")
    if settings["robot_id"] and not settings["robot_id"].startswith("${") and not settings["robot_id"].isdigit():
        raise ValueError("QQ机器人账号必须为数字")


class QQModule(PlatformModule):
    def poll_private(self, runtime, now):
        from murasamebot.adapters.persistence.chat_batches import ChatBatches
        ChatBatches(runtime.store).flush(self.adapter.settings, runtime.config.data["owner_person_id"], self.normalize_event, now)


def build_module(config, store):
    from murasamebot.adapters.platforms.qq import QQAdapter, IMAGE_HOSTS
    from murasamebot.adapters.media.http_images import HTTPImageReader
    adapter = QQAdapter(config.data["qq"],store,config.data["owner_person_id"])
    module = QQModule("qq",adapter,frozenset({"private_reply", "task_result"}),poll_seconds=1,
        private_policy={"multilingual":True,"topic_filter":False})
    module.private_image_reader = HTTPImageReader(hosts=IMAGE_HOSTS,referer="")
    module.resources = (module.private_image_reader,)
    return module
