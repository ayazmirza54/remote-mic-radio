"""
Remote Mic — control your Teams / Meet mic from your phone.

Runs on your Windows laptop. Your phone opens http://<laptop-ip>:8765/?t=<token>
on the same Wi-Fi (or your phone's hotspot).

What it can do
  1. Hardware mic kill-switch   - mutes every active microphone at the Windows level
                                  (built-in, headset, USB). Works for local Teams/Meet
                                  AND for RDP "remote audio recording", because the
                                  RDP client can only send what your mic captures.
  2. Teams / Meet hotkey        - sends Ctrl+Shift+M (Teams) or Ctrl+D (Meet) to the
                                  right window: local Teams, a browser Meet tab, or an
                                  RDP window (mstsc / msrdc) so it reaches Teams inside
                                  the remote session. Focus goes back to whatever you
                                  were using.
  3. Per-app mic mute           - mutes one app's microphone stream only (e.g. only
                                  mstsc.exe = only the RDP session goes silent, or only
                                  ms-teams.exe). "Sticky": re-applied every second, so
                                  it holds when Teams reopens the mic.

Usage
  RemoteMic.exe               # built by GitHub Actions: no Python needed, no console.
                              # Opens a pairing page (QR code) in your browser.
                              # Tick "Start with Windows" there once and forget it.
  python server.py            # same, from source
  python server.py --quiet    # don't open the pairing page (used at Windows login)
  python server.py --install / --uninstall   # start with Windows on/off
  python server.py --mock     # fake backend, for trying the UI on any OS
"""

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FROZEN = getattr(sys, "frozen", False)  # running as the PyInstaller .exe
# Bundled files live in a temp dir when frozen; config/log sit next to the .exe.
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BUNDLE, "static")
CONFIG_PATH = os.path.join(HERE, "config.json")

