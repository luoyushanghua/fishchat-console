# -*- coding: utf-8 -*-
"""
陪刷抖音模块

她在模拟器里自己刷，读到屏幕文字 → 判断有没有意思 → 写一条带 @ 的评论草稿。
默认只生成草稿，不自动发送（避免风控）。

依赖：adb（MuMu 自带）+ 本地模型（通过 qqbot._llm_once）
"""
import io
import json
import os
import re
import subprocess
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "data", "douyin_state.json")
DRAFT_PATH = os.path.join(BASE_DIR, "data", "douyin_drafts.json")

ADB_CANDIDATES = [
    r"D:\mumuc\MuMuPlayer\nx_device\12.0\shell\adb.exe",
    r"D:\mumuc\MuMuPlayer\nx_device\15.0\shell\adb.exe",
]
ADB_SERIAL_DEFAULT = "127.0.0.1:7555"

DOUYIN_PKGS = ["com.ss.android.ugc.aweme", "com.ss.android.ugc.aweme.lite"]

_STATE = {
    "running": False,
    "serial": ADB_SERIAL_DEFAULT,
    "last_text": "",
    "last_title": "",
    "last_reaction": "",
    "last_read_ts": 0,
    "drafts": 0,
    "error": "",
    "started_at": 0,
    "min_interval": 90,      # 两条草稿最小间隔（秒）
}
_lock = threading.Lock()
_thread = None


def adb_path():
    for p in ADB_CANDIDATES:
        if os.path.isfile(p):
            return p
    return "adb"


def _adb(args, timeout=20):
    try:
        r = subprocess.run([adb_path()] + args, capture_output=True, timeout=timeout,
                           creationflags=0x08000000)
        return (r.stdout or b"").decode("utf-8", errors="replace")
    except Exception as e:
        return ""


def devices():
    out = _adb(["devices"])
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            devs.append(parts[0])
    return devs


def current_app(serial):
    out = _adb(["-s", serial, "shell", "dumpsys", "window"])
    m = re.search(r"mCurrentFocus=Window\{[^ ]+ [^ ]+ ([^/]+)/", out)
    return m.group(1) if m else ""


def is_douyin(serial):
    pkg = current_app(serial)
    return pkg in DOUYIN_PKGS, pkg


def screen_text(serial):
    """导出当前界面文字（uiautomator），返回 (文本列表, 原始 xml)"""
    _adb(["-s", serial, "shell", "uiautomator", "dump", "/sdcard/_fui.xml"], timeout=25)
    xml = _adb(["-s", serial, "shell", "cat", "/sdcard/_fui.xml"], timeout=20)
    if not xml.strip():
        return [], ""
    texts = []
    for m in re.finditer(r'text="([^"]*)"', xml):
        t = m.group(1).strip()
        if t and t not in texts:
            texts.append(t)
    return texts, xml


def pick_title(texts):
    """从界面文字里挑出最像视频标题的一条"""
    cand = [t for t in texts if 8 <= len(t) <= 80]
    banned = ("搜索", "首页", "朋友", "消息", "我", "关注", "推荐", "评论", "分享",
              "点赞", "收藏", "转发", "展开", "回复", "复制", "举报")
    cand = [t for t in cand if not any(b == t or t.startswith(b) for b in banned)]
    if not cand:
        return ""
    # 取最长的一条，通常是文案
    return max(cand, key=len)


def load_drafts():
    try:
        if os.path.isfile(DRAFT_PATH):
            with io.open(DRAFT_PATH, encoding="utf-8-sig") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_draft(item):
    arr = load_drafts()
    arr.insert(0, item)
    arr = arr[:50]
    try:
        io.open(DRAFT_PATH, "w", encoding="utf-8", newline="\n").write(
            json.dumps(arr, ensure_ascii=False, indent=2))
    except Exception:
        pass


def status():
    with _lock:
        s = dict(_STATE)
    s["devices"] = devices()
    s["adb"] = adb_path()
    s["adb_exists"] = os.path.isfile(adb_path())
    try:
        ok, pkg = is_douyin(s["serial"])
        s["douyin_active"] = ok
        s["current_app"] = pkg
    except Exception:
        s["douyin_active"] = False
        s["current_app"] = ""
    s["draft_list"] = load_drafts()[:8]
    return s


def _react(title, nick=""):
    """让模型写一条带 @ 的评论草稿"""
    try:
        import qqbot as Q
        persona = (Q.CFG.get("system_prompt") or "")[:900]
        sys_p = (persona + "\n\n【场景】你在刷抖音，看到一条视频。"
                 "用你的人设写一条评论区留言，要求：\n"
                 "- 开头用 @%s\n"
                 "- 20~40 字，口语，别写成文案\n"
                 "- 带一点情绪（撒娇/吐槽/好奇），别客套\n"
                 "- 不要加引号，不要解释" % (nick or "老大"))
        user = "视频标题/文案：%s" % title[:120]
        txt = Q._llm_once(sys_p, user, 120).strip().strip("\"'“”「」")
        return txt[:120]
    except Exception as e:
        return "（生成失败: %s）" % e


def _loop():
    last_gen = 0
    while True:
        with _lock:
            if not _STATE["running"]:
                break
            serial = _STATE["serial"]
            interval = _STATE["min_interval"]
        try:
            ok, pkg = is_douyin(serial)
            if not ok:
                with _lock:
                    _STATE["error"] = "当前不是抖音界面（%s）" % (pkg or "未知")
                time.sleep(5)
                continue
            texts, _ = screen_text(serial)
            if texts:
                title = pick_title(texts)
                with _lock:
                    _STATE["last_text"] = " | ".join(texts[:12])
                    _STATE["last_title"] = title
                    _STATE["last_read_ts"] = int(time.time())
                    _STATE["error"] = ""
                if title and len(title) >= 8 and time.time() - last_gen > interval:
                    last_gen = time.time()
                    reaction = _react(title)
                    with _lock:
                        _STATE["last_reaction"] = reaction
                        _STATE["drafts"] += 1
                    save_draft({
                        "ts": int(time.time()),
                        "title": title,
                        "comment": reaction,
                        "status": "待审核",
                    })
        except Exception as e:
            with _lock:
                _STATE["error"] = str(e)
        time.sleep(12)


def start(serial=None, min_interval=90):
    global _thread
    with _lock:
        if _STATE["running"]:
            return True, "已经在跑了"
        _STATE["running"] = True
        _STATE["started_at"] = int(time.time())
        _STATE["error"] = ""
        if serial:
            _STATE["serial"] = serial
        _STATE["min_interval"] = max(30, int(min_interval or 90))
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    return True, "已开始陪刷（每 %d 秒最多一条草稿）" % _STATE["min_interval"]


def stop():
    with _lock:
        _STATE["running"] = False
    return True, "已停止"


def clear_drafts():
    try:
        io.open(DRAFT_PATH, "w", encoding="utf-8", newline="\n").write("[]")
    except Exception:
        pass
    with _lock:
        _STATE["drafts"] = 0
    return True, "草稿已清空"


if __name__ == "__main__":
    print("adb:", adb_path())
    print("设备:", devices())
    print(json.dumps({k: v for k, v in status().items() if k != "draft_list"},
                     ensure_ascii=False, indent=2))
