"""chat_service の単体テスト。

- _cap_trace_v2: EXT-1（Execution Event v2・`docs/proposals/2026-08-22-拡張設計.md` §2）。
  TOGGLE-RM（2026-09-03）で v1（旧 `_cap_trace`・トグル `SHERPA_EXEC_EVENT_V2`）を撤去し常時
  v2 固定＝`answer.trace_version` は常に2が付く。二段上限（RV是正・needs-work全6件採用）:
  ①ソフト上限（親は必ず残し末端だけ `(parent_id, kind, agent_run_id)` 単位で集約ノードへ畳む・
  集約ノード自体も予算に数える）→②ハード上限（件数・バイト双方。ソフト対応後もなお超過なら
  最も古いサブツリー単位で丸ごと畳む・orphan は構造的に発生しない）→③それでも超過なら
  `budget_limit_reached` マーカーで honest failure、の順に決定的に適用することを固定する。
  集約ノードの evidence_ids は上限K件＋omitted_evidence_count。集約IDは null 明示区別の正規JSON
  を sha1 の全40桁（旧・文字列連結プレースホルダの衝突バグ是正／旧12桁切り詰めの是正）。
  不正 parent_id は親なしへ正規化して**出力ノード自身の parent_id も書き換える**（件数に関わらず
  必ず実行・高速経路でも素通りしない）。`_assert_no_orphans`（既定で orphan を検出・
  `budget_limit_reached` の内容スニッフィングによる自動免除は廃止＝呼び出し側が明示する
  `allow_truncated=True` でのみ免除）／`_assert_dict_ids_match_keys` を全 cap テストへ適用する。
  `_trace_bytes` は実保存（psycopg `Json` 既定＝`ensure_ascii=True`）と同じ測り方（旧 SSE 側基準の
  過小測定を是正）。honest failure（`_budget_limit_truncate`）は件数を合わせた後もバイト上限を
  満たすまで保持ノードを追加削減する収束ループを持つ。`build_event` は集約/マーカー専用の予約
  id（`trace-omitted:`/`trace-subtree:`/`trace-budget-limit-reached`）を拒否し、`_build_reserved_event`
  （集約/マーカー生成専用の内部関数）はその逆を強制する。
  3保存サイト（handle_message／stream_message の answer・clarify）は store をフェイク差し替えして
  PG 不要で検証（`test_*_mock_store_*`）。`handle_message` 経由の実 DB end-to-end は補助として残す
  （DB down は skip）。
- _history_pairs / _clip_history_msg: R1a（会話継続・履歴 priming）。完全対のみ抽出・N対＋文字予算の
  二重キャップ・メッセージ単体切り詰め（要 Postgres・DB down は skip）。
- 確認ID 回帰: 履歴に確認ID マーカーを含む過去ターンがあっても、現在ターンの判定（_can_ask/
  chat_router._resume_lens）は message（別チャネル）だけを見るため影響を受けない（PG/Neo4j 不要）。
- _degrade_overload/_impact_overload_result: secRV 範囲外是正（2026-07-19・影響分析の Neo4j 安全弁）。
  impact レンズが `GraphQueryOverloadError` で失敗した際、LLM 合成を経由させず固定文言の `_result`
  へ差し替えることを純関数（DB/Neo4j 不要・フェイク provider ジェネレータ）で固定する。
- _known_terms: secRV 範囲外是正 追補（2026-07-19・RV指摘 HIGH-2）。以前は `.data()` で無制限に
  全件展開しており、knowledge=true の全チャット（impact/troubleshoot/qa）が安全弁を迂回していた。
  `lens_service._run_capped` 経由になったことで、timeout→空リスト／天井到達→部分リストへソフト
  縮退し、例外を出さないこと・返却形（name 文字列のリスト）が不変であることを固定する。
"""
from __future__ import annotations

import logging
import threading

import pytest

from sherpa import chat_service as CS
from sherpa import exec_event as EE
from sherpa import store


def _try_init():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _new_conv():
    _try_init()
    return store.create_conversation(user_id="admin", world="v1", title="history test")["id"]


def _node(i, detail="d"):
    return {"type": "node", "id": f"n{i}", "kind": "tool", "label": f"label{i}",
            "detail": detail, "status": "done"}


def test_sources_excludes_importance_control_file():
    """`_重要度.txt`（文書の重要度設定ファイル自体）は回答出典に出さない
    （§5・独立入口として `chat_service._sources` 自身も判定する）。"""
    out = CS._sources(["a.md", "_重要度.txt", "4期/_重要度.txt"], "v1")
    doc_ids = {s["doc_id"] for s in out}
    assert doc_ids == {"a.md"}


def test_sources_attaches_importance_when_resolved(monkeypatch):
    """I2（2026-09-05）: world root が解決でき、`resolve_many` が値を返せば出典へ
    `importance`/`importance_reason` を条件付きで足す。`importance_source` は出さない（J4）。
    解決が無い doc はキー自体を持たない（§2 truth table）。"""
    from sherpa.ingest import importance as imp
    monkeypatch.setattr(CS.worlds, "world_dir", lambda w: "/tmp/x")
    res = imp.Resolution(value="高", reason="契約書", config_path="_重要度.txt", rule_line=1)
    monkeypatch.setattr(CS.importance, "resolve_many", lambda w, rels, root=None, sig=None: {"a.md": res})
    out = {s["doc_id"]: s for s in CS._sources(["a.md", "b.md"], "v1")}
    assert out["a.md"]["importance"] == "高" and out["a.md"]["importance_reason"] == "契約書"
    assert "importance_source" not in out["a.md"]
    assert "importance" not in out["b.md"]


def test_sources_unregistered_world_skips_resolve_call_and_stays_two_keys(monkeypatch):
    """未登録 world（`worlds.world_dir` が None）は `resolve_many` 自体を呼ばない——出典は
    従来どおり2キー（`doc_id`/`download_url`）のまま（受け入れ条件＝重要度制御ファイルの無い
    world で出典の出力完全不変）。"""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(CS.worlds, "world_dir", lambda w: None)
    monkeypatch.setattr(CS.importance, "resolve_many", _boom)
    out = CS._sources(["a.md"], "v1")
    assert called["n"] == 0
    assert set(out[0]) == {"doc_id", "download_url"}


# ---- EXT-1: _cap_trace_v2 の二段上限（純関数・DB 不要） ----
# RV是正（needs-work・指摘6件・全件採用）: ①ソフト上限だけでは実効上限にならない例（保護対象＝
# 「他ノードの親」自体が多い／全末端が別 agent_run_id で集約が効かない）があるため、ソフト
# （_MAX_TRACE_NODES）→ハード（_MAX_TRACE_NODES_HARD・件数とバイト）→honest failure マーカーの
# 三段構成に精緻化。②集約ノードの evidence_ids は上限K件＋omitted_evidence_count。③集約IDは
# (parent_id, kind, agent_run_id) を null 明示区別の正規 JSON にして sha1（文字列連結プレースホルダ
# 'root'/'main' は agent_run_id='main' 等の実値と衝突しうるため廃止）。④不正 parent_id は
# 親なしへ正規化（クラッシュさせない）。⑤⑥は下記ヘルパ・テスト側の是正。

def _assert_dict_ids_match_keys(nodes: dict) -> None:
    """fixture 構築ミス検出用（実際に一度踏んだ間違い＝dict のキーとノード自身の id がずれる）。
    dict のキーは必ずそのノード自身の `id` と一致すること。"""
    for k, v in nodes.items():
        assert v["id"] == k, f"dict key {k!r} != node id {v['id']!r}"


def _assert_no_orphans(trace: list, *, allow_truncated: bool = False) -> None:
    """trace 内の各ノードについて、`parent_id` が非 null ならその親も同じ trace 内に存在すること
    （全 cap テストへ適用する汎用ヘルパ）。

    RV是正（needs-work再検証・点(f)）: 以前は `budget_limit_reached`（honest failure・
    `_budget_limit_truncate`）型のノードが trace 内に1件でもあれば検査全体を自動 skip していたが、
    これは「内容を見て検査を緩める」内容スニッフィングであり、truncate 経路以外に紛れ込んだ
    orphan バグを隠しうる。呼び出し側が truncate 経路を検証しているテストだけ、明示的に
    `allow_truncated=True` を渡して免除する（既定は検査を必ず行う）。
    """
    if allow_truncated:
        return
    ids = {n["id"] for n in trace}
    for n in trace:
        pid = n.get("parent_id")
        assert pid is None or pid in ids, f"orphan: {n['id']!r} が存在しない親 {pid!r} を参照"


def _v2_node(i, *, parent_id=None, kind="tool", agent_run_id=None, evidence_ids=None):
    return EE.build_event(f"n{i}", kind, f"label{i}", "d", "done",
                          parent_id=parent_id, agent_run_id=agent_run_id, evidence_ids=evidence_ids)


def test_trace_bytes_matches_real_storage_serialization_not_sse():
    """RV是正（needs-work再検証・点(a)）: `_trace_bytes` は実保存（psycopg の `Json` 既定＝
    `ensure_ascii` 未指定＝True）と同じ測り方でなければならない。日本語主体の detail/label では
    `ensure_ascii=True` の方が `ensure_ascii=False` より大きくなる（非ASCIIがエスケープ列になるため）
    ことも合わせて固定する（過小測定だとバイト上限が実際の保存サイズを守れない）。"""
    import json as _json
    node = EE.build_event("n1", "tool", "検索テスト", "詳細な日本語のテキストです", "done")
    expected = len(_json.dumps([node], ensure_ascii=True, default=str).encode("utf-8"))
    smaller_if_wrong = len(_json.dumps([node], ensure_ascii=False, default=str).encode("utf-8"))
    assert CS._trace_bytes([node]) == expected
    assert expected > smaller_if_wrong                                 # 日本語は ensure_ascii=True の方が大きい


def test_cap_trace_v2_empty_is_none():
    assert CS._cap_trace_v2({}) is None


