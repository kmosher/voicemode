"""Output/input device selection for voicemode playback and recording.

Why this exists: sounddevice (PortAudio) snapshots the device list *and* the
default device when PortAudio initializes, and voicemode runs as a long-lived
MCP server. So a device paired — or made the system default — *after* the
server started is invisible to playback/recording until PortAudio is
re-initialized. The symptom is audio that keeps going to the device that was
default at startup (e.g. built-in speakers) even after you switch outputs.

This module re-initializes PortAudio *lazily* — only when the device a policy
asks for isn't already in the current enumeration (i.e. it may have been
hot-plugged) — so it can be discovered. It deliberately does NOT re-init on the
common already-connected path: terminating PortAudio mid-session can clip the
start of Bluetooth playback as the A2DP device re-wakes. It applies two
name-based policies:

  * Output (``resolve_output_device``): prefer the first connected device whose
    name matches one of ``VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES`` (comma-
    separated substrings, in priority order); fall back to the system default
    when none of them are connected. Lets you say "use my earbuds when they're
    on, otherwise whatever the system default is."

  * Input (``resolve_input_device``): never use a device whose name matches
    ``VOICEMODE_STT_EXCLUDED_INPUT_DEVICES`` — typically a Bluetooth headset,
    whose hands-free (HFP) mic is low quality and also forces the *output* into
    narrowband mode. When the current default input is excluded, fall back to a
    built-in mic (or the first non-excluded input).

Both lists are case-insensitive substrings. Empty (the package default) means
"use the system default", so the behavior is opt-in via config.

Re-initializing PortAudio invalidates any open stream, so it is only done while
no stream is open. Call sites bracket a stream's lifetime with
``stream_open()`` / ``stream_closed()`` (or the ``active_stream`` context
manager) so a concurrent resolution can't terminate PortAudio out from under a
live stream. All of this is serialized by a single re-entrant lock.
"""

import logging
import os
import threading
from contextlib import contextmanager
from typing import List, Optional, Tuple

import sounddevice as sd

logger = logging.getLogger("voicemode")

# Guards PortAudio re-init and the open-stream count together: re-init must not
# run while any stream is open, and the count must not change mid-decision.
_lock = threading.RLock()
_open_streams = 0


def _csv_env(name: str) -> List[str]:
    """Parse a comma-separated env var into a list of non-empty trimmed values."""
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


def _refresh_if_idle() -> None:
    """Re-init PortAudio so newly (dis)connected devices and the current default
    are seen. No-op while a stream is open — terminating PortAudio then would
    invalidate the live stream. Caller must hold ``_lock``."""
    if _open_streams > 0:
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:  # pragma: no cover - platform/driver dependent
        logger.warning("voicemode: PortAudio re-init failed (%s); using cached devices", e)


def stream_open() -> None:
    """Mark a PortAudio stream as open so device refresh won't terminate it."""
    global _open_streams
    with _lock:
        _open_streams += 1


def stream_closed() -> None:
    """Mark a previously-opened stream as closed."""
    global _open_streams
    with _lock:
        _open_streams = max(0, _open_streams - 1)


@contextmanager
def active_stream():
    """Context manager form of ``stream_open``/``stream_closed``."""
    stream_open()
    try:
        yield
    finally:
        stream_closed()


def _name(dev) -> str:
    return str(dev.get("name", ""))


def _find_output(devices, prefs) -> Tuple[Optional[int], Optional[str]]:
    for sub in prefs:
        low = sub.lower()
        for idx, dev in enumerate(devices):
            if dev.get("max_output_channels", 0) > 0 and low in _name(dev).lower():
                return idx, _name(dev)
    return None, None


def resolve_output_device() -> Tuple[Optional[int], Optional[str]]:
    """Return ``(index, name)`` for playback, or ``(None, None)`` to use the
    system default. Prefers a connected device matching
    ``VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES``."""
    prefs = _csv_env("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES")
    if not prefs:
        return None, None
    with _lock:
        idx, name = _find_output(sd.query_devices(), prefs)
        if idx is None:
            # Preferred device isn't in the current enumeration; it may have been
            # hot-plugged. Re-init once (idle only) to discover it, then retry.
            _refresh_if_idle()
            idx, name = _find_output(sd.query_devices(), prefs)
        if idx is not None:
            logger.debug("voicemode: output device -> [%d] %s", idx, name)
            return idx, name
        return None, None


def _pick_input(devices, excl) -> Tuple[Optional[int], Optional[str]]:
    def allowed(dev) -> bool:
        return dev.get("max_input_channels", 0) > 0 and not any(
            s in _name(dev).lower() for s in excl
        )

    # Prefer an obvious built-in microphone.
    for hint in ("macbook", "built-in", "built in", "internal"):
        for idx, dev in enumerate(devices):
            if allowed(dev) and hint in _name(dev).lower():
                return idx, _name(dev)
    # Otherwise the first non-excluded input.
    for idx, dev in enumerate(devices):
        if allowed(dev):
            return idx, _name(dev)
    return None, None


def resolve_input_device() -> Tuple[Optional[int], Optional[str]]:
    """Return ``(index, name)`` for recording, or ``(None, None)`` to use the
    system default. Avoids devices matching
    ``VOICEMODE_STT_EXCLUDED_INPUT_DEVICES`` (e.g. a Bluetooth headset mic),
    falling back to a built-in mic / the first non-excluded input."""
    excl = [s.lower() for s in _csv_env("VOICEMODE_STT_EXCLUDED_INPUT_DEVICES")]
    if not excl:
        return None, None
    with _lock:
        try:
            default_name = _name(sd.query_devices(kind="input"))
        except Exception:
            default_name = ""
        if default_name and not any(s in default_name.lower() for s in excl):
            return None, None  # current default is acceptable
        idx, name = _pick_input(sd.query_devices(), excl)
        if idx is None:
            # No acceptable input in the current enumeration; re-init once (idle
            # only) in case a usable mic was hot-plugged, then retry.
            _refresh_if_idle()
            idx, name = _pick_input(sd.query_devices(), excl)
        if idx is not None:
            logger.debug("voicemode: input device -> [%d] %s", idx, name)
            return idx, name
        # Nothing better available; let the default ride rather than fail.
        logger.warning(
            "voicemode: default input %r is excluded but no alternative input found; using default",
            default_name,
        )
        return None, None
