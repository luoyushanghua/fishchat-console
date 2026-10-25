# -*- coding: utf-8 -*-
"""大肥鱼 · 模型参数监控台后端（零依赖，全部自动探测）"""
import ctypes
import json
import os
import shutil
import string
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

OLLAMA = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
PORT = int(os.environ.get("FISHCHAT_PORT", "8080"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
BOT_CONFIG_PATH = os.path.join(BASE_DIR, "botconfig.json")

DEFAULT_CONFIG = {
    "model": "", "num_ctx": 4096, "temperature": 0.85, "top_p": 0.92,
    "top_k": 40, "repeat_penalty": 1.08, "num_predict": 512,
    "think": False, "keep_alive": "30m", "num_gpu": -1, "system_prompt": "",
}

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".png": "image/png",
    ".jpg": "image/jpeg", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".webm": "video/webm", ".mp4": "video/mp4", ".woff2": "font/woff2",
}

_cache = {}
_checks = []


# ───────────── 自检：探测各个环境要素 ─────────────
def _add(name, ok, value, detail="", group="env"):
    _checks.append({"name": name, "ok": bool(ok), "value": value, "detail": detail, "group": group})


def detect_ollama():
    cands = []
    w = shutil.which("ollama")
    if w:
        cands.append(w)
    env = os.environ.get("OLLAMA_EXE")
    if env:
        cands.append(env)
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        if base:
            cands.append(os.path.join(base, "Programs", "Ollama", "ollama.exe"))
            cands.append(os.path.join(base, "Ollama", "ollama.exe"))
    cands += [r"D:\LocalAI\Ollama\ollama.exe", r"C:\Ollama\ollama.exe"]
    seen = set()
    for p in cands:
        if not p:
            continue
        p = os.path.normpath(p)
        if p in seen:
            continue
        seen.add(p)
        if os.path.isfile(p):
            return p
    return None


def detect_models_dir():
    cands = []
    if os.environ.get("OLLAMA_MODELS"):
        cands.append(os.environ["OLLAMA_MODELS"])
    cands += [r"D:\LocalAI\models"]
    for home in {os.environ.get("USERPROFILE", ""), os.path.expanduser("~"),
                 os.path.join("C:\\Users", os.environ.get("USERNAME", "default"))}:
        if home:
            cands.append(os.path.join(home, ".ollama", "models"))
    cands.append(r"D:\LocalAI\ollama-home\.ollama\models")
    seen = set()
    for p in cands:
        if not p:
            continue
        p = os.path.normpath(p)
        if p in seen:
            continue
        seen.add(p)
        if os.path.isdir(os.path.join(p, "manifests")) or os.path.isdir(os.path.join(p, "blobs")):
            return p
    return None


def dir_size_gb(path):
    if not path:
        return 0
    total = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return round(total / 1024**3, 2)


def detect_gpus():
    """探测 GPU。优先 NVIDIA 独显（index 0），过滤虚拟显示适配器。"""
    gpus = []
    # ① NVIDIA 独显（最可靠，带 index）
    try:
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
        if os.path.isfile(exe):
            q = ("name,memory.used,memory.total,utilization.gpu,temperature.gpu,"
                 "power.draw,power.limit,clocks.current.graphics,driver_version,index")
            out = subprocess.run([exe, f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=6).stdout.strip()
            for line in out.splitlines():
                p = [x.strip() for x in line.split(",")]
                if len(p) < 10:
                    continue
                num = lambda i: (float(p[i]) if p[i] not in ("[N/A]", "") else None)
                used, total = num(1) or 0, num(2) or 1
                gpus.append({
                    "index": int(float(p[9])) if p[9] not in ("[N/A]", "") else len(gpus),
                    "vendor": "NVIDIA", "name": p[0], "discrete": True,
                    "cuda_id": "CUDA" + (p[9] if p[9] not in ("[N/A]", "") else "0"),
                    "vram_used_mb": int(used), "vram_total_mb": int(total),
                    "vram_percent": round(used / max(total, 1) * 100),
                    "util": int(num(3) or 0), "temp": int(num(4) or 0),
                    "power": round(num(5) or 0), "power_limit": round(num(6) or 0),
                    "clock": round(num(7) or 0), "driver": p[8],
                })
    except Exception:
        pass

    # ② 注册表兜底（AMD/Intel），过滤虚拟显示器
    if not gpus:
        VIRTUAL = ("virtual", "basic", "mumu", "gameviewer", "parsec", "sunshine",
                   "remote", "idd", "displaylink", "spacedesk", "todesk")
        try:
            import winreg
            base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i); i += 1
                except OSError:
                    break
                try:
                    k = winreg.OpenKey(key, sub)
                    desc = winreg.QueryValueEx(k, "DriverDesc")[0]
                    drv = winreg.QueryValueEx(k, "DriverVersion")[0]
                    low = desc.lower()
                    if any(v in low for v in VIRTUAL):
                        continue
                    if "Intel" in desc:
                        vendor, disc = "Intel", False
                    elif "AMD" in desc or "Radeon" in desc:
                        vendor, disc = "AMD", True
                    else:
                        vendor, disc = "GPU", True
                    try:
                        idx = int(sub, 10)
                    except Exception:
                        idx = i
                    gpus.append({"index": idx, "vendor": vendor, "name": desc,
                                 "discrete": disc, "cuda_id": None, "driver": drv,
                                 "vram_used_mb": 0, "vram_total_mb": 0, "vram_percent": 0,
                                 "util": 0, "temp": 0, "power": 0, "power_limit": 0,
                                 "clock": 0})
                except Exception:
                    continue
        except Exception:
            pass

    # ③ 排序：独显优先，其次 index 小的在前
    gpus.sort(key=lambda g: (0 if g.get("discrete") else 1, g.get("index", 99)))
    return gpus


def primary_gpu(gpus=None):
    """选择主 GPU：优先 CUDA 独显，绝不返回 Intel 核显（除非只有它）。"""
    gpus = gpus if gpus is not None else detect_gpus()
    if not gpus:
        return {}
    for g in gpus:
        if g.get("cuda_id"):
            return g
    for g in gpus:
        if g.get("discrete"):
            return g
    return gpus[0]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def mem_info():
    try:
        st = MEMORYSTATUSEX(); st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return {"total_gb": round(st.ullTotalPhys / 1024**3, 1),
                "used_gb": round((st.ullTotalPhys - st.ullAvailPhys) / 1024**3, 1),
                "free_gb": round(st.ullAvailPhys / 1024**3, 1),
                "percent": st.dwMemoryLoad,
                "page_total_gb": round(st.ullTotalPageFile / 1024**3, 1),
                "page_used_gb": round((st.ullTotalPageFile - st.ullAvailPageFile) / 1024**3, 1)}
    except Exception:
        return {}


def detect_disks():
    out = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, ch in enumerate(string.ascii_uppercase):
            if not (mask & (1 << i)):
                continue
            root = f"{ch}:\\"
            dt = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
            if dt != 3:  # 仅固定磁盘
                continue
            free = ctypes.c_ulonglong(0); total = ctypes.c_ulonglong(0)
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(root), None, ctypes.byref(total), ctypes.byref(free)):
                if total.value == 0:
                    continue
                out.append({
                    "letter": ch, "total_gb": round(total.value / 1024**3, 1),
                    "free_gb": round(free.value / 1024**3, 1),
                    "used_gb": round((total.value - free.value) / 1024**3, 1),
                    "percent": round((total.value - free.value) / total.value * 100),
                })
    except Exception:
        pass
    return out


def detect_sys():
    info = {"cores": os.cpu_count(), "python": sys.version.split()[0],
            "python_exe": sys.executable, "arch": sys.maxsize > 2**32 and "64-bit" or "32-bit"}
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        info["cpu"] = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        k2 = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        build = winreg.QueryValueEx(k2, "CurrentBuild")[0]
        try:
            ubr = winreg.QueryValueEx(k2, "UBR")[0]
            build = f"{build}.{ubr}"
        except Exception:
            pass
        info["os_build"] = build
        info["os_ver"] = winreg.QueryValueEx(k2, "DisplayVersion")[0]
        info["os_edition"] = winreg.QueryValueEx(k2, "EditionID")[0]
    except Exception:
        pass
    return info