def test_cap_trace_v2_under_limit_passthrough_order_preserved():
    nodes = {f"n{i}": _v2_node(i) for i in range(5)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert [n["id"] for n in out] == [f"n{i}" for i in range(5)]
    _assert_no_orphans(out)


def test_cap_trace_v2_truncates_long_detail():
    nodes = {"n1": {**_v2_node(1), "detail": "x" * 500}}
    out = CS._cap_trace_v2(nodes)
    assert len(out[0]["detail"]) == CS._MAX_TRACE_DETAIL_CHARS


def test_cap_trace_v2_soft_cap_reserves_budget_for_the_aggregate_itself():
    """RV是正①: 集約ノード自体もソフト上限（120）の予算に数える＝v1 のような
    「120件＋要約1件＝121件」ではなく、合計がちょうど120件に収まる（実測: 30件超過の入力で
    集約1件＋古い方1件分を追加で畳んだ計31件省略になる）。
    """
    n = CS._MAX_TRACE_NODES + 30
    nodes = {f"n{i}": _v2_node(i, kind="tool") for i in range(n)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES                            # 集約ノードも込みでちょうど上限
    summary = out[0]
    assert summary["id"].startswith("trace-omitted:")
    assert summary["kind"] == "tool"                                  # 集約元と同じ kind（v1 は固定 think）
    assert summary["metrics"]["omitted_count"] == 31
    kept_ids = [x["id"] for x in out[1:]]
    assert kept_ids == [f"n{i}" for i in range(31, n)]                # 末尾（最新）優先
    _assert_no_orphans(out)


def test_cap_trace_v2_soft_cap_mixed_kind_aggregates_per_group():
    """dropped 対象が think/tool 混在なら、集約ノードも kind ごとに分かれる（集約2件分も予算に数える
    ため、単一 kind のケースより1件多く古い方が dropped 対象になる＝実測 think6/tool6）。
    """
    n = CS._MAX_TRACE_NODES + 10
    nodes = {}
    for i in range(n):
        kind = "think" if i % 2 == 0 else "tool"
        nodes[f"n{i}"] = _v2_node(i, kind=kind)
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    summaries = [x for x in out if x["id"].startswith("trace-omitted:")]
    assert len(summaries) == 2
    by_kind = {s["kind"]: s for s in summaries}
    assert by_kind["think"]["metrics"]["omitted_count"] == 6
    assert by_kind["tool"]["metrics"]["omitted_count"] == 6
    assert len(out) == CS._MAX_TRACE_NODES                            # 集約2件込みでちょうど上限
    _assert_no_orphans(out)


def test_cap_trace_v2_evidence_ids_capped_with_omitted_count():
    """RV是正②: 完全な和集合ではなく上限K件（既定20）。K 未満なら omitted_evidence_count は立たない。"""
    n = CS._MAX_TRACE_NODES + 3                                       # 実測 dropped=4（集約自体の分含む）
    nodes = {f"n{i}": _v2_node(i, evidence_ids=[f"ev-{i:03d}"]) for i in range(n)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    summary = next(x for x in out if x["id"].startswith("trace-omitted:"))
    assert summary["metrics"]["omitted_count"] == 4
    assert len(summary["evidence_ids"]) == 4 <= CS._MAX_TRACE_AGGREGATE_EVIDENCE_IDS
    assert "omitted_evidence_count" not in summary["metrics"]
    _assert_no_orphans(out)


def test_cap_trace_v2_evidence_ids_over_k_sets_omitted_evidence_count():
    """dropped 件数が K（20）を超える場合、evidence_ids は K 件で切られ omitted_evidence_count が立つ
    （実測: dropped=26件・evidence20件保持・6件を omitted_evidence_count で計数）。
    """
    n = CS._MAX_TRACE_NODES + 25
    nodes = {f"n{i}": _v2_node(i, evidence_ids=[f"ev-{i:03d}"]) for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    summary = next(x for x in out if x["id"].startswith("trace-omitted:"))
    assert summary["metrics"]["omitted_count"] == 26
    assert len(summary["evidence_ids"]) == CS._MAX_TRACE_AGGREGATE_EVIDENCE_IDS
    assert summary["metrics"]["omitted_evidence_count"] == 6
    _assert_no_orphans(out)


def test_cap_trace_v2_soft_cap_keeps_all_parents_up_to_hard_cap():
    """親（他ノードの `parent_id` になっているノード）は、ソフト上限（120）を超えていてもハード上限
    （400）以内なら全件残る（追加裁定「親は子が残る限り削除しない」の実効版）。末端（子）側だけが
    集約ノードへ畳まれる（親子リンクは切れない＝集約ノードが子側の代わりに親を指し続ける）。
    """
    n_parents = CS._MAX_TRACE_NODES + 5                                # 125（親だけでソフト上限は超えるがハード上限400未満）
    nodes = {}
    for i in range(n_parents):
        pid = f"p{i}"
        nodes[pid] = EE.build_event(pid, "agent", f"parent{i}", "d", "done")
        nodes[f"c{i}"] = EE.build_event(f"c{i}", "tool", f"child{i}", "d", "done", parent_id=pid)
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    out_ids = {x["id"] for x in out}
    for i in range(n_parents):
        assert f"p{i}" in out_ids                                      # 親は全件生き残る
    summaries = [x for x in out if x["id"].startswith("trace-omitted:")]
    assert len(summaries) == n_parents                                 # 子は親ごとに別集約（agent_run_id等が違えば別グループ）
    assert {s["parent_id"] for s in summaries} == {f"p{i}" for i in range(n_parents)}
    assert len(out) == n_parents + len(summaries)                      # 250（ハード上限400未満なので②は発動しない）
    _assert_no_orphans(out)


def test_cap_trace_v2_hard_cap_collapses_oldest_subtrees_no_orphans():
    """RV是正①(二段目): ソフト上限だけでは削減できないケース（親だけで大量）がハード上限を超えたら、
    最も古いサブツリーから丸ごと1個の集約ノードへ畳む（新しいものを優先保持）。実測: 250組の
    親子ペア（計500件）→ 500-400=100 ペア分を畳んで丁度ハード上限（400）に収まり、
    最も古い（インデックスの小さい）親から畳まれ、新しい親が残る。
    """
    n_pairs = 250
    nodes = {}
    for i in range(n_pairs):
        pid = f"p{i}"
        nodes[pid] = EE.build_event(pid, "agent", f"parent{i}", "d", "done")
        nodes[f"c{i}"] = EE.build_event(f"c{i}", "tool", f"child{i}", "d", "done", parent_id=pid)
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES_HARD                        # ちょうどハード上限に収まる
    subtree_markers = [n for n in out if n["id"].startswith("trace-subtree:")]
    assert len(subtree_markers) == 100
    assert not any(n.get("event_type") == "budget_limit_reached" for n in out)  # ③（honest failure）までは行かない
    # c0/c249（子ノード自身の id）は、ソフト上限段階で全末端が1件ずつ別集約グループになった時点で
    # 既に集約ノード（ハッシュ id）へ置き換わっている（子側の個別 id 自体はソフト段階で消える）ため、
    # 「親が生きているか」と「その親を指す子側の集約がまだ個別に残っているか」で検証する。
    out_ids = {n["id"] for n in out}
    assert "p0" not in out_ids                                         # 最古の親は畳まれる
    assert not any(n.get("parent_id") == "p0" for n in out)            # 子側の集約もサブツリーごと畳まれ個別には残らない
    assert "p249" in out_ids                                           # 最新の親は個別に残る
    assert any(n.get("parent_id") == "p249" for n in out)              # 子側の集約は畳まれず個別に残る
    _assert_no_orphans(out)


def test_cap_trace_v2_byte_cap_collapses_even_singleton_subtrees():
    """RV是正②(バイト上限): ノード数は少なくても metrics 等が肥大化してシリアライズ後バイト数が
    上限を超えるケースは、件数だけなら畳んでも無意味な単独サブツリーでもバイト削減のために畳む。
    """
    nodes = {}
    for i in range(5):
        n = EE.build_event(f"b{i}", "tool", f"label{i}", "d", "done")
        n["metrics"] = {"blob": "x" * 300_000}                         # 1件あたり約300KB
        nodes[f"b{i}"] = n
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    assert len(out) == 5                                                # 件数は減らない（畳んでも1対1）
    collapsed = [n for n in out if n["id"].startswith("trace-subtree:")]
    assert len(collapsed) >= 1                                          # 少なくとも1件は畳まれてバイトが縮む
    _assert_no_orphans(out)


def test_cap_trace_v2_budget_limit_reached_marker_when_hard_cap_unresolvable():
    """RV是正①(honest failure): 全末端が別 agent_run_id（集約しても1件=1グループで件数が減らない）
    かつ親子関係が無い（サブツリーが全て単独＝畳んでも件数が減らない）病的ケースは、
    ②でも解決できずマーカー1件を先頭に置いて機械的に切り詰める。
    """
    n = 500
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"label{i}", "d", "done", agent_run_id=f"run-{i}")
             for i in range(n)}
    _assert_dict_ids_match_keys(nodes)
    out = CS._cap_trace_v2(nodes)
    assert len(out) == CS._MAX_TRACE_NODES_HARD                        # マーカー1件＋残りでハード上限ぴったり
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID
    assert out[0]["event_type"] == "budget_limit_reached"
    assert out[0]["metrics"]["omitted_count"] == n - (CS._MAX_TRACE_NODES_HARD - 1)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    # このケースは honest failure（③）経路そのもの＝orphan 不変条件を意図的に緩める対象だと
    # 呼び出し側が明示する（RV是正・点(f)：内容スニッフィングではなく明示引数で免除）。
    _assert_no_orphans(out, allow_truncated=True)


def test_budget_limit_truncate_converges_bytes_even_when_count_truncation_is_not_enough():
    """RV是正(a): `_budget_limit_truncate` を直接呼び、件数だけの切り詰めでは巨大ノードが残り
    バイト上限を超えうるケースでも収束することを保証する（フルパイプライン経由だと②ハード上限が
    バイト超過ノードを事前に集約ノードへ圧縮してしまい、③がバイト過多な実ノードを受け取る
    状況を再現しにくいため、③自身の契約として単体で固定する）。
    """
    nodes = {}
    for i in range(450):
        n = EE.build_event(f"n{i}", "tool", f"label{i}", "d", "done")
        if i >= 50:                                                    # 新しい400件は巨大な metrics を持つ
            n["metrics"] = {"blob": "x" * 5000}
        nodes[f"n{i}"] = n
    age = {nid: i for i, nid in enumerate(nodes)}

    # 素朴な「件数だけ」の切り詰めだとバイト上限を超えることを先に確認しておく（このテストの前提）。
    ordered = CS._order_by_age(nodes, age)
    naive_kept = ordered[-(CS._MAX_TRACE_NODES_HARD - 1):]
    naive_out = [CS._budget_limit_marker(450 - len(naive_kept), 450)] + naive_kept
    assert CS._trace_bytes(naive_out) > CS._MAX_TRACE_BYTES             # 前提: 件数だけでは収まらない

    out = CS._budget_limit_truncate(nodes, age, original_total=450)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES                  # 実際の出力はバイト上限に収束する
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID
    assert len(out) <= CS._MAX_TRACE_NODES_HARD
    assert out[0]["metrics"]["omitted_count"] == 450 - (len(out) - 1)   # マーカーの件数が実際の kept 数と整合


def test_budget_limit_truncate_converges_to_minimal_kept_set_under_extreme_bloat():
    """既定のバイト上限（100万バイト）に対して、巨大ノード（1件50KB）ばかりでも収束は必ずバイト
    上限内で止まる（極端な入力でも kept を大幅に削って収束する挙動そのものを固定する）。
    このケースは既定の上限では marker 単独までは到達しない（19件が生き残る＝実測）——
    marker 単独への到達自体は
    `test_budget_limit_truncate_falls_back_to_marker_alone_when_byte_budget_is_extremely_tight` で
    バイト上限を絞って別途固定する。
    """
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"l{i}", "d", "done", metrics={"blob": "x" * 50_000})
             for i in range(450)}
    age = {nid: i for i, nid in enumerate(nodes)}
    out = CS._budget_limit_truncate(nodes, age, original_total=450)
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID
    assert len(out) > 1                                              # marker 単独ではなく複数ノード残しで収束する


def test_budget_limit_truncate_falls_back_to_marker_alone_when_byte_budget_is_extremely_tight(monkeypatch):
    """RV是正（最終確認・残1件）: 上のテストは marker 単独まで到達していなかった（名前と実態の不一致）。
    `_MAX_TRACE_BYTES` を「marker 単独ならぎりぎり収まるが、kept を1件でも足すと必ず超える」水準まで
    monkeypatch で絞り、実際に marker 単独（`len(out) == 1`）へ到達することを検証する。

    `_budget_limit_truncate`/`_trace_bytes`/`_within_hard_limits` はいずれも `_MAX_TRACE_BYTES` を
    関数本体でモジュール属性として都度読む（デフォルト引数の値として束縛していない）ため、
    monkeypatch が効く（`docs/17-開発の教訓.md`「デフォルト引数は def 時に束縛され、monkeypatch が
    効かない」の逆＝この関数群は最初からその罠を踏んでいない設計だが、念のためここで実証する）。
    """
    nodes = {f"n{i}": EE.build_event(f"n{i}", "tool", f"l{i}", "d", "done") for i in range(450)}
    age = {nid: i for i, nid in enumerate(nodes)}

    # marker 単独（omitted=全450件）の実サイズを測り、それよりわずかに大きいだけの上限に絞る。
    marker_alone_bytes = CS._trace_bytes([CS._budget_limit_marker(450, 450)])
    monkeypatch.setattr(CS, "_MAX_TRACE_BYTES", marker_alone_bytes + 50)

    out = CS._budget_limit_truncate(nodes, age, original_total=450)
    assert len(out) == 1                                              # marker 単独まで実際に到達している
    assert out[0]["id"] == EE.BUDGET_LIMIT_REACHED_ID
    assert out[0]["metrics"]["omitted_count"] == 450                  # 保持ノードは1件も残らない
    assert CS._trace_bytes(out) <= CS._MAX_TRACE_BYTES


def test_cap_trace_v2_dangling_parent_id_normalized_not_crash(caplog):
    """RV是正④: 非 null の parent_id が現集合内に無い場合は親なしへ正規化し、クラッシュしない
    （warning ログで可視化）。"""
    n = CS._MAX_TRACE_NODES + 30
    nodes = {f"n{i}": _v2_node(i) for i in range(n)}
    nodes["n5"]["parent_id"] = "does-not-exist-in-this-set"
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = CS._cap_trace_v2(nodes)                                   # 例外を出さないこと自体が主張
    assert any("親なしへ正規化" in r.getMessage() for r in caplog.records)
    _assert_no_orphans(out)


def test_cap_trace_v2_dangling_parent_id_rewritten_in_surviving_output_node():
    """RV是正（needs-work再検証・点(b)）: 正規化は内部計算だけでなく、生き残った実ノード自身の
    `parent_id` フィールドも書き換える。dangling な親を持つノードを「保護対象」（他ノードの親）に
    仕立てて個別に生き残らせ、その出力ノード自身の `parent_id` が None になっていることを直接確認する
    （集約に紛れて検証できてしまう leaf を使わない＝旧実装のバグはこのケースでのみ再現した）。
    """
    victim = "ghost-parent-victim"
    nodes = {victim: EE.build_event(victim, "tool", "l", "d", "done", parent_id="does-not-exist"),
            "child-of-victim": EE.build_event("child-of-victim", "tool", "l2", "d", "done", parent_id=victim)}
    out = CS._cap_trace_v2(nodes)
    by_id = {n["id"]: n for n in out}
    assert by_id[victim]["parent_id"] is None                           # 内部計算だけでなく出力自身が書き換わっている
    _assert_no_orphans(out)


def test_cap_trace_v2_dangling_parent_normalized_even_under_soft_cap_fast_path():
    """RV是正（needs-work再検証・点(b)）: 120件以下（ソフト上限未満）の高速経路でも正規化は必ず通る
    （旧実装はこの経路で正規化自体を素通りしていた）。"""
    nodes = {"only-one": EE.build_event("only-one", "tool", "l", "d", "done", parent_id="ghost")}
    out = CS._cap_trace_v2(nodes)
    assert len(out) == 1
    assert out[0]["parent_id"] is None


def test_group_id_distinguishes_none_from_string_placeholders():
    """RV是正③: (parent_id, kind, agent_run_id) の None と実際の文字列値（'root'/'main' 等）が
    衝突しない（旧実装は文字列連結プレースホルダのため `(None,"tool","main")` と
    `("root","tool",None)` が同じ id になっていた＝レビュア指摘の具体例）。全40桁 sha1（RV是正・
    needs-work再検証: 実用上の単射性のため12桁への切り詰めをやめた）。"""
    g1 = CS._group_id(None, "tool", "main")
    g2 = CS._group_id("root", "tool", None)
    assert g1 != g2
    assert g1.startswith("trace-omitted:") and g2.startswith("trace-omitted:")
    assert len(g1.split(":", 1)[1]) == 40 and len(g2.split(":", 1)[1]) == 40
    # 同じ入力は同じ id（安定・決定的）。
    assert CS._group_id(None, "tool", "main") == g1


def test_subtree_id_full_40_hex():
    sid = CS._subtree_id("some-root-id")
    assert sid.startswith("trace-subtree:")
    assert len(sid.split(":", 1)[1]) == 40


