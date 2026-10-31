"""OneBot 11 owner private chat and explicitly scoped passive group chat."""
import time
from urllib.parse import urlparse

import httpx

from murasamebot.domain.models import Event, ImageRef, PlatformError, Receipt, stable_id

IMAGE_HOSTS = ("multimedia.nt.qq.com", "multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn")


class QQAdapter:
    def __init__(self, settings, store, owner_person_id, *, client=None):
        self.settings, self.store, self.owner = settings, store, owner_person_id
        self.account_id = str(settings["robot_id"])
        self.group_settings = {'enabled':False,'group_ids':[]}
        self.client = client or httpx.Client(base_url=settings["api_url"], timeout=15, trust_env=False,
            headers={"Authorization": "Bearer " + settings["access_token"]})

    @property
    def configured(self):
        return bool(self.account_id and self.settings["access_token"] and self.settings["callback_secret"])

    def api(self, action, payload=None, *, writing=False):
        if action not in {"get_login_info", "get_friend_list", "send_private_msg", "send_qzone_msg", "get_msg", "get_group_member_info", "send_group_msg"}:
            raise ValueError("unsupported QQ API")
        try:
            response = self.client.post("/" + action, json=payload or {})
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError):
            if writing:
                return None
            raise PlatformError("QQ接口不可用", platform="qq", code="qq_unavailable") from None
        if not isinstance(result, dict) or result.get("retcode") != 0 or result.get("status") != "ok":
            if writing and isinstance(result, dict) and result.get("status") == "failed":
                return {"failed": True}
            if writing:
                return None
            raise PlatformError("QQ只读检查失败", platform="qq", code="qq_check_failed")
        return result.get("data")

    def account(self):
        data = self.api("get_login_info")
        if not isinstance(data, dict) or str(data.get("user_id")) != self.account_id:
            raise PlatformError("QQ机器人账号不匹配", platform="qq", code="qq_wrong_account", pause=True)
        return {"uid": self.account_id}

    def allows_private_reply(self, account_id, peer_id):
        binding = self.store.binding(self.owner, "qq")
        if (not binding or binding["external_id"] != str(peer_id) or str(account_id) != self.account_id
                or str(peer_id) == self.account_id):
            return False
        self.account()
        friends = self.api("get_friend_list")
        return isinstance(friends, list) and any(str(f.get("user_id")) == str(peer_id) for f in friends if isinstance(f, dict))

    def parse(self, payload, now=None):
        now = time.time() if now is None else now
        if payload.get('post_type')=='message' and payload.get('message_type')=='group':
            return self.parse_group(payload,now)
        if (payload.get("post_type") != "message" or payload.get("message_type") != "private"
                or payload.get("sub_type") != "friend" or str(payload.get("self_id")) != self.account_id):
            return None
        peer = str(payload.get("user_id", ""))
        if not self.allows_private_reply(self.account_id, peer):
            return None
        segments = payload.get("message")
        if not isinstance(segments, list) or len(segments) > 100:
            raise ValueError("QQ需上报Array格式消息段")
        text, images = [], []
        for segment in segments:
            data = segment.get("data", {})
            if segment.get("type") == "text":
                text.append(str(data.get("text", "")))
            elif segment.get("type") == "image":
                url = str(data.get("url", ""))
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname in IMAGE_HOSTS and not parsed.username and parsed.port in {None, 443}:
                    images.append(ImageRef(url, len(images) + 1))
                    text.append(f"[图片{len(images)}]")
                else:
                    text.append("[图片来源不可读取]")
            else:
                text.append("[暂不支持的消息类型]")
        message_id = str(payload.get("message_id", ""))
        if not message_id or not text or len("".join(text)) > 12000 or len(images) > 9:
            raise ValueError("QQ消息标识、大小或图片数量无效")
        return Event(message_id, "qq", self.account_id, peer, f"qq:{self.account_id}:{peer}",
            "private_message", "".join(text), now, "private", images=tuple(images), reply_limit=self.settings["max_reply_messages"])

    @staticmethod
    def private_conversation(account_id, peer_id):
        return stable_id("conversation", "qq", account_id, f"qq:{account_id}:{peer_id}")

    def pending_private_activity(self, account_id, peer_id, now):
        from murasamebot.adapters.persistence.chat_batches import ChatBatches
        binding = self.store.binding(self.owner, "qq")
        if not binding or binding["external_id"] != peer_id or account_id != self.account_id:
            return {"pending": 1, "last_at": now}
        return ChatBatches(self.store).activity(account_id, peer_id, binding["version"], now)

    def send(self, action, mentions, *, before_write=None):
        if action.kind=='group_reply':
            from murasamebot.adapters.persistence.group_inbox import GroupInbox
            if (action.platform!='qq' or action.account_id!=self.account_id or mentions
                    or GroupInbox(self.store).guard(action,self.group_settings,time.time())):
                return Receipt('failed',detail='群聊发送来源或配置无效')
            source=self.store.event(action.event_id)
            self.check_group(source.thread_root,source.actor_id)
            if before_write and not before_write():return Receipt('failed',detail='群聊发送前校验失败')
            result=self.api('send_group_msg',{'group_id':int(source.thread_root),'message':[
                {'type':'reply','data':{'id':action.reply_to}},
                {'type':'at','data':{'qq':source.actor_id}},
                {'type':'text','data':{'text':' '+action.text}}]},writing=True)
            if isinstance(result,dict) and result.get('failed'):return Receipt('failed',detail='群消息明确拒绝')
            if not isinstance(result,dict) or not result.get('message_id'):return Receipt('unknown',detail='群消息无确定回执，禁止自动重发')
            return Receipt('accepted',str(result['message_id']),'群消息已受理，群内可见性待验证')
        if action.kind == "autonomous_post":
            if (action.platform!="qq" or action.account_id!=self.account_id or action.visibility!="public"
                    or action.recipient_id or mentions or action.content or action.artwork):
                return Receipt("failed",detail="QQ空间发布路由无效")
            self.account()
            if before_write and not before_write():
                return Receipt("failed",detail="发送前策略校验失败")
            result=self.api("send_qzone_msg",{"content":action.text,"images":[],"ugc_right":4,"target_uins":[]},writing=True)
            if isinstance(result,dict) and result.get("failed"):
                return Receipt("failed",detail="QQ空间接口明确拒绝；请检查网关版本及空间权限")
            tid=result.get("tid") if isinstance(result,dict) else None
            if not isinstance(tid,str) or not tid.strip():
                return Receipt("unknown",detail="未取得QQ空间确定回执，禁止自动重发")
            return Receipt("accepted",tid,"QQ空间已受理；好友可见，实际可见性待验证")
        if action.kind not in {"private_reply", "task_result", "proactive_private"} or action.platform != "qq" or mentions or not self.allows_private_reply(action.account_id, action.recipient_id):
            return Receipt("failed", detail="QQ收件人或好友权限无效")
        if before_write and not before_write():
            return Receipt("failed", detail="发送前策略校验失败")
        result = self.api("send_private_msg", {"user_id": int(action.recipient_id),
            "message": [{"type": "text", "data": {"text": action.text}}]}, writing=True)
        if result is None or not isinstance(result, dict) or not result.get("message_id"):
            return Receipt("failed" if isinstance(result, dict) and result.get("failed") else "unknown", detail="QQ未取得确定发送回执，禁止自动重发")
        return Receipt("accepted", str(result["message_id"]), "OneBot受理，客户端送达待验证")

    def close(self):
        self.client.close()

    def group_quote(self,group,mid):
        if not mid:return None
        source=stable_id('qq-group',self.account_id,group,str(mid))
        with self.store.connection() as db:
            if db.execute("SELECT 1 FROM memory_suppressed WHERE agent_id=? AND event_id=? UNION SELECT 1 FROM events WHERE agent_id=? AND event_id=? AND state='deleted'",
                    (self.store.agent_id,source,self.store.agent_id,source)).fetchone():return None
            if db.execute("""SELECT 1 FROM actions a JOIN memory_suppressed s
                    ON s.agent_id=a.agent_id AND s.event_id=json_extract(a.payload,'$.event_id')
                    WHERE a.agent_id=? AND a.kind='group_reply' AND json_extract(a.payload,'$.account_id')=?
                    AND json_extract(a.receipt,'$.external_id')=? LIMIT 1""",(self.store.agent_id,self.account_id,str(mid))).fetchone():return None
        try:data=self.api('get_msg',{'message_id':int(mid)})
        except (PlatformError,ValueError):return None
        if (not isinstance(data,dict) or data.get('message_type')!='group' or str(data.get('group_id'))!=group
                or str(data.get('message_id'))!=str(mid)):return None
        sender=data.get('sender') or {}
        if not str(sender.get('user_id','')).isdigit():return None
        try:text=self.group_text(data.get('message'))[0][:2000]
        except ValueError:return None
        return {'user_id':str(sender['user_id']),'name':str(sender.get('card') or sender.get('nickname') or '')[:120],
                'text':text,'message_id':str(mid)}

    @staticmethod
    def group_text(segments):
        text,ats,reply=[],[],''
        if not isinstance(segments,list) or len(segments)>100:raise ValueError('群消息需为有界Array格式')
        for segment in segments:
            if not isinstance(segment,dict) or not isinstance(segment.get('data',{}),dict):raise ValueError('群消息段无效')
            data=segment.get('data',{});kind=segment.get('type')
            if kind=='text':text.append(str(data.get('text','')))
            elif kind=='at':
                ats.append(str(data.get('qq','')));text.append('[提及:'+str(data.get('qq',''))+']')
            elif kind=='reply' and not reply:reply=str(data.get('id',''))
            else:text.append('[未分析的'+str(kind)[:20]+']')
        return ''.join(text),ats,reply

    def parse_group(self,payload,now):
        from murasamebot.adapters.persistence.group_inbox import GroupInbox
        s=self.group_settings;group=str(payload.get('group_id',''));peer=str(payload.get('user_id',''))
        if (not s['enabled'] or group not in s['group_ids'] or payload.get('sub_type')!='normal'
                or str(payload.get('self_id'))!=self.account_id or peer==self.account_id or not peer.isdigit()):return None
        mid=payload.get('message_id')
        if type(mid) is not int or not mid:return None
        text,ats,reply=self.group_text(payload.get('message'))
        if not text.strip() or len(text)>2000:return None
        sender=payload.get('sender') or {}
        if sender.get('user_id') is not None and str(sender['user_id'])!=peer:return None
        event=Event(stable_id('qq-group',self.account_id,group,str(mid)),'qq',self.account_id,peer,
            stable_id('qq-group-conversation',self.account_id,group),'group_message',text,now,'group',
            reply_to=reply,thread_root=group,actor_name=str(sender.get('card') or sender.get('nickname') or '')[:120],sequence=mid)
        quote=self.group_quote(group,reply) if reply and self.account_id not in ats else None
        triggered=self.account_id in ats or bool(quote and quote['user_id']==self.account_id)
        GroupInbox(self.store).receive(event,triggered,s,now)
        return None  # Group events are durably ingested directly, never private-batched.

    def check_group(self,group,peer):
        if not self.group_settings['enabled'] or group not in self.group_settings['group_ids']:
            raise PlatformError('群未授权',platform='qq',code='group_denied')
        self.account()
        for user in (self.account_id,peer):
            data=self.api('get_group_member_info',{'group_id':int(group),'user_id':int(user),'no_cache':True})
            if (not isinstance(data,dict) or str(data.get('user_id'))!=user
                    or str(data.get('group_id',group))!=group
                    or (user==self.account_id and float(data.get('shut_up_timestamp') or 0)>time.time())):
                raise PlatformError('群成员身份或发言权限无效',platform='qq',code='group_denied')
