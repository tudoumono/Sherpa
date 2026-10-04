"""会話メッセージ → どのレンズ（impact / troubleshoot / qa）かを意図で判定し、必要な入力（影響なら起点語）を取り出す。
設計: docs/design/chat.md「1ターンの流れ」

意図語は汎用で、特定テーマの名前は持たない。ここは Tier1＝ヒューリスティック＋確信度。
`confident=False`（曖昧）のときだけ chat_service が Tier2（LLM 分類）→ Tier3（ask_user で確認）へ進む。
確信度: 強い cue（障害/影響）が片方だけ＝確定／両方同居＝曖昧／弱い qa cue のみ・cue 無し＝qa 確定。
clarify の回答は route が最初に解決し、選択ラベル→lens を直接マップする（再質問ループを防ぐ）。
"""
from __future__ import annotations

import re
import secrets

# 汎用の意図キュー（テーマ非依存）。順序は トラブル → 影響 → 作成 → 仕様問い合わせ。
_TROUBLE = ("abend", "エラー", "障害", "失敗", "落ち", "異常", "止ま", "とま", "動かな",
            "原因", "不具合", "ハング", "タイムアウト", "リトライ")
_IMPACT = ("影響", "変え", "変更", "改修", "修正", "直す", "波及", "変わる", "リプレース")
# 作成系の意図キュー（「〜を作って」「パワポに」「Excel で」「報告書にまとめて」等）。
_AUTHOR = ("作って", "作成して", "パワポ", "powerpoint", "excel", "エクセル", "一覧表に",
          "word", "ワードで", "報告書に", "資料にまとめて", "スライドに", "ドラフトして")
_QA = ("仕様", "とは", "規定", "定義", "どうな", "教えて", "何ですか", "方法", "どういう", "ですか", "?", "？")

# clarify（ask_user）の選択肢 ⇄ lens（1:1）。再開メッセージの「選択:」の解決にも使う（ループ回避）。
_CLARIFY_OPTIONS = [
    {"id": "impact", "label": "影響を調べる", "description": "変更したときの波及範囲（どこに影響するか）"},
    {"id": "troubleshoot", "label": "原因を調べる", "description": "不具合・エラー・異常の原因候補"},
    {"id": "qa", "label": "内容・仕様を調べる", "description": "仕様や定義など資料の記述を探す"},
    {"id": "author", "label": "資料を作成する", "description": "調べた内容をもとにファイル（Excel/Word/PowerPoint等）を作る"},
]
_LABEL_LENS = (("影響", "impact"), ("原因", "troubleshoot"), ("仕様", "qa"), ("内容", "qa"), ("作成", "author"))


def _has(text: str, cues) -> bool:
    """cue の存在判定。日本語 cue は部分一致、ASCII 英字 cue は境界付き一致（`word` が `password` に当たらない）。"""
    low = text.lower()
    for c in cues:
        if c.isascii() and c.isalpha():
            if re.search(rf"(?<![a-z0-9]){re.escape(c)}(?![a-z0-9])", low):
                return True
        elif c in low:
            return True
    return False


