#!/usr/bin/env python3
"""`scripts/logs.sh -r` の実体（LOG-UX・2026-09-04・閉域実機フィードバック）。

`data/run/*.log`（sherpa/log_setup.py が書く「%(asctime)s %(levelname)s %(name)s: %(message)s」形式の
行）を読んで、追わずに集計だけして終了するレポートを作る。標準ライブラリのみ（閉域で `.venv` が
無くても素の `python3` で動く契約・`scripts/logs.sh` 側が `.venv/bin/python` を優先しつつ python3 へ
フォールバックする）。

対象:
  - convert.log: 「MD化を開始します: <rel>」の開始時刻の差から1ファイルの所要秒を推定する
    （`office_md.py::_log.info("MD化を開始します: %s", rel)` が出す行）。
  - embed.log: 「embed 進捗 n/m チャンク（world=w）」からスループット（チャンク/分）を推定する
    （`es_index.py::_embed_log` が出す行）。
  - usage.log: `metering.log_usage_line()` が出す `kind=... provider=... ... in=.. cached=.. out=..
    calls=.. elapsed=..s world=..` 形式の行を kind 別に集計する。
  - 全対象ログ横断のエラー/警告要約（メッセージ先頭60字で正規化してグループ化）。

世代（ローテーション）: 既定は現行 `*.log` のみ。`--all` を指定すると `log_setup.rotate_and_prune`
が退避した過去世代（`<stem>-YYYYmmdd-HHMMSS[-N].log`）も古い→新しい順に連結して集計する——ただし
世代の境界は「再起動」とみなし、境界をまたぐ所要秒は数えない（世代ごとに独立して計算し、各世代の
最後の未閉区間は次の世代へ持ち越さない）。

このファイルの集計ロジック（`analyze_convert`/`analyze_embed`/`parse_usage_line`/`summarize_errors`
等）は純関数——`tests/unit/test_log_report.py` が文字列リテラルの小さな fixture で固定する。
"""
from __future__ import annotations

import argparse
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

# ---- 行パース ----------------------------------------------------------------------------------

# sherpa/log_setup.py の共通フォーマット（_make_file_handler/_make_run_log_handler）と同じ。
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>[A-Z]+) (?P<name>\S+): (?P<msg>.*)$")


def parse_log_line(line: str) -> dict | None:
    """1行 -> `{"ts": datetime, "level": str, "name": str, "msg": str}`。形式外の行（継続行・空行等）は None。"""
    m = _LINE_RE.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None
    return {"ts": ts, "level": m.group("level"), "name": m.group("name"), "msg": m.group("msg")}


def parse_lines(raw_lines: list) -> list:
    """複数行 -> パースできた行だけの list（形式外は読み飛ばす＝黙って無視。トレースバックの
    継続行等がここで落ちるのは意図どおり——エラー要約は先頭行のメッセージで足りる）。"""
    out = []
    for line in raw_lines:
        rec = parse_log_line(line)
        if rec is not None:
            out.append(rec)
    return out


# ---- 世代（ローテーション）の発見 ----------------------------------------------------------------

_ROTATED_RE_TEMPLATE = r"^{stem}-\d{{8}}-\d{{6}}(?:-\d+)?\.log$"


def list_generations(log_dir: Path, name: str, include_rotated: bool) -> list:
    """`name`（拡張子なし）の世代ファイルを古い→新しい順に返す（`log_setup.rotate_and_prune` の
    命名規約 `<stem>-YYYYmmdd-HHMMSS[-N].log` に厳密一致するものだけ・現行 `<name>.log` は常に最後）。
    `include_rotated=False` なら現行のみ（無ければ空 list）。"""
    current = log_dir / f"{name}.log"
    if not include_rotated:
        return [current] if current.exists() else []
    pattern = re.compile(_ROTATED_RE_TEMPLATE.format(stem=re.escape(name)))
    rotated = sorted((p for p in log_dir.glob(f"{name}-*.log") if pattern.match(p.name)), key=lambda p: p.name)
    out = list(rotated)
    if current.exists():
        out.append(current)
    return out


