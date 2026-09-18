"""1質問1調査状態（`InvestigationState`）——アプリ側（機械的・LLM なし）で根拠・調査の限界・
呼び出し記録を集約する（`docs/proposals/2026-09-07-調査結果集約と並列実行の改善方針.md`
「改善方針」節）。用途は2つ:

1. 探索ループ（`agentic_search.openai_style`/`anthropic_style`/`gemini`）が自分の会話履歴
   （`msgs`）の文脈整理に使う——古いツール往復を `render()` の要約1通へ置換する（各ループが
   自分専用のインスタンスを持つ・呼び出し元へは公開しない）。
2. ハイブリッド（`providers/base.py::_agentic_run`）が下調べ役・査読の結果を集約し、査読の
   十分性判定・再調査依頼・清書入力を同じ状態から組む（1回の質問全体で1インスタンス）。

このモジュール自身は純粋な Python（I/O なし）——ファイル読み取り・ネットワーク呼び出しは
呼び出し元が既に済ませた `result`/`cites`/`evidence_meta` を受け取るだけで、自らは行わない。
秘密の redaction・制御文字除去だけは `agentic_search._digest_clean`（citation digest 系と共通の
唯一の実装）を関数内 import で再利用する（重複実装で redaction 漏れが2箇所化することを防ぐ）。
"""
from __future__ import annotations

import json
import os

import re
from dataclasses import dataclass, field

_CITATION_QUOTE_CAP = 400   # B の `_SYNTHESIS_QUOTE_CAP` と同じ（citation/構造事実の既定切り詰め長）
# 精読（read_around/read_doc・S3b 原本読取ツール）本文の保存上限（UTF-8 バイト）——清書ダイジェスト
# 予算（`agentic_search._SYNTHESIS_MAX_BYTES`・env `SHERPA_AGENTIC_SYNTHESIS_BUDGET_BYTES`）の 1/4。
# 1件の精読本文だけで清書予算の大半を専有しないための上限で、複数件の精読が同時に清書へ渡っても
# 他の根拠・gaps に予算が残る。circular import 回避のため `agentic_search` の定数は参照できず、同じ
# env を同じ既定・同じクランプで読む（`_SYNTHESIS_MAX_BYTES` の解決と一致することをテストが固定）。


def _synthesis_budget_bytes() -> int:
    raw = os.environ.get("SHERPA_AGENTIC_SYNTHESIS_BUDGET_BYTES")
    default, lo, hi = 256 * 1024, 8 * 1024, 4 * 1024 * 1024
    try:
        v = int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        v = default
    return default if not (lo <= v <= hi) else v


_READ_TEXT_CAP_BYTES = _synthesis_budget_bytes() // 4
# render()（査読・文脈整理の入力）に載せる精読本文 1 件の表示上限（文字・固定）。render の予算は呼び出し元
# ごとに小さい（文脈整理＝48KiB・査読＝清書予算の 1/2）ため、保存上限に連動させると精読 2〜6 件で
# 【限界】が無通知で落ちる。保存本文は `_READ_TEXT_CAP_BYTES` まで持ち、清書入力にはそのまま渡す。
_RENDER_READ_TEXT_CAP = 800

# 縮退（バックエンド不調）の計数を `answer.limits` のフラットな bool 項目へ載せるための対応表
# （`store/usage.py::_USAGE_LIMIT_BOOL_FIELDS` と同じ語彙）。`read_io` は統計項目を持たない
# （障害先の分類にだけ使う）。集計の意味論は「このターンで初めて検出されたかどうか」＝初回検出の
# 計数で、障害が起きた巡の数ではない（`providers/base.py::_limits_delta` が偽→真の巡だけ載せる）。
_BACKEND_LIMIT_FIELD = {"fulltext": "backend_unavailable_fulltext", "graph": "backend_unavailable_graph"}
GRAPH_REINGEST_LIMIT_FIELD = "graph_reingest_required"
_STRUCTURAL_FACT_CAP = 800  # glob_search/doc_outline/compare_documents の実質的結果（パス一覧・
                            # 見出し一覧・差分要点）の切り詰め長——件数だけでなく内容そのものを
                            # 文脈整理後も残すための上限（精読本文の保存上限＝`_READ_TEXT_CAP_BYTES`
                            # とは別契約・目的が違うため揃えない）。
_ARGS_SUMMARY_CAP = 120
_GAP_MESSAGE_CAP = 200
_TOOL_ERROR_CAP = 80

# kind="read"（精読）扱いにするツール名——read_around/read_doc（土台系）と原本読取ツール 5 本（xlsx_sheets を除く＝シート一覧のみで本文を読んでいない）
# （`agentic_search._doc_reader_text_locator` が結果へ `doc_id`/`text` を合成する）。
_READ_KIND_TOOL_NAMES = frozenset({
    "read_around", "read_doc",
    "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "file_head",
})

_LINE_PREFIX_RE = re.compile(r"^(\d+):")   # read_around の "123: 本文" 形式から行番号を復元する

# 複数項目を列挙する区切り記号——末尾に半角空白を含める（`_structural_fact` docstring参照:
# 空白が無いと後続の再 redact 時に `_KV_SECRET_RE` の `\S+` が区切りごと次の項目まで飲み込む）。
_LIST_SEP = "、 "
_EXCERPT_SEP = "／ "


@dataclass
class Evidence:
    """確定根拠1件（citation 由来・構造的根拠由来の両方を同じ形で保持する）。

    `ev_id`（"ev-N"）はこの調査内で最初に割り当てた番号を以後変えない（並べ替え・重複排除で
    再採番しない）——`agentic_search.build_evidence_digest`/`build_synthesis_digest` の ev-N 採番
    （Evidence Packet／帰属専用・こちらは触らない）とは別の「調査内 ID」で、`InvestigationState.
    render()` の中だけで使う（公開 answer・帰属には出さない）。
    """
    ev_id: str
    kind: str             # "citation" | "list" | "graph" | "compare" | "read" | "outline"
    doc_id: str | None
    span: tuple[int, int] | None
    text: str             # quote／集計・カード要約／精読本文（各上限まで切り詰め済み）
    source_tool: str
    verification: str     # "verified" | "span_unmatched" | "structural" | "unverified"
    extra_quotes: list[str] = field(default_factory=list)
    # 原本読取ツール（xlsx_range 等）の kind="read" は `span` が常に None
    # （行番号ではなく "Sheet1!A1:D20" 等の文字列位置のため）——`locator`（`agentic_search.
    # _doc_reader_text_locator` が組む値）を同一性・表示の両方へ足すことで、同じ doc_id の
    # 別シート/別ページ読み取りが `span=None` 同士で1件に潰れないようにする（`_find` 参照）。
    locator: str | None = None
    # kind="read" だけが意味を持つ（他 kind は既定 False のまま）: `text` が `_READ_TEXT_CAP_BYTES`
    # で保存時に切られた（または上流の読取ツール自体が既に打ち切っていた）ため、末尾が清書
    # ダイジェストへ渡らないことを示す。`_upsert` は「長い方を主本文」に統合する際、主本文として
    # 採用した側の値をそのまま引き継ぐ（切られていない本文へ更新されれば False に戻る）。
    text_truncated: bool = False


@dataclass
class ToolCall:
    """調査の限界（`gaps`）を機械的に導くための呼び出し記録1件。"""
    name: str
    args_summary: str
    hits: int | None
    truncated: bool
    error: str | None


# DEPTH-2 S1（docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.2/§2.5）: メイン査読が
# 再調査後もなお不足と判定したとき、全回答を固定文言へ置き換える代わりに主張単位で
# 確定/推定/不明を持たせ、裏付けのある部分を残す。理由コードは閉じた語彙（unknown 限定）。
_CLAIM_STATUSES = frozenset({"confirmed", "inferred", "unknown"})
_CLAIM_UNKNOWN_REASON_CODES = frozenset({
    "not_found_in_scope", "unexplored", "insufficient", "conflict", "budget", "unreadable"})
_CLAIM_KEYS = frozenset({"id", "status", "text", "evidence_refs", "reason", "reason_code"})


@dataclass
class Claim:
    """査読後の清書が扱う主張1件。`status` は確定（`evidence_refs` で裏付け）／推定
    （`reason` に理由）／不明（`reason_code` を閉じた語彙から）。`evidence_refs` は
    `Evidence.ev_id`（この調査状態内の採番）を指す文字列。

    `origin`（DEPTH-2 S4b）: 主張の出所——`"synthesis"`（査読後の最終手段・
    `providers/base.py::_claims_synthesis`）／`"worker"`（下調べ役の一次判断・
    `agentic_search.openai_style` の `final_synthesis=False` 経路）。LLM が出す JSON
    （`_CLAIM_KEYS`）のキーではなく、呼び出し元（`parse_claims`/`set_claims` の `origin`
    引数）が代入する内部メタデータ——`claim_to_dict` の出力には含めない（公開
    envelope・共有・既存テストの形を変えない）。"""
    id: str
    status: str
    text: str
    evidence_refs: list[str] = field(default_factory=list)
    reason: str = ""
    reason_code: str = ""
    origin: str = "synthesis"


def claim_to_dict(c: Claim) -> dict:
    return {"id": c.id, "status": c.status, "text": c.text,
            "evidence_refs": list(c.evidence_refs), "reason": c.reason, "reason_code": c.reason_code}


