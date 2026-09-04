"""sherpa/store/shares.py の unit テスト（フェーズ7 S6・9%→引き上げ）。

会話共有・sanitized snapshot の境界ロジックを実 DB（非破壊）で round-trip する:
  - get_conversation_for_read: 所有者読み／受領共有の active・revoked・personal_blocked 分岐。
  - create_share / resolve_share_by_token / is_invited / accept_share（冪等・共有元消失） / revoke_share。
  - create_sanitized_snapshot: personal フラグ／旧 answer マーカー双方の taint 判定・2nd pass（直前 user
    ターンの伏字連動）・非個人ターンの allowlist 再構築。
  - _safe_share_answer / _strip_shared_message の純関数分岐（DB 非依存・clarify・非 dict 入力）。

既存 tests/api/test_sanitized_share.py・test_auth_sharing.py が同じロジックを「api」マーカーで
厚く検証済みだが、カバレッジゲート（`-m "unit or contract"`）には乗らない。本ファイルは同じ store
関数を tests/unit/ から直接叩くことでゲート対象にする（表面的な行数稼ぎでなく、意味的アサーション
＋既存テストが薄い分岐＝clarify/非 dict 入力を補う）。
"""
from __future__ import annotations

import json
import time

import pytest

from sherpa import store


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


# ===== 純関数（DB 非依存）: _safe_share_answer / _strip_shared_message =====
# 既存 tests/api/test_sanitized_share.py が allowlist 本体・bad lens 落としを検証済みのため、
# ここでは薄い分岐（clarify 専用形・非 dict 入力）だけを補う。

def test_safe_share_answer_clarify_returns_minimal_shape_only():
    out = store._safe_share_answer({"lens": "clarify", "question": {"type": "question"}, "extra": "leak"})
    assert out == {"lens": "clarify"}          # question・extra は一切持ち込まない


def test_safe_share_answer_non_dict_input_returns_none():
    assert store._safe_share_answer(None) is None
    assert store._safe_share_answer("not-a-dict") is None


def test_safe_share_answer_keeps_sources_verified_as_string_list():
    """EXT-2/EV-0（拡張設計 §4.4）: `sources_verified`（出典2区分表示の doc_id 集合）は既知の形
    （文字列の list）へ再構築して共有へ通す（他の allowlist フィールドと同じ経路・`data` の丸ごと
    コピーとは別に明示的に扱う）。文字列でない要素は落とす。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md"}, {"doc_id": "another.md"}],
        "sources_verified": ["real.md", 123, None, "another.md"],
    })
    assert out["sources_verified"] == ["another.md", "real.md"]   # 昇順ソート


def test_safe_share_answer_intersects_sources_verified_with_surviving_sources():
    """`sources_verified` は他フィルタ（個人ヒット除去等）で `sources` から実際に消えた doc_id を
    「精読済み」として復活させない——`sources` に無い doc_id は sources_verified からも落ちる。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md"}],   # dropped.md は sources に無い（他フィルタで消えた想定）
        "sources_verified": ["real.md", "dropped.md"],
    })
    assert out["sources_verified"] == ["real.md"]


def test_safe_share_answer_passes_scope_with_layer_wholesale():
    """`scope`（`layer`/`layer_applied` を含む）は allowlist で個別フィールドを選ばず
    丸ごと通す——将来 scope に新キーが増えても、この一般規則により自動で共有へ乗る契約を固定する。"""
    scope = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit",
             "layer": "code", "layer_applied": True}
    out = store._safe_share_answer({"lens": "qa", "headline": "h", "scope": scope})
    assert out["scope"] == scope


def test_safe_share_answer_omits_sources_verified_when_absent():
    out = store._safe_share_answer({"lens": "qa", "headline": "h", "sources": []})
    assert "sources_verified" not in out


def test_safe_share_answer_excludes_importance_control_file_from_sources():
    """`_重要度.txt` は共有 snapshot の出典にも出さない（§5・独立入口として
    再チェック——上流の `chat_service._sources` を経ない sources が渡ってきても弾く）。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md"}, {"doc_id": "_重要度.txt"}, {"doc_id": "4期/_重要度.txt"}],
    })
    assert [s["doc_id"] for s in out["sources"]] == ["real.md"]


def test_safe_share_answer_keeps_importance_fields_but_not_source():
    """I2（2026-09-05）: `importance`/`importance_reason`（登録者重要度の表示値）は sanitized
    snapshot の allowlist にも残る。`importance_source`（`_重要度.txt` の由来監査情報）は
    出さない（J4）。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md", "download_url": "/x", "importance": "高",
                    "importance_reason": "契約書", "importance_source": "_重要度.txt:1行目"}],
    })
    s = out["sources"][0]
    assert s["importance"] == "高" and s["importance_reason"] == "契約書"
    assert "importance_source" not in s


def test_safe_share_answer_and_strip_shared_message_agree_on_importance_fields():
    """`_safe_share_answer`（allowlist 再構築）と `_strip_shared_message`（denylist・他は素通し）
    の2経路が、`importance`/`importance_reason` の扱いで一致する（両経路とも出す・提案書 I2 実装6）。"""
    answer = {"lens": "qa", "headline": "h",
             "sources": [{"doc_id": "real.md", "download_url": "/x", "importance": "低"}]}
    safe = store._safe_share_answer(answer)
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": {"x": 1},
        "trace": {"y": 1}, "answer": answer}
    stripped = store._strip_shared_message(m)
    assert safe["sources"][0]["importance"] == "低"
    assert stripped["answer"]["sources"][0]["importance"] == "低"


