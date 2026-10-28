"""Admin-only owner QR sessions; never reuse the AI account adapter."""
from io import BytesIO
from threading import RLock
import time

from murasamebot.adapters.platforms.bilibili_history import BilibiliOwnerHistory
from murasamebot.application.owner_history import OwnerHistoryService, OwnerHistorySettings
from murasamebot.domain.models import PlatformError


def history_source(config):
    path = config.root / ".secrets" / "bilibili-owner.json"
    if path.resolve() == (config.root/config.data["bilibili"]["credentials"]).resolve():
        raise ValueError("主人观察凭证不能与 AI 发布凭证共用")
    return BilibiliOwnerHistory(path,proxy_url=config.network_proxy)


def history_service(config, store, source):
    return OwnerHistoryService(store,source,OwnerHistorySettings(**config.data["owner_history"]),config.data["owner_person_id"],"bilibili")


class OwnerLoginManager:
    def __init__(self):
        self.lock = RLock()
        self.pending = None

    def cancel(self):
        with self.lock:
            if self.pending:
                self.pending["source"].close()
                self.pending = None

    def start(self, config, store):
        with self.lock:
            self.cancel()
            binding = store.binding(config.data["owner_person_id"],"bilibili")
            if not binding or binding["external_id"] == str(config.data["bilibili"].get("robot_uid") or ""):
                raise ValueError("请先绑定独立的主人账号")
            try:
                import qrcode
            except ImportError:
                raise ValueError('请先安装扫码依赖：python -m pip install -e ".[login]"') from None
            source = history_source(config)
            try:
                url,key = source.start_login()
                image = BytesIO()
                qrcode.make(url).save(image,format="PNG")
                self.pending = {"source":source,"key":key,"binding":binding,"expires":time.time()+180,"png":image.getvalue()}
            except BaseException:
                source.close()
                raise
            return {"state":"waiting","expires_at":self.pending["expires"],"uid":binding["external_id"]}

    def image(self):
        with self.lock:
            if not self.pending or self.pending["expires"] < time.time():
                self.cancel()
                raise ValueError("请重新生成二维码")
            return self.pending["png"]

    def poll(self, config, store):
        with self.lock:
            if not self.pending or self.pending["expires"] < time.time():
                self.cancel()
                return {"state":"expired"}
            binding = store.binding(config.data["owner_person_id"],"bilibili")
            if binding != self.pending["binding"]:
                self.cancel()
                raise ValueError("主人绑定已变化，请重新扫码")
            try:
                state = self.pending["source"].poll_login(self.pending["key"],binding)
                if state == "success":
                    store.owner_history_reset(binding["person_id"],time.time(),forget=False)
                    config.save_local({"owner_history":{"enabled":True}})
                if state in {"success","expired","error"}:
                    self.cancel()
                return {"state":state}
            except (PlatformError,ValueError,OSError):
                self.cancel()
                raise

    def revoke(self, config, store):
        with self.lock:
            self.cancel()
            store.owner_history_reset(config.data["owner_person_id"],time.time())
            source = history_source(config)
            try:
                source.path.unlink(missing_ok=True)
            finally:
                source.close()
            config.save_local({"owner_history":{"enabled":False}})


def history_status(config,store):
    person = config.data["owner_person_id"]
    state = store.owner_history_state(person)
    binding = store.binding(person,"bilibili")
    valid = False
    source = history_source(config)
    try:
        source.authorization(binding)
        valid = True
    except (ValueError,OSError,PlatformError):
        pass
    finally:
        source.close()
    return {"enabled":config.data["owner_history"]["enabled"],"login_saved":valid,
        "uid":binding["external_id"] if binding else "",**{k:state.get(k) for k in
        ("state","last_attempt","last_success","warning","error","baseline_at")},
        "recommendation": {k:v for k,v in store.get_setting("owner_history_recommendation:"+person,{}).items() if k in {"day","attempts","at","state"}},
        "records":store.owner_history_rows(person,time.time()) if valid else []}
