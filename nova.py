"""
NOVA Gesture OS  v2
===================
Hand-gesture control for Windows: volume, mute, screenshot, open YouTube.
Always-on-top draggable overlay, tray icon, global hotkeys, JSON config.

Default gestures (all changeable in nova_config.json):
  Thumbs UP   (hold)        -> Volume up      (keeps repeating while held)
  Thumbs DOWN (hold)        -> Volume down    (keeps repeating while held)
  Peace sign  (hold 0.5s)   -> Mute / unmute
  Pinch       (hold 0.25s)  -> Screenshot to Desktop (white flash)
  Both thumbs up (hold 0.5s)-> Open YouTube

Global hotkeys (work while any app is focused):
  Ctrl+Alt+Q  quit        Ctrl+Alt+P  pause / resume        Ctrl+Alt+H  hide / show overlay

Overlay: drag anywhere to move it, [II] pauses, [X] quits.

Command line:
  python nova.py                  normal start
  python nova.py --pick-camera    choose the camera again
  python nova.py --camera 1       force camera index
  python nova.py --debug          show finger states and the raw gesture
  python nova.py --no-tray        do not create a tray icon
  python nova.py --autostart on   start with Windows  (off to remove)
"""
from __future__ import annotations

import sys
import os
import math
import time
import json
import copy
import shutil
import argparse
import threading
import subprocess
import datetime
import webbrowser
import ctypes
import ctypes.wintypes as wt
from collections import deque, Counter

IS_WIN = sys.platform == "win32"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "nova_config.json")
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")

# ----------------------------------------------------------------------------
#  DPI awareness (must happen before any window exists)
# ----------------------------------------------------------------------------
if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import cv2
import numpy as np
import mediapipe as mp

# ----------------------------------------------------------------------------
#  Win32 bindings with proper argtypes / restype (safe on 64-bit Python)
# ----------------------------------------------------------------------------
if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    def _sig(fn, res, *args):
        fn.restype = res
        fn.argtypes = list(args)

    c_int = ctypes.c_int
    _sig(user32.FindWindowW, wt.HWND, wt.LPCWSTR, wt.LPCWSTR)
    _sig(user32.SetWindowPos, wt.BOOL, wt.HWND, wt.HWND, c_int, c_int, c_int, c_int, wt.UINT)
    _sig(user32.GetWindowLongW, ctypes.c_long, wt.HWND, c_int)
    _sig(user32.SetWindowLongW, ctypes.c_long, wt.HWND, c_int, ctypes.c_long)
    _sig(user32.ShowWindow, wt.BOOL, wt.HWND, c_int)
    _sig(user32.GetWindowRect, wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    _sig(user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))
    _sig(user32.GetAsyncKeyState, ctypes.c_short, c_int)
    _sig(user32.keybd_event, None, wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t)
    _sig(user32.GetSystemMetrics, c_int, c_int)
    _sig(user32.MessageBoxW, c_int, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT)
    _sig(user32.DestroyWindow, wt.BOOL, wt.HWND)
    _sig(user32.DefWindowProcW, ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _sig(user32.PeekMessageW, wt.BOOL, ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT)
    _sig(user32.TranslateMessage, wt.BOOL, ctypes.POINTER(wt.MSG))
    _sig(user32.DispatchMessageW, ctypes.c_ssize_t, ctypes.POINTER(wt.MSG))
    _sig(user32.SetLayeredWindowAttributes, wt.BOOL, wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD)
    _sig(user32.CreateWindowExW, wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
         c_int, c_int, c_int, c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID)
    _sig(kernel32.GetModuleHandleW, wt.HINSTANCE, wt.LPCWSTR)
    _sig(kernel32.CreateMutexW, wt.HANDLE, ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
    _sig(gdi32.CreateSolidBrush, wt.HBRUSH, wt.DWORD)
    _sig(shell32.SHGetFolderPathW, ctypes.c_long, wt.HWND, c_int, wt.HANDLE, wt.DWORD, wt.LPWSTR)

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", c_int), ("cbWndExtra", c_int),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
        ]

    _sig(user32.RegisterClassW, wt.ATOM, ctypes.POINTER(WNDCLASSW))

HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010


def msgbox(text, title="NOVA", icon=0x10):
    if IS_WIN:
        user32.MessageBoxW(None, text, title, icon)
    else:
        print(f"[{title}] {text}")


def ensure_single_instance():
    if not IS_WIN:
        return
    global _MUTEX
    _MUTEX = kernel32.CreateMutexW(None, False, "Local\\NOVA_GestureOS_v2")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        msgbox("NOVA is already running.\nUse the tray icon or Ctrl+Alt+Q to quit it.",
               "NOVA", 0x40)
        sys.exit(0)


# ----------------------------------------------------------------------------
#  Config
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "camera_index": None,
    "window_pos": [50, 50],
    "volume_step": 5,
    "repeat_ms": 400,
    "hold_ms": {"thumbs_up": 300, "thumbs_down": 300, "peace": 500,
                "pinch": 250, "double_thumbs": 500},
    "cooldown_ms": {"thumbs_up": 150, "thumbs_down": 150, "peace": 1000,
                    "pinch": 1500, "double_thumbs": 4000},
    "actions": {"thumbs_up": "volume_up", "thumbs_down": "volume_down",
                "peace": "mute", "pinch": "screenshot", "double_thumbs": "youtube"},
    "youtube_url": "https://www.youtube.com",
    "browser": "firefox",
    "screenshot_dir": "",
    "flash": True,
    "sound": True,
    "model_complexity": 0,
    "min_detection_confidence": 0.6,
    "min_tracking_confidence": 0.5,
}
VALID_ACTIONS = ("volume_up", "volume_down", "mute", "screenshot", "youtube", "none")
LABELS = ("thumbs_up", "thumbs_down", "peace", "pinch", "double_thumbs")


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    user = {}
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] Could not read nova_config.json ({e}). Using defaults.")
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    for lab in LABELS:
        if cfg["actions"].get(lab) not in VALID_ACTIONS:
            print(f"[WARN] Unknown action for {lab}; set to none.")
            cfg["actions"][lab] = "none"
    return cfg


def save_config(cfg):
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save config: {e}")