def test_safe_share_answer_excludes_importance_control_file_from_data_citations():
    """`data.citations[]`（qa 等の生 citation）からも重要度設定ファイルを
    除外する（top-level `sources[]` だけでは `data.citations` に紐付く doc_id 参照が残ってしまう）。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h", "sources": [{"doc_id": "real.md"}],
        "data": {"citations": [
            {"doc_id": "real.md", "span": [1, 2], "quote": "q", "ext": ".md"},
            {"doc_id": "_重要度.txt", "span": [1, 1], "quote": "q2", "ext": ".txt"},
        ]},
    })
    assert [c["doc_id"] for c in out["data"]["citations"]] == ["real.md"]


def test_safe_share_answer_excludes_importance_control_file_from_candidates():
    """troubleshoot の候補（`data.candidates[]`）からも重要度設定ファイルを
    除外する——grep のみの候補は `name` 自体が doc_id（`lens_service._troubleshoot_cards` の
    `label="Document"` 分岐）。生き残った候補の `evidence.grep`/`evidence.edges` からも除外する。"""
    out = store._safe_share_answer({
        "lens": "troubleshoot", "headline": "h", "sources": [],
        "data": {"candidates": [
            {"name": "_重要度.txt", "label": "Document", "role": "運用手順", "distance": None,
             "path": [], "source": "grep", "evidence": {"edges": [], "grep": [{"doc_id": "_重要度.txt"}]}},
            {"name": "ORDER-MAIN", "label": "Module", "role": "実装", "distance": 1, "path": [],
             "source": "both",
             "evidence": {"edges": [{"type": "REALIZES", "doc": "_重要度.txt"},
                                   {"type": "REALIZES", "doc": "real.cbl"}],
                          "grep": [{"doc_id": "real.md"}, {"doc_id": "_重要度.txt"}]}},
        ]},
    })
    cands = out["data"]["candidates"]
    assert [c["name"] for c in cands] == ["ORDER-MAIN"]     # grep-only 候補は丸ごと落ちる
    assert cands[0]["evidence"]["edges"] == [{"type": "REALIZES", "doc": "real.cbl"}]
    assert cands[0]["evidence"]["grep"] == [{"doc_id": "real.md"}]


def test_safe_share_answer_excludes_importance_control_file_from_impact_items():
    """impact レンズの `data.items[]`/`data.presumed[]`
    （`chat_service._answer_impact` が生の result をそのまま `data` へ埋め込む）の
    evidence からも重要度設定ファイルへの参照を落とす。要素自体（グラフ由来の結論）は残す。"""
    out = store._safe_share_answer({
        "lens": "impact", "headline": "h", "sources": [],
        "data": {
            "items": [{"name": "ORDER-MAIN", "judgement": "sure",
                      "evidence": [{"doc": "_重要度.txt", "line": 1}, {"doc": "real.md", "line": 2}]}],
            "presumed": [{"name": "BILLGEN", "evidence": [{"doc": "_重要度.txt", "line": 1}]}],
        },
    })
    items = out["data"]["items"]
    assert items[0]["name"] == "ORDER-MAIN"                  # 結論自体は残る
    assert items[0]["evidence"] == [{"doc": "real.md", "line": 2}]
    assert out["data"]["presumed"][0]["evidence"] == []


def test_safe_evidence_item_excludes_importance_control_file_from_source_path_and_matched_doc_ids():
    """Evidence Packet の `source_path`／`matched_doc_ids` からも
    重要度設定ファイルを除外する（§5・独立入口として再チェック）。"""
    out = store._safe_evidence_item({
        "evidence_id": "ev-1", "source_type": "document", "source_path": "_重要度.txt",
        "verification_method": "span_verified",
    })
    assert out["source_path"] is None

    out2 = store._safe_evidence_item({
        "evidence_id": "ev-2", "source_type": "document", "source_path": None,
        "matched_doc_ids": ["real.md", "_重要度.txt", "4期/_重要度.txt"],
    })
    assert out2["matched_doc_ids"] == ["real.md"]


def test_strip_shared_message_excludes_importance_control_file_from_sources():
    """通常の受領共有（sanitized snapshot を経由しない元会話そのままの
    read path）でも、旧会話（除外契約が無かった頃に保存された sources）の重要度設定ファイルを
    読者に見せない。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": {"x": 1},
         "trace": {"y": 1}, "answer": {
             "lens": "qa", "headline": "h",
             "sources": [{"doc_id": "real.md"}, {"doc_id": "_重要度.txt"}],
         }}
    out = store._strip_shared_message(m)
    assert [s["doc_id"] for s in out["answer"]["sources"]] == ["real.md"]
    assert out["route"] is None and out["trace"] is None


def test_strip_shared_message_excludes_importance_control_file_from_data_citations():
    """通常の受領共有でも `data.citations[]` から重要度設定ファイルを
    除外する（`sources[]` だけでは足りない・`_safe_share_answer` と共有する実装）。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": None, "trace": None,
         "answer": {"lens": "qa", "headline": "h",
                    "data": {"citations": [
                        {"doc_id": "real.md", "span": [1, 2], "quote": "q", "ext": ".md"},
                        {"doc_id": "_重要度.txt", "span": [1, 1], "quote": "q2", "ext": ".txt"},
                    ]}}}
    out = store._strip_shared_message(m)
    assert [c["doc_id"] for c in out["answer"]["data"]["citations"]] == ["real.md"]


def test_strip_shared_message_excludes_importance_control_file_from_evidence_packet():
    """通常の受領共有でも Evidence Packet の `source_path` から
    重要度設定ファイルを除外する。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": None, "trace": None,
         "answer": {"lens": "qa", "headline": "h",
                    "data": {"evidence_packet": {
                        "task_id": "main",
                        "evidence": [{"evidence_id": "ev-1", "source_type": "document",
                                     "source_path": "_重要度.txt"}],
                    }}}}
    out = store._strip_shared_message(m)
    assert out["answer"]["data"]["evidence_packet"]["evidence"][0]["source_path"] is None