def test_build_event_rejects_reserved_id_prefixes_and_exact_marker_id():
    """RV是正（needs-work再検証・点(c)）: 通常イベント（`build_event`）は集約/マーカー専用の予約
    名前空間を名乗れない（衝突事故を構造的に防ぐ）。"""
    for bad_id in ("trace-omitted:" + "0" * 40, "trace-subtree:" + "0" * 40, EE.BUDGET_LIMIT_REACHED_ID):
        with pytest.raises(ValueError):
            EE.build_event(bad_id, "tool", "l", "d", "done")


def test_build_reserved_event_rejects_non_reserved_id():
    """`_build_reserved_event`（集約/マーカー生成専用の内部関数）は `build_event` の逆＝予約名前空間
    以外の id を渡すと ValueError（呼び出し側のハッシュ化忘れ等の実装ミスを検出する）。"""
    with pytest.raises(ValueError):
        EE._build_reserved_event("not-a-reserved-id", "tool", "l", "d", "done")


def test_aggregate_and_marker_ids_are_actually_reserved():
    """集約/マーカー生成が実際に予約名前空間を使っていること自体を固定する（点(c)是正の効果が
    _cap_trace_v2 の実出力にも及んでいることの確認）。"""
    n = CS._MAX_TRACE_NODES + 30
    nodes = {f"n{i}": _v2_node(i) for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    summary_id = out[0]["id"]
    assert summary_id.startswith("trace-omitted:")
    with pytest.raises(ValueError):
        EE.build_event(summary_id, "tool", "l", "d", "done")            # 通常イベントとしては使えない


def test_assert_no_orphans_helper_actually_detects_orphans_by_default():
    """RV是正（needs-work再検証・点(f)）: `_assert_no_orphans` は既定（`allow_truncated=False`）で
    実際に orphan を検出すること自体をテストする（メタテスト＝内容スニッフィングで検査が
    無効化されていないことの直接証拠）。"""
    broken = [{"id": "a", "parent_id": "does-not-exist"}]
    with pytest.raises(AssertionError):
        _assert_no_orphans(broken)


def test_assert_no_orphans_helper_allow_truncated_explicitly_skips():
    """同じ壊れた入力でも `allow_truncated=True` を明示すれば免除される（呼び出し側の明示責任）。"""
    broken = [{"id": "a", "parent_id": "does-not-exist"}]
    _assert_no_orphans(broken, allow_truncated=True)                    # 例外を出さないこと自体が主張


def test_cap_trace_v2_is_deterministic_across_repeated_calls():
    """全段（ソフト→ハード→honest failure）とも決定的＝同じ入力なら常に同じ出力。"""
    nodes = {}
    for i in range(250):
        pid = f"p{i}"
        nodes[pid] = EE.build_event(pid, "agent", f"parent{i}", "d", "done")
        nodes[f"c{i}"] = EE.build_event(f"c{i}", "tool", f"child{i}", "d", "done", parent_id=pid)
    out1 = CS._cap_trace_v2(dict(nodes))
    out2 = CS._cap_trace_v2(dict(nodes))
    assert [n["id"] for n in out1] == [n["id"] for n in out2]


def test_cap_trace_v2_summary_node_uses_hashed_id_and_real_data():
    n = CS._MAX_TRACE_NODES + 5
    nodes = {f"n{i}": _v2_node(i, kind="tool") for i in range(n)}
    out = CS._cap_trace_v2(nodes)
    assert out[0]["kind"] == "tool"                                  # 実データ由来
    assert out[0]["id"].startswith("trace-omitted:")                 # ハッシュ化 id（③是正後）


# ---- EXT-1: 3保存サイト（handle_message／stream_message の answer・clarify）----
# フェイク provider に加え store.* もフェイクへ差し替え、PG 起動なしで検証する（RV是正⑥）。
# `_new_conv()` を使う DB 版（下段）は実 DB 到達時のみ動く end-to-end 確認として残す。

class _FakeExecEventProvider:
    """`get_provider(settings)` の代わりに使うフェイク（固定イベント列を yield するだけ）。"""

    def __init__(self, events):
        self._events = events

    def run(self, ctx):
        return iter(self._events)


def _fixed_result(headline):
    return {"type": "_result",
            "env": {"headline": headline, "summary": {}, "data": {}, "sources": [],
                    "scope": {"world": "v1", "scope_paths": [], "source": "all"}},
            "decision": {"lens": "qa", "input": "q", "reason": "t"}}


def _fixed_question():
    return {"type": "question", "interaction_id": "q1", "mode": "single",
            "prompt": "確認したいことがあります。",
            "options": [{"id": "yes", "label": "はい", "description": ""},
                       {"id": "no", "label": "いいえ", "description": ""}],
            "allow_free_text": False}


def _mock_store_no_db(monkeypatch):
    """`store.*` を DB 不要のフェイクへ差し替える（handle/stream 双方が呼ぶ範囲を一通りカバー）。
    戻り値は `store.add_message` に渡された行を挿入順に保持するリスト。"""
    saved: list = []
    counter = [0]

    def fake_add_message(conversation_id, role, content="", lens=None, route=None, trace=None,
                         answer=None, personal=False):
        counter[0] += 1
        row = {"id": counter[0], "conversation_id": conversation_id, "role": role, "content": content,
               "lens": lens, "route": route, "trace": trace, "answer": answer, "personal": personal}
        saved.append(row)
        return row

    monkeypatch.setattr(store, "add_message", fake_add_message)
    monkeypatch.setattr(store, "recent_messages", lambda conversation_id, limit: [])
    monkeypatch.setattr(store, "get_session_id", lambda conversation_id: None)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {})
    # get_provider() と共有する WEB-1 唯一の読取点（fresh・非キャッシュ）。handle_message/
    # stream_message は knowledge の有無に関わらず必ずこれを呼ぶため、get_system_settings
    # （キャッシュ経由）ではなくこちらを差し替える。
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: {})
    monkeypatch.setattr(store, "set_contains_personal_workspace", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_message_personal", lambda *a, **k: None)
    monkeypatch.setattr(store, "set_session_id", lambda *a, **k: None)
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    return saved


def test_handle_message_mock_store_answer_trace_version(monkeypatch):
    """保存サイト1/3（handle_message・非ストリーミング）。PG 不要（store をフェイク差し替え）。
    trace_version は常に2（TOGGLE-RM・2026-09-03 で v1 退避トグルを撤去）。"""
    saved = _mock_store_no_db(monkeypatch)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    events = [node, _fixed_result("mock 回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))

    out = CS.handle_message(None, "mock store テスト", world="v1", conversation_id=999,
                            user_id="admin", knowledge=False)
    assert out["message"] is saved[-1]
    assert saved[-1]["answer"]["trace_version"] == 2
    assert [n["id"] for n in saved[-1]["trace"]] == ["understand"]


def test_stream_message_mock_store_answer_trace_version(monkeypatch):
    """保存サイト2/3（stream_message の `_result`→answer 保存）。PG 不要。
    trace_version は常に2（TOGGLE-RM・2026-09-03 で v1 退避トグルを撤去）。"""
    saved = _mock_store_no_db(monkeypatch)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    events = [node, _fixed_result("mock stream 回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))

    list(CS.stream_message(None, "mock store ストリームテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["answer"]["trace_version"] == 2


# ===== system_settings は1ターン1回の fresh read を _dispatch/get_provider で共有する =====

def test_handle_message_shares_one_system_settings_snapshot_with_get_provider(monkeypatch):
    """決定的レンズ（`_dispatch` の調べる深さ計算）と agentic 経路（`get_provider` の provider 選択）
    が同じ fresh snapshot を受け取る（別世代の system_settings を見ない・WEB-1 契約と統合）。"""
    _mock_store_no_db(monkeypatch)
    sentinel = {"depth_base_max_turns": 42}
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: sentinel)
    captured = {}

    def fake_get_provider(settings, system_settings=None):
        captured["system_settings"] = system_settings
        return _FakeExecEventProvider([_fixed_result("mock 回答")])

    monkeypatch.setattr(CS, "get_provider", fake_get_provider)
    CS.handle_message(None, "mock store テスト", world="v1", conversation_id=999,
                      user_id="admin", knowledge=False)
    assert captured["system_settings"] is sentinel


def test_stream_message_shares_one_system_settings_snapshot_with_get_provider(monkeypatch):
    _mock_store_no_db(monkeypatch)
    sentinel = {"depth_base_max_turns": 42}
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: sentinel)
    captured = {}

    def fake_get_provider(settings, system_settings=None):
        captured["system_settings"] = system_settings
        return _FakeExecEventProvider([_fixed_result("mock stream 回答")])

    monkeypatch.setattr(CS, "get_provider", fake_get_provider)
    list(CS.stream_message(None, "mock store ストリームテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    assert captured["system_settings"] is sentinel


def test_handle_message_fails_closed_when_system_settings_read_fails(monkeypatch):
    """system_settings の fresh read が失敗（DB 不達）したら、調べる深さを env 既定へ縮退させて
    ターンを続けるのではなく、ターン全体を fail-closed にする（WEB-1 の既存契約と同じ）。"""
    _mock_store_no_db(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_read_system_settings_fresh", _boom)
    monkeypatch.setattr(CS, "get_provider",
                        lambda settings, **kw: _FakeExecEventProvider([_fixed_result("x")]))
    with pytest.raises(RuntimeError):
        CS.handle_message(None, "mock store テスト", world="v1", conversation_id=999,
                          user_id="admin", knowledge=False)


def test_stream_message_fails_closed_when_system_settings_read_fails(monkeypatch):
    _mock_store_no_db(monkeypatch)

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_read_system_settings_fresh", _boom)
    monkeypatch.setattr(CS, "get_provider",
                        lambda settings, **kw: _FakeExecEventProvider([_fixed_result("x")]))
    with pytest.raises(RuntimeError):
        list(CS.stream_message(None, "mock store ストリームテスト", world="v1", conversation_id=999,
                               user_id="admin", knowledge=False))


# ===== WEB-1: 実行に使う codex_web_search はチャットごとの引数のみ（保存済み個人設定は無視） =====
# `_select_provider`（providers/__init__.py）は settings["codex_web_search"] を読んで
# CodexProvider を組み立てる。handle_message/stream_message は `get_provider(settings)` に渡す
# ローカル複製だけをこの引数で上書きする（DB へは書き戻さない）契約をここで固定する。

@pytest.mark.parametrize("stored,requested", [(True, False), (False, True), (False, False), (True, True)])
def test_handle_message_web_search_param_overrides_stored_codex_web_search(monkeypatch, stored, requested):
    _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {"codex_web_search": stored})
    captured = {}

    def _fake_get_provider(settings, **kw):
        captured["settings"] = settings
        return _FakeExecEventProvider([_fixed_result("mock 回答")])
    monkeypatch.setattr(CS, "get_provider", _fake_get_provider)

    CS.handle_message(None, "web_search override テスト", world="v1", conversation_id=999,
                      user_id="admin", knowledge=False, web_search=requested)
    assert captured["settings"]["codex_web_search"] is requested, (
        f"保存済み codex_web_search={stored} が実行に混ざっている"
        "（チャットごとの希望のみを見る契約に違反）")


@pytest.mark.parametrize("stored,requested", [(True, False), (False, True), (False, False), (True, True)])
def test_stream_message_web_search_param_overrides_stored_codex_web_search(monkeypatch, stored, requested):
    _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "get_settings", lambda user_id: {"codex_web_search": stored})
    captured = {}

    def _fake_get_provider(settings, **kw):
        captured["settings"] = settings
        return _FakeExecEventProvider([_fixed_result("mock stream 回答")])
    monkeypatch.setattr(CS, "get_provider", _fake_get_provider)

    list(CS.stream_message(None, "web_search override ストリームテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False, web_search=requested))
    assert captured["settings"]["codex_web_search"] is requested, (
        f"保存済み codex_web_search={stored} が実行に混ざっている"
        "（チャットごとの希望のみを見る契約に違反）")


def test_stream_message_mock_store_clarify_trace_version(monkeypatch):
    """保存サイト3/3（stream_message の `question`→clarify 保存）。PG 不要。
    trace_version は常に2（TOGGLE-RM・2026-09-03 で v1 退避トグルを撤去）。"""
    saved = _mock_store_no_db(monkeypatch)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    events = [node, _fixed_question()]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))

    list(CS.stream_message(None, "mock store clarify テスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))   # knowledge=True は Neo4j session が要るため fake provider では使わない
    q_row = saved[-1]
    assert q_row["lens"] == "clarify"
    assert q_row["answer"]["trace_version"] == 2


# ---- 1ターンの所要時間（answer.duration_ms）。3保存サイトいずれも、会話準備の開始
# （関数入口）〜assistant/clarify 保存直前までの経過時間を埋め込む。`time.monotonic` を決定的な
# 2値の列（開始・保存直前）に差し替えて、実時間のブレに依存しない形で固定する。----

def _fake_monotonic(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(CS.time, "monotonic", lambda: next(it))


def test_handle_message_saves_duration_ms(monkeypatch):
    """保存サイト1/3（handle_message）。"""
    saved = _mock_store_no_db(monkeypatch)
    _fake_monotonic(monkeypatch, 100.0, 100.25)
    events = [_fixed_result("mock 回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))
    CS.handle_message(None, "duration テスト", world="v1", conversation_id=999,
                      user_id="admin", knowledge=False)
    assert saved[-1]["answer"]["duration_ms"] == 250


def test_stream_message_answer_saves_duration_ms(monkeypatch):
    """保存サイト2/3（stream_message の `_result`→answer 保存）。"""
    saved = _mock_store_no_db(monkeypatch)
    _fake_monotonic(monkeypatch, 200.0, 200.5)
    events = [_fixed_result("mock stream 回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))
    list(CS.stream_message(None, "duration ストリームテスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    assert saved[-1]["role"] == "assistant"
    assert saved[-1]["answer"]["duration_ms"] == 500


def test_stream_message_clarify_saves_duration_ms(monkeypatch):
    """保存サイト3/3（stream_message の `question`→clarify 保存）。"""
    saved = _mock_store_no_db(monkeypatch)
    _fake_monotonic(monkeypatch, 300.0, 300.1)
    events = [_fixed_question()]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))
    list(CS.stream_message(None, "duration clarify テスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False))
    q_row = saved[-1]
    assert q_row["lens"] == "clarify"
    assert q_row["answer"]["duration_ms"] == 100


def test_stream_message_stopped_before_result_saves_no_duration(monkeypatch):
    """途中停止（stop_event）ターンは assistant/clarify を保存しない＝duration_ms も存在しない
    （計測対象外になることの確認）。"""
    saved = _mock_store_no_db(monkeypatch)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    events = [node, _fixed_result("到達しないはずの回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))
    stop_event = threading.Event()
    stop_event.set()   # 最初の yield から停止扱いにする
    list(CS.stream_message(None, "duration stop テスト", world="v1", conversation_id=999,
                           user_id="admin", knowledge=False, stop_event=stop_event))
    # user メッセージのみ保存（assistant は保存されない＝duration_ms を持つ行が無い）。
    assert all(r["role"] != "assistant" for r in saved)


# ---- EXT-2: evidence_committed は `_result.env` のサイドカー（独立イベントとして yield しない）----
# providers/base.py が `_result` へ同梱し、chat_service._pop_evidence_committed が永続化と同じ
# 呼び出しの中で trace へ折り込む契約（孤児イベント防止）を PG 不要（store フェイク）で固定する。

def _evidence_committed_sidecar():
    return {"type": "node", "id": "evidence-committed", "kind": "evidence", "label": "根拠を確定",
            "detail": "1 件の根拠を機械検証済みとして確定しました", "status": "done",
            "event_type": "evidence_committed", "evidence_ids": ["ev-1"]}


def test_stream_message_evidence_committed_sidecar_persisted_and_streamed_after_result(monkeypatch):
    """正常系: `_result.env["_evidence_committed"]` は (a) 公開 answer には残らない（pop 済み）・
    (b) 永続化する trace へ折り込まれる・(c) ライブ配信でも `answer` イベントより前にノードとして
    流れる（永続化成功後に配信＝孤児化しない順序）。"""
    saved = _mock_store_no_db(monkeypatch)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    result = _fixed_result("evidence 付き回答")
    result["env"]["_evidence_committed"] = _evidence_committed_sidecar()
    events_in = [node, result]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events_in))

    out_events = list(CS.stream_message(None, "evidence sidecar テスト", world="v1",
                                        conversation_id=999, user_id="admin", knowledge=False))
    assert "_evidence_committed" not in saved[-1]["answer"]   # 公開 answer には残らない
    trace_ids = [n["id"] for n in saved[-1]["trace"]]
    assert "evidence-committed" in trace_ids                  # 永続化する trace には折り込まれる

    idx_node = next(i for i, e in enumerate(out_events)
                    if e.get("type") == "node" and e.get("id") == "evidence-committed")
    idx_answer = next(i for i, e in enumerate(out_events) if e.get("type") == "answer")
    assert idx_node < idx_answer   # ライブ配信でも answer より前（永続化成功後に配信）


def _stoppable_provider(stop_event, env):
    """ノード yield 直後（`_result` を返す直前）に stop_event をセットする（consumer 側の停止判定が
    `_result` を discard するタイミングを再現する）。"""
    class _P:
        def run(self, ctx):
            yield {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
                  "detail": "内容を把握しました", "status": "done"}
            stop_event.set()   # ここで停止要求が来た、を模す
            yield {"type": "_result", "env": env,
                  "decision": {"lens": "qa", "input": "q", "reason": "t"}}
    return _P()


def test_stream_message_stop_before_result_discards_evidence_committed_sidecar_atomically(monkeypatch):
    """停止要求が `_result`（evidence_committed サイドカー同梱）の直前に来た場合、consumer の
    停止判定が `_result` ごと discard する——サイドカーが `_result` と切り離されて先に処理・孤児化
    することはない（embedded サイドカーは常に `_result` と不可分）。assistant メッセージも
    保存されない。"""
    saved = _mock_store_no_db(monkeypatch)
    stop_event = threading.Event()
    env = {"headline": "回答", "summary": {}, "data": {}, "sources": [],
           "scope": {"world": "v1", "scope_paths": [], "source": "all"},
           "_evidence_committed": _evidence_committed_sidecar()}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _stoppable_provider(stop_event, env))

    out_events = list(CS.stream_message(None, "stop test", world="v1", conversation_id=999,
                                        user_id="admin", knowledge=False, stop_event=stop_event))
    assert all(row["role"] != "assistant" for row in saved)   # assistant は保存されない（user 質問のみ保存済み）
    assert any(e.get("type") == "stopped" for e in out_events)
    assert not any(e.get("event_type") == "evidence_committed" for e in out_events)   # 孤児として出ない


def _partial_stream_then_stop_provider(stop_event, partial_text):
    """`answer_delta` を数回ライブ配信してから停止要求を受け、`_result` の headline に
    それまでの累積本文（provider 単体の契約＝拡張設計 §4.4「停止＝その時点までの配信本文」）を
    そのまま載せて返す（帰属呼び出しは行わない＝provider 側の実挙動を模す）。"""
    class _P:
        def run(self, ctx):
            for ch in partial_text:
                yield {"type": "answer_delta", "text": ch}
            stop_event.set()   # ストリーム途中で停止要求が来た、を模す
            yield {"type": "_result",
                  "env": {"headline": partial_text, "summary": {}, "data": {}, "sources": [],
                         "scope": {"world": "v1", "scope_paths": [], "source": "all"}},
                  "decision": {"lens": "qa", "input": "q", "reason": "t"}}
    return _P()


def test_stream_message_stop_mid_stream_forwards_partial_deltas_but_persists_no_assistant(monkeypatch):
    """停止契約の統合固定（拡張設計 §4.4）: provider 単体は「停止＝その時点までの
    配信本文」を `_result.env["headline"]` に積んで返す（ここでは `answer_delta` を経由してクライアント
    へライブ配信済みの本文と同じもの）。しかし chat_service 統合では、その `_result` は stop_event が
    立った後に届くため discard され——**assistant メッセージは保存されない＝会話履歴に本文は残らない**
    （client は SSE で部分本文を見たが、次に会話を開いても・次ターンの履歴 priming にも一切現れない）。"""
    saved = _mock_store_no_db(monkeypatch)
    stop_event = threading.Event()
    partial_text = "調査した結果、"
    monkeypatch.setattr(CS, "get_provider",
                        lambda settings, **kw: _partial_stream_then_stop_provider(stop_event, partial_text))

    out_events = list(CS.stream_message(None, "mid-stream stop test", world="v1", conversation_id=999,
                                        user_id="admin", knowledge=False, stop_event=stop_event))
    deltas = [e["text"] for e in out_events if e.get("type") == "answer_delta"]
    assert "".join(deltas) == partial_text                    # client はストリーム中の部分本文を受け取った
    assert any(e.get("type") == "stopped" for e in out_events)
    assert not any(e.get("type") == "answer" for e in out_events)   # 確定 answer イベントは出ない
    assert all(row["role"] != "assistant" for row in saved)   # 履歴には一切残らない（user 質問のみ保存済み）


def test_stream_message_persistence_failure_prevents_sidecar_live_delivery(monkeypatch):
    """`_result` サイドカー（evidence_committed）の永続化（assistant メッセージの `store.add_message`）
    が失敗すると、その後段のライブ配信（evidence_committed ノード・answer イベント）は一切発行され
    ない——サイドカーは `store.add_message` の成功と不可分（例外境界の確認、コード変更ではなく
    既存の非 try/except 構造がこの契約を満たすことをテストで固定する）。"""
    _mock_store_no_db(monkeypatch)

    def failing_add_message(conversation_id, role, content="", **k):
        if role == "assistant":
            raise RuntimeError("db write failed")
        return {"id": 1, "conversation_id": conversation_id, "role": role, "content": content, **k}

    monkeypatch.setattr(store, "add_message", failing_add_message)
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    result = _fixed_result("evidence 付き回答")
    result["env"]["_evidence_committed"] = _evidence_committed_sidecar()
    events_in = [node, result]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events_in))

    collected = []
    with pytest.raises(RuntimeError, match="db write failed"):
        for ev in CS.stream_message(None, "persist fail test", world="v1", conversation_id=999,
                                    user_id="admin", knowledge=False):
            collected.append(ev)
    assert not any(e.get("event_type") == "evidence_committed" for e in collected)
    assert not any(e.get("type") == "answer" for e in collected)


# ---- EXT-1: handle_message end-to-end（要 Postgres・DB down は skip） ----

def test_handle_message_saves_trace_version_always(monkeypatch):
    """TOGGLE-RM（2026-09-03）: trace_version は常に2で保存される（v1 退避トグルは撤去済み）。"""
    conv_id = _new_conv()
    node = {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
            "detail": "内容を把握しました", "status": "done"}
    events = [node, _fixed_result("v2 既定回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))

    out = CS.handle_message(None, "EXT-1 trace_version テスト", world="v1", conversation_id=conv_id,
                            user_id="admin", knowledge=False)
    msg = out["message"]
    assert msg["answer"]["trace_version"] == 2


def test_handle_message_saves_trace_version_and_hierarchy(monkeypatch):
    """受け入れ条件(b): trace_version=2 のターンで parent_id/agent_run_id を持つイベントが
    保存される（`docs/proposals/2026-08-22-拡張設計.md` §11.3①）。"""
    conv_id = _new_conv()
    parent = EE.build_event("agent-1", "agent", "サブ開始", "worker1 を起動", "done",
                            event_type="agent_started", agent_run_id="sub:worker1:1",
                            parent_agent_run_id="main", run_id="run-abc")
    child = EE.build_event("tool-1", "tool", "資料を検索", "「消費税」", "done",
                           event_type="tool_started", parent_id="agent-1",
                           agent_run_id="sub:worker1:1", run_id="run-abc", phase="gather", seq=1)
    events = [parent, child, _fixed_result("v2 テスト回答")]
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeExecEventProvider(events))

    out = CS.handle_message(None, "EXT-1 flag on テスト", world="v1", conversation_id=conv_id,
                            user_id="admin", knowledge=False)
    msg = out["message"]
    assert msg["answer"]["trace_version"] == 2
    by_id = {n["id"]: n for n in msg["trace"]}
    assert by_id["agent-1"]["agent_run_id"] == "sub:worker1:1"
    assert by_id["agent-1"]["event_type"] == "agent_started"
    assert by_id["tool-1"]["parent_id"] == "agent-1"
    assert by_id["tool-1"]["agent_run_id"] == "sub:worker1:1"
    assert by_id["tool-1"]["phase"] == "gather"


# ---- R1a: _clip_history_msg（純関数・DB 不要） ----

def test_clip_history_msg_short_text_unchanged():
    assert CS._clip_history_msg("短い文") == "短い文"


def test_clip_history_msg_empty_and_none_are_empty_string():
    assert CS._clip_history_msg("") == ""
    assert CS._clip_history_msg(None) == ""


def test_clip_history_msg_truncates_long_text_keeps_head_appends_marker():
    long = "あ" * (CS._HISTORY_MSG_CHARS + 50)
    out = CS._clip_history_msg(long)
    assert out.startswith("あ" * 10)
    assert out.endswith("…（省略）")
    assert len(out) == CS._HISTORY_MSG_CHARS + len("…（省略）")


# ---- R1a: _history_pairs（要 Postgres・DB down は skip） ----

def test_history_pairs_none_conversation_id_returns_empty():
    assert CS._history_pairs(None) == []


def test_history_pairs_only_complete_pairs_in_chronological_order():
    cid = _new_conv()
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", "回答1")
    store.add_message(cid, "user", "質問2")
    store.add_message(cid, "assistant", "回答2")
    assert CS._history_pairs(cid) == [
        {"role": "user", "content": "質問1"}, {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "質問2"}, {"role": "assistant", "content": "回答2"},
    ]


def test_history_pairs_drops_unpaired_user_row_from_stopped_turn():
    """途中停止（UI フィードバック1）で assistant 未保存のまま残る不対 user 行は、対にならないため
    履歴から落ちる（anthropic の交互制約・gemini の role 制約に対して安全）。"""
    cid = _new_conv()
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", "回答1")
    store.add_message(cid, "user", "止められた質問")   # stopped＝assistant 保存なし（chat_service 仕様）
    store.add_message(cid, "user", "質問2")            # 次のターンの user（前の不対 user と連続）
    store.add_message(cid, "assistant", "回答2")
    hist = CS._history_pairs(cid)
    assert hist == [
        {"role": "user", "content": "質問1"}, {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "質問2"}, {"role": "assistant", "content": "回答2"},
    ]
    assert "止められた質問" not in [m["content"] for m in hist]


def test_history_pairs_caps_to_recent_n_pairs():
    cid = _new_conv()
    n = CS._HISTORY_TURNS + 2
    for i in range(n):
        store.add_message(cid, "user", f"質問{i}")
        store.add_message(cid, "assistant", f"回答{i}")
    hist = CS._history_pairs(cid)
    assert len(hist) == CS._HISTORY_TURNS * 2                          # N 対（対数キャップ）だけ残る
    kept_users = [m["content"] for m in hist if m["role"] == "user"]
    assert kept_users == [f"質問{i}" for i in range(n - CS._HISTORY_TURNS, n)]   # 直近 N 対（新しい方）


def test_history_pairs_respects_char_budget_dropping_oldest_pairs_first():
    cid = _new_conv()
    big = "x" * 1000                                    # 1対 ≈ 2000+ 文字（budget=6000 を超える対数を作る）
    for i in range(5):
        store.add_message(cid, "user", f"{big}-u{i}")
        store.add_message(cid, "assistant", f"{big}-a{i}")
    hist = CS._history_pairs(cid)
    total_chars = sum(len(m["content"]) for m in hist)
    assert total_chars <= CS._HISTORY_CHAR_BUDGET                      # 文字予算を超えない
    kept_users = [m["content"] for m in hist if m["role"] == "user"]
    assert kept_users[-1] == f"{big}-u4"                                # 最新の対は必ず残る
    assert f"{big}-u0" not in kept_users                                 # 最も古い対は文字予算で捨てられる


def test_history_pairs_clips_individual_message_over_char_limit():
    cid = _new_conv()
    long_answer = "あ" * (CS._HISTORY_MSG_CHARS + 100)
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", long_answer)
    hist = CS._history_pairs(cid)
    a = next(m for m in hist if m["role"] == "assistant")
    assert len(a["content"]) == CS._HISTORY_MSG_CHARS + len("…（省略）")
    assert a["content"].endswith("…（省略）")


def test_history_pairs_degrades_to_empty_on_read_failure(monkeypatch):
    """履歴の取得に失敗しても本回答は止めない（fail-open・warn ログのみ）。"""
    def _boom(conversation_id, limit):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "recent_messages", _boom)
    assert CS._history_pairs(123) == []


# ---- R1a RV: 固定窓の劣化対策・N=0 無効化・512 行上限（Codex RV 指摘 2 件・2026-07-14） ----

def test_history_pairs_survives_unpaired_row_pileup_pushing_window():
    """MEDIUM: 固定窓（limit=_HISTORY_TURNS*2+8）のまま不対行（途中停止相当）が9件以上積まれると、
    古い完全対が窓の外に押し出され「直近 N 完全対」を返せない（N 対未満に劣化する）。段階的な窓拡大で
    防ぐ。"""
    cid = _new_conv()
    n = CS._HISTORY_TURNS
    for i in range(n):
        store.add_message(cid, "user", f"質問{i}")
        store.add_message(cid, "assistant", f"回答{i}")
    for i in range(10):                                     # stopped 相当の不対 user 行を10件積む
        store.add_message(cid, "user", f"止められた質問{i}")
    hist = CS._history_pairs(cid)
    kept_users = [m["content"] for m in hist if m["role"] == "user"]
    assert len(kept_users) == n                              # 直近 N 対（対数キャップ通り）を維持
    assert kept_users == [f"質問{i}" for i in range(n)]       # N 対すべて（一番古い対も）残る


def test_history_pairs_zero_turns_disables_priming(monkeypatch):
    """LOW: `_HISTORY_TURNS<=0` は `pairs[-0:]`（全対）ではなく履歴 priming 無効化＝`[]` を意味する。"""
    cid = _new_conv()
    store.add_message(cid, "user", "質問1")
    store.add_message(cid, "assistant", "回答1")
    monkeypatch.setattr(CS, "_HISTORY_TURNS", 0)
    assert CS._history_pairs(cid) == []


def test_history_pairs_window_expansion_capped_at_512_rows(monkeypatch):
    """不対行だらけ（完全対が一つもできない）でも、窓拡大は 512 行で打ち切られ、例外や無限ループなく
    `[]` に degrade する（priming は best-effort・全履歴走査はしない）。"""
    calls = []

    def _fake_recent_messages(conversation_id, limit):
        calls.append(limit)
        return [{"id": i, "role": "user", "content": f"u{i}"} for i in range(limit)]  # 常に不対な user 行

    monkeypatch.setattr(store, "recent_messages", _fake_recent_messages)
    assert CS._history_pairs(999) == []
    assert calls[-1] == 512                                  # 最終的に 512 行まで広げて打ち切り
    assert calls == sorted(calls)                             # 単調増加（段階拡大）


# ---- R1a: 確認ID 回帰（message は別チャネル・履歴に混ざらない・PG/Neo4j 不要） ----


# ---- secRV 範囲外是正（2026-07-19）: impact レンズの Neo4j 安全弁 → チャット縮退（純関数・DB/Neo4j不要） ----

def test_impact_overload_result_shape_is_fixed_and_no_llm_synthesis_hook():
    """固定文言のエンベロープ（LLM合成を経由しない・summary.total=0で偽陰性を誘発する事実を持たない）。"""
    env, decision = (r := CS._impact_overload_result("消費税率を変えたら", "w1", None))["env"], r["decision"]
    assert env["lens"] == "impact"
    assert env["headline"] == CS.GRAPH_OVERLOAD_USER_MESSAGE
    assert env["summary"] == {"total": 0}
    assert env["data"] == {}
    assert env["sources"] == []
    # layer/layer_applied が既定・非適用（impact は層フィルタを受け取っても適用しない）で足される。
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all",
                            "layer": "both", "layer_applied": False}
    assert decision["lens"] == "impact" and decision["input"] == "消費税率を変えたら"


def test_impact_overload_result_preserves_scope_meta_when_given():
    sm = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit"}
    r = CS._impact_overload_result("m", "w1", sm)
    # 既存の scope_meta の中身はそのまま残り、impact は非適用なので layer_applied=False が足される。
    assert r["env"]["scope"] == {**sm, "layer_applied": False}


# ===== _dispatch の layer 配線（探す対象・調べ方ブロック §3.4/§3.5） =====

def test_dispatch_qa_forwards_layer_and_marks_applied(monkeypatch):
    """qa（author も qa 分岐に落ちる）は layer を run_qa/ES 補完へ転送し、layer_applied=True。"""
    captured = {}

    def fake_run_qa(payload, world, scope_paths=None, layer=None, max_hits=None):
        captured["run_qa_layer"] = layer
        return {"type": "qa", "question": payload, "answered": True, "citations": []}

    def fake_merge(result, world, query, sp, layer=None):
        captured["merge_layer"] = layer
        return result

    monkeypatch.setattr(CS, "run_qa", fake_run_qa)
    monkeypatch.setattr(CS, "_merge_qa_with_es", fake_merge)
    env = CS._dispatch(None, "qa", "消費税率とは", "w1",
                       {"world": "w1", "scope_paths": [], "source": "all", "layer": "code"})
    assert captured == {"run_qa_layer": "code", "merge_layer": "code"}
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": "code",
                            "layer_applied": True}


def test_dispatch_impact_receives_layer_meta_but_does_not_apply_it(monkeypatch):
    """impact は layer を run_impact へ渡さず（Cypher に触れない）、`layer_applied=False` を明示する。
    `fake_run_impact` は `layer` を受け取らない固定シグネチャ＝渡されたら TypeError で検出する。"""
    def fake_run_impact(session, payload, world, scope_prefixes=None, depth=None):
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    monkeypatch.setattr(CS, "run_impact", fake_run_impact)
    env = CS._dispatch(None, "impact", "消費税率", "w1",
                       {"world": "w1", "scope_paths": [], "source": "all", "layer": "code"})
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": "code",
                            "layer_applied": False}


def test_dispatch_troubleshoot_receives_layer_meta_but_does_not_apply_it(monkeypatch):
    """troubleshoot は layer を run_troubleshoot/ES 補完へ渡さず、`layer_applied=False` を明示する
    （§3.5・裁定1: グラフ traversal だけでなく ES 補完・運用手順 grep も含め全体を非適用）。"""
    def fake_run_troubleshoot(session, symptom, world, scope_paths=None, depth=None):
        return {"type": "troubleshoot", "world": world, "symptom": symptom,
               "anchors": [], "candidates": []}

    monkeypatch.setattr(CS, "run_troubleshoot", fake_run_troubleshoot)
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", lambda result, world, query, sp: result)
    env = CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1",
                       {"world": "w1", "scope_paths": [], "source": "all", "layer": "docs"})
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all", "layer": "docs",
                            "layer_applied": False}


def test_dispatch_no_scope_meta_defaults_to_both_and_qa_applies(monkeypatch):
    """scope_meta 省略（knowledge オフ相当・呼び出し互換）でも既定 both・qa は layer_applied=True。"""
    monkeypatch.setattr(CS, "run_qa", lambda payload, world, scope_paths=None, layer=None, max_hits=None:
                        {"type": "qa", "question": payload, "answered": False, "citations": []})
    monkeypatch.setattr(CS, "_merge_qa_with_es", lambda result, world, query, sp, layer=None: result)
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", None)
    assert env["scope"] == {"world": "w1", "scope_paths": [], "source": "all",
                            "layer": "both", "layer_applied": True}


# ===== _dispatch の調べる深さ配線（§3.2・SC-6c）=====

def _sm(depth_profile=None, **extra):
    return {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
           "depth_profile": depth_profile, **extra}


@pytest.mark.parametrize("profile,expected_depth", [(None, 8), ("standard", 8), ("deep", 10), ("max", 12)])
def test_dispatch_impact_depth_scales_with_profile(monkeypatch, profile, expected_depth):
    """§3.2 の表: 影響たどりの深さ（既定 8）は標準+0・深く+2・最大+4。"""
    captured = {}

    def fake_run_impact(session, payload, world, scope_prefixes=None, depth=None):
        captured["depth"] = depth
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    monkeypatch.setattr(CS, "run_impact", fake_run_impact)
    CS._dispatch(None, "impact", "消費税率", "w1", _sm(profile))
    assert captured["depth"] == expected_depth


@pytest.mark.parametrize("profile,expected_depth", [(None, 3), ("standard", 3), ("deep", 5), ("max", 7)])
def test_dispatch_troubleshoot_depth_scales_with_profile(monkeypatch, profile, expected_depth):
    """§3.2 の表: トラブルシュート近傍の深さ（既定 3）は標準+0・深く+2・最大+4。"""
    captured = {}

    def fake_run_troubleshoot(session, symptom, world, scope_paths=None, depth=None):
        captured["depth"] = depth
        return {"type": "troubleshoot", "world": world, "symptom": symptom,
               "anchors": [], "candidates": []}

    monkeypatch.setattr(CS, "run_troubleshoot", fake_run_troubleshoot)
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", lambda result, world, query, sp: result)
    CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1", _sm(profile))
    assert captured["depth"] == expected_depth


@pytest.mark.parametrize("profile,expected_hits", [(None, 20), ("standard", 20), ("deep", 30), ("max", 40)])
def test_dispatch_qa_max_hits_scales_with_profile(monkeypatch, profile, expected_hits):
    """§3.2 の表: run_qa の max_hits（既定 20）は標準×1・深く×1.5・最大×2。"""
    captured = {}

    def fake_run_qa(payload, world, scope_paths=None, layer=None, max_hits=None):
        captured["max_hits"] = max_hits
        return {"type": "qa", "question": payload, "answered": True, "citations": []}

    monkeypatch.setattr(CS, "run_qa", fake_run_qa)
    monkeypatch.setattr(CS, "_merge_qa_with_es", lambda result, world, query, sp, layer=None: result)
    CS._dispatch(None, "qa", "消費税率とは", "w1", _sm(profile))
    assert captured["max_hits"] == expected_hits


def test_dispatch_depth_profile_honors_system_settings_base_override(monkeypatch):
    """管理画面の基準値編集（system_settings）が env 既定より優先される（実効基準値）。"""
    captured = {}

    def fake_run_impact(session, payload, world, scope_prefixes=None, depth=None):
        captured["depth"] = depth
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    monkeypatch.setattr(CS, "run_impact", fake_run_impact)
    CS._dispatch(None, "impact", "消費税率", "w1", _sm("deep"),
                system_settings={"depth_base_impact_depth": 20})
    assert captured["depth"] == 22   # 20（基準値上書き）+2（深く）


def test_dispatch_depth_profile_system_settings_none_uses_env_default(monkeypatch):
    """`system_settings=None`（呼び出し元省略・後方互換）は env 既定値のまま動く。"""
    captured = {}

    def fake_run_qa(payload, world, scope_paths=None, layer=None, max_hits=None):
        captured["max_hits"] = max_hits
        return {"type": "qa", "question": payload, "answered": True, "citations": []}

    monkeypatch.setattr(CS, "run_qa", fake_run_qa)
    monkeypatch.setattr(CS, "_merge_qa_with_es", lambda result, world, query, sp, layer=None: result)
    CS._dispatch(None, "qa", "消費税率とは", "w1", _sm("max"), system_settings=None)
    assert captured["max_hits"] == 40   # QA_MAX_HITS_DEFAULT(20) の env 既定 ×2


# ===== _dispatch の絶対上限（SC-6c §8）=====
# 管理画面の基準値編集が Field 上限いっぱいの値を許しても、調べる深さ「最大」との組み合わせで
# 各モジュールの env-parse hi 引数（＝既存の絶対上限）を超えない。

def test_dispatch_impact_depth_abs_max_clamps_admin_base_times_multiplier(monkeypatch):
    """admin が impact_depth の基準値を Field 上限（64）に設定していても、「最大」（+4）で
    68 まで伸びず、`IMPACT_MAX_DEPTH_ABS_MAX`（64）でクランプされる。"""
    captured = {}

    def fake_run_impact(session, payload, world, scope_prefixes=None, depth=None):
        captured["depth"] = depth
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    monkeypatch.setattr(CS, "run_impact", fake_run_impact)
    CS._dispatch(None, "impact", "消費税率", "w1", _sm("max"),
                system_settings={"depth_base_impact_depth": 64})
    assert captured["depth"] == 64   # 68 ではなく 64（絶対上限）


def test_dispatch_troubleshoot_depth_abs_max_clamps_admin_base_times_multiplier(monkeypatch):
    """troubleshoot_depth も同様（Field 上限16・最大+4=20 だが絶対上限16でクランプ）。"""
    captured = {}

    def fake_run_troubleshoot(session, symptom, world, scope_paths=None, depth=None):
        captured["depth"] = depth
        return {"type": "troubleshoot", "world": world, "symptom": symptom,
               "anchors": [], "candidates": []}

    monkeypatch.setattr(CS, "run_troubleshoot", fake_run_troubleshoot)
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", lambda result, world, query, sp: result)
    CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1", _sm("max"),
                system_settings={"depth_base_troubleshoot_depth": 16})
    assert captured["depth"] == 16   # 20 ではなく 16（絶対上限）


def test_dispatch_qa_max_hits_abs_max_clamps_admin_base_times_multiplier(monkeypatch):
    """qa の max_hits も同様（Field 上限1000・最大×2=2000 だが絶対上限1000でクランプ）。"""
    captured = {}

    def fake_run_qa(payload, world, scope_paths=None, layer=None, max_hits=None):
        captured["max_hits"] = max_hits
        return {"type": "qa", "question": payload, "answered": True, "citations": []}

    monkeypatch.setattr(CS, "run_qa", fake_run_qa)
    monkeypatch.setattr(CS, "_merge_qa_with_es", lambda result, world, query, sp, layer=None: result)
    CS._dispatch(None, "qa", "消費税率とは", "w1", _sm("max"),
                system_settings={"depth_base_qa_max_hits": 1000})
    assert captured["max_hits"] == 1000   # 2000 ではなく 1000（絶対上限）


# ===== _dispatch の検索経路トグル（調べ方ブロック §3.6・SC-6e）=====

def _raise_if_called(*_a, **_kw):
    raise AssertionError("OFF/不達のツールが呼ばれてしまった（迂回封鎖のはずが実行された）")


def test_dispatch_impact_blocked_when_graph_off_returns_honest_failure(monkeypatch):
    """impact はグラフ必須——OFF なら run_impact を一切呼ばず明示エラーの envelope を返す。"""
    monkeypatch.setattr(CS, "run_impact", _raise_if_called)
    sm = _sm(tools={"grep": True, "fulltext": True, "graph": False})
    env = CS._dispatch(None, "impact", "消費税率", "w1", sm)
    assert env["data"] == {}
    assert env["sources"] == []
    assert "グラフ" in env["headline"]


def test_dispatch_troubleshoot_blocked_when_graph_off_returns_honest_failure(monkeypatch):
    monkeypatch.setattr(CS, "run_troubleshoot", _raise_if_called)
    sm = _sm(tools={"grep": True, "fulltext": True, "graph": False})
    env = CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1", sm)
    assert env["data"] == {}
    assert env["sources"] == []
    assert "グラフ" in env["headline"]


def test_dispatch_qa_blocked_when_grep_and_fulltext_off_returns_honest_failure(monkeypatch):
    monkeypatch.setattr(CS, "run_qa", _raise_if_called)
    monkeypatch.setattr(CS, "_es_citations", _raise_if_called)
    sm = _sm(tools={"grep": False, "fulltext": False, "graph": True})
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", sm)
    assert env["data"] == {}
    assert env["sources"] == []


def test_dispatch_qa_skips_es_merge_when_fulltext_off(monkeypatch):
    """grep ON・fulltext OFF: run_qa は呼ぶが ES 補完（_merge_qa_with_es）は呼ばない。"""
    monkeypatch.setattr(CS, "run_qa", lambda payload, world, scope_paths=None, layer=None, max_hits=None:
                        {"type": "qa", "question": payload, "answered": True,
                         "citations": [{"doc_id": "a.md", "quote": "x", "span": [1, 1]}]})
    monkeypatch.setattr(CS, "_merge_qa_with_es", _raise_if_called)
    sm = _sm(tools={"fulltext": False})
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", sm)
    assert env["summary"]["total"] == 1


def test_dispatch_qa_uses_es_only_when_grep_off(monkeypatch):
    """grep OFF・fulltext ON: run_qa は呼ばず ES 検索のみで citations を組み立てる。"""
    monkeypatch.setattr(CS, "run_qa", _raise_if_called)
    monkeypatch.setattr(CS, "_es_citations", lambda world, query, sp, layer=None:
                        [{"doc_id": "b.md", "quote": "y", "span": [2, 2]}])
    sm = _sm(tools={"grep": False})
    env = CS._dispatch(None, "qa", "消費税率とは", "w1", sm)
    assert env["summary"]["total"] == 1
    assert env["data"]["citations"][0]["doc_id"] == "b.md"


def test_dispatch_troubleshoot_skips_es_merge_when_fulltext_off(monkeypatch):
    monkeypatch.setattr(CS, "run_troubleshoot", lambda session, symptom, world, scope_paths=None, depth=None:
                        {"type": "troubleshoot", "world": world, "symptom": symptom,
                         "anchors": [], "candidates": [{"name": "X", "label": "Program",
                                                        "category": "コード", "role": "近傍",
                                                        "distance": 1, "path": [], "evidence": {}}]})
    monkeypatch.setattr(CS, "_merge_troubleshoot_with_es", _raise_if_called)
    sm = _sm(tools={"fulltext": False})
    env = CS._dispatch(None, "troubleshoot", "夜間バッチ停止", "w1", sm)
    assert env["summary"]["total"] == 1


def test_dispatch_tools_availability_param_blocks_even_when_pref_is_full_on(monkeypatch):
    """`tools_availability`（呼び出し元がターンに1回だけ計算した実接続結果）だけで判定が変わる——
    `tools_pref` 省略（全ON希望）でも、グラフが不達なら impact はブロックされる。"""
    monkeypatch.setattr(CS, "run_impact", _raise_if_called)
    sm = _sm()   # tools 省略＝全ON希望
    env = CS._dispatch(None, "impact", "消費税率", "w1", sm,
                      tools_availability={"grep": True, "fulltext": True, "graph": False})
    assert env["data"] == {}


def test_dispatch_tools_availability_omitted_defaults_to_fully_available(monkeypatch):
    """`tools_availability` 省略（既定 None）は全て利用可能扱い＝既存呼び出し元・単体テストは
    byte-identical（`_dispatch` 自体は DB/ネットワーク非依存のまま）。"""
    captured = {}

    def fake_run_impact(session, payload, world, scope_prefixes=None, depth=None):
        captured["called"] = True
        return {"items": [], "presumed": [], "start": payload, "starts": []}

    monkeypatch.setattr(CS, "run_impact", fake_run_impact)
    CS._dispatch(None, "impact", "消費税率", "w1", _sm())
    assert captured.get("called") is True


# ===== _resolve_scope の layer 正規化（§8 裁定論点3/4） =====

def test_resolve_scope_layer_omitted_defaults_to_both():
    sm = CS._resolve_scope("質問", "w1", [])
    assert sm == {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
                 "lens_source": "auto", "lens_block": None, "web_search": False,
                 "depth_profile": "standard",
                 "tools": {"grep": True, "fulltext": True, "graph": True}}


def test_resolve_scope_layer_valid_value_passthrough():
    sm = CS._resolve_scope("質問", "w1", ["4期/設計"], "code")
    assert sm == {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit", "layer": "code",
                 "lens_source": "auto", "lens_block": None, "web_search": False,
                 "depth_profile": "standard",
                 "tools": {"grep": True, "fulltext": True, "graph": True}}


def test_resolve_scope_layer_invalid_value_raises():
    """省略（None）だけが both・内部の不正値は ValueError（fail-loud）。
    HTTP 入口は pydantic Literal が防ぐため、ここに届く不正値は呼び出し側のバグを示す。"""
    import pytest
    with pytest.raises(ValueError):
        CS._resolve_scope("質問", "w1", [], "bogus")


# ===== _resolve_scope の lens_source/lens_block（調べ方の明示指定元・SC-6b §3.1・RV1 #2）=====

def test_resolve_scope_lens_source_explicit_passthrough():
    sm = CS._resolve_scope("質問", "w1", [], lens_source="explicit")
    assert sm["lens_source"] == "explicit"


def test_resolve_scope_lens_source_slash_passthrough():
    sm = CS._resolve_scope("質問", "w1", [], lens_source="slash")
    assert sm["lens_source"] == "slash"


def test_resolve_scope_lens_block_passthrough():
    """スラッシュ実行時でも、ブロックの継続設定（`lens_block`）は別途保持する（RV1 #2）。"""
    sm = CS._resolve_scope("質問", "w1", [], lens_source="slash", lens_block="qa")
    assert sm["lens_block"] == "qa" and sm["lens_source"] == "slash"


def test_resolve_scope_lens_block_defaults_to_none():
    sm = CS._resolve_scope("質問", "w1", [])
    assert sm["lens_block"] is None


# ===== _resolve_scope の web_search（WEB-1・チャットごとの Web 検索希望・復元用の記録のみ）=====

def test_resolve_scope_web_search_omitted_defaults_to_false():
    sm = CS._resolve_scope("質問", "w1", [])
    assert sm["web_search"] is False


def test_resolve_scope_web_search_true_passthrough():
    sm = CS._resolve_scope("質問", "w1", [], web_search=True)
    assert sm["web_search"] is True


# ===== _resolve_scope の depth_profile（調べる深さ・調べ方ブロック §3.2・SC-6c）=====

def test_resolve_scope_depth_profile_omitted_defaults_to_standard():
    sm = CS._resolve_scope("質問", "w1", [])
    assert sm["depth_profile"] == "standard"


@pytest.mark.parametrize("profile", ["standard", "deep", "max"])
def test_resolve_scope_depth_profile_valid_value_passthrough(profile):
    sm = CS._resolve_scope("質問", "w1", [], depth_profile=profile)
    assert sm["depth_profile"] == profile


def test_resolve_scope_depth_profile_invalid_value_raises():
    """省略（None）だけが standard・内部の不正値は ValueError（fail-loud・layer と同じ契約）。"""
    with pytest.raises(ValueError):
        CS._resolve_scope("質問", "w1", [], depth_profile="bogus")


# ===== _resolve_scope の tools（検索経路トグル・調べ方ブロック §3.6・SC-6e）=====

def test_resolve_scope_tools_omitted_defaults_to_all_on():
    sm = CS._resolve_scope("質問", "w1", [])
    assert sm["tools"] == {"grep": True, "fulltext": True, "graph": True}


def test_resolve_scope_tools_valid_value_passthrough():
    sm = CS._resolve_scope("質問", "w1", [], tools={"grep": False, "fulltext": True, "graph": True})
    assert sm["tools"] == {"grep": False, "fulltext": True, "graph": True}


def test_resolve_scope_tools_all_off_raises():
    """省略（None）だけが全ON・3つとも false は ValueError（fail-loud・layer/depth_profile と同じ契約）。"""
    with pytest.raises(ValueError):
        CS._resolve_scope("質問", "w1", [], tools={"grep": False, "fulltext": False, "graph": False})


def _fake_provider_gen(events):
    """`get_provider(settings).run(ctx)` の代わりに使うフェイクジェネレータ（イベント列を yield し、
    末尾が例外なら最後に raise する）。
    """
    def _gen():
        for ev in events:
            if isinstance(ev, BaseException):
                raise ev
            yield ev
    return _gen()


def test_degrade_overload_passthrough_when_no_exception():
    """例外が起きなければ、元のイベント列をそのまま素通しする（副作用なし）。"""
    events = [{"type": "node", "id": "n1"}, {"type": "_result", "env": {"headline": "ok"}, "decision": {}}]
    out = list(CS._degrade_overload(_fake_provider_gen(events), "m", "w1", None))
    assert out == events


def test_degrade_overload_converts_overload_error_to_fixed_result(caplog):
    """`GraphQueryOverloadError` がイテレーション中に飛ぶと、以降のイベントは出さず固定文言の
    `_result` 1個に差し替わる（LLM 合成（`_answer_prompt`）を一度も経由しない＝偽陰性の温床を断つ）。
    """
    from sherpa.ingest.world_neo4j import GraphQueryOverloadError
    events = [{"type": "node", "id": "tool-graph"},
              GraphQueryOverloadError("timeout", world="w1")]
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = list(CS._degrade_overload(_fake_provider_gen(events), "消費税率", "w1", None))
    assert len(out) == 2                                   # 先行の node イベント + 差し替えた _result
    assert out[0] == {"type": "node", "id": "tool-graph"}
    assert out[1]["type"] == "_result"
    assert out[1]["env"]["headline"] == CS.GRAPH_OVERLOAD_USER_MESSAGE
    assert out[1]["decision"]["lens"] == "impact"
    assert any("安全弁で縮退" in r.getMessage() and "w1" in r.getMessage() for r in caplog.records)


def test_degrade_overload_does_not_swallow_other_exceptions():
    """`GraphQueryOverloadError` 以外の例外は握り潰さず、そのまま呼び出し元へ伝播する。"""
    events = [RuntimeError("boom")]
    with pytest.raises(RuntimeError):
        list(CS._degrade_overload(_fake_provider_gen(events), "m", "w1", None))


def test_history_does_not_leak_into_message_for_confirm_id_or_routing():
    """履歴に確認ID マーカーを含む過去ターンがあっても、現在ターンの `ctx.message`（別チャネル）は
    汚染されず、`_can_ask`/`chat_router._resume_lens` の判定に影響しない。"""
    from sherpa import chat_router
    from sherpa.providers.base import _can_ask

    history = [
        {"role": "user", "content": "選択: 影響を調べる\n確認ID: ask-0011\n元の依頼: 消費税率を変えたい"},
        {"role": "assistant", "content": "影響分析の結果です。"},
    ]
    assert "確認ID" in history[0]["content"]                          # 前提: 履歴側には確かにマーカーがある
    current_message = "追加で教えて"                                   # 確認ID を含まないクリーンな現在の質問
    assert _can_ask(current_message) is True                          # 履歴に確認ID があっても ask_user は有効
    lens, original = chat_router._resume_lens(current_message)
    assert lens is None and original is None                          # resume 判定も発火しない（message のみ見る）


# ---- _known_terms: HIGH-2（secRV 範囲外是正 追補・2026-07-19） -------------------------------
# `lens_service._run_capped` 経由になったことで安全弁（timeout/緊急天井）を迂回しなくなったことを、
# 実 Neo4j を使わないフェイク session（`tests/unit/test_lens_service.py` と同じパターン）で固定する。

class _CSFakeRecord:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


class _CSFakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(_CSFakeRecord(r) for r in self._rows)

    def consume(self):
        pass   # `_run_capped`（HIGH-1是正後）が天井到達時に呼ぶ。ここでは呼ばれること自体は検証しない。


class _CSFakeSession:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise_exc = raise_exc
        self.calls: list[tuple] = []

    def run(self, query, **params):
        self.calls.append((query, params))
        if self._raise_exc is not None:
            raise self._raise_exc
        return _CSFakeResult(self._rows)


def test_known_terms_degrades_to_empty_on_timeout(caplog):
    """クエリがタイムアウトしても例外を出さず空リストへソフト縮退する（黙殺ではなく warning 付き）。"""
    from neo4j.exceptions import Neo4jError
    exc = Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="timed out")
    s = _CSFakeSession(raise_exc=exc)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = CS._known_terms(s, "w1")
    assert out == []
    assert any("タイムアウト" in r.getMessage() for r in caplog.records)


def test_known_terms_degrades_to_partial_list_on_row_cap(caplog):
    """天井到達時は例外を出さず、cap 件までの部分リストへ縮退する（黙って削らず warning は出す）。"""
    from sherpa import lens_service as LS
    over = LS._NEO4J_MAX_ROWS + 5
    rows = [{"name": f"NODE{i}"} for i in range(over)]
    s = _CSFakeSession(rows=rows)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        out = CS._known_terms(s, "w1")
    assert len(out) == LS._NEO4J_MAX_ROWS
    assert any("緊急天井" in r.getMessage() for r in caplog.records)


def test_known_terms_shape_unchanged_on_normal_result():
    """正常系: 返却形（name 文字列のリスト・None は除外）は変わらない。"""
    rows = [{"name": "TAXCALC"}, {"name": None}, {"name": "TAX-RATE"}]
    s = _CSFakeSession(rows=rows)
    out = CS._known_terms(s, "w1")
    assert out == ["TAXCALC", "TAX-RATE"]


# ===== _resolve_lens（調べ方の明示指定・SC-6b §3.1）=====

def test_resolve_lens_no_input_is_auto():
    explicit, source, block, msg = CS._resolve_lens(None, "消費税率を変えたい")
    assert explicit is None and source == "auto" and block is None and msg == "消費税率を変えたい"


def test_resolve_lens_auto_literal_is_auto():
    """ブロックの「自動」を明示送信しても None と同じ扱い（§4.2 の Literal に "auto" を含む理由）。"""
    explicit, source, block, msg = CS._resolve_lens("auto", "消費税率を変えたい")
    assert explicit is None and source == "auto" and block is None


def test_resolve_lens_chatreq_lens_is_explicit():
    explicit, source, block, msg = CS._resolve_lens("impact", "消費税率を変えたい")
    assert explicit == "impact" and source == "explicit" and block == "impact" and msg == "消費税率を変えたい"


def test_resolve_lens_slash_prefix_strips_and_wins_over_explicit():
    """スラッシュ接頭辞は1回限りの明示＝ChatReq.lens（ブロックの明示選択）より優先し、本文から除く。"""
    explicit, source, block, msg = CS._resolve_lens("qa", "/影響 消費税率を変えたい")
    assert explicit == "impact" and source == "slash" and msg == "消費税率を変えたい"


def test_resolve_lens_slash_prefix_keeps_block_continuing_setting():
    """RV1 #2: スラッシュで実効レンズが上書きされても、ブロックの継続設定（qa）は `lens_block` に残る。"""
    explicit, source, block, msg = CS._resolve_lens("qa", "/影響 消費税率を変えたい")
    assert block == "qa"


def test_resolve_lens_slash_prefix_all_four_words():
    assert CS._resolve_lens(None, "/影響 x")[:2] == ("impact", "slash")
    assert CS._resolve_lens(None, "/原因 x")[:2] == ("troubleshoot", "slash")
    assert CS._resolve_lens(None, "/内容 x")[:2] == ("qa", "slash")
    assert CS._resolve_lens(None, "/作成 x")[:2] == ("author", "slash")


def test_resolve_lens_no_slash_when_not_prefix():
    """先頭以外・語彙外・空白なしは通常文として扱う（誤検出しない）。"""
    explicit, source, block, msg = CS._resolve_lens(None, "これは /影響 ではない")
    assert explicit is None and source == "auto" and block is None and msg == "これは /影響 ではない"


# ===== _build_router の明示指定バイパス（SC-6b §3.1・裁定10）=====

def test_build_router_explicit_lens_bypasses_heuristic_and_llm(monkeypatch):
    """explicit_lens 指定時は heuristic/LLM を一切呼ばず decision_for() で直接確定する。"""
    calls = []
    monkeypatch.setattr(CS.intent_llm, "classify", lambda m, s, **kw: calls.append(m) or None)
    d = CS._build_router([], "w1", {}, can_ask=True, explicit_lens="impact")("消費税の仕様は？")
    assert d["lens"] == "impact" and d["reason"] == "明示指定" and calls == []


def test_build_router_confirm_first_overrides_explicit_lens():
    """「確認してから進めて」は明示指定より優先する例外（裁定10）。"""
    d = CS._build_router([], "w1", {}, can_ask=True, explicit_lens="impact")(
        "税率の一覧を Excel にまとめて。確認してから進めて。")
    assert d["lens"] == "clarify"


def test_build_router_explicit_lens_ignored_when_cannot_ask_for_confirm():
    """can_ask=False では確認カードを出せないため、明示指定がそのまま適用される。"""
    d = CS._build_router([], "w1", {}, can_ask=False, explicit_lens="impact")(
        "税率の一覧を Excel にまとめて。確認してから進めて。")
    assert d["lens"] == "impact"


def test_build_router_confirm_first_embeds_scope_meta_tools():
    """SC-6e: 確認カードの payload に `scope_meta["tools"]` を載せる——無いと再送時に
    全ONへ復元されてしまう（グラフのみで確認カードを出して別会話へ移動して戻る事故）。"""
    sm = {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
         "tools": {"grep": False, "fulltext": False, "graph": True}}
    d = CS._build_router([], "w1", {}, can_ask=True, scope_meta=sm)(
        "税率の一覧を Excel にまとめて。確認してから進めて。")
    assert d["lens"] == "clarify"
    assert d["question"]["tools"] == {"grep": False, "fulltext": False, "graph": True}


def test_build_router_confirm_first_tools_omitted_when_scope_meta_none():
    """knowledge オフ相当（scope_meta=None）でも例外にならず tools は None のまま。"""
    d = CS._build_router([], "w1", {}, can_ask=True, scope_meta=None)(
        "税率の一覧を Excel にまとめて。確認してから進めて。")
    assert d["question"]["tools"] is None


# ===== _no_genuine_results / _retry_hints（出典0件時の案内・SC-6d §5・裁定5・RV1 #6/#9）=====

def _env(sources, scope, data=None):
    """`data` 省略時は「通常の検索結果 envelope」を模す非空 dict（キーの中身自体は問わない）。
    明示エラー（未接続/busy/下調べ失敗等）は `data={}` を渡す（RV1 #6）。"""
    return {"sources": sources, "scope": scope, "data": data if data is not None else {"type": "qa"}}


def test_no_genuine_results_true_when_sources_empty_and_data_present():
    assert CS._no_genuine_results(_env([], {})) is True


def test_no_genuine_results_false_when_sources_present():
    assert CS._no_genuine_results(_env(["doc1"], {})) is False


def test_no_genuine_results_false_when_data_is_empty_dict():
    """未接続/busy/下調べ設定不正/下調べ失敗/層強制不可/Neo4j安全弁はいずれも `data={}`（RV1 #6）。"""
    assert CS._no_genuine_results(_env([], {"scope_paths": ["4期/"]}, data={})) is False


def test_retry_hints_scope_only_when_scope_narrowed_and_layer_both():
    env = _env([], {"scope_paths": ["4期/設計"], "layer": "both", "layer_applied": True})
    hints = CS._retry_hints(env)
    assert hints == [{"kind": "scope", "label": "範囲を全体に広げる", "action": {"scope_paths": []}}]


def test_retry_hints_layer_only_when_layer_narrowed_and_scope_all():
    env = _env([], {"scope_paths": [], "layer": "docs", "layer_applied": True})
    hints = CS._retry_hints(env)
    assert hints == [{"kind": "layer", "label": "コードも含めて探す（今は資料のみ）",
                      "action": {"layer": "both"}}]


def test_retry_hints_code_only_label():
    env = _env([], {"scope_paths": [], "layer": "code", "layer_applied": True})
    hints = CS._retry_hints(env)
    assert hints[0]["label"] == "資料も含めて探す（今はコードのみ）"


def test_retry_hints_order_scope_then_layer():
    env = _env([], {"scope_paths": ["4期/"], "layer": "docs", "layer_applied": True})
    hints = CS._retry_hints(env)
    assert [h["kind"] for h in hints] == ["scope", "layer"]


def test_retry_hints_empty_when_already_loosest():
    """範囲=全体・探す対象=両方（最も緩い）なら、案内するものが無い。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True})
    assert CS._retry_hints(env) == []


def test_retry_hints_layer_not_suggested_when_not_applied():
    """impact/troubleshoot は layer_applied=False＝層を広げても結果は変わらないため案内に出さない。"""
    env = _env([], {"scope_paths": ["4期/"], "layer": "docs", "layer_applied": False})
    hints = CS._retry_hints(env)
    assert [h["kind"] for h in hints] == ["scope"]


# ===== _retry_hints の調べる深さの軸（SC-6c・§3.2・§8 裁定5）=====

def test_retry_hints_depth_standard_suggests_max():
    """標準/深くは既に最も緩い（max）ではないため案内に含める。1回で「最大」へ広げる。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True, "depth_profile": "standard"})
    hints = CS._retry_hints(env)
    assert hints == [{"kind": "depth", "label": "調べる深さを上げて探す（今は標準）",
                      "action": {"depth_profile": "max"}}]


def test_retry_hints_depth_deep_suggests_max():
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True, "depth_profile": "deep"})
    hints = CS._retry_hints(env)
    assert hints == [{"kind": "depth", "label": "調べる深さを上げて探す（今は深く）",
                      "action": {"depth_profile": "max"}}]


def test_retry_hints_depth_max_not_suggested():
    """調べる深さが既に「最大」なら案内に含めない（絞られていない軸は見せない）。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True, "depth_profile": "max"})
    assert CS._retry_hints(env) == []


def test_retry_hints_order_scope_then_layer_then_depth():
    """§8 裁定5: 範囲→探す対象→調べる深さの順。"""
    env = _env([], {"scope_paths": ["4期/"], "layer": "docs", "layer_applied": True,
                    "depth_profile": "deep"})
    hints = CS._retry_hints(env)
    assert [h["kind"] for h in hints] == ["scope", "layer", "depth"]


# ===== _retry_hints の検索経路トグルの軸（SC-6e・調べ方ブロック §3.6）=====

def test_retry_hints_tools_non_default_suggests_reset():
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True,
                    "tools": {"grep": False, "fulltext": False, "graph": True}})
    hints = CS._retry_hints(env)
    assert hints == [{"kind": "tools", "label": "OFF にした検索を戻す",
                      "action": {"tools": {"grep": True, "fulltext": True, "graph": True}}}]


def test_retry_hints_tools_all_on_not_suggested():
    """全ON（既定）なら案内に含めない。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True,
                    "tools": {"grep": True, "fulltext": True, "graph": True}})
    assert CS._retry_hints(env) == []


