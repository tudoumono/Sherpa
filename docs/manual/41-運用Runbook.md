# 41. 運用 Runbook（障害対応・バックアップ・復旧）

[40-運用](40-運用.md) が「平時」の起動・公開・構成の説明であるのに対し、本書は**異常時**に開くページです。
障害シナリオ別の復旧手順、バックアップ/リストアの具体的なコマンド、デプロイ失敗時のロールバック判断基準をまとめます。

対象読者: 運用担当（コマンドを実行できる人）。ユーザー向けの「困ったとき」は [10-使い方](10-使い方-チャットで調べる.md) を参照。

## 最初の5分（トリアージ）

障害の連絡を受けたら、原因の切り分けを次の順で行います。

| # | 見るもの | コマンド / 場所 | 分かること |
|---|----------|-----------------|------------|
| 1 | 状態ドット | 画面右上（全画面共通）。管理者はクリックで詳細 | 緑＝正常／黄＝一部機能制限／赤＝停止 |
| 2 | システム状態画面 | 管理メニュー「システム状態」（[24-システム管理](24-システム管理.md)） | どのコンポーネントが止まっているか＋対処ヒント（停止中が最上部に並ぶ） |
| 3 | 一枚看板 | `make status` | ストア3つ・アプリ（healthz）・LAN/Caddy・URL |
| 4 | 死活応答 | `curl -s http://127.0.0.1:8000/healthz` | `{"ok":true}` が返ればアプリ核は生きている |
| 5 | アプリログ | `tail -n 100 data/run/api.log` | 例外・起動失敗の直接の手がかり |
| 6 | 全ログ（アプリ＋ストア） | `make logs`（絞り込み例: `make logs ARGS="convert embed"`） | api/convert/embed/usage 等と PostgreSQL/Neo4j/Elasticsearch を1画面に合流して追う（`-l` で一覧、`-r` で集計レポート、`-h` でヘルプ） |

> **してはいけないこと**: 復旧目的で `make nuke` を実行しない（ストアの**ボリュームごと削除**＝データ消去です）。
> 迷ったらまず `make up`（ストア再起動）と `make restart`（アプリ再起動）。どちらもデータは消しません。

## 障害シナリオ別の手順

### S1. アプリ（FastAPI）が応答しない

- **症状**: 画面が開かない／`curl -s http://127.0.0.1:8000/healthz` が返らない。ストアは `make ps` で healthy。
- **復旧**: `make restart`（ストアは維持したままアプリだけ再起動）。
- **直らない場合**: `tail -n 200 data/run/api.log` で起動時の例外を確認。env 設定ミス（production の起動拒否ガード等）は
  ログに理由が明記されます。`make prod-check` で env/依存関係を点検。
- **復旧確認**: `make status` が全緑 → ブラウザでログイン → ホームが表示される。

### S2. PostgreSQL が停止した

- **症状**: ログインできない・全画面が赤ドット。`/health/summary` が `down`。会話・台帳・ユーザーが全て読めない。
- **確認**: `make ps` で postgres が Up/healthy か。`docker compose exec postgres pg_isready -U sherpa`。
- **復旧**: `make up`（起動のみ＝healthy を**待ちません**）→ `make ps` で `healthy` を確認 → `make status`。
  アプリの再起動は不要（接続は都度張るため自然回復）。healthy 待ちまで任せたい場合は `make start`（内蔵の待機つき）。
- **ボリューム破損の場合**（起動してもすぐ落ちる・pg のログに破損エラー）: 下の「リストア手順」でバックアップから復元。
- **影響範囲**: PostgreSQL は**一次データ**（会話・文書台帳・world レジストリ・ユーザー/設定・監査ログ）。
  ここだけはバックアップでしか守れません（下の「バックアップ」参照）。

### S3. Neo4j が停止した

- **症状**: 影響分析が使えない（黄ドット）。**取り込みが「失敗」になる**（グラフ反映は原子的＝失敗時は旧グラフ・旧台帳が
  そのまま残り、中途半端な状態にはならない設計）。
- **復旧**: `make up` → 失敗した取り込みがあれば、管理「資料フォルダ」の**「今すぐ更新」**で再取り込み。
- **データ**: グラフは登録フォルダから**再生成可能**。バックアップ不要、復元＝再取り込み。

### S4. Elasticsearch が停止した / 索引の反映に失敗した