# ----------------------------------------------------------------------------
#  Constants / colours (BGR)
# ----------------------------------------------------------------------------
WIN_W, WIN_H = 370, 320
CAM_W, CAM_H = 640, 480
CAM_DISP_H = 200
PANEL_H = WIN_H - CAM_DISP_H

C_ACID = (87, 255, 184)
C_ORANGE = (53, 107, 255)
C_BLUE = (247, 195, 79)
C_WHITE = (235, 235, 235)
C_MUTED = (110, 110, 110)
C_BG = (12, 12, 12)
C_S2 = (20, 20, 20)
C_S3 = (32, 32, 32)
C_RED = (80, 80, 255)
C_PURPLE = (200, 100, 255)
C_YELLOW = (60, 220, 255)
FONT = cv2.FONT_HERSHEY_SIMPLEX

LABEL_NAMES = {"thumbs_up": "THUMBS UP", "thumbs_down": "THUMBS DOWN", "peace": "PEACE",
               "pinch": "PINCH", "double_thumbs": "2x THUMBS UP"}
LABEL_COLORS = {"thumbs_up": C_ACID, "thumbs_down": C_ORANGE, "peace": C_YELLOW,
                "pinch": C_BLUE, "double_thumbs": C_PURPLE}
ACTION_TEXT = {"volume_up": "raise volume", "volume_down": "lower volume", "mute": "toggle mute",
               "screenshot": "take screenshot", "youtube": "open YouTube", "none": "do nothing"}
REPEATABLE = ("volume_up", "volume_down")

HAND_CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
                    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
                    (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]
FINGER_COLORS = [(60, 200, 100), C_ACID, (70, 210, 110), (60, 195, 100), (50, 180, 90)]


# ----------------------------------------------------------------------------
#  Hand geometry + single-hand classifier
# ----------------------------------------------------------------------------
def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle(a, b, c):
    """Angle at b (degrees) between b->a and b->c."""
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n = math.hypot(*v1) * math.hypot(*v2)
    if n < 1e-6:
        return 0.0
    cosv = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / n))
    return math.degrees(math.acos(cosv))


FINGERS = [(5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20)]
ANG_EXT = 145     # PIP angle above this + tip beyond PIP  => extended
ANG_CURL = 125    # PIP angle below this                    => curled


class Hand:
    """One detected hand. `norm` = 21 (x, y, z) normalised landmarks."""

    def __init__(self, norm):
        self.norm = norm
        p = [(x * CAM_W, y * CAM_H) for x, y, _ in norm]
        self.p = p
        self.size = _d(p[0], p[9]) + 1e-6
        self.palm = ((p[0][0] * 2 + p[5][0] + p[9][0] + p[13][0] + p[17][0]) / 6,
                     (p[0][1] * 2 + p[5][1] + p[9][1] + p[13][1] + p[17][1]) / 6)
        self.ext, self.cur = [], []
        for mcp, pip, _dip, tip in FINGERS:
            ang = _angle(p[mcp], p[pip], p[tip])
            self.ext.append(ang >= ANG_EXT and _d(p[tip], p[0]) > _d(p[pip], p[0]))
            self.cur.append(ang <= ANG_CURL)
        t_ang = _angle(p[2], p[3], p[4])
        self.thumb_idx_dist = _d(p[4], p[5]) / self.size
        self.thumb_out = t_ang >= 135 and self.thumb_idx_dist >= 0.5
        self.label = self._classify()

    def _classify(self):
        p, s, e, c = self.p, self.size, self.ext, self.cur
        # Pinch: thumb tip + index tip touching, index not curled into a fist
        if _d(p[4], p[8]) / s < 0.27 and _d(p[8], self.palm) / s > 0.5:
            return "pinch"
        # Peace: index + middle up and apart, ring + pinky curled
        if e[0] and e[1] and c[2] and c[3] and _d(p[8], p[12]) / s > 0.2:
            return "peace"
        # Thumbs up / down: four fingers curled, thumb out and mostly vertical
        if all(c) and self.thumb_out:
            vx, vy = p[4][0] - p[2][0], p[4][1] - p[2][1]
            m = math.hypot(vx, vy) + 1e-6
            if abs(vy) / m >= 0.6:
                return "thumbs_up" if vy < 0 else "thumbs_down"
        return None

    def debug(self):
        f = lambda v: "".join("1" if x else "0" for x in v)
        return f"E{f(self.ext)} C{f(self.cur)} T{int(self.thumb_out)}"

    def palm_norm(self):
        return (self.palm[0] / CAM_W, self.palm[1] / CAM_H)


def frame_label(hands):
    labels = [h.label for h in hands]
    if len(hands) >= 2 and labels[0] == "thumbs_up" and labels[1] == "thumbs_up":
        return "double_thumbs"
    for lab in labels:
        if lab:
            return lab
    return None


# ----------------------------------------------------------------------------
#  Debounce + hold engine
# ----------------------------------------------------------------------------
class Debouncer:
    """The label only changes when it wins `need` of the last `n` frames.
    Survives one or two dropped / noisy frames without resetting a hold."""

    def __init__(self, n=6, need=4):
        self.buf = deque(maxlen=n)
        self.need = need
        self.stable = None

    def push(self, label):
        self.buf.append(label or "none")
        lab, cnt = Counter(self.buf).most_common(1)[0]
        if cnt >= self.need:
            self.stable = None if lab == "none" else lab
        return self.stable

    def reset(self):
        self.buf.clear()
        self.stable = None


class GestureEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cur = None
        self.since = 0.0
        self.fired = False
        self.last_fire = {}
        self.progress = 0.0

    def reset(self):
        self.cur, self.fired, self.progress = None, False, 0.0

    def update(self, label, now, extra_ms=0):
        """Returns the label when its action should run now, else None."""
        if label != self.cur:
            self.cur, self.since, self.fired, self.progress = label, now, False, 0.0
        if not label:
            self.progress = 0.0
            return None
        action = self.cfg["actions"].get(label, "none")
        if action == "none":
            self.progress = 0.0
            return None
        hold = (self.cfg["hold_ms"][label] + extra_ms) / 1000.0
        cool = self.cfg["cooldown_ms"][label] / 1000.0
        last = self.last_fire.get(label, -1e9)
        if not self.fired:
            if now - last < cool:          # still cooling down: hold timer starts after
                self.since, self.progress = now, 0.0
                return None
            elapsed = now - self.since
            self.progress = min(1.0, elapsed / hold)
            if elapsed >= hold:
                self.fired = True
                self.last_fire[label] = now
                return label
            return None
        if action in REPEATABLE:
            gap = now - last
            rep = self.cfg["repeat_ms"] / 1000.0
            self.progress = min(1.0, gap / rep)
            if gap >= rep:
                self.last_fire[label] = now
                return label
        else:
            self.progress = 1.0
        return None