if sys.stdout is None or sys.stderr is None:  # --noconsole exe: print() would crash
    _log = open(os.path.join(HERE, "remote-mic.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _log

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_command():
    if FROZEN:
        return f'"{sys.executable}" --quiet'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}" --quiet'


def autostart(enable=None):
    """Read (enable=None) or set the per-user 'start with Windows' entry. No admin needed."""
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if enable is True:
            winreg.SetValueEx(k, "RemoteMic", 0, winreg.REG_SZ, autostart_command())
        elif enable is False:
            try:
                winreg.DeleteValue(k, "RemoteMic")
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(k, "RemoteMic")
            return True
        except FileNotFoundError:
            return False

DEFAULT_CONFIG = {
    "port": 8765,
    "token": None,  # generated on first run
    "teams_processes": ["ms-teams.exe", "teams.exe"],
    "rdp_processes": ["mstsc.exe", "msrdc.exe", "msrdcw.exe", "windowsapp.exe"],
    "browser_processes": ["chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe"],
    "meet_title_markers": ["Meet -", "Meet –", "meet.google.com"],
    "hotkeys": {"teams": ["ctrl", "shift", "m"], "meet": ["ctrl", "d"],
                "teams_ptt": ["ctrl", "space"]},  # Teams: hold to temporarily unmute
    # Push-to-talk safety: if the phone stops sending heartbeats (screen locked,
    # Wi-Fi dropped) for this long, the talk is released automatically.
    "ptt_timeout_sec": 2.5,
    "restore_focus": True,
    # Optional: auto-mute the mic when the agent starts
    "mute_on_start": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:  # keep running with defaults
            print(f"[warn] could not read config.json: {e}")
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(9)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    return cfg


# --------------------------------------------------------------------------------------
# Windows backend
# --------------------------------------------------------------------------------------
class WindowsBackend:
    VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "space": 0x20}
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001

    def __init__(self, cfg):
        import ctypes.wintypes as wt  # noqa: F401  (Windows only)

        self.cfg = cfg
        self.u32 = ctypes.windll.user32
        self.k32 = ctypes.windll.kernel32
        # Explicit HWND types so 64-bit handles never overflow ctypes' default int.
        H = ctypes.c_void_p
        for fn, args in {"SetForegroundWindow": [H], "BringWindowToTop": [H], "IsIconic": [H],
                         "ShowWindow": [H, ctypes.c_int], "IsWindowVisible": [H],
                         "GetWindowTextLengthW": [H], "IsHungAppWindow": [H],
                         "GetWindowTextW": [H, ctypes.c_wchar_p, ctypes.c_int],
                         "GetWindowThreadProcessId": [H, ctypes.POINTER(ctypes.c_ulong)]}.items():
            getattr(self.u32, fn).argtypes = args
        self.u32.GetForegroundWindow.restype = H
        self._proc_cache = {}
        self._names = {}               # device id -> friendly name (reading it is slow)
        self.mic_kill = False          # "mute all mics" switch
        self.sticky_apps = {}          # exe -> desired mute (bool)
        self.ptt = None                # active push-to-talk hold, if any
        self.ptt_deadline = 0.0
        self._setup_audio()

    # ---------------- processes & windows ----------------
    def exe_of_pid(self, pid):
        if pid in self._proc_cache:
            return self._proc_cache[pid]
        name = ""
        h = self.k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(1024)
            if self.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            self.k32.CloseHandle(h)
        if len(self._proc_cache) > 500:
            self._proc_cache.clear()
        self._proc_cache[pid] = name
        return name

    def list_windows(self):
        """Visible top-level windows in z-order (most recently active first)."""
        wins = []
        u32 = self.u32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def cb(hwnd, _):
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            pid = ctypes.c_ulong()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            wins.append({"hwnd": int(hwnd), "title": buf.value, "pid": pid.value,
                         "exe": self.exe_of_pid(pid.value)})
            return True

        u32.EnumWindows(WNDENUMPROC(cb), 0)
        return wins

    def classify(self, w):
        exe = w["exe"]
        if exe in self.cfg["teams_processes"]:
            return "teams"
        if exe in self.cfg["rdp_processes"]:
            return "rdp"
        if exe in self.cfg["browser_processes"] and any(
                m.lower() in w["title"].lower() for m in self.cfg["meet_title_markers"]):
            return "meet"
        return None

    @staticmethod
    def _teams_score(title):
        t = title.lower()
        score = 0
        if "meeting" in t or "call" in t or "| microsoft teams" not in t:
            score += 2  # meeting windows usually carry the meeting name
        for nav in ("chat |", "activity |", "calendar |", "teams |", "files |", "onedrive |", "apps |"):
            if t.startswith(nav):
                score -= 3
        return score

    def find_target(self, app, target="auto", hwnd=None):
        wins = self.list_windows()
        if target == "hwnd" and hwnd:
            for w in wins:
                if w["hwnd"] == int(hwnd):
                    return w
            return None
        kinds = {w["hwnd"]: self.classify(w) for w in wins}
        local_kind = "teams" if app == "teams" else "meet"
        local = [w for w in wins if kinds[w["hwnd"]] == local_kind]
        rdp = [w for w in wins if kinds[w["hwnd"]] == "rdp"]
        if app == "teams" and local:
            local.sort(key=lambda w: -self._teams_score(w["title"]))  # stable: keeps z-order on ties
        if target == "local":
            return local[0] if local else None
        if target == "rdp":
            return rdp[0] if rdp else None
        # auto: whichever of (best local, top RDP) is higher in z-order
        cands = ([local[0]] if local else []) + ([rdp[0]] if rdp else [])
        if not cands:
            return None
        order = [w["hwnd"] for w in wins]
        return min(cands, key=lambda w: order.index(w["hwnd"]))

    def _focus(self, hwnd):
        u32 = self.u32
        if u32.IsHungAppWindow(hwnd):  # focusing a frozen window can block us too
            return False
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        fg = u32.GetForegroundWindow()
        cur_tid = self.k32.GetCurrentThreadId()
        fg_tid = u32.GetWindowThreadProcessId(fg, None)
        attached = False
        if fg_tid and fg_tid != cur_tid and not u32.IsHungAppWindow(fg):
            attached = bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
        u32.BringWindowToTop(hwnd)
        ok = u32.SetForegroundWindow(hwnd)
        if attached:
            u32.AttachThreadInput(cur_tid, fg_tid, False)
        if not ok:  # classic fallback: a lone ALT tap unlocks SetForegroundWindow
            self._key(0x12, False)
            self._key(0x12, True)
            u32.SetForegroundWindow(hwnd)
        for _ in range(20):
            if u32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.01)
        return u32.GetForegroundWindow() == hwnd

    def _key(self, vk, up):
        # Real scan codes matter: the RDP client forwards scan codes, not VKs.
        scan = self.u32.MapVirtualKeyW(vk, 0)
        flags = self.KEYEVENTF_KEYUP if up else 0
        self.u32.keybd_event(vk, scan, flags, 0)

    def _vk(self, name):
        name = name.lower()
        if name in self.VK:
            return self.VK[name]
        if len(name) == 1:
            return ord(name.upper())
        raise ValueError(f"unknown key {name}")

    def send_combo(self, keys):
        vks = [self._vk(k) for k in keys]
        for vk in vks:
            self._key(vk, False)
            time.sleep(0.02)
        time.sleep(0.03)
        for vk in reversed(vks):
            self._key(vk, True)
            time.sleep(0.02)

    def hotkey(self, app, target="auto", hwnd=None):
        if self.ptt and self.ptt["mode"] == "teams":  # Ctrl is held down: a combo would break it
            return {"ok": False, "error": "Let go of push-to-talk first."}
        w = self.find_target(app, target, hwnd)
        if not w:
            where = {"local": "locally", "rdp": "as an RDP window"}.get(target, "locally or via RDP")
            return {"ok": False, "error": f"No {app.title()} window found {where}."}
        prev = self.u32.GetForegroundWindow()
        if not self._focus(w["hwnd"]):
            return {"ok": False, "error": f"'{w['title']}' is not responding or Windows refused to focus it."}
        time.sleep(0.12 if self.classify(w) == "rdp" else 0.05)
        self.send_combo(self.cfg["hotkeys"][app])
        if self.cfg.get("restore_focus") and prev and prev != w["hwnd"]:
            time.sleep(0.12)
            self._focus(prev)
        via = "RDP" if self.classify(w) == "rdp" else "local"
        return {"ok": True, "sent_to": w["title"], "via": via}

    # ---------------- push-to-talk ----------------
    def ptt_down(self, mode, target="auto", hwnd=None):
        """mode 'mic'   -> unmute all mics at Windows level while held.
           mode 'teams' -> hold Ctrl+Space in Teams (Teams' own temporary unmute),
                           locally or through the RDP window."""
        self.ptt_deadline = time.time() + float(self.cfg.get("ptt_timeout_sec", 2.5))
        if self.ptt:
            return {"ok": True, "mode": self.ptt["mode"], "already": True}
        if mode == "teams":
            w = self.find_target("teams", target, hwnd)
            if not w:
                return {"ok": False, "error": "No Teams or RDP window found for push-to-talk."}
            prev = self.u32.GetForegroundWindow()
            if not self._focus(w["hwnd"]):
                return {"ok": False, "error": f"'{w['title']}' is not responding or Windows refused to focus it."}
            time.sleep(0.12 if self.classify(w) == "rdp" else 0.05)
            vks = [self._vk(k) for k in self.cfg["hotkeys"].get("teams_ptt", ["ctrl", "space"])]
            for vk in vks:  # press and HOLD
                self._key(vk, False)
                time.sleep(0.02)
            self.ptt = {"mode": "teams", "vks": vks, "prev": prev, "hwnd": w["hwnd"]}
            via = "RDP" if self.classify(w) == "rdp" else "local"
            return {"ok": True, "mode": "teams", "sent_to": w["title"], "via": via}
        self.ptt = {"mode": "mic"}
        self.set_mic_all(False)
        return {"ok": True, "mode": "mic"}

    def ptt_beat(self):
        if self.ptt:
            self.ptt_deadline = time.time() + float(self.cfg.get("ptt_timeout_sec", 2.5))
        return {"ok": True, "held": bool(self.ptt)}

    def ptt_up(self):
        p, self.ptt = self.ptt, None
        if not p:
            return {"ok": True}
        if p["mode"] == "teams":
            for vk in reversed(p["vks"]):
                self._key(vk, True)
                time.sleep(0.02)
            if self.cfg.get("restore_focus") and p["prev"] and p["prev"] != p["hwnd"]:
                time.sleep(0.12)
                self._focus(p["prev"])
        else:
            self.set_mic_all(True)
        return {"ok": True}

    # ---------------- Core Audio ----------------
    def _setup_audio(self):
        import comtypes
        from comtypes import CLSCTX_ALL, GUID

        try:
            from pycaw.pycaw import (IAudioEndpointVolume, IAudioSessionControl2,
                                     IAudioSessionManager2, IMMDeviceEnumerator,
                                     ISimpleAudioVolume)
        except ImportError as e:
            sys.exit(f"pycaw is missing ({e}). Run:  pip install -r requirements.txt")
        self.comtypes = comtypes
        self.CLSCTX_ALL = CLSCTX_ALL
        self.I = dict(EndpointVolume=IAudioEndpointVolume, Ctl2=IAudioSessionControl2,
                      Mgr2=IAudioSessionManager2, Enum=IMMDeviceEnumerator,
                      Simple=ISimpleAudioVolume)
        self.CLSID_Enum = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

    def com_init(self):
        self.comtypes.CoInitialize()

    def _enumerator(self):
        return self.comtypes.CoCreateInstance(self.CLSID_Enum, self.I["Enum"],
                                              self.comtypes.CLSCTX_INPROC_SERVER)

    def _activate(self, dev, iface):
        from ctypes import POINTER, cast
        return cast(dev.Activate(iface._iid_, self.CLSCTX_ALL, None), POINTER(iface))

    def _friendly(self, dev):
        did = dev.GetId()
        if did not in self._names:
            self._names[did] = self._friendly_uncached(dev)
        return self._names[did]

    def _friendly_uncached(self, dev):
        try:
            from pycaw.pycaw import AudioUtilities
            d = AudioUtilities.CreateDevice(dev)
            return d.FriendlyName or d.id
        except Exception:
            return dev.GetId()

    def _capture_devices(self):
        en = self._enumerator()
        col = en.EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
        defaults = set()
        for role in (0, 2):  # eConsole, eCommunications
            try:
                defaults.add(en.GetDefaultAudioEndpoint(1, role).GetId())
            except Exception:
                pass
        for i in range(col.GetCount()):
            dev = col.Item(i)
            yield dev, dev.GetId(), dev.GetId() in defaults

    def devices(self):
        out = []
        for dev, did, is_default in self._capture_devices():
            vol = self._activate(dev, self.I["EndpointVolume"])
            out.append({"id": did, "name": self._friendly(dev), "default": is_default,
                        "muted": bool(vol.GetMute()),
                        "level": round(vol.GetMasterVolumeLevelScalar() * 100)})
        return out

    def set_device(self, device_id, muted):
        for dev, did, _ in self._capture_devices():
            if device_id in (did, "*"):
                self._activate(dev, self.I["EndpointVolume"]).SetMute(int(muted), None)

    def _sessions(self):
        for dev, did, _ in self._capture_devices():
            try:
                mgr = self._activate(dev, self.I["Mgr2"])
                se = mgr.GetSessionEnumerator()
            except Exception:
                continue
            for j in range(se.GetCount()):
                try:
                    ctl2 = se.GetSession(j).QueryInterface(self.I["Ctl2"])
                    pid = ctl2.GetProcessId()
                    if not pid:
                        continue
                    yield {"pid": pid, "exe": self.exe_of_pid(pid) or f"pid {pid}",
                           "state": ctl2.GetState(),  # 0 inactive, 1 active, 2 expired
                           "simple": ctl2.QueryInterface(self.I["Simple"]), "device": did}
                except Exception:
                    continue

    def apps(self):
        apps = {}
        for s in self._sessions():
            if s["state"] == 2:
                continue
            a = apps.setdefault(s["exe"], {"exe": s["exe"], "active": False, "muted": True,
                                           "sessions": 0})
            a["sessions"] += 1
            a["active"] = a["active"] or s["state"] == 1
            a["muted"] = a["muted"] and bool(s["simple"].GetMute())
        for exe, want in self.sticky_apps.items():  # show sticky apps even when mic is closed
            apps.setdefault(exe, {"exe": exe, "active": False, "muted": want, "sessions": 0})
        for a in apps.values():
            a["sticky"] = a["exe"] in self.sticky_apps
        return sorted(apps.values(), key=lambda a: (not a["active"], a["exe"]))

    def set_app(self, exe, muted):
        exe = exe.lower()
        if muted:
            self.sticky_apps[exe] = True
        else:
            self.sticky_apps.pop(exe, None)
        for s in self._sessions():
            if s["exe"] == exe:
                s["simple"].SetMute(int(muted), None)

    def set_mic_all(self, muted):
        self.mic_kill = bool(muted)
        self.set_device("*", muted)

    def tick(self):
        """Re-apply mutes: catches new sessions and newly plugged mics.
        Also the push-to-talk watchdog: release if the phone went quiet."""
        if self.ptt and time.time() > self.ptt_deadline:
            print("[ptt] no heartbeat from phone - auto-released")
            self.ptt_up()
        if self.mic_kill:
            for d in self.devices():
                if not d["muted"]:
                    self.set_device(d["id"], True)
        if self.sticky_apps:
            for s in self._sessions():
                if self.sticky_apps.get(s["exe"]) and not s["simple"].GetMute():
                    s["simple"].SetMute(1, None)

    def targets(self):
        out = []
        for w in self.list_windows():
            k = self.classify(w)
            if k:
                out.append({"hwnd": w["hwnd"], "title": w["title"], "exe": w["exe"], "kind": k})
        return out

    def status(self):
        devs = self.devices()
        return {"devices": devs, "apps": self.apps(), "targets": self.targets(),
                "mic_kill": self.mic_kill, "ptt": self.ptt["mode"] if self.ptt else None,
                "all_muted": bool(devs) and all(d["muted"] for d in devs)}