def has_rotated_generations(log_dir: Path, name: str) -> bool:
    """既定モード（`--all` 無し）の末尾通知用: 退避済みの過去世代が実在するか。"""
    pattern = re.compile(_ROTATED_RE_TEMPLATE.format(stem=re.escape(name)))
    return any(pattern.match(p.name) for p in log_dir.glob(f"{name}-*.log"))


def _read_lines(path: Path) -> list:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


# ---- convert.log ----------------------------------------------------------------------------

_CONVERT_START_RE = re.compile(r"^MD化を開始します: (?P<rel>.+)$")


def analyze_convert(generations: list) -> dict:
    """`generations`: 世代ごとの生行 list（`list[list[str]]`・古い→新しい順）。

    「MD化を開始します: rel」の開始時刻の差分で1ファイルぶんの所要秒を推定する（次の開始行との差・
    世代内で完結——境界をまたいで対にしない＝再起動を「所要時間」に数えない）。各世代の最後の
    開始行は「実行中/不明」として `unfinished` に持ち越す（最後の世代の値が最終結果に残る）。

    戻り値: `{"entries": [{"file","seconds"}], "count", "total_seconds", "avg", "median",
    "top_slow": [...上位10件...], "unfinished": rel|None}`。開始行が1行も無ければ全て空/None。
    """
    entries: list = []
    unfinished = None
    for gen_lines in generations:
        starts = []
        for rec in parse_lines(gen_lines):
            m = _CONVERT_START_RE.match(rec["msg"])
            if m:
                starts.append((rec["ts"], m.group("rel")))
        unfinished = None
        for i in range(len(starts) - 1):
            ts0, rel0 = starts[i]
            ts1, _rel1 = starts[i + 1]
            secs = (ts1 - ts0).total_seconds()
            if secs >= 0:   # 負＝時計異常等（通常は起きない）・数えない
                entries.append({"file": rel0, "seconds": secs})
        if starts:
            unfinished = starts[-1][1]
    count = len(entries)
    total = sum(e["seconds"] for e in entries)
    top_slow = sorted(entries, key=lambda e: -e["seconds"])[:10]
    return {
        "entries": entries, "count": count, "total_seconds": total,
        "avg": (total / count) if count else None,
        "median": statistics.median([e["seconds"] for e in entries]) if count else None,
        "top_slow": top_slow, "unfinished": unfinished,
    }


# ---- embed.log --------------------------------------------------------------------------------

# es_index.py::_embed_log.info("es_index: embed 進捗 %d/%d チャンク（world=%s）", ...) の実メッセージ形。
_EMBED_PROGRESS_RE = re.compile(r"embed 進捗 (?P<n>\d+)/(?P<m>\d+) チャンク（world=(?P<world>[^）]*)）")


def analyze_embed(generations: list) -> dict:
    """世代ぶんの生行 list -> world 別の進捗サマリ。

    戻り値: `{"<world>": {"first_n","last_n","last_m","minutes","chunks_per_min","last_ts"}}`。
    スループットは world 内の最初と最後の進捗行から算出（`(last_n-first_n)/経過分`）。経過0分/
    進捗行1件のみは `chunks_per_min` を None にする（0除算・無意味な瞬間値を避ける）。
    世代境界はスループット計算に影響しない——world ごとに **全世代を通した**最初/最後の行を使う
    （embed はプロセス再起動をまたいでも同じ world の続きを処理しうるため、convert のような
    「境界で打ち切る」制約はここでは付けない＝素直に「観測できた範囲全体」のスループットを出す）。
    """
    by_world: dict = {}
    for gen_lines in generations:
        for rec in parse_lines(gen_lines):
            m = _EMBED_PROGRESS_RE.search(rec["msg"])
            if not m:
                continue
            world = m.group("world")
            n, mm = int(m.group("n")), int(m.group("m"))
            w = by_world.setdefault(world, {"first_ts": rec["ts"], "first_n": n,
                                            "last_ts": rec["ts"], "last_n": n, "last_m": mm})
            if rec["ts"] < w["first_ts"]:
                w["first_ts"], w["first_n"] = rec["ts"], n
            if rec["ts"] >= w["last_ts"]:
                w["last_ts"], w["last_n"], w["last_m"] = rec["ts"], n, mm
    out = {}
    for world, w in by_world.items():
        minutes = (w["last_ts"] - w["first_ts"]).total_seconds() / 60.0
        delta_n = w["last_n"] - w["first_n"]
        chunks_per_min = (delta_n / minutes) if minutes > 0 and delta_n > 0 else None
        out[world] = {"first_n": w["first_n"], "last_n": w["last_n"], "last_m": w["last_m"],
                      "minutes": minutes, "chunks_per_min": chunks_per_min}
    return out