def test_retry_hints_tools_missing_key_not_suggested():
    """`scope.tools` 自体が無い（SC-6e 導入前の旧回答）＝全ON扱いで案内に含めない。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True})
    assert CS._retry_hints(env) == []


def test_retry_hints_order_scope_then_layer_then_depth_then_tools():
    """範囲→探す対象→調べる深さ→ツールの順（末尾）。"""
    env = _env([], {"scope_paths": ["4期/"], "layer": "docs", "layer_applied": True,
                    "depth_profile": "deep", "tools": {"grep": False, "fulltext": True, "graph": True}})
    hints = CS._retry_hints(env)
    assert [h["kind"] for h in hints] == ["scope", "layer", "depth", "tools"]


def test_finalize_attaches_retry_hints_to_env():
    env = _env([], {"scope_paths": ["4期/"], "layer": "both", "layer_applied": True})
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert out["retry_hints"] == [{"kind": "scope", "label": "範囲を全体に広げる",
                                   "action": {"scope_paths": []}}]


def test_finalize_omits_retry_hints_key_when_no_hints():
    env = _env(["doc1"], {"scope_paths": [], "layer": "both", "layer_applied": True})
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out


def test_finalize_no_retry_hints_for_explicit_error_envelope():
    """明示エラー（`data={}`）は範囲が絞られていても案内を出さない（RV1 #6・退行テスト）。"""
    env = _env([], {"scope_paths": ["4期/"], "layer": "docs", "layer_applied": True}, data={})
    env["headline"] = "下調べAIでの調査がうまくいきませんでした。設定を確認するか、下調べ機能をOFFにしてください。"
    out = CS._finalize(env, {"lens": "qa", "reason": "下調べ設定の不正"})
    assert "retry_hints" not in out
    assert out["headline"] != CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE   # 確定文言への置換も対象外


