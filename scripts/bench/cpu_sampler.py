#!/usr/bin/env python3
"""1 ターンの間、どのプロセスが CPU を使っているかを /proc から 1 秒ごとに記録する（Linux・読み取りだけ）。

使い方: python3 scripts/bench/cpu_sampler.py --seconds 1800 --out cpu.jsonl
対象: コマンド名に sherpa / codex / uvicorn / python / rg / java(ES/Neo4j) を含むプロセス。
出力: 1 秒ごとに {ts, procs:[{pid, comm, cpu_pct, rss_mb, threads}], cores_busy}。終了時に cpu_pct の累計をプロセス種別ごとに表示。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

CLK = os.sysconf("SC_CLK_TCK")
KEYS = ("sherpa", "codex", "uvicorn", "python", "rg", "java", "postgres", "neo4j", "elastic")


def _procs():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/stat") as f:
                st = f.read()
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        comm = st[st.index("(") + 1:st.rindex(")")]
        rest = st[st.rindex(")") + 2:].split()
        utime, stime, threads, rss = int(rest[11]), int(rest[12]), int(rest[17]), int(rest[21])
        low = (comm + " " + cmd).lower()
        if not any(k in low for k in KEYS):
            continue
        kind = ("mcp_server" if "sherpa.mcp_server" in cmd else "codex" if "codex" in comm else
                "api" if ("uvicorn" in low or "sherpa.api" in low) else "rg" if comm == "rg" else
                "store(java/pg)" if any(k in low for k in ("java", "postgres")) else "python_other")
        yield int(pid), comm, kind, utime + stime, threads, rss * os.sysconf("SC_PAGE_SIZE") // (1 << 20), cmd[:80]


def main() -> int:
    ap = argparse.ArgumentParser(); ap.add_argument("--seconds", type=int, default=600); ap.add_argument("--out", default="cpu_samples.jsonl")
    a = ap.parse_args()
    prev: dict = {}; totals = defaultdict(float); t_end = time.time() + a.seconds
    with open(a.out, "w") as out:
        while time.time() < t_end:
            t0 = time.time(); rows = []
            for pid, comm, kind, ticks, threads, rss, cmd in _procs():
                p = prev.get(pid)
                if p:
                    pct = (ticks - p[0]) / CLK / max(1e-6, t0 - p[1]) * 100
                    rows.append({"pid": pid, "comm": comm, "kind": kind, "cpu_pct": round(pct, 1), "rss_mb": rss, "threads": threads})
                    totals[kind] += pct / 100
                prev[pid] = (ticks, t0)
            out.write(json.dumps({"ts": t0, "cores_busy": round(sum(r["cpu_pct"] for r in rows) / 100, 2), "procs": rows}, ensure_ascii=False) + "\n"); out.flush()
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    print("CPU 秒の累計（種別ごと）:", {k: round(v, 1) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
