"""薄いチャット・ルーティング（M8・MVP-DETAIL §7 / 04-画面の原則.md §3.1）。

会話メッセージ → どのレンズ（impact / troubleshoot / qa）かを**意図で判定**し、必要な入力
（影響なら起点語）を取り出す。ユーザはレンズを選ばない。**意図語は汎用**（障害/影響/仕様…）で、
特定テーマの名前はコードに持たない（起点語は known_terms＝データから・AT-G3）。

**ハイブリッド判定（§3 "intent も Codex 化"・2026-06-30）**: ここは Tier1＝**ヒューリスティック＋確信度**。
`confident=False`（曖昧）のときだけ chat_service が Tier2（LLM 分類）→ Tier3（ask_user で本人確認）へ進む。
**確信度**: 強い cue（障害/影響）が片方だけ＝確定／両方同居＝曖昧／弱い qa cue のみ＝qa 確定／何も無し＝qa 確定（既定検索）。
clarify（確認）の**回答**（フロントが『…選択: X … 元の依頼: Y』に整形して再送）は route が最初に解決し、
選択ラベル→lens を直接マップ＝**再質問ループを防ぐ**（heuristic/曖昧判定へ戻さない）。
"""
from __future__ import annotations

import re
import secrets

# 汎用の意図キュー（テーマ非依存）。順序＝トラブル → 影響 → 作成 → 仕様問い合わせ。
_TROUBLE = ("abend", "エラー", "障害", "失敗", "落ち", "異常", "止ま", "とま", "動かな",
            "原因", "不具合", "ハング", "タイムアウト", "リトライ")
_IMPACT = ("影響", "変え", "変更", "改修", "修正", "直す", "波及", "変わる", "リプレース")
# 作成系の意図キュー（Codex 強化計画 Phase1・P1-a）。「〜を作って/作成して」「パワポ/PowerPoint に」
# 「Excel で/一覧表に」「Word で/報告書に/資料にまとめて」等の作成表現を拾う。
_AUTHOR = ("作って", "作成して", "パワポ", "powerpoint", "excel", "エクセル", "一覧表に",
          "word", "ワードで", "報告書に", "資料にまとめて", "スライドに", "ドラフトして")
_QA = ("仕様", "とは", "規定", "定義", "どうな", "教えて", "何ですか", "方法", "どういう", "ですか", "?", "？")

# clarify（ask_user）の選択肢 ⇄ lens（1:1）。**再開メッセージの "選択:" 解決にも使う**＝ループ回避の要。
_CLARIFY_OPTIONS = [
    {"id": "impact", "label": "影響を調べる", "description": "変更したときの波及範囲（どこに影響するか）"},
    {"id": "troubleshoot", "label": "原因を調べる", "description": "不具合・エラー・異常の原因候補"},
    {"id": "qa", "label": "内容・仕様を調べる", "description": "仕様や定義など資料の記述を探す"},
    {"id": "author", "label": "資料を作成する", "description": "調べた内容をもとにファイル（Excel/Word/PowerPoint等）を作る"},
]
_LABEL_LENS = (("影響", "impact"), ("原因", "troubleshoot"), ("仕様", "qa"), ("内容", "qa"), ("作成", "author"))


def _has(text: str, cues) -> bool:
    """cue の存在判定。日本語 cue は部分一致、ASCII 英字 cue（word/excel/abend 等）は境界付き一致。

    裸の部分一致だと `word` が「password」「keyword」に、`excel` が「excellent」に誤ヒットする
    （RV MEDIUM・author 誤判定で重い作成経路へ入る実害）。`\\b` は日本語文字も \\w 扱いで
    「wordで」「abendした」に一致しなくなるため、ASCII 英数だけを境界とみなす lookaround を使う。
    """
    low = text.lower()
    for c in cues:
        if c.isascii() and c.isalpha():
            if re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", low):
                return True
        elif c in low:
            return True
    return False


