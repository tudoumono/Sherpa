"""sherpa/store/shares.py の契約テスト。

会話共有・sanitized snapshot の境界ロジックを検証する:
  - 純関数（DB 非依存）: `_safe_share_answer`（sanitized 共有の allowlist 再構築）・
    `_strip_shared_message`（受領共有の denylist）・Evidence/locator/claim の allowlist。
  - 実 DB の round-trip: 所有者読み／受領共有の active・revoked・personal_blocked、create_share・
    resolve_share_by_token・is_invited・accept_share（冪等）・revoke_share、
    create_sanitized_snapshot（personal・旧マーカー双方の taint・2nd pass・非個人ターンの allowlist）。
"""
from __future__ import annotations

import json
import time

import pytest

from sherpa import store

IMP = "_重要度.txt"


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _msg(answer, **over):
    return {"id": 1, "role": "assistant", "content": "c", "lens": "qa", "route": {"x": 1},
            "trace": {"y": 1}, "answer": answer, **over}


def _via_safe(answer):
    return store._safe_share_answer(answer)


def _via_strip(answer):
    return store._strip_shared_message(_msg(answer))["answer"]


# ===== 純関数: _safe_share_answer =====

def test_safe_share_answer_clarify_and_non_dict():
    out = store._safe_share_answer({"lens": "clarify", "question": {"type": "question"}, "extra": "leak"})
    assert out == {"lens": "clarify"}          # question・extra は持ち込まない
    assert store._safe_share_answer(None) is None
    assert store._safe_share_answer("not-a-dict") is None


