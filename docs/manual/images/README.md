# 画面キャプチャ（自動生成）

マニュアル各ページが参照する画像の置き場です。**画像は手で撮りません。**
画面が変わったら、次のコマンドで**プログラムが全画像を撮り直します**（AI もストア(Docker)も不要）。

```sh
make screenshots            # 全画像を再生成（docs/manual/images/*.png）
make screenshots ARGS="--only chat"   # 名前に chat を含むものだけ
make screenshots ARGS="--list"        # シーン一覧（画像名→再現方法）
```

しくみ: `scripts/capture_screenshots.py` が、e2e テストと同じ土台
（`tests/e2e/mock_api.py` の決定的モック API ＋ `web/` の静的配信）を再利用して、
Playwright（chromium・headless）で各画面を再現し、要素またはビューポートを撮ります。
専用ポート（既定 8901・dev の 8000 と衝突しない）で動くので、開発中の起動と干渉しません。

## シーンの増やし方（登録簿に 1 エントリ足すだけ）

`scripts/capture_screenshots.py` の `SCENES`（シーン登録簿）に `Scene(...)` を 1 つ足します。

```python
Scene("21-新しい図", "graph.html",        # 画像名（.png なし）／対象ページ
      "何をどう撮るかの一言（--list に出る）",
      setup=lambda p: p.click("#something"),  # goto 後の前準備（省略可）
      crop="#element",                       # 要素を切り出す（None=ビューポート全体）
      viewport=WIDE_VIEWPORT,                # 既定は 1280x800
      mock_kwargs={"stream_events": [...]},  # 回答内容を差し替えたい時（省略可）
      routes=my_extra_routes)                # 既定モックに無い API を足す時（省略可）
```

- **決定性**: 固定ビューポート・アニメーション無効・JST 固定。日時が写る箇所はモックデータ側の
  固定値を使うので、再実行でほぼ同じ絵になります（フォント差の範囲）。
- **前準備**は Playwright の `page` を受け取る関数。クリック/入力/待機を素直に書きます。
- 回答カードの内容を変えたい時は `mock_kwargs={"stream_events": [...]}` で SSE を丸ごと差し替えます。

## 画像一覧（画像名 → 内容）

### 10. 使い方：チャットで調べる
- `10-chat-overview.png` — チャット全体（左=履歴／中央=回答／右=思考の流れ）
- `10-chat-impact-card.png` — 影響分析の答えカード（件数＋対象一覧・経路つき）
- `10-chat-sources.png` — 回答末尾の**出典フッター**（📄=原本DL）
- `10-chat-troubleshoot.png` — トラブルシュートの**原因候補カード**（確度順・経路つき）
- `10-chat-thinking.png` — 右ペインの「思考の流れ」（検索→精読→グラフ近傍）
- `10-chat-clarify.png` — 曖昧時の**確認カード**（選択肢で絞り込む）

### 11. 使い方：範囲とAIの切り替え
- `11-knowledge-toggle.png` — 「ナレッジ参照」トグル（既定オフ）
- `11-scope-brain.png` — **範囲**（フォルダ）と**頭脳**（AI）の選択
- `11-settings.png` — 設定画面（AI 接続：OpenAI/Gemini/Ollama/Codex のキー・モデル）

### 20. 管理：資料の取り込み
- `20-ingest-new.png` — 「資料フォルダ」画面（フォルダ登録→取り込み）
- `20-ingest-status.png` — 「取り込み状況」画面（状態・フォルダツリー・原本DL）

### 21. 管理：グラフと検索
- `21-graph.png` — ナレッジグラフ（色分け・近傍・名前検索）
- `21-graph-search.png` — 関係/条件でのグラフ検索の結果
- `21-graph-ask.png` — グラフへの自然言語質問（管理チャット）と根拠ノード
- `21-es-search.png` — 「取り込み状況」内の**全文検索パネル**（ヒット一覧）

> 備考: デモデータが小規模なため、`21-graph.png`（cytoscape の力学レイアウト）はノード配置が
> 実行ごとに多少変わります（画素完全一致ではなく「同じ画面が同じレイアウトで撮れる」水準）。
> 他の画面は要素/DOM ベースでほぼ安定します。
