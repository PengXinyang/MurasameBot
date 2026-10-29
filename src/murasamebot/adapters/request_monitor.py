from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import re
import logging
import sqlite3
import time

import httpx

from murasamebot.domain.models import PlatformError


POLL_CHANNELS={"notifications","private_inbox","owner_feed","owner_history","exploration"}


def channel_for(path):
    if "/msgfeed/" in path:return "notifications"
    if "get_sessions" in path or "fetch_session_msgs" in path:return "private_inbox"
    if "feed/space" in path:return "owner_feed"
    if "history/cursor" in path:return "owner_history"
    if "newlist" in path or "/search/" in path:return "exploration"
    if path.endswith("/nav"):return "identity"
    if path=="/x/relation":return "relations"
    if "send_msg" in path:return "private_send"
    if "/qrcode/" in path:return "login"
    return "content"


class RequestMonitor:
    def __init__(self,store,settings,role="ai",*,clock=time.time,sleep=time.sleep):
        self.store,self.settings,self.role=store,settings,role
        self.clock,self.sleep=clock,sleep
        self.essential=False

    @contextmanager
    def essential_reads(self):
        previous=self.essential
        self.essential=True
        try:yield
        finally:self.essential=previous

    def allowed(self,channel,now=None):
        now=self.clock() if now is None else now
        if self.store.request_cooldown(self.scope(channel)).get("until",0)>now:return False
        return self.essential or channel not in POLL_CHANNELS or not self.store.polling_quiet(now,self.settings.quiet_before_seconds,self.settings.quiet_after_seconds)

    def before(self,request):
        if request.method!="GET":return
        channel=channel_for(request.url.path)
        if not self.allowed(channel):
            retry=max(self.clock()+1,self.store.request_cooldown(self.scope(channel)).get("until",0))
            raise PlatformError("读取暂缓：通道冷却或计划发送静默窗口",code="read_deferred",platform="bilibili",retry_at=retry)
        slot=self.store.request_slot(self.clock(),self.settings.read_gap_seconds,self.settings.read_max_wait_seconds)
        if slot is None:
            raise PlatformError("读取暂缓：共享读取额度暂忙",code="read_deferred",platform="bilibili",retry_at=self.clock()+5)
        delay=max(0,slot-self.clock())
        if delay:self.sleep(delay)
        if not self.allowed(channel):
            raise PlatformError("读取暂缓：等待期间通道状态已变化",code="read_deferred",platform="bilibili",retry_at=self.clock()+5)

    def retry_at(self,path):
        return self.store.request_cooldown(self.scope(channel_for(path))).get("until",0)

    def scope(self,channel):
        return channel if self.role=="ai" else self.role+"."+channel

    def record(self,request,response,start,error=""):
        now=self.clock()
        code=None
        if response is not None and "json" in response.headers.get("content-type",""):
            try:
                response.read()
                value=response.json()
                candidate=value.get("code") if isinstance(value,dict) else None
                if type(candidate) is int or isinstance(candidate,str) and re.fullmatch(r"-?\d{1,10}",candidate):code=str(candidate)
            except (ValueError,httpx.HTTPError):pass
        status=response.status_code if response is not None else None
        limited=request.method=="GET" and (code=="-509" or status==429)
        retry=0
        if limited:
            value=response.headers.get("retry-after","")
            try:retry=float(value)
            except ValueError:
                try:retry=max(0,parsedate_to_datetime(value).timestamp()-now)
                except (ValueError,TypeError,OverflowError):pass
        record={"role":self.role,"channel":self.scope(channel_for(request.url.path)),"method":request.method,
            "host":request.url.host,"path":re.sub(r"/\d+(?=/|$)","/:id",request.url.path)[:200],
            "created_at":now,"started_at":start,"duration_ms":max(0,int((now-start)*1000)),"http_status":status,"api_code":code,"error":error}
        s=self.settings
        try:
            self.store.request_result(record,limited=limited,base=s.backoff_seconds,maximum=s.backoff_max_seconds,
                                      retry_after=min(86400,max(0,retry)),retention=s.log_retention_days,max_rows=s.log_max_rows)
        except sqlite3.Error:
            logging.getLogger("murasame").warning("接口诊断写入暂忙；保留原平台响应，不重放请求")


class MonitoredTransport(httpx.BaseTransport):
    def __init__(self,inner,monitor):self.inner,self.monitor=inner,monitor
    def handle_request(self,request):
        self.monitor.before(request)
        start=self.monitor.clock()
        try:response=self.inner.handle_request(request)
        except Exception as exc:
            self.monitor.record(request,None,start,type(exc).__name__)
            raise
        self.monitor.record(request,response,start)
        return response
    def close(self):self.inner.close()