def test_strip_shared_message_excludes_importance_control_file_from_candidates():
    """通常の受領共有でも `data.candidates[]` から重要度設定ファイルを除外する
    （`_safe_share_answer` と共有する実装）。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "troubleshoot", "route": None, "trace": None,
         "answer": {"lens": "troubleshoot", "headline": "h",
                    "data": {"candidates": [
                        {"name": "_重要度.txt", "label": "Document", "evidence": {"edges": [], "grep": []}},
                        {"name": "ORDER-MAIN", "label": "Module",
                         "evidence": {"edges": [{"type": "REALIZES", "doc": "_重要度.txt"}], "grep": []}},
                    ]}}}
    out = store._strip_shared_message(m)
    cands = out["answer"]["data"]["candidates"]
    assert [c["name"] for c in cands] == ["ORDER-MAIN"]
    assert cands[0]["evidence"]["edges"] == []


def test_strip_shared_message_excludes_importance_control_file_from_impact_items():
    """通常の受領共有でも `data.items[]`/`data.presumed[]` の evidence から
    重要度設定ファイルへの参照を落とす（`_safe_share_answer` と共有する実装）。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "impact", "route": None, "trace": None,
         "answer": {"lens": "impact", "headline": "h",
                    "data": {"items": [{"name": "ORDER-MAIN", "judgement": "sure",
                                        "evidence": [{"doc": "_重要度.txt"}, {"doc": "real.md"}]}]}}}
    out = store._strip_shared_message(m)
    assert out["answer"]["data"]["items"][0]["evidence"] == [{"doc": "real.md"}]


