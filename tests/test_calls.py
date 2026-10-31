from array import array
from copy import deepcopy
from dataclasses import replace
import json
import time
from types import SimpleNamespace
import httpx
import pytest

from test_qq import setup
from murasamebot.application.calls import DEFAULTS,CallConversation,validate
from murasamebot.adapters.persistence.calls import CallSessions
from murasamebot.adapters.media.speech import Segmenter,SpeechHTTP,wav_pcm,decode_wav
from murasamebot.adapters.media.qq_calls import NativeCall
from murasamebot.domain.models import Action,Event


@pytest.fixture
def call(factory,monkeypatch):
    e,s,a,w=setup(factory);e.settings=replace(e.settings,model_review=True,review_strategy='inline')
    settings={**DEFAULTS,'enabled':True,'video_enabled':True,'capture_device':'input','playback_device':'output',
        'asr_url':'https://asr.invalid/v1/audio/transcriptions','asr_model':'fake','tts_url':'https://tts.invalid/tts','ref_audio_path':'/server/voice.wav'}
    a.call_settings=settings;r=CallSessions(s);now=time.time()
    session=r.start('456','123',settings,'voice',55,now)
    windows=SimpleNamespace(info=lambda h:{'pid':10,'handle':55})
    samples=[];state={'interrupt':False,'fail':False}
    class Stream:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def write(self,data):
            samples.append(data)
            if state['interrupt']:r.interrupt(session['id'])
            if state['fail']:raise OSError('device failed')
            return False
    class Speech:
        def __init__(self,settings):pass
        def tts(self,text):return b'\0\0'*3200,16000,1
        def close(self):pass
    io=NativeCall(s,session,settings,lambda:settings,windows=windows,
        audio=SimpleNamespace(RawOutputStream=Stream),speech_factory=Speech)
    import murasamebot.adapters.media.qq_calls as media
    monkeypatch.setattr(media,'lookup',lambda store,key:io if key==session['id'] else None)
    e.sinks['qq']=a;e.routing=lambda:(('qq','456'),)
    e.event_handlers['call_message']=CallConversation(e,r,settings,a).handle
    e.action_policy=lambda act,at:r.guard(act,settings,at) if act.kind=='call_reply' else ''
    calls=[]
    def decide(op,p):
        calls.append((op,deepcopy(p)));assert op=='call_chat'
        return {'decision':'reply','text':'吾辈听到了，画画的事可以慢慢聊。','can_send':True,'review_reason':'合成审核通过'}
    e.model.decide=decide
    yield e,s,a,r,session,settings,io,samples,state,calls,now
    io.close();a.close()


