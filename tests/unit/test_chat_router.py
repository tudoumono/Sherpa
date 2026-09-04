"""chat_router の hybrid 判定（Tier1）単体テスト: 確信度・clarify 回答の再開（ループ回避）・clarify 形。

依存ゼロ（LLM/DB 不要）。Tier2(LLM)/Tier3(ask_user emit) は chat_service/agents 側の結合で別途検証。
"""
from __future__ import annotations

from sherpa import chat_router as R


def test_confident_single_strong_cue():
    assert R.route("消費税率を変えたい。影響は？")["lens"] == "impact"          # 影響/変え（強・単独）
    assert R.route("夜間バッチ NIGHTLY が ABEND。原因は？")["lens"] == "troubleshoot"
    assert R.route("消費税の端数処理の仕様は？")["lens"] == "qa"
    r = R.route("消費税率を変更したい", known_terms=["消費税率", "税率", "TAX-RATE"])
    assert r["lens"] == "impact" and r["input"] == "消費税率"
    for m in ("消費税率を変えたい。影響は？", "夜間バッチが ABEND。原因は？", "仕様は？"):
        assert R.route(m)["confident"] is True


def test_ambiguous_only_on_conflict():
    r = R.route("税率を変えたら夜間バッチが落ちる？")            # 影響(変え)＋障害(落ち)＝衝突
    assert r["confident"] is False and r["lens"] == "qa"        # 既定 qa だが confident=False で Tier2/3 へ
    r2 = R.route("消費税について")                              # cue 無し＝素の検索（clarify 乱発しない）
    assert r2["confident"] is True and r2["lens"] == "qa"


def test_resume_from_clarify_breaks_loop():
    # フロント整形の clarify 回答（確認ID marker 付き）。元の依頼が衝突 cue でも『選択』で確定＝再質問しない。
    msg = ("確認事項: どの調べ方をしますか？\n確認ID: lens-abc123\n選択: 影響を調べる\n"
           "元の依頼: 税率を変えたら夜間バッチが落ちる？")
    r = R.route(msg, known_terms=["税率"])
    assert r["lens"] == "impact" and r["confident"] is True     # ループ回避（再び曖昧にしない）
    assert r["input"] == "税率"                                  # input は『元の依頼』から抽出
    assert R.route("確認ID: lens-x\n選択: 原因を調べる\n元の依頼: y")["lens"] == "troubleshoot"
    assert R.route("確認ID: lens-x\n選択: 内容・仕様を調べる\n元の依頼: y")["lens"] == "qa"


def test_resume_requires_marker_and_handles_malformed():
    # marker 無し（generic ask_user の回答）は router clarify と誤検出せず通常判定へ。
    r = R.route("選択: 4期\n元の依頼: 影響を教えて")
    assert r["lens"] == "impact" and r["reason"] != "確認の選択"  # 通常 heuristic（影響）で判定
    # marker あり・ラベル解決不可（malformed）は qa（再質問しない）。
    assert R.route("確認ID: lens-x\n選択: 4期\n元の依頼: なにか")["lens"] == "qa"


def test_clarify_decision_shape():
    d = R.clarify_decision("これ直すと？")
    assert d["lens"] == "clarify" and d["fallback"] == "qa" and d["confident"] is False
    q = d["question"]
    assert q["type"] == "question" and q["mode"] == "single"
    assert [o["id"] for o in q["options"]] == ["impact", "troubleshoot", "qa", "author"]
    assert q["original_message"] == "これ直すと？"


def test_decision_for_maps_and_guards():
    assert R.decision_for("troubleshoot", "夜間バッチの話")["lens"] == "troubleshoot"
    assert R.decision_for("bogus", "なにか")["lens"] == "qa"     # 不正 lens は qa にフォールバック
    assert R.decision_for("impact", "税率を変えたい", ["税率"])["input"] == "税率"
    assert R.decision_for("author", "パワポにして")["lens"] == "author"   # P1-a: author は有効な lens


# ---- P1-a（Codex 強化計画 Phase1）: 作成インテント（author）----

def test_author_confident_single_strong_cue():
    """作成系の語（単独）は author で確定（confident=True）。"""
    for m in ("消費税率の一覧をExcelで作って", "この内容をパワポにまとめて",
              "調査結果をWordで報告書にまとめて", "一覧表にして"):
        r = R.route(m)
        assert r["lens"] == "author", f"{m!r} が author にならない: {r}"
        assert r["confident"] is True


def test_author_ambiguous_when_combined_with_other_strong_cue():
    """作成語が影響/障害の強い cue と同居すると曖昧（Tier2/3 へ）。既存の「2カテゴリ以上で曖昧」設計を踏襲。"""
    r = R.route("消費税率を変更してExcelにまとめて")   # 影響(変更) + 作成(Excel)
    assert r["confident"] is False and r["lens"] == "qa"