def _extract_start(message: str, known_terms=None) -> str:
    """影響レンズの起点語を取り出す。既知ノード名/別名が文中にあればそれを優先。

    無ければ「〜を変え／〜の影響」等の汎用パターンで手前の名詞句を拾う（名寄せは後段が担う）。
    """
    low = message.lower()
    hits = [t for t in (known_terms or []) if t and t.lower() in low]
    if hits:
        return max(hits, key=len)                       # 最長一致を起点に
    m = re.search(r"(.+?)\s*(?:を|の)\s*(?:変え|変更|改修|修正|影響|波及)", message)
    if m:
        return re.split(r"[、。,\.\s「」（）()]", m.group(1).strip())[-1]
    return message.strip()


def _decision(lens: str, message: str, known_terms, reason: str, confident: bool = True) -> dict:
    """lens 確定 → run_* へ渡す decision。impact のみ起点語抽出、他は本文そのもの。"""
    inp = _extract_start(message, known_terms) if lens == "impact" else (message or "").strip()
    return {"lens": lens, "input": inp, "reason": reason, "confident": confident}


def _resume_lens(message: str):
    """clarify の回答を検出し (lens, original) を返す。検出できなければ (None, None)。

    **router 由来の clarify だけ**を識別するため `確認ID: lens-*` marker を必須にする（agentic の generic
    `ask_user` も『選択:/元の依頼:』形式を使うため・誤検出回避＝Codex RV High）。marker があれば選択ラベルを
    lens にマップ（解決不可は qa＝再質問しない）。**再開は heuristic/曖昧判定に戻さない**＝再質問ループ回避。
    """
    if not re.search(r"確認ID[:：]\s*lens-\S+", message):
        return None, None                               # router clarify でない（generic ask_user 等）→ 通常判定へ
    pick = re.search(r"選択[:：]\s*(.+)", message)
    label = pick.group(1) if pick else ""
    lens = next((l for kw, l in _LABEL_LENS if kw in label), "qa")   # 解決不可は qa（malformed でも再質問しない）
    orig = re.search(r"元の依頼[:：]\s*(.+)", message, re.S)   # 元の依頼は末尾＝複数行も拾う（DOTALL・RV LOW）
    original = orig.group(1).strip() if orig else message
    return lens, original


def route(message: str, world: str = "v1", known_terms=None) -> dict:
    """メッセージ → {lens, input, reason, confident}。`confident=False`＝曖昧（chat_service が LLM/clarify へ）。

    - clarify の回答（`確認ID: lens-*` marker 付き）は**最初に**解決（選択→lens・元の依頼を input に）＝再質問ループ防止。
    - **強い cue（障害/影響/作成）が2カテゴリ以上同居の時だけ曖昧**。1カテゴリだけ＝確定。
      弱い qa cue（?/ですか 等）は曖昧要因にしない（P1-a: author 追加も同じ既存設計を踏襲）。
    - cue 無しは **qa 既定（confident）**＝AIなし環境で素の検索に毎回 lens 確認を出さない（Codex RV）。
    """
    msg = (message or "").strip()
    lens, original = _resume_lens(msg)
    if lens:                                            # 確認の選択を尊重＝確定（input は元の依頼から）
        return _decision(lens, original, known_terms, "確認の選択", confident=True)
    strong_t, strong_i, strong_a = _has(msg, _TROUBLE), _has(msg, _IMPACT), _has(msg, _AUTHOR)
    if sum([strong_t, strong_i, strong_a]) >= 2:         # 2カテゴリ以上の強い cue が同居＝真の曖昧 → Tier2(LLM)/Tier3(確認) へ
        return _decision("qa", msg, known_terms, "意図が曖昧（要確認）", confident=False)
    if strong_t:
        return _decision("troubleshoot", msg, known_terms, "症状・障害の語", confident=True)
    if strong_i:
        return _decision("impact", msg, known_terms, "変更・影響の語", confident=True)
    if strong_a:
        return _decision("author", msg, known_terms, "作成の語", confident=True)
    if _has(msg, _QA):
        return _decision("qa", msg, known_terms, "問い合わせの語", confident=True)
    return _decision("qa", msg, known_terms, "既定（検索）", confident=True)   # cue 無し＝素の検索（曖昧扱いしない）


