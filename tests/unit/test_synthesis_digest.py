"""`agentic_search.build_synthesis_digest` の単体テスト。

清書（`_answer_prompt` → `providers/prompts.py::_facts`）専用の**確定根拠の全件ダイジェスト**——
帰属専用の `build_evidence_digest`（60字・60行・16KiB 上限）とは目的・上限が異なる別関数
（`build_evidence_digest` 自体の契約はここでは変えない・その回帰は `test_ext2_evidence.py` が
別途固定する）。ev-N 採番だけを共有する。LLM を呼ばない純粋関数のテスト（コスト0）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import sherpa.agentic_search as A  # noqa: E402
from sherpa.providers import base as PB  # noqa: E402


def test_synthesis_digest_includes_fifth_and_beyond_citation():
    """全件ダイジェストは件数上限を持たない（`_facts` の QA 整形が使う4件上限とは別契約）——
    5件目以降の citation も digest に載る。"""
    cites = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "quote": f"quote{i}"} for i in range(6)]
    meta = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "verification_method": "span_verified"}
           for i in range(6)]
    digest, ev_map = A.build_synthesis_digest(cites, meta)
    for i in range(6):
        assert f"4期/{i}.md" in digest and f"quote{i}" in digest
    assert len(ev_map) == 6
    assert "ev-6" in ev_map


def test_synthesis_digest_quote_capped_at_400_with_ellipsis():
    """quote は既定 `quote_cap`（400字）までに切り詰め、切り詰めが発生したときだけ「…」を付ける
    （`build_evidence_digest` の60字切断と違い、全件ダイジェストは個別の切断を明示する契約）。"""
    long_quote = "あ" * 500
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": long_quote}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert ("あ" * 400 + "…") in digest
    assert ("あ" * 401) not in digest

    short_quote = "い" * 300
    cites2 = [{"doc_id": "4期/b.md", "span": [1, 1], "quote": short_quote}]
    meta2 = [{"doc_id": "4期/b.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest2, _ = A.build_synthesis_digest(cites2, meta2)
    assert short_quote in digest2
    assert "…" not in digest2   # 切り詰めが起きていないので省略記号を付けない


def test_synthesis_digest_respects_custom_quote_cap():
    """`quote_cap` は呼び出し元が上書きできる（既定400字に固定しない）。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": "0123456789"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta, quote_cap=5)
    assert "01234…" in digest
    assert "56789" not in digest


def test_synthesis_digest_truncates_by_evidence_unit_with_omitted_count_notice():
    """`max_bytes` を超える分は根拠単位（行の途中では切らない）で末尾から打ち切り、
    「（他 M 件は省略）」を付ける——M は実際に省略した件数と一致し、全体は max_bytes を厳密に
    超えない（許容スラック無し）。"""
    cites = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "quote": "x" * 200} for i in range(50)]
    meta = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "verification_method": "span_verified"}
           for i in range(50)]
    digest, ev_map = A.build_synthesis_digest(cites, meta, max_bytes=2048)
    assert len(digest.encode("utf-8")) <= 2048
    lines = digest.splitlines()
    omitted = 50 - len(ev_map)
    assert omitted > 0
    assert lines[-1] == f"（他 {omitted} 件は省略）"
    # 打ち切られた行は根拠単位（途中の文字で切れた半端な ev-N 行が残っていない）。
    assert all(line.startswith("ev-") for line in lines[:-1])