# --------------------------------------------------------------------------------------
# Mock backend (lets you try the UI on Linux/Mac)
# --------------------------------------------------------------------------------------
class MockBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mic_kill = False
        self._devs = [
            {"id": "d1", "name": "Microphone Array (Realtek Audio)", "default": True, "muted": False, "level": 80},
            {"id": "d2", "name": "Headset Microphone (Jabra Evolve2)", "default": False, "muted": False, "level": 65},
        ]
        self._apps = {"ms-teams.exe": False, "mstsc.exe": False, "chrome.exe": False}
        self.sticky_apps = {}
        self.ptt = None
        self.ptt_deadline = 0.0

    def com_init(self):
        pass

    def tick(self):
        if self.ptt and time.time() > self.ptt_deadline:
            print("[ptt] no heartbeat from phone - auto-released")
            self.ptt_up()

    def ptt_down(self, mode, target="auto", hwnd=None):
        self.ptt_deadline = time.time() + float(self.cfg.get("ptt_timeout_sec", 2.5))
        if not self.ptt:
            self.ptt = {"mode": mode}
            if mode == "mic":
                self.set_mic_all(False)
        res = {"ok": True, "mode": mode}
        if mode == "teams":
            title = "client-vdi-01 - Remote Desktop Connection" if target == "rdp" else "Daily standup | Microsoft Teams"
            res.update(sent_to=title, via="RDP" if target == "rdp" else "local")
        return res

    def ptt_beat(self):
        if self.ptt:
            self.ptt_deadline = time.time() + float(self.cfg.get("ptt_timeout_sec", 2.5))
        return {"ok": True, "held": bool(self.ptt)}

    def ptt_up(self):
        p, self.ptt = self.ptt, None
        if p and p["mode"] == "mic":
            self.set_mic_all(True)
        return {"ok": True}

    def set_device(self, did, muted):
        for d in self._devs:
            if did in (d["id"], "*"):
                d["muted"] = bool(muted)

    def set_mic_all(self, muted):
        self.mic_kill = bool(muted)
        self.set_device("*", muted)

    def set_app(self, exe, muted):
        self._apps[exe] = bool(muted)
        if muted:
            self.sticky_apps[exe] = True
        else:
            self.sticky_apps.pop(exe, None)

    def hotkey(self, app, target="auto", hwnd=None):
        if self.ptt and self.ptt["mode"] == "teams":
            return {"ok": False, "error": "Let go of push-to-talk first."}
        title = {"teams": "Daily standup | Microsoft Teams", "meet": "Meet - abc-defg-hij - Google Chrome"}[app]
        if target == "rdp":
            title = "client-vdi-01 - Remote Desktop Connection"
        return {"ok": True, "sent_to": title, "via": "RDP" if target == "rdp" else "local"}

    def status(self):
        return {
            "devices": self._devs,
            "apps": [{"exe": e, "active": e != "chrome.exe", "muted": m, "sessions": 1,
                      "sticky": e in self.sticky_apps} for e, m in self._apps.items()],
            "targets": [
                {"hwnd": 1, "title": "Daily standup | Microsoft Teams", "exe": "ms-teams.exe", "kind": "teams"},
                {"hwnd": 2, "title": "client-vdi-01 - Remote Desktop Connection", "exe": "mstsc.exe", "kind": "rdp"},
                {"hwnd": 3, "title": "Meet - abc-defg-hij - Google Chrome", "exe": "chrome.exe", "kind": "meet"},
            ],
            "mic_kill": self.mic_kill, "ptt": self.ptt["mode"] if self.ptt else None,
            "all_muted": all(d["muted"] for d in self._devs),
        }