def looks_like_author(message: str) -> bool:
    """作成系の語を含むか（無料ヒューリスティックのみ・LLM 呼び出しなし）。

    P1-a（Codex 強化計画 Phase1）: ナレッジ参照オフ（`route()` を通らない）でも Codex が
    「作成の依頼なのでナレッジ参照が要る」旨を具体的に案内できるよう、cue 語リストを
    `_AUTHOR` に一本化した公開ラッパー（agents.CodexProvider._plain_text から利用）。
    """
    return _has((message or "").strip(), _AUTHOR)


def decision_for(lens: str, message: str, known_terms=None, reason: str = "AI判定") -> dict:
    """LLM 等（Tier2）が選んだ lens を decision に整える（chat_service の合成用）。不正 lens は qa。"""
    if lens not in ("impact", "troubleshoot", "qa", "author"):
        lens = "qa"
    return _decision(lens, message, known_terms, reason, confident=True)


# 調べ方ブロック（2026-08-29-調べ方ブロック.md §3.1・裁定11）: 上級者向けの1回限りのスラッシュ指定。
# `_CLARIFY_OPTIONS` のラベルと対応する4語のみ（「自動」はそもそも既定のため対象外）。半角/全角
# スペースの両方を接頭辞の区切りとして受理する。
_SLASH_LENS = {"影響": "impact", "原因": "troubleshoot", "内容": "qa", "作成": "author"}
_SLASH_RE = re.compile(r"^/(影響|原因|内容|作成)[ 　]+")


def extract_slash_lens(message: str):
    """メッセージ先頭の `/影響 `・`/原因 `・`/内容 `・`/作成 `（半角/全角スペース）を検出する。

    一致すれば `(lens, 接頭辞を除いた本文)` を返し、次のターンには引き継がない**1回限りの明示**
    （chat_service がこの送信のみ `decision_for()` へ渡す・ブロックの選択状態＝`lens_source` の
    「explicit」とは区別する＝「slash」）。一致しなければ `(None, message)`。
    """
    m = _SLASH_RE.match(message or "")
    if not m:
        return None, (message or "")
    return _SLASH_LENS[m.group(1)], message[m.end():]


def clarify_question(message: str) -> dict:
    """曖昧時に出す『どの調べ方か』の確認（ask_user と同じ question イベント形）。"""
    return {"type": "question", "interaction_id": "lens-" + secrets.token_hex(4),
            "mode": "single", "prompt": "どの調べ方をしますか？（影響範囲／原因／仕様・内容／作成）",
            "options": list(_CLARIFY_OPTIONS), "allow_free_text": False,
            "original_message": (message or "").strip()}


def clarify_decision(message: str) -> dict:
    """clarify 用 decision（provider が question を emit して停止）。非対話経路は `fallback`（qa）を使う。"""
    return {"lens": "clarify", "input": (message or "").strip(), "reason": "意図が曖昧（確認）",
            "confident": False, "fallback": "qa", "question": clarify_question(message)}


# High-2（2026-07-07-フィードバック一括.md F2-2）: ユーザーが依頼文に「確認してから進めて」等と書いたら、
# 調査/作成（dispatch/provider）に入る前に**必ず**確認カードを出す決定的ガード。プロンプト遵守任せ
# （agents 側 F2 の文言）では「必ず」を保証できないため、ルーター層で question を emit して停止する。
# lens 選択の clarify とは別物（どの lens かではなく「何を確認してから進めるか」を聞く）＝interaction_id は
# lens-* を使わない（_resume_lens に横取りさせず、回答再送はフル本文で通常ルーティングさせる）。
# 「し」の有無（確認**し**てから／聞いてから）と 進める／すすめる の表記ゆれを吸収する。
_CONFIRM_FIRST_RE = re.compile(r"(?:確認し?|聞い)てから\s*(?:進|すす)め")
_CONFIRM_FIRST_OPTIONS = [
    {"id": "scope", "label": "対象範囲（どの資料/システムか）",
     "description": "どのフォルダ・資料・システムを対象にするか"},
    {"id": "format", "label": "出力形式（形式・粒度）", "description": "回答や成果物の形式・詳しさ"},
    {"id": "approach", "label": "進め方（調査の順序・深さ）", "description": "どこから・どこまで調べるか"},
    {"id": "other", "label": "その他（補足に記入）", "description": "上記以外に確認したいこと"},
]