def test_strip_shared_message_intersects_sources_verified_with_surviving_sources():
    """通常の受領共有でも sources_verified を生き残った sources と再交差する
    （sanitized snapshot 側の `_safe_share_answer` と同じ扱い・共通ヘルパ `_intersect_sources_verified`）。
    フィルタで sources から消えた doc_id（重要度設定ファイル）は sources_verified からも落ちる。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": None, "trace": None,
         "answer": {"lens": "qa", "headline": "h",
                    "sources": [{"doc_id": "real.md"}, {"doc_id": "_重要度.txt"}],
                    "sources_verified": ["real.md", "_重要度.txt"]}}
    out = store._strip_shared_message(m)
    assert out["answer"]["sources_verified"] == ["real.md"]


def test_strip_shared_message_keeps_answer_untouched_when_nothing_needs_filtering():
    """フィルタで何も変わらない（重要度設定ファイルが無い等）場合、`answer` は元のオブジェクトを
    そのまま返す（余計な copy をしない・sources_verified もそのまま）。"""
    m = {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": None, "trace": None,
         "answer": {"lens": "qa", "headline": "h",
                    "sources": [{"doc_id": "real.md"}],
                    "sources_verified": ["real.md"]}}
    out = store._strip_shared_message(m)
    assert out["answer"] is m["answer"]
    assert out["answer"]["sources_verified"] == ["real.md"]


def test_safe_share_answer_troubleshoot_candidates_have_no_internal_cid():
    """`data`（troubleshoot の候補カード）は `_safe_share_answer` で浅いコピーされる
    （`store/shares.py::_safe_share_answer` 内・`dict(data)`）——公開経路
    （`lens_service.run_troubleshoot`）が既に `cid`（内部専用の Neo4j canonical_id）を除去済み
    である前提のまま共有されることを固定する（浅いコピー自体が cid を新たに持ち込まない）。"""
    out = store._safe_share_answer({
        "lens": "troubleshoot", "headline": "h", "sources": [],
        "data": {"candidates": [{"name": "ORDER-MAIN", "label": "Module", "role": "実装",
                                 "distance": 1, "path": [], "source": "graph",
                                 "evidence": {"edges": [], "grep": []}}]},
    })
    assert "cid" not in out["data"]["candidates"][0]


def test_safe_evidence_packet_keeps_only_known_fields():
    """EXT-2: Evidence Packet は `citations.build_evidence_packet` と同じ既知フィールドだけを通す
    （スキーマが将来拡張されても、未知キー＝locator・秘匿種別等の内部表現は自動的に落ちる）。"""
    packet = {
        "task_id": "main", "investigation_status": "sufficient", "summary": "s",
        "claims": ["c1"], "evidence": [{"evidence_id": "ev-1", "source_type": "document",
                                        "source_path": "real.md", "source_span": [1, 2],
                                        "verification_method": "span_verified",
                                        "_internal_locator": "leak-me"}],
        "remaining_gaps": ["g1"], "conflicts": [], "candidates_seen": 3, "candidates_inspected": 2,
        "evidence_selected": 1, "stop_reason": "no_tool_calls", "next_action": "commit_evidence",
        "_secret": "should not leak",
    }
    out = store._safe_evidence_packet(packet)
    assert set(out.keys()) == {
        "task_id", "investigation_status", "summary", "claims", "evidence", "remaining_gaps",
        "conflicts", "candidates_seen", "candidates_inspected", "evidence_selected", "stop_reason",
        "next_action"}
    assert "_secret" not in out
    assert out["evidence"] == [{"evidence_id": "ev-1", "source_type": "document",
                                "source_path": "real.md", "source_span": [1, 2],
                                "verification_method": "span_verified"}]   # _internal_locator は落ちる


def test_safe_evidence_packet_non_dict_returns_none():
    assert store._safe_evidence_packet(None) is None
    assert store._safe_evidence_packet("not-a-dict") is None


def test_safe_locator_keeps_known_fields_with_valid_types():
    """`source_span` が行番号2要素ではなく構造化 locator（`evidence_ir.Locator` 由来・Office/PDF/
    画像由来）のとき、既知フィールド（page/slide/sheet/cell_range/part/object_id/bbox）＋既知の
    値型だけを通す。"""
    loc = {"page": 3, "slide": 2, "sheet": "Sheet1", "cell_range": "A1:B2",
          "part": "xl/worksheets/sheet1.xml", "object_id": "shape42",
          "bbox": [1.0, 2.5, 3, 4], "_secret": {"leak": "me"}, "extension": {"leak": "me"}}
    out = store._safe_locator(loc)
    assert out == {"page": 3, "slide": 2, "sheet": "Sheet1", "cell_range": "A1:B2",
                   "part": "xl/worksheets/sheet1.xml", "object_id": "shape42",
                   "bbox": [1.0, 2.5, 3, 4]}
    assert "_secret" not in out and "extension" not in out


def test_safe_locator_object_id_accepts_str_or_int():
    """`object_id` は `Locator.object_id` と同じ `str | int` のどちらも受け付ける。"""
    assert store._safe_locator({"object_id": "shape-42"}) == {"object_id": "shape-42"}
    assert store._safe_locator({"object_id": 42}) == {"object_id": 42}
    assert store._safe_locator({"object_id": True}) is None   # bool は int のサブクラスだが対象外
    assert store._safe_locator({"object_id": -1}) is None      # 数値は正のみ（page/slide と同じ制約）
    assert store._safe_locator({"object_id": 3.5}) is None     # float は不可


def test_safe_locator_drops_invalid_types_per_field():
    """フィールドごとに型検証する——page/slide は正の非 bool int（6桁上限）のみ、sheet/cell_range/
    part は短い文字列のみ、bbox は有限（NaN/Infinity 不可）な数値4要素のみ。不正な値はキーごと
    落とす（他フィールドは残る）。"""
    assert store._safe_locator({"page": -1}) is None            # 正でない
    assert store._safe_locator({"page": True}) is None          # bool は int 扱いしない
    assert store._safe_locator({"page": "3"}) is None            # 文字列は不可
    assert store._safe_locator({"page": 1_000_000}) is None      # 6桁上限を超える
    assert store._safe_locator({"page": 999_999}) == {"page": 999_999}   # 上限ちょうどは通る
    assert store._safe_locator({"sheet": "x" * 41}) is None        # 長すぎる（sheet/cell_range は40字上限）
    assert store._safe_locator({"part": "x" * 201}) is None        # part は200字上限
    assert store._safe_locator({"bbox": [1, 2, 3]}) is None        # 要素数が4でない
    assert store._safe_locator({"bbox": ["a", "b", "c", "d"]}) is None   # 数値でない
    assert store._safe_locator({"bbox": [float("nan"), 1, 2, 3]}) is None   # NaN は不可
    assert store._safe_locator({"bbox": [float("inf"), 1, 2, 3]}) is None   # Infinity は不可
    assert store._safe_locator({"bbox": [10**7, 1, 2, 3]}) is None          # 上限超の巨大値は不可


def test_safe_locator_huge_int_bbox_does_not_raise_and_is_dropped():
    """`math.isfinite(int)` は内部で float 変換するため、桁が巨大な Python int（任意精度）を渡すと
    `OverflowError` を送出する——共有処理全体を落としうる。`_safe_locator` は絶対値の上限比較を
    isfinite より先に行うため、`10**10000` のような超巨大 int でも例外を送出せず、bbox キーだけを
    落として他の有効フィールドは残す。"""
    out = store._safe_locator({"page": 3, "bbox": [1, 2, 3, 10**10000]})
    assert out == {"page": 3}   # bbox はキーごと除去・page は残る（例外を送出しない）
    assert store._safe_locator({"bbox": [10**10000, 1, 2, 3]}) is None


def test_safe_locator_normalizes_embedded_newlines_instead_of_rejecting():
    """`\\n`・`\\r`・U+2028（LINE SEPARATOR）・U+2029（PARAGRAPH SEPARATOR）等の改行類を含む文字列は
    citations.py の `_clean_locator_field` と同じ規則で単一空白へ正規化する（値ごと落とすのでは
    ない）——秘密の抽出/表示崩れの手段にならないよう改行だけを潰しつつ、正規の値は保持する。"""
    assert store._safe_locator({"sheet": "line1\nline2"}) == {"sheet": "line1 line2"}
    assert store._safe_locator({"sheet": "line1\rline2"}) == {"sheet": "line1 line2"}
    assert store._safe_locator({"sheet": "line1\u2028line2"}) == {"sheet": "line1 line2"}
    assert store._safe_locator({"sheet": "line1\u2029line2"}) == {"sheet": "line1 line2"}
    # 一部だけ不正でも他の有効フィールドは残る（キー単位で判定）。
    assert store._safe_locator({"page": 3, "sheet": "line1\nline2"}) == {"page": 3, "sheet": "line1 line2"}


def test_safe_locator_non_dict_or_empty_returns_none():
    assert store._safe_locator(None) is None
    assert store._safe_locator("not-a-dict") is None
    assert store._safe_locator({}) is None
    assert store._safe_locator({"unknown": "field"}) is None   # 既知キーが1つも無ければ None


def test_safe_evidence_item_accepts_structured_locator_span():
    """`source_span` が構造化 locator のとき（行番号2要素でないとき）`_safe_locator` を通してから
    保持する——未知の入れ子秘密キーは落ちるが、既知の locator フィールドは失われない。"""
    e = {"evidence_id": "ev-1", "source_type": "document", "source_path": "報告書.pptx",
        "source_span": {"slide": 4, "_secret": "leak"}, "verification_method": "exists_no_span"}
    out = store._safe_evidence_item(e)
    assert out["source_span"] == {"slide": 4}
    assert out["source_path"] == "報告書.pptx"


def test_safe_evidence_item_drops_source_span_with_no_valid_locator_fields():
    """`source_span` が行番号2要素でも locator でもない（既知フィールドが1つも無い）場合は
    キーごと落とす（None にすり替えない＝未知の形を持ち込ませない）。"""
    e = {"evidence_id": "ev-1", "source_span": {"totally": "unknown"}}
    out = store._safe_evidence_item(e)
    assert "source_span" not in out


def test_safe_evidence_item_used_true_and_false_pass_through():
    """EV-0（拡張設計 §4.4）: `used` は厳密 bool（True/False どちらも）を共有へそのまま通す。"""
    assert store._safe_evidence_item({"evidence_id": "ev-1", "used": True})["used"] is True
    assert store._safe_evidence_item({"evidence_id": "ev-1", "used": False})["used"] is False


def test_safe_evidence_item_used_invalid_type_is_dropped():
    """`used` が bool でない（1/"true"/None 等）ときはキーごと落とす（値をすり替えない）。"""
    for bad in (1, 0, "true", "false", None, [], {}):
        out = store._safe_evidence_item({"evidence_id": "ev-1", "used": bad})
        assert "used" not in out, f"bad={bad!r} が通ってしまった"


def test_safe_evidence_item_used_absent_stays_absent():
    out = store._safe_evidence_item({"evidence_id": "ev-1"})
    assert "used" not in out


def test_safe_evidence_item_keeps_matched_doc_ids_list_meta_and_card_meta():
    """list_docs/graph の集計・カード単位 Evidence（`source_path=None`）は `matched_doc_ids`
    だけでなく `list_meta`（総件数・条件・列挙範囲）／`card_meta`（対象名・関係・カテゴリ・経路）も
    共有 round-trip で保持する——`matched_doc_ids` だけでは、条件の異なる list_docs 呼び出しや
    path/category が異なる graph カードが共有先で見分けが付かなくなる。"""
    e = {"evidence_id": "ev-1", "source_type": "graph", "source_path": None,
        "matched_doc_ids": ["module:v1:04_運用/taxcalc.cob#TAXCALC"],
        "list_meta": {"count": 3, "shown": 2, "prefix": "4期", "pattern": "*.md"},
        "card_meta": {"name": "TAXCALC", "role": "実装", "category": "プログラム",
                     "path": ["請求処理", "TAXCALC"]}}
    out = store._safe_evidence_item(e)
    assert out["matched_doc_ids"] == ["module:v1:04_運用/taxcalc.cob#TAXCALC"]
    assert out["list_meta"] == {"count": 3, "shown": 2, "prefix": "4期", "pattern": "*.md"}
    assert out["card_meta"] == {"name": "TAXCALC", "role": "実装", "category": "プログラム",
                                "path": ["請求処理", "TAXCALC"]}


def test_safe_evidence_item_drops_matched_doc_ids_with_non_string_elements():
    """`matched_doc_ids` は文字列のリストのときだけ通す（未知の形はキーごと落とす）。"""
    out = store._safe_evidence_item({"evidence_id": "ev-1", "matched_doc_ids": ["a", 1, None]})
    assert "matched_doc_ids" not in out
    assert "list_meta" not in out
    assert "card_meta" not in out


def test_safe_evidence_item_list_meta_drops_unknown_type_fields():
    """`list_meta`/`card_meta` は既知フィールド・既知の型だけを通す allowlist——型不正・未知
    キーが紛れても既知の正しいフィールドまでは失わない。"""
    e = {"evidence_id": "ev-1", "matched_doc_ids": ["a.md"],
        "list_meta": {"count": "not-an-int", "shown": 2, "_secret": "leak"},
        "card_meta": {"name": "X", "path": [1, 2], "_secret": "leak"}}
    out = store._safe_evidence_item(e)
    assert out["list_meta"] == {"shown": 2}          # count は型不正で落ちる・shown は残る
    assert out["card_meta"] == {"name": "X"}          # path は要素が str でないため落ちる・name は残る


def test_safe_evidence_item_list_meta_and_card_meta_absent_when_no_matched_doc_ids():
    """通常の citation 由来 Evidence（`matched_doc_ids` を持たない）には `list_meta`/`card_meta`
    キー自体が現れない。"""
    out = store._safe_evidence_item({"evidence_id": "ev-1", "source_path": "a.md",
                                     "list_meta": {"count": 1}})
    assert "matched_doc_ids" not in out
    assert "list_meta" not in out
    assert "card_meta" not in out


def test_safe_share_answer_reconstructs_evidence_packet_via_allowlist():
    """`data` は KB 由来として丸ごと通すが、`evidence_packet` だけは allowlist 再構築を経由する。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h", "sources": [],
        "data": {"citations": [{"doc_id": "real.md"}],
                 "evidence_packet": {"task_id": "main", "investigation_status": "sufficient",
                                     "_secret": "leak"}},
    })
    assert out["data"]["citations"] == [{"doc_id": "real.md"}]      # 他の data フィールドは無改修
    assert "_secret" not in out["data"]["evidence_packet"]
    assert out["data"]["evidence_packet"]["task_id"] == "main"


