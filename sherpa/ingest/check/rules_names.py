"""点検の名前の項目（ファイル名と中に書かれた名前のずれ・どこにも定義の無い名前）。名前の台帳の行とグラフの未解決の申告から組む。"""
from __future__ import annotations

from pathlib import PurePosixPath

from sherpa.ingest import name_rows

from .model import GOOD, WARN, Report, names_text

RULE = "名前"
_NO_LEDGER = "名前の台帳がまだ無いため、ファイル名と中に書かれた名前の点検は出していない（取り込み直すと出る）。"
_NO_UNRESOLVED = "関係グラフから未解決の参照を読めなかったため、どこにも定義の無い名前の点検は出していない。"


def _declared_by_rel(rows: list) -> dict:
    out: dict = {}
    for r in rows:
        if r["source"] == name_rows.SOURCE_DECLARED and r["kind"] in name_rows.PROGRAM_KINDS:
            out.setdefault(r["rel_path"], set()).add(r["name"])
    return out


def _mismatch(rows: list) -> list:
    """ファイル名と中に書かれた名前が違うソースの `(相対パス, 中に書かれた名前の並び)`。ファイル名全体（拡張子つき）が中の名前と同じものは違いに数えない。"""
    names = {r["rel_path"]: r["name"] for r in rows if r["source"] == name_rows.SOURCE_FILENAME}
    out = []
    for rel, decl in sorted(_declared_by_rel(rows).items()):
        stem = names.get(rel)
        if stem is not None and stem not in decl and name_rows.normalize_name(PurePosixPath(rel).name) not in decl:
            out.append((rel, sorted(decl)))
    return out


def _mismatch_item(rep: Report, rows: list) -> None:
    bad = _mismatch(rows)
    if not bad:
        rep.add(RULE, GOOD, "", "ファイル名と中に書かれた名前が違うソースは無かった",
                "ファイル名で探しても、中に書かれた名前で探しても、同じソースが見つかる。", count=0)
        return
    rep.add(RULE, WARN, "", f"ファイル名と中に書かれた名前が違うソースが {len(bad)} 件ある",
            "グラフは中に書かれた名前でつなぐ。ファイル名の文字列の検索・一覧では、中に書かれた名前では見つからない。"
            "名前の台帳（name_lookup）は、中に書かれた名前からもファイル名からも引ける。",
            "実行される名前が中に書かれた名前なら、そのままでよい。ファイル名のほうが実行される名前なら、ソースの中の名前を直すか、"
            "実行される名前とソースの対応を確かめる。",
            count=len(bad), examples=[f"{rel}（中に書かれた名前 {names_text(decl)}）" for rel, decl in bad])


def _unresolved_item(rep: Report, rows: list, unresolved: dict) -> None:
    by_name: dict = {}
    for r in rows:
        by_name.setdefault(r["name"], []).append(r)
    names = unresolved.get("names") or {}
    if not names:
        rep.add(RULE, GOOD, "", "参照されているのにどこにも定義の無い名前は無かった", "参照はすべて定義につながる。", count=0)
        return
    examples = []
    for name in sorted(names):
        here = by_name.get(name)
        near = sorted({r["name"] for r in rows if name_rows.is_near(name, r["name"])})
        hint = (f"・ファイル名では見つかる: {here[0]['rel_path']}" if here
                else f"・番号だけが違う近い名前（確認していない候補）: {names_text(near, 3)}" if near else "")
        examples.append(f"{name}（参照元 {names[name][0]}{hint}）")
    more = unresolved.get("overflow") or 0
    rep.add(RULE, WARN, "", f"参照されているのにどこにも定義の無い名前が {len(names)} 件ある" + (f"（保存の上限で数えなかった参照が {more} 件ある）" if more else ""),
            "その参照はグラフでつながらない。影響の調査ではつながりを確かめられない範囲になる。番号だけが違う近い名前は、確認していない候補として示すだけで、同じものとは扱わない。",
            "参照先のソースが登録するフォルダに入っているか確かめる。名前だけがずれているなら、実行される名前とソースの対応を確かめる。",
            count=len(names), examples=examples)


def names_rules(rep: Report, rows: list | None, unresolved: dict | None) -> None:
    """名前の台帳の行 `rows` とグラフの未解決の申告 `unresolved`（`{names: {名前: [参照元]}, overflow}`・読めなければ None）から項目を足す。"""
    if rows is None:
        rep.not_counted = (*rep.not_counted, _NO_LEDGER)
        return
    _mismatch_item(rep, rows)
    if unresolved is None:
        rep.not_counted = (*rep.not_counted, _NO_UNRESOLVED)
        return
    _unresolved_item(rep, rows, unresolved)
