#!/usr/bin/env python
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 WaveboSF
"""
AudioOBS - Per-process audio capture for Windows
=================================================

GUI wrapping the wincaptureaudio DLL (Windows 10 2004+ Process Loopback API).
Captures audio from a specific running application and encodes via ffmpeg
to any common audio format.

Sequential mode: when the audio level drops below ~ -50 dB for at least N
seconds (configurable next to the button), the current segment file is
closed and a new one is started as soon as audio resumes. Useful for
splitting playlists/album rips into individual track files.

Requires: PySide6, numpy, wincaptureaudio (sibling .py + .dll),
          ffmpeg on PATH.

License
-------
AudioOBS is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free
Software Foundation, either version 2 of the License, or (at your option)
any later version.

AudioOBS is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for details.

You should have received a copy of the GNU General Public License along
with AudioOBS (see the LICENSE file in the project root). If not, see
<https://www.gnu.org/licenses/>.

The C++ DLL adapts code from the win-capture-audio OBS Studio plugin by
bozbez (https://github.com/bozbez/win-capture-audio), which is GPL-2.0 -
that's why the whole project is GPL-2.0-or-later.

Repository: https://github.com/WaveboSF/AudioOBS

Version 1.00 - first public release.
"""

from __future__ import annotations

__version__ = "1.00"

import ctypes
import collections
import gc
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import List, Optional

import numpy as np
from PySide6.QtCore import (
    Qt, QTimer, Signal, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import (
    QFont, QColor, QCloseEvent, QPixmap, QIcon, QImage, QPainter, QPen,
    QMouseEvent,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QComboBox, QFileDialog, QMessageBox,
    QGroupBox, QTextEdit, QLineEdit, QCheckBox, QSizePolicy,
    QStyleFactory, QGraphicsDropShadowEffect, QDialog, QFrame, QDoubleSpinBox,
    QStackedWidget,
)

try:
    import wincaptureaudio as wca
    WCA_ERROR: Optional[str] = None
except Exception as exc:                       # pragma: no cover
    wca = None                                  # type: ignore
    WCA_ERROR = f"{type(exc).__name__}: {exc}"


def _settings_dir() -> Path:
    """
    Directory for AudioOBS.json.

    For normal `python AudioOBS_1_00.py` runs this is the directory of
    the script itself. For frozen builds (Nuitka --onefile / PyInstaller)
    `__file__` would point into a temporary extraction folder that gets
    wiped on shutdown, so we use the EXE's directory instead.
    """
    # Nuitka sets sys.frozen, PyInstaller sets sys.frozen too. Either way
    # sys.executable is the actual EXE on disk, which is what we want.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


SETTINGS_FILE = _settings_dir() / "AudioOBS.json"
# About-dialog logo: the photo-style reference image the user chose
# as the visual mascot for the credits screen. Big, recognisable
# real-world toucan portrait.
LOGO_PATH        = _settings_dir() / "resources" / "icons" / "logo.png"
# Stylised brand mark: the simplified 256x256 toucan-on-audio-bars
# used everywhere a small icon needs to remain readable (taskbar,
# title bar, desktop shortcut, README header on GitHub). Acts as the
# PNG fallback for the .ico in case the .ico file goes missing.
STYLED_LOGO_PATH = _settings_dir() / "resources" / "icons" / "logo_styled.png"
# Windows-native multi-resolution icon - used for the taskbar / title
# bar / desktop shortcut. PNG would work too but .ico carries all the
# pre-rasterized sizes (16, 24, 32, 48, 64, 128, 256) in one file,
# which gives crisper results across DPI scales than letting Qt
# downscale a single high-res PNG.
ICON_PATH        = _settings_dir() / "resources" / "icons" / "AudioOBS.ico"


# =====================================================================
# Windows process priority
# =====================================================================
# Windows process priority class constants (from winbase.h). We use
# Above-Normal for our own process and pass High to the ffmpeg subprocess
# via subprocess.Popen creationflags. This keeps the capture/mixer
# threads from being preempted by background work, which is the most
# common cause of dropouts ("crackles") in process-loopback capture.

ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS         = 0x00000080
NORMAL_PRIORITY_CLASS       = 0x00000020


def set_own_process_priority_above_normal() -> bool:
    """
    Bump our own process priority class to "Above Normal".
    Returns True on success. No-op (returns False) on non-Windows.

    "Above Normal" is the sweet spot: high enough that the audio thread
    won't get preempted by Chrome/Discord/etc, low enough that we never
    starve system services. "High" or "Realtime" would risk locking up
    the desktop if anything in our pipeline ever stalls.
    """
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


# =====================================================================
# ffmpeg discovery
# =====================================================================
# AudioOBS pipes captured PCM into a long-lived ffmpeg subprocess for
# encoding to FLAC / MP3 / AAC / Opus / etc. ffmpeg itself ships
# separately - we don't bundle it because the official builds are
# updated frequently, the licensing situation around redistributing
# them is messy, and a single ffmpeg.exe is ~150 MB on its own.
#
# find_ffmpeg() locates ffmpeg.exe using:
#   1. shutil.which()  - the canonical "is this on PATH" check
#   2. A handful of common Windows install locations as a fallback,
#      so we can tell the user "we found it here, just not on PATH"
#      (planned for a future polish step; today we just use the path).
#
# Returns the resolved absolute path string, or None if nothing was
# found. The result is cached on the main window at startup and used
# directly when spawning the encoder subprocess.

def find_ffmpeg() -> Optional[str]:
    """
    Locate an ffmpeg executable. Returns the resolved path string or
    None if nothing usable was found.
    """
    # Primary check: is ffmpeg reachable through PATH? This is what
    # users expect "ffmpeg installed" to mean.
    p = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if p:
        return p

    # Fallback: poke at a handful of typical Windows install spots so
    # users who unpacked ffmpeg but didn't touch PATH at least get a
    # working app (we'll still warn them that PATH isn't set so other
    # tools won't find it either).
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
                / "Programs" / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\tools\ffmpeg\bin\ffmpeg.exe"),
        ]
        for c in candidates:
            try:
                if c.exists() and c.is_file():
                    return str(c)
            except Exception:
                pass
    return None


# Plain-text instructions for the "ffmpeg missing" dialog. Kept as a
# module constant so the same wording is used at startup and on the
# (rare) FileNotFoundError fallback during _open_ffmpeg, and so any
# future translation pass has a single string to look at.
FFMPEG_MISSING_TITLE = "ffmpeg not found"

FFMPEG_MISSING_HTML = (
    "<b>ffmpeg was not found on your system.</b><br><br>"
    "AudioOBS uses ffmpeg to encode the captured audio into FLAC, "
    "ALAC, MP3, AAC, Opus, EAC3 or WAV. Without it, <b>recording is "
    "disabled</b>, but the rest of the app still works.<br><br>"

    "<b>What still works without ffmpeg</b><br>"
    "&nbsp;&nbsp;\u2022 Picking a running application as the audio source<br>"
    "&nbsp;&nbsp;\u2022 Live VU meter (per-channel peak hold)<br>"
    "&nbsp;&nbsp;\u2022 Scrolling spectrogram (click the meter to toggle)<br>"
    "&nbsp;&nbsp;\u2022 WASAPI capture-format readout "
    "(sample rate / channels / bit depth / data rate)<br><br>"

    "<b>What needs ffmpeg</b><br>"
    "&nbsp;&nbsp;\u2022 Recording to a file (all codecs)<br>"
    "&nbsp;&nbsp;\u2022 Sequential / album mode and manual track-split<br><br>"

    "<b>How to install ffmpeg on Windows</b><br>"
    "1.&nbsp;Download a recent Windows build from one of:<br>"
    "&nbsp;&nbsp;&nbsp;<a href='https://www.gyan.dev/ffmpeg/builds/'>"
    "https://www.gyan.dev/ffmpeg/builds/</a> (recommended)<br>"
    "&nbsp;&nbsp;&nbsp;<a href='https://github.com/BtbN/FFmpeg-Builds/releases'>"
    "https://github.com/BtbN/FFmpeg-Builds/releases</a><br>"
    "2.&nbsp;Extract the archive somewhere stable, e.g.&nbsp;"
    "<code>C:\\ffmpeg\\</code>.<br>"
    "3.&nbsp;Add the <code>bin</code> folder (the one containing "
    "<code>ffmpeg.exe</code>) to the Windows <b>PATH</b> environment "
    "variable:<br>"
    "&nbsp;&nbsp;&nbsp;Start menu \u2192 type <i>environment variables</i> "
    "\u2192 <i>Edit the system environment variables</i> \u2192 "
    "<i>Environment Variables\u2026</i><br>"
    "&nbsp;&nbsp;&nbsp;under <i>User variables</i> select <b>Path</b> "
    "\u2192 <b>Edit\u2026</b> \u2192 <b>New</b> \u2192 paste e.g. "
    "<code>C:\\ffmpeg\\bin</code> \u2192 <b>OK</b> on every dialog.<br>"
    "4.&nbsp;<b>Restart AudioOBS</b> so it can pick up the new PATH.<br><br>"

    "<i>Verify in a new Command Prompt:</i> "
    "<code>ffmpeg -version</code> should print a version banner."
)


# =====================================================================
# Color palette
# =====================================================================
class C:
    BG_DEEP_CENTER = "#1a2050"
    BG_DEEP_EDGE   = "#04060f"
    BG_CARD_TOP    = "#1e2a5c"
    BG_CARD_BOTTOM = "#111a35"
    BG_INSET       = "#0c1226"
    BORDER         = "#1f3a6e"
    BORDER_BRIGHT  = "#2d5cab"

    TEXT_BRIGHT    = "#f4f4f8"
    TEXT_NORMAL    = "#c8c9d6"
    TEXT_DIM       = "#8a8ca5"

    PINK           = "#e94560"
    CYAN           = "#00d9ff"
    PURPLE         = "#9d4edd"
    AMBER          = "#ffc857"
    GREEN          = "#1DB954"
    BLUE           = "#3a86ff"

    LEVEL_LOW      = "#2ecc71"
    LEVEL_MID      = "#f1c40f"
    LEVEL_HIGH     = "#e74c3c"
    LEVEL_EMPTY    = "#3a4870"
    LEVEL_PEAK     = "#ffffff"

    ERROR          = "#ff4d6d"
    WARNING        = "#ffa726"

    # Link color in the About dialog
    LINK           = "#7ddcff"


# =====================================================================
# Encoder presets
# =====================================================================
@dataclass(frozen=True)
class CodecPreset:
    label: str
    codec: str
    extension: str
    bitrates: List[str]
    container_args: List[str] = field(default_factory=list)
    note: str = ""


CODEC_PRESETS: List[CodecPreset] = [
    CodecPreset("FLAC (lossless)",              "flac",       "flac", [],
                note="Free Lossless Audio Codec - no quality loss, ~50-60% of WAV size."),
    CodecPreset("ALAC (lossless, Apple)",       "alac",       "m4a",  [],
                note="Apple Lossless - same idea as FLAC, plays natively in iTunes."),
    CodecPreset("WAV (PCM float32)",            "pcm_f32le",  "wav",  [],
                note="Uncompressed raw PCM - largest files, zero processing."),
    CodecPreset("MP3",                          "libmp3lame", "mp3",  ["128k", "192k", "256k", "320k"],
                note="Most compatible lossy codec. 320k recommended for archive use."),
    CodecPreset("AAC",                          "aac",        "m4a",  ["128k", "192k", "256k", "320k"],
                note="More efficient than MP3 at the same bitrate."),
    CodecPreset("Opus",                         "libopus",    "opus", ["96k", "128k", "192k", "256k"],
                note="Best lossy codec available - transparent at 192k."),
    CodecPreset("EAC3 (Dolby Digital Plus)",    "eac3",       "ec3",  ["256k", "384k", "448k", "640k"],
                note="Surround-capable codec for multichannel app output."),
]


SILENCE_THRESHOLD_RMS = 10 ** (-50.0 / 20.0)