def ollama_get(path, timeout=8):
    with urlopen(f"{OLLAMA}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def port_open(port, host="127.0.0.1"):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


NAPCAT_WEBUI = os.environ.get("NAPCAT_WEBUI", "http://127.0.0.1:6099/webui")


def probe_url(url, timeout=3):
    """探测 URL 是否可达（4xx 也算可达，说明服务在）"""
    from urllib.error import HTTPError
    try:
        req = Request(url, headers={"User-Agent": "fishchat-probe"})
        with urlopen(req, timeout=timeout) as r:
            return {"ok": True, "code": getattr(r, "status", 200)}
    except HTTPError as e:
        return {"ok": True, "code": e.code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def napcat_webui_conf():
    """自动探测 NapCat WebUI 的 host / port / token"""
    cands = [
        os.path.join(NAPCAT_DIR, "napcat", "config", "webui.json"),
        os.path.join(NAPCAT_DIR, "config", "webui.json"),
        os.path.join(NAPCAT_DIR, "napcat", "webui.json"),
        os.path.join(NAPCAT_DIR, "webui.json"),
    ]
    for p in cands:
        if os.path.isfile(p):
            try:
                j = json.load(open(p, encoding="utf-8"))
                return {
                    "host": j.get("host", "127.0.0.1"),
                    "port": int(j.get("port", 6099) or 6099),
                    "token": str(j.get("token", "") or ""),
                    "path": p,
                }
            except Exception:
                pass
    return {"host": "127.0.0.1", "port": 6099, "token": "", "path": ""}


def napcat_login_info():
    """自适应：查询 NapCat 当前登录的 QQ"""
    try:
        req = Request("http://127.0.0.1:3000/get_login_info",
                      headers={"User-Agent": "fishchat"})
        with urlopen(req, timeout=5) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("retcode") == 0 and j.get("data"):
            return {"qq": j["data"].get("user_id"), "nickname": j["data"].get("nickname", "")}
    except Exception:
        pass
    return {}


def onebot_api(path, timeout=12):
    """调用 OneBot 11 HTTP 接口"""
    try:
        req = Request("http://127.0.0.1:3000" + path,
                      headers={"User-Agent": "fishchat",
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"retcode": -1, "error": str(e)}


def napcat_groups():
    j = onebot_api("/get_group_list")
    if j.get("retcode") != 0:
        return {"ok": False, "error": j.get("error") or j.get("message") or "获取失败", "items": []}
    items = []
    for g in (j.get("data") or []):
        items.append({
            "id": g.get("group_id"),
            "name": g.get("group_name") or "",
            "count": g.get("member_count"),
            "max": g.get("max_member_count"),
        })
    items.sort(key=lambda x: -(x.get("count") or 0))
    return {"ok": True, "items": items}


def napcat_friends():
    j = onebot_api("/get_friend_list")
    if j.get("retcode") != 0:
        return {"ok": False, "error": j.get("error") or j.get("message") or "获取失败", "items": []}
    items = []
    for f in (j.get("data") or []):
        items.append({
            "id": f.get("user_id"),
            "name": f.get("remark") or f.get("nickname") or "",
            "nickname": f.get("nickname") or "",
        })
    items.sort(key=lambda x: x.get("name") or "")
    return {"ok": True, "items": items}


# ───────────── 记忆库管理 ─────────────
MEMORY_DB = os.path.join(BASE_DIR, "data", "memory.db")


def _bot_cfg():
    try:
        return json.load(open(BOT_CONFIG_PATH, encoding="utf-8-sig"))
    except Exception:
        return {}


def memory_store():
    import memory as memmod
    bc = _bot_cfg()
    return memmod.MemoryStore(
        MEMORY_DB,
        max_turns=int(bc.get("memory_max_turns", 6) or 6),
        ttl_days=int(bc.get("memory_ttl_days", 7) or 7),
        max_rows=int(bc.get("memory_max_rows", 20000) or 20000),
    )


def memory_overview():
    bc = _bot_cfg()
    st = memory_store()
    return {
        "ok": True,
        "db_path": MEMORY_DB,
        "stats": st.stats(),
        "keys": st.list_keys(40),
        "config": {
            "ttl_days": int(bc.get("memory_ttl_days", 7) or 7),
            "max_turns": int(bc.get("memory_max_turns", 6) or 6),
            "max_rows": int(bc.get("memory_max_rows", 20000) or 20000),
            "cleanup_interval_min": int(bc.get("cleanup_interval_min", 30) or 30),
            "log_max_mb": float(bc.get("log_max_mb", 2) or 2),
        },
        "log_size_kb": round(os.path.getsize(os.path.join(BASE_DIR, "logs", "bot.log"))
                             / 1024, 1) if os.path.isfile(
                                 os.path.join(BASE_DIR, "logs", "bot.log")) else 0,
    }


def napcat_status():
    wc = napcat_webui_conf()
    port = wc["port"]
    base = "http://127.0.0.1:%d/webui" % port
    with_tok = base + ("?token=" + wc["token"] if wc["token"] else "")
    return {
        "webui": probe_url(base),
        "webui_url": base,
        "webui_url_token": with_tok,
        "token": wc["token"],
        "webui_port": port,
        "webui_conf_path": wc["path"],
        "login": napcat_login_info(),
        "api3000": probe_url("http://127.0.0.1:3000/"),
        "bot8081": probe_url("http://127.0.0.1:8081/"),
        "port6099": port_open(port),
        "port3000": port_open(3000),
        "port8081": port_open(8081),
        "qq_running": len(_qq_pids()) > 0,
        "qq_pids": _qq_pids(),
        "launcher": os.path.isfile(NAPCAT_LAUNCHER),
        "dir": NAPCAT_DIR,
        "ts": int(time.time()),
    }


# ───────────── NapCat 一键启动 ─────────────
def _detect_napcat_dir():
    cands = [os.environ.get("NAPCAT_DIR", ""), r"D:\LocalAI\NapCat", r"D:\NapCat",
             r"C:\LocalAI\NapCat", os.path.join(BASE_DIR, "..", "NapCat")]
    for p in cands:
        if p and os.path.isfile(os.path.join(p, "启动NapCat.bat")):
            return os.path.normpath(p)
    return r"D:\LocalAI\NapCat"


NAPCAT_DIR = _detect_napcat_dir()
NAPCAT_LAUNCHER = os.path.join(NAPCAT_DIR, "启动NapCat.bat")
NAPCAT_LAUNCHER_USER = os.path.join(NAPCAT_DIR, "启动NapCat-免管理员.bat")


def _qq_pids():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=10,
                           encoding="gbk", errors="replace", creationflags=0x08000000)
        pids = []
        for line in (r.stdout or "").splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "qq.exe":
                try:
                    pid = int(parts[1])
                    if _proc_alive(pid):      # 过滤僵尸进程
                        pids.append(pid)
                except Exception:
                    pass
        return pids
    except Exception:
        return []


def _proc_alive(pid):
    """进程是否真的活着（排除僵尸/已退出但句柄未释放）"""
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259
    except Exception:
        return True


def kill_qq():
    """彻底关闭 QQ 与 NapCat 注入器（含自动重试，防 QQ 自杀重启）"""
    killed = 0
    for name in ("QQ.exe", "NapCatWinBootMain.exe", "NapCatWinBootHook.exe"):
        try:
            subprocess.run(["taskkill", "/IM", name, "/F", "/T"], capture_output=True,
                           text=True, timeout=25, encoding="gbk", errors="replace",
                           creationflags=0x08000000)
        except Exception:
            pass
    # 等 6 秒，最多重试 3 轮
    for _ in range(3):
        time.sleep(2)
        pids = _qq_pids()
        if not pids:
            break
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=15, creationflags=0x08000000)
                killed += 1
            except Exception:
                pass
    left = _qq_pids()
    if left:
        return False, "仍有 %d 个 QQ 进程无法关闭，请手动退出" % len(left)
    return True, "QQ 已完全退出（%d 个进程）" % (killed or 1)


def _qq_exe():
    """找 QQ.exe 路径：注册表 → 常见目录"""
    cands = []
    try:
        import winreg
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    v = winreg.QueryValueEx(k, "UninstallString")[0].strip('"')
                    cands.append(os.path.join(os.path.dirname(v), "QQ.exe"))
            except Exception:
                pass
    except Exception:
        pass
    cands += [r"D:\qq\QQ.exe",
              os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tencent", "QQNT", "QQ.exe"),
              r"D:\Program Files\Tencent\QQNT\QQ.exe"]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def start_napcat(mode="admin"):
    """无窗口启动 NapCat：直接调注入器，二维码写入缓存供控制台显示"""
    nap = os.path.join(NAPCAT_DIR, "napcat")
    exe = os.path.join(nap, "NapCatWinBootMain.exe")
    hook = os.path.join(nap, "NapCatWinBootHook.dll")
    if not os.path.isfile(exe):
        return False, "找不到 NapCatWinBootMain.exe"
    qq = _qq_exe()
    if not qq:
        return False, "找不到 QQ.exe，请确认 QQ 已安装"
    if _qq_pids():
        ok, msg = kill_qq()
        if not ok:
            return False, msg
    try:
        os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
        logf = open(os.path.join(BASE_DIR, "logs", "napcat.log"), "ab")
        args = [exe, qq, hook]
        subprocess.Popen(args, cwd=nap, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        return True, "已静默启动（无窗口），二维码会自动出现在下方"
    except Exception as e:
        return False, str(e)


# ───────────── 服务管理（单独启停 + 自启动记忆）─────────────
AUTOSTART_PATH = os.path.join(BASE_DIR, "data", "autostart.json")
_AUTOSTART_DEFAULT = {
    "console": True,     # 控制台本身
    "model": False,      # 加载 Ollama 模型
    "gsv": False,        # GPT-SoVITS 语音
    "bot": False,        # QQ 机器人
    "tray": True,        # 托盘图标
}


def load_autostart():
    cfg = dict(_AUTOSTART_DEFAULT)
    try:
        if os.path.isfile(AUTOSTART_PATH):
            with open(AUTOSTART_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_autostart(patch):
    cfg = load_autostart()
    cfg.update(patch or {})
    os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
    with open(AUTOSTART_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def _model_loaded():
    try:
        ps = ollama_get("/api/ps").get("models", [])
        return [{"name": m.get("name"), "vram_gb": round((m.get("size_vram") or 0) / 1024**3, 2),
                 "processor": m.get("processor")} for m in ps]
    except Exception:
        return []


def services_state():
    auto = load_autostart()
    loaded = _model_loaded()
    return {
        "ok": True,
        "autostart": auto,
        "model": Handler.cfg.get("model"),
        "services": [
            {"id": "console", "label": "控制台", "running": True,
             "detail": "端口 %d" % PORT, "can_start": False},
            {"id": "model", "label": "语言模型",
             "running": bool(loaded),
             "detail": ("；".join("%s %.1fGB %s" % (m["name"], m["vram_gb"], m["processor"] or "")
                                  for m in loaded) or Handler.cfg.get("model")),
             "vram": sum(m["vram_gb"] for m in loaded), "can_start": True},
            {"id": "gsv", "label": "语音引擎 GPT-SoVITS",
             "running": port_open(9880), "detail": "端口 9880", "can_start": True},
            {"id": "bot", "label": "QQ 机器人",
             "running": port_open(8081), "detail": "端口 8081", "can_start": True},
            {"id": "tray", "label": "托盘图标",
             "running": _tray_running(), "detail": "通知区域", "can_start": True},
        ],
    }


def _tray_running():
    p = os.path.join(BASE_DIR, ".tray.lock")
    try:
        if os.path.isfile(p):
            pid = int(open(p, encoding="ascii").read().strip())
            import subprocess as sp
            r = sp.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True, text=True,
                       timeout=8, encoding="gbk", errors="replace", creationflags=0x08000000)
            return str(pid) in (r.stdout or "")
    except Exception:
        pass
    return False


def _py():
    """找一个可用的 Python 解释器"""
    import shutil as _sh
    cands = [sys.executable, _sh.which("python"), _sh.which("python3"), _sh.which("py")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return "python"


def service_start(sid):
    if sid == "model":
        c = Handler.cfg
        payload = {"model": c.get("model"), "prompt": "你好", "stream": False,
                   "think": bool(c.get("think")), "keep_alive": c.get("keep_alive", "30m"),
                   "options": {"num_ctx": int(c.get("num_ctx", 4096)), "num_predict": 4,
                               "num_gpu": int(c.get("num_gpu", -1))}}
        import threading as _t
        def _run():
            try:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = Request(f"{OLLAMA}/api/generate", data=data,
                              headers={"Content-Type": "application/json; charset=utf-8"})
                with urlopen(req, timeout=900) as r:
                    r.read()
            except Exception:
                pass
        _t.Thread(target=_run, daemon=True).start()
        return True, "模型加载中（约 10-90 秒，看模型大小）"
    if sid == "gsv":
        d = ""
        try:
            import tts as t
            d = t.detect_gsv_dir()
        except Exception:
            pass
        if not d:
            return False, "未找到 GPT-SoVITS 目录"
        py = os.path.join(d, "runtime", "python.exe")
        if not os.path.isfile(py):
            py = "python"
        cfgf = os.path.join(d, "GPT_SoVITS", "configs", "tts_infer_retrain.yaml")
        if not os.path.isfile(cfgf):
            cfgf = os.path.join(d, "GPT_SoVITS", "configs", "tts_infer.yaml")
        logf = open(os.path.join(BASE_DIR, "logs", "gsv.log"), "ab")
        subprocess.Popen([py, "api_v2.py", "-a", "127.0.0.1", "-p", "9880", "-c", cfgf],
                         cwd=d, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=0x08000000)
        return True, "语音引擎启动中（约 40 秒）"
    if sid == "bot":
        if port_open(8081):
            return True, "机器人已在运行"
        subprocess.Popen([_py(), "qqbot.py"], cwd=BASE_DIR,
                         stdout=open(os.path.join(BASE_DIR, "logs", "bot.out.log"), "ab"),
                         stderr=subprocess.STDOUT, creationflags=0x08000000)
        return True, "机器人启动中"
    if sid == "tray":
        if _tray_running():
            return True, "托盘已在运行"
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                          "-ExecutionPolicy", "Bypass", "-File",
                          os.path.join(BASE_DIR, "tray.ps1")],
                         creationflags=0x08000000)
        return True, "托盘已启动"
    return False, "未知服务"


def service_stop(sid):
    try:
        if sid == "model":
            data = json.dumps({"model": Handler.cfg.get("model"), "keep_alive": 0}).encode("utf-8")
            req = Request(f"{OLLAMA}/api/generate", data=data,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=30) as r:
                r.read()
            return True, "模型已卸载，显存已释放"
        if sid == "gsv":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*api_v2*' } | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                           capture_output=True, timeout=30, creationflags=0x08000000)
            return True, "语音引擎已停止"
        if sid == "bot":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*qqbot.py*' } | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                           capture_output=True, timeout=30, creationflags=0x08000000)
            return True, "机器人已停止"
        if sid == "tray":
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tray.ps1*' -and "
                            "$_.CommandLine -notlike '*NonInteractive*' } | "
                            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                           capture_output=True, timeout=30, creationflags=0x08000000)
            return True, "托盘已停止"
    except Exception as e:
        return False, str(e)
    return False, "未知服务"
PRESET_DIR = os.path.join(BASE_DIR, "presets")
IMPORT_DIR = os.path.join(BASE_DIR, "imports")
PRESET_KINDS = {"personas": "人设", "bots": "机器人"}


def ensure_dirs():
    for k in PRESET_KINDS:
        os.makedirs(os.path.join(PRESET_DIR, k), exist_ok=True)
    os.makedirs(IMPORT_DIR, exist_ok=True)


def safe_name(name):
    name = (name or "").strip()
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name[:60] or "未命名"


def list_presets(kind):
    ensure_dirs()
    out = []
    d = os.path.join(PRESET_DIR, kind)
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(".json"):
            continue
        p = os.path.join(d, f)
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        out.append({
            "name": data.get("name") or os.path.splitext(f)[0],
            "file": f,
            "description": data.get("description", ""),
            "mtime": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p))),
        })
    return out


