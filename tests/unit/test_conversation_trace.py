"""`scripts/conversation_trace.py`（make trace）: 段と道具の時刻順・ヒット件数・トークンの増分、伏せ字の自己検査、
セッション記録が無いときの欠落の明示。値はすべて架空。"""
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "conversation_trace", Path(__file__).resolve().parents[2] / "scripts" / "conversation_trace.py")
CT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = CT
_SPEC.loader.exec_module(CT)

_Q_AT = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)
_A_AT = datetime(2026, 10, 1, 0, 5, 0, tzinfo=timezone.utc)
_QUERY = "架空の項目名"
_DOC = "src/sample/架空資料.cbl"


def _ts(sec: int) -> str:
    return f"2026-10-01T00:{sec // 60:02d}:{sec % 60:02d}.000Z"


def _ev(sec, type_, payload):
    return {"timestamp": _ts(sec), "type": type_, "payload": payload}


def _mcp(sec, cid, tool, args, result, secs=1):
    return [
        _ev(sec, "response_item", {"type": "function_call", "name": tool, "namespace": "mcp__sherpa",
                                   "arguments": json.dumps(args, ensure_ascii=False), "call_id": cid}),
        _ev(sec + secs, "event_msg", {"type": "mcp_tool_call_end", "call_id": cid,
                                      "invocation": {"server": "sherpa", "tool": tool, "arguments": args},
                                      "duration": {"secs": secs, "nanos": 0},
                                      "result": {"Ok": {"content": [{"type": "text", "text": json.dumps(
                                          result, ensure_ascii=False)}], "isError": False}}}),
    ]


def _tokens(sec, row):
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
    return _ev(sec, "event_msg", {"type": "token_count", "info": {"last_token_usage": dict(zip(keys, row))}})


def _exec(sec, cid, cmd, exit_code, output):
    inner = json.dumps({"cmd": cmd}, ensure_ascii=False)
    return [
        _ev(sec, "response_item", {"type": "custom_tool_call", "name": "exec", "call_id": cid,
                                   "input": f"const r = await tools.exec_command({inner});\ntext(r);\n"}),
        _ev(sec + 1, "response_item", {"type": "custom_tool_call_output", "call_id": cid, "output": [
            {"type": "input_text", "text": json.dumps({"chunk_id": "k", "exit_code": exit_code, "output": output})}]}),
    ]


