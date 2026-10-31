"""Bounded HTTP speech I/O, no recordings on disk or inherited proxy credentials."""
from array import array
from io import BytesIO
import math
import wave
import httpx


def wav_pcm(pcm,rate=16000):
    out=BytesIO()
    with wave.open(out,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm)
    return out.getvalue()


def decode_wav(data):
    if len(data)>8*1024*1024:raise ValueError('语音响应过大')
    with wave.open(BytesIO(data),'rb') as w:
        if w.getsampwidth()!=2 or w.getnchannels() not in (1,2) or not 8000<=w.getframerate()<=96000 or w.getnframes()/w.getframerate()>60:raise ValueError('需要60秒以内的16位PCM WAV')
        return w.readframes(w.getnframes()),w.getframerate(),w.getnchannels()


class Segmenter:
    def __init__(self,settings):
        self.settings=settings;self.buf=bytearray();self.silent=0;self.voiced=0
    def feed(self,pcm):
        samples=array('h',pcm)
        rms=math.sqrt(sum(v*v for v in samples)/max(1,len(samples)))
        voice=rms>=self.settings['vad_threshold'];started=voice and not self.buf
        if voice or self.buf:
            self.buf.extend(pcm);self.voiced+=len(pcm) if voice else 0
            self.silent=0 if voice else self.silent+len(pcm)
        done=self.buf and (self.silent>=32*self.settings['silence_ms'] or len(self.buf)>=32000*self.settings['max_utterance_seconds'])
        result=bytes(self.buf) if done and self.voiced>=3200 else None
        if done:self.buf.clear();self.silent=0;self.voiced=0
        return started,result


class SpeechHTTP:
    def __init__(self,settings,client=None):
        self.settings=settings;self.client=client or httpx.Client(timeout=45,trust_env=False,follow_redirects=False)
    def read(self,url,**kwargs):
        chunks=bytearray()
        with self.client.stream('POST',url,**kwargs) as r:
            r.raise_for_status()
            for c in r.iter_bytes():
                chunks.extend(c)
                if len(chunks)>8*1024*1024:raise ValueError('语音服务响应超限')
        return bytes(chunks)
    def asr(self,pcm):
        s=self.settings
        data=self.read(s['asr_url'],headers={'Authorization':'Bearer '+s['asr_key']} if s['asr_key'] else {},
            files={'file':('speech.wav',wav_pcm(pcm),'audio/wav')},data={'model':s['asr_model']})
        import json
        text=json.loads(data).get('text')
        if not isinstance(text,str) or len(text)>2000:raise ValueError('识别服务未返回合法转写')
        return text
    def tts(self,text):
        s=self.settings
        data=self.read(s['tts_url'],headers={'Authorization':'Bearer '+s['tts_key']} if s['tts_key'] else {},
            json={'text':text,'text_lang':s['text_lang'],'ref_audio_path':s['ref_audio_path'],
                'prompt_text':s['prompt_text'],'prompt_lang':s['prompt_lang'],'media_type':'wav','streaming_mode':False})
        return decode_wav(data)
    def close(self):self.client.close()