def read_preset(kind, name):
    ensure_dirs()
    p = os.path.join(PRESET_DIR, kind, safe_name(name) + ".json")
    if not os.path.isfile(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def write_preset(kind, name, data):
    ensure_dirs()
    nm = safe_name(name)
    if isinstance(data, dict):
        data["name"] = nm
    p = os.path.join(PRESET_DIR, kind, nm + ".json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


def delete_preset(kind, name):
    p = os.path.join(PRESET_DIR, kind, safe_name(name) + ".json")
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


# ───────────── 模型导入 ─────────────
_import_state = {"running": False, "kind": "", "name": "", "log": "", "ok": None,
                 "started": 0, "finished": 0}
_import_lock = threading.Lock()


def _imp_log(msg):
    with _import_lock:
        _import_state["log"] = (_import_state["log"] + msg + "\n")[-8000:]


def ollama_bin():
    exe = detect_ollama()
    return exe or "ollama"


def _run_import(kind, name, path=""):
    ok = False
    try:
        if kind == "pull":
            _imp_log("$ ollama pull %s" % name)
            proc = subprocess.Popen([ollama_bin(), "pull", name],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, creationflags=0x08000000)
            for line in proc.stdout:
                s = line.rstrip()
                if s:
                    _imp_log(s)
            ok = (proc.wait() == 0)
        elif kind == "gguf":
            if not os.path.isfile(path):
                _imp_log("找不到 GGUF 文件: %s" % path)
            else:
                _imp_log("GGUF: %s (%.2f GB)" % (path, os.path.getsize(path) / 1024**3))
                mf = os.path.join(IMPORT_DIR, safe_name(name) + ".Modelfile")
                with open(mf, "w", encoding="utf-8") as f:
                    f.write('FROM "%s"\n' % path.replace("\\", "/"))
                    f.write("PARAMETER num_ctx 4096\n")
                    f.write("PARAMETER temperature 0.85\n")
                _imp_log("生成 Modelfile: %s" % mf)
                _imp_log("$ ollama create %s -f %s" % (name, mf))
                proc = subprocess.Popen([ollama_bin(), "create", name, "-f", mf],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        bufsize=1, creationflags=0x08000000)
                for line in proc.stdout:
                    s = line.rstrip()
                    if s:
                        _imp_log(s)
                ok = (proc.wait() == 0)
        _imp_log("完成" if ok else "失败")
    except Exception as e:
        _imp_log("异常: %s" % e)
    with _import_lock:
        _import_state["running"] = False
        _import_state["ok"] = ok
        _import_state["finished"] = int(time.time())


def start_import(kind, name, path=""):
    with _import_lock:
        if _import_state["running"]:
            return False, "已有导入任务在进行"
        _import_state.update({"running": True, "kind": kind, "name": name,
                              "log": "", "ok": None, "started": int(time.time())})
    threading.Thread(target=_run_import, args=(kind, name, path), daemon=True).start()
    return True, "已开始"


def delete_model(name):
    try:
        r = subprocess.run([ollama_bin(), "rm", name], capture_output=True,
                           text=True, timeout=60, creationflags=0x08000000)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, str(e)


def list_gguf_files():
    """在常见位置搜索 GGUF 文件，供导入选择"""
    out = []
    roots = [IMPORT_DIR, r"D:\LocalAI\models", r"D:\LocalAI", os.path.expanduser("~/Downloads")]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for dirpath, _, files in os.walk(root):
                if dirpath.count(os.sep) - root.count(os.sep) > 3:
                    continue
                for f in files:
                    if f.lower().endswith(".gguf"):
                        p = os.path.join(dirpath, f)
                        try:
                            out.append({"path": p, "name": f,
                                        "gb": round(os.path.getsize(p) / 1024**3, 2)})
                        except Exception:
                            pass
                if len(out) > 200:
                    break
        except Exception:
            pass
    out.sort(key=lambda x: -x["gb"])
    return out[:60]


# ───────────── 完整自检 ─────────────
def run_selfcheck():
    global _checks
    _checks = []

    # 1 Ollama 可执行
    exe = detect_ollama()
    _add("Ollama 程序", bool(exe), exe or "未找到",
         "自动搜索 PATH / Program Files / 本地目录" if exe else "请安装 Ollama 或设置 OLLAMA_EXE", "ollama")

    # 2 服务
    ver = None
    try:
        ver = ollama_get("/api/version", timeout=4).get("version")
    except Exception:
        pass
    _add("推理服务", bool(ver), f"v{ver}" if ver else "未响应",
         f"127.0.0.1:11434 {'在线' if ver else '离线'}", "ollama")

    # 3 模型目录
    mdir = detect_models_dir()
    mb_cache = _cache.get("mds")
    size = None
    if mdir:
        now = time.time()
        if _cache.get("mds_time", 0) + 120 > now and _cache.get("mds_dir") == mdir:
            size = _cache["mds"]
        else:
            size = dir_size_gb(mdir)
            _cache.update({"mds": size, "mds_time": now, "mds_dir": mdir})
        mb_cache = size
    _add("模型目录", bool(mdir), mdir or "未找到",
         f"占用 {mb_cache} GB" if mdir else "未发现 manifests/blobs", "ollama")

    # 4 已装模型
    models, loaded = [], []
    if ver:
        try:
            tags = ollama_get("/api/tags").get("models", [])
            ps = {m.get("name"): m for m in ollama_get("/api/ps").get("models", [])}
            for m in tags:
                d = m.get("details") or {}
                lm = ps.get(m.get("name"))
                models.append({
                    "name": m.get("name"),
                    "size_gb": round((m.get("size") or 0) / 1024**3, 2),
                    "param": d.get("parameter_size"), "quant": d.get("quantization_level"),
                    "family": d.get("family"),
                    "modified": (m.get("modified_at") or "")[:19].replace("T", " "),
                    "loaded": bool(lm),
                    "size_vram_gb": round((lm.get("size_vram") or 0) / 1024**3, 2) if lm else 0,
                    "processor": lm.get("processor") if lm else None,
                    "context": lm.get("context_length") if lm else None,
                    "expires": (lm.get("expires_at") or "")[:19].replace("T", " ") if lm else None,
                })
            models.sort(key=lambda x: x["size_gb"], reverse=True)
            loaded = [m for m in models if m["loaded"]]
        except Exception:
            pass
    _add("已安装模型", len(models) > 0, f"{len(models)} 个",
         "、".join(m["name"] for m in models) or "无", "ollama")
    _cache["models"] = models
    _cache["loaded"] = loaded
    _cache["gpus"] = detect_gpus()

    # 5 GPU
    gpus = detect_gpus()
    prim = primary_gpu(gpus)
    if gpus:
        for g in gpus:
            mark = " ← 使用中" if g is prim else ""
            kind = "独显" if g.get("discrete") else "核显"
            cid = f" · {g['cuda_id']}" if g.get("cuda_id") else ""
            _add(f"GPU {g.get('index','?')} · {g['vendor']}", True, g["name"] + mark,
                 f"{kind}{cid} · 显存 {g['vram_total_mb']} MB · 驱动 {g.get('driver','?')}", "hw")
    else:
        _add("GPU", False, "未检测到", "无 nvidia-smi 也无可用显示适配器", "hw")

    # 6 CPU
    sy = detect_sys()
    _add("CPU", bool(sy.get("cpu")), sy.get("cpu", "未知"),
         f"{sy.get('cores')} 线程 · {sy.get('arch')}", "hw")

    # 7 内存
    mem = mem_info()
    _add("内存", bool(mem), f"{mem.get('total_gb')} GB",
         f"已用 {mem.get('used_gb')} GB ({mem.get('percent')}%)", "hw")

    # 8 磁盘
    disks = detect_disks()
    _add("磁盘", len(disks) > 0, f"{len(disks)} 个固定盘",
         " · ".join(f"{d['letter']}: {d['free_gb']}/{d['total_gb']}GB 可用" for d in disks), "hw")

    # 9 Python
    _add("Python", True, f"{sy.get('python')}",
         sy.get("python_exe", ""), "runtime")

    # 10 端口
    p_ol = port_open(11434); p_web = port_open(PORT)
    _add("端口 11434", p_ol, "监听中" if p_ol else "未监听", "Ollama API", "runtime")
    _add(f"端口 {PORT}", p_web, "监听中" if p_web else "未监听", "本控制台", "runtime")

    # 11 配置
    cfg_ok = os.path.isfile(CONFIG_PATH)
    _add("配置文件", True, "已加载" if cfg_ok else "使用默认",
         CONFIG_PATH, "runtime")

    # 12 人设
    persona = os.path.join(BASE_DIR, "persona.py")
    _add("人设文件", os.path.isfile(persona),
         "存在" if os.path.isfile(persona) else "缺失", persona, "runtime")

    ok_n = sum(1 for c in _checks if c["ok"])
    return {"items": _checks, "ok": ok_n, "total": len(_checks),
            "ts": int(time.time())}


# ───────────── 配置 ─────────────
# ───────────── 内存清理（扫描 + 选择性关闭第三方）─────────────
MEM_NEVER = {
    "system", "registry", "smss", "csrss", "wininit", "winlogon", "services", "lsass",
    "svchost", "dwm", "explorer", "fontdrvhost", "sihost", "ctfmon", "taskhostw",
    "runtimebroker", "searchhost", "startmenuexperiencehost", "shellexperiencehost",
    "applicationframehost", "textinputhost", "securityhealthsystray", "msmpeng",
    "audiodg", "conhost", "dllhost", "backgroundtaskhost", "lockapp",
    "ollama", "llama-server", "ollama_llama_server", "pythonw",
}
MEM_SUGGEST = {
    "msedge": "Edge 浏览器", "chrome": "Chrome 浏览器", "firefox": "Firefox",
    "msedgewebview2": "Edge WebView 常驻组件", "doubao": "豆包",
    "wechat": "微信", "weixin": "微信", "qq": "QQ", "qqex": "QQ 扩展",
    "baidunetdisk": "百度网盘", "thunder": "迅雷", "quark": "夸克",
    "360se": "360浏览器", "360safe": "360安全卫士", "dingtalk": "钉钉",
    "wps": "WPS", "teams": "Teams", "onedrive": "OneDrive",
    "spotify": "Spotify", "cloudmusic": "网易云音乐", "qqmusic": "QQ音乐",
    "kook": "KOOK", "discord": "Discord", "obs64": "OBS",
    "steam": "Steam", "wegame": "WeGame", "epicgameslauncher": "Epic",
}
MEM_CAUTION = {
    "pycharm64": "PyCharm 正在使用", "pycharm": "PyCharm 正在使用",
    "code": "VS Code 正在使用", "idea64": "IntelliJ 正在使用",
    "devenv": "Visual Studio 正在使用",
    "mumunxmain": "MuMu 模拟器（挂机中？）", "mumunxdevice": "MuMu 设备",
    "mumunxservice": "MuMu 服务", "blender": "Blender", "comfyui": "ComfyUI",
}


def _proc_list():
    out = []
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                           timeout=25, encoding="gbk", errors="replace", creationflags=0x08000000)
        for line in (r.stdout or "").splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) < 5:
                continue
            name = parts[0]
            try:
                pid = int(parts[1])
                mem = int(parts[4].replace(",", "").replace("K", "").strip() or 0) // 1024
            except Exception:
                continue
            out.append({"pid": pid, "name": name, "exe": name.lower().replace(".exe", ""), "mb": mem})
    except Exception:
        pass
    return out