# --------------------------------------------------------------------------------------
# COM thread with self-healing
# --------------------------------------------------------------------------------------
class ComThread:
    """All Core Audio / window calls run on one COM thread. If a call hangs (a stuck audio
    driver, a frozen window), that thread is abandoned and a fresh one takes over, so the
    agent never stays stuck."""

    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self._start()

    def _start(self):
        self.ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="com")
        self.ex.submit(self.backend.com_init)

    def run(self, fn, *a, timeout=8):
        ex = self.ex
        try:
            return ex.submit(fn, *a).result(timeout=timeout)
        except FuturesTimeout:
            with self.lock:
                if self.ex is ex:
                    print("[com] a call hung; starting a fresh COM thread")
                    ex.shutdown(wait=False, cancel_futures=True)
                    self._start()
            raise TimeoutError("The laptop was busy. Try again.") from None


def agent_tag(token):
    """Public fingerprint of the token: lets the phone app recognise its laptop without leaking the token."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def discovery(port, token):
    """Answer 'RMDISCOVER' on UDP so the phone app can find this laptop after its IP changes."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", port))
    except OSError as e:
        print(f"[discovery] off: {e}")
        return
    reply = f"RMAGENT {port} {agent_tag(token)}".encode()
    while True:
        try:
            data, addr = s.recvfrom(64)
            if data.startswith(b"RMDISCOVER"):
                s.sendto(reply, addr)
        except OSError:
            time.sleep(1)


