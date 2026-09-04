# Sherpa 独立・実サービスUI自動化

`ui_automation/` は、既存の `tests/e2e` と `tests/e2e_live` から独立したUI試験です。
Playwrightで実際のSherpaを操作し、PostgreSQL、Elasticsearch、Neo4j、AI、OCR、
VLM、Office変換を実サービスとして検証します。既存テストのfixtureやサーバはimportしません。

この試験ではAPI差し替え、通信intercept、疑似provider、定型の成功応答を使用しません。
テスト資料は実際にWorldへ登録・取り込みする入力であり、Sherpaの回答を偽装するものではありません。

## 実行前に確認すること

- Docker Engineと`docker compose`が利用できること
- `.venv`へ`ui_automation/requirements.txt`が導入済みであること
- `python -m playwright install chromium`を実行済みであること
- full/chatではCodex CLIがインストール済みで、実行専用env fileから実OpenAI資格情報を利用できること
- fullではOllama、Gemini、Bedrockの実資格情報・実モデルも利用できること
- fullではOCRモデル、VLM、OOXML変換、LibreOfficeも利用できること
- 実OpenAI、Gemini、Bedrock等を選ぶケースではAPI利用料とrate limitが発生すること

依存関係とChromiumは次のように準備します。

```bash
.venv/bin/python -m pip install -r ui_automation/requirements.txt
.venv/bin/python -m playwright install chromium
```

資格情報はリポジトリへ書かず、実行専用env fileまたは既存の安全な認証ストアから渡します。
`OPENAI_API_KEY`等の値、cookie、Authorization header、password、Codexの`auth.json`は
artifactへ保存しません。資格情報の有無と接続結果だけを記録します。

Codexはrun固有の空の`CODEX_HOME`を使うため、個人のglobal loginは再利用しません。
`OPENAI_API_KEY`がある場合だけ、そのrunの専用領域へ標準入力で一時ログインします。
キーを子processのcommand lineへ含めることはありません。

## 実行方法

全機能、全env profile、全実サービスを順番に実行します。

```bash
make test-ui-automation UI_AUTOMATION_ARGS="--env-file /absolute/path/to/ui.env"
```

用途別の入口もあります。

```bash
make test-ui-automation-smoke
make test-ui-automation-chat UI_AUTOMATION_ARGS="--env-file /absolute/path/to/ui.env"
make test-ui-automation-env UI_AUTOMATION_ARGS="--env-file /absolute/path/to/ui.env"
```

Python moduleとして直接実行する場合は次の形式です。

```bash
python -m ui_automation run --suite full --env-file /absolute/path/to/ui.env
python -m ui_automation run --suite env --profile env-precedence --profile auth-enabled
python -m ui_automation run --suite smoke --headed --case-timeout-ms 60000
```

利用できるoptionは次のとおりです。

- `--suite {full,smoke,chat,env}`: 実行するsuite
- `--env-file PATH`: 資格情報と外部接続先を読む専用env file。省略時にルート`.env`へfallbackしない
- `--profile NAME`: suite内のprofileを絞る。複数回指定可能
- `--headed`: Chromiumを画面表示して実行
- `--stack-timeout SECONDS`: 実stack起動の上限時間
- `--case-timeout-ms MS`: Playwright操作の上限時間
- `--retention N`: 保持するrun数。1以上、既定10

retry、既存stack流用、cleanup省略のoptionはありません。失敗を成功へ変える自動再試行は行いません。

## suiteの意味

- `full`: 機能台帳とenv台帳をすべて検証する。必須能力が一つでも不足すれば最終結果はFAIL
- `smoke`: 実FastAPI、実store、Chromiumと主要画面を短時間で確認する。全網羅の合格証明ではない
- `chat`: 実AIの質問送信、SSE、構造化実行トレース、永続化、監査、usageを照合する
- `env`: env profileごとにprocessを再起動し、既定・変更・境界・不正・優先順位・再起動を確認する

env/fullは軽量な代表値試験ではありません。台帳で`tested`の149変数ごとに6 scenarioを別profile、別process、
別の隔離stackとして順次実行するため、変数単独で894 profileを生成します。さらに固定pairwise 26 profileと
用途別profileを加え、現在はenv 932 profile、full 943 profileです。Docker image、AI/OCR処理、rate limit次第で
数時間以上かかり得ますが、この生成profile群をfull契約から外しません。絞り込み実行は診断用であり、
完全網羅の合格証明にはなりません。

ケース失敗、ブラウザ起動失敗、アプリ起動失敗、collection error、cleanup失敗があっても、
実行可能な後続profileは続けます。すべての結果を集計した後、一件でも失敗があれば非ゼロで終了します。
fullで前提不足、skip、xfail、証跡不足を合格として扱いません。

## 隔離

runnerはrunごとに次を作成します。

