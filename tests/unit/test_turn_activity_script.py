"""`scripts/turn_activity.py` の表示: 活動記録の数字だけを出し、記録の無いターンはそう明示する。"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "turn_activity", Path(__file__).resolve().parents[2] / "scripts" / "turn_activity.py")
TA = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(TA)


def test_format_turn_shows_numbers_per_agent_and_marks_missing_activity():
    row = {
        "id": 42, "created_at": "2026-09-23 18:32", "stop_kind": "completed",
        "codex_error_code": "context_window_exceeded", "duration_ms": 1601800,
        "investigation": {"complete": False, "continuations": 0, "counts": {"source_confirmed": 3}},
        "activity": {
            "v": 1, "source": "codex_rollout", "app_version": "0.13.1+abcd1234",
            "settings": {"provider": "codex", "mode": "standard", "model": "gpt-5.4", "depth": "standard"},
            "phases_ms": {"prepare": 12000, "agent": 1580000, "post": 9800, "total": 1601800},
            "agents": [
                {"role": "parent", "model": "gpt-5.4",
                 "tokens": {"input_tokens": 3210000, "cached_input_tokens": 2900000,
                            "output_tokens": 8000, "reasoning_output_tokens": 2000},
                 "rounds": [[100000, 90000, 10, 0], [240000, 200000, 20, 5]],
                 "compactions": [2],
                 "tools": {"ripgrep_search": {"calls": 20, "bytes": 389120, "max_bytes": 49152,
                                              "clipped": 5, "truncated": 0, "errors": 0, "ms": 1234567},
                           "web_search": {"calls": 1}},
                 "unparsed": {}},
                {"role": "child", "model": "gpt-5.4", "tokens": {"input_tokens": 500},
                 "rounds": [], "compactions": [], "tools": {}, "unparsed": {"invalid_type": 1}},
            ],
        },
    }
    text = "\n".join(TA.format_turn(row))
    assert "context_window_exceeded" in text and "1,602s" in text
    assert "provider=codex mode=standard" in text   # mode（素の Codex モード）も設定行に出る
    assert "[本体 gpt-5.4] 入力 3,210,000（キャッシュ 2,900,000）" in text
    assert "往復 2 最大入力 240,000 圧縮 1回 @2" in text
    assert "ripgrep_search 20回 380KiB 最大48KiB 計1,235s 切詰5" in text   # 所要（MCP の実測）も出す
    assert "web_search 1回 -" in text          # 大きさを測れないツールは数字を捏造しない
    assert "[下調べ役1 gpt-5.4] 入力 500" in text and "未解析: invalid_type=1" in text
    assert "台帳: 完了=False 継続=0 終端: source_confirmed=3" in text

    # 識別子の形でない文字列（資料名に見えるもの・日本語・空白入りのエラー）は中身を出さずにまとめる
    odd = dict(row, codex_error_code="bad code with spaces")
    odd["activity"] = dict(row["activity"], agents=[dict(row["activity"]["agents"][0], tools={
        "SAMPLE01.c": {"calls": 2, "bytes": 1024}, "資料名": {"calls": 1, "bytes": 2048}},
        unparsed={"event_msg:新種": 3})])
    odd_text = "\n".join(TA.format_turn(odd))
    assert "SAMPLE01" not in odd_text and "資料名" not in odd_text and "新種" not in odd_text
    assert "bad code" not in odd_text and "エラー: （その他）" in odd_text
    assert "（その他） 3回 3KiB" in odd_text and "未解析: （その他）=3" in odd_text

    missing = "\n".join(TA.format_turn({"id": 7, "created_at": "x", "activity": None}))
    assert "活動記録なし" in missing
    api_only = "\n".join(TA.format_turn({"id": 8, "created_at": "x", "activity": {
        "v": 1, "source": "none", "app_version": "0.13.1+abcd1234", "phases_ms": {"total": 5000}}}))
    assert "Codex の詳しい記録なし" in api_only and "版=0.13.1+abcd1234" in api_only
    assert "本体" not in api_only
    # 版の形が想定と違っても「（その他）」にせず、英数字と . + _ - 以外を ? にして出す
    odd_ver = "\n".join(TA.format_turn({"id": 9, "created_at": "x", "activity": {
        "v": 1, "source": "none", "app_version": "\ufeff0.13.1-rc1 資料", "phases_ms": {"total": 1}}}))
    assert "版=?0.13.1-rc1???" in odd_ver and "資料" not in odd_ver
