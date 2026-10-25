# -*- coding: utf-8 -*-
"""
大肥鱼 · 语音合成

优先级：
  1. GPT-SoVITS（你自己训练的爱弥斯模型，最接近原声）
  2. edge-tts（微软神经语音，自然）
  3. Windows SAPI（机械声，兜底）
"""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICE_DIR = os.path.join(BASE_DIR, "data", "voice")
CFG_PATH = os.path.join(BASE_DIR, "ttsconfig.json")

# GPT-SoVITS 常见安装位置（自动探测）
GSV_CANDIDATES = [
    r"D:\AIAIAIAI\GPT-SoVITS-v2pro-20250604-nvidia50\GPT-SoVITS-v2pro-20250604-nvidia50",
    r"D:\GPT-SoVITS",
    r"C:\GPT-SoVITS",
]

DEFAULT_CFG = {
    "engine": "auto",                     # auto | gpt-sovits | edge | sapi
    "gsv_url": "http://127.0.0.1:9880",
    "gsv_dir": "",
    "gsv_ref_audio": "",
    "gsv_aux_refs": [],
    "gsv_prompt_text": "",
    "gsv_prompt_lang": "zh",
    "gsv_speed": 1.0,
    "gsv_top_k": 15,
    "gsv_top_p": 1.0,
    "gsv_temperature": 1.0,
    "gsv_repetition_penalty": 1.35,
    "gsv_sample_steps": 32,
    "edge_voice": "zh-CN-XiaoyiNeural",
    "max_chars": 120,
    "allow_sapi_fallback": True,
}

VOICES = [
    {"id": "zh-CN-XiaoyiNeural",    "name": "晓伊 · 少女活泼", "tag": "edge"},
    {"id": "zh-CN-XiaoxuanNeural",  "name": "晓萱 · 少女俏皮", "tag": "edge"},
    {"id": "zh-CN-XiaoxiaoNeural",  "name": "晓晓 · 温柔",     "tag": "edge"},
]


# ── 情绪参考库（轻重缓急）──
EMO_DIR = os.path.join(BASE_DIR, "data", "refs", "emotion")
EMO_MANIFEST = os.path.join(EMO_DIR, "manifest.json")

EMO_STYLE = {
    "happy":   {"speed": 1.06, "topk": 6,  "temp": 1.02},
    "gentle":  {"speed": 0.94, "topk": 5,  "temp": 0.98},
    "sad":     {"speed": 0.90, "topk": 5,  "temp": 0.95},
    "urgent":  {"speed": 1.12, "topk": 6,  "temp": 1.02},
    "tsun":    {"speed": 1.04, "topk": 6,  "temp": 1.05},
    "question":{"speed": 1.02, "topk": 5,  "temp": 1.00},
    "calm":    {"speed": 1.00, "topk": 5,  "temp": 1.00},
}

EMO_RULES = [
    ("urgent", ["快", "赶紧", "马上", "危险", "小心", "来不及"]),
    ("sad",    ["难过", "孤独", "一个人", "对不起", "再见", "呜", "唉", "算了"]),
    ("tsun",   ["才不", "才没", "哼", "笨蛋", "讨厌"]),
    ("happy",  ["啦", "呀", "好耶", "呜呼", "嘻嘻", "哈哈", "！", "!"]),
    ("gentle", ["呢", "吧", "嘛", "谢谢", "睡", "慢慢"]),
    ("question", ["？", "?", "吗"]),
]