def extract_claims_json(acc: str) -> list | None:
    """`{"claims": [...]}` 1個だけの応答本文から生の主張配列を取り出す（構造検証は `parse_claims`
    が別途行う・本関数は JSON 抽出だけの純関数）。JSON でない・`claims` 以外のキーを含む・配列で
    ない場合はいずれも `None`（追記で継ぎ足さない＝失敗として扱う・§2.5「途中で切れた JSON は
    失敗」）。`providers/base.py::_claims_synthesis`（査読後の最終手段）と `agentic_search.
    openai_style` の worker 一次判断（`final_synthesis=False` 経路）が共通で使う。"""
    m = re.search(r"\{.*\}", acc, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(parsed, dict) or set(parsed.keys()) != {"claims"}:
        return None
    return parsed.get("claims")


def parse_claims(raw, origin: str = "synthesis") -> list[Claim] | None:
    """構造化出力（JSON の `claims` 配列）を検証して `Claim` のリストへ変換する（純関数）。

    キー集合が `_CLAIM_KEYS` の部分集合かつ `id`/`status`/`text` を持つこと・`status` が閉じた
    語彙であること・`status == "unknown"` のときだけ `reason_code` が閉じた語彙を持つこと（他の
    区分では reason_code を持たない）・`id` の重複が無いことを全て満たさなければ `None`
    （呼び出し元はこれを「失敗」として扱う——追記で継ぎ足したり部分的に採用したりしない・
    §2.5「途中で切れた JSON は失敗」）。

    `origin`（省略可・既定 `"synthesis"`）: 生成した Claim 全件へ付ける出所（`Claim.origin`
    docstring 参照）。`raw` 自体のキーとは無関係（LLM 出力の語彙を広げない）。
    """
    if not isinstance(raw, list):
        return None
    claims: list[Claim] = []
    seen_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        keys = set(item.keys())
        if not keys <= _CLAIM_KEYS or not {"id", "status", "text"} <= keys:
            return None
        cid, status, text = item.get("id"), item.get("status"), item.get("text")
        if not isinstance(cid, str) or not cid.strip() or cid in seen_ids:
            return None
        if not isinstance(status, str) or status not in _CLAIM_STATUSES:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        evidence_refs = item.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or not all(isinstance(r, str) for r in evidence_refs):
            return None
        reason = item.get("reason", "")
        if not isinstance(reason, str):
            return None
        reason_code = item.get("reason_code", "")
        if not isinstance(reason_code, str):
            return None
        if status == "unknown":
            if reason_code not in _CLAIM_UNKNOWN_REASON_CODES:
                return None
        elif reason_code:
            return None   # reason_code は unknown 限定（他区分に紛れ込ませない）
        if status == "inferred" and not reason.strip():
            return None   # 推定は理由必須（空白のみも不可・提案書 §2.5「推定（理由）」）
        seen_ids.add(cid)
        claims.append(Claim(id=cid, status=status, text=text.strip(),
                            evidence_refs=list(evidence_refs), reason=reason, reason_code=reason_code,
                            origin=origin))
    return claims


# DEPTH-2 S5（§2.4）: evaluator の指摘（不足の軸・主張 ID 単位の反証）の状態。
# 「未解決」＝次巡の指示に載る／「解決」＝次巡で根拠が足され orchestrator が確定した／
# 「撤回」＝evaluator 自身が取り下げた。どちらの遷移も orchestrator（`providers/base.py::
# _agentic_run` の巡ループ）が確定する——evaluator は指摘を出すだけで状態を書き換えない。
FINDING_STATES = frozenset({"open", "resolved", "withdrawn"})
_FINDING_KEYS = frozenset({"id", "claim_id", "text", "refutes", "state"})


@dataclass
class Finding:
    """evaluator の指摘 1 件。`round_no` は指摘が出た巡（1 始まり）——右ペインの思考ノードと
    巡別計測（`chat-round`）が巡を区別するために持つ。`claim_id` は対象の主張 ID（空＝主張に
    紐づかない全体的な不足）。`refutes` が真なら対象の主張はその巡の中で採用不可へ落ちる
    （`InvestigationState.apply_findings`）。"""
    id: str
    round_no: int
    claim_id: str = ""
    text: str = ""
    refutes: bool = False
    state: str = "open"
    # 指摘を出した時点の対象主張の根拠参照数と区分。次巡で「根拠が足されたか／確定へ戻ったか」を
    # 判定する基準点＝`apply_findings` が代入する内部メタデータ（LLM 出力のキーではないため
    # `_FINDING_KEYS`・`finding_to_dict` には含めない）。
    ev_count: int = 0
    claim_status: str = ""


def finding_to_dict(f: Finding) -> dict:
    return {"id": f.id, "round_no": f.round_no, "claim_id": f.claim_id, "text": f.text,
            "refutes": f.refutes, "state": f.state}


def parse_findings(raw, round_no: int) -> list[Finding] | None:
    """evaluator 応答の `findings` 配列を検証して `Finding` へ変換する（純関数）。

    `None`（キー自体が無い）は「指摘なし」＝空リストとして扱えるよう、呼び出し元が区別できる
    `None` ではなく空リストを返す。形が不正（配列でない・未知のキー・`id` 欠落/重複・`state` が
    閉じた語彙でない）なら `None`——呼び出し元はこれを「指摘を読めなかった」として扱い、主張の
    採否は変更しない（部分採用しない・`parse_claims` と同じ規律）。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out: list[Finding] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            return None
        keys = set(item.keys())
        if not keys <= _FINDING_KEYS or "id" not in keys:
            return None
        fid = item.get("id")
        if not isinstance(fid, str) or not fid.strip() or fid in seen:
            return None
        claim_id = item.get("claim_id", "")
        text = item.get("text", "")
        if not isinstance(claim_id, str) or not isinstance(text, str):
            return None
        refutes = item.get("refutes", False)
        if not isinstance(refutes, bool):
            return None
        st = item.get("state", "open")
        if not isinstance(st, str) or st not in FINDING_STATES:
            return None
        seen.add(fid)
        out.append(Finding(id=fid.strip(), round_no=int(round_no), claim_id=claim_id.strip(),
                           text=text.strip(), refutes=refutes, state=st))
    return out


def render_findings(findings: list[Finding]) -> str:
    """次巡の指示・右ペインの思考ノードへ渡す未解決の指摘だけの整形（解決済み・撤回は落とす）。

    先頭に指摘 ID を出す——査読は同じ指摘を同じ ID で返し直し、別の指摘には未使用の ID を
    付ける契約（`providers/prompts.py::review_prompt`）で、ID を別の指摘へ使い回されると
    `apply_findings` の既存行更新が別の未解決の反証を消してしまうため。
    """
    lines = []
    for f in findings:
        if f.state != "open":
            continue
        target = f"[{f.claim_id}] " if f.claim_id else ""
        lines.append(f"- ({f.id}) {target}{f.text}" + ("（反証）" if f.refutes else ""))
    return "\n".join(lines)


def adoptable_claims(claims: list[Claim]) -> list[Claim]:
    """停止・失敗・時間切れの未完了回答へ載せてよい主張（§2.4）＝確定/推定のうち反証されて
    いないもの。反証された主張は `apply_findings` が `unknown`（`reason_code="conflict"`）へ
    落とし済みのため、区分で判定できる。

    確定（`confirmed`）はさらに根拠参照を持つものに限る——根拠 ID の公開採番への変換
    （`resolve_claim_evidence_ids`）で `evidence_refs` が空になった確定は裏付けを示せない
    ため、未完了回答（停止・失敗）にも載せない（grounded QA の根拠ゲートと同じ規律）。
    """
    return [c for c in claims
            if c.status == "inferred" or (c.status == "confirmed" and c.evidence_refs)]


def render_claims(claims: list[Claim]) -> str:
    """清書プロンプトへ渡す主張構造の整形（`InvestigationState.render()` とは別の専用ビュー
    ——根拠 digest とは独立に「どの主張を確定／推定／不明として書くか」だけを伝える）。"""
    if not claims:
        return ""
    lines = []
    for c in claims:
        if c.status == "confirmed":
            refs = "、".join(c.evidence_refs) if c.evidence_refs else "(根拠参照なし)"
            lines.append(f"[{c.id}] 確定: {c.text}（根拠: {refs}）")
        elif c.status == "inferred":
            lines.append(f"[{c.id}] 推定: {c.text}（理由: {c.reason or '根拠が薄い'}）")
        else:
            lines.append(f"[{c.id}] 不明: {c.text}（理由コード: {c.reason_code}）")
    return "\n".join(lines)


def _span_tuple(span) -> tuple[int, int] | None:
    if (isinstance(span, (list, tuple)) and len(span) == 2
            and isinstance(span[0], int) and not isinstance(span[0], bool)
            and isinstance(span[1], int) and not isinstance(span[1], bool)):
        return (span[0], span[1])
    return None


def _span_loc(span: tuple[int, int] | None) -> str:
    return f" 行 {span[0]}-{span[1]}" if span else ""


def _read_span_from_text(text: str) -> tuple[int, int] | None:
    """`read_around` の本文（各行 `"123: 内容"`）から実際に読んだ行範囲を復元する（`result` 自体には
    行範囲フィールドが無いため）。行番号の書式が崩れていれば None（黙って誤った範囲を作らない）。
    """
    lines = [ln for ln in (text or "").splitlines() if ln]
    if not lines:
        return None
    m0, m1 = _LINE_PREFIX_RE.match(lines[0]), _LINE_PREFIX_RE.match(lines[-1])
    if not (m0 and m1):
        return None
    return (int(m0.group(1)), int(m1.group(1)))


def _capped_fact(head: str, items: list, total: int, sep: str, note_fmt, clean,
                 max_items: int) -> str:
    """要約行を `_STRUCTURAL_FACT_CAP` に収める。項目は先頭から `max_items` 件まで、かつ予算内に
    収まる件数だけ載せ、載せられなかった件数（項目数上限と文字数上限の両方の理由）を必ず
    `note_fmt(n)` の注記で残す——「該当＝列挙」だけが残って全件と書けないようにする。"""
    head = clean(head)
    # head（条件文字列＝モデル生成の pattern 等を含む）も上限内に収める——1 件も載らない run で
    # 要約行が上限を超えて膨らみ、render で【限界】を押し出さないため。
    head_room = max(0, _STRUCTURAL_FACT_CAP - len(clean(note_fmt(total)) if total > 0 else ""))
    if len(head) > head_room:
        head = head[:max(0, head_room - 1)] + "…"
    shown: list = []
    cur = head
    for it in items[:max_items]:
        piece = clean(str(it))
        cand = cur + (": " if not shown else sep) + piece
        remaining = total - (len(shown) + 1)
        note = clean(note_fmt(remaining)) if remaining > 0 else ""
        if len(cand) + len(note) > _STRUCTURAL_FACT_CAP:
            break
        shown.append(piece)
        cur = cand
    omitted = total - len(shown)
    return cur + (clean(note_fmt(omitted)) if omitted > 0 else "")


def _args_summary(args: dict | None, clean) -> str:
    """`clean`（`agentic_search._digest_clean`）は生値全体に先に適用してから `_ARGS_SUMMARY_CAP`
    で切る——先に切ってから clean すると、秘密パターンが切断境界をまたいだ場合に断片化して
    `_redact` の正規表現にマッチしなくなり、境界の位置次第で redaction をすり抜けてしまう。
    """
    args = args or {}
    for key in ("query", "doc_id", "path_prefix", "name_pattern", "doctype", "state", "pattern", "prompt"):
        v = args.get(key)
        if v:
            return clean(str(v).strip())[:_ARGS_SUMMARY_CAP]
    return ""


def _structural_kind(m: dict) -> str:
    return "graph" if "card_meta" in m else "list"   # list_meta/tree_meta は同じ「集計事実」枠


def _structural_fact(m: dict, clean) -> str:
    """`agentic_search.build_synthesis_digest` と同じ体裁の集計・カード要約1行を組む（ただしパスは
    先頭10件＝探索ループの文脈整理用の別上限・清書ダイジェストの予算内連結とは異なる）
    （`clean` は呼び出し元が渡す `agentic_search._digest_clean`）。

    複数項目の列挙区切りは空白付き（`_LIST_SEP`/`_EXCERPT_SEP`）にする——呼び出し元は
    この戻り値を埋め込んだ文字列へ後で再度 `clean()` を掛ける（二重適用自体は無害・
    `render()` docstring 参照）が、区切りに空白が無いと `_KV_SECRET_RE` の `\\S+` が
    区切り記号ごと次の項目まで飲み込み、`key=value` 形の秘密を含む項目の直後の項目が
    丸ごと消えてしまう（`\\S+` は空白でしか止まらない）。
    """
    if "list_meta" in m:
        lm = m.get("list_meta") or {}
        cond_parts = [f"path_prefix={clean(lm['prefix'])}" if lm.get("prefix") else None,
                     f"name_pattern={clean(lm['pattern'])}" if lm.get("pattern") else None,
                     f"doctype={clean(lm['doctype'])}" if lm.get("doctype") else None,
                     f"state={clean(lm['state'])}" if lm.get("state") else None]
        cond = _LIST_SEP.join(c for c in cond_parts if c)
        cond_text = f"（条件: {cond}）" if cond else ""
        matched = m.get("matched_doc_ids") or []
        paths = _LIST_SEP.join(clean(d) for d in matched[:10])
        # 要約はパスを先頭 10 件で切る（文脈整理・査読の入力）。切った事実は清書入力と同じ注記で残す＝
        # 「該当＝列挙」だけを見て全件と書けないようにする。
        rest = f"（他 {len(matched) - 10} 件のパスは未提示＝この一覧は全件として書かない）" if len(matched) > 10 else ""
        return (f"[list_docs] 該当 {lm.get('count', 0)} 件{cond_text}／列挙 "
               f"{lm.get('shown', 0)} 件" + (f": {paths}" if paths else "") + rest)
    if "tree_meta" in m:
        tm = m.get("tree_meta") or {}
        cond_text = f"（path_prefix={clean(tm['prefix'])}）" if tm.get("prefix") else ""
        return (f"[folder_tree] 深さ{tm.get('depth')}{cond_text}／該当フォルダ "
               f"{tm.get('count', 0)} 件／列挙 {tm.get('shown', 0)} 件")
    if "card_meta" in m:
        cm = m.get("card_meta") or {}
        matched = m.get("matched_doc_ids") or []
        docs_text = _LIST_SEP.join(clean(d) for d in matched[:5])
        return (f"[graph] {clean(cm.get('name', ''))}"
               f"（{clean(cm.get('role', ''))}"
               f"{'・' + clean(cm['category']) if cm.get('category') else ''}"
               f"・経路={clean(str(cm.get('path') or ''))}"
               f"{'・辺=' + clean('、'.join(cm['edges'])) if cm.get('edges') else ''}）"
               + (f"／裏付け: {docs_text}" if docs_text else ""))
    return ""


def resolve_claim_evidence_ids(claims: list[Claim], evidence: list[Evidence],
                               combined_evidence_meta: list) -> list[Claim]:
    """DEPTH-2 S1 是正（RV C2）: `claims[].evidence_refs` は `set_claims` 時点では `self.evidence`
    （この調査状態内で安定した採番・並べ替え/重複排除で変わらない）の `ev_id` を指す。清書ダイジェスト
    （`agentic_search.build_synthesis_digest`）と Evidence Packet（`_evidence_packet_evidence`）は
    `combined_evidence_meta`（citation の重複排除・span 統合、構造的根拠の重複排除を経た別の list）を
    `ev-{i+1}` で**別採番**するため、再調査を挟んで両 list の並び・件数がずれると同じ "ev-N" 文字列が
    別の根拠を指しうる。この関数は根拠の同一性（citation は doc_id と span の一致／包含、構造的根拠は
    整形済み事実テキストの一致）で参照を `combined_evidence_meta` 側の ev-N へ書き換える——呼び出し元
    は書き換え後の `claims` を `data["claims"]`（envelope・共有）・`render_claims`（清書入力）の
    両方に使う（同じ ID を指す契約）。対応する Packet エントリが無い参照（read/outline/compare 等、
    Evidence Packet に現れない種別、または `combined_evidence_meta` から重複排除で消えた根拠）は
    黙って落とす——存在しない ev-N を清書・共有へ残さないため（confirmed が結果として空の
    `evidence_refs` になり得る＝この関数の後段では検証し直さない・呼び出し元の責務）。
    """
    from . import agentic_search as _as
    clean = _as._digest_clean
    by_id = {e.ev_id: e for e in evidence}

    def _target_ev_id(ev: Evidence) -> str | None:
        if ev.kind == "citation":
            fallback = None
            for i, m in enumerate(combined_evidence_meta):
                if not isinstance(m, dict) or m.get("matched_doc_ids") is not None:
                    continue   # 構造的根拠エントリ（citation とは別枠）
                if m.get("doc_id") != ev.doc_id:
                    continue
                m_span = _span_tuple(m.get("span"))
                if ev.span is not None and m_span is not None:
                    if m_span[0] <= ev.span[0] and m_span[1] >= ev.span[1]:
                        return f"ev-{i + 1}"   # span 統合後の範囲が元の span を包含＝同一根拠
                    continue
                if fallback is None:
                    fallback = i   # span 無し同士・または比較不能＝同一 doc の先頭候補
            return f"ev-{fallback + 1}" if fallback is not None else None
        if ev.kind in ("list", "graph"):
            for i, m in enumerate(combined_evidence_meta):
                if not isinstance(m, dict) or m.get("matched_doc_ids") is None:
                    continue   # citation エントリ（構造的根拠とは別枠）
                if clean(_structural_fact(m, clean)) == ev.text:
                    return f"ev-{i + 1}"
            return None
        return None   # read/outline/compare は Evidence Packet に現れない種別＝写像先が無い

    out: list[Claim] = []
    for c in claims:
        new_refs: list[str] = []
        for r in c.evidence_refs:
            ev = by_id.get(r)
            target = _target_ev_id(ev) if ev is not None else None
            if target is not None and target not in new_refs:
                new_refs.append(target)
        out.append(Claim(id=c.id, status=c.status, text=c.text, evidence_refs=new_refs,
                         reason=c.reason, reason_code=c.reason_code, origin=c.origin))
    return out


def remap_claim_refs_to_evidence(claims: list[Claim], source_evidence: list[Evidence],
                                 target_evidence: list[Evidence]) -> list[Claim]:
    """DEPTH-2 S4b: worker（下調べ役・`agentic_search.openai_style` の `final_synthesis=False`
    経路）が自分専用のローカル `InvestigationState.evidence`（`source_evidence`）基準で組んだ
    主張の `evidence_refs` を、親 `InvestigationState`（`providers/base.py::
    _ingest_sub_final_into_state` が同じ根拠を既に `add_tool_result` で取り込み済みの
    `target_evidence`）の実際の ev_id へ書き換える。

    `resolve_claim_evidence_ids`（citation は doc_id＋span、structural はテキストの一致で
    `combined_evidence_meta`—辞書の list—へ書き換える）と役割は同じだが、こちらは両側とも
    `Evidence` オブジェクト同士の内容一致（citation/read は kind＋doc_id＋span［＋read の
    locator］、list/graph/compare は kind＋text）で突き合わせる——worker のローカル状態は
    Evidence Packet 化前（`_commit_evidence`/`combined_evidence_meta` を経ていない）のため、
    辞書形ではなく親の `state.evidence` を直接の突き合わせ先にする。対応する親 Evidence が
    見つからない参照（内容一致しない・親側の重複排除で消えた・read/outline 等その他の種別で
    未対応）は黙って落とす（`resolve_claim_evidence_ids` と同じ「存在しない ev-N を残さない」
    契約——confirmed が結果として空の `evidence_refs` になり得る・呼び出し元の
    `InvestigationState.set_claims` が最終検証する）。`origin` は書き換えない（呼び出し元が
    `set_claims(..., origin=...)` で明示的に付け直す）。
    """
    by_id = {e.ev_id: e for e in source_evidence}

    def _target(ev: Evidence) -> Evidence | None:
        for t in target_evidence:
            if t.kind != ev.kind:
                continue
            if ev.kind in _TEXT_KEYED_DEDUPE_KINDS:
                if t.text == ev.text:
                    return t
                continue
            if t.doc_id != ev.doc_id or t.span != ev.span:
                continue
            if ev.kind == "read" and ev.span is None and t.locator != ev.locator:
                continue
            return t
        return None

    out: list[Claim] = []
    for c in claims:
        new_refs: list[str] = []
        for r in c.evidence_refs:
            src = by_id.get(r)
            tgt = _target(src) if src is not None else None
            if tgt is not None and tgt.ev_id not in new_refs:
                new_refs.append(tgt.ev_id)
        out.append(Claim(id=c.id, status=c.status, text=c.text, evidence_refs=new_refs,
                         reason=c.reason, reason_code=c.reason_code, origin=c.origin))
    return out


_RENDER_TRUNCATION_NOTICE_TMPL = "（古い根拠 {n} 件は省略・直近を優先）"

# list（list_docs/glob_search の集計）・graph・compare の事実は doc_id/span が常に None のため、
# text（条件・件数を含む整形済み文字列）まで鍵に含めないと異なる条件の集計が同一エントリへ
# 潰れてしまう——citation/read/outline（doc_id/span が実体を指す）はこの2つだけで同一性が決まる
# ため text を鍵に含めない（同 doc/span の再取得＝本文が異なっても1件に統合する契約・
# 下記 `_upsert` 参照）。
_TEXT_KEYED_DEDUPE_KINDS = frozenset({"list", "graph", "compare"})

# verification の「確からしさ」順位——同一 doc/span への複数回の取得で値が食い違うとき、
# 高い方を残す（未検証から確定値への昇格はもちろん、"span_unmatched" から "verified" への
# 昇格も許す・逆方向の格下げはしない）。"structural"（read 精読・list/graph 等の構造事実）は
# 検証ステップ自体が無い＝最初から確定扱いのため "verified" と同格。
_VERIFICATION_RANK = {"unverified": 0, "span_unmatched": 1, "verified": 2, "structural": 2}


@dataclass
class InvestigationState:
    """1質問1調査状態。`question`/`scope` は記録用（`render()` 本文には出さない・将来の参照用）。"""
    question: str
    scope: dict
    evidence: list[Evidence] = field(default_factory=list)
    tool_log: list[ToolCall] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # limits: この run 中に実際に当たった内部制限のカウンタ（緩める/強めるための値ではなく計測専用
    # ・利用統計の「打ち切りの内訳」の元データ）。回数系（*_clipped/*_compactions/auto_continues）は
    # int・当たったか系（total_budget_hit/synthesis_truncated/depth_escalated）は bool。
    # `depth_escalated`＝必要な根拠種別が揃わず深さを1段だけ自動で引き上げたターン（`providers/
    # base.py` の巡ループが唯一の書き手）。
    limits: dict = field(default_factory=lambda: {
        "tool_result_clipped": 0, "total_budget_hit": False, "context_compactions": 0,
        "synthesis_truncated": False, "search_truncated": 0, "auto_continues": 0,
        "depth_escalated": False})
    # DEPTH-2 S1: 主張と採否の状態（`set_claims` が検証済みのものだけを保持する・既定は空＝
    # 査読の全回答破棄をこの状態で置き換えていない通常のターンでは触らないまま）。
    claims: list = field(default_factory=list)
    # DEPTH-2 S5: evaluator の指摘（`Finding`）。巡ループが `apply_findings` 経由でのみ追記する。
    findings: list = field(default_factory=list)
    # DEPTH-2 S2（§2.7）: `write_output_file` ツールが台帳登録に成功するたびに1件追記する
    # （`{"rel_path","download_url",...}`・Codex の created_files カードと同じ形）。既定は空＝
    # このツールを一度も呼ばなかった/一度も成功しなかったターンでは触らないまま。
    created_files: list = field(default_factory=list)
    # ツール例外・読取I/O失敗の障害種別（閉じた語彙・回復可能なものだけを個別に記録する）。
    # 単発フォールバックへの縮退可否判定（`providers/base.py`）が使う——記録があること自体は
    # 「縮退してよい」の必要条件でしかなく、`non_recoverable_failure` が立っていれば禁止する。
    backend_failures: dict = field(default_factory=lambda: {
        "fulltext": False, "graph": False, "read_io": False})
    # 回復不可な障害（プログラムの欠陥等・接続断/タイムアウト/読取I/O以外の例外）を検知したら真。
    # 一度立てたら run 内で戻さない——同一ターンに回復可能な障害と混在しても縮退を禁止する側に倒す。
    non_recoverable_failure: bool = False
    # グラフのスキーマ世代不一致（`GraphSchemaEraError`）をこのターンで検知したか。接続断
    # （`backend_failures["graph"]`）とは別状態として持つ——利用者への通知文言（再取り込み待ち／
    # 接続できない）と統計の項目が別（`graph_reingest_required`／`backend_unavailable_graph`）。
    graph_schema_era_mismatch: bool = False

    def mark_backend_failure(self, kind: str) -> None:
        """回復可能な障害（接続断・タイムアウト・読取I/O）を種別ごとに記録する（未知の kind は無視）。"""
        if kind in self.backend_failures:
            self.backend_failures[kind] = True
            field = _BACKEND_LIMIT_FIELD.get(kind)
            if field:
                self.mark_limit(field)

    def mark_graph_schema_era_mismatch(self) -> None:
        """グラフの世代不一致を記録する（一度真になったら run 内で戻さない）。"""
        self.graph_schema_era_mismatch = True
        self.mark_limit(GRAPH_REINGEST_LIMIT_FIELD)

    def mark_non_recoverable_failure(self) -> None:
        """回復不可な障害（プログラムの欠陥等）を記録する。一度真になったら run 内で戻さない。"""
        self.non_recoverable_failure = True

    def apply_findings(self, raw, round_no: int, verdict: str | None = None) -> bool:
        """この巡の evaluator の指摘を取り込み、反証された主張を同じ巡の中で採用不可へ落とす。

        未解決から「解決」へ遷移させるのは、**根拠が進んだ**（対象主張の `evidence_refs` が
        指摘時点より増えた、または区分が確定へ戻った）かつ **orchestrator が確認した**（この巡の
        判定が `sufficient`、または当該指摘が再指摘されなかった）の両方が成立した指摘だけ——
        再指摘が無いことだけでは解決にしない（未解決のまま次巡の指示に残す・§2.4）。

        同じ `id` の指摘は既存の行を更新する（撤回＝`state="withdrawn"` もこの更新で反映され、
        次巡の指示から落ちる）。形が不正（`parse_findings` が `None`）なら何も変更せず
        `False`——主張の採否を推測で動かさない。
        """
        parsed = parse_findings(raw, round_no)
        if parsed is None:
            return False
        by_claim = {c.id: c for c in self.claims}
        reraised = {f.id for f in parsed}
        for old in self.findings:
            if old.state != "open" or not old.claim_id:
                continue
            c = by_claim.get(old.claim_id)
            if c is None:
                continue
            progressed = (len(c.evidence_refs) > old.ev_count
                          or (c.status == "confirmed" and old.claim_status != "confirmed"))
            if progressed and (verdict == "sufficient" or old.id not in reraised):
                old.state = "resolved"
        existing = {f.id: f for f in self.findings}
        for f in parsed:
            c = by_claim.get(f.claim_id)
            f.ev_count = len(c.evidence_refs) if c is not None else 0
            f.claim_status = c.status if c is not None else ""
            old = existing.get(f.id)
            if old is not None:
                # 同じ指摘の再送・撤回は既存行を更新する（旧 open 行を残して次巡に持ち越さない）。
                (old.round_no, old.claim_id, old.text, old.refutes, old.state,
                 old.ev_count, old.claim_status) = (f.round_no, f.claim_id, f.text, f.refutes,
                                                    f.state, f.ev_count, f.claim_status)
            else:
                self.findings.append(f)
        self._apply_open_refutations()
        return True

    def _apply_open_refutations(self) -> None:
        """未解決の反証を主張へ反映する（反証＝その時点で採用不可）。閉じた語彙
        （`_CLAIM_UNKNOWN_REASON_CODES`）の `conflict` を付けて不明へ落とす——清書・未完了回答・
        公開 envelope の全てから外れる。`set_claims` からも呼ぶ＝後から生成・再取得された主張が
        未解決の反証を上書きして再採用されることはない。"""
        by_id = {c.id: c for c in self.claims}
        for f in self.findings:
            if not (f.refutes and f.state == "open"):
                continue
            c = by_id.get(f.claim_id)
            if c is None or c.status == "unknown":
                continue
            c.status = "unknown"
            c.reason_code = "conflict"
            c.reason = f.text or c.reason

    def set_claims(self, raw, origin: str = "synthesis") -> bool:
        """`parse_claims` で検証した主張配列を採否状態として保持する。空リスト・不正な形は
        何も変更せず False を返す（呼び出し元は既存の honest failure へ落とす・§2.5）。

        `parse_claims` は構造（キー・閉じた語彙）だけを見る純関数のため、`status == "confirmed"`
        の `evidence_refs` がこの調査で実在する根拠を指すかはここで検証する（`self.evidence` を
        持つのは `InvestigationState` だけ）。confirmed が `evidence_refs` を持たない、または
        存在しない `ev_id` を含む場合は claims 全体を不正として扱い、何も変更せず False を返す
        （RV C1: 裏付けの無い主張を確定として清書へ渡さない・worker 由来（DEPTH-2 S4b）でも
        同じ規律を適用する）。

        `origin`（省略可・既定 `"synthesis"`）: `Claim.origin` 参照。呼び出し元が
        `"worker"`（下調べ役の一次判断）を明示できる。

        保持した直後に未解決の反証を再適用する（`_apply_open_refutations`）——反証済みの ID を
        同じ内容で返し直しても再採用されない。
        """
        parsed = parse_claims(raw, origin=origin)
        if not parsed:
            return False
        valid_ids = {e.ev_id for e in self.evidence}
        for c in parsed:
            if c.status == "confirmed" and (
                    not c.evidence_refs or not all(r in valid_ids for r in c.evidence_refs)):
                return False
        self.claims = parsed
        self._apply_open_refutations()
        return True

    def bump_limit(self, key: str, n: int = 1) -> None:
        """回数系カウンタを加算する（キーは固定語彙＝呼び出し側が `limits` の既定キーだけを渡す）。"""
        self.limits[key] = self.limits.get(key, 0) + n

    def mark_limit(self, key: str) -> None:
        """当たったか系（bool）を真にする（一度真になったら run 内で戻さない）。"""
        self.limits[key] = True

    def _find(self, kind: str, doc_id, span, text: str, locator: str | None = None) -> Evidence | None:
        """重複排除の鍵は kind＋doc_id＋span（citation/read）——同じ doc/span への複数回の取得は
        本文が違っても常に1件に統合する（`_upsert` 参照）。doc_id/span が常に None の集計事実
        （list/graph/compare）だけは text も鍵に含める（さもないと条件の異なる集計が1件に潰れる）。

        kind="read" かつ `span is None`（原本読取ツールの精読＝行番号でなく
        `locator` 文字列で位置を表す）は `locator` も鍵に含める——さもないと同じ doc_id への
        別シート/別ページの読み取りが `(doc_id, span=None)` だけで同一とみなされ1件に潰れる。
        `span` が数値範囲を持つ場合（read_around/read_doc）は locator を鍵に含めない（既存契約を
        変えない・そちらは常に locator=None）。
        """
        for e in self.evidence:
            if e.kind != kind or e.doc_id != doc_id or e.span != span:
                continue
            if kind == "read" and span is None and e.locator != locator:
                continue
            if kind in _TEXT_KEYED_DEDUPE_KINDS and e.text != text:
                continue
            return e
        return None

    def _upsert(self, *, kind: str, doc_id, span, text: str, source_tool: str,
               verification: str, extra_quotes: list[str] | None = None,
               locator: str | None = None, text_truncated: bool = False) -> None:
        if not text and doc_id is None:
            return   # 中身の無いエントリは記録しない（例: 精読が空文字を返した等）
        existing = self._find(kind, doc_id, span, text, locator=locator)
        if existing is not None:
            # 同 doc/span の再取得——ev_id は変えない。本文が食い違う場合（citation/read のみ
            # 起こりうる・list/graph/compare は text も鍵のため常に一致）は長い方を主本文として
            # 残し、短い方は事実を黙って捨てず extra_quotes へ退避する。`text_truncated` は
            # 採用した側（主本文になった方）の値を引き継ぐ——切られていない長い本文に更新
            # されれば False に戻り、逆に長い方が切断済みならそのまま True になる。
            if existing.text != text:
                if len(text) > len(existing.text):
                    longer, shorter, longer_truncated = text, existing.text, text_truncated
                else:
                    longer, shorter, longer_truncated = existing.text, text, existing.text_truncated
                existing.text = longer
                existing.text_truncated = longer_truncated
                if shorter and shorter not in existing.extra_quotes:
                    existing.extra_quotes.append(shorter)
            else:
                # 本文が同一（例: 再取り込み経路で同じ切り詰め済みテキストが戻ってきた）——切断の
                # 事実を黙って消さないよう、どちらかが切断済みなら真のまま保つ（OR・降格しない）。
                existing.text_truncated = existing.text_truncated or text_truncated
            # 検証状態は格上げのみ許す（"verified"/"span_unmatched" が確定した後に "unverified" で
            # 上書きして退行させない・同格以上のときだけ新しい値を採用）。
            if _VERIFICATION_RANK.get(verification, 0) >= _VERIFICATION_RANK.get(existing.verification, 0):
                existing.verification = verification
            for q in (extra_quotes or []):
                if q not in existing.extra_quotes:
                    existing.extra_quotes.append(q)
            return
        self.evidence.append(Evidence(
            ev_id=f"ev-{len(self.evidence) + 1}", kind=kind, doc_id=doc_id, span=span, text=text,
            source_tool=source_tool, verification=verification, extra_quotes=list(extra_quotes or []),
            locator=locator, text_truncated=text_truncated))

    def _add_gap(self, gap: str) -> None:
        """同じ文面の限界は1本だけ持つ（同じ doc の精読を再取り込みするたびに同一行が増えて、
        清書入力の限界枠（先頭 20 件）を埋め尽くさないようにする）。"""
        if gap not in self.gaps:
            self.gaps.append(gap)

    def add_tool_result(self, name: str, args: dict, result, cites: list | None,
                        evidence_meta: list | None) -> None:
        """1回のツール実行の結果を状態へ反映する（重複は ev_id で吸収・同 doc/span は1件）。

        `cites`/`evidence_meta` は `run_tool` のこの呼び出し1回分（`combined_evidence_meta` と同じ
        「先頭 `len(cites)` 件が `cites` と1対1」の契約——`evidence_meta` に構造的根拠だけを渡す
        場合や省略（None）も可、その場合は `matched_doc_ids` を持つエントリだけが構造的根拠として
        処理される）。`evidence_meta[i]["verification_method"]` があれば "span_verified"→"verified"／
        "span_unmatched"→そのまま反映し、無ければ（探索ループ内・検証前）"unverified" のまま持つ
        （検証は `agentic_search._commit_evidence` がループの最後に1回だけ行うため、探索途中の
        citation は本来まだ未検証——ここで嘘の "verified" を名乗らない）。
        """
        from . import agentic_search as _as   # 遅延 import（循環回避・_digest_clean/_tool_hit_count 再利用）
        cites = cites or []
        evidence_meta = evidence_meta or []
        result_dict = result if isinstance(result, dict) else {}
        has_error = isinstance(result_dict.get("error"), str)   # 空文字列の error も「有り」扱い（既存契約）
        # clean は生値全体に先に適用してから各上限（_GAP_MESSAGE_CAP/_TOOL_ERROR_CAP）で切る——
        # 先に切ってから clean すると、秘密パターンが切断境界をまたいだ場合に断片化して
        # `_redact` の正規表現にマッチしなくなり得る（render() 側の最終 clean は無傷の断片にしか
        # 効かない）。ここで一度 clean した値を `ToolCall.error`/gaps の両方が使い回すことで、
        # 上限の異なる2箇所（gaps は200字・`_fmt_tool` は80字）が同じ安全な生値を切るようにする。
        error = _as._digest_clean(result_dict["error"]) if has_error else None
        hits = _as._tool_hit_count(name, result_dict) if not has_error else None
        if hits == 0 and name in _READ_KIND_TOOL_NAMES and result_dict.get("text"):
            # 精読の結果に本文があれば「0件」ではない（行番号を持たない再取り込み結果は行数を
            # 数えられない）——gaps と呼び出し記録の両方から偽の 0 件を消す。
            hits = None
        truncated = bool(result_dict.get("text_truncated") or result_dict.get("truncated_docs")
                        or result_dict.get("file_truncated") or result_dict.get("truncated")
                        or result_dict.get("folders_truncated"))
        # list_docs の `truncated` は「一覧に次ページがある」（本文の切断ではない）。続きを取れば
        # 解消する事実で、取らなかった不足は list_meta（該当 N 件／列挙 M 件）に残るため、
        # 消せない gaps には積まない（呼び出し記録の打ち切り印は残す）。
        page_truncated = name == "list_docs" and truncated
        # 下調べ役の精読を親へ再取り込みする経路（`reingested`）: 切断の事実は子の gaps と
        # `text_truncated` で既に伝わっているため、親側で切断 gap を作り直さない（文面違いの重複を防ぐ）。
        reingested = bool(result_dict.get("reingested"))
        args_summary = _args_summary(args, _as._digest_clean)
        self.tool_log.append(ToolCall(name=name, args_summary=args_summary, hits=hits,
                                      truncated=truncated, error=error))

        # gaps は機械的に作る（モデルの散文は事実として取り込まない）。
        label = f"{name}『{args_summary}』" if args_summary else name
        if error:
            self.gaps.append(f"{label}: {error[:_GAP_MESSAGE_CAP]}")
        elif hits == 0:
            self.gaps.append(f"{label}: 0件")
        elif hits is None and result_dict.get("degrade_reason"):
            self.gaps.append(f"{name}: 索引なし（キーワード一致のみ）")
        elif (name == "compare_documents"
              and result_dict.get("status") in ("unsupported", "needs_disambiguation")):
            # 比較できなかった事実は限界として残す（error キーを持たない正常応答のため上の分岐に入らない）。
            reason = _as._digest_clean(str(result_dict.get("reason") or result_dict.get("status")))[:_GAP_MESSAGE_CAP]
            self._add_gap(f"{label}: 機械的な突合せができず未確認（{reason}）")
        if (name in ("ripgrep_search", "es_search") and result_dict.get("truncated") and not reingested
                and (result_dict.get("text_truncated") or result_dict.get("truncated_docs")
                     or result_dict.get("file_truncated"))):
            # 本文の切断と併発したときもヒット上限の事実は独立に残す（下の elif 連鎖は本文側を選ぶ）。
            self._add_gap(f"{label}: 検索ヒットが上限で打ち切り（全件ではない・範囲を絞るか別の語で）")
        if truncated and not page_truncated and not reingested:
            if name == "graph_neighbors":
                # 近傍一覧の打ち切り（本文の切断ではない）＝総数と一部しか返っていない事実を残す。
                self._add_gap(f"{label}: 近傍が上限で打ち切り（全 {result_dict.get('count')} 件中の一部）")
            elif name in ("ripgrep_search", "es_search") and not result_dict.get("text_truncated") \
                    and not result_dict.get("truncated_docs") and not result_dict.get("file_truncated"):
                # ヒット数上限の打ち切りだけ（本文の切断ではない）＝母集団の一部しか見ていない事実を残す。
                self._add_gap(f"{label}: 検索ヒットが上限で打ち切り（全件ではない・範囲を絞るか別の語で）")
            elif name == "xlsx_sheets":
                # シート一覧の打ち切り（本文は返さないツール・続きを取る引数は無い）。
                self._add_gap(f"{label}: シート一覧が上限で打ち切り（全件ではない）")
            elif name == "folder_tree" and result_dict.get("folders_truncated"):
                # フォルダ一覧の打ち切り（続きを取る引数は無い）＝総数と一部しか返っていない事実を残す。
                self._add_gap(f"{label}: フォルダ一覧が上限で打ち切り（全 {result_dict.get('count')} 件中の一部）")
            elif name in ("glob_search", "doc_outline") and result_dict.get("truncated"):
                # パス一覧／見出し一覧の打ち切り。doc_outline で `file_truncated` も立つときは総数も過小。
                under = "・総数も過小" if result_dict.get("file_truncated") else ""
                self._add_gap(f"{label}: 一覧が上限で打ち切り（全 {result_dict.get('count')} 件中の一部{under}）")
            elif name == "doc_outline":
                # `file_truncated` だけ＝文書が大きく読み切れていない（見出し件数・総行数は過小）。
                self._add_gap(f"{label}: 文書が大きく見出しを読み切れていない（件数・総行数は過小）")
            else:
                doc_part = f" doc {result_dict.get('doc_id')}" if result_dict.get("doc_id") else ""
                self._add_gap(f"{name}{doc_part}: 本文が上限で切断")

        # 構造的根拠（list_docs/folder_tree/graph_neighbors の集計・カード要約）。
        for m in evidence_meta:
            if isinstance(m, dict) and m.get("matched_doc_ids") is not None:
                fact = _structural_fact(m, _as._digest_clean)
                if fact:
                    self._upsert(kind=_structural_kind(m), doc_id=None, span=None,
                                text=_as._digest_clean(fact), source_tool=name, verification="structural")

        # citation（grep/es_search 由来の quote）。
        for i, c in enumerate(cites):
            if not isinstance(c, dict):
                continue
            doc_id = c.get("doc_id")
            if not doc_id:
                continue
            span = _span_tuple(c.get("span"))
            quote = _as._digest_clean(c.get("quote") or "")[:_CITATION_QUOTE_CAP]
            verification = "unverified"
            if i < len(evidence_meta) and isinstance(evidence_meta[i], dict):
                vm = evidence_meta[i].get("verification_method")
                if vm == "span_verified":
                    verification = "verified"
                elif vm == "span_unmatched":
                    verification = "span_unmatched"
            self._upsert(kind="citation", doc_id=doc_id, span=span, text=quote,
                        source_tool=name, verification=verification)

        # 精読（read_around/read_doc・S3b 原本読取ツール6本）: 本文は既に run_tool 側で `_redact`
        # 済み——ここでは制御文字除去（改行を1行の要約行へ畳む）と保存上限（`_READ_TEXT_CAP_BYTES`）
        # だけ追加で適用する。原本読取ツール（xlsx_range/docx_paragraphs/pptx_slides/pdf_pages/
        # file_head・本文を返す 5 本）は結果に `doc_id`/`text`（run_tool が合成・
        # `_doc_reader_text_locator` 参照）を持つため、read_around/read_doc と同じ経路で
        # read_evidence（`InvestigationState`・根拠ゲート）に載せる。xlsx_sheets（シート一覧のみ・
        # 本文を読んでいない）は精読にしない。
        if name in _READ_KIND_TOOL_NAMES and not error:
            text = result_dict.get("text")
            if text:
                cleaned_full = _as._digest_clean(text)
                # 保存時にここで新たに切ったか（読取ツール自体が既に打ち切っていた場合＝上の
                # `truncated` とは独立に判定する——`truncated` が False でも、精読本文がこの
                # 保存上限を超えていれば末尾が清書ダイジェストから欠落する）。
                save_truncated = len(cleaned_full.encode("utf-8")) > _READ_TEXT_CAP_BYTES
                cleaned = (_as._clip_utf8_bytes(cleaned_full, _READ_TEXT_CAP_BYTES)
                          if save_truncated else cleaned_full)
                if name == "read_doc":
                    sl, el = result_dict.get("start_line"), result_dict.get("end_line")
                    span = _span_tuple((sl, el)) if isinstance(sl, int) and isinstance(el, int) else None
                elif name == "read_around":
                    span = _read_span_from_text(text)
                else:
                    # S3b 6ツール: 位置情報は text 本文の行頭表記（"Sheet1!A1:D20"・"段落3" 等・
                    # `locator` フィールド）に残す——read_around の行番号のような単純な数値span化はしない。
                    span = None
                doc_id = result_dict.get("doc_id")
                if save_truncated and not reingested:
                    # `truncated`（読取ツール自体の打ち切り）とは別の、保存時の切断だけを表す専用
                    # gap——黙って行うと `build_synthesis_digest` の清書入力から末尾の条件・例外が
                    # 消えたことに誰も気づけない。
                    self._add_gap(f"{name} doc {doc_id}{_span_loc(span)}: 保存時に本文を "
                                  f"{len(cleaned)} 字で切断（末尾は清書に渡らない）")
                # 原本読取ツール6本の `locator`（read_around/read_doc は常に None）を同一性の
                # 鍵にも表示にも渡す（`_upsert`/`_find` 参照）。
                self._upsert(kind="read", doc_id=doc_id, span=span, text=cleaned,
                            source_tool=name, verification="structural",
                            locator=result_dict.get("locator") if span is None else None,
                            text_truncated=truncated or save_truncated)

        # ファイル名検索（glob_search）: 見つけたパス一覧そのものを保存する（件数だけでは文脈整理後
        # に「何を見つけたか」が失われる）。doc_id/span は複数 doc 横断のため常に None——同じ
        # パターンの再取得は1件に統合、異なるパターンは別条件として残る（_TEXT_KEYED_DEDUPE_KINDS）。
        if name == "glob_search" and not error:
            paths = [p for p in (result_dict.get("paths") or []) if isinstance(p, str)]
            count = result_dict.get("count", 0)
            if paths or count:
                pattern = str((args or {}).get("pattern") or "").strip()
                cond_text = f"（パターン: {_as._digest_clean(pattern)}）" if pattern else ""
                head = f"[glob_search] 該当 {count} 件{cond_text}／列挙 {len(paths)} 件"
                self._upsert(kind="list", doc_id=None, span=None,
                            text=_capped_fact(head, paths, len(paths), _LIST_SEP,
                                              lambda n: f"（他 {n} 件のパスは未提示＝この一覧は全件として書かない）",
                                              _as._digest_clean, 20),
                            source_tool=name, verification="structural")

        # 見出し構造（doc_outline）: 見出し一覧そのものを保存する（`headings[]["title"]` は
        # run_tool 側で既に `_redact` 済み）。doc_id は実体を指すため citation/read と同じ
        # doc_id/span（span=None＝行範囲ではなく文書全体の見出し要約）の同一性で統合する。
        if name == "doc_outline" and not error:
            headings = [h for h in (result_dict.get("headings") or []) if isinstance(h, dict)]
            if headings:
                head = (f"[doc_outline] {_as._digest_clean(result_dict.get('doc_id'))}: "
                        f"見出し {result_dict.get('count', 0)} 件／列挙 {len(headings)} 件")
                self._upsert(kind="outline", doc_id=result_dict.get("doc_id"), span=None,
                            text=_capped_fact(head, [str(h.get("title") or "") for h in headings], len(headings),
                                              _LIST_SEP,
                                              lambda n: f"（他 {n} 件の見出しは未提示＝この一覧は全件として書かない）",
                                              _as._digest_clean, 20),
                            source_tool=name, verification="structural")

        # 比較（compare_documents）: 差分の要点行（+/- 行の抜粋）まで保存する——件数だけでは
        # 文脈整理後に「何が変わったか」が失われる。doc_id/span は2つの doc を跨ぐため常に None。
        if name == "compare_documents" and result_dict.get("status") == "comparable":
            cc = result_dict.get("compare_conditions") or {}
            left = (cc.get("left") or {}).get("doc_id")
            right = (cc.get("right") or {}).get("doc_id")
            # 複数行にまたがる秘密（PEM 秘密鍵等）が抜粋の10行制限をまたいでも漏れないよう、
            # 行分割・抜粋前に diff 全体へ redact する（`_redact` は `_digest_clean` と違い
            # 制御文字除去をしない＝改行を保ったまま複数行パターンを検出できる）。
            diff = _as._redact(result_dict.get("diff") or "")
            diff_lines = diff.splitlines()
            # 先頭2行（`difflib.unified_diff` が出す `--- fromfile`/`+++ tofile`）だけを位置で
            # ヘッダーとして除外する——内容が偶然 "+++"/"---" で始まる変更行（3行目以降）まで
            # 誤って除外しない。
            body_lines = diff_lines[2:] if len(diff_lines) >= 2 else []
            diff_change_lines = [ln for ln in body_lines if ln.startswith("+") or ln.startswith("-")]
            if left or right:
                head = f"[compare] {left} vs {right}: 差分 {len(diff_change_lines)} 行"
                self._upsert(kind="compare", doc_id=None, span=None,
                            text=_capped_fact(head, diff_change_lines, len(diff_change_lines), _EXCERPT_SEP,
                                              lambda n: f"（他 {n} 行は未提示＝この差分は全件として書かない）",
                                              _as._digest_clean, 10),
                            source_tool=name, verification="structural")

    def _fmt_evidence(self, e: Evidence) -> str:
        if e.kind in ("citation", "read"):
            loc = _span_loc(e.span)
            # `locator`（原本読取ツール・span=None のときだけ持つ）を本文へ前置する
            # ——同じ doc_id で複数エントリになった場合（別シート等）に、どの箇所の精読かを
            # 表示の上でも区別できるようにする（同一性は `_find` 側で既に区別済み）。
            # 精読本文の保存上限は清書予算に合わせて大きい（`_READ_TEXT_CAP_BYTES`）が、render は
            # 査読・文脈整理の入力＝根拠を最優先に配分するため、精読1件が長いと【限界】が無通知で
            # 落ちる。表示は旧来の長さ（`_RENDER_READ_TEXT_CAP`）に切り、全文は清書入力にだけ渡す。
            body_text = e.text
            if e.kind == "read" and len(body_text) > _RENDER_READ_TEXT_CAP:
                body_text = body_text[:_RENDER_READ_TEXT_CAP] + "…"
            text = f"{e.locator}: {body_text}" if (e.kind == "read" and e.locator and body_text) else body_text
            core = f"{e.doc_id}{loc}「{text}」" if text else f"{e.doc_id}{loc}"
            body = f"精読: {core}" if e.kind == "read" else core
            if e.extra_quotes:
                # 精読の退避本文も表示上限を掛ける（保存本文は `_READ_TEXT_CAP_BYTES` まで持つため、
                # 素通しにすると render の1行が再び数千字に膨らんで【限界】を押し出す）。
                def _q(q: str) -> str:
                    if e.kind == "read" and len(q) > _RENDER_READ_TEXT_CAP:
                        return q[:_RENDER_READ_TEXT_CAP] + "…"
                    return q
                extras = _LIST_SEP.join(f"「{_q(q)}」" for q in e.extra_quotes)
                body += f"／別の一致: {extras}"
        else:
            body = e.text
        return f"{e.ev_id}: {body}"

    def _fmt_tool(self, t: ToolCall) -> str:
        # `t.error`/`t.args_summary` は `add_tool_result` が格納時点で既に clean 済み（生値全体を
        # clean してから上限で切っている）——ここでの `[:_TOOL_ERROR_CAP]` は既に安全な文字列を
        # 切るだけなので、秘密パターンを断片化させる心配はない。
        label = f"{t.name}『{t.args_summary}』" if t.args_summary else t.name
        if t.error:
            return f"{label}: エラー（{t.error[:_TOOL_ERROR_CAP]}）"
        if t.hits is not None:
            return f"{label}: {t.hits}件" + ("・打ち切りあり" if t.truncated else "")
        if t.truncated:
            return f"{label}: 打ち切りあり"
        return label

    def render(self, *, max_bytes: int, keep_recent_tools: int = 0) -> str:
        """メイン入力用の要約（B の synthesis digest と同型＋限界＋呼び出し記録）。

        `keep_recent_tools`（既定0＝全件対象）: 末尾 N 件の `tool_log`（＝会話履歴に生の tool
        メッセージとしてまだ残っている分）を「呼び出し記録」セクションから除く——呼び出し元
        （探索ループの文脈整理）が「置換した古い分だけ要約すればよい」ときに渡す。1ラウンド＝1
        ツール呼び出しの通常時は「直近 N ラウンド」と厳密に一致するが、1ラウンドに複数呼び出しが
        あれば近似になる（呼び出し記録の表示だけに影響し、根拠・gaps の正しさには影響しない）。

        redaction 境界: `Evidence.doc_id`/`text`/`extra_quotes`・`ToolCall.name`/`args_summary`/
        `error`・`gaps` はいずれも呼び出し元がツール引数・エラー文字列等から作った動的な文字列
        （多くは既に格納時点で `_digest_clean` 済みだが、`doc_id`/`args_summary`/`error`/`gaps`
        文字列はそうではない）。ハイブリッドでは下調べ役（ローカル/安価な LLM）が組んだこの状態が
        メイン（外部クラウド）の入力へそのまま渡るため、**行を組み立てた直後・バイト数を数える前**
        （＝下記セクション行 `ev_lines`/`tool_lines`/`gap_lines` を作る時点）で `_digest_clean` を
        1回適用する（二重適用は無害）。`_redact` は `"[REDACTED]"` へ置換するため短い値ほど
        **伸びる**——redaction 後の文字列に対して予算判定しないと、判定時は収まっていた行が
        redaction で膨らんで `max_bytes` を超えうる。そのため clean 済みの行だけをバイト数計算・
        予算配分の対象にする（最終出力を組んだ後に改めて clean し直すことはしない）。

        `max_bytes`（最終文字列の UTF-8 バイト数）は**出力全体**に厳密に適用する——根拠だけを
        予算内に収めても、限界・呼び出し記録セクションが大きければ全体は超過し得るため、3セクション
        まとめて予算を配分する。**優先度は 根拠 ＞ gaps ＞ 呼び出し記録**（根拠を全件確保できる
        だけの余白を先に確保し、余りを gaps→呼び出し記録の順に割り当てる。埋まらなければ呼び出し
        記録→gaps の順に切り捨て、それでも根拠が全件入らない場合だけ根拠自体を切り捨てる）。
        根拠を最優先にするのは、gaps/呼び出し記録が多い（例: 同種の0件検索を多数記録した）だけで
        直近に見つかった新しい根拠まで真っ先に消える事故を防ぐため。根拠の切り捨ては**古い方から**
        （`build_synthesis_digest` の「新しい方から打ち切る」とは逆）——ここでの利用者（探索ループの
        文脈整理・査読の再調査）はどちらも「直近の発見が最も関連が高い」ため、直近を優先して残す。
        gaps を呼び出し記録より優先するのは、間引かれた古い根拠の代わりに「何を確認できなかったか」
        という事実だけは呼び出し記録より優先して伝えるため。

        計算量: 各セクションの行ごとのバイト数を一度だけ計算し、末尾からの累積和（新しい方を
        優先して残す配分に使う）で予算内に収まる行数を求める——全体を毎回 `"\\n".join` で
        再結合して長さを測り直すことはしない（件数に対して線形）。
        """
        from . import agentic_search as _as
        clean = _as._digest_clean
        # clean は「行を組み立てた直後・バイト数を数める前」に適用する（上の redaction 境界の
        # docstring 参照）——これ以降のバイト数計算・予算配分は全てこの clean 済みの行を対象にする。
        ev_lines = [clean(self._fmt_evidence(e)) for e in self.evidence]
        kept_tool_log = self.tool_log[:-keep_recent_tools] if keep_recent_tools > 0 else list(self.tool_log)
        tool_lines = [clean(f"- {self._fmt_tool(t)}") for t in kept_tool_log]
        gap_lines = [clean(f"- {g}") for g in self.gaps]

        def _costs(lines: list[str]) -> list[int]:
            return [len(x.encode("utf-8")) for x in lines]

        def _suffix_sums(costs: list[int]) -> list[int]:
            # sums[k] = 末尾 k 行（改行区切り・見出し無し）の UTF-8 バイト数。sums[0] = 0。
            n = len(costs)
            sums = [0] * (n + 1)
            for k in range(1, n + 1):
                sums[k] = costs[n - k] + (1 if k > 1 else 0) + sums[k - 1]
            return sums

        def _max_k(sums: list[int], budget: int) -> int:
            # sums は非減少列——収まる最大の k を後ろから線形に探す（O(n)・再結合なし）。
            k = len(sums) - 1
            while k > 0 and sums[k] > budget:
                k -= 1
            return k

        def _section_bytes(header_bytes: int, sums: list[int], k: int) -> int:
            return (header_bytes + 1 + sums[k]) if k > 0 else 0

        def _max_k_with_header(header_bytes: int, sums: list[int], budget: int) -> int:
            # 見出し込みで budget バイトに収まる末尾 k 行を線形に探す（k=0＝セクション自体を省く）。
            k = len(sums) - 1
            while k > 0 and (header_bytes + 1 + sums[k]) > budget:
                k -= 1
            return k

        ev_costs, gap_costs, tool_costs = _costs(ev_lines), _costs(gap_lines), _costs(tool_lines)
        ev_sums, gap_sums, tool_sums = _suffix_sums(ev_costs), _suffix_sums(gap_costs), _suffix_sums(tool_costs)
        gap_header_bytes = len("【限界】".encode("utf-8"))
        tool_header_bytes = len("【呼び出し記録】".encode("utf-8"))
        n_ev = len(ev_lines)

        ev_full_bytes = ev_sums[n_ev]
        remaining_for_tail = max_bytes - ev_full_bytes - (1 if n_ev > 0 else 0)

        if remaining_for_tail >= 0:
            # 根拠は全件確保できる——余りを gaps（優先）→呼び出し記録の順に割り当てる。
            ev_k, ev_dropped = n_ev, 0
            gap_k = _max_k_with_header(gap_header_bytes, gap_sums, remaining_for_tail)
            gap_bytes = _section_bytes(gap_header_bytes, gap_sums, gap_k)
            if gap_k == len(gap_costs):   # gaps 全件が収まった時だけ残りを呼び出し記録へ回す
                tool_budget = max(0, remaining_for_tail - gap_bytes - (1 if gap_k > 0 else 0))
                tool_k = _max_k_with_header(tool_header_bytes, tool_sums, tool_budget)
            else:   # gaps が既に間引かれた＝呼び出し記録（優先度最低）は0件にする
                tool_k = 0
        else:
            # 根拠だけで（gaps/呼び出し記録抜きでも）入り切らない——tail は空にし、根拠自体を
            # 古い方から間引く（打ち切り注記込みで収まるまで・件数の桁上がりも都度再計算）。
            gap_k = tool_k = 0
            ev_k = _max_k(ev_sums, max_bytes)
            ev_dropped = n_ev - ev_k
            if ev_dropped > 0:
                while True:
                    notice_bytes = len(_RENDER_TRUNCATION_NOTICE_TMPL.format(n=ev_dropped).encode("utf-8"))
                    body_bytes = ev_sums[ev_k] if ev_k > 0 else 0
                    total = notice_bytes + (1 if ev_k > 0 else 0) + body_bytes
                    if total <= max_bytes:
                        break
                    if ev_k == 0:
                        # 通知（固定文言＋件数）すら収まらない極小予算——通知も出さず空にする
                        # （`ev_k==0` を「これ以上削れない」で打ち切ると、収まらない通知だけが
                        # 出力に残り max_bytes を超えてしまう）。
                        ev_dropped = 0
                        break
                    ev_k -= 1
                    ev_dropped += 1

        kept_ev = ev_lines[n_ev - ev_k:] if ev_k > 0 else []
        kept_gap = gap_lines[len(gap_lines) - gap_k:] if gap_k > 0 else []
        kept_tool = tool_lines[len(tool_lines) - tool_k:] if tool_k > 0 else []

        body = ([_RENDER_TRUNCATION_NOTICE_TMPL.format(n=ev_dropped)] if ev_dropped else []) + kept_ev
        tail: list[str] = []
        if kept_gap:
            tail.append("【限界】")
            tail.extend(kept_gap)
        if kept_tool:
            tail.append("【呼び出し記録】")
            tail.extend(kept_tool)
        # セクション間は他の行と同じ単一の改行区切り（空行は挟まない）——予算計算はどの隣接行対も
        # 一律1バイトの改行と見なしているため、ここで空行（2バイト分の改行）を挟むと計算が
        # 一致しなくなる（見出し「【限界】」等それ自体が区切りとして十分視認できる）。
        body.extend(tail)
        # ここに来る行（ev_lines/gap_lines/tool_lines）は全て構築時点で clean 済み——見出し・
        # 打ち切り注記は固定文言（動的値は件数のみ）のため追加の clean は不要（二重に行うと
        # せっかく予算内に収めた計算がまた redaction による伸長リスクを負う＝上の docstring 参照）。
        return "\n".join(body)


# ---- 根拠の種別（§0(b) の閉集合）------------------------------------------------------------
# 評価（見直し）は根拠の**量**ではなく、質問の型ごとに必要な**種別**が揃っているかで判定する。
# 語彙は閉集合で、統計（`chat-round` の `missing_codes`）・回答の告知で同じ語を使う。
EVIDENCE_KINDS = ("source", "spec_doc", "definition", "log_config", "callgraph")

# 告知・プロンプトで使う平文ラベル（専門用語ゼロ・画面にそのまま出せる語）。
EVIDENCE_KIND_LABELS = {
    "source": "ソース", "spec_doc": "設計書", "definition": "定義",
    "log_config": "ログ・設定", "callgraph": "呼出関係"}

# レンズ別の必須種別（ソースは常に必須）。影響調査の「定義」は任意（必須には入れない）。
LENS_REQUIRED_EVIDENCE_KINDS = {
    "qa": ("source", "spec_doc"),
    "impact": ("source", "callgraph"),
    "troubleshoot": ("source", "log_config"),
    "author": ("source", "spec_doc"),
}

# 定義（DDL・copybook・データ構造の宣言）。コード層の拡張子だが「実装そのもの」ではないため
# `source` と分ける。設定ファイルはここではなく `log_config`（下記）。
_DEFINITION_EXT = frozenset({".sql", ".cpy", ".copybook", ".json", ".xml", ".toml"})
# ログ・設定（運用ログ・貼り付けた表・アプリの設定ファイル）。トラブルシュートの必須種別は
# 「ソース＋ログ・設定」で、設定値を読んで初めて症状の条件が確かめられるため、設定ファイル系は
# `definition` ではなくこちらに置く。
_LOG_CONFIG_EXT = frozenset({
    ".log", ".csv", ".tsv",
    ".properties", ".yaml", ".yml", ".ini", ".cfg", ".conf"})
# 設計書の原本（決定的MD の元）。派生MD は `{rel}.md`＝原本拡張子を含む名前で持つ
# （`ingest/office_md.py`）ため、`.md` を剥がした内側の拡張子で判定できる。
_SPEC_ORIGINAL_EXT = frozenset({".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".pdf"})
_MD_EXT = frozenset({".md", ".markdown"})
# 素のテキスト資料。設計書が `.txt`/`.rtf` で運用されている world があるため、ログ側ではなく
# 設計書として扱う（ログ側に置くと、その world で「設計書は範囲に無い」と事実に反する明示が出る）。
_SPEC_TEXT_EXT = frozenset({".txt", ".rtf"})

# グラフ照会（`graph_neighbors`）は呼出関係そのもの。グラフが無い・不調のときは、ソースに対する
# 呼出し検索（`ripgrep_search`）が代替になる（§0(b)「無ければ grep の呼出し検索で代替」）。
_CALLGRAPH_TOOLS = frozenset({"graph_neighbors", "find_paths"})
_CALLGRAPH_FALLBACK_TOOLS = frozenset({"ripgrep_search"})


def evidence_kind_of_doc(doc_id) -> str | None:
    """doc_id（rel_path）から根拠種別を決める純関数（ファイル本文は読まない）。

    判定できない（doc_id が無い・未対応の付帯物）ときは `None`——不足の判定には数えない。
    層の近似（`layer.layer_of`＝`CODE_EXT` メンバーシップ）を最後の分岐に使うため、
    アナライザ登録簿に新しい言語が増えれば自動で `source` 側に載る。
    """
    if not isinstance(doc_id, str) or not doc_id.strip():
        return None
    from pathlib import PurePosixPath
    name = PurePosixPath(doc_id.replace("\\", "/")).name.lower()
    stem, _, ext = name.rpartition(".")
    ext = f".{ext}" if stem else ""
    if ext in _MD_EXT:
        # 派生MD（`{原本名}.md`）は原本の拡張子で種別が決まる。素の `.md`/`.markdown` は
        # 既存の表示 doctype（`corpus_docs._NONCODE_DOCTYPE`）と同じく設計書として扱う。
        inner = PurePosixPath(stem).suffix.lower()
        if inner in _LOG_CONFIG_EXT:
            return "log_config"
        if inner in _DEFINITION_EXT:
            return "definition"
        return "spec_doc"
    if ext in _SPEC_ORIGINAL_EXT or ext in _SPEC_TEXT_EXT:
        return "spec_doc"
    if ext in _LOG_CONFIG_EXT:
        return "log_config"
    if ext in _DEFINITION_EXT:
        return "definition"
    from . import layer as layer_mod   # 葉ノードのまま保つための関数内 import
    return "source" if layer_mod.layer_of(doc_id) == "code" else None


def evidence_kind_of(doc_id, source_tool: str = "") -> str | None:
    """根拠1件の種別。呼出関係だけは doc_id ではなく取得手段（ツール）で決まる——
    グラフ照会は常に呼出関係、ソースに対する呼出し検索（ripgrep）はグラフが無い環境での代替。
    """
    if source_tool in _CALLGRAPH_TOOLS:
        return "callgraph"
    kind = evidence_kind_of_doc(doc_id)
    if kind == "source" and source_tool in _CALLGRAPH_FALLBACK_TOOLS:
        return "callgraph"
    return kind


def evidence_kinds_of(evidence: list) -> set:
    """根拠の束が実際に持っている種別の集合。"""
    out = set()
    for e in evidence or []:
        k = evidence_kind_of(e.doc_id, e.source_tool)
        if k:
            out.add(k)
        # 呼出し検索はソースの本文でもある——両方の種別として数える（片方に倒すと、
        # ripgrep だけで調べたターンの `source` が永久に不足になる）。
        if k == "callgraph" and e.source_tool in _CALLGRAPH_FALLBACK_TOOLS:
            out.add("source")
    return out


def claim_evidence_kinds(claim: Claim, evidence: list) -> set:
    """主張1件が実際に参照している根拠の種別（`evidence_refs` が指す `Evidence` から導く）。"""
    refs = set(claim.evidence_refs or [])
    return evidence_kinds_of([e for e in (evidence or []) if e.ev_id in refs])


def required_evidence_kinds(lens: str) -> tuple:
    """レンズ別の必須種別（未知のレンズは仕様問い合わせと同じ扱い）。"""
    return LENS_REQUIRED_EVIDENCE_KINDS.get(lens, LENS_REQUIRED_EVIDENCE_KINDS["qa"])


def missing_code_for_kind(kind: str) -> str:
    """不足種別 → `missing_codes` の閉じた語彙（本文は持たない）。"""
    return f"{'spec' if kind == 'spec_doc' else 'log' if kind == 'log_config' else kind}_missing"


def evidence_kind_labels(kinds) -> str:
    """種別の集合を平文ラベルの読み下しにする（告知・プロンプト用・順序は `EVIDENCE_KINDS` 固定）。"""
    return "・".join(EVIDENCE_KIND_LABELS[k] for k in EVIDENCE_KINDS if k in set(kinds or ()))
