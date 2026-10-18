"""試作: COBOL/JCL から「ファイル（データセット）経由のプログラム間の流れ」と「項目レベルの流れ」を規則で抜き、
現行グラフ（COPY/CALL/EXEC PGM）では届かない影響先がどれだけ増えるかを見る。
使い方: python3 scripts/bench/proto_dataflow.py <world_dir> <起点の項目名>
出す辺: Module -READS/WRITES-> Dataset（SELECT..ASSIGN TO ddname ＋ JCL DD DSN）, DataItem -FLOWS_TO-> DataItem（MOVE/COMPUTE/ADD）,
        Module -USES-> DataItem（手続き部で名前が出る項目）。レコード（01）の下の項目は、その FD のファイルへ流れる。
"""
import re
import sys
from collections import defaultdict, deque
from pathlib import Path

SELECT_RE = re.compile(r"SELECT\s+([\w-]+)\s+ASSIGN\s+TO\s+([\w-]+)", re.I)
FD_RE = re.compile(r"^\s*FD\s+([\w-]+)", re.I)
LEVEL_RE = re.compile(r"^\s*(\d\d)\s+([\w-]+)", re.I)
COPY_RE = re.compile(r"^\s*COPY\s+([\w-]+)", re.I)
MOVE_RE = re.compile(r"\bMOVE\s+([\w-]+)\s+TO\s+([\w-]+)", re.I)
COMPUTE_RE = re.compile(r"\bCOMPUTE\s+([\w-]+)\s*=\s*(.+?)(?:\.|$)", re.I)
ADD_RE = re.compile(r"\bADD\s+([\w-]+)\s+TO\s+([\w-]+)", re.I)
READ_RE = re.compile(r"\bREAD\s+([\w-]+)", re.I)
WRITE_RE = re.compile(r"\bWRITE\s+([\w-]+)(?:\s+FROM\s+([\w-]+))?", re.I)
PGM_RE = re.compile(r"PROGRAM-ID\.\s*([\w-]+)", re.I)
EXEC_RE = re.compile(r"^//(\w+)\s+EXEC\s+PGM=([\w-]+)", re.I)
DD_RE = re.compile(r"^//(\w+)\s+DD\s+.*DSN=([\w.]+)", re.I)
KEYWORDS = {"TO", "FROM", "AT", "END", "UNTIL", "NOT", "INPUT", "OUTPUT", "SPACES", "ZERO", "ZEROS"}


def read(p: Path) -> str:
    b = p.read_bytes()
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("cp932", "replace")


def copybook_items(cpy_dir_files: dict, name: str) -> list:
    text = cpy_dir_files.get(name.upper())
    if text is None:
        return []
    return [m.group(2).upper() for line in text.splitlines() if (m := LEVEL_RE.match(line)) and m.group(1) not in ("66", "88")]