def _write(path: Path, meta: dict, events: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": _ts(1), "type": "session_meta", "payload": meta}] + events
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")


def _sessions(users_dir: Path) -> None:
    base = users_dir / "u1" / "workspace" / ".codex-sessions" / "7" / "sessions" / "2026" / "10" / "01"
    parent = [
        _ev(5, "event_msg", {"type": "task_started"}),
        _ev(5, "event_msg", {"type": "user_message", "message": "最初の依頼の全文（架空）"}),
        *_mcp(10, "c1", "ripgrep_search", {"query": _QUERY, "item": "I-alpha", "zz_secret_key": "社外秘の値"},
              {"hits": [{"doc_id": _DOC}, {"doc_id": _DOC}, {"doc_id": _DOC}], "truncated": True, "next_offset": 3}),
        _tokens(12, [100, 50, 10, 5]),
        _tokens(13, ["x", 0, 0, 0]),
        _ev(14, "event_msg", {"type": "token_count", "info": {"last_token_usage": "oops"}}),
        *_exec(15, "c2", f"rg -n 架空語 {_DOC}", 0, "a\nb\n"),
        *_mcp(17, "c3", "ledger_item_put", {"id": "I-alpha", "kind": "flow", "subject": "架空の対象",
                                            "required_checks": ["source"], "evidence": [],
                                            "status": "not_found_in_scope", "reason": "見つからない", "owner": "main"},
              {"ok": True, "id": "I-alpha"}),
        {"timestamp": _ts(18), "type": "compacted", "payload": {}},
        _tokens(20, [200, 150, 20, 10]),
        *_exec(40, "c6", "rg -n token secret.key:", 1, ""),
        *_mcp(50, "c7", "glob_search", {"pattern": 4711234}, {"count": 0, "paths": [], "truncated": False}),
        _ev(60, "event_msg", {"type": "task_started"}),
        _ev(60, "event_msg", {"type": "user_message", "message": "調査台帳に未完了の項目があります。未完了: I-alpha。"}),
        *_mcp(65, "c4", "read_around", {"doc_id": _DOC, "line": 10, "window": 5},
              {"doc_id": _DOC, "start_line": 5, "end_line": 15, "total_lines": 99, "text": "架空の本文",
               "file_truncated": True}),
        _tokens(66, [300, 250, 30, 15]),
        _ev(120, "event_msg", {"type": "task_started"}),
        _ev(120, "event_msg", {"type": "user_message", "message": "回答を確定する前の点検です（架空）"}),
        _tokens(125, [50, 40, 5, 0]),
    ]
    _write(base / "rollout-parent.jsonl", {"id": "p1", "thread_source": "user", "model_provider": "openai"}, parent)
    with open(base / "rollout-parent.jsonl", "a", encoding="utf-8") as f:
        f.write("{壊れた行\n")
    with open(base / "rollout-parent.jsonl", "ab") as f:
        f.write(b'{"timestamp": "2026-10-01T00:00:14.000Z", "type": "event_msg", '
                b'"payload": {"type": "agent_message", "message": "\xff\xfe"}}\n')
    (base / "rollout-broken.jsonl").write_text("読めないファイル\n", encoding="utf-8")
    child = [*_mcp(30, "c5", "es_search", {"query": _QUERY}, {"hits": [{"doc_id": _DOC, "text_truncated": True}]}),
             _tokens(31, [80, 0, 8, 2])]
    _write(base / "rollout-child.jsonl", {"id": "c-1", "parent_thread_id": "p1", "thread_source": "subagent",
                                          "agent_role": "worker"}, child)


def _conv(**answer_over) -> dict:
    answer = {
        "duration_ms": 300000, "stop_kind": "completed",
        "usage": {"provider": "codex", "model": "gpt-x", "input_tokens": 730, "cached_input_tokens": 490,
                  "output_tokens": 73, "reasoning_output_tokens": 32,
                  "codex_usage_breakdown": {"parent": {"input_tokens": 650, "cached_input_tokens": 490,
                                                       "output_tokens": 65, "reasoning_output_tokens": 30},
                                            "children": {"input_tokens": 80, "cached_input_tokens": 0,
                                                         "output_tokens": 8, "reasoning_output_tokens": 2}}},
        "activity": {"v": 1, "source": "codex_rollout", "phases_ms": {"prepare": 1000, "agent": 290000, "post": 9000},
                     "settings": {"mode": "standard", "config": "openai", "depth": "standard", "reasoning": "medium",
                                  "review_rounds": 2, "multi_agent": True},
                     "app_version": "0.14.40+abc1234",
                     "agents": [{"role": "parent", "model": "gpt-x", "tokens": {"input_tokens": 650},
                                 "rounds": [[650, 490, 65, 30]], "compactions": [1], "unparsed": {"zz": 1},
                                 "tools": {"ripgrep_search": {"calls": 2, "bytes": 2048, "max_bytes": 1536,
                                                              "clipped": 0, "truncated": 1, "errors": 0, "ms": 1000},
                                           "zz_custom_tool": {"calls": 1, "bytes": 10, "max_bytes": 10}}},
                                {"role": "child", "model": "llama3:8b-q4_companysecret", "tokens": {"input_tokens": 80},
                                 "rounds": [[80, 0, 8, 2]], "compactions": [], "tools": {}}]},
        "scope": {"world": "wld-sample", "scope_paths": ["src/sample"]},
        "investigation": {"complete": True, "continuations": 1, "counts": {"unverified": 1}},
    }
    answer.update(answer_over)
    return {
        "id": 7, "errors": [],
        "conversation": {"id": 7, "user_id": "u1", "version": "wld-sample", "title": "架空の題名"},
        "messages": [
            {"id": 10, "role": "user", "content": "架空の質問です。処理の流れを教えて", "personal": False,
             "answer": None, "trace": None, "created_at": _Q_AT},
            {"id": 11, "role": "assistant", "content": "架空の回答の先頭行", "personal": False, "answer": answer,
             "trace": [{"id": "cx-1", "kind": "tool", "label": "資料を検索（grep）", "detail": _QUERY, "status": "done"},
                       {"id": "ledger-continue-1", "kind": "think", "label": "調査台帳を確認", "detail": "",
                        "status": "done"},
                       {"id": "ledger-review-1", "kind": "think", "label": "回答前の点検", "detail": "",
                        "status": "done"}],
             "created_at": _A_AT},
        ],
        "audits": [{"id": 1, "detail": {"message_id_user": 10, "message_id_assistant": 11, "provider": "codex"},
                    "outcome": "success", "created_at": _A_AT}],
        "turn_metrics": {},
        "investigations": {11: {"complete": True, "truncated": False, "items": {"I-alpha": {
            "status": "unverified", "subject": "架空の対象", "reason": "範囲内で確認できない", "evidence": [],
            "owner": "main"}}, "coverage": {"I-alpha": ["truncated", "no_hits"]}, "reviews": []}},
        "usage_events": [],
        "user": {"uid": "u1", "email": "someone@example.invalid", "display_name": "架空 太郎"},
        "world": {"world_id": "wld-sample", "root_path": "/mnt/x/wld-sample", "label": "架空フォルダ"},
    }


def test_stages_tools_hits_and_token_deltas_in_time_order(tmp_path):
    _sessions(tmp_path)
    conv = _conv(data={"citations": [{"doc_id": "conf/server.pem", "quote": "架空の引用"}]})
    conv["messages"][0]["content"] += "（conf/app.key:12 と conf/app.key#L3 も見て）"
    out = CT.build_output([conv], CT.Masker(mask=False, salt=b"s" * 16), tmp_path)

    assert "段1 本体" in out and "段2 下調べ役1（worker）" in out and "段3 台帳の続き1" in out and "段4 見直し1" in out
    assert "圧縮 1 回" in next(ln for ln in out.splitlines() if "段1 本体" in ln)
    # 活動記録の数字（設定・台帳の終端の件数・本体/下調べ役のトークン・往復・圧縮・道具ごとの集計）
    assert "設定: review_rounds=2 multi_agent=True  版 0.14.40+abc1234" in out and "終端: unverified=1" in out
    assert "[道具ごとの集計]（活動記録から）" in out
    assert "本体  モデル gpt-x  入力 650" in out and "往復 1  最大入力 650  圧縮 1 回 @1" in out
    assert "ripgrep_search 2 回 2KiB 最大 2KiB 計 1.0s 打切1" in out and "読めなかった記録 1 件" in out
    assert "下調べ役1  モデル llama3:8b-q4_companysecret  入力 80" in out
    assert "目印と合わない" not in out
    tool_lines = [ln for ln in out.splitlines() if ln.startswith("  ") and "件数 " in ln]
    order = [next(t for t in ("ripgrep_search", "exec", "ledger_item_put", "es_search", "glob_search", "read_around")
                  if f"  {t}  " in ln) for ln in tool_lines]
    assert order == ["ripgrep_search", "exec", "ledger_item_put", "es_search", "exec", "glob_search", "read_around"]
    rg = tool_lines[0]
    assert "件数 3" in rg and "打ち切り: 件数の上限・続きあり" in rg and "[本体]" in rg
    assert "zz_secret_key" not in out and "社外秘の値" not in out and "他 1 項目" in rg
    assert "件数 2（出力行）" in tool_lines[1] and "exit 0" in tool_lines[1]
    assert "[下調べ役1（worker）]" in tool_lines[3] and "本文の切詰め（ヒット内）" in tool_lines[3]
    assert "[台帳の続き1]" in tool_lines[6] and "件数 11 行" in tool_lines[6] and "ファイルの切詰め" in tool_lines[6]

    assert "+入力 100（キャッシュ 50）" in out and "→ 累計入力 730" in out
    assert "食い違う" not in out
    assert "not_found_in_scope → unverified へ自動で下げた" in out
    assert "/conversations/7/messages/11/investigation" in out
    assert "secret.key" not in out and "（秘匿ファイル）" in out          # 句読点付きの秘匿名も伏せる
    assert "app.key" not in out                                        # 行番号付きの秘匿名も伏せる
    assert "server.pem" not in out and "架空の回答の先頭行" not in out
    assert "秘匿資料を含むため本文は出さない" in out
    assert "壊れた行（文字コードが壊れている・JSON として読めない・時刻が無い）を 2 行" in out
    assert "読めないセッションファイルを 1 本" in out
    assert "壊れたトークンの記録（数値でない値）を 2 件読み飛ばした" in out


def test_mask_hides_values_with_stable_symbols_and_fails_closed_on_leak(tmp_path, monkeypatch):
    _sessions(tmp_path)
    masker = CT.Masker(mask=True, salt=b"s" * 16)
    conv = _conv(codex_error_code="zz_internal_failure_code")
    conv["messages"][1]["answer"]["usage"]["model"] = "gpt-4o-company-prod"
    conv["messages"][1]["answer"]["activity"]["settings"]["config"] = "azure"
    conv["investigations"][11]["items"]["I-alpha"]["evidence"] = [
        {"kind": "source", "path": _DOC, "line": "id_rsa"}, {"kind": "source", "path": _DOC, "line": 12}]
    out = CT.build_output([conv], masker, tmp_path)
    assert ".cbl:12" in out and "形が壊れたもの（行番号が整数でない等）1 件" in out and "id_rsa" not in out

    for raw in ("架空の質問", "架空の回答", "架空資料", "架空語", _QUERY, "I-alpha", "src/sample", "wld-sample",
                "someone@example.invalid", "架空 太郎", "架空の題名", "架空の対象", "範囲内で確認できない", "secret.key",
                "zz_secret_key", "社外秘の値", "4711234", "zz_internal_failure_code", "zz_custom_tool",
                "plan.companysecret"):
        assert raw not in out, raw
    sym = masker.term(_QUERY)
    assert out.count(sym) >= 2             # 本体と下調べ役が同じ語で検索した＝同じ記号
    assert ".cbl" in out and "ripgrep_search" in out and "件数 3" in out and "+入力 100" in out
    assert CT.find_leaks(out, masker) == 0
    assert CT.find_leaks(out + "\n架空資料.cbl\n", masker) > 0

    assert "エラー: エラー#" in out and "pattern=数#" in out and "道具#" in out
    # モデル名は伏せ字でも出す（コスト計算用）。Azure は実際のモデルが記録に無いことを示す。
    assert "モデル: gpt-4o-company-prod（実際のモデル: 記録なし）" in out
    assert "下調べ役1  モデル llama3:8b-q4_companysecret" in out and "伏せ字: あり（モデル名は出す）" in out
    for raw in ("zz_internal_failure_code", "4711234"):
        assert CT.find_leaks(out + f"\n{raw}\n", masker) > 0, raw
    # MASK_MODELS=1 のときだけ、公開のモデル名以外を伏せて自己検査に入れる。
    masker_m = CT.Masker(mask=True, salt=b"s" * 16, mask_models=True)
    out_m = CT.build_output([conv], masker_m, tmp_path)
    assert "gpt-4o-company-prod" not in out_m and "companysecret" not in out_m and "モデル: モデル#" in out_m
    assert CT.find_leaks(out_m + "\ngpt-4o-company-prod\n", masker_m) > 0
    for model, public in (("gpt-4o-2099-12-31", False), ("llama3:8b-q4_companysecret", False),
                          ("gpt-4o-2024-08-06", True), ("llama3.1:8b", True)):
        assert (masker_m.model(model) == model) is public, model
    assert masker.word("UnicodeDecodeError", "error").startswith("エラー#")
    assert masker.word("TimeoutError", "error") == "TimeoutError"
    hidden_ext = masker.path("docs/plan.companysecret")
    assert ".companysecret" not in hidden_ext and "拡張子#" in hidden_ext
    assert CT.find_leaks(out + "\nplan.companysecret\n", masker) > 0
    public = _conv()
    public["messages"][1]["answer"]["usage"]["model"] = "gpt-5.5"
    public["messages"][1]["trace"][0:0] = [{"id": "x", "kind": "tool", "label": "社外秘ラベル", "status": "done"},
                                           {"id": "y", "kind": "tool", "label": "OPENAI_API_KEY", "status": "done"}]
    no_session = CT.build_output([public], CT.Masker(mask=True, salt=b"s" * 16), tmp_path / "x")
    assert "モデル: gpt-5.5" in no_session
    assert "社外秘ラベル" not in no_session and "OPENAI_API_KEY" not in no_session
    assert "資料を検索（grep）" in no_session

    orig = CT.render_conversation
    monkeypatch.setattr(CT, "render_conversation",
                        lambda *a, **k: orig(*a, **k) + [f"  {_DOC}"])
    with pytest.raises(CT.LeakError):
        CT.build_output([_conv()], CT.Masker(mask=True, salt=b"s" * 16), tmp_path)


def test_without_session_records_prints_db_only_and_names_the_gaps(tmp_path):
    conv = _conv(data={"evidence_packet": {"evidence": [{"evidence_id": "e1", "source_path": "keys/id_rsa"}]}})
    out = CT.build_output([conv], CT.Masker(mask=False, salt=b"s" * 16), tmp_path / "nothing")
    assert "秘匿資料を含むため本文は出さない" in out and "架空の回答の先頭行" not in out and "id_rsa" not in out
    nested = _conv(data={"candidates": [{"name": "架空部品", "evidence": {"doc": "conf/app.key", "line": 3}}]})
    nested_out = CT.build_output([nested], CT.Masker(mask=False, salt=b"s" * 16), tmp_path / "nothing")
    assert "秘匿資料を含むため本文は出さない" in nested_out and "app.key" not in nested_out
    broken = _conv()
    broken["messages"][1]["answer"]["activity"]["agents"][1]["rounds"].append(["x", 0, 0, 0])
    broken["messages"][1]["answer"]["activity"]["agents"][1]["tokens"] = "oops"
    broken["usage_events"] = [{"ts": _A_AT, "kind": "intent", "input_tokens": "12", "output_tokens": 3}]
    broken_out = CT.build_output([broken], CT.Masker(mask=False, salt=b"s" * 16), tmp_path / "nothing")
    assert "数値でないトークンの値（DB・活動記録）を 3 件読み飛ばした" in broken_out
    assert "intent  所要 -  トークン 記録なし" in broken_out

    assert "セッション記録: なし" in out
    assert "Codex のセッション記録が無い" in out
    assert "思考の流れの並びだけ" in out and "資料を検索" in out
    assert "台帳の続き 1 回" in out
    assert "往復 1: +入力 650" in out and "DB（回答の usage） 入力 730" in out
    assert "最終形（investigation_records）" in out
    assert "ripgrep_search  query=" not in out and "[道具ごとの集計]（活動記録から）" in out


def test_make_trace_passes_conv_and_out_without_shell_injection(tmp_path):
    root = Path(__file__).resolve().parents[2]
    planted = tmp_path / "planted"
    dry = subprocess.run(["make", "-n", "trace", "CONV=1", "MASK_MODELS=1", f'OUT=x"; touch {planted}; echo "'],
                         cwd=root, capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0 and str(planted) not in dry.stdout and '"${OUT}"' in dry.stdout
    assert '"${MASK_MODELS:-}" = 1 ]; then set -- "$@" --mask-models' in dry.stdout
    bad = subprocess.run(["make", "-s", "trace", f"CONV=1;touch {planted}"],
                         cwd=root, capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0 and "数字とカンマだけ" in bad.stdout and not planted.exists()
