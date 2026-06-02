"""Voice-driven Claude Code permission prompts.

When Claude wants to run a tool that needs approval, the PermissionRequest
hook normally surfaces a TTY dialog. This module lets that dialog be answered
by voice: announce the request through TTS, capture a yes/no via STT, and
emit the decision JSON Claude expects.

Activation is scoped by $CLAUDE_CODE_TMPDIR. Each Claude Code session gets a
unique tmpdir that's inherited by both the MCP server (where converse() runs)
and hook subprocesses, so a sentinel file written there is naturally visible
only to the Claude that wrote it. Filesystem isolation enforces the scoping
that the user asked for, without needing to plumb session_id through MCP.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("voicemode.permission_hook")

SENTINEL_NAME = "voicemode-active"
DEFAULT_STALE_AFTER_S = 600.0
DEFAULT_LISTEN_MAX_S = 15.0

# Match standalone tokens so "I know" isn't read as affirmative.
_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|allow|approve|approved|ok|okay|"
    r"go\s+ahead|do\s+it|please|fine|sounds\s+good)\b",
    re.I,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|nah|deny|denied|don'?t|stop|cancel|reject|abort|negative|never)\b",
    re.I,
)


def _sentinel_path() -> Optional[Path]:
    tmpdir = os.environ.get("CLAUDE_CODE_TMPDIR")
    if not tmpdir:
        return None
    return Path(tmpdir) / SENTINEL_NAME


def mark_voice_active(session_hint: Optional[str] = None) -> None:
    """Touch the sentinel so the permission hook prompts by voice.

    Called from converse() on entry. The file's mtime tracks recency; a stale
    sentinel (older than VOICEMODE_PERMISSION_STALE_SECONDS) is treated as
    inactive so a Claude that hasn't spoken in a while falls back to the TTY
    prompt instead of nagging by voice.
    """
    p = _sentinel_path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(session_hint or "")
    except OSError as e:
        logger.debug(f"could not write voice-active sentinel {p}: {e}")


def _sentinel_is_active(stale_after_s: float) -> bool:
    p = _sentinel_path()
    if not p or not p.exists():
        return False
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        return False
    return age <= stale_after_s


def _summarize_tool_call(tool_name: str, tool_input: dict) -> str:
    """Short, speakable description of what Claude wants to do."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + " — truncated"
        return f"run shell command: {cmd}"
    if tool_name in ("Edit", "Write"):
        return f"{tool_name.lower()} the file {tool_input.get('file_path', 'unknown path')}"
    if tool_name == "WebFetch":
        return f"fetch the URL {tool_input.get('url', 'unknown')}"
    if tool_name == "Agent":
        return f"launch a sub-agent: {tool_input.get('description', tool_input.get('subagent_type', '?'))}"
    keys = ", ".join(sorted(tool_input.keys())) or "no arguments"
    return f"call the {tool_name} tool with {keys}"


def _classify(transcript: str) -> Optional[str]:
    """Map a transcription to 'allow' | 'deny' | None (no clear answer)."""
    if not transcript:
        return None
    neg = _NEGATIVE.search(transcript)
    aff = _AFFIRMATIVE.search(transcript)
    # If both appear ("no wait, yes"), trust the later one.
    if neg and aff:
        return "allow" if aff.start() > neg.start() else "deny"
    if neg:
        return "deny"
    if aff:
        return "allow"
    return None


async def _ask_voice(question: str, listen_max: float) -> Optional[str]:
    # Lazy imports — pulling in sounddevice/numpy/etc. is slow, and we don't
    # want to pay that cost on every hook invocation when the sentinel is
    # missing (i.e. voice mode isn't engaged).
    from voice_mode.tools.converse import (
        text_to_speech_with_failover,
        record_audio_with_silence_detection,
        speech_to_text,
    )

    ok, _metrics, _cfg = await text_to_speech_with_failover(message=question)
    if not ok:
        logger.warning("TTS failed; cannot ask permission by voice")
        return None

    audio_data, speech_detected = await asyncio.to_thread(
        record_audio_with_silence_detection,
        listen_max,
        False,
        0.5,
    )
    if not speech_detected:
        logger.info("no speech detected during permission prompt")
        return None

    result = await speech_to_text(audio_data, save_audio=False)
    if not result or not result.get("text"):
        return None
    transcript = result["text"].strip()
    logger.info(f"permission answer transcript: {transcript!r}")
    return _classify(transcript)


def _emit(decision: str, rule: Optional[str] = None) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }
    if rule and decision == "allow":
        out["hookSpecificOutput"]["permissionRule"] = rule
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _fall_through() -> None:
    """Exit 0 with no output — Claude then shows its normal TTY prompt."""
    sys.exit(0)


async def handle_permission_request_async() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        logger.debug(f"could not parse hook stdin as JSON: {e}")
        _fall_through()
        return

    stale_after_s = float(
        os.environ.get("VOICEMODE_PERMISSION_STALE_SECONDS", DEFAULT_STALE_AFTER_S)
    )
    if not _sentinel_is_active(stale_after_s):
        _fall_through()
        return

    tool_name = payload.get("tool_name", "an unknown tool")
    tool_input = payload.get("tool_input") or {}
    listen_max = float(
        os.environ.get("VOICEMODE_PERMISSION_LISTEN_SECONDS", DEFAULT_LISTEN_MAX_S)
    )

    summary = _summarize_tool_call(tool_name, tool_input)
    question = f"Claude is asking permission to {summary}. Say yes or no."

    try:
        decision = await _ask_voice(question, listen_max=listen_max)
    except Exception as e:
        logger.warning(f"voice permission flow failed: {e}", exc_info=True)
        _fall_through()
        return

    if decision in ("allow", "deny"):
        _emit(decision)
    else:
        _fall_through()


def handle_permission_request() -> None:
    """Sync entry point for the CLI subcommand."""
    asyncio.run(handle_permission_request_async())