# ----------------------------------------------------------------------------
#  Hand tracker: legacy mp.solutions if present, otherwise the Tasks API
# ----------------------------------------------------------------------------
def ensure_model():
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 1_000_000:
        return
    import urllib.request
    print("[..] Downloading hand model (one time, ~8 MB)...")
    tmp = MODEL_PATH + ".part"
    urllib.request.urlretrieve(MODEL_URL, tmp)
    os.replace(tmp, MODEL_PATH)
    print("[OK] Model saved:", MODEL_PATH)


class HandTracker:
    def __init__(self, cfg):
        self.backend = None
        self._last_ts = 0
        try:
            hands_mod = mp.solutions.hands
            self._h = hands_mod.Hands(
                static_image_mode=False, max_num_hands=2,
                model_complexity=int(cfg["model_complexity"]),
                min_detection_confidence=cfg["min_detection_confidence"],
                min_tracking_confidence=cfg["min_tracking_confidence"])
            self.backend = "legacy"
        except Exception:
            ensure_model()
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
            opts = vision.HandLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,
                min_hand_detection_confidence=cfg["min_detection_confidence"],
                min_hand_presence_confidence=0.5,
                min_tracking_confidence=cfg["min_tracking_confidence"])
            self._h = vision.HandLandmarker.create_from_options(opts)
            self.backend = "tasks"
        print(f"[OK] Hand tracking backend: {self.backend}")

    def process(self, rgb):
        """rgb: contiguous uint8 HxWx3. Returns list of hands, each 21 (x,y,z)."""
        if self.backend == "legacy":
            rgb.flags.writeable = False
            res = self._h.process(rgb)
            rgb.flags.writeable = True
            return [[(l.x, l.y, l.z) for l in hl.landmark]
                    for hl in (res.multi_hand_landmarks or [])]
        ts = max(self._last_ts + 1, int(time.monotonic() * 1000))
        self._last_ts = ts
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        res = self._h.detect_for_video(img, ts)
        return [[(l.x, l.y, l.z) for l in hl] for hl in (res.hand_landmarks or [])]

    def close(self):
        try:
            self._h.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
#  Threaded camera
# ----------------------------------------------------------------------------
class CameraStream:
    def __init__(self, index):
        self.index = index
        self.lock = threading.Lock()
        self.frame, self.seq = None, 0
        self.alive, self.lost = True, False
        self.cap = self._open()
        if self.cap is None:
            raise RuntimeError(f"Could not open camera {index}")
        self.th = threading.Thread(target=self._loop, daemon=True)
        self.th.start()

    def _open(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW) if IS_WIN else cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            cap.release()
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAM_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAM_H)
        cap.set(cv2.CAP_PROP_FPS, 30)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    def _loop(self):
        fails = 0
        while self.alive:
            if self.cap is None:
                self.lost = True
                time.sleep(1.0)
                self.cap = self._open()
                continue
            ok, f = self.cap.read()
            if ok and f is not None:
                fails, self.lost = 0, False
                with self.lock:
                    self.frame, self.seq = f, self.seq + 1
            else:
                fails += 1
                if fails >= 45:
                    self.lost = True
                    self.cap.release()
                    self.cap, fails = None, 0
                else:
                    time.sleep(0.01)

    def get(self):
        with self.lock:
            return self.frame, self.seq

    def stop(self):
        self.alive = False
        time.sleep(0.05)
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass


# ----------------------------------------------------------------------------
#  Camera discovery / picker
# ----------------------------------------------------------------------------
def _camera_names_win():
    if not IS_WIN:
        return {}
    try:
        ps = ("Get-PnpDevice -Class Camera -Status OK | "
              "Select-Object -ExpandProperty FriendlyName | ConvertTo-Json -Compress")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=6)
        raw = r.stdout.strip()
        if raw:
            data = json.loads(raw)
            if isinstance(data, str):
                data = [data]
            return {i: n for i, n in enumerate(data)}
    except Exception:
        pass
    return {}


def enumerate_cameras(max_test=5):
    names = _camera_names_win()
    found, slot = [], 0
    for i in range(max_test):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if IS_WIN else cv2.VideoCapture(i)
        if not cap.isOpened():
            cap.release()
            continue
        ok, _ = cap.read()
        cap.release()
        if not ok:
            slot += 1
            continue
        name = names.get(slot) or ("Built-in Webcam" if slot == 0 else f"External Camera {slot}")
        lo = name.lower()
        virtual = any(k in lo for k in ("iriun", "droidcam", "epoccam", "virtual", "obs", "manycam",
                                        "reincubate", "ivcam", "ndi", "screen capture", "desktop"))
        builtin = (not virtual) and (slot == 0 or any(k in lo for k in (
            "integrated", "built-in", "internal", "laptop", "hd camera", "hd webcam", "ir camera",
            "front camera", "surface", "facetime")))
        found.append((i, name, builtin))
        slot += 1
    return found


