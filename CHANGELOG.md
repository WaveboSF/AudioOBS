# Changelog

All notable changes to AudioOBS.
This project follows the spirit of [Keep a Changelog](https://keepachangelog.com/).

---

## [1.00] — fixes (unreleased)

Bug fixes and documentation for 1.00. No new features, no version
change.

### Fixed

- Startup no longer crashes when `wincaptureaudio.dll` cannot be
  loaded; the GUI shows the "DLL not loaded" state and logs the reason.
- ffmpeg's error output is read continuously. Before, a chatty ffmpeg
  could fill the pipe, stop reading audio and stall the capture.
- A segment that cannot be opened (e.g. ffmpeg missing) now stops
  Sequential with one error, instead of retrying on every tick.
- Any ffmpeg start error is reported instead of escaping as an
  unhandled exception; a killed ffmpeg is waited for.
- The source list is no longer rebuilt while recording, so the
  selection (which names segment and split files) cannot jump to
  another app.
- Two recordings started within the same second no longer overwrite
  each other (a counter is appended to the file name).
- The VU meter keeps working when starting a recording fails after a
  new capture was opened.
- Log messages are escaped, so text containing `<` or `&` is shown
  correctly.
- `wincaptureaudio`: session enumeration retries when a session
  appears between size query and fill (`BUFFER_TOO_SMALL`); a capture
  is stopped if reading its format fails right after start.

### Documentation

- README matched to the actual repository and code: flat file layout,
  download of the split ZIP, ffmpeg search locations, output file
  names, `AudioOBS.json` keys, troubleshooting. Removed references to
  build scripts and C++ sources that are not in this repository.
- VU meter description corrected: 0 dBFS-anchored scale with a
  -40 … -90 dB floor depending on window width (the "-50 … +4 dB"
  scale mentioned below is outdated).

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