def _free_gb():
    try:
        st = MEMORYSTATUSEX(); st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / 1024**3
    except Exception:
        return 0


def memory_scan(min_mb=100):
    procs = _proc_list()
    seen = {}
    for p in procs:
        if p["mb"] < min_mb:
            continue
        exe = p["exe"]
        if exe in MEM_NEVER:
            continue
        if exe in MEM_CAUTION:
            level, note = "caution", MEM_CAUTION[exe]
        elif exe in MEM_SUGGEST:
            level, note = "suggest", MEM_SUGGEST[exe]
        else:
            level, note = "unknown", ""
        if exe not in seen:
            seen[exe] = {"exe": exe, "name": p["name"], "mb": 0, "pids": [],
                         "level": level, "note": note, "count": 0}
        seen[exe]["mb"] += p["mb"]
        seen[exe]["count"] += 1
        seen[exe]["pids"].append(p["pid"])
    items = list(seen.values())
    order = {"suggest": 0, "unknown": 1, "caution": 2}
    items.sort(key=lambda x: (order.get(x["level"], 3), -x["mb"]))
    return {"ok": True, "free_gb": round(_free_gb(), 1),
            "total_mb": sum(i["mb"] for i in items), "items": items[:40]}


def memory_clean(groups):
    killed, freed, refused = 0, 0, []
    for g in groups or []:
        exe = (g.get("exe") or "").lower()
        level = g.get("level") or "unknown"
        if level == "never" or exe in MEM_NEVER:
            refused.append(exe or "?")
            continue
        pids = g.get("pids") or []
        per = (int(g.get("mb") or 0) // max(len(pids), 1))
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                               timeout=15, creationflags=0x08000000)
                killed += 1
                freed += per
            except Exception:
                pass
    return {"ok": True, "killed": killed, "freed_mb": freed,
            "refused": refused, "free_gb": round(_free_gb(), 1)}


