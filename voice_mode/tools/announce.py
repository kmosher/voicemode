"""Fire-and-forget voice announcement tool.

Designed for the "agent finished a task, tell the user out loud" pattern, not
for two-way conversation. Returns control to the caller the moment the request
is scheduled — synthesis and playback run in a detached background task so the
caller can keep working (or, in Claude's case, return control to the user for
the next prompt) without waiting for ~5-15s of audio.

Voice resolution: any string passed as `voice` that isn't already a recognized
Kokoro voice ID (af_*/am_*/bf_*/bm_*) or blended ID (claude_*/blend_*) is
SHA-256-hashed into a stable `claude_<hex>` ID. This lets each Claude session
pass its own session_id and get a deterministic unique voice without any other
coordination.
"""

import asyncio
import hashlib
import logging
import os
import re
import traceback
from typing import Optional, Set

from voice_mode.server import mcp
from voice_mode.tools.converse import text_to_speech_with_failover

logger = logging.getLogger("voicemode")


def _default_speed() -> float:
    """Resting playback speed for announcements when the caller doesn't pass one.

    Announcements are notifications meant to deliver information quickly, so the
    default is slightly faster than natural (1.3x) — the F5 backend renders this
    cleanly via its wired `speed` lever. Override with VOICEMODE_ANNOUNCE_SPEED;
    an explicit `speed=` argument to announce() always wins over both.
    """
    raw = os.environ.get("VOICEMODE_ANNOUNCE_SPEED", "1.3")
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid VOICEMODE_ANNOUNCE_SPEED=%r; falling back to 1.3", raw)
        return 1.3

# Hold strong references to in-flight announcement tasks so the event loop
# doesn't garbage-collect them mid-flight. Tasks remove themselves on
# completion via the done-callback.
_pending: Set[asyncio.Task] = set()

# Voice IDs we treat as "already resolved" and pass through unmodified.
# Anything else gets hashed.
_PASSTHROUGH_VOICE_RE = re.compile(r"^(?:[abfm][fm]_|claude_|blend_)")


def _resolve_voice(voice: Optional[str]) -> Optional[str]:
    """Map an arbitrary caller-supplied string to a stable voice ID.

    - None → None (caller's TTS config picks default).
    - Recognizable Kokoro voice (e.g. `af_heart`) or blended ID
      (`claude_<hex>`, `blend_<hex>`) → returned as-is.
    - Anything else (e.g. a Claude session ID, a UUID, an arbitrary seed) →
      hashed into `claude_<hex8>`. Same seed always produces the same ID.
    """
    if not voice:
        return None
    voice = voice.strip()
    if not voice:
        return None
    if _PASSTHROUGH_VOICE_RE.match(voice):
        return voice
    digest = hashlib.sha256(voice.encode("utf-8")).hexdigest()[:16]
    return f"claude_{digest}"


async def _speak_in_background(message: str, voice: Optional[str], speed: Optional[float]) -> None:
    """Run TTS + playback. Log errors; never raise back to the event loop."""
    try:
        success, _metrics, _config = await text_to_speech_with_failover(
            message=message,
            voice=voice,
            speed=speed,
        )
        if not success:
            logger.warning("voicemode:announce TTS failed (no exception raised)")
    except Exception:
        logger.warning(
            "voicemode:announce background task raised:\n%s", traceback.format_exc()
        )


@mcp.tool()
async def announce(
    message: str,
    voice: Optional[str] = None,
    speed: Optional[float] = None,
    wait: bool = False,
) -> str:
    """Fire-and-forget voice announcement. Returns immediately; speech plays in background.

    Designed for "task complete, tell the user" notifications — especially
    useful when multiple Claude sessions run in parallel and each needs to
    surface its own completions without all sounding identical or blocking
    on each other.

    KEY DIFFERENCES FROM voicemode:converse:
    • Returns in <100ms, before synthesis or playback begin. No blocking
      on the ~5-15s TTS pipeline.
    • One-way only: no microphone, no transcription, no conch dance, no
      response captured.
    • Errors are logged but not returned. By the time TTS could fail, the
      caller has already moved on; surfacing the failure has no upside.

    KEY PARAMETERS:
    • message (required): What to speak.
    • voice (optional): Any string. If it's already a known voice ID
      (af_heart, am_michael, claude_<hex>, blend_<hex>, etc.) it's used
      verbatim. Otherwise it's SHA-256-hashed into a stable `claude_<hex>`
      ID — pass your session ID or any other stable seed and you'll get a
      deterministic unique voice. Omit to use the default.
    • speed (optional): 0.25-4.0, where 1.0 is normal. Defaults to 1.3
      (slightly fast — announcements are meant to be heard quickly);
      override the default with VOICEMODE_ANNOUNCE_SPEED.
    • wait (optional, default False): if True, do NOT return until synthesis
      AND playback have finished — turning this into a blocking call. The usual
      fire-and-forget behavior (return in <100ms) is the default; set wait=True
      when the caller needs a reliable "playback done" signal, e.g. to play a
      sequence of clips back-to-back without overlap (handy for A/B debugging).

    WHEN TO USE THIS vs converse:
    • USE announce: end-of-task summaries, "build is done", "PR merged",
      "agent X finished its work" — anything where the user shouldn't wait
      for audio to finish before continuing.
    • USE converse: two-way conversation, when you want the user's spoken
      response captured, multi-agent coordination via the conch.

    Returns the resolved voice ID and a confirmation that the announcement
    was scheduled. The string format is intentionally short to avoid
    polluting the caller's context with tool-result chrome.
    """
    if not message or not message.strip():
        return "✗ announce: empty message, nothing scheduled"

    resolved_voice = _resolve_voice(voice)
    voice_note = f" voice={resolved_voice}" if resolved_voice else ""

    # Apply the slightly-fast announcement default when the caller didn't
    # specify a speed. An explicit speed (including 1.0) always wins.
    if speed is None:
        speed = _default_speed()

    if wait:
        # Blocking mode: await the full TTS + playback pipeline so the caller
        # gets a real completion signal. _speak_in_background swallows errors,
        # so this resolves once audio has actually finished playing.
        await _speak_in_background(message, resolved_voice, speed)
        return f"✓ done{voice_note}"

    task = asyncio.create_task(
        _speak_in_background(message, resolved_voice, speed)
    )
    _pending.add(task)
    task.add_done_callback(_pending.discard)

    return f"✓ scheduled{voice_note}"