def pick_camera_gui(cameras):
    BW, BH, PAD = 360, 50, 10
    H = PAD + 36 + len(cameras) * (BH + PAD) + PAD + 22
    WIN = "NOVA - Choose Camera"
    chosen, hover = [None], [-1]

    def make_img():
        img = np.full((H, BW + PAD * 2, 3), (14, 14, 14), dtype=np.uint8)
        cv2.putText(img, "NOVA  -  SELECT CAMERA", (PAD + 8, 26), FONT, 0.5, C_ACID, 1, cv2.LINE_AA)
        for i, (idx, name, builtin) in enumerate(cameras):
            y0 = PAD + 36 + i * (BH + PAD)
            hl = i == hover[0]
            cv2.rectangle(img, (PAD, y0), (PAD + BW, y0 + BH), (35, 55, 35) if hl else (26, 26, 26), -1)
            cv2.rectangle(img, (PAD, y0), (PAD + BW, y0 + BH), C_ACID if hl else (50, 50, 50), 1)
            cv2.putText(img, f"[{i}]  {name}", (PAD + 12, y0 + 18), FONT, 0.38,
                        C_ACID if hl else C_WHITE, 1, cv2.LINE_AA)
            cv2.putText(img, "  [Built-in]" if builtin else "  [External]", (PAD + 12, y0 + 36), FONT, 0.3,
                        (100, 200, 100) if builtin else (80, 160, 220), 1, cv2.LINE_AA)
        cv2.putText(img, "Click or press key 0..9", (PAD + 4, H - 6), FONT, 0.27, C_MUTED, 1, cv2.LINE_AA)
        return img

    def on_mouse(event, x, y, flags, param):
        rel = y - (PAD + 36)
        i = rel // (BH + PAD) if rel >= 0 else -1
        if 0 <= i < len(cameras) and PAD <= x <= PAD + BW:
            hover[0] = i
            if event == cv2.EVENT_LBUTTONDOWN:
                chosen[0] = cameras[i][0]
        else:
            hover[0] = -1

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.resizeWindow(WIN, BW + PAD * 2, H)
    cv2.setMouseCallback(WIN, on_mouse)
    if IS_WIN:
        try:
            time.sleep(0.12)
            hwnd = user32.FindWindowW(None, WIN)
            if hwnd:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 200, 200, BW + PAD * 2, H, SWP_NOACTIVATE)
        except Exception:
            pass
    while chosen[0] is None:
        cv2.imshow(WIN, make_img())
        key = cv2.waitKey(30) & 0xFF
        if key == 27:
            chosen[0] = cameras[0][0]
        elif ord("0") <= key <= ord("9") and (key - ord("0")) < len(cameras):
            chosen[0] = cameras[key - ord("0")][0]
    cv2.destroyWindow(WIN)
    return chosen[0]


def choose_camera(cfg, args, force_pick=False):
    if args.camera is not None:
        return args.camera
    if cfg.get("camera_index") is not None and not (args.pick_camera or force_pick):
        return cfg["camera_index"]
    print("[..] Scanning cameras...")
    cams = enumerate_cameras()
    if not cams:
        msgbox("No webcam found!\nConnect a camera and restart NOVA.")
        sys.exit(1)
    idx = cams[0][0] if len(cams) == 1 else pick_camera_gui(cams)
    cfg["camera_index"] = idx
    save_config(cfg)
    return idx


# ----------------------------------------------------------------------------
#  Volume (pycaw, main thread only)
# ----------------------------------------------------------------------------
class VolumeController:
    def __init__(self):
        self.ep, self.err, self._last_try = None, "", 0.0
        self._init()

    def _init(self):
        self._last_try = time.time()
        try:
            from pycaw.pycaw import AudioUtilities
            dev = AudioUtilities.GetSpeakers()
            ep = getattr(dev, "EndpointVolume", None)
            if ep is None:  # older pycaw
                from pycaw.pycaw import IAudioEndpointVolume
                from comtypes import CLSCTX_ALL
                iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                ep = ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))
            ep.GetMasterVolumeLevelScalar()
            self.ep = ep
        except Exception as e:
            self.ep, self.err = None, str(e)

    def _ready(self):
        if self.ep is None and time.time() - self._last_try > 5.0:
            self._init()          # re-detect after headphones / device changes
        return self.ep is not None

    @property
    def ok(self):
        return self.ep is not None

    def get(self, default=50):
        if not self._ready():
            return default
        try:
            return round(self.ep.GetMasterVolumeLevelScalar() * 100)
        except Exception:
            self.ep = None
            return default

    def set(self, pct):
        if not self._ready():
            return
        try:
            self.ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, pct / 100.0)), None)
        except Exception:
            self.ep = None

    def muted(self):
        if not self._ready():
            return False
        try:
            return bool(self.ep.GetMute())
        except Exception:
            self.ep = None
            return False

    def set_mute(self, state):
        if not self._ready():
            return
        try:
            self.ep.SetMute(1 if state else 0, None)
        except Exception:
            self.ep = None


def show_volume_osd(at_max):
    """Pop the native Windows volume bar. Sends a +1/-1 media key step pair that
    nets to zero (order flipped at 100% so it never clamps)."""
    if not IS_WIN:
        return

    def _run():
        up, down, ku = 0xAF, 0xAE, 0x0002
        seq = (down, up) if at_max else (up, down)
        for vk in seq:
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, ku, 0)
            time.sleep(0.01)
    threading.Thread(target=_run, daemon=True).start()


# ----------------------------------------------------------------------------
#  Full-screen flash (fading layered window, proper message pumping)
# ----------------------------------------------------------------------------
_flash_lock = threading.Lock()
_flash_ready = False
_FLASH_CB = None
_FLASH_HINST = None


def _ensure_flash_class():
    global _flash_ready, _FLASH_CB, _FLASH_HINST
    with _flash_lock:
        if _flash_ready:
            return True
        _FLASH_CB = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        _FLASH_HINST = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = _FLASH_CB
        wc.hInstance = _FLASH_HINST
        wc.hbrBackground = gdi32.CreateSolidBrush(0x00FFFFFF)
        wc.lpszClassName = "NOVAFlashWnd"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            return False
        _flash_ready = True
        return True


def full_screen_flash(duration_ms=200, peak_alpha=190):
    if not IS_WIN:
        return

    def _run():
        try:
            if not _ensure_flash_class():
                return
            sx, sy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
            sw, sh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
            ex = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
            hwnd = user32.CreateWindowExW(ex, "NOVAFlashWnd", "NOVAFlash", 0x80000000,
                                          sx, sy, sw, sh, None, None, _FLASH_HINST, None)
            if not hwnd:
                return
            user32.SetLayeredWindowAttributes(hwnd, 0, peak_alpha, 2)
            user32.ShowWindow(hwnd, 4)
            msg = wt.MSG()
            t0 = time.time()
            dur = duration_ms / 1000.0
            while True:
                t = (time.time() - t0) / dur
                if t >= 1.0:
                    break
                user32.SetLayeredWindowAttributes(hwnd, 0, int(peak_alpha * (1 - t) ** 1.5), 2)
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.008)
            user32.DestroyWindow(hwnd)
        except Exception as e:
            print(f"[WARN] Flash failed: {e}")
    threading.Thread(target=_run, daemon=True).start()


