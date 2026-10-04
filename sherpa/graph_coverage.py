"""グラフ読み取りの未完了・打ち切りの申告（`coverage`）。

「関連なし」と「調べきれなかった」を区別するための共通の欄で、Codex の道具（`graph_neighbors`）・原因調査のレンズ
（`lens_service.run_troubleshoot`）・影響レンズ（`impact_service.run_impact`）・外部 API の graph 検索が同じ語彙を使う。
契約: docs/design/interfaces.md「coverage（未完了・打ち切りの申告）」・docs/design/external-api.md（C-EXT-SEARCH-01）。
このモジュールは他のモジュールを import しない（読み取り部品 `parts/read` からも使える）。
"""
from __future__ import annotations

# 打ち切り・未完了の理由（`limits[].kind`）。閉じた集合。
KIND_TIMEOUT = "timeout"                          # Neo4j の問い合わせが時間内に終わらなかった
KIND_ROW_CAP = "row_cap"                          # Neo4j の行数の緊急天井に達した（部分結果）
KIND_DEPTH = "depth"                              # 深さの上限で止まり、先に辺が残る
KIND_RESULT_CAP = "result_cap"                    # `k` や件数で結果を切った
KIND_DOC_SEARCH_TRUNCATED = "doc_search_truncated"  # 文書側の探索（grep）が大きな文書を先頭だけで打ち切った
KIND_CARD_CAP = "card_cap"                        # 近傍カードの件数・バイトの上限で切った
KIND_GRAPH_UNAVAILABLE = "graph_unavailable"
KIND_GRAPH_REINGEST_REQUIRED = "graph_reingest_required"
KIND_PLUGIN_FAILED = "plugin_failed"              # FW プラグインの失敗（段階 2 から使う語）

LIMIT_KINDS = (
    KIND_TIMEOUT, KIND_ROW_CAP, KIND_DEPTH, KIND_RESULT_CAP, KIND_DOC_SEARCH_TRUNCATED, KIND_CARD_CAP,
    KIND_GRAPH_UNAVAILABLE, KIND_GRAPH_REINGEST_REQUIRED, KIND_PLUGIN_FAILED,
)

# 処理の段階（`limits[].stage`・Codex の道具と各レンズだけが付ける。外部 API の `limits` には出さない）。
STAGE_ANCHOR = "anchor"        # 起点の解決（名前 → グラフのノード）
STAGE_NEIGHBORS = "neighbors"  # 近傍の取得（Neo4j の近傍たどり）
STAGE_DOCS = "docs"            # 文書のカード（起点名の grep）
STAGE_CARDS = "cards"          # カードの件数・バイトの上限
STAGE_IMPACT = "impact"        # 影響たどり
STAGE_PRESUMED = "presumed"    # 構造の結果が無いときの資料からの推定（grep）

_OVERLOAD_KIND = {"timeout": KIND_TIMEOUT, "too_many_rows": KIND_ROW_CAP}


def kind_of_overload(reason: str) -> str:
    """`GraphQueryOverloadError.reason`（`timeout`／`too_many_rows`）→ `limits[].kind`。"""
    return _OVERLOAD_KIND.get(reason, KIND_ROW_CAP)


class Coverage:
    """読み取りの途中で見つかった打ち切りを集める入れ物。同じ `(kind, stage)` は 1 件にまとめる。"""

    def __init__(self) -> None:
        self.limits: list[dict] = []

    def add(self, kind: str, stage: str | None = None) -> None:
        if kind not in LIMIT_KINDS:
            raise ValueError(f"unknown coverage kind: {kind}")
        item = {"kind": kind} if stage is None else {"kind": kind, "stage": stage}
        if item not in self.limits:
            self.limits.append(item)

    def has(self, *kinds: str) -> bool:
        return any(lim["kind"] in kinds for lim in self.limits)

    def copy(self) -> "Coverage":
        c = Coverage()
        c.limits = [dict(lim) for lim in self.limits]
        return c

    def as_dict(self, *, omitted: int | None = None, depth: dict | None = None) -> dict:
        """`{complete, limits, omitted, depth?}`。`omitted` は分かるときだけ渡す（打ち切り無しなら 0・分からなければ null）。"""
        complete = not self.limits
        out: dict = {"complete": complete, "limits": [dict(lim) for lim in self.limits],
                     "omitted": 0 if complete else omitted}
        if depth is not None:
            out["depth"] = depth
        return out


def add_limit(coverage: dict, kind: str, stage: str | None = None) -> None:
    """`Coverage.as_dict` 済みの辞書へ打ち切りを 1 件足し、`complete` を落とす（`omitted` は分からないので null にする）。"""
    if kind not in LIMIT_KINDS:
        raise ValueError(f"unknown coverage kind: {kind}")
    item = {"kind": kind} if stage is None else {"kind": kind, "stage": stage}
    if item not in coverage["limits"]:
        coverage["limits"].append(item)
    coverage["complete"] = False
    if coverage.get("omitted") == 0:
        coverage["omitted"] = None