def network_is_public():
    """Windows Firewall blocks phones on 'Public' networks by default."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-NetConnectionProfile).NetworkCategory -join ','"],
                             capture_output=True, text=True, timeout=6, creationflags=0x08000000).stdout
        return "Public" in out
    except Exception:
        return False


def allow_firewall():
    """One UAC prompt: allow this program on every network profile (TCP for the page, UDP for discovery)."""
    if sys.platform != "win32":
        return False
    args = (f'advfirewall firewall add rule name="Remote Mic" dir=in action=allow '
            f'program="{sys.executable}" enable=yes profile=any')
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", args, None, 0) > 32


# --------------------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------------------
MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
        ".json": "application/json", ".webmanifest": "application/manifest+json",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


def pair_page(cfg, port):
    urls = phone_urls(cfg, port)
    qr = ""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(urls[0], image_factory=qrcode.image.svg.SvgPathImage, box_size=12, border=2)
        qr = img.to_string(encoding="unicode")
    except Exception:
        qr = "<p>(QR needs the qrcode package; type the link instead)</p>"
    links = "".join(f"<li><code>{u}</code></li>" for u in urls)
    warn = ("<p class='warn'><b>Your Wi-Fi is set to Public</b>, so Windows Firewall blocks your phone.</p>"
            if network_is_public() else "")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Remote Mic: pair your phone</title>
<style>body{{font:15px/1.6 Consolas,monospace;background:#e6e5e1;color:#141414;display:flex;justify-content:center;padding:40px}}
.card{{background:#f4f3f0;border-radius:24px;padding:32px;max-width:560px;box-shadow:0 20px 40px rgba(0,0,0,.15)}}
h1{{margin:0 0 6px;font-size:22px}} .qr{{background:#fff;border-radius:16px;padding:12px;width:300px}} .qr svg{{width:100%;height:auto}}
button{{font:inherit;border:none;border-radius:10px;padding:10px 16px;cursor:pointer;margin-right:8px}}
.org{{background:#ff5b1f;color:#1a0a03}} .gph{{background:#2b2b2b;color:#fff}} small{{color:#77766f}} .warn{{color:#b3261e}}</style></head>
<body><div class="card"><h1>rm-01 · pair your phone</h1><small>Scan with your phone camera (same Wi-Fi), then Add to Home screen.</small>
<div class="qr">{qr}</div><ul>{links}</ul>
<p><label><input type="checkbox" id="as"> Start with Windows (runs quietly at login)</label></p>
{warn}<p><button class="org" id="fw">Allow through firewall</button> <small id="fwm">Phone can't connect? Click once (asks for admin).</small></p>
<button class="gph" onclick="fetch('/local/quit',{{method:'POST'}}).then(()=>document.body.innerHTML='<p>Agent stopped. Close this tab.</p>')">Quit agent</button>
<small>Open this page again any time: run RemoteMic again.</small></div>
<script>const a=document.getElementById('as');fetch('/local/autostart').then(r=>r.json()).then(j=>a.checked=j.enabled);
a.onchange=()=>fetch('/local/autostart',{{method:'POST',body:JSON.stringify({{enabled:a.checked}})}}).then(r=>r.json()).then(j=>a.checked=j.enabled);
document.getElementById('fw').onclick=()=>fetch('/local/firewall',{{method:'POST'}}).then(r=>r.json()).then(j=>document.getElementById('fwm').textContent=j.ok?'Done once you approve the Windows prompt. Reload the page on your phone.':'Could not start the firewall prompt.');</script>
</body></html>"""


