"""途中経過だけで閉じたターンの判定（自動継続の要否）と回答見出しの選び方。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import re


# ---- 回答 headline の選び方（進行中の作業宣言を見出しにしない）----
# LLM を使わず決定的に、「結論を含む最後の agent_message」を優先し、末尾の作業宣言を落として選ぶ。
# 語尾は「次アクション動詞」の curated list に限定する（汎用の「〜します」を全部弾くと所見まで落ちるため）。
_PROGRESS_VERBS = (
    "確認します", "切り分けます", "調べます", "特定します", "検討します", "探します",
    "洗い出します", "整理します", "確かめます", "突き止めます", "チェックします", "見ていきます",
    "精査します", "分析します", "追います", "たどります", "把握します", "収集します", "集めます",
    "比較します", "検証します", "調査します", "確認していきます", "見ます",
)
_PROGRESS_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _PROGRESS_VERBS)) + r")[。.!！\s]*$")
# 語尾が作業宣言でも単文の事実記述は progress と誤判定しない。手順マーカー（順序表現）で始まる文は作業宣言とする。
_PROGRESS_MARKERS = (
    "まず", "次に", "続いて", "これから", "今から", "この後", "最後に", "では", "それでは",
)
# 「調べてから伝える」型の宣言語尾。手順マーカーで始まる文に限って次アクション扱いにする（`_PROGRESS_VERBS` には入れない）。
_REPORT_BACK_VERBS = ("報告します", "お伝えします", "まとめます", "回答します", "共有します")
_REPORT_BACK_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_BACK_VERBS)) + r")[。.!！\s]*$")
# 報告系語尾に付けるマーカーからは「最後に」を除く（結論の締めのため）。
_REPORT_BACK_MARKERS = tuple(m for m in _PROGRESS_MARKERS if m != "最後に")


def _is_next_action_sentence(s: str) -> bool:
    """文が次アクション宣言か（作業宣言語尾、または手順マーカー付きの「結果を報告します」型）。"""
    return bool(_PROGRESS_END_RE.search(s)) or (
        s.startswith(_REPORT_BACK_MARKERS) and bool(_REPORT_BACK_END_RE.search(s)))


def _is_progress_only(text: str) -> bool:
    """text の全ての文が次アクション宣言なら True（結論文が1つも無い）。
    句点/改行で文に割り、(a) いずれかの文が手順マーカーで始まる、または (b) 文が2つ以上ある、のどちらかを満たすときだけ「作業宣言だけの message」とみなす。単文・マーカー無しは False。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return True
    if not all(_is_next_action_sentence(s) for s in sents):
        return False
    return any(s.startswith(_PROGRESS_MARKERS) for s in sents) or len(sents) >= 2


def _split_trailing_progress(text: str) -> tuple[str, str]:
    """単一段落（改行なし）の平文に限り、末尾の連続する作業宣言文を (結論, 落とした文) に分ける。
    改行や箇条書きを含む場合、または全部が作業宣言で空になる場合は (元文, 空文字)。
    """
    if "\n" in text:
        return text, ""
    parts = [p for p in re.findall(r"[^。]*。|[^。]+$", text) if p.strip()]
    dropped: list[str] = []
    while len(parts) > 1 and _PROGRESS_END_RE.search(parts[-1].strip()):
        dropped.insert(0, parts.pop())
    kept = "".join(parts).strip()
    if not kept:
        return text, ""
    return kept, "".join(dropped).strip()


def _trim_trailing_progress(text: str) -> str:
    """末尾の作業宣言文を落として結論で締める（落とした文は `_split_trailing_progress` が返す）。"""
    return _split_trailing_progress(text)[0]


def _pick_codex_headline(completed: list[str], partial: str = "", prefer_marker: str | None = None,
                         dropped: list | None = None) -> str:
    """集めた複数の agent_message から headline を決定的に選ぶ（LLM 不使用）。
    `prefer_marker`（素の Codex 用）: この文字列を含む message があれば、その最後のものを優先する。
    ① 結論を含む最後の message を優先する。② その末尾に連なる作業宣言文は落とす（`_trim_trailing_progress`）。③ どれも作業宣言だけなら最後の message をそのまま返す。
    `partial`＝item.updated だけ来て item.completed が来なかった未完 message（打ち切り時の保険）。
    `dropped`（省略可）: 末尾から落とした作業宣言文を `{"kind": "trailing_progress", "text": ...}` で足す（内部記録用）。
    """
    def _pick(m: str) -> str:
        kept, cut = _split_trailing_progress(m)
        if cut and dropped is not None:
            dropped.append({"kind": "trailing_progress", "text": cut})
        return kept

    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return ""
    if prefer_marker:
        for m in reversed(msgs):
            if prefer_marker in m and not _is_progress_only(m):
                return _pick(m)
    for m in reversed(msgs):
        if not _is_progress_only(m):
            return _pick(m)
    return msgs[-1]