def bot_stats():
    """机器人运行数据统计（读记忆库）"""
    import sqlite3
    db = os.path.join(BASE_DIR, "data", "memory.db")
    out = {"ok": True, "msgs_1h": 0, "msgs_24h": 0, "replies_24h": 0,
           "users_7d": 0, "groups_7d": 0, "facts": 0, "active_now": 0,
           "hourly": [0] * 12, "ts": int(time.time())}
    try:
        c = sqlite3.connect(db, timeout=8)
        now = int(time.time())
        hour_ago = now - 3600
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        out["msgs_1h"] = c.execute("SELECT COUNT(*) FROM messages WHERE ts>?", (hour_ago,)).fetchone()[0]
        out["msgs_24h"] = c.execute("SELECT COUNT(*) FROM messages WHERE ts>?", (day_ago,)).fetchone()[0]
        out["replies_24h"] = c.execute(
            "SELECT COUNT(*) FROM messages WHERE ts>? AND role='assistant'", (day_ago,)).fetchone()[0]
        out["users_7d"] = c.execute(
            "SELECT COUNT(DISTINCT chat_key) FROM messages WHERE ts>? AND chat_key LIKE 'u:%'",
            (week_ago,)).fetchone()[0]
        out["groups_7d"] = c.execute(
            "SELECT COUNT(DISTINCT chat_key) FROM messages WHERE ts>? AND chat_key LIKE 'g:%'",
            (week_ago,)).fetchone()[0]
        try:
            out["facts"] = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        except Exception:
            pass
        # 最近 12 小时每小时消息量
        buckets = []
        for i in range(11, -1, -1):
            start = now - i * 3600 - 3600
            end = start + 3600
            n = c.execute("SELECT COUNT(*) FROM messages WHERE ts>=? AND ts<?", (start, end)).fetchone()[0]
            buckets.append(n)
        out["hourly"] = buckets
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


# ───────────── 版本检测与官方更新 ─────────────
NAPCAT_REPO = "NapNeko/NapCatQQ"
NAPCAT_ASSET = "NapCat.Shell.zip"


def _file_version(path):
    try:
        import subprocess as sp
        r = sp.run(["powershell", "-NoProfile", "-Command",
                    "(Get-Item '%s').VersionInfo.FileVersion" % path],
                   capture_output=True, text=True, timeout=15,
                   encoding="utf-8", errors="replace", creationflags=0x08000000)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _napcat_version():
    import re as _re
    # NapCat 自身版本号写在 napcat.mjs 里（如 4.18.41）
    for p in (os.path.join(NAPCAT_DIR, "napcat", "napcat.mjs"),
              os.path.join(NAPCAT_DIR, "napcat.mjs")):
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    m = _re.search(r"\b(4\.\d+\.\d+)\b", f.read(400000))
                if m:
                    return m.group(1)
        except Exception:
            pass
    for p in (os.path.join(NAPCAT_DIR, "package.json"),
              os.path.join(NAPCAT_DIR, "napcat", "package.json")):
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8-sig") as f:
                    v = json.load(f).get("version", "")
                if v and not v.startswith("9.9."):
                    return v
        except Exception:
            pass
    return ""


def _qq_version():
    exe = _qq_exe()
    if not exe:
        return "", ""
    return exe, _file_version(exe)