# ---- usage.log ---------------------------------------------------------------------------------

_KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_usage_line(msg: str) -> dict | None:
    """`metering.log_usage_line()` が出すメッセージ本文 -> kv dict。`kind` が無ければ None
    （usage.log 以外の行が混ざっていても無視できる）。`in`/`cached`/`out` の `?`（報告不能マーカー）
    は None。`elapsed` は末尾 `s` を剥がして float 化（無ければ None）。"""
    kv = dict(_KV_RE.findall(msg))
    if "kind" not in kv:
        return None

    def _tok(key):
        v = kv.get(key)
        if v is None or v == "?":
            return None
        try:
            return int(v)
        except ValueError:
            return None

    elapsed = None
    el = kv.get("elapsed")
    if el and el.endswith("s"):
        try:
            elapsed = float(el[:-1])
        except ValueError:
            elapsed = None
    calls = None
    try:
        calls = int(kv["calls"]) if "calls" in kv else None
    except ValueError:
        calls = None
    return {"kind": kv.get("kind"), "provider": kv.get("provider"), "model": kv.get("model"),
            "in": _tok("in"), "cached": _tok("cached"), "out": _tok("out"),
            "calls": calls, "elapsed": elapsed, "world": kv.get("world")}


def analyze_usage(generations: list) -> dict:
    """世代ぶんの生行 list -> kind 別集計 `{"<kind>": {"in","cached","out","calls","elapsed","lines"}}`。
    トークン欄は報告不能（None）だった行を無視して合算する（`?` を0として扱うと過小評価するため）。"""
    by_kind: dict = {}
    for gen_lines in generations:
        for rec in parse_lines(gen_lines):
            u = parse_usage_line(rec["msg"])
            if u is None:
                continue
            k = by_kind.setdefault(u["kind"], {"in": 0, "cached": 0, "out": 0, "calls": 0,
                                               "elapsed": 0.0, "lines": 0})
            k["lines"] += 1
            for field in ("in", "cached", "out"):
                if u[field] is not None:
                    k[field] += u[field]
            if u["calls"] is not None:
                k["calls"] += u["calls"]
            if u["elapsed"] is not None:
                k["elapsed"] += u["elapsed"]
    return by_kind


# ---- エラー/警告の要約（全ログ横断） -------------------------------------------------------------

_ERR_LEVELS = frozenset({"ERROR", "CRITICAL", "WARNING"})
_ERR_TEXT_RE = re.compile(r"ERROR|CRITICAL|Traceback|失敗|✗")


def _is_error_like(rec: dict) -> bool:
    return rec["level"] in _ERR_LEVELS or bool(_ERR_TEXT_RE.search(rec["msg"]))


def normalize_error_message(msg: str, width: int = 60) -> str:
    """先頭 `width` 字で正規化（前後空白トリム）——グループ化キー。"""
    return msg.strip()[:width]


def summarize_errors(records: list, top: int = 10) -> list:
    """`records`: `{"ts","level","name","msg","source"}` の list（複数ログ・複数世代を跨いでよい）。
    ERROR/CRITICAL/WARNING レベル、または本文に ERROR/Traceback/失敗/✗ を含む行だけを対象に、
    メッセージ先頭60字で正規化してグループ化し、件数降順で `top` 件を返す
    （各グループは `{"key","count","first","last","sources"}`）。"""
    groups: dict = {}
    for r in records:
        if not _is_error_like(r):
            continue
        key = normalize_error_message(r["msg"])
        g = groups.setdefault(key, {"key": key, "count": 0, "first": r["ts"], "last": r["ts"], "sources": set()})
        g["count"] += 1
        g["first"] = min(g["first"], r["ts"])
        g["last"] = max(g["last"], r["ts"])
        if r.get("source"):
            g["sources"].add(r["source"])
    ordered = sorted(groups.values(), key=lambda g: -g["count"])[:top]
    for g in ordered:
        g["sources"] = sorted(g["sources"])
    return ordered