# 「作業報告＋次アクション」型の途中経過（例:「関連資料を確認しました。次に影響範囲を調べます。」）。
# 完了形の作業報告が全部で、明示的な次アクション文を伴うときだけ途中経過とみなす（自動継続の判定専用・見出しの選び方は変えない）。
_REPORT_VERBS = (
    "確認しました", "確認済みです", "調べました", "調査しました", "特定しました", "把握しました",
    "整理しました", "洗い出しました", "検索しました", "取得しました", "読みました", "精読しました",
    "収集しました", "集めました", "検証しました", "比較しました", "分析しました", "チェックしました",
    "たどりました", "追いました", "見ました", "見つけました", "確かめました",
)
_REPORT_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_VERBS)) + r")[。.!！\s]*$")


def _is_report_with_next_action(text: str) -> bool:
    """全文が「完了形の作業報告」または「次アクション宣言」で、両方を少なくとも1文ずつ含む。
    次アクション宣言だけの message は `_is_progress_only` の領分（単文の事実記述を結論扱いにするため）。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return False
    has_next = any(_is_next_action_sentence(s) for s in sents)
    has_report = any(_REPORT_END_RE.search(s) and not _is_next_action_sentence(s) for s in sents)
    if not (has_next and has_report):
        return False
    return all(_is_next_action_sentence(s) or _REPORT_END_RE.search(s) for s in sents)


def _needs_continuation(completed: list[str], partial: str = "") -> bool:
    """集めた agent_message が1件以上あり、連結したテキストが途中経過（作業宣言だけ、または作業報告＋次アクション）で結論文が1つも無いなら True。
    message 単位の保護: 単文・手順マーカーで始まらない・`_PROGRESS_END_RE` に合う message が1つでもあれば False（結論あり）。
    それ以外は全 message を連結して判定する（作業報告と次アクションが別 message に分かれていても拾うため）。空/空白のみの message は対象外、1件も無ければ False。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return False
    for m in msgs:
        sents = [s.strip() for s in re.split(r"[。\n]+", m) if s.strip()]
        if (len(sents) == 1 and not sents[0].startswith(_PROGRESS_MARKERS)
                and _PROGRESS_END_RE.search(sents[0])):
            return False
    joined = "\n".join(msgs)
    return _is_progress_only(joined) or _is_report_with_next_action(joined)


# 続き・見直しで最終回答を書き直すときの規則（回答の情報量を削らない）。台帳継続・見直しのプロンプト（`ledger_gate.py`）も共有する。
_KEEP_FULL_ANSWER_RULE = (
    "書き直すときは前の回答の内容を削らず、直した点を反映した完全な回答を書き、短くまとめ直さないでください。"
    "worker・evaluator が見つけた事実と根拠は統合のときに落とさず含め、"
    "台帳の各項目の内容・条件・例外・根拠は詳しく書いてください（台帳に無い新しい主張は作らない）。"
)
_FULL_ANSWER_RULE = (
    "回答の説明はすべて answer に書いてください（claims は機械用の索引で画面に出ないため、"
    "claims にだけ書いた説明は利用者に届きません）。"
    + _KEEP_FULL_ANSWER_RULE
)

_CONTINUE_PROMPT = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて最終回答（結論と根拠）を書いてください。"
    "最終回答はそのまま利用者に見せるので、利用者の元の質問への回答として書き、この指示や途中の経緯には触れないでください。"
    + _KEEP_FULL_ANSWER_RULE
)
# 道具を 1 回も使わずに答えたターンへの促し（1 ターン 1 回）。
_TOOL_ZERO_PREFIX = "資料とソースを調べてから答えてください。まだ資料もソースも調べていません。"
# 出力スキーマ有効時（`_schema_on`）だけ使う継続プロンプト（AGENTS.md が構造化応答 `status`／`answer`／`next_step` を求めるのはスキーマ有効時だけのため）。
_CONTINUE_PROMPT_SCHEMA = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて `status` を `final` にした"
    "最終回答（結論と根拠）を書いてください。ただし全件・一覧・すべての依頼で対象範囲の確認が"
    "終わっていなければ `final` にせず、`in_progress` のまま `next_step` に残りを書いてください。"
    "`final` の `answer` はそのまま利用者に見せるので、利用者の元の質問への回答として書き、この指示や途中の経緯には触れないでください。"
    + _FULL_ANSWER_RULE
)
_TOOL_ZERO_PROMPT = _TOOL_ZERO_PREFIX + _CONTINUE_PROMPT
_TOOL_ZERO_PROMPT_SCHEMA = _TOOL_ZERO_PREFIX + _CONTINUE_PROMPT_SCHEMA