def _latest_napcat():
    """查最新版本与下载地址（GitHub 官方发布渠道）"""
    try:
        data = json.loads(urlopen(
            "https://api.github.com/repos/%s/releases/latest" % NAPCAT_REPO,
            timeout=25).read().decode("utf-8"))
        tag = (data.get("tag_name") or "").lstrip("v")
        url = ""
        for a in data.get("assets") or []:
            if a.get("name") == NAPCAT_ASSET:
                url = a.get("browser_download_url") or ""
                break
        return {"ok": bool(url), "version": tag, "url": url,
                "published": (data.get("published_at") or "")[:10]}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def update_napcat():
    """从官方下载最新 NapCat Shell 并覆盖安装（保留 config 目录）"""
    info = _latest_napcat()
    if not info.get("ok"):
        return False, "查询最新版失败：%s" % info.get("msg", "")
    cur = _napcat_version()
    if cur and info["version"] and cur == info["version"]:
        return True, "NapCat 已是最新（v%s）" % cur
    import tempfile
    import zipfile
    tmp = tempfile.mkdtemp(prefix="napcat_up_")
    zpath = os.path.join(tmp, "shell.zip")
    try:
        with urlopen(info["url"], timeout=300) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        extract = os.path.join(tmp, "x")
        os.makedirs(extract, exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(extract)
        # 备份并覆盖（跳过 config）
        import shutil
        bak = NAPCAT_DIR + ".bak-" + time.strftime("%Y%m%d%H%M%S")
        if os.path.isdir(bak):
            shutil.rmtree(bak, ignore_errors=True)
        try:
            shutil.copytree(NAPCAT_DIR, bak)
        except Exception:
            bak = ""
        for root, dirs, files in os.walk(extract):
            rel = os.path.relpath(root, extract)
            if rel.split(os.sep)[0] == "config":
                continue
            dst = os.path.join(NAPCAT_DIR, rel) if rel != "." else NAPCAT_DIR
            os.makedirs(dst, exist_ok=True)
            for fn in files:
                try:
                    shutil.copy2(os.path.join(root, fn), os.path.join(dst, fn))
                except Exception:
                    pass
        return True, "NapCat 已更新到 v%s（旧版备份在 %s）" % (info["version"], bak or "未备份")
    except Exception as e:
        return False, "更新失败：%s" % e
    finally:
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def selfcheck_full():
    """自检：各组件状态 + 版本 + 可用修复动作"""
    items = []

    def add(name, ok, detail="", fix=None, level=None):
        items.append({"name": name, "ok": bool(ok),
                      "level": level or ("ok" if ok else "bad"),
                      "detail": detail, "fix": fix})

    # Python
    add("Python 运行环境", True, sys.version.split()[0])
    # 控制台
    add("控制台服务", port_open(PORT), "端口 %d" % PORT)
    # Ollama
    try:
        ver = json.loads(urlopen(f"{OLLAMA}/api/version", timeout=6).read().decode()).get("version", "")
        add("Ollama 服务", True, "v" + ver)
    except Exception:
        add("Ollama 服务", False, "未响应 11434")
    # 模型
    model = Handler.cfg.get("model")
    try:
        names = [m.get("name") for m in ollama_get("/api/tags").get("models", [])]
        has = model in names if model else False
        add("模型 %s" % model, has, "已安装 %d 个模型" % len(names))
    except Exception:
        add("模型 %s" % model, False, "无法读取模型列表")
    # 显存
    try:
        gpus = detect_gpus()
        if gpus:
            g = gpus[0]
            add("显存", True, "%s / %s MB · %s" % (g.get("vram_used_mb"), g.get("vram_total_mb"), g.get("name", "")))
        else:
            add("显存", False, "未检测到 GPU", level="warn")
    except Exception as e:
        add("显存", False, "读取失败: %s" % e, level="warn")
    # 内存库
    try:
        st = memory_store().stats()
        add("记忆库", True, "%d 条 / %.1f MB" % (st["rows"], st.get("size_kb", 0) / 1024))
    except Exception as e:
        add("记忆库", False, str(e))
    # QQ
    qq_exe, qq_ver = _qq_version()
    add("QQ 安装", bool(qq_exe), qq_ver or "未找到", level="ok" if qq_exe else "bad")
    # NapCat + 版本匹配
    nc_ver = _napcat_version()
    nc_ok = bool(nc_ver)
    latest = _latest_napcat()
    newer = bool(latest.get("ok") and nc_ver and latest["version"] != nc_ver)
    detail = "v%s" % nc_ver if nc_ok else "未安装"
    if latest.get("ok"):
        detail += "（官方最新 v%s）" % latest["version"]
    add("NapCat", nc_ok, detail,
        fix=("napcat_update" if newer else None),
        level="warn" if (nc_ok and newer) else ("ok" if nc_ok else "bad"))
    # 兼容性提示
    if qq_exe and nc_ok:
        add("NapCat × QQ 兼容", True,
            "QQ %s / NapCat %s（如注入报「文件已损坏」，点更新 NapCat 或降级 QQ）" % (qq_ver, nc_ver),
            level="ok")
    # 语音引擎
    add("语音引擎 GPT-SoVITS", port_open(9880), "端口 9880",
        level="ok" if port_open(9880) else "warn")
    # 机器人
    add("QQ 机器人", port_open(8081), "端口 8081",
        level="ok" if port_open(8081) else "warn")
    # NapCat OneBot
    add("OneBot 接口", port_open(3000), "端口 3000",
        level="ok" if port_open(3000) else "warn")

    bad = sum(1 for i in items if i["level"] == "bad")
    warn = sum(1 for i in items if i["level"] == "warn")
    return {"ok": bad == 0, "bad": bad, "warn": warn, "items": items,
            "napcat_version": nc_ver, "qq_version": qq_ver}


# ───────────── 控制台鉴权 ─────────────
TOKEN_PATH = os.path.join(BASE_DIR, "data", ".console_token")


def console_token():
    """本机固定的访问令牌（首次自动生成）"""
    try:
        if os.path.isfile(TOKEN_PATH):
            t = open(TOKEN_PATH, "r", encoding="ascii").read().strip()
            if len(t) >= 24:
                return t
    except Exception:
        pass
    import secrets
    t = secrets.token_hex(24)
    try:
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="ascii") as f:
            f.write(t)
    except Exception:
        pass
    return t