def test_safe_share_answer_round_trip_preserves_used_true_false_drops_invalid():
    """EV-0: `_safe_share_answer`（sanitized 共有の allowlist 経路）を通しても
    Packet の `evidence[].used` は True/False をそのまま保持し、型不正は落ちる。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h", "sources": [],
        "data": {"evidence_packet": {
            "task_id": "main", "investigation_status": "sufficient",
            "evidence": [
                {"evidence_id": "ev-1", "source_path": "a.md", "used": True},
                {"evidence_id": "ev-2", "source_path": "b.md", "used": False},
                {"evidence_id": "ev-3", "source_path": "c.md", "used": "yes"},
            ],
        }},
    })
    ev = out["data"]["evidence_packet"]["evidence"]
    assert ev[0]["used"] is True
    assert ev[1]["used"] is False
    assert "used" not in ev[2]   # 型不正（文字列）は共有に出ない


def test_strip_shared_message_drops_route_trace_and_answer_internal_keys():
    msg = {
        "id": 1, "role": "assistant", "content": "answer text",
        "route": {"tool": "grep salary.xlsx"}, "trace": {"q": "900万"},
        "answer": {"headline": "h", "question": {"type": "question"}, "usage": {"tokens": 10}, "keep": "ok"},
    }
    out = store._strip_shared_message(msg)
    assert out["route"] is None and out["trace"] is None
    assert "question" not in out["answer"] and "usage" not in out["answer"]
    assert out["answer"]["keep"] == "ok"                    # 非内部キーは保持


def test_strip_shared_message_preserves_scope_with_layer():
    """受領共有（非 sanitize）は `route`/`trace`/`question`/`usage` 系だけを伏せ、
    `answer.scope`（`layer`/`layer_applied` 含む）はそのまま読者に届く（`_drop` allowlist に scope が
    無い＝一般キーとして通る契約を固定する）。"""
    scope = {"world": "w1", "scope_paths": [], "source": "all", "layer": "docs", "layer_applied": True}
    msg = {"id": 1, "role": "assistant", "content": "answer text",
          "route": {"tool": "x"}, "trace": {"q": "x"},
          "answer": {"headline": "h", "scope": scope}}
    out = store._strip_shared_message(msg)
    assert out["answer"]["scope"] == scope


def test_strip_shared_message_drops_usage_sub():
    """S3（2026-07-15-LLMオーケストレーション実装計画.md §5.0）: usage_sub（サブループのトークン
    サイドカー）も usage と同格の内部情報＝受領共有の読者に見せない。

    レビュー是正（LOW・2026-07-18 Codex RV 1巡目・§5.0 項6）: `usage_sub` には `profile`
    （どのサブエージェント・プロファイルの消費かを表す id）が追加されたが、`_drop` はキー単位で
    `usage_sub` 全体を落とすため `profile` フィールドも道連れで落ちることを確認する
    （個別フィールドの部分マスクではなく丸ごと非表示＝実装が誤って部分公開しないことの固定）。
    """
    msg = {
        "id": 3, "role": "assistant", "content": "answer text",
        "route": None, "trace": None,
        "answer": {"headline": "h", "usage": {"tokens": 10},
                   "usage_sub": {"provider": "ollama", "model": "qwen2.5", "input_tokens": 5,
                                "profile": "local-worker"},
                   "keep": "ok"},
    }
    out = store._strip_shared_message(msg)
    assert "usage_sub" not in out["answer"] and "usage" not in out["answer"]
    assert "profile" not in str(out["answer"])   # usage_sub.profile も含め丸ごと非公開
    assert "local-worker" not in str(out["answer"])
    assert out["answer"]["keep"] == "ok"


def test_strip_shared_message_drops_usage_subs():
    """S4-b（2026-07-15-LLMオーケストレーション実装計画.md §6.3）: 複数プロファイル並用時の
    複数形サイドカー `usage_subs`（`_run_sub_plan` が集める配列）も `usage_sub`（単数形）と同格の
    内部情報＝受領共有の読者に見せない（同一コミットで `_drop` へ追加・漏洩防止）。"""
    msg = {
        "id": 4, "role": "assistant", "content": "answer text",
        "route": None, "trace": None,
        "answer": {"headline": "h",
                   "usage_subs": [{"provider": "ollama", "model": "qwen2.5", "input_tokens": 5,
                                   "profile": "worker1"},
                                  {"provider": "ollama", "model": "qwen3", "input_tokens": 3,
                                   "profile": "worker2"}],
                   "keep": "ok"},
    }
    out = store._strip_shared_message(msg)
    assert "usage_subs" not in out["answer"]
    assert "worker1" not in str(out["answer"]) and "worker2" not in str(out["answer"])
    assert out["answer"]["keep"] == "ok"


def test_strip_shared_message_passes_through_when_answer_not_dict():
    msg = {"id": 2, "role": "user", "content": "q", "route": None, "trace": None, "answer": None}
    out = store._strip_shared_message(msg)
    assert out["answer"] is None and out["route"] is None and out["trace"] is None


# ===== DB round-trip =====

def _mk_conv(uid: str, title=None) -> int:
    return store.create_conversation(user_id=uid, world="v1", title=title)["id"]


def test_get_conversation_for_read_owner_sees_own_messages_other_uid_sees_none():
    _try_init()
    sfx = _sfx()
    owner = f"unit-shares-owner-{sfx}"
    cid = _mk_conv(owner, title="my conv")
    store.add_message(cid, "user", content="hello")

    result = store.get_conversation_for_read(owner, cid)
    assert result is not None and len(result["messages"]) == 1

    assert store.get_conversation_for_read(f"unit-shares-other-{sfx}", cid) is None


def test_share_lifecycle_create_resolve_invite_accept_revoke():
    """create_share → resolve_share_by_token(active) → is_invited → accept_share(冪等) → revoke_share
    → resolve_share_by_token(active=False) の一連を1本で確認する（相互に依存するため分割しない）。"""
    _try_init()
    sfx = _sfx()
    owner = f"unit-shares-owner2-{sfx}"
    invitee = f"unit-shares-invitee-{sfx}"
    outsider = f"unit-shares-outsider-{sfx}"
    cid = _mk_conv(owner, title="shared conv")
    store.add_message(cid, "user", content="q1")
    store.add_message(cid, "assistant", content="a1")

    th = f"unit-token-hash-{sfx}"
    sid = store.create_share(cid, owner, th, None, [invitee])   # expires_at=None=無期限

    share = store.resolve_share_by_token(th)
    assert share is not None and share["active"] is True and share["conversation_id"] == cid

    assert store.is_invited(sid, invitee) is True
    assert store.is_invited(sid, outsider) is False

    wid1 = store.accept_share(sid, invitee)
    wid2 = store.accept_share(sid, invitee)                    # 二重クリックは同じ wrapper（冪等）
    assert wid1 == wid2

    wrapper = store.get_conversation_for_read(invitee, wid1)
    assert wrapper is not None
    assert len(wrapper["messages"]) == 2                       # 元会話の本文をそのまま参照

    # 取消後は active=False（同一 owner のみ取消可）。
    assert store.revoke_share(sid, outsider) is False           # 所有者以外は不可
    assert store.revoke_share(sid, owner) is True
    assert store.revoke_share(sid, owner) is False               # 二重取消は不成立（rowcount=0）
    share_after = store.resolve_share_by_token(th)
    assert share_after["active"] is False

    # 取消後は get_conversation_for_read が unavailable を返す（読めなくなる）。
    after = store.get_conversation_for_read(invitee, wid1)
    assert after["share_status"] == "unavailable" and after["messages"] == []


def test_get_conversation_for_read_personal_blocked_after_share_created():
    """RV BLOCKER: 共有後に元会話へ個人 workspace 参照フラグが立った場合、受領側は読めない。"""
    _try_init()
    sfx = _sfx()
    owner = f"unit-shares-owner3-{sfx}"
    invitee = f"unit-shares-invitee3-{sfx}"
    cid = _mk_conv(owner)
    store.add_message(cid, "user", content="q")

    th = f"unit-token-hash-pb-{sfx}"
    sid = store.create_share(cid, owner, th, None, [invitee])
    wid = store.accept_share(sid, invitee)

    store.set_contains_personal_workspace(cid)                 # 共有後に個人参照が追加された
    result = store.get_conversation_for_read(invitee, wid)
    assert result["share_status"] == "personal_blocked" and result["messages"] == []


def test_get_conversation_for_read_received_share_strips_internal_fields():
    _try_init()
    sfx = _sfx()
    owner = f"unit-shares-owner4-{sfx}"
    invitee = f"unit-shares-invitee4-{sfx}"
    cid = _mk_conv(owner)
    store.add_message(cid, "user", content="q1")
    store.add_message(cid, "assistant", content="a1",
                      route={"tool": "grep x"}, trace={"q": "x"},
                      answer={"headline": "h", "question": {"type": "question"}, "usage": {"tokens": 1}})

    th = f"unit-token-hash-strip-{sfx}"
    sid = store.create_share(cid, owner, th, None, [invitee])
    wid = store.accept_share(sid, invitee)

    result = store.get_conversation_for_read(invitee, wid)
    assistant_msg = next(m for m in result["messages"] if m["role"] == "assistant")
    assert assistant_msg["route"] is None and assistant_msg["trace"] is None
    assert "question" not in assistant_msg["answer"] and "usage" not in assistant_msg["answer"]
    assert assistant_msg["answer"]["headline"] == "h"


def test_accept_share_raises_value_error_when_share_missing():
    _try_init()
    with pytest.raises(ValueError):
        store.accept_share(-999, "someone")


def test_create_sanitized_snapshot_returns_none_for_missing_source():
    _try_init()
    assert store.create_sanitized_snapshot("someone", -999) is None


def test_create_sanitized_snapshot_redacts_personal_turns_and_keeps_kb_turns():
    """personal=True・旧 answer マーカー（personal_sources）双方の taint 判定＋2nd pass（直前 user の連動）
    を1本で確認する。"""
    _try_init()
    sfx = _sfx()
    owner = f"unit-shares-owner5-{sfx}"
    cid = _mk_conv(owner, title="my_salary.xlsx を要約して")

    # (1) 個人ターン: personal=True（新フラグ）。
    store.add_message(cid, "user", content="my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", content="年収900万です",
                      answer={"headline": "年収900万です",
                              "personal_sources": [{"doc_id": "my_salary.xlsx"}]},
                      personal=True)
    # (2) 旧データ互換: personal フラグ未設定だが answer に旧マーカーがある assistant ターン。
    #     2nd pass で直前の user 質問も伏字化される想定。
    store.add_message(cid, "user", content="別の個人依頼")
    store.add_message(cid, "assistant", content="旧マーカーの回答",
                      answer={"headline": "旧マーカーの回答", "_personal_facts": "年収900万"})
    # (3) 非個人ターン: KB 由来。
    store.add_message(cid, "user", content="TAX-RATE の影響は？")
    store.add_message(cid, "assistant", content="共有KBの一般回答",
                      route={"tool": "es_search"}, trace={"q": "TAX-RATE"},
                      answer={"headline": "共有KBの一般回答", "lens": "impact",
                              "sources": [{"doc_id": "kb1", "source": "KB"},
                                          {"doc_id": "p1", "source": "個人ファイル内ヒット"}]})

    snap_cid = store.create_sanitized_snapshot(owner, cid)
    assert snap_cid is not None

    snap = store.get_conversation_for_read(owner, snap_cid)
    conv = snap["conversation"]
    assert conv["title"] == store._SANITIZED_TITLE           # 元タイトル（個人ファイル名含む）は漏れない
    assert conv["read_only"] is True and conv["contains_personal_workspace"] is False

    msgs = snap["messages"]
    assert len(msgs) == 6
    # (1) 新フラグ taint: Q/A とも伏字。
    assert msgs[0]["content"] == store._REDACTED_TEXT
    assert msgs[1]["content"] == store._REDACTED_TEXT and msgs[1]["answer"] == {"headline": store._REDACTED_TEXT}
    # (2) 旧マーカー taint: assistant 本体＋2nd pass で直前 user も伏字。
    assert msgs[2]["content"] == store._REDACTED_TEXT          # 「別の個人依頼」も伏字化される
    assert msgs[3]["content"] == store._REDACTED_TEXT
    # (3) 非個人ターン: content 保持・answer は allowlist 再構築（個人ヒットは除去・route/trace は常に NULL）。
    assert msgs[4]["content"] == "TAX-RATE の影響は？"
    assert msgs[5]["content"] == "共有KBの一般回答"
    assert msgs[5]["route"] is None and msgs[5]["trace"] is None
    assert [s["doc_id"] for s in msgs[5]["answer"]["sources"]] == ["kb1"]   # 個人ヒット除去


class _CidFakeRecord:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _CidFakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(_CidFakeRecord(r) for r in self._rows)

    def consume(self):
        pass

    def data(self):
        # rv-s3-removal: `check_schema_era`（`neo4j_related` の主クエリ直後に呼ばれる）は `.data()`
        # で一括取得する。このフェイクは常に同じ cid 付き行を返すため（`c`/`era` キーは無い）、
        # `check_schema_era` は形が合わず早期 return する（ゲート不発動・既存の cid 除去検証には無関係）。
        return [dict(r) for r in self._rows]


class _CidFakeSession:
    """`lens_service.run_troubleshoot` が呼ぶクエリに cid 付きの1行を返す最小スタブ
    （`tests/unit/test_lens_service.py::_FakeSession` と同じ最小主義・実 Neo4j 不要）。"""

    def run(self, query, **params):
        return _CidFakeResult([{
            "cid": "module:v1:04_運用/order.cob#ORDER-MAIN", "name": "ORDER-MAIN", "label": "Module",
            "em": "static", "status": "active", "path_names": ["ROOT", "ORDER-MAIN"],
            "edges": [{"type": "USES", "doc": "order.md"}], "dist": 1,
        }])


def test_troubleshoot_answer_round_trip_has_no_cid_in_saved_read_or_shared_data(monkeypatch):
    """非 agentic 会話保存 → 取得 → sanitized 共有／JSON 書き出し用 `data` まで、実データで `cid`
    が一切残らないことを固定する——単発の dict を直接 `_safe_share_answer` に渡すテストと違い、
    cid 付きの Neo4j 行から出発する（`lens_service.run_troubleshoot`（公開・cid 除去）を実際に
    通した後の `data.candidates` を DB へ保存・取得・共有まで通す end-to-end）。"""
    from sherpa import lens_service

    _try_init()
    monkeypatch.setattr(lens_service, "grep_search", lambda *a, **k: [])
    result = lens_service.run_troubleshoot(_CidFakeSession(), "ORDER-MAIN で ABEND", "v1")
    assert result["candidates"] and "cid" not in result["candidates"][0]   # 発生源では既に無い

    sfx = _sfx()
    owner = f"unit-shares-owner-cid-{sfx}"
    cid_conv = _mk_conv(owner, title="ORDER-MAIN で ABEND")
    store.add_message(cid_conv, "user", content="ORDER-MAIN で ABEND")
    store.add_message(cid_conv, "assistant", content="原因候補 1件。",
                      answer={"headline": "原因候補 1件。", "lens": "troubleshoot",
                              "sources": [], "data": result})

    # 非 agentic 会話保存 → 取得（所有者本人の通常表示・JSON 書き出しが読む形と同じ）。
    read = store.get_conversation_for_read(owner, cid_conv)
    saved_data = read["messages"][-1]["answer"]["data"]
    assert saved_data["candidates"] and "cid" not in saved_data["candidates"][0]
    assert json.dumps(saved_data).find('"cid"') == -1   # JSON 書き出し用テキストにも "cid" キーが無い

    # sanitized 共有（凍結スナップショット）でも同様。
    snap_cid = store.create_sanitized_snapshot(owner, cid_conv)
    assert snap_cid is not None
    snap = store.get_conversation_for_read(owner, snap_cid)
    shared_data = snap["messages"][-1]["answer"]["data"]
    assert shared_data["candidates"] and "cid" not in shared_data["candidates"][0]
