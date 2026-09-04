"""根拠文書の解決（鏡モデル・DL は**パス基準**・MIRROR-MODEL §2.2/§4）。

evidence の `doc`＝**rel_path**（world root 相対）→ 原本 Path に解決する。旧 basename 台帳・version 別 md/src は撤去。
同名でも別パスを取り違えない（`world_graph.resolve_path`＝root 配下への直接解決・symlink 混入と`..`/絶対は拒否）。
特定テーマの名前はコードに持たない。
"""
from __future__ import annotations

import os

from . import scope_infer, worlds
from .ingest import importance
from .ingest.world_graph import resolve_path


def resolve(rel: str, world: str | None = None):
    """rel_path（world root 相対）→ 原本 Path（無ければ None）。

    `world` 省略時は env `SHERPA_VERSION`（既定 world）。トラバーサル/絶対/未実在/重要度設定
    ファイル自体は None（§5 の原本DL入口）。

    filesystem のみで完結する（DB 不要・`tests/unit` の「外部サービス不要」契約を保つ——
    `agentic_search.verify_doc_exists`（世界を問わずスコープ制限付きの citation 検証）や
    `corpus_docs`/`doc_ledger` の各種一覧はここを介して DB 非依存のまま動く）。
    文書台帳との正準一致確認（別名/列挙不能ディレクトリ対策）は `/documents/download`
    エンドポイント（`sherpa/routers/documents.py`）側だけの責務とし、本関数自体には
    持ち込まない——DB 到達に依存させると `tests/unit` が要求する DB 非依存性が壊れるため。
    """
    if importance.is_importance_control_path(rel):
        return None
    world = world or os.environ.get("SHERPA_VERSION") or worlds.default_world()
    wd = worlds.world_dir(world)
    if not wd:
        return None
    return resolve_path(wd, rel)


def world_rel_set(world: str | None = None, root=None, strict: bool = False, *,
                  deadline: float | None = None) -> set:
    """world 内の**実在 rel_path 集合**（`safe_files` を1回だけ走査）。

    ES ヒット等の実在チェックを per-hit のツリー走査にしないための batch 版（rv MED）。`resolve_path` の
    実在集合と同じ（`..`/絶対は安全な doc_id なら現れない＝membership で十分）。world 未解決は空集合。

    `root`（省略可）: 呼び出し側が strict resolver で既に world root を解決済みなら渡す——
    渡された場合は `worlds.world_dir()` を再度呼ばない（preflight 後の再解決を避ける）。
    `strict`: `scope_infer.safe_files` へそのまま渡す（外部 API 経路は OSError を re-raise させる）。
    `deadline`（省略可・キーワード専用・既定 None＝無期限＝既存呼び出し元は無変更）:
    `scope_infer.safe_files` へそのまま転送する（PART-4 の `agentic_search.run_tool` の
    `es_search` 分岐が残り時間ベースで渡す・超過時は `scope_infer.ScopeWalkDeadlineExceeded`
    を送出）。
    """
    wd = root
    if wd is None:
        world = world or os.environ.get("SHERPA_VERSION") or worlds.default_world()
        wd = worlds.world_dir(world)
    if not wd:
        return set()
    return {r for _rp, r in scope_infer.safe_files(wd, strict=strict, deadline=deadline)
           if not importance.is_importance_control_path(r)}
