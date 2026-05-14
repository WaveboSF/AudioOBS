"""
wincaptureaudio - Python bindings for the win-capture-audio DLL.

The DLL captures audio from a specific Windows process tree using the
Process Loopback API (Windows 10 2004+). Audio arrives as raw float32
PCM frames via callback - feed them into ffmpeg, soundfile, sounddevice,
a queue, or anywhere else.

Typical usage:

    import wincaptureaudio as wca

    # See what's currently making sound
    for s in wca.enumerate_sessions():
        print(s.pid, s.executable)

    # Capture everything from any chrome.exe process
    with wca.Capture.by_executable("chrome.exe") as cap:
        fmt = cap.format
        print(f"format: {fmt.sample_rate} Hz x {fmt.channels} ch, float32")

        @cap.on_audio
        def feed(pcm_bytes, num_frames, timestamp):
            ...  # write to ffmpeg.stdin, soundfile, queue, etc.

        input("Press Enter to stop...")

The audio callback runs on the DLL's mixer thread. Keep it short -
push frames into a queue if you need to do heavier work elsewhere.
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import (
    c_void_p, c_int, c_uint32, c_uint16, c_uint64,
    c_bool, c_char_p, c_wchar_p, c_float,
    Structure, POINTER, byref,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional


# ===================================================================
# DLL loading
# ===================================================================

_DLL: Optional[ctypes.WinDLL] = None


def _load_dll() -> ctypes.WinDLL:
    global _DLL
    if _DLL is not None:
        return _DLL
    here = Path(__file__).parent
    candidates = [
        here / "wincaptureaudio.dll",
        here.parent / "build" / "wincaptureaudio.dll",
        Path("wincaptureaudio.dll"),
    ]
    for c in candidates:
        if c.exists():
            _DLL = ctypes.WinDLL(str(c))
            break
    else:
        _DLL = ctypes.WinDLL("wincaptureaudio.dll")  # PATH search
    _bind(_DLL)
    return _DLL


# ===================================================================
# Result codes
# ===================================================================

_RESULT_NAMES = {
       0: "OK",
      -1: "INVALID_ARG",
      -2: "NOT_INITIALIZED",
      -3: "ALREADY_INITIALIZED",
      -4: "NOT_FOUND",
      -5: "ACTIVATE_FAILED",
      -6: "BUFFER_TOO_SMALL",
     -99: "INTERNAL",
}


class WCAError(RuntimeError):
    def __init__(self, code: int, what: str = ""):
        self.code = code
        name = _RESULT_NAMES.get(code, "?")
        msg = f"{what}: {name} ({code})" if what else f"{name} ({code})"
        super().__init__(msg)


def _check(code: int, what: str = ""):
    if code != 0:
        raise WCAError(code, what)


# ===================================================================
# Structs (must match capture_api.h exactly)
# ===================================================================

class _AudioFormatC(Structure):
    _fields_ = [
        ("sample_rate",      c_uint32),
        ("channels",         c_uint16),
        ("bits_per_sample",  c_uint16),
    ]


class _SessionInfoC(Structure):
    _fields_ = [
        ("pid",        c_uint32),
        ("executable", ctypes.c_wchar * 260),
    ]


# Callback signatures - WINFUNCTYPE = __stdcall on Windows
_AUDIO_CB = ctypes.WINFUNCTYPE(
    None,
    c_void_p,           # user_data
    POINTER(c_float),   # pcm
    c_uint32,           # num_frames
    c_uint64,           # timestamp_100ns
)

_STATUS_CB = ctypes.WINFUNCTYPE(
    None,
    c_void_p,           # user_data
    c_int,              # result code
    c_char_p,           # message
)


# ===================================================================
# DLL function bindings
# ===================================================================

def _bind(dll: ctypes.WinDLL) -> None:
    dll.wca_init.restype  = c_int
    dll.wca_init.argtypes = []

    dll.wca_shutdown.restype  = None
    dll.wca_shutdown.argtypes = []

    dll.wca_version.restype  = c_char_p
    dll.wca_version.argtypes = []

    dll.wca_set_log_level.restype  = None
    dll.wca_set_log_level.argtypes = [c_int]

    dll.wca_get_log_level.restype  = c_int
    dll.wca_get_log_level.argtypes = []

    dll.wca_enumerate_sessions.restype  = c_int
    dll.wca_enumerate_sessions.argtypes = [POINTER(_SessionInfoC), POINTER(c_uint32)]

    dll.wca_start_capture_pid.restype  = c_int
    dll.wca_start_capture_pid.argtypes = [
        c_uint32, c_bool, _AUDIO_CB, _STATUS_CB, c_void_p, POINTER(c_void_p),
    ]

    dll.wca_start_capture_executable.restype  = c_int
    dll.wca_start_capture_executable.argtypes = [
        c_wchar_p, c_bool, c_bool, _AUDIO_CB, _STATUS_CB, c_void_p, POINTER(c_void_p),
    ]

    dll.wca_get_format.restype  = c_int
    dll.wca_get_format.argtypes = [c_void_p, POINTER(_AudioFormatC)]

    dll.wca_stop_capture.restype  = c_int
    dll.wca_stop_capture.argtypes = [c_void_p]


# ===================================================================
# Module lifecycle
# ===================================================================

_initialized = False

# How long to wait after wca_init for the SessionMonitor worker thread
# to finish its initial device + session enumeration. 300ms is generous
# on any modern system (actual enumeration is ~10-50ms). Override via
# init(warmup=...) for slower systems or zero for never-waiting tools.
_DEFAULT_WARMUP_SECONDS = 0.3


def init(warmup: float = _DEFAULT_WARMUP_SECONDS) -> None:
    """
    Initialize the DLL and block briefly while the SessionMonitor
    worker thread does its initial scan. Without warmup, an immediate
    enumerate_sessions() after init can race the worker and return
    an empty list.
    """
    global _initialized
    if _initialized:
        return
    code = _load_dll().wca_init()
    # ALREADY_INITIALIZED is benign - DLL might have been initialized
    # by another loader in the same process.
    if code not in (0, -3):
        _check(code, "wca_init")
    if warmup > 0:
        time.sleep(warmup)
    _initialized = True


def shutdown() -> None:
    global _initialized
    if not _initialized:
        return
    _load_dll().wca_shutdown()
    _initialized = False


def version() -> str:
    init()
    return _load_dll().wca_version().decode("utf-8")


# ===================================================================
# Log level control
# ===================================================================

# Numeric levels mirror wca_log_level in wca_logging.hpp.
LOG_ERROR = 0
LOG_WARN  = 1
LOG_INFO  = 2
LOG_DEBUG = 3

_LEVEL_NAMES = {
    "error": LOG_ERROR, "err":   LOG_ERROR,
    "warn":  LOG_WARN,  "warning": LOG_WARN,
    "info":  LOG_INFO,
    "debug": LOG_DEBUG, "dbg":   LOG_DEBUG,
}


def set_log_level(level) -> None:
    """
    Adjust the DLL's stderr log threshold.

    Accepts an int (0..3) or a name string (case-insensitive):
        "error" / "warn" / "info" / "debug"

    Default after wca_init is INFO. Useful when diagnosing capture
    issues - flip to DEBUG to see device add/remove and session
    watcher traffic.
    """
    init()
    if isinstance(level, str):
        key = level.strip().lower()
        if key not in _LEVEL_NAMES:
            raise ValueError(f"Unknown log level name: {level!r}")
        numeric = _LEVEL_NAMES[key]
    else:
        numeric = int(level)
    _load_dll().wca_set_log_level(numeric)


def get_log_level() -> int:
    """Returns the current DLL log threshold (0..3)."""
    init()
    return int(_load_dll().wca_get_log_level())


# ===================================================================
# Public dataclasses
# ===================================================================

@dataclass(frozen=True)
class SessionInfo:
    pid: int
    executable: str


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    bits_per_sample: int = 32

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * (self.bits_per_sample // 8)

    @property
    def ffmpeg_input_args(self) -> list[str]:
        """Args for piping raw PCM into ffmpeg via stdin."""
        return [
            "-f", "f32le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-i", "pipe:0",
        ]


# ===================================================================
# Enumeration
# ===================================================================

def enumerate_sessions() -> List[SessionInfo]:
    """
    Snapshot the currently audio-active processes.

    The underlying SessionMonitor reports one entry per (process, audio
    device, session_id) tuple - so a process that has open audio sessions
    on multiple endpoints appears multiple times. We deduplicate by
    (pid, executable) here since callers usually want to think in terms
    of applications, not sessions.
    """
    init()
    dll = _load_dll()
    count = c_uint32(0)
    dll.wca_enumerate_sessions(None, byref(count))
    if count.value == 0:
        return []
    buf = (_SessionInfoC * count.value)()
    _check(dll.wca_enumerate_sessions(buf, byref(count)), "enumerate_sessions")

    seen: set[tuple[int, str]] = set()
    result: List[SessionInfo] = []
    for s in buf[: count.value]:
        key = (s.pid, s.executable)
        if key in seen:
            continue
        seen.add(key)
        result.append(SessionInfo(pid=s.pid, executable=s.executable))
    return result


# ===================================================================
# Capture
# ===================================================================

AudioCallback  = Callable[[bytes, int, int], None]  # (pcm, num_frames, ts)
StatusCallback = Callable[[int, str], None]         # (status_code, message)


class Capture:
    """A live capture session. Construct via by_pid() or by_executable()."""

    def __init__(self):
        self._handle = c_void_p(0)
        # CRITICAL: must keep references to the C-callable thunks for
        # the lifetime of the capture. If the GC drops them, the DLL
        # calls into freed memory and the process dies.
        self._audio_thunk = None
        self._status_thunk = None
        self._audio_cb: Optional[AudioCallback] = None
        self._status_cb: Optional[StatusCallback] = None
        self._format: Optional[AudioFormat] = None

    # ---------- factory constructors ----------

    @classmethod
    def by_pid(cls,
               pid: int,
               include_tree: bool = True,
               on_audio: Optional[AudioCallback] = None,
               on_status: Optional[StatusCallback] = None) -> "Capture":
        init()
        cap = cls()
        cap._set_callbacks(on_audio, on_status)
        _check(_load_dll().wca_start_capture_pid(
            c_uint32(pid), c_bool(include_tree),
            cap._audio_thunk, cap._status_thunk, None,
            byref(cap._handle),
        ), f"start_capture_pid({pid})")
        cap._cache_format()
        return cap

    @classmethod
    def by_executable(cls,
                      name: str,
                      include_tree: bool = True,
                      exclude: bool = False,
                      on_audio: Optional[AudioCallback] = None,
                      on_status: Optional[StatusCallback] = None) -> "Capture":
        init()
        cap = cls()
        cap._set_callbacks(on_audio, on_status)
        _check(_load_dll().wca_start_capture_executable(
            c_wchar_p(name), c_bool(include_tree), c_bool(exclude),
            cap._audio_thunk, cap._status_thunk, None,
            byref(cap._handle),
        ), f"start_capture_executable({name!r})")
        cap._cache_format()
        return cap

    # ---------- callback wiring ----------

    def _set_callbacks(self,
                       on_audio: Optional[AudioCallback],
                       on_status: Optional[StatusCallback]) -> None:
        self._audio_cb  = on_audio
        self._status_cb = on_status

        def audio_trampoline(user_data, pcm_ptr, num_frames, ts):
            cb = self._audio_cb
            fmt = self._format
            if cb is None or fmt is None:
                return
            byte_count = int(num_frames) * fmt.bytes_per_frame
            try:
                # string_at copies; ok for typical block sizes.
                # For ultra-low-latency, use ctypes.cast + np.frombuffer
                # for a zero-copy view (only valid inside this call).
                buf = ctypes.string_at(pcm_ptr, byte_count)
                cb(buf, int(num_frames), int(ts))
            except Exception:
                import traceback; traceback.print_exc()

        def status_trampoline(user_data, code, msg):
            cb = self._status_cb
            if cb is None:
                return
            text = msg.decode("utf-8", "replace") if msg else ""
            try:
                cb(int(code), text)
            except Exception:
                import traceback; traceback.print_exc()

        self._audio_thunk  = _AUDIO_CB(audio_trampoline)
        self._status_thunk = _STATUS_CB(status_trampoline)

    def _cache_format(self) -> None:
        fmt_c = _AudioFormatC()
        _check(_load_dll().wca_get_format(self._handle, byref(fmt_c)), "get_format")
        self._format = AudioFormat(
            sample_rate=fmt_c.sample_rate,
            channels=fmt_c.channels,
            bits_per_sample=fmt_c.bits_per_sample,
        )

    # ---------- public ----------

    @property
    def format(self) -> AudioFormat:
        if self._format is None:
            self._cache_format()
        return self._format  # type: ignore[return-value]

    def on_audio(self, callback: AudioCallback) -> AudioCallback:
        """Decorator-friendly: @cap.on_audio."""
        self._audio_cb = callback
        return callback

    def on_status(self, callback: StatusCallback) -> StatusCallback:
        self._status_cb = callback
        return callback

    def stop(self) -> None:
        if not self._handle:
            return
        try:
            _check(_load_dll().wca_stop_capture(self._handle), "stop_capture")
        finally:
            self._handle = c_void_p(0)
            self._audio_thunk = None
            self._status_thunk = None

    def __enter__(self) -> "Capture":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass


__all__ = [
    "init", "shutdown", "version",
    "enumerate_sessions",
    "AudioFormat", "SessionInfo",
    "Capture", "WCAError",
    "AudioCallback", "StatusCallback",
]