def test_author_resume_from_clarify_selects_author_lens():
    msg = ("確認事項: どの調べ方をしますか？\n確認ID: lens-abc123\n選択: 資料を作成する\n"
           "元の依頼: 消費税率の一覧をまとめて")
    r = R.route(msg)
    assert r["lens"] == "author" and r["confident"] is True
    assert r["input"] == "消費税率の一覧をまとめて"


def test_ascii_cue_requires_word_boundary():
    """RV MEDIUM（Phase1）: ASCII 英字 cue は境界付き一致。裸の部分一致だと `word` が
    「password」「keyword」に誤ヒットし、通常の QA が author 扱いになる実害があった。"""
    assert R.route("passwordの規定を教えて")["lens"] == "qa"
    assert R.route("keywordの定義は？")["lens"] == "qa"
    assert not R.looks_like_author("passwordの規定を教えて")
    # 境界があれば従来どおり一致する（日本語文字は境界扱い＝「wordで」「excelで」は拾う）。
    assert R.looks_like_author("この内容をwordで清書して")
    assert R.looks_like_author("excelでください")
    # 境界化は trouble 側の ASCII cue にも及ぶ＝「abendした」は引き続き troubleshoot。
    assert R.route("バッチがabendした。原因は？")["lens"] == "troubleshoot"


# ---- 調べ方ブロック（2026-08-29-調べ方ブロック.md §3.1・裁定11）: スラッシュ指定の1回限りの明示 ----

def test_extract_slash_lens_all_four_words():
    assert R.extract_slash_lens("/影響 消費税率を変えたい") == ("impact", "消費税率を変えたい")
    assert R.extract_slash_lens("/原因 夜間バッチが止まった") == ("troubleshoot", "夜間バッチが止まった")
    assert R.extract_slash_lens("/内容 消費税の端数処理は？") == ("qa", "消費税の端数処理は？")
    assert R.extract_slash_lens("/作成 一覧表にして") == ("author", "一覧表にして")


def test_extract_slash_lens_fullwidth_space():
    assert R.extract_slash_lens("/影響　消費税率") == ("impact", "消費税率")


def test_extract_slash_lens_no_match_returns_original():
    assert R.extract_slash_lens("消費税率を変えたい") == (None, "消費税率を変えたい")
    assert R.extract_slash_lens("これは /影響 ではない") == (None, "これは /影響 ではない")
    assert R.extract_slash_lens("/影響消費税率") == (None, "/影響消費税率")   # 区切り空白なし＝非一致
    assert R.extract_slash_lens("/自動 消費税率") == (None, "/自動 消費税率")   # 4語以外は通常文（裁定11）


def test_extract_slash_lens_empty_message():
    assert R.extract_slash_lens("") == (None, "")
    assert R.extract_slash_lens(None) == (None, "")


# ---- 確認カード（「確認してから進めて」）の payload（RV1 #3・RV2 #1） ----

def test_confirm_first_question_embeds_resolved_lens_layer_scope():
    q = R.confirm_first_question("なにか。確認してから進めて。", lens="impact", layer="docs",
                                 scope_paths=["4期/設計"], lens_source="explicit", lens_block="impact")
    assert q["lens"] == "impact" and q["layer"] == "docs" and q["scope_paths"] == ["4期/設計"]
    assert q["lens_source"] == "explicit" and q["lens_block"] == "impact"


def test_confirm_first_question_defaults_to_none_and_empty_list():
    q = R.confirm_first_question("なにか。確認してから進めて。")
    assert q["lens"] is None and q["layer"] is None and q["scope_paths"] == []
    assert q["lens_source"] is None and q["lens_block"] is None


def test_confirm_first_decision_passes_through_to_question():
    d = R.confirm_first_decision("なにか。確認してから進めて。", lens="qa", layer="both",
                                 scope_paths=[], lens_source="slash", lens_block="qa")
    assert d["question"]["lens"] == "qa"
    assert d["question"]["lens_source"] == "slash" and d["question"]["lens_block"] == "qa"


# ---- 確認カードの検索経路トグル（SC-6e）----

def test_confirm_first_question_embeds_resolved_tools():
    q = R.confirm_first_question("なにか。確認してから進めて。",
                                 tools={"grep": False, "fulltext": False, "graph": True})
    assert q["tools"] == {"grep": False, "fulltext": False, "graph": True}


def test_confirm_first_question_tools_defaults_to_none():
    """省略時は None（全ON扱いは呼び出し元＝web/chat.js の再送 override 側で解決する）。"""
    q = R.confirm_first_question("なにか。確認してから進めて。")
    assert q["tools"] is None


def test_confirm_first_decision_passes_through_tools_to_question():
    d = R.confirm_first_decision("なにか。確認してから進めて。",
                                 tools={"grep": True, "fulltext": False, "graph": True})
    assert d["question"]["tools"] == {"grep": True, "fulltext": False, "graph": True}