def analyze(world: Path):
    cpys = {p.stem.upper(): read(p) for p in world.rglob("*.cpy")}
    edges = []   # (type, src, dst, where)
    for p in world.rglob("*.cbl"):
        text = read(p)
        pgm = (PGM_RE.search(text) or [None, p.stem])[1].upper()
        dd_of_file = {m.group(1).upper(): m.group(2).upper() for m in SELECT_RE.finditer(text)}
        # FD ごとのレコード項目（01 と COPY の下）
        items_of_file = defaultdict(set)
        cur_fd = None
        in_file_section = False
        for line in text.splitlines():
            up = line.upper()
            if "FILE SECTION" in up:
                in_file_section = True
            if "WORKING-STORAGE" in up or "PROCEDURE DIVISION" in up:
                in_file_section = False
                cur_fd = None
            m = FD_RE.match(line)
            if m:
                cur_fd = m.group(1).upper()
                continue
            if in_file_section and cur_fd:
                m = LEVEL_RE.match(line)
                if m:
                    items_of_file[cur_fd].add(m.group(2).upper())
                m = COPY_RE.match(line)
                if m:
                    items_of_file[cur_fd].update(copybook_items(cpys, m.group(1)))
        # 手続き部の流れ
        proc = text.upper().split("PROCEDURE DIVISION", 1)[-1]
        for m in MOVE_RE.finditer(proc):
            edges.append(("FLOWS_TO", m.group(1), m.group(2), pgm))
        for m in ADD_RE.finditer(proc):
            edges.append(("FLOWS_TO", m.group(1), m.group(2), pgm))
        for m in COMPUTE_RE.finditer(proc):
            for src in set(re.findall(r"[A-Z][\w-]+", m.group(2))) - KEYWORDS:
                edges.append(("FLOWS_TO", src, m.group(1), pgm))
        for m in READ_RE.finditer(proc):
            f = m.group(1)
            if f in dd_of_file:
                edges.append(("READS", pgm, "DD:" + dd_of_file[f], pgm))
                for it in items_of_file.get(f, ()):
                    edges.append(("FLOWS_TO", "DD:" + dd_of_file[f], it, pgm))
        for m in WRITE_RE.finditer(proc):
            rec, frm = m.group(1), m.group(2)
            for f, items in items_of_file.items():
                if rec in items or (frm and frm in items):
                    edges.append(("WRITES", pgm, "DD:" + dd_of_file.get(f, f), pgm))
                    if frm:
                        edges.append(("FLOWS_TO", frm, "DD:" + dd_of_file.get(f, f), pgm))
                    for it in items:
                        edges.append(("FLOWS_TO", it, "DD:" + dd_of_file.get(f, f), pgm))
        # レコード（グループ）の下の項目は、そのグループへ流れる（WRITE ... FROM SALES-REC が項目を運ぶ）
        for cname, ctext in cpys.items():
            if re.search(rf"^\s*COPY\s+{re.escape(cname)}\b", text, re.I | re.M):
                lines = ctext.splitlines()
                groups = [m.group(2).upper() for line in lines if (m := LEVEL_RE.match(line)) and m.group(1) == "01"]
                for g in groups:
                    for it in copybook_items(cpys, cname):
                        if it != g:
                            edges.append(("FLOWS_TO", it, g, pgm))
        for it in set(re.findall(r"[A-Z][\w-]+", proc)) - KEYWORDS:
            edges.append(("USES", pgm, it, pgm))
    # JCL: DD 名 → データセット名で、プログラム間を結ぶ
    for p in world.rglob("*.jcl"):
        step_pgm = None
        for line in read(p).splitlines():
            m = EXEC_RE.match(line)
            if m:
                step_pgm = m.group(2).upper()
                continue
            m = DD_RE.match(line)
            if m and step_pgm:
                edges.append(("DD_IS", f"{step_pgm}/DD:{m.group(1).upper()}", "DSN:" + m.group(2).upper(), p.stem))
    return edges


def main() -> int:
    world, start = Path(sys.argv[1]), sys.argv[2].upper()
    edges = analyze(world)
    # 前向き閉包（起点の項目が流れる先）。DD はプログラム名で修飾して DSN に結び、DSN から別プログラムの DD へ戻す。
    fwd = defaultdict(set)
    dd_dsn = defaultdict(set)
    dsn_dd = defaultdict(set)
    for t, s, d, where in edges:
        if t == "FLOWS_TO":
            fwd[s].add(d)
            if d.startswith("DD:"):
                fwd[d].add(f"{where}/{d}")
            if s.startswith("DD:"):
                fwd[f"{where}/{s}"].add(d)
        if t == "DD_IS":
            dd_dsn[s].add(d)
            dsn_dd[d].add(s)
    for dd, dsns in dd_dsn.items():
        for dsn in dsns:
            fwd[dd].add(dsn)
            for other in dsn_dd[dsn]:
                fwd[dsn].add(other)
    seen = {start}
    dq = deque([start])
    order = []
    while dq:
        x = dq.popleft()
        for y in fwd.get(x, ()):
            if y not in seen:
                seen.add(y)
                order.append(y)
                dq.append(y)
    print(f"起点 {start} の流れ先（前向き閉包）:", " → ".join(order))
    users = sorted({where for t, s, d, where in edges if t == "USES" and d in seen})
    print("流れ先を使うプログラム:", users)
    print("辺の数:", len(edges))
    return 0


if __name__ == "__main__":
    sys.exit(main())
