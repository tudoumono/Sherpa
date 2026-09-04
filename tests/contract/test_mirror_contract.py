"""鏡モデル（03-鏡モデル.md）の契約テスト＝再プランの定義(definition-of-done)。

代表 fixture `fixtures/mirror/`（2世代 4期更改/5期更改・copybook を世代ごとに複製・
**corpus/ の外**＝旧 version glob に拾わせない）で、
鏡モデルの核を固定する:
  - 同一性＝パス（複製同名は別ノード）
  - リンク＝同世代内最近傍（世代をまたいで線を引かない）
  - 部分木フィルタ（トップ/工程で1世界として絞れる）
  - 原本DL＝パス基準（basename 衝突で誤対象を返さない）

**現状**: fixture 健全性テストのみ緑。鏡挙動テストは `model.py` のパス修飾 ID（手順2）・
`static_analysis` の2パス解決（手順3）が入るまで **SKIP**（実装したら順次 un-skip）。
pytest でも `python3 tests/contract/test_mirror_contract.py` でも実行可。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

MIRROR = ROOT / "fixtures/mirror"
GENS = ("4期更改", "5期更改")
SRC = "03_開発/01_ソース"


def _todo(reason):   # 未実装の契約＝SKIP（実装で un-skip）
    try:
        import pytest
        pytest.skip(reason)
    except Exception:
        print(f"TODO {reason}")


def test_fixture_is_well_formed():
    """代表 fixture が鏡の難所を備える: 2世代・複製同名・世代ごとの copybook（値違い）・COPY/CALL あり。"""
    for g in GENS:
        base = MIRROR / g / SRC
        assert (base / "ORDER-MAIN.cbl").is_file()
        assert (base / "ORDER-SUB.cbl").is_file()
        assert (base / "SHARED-CPY.cpy").is_file()
        main = (base / "ORDER-MAIN.cbl").read_text(encoding="utf-8")
        assert "COPY SHARED-CPY" in main and "CALL 'ORDER-SUB'" in main   # 世代内リンク対象
    # copybook は世代ごとに複製され中身が違う（別ノードである根拠）
    v4 = (MIRROR / "4期更改" / SRC / "SHARED-CPY.cpy").read_text(encoding="utf-8")
    v5 = (MIRROR / "5期更改" / SRC / "SHARED-CPY.cpy").read_text(encoding="utf-8")
    assert "VALUE 100" in v4 and "VALUE 200" in v5 and v4 != v5
    # 同名ファイルが2世代に実体で存在（＝パス同一性で別ノードにすべき対象）
    assert (MIRROR / "4期更改" / SRC / "ORDER-MAIN.cbl").read_text() == \
           (MIRROR / "5期更改" / SRC / "ORDER-MAIN.cbl").read_text()


def _build():
    from sherpa.ingest import world_graph
    return world_graph.build_world(MIRROR, "mirror")


def _find(nodes, label, name, top):
    return [n for n in nodes if n["label"] == label and n["name"] == name and n["top_scope"] == top]


def test_path_identity_duplicates_are_separate_nodes():
    """複製同名（4期/5期の ORDER-MAIN・SHARED-CPY）は別ノード（canonical_id がパス修飾）。"""
    nodes, _edges, flags = _build()
    assert flags == []                                   # 複製ありでも曖昧に落ちない（最近傍で解決）
    m4 = _find(nodes, "Module", "ORDER-MAIN", "4期更改")
    m5 = _find(nodes, "Module", "ORDER-MAIN", "5期更改")
    assert len(m4) == 1 and len(m5) == 1 and m4[0]["cid"] != m5[0]["cid"]   # 同名でも別ノード
    # copybook も世代ごとに別ノード＋値が違う（100/200）
    amt = {n["top_scope"]: n["value"] for n in nodes if n["name"] == "SHARED-AMT"}
    assert amt == {"4期更改": "100", "5期更改": "200"}
    # メタデータ（検索スコープ）が載る
    assert m4[0]["world_id"] == "mirror" and m4[0]["phase"] == "03_開発" and m4[0]["category"] == "01_ソース"


def test_link_resolves_within_generation_only():
    """4期 ORDER-MAIN は 4期 SHARED-CPY/ORDER-SUB にのみリンク（世代をまたいで線を引かない）。"""
    nodes, edges, _flags = _build()
    for top in ("4期更改", "5期更改"):
        src = _find(nodes, "Module", "ORDER-MAIN", top)[0]["cid"]
        copies = [e["dst"] for e in edges if e["type"] == "COPIES" and e["src"] == src]
        calls = [e["dst"] for e in edges if e["type"] == "INVOKES" and e["src"] == src]
        assert all(top in d for d in copies) and copies, f"{top} COPIES が同世代でない: {copies}"
        assert all(top in d for d in calls) and calls, f"{top} INVOKES が同世代でない: {calls}"
        # 相手世代へは引かれていない
        other = "5期更改" if top == "4期更改" else "4期更改"
        assert not any(other in d for d in copies + calls)


def test_subtree_filter_yields_single_generation_subgraph():
    """トップ/工程フォルダで絞ると、その部分木だけの subgraph になる（path prefix フィルタ＝§3）。"""
    from sherpa.ingest import world_graph
    nodes, edges, _ = _build()
    n4, e4 = world_graph.subgraph(nodes, edges, "4期更改")
    assert n4 and all(n["top_scope"] == "4期更改" for n in n4)        # 4期だけ
    assert not any("5期更改" in (e["src"] + e["dst"]) for e in e4)      # 5期へは出ない
    # さらに工程で絞れる（どの階層でも）
    n4dev, _ = world_graph.subgraph(nodes, edges, "4期更改/03_開発")
    assert n4dev and all(n["path"].startswith("4期更改/03_開発/") for n in n4dev)
    # 全体は両世代を含む（横断が見える）
    full_n, _ = world_graph.subgraph(nodes, edges, None)
    assert {n["top_scope"] for n in full_n} >= {"4期更改", "5期更改"}


def test_original_download_is_path_based_not_basename():
    """原本DL はパス基準＝同名2ファイルを取り違えない（4期 ORDER-MAIN と 5期 ORDER-MAIN を区別）。"""
    from sherpa.ingest import world_graph
    p4 = world_graph.resolve_path(MIRROR, "4期更改/03_開発/01_ソース/ORDER-MAIN.cbl")
    p5 = world_graph.resolve_path(MIRROR, "5期更改/03_開発/01_ソース/ORDER-MAIN.cbl")
    assert p4 and p5 and p4.is_file() and p5.is_file() and p4 != p5    # 同名でも別パスを返す
    assert world_graph.resolve_path(MIRROR, "../etc/passwd") is None     # トラバーサル拒否
    assert world_graph.resolve_path(MIRROR, "4期更改/03_開発/01_ソース/NOPE.cbl") is None


def test_mention_edges_link_to_all_generations_with_same_name():
    """言及エッジ（Pass3・S2）は骨格の「同世代内最近傍」規律とは別の**制度化された例外**（K5）:
    名前一致する**全世代**のコードノードへ張る。両世代の受注設計書.md はそれぞれ ORDER-MAIN に
    言及しており、各 Document は自世代/他世代を問わず両方の ORDER-MAIN ノードへ DOCUMENTS
    （via=mention）を持つ（世代跨ぎが許される唯一の構造エッジ族＝影響 traversal には乗らない）。
    """
    nodes, edges, flags = _build()
    assert flags == []
    for top in ("4期更改", "5期更改"):
        doc_cid = f"document:mirror:{top}/02_設計/01_基本設計/受注設計書.md"
        mentions = {e["dst"] for e in edges
                   if e["type"] == "DOCUMENTS" and e.get("via") == "mention" and e["src"] == doc_cid}
        assert mentions == {
            f"module:mirror:4期更改/03_開発/01_ソース/ORDER-MAIN.cbl#ORDER-MAIN",
            f"module:mirror:5期更改/03_開発/01_ソース/ORDER-MAIN.cbl#ORDER-MAIN",
        }, mentions


CORPUS_V1 = ROOT / "fixtures/corpus/v1"
GOLDEN = ROOT / "tests/contract/goldens/code_analyzer_migration_before.json"
_GOLDEN_FIXTURES = (("v1", CORPUS_V1, "v1"), ("mirror", MIRROR, "mirror"))
_GOLDEN_FIELDS = ("nodes_by_label_name", "nodes_by_cid", "edges_by_label_name", "edges_by_cid")


def test_mention_edge_reaches_qualified_copybook_child_via_leaf_name():
    """実データ（`fixtures/corpus/v1`）固定: 税計算仕様書.md は「TAX-RATE」に言及しているが、
    その定義は TAX-CPY.cpy 内で `01 TAX-AREA.` 配下の子項目（修飾名 `TAX-AREA.TAX-RATE`）——
    トークナイザは `.` で分断するため修飾名そのものは辞書突合が構造的に一致しえず、
    単純名（表示名）でも突合できるようにする修正（S2-LEAFNAME）が無いと張られない
    （2026-09-05 調査で実証されたバグ）。
    """
    from sherpa.ingest import world_graph
    nodes, edges, flags = world_graph.build_world(CORPUS_V1, "v1")
    assert not [f for f in flags if f.get("reason") == "mention_ambiguous_names"]
    doc_cid = "document:v1:4期/02_設計/01_基本設計/税計算仕様書.md"
    dst_cid = "dataitem:v1:4期/00_共通/標準コピーブック/TAX-CPY.cpy#TAX-AREA.TAX-RATE"
    assert dst_cid in {n["cid"] for n in nodes}
    mentions = {e["dst"] for e in edges
               if e["type"] == "DOCUMENTS" and e.get("via") == "mention" and e["src"] == doc_cid}
    assert dst_cid in mentions, mentions


def _rich_summary(nodes, edges) -> dict:
    """`build_world()` の出力を「緩めた一致」比較用に正規化（§7 裁定7）。

    **主**: `nodes_by_label_name`＝(label,name) の集合、`edges_by_label_name`＝
    (edge_type,(src_label,src_name),(dst_label,dst_name)) の集合——順序・内部 cid・生成順は無視する。
    **補助**: `nodes_by_cid`／`edges_by_cid`（cid＝label+world+path+name を符号化した識別子）も
    併せて比較する——(label,name) だけでは、同名ノードが別世代/別パスで**重複したまま cid が
    2本→1本に潰れる**ような欠落（cid 集合の方が減る）を検出できないため（例: fixtures/mirror は
    2世代に同名 `Module:ORDER-MAIN` を持ち、(label,name) 集合は1件だが cid 集合は2件になる）。

    S2（2026-09-04-グラフのソース正典化.md・言及エッジ）以降: `via=="mention"` の
    `DOCUMENTS` エッジと、それだけで生成される `Document` ノードは比較から除外する
    （判断・2026-09-04）。本 golden は `build_world()` を `concepts_path`/`semantic_path` 未指定で
    呼ぶ入力から採っており、Document は元々0件（S2 前の golden）——CODE-1a 移行が骨格を
    黙って欠かさないことの確認という本テストの意図に、S2 で新設された言及突合の結果を
    混ぜない。
    """
    edges = [e for e in edges if e.get("via") != "mention"]
    nodes = [n for n in nodes if n["label"] != "Document"]
    by_cid = {n["cid"]: n for n in nodes}

    def _endpoint(cid):
        n = by_cid.get(cid)
        return (n["label"], n["name"]) if n else ("?", cid)

    nodes_by_label_name = sorted({(n["label"], n["name"]) for n in nodes})
    nodes_by_cid = sorted({n["cid"] for n in nodes})
    edges_by_label_name = sorted({(e["type"], _endpoint(e["src"]), _endpoint(e["dst"])) for e in edges})
    edges_by_cid = sorted({(e["type"], e["src"], e["dst"]) for e in edges})
    return {
        "nodes_by_label_name": [list(x) for x in nodes_by_label_name],
        "nodes_by_cid": nodes_by_cid,
        "edges_by_label_name": [[t, list(s), list(d)] for t, s, d in edges_by_label_name],
        "edges_by_cid": [list(x) for x in edges_by_cid],
    }


def test_analyzer_registry_migration_keeps_graph_set_identical():
    """コード解析層のコンポーネント化（CODE-1a）の移行完了条件（§7 裁定7）。

    `sherpa/ingest/analyzers/` への移植前（`world_graph.build_world()` に COBOL/JCL/コピーブックの
    分岐がベタ書きだった版・`tests/contract/goldens/code_analyzer_migration_before.json` の
    `base_sha` 時点）に `fixtures/corpus/v1`・`fixtures/mirror` で採った golden と、移行後の現行
    コードの出力を比較する。(label,name) の集合と cid の集合の**両方**が一致すればよい
    （順序・内部生成順・タイムスタンプは無視・golden の由来と正規化規則は golden JSON 自身の
    ヘッダ＝`note`/`normalization` を参照）。グラフの正しさそのものではなく「移し替えで骨格が
    黙って欠けない」ことの確認——golden 自体は他のテスト（`test_path_identity_duplicates_are_separate_nodes`
    等）が別途 flags==[] を検証済みの入力から採っている。
    """
    import json
    from sherpa.ingest import world_graph
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for key, world_dir, world_id in _GOLDEN_FIXTURES:
        nodes, edges, flags = world_graph.build_world(world_dir, world_id)
        assert flags == [], f"{key}: 移行後に新規 flag が発生（骨格が変化した疑い）: {flags}"
        got = _rich_summary(nodes, edges)
        for field in _GOLDEN_FIELDS:
            assert got[field] == golden[key][field], f"{key}.{field}: golden と不一致"


def test_regenerate_migration_golden_from_base_sha():
    """golden の由来を実行可能な手順として固定する（既定 skip・§7 裁定7＝緩めた一致）。

    `SHERPA_REGEN_CODE1A_GOLDEN=1` で有効化。golden の `base_sha`（`git show`）から当時の
    `world_graph.py` を一時モジュールとしてロードし、fixtures に対する `build_world()` 出力を
    `_rich_summary()` で正規化して現行の golden と比較する——golden の再現性そのものの検証であり、
    fixtures を変えた場合はこのテストの `got` を新しい golden として書き出す（手動反映）。
    既定で重い処理（git show・一時 import）を伴うため CI では動かさない。
    """
    import importlib
    import json
    import os
    import subprocess
    import sys

    if os.environ.get("SHERPA_REGEN_CODE1A_GOLDEN") != "1":
        _todo("既定 skip（SHERPA_REGEN_CODE1A_GOLDEN=1 で有効化・golden 再現性の検証）")
        return
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    base_sha = golden["base_sha"]
    src = subprocess.run(["git", "show", f"{base_sha}:sherpa/ingest/world_graph.py"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    tmp_name = "sherpa.ingest._regen_code1a_golden_tmp"
    tmp_path = ROOT / "sherpa/ingest/_regen_code1a_golden_tmp.py"
    tmp_path.write_text(src, encoding="utf-8")
    try:
        old = importlib.import_module(tmp_name)
        for key, world_dir, world_id in _GOLDEN_FIXTURES:
            nodes, edges, flags = old.build_world(world_dir, world_id)
            assert flags == [], (key, flags)
            got = _rich_summary(nodes, edges)
            for field in _GOLDEN_FIELDS:
                assert got[field] == golden[key][field], \
                    f"{key}.{field}: base_sha={base_sha} 時点の再生成結果が golden と乖離（fixtures 変更後の再生成漏れ疑い）"
    finally:
        tmp_path.unlink(missing_ok=True)
        sys.modules.pop(tmp_name, None)


MENTION_GOLDEN = ROOT / "tests/contract/goldens/mention_edges_v1.json"


def test_mention_edges_golden_and_determinism():
    """言及エッジ（Pass3・辞書突合）専用の golden 検査（rv-s2-mention #7）。

    `_rich_summary()`/`test_analyzer_registry_migration_keeps_graph_set_identical` は骨格の golden
    と混ざらないよう `via=="mention"` を明示的に**除外**している（同関数 docstring 参照）——言及
    エッジ自体の回帰は別に固定していなかったため、ここで `fixtures/corpus/v1` の DOCUMENTS
    (via=mention) の (src,dst) をソート済み全列挙して golden 比較する。あわせて①全端点（src/dst
    双方の cid）がノード集合に実在すること、②2回連続で構築した結果が完全一致すること（決定的＝
    辞書突合が構築順や内部キャッシュに依存しない）も固定する。
    """
    import json
    from sherpa.ingest import world_graph
    nodes1, edges1, flags1 = world_graph.build_world(CORPUS_V1, "v1")
    nodes2, edges2, flags2 = world_graph.build_world(CORPUS_V1, "v1")
    assert flags1 == [] and flags2 == []

    def _mention_pairs(edges):
        return sorted({(e["src"], e["dst"]) for e in edges
                       if e["type"] == "DOCUMENTS" and e.get("via") == "mention"})

    pairs1, pairs2 = _mention_pairs(edges1), _mention_pairs(edges2)
    assert pairs1 == pairs2                               # 2回構築して完全一致（決定的）

    all_cids = {n["cid"] for n in nodes1}
    missing = [p for p in pairs1 if p[0] not in all_cids or p[1] not in all_cids]
    assert not missing, f"言及エッジの端点がノード集合に無い: {missing}"

    golden = json.loads(MENTION_GOLDEN.read_text(encoding="utf-8"))
    assert [list(p) for p in pairs1] == golden["mention_edges"], \
        "言及エッジ (src,dst) の全列挙が golden と不一致（回帰の疑い）"


def test_deleted_doc_yields_no_mention_edges_on_rebuild():
    """鏡＝削除で消える: 言及元 Document（Pass3）は毎回 `files`（現存ファイル一覧）だけを走査する
    ため、削除済み doc は再構築時にそもそも走査されず、その言及エッジも載らない（旧 L 意味層の
    `valid_docs` フィルタと同じ帰結を、キャッシュを持たない都度再構築という構造そのもので満たす）。
    """
    import tempfile
    from sherpa.ingest import world_graph as g
    d = tempfile.mkdtemp()
    base = pathlib.Path(d) / "案件A" / "01_ソース"
    base.mkdir(parents=True)
    (base / "TAXCALC.cbl").write_text(
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. TAXCALC.\nPROCEDURE DIVISION.\nSTOP RUN.\n", encoding="utf-8")
    doc = pathlib.Path(d) / "案件A" / "02_設計" / "spec.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("TAXCALC を使う。\n", encoding="utf-8")

    nodes1, edges1, _flags = g.build_world(pathlib.Path(d), "tmp")
    assert any(e["type"] == "DOCUMENTS" and e.get("via") == "mention" for e in edges1)

    doc.unlink()                                      # 削除＝消える
    nodes2, edges2, _flags = g.build_world(pathlib.Path(d), "tmp")
    assert not any(e["type"] == "DOCUMENTS" and e.get("via") == "mention" for e in edges2)
    assert not any(n["label"] == "Document" for n in nodes2)
