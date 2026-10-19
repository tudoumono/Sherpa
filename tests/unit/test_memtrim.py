"""`sherpa/memtrim.py`: Linux（glibc）だけが `malloc_trim` を呼び、macOS では何もしない。連続の呼び出しは間引く。"""
from __future__ import annotations

from sherpa import memtrim


class _FakeLibc:
    def __init__(self):
        self.calls = []

    def malloc_trim(self, n):
        self.calls.append(n)
        return 1


def _reset(monkeypatch, platform, libc):
    monkeypatch.setattr(memtrim.sys, "platform", platform)
    monkeypatch.setattr(memtrim, "_libc", libc)
    monkeypatch.setattr(memtrim, "_libc_loaded", True)
    monkeypatch.setattr(memtrim, "_last", 0.0)


def test_release_trims_on_linux_and_is_throttled(monkeypatch):
    libc = _FakeLibc()
    _reset(monkeypatch, "linux", libc)
    assert memtrim.release() is True and libc.calls == [0]
    assert memtrim.release() is False and libc.calls == [0]      # 直後の呼び出しは見送る
    assert memtrim.release(force=True) is True and libc.calls == [0, 0]


def test_release_does_nothing_on_macos(monkeypatch):
    libc = _FakeLibc()
    _reset(monkeypatch, "darwin", libc)
    assert memtrim.release() is False and libc.calls == []