- `sherpa-ui-automation-`で始まる専用Compose project
- 空いている専用port
- 専用PostgreSQL、Elasticsearch、Neo4j volume
- 専用KB、users、derived、observation、OCR領域
- 専用`HOME`、`CODEX_HOME`、XDG cache/config/data、`TMPDIR`

普段の`SHERPA_COMPOSE_PROJECT=sherpa-mvp`、リポジトリ直下の`data/`、既存World、
個人workspaceへ接続した場合は開始前に停止します。cleanupはrunnerが作成したPID、container、volumeだけを
対象にし、失敗した場合も最終結果をFAILにします。

Dockerは`/var/run/docker.sock`のlocal Unix socketへ固定します。processや専用env fileに
`DOCKER_HOST`、`DOCKER_CONTEXT`、`DOCKER_CONFIG`、TLS関連overrideが一つでもあれば、値がlocal向けでも
ambient設定として開始前に拒否します。最初のDocker操作でsocket identityとdaemon IDのSHA-256だけを証跡化し、
以後のDocker/Compose操作も毎回同じsocket identityを確認します。

runtimeとapp PIDにはprocess start ticksを記録し、PID番号だけでは停止・retention判定をしません。異常終了runは
次回開始時にownership markerを検証し、appはPID＋start ticks、Docker資源はCompose project＋run固有owner labelの
両方が一致した場合だけ回収します。回収を完了できない場合でも、marker一致を確認したrun専用`profile.env`、
`CODEX_HOME`を含む`user-home/`、secret registryは独立したscrub処理で削除し、run全体をFAILにします。
再帰的な読取・権限変更・削除の直前には`/proc/self/mountinfo`で同一path以下のbind mountを含むmount targetを検査し、
一つでもあれば操作を拒否します。root所有物の回収用Docker bindも再帰mountを無効化します。
証跡の書込み、secret registryの追記、PID/markerの読取り、再帰chmod・削除は、dirfdと`O_NOFOLLOW`で開いたinodeを
所有者・mode・link数・device/inodeまで照合してから行います。既存inodeへの秘密追記はせず、新しい0600 inodeへ
全量を書いてatomic replaceします。hardlink、symlink、mount、root差替えを検出した場合はcleanupを含めFAILです。

## 機能と環境変数の台帳

- `config/coverage.yaml`: 全画面、役割、機能カテゴリ、UIが利用するendpoint、担当case
- `config/env_matrix.yaml`: runtime envの分類、scenario、restart境界、観測点、全factor値ペアを覆う決定的pairwise covering array（現在26 row）
- `config/capabilities.full.yaml`: suiteごとの必須実サービスと確認証跡

full開始時にHTML、ナビゲーション、JavaScriptの`fetch`・`EventSource`通信先に加え、製品Pythonに宣言された
FastAPI/APIRouter routeを静的走査します。現在84 routeのうち72本をUI surfaceへ対応付け、UIが直接使わない
互換・外部連携route 12本だけを詳細理由付きで分類しています。未登録の画面、操作、通信先、routeがあればFAILです。

env参照は`.env`のactive key、Pythonの`os.getenv`・`os.environ`・alias・`get`・`setdefault`・`pop`・subscript、
shell/Compose/Makeの`${VAR}`・`$VAR`を走査します。動的な変数名は解決できなければFAILとし、汎用env loaderのような
有限列挙不能な箇所だけをpath・function・参照種別・式・詳細理由で明示除外します。現在211変数を発見し、台帳245項目の
いずれかへ分類しており、未分類は0です。

`page.route()`だけでなく、CDP Fetch/Network interception、browser内のfetch/EventSource/XHR差替え、Service Worker、
外部HTTP test server起動もsource policyで拒否します。禁止能力を文字列連結やaliasで参照した場合も静的検査対象です。

pytest collectionが途中で失敗した場合も、機能台帳へ登録済みのcaseを事前実行planへunionします。実行証跡を
作れなかったcaseごとに独立した`result.json`を生成し、collection失敗を一つの集計エラーだけに縮退させません。

現在の機能台帳は58 case、自動走査で発見した263個の静的・動的操作要素を登録しています。
そのうち利用者が操作する261個はcaseのselectorへ紐付け、即時に生成・破棄される内部要素2個だけを
理由付きで除外します。実行後はcaseの成否だけでなく、
各スクリーンショットへ記録した実URL、認可role、viewport寸法を画面台帳へ突合します。admin、一般user、
anonymousとdesktop/narrowの宣言に実証跡がなければ、case自体が一部成功していてもfull coverageはFAILです。

分類だけではenv coverageを満たしません。`tested`の各変数について、既定値、有効値、境界値、
不正値、優先順位、再起動の各scenarioが`execution_coverage`の実profileへ対応し、そのprofileがPASSし、
case証跡が存在した場合だけ実行済みになります。一つでも未対応・未実行ならenv/fullはFAILです。

