"""点検の報告の書き出し（利用者向けの Markdown・機械向けの JSON）。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .model import GOOD, RESULTS, RULES, WARN, BAD, Report, label, where


def summarize(items: list) -> dict:
    """決まりごとの 合う・注意・合わない の件数。"""
    out = {name: {x: 0 for x in RESULTS} for name, _ in RULES}
    for it in items:
        out[it.rule][it.result] += 1
    return out


def to_json(report: Report, root_name: str) -> dict:
    return {"root": root_name, "summary": summarize(report.items), "items": [asdict(it) for it in report.items],
            "not_counted": list(report.not_counted)}


def json_text(report: Report, root_name: str) -> str:
    return json.dumps(to_json(report, root_name), ensure_ascii=False, indent=2) + "\n"


def to_markdown(report: Report, root_name: str) -> str:
    items = report.items
    summary = summarize(items)
    intro = ("登録するフォルダを Sherpa がどう読むかの点検です。結果は「合う」（決まりどおり）・「注意」（仮定つきの答えになる、または確かめてほしい）・"
             "「合わない」（その範囲は期として読めず、「確かめられない」が増える。使えなくなることはない）の 3 段です。")
    lines = [f"# フォルダの点検報告（{root_name}）", "", intro, "", "| 決まり | 合う | 注意 | 合わない |", "|---|---:|---:|---:|"]
    for name, title in RULES:
        s = summary[name]
        lines.append(f"| {label(name, title)} | {s[GOOD]} | {s[WARN]} | {s[BAD]} |")
    for name, title in RULES:
        lines += ["", f"## {label(name, title)}", ""]
        mine = sorted((it for it in items if it.rule == name), key=lambda it: RESULTS[::-1].index(it.result))
        if not mine:
            lines.append("該当する項目は無い。")
        for it in mine:
            lines.append(f"- **{it.result}** `{where(it.path)}` — {it.what}")
            lines.append(f"  - Sherpa の扱い: {it.treatment}")
            if it.fix:
                lines.append(f"  - 直し方: {it.fix}")
            if it.examples:
                more = f"（全 {it.count} 件、上限まで）" if it.count is not None and it.count > len(it.examples) else ""
                lines.append(f"  - 例{more}:")
                lines += [f"    - `{e}`" for e in it.examples]
    if report.not_counted:
        lines += ["", "## 数えていないもの", ""] + [f"- {t}" for t in report.not_counted]
    return "\n".join(lines) + "\n"


def write_markdown(report: Report, root_name: str, path) -> None:
    Path(path).write_text(to_markdown(report, root_name), encoding="utf-8", newline="\n")


def write_json(report: Report, root_name: str, path) -> None:
    Path(path).write_text(json_text(report, root_name), encoding="utf-8", newline="\n")
