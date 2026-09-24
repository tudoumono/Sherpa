"""鏡モデルのテスト用ヘルパ（fixtures/corpus/v1 のデータを Neo4j にロード）。test_ 接頭辞でないので runner の対象外。

**世界 ID は 'v1' でなく専用 ID `TEST_WORLD_ID`（既定 'pytest-v1'・env `SHERPA_TEST_WORLD_ID` で上書き可）を
使う**（2026-07-03 インシデント対応 HIGH#2）: 実 Neo4j/ES は共有のため、固定名 'v1' が将来どこかの実登録
world と衝突すると `reconcile_derivatives`/セッション終了クリーンアップが実データを誤って巻き込みうる。
fixture データ源（`fixtures/corpus/v1`）自体は変えず、`SHERPA_TEST_WORLD_ID` により
`sherpa.worlds.world_dir()` が `TEST_WORLD_ID` を `fixtures/corpus/v1` へ写像する（`sherpa/worlds.py`
参照）。**env を明示すれば別 world id（例 `pytest-<lane>`）を使える**——`scripts/gate-lane.sh`／
`scripts/gate-integration.sh` はこれでレーンごとに Neo4j/ES の world を分離し、複数レーンの真の並走を
成立させる。
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")   # テストは実埋め込み API を叩かない（ES は BM25 のみ）

# env が無ければ既定 'pytest-v1'（挙動不変）。明示されていれば呼び出し元（gate スクリプト等）の
# 指定を使う——setdefault ではなく明示代入にして、既定値を使った場合も env に確定させる
# （worlds.world_dir() は os.environ を直接読むため、この行が唯一の真実源になる）。
TEST_WORLD_ID = os.environ.get("SHERPA_TEST_WORLD_ID", "pytest-v1")
if not TEST_WORLD_ID.startswith("pytest-"):
    # テスト専用 namespace に限定する（実 world との衝突防止・下の ensure_v1() の base registry
    # 照会と二重の防御）。env の誤設定で実 world をテストの書込み対象にする事故を構造的に防ぐ。
    raise RuntimeError(
        f"SHERPA_TEST_WORLD_ID={TEST_WORLD_ID!r} は 'pytest-' 接頭辞のみ許可します"
        "（テスト専用 namespace・実 world との衝突を防ぐため）。"
    )
os.environ["SHERPA_TEST_WORLD_ID"] = TEST_WORLD_ID   # worlds.world_dir() の fixtures エイリアス用

# テストの頭脳は heuristic に固定（/chat を叩くテストが、利用者の保存した agent=codex 等で
# 本物の codex/外部LLM を起動して固まるのを防ぐ）。実ユーザ設定はスナップショットし atexit で復元。
import atexit  # noqa: E402

try:
    from sherpa import store as _store

    _ORIG_SETTINGS = _store.get_settings()

    def _restore_settings():
        f = {}
        for k in _store._SETTINGS_FIELDS:
            v = _ORIG_SETTINGS.get(k)
            if k in ("openai_api_key", "gemini_api_key"):
                f[k] = v if v else ""                    # None→""（クリア）で元の状態に一致
            elif v is not None:
                f[k] = v
        try:
            _store.update_settings(**f)
        except Exception:
            pass

    atexit.register(_restore_settings)
    _store.update_settings(agent="heuristic")
except Exception:
    pass

# v1 world の代表 rel_path（doc_id＝パス・MIRROR §2.2）。テストで使い回す。
TAXCALC = "4期/03_開発/01_ソース/TAXCALC.cbl"
BILLGEN = "4期/03_開発/01_ソース/BILLGEN.cbl"
TAXCPY = "4期/00_共通/標準コピーブック/TAX-CPY.cpy"
SPEC = "4期/02_設計/01_基本設計/税計算仕様書.md"
OPS = "4期/02_保守/03_運用手順/締めバッチ運用手順.md"
NIGHTLY_CID = f"batch:{TEST_WORLD_ID}:4期/03_開発/01_ソース/NIGHTLY.jcl#NIGHTLY"
# 範囲 prefix（フォルダ）
S_SRC = "4期/03_開発/01_ソース"
S_DESIGN = "4期/02_設計/01_基本設計"
S_OPS = "4期/02_保守"


def driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
        notifications_min_severity="OFF",
    )


def ensure_v1():
    """fixtures/corpus/v1 のデータを `TEST_WORLD_ID`（pytest-v1）として Neo4j に world クリーン rebuild
    （要 Neo4j・冪等）。関数名は既存の import 箇所を壊さないため `ensure_v1` のまま維持している
    （実体の world_id は 'v1' ではない・上のモジュール docstring 参照）。

    文書台帳（Postgres `documents`）も `worker._ledger_rows`＋`store.replace_documents` で同じ内容
    へ揃える——`/documents/download` は台帳の正準一致確認を持つため（別名/列挙不能ディレクトリ
    対策）、台帳が空のままだと本関数が用意した実ファイルも DL できない。
    """
    from sherpa import store, worlds
    from sherpa.ingest import worker, world_graph, world_neo4j
    from _world_registry import _is_protected, _query_real_registered_world_ids, register_test_world
    # load_world()（Neo4j への破壊的 DETACH DELETE＋書込み）の**前**に base registry を照会し、
    # SHERPA_TEST_WORLD_ID が実登録 world と同名なら明示エラーで停止する（'pytest-' 接頭辞の
    # 強制と合わせた二重防御・2026-07-03 インシデント対応 HIGH#2 の再発防止）。
    # 照会結果 None（SHERPA_ORIG_PG_DSN 未設定・接続不可・SELECT 失敗のいずれか）は
    # `_is_protected` に渡すと denylist 止まりの fail-safe になる（_world_registry.py の
    # register/cleanup 用途ではそれでよい設計）が、ここでは「確認できない」を「安全」として
    # 進めず fail-closed で停止する——env 注入で任意の world_id を受け付けるようになった以上、
    # 実 world との衝突を検証できないまま Neo4j への破壊的書込みへ進んではならない。
    real_ids = _query_real_registered_world_ids()
    if real_ids is None:
        raise RuntimeError(
            "base registry（実 world 一覧）を照会できませんでした"
            "（SHERPA_ORIG_PG_DSN 未設定・接続不可・クエリ失敗のいずれか）。"
            f"TEST_WORLD_ID={TEST_WORLD_ID!r} が実登録 world と衝突していないか確認できないため、"
            "Neo4j への書込みを行わず停止します。"
        )
    if _is_protected(TEST_WORLD_ID, real_ids):
        raise RuntimeError(
            f"world_id {TEST_WORLD_ID!r} は実登録 world として保護されています"
            "（denylist または base registry 一致）。SHERPA_TEST_WORLD_ID の値を確認してください"
            "（実 world の Neo4j データを誤って上書きする事故を防ぐため停止します）。"
        )
    wd = worlds.world_dir(TEST_WORLD_ID)              # エイリアス経由で fixtures/corpus/v1 を解決
    # S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出・REALIZES 橋は撤去済み
    # ＝`build_world` は骨格（Pass1/Pass2）＋言及エッジ（Pass3）のみを構築する。
    nodes, edges, _flags = world_graph.build_world(wd, TEST_WORLD_ID)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, TEST_WORLD_ID, env["uri"], env["user"], env["pw"])
    # Neo4j 反映**直後**（PG 台帳書込より前）に登録する——ここで例外が飛ぶと Neo4j にはもう
    # 書き込み済みなのに登録簿に無いまま関数が抜け、session teardown の一括削除がその Neo4j
    # 残骸を拾えなくなる（PG 障害時の孤立データ漏れ防止）。
    register_test_world(TEST_WORLD_ID)   # セッション終了時にまとめて削除（tests/_world_registry.py）
    store.replace_documents(TEST_WORLD_ID, worker._ledger_rows(TEST_WORLD_ID))
