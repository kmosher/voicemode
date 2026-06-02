---
description: Install VoiceMode, FFmpeg, and local voice services
allowed-tools: Bash(uvx:*), Bash(voicemode:*), Bash(brew:*), Bash(uname:*), Bash(which:*)
---

# /voicemode:install

Install VoiceMode and all dependencies needed for voice conversations.

## Quick Install (Non-Interactive)

**On Apple Silicon, prefer the unified mlx-audio service** — it provides both STT and TTS in one process, sidesteps the C++ toolchain whisper.cpp needs, and has no separate kokoro process to manage:

```bash
uvx voice-mode-install --yes
voicemode service install mlx-audio
```

Only fall back to the separate whisper + kokoro services if mlx-audio is not viable (non-Apple-Silicon, or user explicitly wants the classic stack):

```bash
voicemode service install whisper
voicemode service install kokoro
```

Before installing, check whether the user already has mlx-audio running locally — many setups already do, and installing whisper/kokoro on top is wasted work.

## What Gets Installed

| Component | Size | Purpose |
|-----------|------|---------|
| FFmpeg | ~50MB | Audio processing (via Homebrew) |
| VoiceMode CLI | ~10MB | Command-line tools |
| mlx-audio (Apple Silicon, recommended) | ~0MB binary; pulls models lazily on first use | Unified STT + TTS on port 8890 |
| Whisper (base) | ~150MB | Classic local STT (port 2022) — only if not using mlx-audio |
| Kokoro | ~350MB | Classic local TTS (port 8880) — only if not using mlx-audio |

## Implementation

1. **Check architecture:** `uname -m` (arm64 = Apple Silicon, recommended for local services)

2. **Check what's already installed:**
   ```bash
   which voicemode  # VoiceMode CLI
   which ffmpeg     # Audio processing
   ```

3. **Install missing components:**
   ```bash
   # Full install (installs ffmpeg, voicemode, and checks dependencies)
   uvx voice-mode-install --yes

   # Apple Silicon: unified service (preferred)
   voicemode service install mlx-audio

   # Classic stack — only if mlx-audio isn't viable:
   #   voicemode service install whisper
   #   voicemode service install kokoro
   ```

4. **Verify services are running:**
   ```bash
   voicemode service status mlx-audio   # or: whisper / kokoro if installed
   ```

5. **Reconnect MCP server:**
   After installation, the VoiceMode MCP server needs to reconnect:
   - Run `/mcp` and select voicemode, then click "Reconnect", OR
   - Restart Claude Code

## Whisper Model Selection

For Apple Silicon Macs with 16GB+ RAM, the large-v2 model is recommended:

| Model | Download | RAM Usage | Accuracy |
|-------|----------|-----------|----------|
| base | ~150MB | ~300MB | Good (default) |
| small | ~460MB | ~1GB | Better |
| large-v2 | ~3GB | ~5GB | Best (recommended for 16GB+ RAM) |
| large-v3-turbo | ~1.5GB | ~3GB | Fast & accurate |

To install the recommended model:
```bash
voicemode whisper install --model large-v2
```

## Prerequisites

This install process assumes:
- **UV** - Python package manager (install: `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Homebrew** - macOS package manager (install: `brew.sh`)

The VoiceMode installer will install Homebrew if missing on macOS.

For complete documentation, load the `voicemode` skill.