# ----------------------------------------------------------------------------
#  Screenshot / browser / autostart
# ----------------------------------------------------------------------------
def desktop_path():
    if IS_WIN:
        buf = ctypes.create_unicode_buffer(260)
        shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
        if buf.value:
            return buf.value
    return os.path.expanduser("~/Desktop")


def take_screenshot(folder):
    folder = folder or desktop_path()
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, f"NOVA_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
    try:
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[0])
                mss.tools.to_png(shot.rgb, shot.size, output=out)
            return out
        except ImportError:
            pass
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(out, "PNG")
        return out
    except Exception as e:
        print(f"[ERR] Screenshot failed: {e}  (try: pip install mss)")
        return None


def open_url(url, browser):
    def _run():
        if browser.lower() == "firefox":
            cands = [shutil.which("firefox"),
                     r"C:\Program Files\Mozilla Firefox\firefox.exe",
                     r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"]
            for fp in cands:
                if fp and os.path.exists(fp):
                    subprocess.Popen([fp, url])
                    print("[OK] Opened in Firefox:", url)
                    return
        webbrowser.open(url)
        print("[OK] Opened in default browser:", url)
    threading.Thread(target=_run, daemon=True).start()


def set_autostart(enable):
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    if enable:
        pw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pw if os.path.exists(pw) else sys.executable
        winreg.SetValueEx(key, "NOVAGestureOS", 0, winreg.REG_SZ, f'"{exe}" "{os.path.abspath(__file__)}"')
        print("[OK] NOVA will start with Windows.")
    else:
        try:
            winreg.DeleteValue(key, "NOVAGestureOS")
        except FileNotFoundError:
            pass
        print("[OK] Autostart removed.")
    winreg.CloseKey(key)


# ----------------------------------------------------------------------------
#  Tray icon (optional: pip install pystray pillow)
# ----------------------------------------------------------------------------
class Tray:
    def __init__(self, app):
        self.icon = None
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            print("[i] Tray icon off (pip install pystray pillow to enable). Hotkeys still work.")
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        dr.ellipse((4, 4, 60, 60), fill=(12, 12, 12, 255), outline=(184, 255, 87, 255), width=4)
        dr.polygon([(20, 44), (20, 20), (26, 20), (38, 36), (38, 20), (44, 20), (44, 44), (38, 44),
                    (26, 28), (26, 44)], fill=(184, 255, 87, 255))
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: "Resume" if app.paused else "Pause",
                             lambda icon, item: app.toggle_pause()),
            pystray.MenuItem("Show / hide overlay", lambda icon, item: app.toggle_overlay()),
            pystray.MenuItem("Quit NOVA", lambda icon, item: app.request_quit()))
        self.icon = pystray.Icon("NOVA", img, "NOVA Gesture OS", menu)
        try:
            self.icon.run_detached()
        except Exception:
            threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, title, msg):
        if self.icon:
            try:
                self.icon.notify(msg, title)
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


# ----------------------------------------------------------------------------
#  Draw helpers
# ----------------------------------------------------------------------------
def draw_rrect(img, x, y, w, h, r, color):
    r = max(0, min(r, h // 2, w // 2))
    cv2.rectangle(img, (x + r, y), (x + w - r, y + h), color, -1)
    cv2.rectangle(img, (x, y + r), (x + w, y + h - r), color, -1)
    for cx, cy in ((x + r, y + r), (x + w - r, y + r), (x + r, y + h - r), (x + w - r, y + h - r)):
        cv2.circle(img, (cx, cy), r, color, -1)


def put_center(img, text, cx, cy, scale, color, thick=1):
    sz, _ = cv2.getTextSize(text, FONT, scale, thick)
    cv2.putText(img, text, (cx - sz[0] // 2, cy + sz[1] // 2), FONT, scale, color, thick, cv2.LINE_AA)


def draw_arc(img, cx, cy, r, a0, a1, color, thick=2):
    steps = max(12, int(abs(a1 - a0) / 5))
    pts = [(int(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / steps))),
            int(cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / steps)))) for i in range(steps + 1)]
    for i in range(len(pts) - 1):
        cv2.line(img, pts[i], pts[i + 1], color, thick, cv2.LINE_AA)


# ----------------------------------------------------------------------------
#  Window helpers
# ----------------------------------------------------------------------------
def virtual_screen():
    if not IS_WIN:
        return 0, 0, 1920, 1080
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def setup_overlay_window(name, pos):
    if not IS_WIN:
        return None
    hwnd = user32.FindWindowW(None, name)
    if not hwnd:
        return None
    GWL_STYLE = -16
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    style &= ~(0x00C00000 | 0x00040000 | 0x00800000)   # caption, thickframe, border
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    vx, vy, vw, vh = virtual_screen()
    x = max(vx, min(int(pos[0]), vx + vw - WIN_W))
    y = max(vy, min(int(pos[1]), vy + vh - WIN_H))
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, WIN_W, WIN_H, 0x0020 | SWP_NOACTIVATE)
    return hwnd


def key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000) if IS_WIN else False


