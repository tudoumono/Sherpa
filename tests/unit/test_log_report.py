"""`scripts/log_report.py`（`scripts/logs.sh -r` の実体）の単体テスト。"""
from __future__ import annotations

from datetime import datetime, timedelta

import scripts.log_report as lr


def _l(ts, msg, name="x", level="INFO"):
    return f"2026-09-04 {ts} {level} {name}: {msg}"


def test_parse_log_line():
    rec = lr.parse_log_line("2026-09-04 10:00:01,123 INFO sherpa.ingest.convert: MD化を開始します: a/b.docx")
    assert rec == {"ts": datetime(2026, 9, 4, 10, 0, 1, 123000), "level": "INFO",
                   "name": "sherpa.ingest.convert", "msg": "MD化を開始します: a/b.docx"}
    assert all(lr.parse_log_line(x) is None for x in ("  継続行（トレースバック等）", "", "not a log line at all"))


def test_analyze_convert_pairs_starts_within_one_generation_only():
    start = "MD化を開始します: "
    gen = [_l("10:00:00,000", start + "a.docx"), _l("10:00:05,000", start + "b.docx"), _l("10:00:12,000", start + "c.docx")]
    r = lr.analyze_convert([gen])
    assert r["count"] == 2 and [e["file"] for e in r["entries"]] == ["a.docx", "b.docx"]
    assert [e["seconds"] for e in r["entries"]] == [5.0, 7.0] and r["avg"] == r["median"] == 6.0
    assert r["unfinished"] == "c.docx"   # 最後の開始行は「実行中/不明」
    empty = lr.analyze_convert([[_l("10:00:00,000", "無関係な行")]])
    assert empty["count"] == 0 and empty["entries"] == [] and empty["avg"] is None and empty["unfinished"] is None
    # 世代境界（再起動）をまたぐ差分は数えない・最後の世代の unfinished だけが残る
    old = [_l("09:00:00,000", start + "old1.docx"), _l("09:00:03,000", start + "old2.docx")]
    new = [_l("10:30:00,000", start + "new1.docx"), _l("10:30:04,000", start + "new2.docx")]
    r = lr.analyze_convert([old, new])
    assert {e["file"]: e["seconds"] for e in r["entries"]} == {"old1.docx": 3.0, "new1.docx": 4.0}
    assert r["unfinished"] == "new2.docx"
    # 遅い順の上位 10 件
    t, gen = datetime(2026, 9, 4, 10, 0, 0), []
    for i in range(12):
        gen.append(f"{t.strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]} INFO x: {start}f{i}.docx")
        t += timedelta(seconds=i + 1)
    r = lr.analyze_convert([gen])
    secs = [e["seconds"] for e in r["top_slow"]]
    assert r["count"] == 11 and len(secs) == 10 and secs == sorted(secs, reverse=True)


def test_analyze_embed_progress_and_throughput():
    prog = "es_index: embed 進捗 {}/{} チャンク（world={}）"
    r = lr.analyze_embed([[_l("10:00:00,000", prog.format(0, 500, "test2"), "sherpa.embed"),
                           _l("10:01:00,000", prog.format(120, 500, "test2"), "sherpa.embed"),
                           _l("10:00:00,000", prog.format(10, 100, "a"), "sherpa.embed")]])
    assert r["test2"]["last_n"] == 120 and r["test2"]["last_m"] == 500 and r["test2"]["chunks_per_min"] == 120.0
    assert r["a"]["last_m"] == 100 and r["a"]["chunks_per_min"] is None   # 行が 1 つならスループットは出さない


def test_usage_line_parse_and_aggregate():
    u = lr.parse_usage_line("kind=embed provider=openai model=text-embedding-3-small in=52340 cached=0 out=0 "
                            "calls=3 elapsed=12.4s world=test2")
    assert u == {"kind": "embed", "provider": "openai", "model": "text-embedding-3-small",
                 "in": 52340, "cached": 0, "out": 0, "calls": 3, "elapsed": 12.4, "world": "test2"}
    u2 = lr.parse_usage_line("kind=graph_ask provider=bedrock model=claude in=? cached=? out=? calls=1")
    assert u2["in"] is None and u2["elapsed"] is None and u2["world"] is None
    assert lr.parse_usage_line("MD化を開始します: a.docx") is None
    gen = [_l("10:00:00,000", "kind=embed provider=openai model=m in=10 cached=0 out=0 calls=1 elapsed=1.0s", "sherpa.usage"),
           _l("10:00:01,000", "kind=embed provider=openai model=m in=? cached=? out=? calls=1", "sherpa.usage"),
           _l("10:00:02,000", "kind=intent provider=ollama model=m2 in=5 cached=0 out=2 calls=1 elapsed=0.5s", "sherpa.usage")]
    r = lr.analyze_usage([gen])
    assert (r["embed"]["in"], r["embed"]["calls"], r["embed"]["lines"]) == (10, 2, 2)   # 報告不能の行は無視して合算
    assert r["intent"]["in"] == 5 and r["intent"]["elapsed"] == 0.5