# =====================================================================
# Stylesheet
# =====================================================================
def build_stylesheet() -> str:
    return f"""
        QMainWindow {{
            background: qradialgradient(cx:0.5, cy:0.35, radius:1.1,
                                        fx:0.5, fy:0.35,
                                        stop:0 {C.BG_DEEP_CENTER},
                                        stop:1 {C.BG_DEEP_EDGE});
        }}
        QDialog {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 {C.BG_CARD_TOP},
                                        stop:1 {C.BG_CARD_BOTTOM});
            color: {C.TEXT_NORMAL};
        }}
        QWidget#root {{ background: transparent; }}

        QLabel {{ color: {C.TEXT_NORMAL}; background: transparent; }}

        QGroupBox {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 {C.BG_CARD_TOP},
                                        stop:1 {C.BG_CARD_BOTTOM});
            border: 1px solid {C.BORDER};
            border-radius: 12px;
            margin-top: 12px;
            padding: 14px 12px 10px 12px;
            font-weight: bold;
            color: {C.TEXT_BRIGHT};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 14px;
            padding: 2px 10px;
            background: {C.BG_DEEP_EDGE};
            border: 1px solid {C.BORDER};
            border-radius: 6px;
        }}
        QGroupBox#sourceCard::title  {{ color: {C.CYAN};   }}
        QGroupBox#formatCard::title  {{ color: {C.PURPLE}; }}
        QGroupBox#outputCard::title  {{ color: {C.AMBER};  }}
        QGroupBox#liveCard::title    {{ color: {C.GREEN};  }}
        QGroupBox#logCard::title     {{ color: {C.BLUE};   }}

        QFrame#vuFrame {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #0a1024,
                                        stop:1 #050816);
            border: 1px solid {C.BORDER_BRIGHT};
            border-radius: 8px;
        }}

        QComboBox, QLineEdit {{
            background: {C.BG_INSET};
            color: {C.TEXT_BRIGHT};
            border: 1px solid {C.BORDER};
            border-radius: 6px;
            padding: 5px 10px;
            min-height: 22px;
            selection-background-color: {C.BORDER_BRIGHT};
        }}
        QComboBox:hover, QLineEdit:hover {{
            border: 1px solid {C.BORDER_BRIGHT};
        }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox::down-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 6px solid {C.TEXT_NORMAL};
            margin-right: 8px;
        }}
        QComboBox QAbstractItemView {{
            background: {C.BG_INSET};
            color: {C.TEXT_BRIGHT};
            border: 1px solid {C.BORDER_BRIGHT};
            selection-background-color: {C.BORDER_BRIGHT};
            outline: 0;
        }}

        QDoubleSpinBox {{
            background: {C.BG_INSET};
            color: {C.TEXT_BRIGHT};
            border: 1px solid {C.BORDER};
            border-radius: 6px;
            padding: 4px 4px 4px 8px;
            min-height: 30px;
            font-weight: bold;
            selection-background-color: {C.BORDER_BRIGHT};
        }}
        QDoubleSpinBox:hover {{ border: 1px solid {C.BORDER_BRIGHT}; }}
        QDoubleSpinBox::up-button {{
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 18px;
            border-left: 1px solid {C.BORDER};
            background: #1c2a55;
            border-top-right-radius: 6px;
        }}
        QDoubleSpinBox::up-button:hover {{ background: #243466; }}
        QDoubleSpinBox::up-button:pressed {{ background: #2d5cab; }}
        QDoubleSpinBox::down-button {{
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 18px;
            border-left: 1px solid {C.BORDER};
            border-top: 1px solid {C.BORDER};
            background: #1c2a55;
            border-bottom-right-radius: 6px;
        }}
        QDoubleSpinBox::down-button:hover {{ background: #243466; }}
        QDoubleSpinBox::down-button:pressed {{ background: #2d5cab; }}
        QDoubleSpinBox::up-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-bottom: 5px solid {C.TEXT_NORMAL};
            width: 0; height: 0;
        }}
        QDoubleSpinBox::down-arrow {{
            image: none;
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {C.TEXT_NORMAL};
            width: 0; height: 0;
        }}

        QPushButton {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #1c2a55, stop:1 #11183a);
            color: {C.TEXT_BRIGHT};
            border: 1px solid {C.BORDER};
            border-radius: 6px;
            padding: 6px 14px;
            font-weight: 500;
        }}
        QPushButton:hover {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                        stop:0 #243466, stop:1 #161f47);
            border: 1px solid {C.BORDER_BRIGHT};
        }}
        QPushButton:disabled {{
            color: {C.TEXT_DIM};
            background: {C.BG_INSET};
            border: 1px solid {C.BORDER};
        }}
        QPushButton#iconBtn {{ padding: 4px 8px; font-size: 16px; font-weight: bold; }}

        /* Recording buttons - shared style.
           Visual language:
             - default (idle / available)  -> green   ("ready / waiting")
             - [recording="true"]          -> red     ("currently recording")
             - :disabled                   -> gray    ("not available right now")
           Green tone matches the AudioOBS header (C.GREEN = #1DB954) but
           sits one shade darker so the buttons don't out-shout the header.
           Pulse shadow (driven from Python) is started only on the button
           that is currently the active recording target. */
        QPushButton#recordBtn {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #169c4a, stop:0.5 #128a3e, stop:1 #0a6c2e);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: bold;
            padding: 12px;
        }}
        QPushButton#recordBtn:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #1cb557, stop:0.5 #179449, stop:1 #128a3e);
        }}
        QPushButton#recordBtn[recording="true"] {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #ff5a4d, stop:0.5 #e74c3c, stop:1 #a02a1e);
            color: white;
        }}
        QPushButton#recordBtn[recording="true"]:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #ff7468, stop:0.5 #ff5a4d, stop:1 #e74c3c);
        }}
        QPushButton#recordBtn:disabled {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #555a66, stop:1 #353945);
            color: #9aa0ad;
        }}

        /* Split-Now button - its own visual identity. Always a slightly
           darker red than the active-recording red (it's an "inside a
           recording" action, not a state toggle). No pulse animation:
           only the currently active main button pulses.
           :pressed darkens it further so the user feels the click;
           [flashed="true"] gets briefly applied from Python on click for
           an extra ~180 ms confirmation flash after release. */
        QPushButton#splitBtn {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #b8362a, stop:0.5 #8c2a20, stop:1 #5c1a13);
            color: white;
            border: none;
            border-radius: 12px;
            font-size: 18px;
            font-weight: bold;
            padding: 12px 0;
        }}
        QPushButton#splitBtn:hover {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #c9402f, stop:0.5 #a13225, stop:1 #761f17);
        }}
        QPushButton#splitBtn:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #6e2018, stop:0.5 #4e1410, stop:1 #2a0905);
            padding-top: 14px;
            padding-bottom: 10px;
        }}
        QPushButton#splitBtn[flashed="true"] {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #ff6b54, stop:0.5 #e74c3c, stop:1 #a02a1e);
            color: #fff8f6;
        }}
        QPushButton#splitBtn:disabled {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #169c4a, stop:0.5 #128a3e, stop:1 #0a6c2e);
            color: white;
        }}

        QCheckBox {{ color: {C.TEXT_NORMAL}; spacing: 8px; background: transparent; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {C.BORDER_BRIGHT};
            border-radius: 4px;
            background: {C.BG_INSET};
        }}
        QCheckBox::indicator:checked {{
            background: {C.CYAN};
            border: 1px solid {C.CYAN};
        }}

        QTextEdit {{
            background: {C.BG_INSET};
            color: {C.TEXT_NORMAL};
            border: 1px solid {C.BORDER};
            border-radius: 6px;
            font-family: "Consolas", "Cascadia Mono", monospace;
            font-size: 11px;
        }}

        QLabel#statusReady   {{ color: {C.GREEN}; font-weight: bold; padding: 4px; font-size: 13px; }}
        QLabel#statusRec     {{ color: {C.PINK};  font-weight: bold; padding: 4px; font-size: 13px; }}
        QLabel#statusSeq     {{ color: {C.BLUE};  font-weight: bold; padding: 4px; font-size: 13px; }}
        QLabel#statusWait    {{ color: {C.AMBER}; font-weight: bold; padding: 4px; font-size: 13px; }}
        QLabel#statusError   {{ color: {C.ERROR}; font-weight: bold; padding: 4px; font-size: 13px; }}

        QLabel#bigStat       {{ color: {C.TEXT_BRIGHT}; font-size: 17px; font-weight: bold; background: transparent; }}
        QLabel#statCaption   {{ color: {C.TEXT_DIM}; font-size: 10px; background: transparent; }}
        QLabel#dimLabel      {{ color: {C.TEXT_DIM}; font-size: 11px; background: transparent; }}
        QLabel#formatInfo    {{
            color: {C.AMBER};
            font-size: 11px;
            font-weight: bold;
            background: transparent;
            padding-left: 4px;
            letter-spacing: 0.3px;
        }}

        QLabel#headerTitle   {{
            color: {C.GREEN};
            font-size: 26px;
            font-weight: bold;
            background: transparent;
            padding: 2px 0 0 0;
        }}
        QLabel#headerSub     {{
            color: {C.TEXT_DIM};
            font-size: 11px;
            background: transparent;
            padding: 0 0 2px 0;
        }}
        QLabel#seqPauseCap   {{ color: {C.TEXT_DIM}; font-size: 10px; background: transparent; }}

        QLabel#vuChan        {{
            color: {C.TEXT_DIM};
            font-family: "Consolas", "Cascadia Mono", monospace;
            font-size: 14px;
            font-weight: bold;
            background: transparent;
        }}
        QLabel#vuDb          {{
            color: {C.TEXT_NORMAL};
            font-family: "Consolas", "Cascadia Mono", monospace;
            font-size: 13px;
            font-weight: bold;
            background: transparent;
        }}

        QLabel#aboutTitle    {{ color: {C.GREEN};   font-size: 24px; font-weight: bold; background: transparent; }}
        QLabel#aboutTagline  {{ color: {C.TEXT_DIM}; font-size: 12px; background: transparent; }}
        QLabel#aboutTech     {{ color: {C.TEXT_DIM}; font-size: 10px; background: transparent; }}
    """


# =====================================================================
# About dialog body (HTML, with clickable links)
# =====================================================================
def _link(href: str, text: str, bold: bool = True) -> str:
    """Render a styled <a> tag in the theme's link color."""
    weight = "bold" if bold else "normal"
    return (f'<a href="{href}" '
            f'style="color: {C.LINK}; text-decoration: none; font-weight: {weight};">'
            f'{text}</a>')


def _about_body_html() -> str:
    return f"""
    <p><span style="color: {C.CYAN}; font-weight: bold; letter-spacing: 1px;">CORE COMPONENTS</span></p>

    <p>{_link("https://github.com/bozbez/win-capture-audio", "win-capture-audio")}
      &nbsp;by {_link("https://github.com/bozbez", "bozbez", bold=False)}<br>
      <span style="color: {C.TEXT_DIM};">The C++ core: WASAPI Process Loopback activation,
      session monitoring and mixing. Adapted from the original OBS Studio plugin into
      a standalone DLL. Licensed GPL-2.0.</span></p>

    <p>{_link("https://github.com/microsoft/wil", "Microsoft WIL")}<br>
      <span style="color: {C.TEXT_DIM};">Windows Implementation Library &mdash; modern C++
      helpers for Win32 and COM.</span></p>

    <p>{_link("https://ffmpeg.org/", "FFmpeg")}
      &nbsp;({_link("https://github.com/FFmpeg/FFmpeg", "GitHub mirror", bold=False)})<br>
      <span style="color: {C.TEXT_DIM};">The audio encoding pipeline. Handles FLAC, ALAC,
      MP3, AAC, Opus, EAC3 and raw PCM via subprocess + stdin pipe.</span></p>

    <p>{_link("https://doc.qt.io/qtforpython-6/", "PySide6")} / Qt<br>
      <span style="color: {C.TEXT_DIM};">GUI framework, signal routing, threading.</span></p>

    <p>{_link("https://numpy.org/", "NumPy")}<br>
      <span style="color: {C.TEXT_DIM};">All audio number-crunching on the
      Python side: RMS for the VU meter and silence detection, plus the
      <span style="color: {C.AMBER};">FFT</span> that drives the scrolling
      Spectrogram (<code>numpy.fft.rfft</code>, Hann window, precomputed
      log-frequency bin map and 256-entry magma LUT for the column
      colours - the whole pipeline is fancy-index + BLAS, no Python
      loop on the hot path).</span></p>

    <p>&nbsp;</p>

    <p><span style="color: {C.PURPLE}; font-weight: bold; letter-spacing: 1px;">SPECIAL THANKS</span></p>

    <p><span style="color: {C.TEXT_DIM};">The Braille-glyph VU meter aesthetic is
      inspired by {_link("https://github.com/aristocratos/btop", "BTop")}&apos;s style of
      usage bars (this codebase&apos;s direct inspiration came via gpu_manager.py, which
      uses the same idea) &mdash; same characters (⣿ filled, ⣀ empty), same
      color-zoning approach.</span></p>

    <p><span style="color: {C.TEXT_DIM};">The scrolling Spectrogram view follows the
      conventions of {_link("https://www.audacityteam.org/", "Audacity")}:
      log-frequency bottom-to-top, time flowing right-to-left, magma-style
      colour intensity for dB. Numerics are pure NumPy / FFT - the SIMD-friendly
      caching idea (precompute the per-frame work once, fancy-index it many times)
      by {_link("https://github.com/WaveboSF", "WaveboSF")}.</span></p>

    <p>&nbsp;</p>

    <p><span style="color: {C.AMBER}; font-weight: bold; letter-spacing: 1px;">AUTHORS</span></p>

    <p><span style="color: {C.TEXT_DIM};">
      {_link("https://github.com/WaveboSF", "WaveboSF")}
      &mdash; concept, design, integration<br>
      {_link("https://anthropic.com", "Claude")} (Anthropic)
      &mdash; C++ skeleton, Python bindings, debugging collaboration
      </span></p>

    <p>&nbsp;</p>

    <p><span style="color: {C.GREEN}; font-weight: bold; letter-spacing: 1px;">LICENSE &amp; SOURCE</span></p>

    <p><span style="color: {C.TEXT_DIM};">
      AudioOBS is free software, licensed under
      {_link("https://www.gnu.org/licenses/old-licenses/gpl-2.0.html",
             "GPL-2.0-or-later")}.
      The license is inherited from
      {_link("https://github.com/bozbez/win-capture-audio", "win-capture-audio")},
      whose C++ code forms the capture core.<br>
      Source code &amp; releases:
      {_link("https://github.com/WaveboSF/AudioOBS", "github.com/WaveboSF/AudioOBS")}
      </span></p>
    """