環境変数は「コマンドで明示した値 > `SHERPA_ENV_FILE` > 製品既定」の順で検証します。
初回だけDBへseedするAI資格情報等は、fresh DB、同じDBでenv変更、UI保存後の再起動を分けます。
`SHERPA_DISABLE_EMBED`は値ではなく存在で無効化されるため、`SHERPA_DISABLE_EMBED=0`も専用caseです。

## チャットと思考の流れ

ここでいう「思考の流れ」は、モデルの非公開な内部推論ではありません。画面とAPIが利用者へ公開する
質問理解、検索、tool実行、委譲、完了・失敗などの構造化実行トレースです。

チャットcaseはブラウザとは別の認証済みHTTP接続でも同じturnのSSEを保存し、次を照合します。

- `turn_id`と`conversation_id`
- node ID、表示順、進行中から完了・失敗へのstatus遷移
- `answer_delta`と最終answer
- UI、会話API、PostgreSQL trace、監査ログ、usage
- 生SSEが実際に保存上限を越えたrunでは、保持・集約・省略件数をPostgreSQL traceと厳密照合
- Codex/OpenAIの実provider、非ゼロusage、実tool node、session継続
- 停止、再読込、再購読、会話切替、同時実行制限、失敗後のturn slot解放

回答本文の完全一致は求めません。実際に取り込んだ小型Worldの固有事実、引用、scope、tool利用、
永続化を意味的に確認します。定型フォールバック、認証切れ、timeout、CLI異常終了を成功扱いしません。

通常の実AI turnは固定の120 node上限へ届かないことがあります。その場合も生SSE・UI・DBの通常相関caseは
正しく判定しますが、`runtime_evidence_coverage` の `persisted-trace-cap-after-real-turn` は
`NOT_PROVEN`となり、full全体はFAILです。上限を越えた実turnの証跡なしに「保存上限を検証済み」とは報告しません。

## 証跡

証跡は次の場所に保存されます。このディレクトリはGit管理外です。

```text
ui_automation/artifacts/<run-id>/<env-profile>/<feature>/<case-id>/
├── screenshots/010__feature-state.png
├── browser/console.jsonl
├── browser/page-errors.jsonl
├── browser/console-error-allowances.jsonl
├── browser/request-failures.jsonl
├── browser/request-failure-allowances.jsonl
├── browser/trace.zip
├── network/http.jsonl
├── network/navigation.jsonl
├── network/sse.jsonl
├── network/sse-timing.jsonl
├── services/app.log
├── services/compose.log
├── state/db-summary.json
├── state/files.sha256
└── result.json
```

run直下にはHTML、JSON、JUnit、機能coverage、env coverage、失敗一覧を保存します。
スクリーンショット名は`順番__機能-状態.png`とし、名前だけで場面を判断できるようにします。
通常は直近10 runを保持します。

Chromiumの`ERR_ABORTED`はそれだけで成功扱いしません。同一page/frameの成功したmain document responseと
実際のnavigation commitに挟まれ、遷移元のrequestと一意に相関できた中断だけをexpectedにします。
テストが意図的に通信を中断する場合は、case固有のmethod・URL path・resource type・失敗値・理由を
完全一致で一度だけ許可し、0回または2回以上の一致はFAILにします。判定と許可は上記2つのJSONLへ保存します。

Chromiumの汎用console errorは文面だけで許可しません。`message.location.url`、page、event順、狭い時間窓、
実responseまたはrequest failureのIDを一対一で照合し、URL欠落や同一URL候補の重複はFAILにします。認証前の
画面初期化中だけ、`/health/summary`、`/auth/me`、`/chat/turns/running`、`/auth/login`の実401を同じ方法で
相関します。case固有のHTTP error許可もmethod・path・status・理由・期待件数を完全一致で証跡化します。

artifact生成後にsecret形式を走査します。漏えい候補を見つけた証跡は残さず、場所と件数だけを
redaction reportへ記録し、そのcaseをFAILにします。実際のsecret値を調査ログへコピーしないでください。
各PNGにはcapture時のDOM走査・mask・SHA-256証明を付け、Playwright traceではpixel screenshotを無効化した
うえでDOM/network記録をsanitizeし、trace内に画像frameやsecret候補が残っていないことも検査します。
open/nested Shadow DOM内の検出要素にはcapture中だけinline maskを直接適用し、closed Shadow hostは
`attachShadow`の事前hookで追跡します。固定色pixel canaryが保存PNGへ一画素も残らないことと、capture後に
元のopacity・priority・`style`属性の有無が完全復元されることもsmokeで検証します。

pytestのJUnitはXMLを文字列置換せず、要素text・tail・attributeをparse後にredactして再serializeします。
そのため`<redacted>`等の置換値がXMLを壊した場合も成功扱いせず、再parseと件数照合に失敗すればFAILです。
