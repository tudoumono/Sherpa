"""合成コーパス生成器: COBOL/コピーブック/JCL/設計書MD を既知の依存グラフで生成し、正解（影響範囲）を gold.json に書く。
実環境の固有名は一切使わない（架空の名前のみ）。
"""
import argparse, json, random, os
from pathlib import Path

JA_NOISE = ["本機能は月次の締め処理で使用する。", "入力ファイルは前日分の取引明細である。", "異常終了時は運用手順書に従い再実行する。",
            "項目の桁数は共通コピーブックの定義に従う。", "消費税率は税率マスタから取得する。", "出力帳票は翌営業日に配信する。",
            "手数料の計算は代理店区分ごとに行う。", "この処理は夜間バッチから起動される。", "変更時は関連プログラムの影響を確認すること。",
            "入力チェックでエラーの場合は該当行をスキップする。", "ログには処理件数とエラー件数を出力する。", "マスタの更新は日次で行う。"]
COBOL_NOISE = ["           MOVE ZERO TO WS-COUNT.", "           PERFORM 1000-INIT.", "           IF WS-EOF = 'Y' GO TO 9000-END.",
               "           ADD 1 TO WS-COUNT.", "           DISPLAY 'PROCESSING' WS-COUNT.", "           READ IN-FILE AT END MOVE 'Y' TO WS-EOF.",
               "           WRITE OUT-REC.", "           PERFORM 2000-MAIN UNTIL WS-EOF = 'Y'.", "           COMPUTE WS-TOTAL = WS-TOTAL + WS-AMT.",
               "           EVALUATE WS-KBN", "             WHEN '1' PERFORM 3100-TYPE1", "             WHEN OTHER PERFORM 3900-OTHER", "           END-EVALUATE."]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--programs", type=int, default=300)
    ap.add_argument("--copybooks", type=int, default=60); ap.add_argument("--jobs", type=int, default=60)
    ap.add_argument("--docs", type=int, default=200); ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pgm-lines", type=int, default=400); ap.add_argument("--doc-lines", type=int, default=300)
    ap.add_argument("--questions", type=int, default=40)
    a = ap.parse_args(); rnd = random.Random(a.seed)
    out = Path(a.out); sysname = "架空システム"
    d_cpy = out / sysname / "00_共通" / "コピーブック"; d_src = out / sysname / "03_開発" / "01_ソース"
    d_jcl = out / sysname / "03_開発" / "02_JCL"; d_doc = out / sysname / "02_設計" / "01_基本設計"
    for d in (d_cpy, d_src, d_jcl, d_doc):
        d.mkdir(parents=True, exist_ok=True)
    cpys = [f"CP{i:04d}" for i in range(a.copybooks)]
    items = {c: [f"{c}-ITEM-{k:02d}" for k in range(rnd.randint(5, 30))] for c in cpys}
    all_items = [it for c in cpys for it in items[c]]
    pgms = [f"PG{i:05d}" for i in range(a.programs)]
    copies = {p: rnd.sample(cpys, k=min(len(cpys), rnd.randint(1, 4))) for p in pgms}
    calls = {}
    for p in pgms:
        k = rnd.choice([0, 0, 1, 1, 2, 3])
        calls[p] = [q for q in rnd.sample(pgms, k=min(k, len(pgms) - 1)) if q != p]
    jobs = [f"JB{i:04d}" for i in range(a.jobs)]
    execs = {j: rnd.sample(pgms, k=min(len(pgms), rnd.randint(1, 4))) for j in jobs}
    # 設計書: 各プログラム or コピーブックを「主題」にする
    doc_subjects = []
    for i in range(a.docs):
        doc_subjects.append(("pgm", rnd.choice(pgms)) if rnd.random() < 0.8 else ("cpy", rnd.choice(cpys)))
    # ---- 書き出し ----
    def w(path, text, enc="utf-8"):
        path.write_bytes(text.encode(enc, errors="replace"))
    for c in cpys:
        lines = [f"      * {c} 共通データ定義", f"       01  {c}-REC."]
        for it in items[c]:
            pic = rnd.choice(["PIC X(10)", "PIC 9(7)V99", "PIC S9(9) COMP-3", "PIC X(1)"])
            lines.append(f"           05  {it:<28} {pic}.")
        w(d_cpy / f"{c}.cpy", "\n".join(lines) + "\n", enc=rnd.choice(["utf-8", "cp932"]))
    decoy_mentions = {}  # pgm -> [item] コメントだけで言及（依存ではない）
    for p in pgms:
        lines = ["       IDENTIFICATION DIVISION.", f"       PROGRAM-ID. {p}.", "       ENVIRONMENT DIVISION.", "       DATA DIVISION.",
                 "       WORKING-STORAGE SECTION."]
        for c in copies[p]:
            lines.append(f"           COPY {c}.")
        lines += ["       01  WS-COUNT PIC 9(7).", "       01  WS-EOF   PIC X.", "       PROCEDURE DIVISION."]
        used_items = [it for c in copies[p] for it in items[c]]
        # 現実の COBOL と同じく、COPY していても一部の項目しか参照しないプログラムを混ぜる（レイアウト変更の影響は参照の有無と無関係）
        if rnd.random() < 0.5:
            used_items = rnd.sample(used_items, k=max(1, len(used_items) // 4))
        decoys = rnd.sample(all_items, k=2)
        decoy_mentions[p] = decoys
        body = []
        while len(body) < a.pgm_lines:
            r = rnd.random()
            if r < 0.15 and used_items:
                body.append(f"           MOVE {rnd.choice(used_items)} TO WS-COUNT.")
            elif r < 0.18 and calls[p]:
                body.append(f"           CALL '{rnd.choice(calls[p])}' USING WS-COUNT.")
            elif r < 0.20:
                body.append(f"      * 参考: {rnd.choice(decoys)} は別機能の項目（本プログラムでは未使用）")
            else:
                body.append(rnd.choice(COBOL_NOISE))
        # 呼出先は必ず 1 回以上 CALL 文として出す（正解＝テキストに実在する依存だけにする）
        for q in calls[p]:
            if not any(f"CALL '{q}'" in l for l in body):
                body.append(f"           CALL '{q}' USING WS-COUNT.")
        lines += body + ["       9000-END.", "           STOP RUN."]
        w(d_src / f"{p}.cbl", "\n".join(lines) + "\n", enc=rnd.choice(["utf-8", "cp932", "cp932"]))
    for j in jobs:
        lines = [f"//{j}     JOB (ACCT),'NIGHTLY',CLASS=A"]
        for n, p in enumerate(execs[j], 1):
            lines += [f"//STEP{n:02d}   EXEC PGM={p}", "//SYSOUT   DD SYSOUT=*", "//IN       DD DSN=IN.DATA,DISP=SHR"]
        w(d_jcl / f"{j}.jcl", "\n".join(lines) + "\n")
    doc_index = {}
    for i, (kind, subj) in enumerate(doc_subjects):
        name = f"機能設計書_{i:05d}.md"
        lines = [f"# {('プログラム' if kind=='pgm' else 'コピーブック')} {subj} 機能設計書", "", "## 1. 概要", ""]
        lines += [rnd.choice(JA_NOISE) for _ in range(8)]
        lines += ["", "## 2. 入出力項目", "", "| 項目 | 桁数 | 説明 |", "|---|---|---|"]
        if kind == "pgm":
            its = [it for c in copies[subj] for it in items[c]]
        else:
            its = items[subj]
        for it in rnd.sample(its, k=min(len(its), 12)):
            lines.append(f"| {it} | 9(7) | {rnd.choice(JA_NOISE)} |")
        lines += ["", "## 3. 処理概要", ""]
        if kind == "pgm":
            for q in calls[subj]:
                lines.append(f"- 下位プログラム {q} を呼び出す。")
            for c in copies[subj]:
                lines.append(f"- 共通定義 {c} を使用する。")
        lines += ["", "## 4. 補足", ""]
        while len(lines) < a.doc_lines:
            lines.append(rnd.choice(JA_NOISE) + rnd.choice(JA_NOISE))
        w(d_doc / name, "\n".join(lines) + "\n")
        doc_index[name] = (kind, subj)
    # ---- 正解（影響範囲）: 項目 X ∈ コピーブック C を変更 ----
    callers = {p: set() for p in pgms}
    for p, qs in calls.items():
        for q in qs:
            callers[q].add(p)
    def closure(start_pgms):
        seen = set(start_pgms); stack = list(start_pgms)
        while stack:
            x = stack.pop()
            for y in callers[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        return seen
    def rel(d, f):
        return str((d / f).relative_to(out)).replace(os.sep, "/")
    questions = []
    for c in rnd.sample(cpys, k=min(a.questions, len(cpys))):
        x = rnd.choice(items[c])
        direct = sorted(p for p in pgms if c in copies[p])
        trans = closure(direct)
        indirect = sorted(trans - set(direct))
        jobs_hit = sorted(j for j in jobs if any(p in trans for p in execs[j]))
        docs_hit = sorted(n for n, (k, s) in doc_index.items() if (k == "cpy" and s == c) or (k == "pgm" and s in trans))
        grep_visible_pgms = sorted(p for p in pgms if p in direct or x in decoy_mentions[p])
        questions.append({
            "id": f"impact-{c}-{x}", "kind": "impact", "question": f"コピーブック {c} の項目 {x} の桁数を変更した場合の影響範囲（プログラム・ジョブ・設計書）を全件挙げてください。",
            "start": {"copybook": rel(d_cpy, f"{c}.cpy"), "item": x},
            "gold": {"programs_direct": [rel(d_src, f"{p}.cbl") for p in direct],
                     "programs_indirect": [rel(d_src, f"{p}.cbl") for p in indirect],
                     "jobs": [rel(d_jcl, f"{j}.jcl") for j in jobs_hit],
                     "docs": [rel(d_doc, n) for n in docs_hit]},
            "grep_literal_hits": {"programs": [rel(d_src, f"{p}.cbl") for p in grep_visible_pgms],
                                  "decoy_programs": [rel(d_src, f"{p}.cbl") for p in pgms if x in decoy_mentions[p] and p not in direct]},
        })
    (out / "gold.json").write_text(json.dumps({"questions": questions, "stats": {"programs": len(pgms), "copybooks": len(cpys), "jobs": len(jobs), "docs": len(doc_subjects)}}, ensure_ascii=False, indent=1), encoding="utf-8")
    n = sum(1 for _ in out.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"generated files={n} bytes={size/1e6:.1f}MB questions={len(questions)} at {out}")

if __name__ == "__main__":
    main()