- **症状**: 全文検索のヒットが薄い・取り込み状況に「全文検索 未接続」。取り込み自体は成功します（ES は best-effort）。
  索引の書き込みに失敗した場合は、取り込み一覧に
  **「※ 前回の取り込みで全文検索への反映に失敗しました。検索結果が古い可能性があります」**という注意が出ます（半壊状態の可視化）。
- **復旧**: `make up` → `make ps` で healthy 確認 → 対象フォルダを**「今すぐ更新」**。なお索引の鮮度ズレ（空・署名ズレ）は
  次回の更新チェック時に自動検知して張り直すため、放置しても次の取り込み機会に自己修復されます。
- **表示の注意**: 上の警告表示は「直近の取り込みの記録」に紐づくため、ES 復旧・索引の自己修復が済んだ後も、
  **次に内容が変わって取り込みが走るまで表示が残ることがあります**（検索自体は直っています。故障ではありません）。
- **データ**: 索引は**再生成可能**。バックアップ不要。

### S5. ディスクが逼迫した

- **確認**: `df -h`（ホスト全体）／`docker system df`（イメージ・ボリューム）／`du -sh data/*`（アプリ側の内訳）。
- **よくある解消先**（安全な順）:
  1. `data/run/api.log` の肥大 → `: > data/run/api.log`（稼働中でも安全に空にできます）。
  2. 使っていない Docker イメージ → `docker image prune`（**volume prune は実行しない**＝pg/neo4j/es のデータが消えます）。
  3. `data/derived/{world}/`（Office→MD の写し `md/`・RAG 正本 `rag/`・中間表現 `ir/`）は再生成可能ですが、直接消すより空きを確保してから「今すぐ更新」で
     作り直す方が安全。`data/derived/{world}/semantic/` は退役済み（意味レイヤの残置・下の表参照）で、消しても支障ありません。
- **ES の読み取り専用ロック**: Elasticsearch は空き容量が閾値を切ると索引を自動で read-only 化します（ログに
  `flood stage disk watermark` ）。**空き容量を確保した後**、次で解除します:

  ```bash
  curl -X PUT 'http://127.0.0.1:9200/_all/_settings' \
       -H 'Content-Type: application/json' \
       -d '{"index.blocks.read_only_allow_delete": null}'
  ```

- **復旧確認**: `make status` が全緑 → 「今すぐ更新」が成功すること（**新しい**失敗警告が増えないこと。
  過去の警告表示が次の実取り込みまで残り得るのは S4 の注意のとおりで、故障ではありません）。

### S6. 資料フォルダ（参照元）にアクセスできない

- **症状**: 取り込み状況に「参照元フォルダにアクセスできません」・「今すぐ更新」が失敗する。
- **よくある原因**: Windows 共有マウントの切断（Linux サーバ・[40-運用 §4](40-運用.md)）／参照元フォルダの
  移動・リネーム／WSL の `/mnt` 未マウント。
- **安全性**: 参照元不可は fail-closed 設計＝**取り込み済みの鏡（索引・グラフ・台帳）は消えません**。
  ただし**障害中は、そのフォルダへの検索・影響分析・原本ダウンロードも「参照元不可」として使えません**
  （中途半端に古い結果を返さない設計）。復旧すれば**再取り込みなしでそのまま**使えるようになります。
  慌てて再登録・削除をしないこと。
- **復旧**: マウントなら `ls <マウント先>` で見えるか確認 → `sudo mount -a`（automount 構成なら再アクセスで
  自動再接続）。フォルダ自体が恒久的に移動した場合は、登録画面から同じ資料フォルダに新しいパスを
  付け替えます（再バインド）。
- **復旧確認**: 取り込み一覧の状況表示が通常に戻り、「今すぐ更新」が成功すること。

### S7. デプロイ（更新）に失敗した — ロールバック

**デプロイ前の約束**（これをやっていればロールバックは怖くない）:

1. PostgreSQL をバックアップ（下の1コマンド）。
2. `make prod-check`（env/依存の事前点検）。本番 env なら `scripts/check-production.sh` も。
   OpenAI 接続先の検査は**初回起動前後で対象が変わる**（DB へ到達でき初回シード済みなら
   `system_settings` の実効値を検査・未到達／初回起動前なら env 候補値を検査＝出力の
   「接続先の検査モード」行でどちらか分かる）。