def _extract_start(message: str, known_terms=None) -> str:
    """影響レンズの起点語を取り出す。既知ノード名/別名が文中にあれば優先し、無ければ「〜を変え／〜の影響」等のパターンで名詞句を拾う。"""
    low = message.lower()
    hits = [t for t in (known_terms or []) if t and t.lower() in low]
    if hits:
        return max(hits, key=len)  # 最長一致を起点に
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
    router 由来の clarify だけを `確認ID: ask-*` marker で識別し（旧 `lens-*` も受理）、選択ラベルを lens にマップする。解決不可は qa。
    """
    if not re.search(r"確認ID[:：]\s*(?:ask|lens)-\S+", message):
        return None, None                               # router clarify でない（generic ask_user 等）→ 通常判定へ
    pick = re.search(r"選択[:：]\s*(.+)", message)
    label = pick.group(1) if pick else ""
    lens = next((l for kw, l in _LABEL_LENS if kw in label), "qa")  # 解決不可は qa
    orig = re.search(r"元の依頼[:：]\s*(.+)", message, re.S)  # 元の依頼は末尾＝複数行も拾う（DOTALL）
    original = orig.group(1).strip() if orig else message
    return lens, original


def route(message: str, known_terms=None) -> dict:
    """メッセージ → {lens, input, reason, confident}。`confident=False` は曖昧（chat_service が LLM/clarify へ進む）。
    ① clarify の回答（`確認ID: ask-*` 付き）を最初に解決する。
    ② 強い cue（障害/影響/作成）が 2 カテゴリ以上同居するときだけ曖昧にする。
    ③ cue 無しは qa 既定（confident）。
    """
    msg = (message or "").strip()
    lens, original = _resume_lens(msg)
    if lens:  # 確認の選択を尊重して確定（input は元の依頼から）
        return _decision(lens, original, known_terms, "確認の選択", confident=True)
    strong_t, strong_i, strong_a = _has(msg, _TROUBLE), _has(msg, _IMPACT), _has(msg, _AUTHOR)
    if sum([strong_t, strong_i, strong_a]) >= 2:  # 強い cue が 2 カテゴリ以上同居＝曖昧 → Tier2(LLM)/Tier3(確認) へ
        return _decision("qa", msg, known_terms, "意図が曖昧（要確認）", confident=False)
    if strong_t:
        return _decision("troubleshoot", msg, known_terms, "症状・障害の語", confident=True)
    if strong_i:
        return _decision("impact", msg, known_terms, "変更・影響の語", confident=True)
    if strong_a:
        return _decision("author", msg, known_terms, "作成の語", confident=True)
    if _has(msg, _QA):
        return _decision("qa", msg, known_terms, "問い合わせの語", confident=True)
    return _decision("qa", msg, known_terms, "既定（検索）", confident=True)  # cue 無し＝素の検索


def decision_for(lens: str, message: str, known_terms=None, reason: str = "AI判定") -> dict:
    """LLM 等（Tier2）が選んだ lens を decision に整える。不正な lens は qa。"""
    if lens not in ("impact", "troubleshoot", "qa", "author"):
        lens = "qa"
    return _decision(lens, message, known_terms, reason, confident=True)


# 上級者向けの 1 回限りのスラッシュ指定。`_CLARIFY_OPTIONS` のラベルに対応する 4 語のみ（半角/全角スペース区切り）。
_SLASH_LENS = {"影響": "impact", "原因": "troubleshoot", "内容": "qa", "作成": "author"}
_SLASH_RE = re.compile(r"^/(影響|原因|内容|作成)[ 　]+")


def extract_slash_lens(message: str):
    """メッセージ先頭の `/影響 `・`/原因 `・`/内容 `・`/作成 ` を検出する。
    一致すれば `(lens, 接頭辞を除いた本文)`（次のターンへは引き継がない 1 回限り）、しなければ `(None, message)`。
    """
    m = _SLASH_RE.match(message or "")
    if not m:
        return None, (message or "")
    return _SLASH_LENS[m.group(1)], message[m.end():]


def clarify_question(message: str) -> dict:
    """曖昧時に出す「どの調べ方か」の確認（ask_user と同じ question イベント形）。"""
    return {"type": "question", "interaction_id": "ask-" + secrets.token_hex(4),
            "mode": "single", "prompt": "どのやりたいことをしますか？（影響範囲／原因／仕様・内容／作成）",
            "options": list(_CLARIFY_OPTIONS), "allow_free_text": False,
            "original_message": (message or "").strip()}


def clarify_decision(message: str) -> dict:
    """clarify 用 decision（provider が question を emit して停止）。非対話経路は `fallback`（qa）を使う。"""
    return {"lens": "clarify", "input": (message or "").strip(), "reason": "意図が曖昧（確認）",
            "confident": False, "fallback": "qa", "question": clarify_question(message)}


# 「確認してから進めて」等の依頼は、調査/作成に入る前に必ず確認カードを出す（ルーター層の決定的ガード）。
# interaction_id は ask-*（および旧 lens-*）を使わない（`_resume_lens` に横取りさせない）。表記ゆれ（し/聞い・進める/すすめる）を吸収する。
_CONFIRM_FIRST_RE = re.compile(r"(?:確認し?|聞い)てから\s*(?:進|すす)め")
_CONFIRM_FIRST_OPTIONS = [
    {"id": "scope", "label": "対象範囲（どの資料/システムか）",
     "description": "どのフォルダ・資料・システムを対象にするか"},
    {"id": "format", "label": "出力形式（形式・粒度）", "description": "回答や成果物の形式・詳しさ"},
    {"id": "approach", "label": "進め方（調査の順序・深さ）", "description": "どこから・どこまで調べるか"},
    {"id": "other", "label": "その他（補足に記入）", "description": "上記以外に確認したいこと"},
]


def wants_confirm_first(message: str) -> bool:
    """依頼文に「確認してから進めて」等が含まれ、かつ回答の再送でないか。確認ID 付き（回答の再送）は発動しない。"""
    m = message or ""
    if re.search(r"確認ID[:：]", m):
        return False
    return bool(_CONFIRM_FIRST_RE.search(m))


def confirm_first_question(message: str, *, lens: str | None = None, layer: str | None = None,
                           scope_paths=None, lens_source: str | None = None,
                           lens_block: str | None = None, tools: dict | None = None) -> dict:
    """「確認してから進めて」指定時の確認カード（ask_user と同じ question イベント形）。
    `lens`/`layer`/`scope_paths`/`lens_source`/`lens_block`/`tools` は、確認カードを出した時点で解決済みの指定で、
    本文に残らないため payload に載せて運び、回答の再送時に既存の経路へ 1 回だけ戻す。
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