def test_synthesis_digest_no_truncation_notice_when_everything_fits():
    """全件が `max_bytes` に収まるときは打ち切り注記を付けない。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": "短い引用"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "省略" not in digest


def test_synthesis_digest_includes_list_docs_and_graph_structural_evidence():
    """list_docs 集計・graph カードは `build_evidence_digest` と同じ表現（文書パス先頭10件・
    裏付け doc 先頭5件のまま）で全件ダイジェストにも入る——引用が無くても『該当なし』にしない。"""
    shown = [f"4期/{i}.md" for i in range(20)]
    doc = "4期/設計/請求.md"
    meta = [
        {"doc_id": None, "span": None, "verification_method": "list_docs_verified",
         "list_meta": {"count": 1000, "shown": 20, "prefix": "4期", "pattern": ""},
         "matched_doc_ids": shown},
        {"doc_id": None, "span": None, "verification_method": "graph_verified",
         "source_type": "graph", "matched_doc_ids": [doc],
         "card_meta": {"name": "BILLINGJOB", "role": "実装", "category": "プログラム",
                       "path": ["請求処理", "BILLINGJOB"]}},
    ]
    digest, ev_map = A.build_synthesis_digest([], meta)
    assert "[list_docs]" in digest and "該当 1000 件" in digest
    assert all(p in digest for p in shown[:10])   # 既存どおり先頭10件まで
    assert "[graph]" in digest and "BILLINGJOB" in digest and "請求処理" in digest
    assert ev_map["ev-1"] == shown
    assert ev_map["ev-2"] == [doc]


def test_synthesis_digest_list_docs_join_separator_does_not_swallow_next_path():
    """`build_evidence_digest`（`test_ext2_evidence.py` 側で固定）と同じ列挙区切りの契約——
    区切りに空白が無いと `key=value` 形の秘密を含む1件目の直後で2件目が消える回帰を防ぐ。"""
    meta = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
            "list_meta": {"count": 2, "shown": 2, "prefix": "", "pattern": ""},
            "matched_doc_ids": ["config/api_key=secret.md", "keep.md"]}]
    digest, _ = A.build_synthesis_digest([], meta)
    assert "keep.md" in digest


def test_synthesis_digest_ev_id_matches_build_evidence_digest():
    """同じ入力を渡せば `build_evidence_digest` と同じ ev-N が同じ doc_id を指す
    （両関数の唯一の共通契約・`combined_evidence_meta` の添字＋1）。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": "q1"},
            {"doc_id": "4期/b.md", "span": [2, 2], "quote": "q2"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"},
           {"doc_id": "4期/b.md", "span": [2, 2], "verification_method": "span_verified"}]
    structural = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
                  "list_meta": {"count": 1, "shown": 1, "prefix": "", "pattern": ""},
                  "matched_doc_ids": ["4期/c.md"]}]
    combined = meta + structural
    synth_digest, synth_map = A.build_synthesis_digest(cites, combined)
    attr_digest, attr_map = A.build_evidence_digest(cites, combined)
    assert set(synth_map.keys()) == set(attr_map.keys()) == {"ev-1", "ev-2", "ev-3"}
    for ev_id in synth_map:
        assert synth_map[ev_id] == attr_map[ev_id]
    # Evidence Packet の採番とも一致する（`build_evidence_digest` の既存契約と同じ検証）。
    packet_evidence = PB._evidence_packet_evidence(combined, set(synth_map.keys()))
    for pe in packet_evidence:
        assert synth_map[pe["evidence_id"]] == ([pe["source_path"]] if pe["source_path"]
                                                 else pe.get("matched_doc_ids"))


