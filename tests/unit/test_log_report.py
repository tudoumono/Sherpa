"""`scripts/log_report.py`（`scripts/logs.sh -r` の実体・LOG-UX・2026-09-04）の単体テスト。

純関数（行パース・所要秒計算・進捗行パース・エラー正規化グループ化・世代連結）だけを対象にする——
CLI 全体（`main()`）は薄い glue のため対象外。`scripts/` は暗黙の名前空間パッケージとして
`import scripts.log_report` できる（`tests/unit/test_ab_search_script.py` と同じ手法）。
"""
from __future__ import annotations

from datetime import datetime

import scripts.log_report as lr


# ---- parse_log_line ----

def test_parse_log_line_extracts_ts_level_name_msg():
    rec = lr.parse_log_line("2026-09-04 10:00:01,123 INFO sherpa.ingest.convert: MD化を開始します: a/b.docx")
    assert rec == {"ts": datetime(2026, 9, 4, 10, 0, 1, 123000), "level": "INFO",
                   "name": "sherpa.ingest.convert", "msg": "MD化を開始します: a/b.docx"}


def test_parse_log_line_rejects_non_conforming_lines():
    assert lr.parse_log_line("  継続行（トレースバック等）") is None
    assert lr.parse_log_line("") is None
    assert lr.parse_log_line("not a log line at all") is None


# ---- analyze_convert: 所要秒計算 ----

def test_analyze_convert_pairs_consecutive_starts_within_one_generation():
    gen = [
        "2026-09-04 10:00:00,000 INFO sherpa.ingest.convert: MD化を開始します: a.docx",
        "2026-09-04 10:00:05,000 INFO sherpa.ingest.convert: MD化を開始します: b.docx",
        "2026-09-04 10:00:12,000 INFO sherpa.ingest.convert: MD化を開始します: c.docx",
    ]
    r = lr.analyze_convert([gen])
    assert r["count"] == 2
    assert [e["file"] for e in r["entries"]] == ["a.docx", "b.docx"]
    assert r["entries"][0]["seconds"] == 5.0
    assert r["entries"][1]["seconds"] == 7.0
    assert r["unfinished"] == "c.docx"   # 最後の開始行は「実行中/不明」
    assert r["avg"] == 6.0
    assert r["median"] == 6.0


def test_analyze_convert_no_start_lines_is_empty():
    r = lr.analyze_convert([["2026-09-04 10:00:00,000 INFO x: 無関係な行"]])
    assert r["count"] == 0
    assert r["entries"] == []
    assert r["avg"] is None and r["median"] is None
    assert r["unfinished"] is None


def test_analyze_convert_top_slow_sorted_desc_and_capped_at_10():
    from datetime import timedelta
    gen = []
    t0 = datetime(2026, 9, 4, 10, 0, 0)
    t = t0
    # 12ファイル分の開始行（11区間）を作り、上位10件だけが top_slow に残ることを確認する。
    for i in range(12):
        gen.append(f"{t.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} INFO x: MD化を開始します: f{i}.docx")
        t += timedelta(seconds=(i + 1))   # 区間ごとに所要秒を変える（重複なしで順序を一意にする）
    r = lr.analyze_convert([gen])
    assert r["count"] == 11
    assert len(r["top_slow"]) == 10
    secs = [e["seconds"] for e in r["top_slow"]]
    assert secs == sorted(secs, reverse=True)
    assert secs[0] == max(e["seconds"] for e in r["entries"])