def load_emotion_lib():
    if not os.path.isfile(EMO_MANIFEST):
        return {}
    try:
        with open(EMO_MANIFEST, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def detect_emotion(text):
    t = text or ""
    for tag, kws in EMO_RULES:
        for k in kws:
            if k in t:
                return tag
    return "calm"


def pick_ref(tag):
    """返回 (参考音频, 参考文本)；该情绪缺失时回退默认"""
    lib = load_emotion_lib()
    arr = lib.get(tag) or lib.get("calm") or []
    if not arr:
        cfg = load_cfg()
        return cfg.get("gsv_ref_audio"), cfg.get("gsv_prompt_text")
    it = arr[0]
    return it.get("file"), it.get("prompt", "")


def add_pacing(text):
    """轻重缓急：长句在连接词前断句，省略号处留停顿"""
    t = (text or "").strip()
    if len(t) > 26:
        t = re.sub(r"(而且|但是|不过|所以|因为|然后|其实|只是)", r"，\1", t)
    t = re.sub(r"…+", "……", t)
    t = re.sub(r"，{2,}", "，", t)
    t = re.sub(r"([。！？；])([^。！？；])", r"\1\2", t)
    return t


def load_cfg():
    cfg = dict(DEFAULT_CFG)
    if os.path.isfile(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_cfg(patch):
    cfg = load_cfg()
    cfg.update(patch or {})
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def detect_gsv_dir():
    cfg = load_cfg()
    if cfg.get("gsv_dir") and os.path.isdir(cfg["gsv_dir"]):
        return cfg["gsv_dir"]
    for p in GSV_CANDIDATES:
        if os.path.isfile(os.path.join(p, "api_v2.py")):
            return p
    return ""


def clean_text(text, max_len=None):
    """去掉动作描写、颜文字，只留能念的字"""
    if not text:
        return ""
    limit = max_len or load_cfg().get("max_chars", 120)
    t = text
    t = re.sub(r"（[^）]*）", "", t)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"【[^】]*】", "", t)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"[*_#>`~^|/\\]", "", t)
    t = re.sub(r"[♪♫✨💫🐟🍚😊😌🥺❤️💕…]+", "，", t)
    t = re.sub(r"[，,]{2,}", "，", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,")
    if len(t) > limit:
        cut = t[:limit]
        for sep in ("。", "！", "？", "，", "；"):
            p = cut.rfind(sep)
            if p > limit * 0.55:
                return cut[:p + 1]
        return cut + "。"
    return t


def _ensure_dir():
    os.makedirs(VOICE_DIR, exist_ok=True)


# ───────── GPT-SoVITS ─────────
def gsv_alive(url=None):
    cfg = load_cfg()
    url = (url or cfg.get("gsv_url") or "http://127.0.0.1:9880").rstrip("/")
    try:
        req = urllib.request.Request(url + "/docs", headers={"User-Agent": "fishchat"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status in (200, 307)
    except Exception:
        pass
    try:
        with urllib.request.urlopen(url + "/", timeout=3) as r:
            return r.status in (200, 404)
    except Exception:
        return False


def split_text(text, max_len=16):
    """按标点切成短句，每句不超过 max_len 字（长句发音会退化）"""
    parts = re.split(r"(?<=[，。！？；、,.!?;])", text)
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) <= max_len:
            cur += p
        else:
            if cur:
                chunks.append(cur)
            # 单句本身超长则硬切
            while len(p) > max_len:
                chunks.append(p[:max_len])
                p = p[max_len:]
            cur = p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", c)]


def _tts_request(text, cfg, out_path):
    """单次 TTS 请求，成功返回 True"""
    url = (cfg.get("gsv_url") or "http://127.0.0.1:9880").rstrip("/")
    aux = [p for p in (cfg.get("gsv_aux_refs") or []) if p and os.path.isfile(p)]
    payload = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": cfg.get("gsv_ref_audio"),
        "aux_ref_audio_paths": aux,
        "prompt_text": cfg.get("gsv_prompt_text", ""),
        "prompt_lang": cfg.get("gsv_prompt_lang", "zh"),
        "text_split_method": "cut0",   # 已手动分段，不再内部切
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": False,
        "speed_factor": float(cfg.get("gsv_speed", 1.0) or 1.0),
        "top_k": int(cfg.get("gsv_top_k", 5) or 5),
        "top_p": float(cfg.get("gsv_top_p", 1.0) or 1.0),
        "temperature": float(cfg.get("gsv_temperature", 1.0) or 1.0),
        "repetition_penalty": float(cfg.get("gsv_repetition_penalty", 1.12) or 1.12),
        "sample_steps": int(cfg.get("gsv_sample_steps", 32) or 32),
    }
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url + "/tts", data=body,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
        if len(data) > 1500:
            with open(out_path, "wb") as f:
                f.write(data)
            return True
    except Exception:
        pass
    return False


def concat_wav(paths, out_path, gap_ms=120):
    """拼接多个 wav，中间留自然停顿"""
    import wave as _w
    frames = []
    params = None
    for p in paths:
        try:
            with _w.open(p) as w:
                if params is None:
                    params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
                frames.append(w.readframes(w.getnframes()))
        except Exception:
            continue
    if not frames or not params:
        return None
    ch, sw, sr = params
    silence = b"\x00" * int(sr * gap_ms / 1000) * ch * sw
    with _w.open(out_path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(sr)
        for i, fr in enumerate(frames):
            if i:
                w.writeframes(silence)
            w.writeframes(fr)
    return out_path


def synth_gsv(text, cfg=None, emotion=None):
    """调用 GPT-SoVITS 合成，返回 wav 路径（长句自动分段，避免后半句发音退化）"""
    cfg = cfg or load_cfg()
    ref = (cfg.get("gsv_ref_audio") or "").strip()
    if not ref or not os.path.isfile(ref):
        return None
    _ensure_dir()
    stamp = int(time.time() * 1000)
    chunks = split_text(text, int(cfg.get("split_len", 16) or 16))
    if not chunks:
        return None
    if len(chunks) == 1:
        out = os.path.join(VOICE_DIR, "gsv_%d.wav" % stamp)
        return out if _tts_request(chunks[0], cfg, out) else None

    parts = []
    for i, c in enumerate(chunks):
        p = os.path.join(VOICE_DIR, "_seg_%d_%d.wav" % (stamp, i))
        if _tts_request(c, cfg, p):
            parts.append(p)
    if not parts:
        return None
    out = os.path.join(VOICE_DIR, "gsv_%d.wav" % stamp)
    res = concat_wav(parts, out)
    for p in parts:
        try:
            os.remove(p)
        except Exception:
            pass
    return res


# ───────── edge-tts ─────────
async def _edge_save(text, voice, path):
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate="+8%", pitch="+4Hz")
    await comm.save(path)


def synth_edge(text, voice=None):
    cfg = load_cfg()
    voice = voice or cfg.get("edge_voice") or "zh-CN-XiaoyiNeural"
    _ensure_dir()
    path = os.path.join(VOICE_DIR, "edge_%d.mp3" % int(time.time() * 1000))
    try:
        asyncio.run(_edge_save(text, voice, path))
        if os.path.isfile(path) and os.path.getsize(path) > 800:
            return path
    except Exception:
        pass
    return None


# ───────── SAPI ─────────
def synth_sapi(text):
    _ensure_dir()
    path = os.path.join(VOICE_DIR, "sapi_%d.wav" % int(time.time() * 1000))
    safe = text.replace("'", "''").replace('"', "")
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          "$s.SelectVoice('Microsoft Huihui Desktop'); $s.Rate = 2; "
          "$s.SetOutputToWaveFile('%s'); $s.Speak('%s'); $s.Dispose()") % (path, safe)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60,
                       creationflags=0x08000000)
        if os.path.isfile(path) and os.path.getsize(path) > 2000:
            return path
    except Exception:
        pass
    return None