def utterance(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    revision=r.interrupt(session['id'])
    event=r.ingest(session['id'],revision,'继续聊聊画画',settings,now)
    assert event and e.process_one_event(now)
    return event,Action.from_dict(json.loads(s.inspect('actions')[0]['payload']))


def test_complete_call_uses_shared_private_memory_review_outbox_and_no_text_fallback(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    source=Event('history','qq','456','123',a.private_conversation('456','123'),'private_message','之前聊过水彩画。',now-10,'private')
    s.enqueue_event(source);s.finish_event(source.event_id,'done')
    event,action=utterance(call)
    assert '之前聊过水彩画' in json.dumps(calls[0][1],ensure_ascii=False)
    assert action.review_state=='passed' and action.kind=='call_reply'
    assert e.dispatch_one(now)
    assert samples and not a.store.platform_console('qq')['buffered']['messages']
    records=s.inspect('conversation_records')
    assert any(row['role']=='agent' and row['text']==action.text for row in records)
    assert all(row['visibility']=='private' for row in records)
    assert not e.dispatch_one(now+30)


@pytest.mark.parametrize('change',['interrupt','stop','binding','epoch','settings','window'])
def test_final_gate_blocks_invalid_session(call,change):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    event,action=utterance(call)
    if change=='interrupt':r.interrupt(session['id'])
    if change=='stop':r.stop(session['id'])
    if change=='binding':s.revoke_binding('owner','qq')
    if change=='epoch':s.set_setting('memory_epoch',s.get_setting('memory_epoch',0)+1)
    if change=='settings':settings['prompt_text']='changed'
    if change=='window':io.windows.info=lambda _:None
    assert io.send(action).state=='failed' and not samples


@pytest.mark.parametrize('failure',['interrupt','fail'])
def test_speech_start_interrupts_partial_playback_without_archiving_full_reply(call,failure):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    event,action=utterance(call);state[failure]=True
    assert io.send(action).state=='unknown'
    assert samples and not any(row['role']=='agent' for row in s.inspect('conversation_records'))


def test_after_synthesis_last_gate_can_cancel_without_audio(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    _,action=utterance(call)
    assert io.send(action,before_write=lambda:False).state=='failed' and not samples


def test_late_asr_and_duplicate_utterance_do_not_replay(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    rev=r.interrupt(session['id']);r.interrupt(session['id'])
    assert r.ingest(session['id'],rev,'过时的转写',settings,now) is None
    rev=r.get(session['id'])['revision']
    one=r.ingest(session['id'],rev,'现在的话',settings,now)
    two=r.ingest(session['id'],rev,'现在的话',settings,now)
    assert one.event_id==two.event_id and len(s.inspect('events'))==1
    r.stop(session['id']);assert r.ingest(session['id'],rev,'结束后',settings,now) is None


def test_restart_missing_media_never_sends_ordinary_qq_message(call,monkeypatch):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    _,action=utterance(call)
    import murasamebot.adapters.media.qq_calls as media
    monkeypatch.setattr(media,'lookup',lambda *_:None)
    assert a.send(action,{}).state=='failed' and not samples


def test_call_skip_and_draft_review_are_respected(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    e.model.decide=lambda op,p:{'decision':'skip'}
    rev=r.interrupt(session['id']);r.ingest(session['id'],rev,'一句话',settings,now);e.process_one_event(now)
    assert not s.inspect('actions')
    e.model.decide=lambda op,p:{'decision':'reply','text':'吾辈明白。','can_send':False,'review_reason':'人工确认'}
    rev=r.interrupt(session['id']);r.ingest(session['id'],rev,'另一句话',settings,now);e.process_one_event(now)
    assert s.inspect('actions')[0]['state']=='draft' and not e.dispatch_one(now)


def test_vad_segments_silence_and_bounds_without_recording_files():
    s=Segmenter(DEFAULTS);voice=array('h',[1500]*320).tobytes();silence=b'\0'*640
    assert s.feed(silence)==(False,None)
    assert s.feed(voice)==(True,None)
    for _ in range(6):s.feed(voice)
    chunks=[]
    for _ in range(50):
        _,out=s.feed(silence)
        if out:chunks.append(out)
    assert len(chunks)==1 and len(chunks[0])<32000*12
    assert s.feed(voice)[0]


def test_http_asr_tts_distinct_credentials_and_server_reference_path():
    requests=[];pcm=b'\0\0'*1600
    settings={**DEFAULTS,'asr_url':'https://asr.invalid/transcribe','asr_key':'asr-secret','asr_model':'test',
        'tts_url':'https://tts.invalid/tts','tts_key':'tts-secret','ref_audio_path':'/server/ref.wav'}
    def transport(request):
        requests.append(request)
        if request.url.host=='asr.invalid':
            assert request.headers['Authorization']=='Bearer asr-secret'
            assert b'speech.wav' in request.content
            return httpx.Response(200,json={'text':'听到的话'})
        assert request.headers['Authorization']=='Bearer tts-secret'
        assert json.loads(request.content)['ref_audio_path']=='/server/ref.wav'
        return httpx.Response(200,content=wav_pcm(pcm))
    speech=SpeechHTTP(settings,httpx.Client(transport=httpx.MockTransport(transport)))
    try:
        assert speech.asr(pcm)=='听到的话'
        assert speech.tts('回复')[0]==pcm and len(requests)==2
    finally:speech.close()
    with pytest.raises(Exception):decode_wav(b'not a wav')


def test_video_frame_reuses_vision_service_and_stays_in_private_call(call,monkeypatch):
    from io import BytesIO
    from PIL import Image
    from murasamebot.adapters.media.qq_calls import FrameReader
    from murasamebot.application.vision import VisionService,VisionSettings
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    with s.connection() as db:db.execute("UPDATE call_sessions SET mode='video' WHERE id=?",(session['id'],))
    frame=BytesIO();Image.new('RGB',(40,30),'green').save(frame,format='JPEG')
    io.frame=(time.time(),frame.getvalue(),40,30)
    vision_calls=[]
    class VisionModel:
        def decide(self,op,payload):
            vision_calls.append(payload)
            assert op=='describe_images' and payload['_image_inputs'][0]['data_url'].startswith('data:image/jpeg;base64,')
            return {'summary':'绿色测试画面','images':[{'index':1,'description':'一张绿色测试图'}]}
    vision=VisionService(VisionSettings(),FrameReader(s),VisionModel(),s,model_name='fake-vision')
    e.event_handlers['call_message']=CallConversation(e,r,settings,a,vision).handle
    utterance(call)
    assert vision_calls and calls[-1][1]['visual']['status']=='complete'
    assert '绿色测试画面' in calls[-1][1]['visual']['summary']
    assert all(row['visibility']=='private' for row in s.inspect('conversation_records'))


def test_new_utterance_supersedes_old_pending_retry_without_blocking(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    rev=r.interrupt(session['id']);old=r.ingest(session['id'],rev,'旧的转写',settings,now)
    s.retry_event(old.event_id,now,'fake failure',3)
    rev=r.interrupt(session['id']);new=r.ingest(session['id'],rev,'新的转写',settings,now+1)
    assert e.process_one_event(now+1)
    assert calls[0][1]['message']=='新的转写'


def test_model_error_after_barge_in_does_not_create_blocking_retry(call):
    from murasamebot.domain.models import ModelError
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    def decide(op,p):r.interrupt(session['id']);raise ModelError('fake error')
    e.model.decide=decide
    rev=r.interrupt(session['id']);r.ingest(session['id'],rev,'旧的转写',settings,now)
    assert e.process_one_event(now)
    assert s.inspect('events')[0]['state']=='done'


def test_call_configuration_secrets_are_local_and_redacted(tmp_path):
    from test_receivers import config_path
    from murasamebot.bootstrap.config import Config
    import yaml
    path=config_path(tmp_path);cfg=Config(path)
    assert not cfg.data['qq_calls']['enabled'] and cfg.data['qq_calls']['tts_url']==''
    raw=yaml.safe_load(cfg.local_path.read_text(encoding='utf-8'))
    raw['qq_calls']={'asr_key':'private-asr','tts_key':'private-tts'}
    cfg.local_path.write_text(yaml.safe_dump(raw),encoding='utf-8')
    cfg=Config(path)
    assert 'private-asr' not in json.dumps(cfg.public()) and 'private-tts' not in json.dumps(cfg.public())
    with pytest.raises(ValueError):cfg.save_local({'qq_calls':{'tts_key':'remote-overwrite'}})
    cfg.save_local({'qq_calls':{'enabled':True}})
    assert Config(path).data['qq_calls']['tts_key']=='private-tts'


def test_actual_capture_vad_asr_queue_uses_only_media_threads(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    packets=[array('h',[1600]*320).tobytes()]*8+[b'\0'*640]*45
    class Input:
        closed=False
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*_):self.closed=True
        def abort(self):self.closed=True
        def read(self,_):
            time.sleep(.003)
            if self.closed:raise OSError('closed')
            return packets.pop(0) if packets else b'\0'*640,False
    class Speech:
        def __init__(self,_):pass
        def asr(self,pcm):return '设备语音转写'
        def close(self):pass
    io.audio=SimpleNamespace(RawInputStream=Input,check_input_settings=lambda **_:None,check_output_settings=lambda **_:None)
    io.speech_factory=Speech;io.start()
    deadline=time.monotonic()+3
    while not s.inspect('events') and time.monotonic()<deadline:time.sleep(.01)
    assert s.inspect('events') and calls==[] # no second chat model loop in media transport
    assert e.process_one_event(time.time())
    assert calls[0][1]['message']=='设备语音转写'


def test_capture_watchdog_stops_unresponsive_device(call):
    e,s,a,r,session,settings,io,samples,state,calls,now=call
    io.last_audio=time.monotonic()-5
    io.run(io.watchdog)
    assert io.stop_event.is_set() and r.get(session['id'])['state']=='stopped'


@pytest.mark.parametrize('change',[{'enabled':'true'},{'tts_url':'file:///secret'},{'asr_url':'https://user:pass@server/path'},
    {'session_minutes':0},{'max_utterance_seconds':True}])
def test_config_validation_and_empty_remote_addresses(change):
    validate(DEFAULTS)
    with pytest.raises(ValueError):validate({**DEFAULTS,**change})