def test_analyze_convert_generation_boundary_is_not_paired_and_concatenates_old_to_new():
    """世代境界（再起動）をまたぐ差分は所要秒に数えない——各世代は独立に計算し、最後の世代の
    unfinished だけが最終結果に残る（追加要件5）。"""
    gen_old = [
        "2026-09-04 09:00:00,000 INFO x: MD化を開始します: old1.docx",
        "2026-09-04 09:00:03,000 INFO x: MD化を開始します: old2.docx",
        # ここで世代が終わる（プロセス再起動）。old2.docx は旧世代内では「未完」だったはずだが、
        # 次の世代の最初の行と対にして所要秒を計算してはいけない。
    ]
    gen_new = [
        "2026-09-04 10:30:00,000 INFO x: MD化を開始します: new1.docx",   # old2 の90分後（再起動の証拠）
        "2026-09-04 10:30:04,000 INFO x: MD化を開始します: new2.docx",
    ]
    r = lr.analyze_convert([gen_old, gen_new])
    # old1->old2（3秒）と new1->new2（4秒）だけが数えられる。old2->new1（90分）は数えない。
    assert r["count"] == 2
    files_secs = {e["file"]: e["seconds"] for e in r["entries"]}
    assert files_secs == {"old1.docx": 3.0, "new1.docx": 4.0}
    assert r["unfinished"] == "new2.docx"   # 最後の世代（現行ログ）の未完だけが残る


# ---- analyze_embed: 進捗行パース・スループット ----

def test_analyze_embed_parses_progress_and_computes_throughput():
    gen = [
        "2026-09-04 10:00:00,000 INFO sherpa.embed: es_index: embed 進捗 0/500 チャンク（world=test2）",
        "2026-09-04 10:01:00,000 INFO sherpa.embed: es_index: embed 進捗 120/500 チャンク（world=test2）",
    ]
    r = lr.analyze_embed([gen])
    assert set(r.keys()) == {"test2"}
    w = r["test2"]
    assert w["last_n"] == 120 and w["last_m"] == 500
    assert w["chunks_per_min"] == 120.0   # 120チャンク / 1分


def test_analyze_embed_multiple_worlds_are_independent():
    gen = [
        "2026-09-04 10:00:00,000 INFO sherpa.embed: es_index: embed 進捗 10/100 チャンク（world=a）",
        "2026-09-04 10:00:00,000 INFO sherpa.embed: es_index: embed 進捗 5/50 チャンク（world=b）",
    ]
    r = lr.analyze_embed([gen])
    assert r["a"]["last_m"] == 100
    assert r["b"]["last_m"] == 50


def test_analyze_embed_single_line_has_no_throughput():
    gen = ["2026-09-04 10:00:00,000 INFO sherpa.embed: es_index: embed 進捗 1/10 チャンク（world=w）"]
    r = lr.analyze_embed([gen])
    assert r["w"]["chunks_per_min"] is None


# ---- parse_usage_line / analyze_usage ----

def test_parse_usage_line_full_and_unreported_tokens():
    u = lr.parse_usage_line(
        "kind=embed provider=openai model=text-embedding-3-small in=52340 cached=0 out=0 "
        "calls=3 elapsed=12.4s world=test2")
    assert u == {"kind": "embed", "provider": "openai", "model": "text-embedding-3-small",
                "in": 52340, "cached": 0, "out": 0, "calls": 3, "elapsed": 12.4, "world": "test2"}

    u2 = lr.parse_usage_line("kind=graph_ask provider=bedrock model=claude in=? cached=? out=? calls=1")
    assert u2["in"] is None and u2["cached"] is None and u2["out"] is None
    assert u2["elapsed"] is None and u2["world"] is None


def test_parse_usage_line_non_usage_message_returns_none():
    assert lr.parse_usage_line("MD化を開始します: a.docx") is None


def test_analyze_usage_aggregates_by_kind_ignoring_unreported_tokens():
    gen = [
        "2026-09-04 10:00:00,000 INFO sherpa.usage: kind=embed provider=openai model=m in=10 cached=0 out=0 calls=1 elapsed=1.0s",
        "2026-09-04 10:00:01,000 INFO sherpa.usage: kind=embed provider=openai model=m in=? cached=? out=? calls=1",
        "2026-09-04 10:00:02,000 INFO sherpa.usage: kind=intent provider=ollama model=m2 in=5 cached=0 out=2 calls=1 elapsed=0.5s",
    ]
    r = lr.analyze_usage([gen])
    assert r["embed"]["in"] == 10   # 報告不能（None）だった1行は無視して合算
    assert r["embed"]["calls"] == 2
    assert r["embed"]["lines"] == 2
    assert r["intent"]["in"] == 5 and r["intent"]["elapsed"] == 0.5


