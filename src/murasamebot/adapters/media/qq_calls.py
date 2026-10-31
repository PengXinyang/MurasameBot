"""Windows media transport only. Chat decisions stay in WorkerCoordinator."""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from queue import Queue,Empty,Full
from threading import Event,Thread,RLock
import base64
import os
import time

from murasamebot.adapters.media.speech import Segmenter,SpeechHTTP
from murasamebot.adapters.persistence.calls import CallSessions
from murasamebot.domain.models import ImageInput,MediaError,Receipt

_live={};_lock=RLock()


def lookup(store,key):
    with _lock:return _live.get((str(store.path.resolve()),store.agent_id,key))


class WindowsCallWindows:
    @staticmethod
    def info(handle):
        if os.name!='nt':raise ValueError('QQ通话窗口采集目前只支持Windows')
        import ctypes as c
        from ctypes import wintypes as w
        hwnd=w.HWND(handle);user=c.windll.user32;kernel=c.windll.kernel32
        if not user.IsWindow(hwnd):return None
        title=c.create_unicode_buffer(1024);user.GetWindowTextW(hwnd,title,1024)
        pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,c.byref(pid))
        kernel.OpenProcess.restype=w.HANDLE
        kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD]
        process=kernel.OpenProcess(0x1000,False,pid.value)
        if not process:return None
        try:
            path=c.create_unicode_buffer(32768);size=w.DWORD(len(path))
            kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,c.POINTER(w.DWORD)]
            if not kernel.QueryFullProcessImageNameW(process,0,path,c.byref(size)):return None
            if Path(path.value).name.casefold()!='qq.exe' or '通话' not in title.value:return None
            return {'handle':int(handle),'title':title.value,'pid':pid.value}
        finally:
            kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle(process)

    def windows(self):
        if os.name!='nt':return []
        import ctypes as c
        from ctypes import wintypes as w
        result=[]
        @c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        def visit(hwnd,_):
            info=self.info(hwnd)
            if info:result.append(info)
            return True
        c.windll.user32.EnumWindows(visit,0)
        return result

    def frame(self,handle):
        if not self.info(handle):raise MediaError('通话窗口已失效')
        from PIL import ImageGrab
        image=ImageGrab.grab(window=handle).convert('RGB');image.thumbnail((1280,720))
        data=BytesIO();image.save(data,format='JPEG',quality=80)
        return data.getvalue(),image.width,image.height


class FrameReader:
    def __init__(self,store):self.store=store
    @contextmanager
    def batch(self,_):yield
    def load(self,ref,limit):
        row=CallSessions(self.store).for_event(ref.url)
        io=lookup(self.store,row['id']) if row else None
        if not io or not io.alive(True) or not io.frame or time.time()-io.frame[0]>5 or row['input_revision']!=row['revision']:raise MediaError('无新鲜通话画面')
        _,data,width,height=io.frame
        if len(data)>limit:raise MediaError('通话画面超限')
        return ImageInput('data:image/jpeg;base64,'+base64.b64encode(data).decode('ascii'),sha256(data).hexdigest(),len(data),False,width,height)


