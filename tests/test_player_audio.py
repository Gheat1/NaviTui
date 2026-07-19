"""Stdlib-only tests for the equalizer + audio-device logic on Player.

The repo ships no test framework, so this is a self-contained runner:
`python tests/test_player_audio.py`. It never touches a real mpv — it builds a
Player without running __init__ and drives the methods against a fake mpv core
that records `af` commands and answers the device/driver properties.
"""

from __future__ import annotations

from navitui import player as playermod
from navitui.player import Player, NullPlayer


class FakeMPV:
    def __init__(self, devices=None, ao=None, audio_device="auto"):
        self.af_calls: list[tuple] = []
        self.audio_device_list = devices or []
        self.ao = ao
        self.audio_device = audio_device

    def command(self, *args):
        if args and args[0] == "af":
            self.af_calls.append(tuple(args[1:]))


def _player_with(mpv: FakeMPV) -> Player:
    p = Player.__new__(Player)  # skip __init__ (no real mpv)
    p._m = mpv
    return p


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(f"FAILED: {name}")
    print(f"  ok: {name}")


def test_equalizer_uses_labeled_filter() -> None:
    mpv = FakeMPV()
    p = _player_with(mpv)
    p.set_equalizer([3.0, 0.0, -2.0, 0, 0, 0, 0, 0, 0, 6.0])
    # first a defensive remove of any prior @eq, then a single labeled add
    check("removes @eq before adding", ("remove", "@eq") in mpv.af_calls)
    adds = [c for c in mpv.af_calls if c[0] == "add"]
    check("issues exactly one add", len(adds) == 1)
    spec = adds[0][1]
    check("add is labeled @eq (not a bare chain that would clobber @nav)",
          spec.startswith("@eq:lavfi=["))
    check("covers the lowest band 31Hz", "equalizer=f=31:" in spec)
    check("covers the highest band 16kHz", "equalizer=f=16000:" in spec)
    check("carries the gains", "g=3.0" in spec and "g=6.0" in spec)


def test_equalizer_clear_removes_only() -> None:
    mpv = FakeMPV()
    p = _player_with(mpv)
    p.set_equalizer([0.0] * 10)  # all-zero → clear
    check("all-zero gains issue no add", not any(c[0] == "add" for c in mpv.af_calls))
    check("all-zero gains still remove @eq", ("remove", "@eq") in mpv.af_calls)

    mpv2 = FakeMPV()
    _player_with(mpv2).set_equalizer([])  # empty → clear
    check("empty gains issue no add", not any(c[0] == "add" for c in mpv2.af_calls))


def test_get_audio_devices_filters_and_dedupes() -> None:
    devices = [
        {"name": "auto", "description": "Autoselect"},
        {"name": "pulse/alsa_output.usb-DAC", "description": "USB DAC"},
        {"name": "pulse/alsa_output.usb-DAC.2", "description": "USB DAC"},  # dup desc
        {"name": "alsa/hw:0,0", "description": "Built-in"},
        {"name": "alsa/surround51", "description": "Generic surround"},  # dropped
        {"name": "jack/something", "description": "JACK"},  # not a kept backend
    ]
    p = _player_with(FakeMPV(devices=devices))
    got = p.get_audio_devices()
    names = [d["name"] for d in got]
    check("keeps auto", "auto" in names)
    check("keeps pulse device", "pulse/alsa_output.usb-DAC" in names)
    check("de-dupes by description", names.count("pulse/alsa_output.usb-DAC.2") == 0)
    check("keeps alsa/hw:", "alsa/hw:0,0" in names)
    check("drops generic alsa/", "alsa/surround51" not in names)
    check("drops unknown backend", "jack/something" not in names)


def test_set_audio_device_normalizes_prefix() -> None:
    # saved under pipewire, but we're now running under pulse → rewrite prefix
    mpv = FakeMPV(ao="pulse")
    p = _player_with(mpv)
    p.set_audio_device("pipewire/alsa_output.usb-DAC")
    check("prefix rewritten to live driver", mpv.audio_device == "pulse/alsa_output.usb-DAC")

    # matching prefix is left alone
    mpv2 = FakeMPV(ao="pulse")
    _player_with(mpv2).set_audio_device("pulse/alsa_output.usb-DAC")
    check("matching prefix untouched", mpv2.audio_device == "pulse/alsa_output.usb-DAC")

    # ao as a list of dicts (mpv sometimes reports it that way)
    mpv3 = FakeMPV(ao=[{"name": "pipewire"}])
    _player_with(mpv3).set_audio_device("pulse/dev")
    check("handles ao reported as a list", mpv3.audio_device == "pipewire/dev")


def test_nullplayer_stubs_are_safe() -> None:
    n = NullPlayer()
    n.set_equalizer([1.0] * 10)  # must not raise
    check("NullPlayer.set_equalizer is a no-op", True)
    check("NullPlayer.get_audio_devices returns []", n.get_audio_devices() == [])
    check("NullPlayer.get_current_audio_device", n.get_current_audio_device() == "auto")
    n.set_audio_device("anything")
    check("NullPlayer.set_audio_device is a no-op", True)


def main() -> None:
    print("test_player_audio:")
    test_equalizer_uses_labeled_filter()
    test_equalizer_clear_removes_only()
    test_get_audio_devices_filters_and_dedupes()
    test_set_audio_device_normalizes_prefix()
    test_nullplayer_stubs_are_safe()
    print("all player_audio tests passed")


if __name__ == "__main__":
    main()
