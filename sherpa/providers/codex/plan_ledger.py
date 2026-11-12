"""調査計画書（`.tmp/investigation/plan.md`）: 目録の節を読むパーサ・計画書から回答への整形・評価用の記録。
方式は `effective_style`（plan_main＝C-check／json＝JSON 台帳の A）。json のときこのモジュールは何もしない。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path

from ... import depth_profile as depth_profile_mod
from ... import tool_call_log
from .citations import _HEADING_RE as _REFS_HEADING_RE, _looks_like_ref, code_block_lines, last_heading_index
from .ledger_gate import _FINAL_ANSWER_VOICE

REVIEW_CAP_ENV = "SHERPA_LEDGER_REVIEW_CAP"
PLAN_FILE_NAME = "plan.md"
PLAN_REL_PATH = ".tmp/investigation/plan.md"

# 深さごとの見直しの上限（指示文で渡す）。
PLAN_REVIEW_CAPS = {"quick": 0, "standard": 1, "deep": 3, "max": 6}

_MARKS = {" ": "unstarted", "x": "done", "-": "closed"}
_ROW_RE = re.compile(r"^-\s*\[([ xX\-])\]\s*(.*)$")
_SUB_RE = re.compile(r"^\s+-\s*(.*)$")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_REASON_WORDS = ("範囲外", "読めない", "無い")
_REASON_PHRASES = {
    "範囲外": "範囲外のため確認できませんでした",
    "読めない": "読み取れませんでした",
    "無い": "登録範囲内では見つかりませんでした",
}
_UNSTARTED_PHRASE = "調べ終わっていません"
_NO_SOURCE_PHRASE = "ソースの根拠（パス:行）が記載されていないため未確認として扱います"
_TAG_RE = re.compile(r"\s*\[([^\[\]\s]+)\]")
_REF_RE = re.compile(r":\d+(?:-\d+)?(?:,\s*\d+(?:-\d+)?)*$")
_PATH_TOKEN_RE = re.compile(r"(?:[\w\-.]+/)*[\w\-.]+\.[A-Za-z][A-Za-z0-9]{0,15}")
_PLAN_ONLY_PATHS_MAX = 50


def effective_style(*, plain: bool, schema_level: int) -> str:
    """調査の方式。標準モード（OpenAI 系・Codex(Ollama)）で出力スキーマ v2 なら plan_main（C-check）、それ以外は json（A）。"""
    return "plan_main" if not plain and schema_level == 2 else "json"


def review_cap_for(profile) -> int:
    """深さごとの見直しの上限。評価用の環境変数 `SHERPA_LEDGER_REVIEW_CAP`（0〜6）があればそれを使う。"""
    raw = os.environ.get(REVIEW_CAP_ENV, "").strip()
    if raw.isdigit() and 0 <= int(raw) <= max(PLAN_REVIEW_CAPS.values()):
        return int(raw)
    return PLAN_REVIEW_CAPS[depth_profile_mod.normalize_depth_profile(profile)]


@dataclass(frozen=True)
class PlanRow:
    id: str
    mark: str  # " " 未着手 / "x" 済 / "-" 打ち切り
    kind: str
    subject: str
    needs: tuple[str, ...]
    subs: tuple[str, ...]
    malformed: bool = False
    stray: bool = False  # `- [ ]` 形式でない本文の行（未着手の行ではない）

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(ref for k, v in (_split_sub(s) for s in self.subs) if k == "source"
                     for ref in _split_refs(v))

    @property
    def reason(self) -> tuple[str, str] | None:
        for s in self.subs:
            k, v = _split_sub(s)
            if k in _REASON_WORDS and v:
                return k, v
        return None


@dataclass(frozen=True)
class Plan:
    has_population: bool = False
    rows: tuple[PlanRow, ...] = ()
    perspectives: tuple[str, ...] = ()
    sections: dict = field(default_factory=dict)
    text: str = ""
    normalizations: list = field(default_factory=list)  # 表記を正規化した行 {original, normalized, kind}

    @property
    def unstarted(self) -> tuple[PlanRow, ...]:
        return tuple(r for r in self.rows if r.mark == " " or r.malformed)


def _split_refs(value: str) -> list[str]:
    """`source` の値をセミコロン（; ；）で分け、`パス:行`（行は 12・12-34・カンマ区切りの複数）として読めた参照だけを返す。"""
    return [ref for ref in (seg.strip() for seg in re.split(r"[;；]", value))
            if _REF_RE.search(ref.strip("`")) and _path_of(ref)]


def _split_sub(s: str) -> tuple[str, str]:
    m = re.match(r"^([^:：]+)[:：]\s*(.*)$", s.strip())
    return (m.group(1).strip(), m.group(2).strip()) if m else ("", s.strip())


_KIND_UNFILLED = "（未記入）"
_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_BRACKET_ID_RE = re.compile(r"^\[([A-Za-z0-9_-]+)\]$")
_NEEDS_RE = re.compile(r"^要\s*[:：]\s*\S")


def _normalize_fields(raw: str) -> tuple[list[str], list[str]]:
    """行の欄を `·` で分け、意味が変わらない表記だけ直す（id の角括弧を外す・種別の欄が無い 3 欄の行に固定の印を補う）。戻り値＝（欄, 直した種類）。"""
    fields = [f.strip() for f in raw.split("·")]
    kinds: list[str] = []
    if fields:
        m = _BRACKET_ID_RE.match(fields[0])
        if m:
            fields[0] = m.group(1)
            kinds.append("id_brackets")
    if len(fields) == 3 and _ID_RE.fullmatch(fields[0]) and fields[1] and _NEEDS_RE.match(fields[2]):
        fields.insert(1, _KIND_UNFILLED)
        kinds.append("kind_missing")
    return fields, kinds


def _parse_row(m: re.Match, subs: list[str], normalizations: list | None = None) -> PlanRow:
    mark = m.group(1).lower() if m.group(1) != " " else " "
    fields, kinds = _normalize_fields(m.group(2))
    if kinds and normalizations is not None:
        normalizations.append({"original": m.group(0).strip(), "kinds": kinds,
                               "normalized": f"- [{mark}] " + " · ".join(fields)})
    ok = (len(fields) == 4 and all(fields[:3]) and _ID_RE.fullmatch(fields[0])
          and _NEEDS_RE.match(fields[3]))
    needs = tuple(t.strip() for t in re.split(r"[,、，]", re.sub(r"^要\s*[:：]\s*", "", fields[3]))
                  if t.strip()) if len(fields) == 4 else ()
    row = PlanRow(id=fields[0] if fields else "", mark=mark,
                  kind=fields[1] if len(fields) > 1 else "",
                  subject=fields[2] if len(fields) > 2 else "", needs=needs,
                  subs=tuple(subs), malformed=not bool(ok))
    return replace(row, malformed=True) if mark == "-" and row.reason is None else row


def split_sections(text: str) -> dict:
    """`## 見出し` ごとの本文を、見出しの名前をキーにして読む。"""
    sections: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h:
            cur = h.group(1)
            sections.setdefault(cur, [])
        elif cur is not None:
            sections[cur].append(line)
    return {k: "\n".join(v).strip("\n") for k, v in sections.items()}


def _section(sections: dict, name: str) -> str | None:
    return sections.get(name)


def parse_plan(text: str | None) -> Plan:
    """計画書の本文を読む。目録（母集団）の節だけ文法を固定し、他は自由文として節ごとに保持する。崩れた行は `malformed` の行として残す。"""
    if not text:
        return Plan()
    sections = split_sections(text)
    pop = _section(sections, "母集団")
    rows: list[PlanRow] = []
    normalizations: list[dict] = []
    if pop is not None:
        cur_m: re.Match | None = None
        cur_subs: list[str] = []
        for line in pop.splitlines():
            m = _ROW_RE.match(line)
            if m:
                if cur_m is not None:
                    rows.append(_parse_row(cur_m, cur_subs, normalizations))
                cur_m, cur_subs = m, []
                continue
            s = _SUB_RE.match(line)
            if s and cur_m is not None:
                cur_subs.append(s.group(1).strip())
            elif line.strip():
                if cur_m is not None:
                    rows.append(_parse_row(cur_m, cur_subs, normalizations))
                    cur_m, cur_subs = None, []
                rows.append(PlanRow("", " ", "", line.strip(), (), (), malformed=True, stray=True))
        if cur_m is not None:
            rows.append(_parse_row(cur_m, cur_subs, normalizations))
    counts = {}
    for row in rows:
        counts[row.id] = counts.get(row.id, 0) + 1
    rows = [replace(r, malformed=True) if r.id and counts[r.id] > 1 else r for r in rows]
    persp_text = _section(sections, "観点") or ""
    persp = tuple(re.sub(r"^-\s*", "", ln.strip()) for ln in persp_text.splitlines()
                  if ln.strip("- ").strip())
    return Plan(has_population=pop is not None, rows=tuple(rows), perspectives=persp, sections=sections, text=text,
                normalizations=normalizations)


def read_plan(investigation_dir: Path) -> Plan | None:
    """`plan.md` を読む。無い・symlink・読めないときは None。"""
    try:
        parent = os.open(investigation_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            directory = os.open(investigation_dir.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            fd = os.open(PLAN_FILE_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            text = stream.read(2_000_001)
            return parse_plan(text) if len(text) <= 2_000_000 else None
    except OSError:
        return None


# ---- 計画書 → 回答 ----

def unconfirmed_items(plan: Plan) -> list[dict]:
    """未確認の一覧（`{"item","reason"}`）。未着手・打ち切り・済でも「要: source」に `- source:` が無い行。理由行の文は転記する（言い換えない）。"""
    return _unconfirmed_rows(plan.rows)


def _unconfirmed_rows(rows) -> list[dict]:
    """渡した行のうち未確認のものを `{"item","reason"}` にする。"""
    out = []
    for r in rows:
        name = r.subject or r.id
        if r.malformed:
            out.append({"item": name, "id": r.id, "reason": "記載の形が読み取れず、確かめられませんでした"})
        elif r.mark == " ":
            out.append({"item": name, "id": r.id, "reason": _UNSTARTED_PHRASE})
        elif r.mark == "-":
            rs = r.reason
            if rs is None:
                out.append({"item": name, "id": r.id, "reason": "確認できませんでした"})
            else:
                word, detail = rs
                phrase = _REASON_PHRASES[word]
                out.append({"item": name, "id": r.id, "reason": f"{phrase}: {detail}" if detail else phrase})
        elif r.mark == "x" and "source" in r.needs and not r.sources:
            out.append({"item": name, "id": r.id, "reason": _NO_SOURCE_PHRASE})
    return out


def _path_of(ref: str) -> str:
    ref = ref.strip().strip("`")
    return _REF_RE.sub("", ref).strip().strip("`").strip()


def _extract_refs(body: str) -> list[str]:
    """回答本文の『参照した資料:』以降の行からパスを取り出す。"""
    idx = body.rfind("参照した資料")
    if idx < 0:
        return []
    refs = []
    for ln in body[idx:].splitlines()[1:]:
        ln = re.sub(r"^[\s\-*・]+", "", ln).strip().strip("`")
        if ln:
            refs.append(_path_of(ln))
    return refs


def strip_refs_block(body: str) -> str:
    idx = body.rfind("参照した資料")
    return body[:idx].rstrip() if idx >= 0 else body.rstrip()


_CODE_SPAN_RE = re.compile(r"(?P<t>`+)(?:(?!(?P=t)).)+?(?P=t)")
_ID_CHAR_RE = re.compile(r"[A-Za-z0-9_]")
_LINK_DEF_RE = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*\S")
_LINK_DEF_LABEL_RE = re.compile(r"^(?: {0,3}>)*(?: ?>)* {0,3}\[([^\]\n]+)\]:[ \t]*\S", re.M)
_QUOTE_PREFIX_RE = re.compile(r"^ {0,3}>(?: ?>)* ?")
_TASK_BOX_RE = re.compile(r"^\s*(?:>\s*)*(?:[-*+]|\d{1,9}[.)])\s+\[[ xX]\](?=\s|$)")


def _strip_tags_in_text(text: str, ids: set[str]) -> str:
    """コードの外の文字から、独立した引用の印として付いた id の `[id]`（`[A1][A2]` の連続を含む）だけを除く。
    直前が英数字・`_`・`]`・`)` の `[…]`（添字・参照。数字だけの id は日本語の文字も含む）と、直後が `(`・`[`・英数字の `[…]`（リンク）は残す。"""
    runs: list[list[re.Match]] = []
    for m in _TAG_RE.finditer(text):
        if m.group(1) not in ids:
            continue
        if runs and runs[-1][-1].end() == m.start():
            runs[-1].append(m)
        else:
            runs.append([m])
    out, pos = [], 0
    for run in runs:
        first, last = run[0], run[-1]
        tag_start = first.start(1) - 1
        before = text[tag_start - 1] if tag_start > 0 else ""
        after = text[last.end()] if last.end() < len(text) else ""
        # 数字だけの id は添字（`値[0]`）と区別できないので、日本語を含む文字の直後も外さない。英字を含む id は日本語の直後なら外す
        numeric = first.group(1).isdigit()
        word_char = (before.isalnum() or before == "_") if numeric else bool(_ID_CHAR_RE.match(before))
        glued_before = not first.group(0)[:1].isspace() and before and (word_char or before in "])")
        glued_after = after and (_ID_CHAR_RE.match(after) or after in "([")
        if glued_before or glued_after:
            continue
        out.append(text[pos:first.start()])
        pos = last.end()
    out.append(text[pos:])
    return "".join(out)


def _strip_id_tags(text: str, ids: set[str]) -> str:
    """回答の文に添えた根拠の行の id（`[id]`）だけを除く。id 以外の角括弧、コードスパン・コードブロック（字下げのものを含む）の中、添字やリンクに隣接する角括弧、行頭のチェックボックス（`- [x]`）・リンクの定義（`[a]: URL`）と、定義のある名前の `[a]`（リンク）は残す。"""
    lines = text.splitlines(keepends=True)
    codes = code_block_lines([ln.rstrip("\r\n") for ln in lines])
    # リンクの定義はコードブロックの外のものだけを数える
    defined = {m.group(1).casefold() for ln, code in zip(lines, codes) if not code
               for m in _LINK_DEF_LABEL_RE.finditer(ln)}
    ids = {i for i in ids if i.casefold() not in defined}
    out: list[str] = []
    for line, code in zip(lines, codes):
        # コードブロック（囲み・字下げ・引用や箇条書きの中）の行は触らない
        if code:
            out.append(line)
            continue
        quote = _QUOTE_PREFIX_RE.match(line)
        inner = line[quote.end():] if quote else line
        if _LINK_DEF_RE.match(inner):
            out.append(line)
            continue
        box = _TASK_BOX_RE.match(line)
        head, line = (line[:box.end()], line[box.end():]) if box else ("", line)
        parts, pos = [head], 0
        for c in _CODE_SPAN_RE.finditer(line):
            parts.append(_strip_tags_in_text(line[pos:c.start()], ids))
            parts.append(c.group(0))
            pos = c.end()
        parts.append(_strip_tags_in_text(line[pos:], ids))
        out.append("".join(parts))
    return "".join(out)


def _conclusion_line(line: str, ids: set[str], unknown: set[str]) -> str:
    """結論の主張行に既知の確認済み id が無ければ、本文を変えず未確認の印を付ける。"""
    tags = {m.group(1) for m in _TAG_RE.finditer(line)}
    formatting = (not line.strip()
                  or re.match(r"^\s{0,3}(?:#{1,6}(?:\s|$)|`{3,}|~{3,})", line)
                  or re.fullmatch(r"\s*(?:[-*_]\s*){3,}", line))
    prefix = "未確認: " if not formatting and (not tags.intersection(ids) or tags.intersection(unknown)) else ""
    return prefix + _strip_id_tags(line, ids)


def plan_gaps(plan: Plan) -> tuple[list[dict], list[dict]]:
    """計画書の目録の未完了を2種に分ける。戻り値＝（計画書の記載の不備, 確認できなかった項目）。各要素は `{"item","id","reason"}`。
    不備＝書式不正の行・済なのに読める source 行が無い行。確認できなかった＝未着手・打ち切り（範囲外・読めない・無い）。"""
    fmt, unverified = [], []
    for r in plan.rows:
        name = r.subject or r.id
        if r.malformed:
            fmt.append({"item": name, "id": r.id, "reason": "行の書式または打ち切りの理由が読み取れません"})
        elif r.mark == " ":
            unverified.append({"item": name, "id": r.id, "reason": _UNSTARTED_PHRASE})
        elif r.mark == "-":
            word, detail = r.reason
            phrase = _REASON_PHRASES[word]
            unverified.append({"item": name, "id": r.id, "reason": f"{phrase}: {detail}" if detail else phrase})
        elif r.mark == "x" and "source" in r.needs and not r.sources:
            fmt.append({"item": name, "id": r.id, "reason": "済の印ですが、根拠の行（パス:行）が読み取れません"})
    return fmt, unverified


def _split_ref_block(answer: str) -> tuple[str, list[str]]:
    """回答を（参照ブロックを除いた本文, ブロックの行）に分ける。ブロックの範囲は `citations.parse_referenced_doc_lines` と同じ（最後の見出し・続く参照らしい行まで）。ブロックが無ければ（本文, [])。"""
    lines = answer.splitlines()
    idx = last_heading_index(lines, _REFS_HEADING_RE)
    if idx is None:
        return answer.rstrip(), []
    j = idx + 1
    if not (_REFS_HEADING_RE.match(lines[idx]).group(1) or "").strip():
        while j < len(lines) and not lines[j].strip():
            j += 1
    while j < len(lines) and lines[j].strip() and _looks_like_ref(lines[j]):
        j += 1
    head = lines[:idx]
    while head and not head[-1].strip():
        head.pop()
    tail = lines[j:]
    while tail and not tail[0].strip():
        tail.pop(0)
    body = "\n".join(head + ([""] + tail if head and tail else tail))
    return body.rstrip(), lines[idx:j]


def _block_ranges(block: list[str]) -> dict[str, set[str]]:
    """参照ブロックの各行を（パス → 行範囲の集合）にする。範囲の無い行は空集合。"""
    out: dict[str, set[str]] = {}
    for n, ln in enumerate(block):
        if n == 0:
            ln = _REFS_HEADING_RE.match(ln).group(1) or ""
        ln = re.sub(r"^[\s\-*・]+", "", ln).strip().strip("`")
        if not ln:
            continue
        m = _REF_RE.search(ln)
        ranges = set(re.sub(r"\s+", "", m.group(0)[1:]).split(",")) if m else set()
        out.setdefault(_path_of(ln), set()).update(ranges)
    return out


def _plan_refs(plan: Plan) -> dict[str, list[str]]:
    """計画書の source 行の参照を（パス → 行範囲の並び）にまとめる。"""
    refs: dict[str, list[str]] = {}
    for r in plan.rows:
        for s in r.sources:
            m = _REF_RE.search(s.strip("`"))
            lines = refs.setdefault(_path_of(s), [])
            for rng in re.sub(r"\s+", "", m.group(0)[1:]).split(","):
                if rng not in lines:
                    lines.append(rng)
    return refs


def _plan_only_paths(plan: Plan, refs: dict[str, list[str]], model_answer: str) -> list[str]:
    """計画書の結論・source 行に出てくるパスのうち、モデルの回答（参照ブロックを含む）に無いもの。"""
    found = list(refs) + _PATH_TOKEN_RE.findall((_section(plan.sections, "結論") or ""))
    out: list[str] = []
    for p in found:
        if p and p not in model_answer and p not in out:
            out.append(p)
    return out[:_PLAN_ONLY_PATHS_MAX]


def conclusion_text(st) -> str:
    """このターンの計画書の結論の節（無ければ空）。"""
    plan = getattr(st, "_plan_final", None)
    return (_section(plan.sections, "結論") or "") if plan is not None else ""


def compose_with_model_answer(plan: Plan, model_answer: str) -> tuple[str, dict]:
    """モデルの回答を本文にそのまま残し、計画書の source 行にだけある参照を「参照した資料:」に追記する。
    順序は 本文 → 「参照した資料:」（モデルの参照行 → 計画書にだけある参照）。戻り値＝（組んだ本文, 記録用の要約）。
    設計: docs/design/codex.md「評価用の計画書では誰が完了を決めるか」"""
    refs = _plan_refs(plan)
    fmt, unverified = plan_gaps(plan)
    body, block = _split_ref_block(model_answer)
    known = _block_ranges(block)
    added = []
    for p, ranges in refs.items():
        new = [x for x in ranges if x not in known.get(p, set())]
        if new:
            added.append(f"- {p}:{', '.join(new)}")
    parts = [body]
    if block or added:
        parts.append("\n".join(block or ["参照した資料:"]) + ("\n" + "\n".join(added) if added else ""))
    parts = [x for x in parts if x]
    info = {"format_issues": [{"id": x["id"], "reason": x["reason"]} for x in fmt],
            "unverified_items": [{"id": x["id"], "reason": x["reason"]} for x in unverified],
            "plan_only_paths": _plan_only_paths(plan, refs, model_answer),
            "refs_added_from_plan": len(added)}
    return "\n\n".join(parts), info


def compose_answer(plan: Plan, fallback_body: str = "") -> str:
    """計画書の結論を回答にする（結論 → 参照した資料。モデルの回答が空のときだけ）。機械的な並べ替えと id の除去だけで、文章は要約も言い換えもしない。
    確認の状況（件数・確認できなかった項目）は本文に入れない（`turn_report` から回答の補足の欄と調査の記録に出る）。"""
    ids = {r.id for r in plan.rows if r.id}
    conclusion = (_section(plan.sections, "結論") or "").strip()
    if not conclusion:
        conclusion = strip_refs_block(fallback_body or "")
    unknown = {u["id"] for u in unconfirmed_items(plan)}
    conclusion = "\n".join(_conclusion_line(line, ids, unknown) for line in conclusion.splitlines()).strip()
    parts = [conclusion] if conclusion else []
    refs: dict[str, list[str]] = {}
    for r in plan.rows:
        for s in r.sources:
            m = _REF_RE.search(s.strip("`"))
            lines = refs.setdefault(_path_of(s), [])
            for rng in re.sub(r"\s+", "", m.group(0)[1:]).split(","):
                if rng not in lines:
                    lines.append(rng)
    for p in _extract_refs(fallback_body or ""):
        if p:
            refs.setdefault(p, [])
    if refs:
        parts.append("参照した資料:\n" + "\n".join(
            f"- {p}:{', '.join(lines)}" if lines else f"- {p}" for p, lines in refs.items()))
    return "\n\n".join(parts)


# ---- グラフの結果（評価用の記録） ----

def graph_impact_page_results(st) -> list[dict]:
    """このターンの graph_impact の呼び出しごとの結果（ページ送りの 1 ページ 1 件・`tool_call_log.graph_view` ＋ call_id・tool・role）。記録のファイルは消さない。読めなければ空。"""
    try:
        merged = st.call_log
        if merged is None:
            merged = tool_call_log.merge_call_logs(st._call_log_dir, st.turn_uid)
            tool_call_log.merge_graph_results(st._call_log_dir, st.turn_uid, merged)
        return [g for g in merged.graph if g.get("tool") == "graph_impact"]
    except Exception:
        return []


def graph_impact_results(st) -> list[dict]:
    """このターンの graph_impact の結果。同じ結果（同じ起点・条件・世代）の複数ページは 1 つに合わせる（`tool_call_log.merge_impact_pages`）。"""
    return tool_call_log.merge_impact_pages(graph_impact_page_results(st))


# ---- ターンの仕上げ・評価用の記録 ----

EVAL_DIR_ENV = "SHERPA_LEDGER_EVAL_DIR"


def compose_turn_answer(st) -> None:
    """計画書方式のターンで、モデルの回答を本文にそのまま残し、計画書にだけある参照を追記する（モデルの回答が空のときだけ計画書の結論を本文にする。計画書に目録の行が無いときは何もしない）。"""
    stats = {"applied": False, "body_source": "unchanged", "plan_present": False, "plan_rows": 0,
             "format_issues": [], "unverified_items": [], "plan_only_paths": [],
             "refs_added_from_plan": 0}
    st._compose_stats = stats
    if st._ledger_style == "json" or st._investigation_dir is None:
        return
    plan = read_plan(st._investigation_dir)
    st._plan_final = plan
    stats["plan_present"] = plan is not None
    if plan is None or not plan.rows:
        return
    stats["plan_rows"] = len(plan.rows)
    model_answer = (st.answer or "").strip()
    if model_answer:
        st.answer, info = compose_with_model_answer(plan, model_answer)
        stats.update(info, applied=True, body_source="model_answer")
        return
    fmt, unverified = plan_gaps(plan)
    st.answer = compose_answer(plan, "")
    stats.update(applied=True, body_source="plan_conclusion_fallback",
                 format_issues=[{"id": x["id"], "reason": x["reason"]} for x in fmt],
                 unverified_items=[{"id": x["id"], "reason": x["reason"]} for x in unverified])


def turn_report(st) -> dict:
    """`env["investigation"]` に載せる計画書の要約（本文は載せない）。"""
    plan = st._plan_final if st._plan_final is not None else (read_plan(st._investigation_dir) if st._investigation_dir else None)
    marks = {"unstarted": 0, "done": 0, "closed": 0}
    if plan is not None:
        for r in plan.rows:
            marks["unstarted" if r.malformed else _MARKS[r.mark]] += 1
    review_text = (_section(plan.sections, "見直し") or "") if plan is not None else ""
    unconfirmed = unconfirmed_items(plan) if plan is not None else []
    fmt, _unverified = plan_gaps(plan) if plan is not None else ([], [])
    n_unstarted = sum(1 for r in plan.rows if r.mark == " " and not r.malformed) if plan is not None else 0
    # 回答に出す件数と一覧の材料。目録の行だけを数える（本文の stray 行は目録の行ではない）
    listed_rows = [r for r in plan.rows if not r.stray] if plan is not None else []
    listed = _unconfirmed_rows(listed_rows)
    n_total = len(listed_rows)
    return {
        "ledger": "plan", "style": st._ledger_style, "plan_present": plan is not None,
        "rows": len(plan.rows) if plan is not None else 0, "marks": marks,
        "plan_counts": {"total": n_total, "confirmed": n_total - len(listed),
                        "unconfirmed": len(listed) - n_unstarted, "unstarted": n_unstarted},
        "plan_unverified_items": listed,
        "plan_format_issues": fmt,
        "unconfirmed_items": unconfirmed,
        "plan_review_lines": sum(1 for ln in review_text.splitlines() if ln.strip().startswith("-")),
        "stopped_reason": st._investigation_stopped_reason,
        "malformed_rows": sum(r.malformed for r in plan.rows) if plan else 0,
        "normalizations": list(plan.normalizations) if plan is not None else [],
    }


def finish_report(st, ctx, env) -> None:
    """終了状態を計画書の要約と評価用の記録へ反映する。"""
    if st._ledger_style != "json":
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            reason = "user_stop"
        elif st._wall_clock_state["hit"]:
            reason = "wall_clock"
        elif st.codex_question is not None:
            reason = "ask_user"
        elif st._turn_failed or st._stream_error or st.attempt_returncode not in (0, None):
            reason = "failed"
        elif st._codex_stopped_early:
            reason = "auto_continue_cap"
        else:
            reason = "self_final"
        st._investigation_stopped_reason = reason
        env["investigation"] = {**(env.get("investigation") or {}), **turn_report(st),
                                "stopped_reason": reason, "resume_count": st._resume_count,
                                "attempts": st._attempt_no, "auto_continue_count": st._auto_continue_count}
    dump_eval_record(st, ctx)


def dump_review_candidate(st, ctx, k: int, text: str) -> None:
    """評価用: 見直しを頼む直前の回答の候補の本文を `conv-<id>.review-<k>.md` に書く。失敗は握りつぶす。"""
    d = os.environ.get(EVAL_DIR_ENV, "").strip()
    if not d or ctx.conversation_id is None:
        return
    try:
        out = Path(d)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"conv-{ctx.conversation_id}.review-{k}.md").write_text(text or "", encoding="utf-8")
    except Exception:
        pass


def _graph_stats(st) -> dict:
    rows = graph_impact_results(st)
    return {"impact_calls": len(rows),   # ページ送りは 1 つの結果として数える（未完了の率の分母と揃える）
            "impact_incomplete": sum(1 for g in rows if g.get("complete") is False),
            "impact_depth_truncated": sum(1 for g in rows if g.get("depth_truncated") is True)}


def _gate_stats(plan: Plan | None, graph: dict) -> dict:
    closed = []
    for r in (plan.rows if plan is not None else ()):
        if r.mark == "-":
            word, detail = r.reason or (None, "")
            closed.append({"id": r.id, "reason": word, "detail": detail})
    return {"accepted_with_graph_incomplete": bool(graph["impact_incomplete"] and closed),
            "closed_rows": closed}


def _candidate_stats(st) -> dict:
    """評価用: 受理境界以降の final の数と、本文に使った候補の位置・選んだ理由・見直し前への切り戻しの有無。"""
    pick = st._candidate_pick or {"picked_index": None, "reason": "none"}
    finals = [a for a in st._structured_answers[st._structured_answers_valid_from:] if a.get("status") == "final"]
    return {"finals_total": len(finals), "valid_from": st._structured_answers_valid_from,
            "picked_index": pick["picked_index"], "reason": pick["reason"],
            "reverted_review": bool(st._ledger_review_reverted)}


def _compose_stats(st) -> dict:
    return st._compose_stats or {"applied": False, "body_source": "unchanged", "plan_present": False,
                                 "plan_rows": 0, "format_issues": [], "unverified_items": [],
                                 "plan_only_paths": [], "refs_added_from_plan": 0}


def _review_stats(st) -> dict:
    """評価用: 見直しを Sherpa が数えて止めたか（A）、指示文の数字だけか（C）。"""
    if st._ledger_style == "json":
        return {"app_gating": True, "app_review_cap": st._plan_review_cap,
                "app_review_requests": st._ledger_review_requests, "instruction_review_cap": None}
    return {"app_gating": False, "app_review_cap": None, "app_review_requests": 0,
            "instruction_review_cap": st._plan_review_cap}


def dump_eval_record(st, ctx) -> None:
    """評価用: 環境変数 `SHERPA_LEDGER_EVAL_DIR` があれば、会話ごとの計画書と挙動の数字を書く。失敗は握りつぶす。"""
    d = os.environ.get(EVAL_DIR_ENV, "").strip()
    if not d or ctx.conversation_id is None:
        return
    import json
    from ... import investigation_ledger
    try:
        out = Path(d)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"conv-{ctx.conversation_id}"
        plan = st._plan_final or (read_plan(st._investigation_dir) if st._ledger_style != "json"
                                 and st._investigation_dir is not None else None)
        if plan is not None:
            (out / f"{stem}.plan.md").write_text(plan.text, encoding="utf-8")
        else:
            (out / f"{stem}.plan.md").unlink(missing_ok=True)
        reviews_asked = st._ledger_review_requests if st._ledger_style == "json" else 0
        for old in out.glob(f"{stem}.review-*.md"):
            k = old.name[len(stem) + len(".review-"):-len(".md")]
            if not k.isdigit() or int(k) > reviews_asked:
                old.unlink(missing_ok=True)
        graph = _graph_stats(st)
        for name, text in (("adopted", st._adopted_answer), ("composed", st._composed_answer)):
            (out / f"{stem}.{name}.md").write_text(text or "", encoding="utf-8")
        stats = {
            "style": st._ledger_style, "auto_continue_count": st._auto_continue_count,
            "attempts": st._attempt_no, "resume_count": st._resume_count, "resume_fallback": bool(st._resume_fallback_happened),
            "ledger_continuations": st._ledger_continuations,
            "ledger_review_requests": st._ledger_review_requests,
            "json_review_entries": len(investigation_ledger.load_reviews(st._investigation_dir))
            if st._ledger_style == "json" and st._investigation_dir is not None else 0,
            "json_population_count": len(st._investigation_snapshot.manifest["items"])
            if st._investigation_snapshot and st._investigation_snapshot.manifest is not None else None,
            "json_stopped_reason": st._investigation_stopped_reason,
            "stopped_reason": st._investigation_stopped_reason,
            "rows": len(st._plan_final.rows) if st._plan_final else (
                len(st._investigation_snapshot.items) if st._investigation_snapshot else 0),
            "tool_zero_nudged": bool(st._tool_zero_nudged), "multi_agent": bool(st._multi_agent_enabled),
            "codex_stopped_early": bool(st._codex_stopped_early),
            "plan": turn_report(st) if st._ledger_style != "json" else None,
            "review_cap": st._plan_review_cap, "graph": graph,
            "gate": _gate_stats(plan, graph) if st._ledger_style != "json" else None,
            "plan_normalizations": list(plan.normalizations) if plan is not None else [],
            "candidate": _candidate_stats(st), "compose": _compose_stats(st),
            "review": _review_stats(st), "composed_taken": "before_preamble_split",
            "trimmed": [{"kind": t.get("kind"), "chars": len(t.get("text") or "")} for t in st._trimmed],
        }
        (out / f"{stem}.stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    except Exception:
        pass