3. 直前の稼働版（git のコミット/タグ、または配布 tarball の版）を控える。

**ロールバック判断基準**（いずれかに該当したら戻す）:

| 条件 | 判断 |
|------|------|
| `make restart` 後、healthz が **10分**経っても返らない | ロールバック |
| 起動はするが、スモーク（ログイン→ホーム表示→検索1回→取り込み一覧表示）のどれかが失敗し、原因の目星が**30分**で立たない | ロールバック |
| データ破損の疑い（台帳と画面の不一致・監査ログの異常） | **即**ロールバック＋バックアップ復元を検討 |

**ロールバック手順**（展開形態で分岐。どちらか自分の環境の方だけ実行）:

リポジトリ直下で `make start` 運用している場合（開発/検証・WSL）:

```bash
cd /path/to/sherpa
git log --oneline -5                 # 直前の稼働版を確認（タグ運用ならタグ名）
git checkout <直前の稼働コミット>
make restart                         # stop(ストア維持)→start。依存の巻き戻しも自動（ハッシュ検知で再インストール）
make status                          # 全緑＋スモークで確認
```

tarball＋systemd で本番展開している場合（[40-運用](40-運用.md)「本番展開の標準手順」の、版ごとのフォルダに
展開して `current` リンクを切り替える形）: **旧版フォルダは `/opt/sherpa/releases/` に残っている**ため、
再展開は不要でリンクを戻すだけです。

**同時に複数の導入作業をしない**でください（この手順自体には多重実行の排他機構がありません。
導入スクリプト実行中はロールバックしない／導入とロールバックを同時に行わないでください。
メンテナンス時間帯を確保し、単一のオペレータが1つずつ実行してください）。

