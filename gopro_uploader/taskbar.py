"""Upload progress on the Windows taskbar button (ITaskbarList3), via ctypes only.

Green bar while uploading, an animated bar while merging chapters, red on errors,
and a flash when the batch is done. Every call is best-effort: on any failure (not
Windows, no window yet, COM unavailable) it silently does nothing.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger(__name__)

NONE, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8

_CLSID_TaskbarList = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
# ITaskbarList4 extends ITaskbarList3 (progress calls in the same slots). On a
# Windows 11 PC a direct request for ITaskbarList3 returned E_NOINTERFACE; ITaskbarList4 works.
_IID_ITaskbarList4 = "{C43DC798-95D1-4BEA-9030-BB99E2983A1A}"
# vtable slots: IUnknown 0-2, ITaskbarList 3-7 (HrInit = 3), ITaskbarList2 8, ITaskbarList3 9+
_HRINIT, _SET_VALUE, _SET_STATE = 3, 9, 10


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort),
                ("d4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    g = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
    return g


class Taskbar:
    def __init__(self, title: str):
        self.title = title
        self._hwnd = None
        self._local = threading.local()  # COM objects belong to the thread that made them

    # ----------------------------------------------------------------- plumbing
    def _ok(self) -> bool:
        return sys.platform == "win32"

    def _window(self):
        if not self._hwnd:
            h = ctypes.windll.user32.FindWindowW(None, self.title)
            self._hwnd = h or None
        return self._hwnd

    def _iface(self):
        p = getattr(self._local, "p", None)
        if p is None:
            ole32 = ctypes.oledll.ole32
            try:
                ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED; fine if already done
            except OSError:
                pass
            p = ctypes.c_void_p()
            ole32.CoCreateInstance(ctypes.byref(_guid(_CLSID_TaskbarList)), None, 1,  # CLSCTX_INPROC_SERVER
                                   ctypes.byref(_guid(_IID_ITaskbarList4)), ctypes.byref(p))
            self._call(p, _HRINIT)
            self._local.p = p
        return p

    @staticmethod
    def _call(p, slot, *args, argtypes=()):
        vtbl = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[slot])
        return fn(p, *args)

    def _safe(self, fn):
        if not self._ok():
            return
        try:
            hwnd = self._window()
            if hwnd:
                fn(self._iface(), hwnd)
        except Exception as e:  # noqa: BLE001 - cosmetic, never break an upload
            log.debug("taskbar progress unavailable: %s", e)

    # --------------------------------------------------------------------- API
    def progress(self, fraction: float):
        """0..1 of the whole batch, green."""
        v = max(0, min(1000, round(fraction * 1000)))

        def go(p, hwnd):
            self._call(p, _SET_STATE, hwnd, NORMAL, argtypes=(ctypes.c_void_p, ctypes.c_int))
            self._call(p, _SET_VALUE, hwnd, v, 1000,
                       argtypes=(ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong))
        self._safe(go)

    def state(self, flag: int):
        """NONE / INDETERMINATE (merging) / ERROR (red) / PAUSED."""
        self._safe(lambda p, hwnd: self._call(p, _SET_STATE, hwnd, flag,
                                               argtypes=(ctypes.c_void_p, ctypes.c_int)))

    def flash(self):
        """Blink the taskbar button until the window is brought to the front."""
        if not self._ok():
            return
        try:
            hwnd = self._window()
            if not hwnd:
                return

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                            ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]
            fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x2 | 0xC, 0, 0)  # TRAY | TIMERNOFG
            ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
        except Exception as e:  # noqa: BLE001
            log.debug("taskbar flash unavailable: %s", e)
