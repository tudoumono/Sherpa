"""データの流れの段（Pass 2 の後・集約の前）: JCL の DD 文と COBOL のファイル入出力、COPY のコピーブック項目の使用を結ぶ。

設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from collections import defaultdict

from .aggregate import EDGE_SOURCES_MAX
from .select import top_folder
from .state import WorldBuild, make_node


USE_AMBIGUOUS_NAMES_MAX = 5   # `use_ambiguous` の申告に載せる名前の上限

def _dataset_value(bindings: list) -> str | None:
    """`Dataset.value`: 相対世代を使っていれば `gdg=true`、`DISP` の先頭が `NEW`／`MOD` なら `disp=…`（参考値）。"""
    parts = []
    if any(b.gdg_rel for _rel, b in bindings):
        parts.append("gdg=true")
    disps = sorted({b.disp.split(",")[0] for _rel, b in bindings if b.disp.split(",")[0] in ("NEW", "MOD")})
    if disps:
        parts.append("disp=" + ",".join(disps))
    return " ".join(parts) or None


def _dd_evidence(b) -> str:
    """JCL の DD 文の要約（根拠の `evidence_text`・GDG の相対世代を残す）。"""
    dsn = f"{b.dsn}({b.gdg_rel})" if b.gdg_rel else b.dsn
    return f"DD {b.ddname} DSN={dsn}" + (f" DISP={b.disp}" if b.disp else "")


def _copy_item_flows(nodes: dict, link_edges: list, names_by_rel: dict, raw: list, flags: list) -> None:
    """③ 手続き部の名前を、その Module が COPY（入れ子を含む）で取り込んだコピーブックの項目名に突き合わせ、`DataItem -FLOWS_TO(use)-> Module` を `raw` に足す。"""
    copies_of: dict = defaultdict(set)
    for e in link_edges:
        if e["type"] == "COPIES":
            copies_of[nodes[e["src"]]["path"]].add(nodes[e["dst"]]["path"])
    items_by_rel: dict = defaultdict(lambda: defaultdict(list))
    for n in nodes.values():
        if n["label"] == "DataItem":
            items_by_rel[n["path"]][n["name"]].append(n["cid"])
    for rel in sorted(names_by_rel):
        module_cid, names = names_by_rel[rel]
        closure, todo = set(), list(copies_of.get(rel, ()))
        while todo:
            cb = todo.pop()
            if cb not in closure:
                closure.add(cb)
                todo.extend(copies_of.get(cb, ()))
        ambiguous: list = []
        for name, line in sorted(names.items(), key=lambda kv: (kv[1], kv[0])):
            hits = [c for cb in sorted(closure) for c in items_by_rel.get(cb, {}).get(name, ())]
            if len(hits) == 1:
                raw.append({"type": "FLOWS_TO", "src": hits[0], "dst": module_cid, "doc": rel, "line": line,
                            "status": "active", "via": "use", "_rule": "copy_item_name",
                            "_from_def": {"file": rel, "key": None}})
            elif hits:
                ambiguous.append(name)
        if ambiguous:
            flags.append({"reason": "use_ambiguous", "from": rel, "count": len(ambiguous),
                          "names": ambiguous[:USE_AMBIGUOUS_NAMES_MAX]})


def dataflow_pass(world_id: str, nodes: dict, link_edges: list, io_by_rel: dict, dd_by_rel: dict, names_by_rel: dict):
    """データの流れの段（Pass 2 の後・集約の前）。`(新しい節点, 流れの辺の候補, flags, 未解決の申告)` を返す。

    ① JCL の `EXEC PGM=` の解決済みの `INVOKES` と `DdBinding` を突き合わせ、Module ごとの DD 名 → DSN の表を作る
    ② COBOL の `FileIo` の DD 名を引き、`Module -FLOWS_TO(write_file)-> Dataset`／`Dataset -FLOWS_TO(read_file)-> Module` を張る（結べない DD 名は未解決として申告）
    ③ 手続き部の名前を、その Module が COPY（入れ子を含む）で取り込んだコピーブックの項目名に突き合わせ、`DataItem -FLOWS_TO(use)-> Module` を張る
    `Dataset` の同一性は（世代, DSN）。一時データセット `&&NAME` は同じ JCL ファイルの中だけ。
    """
    new_nodes: dict = {}
    raw: list = []
    flags: list = []
    unresolved: dict = {}

    exec_target: dict = {}     # (JCL の rel, EXEC の行) -> 呼び出し先 Module の rel
    for e in link_edges:
        if e["type"] == "INVOKES" and e["doc"] in dd_by_rel:
            dst = nodes.get(e["dst"])
            if dst is not None and dst["label"] == "Module":
                exec_target[(e["doc"], e["line"])] = dst["path"]

    dd_table: dict = defaultdict(lambda: defaultdict(list))    # Module の rel -> DD 名 -> [(JCL の rel, DdBinding)]
    dsn_bindings: dict = defaultdict(list)                     # Dataset の鍵 -> [(JCL の rel, DdBinding)]（全 DD 文）
    for jcl_rel in sorted(dd_by_rel):
        for b in dd_by_rel[jcl_rel]:
            dsn_bindings[(top_folder(jcl_rel), f"{jcl_rel}:{b.dsn}" if b.dsn.startswith("&&") else b.dsn)].append((jcl_rel, b))
            module_rel = exec_target.get((jcl_rel, b.exec_line))
            if module_rel is not None:
                dd_table[module_rel][b.ddname].append((jcl_rel, b))

    def _dataset(jcl_rel: str, dsn: str) -> str:
        key = (top_folder(jcl_rel), f"{jcl_rel}:{dsn}" if dsn.startswith("&&") else dsn)
        cid = f"dataset:{world_id}:{key[0] or ''}#{key[1]}"
        if cid in new_nodes:
            return cid
        bindings = sorted(dsn_bindings[key], key=lambda x: (x[0], x[1].line))
        node = {**make_node("Dataset", world_id, bindings[0][0], dsn, value=_dataset_value(bindings)),
                "cid": cid, "analyzer": "jcl", "membership_paths": sorted({r for r, _b in bindings})}
        srcs = [{"doc_id": r, "file": r, "line": b.line, "evidence_text": _dd_evidence(b)} for r, b in bindings]
        node["sources"] = srcs[:EDGE_SOURCES_MAX]
        if len(srcs) > EDGE_SOURCES_MAX:
            node["sources_overflow_count"] = len(srcs) - EDGE_SOURCES_MAX
        new_nodes[cid] = node
        return cid

    def _flow(src, dst, via, doc, line, binding_path, evidence=None):
        e = {"type": "FLOWS_TO", "src": src, "dst": dst, "doc": doc, "line": line, "status": "active", "via": via,
             "_rule": "dd_name_match", "_from_def": {"file": doc, "key": None}, "_binding_path": binding_path}
        if evidence:
            e["_evidence_text"] = evidence
        raw.append(e)

    for rel in sorted(io_by_rel):
        module_cid, file_io = io_by_rel[rel]
        for fio in file_io:
            if fio.assign_kind != "ddname":
                continue
            bound = dd_table.get(rel, {}).get(fio.ddname)
            if not bound:
                via = "read_file" if fio.mode == "r" else "write_file"
                flags.append({"reason": "unresolved", "from": rel, "kind": "Dataset", "name": fio.ddname,
                              "line": fio.line, "via": via})
                unresolved.setdefault(rel, []).append({
                    "line": fio.line, "reason": "unresolved", "kind": "Dataset", "name": fio.ddname, "via": via,
                    "from_def": {"file": rel, "key": None}})
                continue
            for jcl_rel, b in bound:
                ds_cid = _dataset(jcl_rel, b.dsn)
                if fio.mode in ("w", "rw"):
                    _flow(module_cid, ds_cid, "write_file", rel, fio.line, jcl_rel)
                    _flow(module_cid, ds_cid, "write_file", jcl_rel, b.line, jcl_rel, _dd_evidence(b))
                if fio.mode in ("r", "rw"):
                    _flow(ds_cid, module_cid, "read_file", rel, fio.line, jcl_rel)
                    _flow(ds_cid, module_cid, "read_file", jcl_rel, b.line, jcl_rel, _dd_evidence(b))

    if names_by_rel:
        _copy_item_flows(nodes, link_edges, names_by_rel, raw, flags)
    return new_nodes, raw, flags, unresolved


def add_dataflow(b: WorldBuild) -> None:
    """データの流れの節点・辺の候補・申告を、集約の前に作業領域へ足す。"""
    flow_nodes, flow_edges, flow_flags, flow_unresolved = dataflow_pass(
        b.world_id, b.nodes, b.link_edges, b.io_by_rel, b.dd_by_rel, b.names_by_rel)
    b.nodes.update(flow_nodes)
    b.link_edges.extend(flow_edges)
    b.flags.extend(flow_flags)
    for rel, items in flow_unresolved.items():
        b.unresolved_by_rel.setdefault(rel, []).extend(items)
        b.unresolved_subject[rel] = b.io_by_rel[rel][0]
