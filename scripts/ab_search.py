#!/usr/bin/env python3
"""rag.md 方式（B）と人間向け表MD方式（A）の検索比較ツール（AB1・standalone・裁定2026-09-03）。

利用者（管理者）が「rag.md の方が検索に効く」を自分の文書と自分のクエリで**体験**するための
使い捨てラボツール。`Makefile` には繋がない（単体実行のみ）。

やること:
  1. 指定した Office ファイル（複数可）／ディレクトリを一時ディレクトリへ**コピー**し、
     `sherpa.ingest.office_md.build_derived` で両表現（人間向け `{rel}.md` と RAG 正本
     `{rel}.rag.md`）を生成する。**入力元には一切書き込まない**（本番 world を指定しても
     READ-ONLY を破らない・コピー先は tempfile 管理の使い捨てディレクトリ）。
  2. A＝人間向け MD を見出し（`##`/`###`）単位でチャンク化、B＝rag.md を D1 アンカー
     （`<!-- chunk:{chunk_id} -->`）単位でチャンク化する。
  3. 実行中の Elasticsearch（`sherpa.es_index._url()` と同じ解決＝`ES_URL`／`SHERPA_ES_PORT`）に
     `abtest_` プレフィクスの一時 index を A/B 用に1本ずつ作る（kuromoji 形態素・無ければ
     standard へ縮退）。**費用ゼロ**（既定は BM25 のみ・埋め込みは呼ばない）。
  4. 一時 index へチャンクを投入し、指定クエリ（`-q` 複数可・省略時は対話入力）ごとに A/B の
     上位 N 件（既定 3・`-k` で可変）を並記する。**どちらが良いかの判断はしない**（並べるだけ）。
  5. 終了時（正常終了・エラー・Ctrl-C いずれでも）一時 index を必ず削除する。

注意:
  - ES が起動していない場合は接続案内を出して終了する（トレースバックは見せない）。
  - `--vector` で埋め込み込みのハイブリッド比較ができるが、既定は OFF（費用ゼロ優先）。
    埋め込み設定（`sherpa.embeddings.cfg()`）が解決できない場合は自動で BM25 のみへ縮退する。
  - 一時ディレクトリ（変換結果・一時 index）はこのプロセス終了時に破棄される
    （成果物として保存はしない＝ラボツール）。

使用例:
    .venv/bin/python scripts/ab_search.py fixtures/eval/deprecation_markers/inputs/DEP-XLSX-MARKERS.xlsx \\
        -q "旧税率はいくつ"
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import string
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sherpa import es_index                     # noqa: E402  URL解決のみ再利用（実装は自前・私的関数への深入りはしない）
from sherpa.ingest import office_md              # noqa: E402  build_derived と派生3層の兄弟ディレクトリ規約のみ再利用

_INDEX_PREFIX = "abtest_"
_DEFAULT_K = 3
_SNIPPET_MAX_CHARS = 240
_ES_TIMEOUT = 30
_ES_AVAILABLE_TIMEOUT = 5

# rag.md（D1・RAG正本）のチャンクアンカー契約。`es_index._RAG_MD_CHUNK_ANCHOR_RE` と同一形式
# （フォーマットの定義源は `evidence_render` の renderer 契約）。本スクリプトは touch 禁止の
# `sherpa/**` へは書けない私的関数を import せず、narrow な正規表現だけを自前で複製する
# （`docs/17-開発の教訓.md` 相当の判断＝private member への結合を避ける）。
_ANCHOR_RE = re.compile(r"^<!-- chunk:(\S+) -->\r?\n", re.MULTILINE)
# 人間向け MD の見出し単位チャンク境界（`##`/`###` のみ・`#` 単独や `####` 以下は境界にしない）。
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$", re.MULTILINE)


def _random_suffix(n: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ===== 入力ステージング（READ-ONLY 厳守: コピーのみ・入力元へは一切書かない） =====

def _stage_inputs(inputs: list[str], staging_src: Path) -> None:
    """指定パス（ファイル／ディレクトリ、複数可）を `staging_src` 配下へコピーする。

    各入力を `staging_src/{NNN}/` という専用の連番サブディレクトリへ隔離するため、同名ファイルの
    衝突を避けつつ、後段の表示では連番ディレクトリを取り除いて元の相対名をそのまま出せる
    （`_doc_display_name` 参照）。symlink は辿らない（`shutil.copytree(symlinks=False)`）。
    """
    for i, raw in enumerate(inputs):
        p = Path(raw)
        if not p.exists():
            sys.exit(f"入力が見つかりません: {raw}")
        dest_parent = staging_src / f"{i:03d}"
        dest_parent.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            shutil.copytree(p, dest_parent / p.name, symlinks=False)
        elif p.is_file():
            shutil.copy2(p, dest_parent / p.name)
        else:
            sys.exit(f"入力はファイルまたはディレクトリのみ対応しています: {raw}")


def _doc_display_name(rel: str) -> str:
    """連番ステージングディレクトリ（`_stage_inputs` が付与した先頭 `NNN/`）を除いた表示用の相対名。"""
    parts = Path(rel).parts
    if parts and parts[0].isdigit() and len(parts[0]) == 3:
        return str(Path(*parts[1:])) if len(parts) > 1 else rel
    return rel


# ===== チャンク化（A=人間向けMD見出し単位 / B=rag.mdアンカー単位） =====

def _chunk_human_md(text: str) -> list[dict]:
    """人間向け `{rel}.md` を `##`/`###` 見出し単位でチャンク化する（A）。"""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        body = text.strip()
        return [{"heading": None, "text": body}] if body else []
    chunks = []
    leading = text[: matches[0].start()].strip()
    if leading:
        chunks.append({"heading": None, "text": leading})
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block = text[start:end].strip()
        if block:
            chunks.append({"heading": m.group(2).strip(), "text": block})
    return chunks


def _chunk_rag_md(text: str) -> list[dict]:
    """RAG 正本 `{rel}.rag.md` を D1 アンカー単位でチャンク化する（B）。"""
    matches = list(_ANCHOR_RE.finditer(text))
    chunks = []
    for i, m in enumerate(matches):
        chunk_id = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            chunks.append({"chunk_id": chunk_id, "text": body})
    return chunks


def _collect_chunks(derived_md_dir: Path, derived_rag_dir: Path) -> tuple[list[dict], list[dict], int]:
    """変換済み派生物から A/B チャンク集合を作る。返値 `(chunks_a, chunks_b, doc_count)`。

    B 側が無い文書（rag.md 非対応の旧形式や変換失敗）は、その文書について B へ 0 件を計上するだけで
    処理は止めない（A/B の文書集合が完全一致する保証はない＝比較対象がある文書だけが両方に載る）。
    """
    chunks_a: list[dict] = []
    chunks_b: list[dict] = []
    doc_count = 0
    if not derived_md_dir.is_dir():
        return chunks_a, chunks_b, doc_count
    for md_path in sorted(derived_md_dir.rglob("*.md")):
        rel = str(md_path.relative_to(derived_md_dir))[: -len(".md")]
        doc = _doc_display_name(rel)
        doc_count += 1
        try:
            a_text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            a_text = ""
        for c in _chunk_human_md(a_text):
            chunks_a.append({"doc": doc, "heading": c["heading"], "chunk_id": None, "text": c["text"]})
        rag_path = derived_rag_dir / (rel + ".rag.md")
        if rag_path.is_file():
            try:
                b_text = rag_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                b_text = ""
            for c in _chunk_rag_md(b_text):
                chunks_b.append({"doc": doc, "heading": None, "chunk_id": c["chunk_id"], "text": c["text"]})
    return chunks_a, chunks_b, doc_count


# ===== Elasticsearch（一時 index・自前実装。URL 解決のみ es_index._url() を再利用） =====

def _es_req(method: str, path: str, body=None, ndjson: bool = False, timeout: int = _ES_TIMEOUT):
    if ndjson:
        data = body.encode("utf-8")
        ctype = "application/x-ndjson"
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        ctype = "application/json"
    req = urllib.request.Request(
        es_index._url() + path, data=data, method=method, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _es_available() -> bool:
    try:
        _es_req("GET", "/", timeout=_ES_AVAILABLE_TIMEOUT)
        return True
    except Exception:
        return False


def _mapping(analyzer: str, dim: int | None) -> dict:
    props = {
        "doc": {"type": "keyword"},
        "heading": {"type": "keyword"},
        "chunk_id": {"type": "keyword"},
        "text": {"type": "text", "analyzer": analyzer},
    }
    if dim:
        props["embedding"] = {"type": "dense_vector", "dims": dim, "index": True, "similarity": "cosine"}
    return {"mappings": {"properties": props}}


def _create_index(name: str, dim: int | None) -> str:
    """一時 index を作成し、使用した analyzer 名を返す。kuromoji 不明時は standard へフォールバック
    （`sherpa.es_index.ensure_index` と同型のフォールバック・実装はここに複製）。
    """
    for analyzer in ("kuromoji", "standard"):
        try:
            _es_req("PUT", "/" + name, _mapping(analyzer, dim))
            return analyzer
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                detail = str(e)
            if e.code == 400 and analyzer == "kuromoji":
                continue                          # kuromoji プラグイン未導入 → standard で再試行
            raise RuntimeError(f"一時index作成に失敗しました（{name}）: {detail}") from e
    raise RuntimeError(f"一時index作成に失敗しました（{name}）: kuromoji/standard いずれも失敗")


def _delete_index(name: str) -> None:
    try:
        _es_req("DELETE", "/" + name)
    except Exception:
        pass                                       # 既に無い/一時的な不達は無視（best-effort）


def _bulk_index(name: str, docs: list[dict], vectors: list[list[float]] | None) -> None:
    if not docs:
        return
    lines = []
    for i, d in enumerate(docs):
        lines.append(json.dumps({"index": {"_id": str(i)}}))
        body = dict(d)
        if vectors is not None:
            body["embedding"] = vectors[i]
        lines.append(json.dumps(body, ensure_ascii=False))
    ndjson = "\n".join(lines) + "\n"
    _es_req("POST", f"/{name}/_bulk", ndjson, ndjson=True)
    _es_req("POST", f"/{name}/_refresh")


def _search(name: str, query: str, k: int, query_vector: list[float] | None) -> list[dict]:
    body: dict = {"size": k, "query": {"bool": {"must": [{"match": {"text": query}}]}}}
    if query_vector is not None:
        body["knn"] = {"field": "embedding", "query_vector": query_vector, "k": k,
                        "num_candidates": max(50, k * 5)}
    try:
        res = _es_req("POST", f"/{name}/_search", body)
    except Exception as e:
        print(f"    検索に失敗しました（{name}）: {e}")
        return []
    hits = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        hits.append({"score": h.get("_score"), "doc": src.get("doc"),
                     "heading": src.get("heading"), "chunk_id": src.get("chunk_id"),
                     "text": src.get("text") or ""})
    return hits


# ===== --vector（任意・埋め込み設定が解決できる場合のみ） =====

def _resolve_embed_cfg():
    """埋め込み設定を解決する。DB/設定が無ければ None（呼び出し側は BM25 のみへ縮退する）。"""
    try:
        from sherpa import embeddings
        return embeddings.cfg()
    except Exception:
        return None


def _embed_texts(texts: list[str], cfg: dict) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        from sherpa import embeddings
        return embeddings.embed(texts, cfg)
    except Exception:
        return None


# ===== 表示 =====

def _snippet(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) > _SNIPPET_MAX_CHARS:
        return flat[:_SNIPPET_MAX_CHARS] + "…"
    return flat


def _print_hits(label: str, hits: list[dict]) -> None:
    print(f"  --- {label} ---")
    if not hits:
        print("    （ヒットなし）")
        return
    for i, h in enumerate(hits, start=1):
        loc = f"chunk_id={h['chunk_id']}" if h.get("chunk_id") else f"見出し={h.get('heading') or '(見出しなし)'}"
        score = h.get("score")
        score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "?"
        print(f"    [{i}] score={score_s} 文書={h.get('doc')} {loc}")
        print(f"        {_snippet(h.get('text') or '')}")


# ===== main =====

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ab_search.py",
        description="rag.md 方式（B）と人間向け表MD方式（A）の検索比較ツール（standalone・Makefile非連結）。",
        epilog="例: .venv/bin/python scripts/ab_search.py DEP-XLSX-MARKERS.xlsx -q \"旧税率はいくつ\"",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="Office ファイル（複数可）またはディレクトリ。読み取り専用。")
    ap.add_argument("-q", "--query", action="append", dest="queries", metavar="QUERY",
                    help="比較するクエリ（複数指定可）。省略時は対話入力（空行で終了）。")
    ap.add_argument("-k", "--top-k", type=int, default=_DEFAULT_K, metavar="N",
                    help=f"A/B それぞれの上位何件を表示するか（既定 {_DEFAULT_K}）。")
    ap.add_argument("--vector", action="store_true",
                    help="埋め込み込みのハイブリッド比較を試みる（既定 OFF＝BM25のみ・費用ゼロ）。"
                         "埋め込み設定が解決できなければ自動で BM25 のみへ縮退する。")
    args = ap.parse_args()

    if args.top_k < 1:
        sys.exit("-k は1以上を指定してください")

    if not _es_available():
        sys.exit(
            "Elasticsearch に接続できません"
            f"（{es_index._url()}）。docker compose 等で ES を起動してから再実行してください。")

    with tempfile.TemporaryDirectory(prefix="ab_search_") as tmp:
        tmp_root = Path(tmp)
        staging_src = tmp_root / "src"
        staging_src.mkdir()
        _stage_inputs(args.inputs, staging_src)

        derived_md_dir = tmp_root / "derived" / "md"
        print("文書を変換しています（人間向けMD＋rag.md）…")
        try:
            rep = office_md.build_derived(staging_src, derived_md_dir)
        except Exception as e:
            sys.exit(f"文書の変換に失敗しました: {e.__class__.__name__}: {e}")
        if rep.get("error"):
            sys.exit(f"文書の変換に失敗しました: {rep['error']}")

        derived_rag_dir = office_md._sibling_layer_dir(derived_md_dir, "rag")
        chunks_a, chunks_b, doc_count = _collect_chunks(derived_md_dir, derived_rag_dir)
        print(f"文書 {doc_count} 件 / A(人間向けMD・見出し単位) {len(chunks_a)} チャンク "
              f"/ B(rag.md・アンカー単位) {len(chunks_b)} チャンク")
        if doc_count == 0:
            sys.exit("変換できた文書がありません（対応拡張子か確認してください）。")

        vector_cfg = None
        if args.vector:
            vector_cfg = _resolve_embed_cfg()
            if vector_cfg is None:
                print("埋め込み設定が見つからないため、BM25のみで比較します。")
            else:
                print(f"埋め込み設定を検出しました（provider={vector_cfg.get('provider')} "
                      f"model={vector_cfg.get('model')}）。ハイブリッド比較を行います。")

        suffix = _random_suffix()
        index_a = f"{_INDEX_PREFIX}a_{suffix}"
        index_b = f"{_INDEX_PREFIX}b_{suffix}"
        try:
            dim = vector_cfg.get("dim") if vector_cfg else None
            _create_index(index_a, dim)
            _create_index(index_b, dim)

            vec_a = vec_b = None
            if vector_cfg:
                vec_a = _embed_texts([c["text"] for c in chunks_a], vector_cfg)
                vec_b = _embed_texts([c["text"] for c in chunks_b], vector_cfg)
                if vec_a is None or vec_b is None:
                    print("埋め込みの取得に失敗したため、BM25のみで比較します。")
                    vector_cfg, vec_a, vec_b = None, None, None

            _bulk_index(index_a, chunks_a, vec_a)
            _bulk_index(index_b, chunks_b, vec_b)

            queries = args.queries
            if not queries:
                queries = []
                print("クエリを入力してください（空行で終了）:")
                while True:
                    try:
                        line = input("> ").strip()
                    except EOFError:
                        break
                    if not line:
                        break
                    queries.append(line)

            if not queries:
                print("クエリが指定されませんでした。終了します。")
                return

            for q in queries:
                print(f"\n=== クエリ: {q} ===")
                qvec = None
                if vector_cfg:
                    qv = _embed_texts([q], vector_cfg)
                    if qv:
                        qvec = qv[0]
                hits_a = _search(index_a, q, args.top_k, qvec)
                hits_b = _search(index_b, q, args.top_k, qvec)
                _print_hits("A: 人間向けMD（見出し単位）", hits_a)
                _print_hits("B: rag.md（アンカー単位）", hits_b)
        finally:
            _delete_index(index_a)
            _delete_index(index_b)


if __name__ == "__main__":
    main()