```bash
ls /opt/sherpa/releases/                       # 旧版フォルダ名を確認（例 sherpa-v0.1.0）

# 切替（symlink 張り替え）だけでなく、切替後のプリフライト（check-production.sh）・再起動・
# 状態確認までを**同じ** set -euo pipefail サブシェルに含める。切替後に分離したコマンド列で
# 書くと、途中（例えば check-production.sh）が失敗しても後続の systemctl restart がそのまま
# 実行されてしまい、失敗に気づかないまま先に進んでしまう。
( set -euo pipefail   # 対話シェルでも1コマンドの失敗で確実に中断する（続行して事故らないため）

  OLD_RELEASE="/opt/sherpa/releases/<旧版フォルダ名>"   # 上の一覧から実在する版名に置き換える

  # 戻す先が「実ディレクトリ」として存在することを確認してから進める（symlink ではないこと
  # も確認する＝ releases/<版> は本来 immutable な実ディレクトリのみのはずで、symlink なら
  # 想定外の場所を指している可能性がある。version 名の入力ミス・削除済みの版を指すミスも
  # ここで止める）。
  if [ ! -d "$OLD_RELEASE" ] || [ -L "$OLD_RELEASE" ]; then
    echo "エラー: $OLD_RELEASE が実ディレクトリとして見つかりません（symlink である場合も含む）" >&2
    exit 1
  fi

  # 未完成マーカー（.sherpa-incomplete）付きの版は、依存導入・検証を完了できなかった
  # 残骸なので戻し先として選べない（--list-releases で表示される完成済みの版名を使う）。
  if [ -e "$OLD_RELEASE/.sherpa-incomplete" ]; then
    echo "エラー: $OLD_RELEASE は未完成マーカー付きです（依存導入・検証が完了していません）" >&2
    exit 1
  fi

  # current が旧方式（リンクでない実フォルダ）のままだとこの手順は使えない＝先に通常導入を1回行う
  if [ -e /opt/sherpa/current ] && [ ! -L /opt/sherpa/current ]; then
    echo "エラー: /opt/sherpa/current がリンクではありません（旧方式のまま）" >&2
    exit 1
  fi

  # 切替に失敗した/切替後の確認に失敗した場合に戻せるよう、切替前に current の指す先を
  # 控えておく。
  PREV_TARGET=""
  if [ -L /opt/sherpa/current ]; then
    PREV_TARGET="$(readlink -f /opt/sherpa/current)"
  fi

  # current の切替は一発の rename で行う（ln -sfn は「削除→作成」の2手順で途中状態が生じうるため
  # 使わない。固定名ではなく一意な一時フォルダにリンクを作ってから mv -T で置き換える）。
  # mktemp の戻り値・生成結果を明示的に確認してから使う。
  TMP_LINK_DIR="$(sudo mktemp -d /opt/sherpa/.sherpa-current-tmp.XXXXXX)"
  if [ -z "$TMP_LINK_DIR" ] || [ ! -d "$TMP_LINK_DIR" ]; then
    echo "エラー: 一時ディレクトリの作成に失敗しました" >&2
    exit 1
  fi
  sudo ln -s "$OLD_RELEASE" "$TMP_LINK_DIR/current"
  sudo mv -T "$TMP_LINK_DIR/current" /opt/sherpa/current
  sudo rmdir "$TMP_LINK_DIR" 2>/dev/null || true

  # 切替後、current 経由で check-production.sh を実行する（$ROOT が current の symlink パスの
  # まま解決されるため、SHERPA_DERIVED_DIR 等の current 相対の誤配置検出が正しく効く）。
  # sudo は既定で環境変数をリセットするため `sudo env VAR=val ... cmd` の形で明示的に渡す
  # （PYTHON_BIN 無しだと system python3 を検査してしまい、健全な venv でも依存不足と誤判定する）。
  #
  # ここから先（プリフライト・再起動・状態確認）のどれかが失敗したら、切替前の版
  # （$PREV_TARGET）へ戻してから中止する（ロールバックのつもりが別の問題を持ち込んで
  # 終わらないようにするため）。
  POSTFLIGHT_OK=1
  sudo env SHERPA_ENV_FILE=/etc/sherpa/sherpa.env PYTHON_BIN=/opt/sherpa/current/.venv/bin/python \
    /opt/sherpa/current/scripts/check-production.sh || POSTFLIGHT_OK=0
  if [ "$POSTFLIGHT_OK" = 1 ]; then
    sudo systemctl restart sherpa-api.service || POSTFLIGHT_OK=0
  fi
  if [ "$POSTFLIGHT_OK" = 1 ]; then
    sudo systemctl status sherpa-api.service || POSTFLIGHT_OK=0
  fi
  # 起動直後は数秒〜数十秒応答しないことがあるため、healthz は即断せず一定時間リトライしてから
  # 判定する（最大30回・1秒間隔。起動に時間がかかっているだけで実際は正常起動する
  # ケースを誤って失敗扱いにしないため）。curl 自体にも --connect-timeout 2 --max-time 5 を
  # 付け、TCP 接続はできるのに応答が返らない状態で無期限に止まらないようにする（実効の最大
  # 待ち時間は、接続が即座に拒否される通常の失敗時で約30秒、毎回タイムアウトいっぱいまで
  # 粘る最悪ケースで最大 約180秒＝30回 x (5秒+1秒)）。
  if [ "$POSTFLIGHT_OK" = 1 ]; then
    HEALTHZ_OK=0
    for i in $(seq 1 30); do
      curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && { HEALTHZ_OK=1; break; }
      sleep 1
    done
    [ "$HEALTHZ_OK" = 1 ] || POSTFLIGHT_OK=0
  fi

  if [ "$POSTFLIGHT_OK" != 1 ]; then
    echo "エラー: 切替後の確認（プリフライト/再起動/状態確認/healthz）に失敗しました。" >&2
    if [ -n "$PREV_TARGET" ]; then
      TMP_LINK_DIR2="$(sudo mktemp -d /opt/sherpa/.sherpa-current-tmp.XXXXXX)"
      sudo ln -s "$PREV_TARGET" "$TMP_LINK_DIR2/current"
      sudo mv -T "$TMP_LINK_DIR2/current" /opt/sherpa/current
      sudo rmdir "$TMP_LINK_DIR2" 2>/dev/null || true
      echo "切替前の版（$PREV_TARGET）へ戻しました。" >&2

      # リンクを戻しただけではサービスは復旧しない（restart の途中（stop 済み・start 失敗等）で
      # 止まっている可能性がある）。戻した版で改めて再起動・状態確認・healthz まで行う。
      RECOVERY_OK=1
      sudo systemctl restart sherpa-api.service || RECOVERY_OK=0
      if [ "$RECOVERY_OK" = 1 ]; then
        sudo systemctl status sherpa-api.service || RECOVERY_OK=0
      fi
      if [ "$RECOVERY_OK" = 1 ]; then
        # こちらも切替成功側と同じ基準（最大30回・1秒間隔＋curl 自体に --connect-timeout 2
        # --max-time 5、実効の最大待ち時間は約30秒〜最悪 約180秒）で healthz をリトライしてから
        # 判定する。
        HEALTHZ_OK=0
        for i in $(seq 1 30); do
          curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:8000/healthz >/dev/null 2>&1 && { HEALTHZ_OK=1; break; }
          sleep 1
        done
        [ "$HEALTHZ_OK" = 1 ] || RECOVERY_OK=0
      fi

      if [ "$RECOVERY_OK" = 1 ]; then
        echo "切替前の版（$PREV_TARGET）でサービスが復旧しました。ロールバック先（$OLD_RELEASE）への" >&2
        echo "切替はできていません。原因を確認してから、あらためて実行してください。" >&2
      else
        echo "エラー: 切替前の版へ戻しても復旧できませんでした。**サービスは停止中の可能性があります**。" >&2
        echo "  ここからは自動処理できません。人手で対応してください" >&2
        echo "  （journalctl -u sherpa-api.service / systemctl status sherpa-api.service で原因確認）。" >&2
      fi
    else
      echo "エラー: 戻し先の版が分かりませんでした（切替前は current が未設定でした）。" >&2
      echo "  **サービスは停止中の可能性があります**。人手で対応してください。" >&2
    fi
    exit 1
  fi
)
```

