"""Tests for output/input device selection (voice_mode.audio_devices).

The module wraps sounddevice/PortAudio; these tests stub the ``sd`` object it
imported so we can exercise the selection policy and the re-init guard without
real hardware.
"""
import importlib

import pytest


@pytest.fixture
def ad(monkeypatch):
    """Reload audio_devices with a stub sounddevice and a reset stream count."""
    import voice_mode.audio_devices as ad

    importlib.reload(ad)

    class FakeSD:
        def __init__(self):
            self.devices = []
            self.default_input_name = ""
            self.terminate_calls = 0
            self.initialize_calls = 0
            # If set, a re-init swaps this in — simulates a hot-plugged device
            # becoming visible only after PortAudio is re-initialized.
            self.devices_after_refresh = None

        def query_devices(self, kind=None):
            if kind == "input":
                for d in self.devices:
                    if d["name"] == self.default_input_name:
                        return d
                return {"name": self.default_input_name}
            return self.devices

        def _terminate(self):
            self.terminate_calls += 1

        def _initialize(self):
            self.initialize_calls += 1
            if self.devices_after_refresh is not None:
                self.devices = self.devices_after_refresh

    fake = FakeSD()
    monkeypatch.setattr(ad, "sd", fake)
    ad._open_streams = 0
    return ad, fake


def _dev(name, out=0, inp=0):
    return {"name": name, "max_output_channels": out, "max_input_channels": inp}


def _macos_freeclip_layout(fake):
    # macOS exposes a BT headset as two PortAudio devices: an HFP mic (in-only)
    # and an A2DP output (out-only). FreeClip is the default for both.
    fake.devices = [
        _dev("MacBook Pro Microphone", out=0, inp=1),
        _dev("MacBook Pro Speakers", out=2, inp=0),
        _dev("HUAWEI FreeClip 2", out=0, inp=1),
        _dev("HUAWEI FreeClip 2", out=2, inp=0),
    ]
    fake.default_input_name = "HUAWEI FreeClip 2"


def test_output_prefers_connected_device(ad, monkeypatch):
    mod, fake = ad
    _macos_freeclip_layout(fake)
    monkeypatch.setenv("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES", "HUAWEI FreeClip")
    idx, name = mod.resolve_output_device()
    # Must pick the output-capable endpoint (index 3), not the in-only one.
    assert idx == 3
    assert name == "HUAWEI FreeClip 2"
    # Already connected -> no PortAudio churn (would clip Bluetooth playback).
    assert fake.terminate_calls == 0


def test_output_reinits_to_discover_hotplugged_device(ad, monkeypatch):
    mod, fake = ad
    # Preferred device absent from the initial enumeration...
    fake.devices = [
        _dev("MacBook Pro Microphone", out=0, inp=1),
        _dev("MacBook Pro Speakers", out=2, inp=0),
    ]
    # ...but appears after a re-init (as if just paired).
    fake.devices_after_refresh = [
        _dev("MacBook Pro Microphone", out=0, inp=1),
        _dev("MacBook Pro Speakers", out=2, inp=0),
        _dev("HUAWEI FreeClip 2", out=2, inp=0),
    ]
    monkeypatch.setenv("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES", "HUAWEI FreeClip")
    idx, name = mod.resolve_output_device()
    assert name == "HUAWEI FreeClip 2"
    assert fake.terminate_calls == 1  # one re-init to discover it


def test_output_falls_back_to_default_when_absent(ad, monkeypatch):
    mod, fake = ad
    fake.devices = [
        _dev("MacBook Pro Microphone", out=0, inp=1),
        _dev("MacBook Pro Speakers", out=2, inp=0),
    ]
    monkeypatch.setenv("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES", "HUAWEI FreeClip,AirPods")
    assert mod.resolve_output_device() == (None, None)


def test_output_no_pref_uses_default(ad, monkeypatch):
    mod, fake = ad
    _macos_freeclip_layout(fake)
    monkeypatch.delenv("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES", raising=False)
    assert mod.resolve_output_device() == (None, None)


def test_input_excludes_bluetooth_and_falls_back_to_builtin(ad, monkeypatch):
    mod, fake = ad
    _macos_freeclip_layout(fake)
    monkeypatch.setenv("VOICEMODE_STT_EXCLUDED_INPUT_DEVICES", "HUAWEI FreeClip")
    idx, name = mod.resolve_input_device()
    assert idx == 0
    assert name == "MacBook Pro Microphone"
    # Built-in mic was already enumerated -> no re-init churn.
    assert fake.terminate_calls == 0


def test_input_default_acceptable_returns_none(ad, monkeypatch):
    mod, fake = ad
    _macos_freeclip_layout(fake)
    fake.default_input_name = "MacBook Pro Microphone"  # already a good mic
    monkeypatch.setenv("VOICEMODE_STT_EXCLUDED_INPUT_DEVICES", "HUAWEI FreeClip")
    assert mod.resolve_input_device() == (None, None)


def test_input_no_exclusions_uses_default(ad, monkeypatch):
    mod, fake = ad
    _macos_freeclip_layout(fake)
    monkeypatch.delenv("VOICEMODE_STT_EXCLUDED_INPUT_DEVICES", raising=False)
    assert mod.resolve_input_device() == (None, None)


def test_refresh_skipped_while_stream_open(ad, monkeypatch):
    mod, fake = ad
    # Preferred device absent -> resolution WOULD try to re-init to discover it,
    # but a stream is open, so the re-init must be skipped (terminating PortAudio
    # would invalidate the live stream).
    fake.devices = [
        _dev("MacBook Pro Microphone", out=0, inp=1),
        _dev("MacBook Pro Speakers", out=2, inp=0),
    ]
    monkeypatch.setenv("VOICEMODE_TTS_PREFERRED_OUTPUT_DEVICES", "HUAWEI FreeClip")
    mod.stream_open()
    try:
        assert mod.resolve_output_device() == (None, None)
        assert fake.terminate_calls == 0  # never terminate under a live stream
    finally:
        mod.stream_closed()


def test_stream_count_never_negative(ad):
    mod, _ = ad
    mod.stream_closed()
    mod.stream_closed()
    assert mod._open_streams == 0