def make_handler(backend, cfg, com, ctl):
    """All backend calls run on one COM thread (`com` executor) — Core Audio is COM."""
    log = {"last": None}

    def run(fn, *a):
        return com.run(fn, *a)

    def act(fn, *a):
        """Run an action, then refresh the cached status so the phone sees the result at once."""
        def job():
            res = fn(*a)
            ctl["refresh"]()
            return res
        return com.run(job)

    class H(BaseHTTPRequestHandler):
        server_version = "RemoteMic/1.0"

        def log_message(self, fmt, *args):
            pass  # quiet

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            q = parse_qs(urlparse(self.path).query)
            tok = self.headers.get("X-Token") or (q.get("t") or [None])[0]
            return tok and secrets.compare_digest(tok, cfg["token"])

        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/pair" and self._local():
                return self._send(200, pair_page(cfg, ctl["port"]).encode(), "text/html; charset=utf-8")
            if path == "/local/autostart" and self._local():
                return self._send(200, {"enabled": autostart()})
            if path.startswith("/api/"):
                if not self._authed():
                    return self._send(401, {"ok": False, "error": "bad token"})
                if path == "/api/status":  # served from cache: never waits on Windows
                    if not ctl["status"]:
                        return self._send(503, {"ok": False, "error": "starting"})
                    st = dict(ctl["status"])
                    st["last"] = log["last"]
                    st["stale"] = time.time() - ctl["status_at"] > 5
                    return self._send(200, st)
                return self._send(404, {"ok": False})
            if path == "/":
                path = "/index.html"
            fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
            if not fp.startswith(STATIC) or not os.path.isfile(fp):
                return self._send(404, b"not found", "text/plain")
            with open(fp, "rb") as f:
                self._send(200, f.read(), MIME.get(os.path.splitext(fp)[1], "application/octet-stream"))

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/local/"):
                if not self._local():
                    return self._send(403, {"ok": False})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if path == "/local/autostart":
                    return self._send(200, {"enabled": autostart(bool(body.get("enabled")))})
                if path == "/local/firewall":
                    return self._send(200, {"ok": allow_firewall()})
                if path == "/local/quit":
                    self._send(200, {"ok": True})
                    threading.Thread(target=ctl["httpd"].shutdown, daemon=True).start()
                    return
                return self._send(404, {"ok": False})
            if not self._authed():
                return self._send(401, {"ok": False, "error": "bad token"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad json"})
            path = urlparse(self.path).path
            try:
                if path == "/api/mic":  # {"muted": true|false}  -> all mics
                    act(backend.set_mic_all, bool(body.get("muted")))
                    res = {"ok": True}
                    log["last"] = ("Mic muted" if body.get("muted") else "Mic live")
                elif path == "/api/device":  # {"id":..., "muted":...}
                    act(backend.set_device, body["id"], bool(body.get("muted")))
                    res = {"ok": True}
                elif path == "/api/app":  # {"exe":..., "muted":...}
                    act(backend.set_app, body["exe"], bool(body.get("muted")))
                    res = {"ok": True}
                    log["last"] = f"{body['exe']} {'muted' if body.get('muted') else 'unmuted'}"
                elif path == "/api/hotkey":  # {"app":"teams|meet","target":"auto|local|rdp|hwnd","hwnd":..}
                    app = body.get("app", "teams")
                    if app not in ("teams", "meet"):
                        return self._send(400, {"ok": False, "error": "unknown app"})
                    res = run(backend.hotkey, app, body.get("target", "auto"), body.get("hwnd"))
                    if res.get("ok"):
                        log["last"] = f"{app.title()} toggle -> {res['sent_to']} ({res['via']})"
                elif path == "/api/ptt":  # {"action":"down|beat|up","mode":"mic|teams","target":..,"hwnd":..}
                    action = body.get("action")
                    if action == "down":
                        mode = body.get("mode", "mic")
                        if mode not in ("mic", "teams"):
                            return self._send(400, {"ok": False, "error": "unknown mode"})
                        res = act(backend.ptt_down, mode, body.get("target", "auto"), body.get("hwnd"))
                        if res.get("ok"):
                            log["last"] = f"Push-to-talk ({mode}) - talking"
                    elif action == "beat":
                        res = backend.ptt_beat()  # just a timestamp: never queue behind Windows calls
                    elif action == "up":
                        res = act(backend.ptt_up)
                        log["last"] = "Push-to-talk released"
                    else:
                        return self._send(400, {"ok": False, "error": "bad action"})
                else:
                    return self._send(404, {"ok": False})
            except TimeoutError as e:
                res = {"ok": False, "error": str(e)}
            except Exception as e:
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._send(200, res)

    return H


def phone_urls(cfg, port):
    return [f"http://{ip}:{port}/?t={cfg['token']}" for ip in lan_ips()] or \
           [f"http://<this-pc-ip>:{port}/?t={cfg['token']}"]


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks the outbound interface
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    ap = argparse.ArgumentParser(description="Remote Mic agent")
    ap.add_argument("--mock", action="store_true", help="fake backend (any OS) for testing the UI")
    ap.add_argument("--port", type=int)
    ap.add_argument("--quiet", action="store_true", help="don't open the pairing page")
    ap.add_argument("--install", action="store_true", help="start with Windows (current user)")
    ap.add_argument("--uninstall", action="store_true", help="don't start with Windows")
    args = ap.parse_args()

    if args.install or args.uninstall:
        print("start with Windows:", autostart(bool(args.install)))
        return

    cfg = load_config()
    port = args.port or cfg["port"]
    pair_url = f"http://127.0.0.1:{port}/pair"

    # Already running? Just show its pairing page instead of failing on the port.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            if not args.quiet:
                import webbrowser
                webbrowser.open(pair_url)
            print("Remote Mic is already running:", pair_url)
            return
    except OSError:
        pass

    if args.mock:
        backend = MockBackend(cfg)
    elif sys.platform != "win32":
        sys.exit("This agent controls Windows. Use --mock to try the UI elsewhere.")
    else:
        backend = WindowsBackend(cfg)

    com = ComThread(backend)
    ctl = {"port": port, "httpd": None, "status": None, "status_at": 0.0}

    def refresh():  # runs on the COM thread
        try:
            ctl["status"] = backend.status()
            ctl["status_at"] = time.time()
        except Exception as e:
            print(f"[status] {e}")

    def tick():
        backend.tick()
        refresh()

    ctl["refresh"] = refresh
    try:
        if cfg.get("mute_on_start"):
            com.run(backend.set_mic_all, True)
        com.run(refresh, timeout=20)
    except TimeoutError as e:
        print(f"[start] {e}")

    def ticker():  # one tick at a time; a hung tick gets its thread replaced by ComThread
        while True:
            time.sleep(1.0)
            try:
                com.run(tick, timeout=10)
            except Exception as e:
                print(f"[tick] {e}")

    threading.Thread(target=ticker, daemon=True).start()
    threading.Thread(target=discovery, args=(port, cfg["token"]), daemon=True).start()

    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(backend, cfg, com, ctl))
    ctl["httpd"] = httpd
    urls = phone_urls(cfg, port)
    print("\n  Remote Mic is running" + ("  [MOCK MODE]" if args.mock else ""))
    print("  Open this on your phone (same Wi-Fi):\n")
    for u in urls:
        print("   ", u)
    try:
        import qrcode  # optional
        q = qrcode.QRCode(border=1)
        q.add_data(urls[0])
        q.print_ascii(invert=True)
    except Exception:
        print("\n  (pip install qrcode  to get a scannable QR code here)")
    print(f"\n  Pairing page: {pair_url}\n  Ctrl+C to stop.\n")
    if not args.quiet:
        import webbrowser
        threading.Timer(0.8, webbrowser.open, args=(pair_url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)  # don't wait on an abandoned (hung) COM thread


if __name__ == "__main__":
    main()