def test_finalize_replaces_headline_when_loosest_and_no_hints_qa():
    """全軸が最も緩い設定でなお0件（qa/author）は §5 確定文言へ置換する（RV1 #9）。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True})
    env["headline"] = "該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。"
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out
    assert out["headline"] == CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE


def test_finalize_keeps_budget_headline_for_main_task_id():
    """STOP-1: `providers/base.py::_agentic_run`（`self._sub is None`）が既に据えた固定 headline
    （予算到達・出典0件）は、`_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE` で上書きしない
    （`task_id == "main"` かつ予算系 stop_reason の場合だけの例外）。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True},
              data={"evidence_packet": {"task_id": "main", "stop_reason": "turns_exhausted"}})
    env["headline"] = "調査が上限に達したため、ここまでに確認できた内容のみをお伝えします。"
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out
    assert out["headline"] == "調査が上限に達したため、ここまでに確認できた内容のみをお伝えします。"


def test_finalize_replaces_headline_for_hybrid_sub_task_id_even_with_budget_stop_reason():
    """STOP-1: ハイブリッド（`task_id == "sub:{profile_id}"`）は provider 側の budget_exhausted
    ガード自体が `self._sub is None` に限定され固定 headline を据えていないため、`_finalize` の
    例外対象にも含めない——0件時は従来どおり `_NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE` へ置換する
    （`_is_budget_exhausted` を `stop_reason` だけで判定すると、この従来挙動が変わってしまう回帰）。
    """
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": True},
              data={"evidence_packet": {"task_id": "sub:worker", "stop_reason": "turns_exhausted"}})
    env["headline"] = "サブエージェントの合成本文（このテストでは中身を問わない）"
    out = CS._finalize(env, {"lens": "qa", "reason": "既定（検索）"})
    assert "retry_hints" not in out
    assert out["headline"] == CS._NO_RESULTS_EVEN_AT_LOOSEST_HEADLINE