class NativeCall:
    def __init__(self,store,session,settings,current,windows=None,audio=None,speech_factory=SpeechHTTP,owner='owner'):
        self.store,self.session,self.settings,self.current=store,session,settings,current
        self.repo=CallSessions(store,owner);self.windows=windows or WindowsCallWindows()
        self.stop_event=Event();self.queue=Queue(maxsize=2);self.threads=[];self.error='';self.frame=None
        self._checked=0;self._valid=False;self.input_stream=None;self.output_stream=None;self.last_audio=time.monotonic();self.audio=audio;self.speech_factory=speech_factory;self.identity=self.windows.info(session['window'])

    def alive(self,force=False):
        if self.stop_event.is_set():return False
        if not force and time.monotonic()-self._checked<.2:return self._valid
        info=self.windows.info(self.session['window'])
        self._valid=(not self.stop_event.is_set() and info and self.identity and info['pid']==self.identity['pid']
            and self.repo.active(self.repo.get(self.session['id']),self.current(),time.time()))
        self._checked=time.monotonic()
        return self._valid

    def start(self):
        if self.audio is None:
            import sounddevice
            self.audio=sounddevice
        if not self.identity:raise ValueError('需选定有效的QQ通话窗口')
        for key in ('capture_device','playback_device'):
            if not self.settings[key]:raise ValueError('请明确配置隔离的录音和播放设备')
        if self.settings['capture_device']==self.settings['playback_device']:raise ValueError('录音和播放需为隔离音频端点')
        # Probe both directions before starting any recording thread.
        self.audio.check_input_settings(device=self.device('capture_device'),channels=1,dtype='int16',samplerate=16000)
        self.audio.check_output_settings(device=self.device('playback_device'),channels=1,dtype='int16',samplerate=32000)
        with _lock:_live[(str(self.store.path.resolve()),self.store.agent_id,self.session['id'])]=self
        self.last_audio=time.monotonic()
        targets=[self.capture,self.recognize,self.watchdog]
        if self.session['mode']=='video' and self.settings['avatar_image']:targets.append(self.camera)
        for target in targets:
            thread=Thread(target=self.run,args=(target,),daemon=True,name='murasame-call-media');thread.start();self.threads.append(thread)

    def run(self,target):
        try:target()
        except Exception:self.error='媒体或语音服务失败，请检查配置/设备';self.stop_event.set()
        finally:
            if self.stop_event.is_set() or not self.alive():
                self.stop_event.set();self.repo.stop(self.session['id']);self.frame=None
                self.abort_streams()

    def device(self,key):
        value=self.settings[key]
        return int(value) if value.isascii() and value.isdigit() else value

    def abort_streams(self):
        for stream in (self.input_stream,self.output_stream):
            if stream:
                try:stream.abort()
                except Exception:pass

    def watchdog(self):
        while self.alive():
            if time.monotonic()-self.last_audio>3:raise ValueError('音频设备停止提供数据')
            self.stop_event.wait(.5)

    def capture(self):
        segmenter=Segmenter(self.settings);revision=0
        with self.audio.RawInputStream(device=self.device('capture_device'),samplerate=16000,channels=1,dtype='int16',blocksize=320) as stream:
            self.input_stream=stream
            while self.alive():
                pcm,overflow=stream.read(320)
                self.last_audio=time.monotonic()
                if overflow:raise ValueError('录音溢出，拒绝不完整转写')
                started,chunk=segmenter.feed(bytes(pcm))
                if started:revision=self.repo.interrupt(self.session['id'])
                if chunk:
                    try:self.queue.put_nowait((revision,chunk))
                    except Full:
                        try:self.queue.get_nowait()
                        except Empty:pass
                        self.queue.put_nowait((revision,chunk))

    def recognize(self):
        speech=self.speech_factory(self.settings);attempts=0
        try:
            while self.alive():
                try:revision,pcm=self.queue.get(timeout=.2)
                except Empty:continue
                if self.repo.get(self.session['id'])['revision']!=revision:continue
                attempts+=1
                if attempts>100:self.stop_event.set();break
                text=speech.asr(pcm)
                if not self.alive() or self.repo.get(self.session['id'])['revision']!=revision:continue
                if self.session['mode']=='video':
                    data,width,height=self.windows.frame(self.session['window']);self.frame=(time.time(),data,width,height)
                self.repo.ingest(self.session['id'],revision,text,self.current(),time.time())
        finally:speech.close()

    def camera(self):
        from PIL import Image
        import numpy as np
        import pyvirtualcam
        frame=np.asarray(Image.open(self.settings['avatar_image']).convert('RGB').resize((1280,720)))
        with pyvirtualcam.Camera(width=1280,height=720,fps=10) as camera:
            while self.alive():camera.send(frame);camera.sleep_until_next_frame()

    def send(self,action,before_write=None):
        if not self.alive(True) or self.repo.guard(action,self.current(),time.time()):return Receipt('failed',detail='通话不可用')
        speech=self.speech_factory(self.settings);written=False
        try:
            pcm,rate,channels=speech.tts(action.text)
            if not self.alive(True) or self.repo.guard(action,self.current(),time.time()) or (before_write and not before_write()):return Receipt('failed',detail='合成后通话已变化')
            with self.audio.RawOutputStream(device=self.device('playback_device'),samplerate=rate,channels=channels,dtype='int16') as stream:
                self.output_stream=stream
                step=max(2,rate//20*channels*2)
                for index in range(0,len(pcm),step):
                    if not self.alive(True) or self.repo.guard(action,self.current(),time.time()):return Receipt('unknown' if written else 'failed',detail='通话播放被打断，禁止自动重播')
                    written=True
                    if stream.write(pcm[index:index+step]):raise ValueError('播放缓冲欠载')
            return Receipt('accepted' if written else 'failed',action.action_id,'完整语音已写入所选设备；对端实际听到待确认')
        except Exception:return Receipt('unknown' if written else 'failed',detail='通话合成/播放失败；不自动重播')
        finally:speech.close()

    def close(self):
        self.stop_event.set();self.repo.stop(self.session['id']);self.frame=None
        self.abort_streams()
        for thread in self.threads:thread.join(timeout=.5)
        with _lock:_live.pop((str(self.store.path.resolve()),self.store.agent_id,self.session['id']),None)