（オフラインキット導入＝`scripts/install_offline_kit.sh --target-dir` を使った環境では、上のリンク切替の
代わりに `scripts/install_offline_kit.sh --target-dir /opt/sherpa/current --rollback-to <旧版フォルダ名>`
の1コマンドで同じ切替ができます。`--list-releases` で版名一覧を確認できます。）

旧版の `releases/<版>/.venv` はそのまま残っているため、依存関係を入れ直す必要はありません
（`releases/<版>` ごと venv を持つ設計・別 Python 版に切替済みなら作り直してください）。

- DB スキーマは起動時の自動適用で、**基本は「不足分の追加」型**（旧版に戻しても新しい列は無視されるだけ）ですが、
  版によっては**制約変更やデータ更新を含む**ことがあり、`git checkout` では DB は戻りません。だからこそ
  **デプロイ前の `make backup` は保険ではなく前提**です。戻した後の動作が怪しければバックアップ復元まで行います。

### S8. OpenAI 系の応答が「接続先が未確定」で止まる（fail-closed）

**症状**: チャット（OpenAI 直結／Codex(OpenAI) 構成）・埋め込み・検索用文書の整形（有効時）などが揃って失敗し、
理由に「OpenAI 接続先の設定が未確定のため停止しています（env の設定を修正して再起動してください）」
が出る。管理画面の状態ページの「OpenAI API」行にも同じ理由が出る。

**原因**: 起動時 env シード（`OPENAI_BASE_URL`／`SHERPA_OPENAI_ENDPOINT_KIND`／
`SHERPA_OPENAI_AUTH_HEADER`／`SHERPA_OPENAI_API_VERSION`）の候補が不正で確定できなかった
（例: `OPENAI_BASE_URL` が https でない・ホスト名を欠く・`SHERPA_OPENAI_ENDPOINT_KIND` が
`openai`/`azure`/`custom` のいずれでもない等）。「未設定＝OpenAI 本家既定」という正当な状態と
DB 上区別が付かないため、黙って本家既定へ倒さず、プロセス内フラグ（`sherpa/llm.py::
set_openai_endpoint_seed_blocked`）で OpenAI 系 I/O 全体を止める設計（DB へは一切書かない）。

**この仕組みの既知の限界（運用契約として明記）**:
- **プロセス内フラグのみ**＝DB で共有しない（簡素化の裁定・DB 化はしない）。このため:
  - **複数 worker／複数 replica 構成は非対応**。`SHERPA_UVICORN_WORKERS` を1に固定する運用
    契約（`scripts/check-production.sh` の「SHERPA_UVICORN_WORKERS=1 (single worker)」検査が
    これを検出する・複数 worker はチャットのターン内状態・レート制限と同じ理由で元々非対応）が、
    この機構にもそのまま適用される。**外部 ASGI ランチャー（gunicorn 等でワーカー数を管理する
    構成）や複数レプリカ（コンテナ/プロセスを横に並べる構成）でこの機構を使う場合、
    ブロックは「そのワーカー/レプリカだけ」に効き、他のワーカー/レプリカは対象外**
    （env が不正なら通常は全ワーカーが同じ不正候補を評価しブロックされるが、ワーカーごとに
    起動タイミングがずれ DB 読取に失敗する等で状態が割れる可能性は理論上残る）。
  - **再起動のたびに再評価**する（プロセスを跨いで残らない＝新しいプロセスは env を再度検証する）。
    env を直したら**再起動が必須**（`healthz()` の再試行だけでも、次回呼び出し時に env
    シード候補の再評価は走るが、修正後は最終的に `make restart` で確実に反映させること）。
  - **worker 間で状態は伝播しない**（あるワーカーが解除されても他のワーカーには伝わらない・
    DB のマーカー確定を全ワーカーがそれぞれ検知して初めて全員解除される）。

