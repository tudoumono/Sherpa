"""点検の決まり 5（ビルドの定義の読み取りの結果）。最後に作ったグラフの報告を読むだけで、資料フォルダは歩かない。"""
from __future__ import annotations

from .model import BAD, GOOD, WARN, Report

_WHY = {"size_exceeded": "大きすぎる", "unreadable": "読めない", "encoding_undetermined": "文字コードが分からない",
        "no_pair": "実行名と材料の組を作れない形"}


def _unreadable_text(u: dict) -> str:
    name = f"（実行名 {u['name']}）" if u.get("name") else ""
    return f"{u.get('path')}: {_WHY.get(u.get('why'), u.get('why'))}{name}"


def rule5(rep: Report, report: dict | None, marks_sig: str) -> None:
    """読んだ数・つないだ実行名の数・読めなかったファイル・材料が解けないもの・定義の衝突・印の未反映を並べる。"""
    r = "決まり5"
    if report is None:
        rep.add(r, WARN, ".", "ビルドの定義を読んだ結果を確かめられない（グラフが無いか、読めない）",
                "ジョブやスクリプトから呼ばれる実行名は、ビルドの定義からはつながっていない可能性がある。",
                "「今すぐ更新」を行ってから、もう一度点検する。")
        return
    if (report.get("marks_sig") or "") != marks_sig:
        rep.add(r, WARN, ".", "管理画面の「ビルドの定義」の印が、まだグラフに効いていない",
                "今のグラフは前の印で作ったもののまま。", "「今すぐ更新」を行う（グラフだけを作り直す）。")
    if not report.get("read"):
        rep.add(r, WARN, ".", "ビルドの定義を 1 つも読まなかった",
                "呼び出しの名前とソースの名前が違う実行名は、つながらないまま。",
                "ビルドの定義をソースの組のフォルダ（src・source・sources・ソース・ソースコード）に置くか、管理画面で「読む場所にする」を付ける。", count=0)
    else:
        scopes = report.get("scopes") or {}
        rep.add(r, GOOD, ".", f"ビルドの定義を {report['read']} 個読み、実行名 {report.get('linked_names', 0)} 個を材料のソースとつないだ"
                              f"（行で {scopes.get('line', 0)}・まとまりで {scopes.get('block', 0)}・ファイル全体で {scopes.get('file', 0)}）",
                "呼び出し元から実行名を経て材料のソースまでたどれる。", count=report["read"])
    if report.get("unreadable_count"):
        rep.add(r, WARN, ".", f"読めなかったビルドの定義が {report['unreadable_count']} 件ある",
                "その実行名は、ビルドの定義からはつながらない。回答のときの検索で補う。",
                "読める形に書き直すか、関係の無いファイルなら管理画面で「読む場所にしない」を付ける。",
                count=report["unreadable_count"], examples=[_unreadable_text(u) for u in report.get("unreadable") or []])
    if report.get("input_unresolved_count"):
        rep.add(r, WARN, ".", f"ビルドの定義の材料のうち、グラフのソースに結べないものが {report['input_unresolved_count']} 件ある",
                "その材料からは実行名へたどれない（同じ名前のソースが同じ近さに複数ある、解析できない言語のソースなど）。",
                "材料をパスで書くと 1 つに決まる。解析できない言語は回答のときの検索で補う。",
                count=report["input_unresolved_count"],
                examples=[f"{u.get('path')}: {u.get('token')}" for u in report.get("input_unresolved") or []])
    if report.get("no_definition_count"):
        rep.add(r, WARN, ".", f"呼ばれているのにつながらず、読んだビルドの定義にも無い実行名が {report['no_definition_count']} 個ある",
                "ソースが資料フォルダに無いか、ビルドの定義を読めていない。",
                "ソースとビルドの定義が資料フォルダにあるかを確かめる。",
                count=report["no_definition_count"], examples=report.get("no_definition") or [])
    if report.get("conflict_count"):
        rep.add(r, BAD, ".", f"呼び出し元から同じ近さに、材料の違う同じ実行名の定義がある（{report['conflict_count']} 件）",
                "どちらか決められないので、その呼び出しはつながないまま。",
                "使わない定義を管理画面で「読む場所にしない」にするか、置き場を分ける。",
                count=report["conflict_count"],
                examples=[f"{c.get('name')}（{c.get('from')}）: {'・'.join(c.get('candidates') or [])}" for c in report.get("conflicts") or []])
    if report.get("dest_mismatch_count"):
        rep.add(r, WARN, ".", f"ビルドの定義と、宣言名でつながっていた行き先が合わない呼び出しが {report['dest_mismatch_count']} 件ある",
                "宣言名でつながった今の線を残し、実行名へは付け替えない。",
                "ビルドの定義の材料と、呼ばれるプログラムの宣言名が同じソースを指すか確かめる。",
                count=report["dest_mismatch_count"],
                examples=[f"{m.get('name')}（{m.get('from')}）: {m.get('definition')}" for m in report.get("dest_mismatch") or []])