# ---- レポート整形・CLI --------------------------------------------------------------------------

def _fmt_secs(v: float | None) -> str:
    return "?" if v is None else f"{v:.1f}"


def render_report(log_dir: Path, names: list, include_rotated: bool) -> str:
    out = []
    all_records = []   # エラー要約用（対象ログ全て・全世代）
    rotated_skipped = []   # --all 無し・かつ過去世代が実在する名前（末尾通知用）

    for name in names:
        gens_paths = list_generations(log_dir, name, include_rotated)
        gen_lines = [_read_lines(p) for p in gens_paths]
        for gl in gen_lines:
            for rec in parse_lines(gl):
                all_records.append({**rec, "source": name})
        if not include_rotated and has_rotated_generations(log_dir, name):
            rotated_skipped.append(name)

        if name == "convert" and gen_lines:
            r = analyze_convert(gen_lines)
            out.append("== 変換（convert.log） ==")
            if r["count"] == 0:
                out.append("  変換の開始行がありません。")
            else:
                out.append(f"  件数: {r['count']}  合計: {_fmt_secs(r['total_seconds'])}秒"
                           f"  平均: {_fmt_secs(r['avg'])}秒  中央値: {_fmt_secs(r['median'])}秒")
                if r["total_seconds"] and r["total_seconds"] > 0:
                    out.append(f"  件/時: {r['count'] / (r['total_seconds'] / 3600.0):.1f}")
                out.append("  遅い順トップ10:")
                for e in r["top_slow"]:
                    out.append(f"    {_fmt_secs(e['seconds'])}秒  {e['file']}")
                if r["unfinished"]:
                    out.append(f"  実行中/不明: {r['unfinished']}")
            out.append("")

        if name == "embed" and gen_lines:
            r = analyze_embed(gen_lines)
            out.append("== 埋め込み（embed.log） ==")
            if not r:
                out.append("  進捗行がありません。")
            else:
                for world, w in r.items():
                    cpm = "?" if w["chunks_per_min"] is None else f"{w['chunks_per_min']:.1f}"
                    out.append(f"  world={world}: 進捗 {w['last_n']}/{w['last_m']}  "
                               f"スループット {cpm} チャンク/分")
            out.append("")

        if name == "usage" and gen_lines:
            r = analyze_usage(gen_lines)
            out.append("== 利用量（usage.log） ==")
            if not r:
                out.append("  記録がありません。")
            else:
                for kind, k in sorted(r.items()):
                    out.append(f"  kind={kind}: in={k['in']} cached={k['cached']} out={k['out']}"
                               f"  calls={k['calls']}  elapsed合計={_fmt_secs(k['elapsed'])}秒"
                               f"  ({k['lines']}行)")
            out.append("")

    out.append("== エラー/警告の要約（全対象ログ横断・トップ10） ==")
    err_groups = summarize_errors(all_records)
    if not err_groups:
        out.append("  該当行がありません。")
    else:
        for g in err_groups:
            out.append(f"  {g['count']:>4}件  [{','.join(g['sources'])}]  {g['key']}")
            out.append(f"        初回 {g['first']}  最終 {g['last']}")
    out.append("")

    if rotated_skipped:
        out.append(f"※ 退避された過去世代があります（{', '.join(rotated_skipped)}）: -A/--all で含めて集計できます。")

    return "\n".join(out)


def _default_names(log_dir: Path) -> list:
    return sorted(p.stem for p in log_dir.glob("*.log") if not re.search(r"-\d{8}-\d{6}(?:-\d+)?$", p.stem))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="アプリ系ログ（data/run/*.log）の集計レポート（追わずに終了）。")
    ap.add_argument("--log-dir", required=True, help="対象ディレクトリ（例: data/run）")
    ap.add_argument("--all", "-A", action="store_true", dest="all_gens",
                    help="退避済みの過去世代（*.log.1 相当）も連結して集計する")
    ap.add_argument("names", nargs="*", help="対象の名前（例: convert embed usage）。省略時は log-dir 内の全 *.log")
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir)
    names = args.names or _default_names(log_dir)
    if not names:
        print(f"ログがありません: {log_dir}/*.log", file=sys.stderr)
        return 1
    print(render_report(log_dir, names, args.all_gens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