def test_summarize_errors_groups_by_normalized_prefix_and_orders_by_count():
    common = ("接続に失敗しました: " + "リモートホストへの到達性が確認できませんでした" * 3)[:70]

    def rec(minute, level, msg, source):
        return {"ts": datetime(2026, 9, 4, 10, minute, 0), "level": level, "name": "x", "msg": msg, "source": source}
    groups = lr.summarize_errors([rec(0, "ERROR", common + " attempt=1", "api"), rec(5, "ERROR", common + " attempt=2", "api"),
                                  rec(1, "WARNING", "設定が古い可能性があります", "convert"),
                                  rec(2, "INFO", "MD化を開始します: ok.docx", "convert")])   # 通常行は対象外
    assert len(groups) == 2 and groups[0]["count"] == 2 and groups[0]["sources"] == ["api"]
    assert groups[0]["first"] == datetime(2026, 9, 4, 10, 0, 0) and groups[0]["last"] == datetime(2026, 9, 4, 10, 5, 0)
    assert groups[1]["count"] == 1 and groups[1]["key"] == "設定が古い可能性があります"
    long_msg = "失敗理由の説明が非常に長く続く場合でも先頭60字だけを見てグループ化する" * 3
    assert len(lr.summarize_errors([rec(0, "ERROR", long_msg, "api")])[0]["key"]) == 60
    assert lr.summarize_errors([rec(0, "INFO", "MD化を開始します: ok.docx", "convert")]) == []


def test_list_generations_and_has_rotated(tmp_path):
    (tmp_path / "convert.log").write_text("current\n", encoding="utf-8")
    assert lr.has_rotated_generations(tmp_path, "convert") is False
    (tmp_path / "convert-20260901-090000.log").write_text("old\n", encoding="utf-8")
    (tmp_path / "convert-20260902-090000.log").write_text("mid\n", encoding="utf-8")
    (tmp_path / "convert-notes.log").write_text("触らない\n", encoding="utf-8")   # 命名規約に一致しない
    assert lr.has_rotated_generations(tmp_path, "convert") is True
    assert [p.name for p in lr.list_generations(tmp_path, "convert", include_rotated=True)] == [
        "convert-20260901-090000.log", "convert-20260902-090000.log", "convert.log"]
    assert [p.name for p in lr.list_generations(tmp_path, "convert", include_rotated=False)] == ["convert.log"]


# ---- scripts/logs.sh と make logs の語・短縮 ----

def test_logs_words_aliases_help_and_unknown_names(tmp_path):
    import os
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parents[2]
    for name in ("api", "convert", "embed"):
        (tmp_path / f"{name}.log").write_text(f"{name} line\n", encoding="utf-8")
    env = {**os.environ, "SHERPA_LOG_DIR": str(tmp_path)}

    def logs(*args: str, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(root / "scripts" / "logs.sh"), *args], cwd=root,
                              env={**env, **extra}, capture_output=True, text=True, timeout=120)

    # 短縮（c=convert・e=embed・app=api）と複数名・n=行数が解決される（-l は追わずに一覧）
    r = logs("-l", "c", "e", "n=3")
    assert r.returncode == 0, r.stderr
    assert "convert.log" in r.stdout and "embed.log" in r.stdout and "api.log" not in r.stdout
    assert "api.log" in logs("-l", "app").stdout
    # help は実在のログ名を「名前（短縮）」と説明つきで列挙する（make logs help・make l h と同じ）
    out = logs("h").stdout
    assert "convert（c）" in out and "embed（e）" in out and "api（app）" in out and "help（h）" in out
    assert "convert（c）" in logs("-h").stdout
    # make 経由の語は環境変数で渡る
    assert "embed（e）" in logs(LOGS_WORDS="h").stdout
    # 知らない名前・数字でない n= は「指定できる名前」を出して止まる
    for bad in ("zzz", "n=abc"):
        r = logs("-l", bad)
        assert r.returncode == 1 and "指定できる名前:" in r.stderr and "convert（c）" in r.stderr, r.stderr
    # make の展開: 後ろの語は引数として渡り、同名の実目標（help）は走らず警告も出ない
    for goals in (["logs", "convert", "embed"], ["l", "c", "e"], ["logs", "help"], ["l", "n=500", "c"]):
        m = subprocess.run(["make", "-n", *goals], cwd=root, capture_output=True, text=True, timeout=60)
        assert m.returncode == 0 and "warning" not in m.stderr, m.stderr
        assert m.stdout.count("./scripts/logs.sh") == 1 and "Sherpa — make の使い方" not in m.stdout
    # 後ろに実在の目標名があれば、何も実行せず非 0 で止まる（実目標が走らない）
    for goals in (["logs", "help", "nuke"], ["l", "start"], ["logs", "c", "stop"]):
        m = subprocess.run(["make", "-n", *goals], cwd=root, capture_output=True, text=True, timeout=60)
        assert m.returncode != 0 and "指定できない語" in m.stderr, m.stderr
        assert "nuke.sh" not in m.stdout and "start.sh" not in m.stdout and "stop.sh" not in m.stdout