def test_finalize_does_not_replace_headline_for_impact_even_when_loosest():
    """impact/troubleshoot には層の概念が無く既存 headline が十分具体的なため対象外（RV1 #9）。"""
    env = _env([], {"scope_paths": [], "layer": "both", "layer_applied": False})
    env["headline"] = "「税率」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"
    out = CS._finalize(env, {"lens": "impact", "reason": "変更・影響の語"})
    assert "retry_hints" not in out
    assert out["headline"] == "「税率」の影響先は見つかりませんでした（表記ゆれ、または影響なし）。"


# ===== handle_message/stream_message の lens 配線（SC-6b・end-to-end but DB 不要）=====

class _FakeCtxCaptureProvider:
    """`get_provider(settings)` の代わり。実プロバイダの `_gather`（agentic 反復検索）は経由せず、
    渡された `ctx`（route/scope_meta の配線を検証する対象）を捕捉したうえで固定 `_result` を返す。
    """

    def __init__(self, captured):
        self._captured = captured

    def run(self, ctx):
        self._captured["ctx"] = ctx
        return iter([_fixed_result("mock 回答")])


def test_handle_message_explicit_lens_wires_into_ctx_route(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeCtxCaptureProvider(captured))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    CS.handle_message(None, "消費税率を変えたい", world="v1", conversation_id=999,
                      user_id="admin", knowledge=True, lens="impact")
    ctx = captured["ctx"]
    assert ctx.scope_meta["lens_source"] == "explicit"
    d = ctx.route("消費税率を変えたい")
    assert d["lens"] == "impact" and d["reason"] == "明示指定"
    user_row = next(r for r in saved if r["role"] == "user")
    assert user_row["content"] == "消費税率を変えたい"   # 通常の明示（スラッシュではない）は本文を変えない