# ----------------------------------------------------------------------------
#  App
# ----------------------------------------------------------------------------
class NovaApp:
    WIN_NAME = "NOVA Gesture OS"

    def __init__(self, cfg, cam, tracker, args):
        self.cfg, self.cam, self.tracker, self.args = cfg, cam, tracker, args
        self.vol = VolumeController()
        self.volume = self.vol.get()
        self.muted = self.vol.muted()
        self.engine = GestureEngine(cfg)
        self.debounce = Debouncer()
        self.stable = None
        self.raw_label = None
        self.paused = False
        self.quit_flag = False
        self.visible = True
        self.toast = ("", C_MUTED, 0.0)
        self.flash_frames = 0
        self.fps, self._fps_n, self._fps_t = 0.0, 0, time.time()
        self.hand_present = False
        self.last_hand_ts = time.time()
        self.skip_toggle = False
        self.pending_resync = None
        self.next_vol_sync = 0.0
        self.hk_prev = {}
        self.dragging, self.drag_off = False, (0, 0)
        self.btn_close = (WIN_W - 22, 4, 18, 14)
        self.btn_pause = (WIN_W - 44, 4, 18, 14)
        self.disp = None
        self.last_hands = []

        cv2.namedWindow(self.WIN_NAME, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
        cv2.resizeWindow(self.WIN_NAME, WIN_W, WIN_H)
        cv2.imshow(self.WIN_NAME, np.zeros((WIN_H, WIN_W, 3), dtype=np.uint8))
        cv2.waitKey(1)
        time.sleep(0.15)
        self.hwnd = setup_overlay_window(self.WIN_NAME, cfg["window_pos"])
        cv2.setMouseCallback(self.WIN_NAME, self.on_mouse)

        self.tray = None if args.no_tray else Tray(self)

        print("[OK] NOVA overlay running.")
        print(f"[OK] Volume control: {'active' if self.vol.ok else 'UNAVAILABLE (' + self.vol.err + ')'}")
        for lab in LABELS:
            a = cfg["actions"][lab]
            print(f"     {LABEL_NAMES[lab]:<12} (hold {cfg['hold_ms'][lab]} ms) -> {a}")
        print("[OK] Ctrl+Alt+Q quit | Ctrl+Alt+P pause | Ctrl+Alt+H hide/show")

    # ---- controls ---------------------------------------------------------
    def request_quit(self):
        self.quit_flag = True

    def toggle_pause(self):
        self.paused = not self.paused
        self.engine.reset()
        self.debounce.reset()
        self.stable = None
        self.set_toast("PAUSED" if self.paused else "RESUMED", C_YELLOW)
        if self.tray:
            self.tray.notify("NOVA", "Paused" if self.paused else "Resumed")

    def toggle_overlay(self):
        self.visible = not self.visible
        if IS_WIN and self.hwnd:
            user32.ShowWindow(self.hwnd, 4 if self.visible else 0)

    def set_toast(self, text, color=C_ACID):
        self.toast = (text, color, time.time())

    @staticmethod
    def _hit(box, x, y):
        return box[0] <= x <= box[0] + box[2] and box[1] <= y <= box[1] + box[3]

    def on_mouse(self, event, x, y, flags, param):
        if not IS_WIN or not self.hwnd:
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            if self._hit(self.btn_close, x, y):
                self.request_quit()
                return
            if self._hit(self.btn_pause, x, y):
                self.toggle_pause()
                return
            pt, rc = wt.POINT(), wt.RECT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
            self.drag_off = (pt.x - rc.left, pt.y - rc.top)
            self.dragging = True
        elif event == cv2.EVENT_MOUSEMOVE and self.dragging:
            if not (flags & cv2.EVENT_FLAG_LBUTTON):
                self._end_drag()
                return
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, pt.x - self.drag_off[0], pt.y - self.drag_off[1],
                                0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
        elif event == cv2.EVENT_LBUTTONUP and self.dragging:
            self._end_drag()

    def _end_drag(self):
        self.dragging = False
        rc = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
        self.cfg["window_pos"] = [rc.left, rc.top]

    # ---- actions ----------------------------------------------------------
    def change_volume(self, delta, now):
        if self.vol.muted():
            self.vol.set_mute(False)
            self.muted = False
        cur = self.vol.get(self.volume)
        new = max(0, min(100, cur + delta))
        self.vol.set(new)
        self.volume = new
        show_volume_osd(at_max=new >= 100)
        self.pending_resync = (now + 0.15, new)   # re-assert exact value after OSD key steps
        self.set_toast(f"VOLUME {new}%", C_ACID if delta > 0 else C_ORANGE)

    def toggle_mute(self):
        state = not self.vol.muted()
        self.vol.set_mute(state)
        self.muted = state
        self.set_toast("MUTED" if state else "UNMUTED", C_YELLOW)

    def do_screenshot(self):
        self.flash_frames = 6

        def run():
            path = take_screenshot(self.cfg["screenshot_dir"])
            if self.cfg["flash"]:
                full_screen_flash()
            if self.cfg["sound"] and IS_WIN:
                try:
                    import winsound
                    winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception:
                    pass
            if path:
                name = os.path.basename(path)
                self.set_toast(f"Saved {name}", C_BLUE)
                print("[OK] Screenshot:", path)
                if self.tray:
                    self.tray.notify("Screenshot saved", name)
            else:
                self.set_toast("Screenshot failed (pip install mss)", C_RED)
        threading.Thread(target=run, daemon=True).start()

    def run_action(self, label, now):
        action = self.cfg["actions"][label]
        step = int(self.cfg["volume_step"])
        if action == "volume_up":
            self.change_volume(+step, now)
        elif action == "volume_down":
            self.change_volume(-step, now)
        elif action == "mute":
            self.toggle_mute()
        elif action == "screenshot":
            self.do_screenshot()
        elif action == "youtube":
            open_url(self.cfg["youtube_url"], self.cfg["browser"])
            self.set_toast("Opening YouTube...", C_PURPLE)
        print(f"[>] {LABEL_NAMES[label]} -> {action}")

    # ---- per-iteration housekeeping --------------------------------------
    def poll_system(self, now):
        if IS_WIN:
            ctrl, alt = key_down(0x11), key_down(0x12)
            for name, vk, fn in (("q", 0x51, self.request_quit), ("p", 0x50, self.toggle_pause),
                                 ("h", 0x48, self.toggle_overlay)):
                down = ctrl and alt and key_down(vk)
                if down and not self.hk_prev.get(name):
                    fn()
                self.hk_prev[name] = down
        if self.pending_resync and now >= self.pending_resync[0]:
            self.vol.set(self.pending_resync[1])
            self.pending_resync = None
        if now >= self.next_vol_sync and not self.pending_resync:
            self.volume = self.vol.get(self.volume)
            self.muted = self.vol.muted()
            self.next_vol_sync = now + 0.5

    # ---- drawing ----------------------------------------------------------
    def draw_hand(self, img, hand):
        pts = [(int(x * WIN_W), int(y * CAM_DISP_H)) for x, y, _ in hand.norm]
        for a, b in HAND_CONNECTIONS:
            cv2.line(img, pts[a], pts[b], FINGER_COLORS[max(a, b) // 4 if max(a, b) < 20 else 4], 1, cv2.LINE_AA)
        col = LABEL_COLORS.get(hand.label, (80, 200, 110))
        for i, pt in enumerate(pts):
            if i in (4, 8, 12, 16, 20):
                cv2.circle(img, pt, 5, col, -1, cv2.LINE_AA)
                if hand.label:
                    cv2.circle(img, pt, 8, col, 1, cv2.LINE_AA)
            elif i == 0:
                cv2.circle(img, pt, 4, (180, 180, 180), -1, cv2.LINE_AA)
            else:
                cv2.circle(img, pt, 2, (80, 200, 110), -1, cv2.LINE_AA)

    def build_frame(self, mirrored, hands, now):
        cam = cv2.convertScaleAbs(cv2.resize(mirrored, (WIN_W, CAM_DISP_H)), alpha=0.78)
        if self.flash_frames > 0:
            a = self.flash_frames / 6.0
            cam = cv2.addWeighted(np.full_like(cam, 255), a, cam, 1 - a, 0)
            self.flash_frames -= 1
        for h in hands:
            self.draw_hand(cam, h)

        # progress ring
        if self.engine.progress > 0 and self.stable and hands:
            if self.stable == "double_thumbs" and len(hands) >= 2:
                a, b = hands[0].palm_norm(), hands[1].palm_norm()
                cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            else:
                prim = next((h for h in hands if h.label), hands[0])
                cx, cy = prim.palm_norm()
            px, py = int(cx * WIN_W), int(cy * CAM_DISP_H)
            col = LABEL_COLORS.get(self.stable, C_ACID)
            cv2.circle(cam, (px, py), 24, col, 1, cv2.LINE_AA)
            draw_arc(cam, px, py, 24, -90, -90 + self.engine.progress * 359.9, col, 3)

        lost = self.cam.lost
        if lost:
            put_center(cam, "CAMERA LOST - RECONNECTING", WIN_W // 2, CAM_DISP_H // 2, 0.4, C_RED, 1)
        elif self.paused:
            put_center(cam, "PAUSED", WIN_W // 2, CAM_DISP_H // 2, 0.7, C_YELLOW, 2)
        elif not hands:
            put_center(cam, "SHOW YOUR HAND", WIN_W // 2, CAM_DISP_H // 2, 0.38, C_MUTED, 1)

        if self.args.debug:
            for i, h in enumerate(hands):
                cv2.putText(cam, f"{i}: {h.label or '-'}  {h.debug()}", (6, CAM_DISP_H - 22 + i * 11),
                            FONT, 0.28, C_WHITE, 1, cv2.LINE_AA)
            cv2.putText(cam, f"stable={self.stable} backend={self.tracker.backend}", (6, 12),
                        FONT, 0.28, C_WHITE, 1, cv2.LINE_AA)

        # corner brackets + scanlines
        for bx, by, fx, fy in ((4, 4, 1, 1), (WIN_W - 5, 4, -1, 1), (4, CAM_DISP_H - 5, 1, -1),
                               (WIN_W - 5, CAM_DISP_H - 5, -1, -1)):
            cv2.line(cam, (bx, by), (bx + 12 * fx, by), C_ACID, 1)
            cv2.line(cam, (bx, by), (bx, by + 12 * fy), C_ACID, 1)
        cam[::4] = (cam[::4] * 0.85).astype(np.uint8)

        # window buttons
        for box, txt in ((self.btn_pause, "II"), (self.btn_close, "X")):
            draw_rrect(cam, box[0], box[1], box[2], box[3], 3, (30, 30, 30))
            put_center(cam, txt, box[0] + box[2] // 2, box[1] + box[3] // 2, 0.3, C_WHITE, 1)

        # ---- panel ----
        panel = np.full((PANEL_H, WIN_W, 3), C_S2, dtype=np.uint8)
        cv2.line(panel, (0, 0), (WIN_W, 0), (40, 40, 40), 1)
        y = 16
        cv2.putText(panel, "NOVA", (10, y), FONT, 0.48, C_ACID, 1, cv2.LINE_AA)
        fcol = C_ACID if self.fps >= 20 else (C_ORANGE if self.fps >= 12 else C_RED)
        cv2.putText(panel, f"{int(self.fps)}fps", (WIN_W - 52, y), FONT, 0.33, fcol, 1, cv2.LINE_AA)

        if self.paused:
            mode, mcol = "PAUSED", C_YELLOW
        elif lost:
            mode, mcol = "NO CAM", C_RED
        elif not hands:
            mode, mcol = "IDLE", (70, 70, 70)
        elif self.stable:
            mode, mcol = LABEL_NAMES[self.stable], LABEL_COLORS[self.stable]
        else:
            mode, mcol = "TRACK", (90, 90, 90)
        (mw, _), _ = cv2.getTextSize(mode, FONT, 0.32, 1)
        bw = mw + 16
        draw_rrect(panel, WIN_W // 2 - bw // 2, y - 11, bw, 14, 3, mcol)
        put_center(panel, mode, WIN_W // 2, y - 4, 0.32, C_BG, 1)
        cv2.line(panel, (8, y + 7), (WIN_W - 8, y + 7), (35, 35, 35), 1)

        y = 39
        vtxt = "MUTE" if self.muted else f"{self.volume}%"
        cv2.putText(panel, vtxt, (10, y), FONT, 0.55, C_YELLOW if self.muted else C_ACID, 1, cv2.LINE_AA)
        cv2.putText(panel, "VOL", (10, y + 13), FONT, 0.28, C_MUTED, 1, cv2.LINE_AA)
        bx2, bw2, bh2, by2 = 62, WIN_W - 72, 7, y - 7
        draw_rrect(panel, bx2, by2, bw2, bh2, 3, C_S3)
        fw = int(bw2 * (0 if self.muted else self.volume) / 100)
        if fw > 2:
            draw_rrect(panel, bx2, by2, fw, bh2, 3, C_ORANGE if self.volume > 75 else C_ACID)
        for i, lbl in enumerate(("0", "25", "50", "75", "100")):
            cv2.putText(panel, lbl, (bx2 + int(bw2 * i / 4) - 3, by2 + bh2 + 9), FONT, 0.25, C_MUTED, 1,
                        cv2.LINE_AA)
        cv2.line(panel, (8, 50), (WIN_W - 8, 50), (30, 30, 30), 1)

        cv2.putText(panel, "GESTURE", (10, 63), FONT, 0.27, C_MUTED, 1, cv2.LINE_AA)
        gtxt = LABEL_NAMES[self.stable] if self.stable else "-"
        cv2.putText(panel, gtxt, (78, 63), FONT, 0.36, LABEL_COLORS.get(self.stable, C_MUTED), 1, cv2.LINE_AA)

        bar_y, bar_w = 71, WIN_W - 20
        draw_rrect(panel, 10, bar_y, bar_w, 6, 3, C_S3)
        if self.stable and self.engine.progress > 0:
            f = int(bar_w * self.engine.progress)
            if f > 2:
                draw_rrect(panel, 10, bar_y, f, 6, 3, LABEL_COLORS[self.stable])
            act = ACTION_TEXT.get(self.cfg["actions"].get(self.stable, "none"), "")
            cv2.putText(panel, f"{LABEL_NAMES[self.stable]} - hold to {act}", (10, 89), FONT, 0.27,
                        LABEL_COLORS[self.stable], 1, cv2.LINE_AA)
        else:
            cv2.putText(panel, "Thumb up/down = vol   Peace = mute   Pinch = shot   2x thumb = YT",
                        (10, 89), FONT, 0.24, C_MUTED, 1, cv2.LINE_AA)

        text, tcol, tts = self.toast
        if text and now - tts < 3.5:
            cv2.putText(panel, text[:60], (10, 102), FONT, 0.3, tcol, 1, cv2.LINE_AA)
        cv2.putText(panel, "Drag to move | Ctrl+Alt+Q quit | Ctrl+Alt+P pause", (8, PANEL_H - 5), FONT,
                    0.25, C_MUTED, 1, cv2.LINE_AA)
        return np.vstack([cam, panel])

    # ---- main loop --------------------------------------------------------
    def run(self):
        last_seq, topmost_n = -1, 0
        while True:
            now = time.time()
            self.poll_system(now)
            if self.quit_flag:
                break

            frame, seq = self.cam.get()
            new_frame = frame is not None and seq != last_seq
            if new_frame:
                last_seq = seq
                self._fps_n += 1
                if now - self._fps_t >= 0.6:
                    self.fps, self._fps_n, self._fps_t = self._fps_n / (now - self._fps_t), 0, now

                mirrored = cv2.flip(frame, 1)
                hands = []
                if not self.paused and not self.cam.lost:
                    idle = (now - self.last_hand_ts) > 3.0
                    self.skip_toggle = not self.skip_toggle
                    if not (idle and self.skip_toggle):     # half the work while nobody is there
                        rgb = cv2.cvtColor(mirrored, cv2.COLOR_BGR2RGB)
                        hands = [Hand(h) for h in self.tracker.process(rgb)]
                    self.last_hands = hands
                    if hands:
                        self.last_hand_ts = now
                    self.raw_label = frame_label(hands)
                    self.stable = self.debounce.push(self.raw_label)
                    extra = 250 if (len(hands) >= 2 and self.stable in ("thumbs_up", "thumbs_down")) else 0
                    fired = self.engine.update(self.stable, now, extra)
                    if fired:
                        self.run_action(fired, now)
                else:
                    self.engine.reset()
                    self.debounce.reset()
                    self.stable = None

                if self.visible:
                    self.disp = self.build_frame(mirrored, hands, now)
                    cv2.imshow(self.WIN_NAME, self.disp)

            key = cv2.waitKey(1 if new_frame else 4) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                break

            topmost_n += 1
            if topmost_n >= 90 and self.visible and IS_WIN and self.hwnd:
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                topmost_n = 0

        self.shutdown()

    def shutdown(self):
        if IS_WIN and self.hwnd and not self.dragging:
            try:
                rc = wt.RECT()
                user32.GetWindowRect(self.hwnd, ctypes.byref(rc))
                self.cfg["window_pos"] = [rc.left, rc.top]
            except Exception:
                pass
        save_config(self.cfg)
        if self.tray:
            self.tray.stop()
        self.cam.stop()
        self.tracker.close()
        cv2.destroyAllWindows()
        print("NOVA stopped.")


# ----------------------------------------------------------------------------
#  Entry
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="NOVA Gesture OS v2")
    ap.add_argument("--pick-camera", action="store_true", help="choose the camera again")
    ap.add_argument("--camera", type=int, default=None, help="force a camera index")
    ap.add_argument("--debug", action="store_true", help="show finger states / raw gesture")
    ap.add_argument("--no-tray", action="store_true", help="do not create a tray icon")
    ap.add_argument("--autostart", choices=("on", "off"), help="start with Windows on/off")
    args = ap.parse_args()

    if args.autostart:
        set_autostart(args.autostart == "on")
        return

    ensure_single_instance()
    cfg = load_config()
    if not os.path.exists(CFG_PATH):
        save_config(cfg)

    print("NOVA Gesture OS v2 starting...")
    try:
        tracker = HandTracker(cfg)
    except Exception as e:
        msgbox(f"Could not start hand tracking:\n{e}")
        sys.exit(1)

    cam = None
    idx = choose_camera(cfg, args)
    for attempt in range(2):
        try:
            cam = CameraStream(idx)
            break
        except RuntimeError:
            if attempt == 0 and args.camera is None:
                print(f"[WARN] Camera {idx} failed. Re-scanning...")
                idx = choose_camera(cfg, args, force_pick=True)
            else:
                msgbox(f"Could not open camera {idx}.\nCheck that no other app is using it.")
                sys.exit(1)
    print(f"[OK] Camera {idx} opened (mirrored)")

    NovaApp(cfg, cam, tracker, args).run()


if __name__ == "__main__":
    main()
