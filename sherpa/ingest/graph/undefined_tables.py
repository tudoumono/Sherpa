"""定義（`CREATE TABLE`）が資料フォルダに無い表の参照を、トップフォルダごとの `Table` のノードにつなぐ。
ソースの中の SQL が書いた表を、定義が無くても「この表を使うソース」として辿れるようにする。ノードの `path` はトップフォルダ（同一性＝トップフォルダ内の表の名前）、`membership_paths` は参照元のソース（範囲の判定と代表のパスに使う）。
設計: docs/05-グラフ語彙.md（Table）・docs/03-鏡モデル.md（リンクは同じトップフォルダの中だけ）
"""
from __future__ import annotations

from .select import top_folder
from .state import WorldBuild, make_node

RULE = "undefined_table"
VALUE = "undefined"   # ノードの `value`（定義の無い表の印）
VIAS = frozenset({"exec_sql", "string_sql"})   # ソースに埋め込んだ SQL の参照だけ（XML の SQL・VBA の SQL は今まで通り未解決の申告）


def undefined_table(b: WorldBuild, name, ref_rel: str):
    """定義の無い表のノードを（無ければ）作り、`(path, cid の名前)` を返す。トップフォルダの外のファイルからの参照は作らず None。"""
    top = top_folder(ref_rel)
    simple = getattr(name, "simple", name)
    if top is None or not simple:
        return None
    schema = getattr(name, "schema", None)
    key = f"{schema}.{simple}" if schema else simple   # schema の違う表は別のノード（定義のある表の cid_key と同じ形）
    node = make_node("Table", b.world_id, top, key, value=VALUE)
    node["name"] = simple
    if schema:
        node["schema"], node["qualified_name"] = schema, key
    node = b.nodes.setdefault(node["cid"], node)
    members = node.setdefault("membership_paths", [])   # 範囲の検索は参照元のどれかが範囲内なら範囲内にする
    if ref_rel not in members:
        members.append(ref_rel)
    return top, key