# =====================================================================
# About dialog
# =====================================================================
class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About AudioOBS {__version__}")
        self.setModal(True)
        # Slightly wider than before so content column + logo column fit
        # side by side (analogous to FFmpeg Converter GUI's About dialog).
        self.setMinimumWidth(700)
        self.setMaximumWidth(820)

        # Outer HBox: content left (expanding), logo right (fixed-width, top-aligned).
        main = QHBoxLayout(self)
        main.setSpacing(20)
        main.setContentsMargins(28, 22, 28, 18)

        # --- Content column (left) ---
        content = QVBoxLayout()
        content.setSpacing(8)
        content.setContentsMargins(0, 0, 0, 0)

        title = QLabel(f"\U0001F3B5  AudioOBS {__version__}")
        title.setObjectName("aboutTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(title)

        tagline = QLabel("A quiet observation lounge for your apps' audio.")
        tagline.setObjectName("aboutTagline")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        content.addWidget(tagline)

        tech = QLabel("Built on the Windows 10 Process Loopback API "
                      "(build 19041 / version 2004 or newer)")
        tech.setObjectName("aboutTech")
        tech.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tech.setWordWrap(True)
        content.addWidget(tech)

        content.addSpacing(10)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {C.BORDER}; max-height: 1px; border: none;")
        content.addWidget(sep)

        body = QLabel(_about_body_html())
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setOpenExternalLinks(True)         # clicks open in default browser
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        body.setStyleSheet(f"color: {C.TEXT_NORMAL}; background: transparent;")
        content.addWidget(body, 1)

        content.addSpacing(6)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setMinimumWidth(110)
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        content.addLayout(btn_row)

        # Content column first - takes remaining width via stretch=1.
        main.addLayout(content, 1)

        # --- Logo column (right, top-aligned) ---
        # Loaded from resources/icons/logo.png next to the script/EXE.
        # Same pattern as FFmpeg Converter GUI: 96x96 scaled, top-right,
        # fixed 110px column width so the layout doesn't drift.
        if LOGO_PATH.exists():
            logo_label = QLabel()
            pixmap = QPixmap(str(LOGO_PATH))
            logo_label.setPixmap(pixmap.scaled(
                96, 96,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            ))
            logo_label.setAlignment(
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
            )
            logo_label.setFixedWidth(110)
            logo_label.setStyleSheet("background: transparent;")
            main.addWidget(logo_label)


# =====================================================================
# Braille VU meter
# =====================================================================
class VuMeter(QWidget):
    BRAILLE_FILLED = "\u28ff"
    BRAILLE_EMPTY  = "\u28c0"
    BRAILLE_PEAK   = "\u28b6"

    # Emitted on left-mouse click anywhere in the widget. Used by the
    # main window to toggle between VU and Spectrogram views.
    clicked = Signal()

    # Zone thresholds on the -50..+4 dB scale:
    #   green   : 0      .. 0.741   (-50 dB to -10 dB)   - safe headroom
    #   yellow  : 0.741  .. 0.926   (-10 dB to   0 dB)   - hot but ok
    #   red     : 0.926  .. 1.0     (  0 dB to  +4 dB)   - digital clipping
    ZONE_GREEN_END  = 0.741
    ZONE_YELLOW_END = 0.926

    # dB range constants - editing these is the only thing needed to retune.
    DB_FLOOR        = -50.0
    DB_CEILING      = +4.0
    DB_RANGE        = DB_CEILING - DB_FLOOR        # 54 dB total span

    def __init__(self, parent=None):
        super().__init__(parent)
        self._meter_l = 0.0
        self._meter_r = 0.0
        self._peak_l  = 0.0
        self._peak_r  = 0.0
        self._peak_decay = 0.94
        # Smoothing factor for the bar's slow-fall. Higher = slower decay,
        # less visible integer-cell jitter at the right edge. 0.80 was
        # noticeably twitchy (5%-step at 50Hz makes adjacent cells flip).
        self._meter_fall = 0.92
        self._bar_len = 45
        # Caches to avoid re-setting identical HTML every tick - Qt will
        # otherwise relayout/repaint even when nothing visually changed,
        # and that's a second source of perceived flicker.
        self._last_html_l: str = ""
        self._last_html_r: str = ""

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)

        self.lbl_l = QLabel("L"); self.lbl_l.setObjectName("vuChan")
        self.lbl_r = QLabel("R"); self.lbl_r.setObjectName("vuChan")
        layout.addWidget(self.lbl_l, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.lbl_r, 1, 0, Qt.AlignmentFlag.AlignVCenter)

        bar_font = QFont("Consolas", 14, QFont.Bold)
        bar_font.setStyleHint(QFont.StyleHint.Monospace)

        self.bar_l = QLabel(); self.bar_l.setFont(bar_font); self.bar_l.setTextFormat(Qt.TextFormat.RichText)
        self.bar_r = QLabel(); self.bar_r.setFont(bar_font); self.bar_r.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.bar_l, 0, 1)
        layout.addWidget(self.bar_r, 1, 1)
        layout.setColumnStretch(1, 1)

        self.db_l = QLabel("-\u221e dB"); self.db_l.setObjectName("vuDb")
        self.db_l.setMinimumWidth(64); self.db_l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.db_r = QLabel("-\u221e dB"); self.db_r.setObjectName("vuDb")
        self.db_r.setMinimumWidth(64); self.db_r.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.db_l, 0, 2, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.db_r, 1, 2, Qt.AlignmentFlag.AlignVCenter)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._render(0.0, 0.0)

    @staticmethod
    def _rms_to_meter(rms: float) -> float:
        if rms < 1e-7:
            return 0.0
        db = 20.0 * math.log10(rms)
        # Map db in [DB_FLOOR, DB_CEILING] linearly to [0, 1].
        # Working with a digital source (Spotify, ffmpeg pipe, etc.) so
        # +10 dB is the ceiling - above that everything clips into mush
        # and there's no point reserving meter real estate for it.
        return max(0.0, min(1.0,
                            (db - VuMeter.DB_FLOOR) / VuMeter.DB_RANGE))

    @staticmethod
    def _rms_to_db_text(rms: float) -> str:
        if rms < 1e-7:
            return "-\u221e dB"
        db = 20.0 * math.log10(rms)
        return f"{db:+5.1f} dB"

    def set_levels(self, rms_l: float, rms_r: float) -> None:
        new_l = self._rms_to_meter(rms_l)
        new_r = self._rms_to_meter(rms_r)
        self._meter_l = max(new_l, self._meter_l * self._meter_fall)
        self._meter_r = max(new_r, self._meter_r * self._meter_fall)
        self._peak_l = max(self._peak_l * self._peak_decay, new_l)
        self._peak_r = max(self._peak_r * self._peak_decay, new_r)

        self._render(self._meter_l, self._meter_r)
        self.db_l.setText(self._rms_to_db_text(rms_l))
        self.db_r.setText(self._rms_to_db_text(rms_r))

    def reset(self) -> None:
        self._meter_l = self._meter_r = 0.0
        self._peak_l  = self._peak_r  = 0.0
        self._last_html_l = ""           # invalidate render cache
        self._last_html_r = ""
        self._render(0.0, 0.0)
        self.db_l.setText("-\u221e dB")
        self.db_r.setText("-\u221e dB")

    def resizeEvent(self, event) -> None:
        fm = self.bar_l.fontMetrics()
        char_w = fm.horizontalAdvance(self.BRAILLE_FILLED) or 10
        available = max(self.bar_l.width(), 100)
        new_len = max(20, min(80, available // char_w - 1))
        if new_len != self._bar_len:
            self._bar_len = new_len
            self._last_html_l = ""       # bar length changed - force redraw
            self._last_html_r = ""
            self._render(self._meter_l, self._meter_r)
        super().resizeEvent(event)

    def mousePressEvent(self, event) -> None:
        # Left-click anywhere in the VU meter emits clicked() so the
        # main window can toggle to the Spectrogram view.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def _bar_html(self, level: float, peak: float) -> str:
        n = self._bar_len
        # Stable round-half-up - Python's round() uses banker's rounding
        # which alternates at exact .5 boundaries and contributes to the
        # ±1 cell flicker we're trying to eliminate.
        filled   = max(0, min(n,     int(level * n + 0.5)))
        # peak_idx is the *cell index* of the peak marker (0..n-1).
        # Clamping to n-1 means a peak at full-scale stays visible at the
        # right edge instead of disappearing past the end.
        peak_idx = max(0, min(n - 1, int(peak  * n + 0.5)))
        # Show the peak marker whenever it is at or past the first empty
        # cell (>= filled).  Using strict `>` here used to make the peak
        # vanish the moment `filled` caught up by one cell, which - paired
        # with the off-by-one in gap_before below - made the rightmost
        # cell flicker on/off every tick at high levels.
        show_peak = peak_idx >= filled and filled < n

        green_max  = int(self.ZONE_GREEN_END  * n)
        yellow_max = int(self.ZONE_YELLOW_END * n)

        parts: List[str] = []

        # ---- filled section, split into green / yellow / red zones ----
        gc = max(0, min(filled, green_max))
        if gc:
            parts.append(f'<span style="color:{C.LEVEL_LOW};">'
                         f'{self.BRAILLE_FILLED * gc}</span>')
        yc = max(0, min(filled - green_max, yellow_max - green_max))
        if yc:
            parts.append(f'<span style="color:{C.LEVEL_MID};">'
                         f'{self.BRAILLE_FILLED * yc}</span>')
        rc = max(0, filled - yellow_max)
        if rc:
            parts.append(f'<span style="color:{C.LEVEL_HIGH};">'
                         f'{self.BRAILLE_FILLED * rc}</span>')

        # ---- empty section, optionally with peak marker ----
        # Geometry (peak_idx is the cell index where the peak is drawn):
        #   filled cells : indices 0 .. filled-1            count = filled
        #   gap_before   : indices filled .. peak_idx-1     count = peak_idx - filled
        #   peak char    : index peak_idx                   count = 1
        #   gap_after    : indices peak_idx+1 .. n-1        count = n - peak_idx - 1
        #   total        : filled + (peak_idx-filled) + 1 + (n-peak_idx-1) = n  ✓
        # The previous formula had `peak_idx - filled - 1` which made the
        # bar one character SHORTER whenever the peak was shown - that's
        # what caused the rightmost cell to blink in and out.
        if show_peak:
            gap_before = peak_idx - filled
            gap_after  = n - peak_idx - 1
            if gap_before > 0:
                parts.append(f'<span style="color:{C.LEVEL_EMPTY};">'
                             f'{self.BRAILLE_EMPTY * gap_before}</span>')
            parts.append(f'<span style="color:{C.LEVEL_PEAK};">'
                         f'{self.BRAILLE_PEAK}</span>')
            if gap_after > 0:
                parts.append(f'<span style="color:{C.LEVEL_EMPTY};">'
                             f'{self.BRAILLE_EMPTY * gap_after}</span>')
        elif filled < n:
            empty = n - filled
            parts.append(f'<span style="color:{C.LEVEL_EMPTY};">'
                         f'{self.BRAILLE_EMPTY * empty}</span>')

        return "".join(parts)

    def _render(self, level_l: float, level_r: float) -> None:
        # Only setText when the bar HTML actually changed. Setting the
        # same HTML every tick still causes Qt to schedule a relayout
        # and repaint - and on text-heavy QLabel with rich text that is
        # visible as a faint twitch in the empty/peak region.
        new_l = self._bar_html(level_l, self._peak_l)
        if new_l != self._last_html_l:
            self.bar_l.setText(new_l)
            self._last_html_l = new_l
        new_r = self._bar_html(level_r, self._peak_r)
        if new_r != self._last_html_r:
            self.bar_r.setText(new_r)
            self._last_html_r = new_r


# =====================================================================
# Spectrogram (scrolling waterfall) - shows L and R channels stacked
# =====================================================================
class SpectrogramWidget(QWidget):
    """
    Real-time scrolling spectrogram (a.k.a. waterfall display).

    New audio columns appear at the right edge and scroll left over time,
    same convention as Adobe Audition / Audacity. Two stacked panels,
    L channel on top and R channel on bottom, with a thin divider in
    between. Frequency axis is logarithmic (20 Hz - 20 kHz) so the
    bass-heavy bottom and the airy top are both visible without one
    swallowing the other. dB-to-color mapping uses a 256-entry magma
    LUT (purple -> red -> orange -> yellow), matching what the user
    sees in professional DAWs.

    Performance comes from doing the work in numpy with precomputed
    tables - same idea as the gradient cache in Arcade_Scrollerv1_1.py
    but for FFT bins and color mapping:

      - Hann window: computed once
      - log-frequency -> FFT-bin index map: computed once per sample rate
      - magma color LUT (256 RGBA entries): computed once
      - internal pixel buffer: allocated once, modified in-place via
        a single np slice-assignment shift each tick (no per-frame alloc)
      - QImage points directly at that buffer (zero copy) and gets
        stretched to widget size in paintEvent

    The audio callback feeds chunks via feed_chunk() under a lock; the
    GUI timer calls tick() to compute one new column and scroll.
    """

    FFT_SIZE     = 2048
    DB_MIN       = -100.0
    DB_MAX       =    0.0
    FREQ_MIN     =    20.0
    FREQ_MAX     = 20000.0
    PANEL_ROWS   = 45                 # internal pixel rows per channel
    DIVIDER_ROW  = 1                  # thin separator between L and R
    INTERNAL_COLS = 320               # internal width; stretched to widget
    # FFT-magnitude calibration: divide by sum(hann) / 2 so a full-scale
    # sine reads ~0 dBFS at its peak bin instead of +60 dB nonsense.
    # sum(hann) over N samples = N/2  ->  norm = 2 / (N/2) = 4/N.
    MAG_NORM     = 4.0 / FFT_SIZE

    # Emitted on left-mouse click - mirrors VuMeter.clicked so the main
    # window can wire both to the same toggle handler.
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sample_rate: int = 48000

        # ---- precomputed: window, freq map, color LUT ----
        self._window = np.hanning(self.FFT_SIZE).astype(np.float32)
        self._lut    = self._build_magma_lut()
        self._row_to_bin: Optional[np.ndarray] = None
        self._rebuild_freq_map()

        # ---- internal RGBA8888 pixel buffer (in-place shifted each tick)
        self._H = self.PANEL_ROWS * 2 + self.DIVIDER_ROW
        self._W = self.INTERNAL_COLS
        # Allocate as one contiguous buffer; QImage references it directly.
        self._buf = np.zeros((self._H, self._W, 4), dtype=np.uint8)
        self._buf[..., 3] = 255                                  # opaque alpha
        self._buf[self.PANEL_ROWS, :, :3] = (45, 60, 110)        # divider line
        # QImage as a *view* on the same memory - drawImage stretches it.
        self._qimg = QImage(
            self._buf.data, self._W, self._H, self._W * 4,
            QImage.Format.Format_RGBA8888,
        )

        # ---- producer/consumer queue between audio and GUI thread ----
        # deque is thread-safe for single producer + single consumer in
        # CPython (GIL serializes append/popleft).
        self._chunks: "collections.deque[np.ndarray]" = collections.deque(maxlen=32)

        # Layout sizing - height fits inside the existing live-card frame
        # so the toggle between VU and spectrogram doesn't cause a layout
        # jump. We just claim the same vertical real estate.
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(96)
        self.setMaximumHeight(96)

    # ----- precomputed tables ----------------------------------------
    @staticmethod
    def _build_magma_lut() -> np.ndarray:
        """
        256-entry RGBA8888 LUT approximating matplotlib's 'magma'.
        Polynomial fit per channel - close enough for visualisation
        and avoids the matplotlib dependency.
        """
        n = 256
        t = np.linspace(0.0, 1.0, n)
        # Hand-tuned polynomial approximations of magma.
        r = np.clip(  0.05 + 1.85*t - 0.85*t*t, 0.0, 1.0)
        g = np.clip( -0.10 + 0.60*t + 0.50*t*t, 0.0, 1.0)
        b = np.clip(  0.30 + 1.20*t - 1.60*t*t + 0.45*t*t*t, 0.0, 1.0)
        # Background row 0 stays near-black; ramp up smoothly to bright yellow.
        rgba = np.stack([r, g, b, np.ones(n)], axis=1)
        return (rgba * 255.0 + 0.5).astype(np.uint8)

    def _rebuild_freq_map(self) -> None:
        """
        For each display row, store the FFT-bin index that contributes.
        Top row = FREQ_MAX (high freq), bottom row = FREQ_MIN (low freq).
        """
        nyquist = self._sample_rate * 0.5
        fft_bin_count = self.FFT_SIZE // 2 + 1
        bin_freqs = np.linspace(0.0, nyquist, fft_bin_count)
        # Display freqs from high (top) to low (bottom), log-spaced.
        f_top    = min(self.FREQ_MAX, nyquist)
        f_bot    = self.FREQ_MIN
        log_freqs = np.logspace(np.log10(f_top), np.log10(f_bot), self.PANEL_ROWS)
        # For each display row, find the nearest FFT bin by searchsorted.
        idx = np.searchsorted(bin_freqs, log_freqs)
        idx = np.clip(idx, 1, fft_bin_count - 1)
        self._row_to_bin = idx.astype(np.int32)

    # ----- public API ------------------------------------------------
    def set_sample_rate(self, sr: int) -> None:
        if sr and sr != self._sample_rate:
            self._sample_rate = int(sr)
            self._rebuild_freq_map()

    def feed_chunk(self, samples_lr: np.ndarray) -> None:
        """
        Called from the audio thread with a (N, channels) float32 array.
        Just appends to the deque - all the heavy work happens in tick()
        on the GUI thread.
        """
        if samples_lr.size:
            self._chunks.append(samples_lr)

    def clear(self) -> None:
        """Reset the display to all-dark (e.g. on capture stop)."""
        self._buf[:self.PANEL_ROWS, :, :3] = 0
        self._buf[self.PANEL_ROWS+1:, :, :3] = 0
        self._chunks.clear()
        self.update()

    def tick(self) -> None:
        """
        Pull queued samples, compute one fresh spectrogram column for
        L and R, shift the pixel buffer left by one column, and write
        the new column at the right edge. Called from the GUI timer.
        """
        # Drain everything currently queued. We keep the most recent
        # FFT_SIZE samples so we always FFT the freshest audio window.
        if not self._chunks:
            return
        # Snapshot under deque atomicity (CPython): we pop everything.
        chunks: List[np.ndarray] = []
        try:
            while True:
                chunks.append(self._chunks.popleft())
        except IndexError:
            pass
        if not chunks:
            return

        # Concat into one (N, ch) array.
        try:
            big = np.concatenate(chunks, axis=0)
        except ValueError:
            return
        if big.shape[0] < self.FFT_SIZE:
            # not enough audio yet - put it back so the next tick can use it
            self._chunks.append(big)
            return

        seg = big[-self.FFT_SIZE:]
        if seg.ndim == 1:
            l = r = seg
        elif seg.shape[1] >= 2:
            l = seg[:, 0]
            r = seg[:, 1]
        else:
            l = r = seg[:, 0]

        col_l = self._compute_column(l)     # (PANEL_ROWS, 4) uint8
        col_r = self._compute_column(r)

        # ---- shift entire image one column to the left ----------------
        # This is a single contiguous memcpy under the hood - 320 cols *
        # 113 rows * 4 bytes = ~145 kB per tick. At 25 Hz that's 3.6 MB/s,
        # well below any modern memory bandwidth limit.
        self._buf[:, :-1, :] = self._buf[:, 1:, :]
        # Write new column at the rightmost position.
        last = self._W - 1
        self._buf[:self.PANEL_ROWS,           last, :] = col_l
        self._buf[self.PANEL_ROWS+1:,          last, :] = col_r
        # divider row stays as-is (kept by the slice boundaries)

        self.update()                       # schedule paintEvent

    # ----- inner: FFT -> dB -> log-freq remap -> LUT ----------------
    def _compute_column(self, samples: np.ndarray) -> np.ndarray:
        # Window then FFT (real input -> ~half-size complex output).
        windowed = samples.astype(np.float32, copy=False) * self._window
        spec = np.fft.rfft(windowed)
        # Magnitude scaled so a full-scale sine reads ~0 dBFS at its bin.
        mag = np.abs(spec).astype(np.float32, copy=False) * self.MAG_NORM
        # dB with floor to avoid log(0). 1e-7 ≈ -140 dB which is well
        # below DB_MIN anyway, so it just keeps log10 happy.
        db = 20.0 * np.log10(mag + 1e-7)
        # Pull the per-display-row dB values via the precomputed index map.
        # This is the "gradient_cache" analogue from Arcade Scroller:
        # we touch each FFT bin exactly once via fancy indexing - no
        # per-row Python loop.
        db_rows = db[self._row_to_bin]
        # Normalise [DB_MIN..DB_MAX] -> [0..1], clamp, then [0..255].
        norm = np.clip(
            (db_rows - self.DB_MIN) / (self.DB_MAX - self.DB_MIN),
            0.0, 1.0,
        )
        idx_u8 = (norm * 255.0 + 0.5).astype(np.uint8)
        # Single fancy-indexing pulls all 4 RGBA bytes per row from the LUT.
        return self._lut[idx_u8]            # shape (PANEL_ROWS, 4)

    # ----- painting --------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # Stretch the internal 320-wide buffer to widget width.
        # Using FastTransformation (nearest-neighbor) is intentional -
        # bilinear would smear the time/frequency boundaries.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(self.rect(), self._qimg)
        painter.end()

    def mousePressEvent(self, event) -> None:
        # Left-click toggles back to the VU view via the main window.
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# =====================================================================
# Helpers
# =====================================================================
def make_card_shadow(parent: QWidget) -> QGraphicsDropShadowEffect:
    shadow = QGraphicsDropShadowEffect(parent)
    shadow.setBlurRadius(26)
    shadow.setOffset(0, 4)
    shadow.setColor(QColor(0, 0, 0, 200))
    return shadow


# =====================================================================
# Main window
# =====================================================================
class AudioOBSMainWindow(QMainWindow):

    log_signal   = Signal(str, str)
    status_event = Signal(int, str)

    PULSE_BLUR        = 32
    PULSE_ALPHA_MIN   = 90
    PULSE_ALPHA_MAX   = 220
    PULSE_RGB         = (231, 76, 60)        # pure red (matches LEVEL_HIGH)

    def __init__(self) -> None:
        super().__init__()

        self.capture: Optional["wca.Capture"] = None
        self._monitor_pid: Optional[int] = None
        self.ffmpeg_proc: Optional[subprocess.Popen] = None

        # Cached path to the ffmpeg executable, populated by
        # _check_ffmpeg_at_startup() right after the UI is built.
        # None means "we looked at startup and could not find one";
        # _open_ffmpeg() falls back to the bare "ffmpeg" name in that
        # case so a freshly-installed PATH entry still works without
        # us having to ask the user again.
        self._ffmpeg_path: Optional[str] = None
        # Set to True after the startup check runs so the help dialog
        # can only ever appear once per session. The dialog is a
        # program-launch event, not a per-recording event.
        self._ffmpeg_warning_shown: bool = False

        self.recording_started: Optional[datetime] = None
        self._segment_started: Optional[datetime] = None
        self._current_out_path: Optional[Path] = None

        self.sequential_mode: bool = False
        self._seq_segment_count: int = 0
        self._seq_session_stamp: str = ""
        self._seq_silence_start: Optional[datetime] = None
        self._seq_waiting: bool = False

        self.save_dir: Path = Path(os.path.expanduser("~")) / "Desktop"
        if not self.save_dir.exists():
            self.save_dir = Path(os.path.expanduser("~"))

        self._rms_lock = Lock()
        self._cur_rms_l: float = 0.0
        self._cur_rms_r: float = 0.0
        self._channels: int = 2

        self._loading_settings: bool = False
        # Lazy debouncer for window geometry persistence. We coalesce the
        # storm of resizeEvent/moveEvent calls during drag-resize/drag-move
        # into a single _save_settings() call ~600 ms after the user
        # stops moving the window.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(600)
        self._geometry_save_timer.timeout.connect(self._save_settings)
        # Same idea for the "Wait for:" text field: we don't want to
        # rewrite AudioOBS.json on every keystroke while the user is
        # typing "spotify.exe", so textChanged starts a 500 ms timer
        # and only the last edit reaches the disk.
        self._wait_name_save_timer = QTimer(self)
        self._wait_name_save_timer.setSingleShot(True)
        self._wait_name_save_timer.setInterval(500)
        self._wait_name_save_timer.timeout.connect(self._save_settings)
        # Carries the exe-name of the most recently picked audio source
        # across sessions. _apply_settings populates it from AudioOBS.json,
        # the post-refresh hook in __init__ uses it for auto-select-or-wait.
        self._last_known_source_exe: str = ""
        # Live "is the spectrogram visible right now?" flag. Audio callbacks
        # only push samples into the spectrogram's queue when this is True,
        # so when the user is looking at the VU meter we don't pay the
        # per-chunk .copy() cost. Toggled in _toggle_meter_view().
        self._spectrogram_visible: bool = False

        self._build_ui()
        self._wire_signals()
        self._load_or_create_settings()
        self._wire_settings_persistence()
        self._start_timers()

        if WCA_ERROR:
            self._set_dll_unavailable()
        else:
            wca.init()
            self.log("INFO", f"wincaptureaudio {wca.version()} ready")
            self.log("INFO", f"Settings file: {SETTINGS_FILE}")
            if set_own_process_priority_above_normal():
                self.log("INFO", "Process priority set to Above Normal")
            self._refresh_processes()
            self._apply_remembered_source()
            self._reconcile_monitor()
            self._pulse_stop()           # idle on launch -> no pulse glow

        # Whether or not the capture DLL loaded, look for ffmpeg right
        # now. We do this AFTER the UI exists (so the warning dialog
        # has a parent window and the log card is available) but as
        # part of __init__ so the user sees the result on first paint.
        # Defer the dialog itself by one event-loop tick via QTimer so
        # the main window is fully painted before the modal appears -
        # otherwise the dialog can pop on top of a half-drawn window
        # on slow machines.
        self._check_ffmpeg_at_startup()

    # ===== ffmpeg startup check =====================================
    def _check_ffmpeg_at_startup(self) -> None:
        """
        Locate ffmpeg once at program launch and cache the result on
        the instance. If nothing is found, pop the help dialog so the
        user knows what's going on and how to install it.

        This runs exactly once per program start - the dialog is not
        re-shown later if a recording attempt happens to fail, only
        the log message is. Restarting the app re-runs the check.
        """
        path = find_ffmpeg()
        self._ffmpeg_path = path
        if path:
            self.log("OK", f"ffmpeg located: {path}")
        else:
            self.log("WARN", "ffmpeg not found on PATH \u2013 recording disabled until installed.")
            self.log("INFO", "Monitor mode (VU meter and spectrogram) still works.")
            # Defer the dialog by a single event-loop tick so the
            # main window is fully drawn first.
            QTimer.singleShot(0, self._show_ffmpeg_missing_dialog)

    def _show_ffmpeg_missing_dialog(self) -> None:
        """
        Modal warning explaining what ffmpeg is for, what AudioOBS can
        do without it, and how to install it on Windows. Shown at most
        once per session so we don't badger the user.
        """
        if self._ffmpeg_warning_shown:
            return
        self._ffmpeg_warning_shown = True
        try:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Icon.Warning)
            msg.setWindowTitle(FFMPEG_MISSING_TITLE)
            msg.setTextFormat(Qt.TextFormat.RichText)
            msg.setText(FFMPEG_MISSING_HTML)
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            # Letting the user click the download links opens them in
            # their default browser instead of inside the dialog.
            try:
                lbl = msg.findChild(QLabel, "qt_msgbox_label")
                if lbl is not None:
                    lbl.setOpenExternalLinks(True)
                    lbl.setTextInteractionFlags(
                        Qt.TextInteractionFlag.TextBrowserInteraction
                    )
                    lbl.setMinimumWidth(560)
            except Exception:
                pass
            msg.exec()
        except Exception as e:
            # Worst case: surface the info in the log so the user
            # still sees it.
            self.log("WARN", f"Could not show ffmpeg-missing dialog: {e}")

    def _apply_remembered_source(self) -> None:
        """
        After the first process-list refresh on startup, try to bring back
        the user's last picked audio source so we don't pop a 'No source'
        message-box when the audio app simply hasn't been opened yet.

        Three cases:
          (a) the remembered exe is right there in the live list
              -> select that entry
          (b) the remembered exe is NOT in the list AND nothing else has
              an active audio session
              -> pre-fill 'Wait for: <exe>' and tick the checkbox so the
                 next Start Recording / Sequential will arm against it
          (c) the remembered exe is missing but other processes are
              available
              -> leave the user to pick from the dropdown; pre-fill the
                 wait field anyway so a quick check-box-tick still works
        """
        exe = self._last_known_source_exe
        if not exe or not wca:
            return

        # (a) try direct re-selection
        target = exe.lower()
        for i in range(self.process_combo.count()):
            data = self.process_combo.itemData(i)
            if data and isinstance(data, tuple) and len(data) == 2:
                _, item_exe = data
                if item_exe and item_exe.lower() == target:
                    self.process_combo.setCurrentIndex(i)
                    self.log("INFO", f"Re-selected last source: {item_exe}")
                    return

        # (b) / (c) pre-fill wait field
        self.wait_name.setText(exe)
        # Auto-enable wait checkbox only if there is literally nothing
        # else to pick - otherwise the user might intentionally want a
        # different app and we shouldn't override their starting state.
        no_real_sources = True
        for i in range(self.process_combo.count()):
            data = self.process_combo.itemData(i)
            if data and isinstance(data, tuple) and data[0]:
                no_real_sources = False
                break
        if no_real_sources:
            self.wait_cb.setChecked(True)
            self.log("INFO",
                     f"No audio sessions present - armed 'Wait for: {exe}' "
                     "from last session.")

    # ----- UI construction -----
    def _build_ui(self) -> None:
        self.setWindowTitle(f"AudioOBS {__version__} \u2013 Per-process audio capture")
        # Window icon: prefer the multi-resolution .ico (crisp at any
        # DPI), fall back to the stylised PNG if .ico is missing. The
        # photo logo deliberately is NOT used here - it's reserved for
        # the About dialog so the brand mark stays consistent across
        # taskbar / title bar / desktop shortcut. Set on both
        # QMainWindow and QApplication so the taskbar / Alt-Tab
        # switcher / desktop shortcut all pick it up.
        if ICON_PATH.exists():
            icon = QIcon(str(ICON_PATH))
        elif STYLED_LOGO_PATH.exists():
            icon = QIcon(str(STYLED_LOGO_PATH))
        else:
            icon = QIcon()
        if not icon.isNull():
            self.setWindowIcon(icon)
            QApplication.instance().setWindowIcon(icon)
        # Width tuned for: [record (4) | split (1) | seq (4)] + pause block.
        # Split being only 1/9 of the button strip lets us go back to a
        # narrower layout than the previous 3-equal-buttons version.
        self.setMinimumSize(800, 860)
        self.resize(820, 880)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        rl = QVBoxLayout(root)
        rl.setSpacing(9)
        rl.setContentsMargins(14, 10, 14, 10)

        # ----- Header
        header_row = QHBoxLayout()
        header_row.setSpacing(0)

        phantom = QWidget()
        phantom.setFixedWidth(32)
        phantom.setStyleSheet("background: transparent;")
        header_row.addWidget(phantom, 0)

        title_block = QVBoxLayout()
        title_block.setSpacing(0)
        title_block.setContentsMargins(0, 0, 0, 0)
        title = QLabel("\U0001F3B5  AudioOBS")
        title.setObjectName("headerTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("Per-process audio capture for Windows")
        subtitle.setObjectName("headerSub")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        header_row.addLayout(title_block, 1)

        self.about_btn = QPushButton("\u2139")
        self.about_btn.setObjectName("iconBtn")
        self.about_btn.setFixedSize(32, 32)
        self.about_btn.setToolTip("About AudioOBS \u2013 version & acknowledgments")
        self.about_btn.clicked.connect(self._show_about)
        header_row.addWidget(self.about_btn, 0)

        rl.addLayout(header_row)

        rl.addWidget(self._build_source_card())
        rl.addWidget(self._build_output_card())
        rl.addWidget(self._build_format_card())

        # ===== Record button row =====================================
        # Layout: [Start Recording (4)] [gap] [Split (1)] [gap]
        #         [Sequential (4)] [Pause spinbox + caption]
        # Start Recording and Sequential share the #recordBtn style with
        # stretch=4; Split-Now is a narrow (stretch=1, ~1/4 of the main
        # buttons) red icon-only button with its own #splitBtn style.
        # The Sequential + PAUSE block stays as a unit on the right
        # (proven layout - we don't want to disturb that).
        record_row = QHBoxLayout()
        record_row.setSpacing(10)

        self.record_btn = QPushButton("\u23FA  Start Recording")
        self.record_btn.setObjectName("recordBtn")
        self.record_btn.setMinimumHeight(48)
        self.record_btn.setProperty("recording", "false")
        self.record_btn.setToolTip(
            "Start a single continuous recording into one file.\n"
            "Press again to stop and save."
        )
        self.record_btn.clicked.connect(self._toggle_recording)

        # Pulse shadow only on this button - it gets the red glow when
        # a normal (non-sequential) recording is running.
        self.rec_shadow = QGraphicsDropShadowEffect(self.record_btn)
        self.rec_shadow.setBlurRadius(self.PULSE_BLUR)
        self.rec_shadow.setOffset(0, 5)
        self.rec_shadow.setColor(QColor(*self.PULSE_RGB, 0))   # start invisible
        self.record_btn.setGraphicsEffect(self.rec_shadow)

        record_row.addWidget(self.record_btn, 4)

        gap1 = QWidget()
        gap1.setFixedWidth(4)
        gap1.setStyleSheet("background: transparent;")
        record_row.addWidget(gap1)

        # Split Now - manual file-split during any recording.
        # Icon-only (scissors), ~1/4 the width of the main buttons.
        # Its own #splitBtn style: darker red than recording-red so it
        # reads as "manual cut inside a recording" not "stop button".
        # No graphics shadow - only the currently active main button pulses.
        # A brief [flashed="true"] is set on click (see _split_now) for
        # post-release click confirmation.
        self.split_btn = QPushButton("\u2702")
        self.split_btn.setObjectName("splitBtn")
        self.split_btn.setMinimumHeight(48)
        self.split_btn.setProperty("flashed", "false")
        self.split_btn.setEnabled(False)              # nothing to split when idle
        self.split_btn.setToolTip(
            "Split Now: close the current file and immediately start a new\n"
            "one. Works during normal recording (manual track boundary) and\n"
            "during Sequential (override the auto-detected silence gap when\n"
            "two songs blend together)."
        )
        self.split_btn.clicked.connect(self._split_now)
        record_row.addWidget(self.split_btn, 1)

        gap2 = QWidget()
        gap2.setFixedWidth(4)
        gap2.setStyleSheet("background: transparent;")
        record_row.addWidget(gap2)

        # Sequential button - same #recordBtn style as Start Recording
        self.seq_btn = QPushButton("\u21BB  Sequential")
        self.seq_btn.setObjectName("recordBtn")     # share style
        self.seq_btn.setMinimumHeight(48)
        self.seq_btn.setProperty("recording", "false")
        self.seq_btn.setToolTip(
            "Sequential recording: arms the capture and waits for the first\n"
            "audible sample, then splits into a new file whenever audio is\n"
            "silent for the configured pause duration.\n"
            "Great for ripping playlists - no silent intro at the start."
        )
        self.seq_btn.clicked.connect(self._toggle_sequential)

        # Pulse shadow only on this button - gets the red glow during sequential.
        self.seq_shadow = QGraphicsDropShadowEffect(self.seq_btn)
        self.seq_shadow.setBlurRadius(self.PULSE_BLUR)
        self.seq_shadow.setOffset(0, 5)
        self.seq_shadow.setColor(QColor(*self.PULSE_RGB, 0))   # start invisible
        self.seq_btn.setGraphicsEffect(self.seq_shadow)

        record_row.addWidget(self.seq_btn, 4)

        # Two QPropertyAnimations, one per pulsing button. Stopped at idle
        # and started individually by _pulse_record_start / _pulse_seq_start,
        # so only the button currently driving a recording pulses.
        self.pulse_anim_rec = self._make_pulse_animation(self.rec_shadow)
        self.pulse_anim_seq = self._make_pulse_animation(self.seq_shadow)

        # Pause spinbox + caption
        pause_block = QVBoxLayout()
        pause_block.setSpacing(0)
        pause_block.setContentsMargins(0, 0, 0, 0)
        pause_cap = QLabel("PAUSE")
        pause_cap.setObjectName("seqPauseCap")
        pause_cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seq_pause_spin = QDoubleSpinBox()
        self.seq_pause_spin.setRange(0.5, 30.0)
        self.seq_pause_spin.setSingleStep(0.5)
        self.seq_pause_spin.setValue(2.0)
        self.seq_pause_spin.setDecimals(1)
        self.seq_pause_spin.setSuffix(" s")
        self.seq_pause_spin.setFixedWidth(86)
        self.seq_pause_spin.setToolTip(
            "Silence pause that triggers a file split (in seconds).\n"
            "Can be changed at any time - even while sequential recording\n"
            "is running. The new value applies from the next silence check."
        )
        pause_block.addWidget(pause_cap)
        pause_block.addWidget(self.seq_pause_spin)
        pause_wrap = QWidget()
        pause_wrap.setStyleSheet("background: transparent;")
        pause_wrap.setLayout(pause_block)
        record_row.addWidget(pause_wrap)

        rl.addLayout(record_row)

        rl.addWidget(self._build_live_card())
        rl.addWidget(self._build_log_card(), stretch=1)

        # Snapshot all tooltips set during build so we can quietly switch
        # them off while a recording is running and bring them back on
        # idle. See _set_tooltips_enabled() below.
        self._saved_tooltips: dict = {}
        for w in self.findChildren(QWidget):
            tip = w.toolTip()
            if tip:
                self._saved_tooltips[w] = tip

    def _set_tooltips_enabled(self, enabled: bool) -> None:
        """
        Toggle every tooltip captured at build time on or off.

        Rationale: while a recording is
        running we want a calm, uncluttered surface - no helpful hover
        bubbles popping up over Stop Recording or the VU meter. The
        moment we return to idle, every tooltip comes back exactly as
        defined in _build_ui.
        """
        if enabled:
            for w, tip in self._saved_tooltips.items():
                try:
                    w.setToolTip(tip)
                except RuntimeError:
                    # Widget was destroyed - skip silently.
                    pass
        else:
            for w in self._saved_tooltips.keys():
                try:
                    w.setToolTip("")
                except RuntimeError:
                    pass

    def _make_pulse_animation(self, shadow: QGraphicsDropShadowEffect) -> QPropertyAnimation:
        """Build an infinite 1-second alpha-pulse animation on `shadow.color`."""
        anim = QPropertyAnimation(shadow, b"color")
        anim.setDuration(1000)
        anim.setKeyValueAt(0.0, QColor(*self.PULSE_RGB, self.PULSE_ALPHA_MIN))
        anim.setKeyValueAt(0.5, QColor(*self.PULSE_RGB, self.PULSE_ALPHA_MAX))
        anim.setKeyValueAt(1.0, QColor(*self.PULSE_RGB, self.PULSE_ALPHA_MIN))
        anim.setLoopCount(-1)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        return anim

    def _pulse_record_start(self) -> None:
        """Start the red pulse on the Start/Stop-Recording button only."""
        self.pulse_anim_rec.start()

    def _pulse_seq_start(self) -> None:
        """Start the red pulse on the Sequential button only."""
        self.pulse_anim_seq.start()

    def _pulse_stop(self) -> None:
        """Freeze both pulse shadows to fully invisible (idle / not recording)."""
        self.pulse_anim_rec.stop()
        self.pulse_anim_seq.stop()
        self.rec_shadow.setColor(QColor(*self.PULSE_RGB, 0))
        self.seq_shadow.setColor(QColor(*self.PULSE_RGB, 0))

    def _build_source_card(self) -> QGroupBox:
        box = QGroupBox("\U0001F3AF  Source")
        box.setObjectName("sourceCard")
        box.setGraphicsEffect(make_card_shadow(box))

        v = QVBoxLayout(box)
        v.setSpacing(8)

        row1 = QHBoxLayout()
        row1.setSpacing(8)
        row1.addWidget(QLabel("Application:"))
        self.process_combo = QComboBox()
        self.process_combo.setMinimumWidth(340)
        self.process_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.process_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.process_combo.setToolTip(
            "Pick the running application whose audio you want to capture.\n"
            "Only processes that currently have an active audio session show up\n"
            "here. The list refreshes automatically every 2 seconds."
        )
        self.process_combo.currentIndexChanged.connect(self._on_process_selection_changed)
        row1.addWidget(self.process_combo, 1)

        self.refresh_btn = QPushButton("\u27F3")
        self.refresh_btn.setObjectName("iconBtn")
        self.refresh_btn.setFixedSize(36, 32)
        self.refresh_btn.setToolTip("Refresh process list (auto-refreshes every 2s)")
        self.refresh_btn.clicked.connect(self._refresh_processes)
        row1.addWidget(self.refresh_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(14)
        self.include_tree_cb = QCheckBox("Include children")
        self.include_tree_cb.setChecked(True)
        self.include_tree_cb.setToolTip(
            "Capture from the selected process AND any child processes it spawns."
        )
        self.include_tree_cb.stateChanged.connect(self._on_include_tree_toggled)
        row2.addWidget(self.include_tree_cb)

        self.wait_cb = QCheckBox("Wait for:")
        self.wait_cb.setToolTip(
            "Type an executable name. Recording starts as soon as the app opens\n"
            "an audio session."
        )
        row2.addWidget(self.wait_cb)

        # Stacked area to the right of the "Wait for:" checkbox.
        # Page 0 (default, checkbox UNCHECKED) - dim WASAPI capture-format
        #         readout for whatever source is currently being monitored.
        #         Filled in by _update_format_info() each time the capture
        #         object is created (or cleared).
        # Page 1 (checkbox CHECKED) - the editable wait-name field. The
        #         user types the exe name to wait for and the wait_timer
        #         keeps polling enumerate_sessions for it.
        self.source_info_stack = QStackedWidget()
        self.source_info_stack.setMaximumWidth(260)

        self.format_info = QLabel("\u2014")
        self.format_info.setObjectName("formatInfo")
        self.format_info.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        )
        self.format_info.setToolTip(
            "Live WASAPI capture format of the selected source.\n"
            "Sample rate, channel count, sample depth and raw data rate."
        )
        self.source_info_stack.addWidget(self.format_info)        # index 0

        self.wait_name = QLineEdit()
        self.wait_name.setMaximumWidth(260)
        self.wait_name.setToolTip(
            "Name of the executable to wait for (case-insensitive),\n"
            "e.g. spotify.exe or chrome.exe. Recording arms as soon as\n"
            "that process appears in the audio session list."
        )
        self.source_info_stack.addWidget(self.wait_name)          # index 1

        row2.addWidget(self.source_info_stack, 1)

        # Checkbox toggles which page is shown. The wait_name stays alive
        # behind the scenes when it's hidden, so whatever the user typed
        # is preserved if they re-tick the box later.
        self.wait_cb.toggled.connect(
            lambda checked: self.source_info_stack.setCurrentIndex(1 if checked else 0)
        )
        v.addLayout(row2)

        return box

    def _build_format_card(self) -> QGroupBox:
        box = QGroupBox("\U0001F39A  Format")
        box.setObjectName("formatCard")
        box.setGraphicsEffect(make_card_shadow(box))

        g = QGridLayout(box)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(7)

        g.addWidget(QLabel("Codec:"), 0, 0)
        self.codec_combo = QComboBox()
        for preset in CODEC_PRESETS:
            self.codec_combo.addItem(preset.label)
        self.codec_combo.setToolTip(
            "Output audio codec.\n"
            "  FLAC / ALAC  - lossless, ~50-60% of WAV size\n"
            "  MP3 / AAC / Opus - lossy, configurable bitrate\n"
            "  WAV - uncompressed PCM"
        )
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        g.addWidget(self.codec_combo, 0, 1)

        g.addWidget(QLabel("Bitrate:"), 0, 2)
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.setToolTip(
            "Target bitrate for lossy codecs. Higher = better quality, larger files.\n"
            "Greyed out for lossless codecs (FLAC, ALAC, WAV)."
        )
        g.addWidget(self.bitrate_combo, 0, 3)

        # The per-codec description label is intentionally absent -
        # the codec_combo tooltip + the universally-known FLAC/MP3/AAC
        # labels already convey what each preset is. _on_codec_changed
        # stays wired so bitrate enable/disable still works.
        self._on_codec_changed(0)
        return box

    def _build_output_card(self) -> QGroupBox:
        box = QGroupBox("\U0001F4BE  Output")
        box.setObjectName("outputCard")
        box.setGraphicsEffect(make_card_shadow(box))

        h = QHBoxLayout(box)
        h.setSpacing(8)
        h.addWidget(QLabel("Folder:"))
        self.save_dir_label = QLabel(str(self.save_dir))
        self.save_dir_label.setStyleSheet(f"color: {C.AMBER}; background: transparent;")
        self.save_dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.save_dir_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.save_dir_label.setToolTip(
            "Folder where recordings are saved. Click 'Browse...' to change it."
        )
        h.addWidget(self.save_dir_label, 1)
        btn = QPushButton("\U0001F4C1  Browse\u2026")
        btn.setToolTip("Choose where recorded files are saved.")
        btn.clicked.connect(self._browse_save_dir)
        h.addWidget(btn)
        return box

    def _build_live_card(self) -> QGroupBox:
        box = QGroupBox("\U0001F4CA  Live")
        box.setObjectName("liveCard")
        box.setGraphicsEffect(make_card_shadow(box))

        v = QVBoxLayout(box)
        v.setSpacing(8)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusReady")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setToolTip(
            "Current capture state - 'Ready' when idle, 'Recording' during a normal\n"
            "single-file capture, 'Sequential' while in playlist-split mode."
        )
        v.addWidget(self.status_label)

        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.time_label = QLabel("00:00:00:00"); self.time_label.setObjectName("bigStat")
        self.time_label.setToolTip("Total elapsed recording time.")
        self.size_label = QLabel("0.0 MB"); self.size_label.setObjectName("bigStat")
        self.size_label.setToolTip("Size of the current output file on disk.")
        self.rate_label = QLabel("\u2014 kB/s"); self.rate_label.setObjectName("bigStat")
        self.rate_label.setToolTip("Current write throughput to disk (averaged over the last second).")
        for cap_text, value in [("TIME", self.time_label),
                                ("SIZE", self.size_label),
                                ("RATE", self.rate_label)]:
            block = QVBoxLayout()
            block.setContentsMargins(0, 0, 0, 0)
            block.setSpacing(1)
            cap = QLabel(cap_text)
            cap.setObjectName("statCaption")
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            block.addWidget(cap); block.addWidget(value)
            wrap = QWidget(); wrap.setStyleSheet("background: transparent;"); wrap.setLayout(block)
            stats.addWidget(wrap, 1)
        v.addLayout(stats)

        # ===== Meter area: VU OR Spectrogram, click anywhere to swap =====
        # Both widgets emit clicked() on left-press; they are stacked
        # inside the same dark frame so the toggle is purely visual,
        # no layout reshuffle. Cursor is set to a pointing hand on the
        # frame to advertise the click affordance.
        meter_frame = QFrame()
        meter_frame.setObjectName("vuFrame")
        meter_frame.setCursor(Qt.CursorShape.PointingHandCursor)
        meter_frame.setToolTip(
            "Click anywhere in this panel to toggle between the VU meter\n"
            "and the scrolling Spectrogram (waterfall display).\n"
            "Both update whether or not a recording is running."
        )
        mv = QVBoxLayout(meter_frame)
        mv.setContentsMargins(12, 10, 12, 10)

        self.meter_stack = QStackedWidget()
        self.vu  = VuMeter()
        self.spec = SpectrogramWidget()
        self.meter_stack.addWidget(self.vu)          # index 0
        self.meter_stack.addWidget(self.spec)        # index 1
        self.meter_stack.setCurrentIndex(0)
        mv.addWidget(self.meter_stack)

        # Both widgets call the same handler - click on either side flips.
        self.vu.clicked.connect(self._toggle_meter_view)
        self.spec.clicked.connect(self._toggle_meter_view)

        v.addWidget(meter_frame)

        return box

    def _build_log_card(self) -> QGroupBox:
        box = QGroupBox("\U0001F4DC  Log")
        box.setObjectName("logCard")
        box.setGraphicsEffect(make_card_shadow(box))
        v = QVBoxLayout(box)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(80)
        v.addWidget(self.log_view)
        return box

    def _show_about(self) -> None:
        AboutDialog(self).exec()

    def _wire_signals(self) -> None:
        self.log_signal.connect(self._append_log)
        self.status_event.connect(self._on_status_event)

    def _start_timers(self) -> None:
        self.proc_timer = QTimer(self)
        self.proc_timer.setInterval(2000)
        self.proc_timer.timeout.connect(self._refresh_processes)
        self.proc_timer.start()

        self.vu_timer = QTimer(self)
        self.vu_timer.setInterval(40)
        self.vu_timer.timeout.connect(self._update_vu)
        self.vu_timer.start()

        self.stat_timer = QTimer(self)
        # 30 Hz: lets the centisecond TIME field and the 0.1 MB SIZE
        # field tick visibly. Each call is ~50 us (one file stat, three
        # setText, one RMS-vs-threshold compare for sequential split),
        # so ~1.5 ms/sec total - negligible.
        self.stat_timer.setInterval(33)
        self.stat_timer.timeout.connect(self._on_stat_tick)
        self.stat_timer.start()

        self.wait_timer = QTimer(self)
        self.wait_timer.setInterval(1000)
        self.wait_timer.timeout.connect(self._check_wait_for_app)
        self.wait_timer.start()

    # ===== Settings (AudioOBS.json) =================================
    def _load_or_create_settings(self) -> None:
        self._loading_settings = True
        try:
            if SETTINGS_FILE.exists():
                try:
                    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._apply_settings(data)
                except Exception as e:
                    self.log("WARN", f"Could not parse AudioOBS.json: {e}")
            else:
                self.log("INFO", "AudioOBS.json not found, creating with defaults.")
        finally:
            self._loading_settings = False
        self._save_settings()

    def _apply_settings(self, data: dict) -> None:
        # ----- save_dir ---------------------------------------------
        # We log the fall-back explicitly: if the user's previous
        # output drive is unplugged or a folder got moved/deleted,
        # they want to know AudioOBS quietly went back to Desktop
        # instead of silently writing to a stale path. The default
        # value (Desktop or home) was already set in __init__, so we
        # only need to switch to the stored one if it's still usable.
        if "save_dir" in data:
            try:
                raw = str(data["save_dir"])
                d = Path(raw)
                if d.exists() and d.is_dir():
                    self.save_dir = d
                    self.save_dir_label.setText(str(d))
                else:
                    self.log("WARN",
                             f"Saved output folder is unavailable: {raw}")
                    self.log("INFO",
                             f"Falling back to default: {self.save_dir}")
                    self.save_dir_label.setText(str(self.save_dir))
            except Exception:
                pass
        # ----- codec_index ------------------------------------------
        if "codec_index" in data:
            try:
                idx = int(data["codec_index"])
                if 0 <= idx < self.codec_combo.count():
                    self.codec_combo.setCurrentIndex(idx)
                    self._on_codec_changed(idx)
            except Exception:
                pass
        # ----- bitrate_index ----------------------------------------
        if "bitrate_index" in data:
            try:
                idx = int(data["bitrate_index"])
                if 0 <= idx < self.bitrate_combo.count():
                    self.bitrate_combo.setCurrentIndex(idx)
            except Exception:
                pass
        # ----- include_children -------------------------------------
        if "include_children" in data:
            try:
                self.include_tree_cb.setChecked(bool(data["include_children"]))
            except Exception:
                pass
        # ----- seq_pause_seconds ------------------------------------
        if "seq_pause_seconds" in data:
            try:
                v = float(data["seq_pause_seconds"])
                v = max(0.5, min(30.0, v))
                self.seq_pause_spin.setValue(v)
            except Exception:
                pass
        # ----- window_size ------------------------------------------
        if "window_size" in data:
            try:
                w, h = data["window_size"]
                # Floor matches setMinimumSize() in _build_ui so a stale
                # AudioOBS.json from an earlier layout can't squeeze the
                # window narrower than the three-button row needs.
                self.resize(max(800, int(w)), max(860, int(h)))
            except Exception:
                pass
        # ----- window_pos -------------------------------------------
        if "window_pos" in data:
            try:
                x, y = data["window_pos"]
                # Only restore if the position is on a visible part of
                # any screen. If the user disconnected a second monitor
                # since the last run we'd otherwise spawn off-screen.
                target = QApplication.primaryScreen().availableVirtualGeometry()
                if (target.left() - 50 <= int(x) <= target.right() - 100 and
                        target.top() - 20 <= int(y) <= target.bottom() - 100):
                    self.move(int(x), int(y))
            except Exception:
                pass
        # ----- last_source_exe --------------------------------------
        if "last_source_exe" in data:
            try:
                exe = str(data["last_source_exe"]).strip()
                if exe:
                    self._last_known_source_exe = exe
            except Exception:
                pass
        # ----- wait_for_exe (text of the 'Wait for:' field) ---------
        # Stored independently from the checkbox so the field can be
        # pre-filled even when the user re-launches with the box off
        # (matches the in-session behaviour where wait_name keeps its
        # text while hidden behind the format-info page).
        if "wait_for_exe" in data:
            try:
                exe = str(data["wait_for_exe"]).strip()
                # Be polite: just clamp the length so a corrupted
                # settings file can't put 5 MB of text in the line edit.
                if 0 < len(exe) <= 260:
                    self.wait_name.setText(exe)
            except Exception:
                pass
        # ----- wait_for_enabled (checkbox state) --------------------
        if "wait_for_enabled" in data:
            try:
                self.wait_cb.setChecked(bool(data["wait_for_enabled"]))
            except Exception:
                pass
        # ----- meter_view_index (0 = VU, 1 = Spectrogram) -----------
        # Only stack indices we actually have (currently 2). Anything
        # else is silently ignored - the meter_stack stays at its
        # default index 0 (VU meter).
        if "meter_view_index" in data:
            try:
                idx = int(data["meter_view_index"])
                if 0 <= idx < self.meter_stack.count():
                    # Use _toggle_meter_view-equivalent state update so
                    # _spectrogram_visible matches what we're showing.
                    self.meter_stack.setCurrentIndex(idx)
                    self._spectrogram_visible = (idx == 1)
                    if idx == 1:
                        # Make sure the spec gets a fresh start, just
                        # like a runtime toggle would.
                        try:
                            self.spec.clear()
                        except Exception:
                            pass
            except Exception:
                pass

    def _save_settings(self) -> None:
        if self._loading_settings:
            return
        # Defensive: every widget access here is wrapped in hasattr so
        # an early-shutdown save (e.g. closeEvent during a UI build
        # exception) can't crash on a half-constructed window.
        data = {
            "save_dir":          str(self.save_dir),
            "codec_index":       self.codec_combo.currentIndex(),
            "bitrate_index":     self.bitrate_combo.currentIndex(),
            "include_children":  self.include_tree_cb.isChecked(),
            "seq_pause_seconds": self.seq_pause_spin.value(),
            "window_size":       [self.width(), self.height()],
            "window_pos":        [self.x(), self.y()],
            # Remember the last picked audio source so the next launch can
            # either auto-select it (if it's running) or fall back to
            # "Wait for: <that exe>" without bothering the user with a
            # 'no source' message-box.
            "last_source_exe":   self._current_executable() or self._last_known_source_exe or "",
            # Full save-state: also persist the "Wait for:" UI state
            # and which meter view (VU vs Spectrogram) the user had
            # open. These survive a restart so the next launch looks
            # exactly like the last one was left.
            "wait_for_enabled":  self.wait_cb.isChecked() if hasattr(self, "wait_cb") else False,
            "wait_for_exe":      self.wait_name.text().strip() if hasattr(self, "wait_name") else "",
            "meter_view_index":  self.meter_stack.currentIndex() if hasattr(self, "meter_stack") else 0,
            # Schema marker so future versions can spot old files and
            # migrate them gracefully if the shape ever changes.
            "settings_version":  __version__,
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log("WARN", f"Could not write AudioOBS.json: {e}")

    def _wire_settings_persistence(self) -> None:
        self.codec_combo.currentIndexChanged.connect(self._save_settings)
        self.bitrate_combo.currentIndexChanged.connect(self._save_settings)
        self.include_tree_cb.stateChanged.connect(self._save_settings)
        self.seq_pause_spin.valueChanged.connect(self._save_settings)
        self.seq_pause_spin.valueChanged.connect(self._on_pause_value_changed)
        # New in 1.01: track every other piece of UI state too, so the
        # JSON acts as a full save-state. wait_name uses a 500 ms
        # debouncer so typing doesn't beat up the disk.
        self.wait_cb.toggled.connect(self._save_settings)
        self.wait_name.textChanged.connect(
            lambda _txt: self._wait_name_save_timer.start()
        )
        self.meter_stack.currentChanged.connect(self._save_settings)

    def _on_pause_value_changed(self, new_value: float) -> None:
        """
        Called whenever the PAUSE spinbox value changes. During an active
        sequential recording we log the change so the user can correlate
        any subsequent split timing with the new threshold.
        """
        if self.sequential_mode:
            self.log("INFO",
                     f"PAUSE adjusted live to {new_value:.1f}s "
                     f"(takes effect on next silence check)")

    # ===== DLL unavailable ==========================================
    def _set_dll_unavailable(self) -> None:
        self.process_combo.addItem("(wincaptureaudio DLL unavailable)")
        self.process_combo.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.seq_btn.setEnabled(False)
        self.split_btn.setEnabled(False)
        self.status_label.setObjectName("statusError")
        self.status_label.setText("DLL not loaded")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self._pulse_stop()
        self.log("ERROR", f"Could not import wincaptureaudio: {WCA_ERROR}")
        self.log("ERROR", "Build the DLL first (see project README), then restart this GUI.")

    # ===== Codec/bitrate ============================================
    def _on_codec_changed(self, idx: int) -> None:
        preset = CODEC_PRESETS[idx]
        self.bitrate_combo.clear()
        if preset.bitrates:
            self.bitrate_combo.addItems(preset.bitrates)
            self.bitrate_combo.setCurrentIndex(len(preset.bitrates) - 1)
            self.bitrate_combo.setEnabled(True)
        else:
            self.bitrate_combo.addItem("\u2014  lossless / PCM")
            self.bitrate_combo.setEnabled(False)

    def _current_preset(self) -> CodecPreset:
        return CODEC_PRESETS[self.codec_combo.currentIndex()]

    # ===== Process picker ===========================================
    def _refresh_processes(self) -> None:
        if not wca:
            return
        try:
            sessions = wca.enumerate_sessions()
        except Exception as e:
            self.log("ERROR", f"enumerate_sessions failed: {e}")
            return

        prev_pid = self._current_pid()
        self.process_combo.blockSignals(True)
        self.process_combo.clear()
        for s in sessions:
            self.process_combo.addItem(
                f"  {s.executable}  \u00B7  PID {s.pid}",
                userData=(s.pid, s.executable),
            )
        if prev_pid is not None:
            for i in range(self.process_combo.count()):
                data = self.process_combo.itemData(i)
                if data and data[0] == prev_pid:
                    self.process_combo.setCurrentIndex(i)
                    break
        self.process_combo.blockSignals(False)

        if not sessions and not self.recording_started:
            self.process_combo.addItem("  (no audio-active processes)", userData=(0, ""))

        self._reconcile_monitor()

    def _current_pid(self) -> Optional[int]:
        data = self.process_combo.currentData()
        if data is None:
            return None
        pid, _ = data
        return pid if pid else None

    def _current_executable(self) -> Optional[str]:
        data = self.process_combo.currentData()
        if data is None:
            return None
        _, exe = data
        return exe if exe else None

    def _on_process_selection_changed(self, _idx: int) -> None:
        # Remember the picked exe for the next launch so the source
        # selection survives an AudioOBS restart.
        exe = self._current_executable()
        if exe:
            self._last_known_source_exe = exe
            self._save_settings()
        self._reconcile_monitor()

    def _on_include_tree_toggled(self, _state: int) -> None:
        if self.recording_started:
            return
        self._monitor_pid = None
        self._reconcile_monitor()

    # ===== Monitor mode =============================================
    def _reconcile_monitor(self) -> None:
        if not wca or self.recording_started:
            return
        desired_pid = self._current_pid()
        if desired_pid == self._monitor_pid:
            return
        self._stop_capture_silent()
        if desired_pid:
            self._start_monitor(desired_pid)

    def _start_monitor(self, pid: int) -> None:
        try:
            cap = wca.Capture.by_pid(
                pid,
                include_tree=self.include_tree_cb.isChecked(),
                on_audio=None,
                on_status=self._on_status_from_dll,
            )
        except Exception as e:
            self.log("WARN", f"Monitor start failed for PID {pid}: {e}")
            return

        channels = cap.format.channels
        self._channels = channels

        # Use the same monitor-callback factory the rest of the code path
        # uses (so both spectrogram feeding and the np.dot RMS shortcut
        # apply identically from the very first capture).
        cap._audio_cb = self._make_monitor_callback(channels)
        self.capture = cap
        self._monitor_pid = pid
        # Tell the spectrogram what sample rate to expect, in case the
        # user toggles to it before recording starts.
        try:
            self.spec.set_sample_rate(cap.format.sample_rate)
        except Exception:
            pass
        # Update the WASAPI format-info label (replaces the placeholder
        # in the empty space next to "Wait for:" when that checkbox is
        # unchecked).
        self._update_format_info()
        self.log("INFO", f"Monitoring PID {pid} ({self._current_executable() or '?'})")

    def _stop_capture_silent(self) -> None:
        if self.capture:
            try:
                self.capture.stop()
            except Exception:
                pass
            self.capture = None
            self._monitor_pid = None
        with self._rms_lock:
            self._cur_rms_l = self._cur_rms_r = 0.0
        if hasattr(self, "vu"):
            self.vu.reset()
        if hasattr(self, "spec"):
            self.spec.clear()
        if hasattr(self, "format_info"):
            self.format_info.setText("\u2014")

    # ===== Wait-for-app =============================================
    def _check_wait_for_app(self) -> None:
        if not self.wait_cb.isChecked() or self.recording_started:
            return
        if not wca:
            return
        name = self.wait_name.text().strip().lower()
        if not name:
            return
        try:
            sessions = wca.enumerate_sessions()
        except Exception:
            return
        for s in sessions:
            if s.executable.lower() == name:
                self.log("INFO", f"Wait-target appeared: {s.executable} (PID {s.pid})")
                self.wait_cb.setChecked(False)
                self._refresh_processes()
                for i in range(self.process_combo.count()):
                    data = self.process_combo.itemData(i)
                    if data and data[0] == s.pid:
                        self.process_combo.setCurrentIndex(i)
                        break
                self._start_recording()
                return

    # ===== Save location ============================================
    def _browse_save_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Select output folder", str(self.save_dir))
        if d:
            self.save_dir = Path(d)
            self.save_dir_label.setText(str(self.save_dir))
            self._save_settings()

    def _build_output_path(self, exe_name: str, ext: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_exe = re.sub(r"\.exe$", "", exe_name, flags=re.IGNORECASE)
        safe_exe = re.sub(r"[^A-Za-z0-9._-]", "_", safe_exe) or "Unknown"
        return self.save_dir / f"AudioOBS_{stamp}_{safe_exe}.{ext}"

    def _build_seq_output_path(self, exe_name: str, ext: str, seg_num: int) -> Path:
        safe_exe = re.sub(r"\.exe$", "", exe_name, flags=re.IGNORECASE)
        safe_exe = re.sub(r"[^A-Za-z0-9._-]", "_", safe_exe) or "Unknown"
        return self.save_dir / f"AudioOBS_{self._seq_session_stamp}_seg{seg_num:03d}_{safe_exe}.{ext}"

    # ===== Single-mode recording ====================================
    def _toggle_recording(self) -> None:
        if self.sequential_mode:
            return
        if self.recording_started:
            self._stop_recording()
        else:
            self._start_recording()

    def _ensure_capture(self, pid: int) -> bool:
        if self.capture and self._monitor_pid == pid:
            return True
        self._stop_capture_silent()
        try:
            cap = wca.Capture.by_pid(
                pid,
                include_tree=self.include_tree_cb.isChecked(),
                on_audio=None,
                on_status=self._on_status_from_dll,
            )
        except Exception as e:
            self.log("ERROR", f"start capture failed: {e}")
            return False
        self.capture = cap
        self._monitor_pid = pid
        self._channels = cap.format.channels
        return True

    def _open_ffmpeg(self, out_path: Path) -> bool:
        if not self.capture:
            return False
        fmt = self.capture.format
        preset = self._current_preset()
        # Prefer the resolved path from the startup probe so a non-PATH
        # install location still works; fall back to the bare command
        # so a freshly-installed PATH entry is also picked up without
        # restarting the app.
        ffmpeg_cmd = self._ffmpeg_path or "ffmpeg"
        cmd = [
            ffmpeg_cmd, "-hide_banner", "-loglevel", "warning", "-y",
            *fmt.ffmpeg_input_args,
            "-c:a", preset.codec,
        ]
        if preset.bitrates and self.bitrate_combo.isEnabled():
            cmd += ["-b:a", self.bitrate_combo.currentText()]
        cmd += [str(out_path)]
        self.log("INFO", "ffmpeg " + " ".join(cmd[1:]))

        # ffmpeg gets HIGH priority + a larger stdin pipe buffer.
        # The OS default pipe size is 4 KB on Windows, which is just
        # ~10 ms of float32 stereo @ 48 kHz - way too tight. 256 KB gives
        # us ~700 ms of headroom for a brief encoder hiccup.
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | (HIGH_PRIORITY_CLASS if sys.platform == "win32" else 0)
        )
        try:
            self.ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                bufsize=256 * 1024,                # 256 KB Python-side buffer
            )
        except FileNotFoundError:
            # Path may have changed since startup (user just installed
            # ffmpeg) - re-probe before giving up. If we now find it
            # but the path differs, retry the spawn once. If we still
            # can't find anything, log the failure and update the
            # status indicator, but do NOT pop the dialog again - the
            # ffmpeg-check is a startup-only event so the dialog can
            # only ever appear once per session.
            new_path = find_ffmpeg()
            if new_path and new_path != self._ffmpeg_path:
                self.log("OK", f"ffmpeg located on retry: {new_path}")
                self._ffmpeg_path = new_path
                cmd[0] = new_path
                try:
                    self.ffmpeg_proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        creationflags=creationflags,
                        bufsize=256 * 1024,
                    )
                    return True
                except FileNotFoundError:
                    pass
            self._ffmpeg_path = None
            self.log("ERROR",
                     "ffmpeg not found - cannot start recording. "
                     "Install ffmpeg and restart AudioOBS "
                     "(see the startup dialog for instructions).")
            # Brief visible cue without a modal dialog.
            try:
                self.status_label.setText("ffmpeg missing")
            except Exception:
                pass
            self.ffmpeg_proc = None
            return False
        return True

    def _close_ffmpeg(self) -> None:
        if not self.ffmpeg_proc:
            return
        try:
            if self.ffmpeg_proc.stdin:
                self.ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            _, stderr = self.ffmpeg_proc.communicate(timeout=15)
            if stderr:
                txt = stderr.decode("utf-8", "replace").strip()
                if txt:
                    self.log("INFO", "ffmpeg: " + txt.splitlines()[-1])
        except subprocess.TimeoutExpired:
            self.ffmpeg_proc.kill()
            self.log("WARN", "ffmpeg did not exit in time, killed.")
        self.ffmpeg_proc = None

    def _make_recording_callback(self, channels: int):
        """
        Audio callback for the recording path.

        Hot path priorities, in order:
          1. WRITE TO FFMPEG. Anything else can be skipped on a tight tick;
             this cannot, because a missed write is permanent data loss.
          2. Update the RMS state for the VU meter / silence detection.
             We throttle this to ~5 Hz: the GUI only renders at 25 Hz max
             and silence detection only runs at 5 Hz, so computing RMS
             10x more often is wasted work that just creates GC pressure.

        We use np.dot(x, x) / N (a single BLAS call) instead of np.mean(x**2)
        to avoid allocating a temporary squared array on every packet.
        """
        # Per-callback persistent state lives in this closure
        state = {"last_rms_ts": 0.0, "elapsed_since_rms": 0.0}
        RMS_INTERVAL_S = 0.05      # 20 Hz - more than enough for the VU meter

        def on_audio_recording(pcm: bytes, num_frames: int, ts: int) -> None:
            # ---- 1. Critical: ship bytes to ffmpeg ----
            try:
                if self.ffmpeg_proc and self.ffmpeg_proc.stdin:
                    self.ffmpeg_proc.stdin.write(pcm)
            except (BrokenPipeError, ValueError, OSError):
                pass

            # ---- 2. Parse once - this is a zero-copy view on the pcm
            #         bytes, ~100 ns, used by both spectrogram and RMS.
            try:
                arr = np.frombuffer(pcm, dtype=np.float32)
                if channels > 1:
                    arr = arr.reshape(-1, channels)
            except Exception:
                return

            # ---- 3. Spectrogram feed (only when the panel is visible)
            #         The .copy() is required because the pcm bytes are
            #         released right after this callback returns; the
            #         spectrogram's deque holds onto the data until the
            #         GUI thread consumes it.
            if self._spectrogram_visible:
                try:
                    self.spec.feed_chunk(arr.copy())
                except Exception:
                    pass

            # ---- 4. Throttled RMS computation (20 Hz - plenty for VU
            #         and the silence detector).
            state["elapsed_since_rms"] += num_frames / 48000.0
            if state["elapsed_since_rms"] < RMS_INTERVAL_S:
                return
            state["elapsed_since_rms"] = 0.0

            try:
                if channels > 1:
                    n = arr.shape[0]
                    if n > 0:
                        l = arr[:, 0]
                        r = arr[:, 1]
                        rms_l = math.sqrt(float(np.dot(l, l)) / n)
                        rms_r = math.sqrt(float(np.dot(r, r)) / n)
                    else:
                        rms_l = rms_r = 0.0
                else:
                    n = arr.size
                    if n > 0:
                        rms_l = rms_r = math.sqrt(float(np.dot(arr, arr)) / n)
                    else:
                        rms_l = rms_r = 0.0
                with self._rms_lock:
                    self._cur_rms_l = rms_l
                    self._cur_rms_r = rms_r
            except Exception:
                pass
        return on_audio_recording

    def _make_monitor_callback(self, channels: int):
        """
        Audio callback for monitor mode (no ffmpeg). Same throttling as
        the recording callback, just without the write step.
        """
        state = {"elapsed_since_rms": 0.0}
        RMS_INTERVAL_S = 0.05

        def on_audio_monitor(pcm: bytes, num_frames: int, ts: int) -> None:
            # Parse once - zero-copy view used by both feeders.
            try:
                arr = np.frombuffer(pcm, dtype=np.float32)
                if channels > 1:
                    arr = arr.reshape(-1, channels)
            except Exception:
                return

            # Spectrogram feed (only when visible).
            if self._spectrogram_visible:
                try:
                    self.spec.feed_chunk(arr.copy())
                except Exception:
                    pass

            # Throttled RMS (20 Hz, like the recording path).
            state["elapsed_since_rms"] += num_frames / 48000.0
            if state["elapsed_since_rms"] < RMS_INTERVAL_S:
                return
            state["elapsed_since_rms"] = 0.0

            try:
                if channels > 1:
                    n = arr.shape[0]
                    if n > 0:
                        l = arr[:, 0]
                        r = arr[:, 1]
                        rms_l = math.sqrt(float(np.dot(l, l)) / n)
                        rms_r = math.sqrt(float(np.dot(r, r)) / n)
                    else:
                        rms_l = rms_r = 0.0
                else:
                    n = arr.size
                    if n > 0:
                        rms_l = rms_r = math.sqrt(float(np.dot(arr, arr)) / n)
                    else:
                        rms_l = rms_r = 0.0
                with self._rms_lock:
                    self._cur_rms_l = rms_l
                    self._cur_rms_r = rms_r
            except Exception:
                pass
        return on_audio_monitor

    def _start_recording(self) -> None:
        if not wca:
            return
        pid = self._current_pid()
        exe = self._current_executable() or "Unknown"
        if not pid:
            QMessageBox.warning(self, "No source", "Pick a running application first.")
            return

        if not self._ensure_capture(pid):
            return
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("ERROR", f"Could not create output folder: {e}")
            return

        preset = self._current_preset()
        out_path = self._build_output_path(exe, preset.extension)

        fmt = self.capture.format
        self.log("INFO",
                 f"Recording: {exe} (PID {pid})  \u2192  "
                 f"{fmt.sample_rate} Hz \u00D7 {fmt.channels} ch \u00B7 float32")

        if not self._open_ffmpeg(out_path):
            return

        self.capture._audio_cb = self._make_recording_callback(fmt.channels)
        self._current_out_path = out_path
        self.recording_started = datetime.now()
        self._segment_started = self.recording_started

        self._set_record_btn(recording=True, text="\u23F9  Stop Recording")
        self.seq_btn.setEnabled(False)
        self.split_btn.setEnabled(True)              # split available during normal recording
        self._set_status("\u25CF Recording", "statusRec")
        self._pulse_record_start()       # red pulse only on the active button
        self._set_tooltips_enabled(False)
        self.process_combo.setEnabled(False)
        self.include_tree_cb.setEnabled(False)

        self._recording_about_to_start()

    def _stop_recording(self) -> None:
        self._close_ffmpeg()
        path = self._current_out_path
        if path and Path(path).exists():
            size_mb = Path(path).stat().st_size / (1024 * 1024)
            self.log("OK", f"Saved {path.name} ({size_mb:.1f} MB)")
        else:
            self.log("WARN", "No output file was produced.")

        if self.capture:
            self.capture._audio_cb = self._make_monitor_callback(self.capture.format.channels)

        self.recording_started = None
        self._segment_started = None
        self._current_out_path = None

        self._set_record_btn(recording=False, text="\u23FA  Start Recording")
        self.seq_btn.setEnabled(True)
        self.split_btn.setEnabled(False)
        self._set_status("Ready", "statusReady")
        self.process_combo.setEnabled(True)
        self.include_tree_cb.setEnabled(True)

        self._pulse_stop()               # back to calm green idle
        self._set_tooltips_enabled(True)
        self._recording_just_stopped()

    # ----- GC management around recording -----
    def _recording_about_to_start(self) -> None:
        """
        Disable automatic garbage collection so it can't fire mid-callback.
        Python's GC normally pauses for ~5-50 ms when sweeping; that's
        plenty to cause a WASAPI dropout. We instead run a full collection
        right before recording and then let it idle until we stop, at which
        point we re-enable it and run one more collection ourselves.
        """
        gc.collect()
        gc.disable()
        self.log("INFO", "GC disabled for the duration of recording")

    def _recording_just_stopped(self) -> None:
        """Re-enable GC and tidy up any garbage that built up."""
        gc.enable()
        gc.collect()
        self.log("INFO", "GC re-enabled")

    # ===== Sequential recording =====================================
    def _toggle_sequential(self) -> None:
        if self.recording_started and not self.sequential_mode:
            return
        if self.sequential_mode:
            self._stop_sequential()
        else:
            self._start_sequential()

    def _start_sequential(self) -> None:
        if not wca:
            return
        pid = self._current_pid()
        exe = self._current_executable() or "Unknown"
        if not pid:
            QMessageBox.warning(self, "No source", "Pick a running application first.")
            return

        if not self._ensure_capture(pid):
            return
        try:
            self.save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log("ERROR", f"Could not create output folder: {e}")
            return

        fmt = self.capture.format
        self.capture._audio_cb = self._make_recording_callback(fmt.channels)

        self.sequential_mode = True
        self.recording_started = datetime.now()
        self._seq_session_stamp = self.recording_started.strftime("%Y%m%d_%H%M%S")
        self._seq_segment_count = 0
        self._seq_silence_start = None

        # ----- Arm and wait -----
        # We don't open a file yet. The first segment opens the moment
        # _check_sequential_split() sees audio above the silence threshold.
        # This way the user can hit Sequential first, queue up the player,
        # and the recording starts cleanly with the first audible sample
        # instead of capturing a few seconds of silence prefix.
        self._seq_waiting = True

        self.log("INFO",
                 f"Sequential armed: {exe} (PID {pid})  \u2192  "
                 f"split on {self.seq_pause_spin.value():.1f}s silence")
        self.log("INFO", "Waiting for first audio - press Play in your source app.")

        self.record_btn.setEnabled(False)
        self._set_seq_btn(recording=True, text="\u23F9  Stop Sequential")
        self._set_status("\u23F8 Armed - waiting for first audio\u2026", "statusWait")
        self._pulse_seq_start()          # red pulse only on the active button
        self._set_tooltips_enabled(False)
        self.process_combo.setEnabled(False)
        self.include_tree_cb.setEnabled(False)
        # Note: seq_pause_spin stays enabled - PAUSE is editable on the fly.

        self._recording_about_to_start()

    def _seq_begin_segment(self) -> None:
        if not self.capture:
            return
        self._seq_segment_count += 1
        exe = self._current_executable() or "Unknown"
        preset = self._current_preset()
        out_path = self._build_seq_output_path(exe, preset.extension, self._seq_segment_count)
        if self._open_ffmpeg(out_path):
            self._current_out_path = out_path
            self._segment_started = datetime.now()
            self._seq_waiting = False
            self.split_btn.setEnabled(True)                 # we have a segment to split
            self.log("INFO", f"[Seq #{self._seq_segment_count}] Recording \u2192 {out_path.name}")
            self._set_status(f"\u25CF Sequential segment {self._seq_segment_count}", "statusSeq")

    def _seq_finish_segment(self) -> None:
        self._close_ffmpeg()
        path = self._current_out_path
        if path and Path(path).exists():
            size_mb = Path(path).stat().st_size / (1024 * 1024)
            self.log("OK", f"[Seq #{self._seq_segment_count}] Saved {path.name} ({size_mb:.1f} MB)")
        self._current_out_path = None
        self._segment_started = None
        self._seq_waiting = True
        self.split_btn.setEnabled(False)                    # back to waiting, nothing to split
        self._set_status(
            f"\u23F8 Waiting for audio  ({self._seq_segment_count} saved)",
            "statusWait",
        )

    def _split_now(self) -> None:
        """
        Manually close the current file and immediately open a new one.
        Works both in normal recording (manual track boundary) and in
        Sequential (override the auto-detected silence gap when two
        songs blend together without a detectable silence pause).

        Always triggers a brief click-flash on the button for tactile
        feedback - it's a one-shot action with no persistent state, so
        the flash is the only visual cue the user gets that the press
        registered.
        """
        if not self.recording_started or not self.ffmpeg_proc:
            return

        # ---- visual: short post-click flash via [flashed="true"] ----
        # The CSS :pressed pseudo-state already darkens while the mouse
        # is held; this extends the confirmation past the release so
        # quick clicks still give an unambiguous visual.
        self.split_btn.setProperty("flashed", "true")
        self.split_btn.style().unpolish(self.split_btn)
        self.split_btn.style().polish(self.split_btn)
        QTimer.singleShot(180, self._end_split_flash)

        # ---- actual split ----
        if self.sequential_mode:
            # Sequential: close current segment, log as manual split,
            # then re-arm and open the next segment immediately. Same
            # filename scheme (..._segNNN_...) the auto-split uses.
            finished_idx = self._seq_segment_count
            self._close_ffmpeg()
            path = self._current_out_path
            if path and Path(path).exists():
                size_mb = Path(path).stat().st_size / (1024 * 1024)
                self.log("OK",
                         f"[Seq #{finished_idx}] Saved {path.name} ({size_mb:.1f} MB)  "
                         f"\u2014 manual split")
            self._current_out_path = None
            self._segment_started = None
            # Reset the silence detector so a tiny dip right after the
            # split doesn't immediately trigger an auto-finish on the
            # new segment.
            self._seq_silence_start = None
            self._seq_begin_segment()
        else:
            # Normal single-file recording: close the current file and
            # open a fresh one using the standard timestamped name.
            self._close_ffmpeg()
            path = self._current_out_path
            if path and Path(path).exists():
                size_mb = Path(path).stat().st_size / (1024 * 1024)
                self.log("OK",
                         f"Saved {path.name} ({size_mb:.1f} MB)  \u2014 manual split")

            exe = self._current_executable() or "Unknown"
            preset = self._current_preset()
            new_path = self._build_output_path(exe, preset.extension)
            if self._open_ffmpeg(new_path):
                self._current_out_path = new_path
                # Reset both timers so the new file gets its own TIME
                # readout starting at 00:00:00 - matches the mental
                # model of "this is a new recording".
                self.recording_started = datetime.now()
                self._segment_started   = self.recording_started
                self.log("INFO", f"Recording \u2192 {new_path.name}")
            else:
                # Failed to reopen ffmpeg - treat as a hard stop, fall
                # back to the same teardown _stop_recording does.
                self.log("ERROR",
                         "Could not open new file after split - stopping.")
                self.recording_started   = None
                self._segment_started    = None
                self._current_out_path   = None
                self._set_record_btn(recording=False, text="\u23FA  Start Recording")
                self.seq_btn.setEnabled(True)
                self.split_btn.setEnabled(False)
                self._set_status("Ready", "statusReady")
                self.process_combo.setEnabled(True)
                self.include_tree_cb.setEnabled(True)
                self._pulse_stop()
                self._set_tooltips_enabled(True)
                self._recording_just_stopped()

    def _end_split_flash(self) -> None:
        """Remove the post-click flash class from the Split-Now button."""
        if not hasattr(self, "split_btn"):
            return
        self.split_btn.setProperty("flashed", "false")
        try:
            self.split_btn.style().unpolish(self.split_btn)
            self.split_btn.style().polish(self.split_btn)
        except RuntimeError:
            pass

    def _stop_sequential(self) -> None:
        if self.ffmpeg_proc:
            self._close_ffmpeg()
            path = self._current_out_path
            if path and Path(path).exists():
                size_mb = Path(path).stat().st_size / (1024 * 1024)
                self.log("OK", f"[Seq #{self._seq_segment_count}] Saved {path.name} ({size_mb:.1f} MB)")

        total = self._seq_segment_count
        if total == 0:
            self.log("INFO", "Sequential cancelled - no audio was captured.")
        else:
            self.log("INFO", f"Sequential stopped after {total} segment(s)")

        if self.capture:
            self.capture._audio_cb = self._make_monitor_callback(self.capture.format.channels)

        self.sequential_mode = False
        self.recording_started = None
        self._segment_started = None
        self._current_out_path = None
        self._seq_silence_start = None
        self._seq_waiting = False

        self.record_btn.setEnabled(True)
        self._set_seq_btn(recording=False, text="\u21BB  Sequential")
        self.split_btn.setEnabled(False)
        self._set_status("Ready", "statusReady")
        self.process_combo.setEnabled(True)
        self.include_tree_cb.setEnabled(True)

        self._pulse_stop()               # back to calm green idle
        self._set_tooltips_enabled(True)
        self._recording_just_stopped()

    def _check_sequential_split(self) -> None:
        if not self.sequential_mode or not self.recording_started:
            return

        with self._rms_lock:
            rms = max(self._cur_rms_l, self._cur_rms_r)
        is_silent = rms < SILENCE_THRESHOLD_RMS
        pause_target = float(self.seq_pause_spin.value())

        if is_silent:
            if self._seq_silence_start is None:
                self._seq_silence_start = datetime.now()
            if self.ffmpeg_proc:
                silence_dur = (datetime.now() - self._seq_silence_start).total_seconds()
                if silence_dur >= pause_target:
                    self._seq_finish_segment()
        else:
            self._seq_silence_start = None
            if self._seq_waiting:
                self._seq_begin_segment()

    # ===== Status callback bridge ===================================
    def _on_status_from_dll(self, code: int, msg: str) -> None:
        self.status_event.emit(code, msg)

    def _on_status_event(self, code: int, msg: str) -> None:
        level = "INFO" if code == 0 else f"ERR{code}"
        self.log(level, f"[dll] {msg}")

    # ===== GUI tick handlers ========================================
    def _update_vu(self) -> None:
        with self._rms_lock:
            l, r = self._cur_rms_l, self._cur_rms_r
        self.vu.set_levels(l, r)
        # The spectrogram timer is the same as the VU timer: each tick
        # processes whatever audio chunks the capture thread has queued
        # since last call, computes one FFT column, and scrolls.
        # Cheap when the panel is hidden because feed_chunk() is gated
        # on _spectrogram_visible in the audio callbacks - the deque
        # stays empty and tick() returns immediately.
        if self._spectrogram_visible:
            self.spec.tick()

    def _on_stat_tick(self) -> None:
        self._update_stats()
        self._check_sequential_split()

    def _update_stats(self) -> None:
        if not self.recording_started:
            return

        # HH:MM:SS:cc - centiseconds (10 ms resolution) is the sweet spot
        # for a stopwatch-style display: the 30 Hz timer rate yields
        # ~3 cs steps per tick (smooth motion, no noise), and 2 digits
        # match the classic stopwatch look. We do all arithmetic in
        # integer centiseconds so float-rounding can't make the field
        # jitter between 99 and 00 around the second boundary.
        elapsed = (datetime.now() - self.recording_started).total_seconds()
        total_cs = int(elapsed * 100.0)
        h, rem = divmod(total_cs, 360_000)
        m, rem = divmod(rem,       6_000)
        s, cs  = divmod(rem,         100)
        self.time_label.setText(f"{h:02d}:{m:02d}:{s:02d}:{cs:02d}")

        file_size = 0
        seg_elapsed = 0.0
        if self._segment_started and self._current_out_path:
            seg_elapsed = (datetime.now() - self._segment_started).total_seconds()
            try:
                if self._current_out_path.exists():
                    file_size = self._current_out_path.stat().st_size
            except OSError:
                pass

        mb = file_size / (1024 * 1024)
        self.size_label.setText(f"{mb:.1f} MB")

        if seg_elapsed > 0.2 and file_size > 0:
            rate = file_size / 1024.0 / seg_elapsed
            self.rate_label.setText(f"{rate:.0f} kB/s")
        else:
            self.rate_label.setText("\u2014 kB/s")

    # ===== UI helpers ===============================================
    def _set_record_btn(self, recording: bool, text: str) -> None:
        self.record_btn.setText(text)
        self.record_btn.setProperty("recording", "true" if recording else "false")
        self.record_btn.style().unpolish(self.record_btn)
        self.record_btn.style().polish(self.record_btn)

    def _set_seq_btn(self, recording: bool, text: str) -> None:
        self.seq_btn.setText(text)
        self.seq_btn.setProperty("recording", "true" if recording else "false")
        self.seq_btn.style().unpolish(self.seq_btn)
        self.seq_btn.style().polish(self.seq_btn)

    def _set_status(self, text: str, object_name: str) -> None:
        self.status_label.setText(text)
        self.status_label.setObjectName(object_name)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def _update_format_info(self) -> None:
        """
        Refresh the WASAPI format-info label (sample rate, channel count,
        sample depth, raw data rate). Shown in the space to the right of
        the "Wait for:" checkbox whenever that box is unchecked - so the
        space is always doing something useful instead of sitting empty.
        """
        if not hasattr(self, "format_info"):
            return
        if not self.capture:
            self.format_info.setText("\u2014")
            return
        try:
            fmt = self.capture.format
        except Exception:
            self.format_info.setText("\u2014")
            return

        # Sample rate as kHz with stripped trailing zeros (48 kHz, 44.1 kHz, ...).
        sr_khz = fmt.sample_rate / 1000.0
        if sr_khz == int(sr_khz):
            sr_text = f"{int(sr_khz)} kHz"
        else:
            sr_text = f"{sr_khz:.1f} kHz"

        # Sample depth: anything 32-bit IEEE float is shown as 'float32',
        # otherwise the bit count (16-bit, 24-bit, ...). The DLL currently
        # only ever returns 32-bit float, but if we ever query the real
        # mix format dynamically this label will start showing variations.
        depth = "float32" if fmt.bits_per_sample == 32 else f"{fmt.bits_per_sample}-bit"

        # Raw byte rate going through the pipe (bytes_per_frame * sample_rate).
        data_rate_kbs = (fmt.sample_rate * fmt.bytes_per_frame) / 1024.0

        self.format_info.setText(
            f"{sr_text} \u00B7 {fmt.channels} ch \u00B7 {depth}"
            f"  \u00B7  {data_rate_kbs:,.0f} kB/s"
        )

    def _toggle_meter_view(self) -> None:
        """
        Swap between VU meter (index 0) and Spectrogram (index 1).

        On every transition we clear the spectrogram buffer so the user
        always sees fresh data starting from the click, never a stale
        history from the last time the panel was visible - and so no
        last-frame pixels can linger after the widget is hidden.
        """
        new_idx = (self.meter_stack.currentIndex() + 1) % 2
        # Wipe the spec's pixel buffer on every toggle. Going TO the
        # spectrogram this gives a clean slate; going AWAY it makes
        # sure the hidden widget can't keep a stale top row around if
        # Qt happens to leak any of its last image during the swap.
        self.spec.clear()
        if new_idx == 1:
            # Going to spectrogram - mark as visible so audio callbacks
            # start feeding the FFT queue.
            self._spectrogram_visible = True
            # Hand the spectrogram the current capture sample rate so
            # the log-frequency bin map matches what's actually arriving.
            if self.capture is not None:
                try:
                    self.spec.set_sample_rate(self.capture.format.sample_rate)
                except Exception:
                    pass
            self.log("INFO", "View: Spectrogram (click again for VU meter)")
        else:
            self._spectrogram_visible = False
            self.log("INFO", "View: VU meter (click again for Spectrogram)")
        self.meter_stack.setCurrentIndex(new_idx)

    # ===== Logging ==================================================
    def log(self, level: str, msg: str) -> None:
        self.log_signal.emit(level, msg)

    def _append_log(self, level: str, msg: str) -> None:
        color = {
            "INFO":  C.TEXT_NORMAL,
            "OK":    C.GREEN,
            "WARN":  C.WARNING,
            "ERROR": C.ERROR,
        }.get(level, C.TEXT_NORMAL)
        stamp = datetime.now().strftime("%H:%M:%S")
        html = (f'<span style="color:{C.TEXT_DIM}">{stamp}</span> '
                f'<span style="color:{color}; font-weight:bold">[{level}]</span> '
                f'<span style="color:{C.TEXT_NORMAL}">{msg}</span>')
        self.log_view.append(html)

    # ===== Window geometry persistence ==============================
    # Both events fire many times per second during a drag operation,
    # so we route them through a 600 ms single-shot timer instead of
    # writing AudioOBS.json on every tick. The timer keeps getting
    # postponed while the user drags, and finally fires once the
    # window has been still for 600 ms - one disk write per move.
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._loading_settings:
            self._geometry_save_timer.start()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        if not self._loading_settings:
            self._geometry_save_timer.start()

    # ===== Close handling ===========================================
    def closeEvent(self, ev: QCloseEvent) -> None:
        if self.sequential_mode:
            self._stop_sequential()
        elif self.recording_started:
            self._stop_recording()
        self._stop_capture_silent()
        self._pulse_stop()
        self._save_settings()
        if wca:
            try:
                wca.shutdown()
            except Exception:
                pass
        super().closeEvent(ev)


# =====================================================================
# Entry point
# =====================================================================
def main() -> int:
    # Windows taskbar grouping fix: without an explicit AppUserModelID,
    # Windows uses the Python interpreter's identity for the taskbar
    # icon - so the taskbar shows the Python snake instead of our
    # toucan, even though setWindowIcon() set the right thing for the
    # title bar. Setting our own AUMID tells Windows "I'm a separate
    # app, group me on my own and use my window icon".
    # No-op (silently caught) on non-Windows.
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "WaveboSF.AudioOBS.1"          # CompanyName.ProductName.Version
            )
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setApplicationName("AudioOBS")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(build_stylesheet())

    # Set the app icon up front so the splash flash (if any) and the
    # first paint already use it - belt-and-suspenders with the call
    # inside _build_ui that does the same thing.
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    elif STYLED_LOGO_PATH.exists():
        app.setWindowIcon(QIcon(str(STYLED_LOGO_PATH)))

    w = AudioOBSMainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