def wants_confirm_first(message: str) -> bool:
    """依頼文に「確認してから進めて」等（利用者主導の確認要求）が含まれ、かつ回答の再送でないか。

    確認ID 付き（＝前の質問への回答の再送）は発動しない＝再質問ループ防止（S2 の確認ID 優先と同思想）。
    """
    m = message or ""
    if re.search(r"確認ID[:：]", m):
        return False
    return bool(_CONFIRM_FIRST_RE.search(m))


def confirm_first_question(message: str, *, lens: str | None = None, layer: str | None = None,
                           scope_paths=None, lens_source: str | None = None,
                           lens_block: str | None = None, tools: dict | None = None) -> dict:
    """「確認してから進めて」指定時に出す確認カード（ask_user と同じ question イベント形）。

    `lens`/`layer`/`scope_paths`（省略可・RV1 #3）: 確認カードを出した時点で解決済みだった調べ方の
    明示指定・探す対象・範囲。スラッシュ接頭辞（1回限りの明示）はメッセージ本文からしか読み取れず、
    確認カードで一度差し替わる本文（選択/元の依頼）には残らないため、既存の payload に載せて運ぶ
    （新しい保存経路は作らない・回答の再送は `web/chat.js` の data-ask-submit ハンドラが1回だけ
    既存の `ChatReq.lens`／scope 経路へ戻す）。
    `lens_source`/`lens_block`（省略可・RV2 #1）: `lens` だけでは「スラッシュは1回限り・ブロック
    状態は変えない」契約を再送後も維持できない——`lens_source=="slash"` のとき、フロントは
    この2値を使って再送本文の先頭へ元のスラッシュ接頭辞を復元し、既存のスラッシュ解決・
    接頭辞除去・会話復元経路（`chat_service._resolve_lens`／`web/chat/scope.js::
    applyConversationScope`）をそのまま再利用する（新しい復元経路は作らない）。
    `tools`（省略可・SC-6e）: 確認カードを出した時点で解決済みだった検索経路トグル
    （`scope_meta["tools"]`）。`lens`/`layer`/`scope_paths` と同じ理由（本文には残らない・
    再送は1回だけ戻す）で payload に載せて運ぶ——無ければ再送時に全ONへ復元されてしまう
    （例: グラフのみで確認カードを出し、別会話へ移動して戻ると全ONで再送される事故）。
    """
    return {"type": "question", "interaction_id": "confirm-" + secrets.token_hex(4),
            "mode": "single",
            "prompt": "確認してから進めるよう指定されています。何を確認してから進めますか？",
            "options": list(_CONFIRM_FIRST_OPTIONS), "allow_free_text": True,
            "original_message": (message or "").strip(),
            "lens": lens, "layer": layer, "scope_paths": list(scope_paths or []),
            "lens_source": lens_source, "lens_block": lens_block, "tools": tools}


def confirm_first_decision(message: str, *, lens: str | None = None, layer: str | None = None,
                          scope_paths=None, lens_source: str | None = None,
                          lens_block: str | None = None, tools: dict | None = None) -> dict:
    """confirm-first 用 decision（provider が question を emit して停止）。非対話経路は `fallback`（qa）。"""
    return {"lens": "clarify", "input": (message or "").strip(), "reason": "確認してから進める指定",
            "confident": False, "fallback": "qa",
            "question": confirm_first_question(message, lens=lens, layer=layer,
                                               scope_paths=scope_paths, lens_source=lens_source,
                                               lens_block=lens_block, tools=tools)}