def test_safe_share_answer_sources_verified_is_string_list_intersected_with_surviving_sources():
    """sources_verified は文字列の list だけ（昇順）・sources に残った doc_id との交差のみ
    （他フィルタで sources から消えた doc_id を「精読済み」として復活させない）。無ければキーも無い。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md"}, {"doc_id": "another.md"}],
        "sources_verified": ["real.md", 123, None, "another.md"]})
    assert out["sources_verified"] == ["another.md", "real.md"]
    out = store._safe_share_answer({"lens": "qa", "headline": "h", "sources": [{"doc_id": "real.md"}],
                                    "sources_verified": ["real.md", "dropped.md"]})
    assert out["sources_verified"] == ["real.md"]
    assert "sources_verified" not in store._safe_share_answer({"lens": "qa", "headline": "h", "sources": []})


def test_safe_share_answer_passes_scope_with_layer_wholesale():
    """scope（layer/layer_applied 含む）は個別フィールドを選ばず丸ごと通す（新キーも自動で乗る契約）。"""
    scope = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit",
             "layer": "code", "layer_applied": True}
    assert store._safe_share_answer({"lens": "qa", "headline": "h", "scope": scope})["scope"] == scope


def test_importance_fields_kept_in_both_paths_but_not_source():
    """importance/importance_reason は両経路（allowlist／denylist）で出し、importance_source は
    allowlist 経路では出さない。"""
    answer = {"lens": "qa", "headline": "h",
              "sources": [{"doc_id": "real.md", "download_url": "/x", "importance": "高",
                           "importance_reason": "契約書", "importance_source": "_重要度.txt:1行目"}]}
    s = _via_safe(answer)["sources"][0]
    assert s["importance"] == "高" and s["importance_reason"] == "契約書"
    assert "importance_source" not in s
    stripped = _via_strip(answer)["sources"][0]
    assert stripped["importance"] == "高" and stripped["importance_reason"] == "契約書"


@pytest.mark.parametrize("via", [_via_safe, _via_strip], ids=["sanitized_share", "received_share"])
def test_importance_control_file_is_excluded_from_every_shared_answer_part(via):
    """重要度設定ファイル自体は、sanitized 共有・通常の受領共有の両経路で、出典（sources/sources_verified）・
    data.citations・troubleshoot 候補（候補自身／evidence.edges／evidence.grep）・impact の
    items/presumed の evidence・Evidence Packet の source_path から落ちる（impact の要素自体は残る）。"""
    out = via({
        "lens": "qa", "headline": "h",
        "sources": [{"doc_id": "real.md"}, {"doc_id": IMP}, {"doc_id": "4期/" + IMP}],
        "sources_verified": ["real.md", IMP],
        "data": {
            "citations": [{"doc_id": "real.md", "span": [1, 2], "quote": "q", "ext": ".md"},
                          {"doc_id": IMP, "span": [1, 1], "quote": "q2", "ext": ".txt"}],
            "candidates": [
                {"name": IMP, "label": "Document", "role": "運用手順", "distance": None, "path": [],
                 "source": "grep", "evidence": {"edges": [], "grep": [{"doc_id": IMP}]}},
                {"name": "ORDER-MAIN", "label": "Module", "role": "実装", "distance": 1, "path": [],
                 "source": "both",
                 "evidence": {"edges": [{"type": "REALIZES", "doc": IMP}, {"type": "REALIZES", "doc": "real.cbl"}],
                              "grep": [{"doc_id": "real.md"}, {"doc_id": IMP}]}}],
            "items": [{"name": "ORDER-MAIN", "judgement": "sure",
                       "evidence": [{"doc": IMP, "line": 1}, {"doc": "real.md", "line": 2}]}],
            "presumed": [{"name": "BILLGEN", "evidence": [{"doc": IMP, "line": 1}]}],
            "evidence_packet": {"task_id": "main", "evidence": [
                {"evidence_id": "ev-1", "source_type": "document", "source_path": IMP}]},
        }})
    assert [s["doc_id"] for s in out["sources"]] == ["real.md"]
    assert out["sources_verified"] == ["real.md"]
    data = out["data"]
    assert [c["doc_id"] for c in data["citations"]] == ["real.md"]
    cands = data["candidates"]
    assert [c["name"] for c in cands] == ["ORDER-MAIN"]              # grep-only 候補は丸ごと落ちる
    assert cands[0]["evidence"]["edges"] == [{"type": "REALIZES", "doc": "real.cbl"}]
    assert cands[0]["evidence"]["grep"] == [{"doc_id": "real.md"}]
    assert data["items"][0]["name"] == "ORDER-MAIN"                  # 結論自体は残る
    assert data["items"][0]["evidence"] == [{"doc": "real.md", "line": 2}]
    assert data["presumed"][0]["evidence"] == []
    assert data["evidence_packet"]["evidence"][0]["source_path"] is None


def test_safe_evidence_item_excludes_importance_control_file_from_source_path_and_matched_doc_ids():
    out = store._safe_evidence_item({"evidence_id": "ev-1", "source_type": "document", "source_path": IMP,
                                     "verification_method": "span_verified"})
    assert out["source_path"] is None
    out2 = store._safe_evidence_item({"evidence_id": "ev-2", "source_type": "document", "source_path": None,
                                      "matched_doc_ids": ["real.md", IMP, "4期/" + IMP]})
    assert out2["matched_doc_ids"] == ["real.md"]


def test_strip_shared_message_keeps_answer_untouched_when_nothing_needs_filtering():
    """フィルタで何も変わらないとき answer は元のオブジェクトをそのまま返す（余計な copy をしない）。"""
    m = _msg({"lens": "qa", "headline": "h", "sources": [{"doc_id": "real.md"}], "sources_verified": ["real.md"]},
             route=None, trace=None)
    out = store._strip_shared_message(m)
    assert out["answer"] is m["answer"]
    assert out["answer"]["sources_verified"] == ["real.md"]


def test_safe_share_answer_troubleshoot_candidates_have_no_internal_cid():
    """公開経路（run_troubleshoot）が除去済みの cid（内部専用の Neo4j canonical_id）を、共有の浅いコピーが
    新たに持ち込まない。"""
    out = store._safe_share_answer({
        "lens": "troubleshoot", "headline": "h", "sources": [],
        "data": {"candidates": [{"name": "ORDER-MAIN", "label": "Module", "role": "実装", "distance": 1,
                                 "path": [], "source": "graph", "evidence": {"edges": [], "grep": []}}]}})
    assert "cid" not in out["data"]["candidates"][0]


def test_safe_evidence_packet_keeps_only_known_fields():
    """Evidence Packet は既知フィールドだけを通す（未知キー＝locator・秘匿種別等は落ちる）。非 dict は None。"""
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
    assert out["evidence"] == [{"evidence_id": "ev-1", "source_type": "document",
                                "source_path": "real.md", "source_span": [1, 2],
                                "verification_method": "span_verified"}]
    assert store._safe_evidence_packet(None) is None
    assert store._safe_evidence_packet("not-a-dict") is None


@pytest.mark.parametrize("loc, expected", [
    # 既知フィールド（page/slide/sheet/cell_range/part/object_id/bbox）だけを通し未知キーは落とす
    ({"page": 3, "slide": 2, "sheet": "Sheet1", "cell_range": "A1:B2", "part": "xl/worksheets/sheet1.xml",
      "object_id": "shape42", "bbox": [1.0, 2.5, 3, 4], "_secret": {"leak": "me"}, "extension": {"leak": "me"}},
     {"page": 3, "slide": 2, "sheet": "Sheet1", "cell_range": "A1:B2", "part": "xl/worksheets/sheet1.xml",
      "object_id": "shape42", "bbox": [1.0, 2.5, 3, 4]}),
    ({"object_id": "shape-42"}, {"object_id": "shape-42"}),   # object_id は str | int
    ({"object_id": 42}, {"object_id": 42}),
    ({"object_id": True}, None),                             # bool は対象外
    ({"object_id": -1}, None),                               # 数値は正のみ
    ({"object_id": 3.5}, None),
    ({"page": -1}, None),
    ({"page": True}, None),
    ({"page": "3"}, None),
    ({"page": 1_000_000}, None),                             # 6桁上限
    ({"page": 999_999}, {"page": 999_999}),                  # 上限ちょうどは通る
    ({"sheet": "x" * 41}, None),                             # sheet/cell_range は40字上限
    ({"part": "x" * 201}, None),                             # part は200字上限
    ({"bbox": [1, 2, 3]}, None),                             # 要素数が4でない
    ({"bbox": ["a", "b", "c", "d"]}, None),
    ({"bbox": [float("nan"), 1, 2, 3]}, None),
    ({"bbox": [float("inf"), 1, 2, 3]}, None),
    ({"bbox": [10**7, 1, 2, 3]}, None),                      # 上限超の巨大値
    ({"page": 3, "bbox": [1, 2, 3, 10**10000]}, {"page": 3}),   # 巨大 int でも OverflowError にならず bbox だけ落ちる
    ({"bbox": [10**10000, 1, 2, 3]}, None),
    ({"sheet": "line1\nline2"}, {"sheet": "line1 line2"}),     # 改行類は単一空白へ正規化（値ごと落とさない）
    ({"sheet": "line1\rline2"}, {"sheet": "line1 line2"}),
    ({"sheet": "line1 line2"}, {"sheet": "line1 line2"}),
    ({"sheet": "line1 line2"}, {"sheet": "line1 line2"}),
    ({"page": 3, "sheet": "line1\nline2"}, {"page": 3, "sheet": "line1 line2"}),
    (None, None), ("not-a-dict", None), ({}, None), ({"unknown": "field"}, None),
])
def test_safe_locator_validates_each_field(loc, expected):
    assert store._safe_locator(loc) == expected


def test_safe_evidence_item_source_span():
    """構造化 locator の source_span は _safe_locator を通して保持（未知の入れ子秘密キーは落ちる）。
    既知フィールドが1つも無ければキーごと落とす。"""
    out = store._safe_evidence_item({
        "evidence_id": "ev-1", "source_type": "document", "source_path": "報告書.pptx",
        "source_span": {"slide": 4, "_secret": "leak"}, "verification_method": "exists_no_span"})
    assert out["source_span"] == {"slide": 4}
    assert out["source_path"] == "報告書.pptx"
    assert "source_span" not in store._safe_evidence_item(
        {"evidence_id": "ev-1", "source_span": {"totally": "unknown"}})


def test_safe_evidence_item_used_passes_only_strict_bool():
    assert store._safe_evidence_item({"evidence_id": "ev-1", "used": True})["used"] is True
    assert store._safe_evidence_item({"evidence_id": "ev-1", "used": False})["used"] is False
    for bad in (1, 0, "true", "false", None, [], {}):
        assert "used" not in store._safe_evidence_item({"evidence_id": "ev-1", "used": bad}), f"bad={bad!r}"
    assert "used" not in store._safe_evidence_item({"evidence_id": "ev-1"})


def test_safe_evidence_item_keeps_matched_doc_ids_list_meta_and_card_meta():
    """集計・カード単位 Evidence（source_path=None）は matched_doc_ids に加え list_meta/card_meta も保持する
    （無いと条件の異なる list_docs や path/category の異なる graph カードが共有先で見分けられない）。"""
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


def test_safe_evidence_item_drops_malformed_matched_doc_ids_and_meta():
    out = store._safe_evidence_item({"evidence_id": "ev-1", "matched_doc_ids": ["a", 1, None]})
    assert "matched_doc_ids" not in out and "list_meta" not in out and "card_meta" not in out
    out = store._safe_evidence_item({
        "evidence_id": "ev-1", "matched_doc_ids": ["a.md"],
        "list_meta": {"count": "not-an-int", "shown": 2, "_secret": "leak"},
        "card_meta": {"name": "X", "path": [1, 2], "_secret": "leak"}})
    assert out["list_meta"] == {"shown": 2}      # count は型不正で落ち shown は残る
    assert out["card_meta"] == {"name": "X"}     # path は要素が str でないため落ち name は残る
    out = store._safe_evidence_item({"evidence_id": "ev-1", "source_path": "a.md", "list_meta": {"count": 1}})
    assert "matched_doc_ids" not in out and "list_meta" not in out and "card_meta" not in out


def test_safe_share_answer_data_evidence_packet_allowlist_and_used_round_trip():
    """data は KB 由来として丸ごと通すが evidence_packet だけは allowlist 再構築（未知キーは落ち、
    evidence[].used は True/False をそのまま保持・型不正は落ちる）。"""
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h", "sources": [],
        "data": {"citations": [{"doc_id": "real.md"}],
                 "evidence_packet": {"task_id": "main", "investigation_status": "sufficient", "_secret": "leak",
                                     "evidence": [{"evidence_id": "ev-1", "source_path": "a.md", "used": True},
                                                  {"evidence_id": "ev-2", "source_path": "b.md", "used": False},
                                                  {"evidence_id": "ev-3", "source_path": "c.md", "used": "yes"}]}}})
    assert out["data"]["citations"] == [{"doc_id": "real.md"}]      # 他の data フィールドは無改修
    packet = out["data"]["evidence_packet"]
    assert "_secret" not in packet and packet["task_id"] == "main"
    ev = packet["evidence"]
    assert ev[0]["used"] is True and ev[1]["used"] is False
    assert "used" not in ev[2]
    no_ev = store._safe_share_answer({"lens": "qa", "headline": "h",
                                      "data": {"evidence_packet": {"task_id": "main"}}})
    assert "evidence" not in no_ev["data"]["evidence_packet"]


# ===== 純関数: _strip_shared_message =====

def test_strip_shared_message_drops_internal_keys_keeps_the_rest_and_passes_non_dict_answer():
    """route/trace・answer 内の question/usage/usage_sub（profile ごと）/usage_subs/codex_usage_total は
    受領共有の読者に見せない。非内部キー・scope（layer 含む）はそのまま届く。"""
    scope = {"world": "w1", "scope_paths": [], "source": "all", "layer": "docs", "layer_applied": True}
    out = store._strip_shared_message({
        "id": 1, "role": "assistant", "content": "answer text",
        "route": {"tool": "grep salary.xlsx"}, "trace": {"q": "900万"},
        "answer": {"headline": "h", "question": {"type": "question"}, "usage": {"tokens": 10},
                   "usage_sub": {"provider": "ollama", "model": "qwen2.5", "input_tokens": 5,
                                 "profile": "local-worker"},
                   "usage_subs": [{"provider": "ollama", "profile": "worker1"},
                                  {"provider": "ollama", "profile": "worker2"}],
                   "codex_usage_total": {"session_id": "sid-1", "input_tokens": 30},
                   "scope": scope, "keep": "ok"}})
    a = out["answer"]
    assert out["route"] is None and out["trace"] is None
    for k in ("question", "usage", "usage_sub", "usage_subs", "codex_usage_total"):
        assert k not in a
    for leaked in ("profile", "local-worker", "worker1", "worker2", "sid-1"):
        assert leaked not in str(a)
    assert a["keep"] == "ok" and a["scope"] == scope
    out = store._strip_shared_message({"id": 2, "role": "user", "content": "q", "route": None, "trace": None,
                                       "answer": None})   # answer が dict でなければそのまま通す
    assert out["answer"] is None and out["route"] is None and out["trace"] is None


# ===== claims（主張の区分・理由コード・evidence_kinds が共有でも消えない） =====

def test_safe_share_answer_preserves_claim_status_and_reason_code():
    out = store._safe_share_answer({
        "lens": "qa", "headline": "h", "sources": [],
        "data": {"claims": [
            {"id": "c1", "status": "confirmed", "text": "標準税率は10%。",
             "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""},
            {"id": "c2", "status": "unknown", "text": "適用開始日は不明。",
             "evidence_refs": [], "reason": "", "reason_code": "unexplored"}]}})
    claims = out["data"]["claims"]
    assert len(claims) == 2
    assert next(c for c in claims if c["status"] == "unknown")["reason_code"] == "unexplored"
    assert next(c for c in claims if c["status"] == "confirmed")["evidence_refs"] == ["ev-1"]


def test_strip_shared_message_preserves_claim_status_and_reason_code():
    m = {"role": "assistant", "content": "c", "lens": "qa",
         "answer": {"lens": "qa", "headline": "h", "sources": [],
                    "data": {"claims": [{"id": "c1", "status": "unknown", "text": "t",
                                         "evidence_refs": [], "reason": "", "reason_code": "conflict"}]}}}
    assert store._strip_shared_message(m)["answer"]["data"]["claims"] == [
        {"id": "c1", "status": "unknown", "text": "t", "evidence_refs": [], "reason": "", "reason_code": "conflict"}]


def test_safe_claim_allowlist():
    from sherpa.store.shares import _safe_claim
    assert _safe_claim({"id": 123, "status": "confirmed", "text": "t"}) is None   # 型不正の id は行ごと落とす
    assert _safe_claim("not a dict") is None
    assert _safe_claim({"status": "confirmed", "text": "t"}) is None              # id 欠落
    base = {"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}
    assert _safe_claim({**base, "evidence_kinds": ["source", "callgraph"]})["evidence_kinds"] == ["source", "callgraph"]
    assert "evidence_kinds" not in _safe_claim({**base, "evidence_kinds": None})       # 未申告（旧6キー形）
    assert "evidence_kinds" not in _safe_claim({**base, "evidence_kinds": "source"})   # 型不正


def test_share_card_meta_allowlist_keeps_directed_edges():
    from sherpa.store.shares import _safe_share_card_meta
    out = _safe_share_card_meta({"name": "B", "path": ["A", "B"], "edges": ["A →COPIES→ B（未確認）"], "cid": "m:B"})
    assert out == {"name": "B", "path": ["A", "B"], "edges": ["A →COPIES→ B（未確認）"]}


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
    """create_share → resolve_share_by_token(active) → is_invited → accept_share(冪等) → 受領側の読み
    （内部情報は伏せる）→ revoke_share → resolve（active=False）・読めなくなる、を1本で確認する。"""
    _try_init()
    sfx = _sfx()
    owner, invitee, outsider = (f"unit-shares-{k}-{sfx}" for k in ("owner2", "invitee", "outsider"))
    cid = _mk_conv(owner, title="shared conv")
    store.add_message(cid, "user", content="q1")
    store.add_message(cid, "assistant", content="a1", route={"tool": "grep x"}, trace={"q": "x"},
                      answer={"headline": "h", "question": {"type": "question"}, "usage": {"tokens": 1}})

    th = f"unit-token-hash-{sfx}"
    sid = store.create_share(cid, owner, th, None, [invitee])   # expires_at=None=無期限

    share = store.resolve_share_by_token(th)
    assert share is not None and share["active"] is True and share["conversation_id"] == cid
    assert store.is_invited(sid, invitee) is True
    assert store.is_invited(sid, outsider) is False

    wid1 = store.accept_share(sid, invitee)
    assert store.accept_share(sid, invitee) == wid1             # 二重クリックは同じ wrapper（冪等）

    wrapper = store.get_conversation_for_read(invitee, wid1)
    assert wrapper is not None and len(wrapper["messages"]) == 2   # 元会話の本文をそのまま参照
    a1 = next(m for m in wrapper["messages"] if m["role"] == "assistant")
    assert a1["route"] is None and a1["trace"] is None
    assert "question" not in a1["answer"] and "usage" not in a1["answer"]
    assert a1["answer"]["headline"] == "h"

    assert store.revoke_share(sid, outsider) is False           # 所有者以外は不可
    assert store.revoke_share(sid, owner) is True
    assert store.revoke_share(sid, owner) is False               # 二重取消は不成立
    assert store.resolve_share_by_token(th)["active"] is False

    after = store.get_conversation_for_read(invitee, wid1)
    assert after["share_status"] == "unavailable" and after["messages"] == []


def test_get_conversation_for_read_personal_blocked_after_share_created():
    """共有後に元会話へ個人 workspace 参照フラグが立った場合、受領側は読めない。"""
    _try_init()
    sfx = _sfx()
    owner, invitee = f"unit-shares-owner3-{sfx}", f"unit-shares-invitee3-{sfx}"
    cid = _mk_conv(owner)
    store.add_message(cid, "user", content="q")
    sid = store.create_share(cid, owner, f"unit-token-hash-pb-{sfx}", None, [invitee])
    wid = store.accept_share(sid, invitee)

    store.set_contains_personal_workspace(cid)                 # 共有後に個人参照が追加された
    result = store.get_conversation_for_read(invitee, wid)
    assert result["share_status"] == "personal_blocked" and result["messages"] == []


def test_missing_share_or_source_conversation():
    _try_init()
    with pytest.raises(ValueError):
        store.accept_share(-999, "someone")
    assert store.create_sanitized_snapshot("someone", -999) is None


def test_create_sanitized_snapshot_redacts_personal_turns_and_keeps_kb_turns():
    """personal=True・旧 answer マーカー（personal_sources 等）双方の taint 判定＋2nd pass
    （直後の assistant が taint なら直前の user も伏字）＋非個人ターンの allowlist 再構築を1本で確認する。"""
    _try_init()
    owner = f"unit-shares-owner5-{_sfx()}"
    cid = _mk_conv(owner, title="my_salary.xlsx を要約して")

    # (1) 個人ターン: personal=True（新フラグ）。
    store.add_message(cid, "user", content="my_salary.xlsx を要約して", personal=True)
    store.add_message(cid, "assistant", content="年収900万です",
                      answer={"headline": "年収900万です", "personal_sources": [{"doc_id": "my_salary.xlsx"}]},
                      personal=True)
    # (2) 旧データ互換: personal フラグ未設定だが answer に旧マーカー。直前の user 質問も伏字化される。
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
    assert msgs[0]["content"] == store._REDACTED_TEXT
    assert msgs[1]["content"] == store._REDACTED_TEXT and msgs[1]["answer"] == {"headline": store._REDACTED_TEXT}
    assert msgs[2]["content"] == store._REDACTED_TEXT          # 2nd pass で「別の個人依頼」も伏字化
    assert msgs[3]["content"] == store._REDACTED_TEXT
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
        # check_schema_era は .data() で一括取得する。常に同じ cid 付き行を返すため形が合わず早期 return する。
        return [dict(r) for r in self._rows]


class _CidFakeSession:
    """run_troubleshoot が呼ぶクエリに cid 付きの1行を返す最小スタブ（実 Neo4j 不要）。"""

    def run(self, query, **params):
        return _CidFakeResult([{
            "cid": "module:v1:04_運用/order.cob#ORDER-MAIN", "name": "ORDER-MAIN", "label": "Module",
            "status": "active", "path_names": ["ROOT", "ORDER-MAIN"],
            "edges": [{"type": "USES", "doc": "order.md"}], "dist": 1,
        }])


def test_troubleshoot_answer_round_trip_has_no_cid_in_saved_read_or_shared_data(monkeypatch):
    """cid 付きの Neo4j 行から出発し、run_troubleshoot（cid 除去）→ DB 保存 → 取得 → JSON 書き出し用 data →
    sanitized 共有まで通して cid が一切残らない（end-to-end）。"""
    from sherpa import lens_service

    _try_init()
    monkeypatch.setattr(lens_service, "grep_search", lambda *a, **k: [])
    result = lens_service.run_troubleshoot(_CidFakeSession(), "ORDER-MAIN で ABEND", "v1")
    assert result["candidates"] and "cid" not in result["candidates"][0]   # 発生源では既に無い

    owner = f"unit-shares-owner-cid-{_sfx()}"
    cid_conv = _mk_conv(owner, title="ORDER-MAIN で ABEND")
    store.add_message(cid_conv, "user", content="ORDER-MAIN で ABEND")
    store.add_message(cid_conv, "assistant", content="原因候補 1件。",
                      answer={"headline": "原因候補 1件。", "lens": "troubleshoot", "sources": [], "data": result})

    saved_data = store.get_conversation_for_read(owner, cid_conv)["messages"][-1]["answer"]["data"]
    assert saved_data["candidates"] and "cid" not in saved_data["candidates"][0]
    assert json.dumps(saved_data).find('"cid"') == -1

    snap_cid = store.create_sanitized_snapshot(owner, cid_conv)
    assert snap_cid is not None
    shared_data = store.get_conversation_for_read(owner, snap_cid)["messages"][-1]["answer"]["data"]
    assert shared_data["candidates"] and "cid" not in shared_data["candidates"][0]