def synthesize(text, voice=None):
    """统一入口，返回 (路径, 引擎)"""
    cfg = load_cfg()
    t = clean_text(text, cfg.get("max_chars", 120))
    if not t:
        return None, "empty"
    engine = (cfg.get("engine") or "auto").lower()

    if engine in ("auto", "gpt-sovits", "gpt_sovits") and cfg.get("gsv_ref_audio"):
        if gsv_alive():
            use = dict(cfg)
            tag = "calm"
            if cfg.get("emotion_enabled", True):
                tag = detect_emotion(text)
                ref, prompt = pick_ref(tag)
                if ref and os.path.isfile(ref):
                    use["gsv_ref_audio"] = ref
                    use["gsv_prompt_text"] = prompt
                st = EMO_STYLE.get(tag) or {}
                if st:
                    use["gsv_speed"] = st.get("speed", use.get("gsv_speed", 1.0))
                    use["gsv_top_k"] = st.get("topk", use.get("gsv_top_k", 5))
                    use["gsv_temperature"] = st.get("temp", use.get("gsv_temperature", 1.0))
                use["gsv_aux_refs"] = []
                t = add_pacing(t)
            p = synth_gsv(t, use, emotion=tag)
            if p:
                return p, "gpt-sovits"

    if engine in ("auto", "edge"):
        p = synth_edge(t, voice)
        if p:
            return p, "edge"

    if cfg.get("allow_sapi_fallback", True) and engine in ("auto", "sapi"):
        p = synth_sapi(t)
        if p:
            return p, "sapi"

    return None, "failed"


def cleanup_voice_files(max_files=200, max_age_hours=6):
    _ensure_dir()
    try:
        files = [os.path.join(VOICE_DIR, f) for f in os.listdir(VOICE_DIR)]
        files = [f for f in files if os.path.isfile(f)]
        files.sort(key=os.path.getmtime)
        now = time.time()
        removed = 0
        for f in files:
            if (now - os.path.getmtime(f)) / 3600.0 > max_age_hours or len(files) - removed > max_files:
                try:
                    os.remove(f)
                    removed += 1
                except Exception:
                    pass
        return removed
    except Exception:
        return 0
