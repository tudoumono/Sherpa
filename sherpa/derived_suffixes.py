"""資料 1 件（rel）から作られる派生物の名前の接尾の正本（書く側と、除外の判定・Codex の deny が同じ定数を使う）。
派生物を書く所（取り込みの変換・公開の差し替え・PDF のページ画像・OCR の観測）はここの定数で名前を作り、
`path_rules`（濾過）と `providers/codex/sandbox`（deny）は同じ定数から一覧を作る。新しい派生物を足すときは、ここに足す。
設計: docs/design/rag.md「派生物の置き場」
"""
from __future__ import annotations

# 人間用 MD 層（derived/md）
MD = (".md", ".md.meta.json")
# RAG 正本層（derived/rag）。ファイル
RAG = (".rag.md", ".rag_chunks.jsonl")
# RAG 正本層のフォルダ（`.assets`＝抽出した画像・証跡 / `.pages`＝回答時に描いた PDF のページ画像）
ASSETS = ".assets"
PAGES = ".pages"
RAG_DIRS = (ASSETS, PAGES)
# 中間表現層（derived/ir）。Codex の直読の対象ではない
IR = (".document.json", ".evidence.json", ".context.json", ".ocr_route.json")
MANIFEST = ".derived.json"          # ir 層（rel ごとの sidecar マニフェスト）
OBSERVATION = ".ai_observations.jsonl"  # OCR の観測（観測の置き場）

# 層ごとの名前（Codex が直読みできる置き場は md・rag）
BY_LAYER = {"md": MD, "rag": RAG + RAG_DIRS, "ir": IR + (MANIFEST,)}
# すべての接尾（出力の濾過の派生名の判定に使う）
ALL = MD + RAG + RAG_DIRS + IR + (MANIFEST, OBSERVATION)