# ---- summarize_errors ----

def test_summarize_errors_groups_by_normalized_prefix_and_orders_by_count():
    # 先頭60字が同じになるよう、差分（attempt N）を60字より後ろに置く（60字未満で差が出る文言だと
    # 正規化キーが割れて別グループになってしまう＝グループ化の意図を正しく検証できないため）。
    common_prefix = ("接続に失敗しました: " + "リモートホストへの到達性が確認できませんでした" * 3)[:70]
    assert len(common_prefix) >= 60
    records = [
        {"ts": datetime(2026, 9, 4, 10, 0, 0), "level": "ERROR", "name": "x",
         "msg": common_prefix + " attempt=1", "source": "api"},
        {"ts": datetime(2026, 9, 4, 10, 5, 0), "level": "ERROR", "name": "x",
         "msg": common_prefix + " attempt=2", "source": "api"},
        {"ts": datetime(2026, 9, 4, 10, 1, 0), "level": "WARNING", "name": "x",
         "msg": "設定が古い可能性があります", "source": "convert"},
        {"ts": datetime(2026, 9, 4, 10, 2, 0), "level": "INFO", "name": "x",
         "msg": "MD化を開始します: ok.docx", "source": "convert"},   # 通常行＝対象外
    ]
    groups = lr.summarize_errors(records)
    # ERROR2件（正規化キーが同じで1グループに集約）＋WARNING1件（レベルだけで対象になる・本文に
    # ERROR/失敗等を含まなくてもよい）＝2グループ（INFO の通常行は対象外）。
    assert len(groups) == 2
    assert groups[0]["count"] == 2   # 「接続に失敗しました…」が先頭60字で正規化されて2件グループ化
    assert groups[0]["first"] == datetime(2026, 9, 4, 10, 0, 0)
    assert groups[0]["last"] == datetime(2026, 9, 4, 10, 5, 0)
    assert groups[0]["sources"] == ["api"]
    assert groups[1]["count"] == 1
    assert groups[1]["key"] == "設定が古い可能性があります"


def test_summarize_errors_normalizes_to_60_chars():
    long_msg = "失敗理由の説明が非常に長く続く場合でも先頭60字だけを見てグループ化する" * 3
    records = [{"ts": datetime(2026, 9, 4, 10, 0, 0), "level": "ERROR", "name": "x",
                "msg": long_msg, "source": "api"}]
    groups = lr.summarize_errors(records)
    assert len(groups[0]["key"]) == 60


def test_summarize_errors_ignores_normal_lines():
    records = [{"ts": datetime(2026, 9, 4, 10, 0, 0), "level": "INFO", "name": "x",
                "msg": "MD化を開始します: ok.docx", "source": "convert"}]
    assert lr.summarize_errors(records) == []


# ---- list_generations: 世代の発見・古い→新しい順 ----

def test_list_generations_orders_old_to_new_with_current_last(tmp_path):
    (tmp_path / "convert-20260901-090000.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "convert-20260902-090000.log").write_text("mid\n", encoding="utf-8")
    (tmp_path / "convert.log").write_text("current\n", encoding="utf-8")
    (tmp_path / "convert-notes.log").write_text("触らない\n", encoding="utf-8")   # 命名規約に一致しない

    paths = lr.list_generations(tmp_path, "convert", include_rotated=True)
    assert [p.name for p in paths] == [
        "convert-20260901-090000.log", "convert-20260902-090000.log", "convert.log"]


def test_list_generations_without_rotated_returns_current_only(tmp_path):
    (tmp_path / "convert-20260901-090000.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "convert.log").write_text("current\n", encoding="utf-8")
    paths = lr.list_generations(tmp_path, "convert", include_rotated=False)
    assert [p.name for p in paths] == ["convert.log"]


def test_has_rotated_generations(tmp_path):
    (tmp_path / "convert.log").write_text("current\n", encoding="utf-8")
    assert lr.has_rotated_generations(tmp_path, "convert") is False
    (tmp_path / "convert-20260901-090000.log").write_text("old\n", encoding="utf-8")
    assert lr.has_rotated_generations(tmp_path, "convert") is True
