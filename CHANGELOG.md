# Changelog

All notable changes to AudioOBS.
This project follows the spirit of [Keep a Changelog](https://keepachangelog.com/).

---

## [1.00] — first public release

Per-process audio capture for Windows: pick one running application and
record only its audio, encoded through `ffmpeg` to FLAC, ALAC, MP3, AAC,
Opus, EAC3 or raw PCM.

### Added — core feature set

- Per-process WASAPI capture via the Windows Process Loopback API,
  with optional child-process inclusion (Chrome audio service,
  Spotify renderer, etc.).
- "Wait for:" mode — arm capture against an application by `.exe`
  name and start recording the moment it opens an audio session.
- Sequential / album mode — automatic track-splitting on detected
  silence, with an adjustable silence-gap spinbox.
- Manual track-split — close the current file and start a new one
  instantly, even when songs blend without a gap.
- Live VU meter — Braille-glyph bars, per-channel peak hold,
  green/yellow/red zoning on a -50 … +4 dB scale.
- Scrolling spectrogram — log-frequency waterfall (20 Hz – 20 kHz),
  magma colour map, separate L/R panels; click the meter to toggle.
- Live WASAPI format readout — sample rate, channel count, sample
  depth and raw data rate of the captured source.
- Settings stored in `AudioOBS.json` next to the executable.

### Added — refinements after first-round feedback

- **Startup ffmpeg check.** On launch AudioOBS locates `ffmpeg`
  (via `PATH` and common install locations). If it is missing, a
  dialog explains what still works without it (source picking, VU
  meter, spectrogram, format readout) and gives step-by-step install
  and `PATH` instructions. The dialog appears at most once per
  program start.
- **Full save-state.** `AudioOBS.json` now persists the complete UI
  state — output folder, codec and bitrate, sequential pause-gap,
  last source, "Wait for:" enabled-state, meter view (VU vs
  spectrogram) and window geometry. Restoring a stored value that is
  no longer valid (a missing output folder, an out-of-range index)
  falls back to a safe default and logs it, instead of blocking
  startup.
- **Empty-source-list hint.** When no application is currently
  playing audio, an inline note explains why the picker is empty —
  an app only appears once it actually plays a sound — and points to
  "Wait for:" as the way to capture an app before it makes a sound.
- **"Wait for:" history.** The "Wait for:" field is now an editable
  combo box. Applications that have been waited for successfully are
  remembered in a most-recently-used drop-down (newest first,
  de-duplicated, capped at 10 entries) and stored in `AudioOBS.json`,
  so they can be re-picked without retyping the `.exe` name.

### Fixed

- Spectrogram: the topmost pixel row could linger for a short while
  after switching back to the VU meter. The spectrogram buffer is
  now cleared on every view toggle.