CONSOLE_TOKEN = console_token()
# 这些接口允许无鉴权访问（只读、无副作用）
OPEN_GET = {"/", "/api/health", "/api/status", "/index.html"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if not cfg.get("model"):
        try:
            tags = ollama_get("/api/tags").get("models", [])
            names = sorted([m["name"] for m in tags], key=lambda n: len(n))
            cfg["model"] = names[0] if names else ""
        except Exception:
            cfg["model"] = ""
    if not cfg.get("system_prompt"):
        try:
            from persona import SYSTEM_PROMPT
            cfg["system_prompt"] = SYSTEM_PROMPT
        except Exception:
            cfg["system_prompt"] = ""
    return cfg


def save_config(cfg):
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in cfg.items() if k in DEFAULT_CONFIG})
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = None

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        try:
            if full.endswith("index.html"):
                self.send_header("Set-Cookie",
                    "fishchat_token=%s; Path=/; SameSite=Strict" % CONSOLE_TOKEN)
        except Exception:
            pass
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _send_token_cookie(self):
        try:
            self._extra_headers = [("Set-Cookie",
                "fishchat_token=%s; Path=/; SameSite=Strict" % CONSOLE_TOKEN)]
        except Exception:
            pass

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/").split("?")[0]))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self.send_error(404); return
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1].lower(),
                                                  "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        try:
            if full.endswith("index.html"):
                self.send_header("Set-Cookie",
                    "fishchat_token=%s; Path=/; SameSite=Strict" % CONSOLE_TOKEN)
        except Exception:
            pass
        self.end_headers()
        self.wfile.write(body)

    def _serve_audio(self):
        """提供合成的语音文件"""
        q = parse_qs(urlparse(self.path).query)
        name = os.path.basename((q.get("name") or [""])[0])
        base = os.path.normpath(os.path.join(BASE_DIR, "data", "voice"))
        full = os.path.normpath(os.path.join(base, name))
        if not name or not full.startswith(base) or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/status":
            chk = run_selfcheck()
            gpus = _cache.get("gpus") or detect_gpus()
            return self._json({
                "ok": any(c["name"] == "推理服务" and c["ok"] for c in chk["items"]),
                "ts": chk["ts"],
                "ollama": next((c["value"][1:] for c in chk["items"]
                                if c["name"] == "推理服务" and c["ok"]), None),
                "models": _cache.get("models", []),
                "loaded": _cache.get("loaded", []),
                "gpu": primary_gpu(gpus),
                "gpus": gpus,
                "mem": mem_info(),
                "disks": detect_disks(),
                "sys": detect_sys(),
                "models_dir": detect_models_dir(),
                "models_size_gb": _cache.get("mds", 0),
                "check": {"ok": chk["ok"], "total": chk["total"]},
                "config": Handler.cfg,
            })
        if p == "/api/selfcheck":
            return self._json(selfcheck_full())
        if p == "/api/health":
            return self._json({"ok": True, "port": PORT, "ts": int(time.time())})
        if p == "/api/napcat/qr":
            import base64 as _b64
            cand = [
                os.path.join(NAPCAT_DIR, "napcat", "cache", "qrcode.png"),
                os.path.join(NAPCAT_DIR, "cache", "qrcode.png"),
            ]
            for f in cand:
                if os.path.isfile(f):
                    try:
                        data = open(f, "rb").read()
                        if len(data) > 200:
                            return self._json({
                                "ok": True,
                                "mtime": int(os.path.getmtime(f)),
                                "age": int(time.time() - os.path.getmtime(f)),
                                "png": _b64.b64encode(data).decode("ascii"),
                            })
                    except Exception:
                        pass
            return self._json({"ok": False, "msg": "还没有二维码，先启动 NapCat"})
        if p == "/api/napcat/qr/url":
            # 从 NapCat 控制台输出里找解码后的 URL
            import re as _re
            urls = []
            for f in (os.path.join(NAPCAT_DIR, "napcat", "cache", "qrcode.txt"),
                      os.path.join(BASE_DIR, "logs", "napcat.log")):
                if os.path.isfile(f):
                    try:
                        txt = open(f, "r", encoding="utf-8", errors="replace").read()
                        urls += _re.findall(r"https://txz\.qq\.com/\S+", txt)
                    except Exception:
                        pass
            return self._json({"ok": bool(urls), "url": urls[-1] if urls else ""})
        if p == "/api/napcat/status":
            return self._json(napcat_status())
        if p == "/api/napcat/groups":
            return self._json(napcat_groups())
        if p == "/api/napcat/friends":
            return self._json(napcat_friends())
        if p == "/api/memory":
            return self._json(memory_overview())
        if p == "/api/services":
            return self._json(services_state())
        if p == "/api/mem/scan":
            return self._json(memory_scan())
        if p == "/api/douyin/status":
            try:
                import douyin as _dy
                return self._json({"ok": True, "data": _dy.status()})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)})
        if p == "/api/douyin/drafts":
            try:
                import douyin as _dy
                return self._json({"ok": True, "items": _dy.load_drafts()})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)})
        if p == "/api/stats":
            return self._json(bot_stats())
        if p == "/api/log":
            q = parse_qs(urlparse(self.path).query)
            n = int((q.get("n") or ["10"])[0])
            n = max(1, min(n, 60))
            path = os.path.join(BASE_DIR, "logs", "bot.log")
            lines = []
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.read().splitlines()[-n:]
                except Exception:
                    lines = []
            # 去掉 ANSI 转义
            import re as _re
            lines = [_re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]
            return self._json({"ok": True, "lines": lines})
        if p == "/api/memes":
            d = os.path.join(BASE_DIR, "data", "memes")
            items = []
            if os.path.isdir(d):
                items = [f for f in sorted(os.listdir(d))
                         if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]
            return self._json({"ok": True, "items": items})
        if p == "/api/meme":
            q = parse_qs(urlparse(self.path).query)
            name = os.path.basename((q.get("name") or [""])[0])
            base = os.path.normpath(os.path.join(BASE_DIR, "data", "memes"))
            full = os.path.normpath(os.path.join(base, name))
            if not name or not full.startswith(base) or not os.path.isfile(full):
                self.send_error(404)
                return
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/tts/voices":
            try:
                import tts as t
                return self._json({"ok": True, "voices": t.VOICES, "default": t.DEFAULT_VOICE})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if p == "/api/tts/file":
            return self._serve_audio()
        if p == "/api/tts/status":
            try:
                import tts as t
                cfg = t.load_cfg()
                return self._json({
                    "ok": True,
                    "gsv_alive": t.gsv_alive(cfg.get("gsv_url")),
                    "gsv_url": cfg.get("gsv_url"),
                    "gsv_ref": cfg.get("gsv_ref_audio"),
                    "gsv_ref_exists": os.path.isfile(cfg.get("gsv_ref_audio") or ""),
                    "engine": cfg.get("engine"),
                    "edge_voice": cfg.get("edge_voice"),
                    "gsv_dir": t.detect_gsv_dir(),
                    "voices": t.VOICES,
                })
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if p == "/api/media/list":
            return self._json(media_list())
        if p == "/api/presets":
            q = parse_qs(urlparse(self.path).query)
            kind = (q.get("kind") or ["personas"])[0]
            if kind not in PRESET_KINDS:
                return self._json({"ok": False, "error": "bad kind"}, 400)
            return self._json({"ok": True, "items": list_presets(kind)})
        if p == "/api/presets/item":
            q = parse_qs(urlparse(self.path).query)
            kind = (q.get("kind") or ["personas"])[0]
            name = (q.get("name") or [""])[0]
            return self._json(read_preset(kind, name) or {})
        if p == "/api/models/import/status":
            with _import_lock:
                return self._json(dict(_import_state))
        if p == "/api/models/gguf":
            return self._json({"files": list_gguf_files()})
        if p == "/api/bot/config":
            try:
                return self._json(json.load(open(BOT_CONFIG_PATH, encoding="utf-8-sig")))
            except Exception:
                return self._json({})
        if p == "/api/config":
            return self._json(Handler.cfg)
        if p == "/favicon.ico":
            self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
        return self._static(p)

    def _authorized(self):
        """写操作必须带正确的 X-Auth-Token（阻断本机 CSRF）"""
        tok = self.headers.get("X-Auth-Token") or ""
        if tok == CONSOLE_TOKEN:
            return True
        # 兼容：也接受 cookie 里的 token（方便直接浏览器调试）
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fishchat_token" and v == CONSOLE_TOKEN:
                return True
        return False

    def _deny(self):
        try:
            self._json({"ok": False, "msg": "未授权：缺少或错误的访问令牌"}, 403)
        except Exception:
            pass

    def do_POST(self):
        p = self.path.split("?")[0]
        if p.startswith("/api/") and not self._authorized():
            return self._deny()
        if p == "/api/config":
            body = self._read_json()
            pn = body.pop("persona_name", None)   # 人设切换 → 自动带出唤醒词
            Handler.cfg = save_config({**self.cfg, **body})
            if pn:
                try:
                    src = os.path.join(BASE_DIR, "presets", "personas", pn + ".json")
                    if os.path.isfile(src):
                        pdata = json.load(open(src, "r", encoding="utf-8-sig"))
                        ww = pdata.get("wake_words") or []
                        md = pdata.get("meme_dir") or ""
                        if ww:
                            cur = {}
                            if os.path.isfile(BOT_CONFIG_PATH):
                                try:
                                    cur = json.load(open(BOT_CONFIG_PATH, encoding="utf-8-sig"))
                                except Exception:
                                    cur = {}
                            cur["wake_words"] = ww
                            cur["persona_name"] = pn
                            if md:
                                cur["meme_dir"] = md
                            with open(BOT_CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
                                json.dump(cur, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
            return self._json({"ok": True, "config": Handler.cfg})
        if p == "/api/load":
            return self._load()
        if p == "/api/unload":
            return self._unload()
        if p == "/api/chat":
            return self._chat()
        if p == "/api/presets":
            body = self._read_json()
            kind = body.get("kind", "personas")
            if kind not in PRESET_KINDS:
                return self._json({"ok": False, "error": "bad kind"}, 400)
            name = body.get("name", "")
            if not name:
                return self._json({"ok": False, "error": "缺少预设名"}, 400)
            path = write_preset(kind, name, body.get("data", {}))
            return self._json({"ok": True, "path": path, "items": list_presets(kind)})
        if p == "/api/models/import":
            body = self._read_json()
            kind = body.get("kind")
            name = (body.get("name") or "").strip()
            path = body.get("path", "")
            if not name:
                return self._json({"ok": False, "error": "缺少模型名"}, 400)
            if kind not in ("pull", "gguf"):
                return self._json({"ok": False, "error": "kind 必须是 pull 或 gguf"}, 400)
            started, msg = start_import(kind, name, path)
            return self._json({"ok": started, "msg": msg})
        if p == "/api/bot/config":
            body = self._read_json()
            cur = {}
            if os.path.isfile(BOT_CONFIG_PATH):
                try:
                    cur = json.load(open(BOT_CONFIG_PATH, encoding="utf-8-sig"))
                except Exception:
                    cur = {}
            cur.update(body)
            with open(BOT_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=2)
            return self._json({"ok": True, "config": cur})
        if p == "/api/douyin/start":
            body = self._read_json()
            try:
                import douyin as _dy
                ok, msg = _dy.start(body.get("serial"), body.get("min_interval", 90))
                return self._json({"ok": ok, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)})
        if p == "/api/douyin/stop":
            try:
                import douyin as _dy
                ok, msg = _dy.stop()
                return self._json({"ok": ok, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)})
        if p == "/api/douyin/clear":
            try:
                import douyin as _dy
                ok, msg = _dy.clear_drafts()
                return self._json({"ok": ok, "msg": msg})
            except Exception as e:
                return self._json({"ok": False, "msg": str(e)})
        if p == "/api/update":
            body = self._read_json()
            t = body.get("target")
            if t == "napcat":
                ok, msg = update_napcat()
                return self._json({"ok": ok, "msg": msg})
            if t == "napcat_latest":
                return self._json(_latest_napcat())
            return self._json({"ok": False, "msg": "未知更新目标"}, 400)
        if p == "/api/napcat/start":
            body = self._read_json()
            ok, msg = start_napcat(body.get("mode", "admin"))
            return self._json({"ok": ok, "msg": msg})
        if p == "/api/napcat/killqq":
            ok, msg = kill_qq()
            return self._json({"ok": ok, "msg": msg})
        if p == "/api/memory/cleanup":
            try:
                st = memory_store()
                res = st.cleanup()
                # 顺带轮转日志
                lp = os.path.join(BASE_DIR, "logs", "bot.log")
                rotated = False
                limit = float(_bot_cfg().get("log_max_mb", 2) or 2) * 1024 * 1024
                if os.path.isfile(lp) and os.path.getsize(lp) > limit:
                    old = lp + ".1"
                    if os.path.isfile(old):
                        os.remove(old)
                    os.replace(lp, old)
                    rotated = True
                return self._json({"ok": True, "result": res, "log_rotated": rotated,
                                   "stats": st.stats()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/mem/clean":
            body = self._read_json()
            return self._json(memory_clean(body.get("groups")))
        if p == "/api/services":
            body = self._read_json()
            sid = body.get("id")
            act = body.get("action")
            if act == "autostart":
                auto = save_autostart({sid: bool(body.get("enabled"))})
                return self._json({"ok": True, "autostart": auto})
            if act == "start":
                ok, msg = service_start(sid)
                return self._json({"ok": ok, "msg": msg})
            if act == "stop":
                ok, msg = service_stop(sid)
                return self._json({"ok": ok, "msg": msg})
            return self._json({"ok": False, "msg": "未知操作"}, 400)
        if p == "/api/memory/clear":
            body = self._read_json()
            key = (body.get("key") or "").strip()
            try:
                st = memory_store()
                n = st.clear(key or None)
                return self._json({"ok": True, "deleted": n, "stats": st.stats()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/tts/test":
            body = self._read_json()
            text = (body.get("text") or "诶嘿～你来啦！").strip()
            try:
                import tts as t
                path, engine = t.synthesize(text, body.get("voice"))
                if not path:
                    return self._json({"ok": False, "error": "合成失败"}, 500)
                name = os.path.basename(path)
                return self._json({"ok": True, "engine": engine, "name": name,
                                   "url": "/api/tts/file?name=" + name,
                                   "kb": round(os.path.getsize(path) / 1024, 1)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/tts/config":
            body = self._read_json()
            try:
                import tts as t
                cfg = t.save_cfg(body or {})
                return self._json({"ok": True, "config": cfg})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/gsv/start":
            try:
                import tts as t
                d = t.detect_gsv_dir()
                if not d:
                    return self._json({"ok": False, "error": "未找到 GPT-SoVITS 目录"})
                py = os.path.join(d, "runtime", "python.exe")
                if not os.path.isfile(py):
                    py = "python"
                subprocess.Popen(
                    [py, "api_v2.py", "-a", "127.0.0.1", "-p", "9880",
                     "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
                    cwd=d, creationflags=0x08000000,
                    stdout=open(os.path.join(BASE_DIR, "logs", "gsv.log"), "ab"),
                    stderr=subprocess.STDOUT)
                return self._json({"ok": True, "msg": "正在启动，约需 40 秒"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/gsv/stop":
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*api_v2.py*' } "
                     "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                    capture_output=True, timeout=20)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        self.send_error(404)

    def do_DELETE(self):
        p = self.path.split("?")[0]
        q = parse_qs(urlparse(self.path).query)
        if p == "/api/presets":
            kind = (q.get("kind") or ["personas"])[0]
            name = (q.get("name") or [""])[0]
            if kind not in PRESET_KINDS:
                return self._json({"ok": False, "error": "bad kind"}, 400)
            return self._json({"ok": delete_preset(kind, name), "items": list_presets(kind)})
        if p == "/api/models":
            name = (q.get("name") or [""])[0]
            if not name:
                return self._json({"ok": False, "error": "缺少模型名"}, 400)
            ok, out = delete_model(name)
            return self._json({"ok": ok, "output": out})
        self.send_error(404)

    def _load(self):
        c = self.cfg
        payload = {"model": c["model"], "prompt": "你好", "stream": False,
                   "think": bool(c.get("think", False)),
                   "keep_alive": c.get("keep_alive", "30m"),
                   "options": {"num_ctx": int(c.get("num_ctx", 4096)),
                               "num_predict": 4, "num_gpu": int(c.get("num_gpu", -1))}}
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = Request(f"{OLLAMA}/api/generate", data=data,
                          headers={"Content-Type": "application/json; charset=utf-8"})
            with urlopen(req, timeout=900) as r:
                r.read()
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def _unload(self):
        try:
            data = json.dumps({"model": self.cfg["model"], "keep_alive": 0}).encode("utf-8")
            req = Request(f"{OLLAMA}/api/generate", data=data,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=30) as r:
                r.read()
            return self._json({"ok": True})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 500)

    def _chat(self):
        c = self.cfg
        msgs = self._read_json().get("messages", [])
        sp = c.get("system_prompt") or ""
        payload = {"model": c["model"],
                   "messages": ([{"role": "system", "content": sp}] if sp else []) + msgs,
                   "stream": True, "think": bool(c.get("think", False)),
                   "keep_alive": c.get("keep_alive", "30m"),
                   "options": {"num_ctx": int(c.get("num_ctx", 4096)),
                               "temperature": float(c.get("temperature", .85)),
                               "top_p": float(c.get("top_p", .92)),
                               "top_k": int(c.get("top_k", 40)),
                               "repeat_penalty": float(c.get("repeat_penalty", 1.08)),
                               "num_predict": int(c.get("num_predict", 512)),
                               "num_gpu": int(c.get("num_gpu", -1))}}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = Request(f"{OLLAMA}/api/chat", data=data,
                          headers={"Content-Type": "application/json; charset=utf-8"})
            with urlopen(req, timeout=900) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    piece = (obj.get("message") or {}).get("content")
                    if piece:
                        self.wfile.write(("data: " + json.dumps({"delta": piece},
                                         ensure_ascii=False) + "\n\n").encode("utf-8"))
                        self.wfile.flush()
                    if obj.get("done"):
                        self.wfile.write(("data: " + json.dumps({"stats": {
                            "eval_count": obj.get("eval_count"),
                            "eval_duration": obj.get("eval_duration"),
                            "prompt_eval_count": obj.get("prompt_eval_count"),
                            "prompt_eval_duration": obj.get("prompt_eval_duration"),
                            "load_duration": obj.get("load_duration"),
                            "total_duration": obj.get("total_duration"),
                        }}, ensure_ascii=False) + "\n\n").encode("utf-8"))
                        break
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.wfile.write(("data: " + json.dumps({"error": str(e)},
                                 ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass


def main():
    Handler.cfg = load_config()
    try:
        v = ollama_get("/api/version", timeout=5)
        print(f"[OK] Ollama {v.get('version')}   模型目录: {detect_models_dir()}")
    except Exception as e:
        print(f"[!!] 未连接 Ollama: {e}"); sys.exit(1)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[OK] 控制台: http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