def test_handle_message_slash_prefix_strips_message_and_wins_over_chatreq_lens(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeCtxCaptureProvider(captured))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    CS.handle_message(None, "/影響 消費税率を変えたい", world="v1", conversation_id=999,
                      user_id="admin", knowledge=True, lens="qa")   # ブロックは qa でもスラッシュが勝つ
    ctx = captured["ctx"]
    assert ctx.scope_meta["lens_source"] == "slash"
    d = ctx.route("消費税率を変えたい")
    assert d["lens"] == "impact"
    user_row = next(r for r in saved if r["role"] == "user")
    assert user_row["content"] == "消費税率を変えたい"   # 保存された質問からも接頭辞が除かれる


def test_handle_message_lens_omitted_defaults_to_auto(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeCtxCaptureProvider(captured))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    CS.handle_message(None, "消費税率を変えたい", world="v1", conversation_id=999,
                      user_id="admin", knowledge=True)
    assert captured["ctx"].scope_meta["lens_source"] == "auto"


def test_stream_message_explicit_lens_wires_into_ctx_route(monkeypatch):
    _mock_store_no_db(monkeypatch)
    captured: dict = {}
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _FakeCtxCaptureProvider(captured))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    list(CS.stream_message(None, "夜間バッチが心配", world="v1", conversation_id=999,
                           user_id="admin", knowledge=True, lens="troubleshoot"))
    ctx = captured["ctx"]
    assert ctx.scope_meta["lens_source"] == "explicit"
    assert ctx.route("夜間バッチが心配")["lens"] == "troubleshoot"


