"""評価用の調査計画書（`.tmp/investigation/plan.md`）: 目録の節を読むパーサ・Sherpa 判定型（plan_gate）の受理判定・計画書から回答への整形。
切り替えは環境変数 `SHERPA_LEDGER_STYLE`（plan_main＝既定の C-check／json＝JSON 台帳の A／plan_gate＝B）。json のときこのモジュールは何もしない。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import os
import re
import stat
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from ... import depth_profile as depth_profile_mod
from ... import tool_call_log
from .citations import _HEADING_RE as _REFS_HEADING_RE, _looks_like_ref
from .ledger_gate import _FINAL_ANSWER_VOICE

STYLE_ENV = "SHERPA_LEDGER_STYLE"
REVIEW_CAP_ENV = "SHERPA_LEDGER_REVIEW_CAP"
GATE_DRYRUN_ENV = "SHERPA_LEDGER_GATE_DRYRUN"
STYLES = ("json", "plan_gate", "plan_main")
# 既定の調査の方式（C-check）。
DEFAULT_STYLE = "plan_main"
PLAN_FILE_NAME = "plan.md"
PLAN_REL_PATH = ".tmp/investigation/plan.md"

# 深さごとの見直しの上限（plan_gate は Sherpa が数え、plan_main は指示文で渡す）。
PLAN_REVIEW_CAPS = {"quick": 0, "standard": 1, "deep": 3, "max": 6}
# 差し戻しの継続の総数の上限（json の台帳継続と同じ値）。
PLAN_CONTINUE_CAP = 10
_MISSING_RETRY_CAP = 2

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
_PATH_TOKEN_RE = re.compile(r"(?:[\w\-.]+/)*[\w\-.]+\.[A-Za-z][A-Za-z0-9]{0,5}")
_PLAN_ONLY_PATHS_MAX = 50


def ledger_style() -> str:
    """`SHERPA_LEDGER_STYLE` の実効値。未設定・想定外は plan_main（C-check）。json（A）・plan_gate（B）は指定したときだけ。"""
    v = os.environ.get(STYLE_ENV, "").strip().lower()
    return v if v in STYLES else DEFAULT_STYLE


def effective_style(*, plain: bool, ollama: bool, schema_level: int) -> str:
    """計画書の評価方式は標準・OpenAI 系・構造化 v2 だけで使う。"""
    return ledger_style() if not plain and not ollama and schema_level == 2 else "json"


def gate_dry_run() -> bool:
    """評価専用: `SHERPA_LEDGER_GATE_DRYRUN=1` のとき、plan_gate の判定を記録するだけで差し戻さない。"""
    return os.environ.get(GATE_DRYRUN_ENV, "").strip() == "1"


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


def _signature(plan: Plan | None) -> tuple:
    if plan is None:
        return ()
    return (tuple(sorted((r.id, r.mark, r.kind, r.subject, r.needs, r.subs, r.malformed) for r in plan.rows)),
            tuple(sorted(plan.perspectives)))


def progressed(prev: Plan | None, curr: Plan) -> bool:
    """前回 final 時から目録の内容・状態・根拠・観点が変わったか。"""
    return _signature(prev) != _signature(curr)


def _row_keys(plan: Plan) -> frozenset:
    return frozenset((r.id, r.kind, r.subject) for r in plan.rows)


# ---- 受理判定（plan_gate） ----

_MISSING_PROMPT = (
    f"調査計画書 `{PLAN_REL_PATH}` の「## 母集団」節に目録の行が見つかりません。"
    "計画書を apply_patch で作る・直してください。母集団の各要素を "
    "`- [ ] <id> · <種別> · <対象> · 要: source` の形で書き、調べ終えたら `- [x]`"
    "（`- source: パス:行` の行を添える）か `- [-]`（`- 範囲外:`／`- 読めない:`／`- 無い:` の理由行を添える）に更新してから、"
    "最終回答を返してください。台帳のツールは使わないでください。"
    + _FINAL_ANSWER_VOICE
)
_REVIEW_PROMPT = (
    "回答を確定する前の見直しです（調べ方を変える指示ではありません）。"
    "計画書の「## 分かったこと」を読み返し、「## 母集団」にも「## 観点」にもまだ入っていない要素"
    "（新しい名前・つながり・次に見るべきもの）が無いか確かめてください。"
    "あれば「## 母集団」に `- [ ]` の行を足し、「## 観点」にも書き、調べて更新してください。"
    "無ければ「## 見直し」に「追加なし」と書き、そのまま最終回答を返してください。"
    "計画書は apply_patch で更新し、「## 結論」を最新にしてください。"
    + _FINAL_ANSWER_VOICE
)


def _unstarted_prompt(rows: tuple[PlanRow, ...]) -> str:
    ids = "、".join(r.id or r.subject or "(id 無し)" for r in rows)
    return (
        f"計画書の「## 母集団」に未着手または形式・理由が不正な行が残っています: {ids}。"
        "調べて `- [x]`（`- source: パス:行` の行を添える）か `- [-]`（理由行を添える）に更新し、"
        "「## 結論」を最新にしてから最終回答を返してください。"
        + _FINAL_ANSWER_VOICE
    )


_GRAPH_TRUNCATED_PROMPT = (
    "graph_impact の結果が上限で打ち切られています（coverage.complete=false）。"
    "道具の上限は範囲外の理由になりません。depth や limit を上げて呼び直すか、"
    "ripgrep_search・read_around で呼び出し元をたどって補ってください。"
    "補ったうえで確かめた項目は [x] と source の行に、本当に確かめられない項目だけ [-] と理由の行にしてください。"
    + _FINAL_ANSWER_VOICE
)


class Decision(NamedTuple):
    action: str  # accept / reject_missing / reject_unstarted / reject_graph / reject_targets / review
    prompt: str
    reason: str  # accept のときの打ち切り理由


@dataclass
class PlanGateState:
    prev: Plan | None = None
    continuations: int = 0
    missing_retries: int = 0
    review_requests: int = 0
    graph_nudges: int = 0  # グラフの打ち切りを理由にした差し戻しの回数（1 ターンに 1 回まで）
    review_rounds: int = 0  # 見直しで行か観点が増えた回数
    awaiting: tuple | None = None  # 見直しを頼んだ時点の (行の集合, 観点の集合)
    stopped_reason: str | None = None
    target_nudges: int = 0  # 欠けた対象を示した差し戻しの回数（見直しの枠を使う）
    format_warnings: int = 0  # 書式不正だけを理由にした差し戻しをやめて警告にした回数
    last_target: dict | None = None  # 直近の final での対象の照合（`check_targets`）
    unresolved: dict | None = None  # 受理したが欠落が残った照合
    log: list = field(default_factory=list)  # final ごとの判定の履歴
    shadow: object = None  # dry-run で「本来の判定」を進める仮の状態


def _pending_rows(plan: Plan) -> tuple[PlanRow, ...]:
    """本当に未着手（`- [ ]` の行）の行。書式不正だけの行・本文の stray 行は含めない。"""
    return tuple(r for r in plan.rows if r.mark == " " and not r.stray)


def decide(state: PlanGateState, plan: Plan | None, *, review_cap: int,
           continue_cap: int = PLAN_CONTINUE_CAP, graph_incomplete: bool = False,
           target: dict | None = None) -> Decision:
    """final が届くたびに呼ぶ。計画書の目録の節だけから、差し戻す・見直しを頼む・受理するを決める（純関数に近い・状態は `state` に積む）。
    `graph_incomplete`＝このターンの graph_impact に打ち切られた結果があるとき、`[-]` の行が残る計画書を 1 回だけ差し戻す。
    `target`＝対象の照合（`check_targets`）。欠落があれば見直しの枠 1 回を、欠けた対象を示す差し戻しに使う。"""
    plan = plan or Plan()
    missing_targets = bool(target) and target.get("status") == "missing"
    if not plan.has_population or not plan.rows:
        if state.continuations >= continue_cap or state.missing_retries >= _MISSING_RETRY_CAP:
            state.stopped_reason = "plan_missing"
            return Decision("accept", "", "plan_missing")
        state.missing_retries += 1
        state.continuations += 1
        return Decision("reject_missing", _MISSING_PROMPT, "")
    prog = progressed(state.prev, plan)
    state.prev = plan
    pending = _pending_rows(plan)
    if plan.unstarted and not pending:
        state.format_warnings += 1
    grew_at_cap = False
    graph_hit = (graph_incomplete and state.graph_nudges < 1 and state.continuations < continue_cap
                 and any(r.mark == "-" for r in plan.rows))
    if state.awaiting is not None:
        rows_before, persp_before = state.awaiting
        state.awaiting = None
        grew = bool(_row_keys(plan) - rows_before) or bool(set(plan.perspectives) - persp_before)
        if grew:
            state.review_rounds += 1
            grew_at_cap = state.review_requests >= review_cap
        elif (not pending and not graph_hit
              and not (missing_targets and state.review_requests < review_cap)):
            state.stopped_reason = "complete"
            return Decision("accept", "", "complete")
    if pending:
        if state.continuations >= continue_cap:
            state.stopped_reason = "cap"
            return Decision("accept", "", "cap")
        if prog:
            state.continuations += 1
            return Decision("reject_unstarted", _unstarted_prompt(pending), "")
        state.stopped_reason = "no_progress"
        return Decision("accept", "", "no_progress")
    if graph_hit:
        state.graph_nudges += 1
        state.continuations += 1
        return Decision("reject_graph", _GRAPH_TRUNCATED_PROMPT, "")
    if state.review_requests < review_cap:
        state.review_requests += 1
        state.awaiting = (_row_keys(plan), frozenset(plan.perspectives))
        if missing_targets:
            state.target_nudges += 1
            return Decision("reject_targets", targets_prompt(target), "")
        return Decision("review", _REVIEW_PROMPT, "")
    reason = "review_cap" if grew_at_cap or (review_cap > 0 and state.review_requests >= review_cap
                                                  and state.review_rounds > 0) else "complete"
    state.stopped_reason = reason
    return Decision("accept", "", reason)


def gate_step(state: PlanGateState, plan: Plan | None, *, review_cap: int, graph_incomplete: bool = False,
              target: dict | None = None, dry_run: bool = False) -> Decision:
    """`decide` を 1 回呼び、判定を履歴に残す。dry_run のときは仮の状態で「本来の判定」だけ計算して記録し、常に受理を返す。
    受理で欠落が残っていれば `state.unresolved` に控える。"""
    if dry_run:
        if state.shadow is None:
            state.shadow = PlanGateState()
        d = decide(state.shadow, plan, review_cap=review_cap, graph_incomplete=graph_incomplete, target=target)
    else:
        d = decide(state, plan, review_cap=review_cap, graph_incomplete=graph_incomplete, target=target)
    state.last_target = target
    state.log.append({
        "n": len(state.log) + 1, "action": d.action, "reason": d.reason, "dry_run": dry_run,
        "target_status": (target or {}).get("status"), "missing_count": (target or {}).get("missing_count", 0),
        "unstarted_rows": len(_pending_rows(plan)) if plan is not None else 0,
        "malformed_rows": sum(r.malformed for r in plan.rows) if plan is not None else 0,
        "graph_incomplete": bool(graph_incomplete)})
    if dry_run:
        state.stopped_reason = "dry_run"
        state.unresolved = None
        return Decision("accept", "", "dry_run")
    if d.action == "accept":
        state.unresolved = target if target and target.get("status") == "missing" else None
    return d


# ---- 対象 ID の集合による欠落の照合（plan_gate・影響調査） ----

# 依頼の条件を分ける語（質問 ID や特定の名前は埋め込まない）。
_NARROW_WORDS = ("実行文", "使用している", "使っている", "参照している箇所", "だけ", "のみ")
_DIRECT_WORDS = ("直接",)
_COPY_WORDS = ("COPY", "取り込", "取込", "コピー句")
# 回答の行が「除外」を述べているとみなす語と、使用の有無だけの理由・それ以外の理由を示す語。
_EXCLUDE_WORDS = ("対象外", "除外", "含めない", "影響しない", "影響なし", "該当しない")
_USE_ONLY_RE = re.compile(r"実行文では?(?:使|参照)|使用(?:して)?(?:い|なし)|未使用|使われて(?:い|お)|"
                          r"使って(?:い|お)ない|参照(?:して)?(?:い|お)ない|使用なし")
_OTHER_REASON_WORDS = ("廃止", "別システム", "別世代", "範囲外", "重複", "退役", "削除済", "存在しない", "コメント")
TARGET_PROMPT_MAX = 40
TARGET_RECORD_MAX = 50
_SUPPLEMENT_HEAD_RE = re.compile(r"^##\s*調べた範囲")
_H2_RE = re.compile(r"^#{1,2}(?!#)\s")


def condition_mode(message: str | None) -> str:
    """依頼の条件を 3 つに分ける。範囲を絞る語＝undeterminable／「直接」と COPY・取り込み＝direct_copies／それ以外＝default。"""
    m = message or ""
    if any(w in m for w in _NARROW_WORDS):
        return "undeterminable"
    if any(w in m for w in _DIRECT_WORDS) and any(w in m.upper() for w in _COPY_WORDS):
        return "direct_copies"
    return "default"


def _row_id(r: dict) -> str:
    return r.get("path") or r.get("cid") or r.get("name") or ""


def required_targets(results: list[dict]) -> tuple[dict, list[dict]]:
    """graph_impact の結果から必要な対象（id → 対象）を作る。このターン最初の（エラーでない）呼び出しと、起点がすでに含めた結果の影響先になっている呼び出しだけを含める。
    戻り値＝（対象, 含めた呼び出し）。"""
    targets: dict[str, dict] = {}
    calls: list[dict] = []
    reach_cids: set[str] = set()
    reach_paths: set[str] = set()
    for g in results:
        if g.get("error"):
            continue
        start = g.get("start") or {}
        if calls and not ((start.get("cid") and start["cid"] in reach_cids)
                          or (start.get("path") and start["path"] in reach_paths)):
            continue
        calls.append(g)
        for r in g.get("rows") or []:
            rid = _row_id(r)
            if not rid:
                continue
            if r.get("cid"):
                reach_cids.add(r["cid"])
            if r.get("path"):
                reach_paths.add(r["path"])
            dist = r.get("distance") if isinstance(r.get("distance"), int) else None
            cur = targets.get(rid)
            if cur is None:
                targets[rid] = {"id": rid, "path": r.get("path") or "", "name": r.get("name") or "",
                                "kind": r.get("kind") or "", "distance": dist,
                                **{k: r[k] for k in ("doc", "line") if k in r}}
            elif dist is not None and (cur["distance"] is None or dist < cur["distance"]):
                cur["distance"] = dist
    return targets, calls


def _answer_lines(answer: str | None) -> list[str]:
    """回答の本文の行。『参照した資料:』のブロックと補足の節（調べた範囲）は除く＝出典欄だけにあるものを拾わない。"""
    body, _block = _split_ref_block(answer or "")
    out: list[str] = []
    skip = False
    for ln in body.splitlines():
        if _SUPPLEMENT_HEAD_RE.match(ln):
            skip = True
            continue
        if skip and _H2_RE.match(ln):
            skip = False
        if not skip:
            out.append(ln)
    return out


def _target_regexes(targets: dict) -> dict[str, re.Pattern]:
    """対象ごとの照合パターン。パス全体のほか、一意なファイル名・語幹でも拾う（`ABC.cbl` の `ABC`）。"""
    bases = Counter(); stems = Counter()
    for t in targets.values():
        p = t["path"] or t["name"]
        base = p.rsplit("/", 1)[-1]
        bases[base] += 1
        stems[base.rsplit(".", 1)[0]] += 1
    out = {}
    for rid, t in targets.items():
        p = t["path"] or t["name"]
        base = p.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        alts = [p]
        if bases[base] == 1 and len(base) >= 3:
            alts.append(base)
        if stems[stem] == 1 and len(stem) >= 3:
            alts.append(stem)
        out[rid] = re.compile(r"(?<![A-Za-z0-9_.\-])(?:" + "|".join(re.escape(a) for a in dict.fromkeys(alts))
                              + r")(?![A-Za-z0-9_\-])", re.IGNORECASE)
    return out


def _exclusion_is_valid(line: str) -> bool:
    """除外の理由が使用の有無だけなら無効（条件と合わない）。それ以外の理由があれば有効。"""
    return not _USE_ONLY_RE.search(line) or any(w in line for w in _OTHER_REASON_WORDS)


def check_targets(results: list[dict], message: str | None, answer: str | None) -> dict:
    """graph_impact の影響先（ID の集合）と、採用した回答に書かれた対象を ID で比べる。
    status＝not_applicable（対象集合なし）／undeterminable（依頼の条件を確定できない）／no_missing／missing。
    missing＝必要 − 含めた − 受け入れた除外。retrieved_complete＝欠落が無い／coverage_complete＝含めた呼び出しの coverage がすべて完全。"""
    out = {"status": "not_applicable", "condition_mode": None, "required_count": 0, "included_count": 0,
           "excluded_count": 0, "excluded_invalid_count": 0, "missing_count": 0, "missing": [],
           "retrieved_complete": None, "coverage_complete": None}
    if not results:
        return out
    mode = condition_mode(message)
    out["condition_mode"] = mode
    targets, calls = required_targets(results)
    if not calls:
        return out
    out["coverage_complete"] = all(
        g.get("complete") is True and not (isinstance(g.get("count"), int) and g["count"] > len(g.get("rows") or []))
        for g in calls)
    if mode == "undeterminable":
        out["status"] = "undeterminable"
        return out
    if mode == "direct_copies":
        targets = {k: t for k, t in targets.items() if t["distance"] == 1}
    if not targets:
        return out
    pats = _target_regexes(targets)
    included: set[str] = set(); excluded: set[str] = set(); invalid: set[str] = set()
    for ln in _answer_lines(answer):
        if not ln.strip():
            continue
        hits = [rid for rid, pat in pats.items() if pat.search(ln)]
        if not hits:
            continue
        if any(w in ln for w in _EXCLUDE_WORDS):
            (excluded if _exclusion_is_valid(ln) else invalid).update(hits)
        else:
            included.update(hits)
    excluded -= included
    invalid -= included | excluded
    missing = [t for rid, t in targets.items() if rid not in included and rid not in excluded]
    out.update(required_count=len(targets), included_count=len(included), excluded_count=len(excluded),
               excluded_invalid_count=len(invalid), missing_count=len(missing), missing=missing,
               retrieved_complete=not missing, status="missing" if missing else "no_missing")
    return out


def target_check_for_turn(st, message: str | None, lens: str | None, answer: str | None) -> dict:
    """影響調査のターン（lens が impact、または investigate で依頼に『影響』がある）で、このターンの graph_impact の結果があれば照合する。それ以外は対象集合なし。"""
    if not (lens == "impact" or (lens == "investigate" and "影響" in (message or ""))):
        return check_targets([], message, answer)
    return check_targets(graph_impact_results(st), message, answer)


def targets_prompt(check: dict) -> str:
    """欠けた対象を具体的に示す差し戻しの文。最大 40 件を、関係・根拠つきで並べる。"""
    miss = check.get("missing") or []
    lines = []
    for t in miss[:TARGET_PROMPT_MAX]:
        bits = []
        if t.get("distance") is not None:
            bits.append(f"距離 {t['distance']}")
        if t.get("kind"):
            bits.append(f"種別 {t['kind']}")
        if t.get("doc"):
            bits.append(f"根拠 {t['doc']}" + (f":{t['line']}" if t.get("line") else ""))
        lines.append(f"- {t['id']}" + (f"（{'・'.join(bits)}）" if bits else ""))
    rest = len(miss) - len(lines)
    cond = ("依頼の条件: 構造上の影響先をすべて対象にする（graph_impact の結果の全件）。"
            if check.get("condition_mode") != "direct_copies"
            else "依頼の条件: 直接取り込む対象（graph_impact の距離 1）をすべて対象にする。")
    return (
        f"回答を確定する前の照合です。graph_impact の結果にある影響先のうち、回答の一覧にも除外の説明にも書かれていない対象が"
        f"{len(miss)} 件あります。\n{cond}\n欠けた対象:\n" + "\n".join(lines)
        + (f"\n（ほか {rest} 件）" if rest > 0 else "")
        + "\n各対象を、回答の一覧にパス付きで含めるか、依頼の条件と整合する理由で除外する旨を書いてください"
          "（「実行文で使っていない」だけでは除外の理由になりません）。graph_impact の結果から外す根拠が"
          "確かめられない対象は一覧に含めてください。調べ直しが必要なものは調べてから答えてください。"
        + _FINAL_ANSWER_VOICE)


def unresolved_notice(check: dict) -> str:
    """見直しの枠を使い切っても欠落が残ったときの回答の注記。"""
    miss = check.get("missing") or []
    head = "、".join(t["id"] for t in miss[:5])
    return (f"グラフの影響先のうち {len(miss)} 件が、回答の一覧にも除外の理由にも書かれていません"
            f"（確認できていません）。先頭: {head}" + (" ほか" if len(miss) > 5 else ""))


def target_check_record(state: PlanGateState | None, *, dry_run: bool) -> dict | None:
    """評価用の記録 `target_check`。plan_gate 以外（状態なし）は None。"""
    if state is None:
        return None
    t = state.last_target or check_targets([], None, None)
    missing_now = t.get("status") == "missing"
    if dry_run:
        decision = "recorded_only" if missing_now else "none"
    elif state.unresolved:
        decision = "accepted_unresolved"
    elif state.target_nudges:
        decision = "reject_missing"
    else:
        decision = "none"
    return {"status": t["status"], "condition_mode": t["condition_mode"], "required_count": t["required_count"],
            "included_count": t["included_count"], "excluded_count": t["excluded_count"],
            "excluded_invalid_count": t["excluded_invalid_count"], "missing_count": t["missing_count"],
            "missing": [m["id"] for m in t["missing"][:TARGET_RECORD_MAX]],
            "retrieved_complete": t["retrieved_complete"], "coverage_complete": t["coverage_complete"],
            "decision": decision, "dry_run": dry_run}


# ---- 計画書 → 回答 ----

def unconfirmed_items(plan: Plan) -> list[dict]:
    """未確認の一覧（`{"item","reason"}`）。未着手・打ち切り・済でも「要: source」に `- source:` が無い行。理由行の文は転記する（言い換えない）。"""
    out = []
    for r in plan.rows:
        name = r.subject or r.id
        if r.malformed:
            out.append({"item": name, "id": r.id, "reason": "計画書の形式・id または打ち切りの理由が不正です"})
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


def _strip_id_tags(text: str, ids: set[str]) -> str:
    """結論の各文に添えた根拠の行の id（`[id]`）だけを除く。id 以外の角括弧は残す。"""
    return _TAG_RE.sub(lambda m: "" if m.group(1) in ids else m.group(0), text)


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
    idx = None
    for i, line in enumerate(lines):
        if _REFS_HEADING_RE.match(line):
            idx = i
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


def _plan_supplement(plan: Plan, fmt: list[dict], unverified: list[dict]) -> str:
    """本文の後ろに付ける補足の節「調べた範囲（調査計画書）」。計画書の前提・範囲・目録の件数・未完了の2種を並べる。"""
    parts = []
    premise = (_section(plan.sections, "前提") or "").strip()
    if premise:
        parts.append("### 前提\n" + premise)
    scope_text = (_section(plan.sections, "範囲") or "").strip()
    if scope_text:
        parts.append("### 範囲\n" + scope_text)
    kinds: dict[str, int] = {}
    for r in plan.rows:
        kinds[r.kind or "種別なし"] = kinds.get(r.kind or "種別なし", 0) + 1
    n_closed = sum(1 for r in plan.rows if r.mark == "-" and not r.malformed)
    n_open = sum(1 for r in plan.rows if r.mark == " " and not r.malformed)
    n_done = sum(1 for r in plan.rows if r.mark == "x" and not r.malformed
                 and not ("source" in r.needs and not r.sources))
    parts.append(
        f"母集団 {len(plan.rows)} 件（" + "・".join(f"{k} {v} 件" for k, v in kinds.items()) + "）。"
        f"確認済み {n_done} 件・確認できなかった {n_closed} 件・未着手 {n_open} 件"
        + (f"・計画書の記載の不備 {len(fmt)} 件" if fmt else "") + "。")
    if unverified:
        parts.append("### 確認できなかった項目\n" + "\n".join(f"- {u['item']}（{u['reason']}）" for u in unverified))
    if fmt:
        parts.append("### 計画書の記載の不備\n" + "\n".join(f"- {u['item']}（{u['reason']}）" for u in fmt))
    return "## 調べた範囲（調査計画書）\n\n" + "\n\n".join(parts)


def compose_with_model_answer(plan: Plan, model_answer: str) -> tuple[str, dict]:
    """モデルの回答を本文のまま残し、計画書を補足の節と参照の追記で後ろに付ける（本文・参照行は書き換えない）。
    順序は 本文 → 補足の節 → 「参照した資料:」（モデルの参照行 → 計画書にだけある参照）。戻り値＝（組んだ本文, 記録用の要約）。"""
    refs = _plan_refs(plan)
    fmt, unverified = plan_gaps(plan)
    body, block = _split_ref_block(model_answer)
    known = _block_ranges(block)
    added = []
    for p, ranges in refs.items():
        new = [x for x in ranges if x not in known.get(p, set())]
        if new:
            added.append(f"- {p}:{', '.join(new)}")
    parts = [body, _plan_supplement(plan, fmt, unverified)]
    if block or added:
        parts.append("\n".join(block or ["参照した資料:"]) + ("\n" + "\n".join(added) if added else ""))
    parts = [x for x in parts if x]
    info = {"format_issues": [{"id": x["id"], "reason": x["reason"]} for x in fmt],
            "unverified_items": [{"id": x["id"], "reason": x["reason"]} for x in unverified],
            "plan_only_paths": _plan_only_paths(plan, refs, model_answer),
            "refs_added_from_plan": len(added)}
    return "\n\n".join(parts), info


def compose_answer(plan: Plan, fallback_body: str = "") -> str:
    """計画書を回答に整える（結論 → 前提 → 調べた範囲 → 参照した資料）。機械的な並べ替えと id の除去だけで、文章は要約も言い換えもしない。"""
    ids = {r.id for r in plan.rows if r.id}
    conclusion = (_section(plan.sections, "結論") or "").strip()
    if not conclusion:
        conclusion = strip_refs_block(fallback_body or "")
    unknown = {u["id"] for u in unconfirmed_items(plan)}
    conclusion = "\n".join(_conclusion_line(line, ids, unknown) for line in conclusion.splitlines()).strip()
    parts = [conclusion] if conclusion else []
    premise = (_section(plan.sections, "前提") or "").strip()
    if premise:
        parts.append("## 前提\n" + premise)
    scope_lines = []
    scope_text = (_section(plan.sections, "範囲") or "").strip()
    if scope_text:
        scope_lines.append(scope_text)
    if plan.rows:
        kinds: dict[str, int] = {}
        for r in plan.rows:
            kinds[r.kind or "種別なし"] = kinds.get(r.kind or "種別なし", 0) + 1
        unconf = unconfirmed_items(plan)
        n_done = sum(1 for r in plan.rows if r.mark == "x" and not r.malformed
                     and not ("source" in r.needs and not r.sources))
        n_closed = sum(1 for r in plan.rows if r.mark == "-" and not r.malformed)
        n_open = len(plan.unstarted)
        scope_lines.append(
            f"母集団 {len(plan.rows)} 件（" + "・".join(f"{k} {v} 件" for k, v in kinds.items()) + "）。"
            f"確認済み {n_done} 件・確認できなかった {n_closed} 件・未着手 {n_open} 件"
            + (f"・根拠の行が無く未確認に格下げ {len(unconf) - n_closed - n_open} 件" if len(unconf) > n_closed + n_open else "")
            + "。")
        if unconf:
            scope_lines.append("確認できなかった項目:\n" + "\n".join(
                f"- {u['item']}（{u['reason']}）" for u in unconf))
    if scope_lines:
        parts.append("## 調べた範囲\n" + "\n\n".join(scope_lines))
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


# ---- グラフの結果（plan_gate の関門・評価用の記録） ----

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


def graph_incomplete(st) -> bool:
    """このターンの graph_impact に coverage が完全でない結果があるか。"""
    return any(g.get("complete") is False for g in graph_impact_results(st))


# ---- ターンの仕上げ・評価用の記録 ----

EVAL_DIR_ENV = "SHERPA_LEDGER_EVAL_DIR"


def compose_turn_answer(st) -> None:
    """計画書方式のターンで、モデルの回答を本文のまま残して計画書の補足を後ろに付ける（モデルの回答が空のときだけ計画書の結論を本文にする。計画書に目録の行が無いときは何もしない）。"""
    stats = {"applied": False, "body_source": "unchanged", "plan_present": False, "plan_rows": 0,
             "format_issues": [], "unverified_items": [], "plan_only_paths": [], "refs_added_from_plan": 0}
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
    gate = st._plan_gate
    marks = {"unstarted": 0, "done": 0, "closed": 0}
    if plan is not None:
        for r in plan.rows:
            marks["unstarted" if r.malformed else _MARKS[r.mark]] += 1
    review_text = (_section(plan.sections, "見直し") or "") if plan is not None else ""
    return {
        "ledger": "plan", "style": st._ledger_style, "plan_present": plan is not None,
        "rows": len(plan.rows) if plan is not None else 0, "marks": marks,
        "unconfirmed_items": unconfirmed_items(plan) if plan is not None else [],
        "plan_review_lines": sum(1 for ln in review_text.splitlines() if ln.strip().startswith("-")),
        "review_requests": gate.review_requests if gate else 0,
        "review_rounds": gate.review_rounds if gate else 0,
        "continuations": gate.continuations if gate else 0,
        "stopped_reason": st._investigation_stopped_reason or (gate.stopped_reason if gate else None),
        "malformed_rows": sum(r.malformed for r in plan.rows) if plan else 0,
        "normalizations": list(plan.normalizations) if plan is not None else [],
        "format_warnings": gate.format_warnings if gate else 0,
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
            reason = st._plan_gate.stopped_reason if st._plan_gate else "self_final"
            reason = reason or "no_resume"
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


def _gate_stats(st, plan: Plan | None, graph: dict) -> dict:
    closed = []
    for r in (plan.rows if plan is not None else ()):
        if r.mark == "-":
            word, detail = r.reason or (None, "")
            closed.append({"id": r.id, "reason": word, "detail": detail})
    return {"graph_nudges": st._plan_gate.graph_nudges if st._plan_gate else 0,
            "accepted_with_graph_incomplete": bool(graph["impact_incomplete"] and closed),
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
    """評価用: 見直しを Sherpa が数えて止めたか（A・B）、指示文の数字だけか（C）。"""
    if st._ledger_style == "json":
        return {"app_gating": True, "app_review_cap": st._plan_review_cap,
                "app_review_requests": st._ledger_review_requests, "instruction_review_cap": None}
    if st._ledger_style == "plan_gate":
        return {"app_gating": True, "app_review_cap": st._plan_review_cap,
                "app_review_requests": st._plan_gate.review_requests if st._plan_gate else 0,
                "instruction_review_cap": None}
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
        reviews_asked = (st._plan_gate.review_requests if st._plan_gate else 0) if st._ledger_style != "json" \
            else st._ledger_review_requests
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
            "gate": _gate_stats(st, plan, graph) if st._ledger_style != "json" else None,
            "plan_normalizations": list(plan.normalizations) if plan is not None else [],
            "target_check": target_check_record(st._plan_gate, dry_run=gate_dry_run()),
            "gate_log": list(st._plan_gate.log) if st._plan_gate else None,
            "candidate": _candidate_stats(st), "compose": _compose_stats(st),
            "review": _review_stats(st), "composed_taken": "before_preamble_split",
            "trimmed": [{"kind": t.get("kind"), "chars": len(t.get("text") or "")} for t in st._trimmed],
        }
        (out / f"{stem}.stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                                encoding="utf-8")
    except Exception:
        pass