**復旧手順**:
1. `.env`／`.env.production` の `OPENAI_BASE_URL` 等を確認・修正する
   （`docs/manual/90-リファレンス.md` の env 一覧参照）。
2. `scripts/check-production.sh` で候補値の妥当性を確認する（env 候補モードで検査される・
   S7 の「接続先の検査モード」行参照）。
3. `make restart`（single-worker 運用のため、これで対象プロセス全体が再評価される）。
4. 管理画面の状態ページで「OpenAI API」が緑になることを確認する。

### S9. 影響調査・原因調べで「この資料フォルダは再取り込みが必要です」と出る

- **症状**: 影響分析やトラブルシュートで、答えの代わりに「この資料フォルダは再取り込みが必要です（内部形式が更新されました）。管理者にご連絡ください。」と表示される。
- **原因**: アプリの更新で検索の内部形式（関係グラフの構造）が新しくなった一方、対象フォルダの関係グラフはまだ古い内部形式のまま保存されている（そのフォルダに関係グラフの実データがある場合だけ起きる）。
- **復旧**: 管理「資料フォルダ」で該当フォルダの**「更新」**を実行する。再取り込みで関係グラフが最新の内部形式で作り直され、自動的に解消する。**データが失われるわけではない**（原本は消えず、通常の再取り込みと同じ扱い）。
- **復旧確認**: 同じ質問をもう一度投げて、通常どおり回答が返ること。

## ログ

### 置き場所

| ログ | 場所 | 中身 |
|------|------|------|
| アプリ（run ログ） | `data/run/api.log` | アプリ全体の標準出力/標準エラー（起動処理・各リクエストの警告以上・uvicorn のアクセスログ等） |
| Caddy（LAN 公開時のみ） | `data/run/caddy.log` | リバースプロキシ/HTTPS のログ |
| LibreOffice 変換 | `data/run/libreoffice.log` | 旧形式 Office（.doc/.xls/.ppt）変換の詳細（`legacy_backend`＝libreoffice／office_com 両方。`SHERPA_LOG_DIR` で変更可） |
| MD 変換（取り込み） | `data/run/convert.log` | 取り込み（world スキャン→MD化→索引）の進行ログの詳細 |
| LLM 埋め込み | `data/run/embed.log` | ベクトル埋め込み生成（OpenAI/Gemini/Ollama）の詳細 |
| AI 利用量 | `data/run/usage.log` | LLM 呼び出し1回ごとの kind/provider/model/トークン数/経過秒（`sherpa/metering.py::record`・チャット本回答含む・LOG-UX・2026-09-04） |

**WARNING 以上（障害の疑いがある事象）はサブシステム別ログだけでなく `data/run/api.log`（run ログ）にも残ります**——
上記の専用ログ（usage.log を除く）は INFO 以下の詳細を追うためのもので、見なくても障害には run ログだけで気づける設計です
（LOG-2・裁定 2026-09-03）。障害調査でまず見るのは run ログ、原因が変換/埋め込み系だと分かった後に該当の専用ログを
掘り下げる、という順で使います。