def test_synthesis_digest_shows_line_range_from_span():
    """citation の `span`（[start, end]）から「 行 a-b」を組む——`build_evidence_digest` には無い
    情報（清書は根拠の位置を答えの精度に使えるようにする）。"""
    cites = [{"doc_id": "4期/a.md", "span": [12, 34], "quote": "本文"}]
    meta = [{"doc_id": "4期/a.md", "span": [12, 34], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "4期/a.md 行 12-34「本文」" in digest


def test_synthesis_digest_omits_line_range_when_span_missing():
    """span が無い（rag_chunks 由来等）citation は doc_id だけの表示に落ちる（黙って壊れない）。"""
    cites = [{"doc_id": "4期/a.md", "span": None, "quote": "本文"}]
    meta = [{"doc_id": "4期/a.md", "span": None, "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "4期/a.md「本文」" in digest
    assert " 行 " not in digest


def test_synthesis_digest_appends_extra_quotes_when_present():
    """`citations.merge_overlapping_citations` が `evidence_meta[i]['extra_quotes']` に積んだ、
    統合で消えなかった別の一致を「／別の一致: …」として追記する。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 6], "quote": "採用された広い引用"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 6], "verification_method": "span_verified",
            "extra_quotes": ["別の一致その1", "別の一致その2"]}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "採用された広い引用" in digest
    assert "／別の一致: " in digest
    assert "別の一致その1" in digest and "別の一致その2" in digest


def test_synthesis_digest_no_extra_quotes_suffix_when_absent():
    """`extra_quotes` が無い（統合されていない）citation には「／別の一致」を付けない。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": "単独の引用"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "／別の一致" not in digest


def test_synthesis_digest_empty_input_returns_empty_digest():
    digest, ev_map = A.build_synthesis_digest([], [])
    assert digest == "" and ev_map == {}


def test_synthesis_digest_redacts_secrets_and_strips_control_chars():
    """`_digest_clean`（redact＋制御文字除去）は全件ダイジェストにも適用する
    （帰属 digest と同じ安全側の契約・秘密や偽装改行を清書プロンプトへそのまま渡さない）。"""
    cites = [{"doc_id": "4期/a.md", "span": [1, 1],
             "quote": "config: api_key=sk-ABCDEFGHIJKLMNOP1234\n偽装行"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, _ = A.build_synthesis_digest(cites, meta)
    assert "[REDACTED]" in digest
    assert "sk-ABCDEFGHIJKLMNOP1234" not in digest
    assert len(digest.splitlines()) == 1


def test_synthesis_digest_default_constants_match_documented_contract():
    """既定の `quote_cap`/`max_bytes` は仕様どおり400字／24KiB（呼び出し元が指定しない場合の値）。"""
    assert A._SYNTHESIS_QUOTE_CAP == 400
    assert A._SYNTHESIS_MAX_BYTES == 24 * 1024


# ===== C RV是正2巡目: `gaps` 引数（調査の限界を清書入力へ続けて渡す）=====

def test_synthesis_digest_gaps_appended_after_citations_and_read_evidence():
    cites = [{"doc_id": "4期/a.md", "span": [1, 1], "quote": "本文"}]
    meta = [{"doc_id": "4期/a.md", "span": [1, 1], "verification_method": "span_verified"}]
    digest, ev_map = A.build_synthesis_digest(
        cites, meta, read_evidence=[{"doc_id": "4期/b.md", "span": [1, 2], "text": "精読本文"}],
        gaps=["ripgrep_search『税率』: 0件"])
    lines = digest.splitlines()
    assert lines[0].startswith("ev-1:")             # citation が先頭
    assert lines[1].startswith("精読:")              # read_evidence が続く
    assert lines[2] == "調査の限界: ripgrep_search『税率』: 0件"   # gaps が末尾に続く
    assert set(ev_map.keys()) == {"ev-1"}            # gaps 行は ev_map に登録されない


def test_synthesis_digest_gaps_capped_at_20_items():
    gaps = [f"gap-{i}" for i in range(25)]
    digest, _ = A.build_synthesis_digest([], [], gaps=gaps)
    gap_lines = [ln for ln in digest.splitlines() if ln.startswith("調査の限界: ")]
    assert len(gap_lines) == A._SYNTHESIS_GAPS_MAX_ITEMS == 20
    assert "gap-19" in digest
    assert "gap-20" not in digest   # 20件を超えた分は渡さない（先頭優先）


def test_synthesis_digest_gap_capped_at_200_chars():
    long_gap = "x" * 300
    digest, _ = A.build_synthesis_digest([], [], gaps=[long_gap])
    assert A._SYNTHESIS_GAP_CAP == 200
    assert ("x" * 200) in digest and ("x" * 201) not in digest   # 1件は200字までに切り詰め


def test_synthesis_digest_empty_gaps_do_not_add_limite_section():
    digest, _ = A.build_synthesis_digest([], [], gaps=[])
    assert digest == ""
    digest2, _ = A.build_synthesis_digest([], [])   # 省略時も同じ（既存呼び出し元は無変更）
    assert digest2 == ""


def test_synthesis_digest_gaps_share_max_bytes_budget_with_truncation_notice():
    cites = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "quote": "x" * 100} for i in range(10)]
    meta = [{"doc_id": f"4期/{i}.md", "span": [1, 1], "verification_method": "span_verified"}
           for i in range(10)]
    digest, ev_map = A.build_synthesis_digest(
        cites, meta, gaps=["ripgrep_search『税率』: 0件"] * 5, max_bytes=512)
    assert len(digest.encode("utf-8")) <= 512
    assert "省略" in digest   # 予算に収まらない分がある


def test_synthesis_digest_gaps_are_redacted():
    digest, _ = A.build_synthesis_digest([], [], gaps=["config: api_key=sk-ABCDEFGHIJKLMNOP1234"])
    assert "sk-ABCDEFGHIJKLMNOP1234" not in digest
    assert "[REDACTED]" in digest
