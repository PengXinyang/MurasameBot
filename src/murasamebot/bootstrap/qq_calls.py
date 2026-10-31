"""Management assembly for an explicitly connected QQ owner call."""
import importlib.util
import time

from murasamebot.bootstrap.runtime import prepare_store
from murasamebot.adapters.persistence.calls import CallSessions
from murasamebot.adapters.platforms.qq import QQAdapter
from murasamebot.adapters.media.qq_calls import NativeCall,WindowsCallWindows


class CallManager:
    def __init__(self,path,loader):self.path,self.loader,self.io=path,loader,None
    def status(self):
        config=self.loader(self.path);s=config.data['qq_calls']
        active=bool(self.io and self.io.alive())
        return {'enabled':s['enabled'],'video_enabled':s['video_enabled'],'active':active,
            'mode':self.io.session['mode'] if active else '',
            'error':self.io.error if self.io else '',
            'audio_dependency':importlib.util.find_spec('sounddevice') is not None,
            'asr_configured':bool(s['asr_url'] and s['asr_model']),
            'tts_configured':bool(s['tts_url'] and s['ref_audio_path'])}
    def control(self,data):
        operation=data.get('operation')
        if operation=='status':return self.status()
        if operation=='windows':return WindowsCallWindows().windows()
        if operation=='devices':
            try:
                import sounddevice
                return [{'index':n,'name':d['name'],'inputs':d['max_input_channels'],'outputs':d['max_output_channels']} for n,d in enumerate(sounddevice.query_devices())]
            except ImportError:raise ValueError('请先安装本项目voice可选依赖') from None
        if operation=='stop':self.close();return self.status()
        if operation!='start' or data.get('owner_confirmed') is not True:raise ValueError('请先在QQ接通并确认对端是主人，再明确绑定本次窗口')
        config=self.loader(self.path);s=config.data['qq_calls']
        if 'qq' not in config.enabled_platforms:raise ValueError('请先在管理台启动QQ平台服务')
        if not s['enabled'] or not all(s[k] for k in ('capture_device','playback_device','asr_url','asr_model','tts_url','ref_audio_path')):raise ValueError('请先补齐本地通话开关、设备及ASR/GPT-SoVITS配置')
        window=data.get('window');mode=data.get('mode')
        if type(window) is not int or not WindowsCallWindows().info(window):raise ValueError('通话窗口无效')
        store=prepare_store(config);owner=config.data['owner_person_id'];binding=store.binding(owner,'qq')
        adapter=QQAdapter(config.data['qq'],store,owner)
        try:
            if not binding or not adapter.allows_private_reply(adapter.account_id,binding['external_id']):raise ValueError('机器人身份或主人好友关系未确认')
        finally:adapter.close()
        self.close();sessions=CallSessions(store,owner)
        session=sessions.start(config.data['qq']['robot_id'],binding['external_id'],s,mode,window,time.time())
        def current():
            cfg=self.loader(self.path);policy=dict(cfg.data['qq_calls'])
            if 'qq' not in cfg.enabled_platforms or cfg.data['qq']['robot_id']!=session['account'] or cfg.data['owner_person_id']!=owner:policy['enabled']=False
            return policy
        try:
            self.io=NativeCall(store,session,s,current,owner=owner);self.io.start()
        except ImportError:
            sessions.stop(session['id']);self.close();raise ValueError('通话可选依赖未安装，请查看QQ_CALLS.md') from None
        except Exception:
            sessions.stop(session['id']);self.close();raise ValueError('通话媒体未启动，请检查窗口、音频设备和可选摄像头配置') from None
        return self.status()
    def close(self):
        if self.io:self.io.close();self.io=None