# ===== STOP-1: 予算停止→保存済み answer までの統合（実プロバイダ経由） =====
# `_FakeCtxCaptureProvider`（上）とは異なり、ここでは実際に `_GenProvider._agentic_run` を通す
# （LLM だけ `agentic_search._post` をスタブ）——`providers/base.py` の budget_exhausted ガードが
# 単発 grep フォールバックへ逃げず、固定 headline と Evidence Packet を `store.add_message` まで
# 実際に届けることを end-to-end で固定する（フロント直接注入の e2e だけでは検出できないサーバ側の
# 経路を通す）。

def _mock_store_and_real_openai_provider(monkeypatch, *, max_turns=12, max_tools_per_turn=16,
                                         tool_result_max_total_bytes=200_000):
    """`store` をフェイク差し替えしつつ、`get_provider` は実 `OpenAIProvider` を返す
    （Neo4j 依存の `_known_terms`/router 自動判定は避け、lens 明示指定で `_agentic_run` へ直行する）。

    `MAX_TURNS`/`MAX_TOOLS_PER_TURN` は import 時に env（`SHERPA_AGENTIC_MAX_TURNS`/
    `SHERPA_AGENTIC_MAX_TOOLS_PER_TURN`）で決まる可変値のため、呼び出し元が意図した
    stop_reason（例: `tools_per_turn_exceeded`）とは別の予算超過（例: `turns_exhausted`）へ
    先に落ちてテストが環境依存で不安定にならないよう、ここで明示的に固定する
    （`TOOL_RESULT_MAX_TOTAL_BYTES` はコード既定の固定値だが、同じ理由で揃えて明示的に渡す）。
    """
    import sherpa.agentic_search as A
    from sherpa.providers.openai import OpenAIProvider

    monkeypatch.setattr(A, "MAX_TURNS", max_turns)
    monkeypatch.setattr(A, "MAX_TOOLS_PER_TURN", max_tools_per_turn)
    monkeypatch.setattr(A, "TOOL_RESULT_MAX_TOTAL_BYTES", tool_result_max_total_bytes)
    monkeypatch.setattr(A, "es_index", A.es_index)
    monkeypatch.setattr(A.es_index, "available", lambda: True)
    monkeypatch.setattr(A, "_graph_available", lambda: True)
    saved = _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "get_provider", lambda settings, system_settings=None: OpenAIProvider("sk-dummy", "gpt-5.5"))
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: [])
    return saved, A


def test_stream_message_tools_per_turn_exceeded_saves_budget_headline_and_packet(monkeypatch):
    """実環境の実害の再現: `SHERPA_AGENTIC_MAX_TOOLS_PER_TURN`（既定16）到達で打ち切られた
    ターン（`stop_reason="tools_per_turn_exceeded"`）が、単発 grep フォールバック（Evidence
    Packet ごと喪失）へ落ちずに固定 headline で保存されることを固定する。"""
    from sherpa.providers.prompts import _BUDGET_EXHAUSTED_HEADLINE

    saved, A = _mock_store_and_real_openai_provider(monkeypatch)
    calls = [{"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}
             for i in range(25)]   # MAX_TOOLS_PER_TURN（既定16）超過＝1回の応答だけで打ち切り
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: {
        "choices": [{"message": {"content": "", "tool_calls": calls}}]}
    try:
        list(CS.stream_message(None, "消費税率は?", world="v1", conversation_id=999,
                               user_id="admin", knowledge=True, lens="qa"))
    finally:
        A._post = orig_post

    row = next(r for r in saved if r["role"] == "assistant")
    assert row["answer"]["headline"] == _BUDGET_EXHAUSTED_HEADLINE
    assert row["answer"]["data"]["evidence_packet"]["stop_reason"] == "tools_per_turn_exceeded"


def test_stream_message_budget_exceeded_saves_budget_headline_and_packet(monkeypatch):
    """呼び出し予算（`_CallBudget`）枯渇（`stop_reason="budget_exceeded"`）でも同様に固定
    headline で保存される（`tools_per_turn_exceeded` と別の生成箇所であることの回帰）。"""
    from sherpa.providers.prompts import _BUDGET_EXHAUSTED_HEADLINE

    saved, A = _mock_store_and_real_openai_provider(monkeypatch)
    orig_post = A._post
    # 1ターン目の tool_calls 応答の直後、呼び出し予算が枯渇した状態を模す
    # （`_send` は物理送信のたびに `llm.begin_openai_send` を通るため、鍵検証まで到達させず
    # `_post` 自体をスタブし、2回目の送信で `SendBudgetExceeded` を模擬する）。
    import sherpa.llm as llm

    def fake_begin(call_budget, usage_acc):
        if fake_begin.n == 0:
            fake_begin.n += 1
            return
        raise llm.SendBudgetExceeded("budget_exceeded")
    fake_begin.n = 0
    monkeypatch.setattr(llm, "begin_openai_send", fake_begin)
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]}]
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        list(CS.stream_message(None, "消費税率は?", world="v1", conversation_id=999,
                               user_id="admin", knowledge=True, lens="qa"))
    finally:
        A._post = orig_post

    row = next(r for r in saved if r["role"] == "assistant")
    assert row["answer"]["headline"] == _BUDGET_EXHAUSTED_HEADLINE
    assert row["answer"]["data"]["evidence_packet"]["stop_reason"] == "budget_exceeded"


def test_stream_message_turns_exhausted_with_ungrounded_synthesis_replaces_headline(monkeypatch):
    """STOP-1: `turns_exhausted` の末尾合成（OpenAI 方言は追加 `_post` で非空本文を生成できる）が
    根拠0件のまま断定文を返しても、その未検証の生成本文をそのまま回答として保存しない
    （grounded QA 契約——根拠ゲートを自力で満たさない部分回答は固定 headline へ強制的に
    差し替える。空回答のケースとは別の穴のため独立に固定する）。"""
    from sherpa.providers.prompts import _BUDGET_EXHAUSTED_HEADLINE

    saved, A = _mock_store_and_real_openai_provider(monkeypatch, max_turns=1)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])   # 根拠0件（citation も構造 Evidence も無し）
    monkeypatch.setattr(A, "run_tool", fake_run_tool)

    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        # `max_turns=1` でループが尽きた直後の末尾合成——tool_calls 無し・非空本文（根拠0件のまま）。
        {"choices": [{"message": {"content": "資料にない断定本文（根拠なし）。"}}]},
    ]
    orig_post = A._post
    A._post = lambda url, headers, body, timeout=90: seq.pop(0)
    try:
        list(CS.stream_message(None, "消費税率は?", world="v1", conversation_id=999,
                               user_id="admin", knowledge=True, lens="qa"))
    finally:
        A._post = orig_post

    row = next(r for r in saved if r["role"] == "assistant")
    assert row["answer"]["headline"] == _BUDGET_EXHAUSTED_HEADLINE
    assert "資料にない断定本文" not in row["answer"]["headline"]
    assert row["answer"]["data"]["evidence_packet"]["stop_reason"] == "turns_exhausted"
    assert row["answer"]["data"]["evidence_packet"]["evidence_selected"] == 0


# ===== 打切り申告がチャットの headline へ出る（検収是正） =====

def test_qa_headline_carries_truncation_note_on_zero_hits():
    """ヒット0件の headline は「見つかりませんでした」と断定するため、**探せていないだけ**の
    ときにそれを言うのは誤り。チャットが主入口なので、ここに出ないと利用者は気づけない。"""
    result = {"citations": [], "notes": ["「大規模一覧.xlsx」は大きすぎて全体を検索できていません（先頭部分のみ）。"]}
    env = CS._answer_qa(result, "w")
    assert "見つかりませんでした" in env["headline"]
    assert "大きすぎて全体を検索できていません" in env["headline"]


def test_qa_headline_unchanged_without_notes():
    """打切りが無ければ headline は完全に不変（加算的変更）。"""
    base = CS._answer_qa({"citations": []}, "w")["headline"]
    assert base == CS._answer_qa({"citations": [], "notes": []}, "w")["headline"]
    assert "⚠" not in base


def test_troubleshoot_and_impact_headlines_carry_truncation_note():
    note = "「大規模一覧.xlsx」は大きすぎて全体を検索できていません（先頭部分のみ）。"
    ts = CS._answer_troubleshoot({"candidates": [], "notes": [note]}, "w")
    assert note in ts["headline"]
    im = CS._answer_impact({"items": [], "start": "契約", "notes": [note]}, "w")
    assert note in im["headline"]
