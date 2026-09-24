"""`get_settings` の回答方針（system_prompt）: DB に永続化された旧既定文は読取時に現行既定へ読み替える
（DB 不要・偽 psycopg 接続）。

実害（RV 2026-09-10 #1）: 旧版で「既定に戻す」や無編集保存をすると旧既定文が行として残り、既定を
変えても旧文言がプロンプトへ前置され続ける。独自文と空文字（ユーザが消した）はそのまま返す。
"""
from __future__ import annotations

import pytest

from sherpa.store import settings as store_settings

pytestmark = pytest.mark.unit

_LEGACY = next(iter(store_settings._LEGACY_DEFAULT_SYSTEM_PROMPTS))


class _FakeConn:
    def __init__(self, system_prompt):
        self._system_prompt = system_prompt

    def execute(self, sql, params=None):
        self._last = sql
        return self

    def fetchone(self):
        if "FROM user_settings" in self._last:
            return {"system_prompt": self._system_prompt}
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _get(monkeypatch, stored):
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", lambda **kw: _FakeConn(stored))
    return store_settings.get_settings("u1")["system_prompt"]


def test_stored_legacy_default_reads_as_current_default(monkeypatch):
    assert _get(monkeypatch, _LEGACY) == store_settings.DEFAULT_SYSTEM_PROMPT


def test_null_reads_as_current_default(monkeypatch):
    assert _get(monkeypatch, None) == store_settings.DEFAULT_SYSTEM_PROMPT


@pytest.mark.parametrize("stored", ["", "私の独自方針", _LEGACY + " 追記"])
def test_custom_and_empty_are_kept(monkeypatch, stored):
    assert _get(monkeypatch, stored) == stored


class _CaptureConn(_FakeConn):
    def __init__(self, system_prompt):
        super().__init__(system_prompt)
        self.params = []

    def execute(self, sql, params=None):
        self._last = sql
        if sql.lstrip().startswith("INSERT INTO user_settings"):
            self.params.append(params)
        return self


@pytest.mark.parametrize("sent", [store_settings.DEFAULT_SYSTEM_PROMPT, _LEGACY])
def test_update_settings_stores_default_text_as_null(monkeypatch, sent):
    """既定文（現行・旧）と同文の保存は NULL＝既定に追随として書く（焼き付けない）。"""
    conn = _CaptureConn(None)
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", lambda **kw: conn)
    store_settings.update_settings("u1", system_prompt=sent)
    assert conn.params and conn.params[-1][-1] is None


def test_update_settings_without_system_prompt_does_not_burn_in_default(monkeypatch):
    """回答方針に触れない保存（頭脳バッジの codex_model だけの PUT 等）でも既定文を行へ書かない。"""
    conn = _CaptureConn(None)
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", lambda **kw: conn)
    store_settings.update_settings("u1", codex_model="x")
    assert conn.params and conn.params[-1][-1] is None


def test_update_settings_keeps_custom_text(monkeypatch):
    conn = _CaptureConn(None)
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", lambda **kw: conn)
    store_settings.update_settings("u1", system_prompt="私の独自方針")
    assert conn.params[-1][-1] == "私の独自方針"