**まとめて見る/集計する**: 上記は個別に `tail`/`cat` してもよいですが、`make logs` は data/run/*.log と
Docker ストア（PostgreSQL/Neo4j/Elasticsearch/OCR）のログを1画面に合流して追えます
（`make logs ARGS="convert embed"` で絞り込み・`make logs ARGS="-r"` で追わずに集計レポート・
`make logs ARGS="-h"` でヘルプ。本書冒頭「最初の5分（トリアージ）」表の6行目も参照）。

### make logs のオプション早見

`make logs ARGS="…"` の引数はそのまま `scripts/logs.sh` に渡ります。**名前**（何を見るか）と
**オプション**（どう見るか）を組み合わせます。名前を1つでも指定すると、指定しなかった側
（アプリ/Docker）は表示されません。

**名前（見る対象）**

| 名前 | 中身 |
|---|---|
| `api` | アプリ全般（Web リクエスト・エラー。全行に時刻付き） |
| `convert` | 資料取り込みの MD 化（1ファイルごとの開始/完了・秒数・メモリ） |
| `libreoffice` | 旧形式 Office（.doc/.xls/.ppt）の前段変換 |
| `embed` | 埋め込み（ベクトル化）の進行 |
| `usage` | AI 呼び出し1回ごとの用途・トークン数・経過秒 |
| `postgres` / `neo4j` / `elasticsearch` / `ocr-worker` | Docker ストア側（別名: `pg`・`es`・`ocr`） |

`api-20260904-193821` のような日時付きの名前は**退避された過去世代**です（下の「起動時の退避」参照）。
その環境で指定できる名前の一覧は `make logs ARGS="-h"` が実物から生成して表示します。

**オプション（どう見るか）**

| オプション | 意味 |
|---|---|
| `-n N` | 追う前に末尾 N 行を先に表示（既定 20） |
| `-g PATTERN` | 正規表現に一致する行だけ表示 |
| `-m N` | メモリ行（[mem]＝空きメモリと主要プロセスの使用量）の間隔を N 秒に（既定 10・`0` で非表示） |
| `-x 名前` | 指定した名前を除外（複数回可） |
| `-l` | 追わずに、一覧と各ログの末尾だけ表示して終了 |
| `-r` | 追わずに**集計レポート**（ファイル別の変換所要秒 Top10・埋め込みスループット・用途別トークン・エラーのまとめ） |
| `-r -A` | 集計に退避された過去世代も連結（再起動をまたいだ全期間を見る） |
| `-h` | ヘルプ（その環境で使える名前一覧＋下の用途別レシピ入り） |

**用途別レシピ**

```bash
make logs ARGS="convert embed libreoffice -m 5"   # 資料取り込みを監視（メモリ5秒間隔）
make logs ARGS="-g 'ERROR|WARN|失敗|✗'"           # エラー・警告だけ拾う
make logs ARGS="-x api"                            # アプリ全般から api のノイズを抜く
make logs ARGS=""                                  # 全部（アプリ＋Docker＋メモリ）
make logs ARGS="-l"                                # いまの状況を一覧で（追わない）
make logs ARGS="-r"                                # 取り込み後の振り返りレポート
```

### 起動時の退避（ローテーション）

上記5種のログはいずれも、**起動のたびに上書きしません**。前回の内容が残っていれば
`<ファイル名>-YYYYmmdd-HHMMSS.log`（同一秒で衝突する場合は `-2`/`-3`…を付加）へ退避してから、
空のファイルで書き始めます。前回起動の障害調査は退避されたファイルを見てください
（`ls -t data/run/api-*.log | head` で新しい順に一覧できます）。

退避ファイルは無限には残りません。同じファイル（例: `api.log`）由来の退避ファイル数が
`SHERPA_LOG_KEEP`（既定 10）を超えたら、最古のものから自動で削除します。ディスク逼迫時に
保持数を減らしたい場合は `.env` の `SHERPA_LOG_KEEP` を小さくして `make restart`（既存の
退避ファイルにも次回起動時のプルーニングで反映されます）。

実装: シェル起動経路（run/caddy ログ）は `scripts/run-common.sh::sherpa_rotate_log`、
サブシステム別ログ（Python 側）は `sherpa/log_setup.py::rotate_and_prune`。命名規約・保持数の
意味論は両者で揃えています。

## バックアップ / リストア

### 何を守るか（正本と派生の区別）

> 以下のパス例はリポジトリ直下運用（`data/` 配下）のもの。tarball＋systemd の本番展開では、個人ファイルは
> `SHERPA_USERS_DIR`（本番例 `/srv/sherpa/users`）、派生（`semantic/` 含む）は `SHERPA_DERIVED_DIR`
> （本番例 `/srv/sherpa/derived`）、env は `/etc/sherpa/sherpa.env` に読み替えます（[40-運用](40-運用.md)）。

| データ | 場所 | 種別 | 守り方 |
|--------|------|------|--------|
| 登録フォルダ（原本） | 外部フォルダ（例 `/mnt/c/...`、`data/kb` 配下に置く構成ならそこ） | **正本**（業務側） | 業務側のバックアップ運用に従う（Sherpa は読み取り専用） |
| PostgreSQL | Docker ボリューム `pg` | **一次データ**（会話・台帳・レジストリ・ユーザー/設定・監査） | `make backup`（必須） |
| 個人ファイル | `SHERPA_USERS_DIR`（既定 `data/users/`） | **一次データ** | tar（必須） |
| 意味レイヤの状態（旧・LLM 意味抽出・業務語↔コードの自動橋渡し） | `data/derived/{world}/semantic/`（`concepts.json`/`l_extract.json` 等） | **退役済み**（ソース正典化・2026-09-04で撤去。既存環境の残置ファイルは無害・新規生成はされない） | 対象外（バックアップ不要・`--with-derived` に含めなくてよい） |
| env ファイル | `.env` または `SHERPA_ENV_FILE` の指す先 | 設定（API キー含む） | 安全な場所に控えを保管（アクセス制限必須） |
| Neo4j グラフ / ES 索引 / `data/derived/{world}/`（`md/`・`rag/`・`ir/`） | ボリューム `neo4j`・`es`／派生の3層 | 派生物 | volume は `make backup` に含まれる。ローカル派生物は `--with-derived`（再取り込みでも再生成可） |

### バックアップ手順（日次を推奨）

リポジトリ直下で:

```bash
make backup ARGS="--stop --with-derived"  # 停止→3 volume＋個人領域＋派生物＋env
make start                                 # 日次運用では直後にサービスを戻す
```

`SHERPA_BACKUP_DIR` の既定は `data/backups`。本番では別ディスクの専用ディレクトリを指定します。
`--with-derived` は、再生成に時間のかかる派生物（写しMD・RAG正本・中間表現）を一緒に守るための日次運用向けです。

cron で毎日 3:00 に取り、14日分残す例（`crontab -e`。cron では `%` を `\%` にエスケープ）:

```cron
0 3 * * * cd /path/to/sherpa && bash -c 'make backup ARGS="--stop --with-derived"; rc=$?; make start; exit $rc'
```

保持世代の削除は、バックアップ先を別媒体へ複製できたことを確認してから、運用側のジョブで行います。
このスクリプト自身は古い世代を自動削除しません。

バックアップ先はサーバ本体と**別のディスク/マシン**に置きます（同じディスクではディスク障害と共倒れ）。

### リストア手順

```bash
make stop
make restore FROM=data/backups/<日時>          # sha256/対象/tar を事前検査→volume 作り直し→個人/派生を復元
make start
```

最後に、管理「資料フォルダ」で各フォルダを**「今すぐ更新」**して派生物（グラフ・索引・写しMD・RAG正本）を再生成します。

### RPO / RTO の目安

| 対象 | 失われうる範囲（RPO） | 復旧時間（RTO） |
|------|------------------------|------------------|
| PostgreSQL（会話・台帳・ユーザー・監査） | 最後のバックアップ以降（日次なら最大24時間） | volume 展開＋起動確認の時間（DB規模依存） |
| 個人ファイル（`SHERPA_USERS_DIR`） | 同上 | tar 展開の実行時間 |
| グラフ・索引・派生の3層（写しMD/RAG正本/中間） | **失われない**（再生成可能） | 再取り込み時間＝フォルダ規模に比例 |
| 登録フォルダ（原本） | 対象外（外部の正本） | — |

この既定（日次・RPO 24時間）で足りない場合は、バックアップの頻度を上げるのが最初の一手です。
RTO の絶対値は環境依存のため、**初回のリストア演習で実測し、この表に書き足してください**（演習せずに本番で初実行しない）。

## 定期点検（週次5分）

- `make status` が全緑か。
- `df -h` の空き（ES の watermark に近づいていないか）。
- `ls -lh data/run/api.log`（肥大していたら空にする）。
- `ls "$SHERPA_BACKUP_DIR"`（未設定なら `ls data/backups/`）— バックアップが**今日も増えているか**（取れているつもりで止まっているのが最悪パターン）。
- 取り込み一覧に「※ …失敗しました」の注意が出ていないか（[20-管理-取り込み](20-管理-取り込み.md)）。
