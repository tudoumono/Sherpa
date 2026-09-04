"""LLM 呼び出しの共通配管（旧・意味層抽出は GRAPH-SRC 2026-09-04 で撤去）。

かつては文書 → ナレッジグラフの意味層フル抽出（`extract_world`・l_extract.json）を担っていたが、
`docs/proposals/2026-09-04-グラフのソース正典化.md`（K9-K11）でその抽出本体（LLM 呼び出しの
成否に関わらず概念ごと）を撤去した——事前計算に残すのは決定的（再現100%）に計算できる構造
（骨格グラフ＋辞書突合による言及エッジ）だけにし、文意の解釈はクエリ時のエージェントへ移した
（`docs/03-鏡モデル.md`／`docs/05-グラフ語彙.md` 参照）。

本モジュールはそれでも残る**プロバイダ選択・送信・秘密マスクの共通配管**——`available()`/
`complete_json()`/`_call`/`_probe`/`_mask_secrets`/`_redact_reflected_urls`/`_safe_detail`/
`_http_detail`/`_error_detail`/`_log_masked_exception` は現役の消費者（`intent_llm.py`・
`ingest/llm_render.py`（rag.md の LLM 成形・usage="render"）・`health.py`・
`routers/system.py`/`system_extras.py`（接続テスト）・`providers/base.py`・`ext_api.py`・
`agentic_search.py`・`metering.py`・`doctor_checks.py`）が共用するため残置する（ファイル名も
importer が多いため変更しない）。撤去済みの抽出本体（`extract_world`・`concept_propose` 一族・
`REALIZES` エッジ・`aliasmap`）は復活させない（CLAUDE.md 退役リスト参照）。
"""
from __future__ import annotations

import bisect
import json
import re
import time
import urllib.error
from urllib.parse import quote, quote_plus

from .. import llm

_TIMEOUT = 90
_BEDROCK_MAX_TOKENS = 8000      # Bedrock（Anthropic API は max_tokens 必須）: 1回分の JSON 応答として十分な上限


def available(settings: dict | None = None, *, system_settings: dict | None = None,
             strict: bool = False, usage: str = "extract") -> dict | None:
    """取り込みパイプラインの LLM 呼び出し（現在の唯一の消費者は `sherpa/ingest/llm_render.py`＝
    rag.md の LLM 成形）に使う LLM 設定を返す（無ければ None）。旧・意味層フル抽出
    （`extract_world`）は GRAPH-SRC（2026-09-04・K9-K11）で撤去済み——本関数はチャットの
    頭脳(agent)とは独立に管理者の選択中のクラウドプロバイダ（A7・`sherpa.keys.selected_cloud_provider`）
    で選ぶプロバイダ解決の共通配管として残る。

    管理者の選択中のクラウドプロバイダ（A7・`sherpa.keys.selected_cloud_provider`）で選ぶ
    （bedrock が選択中なら auto でも試す）。A7 を明示的に選んでいる
    （`sherpa.keys.cloud_provider_explicitly_selected`）のにそのプロバイダで解決できない場合は
    Ollama へ倒さず未接続（`llm_unavailable`）扱いにする——クラウドを一度も選んでいない構成の
    ときだけ Ollama へ自動フォールバックする（FBK-1・fail-loud・
    `llm.select_provider`/`resolve_auto_provider` 参照）。OpenAI/Gemini/Bedrock はテキストのみ送信。
    個人設定のプロバイダ/モデル選択は読まない（管理者のカタログ/選択が唯一の真実源）。

    `system_settings`（省略可）: 呼び出し側が既に読んだスナップショットを渡すと、bedrock factory
    （下記 `B()`）の A7 判定・キー解決と `llm.select_provider()` 内部の判定を**同じ**スナップ
    ショットで行う（省略時は自分で読む）。

    `strict`（既定 False）: `llm.select_provider(strict=...)` へそのまま転送する（意図しない課金の
    是正）。実際に LLM へ送信する呼び出し元（`llm_render.py`）は `strict=True` を渡し、
    `InvalidCloudProviderConfigError` を自分の `llm_error` 状態へ変換する。

    `usage`（既定 `"extract"`）: `model_catalog.resolve_model` へそのまま渡すカタログ用途キー。
    `extract` は GRAPH-SRC（2026-09-04）で `model_catalog.USAGES` の一級市民からは外れたが、
    `_DEFAULT_CATALOG`/後方互換読み取りの対象としては残る（唯一の実消費者
    `sherpa/ingest/llm_render.py` は `usage="render"` を明示的に渡す——render 未設定の環境は
    `model_catalog._USAGE_FALLBACK` によりこの extract セルの解決結果へ自動的にフォールバックする）。
    """
    s = settings or {}
    from .. import model_catalog, store as _store
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()

    def G(key):
        return {"provider": "gemini", "key": key,
                "model": model_catalog.resolve_model("gemini", usage, None, system_settings=sys_s)}

    def O(key):
        return {"provider": "openai", "key": key,
                "model": model_catalog.resolve_model("openai", usage, None, system_settings=sys_s),
                # `complete_json` の送信時接続先解決も同じスナップショットで揃える。
                "openai_endpoint_override": sys_s}

    def L(url):
        return {"provider": "ollama", "url": url,
                "model": model_catalog.resolve_model("ollama", usage, None, system_settings=sys_s)}

    def B():
        from .. import agents                      # 遅延 import（Bedrock ヘルパを再利用・循環回避／anthropic SDK を常時 import しない）
        from .. import keys as _keys
        # A7（クラウドプロバイダ排他選択）: bedrock が選ばれていなければ、SigV4 等の静的な手掛かりが
        # 端末にあっても使わない（`providers/__init__.py::_select_provider` の bedrock 分岐と同じゲート）。
        if _keys.selected_cloud_provider(sys_s) != "bedrock":
            return None
        api_key = _keys.resolve_api_key("bedrock", s, system_settings=sys_s)
        if not agents._bedrock_auth_available(api_key):
            return None                              # 認証未解決＝未接続扱い（llm_unavailable）
        # region は常に東京固定（利用者設定からは読まない・`_bedrock_region` が唯一の真実源）。
        return {"provider": "bedrock", "region": agents._bedrock_region(),
                "model": s.get("bedrock_model") or agents._BEDROCK_MODEL, "api_key": api_key}

    return llm.select_provider(settings, openai=O, gemini=G, ollama=L, bedrock=B,
                               system_settings=sys_s, strict=strict)


def complete_json(system: str, user: str, cfg: dict, timeout: int = _TIMEOUT) -> str:
    """1回の補完（JSON文字列を返す）。OpenAI/Gemini/Ollama/Bedrock 対応。**テストはこの関数を差し替える**。

    `timeout` は呼び元で短縮可（既定は抽出用 `_TIMEOUT`＝90s。intent 分類など軽い用途は短くする）。

    S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 各分岐でレスポンスを一旦ローカル変数に
    捕捉し、`metering.acc_add` へ渡す（シグネチャ・返り値は無変更＝既存のモンキーパッチシーム）。
    `acc_add` は呼び出し元が `metering.acc_begin()` でスコープを開いていなければ no-op のため、
    health.py/settings_test のような手組み cfg でのプローブ呼び出しは自動的に計測対象外のまま。
    """
    from .. import metering
    if cfg["provider"] == "gemini":
        data = llm.post_json(llm.gemini_url(cfg["model"]), llm.gemini_headers(cfg["key"]), {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        }, timeout)
        metering.acc_add(metering.usage_from_gemini(data))
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if cfg["provider"] == "openai":
        # SET-2c: `cfg["openai_endpoint_override"]`（省略可）が渡されていれば、DB の system_settings
        # の代わりにそれで接続先/ヘッダを組み立てる（管理画面の接続テストが、保存前の入力中の値で
        # その場だけ試すための経路・DB は書かない・省略時は通常どおり DB を読む）。
        _endpoint_override = cfg.get("openai_endpoint_override")
        # temperature は送らない（gpt-5.5 系は既定値(1)以外を拒否し 400 になる・2026-08-15 実測）。
        # 送信は `llm.openai_post_json`（OpenAI 専用の送信直前ガード付き・`post_json` は
        # Gemini/Ollama とも共用のため一律遮断しない）。intent 分類（`intent_llm.py`）もこの
        # 関数へ委譲するため同じガードの対象になる。
        resp = llm.openai_post_json(llm.openai_url("chat/completions", system_settings=_endpoint_override),
                             llm.openai_headers(cfg["key"], system_settings=_endpoint_override), {
            "model": cfg["model"],
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, timeout)
        metering.acc_add(metering.usage_from_openai_chat(resp))
        return resp["choices"][0]["message"]["content"]
    if cfg["provider"] == "bedrock":
        from anthropic import AnthropicBedrock                # 遅延 import（bedrock 選択時のみ SDK を読み込む）
        from ..providers.bedrock import _bedrock_runtime_base_url
        # `base_url` を明示する（`providers/bedrock.py::_bedrock_runtime_base_url` docstring
        # 参照）: 省略すると SDK が env `ANTHROPIC_BEDROCK_BASE_URL` を読んで接続先を上書きできる。
        client = AnthropicBedrock(api_key=cfg.get("api_key") or None, aws_region=cfg["region"],
                                  base_url=_bedrock_runtime_base_url(), timeout=timeout)
        resp = client.messages.create(model=cfg["model"], max_tokens=_BEDROCK_MAX_TOKENS,
                                      system=system, messages=[{"role": "user", "content": user}])
        metering.acc_add(metering.usage_from_anthropic(getattr(resp, "usage", None)))
        # temperature/top_p/top_k/thinking は送らない（例: jp.anthropic.claude-haiku-4-5 系では 400）・プレフィル無し。
        return "".join(b.text for b in (resp.content or []) if getattr(b, "type", None) == "text")
    resp = llm.post_json(llm.ollama_url(cfg["url"], "/api/chat"), llm.JSON_HEADERS, {
        "model": cfg["model"], "stream": False, "format": "json", "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, timeout)
    metering.acc_add(metering.usage_from_ollama_chat(resp))
    return resp["message"]["content"]


_DEPLOYMENT_NOT_FOUND_HINT = "（Azure OpenAI: モデル名の欄にはデプロイ名を入力してください）"

_HTTP_ERROR_BODY_MAX_BYTES = 65536   # 上流のエラー本文読み込みバイト数の上限（64KiB・メモリ上限対策）。


def _http_detail(e: urllib.error.HTTPError,
                 system_settings: dict | None = None) -> tuple[str, str | None]:
    """OpenAI/Gemini の HTTP エラー本文から code/status/message を抜いて短い理由に整形する（**未マスク・
    未切断のまま返す**＝秘密の伏せ字・長さ調整は呼び出し元の `_safe_detail` が担う。呼び出し元は
    `_http_detail` を直接使わず `_safe_detail` を経由すること）。

    戻り値は `(本文, 案内文または None)` の組。案内文を本文へ連結して1本の文字列にしてしまうと、
    `_safe_detail` が全体を一律に切断した際、本文が長い場合に案内文ごと切り落とされうる
    （表示上重要な案内が消える回帰）。`_safe_detail` 側が「案内文の長さを差し引いた分だけ本文を
    切ってから案内文を付ける」ことで、案内文は必ず残る。

    接続先が Azure OpenAI（`llm.openai_endpoint_kind()=="azure"`）で、HTTP 404・本文の `error.code` が
    `DeploymentNotFound` のときは「モデル名にはデプロイ名を入れてください」と読める案内を返す。
    Azure OpenAI は `model` にモデル名ではなく管理者が作成した**デプロイ名**を要求するため、OpenAI
    本家の感覚で `gpt-5.5` 等をそのまま入れると 404 になる（実 API を叩かずに判別できないため、
    probe が受けたエラーをここで共通に読み替える）。

    `system_settings`（省略可）: 省略時は `llm.openai_endpoint_kind()` がその場で DB を読み直す
    ＝この HTTP エラーを実際に起こした**送信時のスナップショット**とは
    別の読みになりうる（エラー処理までの短い窓で admin が接続先を保存すれば、送信時は Azure
    だったのに案内判定は「本家に戻った後」の kind を見てしまう食い違いが起こり得た）。呼び出し元
    （`_safe_detail`）が `cfg["openai_endpoint_override"]` をそのまま渡せば、送信に使ったのと
    同じスナップショットで判定する。
    """
    try:
        # 上流のエラー本文サイズは信用しない（`e.read()` に無上限で読ませると、悪意/誤動作した
        # 上流が巨大な本文を返した場合にメモリを消費し尽くしうる）。既存の切断境界処理
        # （`_DETAIL_MAX_LEN_HTTP` 等）はマスク後の**文字数**を切るだけで、マスク前の読み込み
        # 自体には上限が無かったため、ここで読み込みバイト数そのものに上限を設ける。
        err = json.loads(e.read(_HTTP_ERROR_BODY_MAX_BYTES)).get("error", {})
        code = err.get("code") or err.get("status") or err.get("type") or ""
        detail = f"{e.code} {code}: {err.get('message', '')}".strip()
    except Exception:
        return f"HTTP {e.code}", None
    try:
        _is_azure = llm.openai_endpoint_kind(system_settings) == "azure"
    except ValueError:
        # `system_settings` の openai_endpoint_kind/openai_base_url が非文字列の破損値だと
        # `openai_endpoint_kind()` は ValueError を送出しうる。この判定は「案内文を
        # 付けるかどうか」の付随情報にすぎないため、失敗時は付けない側へ倒す（detail 自体は
        # 既に確定済み・ここで例外を伝播させて `_http_detail` の呼び出し元を落とさない）。
        _is_azure = False
    hint = _DEPLOYMENT_NOT_FOUND_HINT if (
        e.code == 404 and str(code) == "DeploymentNotFound" and _is_azure) else None
    return detail, hint


def _error_detail(e: Exception, *, secret: str | None = None) -> str:
    """`urllib.error.HTTPError` 以外の例外→短い理由文。Bedrock（Anthropic SDK）の
    `APIStatusError`/`APIConnectionError` は agents.py の既存ヘルパ `_safe_bedrock_detail`（マスクして
    から切断する安全境界・明示キー`secret`と env キーの両方を伏せる）を再利用する（anthropic 未導入
    でも壊れないよう try/except）ため、この分岐の戻り値は**既にマスク・切断済み**（`_http_detail` と
    違い呼び出し元 `_safe_detail` の再マスク・再切断を要しない・二重適用しても無害）。
    それ以外は従来どおり型名＋メッセージ（未マスク・未切断のまま返す＝`_safe_detail` 経由が必要）。
    メッセージ自体（`str(e)`）は `_http_detail` の `e.read()` と異なり呼び出し元がサイズを
    制御できないため、ここで `_HTTP_ERROR_BODY_MAX_BYTES` と同じ上限で切り詰める（マスク前の
    段階でサイズを抑える＝`_redact_reflected_urls` の引用符 pre-pass が極端に長い入力を
    処理しなくて済むようにする・詳細は `_mask_quoted_url_spans` docstring 参照）。"""
    try:
        import anthropic
    except ImportError:
        return f"{type(e).__name__}: {str(e)[:_HTTP_ERROR_BODY_MAX_BYTES]}"
    if isinstance(e, (anthropic.APIStatusError, anthropic.APIConnectionError)):
        from .. import agents
        return agents._safe_bedrock_detail(e, key=secret)
    return f"{type(e).__name__}: {str(e)[:_HTTP_ERROR_BODY_MAX_BYTES]}"


def _call(system: str, user: str, cfg: dict, attempts: int = 3) -> str:
    """`complete_json` を **429（レート/クォータ）だけ指数バックオフで再試行**（一時的な RPM 超過を吸収）。"""
    delay = 2.0
    for i in range(attempts):
        try:
            return complete_json(system, user, cfg)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < attempts - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(float(ra) if (ra and str(ra).isdigit()) else delay, 30))
                delay *= 2
                continue
            raise


# Bearer/api-key の値は区切り（空白・カンマ・セミコロン・引用符）で止める。貪欲な `\S+` だと
# 「Bearer key,code=invalid_api_key」のような値の後ろに続く分類情報まで飲み込んで消してしまう。
_BEARER_RE = re.compile(r"Bearer\s+[^\s,;\"']+", re.IGNORECASE)
_API_KEY_HEADER_RE = re.compile(r"api-key[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")

# 上流（custom/Azure OpenAI 互換エンドポイント）がエラー本文へ要求 URL を echo することがある。
# admin だけが設定できる base URL は path にリソース名/デプロイ名を、query に
# `openai_api_version` を含みうるため、一般ユーザー向け応答・health ログへそのまま出さない。
# stdlib（`http.client`/`urllib`）は URL に空白が混入すると `InvalidURL` の理由文へ**要求パス**
# （request-target・scheme/host を持たない `/openai/deployments/...` 形。実機では単引用符で
# 囲んで反射される＝`'/openai/deployments/...'`。空白を含む request-target（デプロイ名にスペースが
# 混じった場合等）はその空白ごと引用符内に収めて反射される）をそのまま乗せることがあり、この形は
# scheme も percent-encoding も持たないため、通常の URL 指標では検出できない。
#
# 契約（fail-closed のトークンマスク方式）: エラー詳細を
#   0. 引用符区間の pre-pass（`_mask_quoted_url_spans`）: `'...'`/`"..."`/`「...」`/`『...』` で
#      対称に囲まれた区間を1単位として走査し、区間の中身に URL 指標（`_quoted_content_has_url_indicator`）
#      があれば区間全体を `[URL]` に伏せる（引用符自体は地の文として保持する）。単語分割（空白
#      区切り）より先に行う: `InvalidURL` は空白入りの request-target を引用符で囲んで反射する
#      ため、単語単位のままだと空白の位置で分断され後半が指標判定をすり抜けてしまう。対応する
#      閉じ引用符が見つからない非対称な入力（通常の英文中のアポストロフィ等）はこの pre-pass の
#      対象にせず、そのまま次段の単語単位の処理に委ねる。
#   1. 空白区切りの「単語」に分け、URL の指標（平文/encoded scheme・DSN・`mailto:`/`data:`/
#      `file:/` 等・URL 構造文字の percent-escape・多段の平文絶対パス）を含む単語を見つけたら
#      **その単語全体**を伏せる（送信時スナップショットの実効 base URL かどうかは区別しない＝
#      正当な案内 URL も含め一律・診断価値より漏洩防止を優先する）。文字集合を列挙して「URL の
#      終端」を当てにいく方式は、新しい形が見つかるたびに文字集合を拡張し続ける必要があり、かつ
#      拡張するほど過剰マスク（日本語文・通常の平文語の巻き込み）のリスクも上がるため、単語境界
#      （空白）そのものを終端に使うこの方式へ単純化した:
#     a. 範囲確定を先に行う: 単語の前後にある引用符・開き括弧（先頭・全角引用符/括弧
#        `「『【（〈≪` を含む）、句読点・引用符（末尾・`_TRAILING_PUNCT` 参照・全角の閉じ側
#        `」』】）〉≫` を含む）を本体から切り離す（`_split_leading_punct`/
#        `_split_trailing_punct_word`・開き括弧と対応する閉じ括弧は対で保持する＝
#        `_pair_outer_brackets`）。実機の `InvalidURL` は request-target を引用符で囲んで反射する
#        ため、この分離を**指標判定より先に**行わないと、引用符に隠れた本体（`'/openai/...'` 等）
#        が指標判定をすり抜けてしまう。
#     b. 指標判定（`_word_has_url_indicator`）は分離後の本体（`core`）に対して行う: 平文 scheme
#        （RFC3986 の scheme 文法＝英字で始まり英数字/`+`/`.`/`-` が続く任意の scheme＋`://`。
#        `postgresql://`/`redis://`/`bolt://` 等の DSN も検出対象にする＝userinfo にパスワードを
#        含みうるため）／encoded scheme（`http(s)` 限定・scheme 区切りの `:`・`//` がそれぞれ独立に
#        平文／単純encoded(`%3A`/`%2F`)／二重encoded(`%253A`/`%252F`) のいずれでもよい）／`scheme:/`
#        （単一スラッシュ・`file:/etc/passwd` 等）／`mailto:`・`data:` 等スラッシュを伴わない
#        明示列挙 scheme（本体が続く場合のみ）／URL 構造文字の percent-escape（`%2F`/`%3A`/`%3F`/
#        `%3D`/`%26`・単純/二重encoded・hex 大小混在）を1個以上含むかどうかだけを見る（左境界は
#        問わない＝`openai%2Fdeployments%2F...` のように直前が英数字でも捕捉する）。ただし本体
#        全体が「数字（全角含む）と `%2F` の連なり」（日付/割合形・`_DIGIT_ENCODED_SLASH_ONLY_RE`）
#        に一致する場合だけは除外する（`50%2F100`・`2026%2F08%2F26` 等の誤検出を防ぐ・大小不問）。
#        scheme を伴わない多段の平文絶対パス（`/` 区切りの非空セグメントを合計2個以上含む＝
#        連続する空セグメント〈連続スラッシュ〉は無視して数える・request-target の反射対策）も
#        指標にする。非空セグメントが1個以下のパス（`/tmp`・`/tmp/` 等）は対象外＝通常の Unix
#        パス言及の診断価値を保つが、2個以上は request-target の反射と区別できないため診断価値
#        より漏洩防止を優先し伏せる（許容するトレードオフ）。
#     c. 置換: 本体が percent-encoding を一切含まない純粋な平文 URL（`http(s)://` 始まり）なら
#        `llm._redact_url_for_error()` で `host[:port]` へ縮約する。それ以外（`http(s)` 以外の
#        scheme＝DSN・`mailto:`/`data:`/`file:/` 等・encoded scheme・URL 構造文字の
#        percent-escape・平文 scheme + encoded path の混在・平文絶対パスのいずれか）はデコードや
#        部分保持を試みず本体を丸ごと `[URL]` にする（`_is_pure_plain_url` docstring 参照・
#        「正規化した写し」を作らない）。
# 空白を含まない日本語文中に URL が続くと、指標を含む単語＝文全体になり後続の文章ごと消える
# ことがある（漏洩防止を優先し、この場合の可読性低下は許容する）。
_PLAIN_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)   # host[:port] へ縮約してよい scheme

# 指標判定用の汎用 scheme（RFC3986 の scheme 文法＝英字で始まり英数字/`+`/`.`/`-` が続く）。
# `postgresql://admin:db-secret@db.internal/app` のような DSN（http/https 以外）は `_PLAIN_SCHEME_RE`
# では検出できない（userinfo にパスワードを含みうる）ためこちらで検出する。`_is_pure_plain_url` の
# host 縮約対象は `_PLAIN_SCHEME_RE`（http/https のみ）のまま維持し、それ以外の scheme は
# 検出はするが host すら残さず本体を丸ごと `[URL]` にする（userinfo/パスワードを確実に消す＝
# host 縮約は「元の scheme が https 前提で構築された安全な表現」という `_redact_url_for_error`
# の設計意図を http/https 以外にまで広げない）。
_GENERIC_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# `_GENERIC_SCHEME_RE` は `scheme://`（`//` 必須）限定のため、`//` を伴わない scheme 表記
# （`mailto:`／`data:`／`file:/`）は指標判定を素通りする。`data:` は任意ペイロード（base64 等）を
# 含みうるため危険度が高く、fail-closed の契約（URL は一律マスク）に沿って対象を広げる。
# 誤検出防止のため対象は明示列挙に限定し、かつ左境界（直前が `\w` でないこと）を要求する
# （`\w` は Unicode 対応）＝`metadata:value`・`notdata:payload` のように scheme 名が他の単語の
# 語尾に偶然一致するケースを除外する。`12:30` のような時刻表記・`注: ...` のような日本語文中の
# コロンは、scheme 名が既知列挙に含まれない限りそもそも一致しない。
#
# `_split_trailing_punct_word` が句読点クラスタを本体から切り離す前の `rest`（`_sub_word` 参照）
# に対して検索する契約: 本体（`core`）に対して検索すると、body がすべて句読点扱いの文字
# （`,` 等）だけの場合にクラスタごと剥がされて `core` の colon 直後が空になり、`\S` 要求が
# 満たせず取りこぼす（`data:,,` 等）。`rest` はこの句読点クラスタを含んだままなので取りこぼさない。
_EXPLICIT_BODY_SCHEME_RE = re.compile(r"(?<!\w)(?:mailto|data):\S", re.IGNORECASE)
# `file:` は `file:///path`（`//` あり＝`_GENERIC_SCHEME_RE` が検出）に加え `file:/path`
# （`/` 1個だけの短縮形）も実際に使われる。`file://` を二重に拾わないよう直後の `/` を否定先読みで除く。
# 左境界の理由は `_EXPLICIT_BODY_SCHEME_RE` と同じ（`profile:/etc/config` のような偶然一致を除外）。
_FILE_SCHEME_SINGLE_SLASH_RE = re.compile(r"(?<!\w)file:/(?!/)", re.IGNORECASE)

# `//host` 形（scheme 省略の protocol-relative URL）。`_PLAIN_MULTI_SEGMENT_PATH_RE` は非空
# セグメントを2個以上要求するため、path を伴わない `//user:PASS@host.internal?token=...`・
# `//host.internal#TOPSECRET` のような形（userinfo・query・fragment・ドット付き host のみで
# path が無い）は検出できない。userinfo（`@`）・query（`?`）・fragment（`#`）のいずれかを含む、
# または「`/` を含まない＝path 無しのホスト単体」かつドットを含む場合を対象にする（`//tmp`
# のような path を伴う多段形は既存の `_PLAIN_MULTI_SEGMENT_PATH_RE` に委ねる）。
_PROTOCOL_RELATIVE_MARKER_CHARS = "@?#"


def _is_protocol_relative_url(word: str) -> bool:
    """`word` が `//` で始まる protocol-relative URL（scheme 省略形）の指標を持つかどうか。
    `_PROTOCOL_RELATIVE_MARKER_CHARS` 参照。"""
    if not word.startswith("//") or len(word) <= 2:
        return False
    rest = word[2:]
    if any(c in rest for c in _PROTOCOL_RELATIVE_MARKER_CHARS):
        return True
    return "/" not in rest and "." in rest

_PCT = r"%(?:25)?[0-9A-Fa-f]{2}"   # 単純 percent-encoding（`%XX`）、または二重エンコード
# （`%2XX` の "%" 自体がさらに `%25` へ encode された `%25XX` 形）の1バイト。
_ANY_PCT_RE = re.compile(_PCT)   # 置換方式の判定用（`_is_pure_plain_url` 参照・位置は問わない）。
_ENC_COLON = r"(?::|%3A|%253A)"      # scheme 区切りの ":" 相当（平文／単純encoded／二重encoded）
_ENC_SLASH = r"(?:/|%2F|%252F)"      # "/" 相当（同上）
_ENCODED_SCHEME_RE = re.compile(rf"https?{_ENC_COLON}{_ENC_SLASH}{_ENC_SLASH}", re.IGNORECASE)

# URL 構造文字（`/`・`:`・`?`・`=`・`&`）の percent-escape を1個以上含むかどうか（単純/二重encode・
# hex 大小混在を吸収）。scheme を伴わない断片（`%2Fopenai%2Fdeployments%2F...`）・query 相当の
# 断片（`%3Fapi-version%3D...`）のどちらも、この1本で検出する（区切り文字の種類やセグメントの
# 中身〈Unicode・`;` 等〉は問わない＝左境界も見ない）。
_URL_PCT_INDICATOR_RE = re.compile(r"%(?:25)?(?:2F|3A|3F|3D|26)", re.IGNORECASE)

# 上の指標が日付/割合表記（`2026%2F08%2F26`・`50%2F100`・全角数字版含む）にも反応してしまうため、
# 本体**全体**がこの形（数字の連なりが `%2F` だけで区切られている）に一致する場合だけ除外する
# （大小不問）。本体の一部に日本語等の他の文字が混じっていれば除外されない＝地の文と結合した
# トークン全体は通常どおり伏せる対象になる。
_DIGIT_ENCODED_SLASH_ONLY_RE = re.compile(r"^[0-9０-９]+(?:%2[Ff][0-9０-９]+)+$")

# scheme を伴わない多段の平文絶対パス（例 "/openai/deployments/my-secret-deploy"）。stdlib の
# `InvalidURL`（URL に空白が混入した場合の反射）は scheme/host を持たない request-target を
# そのまま理由文へ乗せることがあり、これは平文かつ percent-encoding も伴わないため上の指標では
# 検出できない。「`/` 区切りの非空セグメントを合計2個以上含む」ことを指標にする（区切りの `/` は
# 1個以上の連続でよい＝`/openai//deployments/x`・`//host/openai/x` のように連続スラッシュ
# （空セグメント）を挟む反射でも、非空セグメントの数は変わらないため検出できる）。1セグメントの
# パス（`/tmp`・`/tmp/` 等・末尾に `/` が付くだけの形を含む）は対象外にする＝通常の Unix パス
# 言及の診断価値を保つため。
_PLAIN_MULTI_SEGMENT_PATH_RE = re.compile(r"^/+[^\s/]+(?:/+[^\s/]+)+")

# 単語の前後で本体から切り離す文字（切り離した側は地の文としてそのまま残す）。全角の引用符・
# 括弧の開き側（「『【（〈≪）も ASCII と同列に含める＝`「(https://host)。」` のような全角/半角の
# 入れ子でも先頭側が正しく分離され、`_pair_outer_brackets` が対応する閉じ側を対で保持できる。
_LEADING_PUNCT = "'\"([{<" + "「『【（〈≪"
# 保持する末尾クラスタは「閉じ括弧・閉じ引用符・文末記号」に限定する（`;`/`!`/`?`/`#` は含めない
# ＝これらは query 区切り・フラグメント開始・文の区切りとして URL データ側に現れることが多く、
# 句読点として無条件に地の文へ戻すと `?token=;;;` が `host;;;`・`#!!!` が `host!!!` のように
# データの残骸を出力へ残してしまう＝`_split_trailing_punct_word` 参照）。`]`/`}` も含めない＝
# IPv6 リテラルの閉じ角括弧との衝突を避けるため（対応する開き括弧がある場合だけ
# `_pair_outer_brackets` で個別に保持する）。全角の閉じ括弧（`）〉≫」』】`）は、対応する
# `_LEADING_PUNCT` の全角の開き側と対称に本体から切り離す対象に含める（全角文字は IPv6
# リテラルとの衝突が無いため無条件でよい）。
_TRAILING_PUNCT = ".,)'\"" + "。、」』】" + "）〉≫"

# 先頭の開き括弧を分離した場合、対応する閉じ括弧が本体末尾に残っていればそれも地の文側へ移す
# （`_pair_outer_brackets` 参照）。全角の主要な引用符・括弧の対も含める（`_LEADING_PUNCT`/
# `_TRAILING_PUNCT` の全角文字は基本ケースを自然にカバーするが、末尾クラスタ長の上限
# （`_TRAILING_PUNCT_MAX_LEN`）を超える／間に他の文字が挟まる等で自然な対称処理が崩れる場合の
# 保険として、対応関係を明示しておく）。
_PAIRED_BRACKETS = {
    "(": ")", "[": "]", "{": "}",
    "「": "」", "『": "』", "【": "】", "（": "）", "〈": "〉", "≪": "≫",
}


def _word_has_url_indicator(word: str) -> bool:
    """`word`（`_split_leading_punct`/`_split_trailing_punct_word` 適用後の本体＝空白・前後の
    引用符/句読点を含まない）が URL の指標（平文/encoded scheme・protocol-relative（`//host`）・
    URL 構造文字の percent-escape・多段の平文絶対パスのいずれか）を含むかどうか。呼び出し側が
    本体全体を伏せるかどうかの判定にだけ使う（範囲はここでは決めない）。

    `file:/`・`mailto:`/`data:`（`_FILE_SCHEME_SINGLE_SLASH_RE`/`_EXPLICIT_BODY_SCHEME_RE`）は
    ここに含めない: これらは句読点クラスタで body 全体が本体から切り離される前の `rest` に対して
    検索する必要があるため（`_sub_word` 参照）、`core` だけを受け取るこの関数では判定できない。"""
    if _GENERIC_SCHEME_RE.search(word) or _ENCODED_SCHEME_RE.search(word):
        return True
    if _PLAIN_MULTI_SEGMENT_PATH_RE.search(word) or _is_protocol_relative_url(word):
        return True
    if _URL_PCT_INDICATOR_RE.search(word):
        return not _DIGIT_ENCODED_SLASH_ONLY_RE.match(word)
    return False


_QUOTE_BOUNDARY_CHARS = frozenset("'\"「」『』")


def _is_word_internal_quote_char(content: str, i: int) -> bool:
    """`content[i]` が `don't`/`team's` のような語中の記号（実際の区間境界ではない）かどうか。
    ASCII の対称引用符（'/"）のみが対象: 直前・直後の**両方**が \\w のときに限り語中とみなす。
    直前直後のどちらかが \\w でない場合（別の引用符・空白・行頭行末等に隣接する場合）は語中の
    記号ではなく区間の境界（貪欲対付けで独立した2区間が空白を挟まず併合された継ぎ目等）とみなす。
    全角の引用符（「」『』）は語中で使われる構造が無いため常に境界（自己対称でない＝
    `_is_quote_opener_position` と同じ前提）。"""
    ch = content[i]
    if ch not in ("'", '"'):
        return False
    before = content[i - 1] if i > 0 else ""
    after = content[i + 1] if i + 1 < len(content) else ""
    return bool(_WORD_CHAR_RE.match(before)) and bool(_WORD_CHAR_RE.match(after))


def _split_on_quote_boundaries(content: str) -> list[str]:
    """`content` を空白、および語中でない引用符文字（`_is_word_internal_quote_char` が偽の
    位置）で区切って断片へ分ける（区切りに使った引用符文字自体は断片に含めない）。

    貪欲な同種引用符の対付け（`_mask_quoted_url_spans`）が、空白を挟まず隣接する独立した2区間を
    1区間へ併合した継ぎ目（`field0''/openai/...`・`field0」「/openai/...` 等）では、前の区間の
    閉じ引用符と次の区間の開き引用符が同じ空白区切りトークンの途中に埋もれてしまい、空白分割＋
    前後の句読点剥がしだけでは越えられない（`^/` 始まりを要求する多段パス判定の手前で `field0`
    が地の文としてくっついたまま残り、後続の `/openai/...` が先頭一致に失敗して見逃す）。
    引用符文字自体を区切りとして扱うことで、この継ぎ目を断片の境界にする。

    `team's`/`don't` のような語中の引用符は境界にしない（`_is_word_internal_quote_char` で除外）
    ＝断片を誤って割らない。"""
    frags: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(content):
        if ch.isspace():
            if buf:
                frags.append("".join(buf))
                buf = []
            continue
        if ch in _QUOTE_BOUNDARY_CHARS and not _is_word_internal_quote_char(content, i):
            if buf:
                frags.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        frags.append("".join(buf))
    return frags


def _dequote_for_indicator_scan(content: str) -> str:
    """`content` から引用符文字（`_QUOTE_BOUNDARY_CHARS`）を単純に削除（区切りとしてではなく
    除去）した文字列を返す。`_split_on_quote_boundaries` は引用符を**区切り**として扱うため、
    percent-escape のような複数文字からなる指標が引用符境界そのもので分断される場合
    （`%2''Fopenai/...` のように `%2F` の2文字目と引用符が入れ替わって現れる等）は、区切っても
    生の content 検索でも検出できない（断片化すると `%2` と `Fopenai...` が別断片になり、
    `%2F` という並びがどの断片にも生の content にも現れない）。単純に削除して2文字を隣接させる
    ことで escape シーケンス自体を復元し、fail-closed の指標検査（scheme・percent-escape）に
    再度かけられるようにする。語中のアポストロフィ（`team's` 等）も区別せず削除する
    （この結果は出力には使わない＝指標の有無を判定するためだけの補助表現であり、削除で偶然
    scheme/percent-escape の形に一致しても誤検出ではなく安全側に倒すだけで実害が無い）。"""
    return "".join(c for c in content if c not in _QUOTE_BOUNDARY_CHARS)


def _quoted_content_has_url_indicator(content: str) -> bool:
    """引用符区間の中身（`content`・空白を含みうる・句読点クラスタの切り離しは行われていない）に
    URL 指標があるか。`_word_has_url_indicator` と同じ指標集合に加え、`file:/`・`mailto:`/`data:`
    （`content` は切り離し前の生の区間のため、`_word_has_url_indicator` と異なりこの場で直接
    検索できる）を区間全体に対して直接検索する（scheme 系・percent-escape はいずれも空白の
    有無に関係なく検索できる）。多段の平文絶対パス・protocol-relative だけは文字列先頭からの
    構造を要求するため、区間を空白・引用符境界で断片へ分けて判定する（`InvalidURL` の
    request-target 反射は区間の先頭がパスで始まるのが典型形＝`_mask_quoted_url_spans` 参照）。

    各断片は `_split_on_quote_boundaries`（空白と語中でない引用符文字の両方を区切りにする＝
    貪欲対付けで空白なしに隣接併合された区間の継ぎ目も断片境界にする）で切り出したのち、
    さらに `_split_leading_punct`/`_split_trailing_punct_word` で前後の句読点を剥がしてから
    判定する（単語単位の処理＝`_word_has_url_indicator` と同じ前処理）。区間契約
    （指標があれば区間全体を `[URL]` に伏せる）を貫徹する（引用符境界に巻き込まれた地の文が
    巻き込まれて伏せられるのは診断価値側の許容するトレードオフ＝貪欲マッチで区間が広がった
    分だけ安全側に倒す）。

    引用符文字が percent-escape 自体の途中（`%2''Fopenai/...` のように `%2F` の2文字目が
    引用符境界で分断される等）に紛れ込むと、上の生 content 検索・断片検査のどちらも検出できない
    （断片化は区切りとして引用符を扱う＝除去ではなく空白と同じ位置に切るため、`%2` と `Fopenai...`
    が別断片になり `%2F` という並びがどこにも現れない）。区切りではなく単純に削除（除去）した
    再結合文字列（`_dequote_for_indicator_scan`）に対しても scheme・percent-escape の指標検査を
    重ねて行い、引用符境界がどこにあっても escape シーケンス自体が連結された形で判定できるように
    する（fail-closed＝ヒットしたら区間全体を伏せる）。"""
    if (_GENERIC_SCHEME_RE.search(content) or _ENCODED_SCHEME_RE.search(content)
            or _FILE_SCHEME_SINGLE_SLASH_RE.search(content) or _EXPLICIT_BODY_SCHEME_RE.search(content)):
        return True
    if _URL_PCT_INDICATOR_RE.search(content) and not _DIGIT_ENCODED_SLASH_ONLY_RE.match(content):
        return True
    for tok in _split_on_quote_boundaries(content):
        _, tok_rest = _split_leading_punct(tok)
        tok_core, _ = _split_trailing_punct_word(tok_rest)
        if _PLAIN_MULTI_SEGMENT_PATH_RE.match(tok_core) or _is_protocol_relative_url(tok_core):
            return True
    dequoted = _dequote_for_indicator_scan(content)
    if dequoted != content:
        if (_GENERIC_SCHEME_RE.search(dequoted) or _ENCODED_SCHEME_RE.search(dequoted)
                or _FILE_SCHEME_SINGLE_SLASH_RE.search(dequoted) or _EXPLICIT_BODY_SCHEME_RE.search(dequoted)):
            return True
        if _URL_PCT_INDICATOR_RE.search(dequoted) and not _DIGIT_ENCODED_SLASH_ONLY_RE.match(dequoted):
            return True
    return False


# 引用符区間 pre-pass（`_mask_quoted_url_spans`）が対象にする対（開き文字 → 閉じ文字）。
# ASCII の `'`/`"` は開閉が同一文字（自己対称）、全角の `「」`/`『』` は非対称。
_QUOTE_PAIRS = {"'": "'", '"': '"', "「": "」", "『": "』"}

# 開き位置の制約（_is_quote_opener_position 参照）が要るのは自己対称な ASCII 引用符（'/"）だけ
# ＝これらは地の文のアポストロフィ・引用符と本物の開き引用符を字面だけで区別できない。全角の
# 非対称対（「」／『』）はアポストロフィのような単語内記号と混同され得ないため、位置を問わず
# opener とみなしてよい（"エラー「/path TOPSECRET」でした" のように直前が \w でも開きとして
# 機能する）。
_SYMMETRIC_QUOTE_CHARS = frozenset({"'", '"'})

# 引用符 pre-pass の入れ子深さの上限（性能の安全弁・_mask_quoted_url_spans docstring 参照）。
# 現実的な入れ子はたかだか数階層のため、通常の入力ではこの上限に到達しない。
_MAX_QUOTE_NESTING_DEPTH = 8

_WORD_CHAR_RE = re.compile(r"\w")


def _is_quote_opener_position(text: str, i: int) -> bool:
    """位置 i の引用符文字を開き引用符とみなせるか。自己対称な ASCII 引用符（'/"）だけ、行頭、
    または直前の文字が \\w（英数字・アンダースコア・Unicode の文字全般）でない場合に限る。
    don't のような単語内アポストロフィは直前が n（\\w）のため開き引用符とみなさない＝地の文の
    アポストロフィと、直後にある本物の開き引用符との誤対付けを防ぐ（誤対付けにより本物の開き
    引用符が「閉じ引用符」として消費され、その内側の本来のマスク対象が pre-pass の対象から漏れる）。
    全角の非対称対（_SYMMETRIC_QUOTE_CHARS に含まれない）はこの制約の対象外＝常に真。"""
    if text[i] not in _SYMMETRIC_QUOTE_CHARS:
        return True
    return i == 0 or not _WORD_CHAR_RE.match(text[i - 1])


def _mask_quoted_url_spans(text: str) -> str:
    """text を走査し、_QUOTE_PAIRS の引用符で囲まれた区間を1単位として扱う。区間の中身は最内の
    対から処理してから、その結果に URL 指標（_quoted_content_has_url_indicator）があれば区間
    全体を [URL] に伏せる（引用符自体は地の文として保持する）。入れ子の引用符
    （『outer 「/path TOPSECRET」 tail』等）は、内側の対が独立して先に評価されるため、内側だけに
    現れる指標も外側の判定に埋もれず捕捉できる。_redact_reflected_urls の空白区切り単語処理より
    先に呼ぶ契約: 実機の InvalidURL は空白入りの request-target を引用符で囲んで反射することが
    あり（'/openai/deployments/my-secret deploy' 等）、単語単位のままでは空白の位置で分断され
    後半が指標判定をすり抜けてしまう。

    開き引用符とみなす位置は _is_quote_opener_position に限定する（ASCII 引用符の単語内
    アポストロフィ等は開きとみなさない）。対応する閉じ引用符が見つからない開き引用符（非対称な
    入力）は区間として扱わずそのまま地の文に残す＝後続の単語単位の処理に委ねる（不要に広い
    範囲を伏せない）。

    同種の引用符（'/"・同じ全角引用符の入れ子）は、開き位置の直後にある最後（最大位置）の同種
    閉じ文字と対付ける貪欲マッチにする（'.../team's-secret' のように内容中に同種の文字が
    混じっていても、それより後ろにある本物の閉じ引用符まで正しく捕まえる。『『/path TOPSECRET』』
    のような同種の入れ子も、外側をできるだけ広く取ることで内側の対を正しく見つけられるように
    なる）。区間が本来より広がる分は fail-closed（区間全体に URL 指標があれば丸ごと伏せる）で
    安全側に倒す。異なる種類の引用符の入れ子は、外側の対付けが最大に広がっても内側の対の探索
    範囲に影響しないため、この貪欲化とは独立して正しく機能する。

    実装上の注意（性能・堅牢性）: 各引用符文字の出現位置は text 全体を1回走査して事前に集めて
    おき（bisect で二分探索）、opener ごとに str.find/str.rfind を都度呼ばない。区間の入れ子は
    Python の関数呼び出し再帰ではなく明示スタックで処理する（貪欲マッチは閉じ引用符の無い
    同種文字が大量に連続する入力で非常に深い入れ子を作りうるため、再帰だと RecursionError に
    なる・32万字規模の実機相当の反復入力で確認済み）。さらに、入れ子の深さを
    `_MAX_QUOTE_NESTING_DEPTH` で打ち切る（打ち切り以降は opener とみなさず素通りする）: 深さの
    上限が無いと、各階層で子の結果文字列を `ch + inner + closer` として毎回コピーするコストが
    階層数に比例して積み上がり、閉じ引用符の無い同種文字が連続する入力では階層数が文字数に
    比例してしまうため合計コストが O(n^2) になる（実測で確認済み）。上限を定数にすることで
    1文字あたりの処理階層数を定数に抑え、全体を O(n) 近くに保つ（現実的な引用符の入れ子は
    たかだか数階層であり、この上限に達することはまず無い）。マッチした区間の内側はスタックに
    積んで先に処理し、外側の走査はその区間をまるごと読み飛ばす（i = j + 1）。"""
    positions: dict[str, list[int]] = {}
    for idx, ch in enumerate(text):
        if ch in _QUOTE_PAIRS or ch in ("」", "』"):
            positions.setdefault(ch, []).append(idx)

    def _last_pos_in_range(ch: str, lo: int, hi: int) -> int:
        """ch の出現のうち半開区間 [lo, hi) 内で最後（最大）の位置。無ければ -1。"""
        lst = positions.get(ch)
        if not lst:
            return -1
        k = bisect.bisect_left(lst, hi) - 1
        if k >= 0 and lst[k] >= lo:
            return lst[k]
        return -1

    # 明示スタックでの反復処理（各フレーム＝[hi, pos, out, pending]）。`pending` は
    # このフレームが現在待っている子フレームの (ch, closer, j)（無ければ None）。
    # `pos` はこのフレームの走査位置（子フレーム完了後は j + 1 から再開する）。
    n = len(text)
    stack: list[list] = [[n, 0, [], None]]
    child_result: str | None = None
    while True:
        hi, pos, out, pending = stack[-1]
        if child_result is not None:
            ch, closer, j = pending
            inner = child_result
            if inner and _quoted_content_has_url_indicator(inner):
                out.append(ch + "[URL]" + closer)
            else:
                out.append(ch + inner + closer)
            stack[-1][1] = j + 1
            stack[-1][3] = None
            child_result = None
            continue
        pushed_child = False
        while pos < hi:
            ch = text[pos]
            closer = _QUOTE_PAIRS.get(ch)
            # 深さは root フレーム（最初の1個・実際の引用符の入れ子ではない）を除いて数える
            # ＝ `len(stack) - 1` が現在の入れ子段数。`_MAX_QUOTE_NESTING_DEPTH` 段までの入れ子は
            # 通常どおり再帰的に処理する。
            if (closer is not None and len(stack) - 1 < _MAX_QUOTE_NESTING_DEPTH
                    and _is_quote_opener_position(text, pos)):
                j = _last_pos_in_range(closer, pos + 1, hi)
                if j != -1:
                    stack[-1][1] = pos
                    stack[-1][3] = (ch, closer, j)
                    stack.append([j, pos + 1, [], None])
                    pushed_child = True
                    break
            if (closer is not None and len(stack) - 1 >= _MAX_QUOTE_NESTING_DEPTH
                    and _is_quote_opener_position(text, pos)
                    and _last_pos_in_range(closer, pos + 1, hi) != -1):
                # 上限に達し、これ以上再帰的に対付けできない。この位置から現在のフレームの
                # 終端までを「未処理のまま素通り」させると、深い入れ子の内側にある秘密が
                # 一切評価されずに残ってしまう（fail-closed 違反）。代わりに残り部分文字列を
                # 丸ごと1単位とみなし、URL 指標があれば全体を `[URL]` に伏せる（対付けを試みない
                # 分、正確な区間は組み立てられないが、漏洩防止を優先する）。
                remainder = text[pos:hi]
                if _quoted_content_has_url_indicator(remainder):
                    out.append("[URL]")
                else:
                    out.append(remainder)
                pos = hi
                stack[-1][1] = pos
                break
            out.append(ch)
            pos += 1
            stack[-1][1] = pos
        if pushed_child:
            continue
        finished = "".join(out)
        stack.pop()
        if not stack:
            return finished
        child_result = finished


# OpenAI 等の上流が「先頭数文字＋アスタリスク列＋末尾数文字」でキーを部分マスクしてエラー本文へ
# echo することがある（実機で観測：`AbCd1234` + `*` 数十個 + `Zz99`）。正当な診断情報が `****` を
# 4個以上連続で含むことは実務上ないため、アスタリスク列を含む空白区切りトークンは丸ごと伏せる
# （診断語が `****` を含む実例は無い＝過剰マスクよりキー漏出を避ける安全側に倒す）。
_MASKED_TOKEN_RE = re.compile(r"\S*\*{4,}\S*")

# `_MASKED_TOKEN_RE` が `[REDACTED]` 化したトークンの直前/直後（空白区切りで隣）にある短い
# 英数字トークンも併せて伏せる。`AbCd1234 **** **** Zz99`（空白入りの部分マスク echo）のような
# 形では、アスタリスク列（2箇所）がそれぞれ独立トークンとして伏せられても、隣の接尾辞（とくに
# `_SECRET_FRAGMENT_MIN_LEN` 未満の短い方）が単独では残ってしまうための随伴対策。
#
# **`[REDACTED]` が2個以上連続している場合だけ**発火させる（`[REDACTED]\s+[REDACTED]` を要求）。
# 1個だけ（＝ `_MASKED_TOKEN_RE` が接頭辞・アスタリスク列・接尾辞を空白なしの1トークンとして
# 丸ごと1個の `[REDACTED]` へ既に統合できたケース）には適用しない＝実測で確認済み: 1個だけの
# 場合に無条件で隣接トークンを伏せると、`[REDACTED] You can find...` のような**次の文の最初の
# 単語**（`You` 等・7字以下はよくある）まで巻き込んで消してしまう誤爆が実際に起きた。
# 2個以上連続＝複数箇所に分かれて伏せられた＝同一 echo の残り damage の可能性が高いという構造的な
# シグナルを使うことで、この誤爆を避けつつ空白入り echo の残存断片は塞げる（secret が不明かつ
# アスタリスク列が1箇所だけの echo の残存断片までは塞げない残存リスクはある＝corroborate する
# 材料が無い状態で積極的にマスクするより誤爆を避ける側に倒した）。
_TOKEN_AFTER_REDACTED_RE = re.compile(r"(\[REDACTED\](?:\s+\[REDACTED\])+)(\s+)\b[A-Za-z0-9]{1,7}\b")
_TOKEN_BEFORE_REDACTED_RE = re.compile(r"\b[A-Za-z0-9]{1,7}\b(\s+)(\[REDACTED\](?:\s+\[REDACTED\])+)")

_SECRET_FRAGMENT_MIN_LEN = 7   # secret の接頭辞/接尾辞断片マスクの最短長。6 だと `sk-proj...` 系の
# 接頭辞6字（`sk-pro`）が正当な文言へ偶然一致しうるため 7 へ引き上げる（実機観測の接頭辞は8字＝
# 検出力は維持したまま誤爆リスクだけ下げる）。

_DETAIL_MAX_LEN_HTTP = 400          # _http_detail 相当の理由文の表示上限
_DETAIL_MAX_LEN_GENERIC = 300       # _error_detail 相当の理由文の表示上限


def _mask_secret_fragments(text: str, secret: str) -> str:
    """`secret` の完全一致では捉えられない部分エコー（上流が独自形式で断片だけ表示する等・
    アスタリスク以外の切り詰め方も含む）に対する防御。`secret` の**先頭 N 文字**または
    **末尾 N 文字**（`N >= _SECRET_FRAGMENT_MIN_LEN`）と一致する断片が `text` にあれば、
    見つかった長さすべてを伏せる（同一本文に異なる長さの断片が複数現れても、最初に見つかった
    長さで打ち切ると短い方の断片が残ってしまうため、打ち切らず全長を確認する）。"""
    n = len(secret)
    if n < _SECRET_FRAGMENT_MIN_LEN:
        return text
    for length in range(n, _SECRET_FRAGMENT_MIN_LEN - 1, -1):
        frag = secret[:length]
        if frag in text:
            text = text.replace(frag, "[REDACTED]")
    for length in range(n, _SECRET_FRAGMENT_MIN_LEN - 1, -1):
        frag = secret[-length:]
        if frag in text:
            text = text.replace(frag, "[REDACTED]")
    return text


def _mask_tokens_adjacent_to_redaction(text: str) -> str:
    """`[REDACTED]` マーカーが2個以上連続している箇所の直前/直後にある短い（7字以下の）英数字
    トークンも伏せる（`_MASKED_TOKEN_RE`/`_mask_secret_fragments` が作った `[REDACTED]` に対する
    後処理・上の `_TOKEN_AFTER_REDACTED_RE`/`_TOKEN_BEFORE_REDACTED_RE` の docstring 参照）。"""
    text = _TOKEN_AFTER_REDACTED_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)
    text = _TOKEN_BEFORE_REDACTED_RE.sub(lambda m: "[REDACTED]" + m.group(1) + m.group(2), text)
    return text


def _percent_encoding_insensitive_pattern(s: str) -> str:
    """`s`（`urllib.parse.quote`/`quote_plus` が生成する大文字16進の percent-encoding 形）から、
    各 `%XX` の16進2桁をそれぞれ大小文字を問わず照合する正規表現パターン文字列を組み立てる
    （それ以外の文字は `re.escape` でそのまま照合する）。

    RFC 3986 上は大文字/小文字16進とも同じ値を表すが、上流プロキシ/ゲートウェイが同じ値の
    **中で** 大文字と小文字の16進を混在させて echo することがある（例: 同じトークンの中で
    `%2b` と `%2C` が混在）——固定の「全体を小文字化した変種」を1つ追加で比較するだけ（旧実装）
    では、桁ごとに大小文字が異なる組み合わせを網羅できない。桁ごとに `[Aa]` 形の文字クラスへ
    展開することで、組み合わせを列挙せず1回の正規表現照合で全パターンに一致させる。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "%" and i + 2 < n and re.fullmatch(r"[0-9A-Fa-f]{2}", s[i + 1:i + 3]):
            h1, h2 = s[i + 1], s[i + 2]
            out.append(f"%[{h1.upper()}{h1.lower()}][{h2.upper()}{h2.lower()}]")
            i += 3
        else:
            out.append(re.escape(s[i]))
            i += 1
    return "".join(out)


def _mask_secrets(text: str, secret: str | None) -> str:
    """`text` から、呼び出しで使った実キー（`secret`・素の値と URL エンコード形の両方・部分一致の
    断片）と、一般的な秘密パターン（`Bearer <値>`・`api-key: <値>` ヘッダ・`sk-` 形式のトークン・
    アスタリスク列で部分マスクされたトークンとその隣接断片）を伏せる。上流プロキシ/ゲートウェイや
    プロバイダ自身がリクエストヘッダやキー（の断片）をエラー本文へ echo した場合の最終防衛線
    （`sherpa/providers/bedrock.py::_redact_bedrock_secret` と同じ発想を汎用化したもの）。"""
    if not text:
        return text
    if secret:
        text = text.replace(secret, "[REDACTED]")
        for encoded in (quote(secret, safe=""), quote_plus(secret, safe="")):
            if encoded and encoded != secret:
                # `quote()`/`quote_plus()` は常に大文字16進を生成するが、上流プロキシ/ゲートウェイが
                # 同じ値の中で大文字/小文字16進を混在させて echo することがある（`%2b`+`%2C` 等）ため、
                # 単純な文字列一致（大文字形・固定の全小文字変種の2択）ではなく、16進2桁を1桁ずつ
                # 大小文字を問わない正規表現で照合する（`_percent_encoding_insensitive_pattern`）。
                text = re.sub(_percent_encoding_insensitive_pattern(encoded), "[REDACTED]", text)
        text = _mask_secret_fragments(text, secret)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _API_KEY_HEADER_RE.sub("api-key: [REDACTED]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    text = _MASKED_TOKEN_RE.sub("[REDACTED]", text)
    text = _mask_tokens_adjacent_to_redaction(text)
    return text


def _split_leading_punct(word: str) -> tuple[str, str]:
    """`word` の先頭にある引用符・開き括弧（`_LEADING_PUNCT`）を本体から切り離す
    （地の文の引用符・括弧の対応を壊さないため・`_redact_reflected_urls` 参照）。"""
    end = 0
    while end < len(word) and word[end] in _LEADING_PUNCT:
        end += 1
    return word[:end], word[end:]


_TRAILING_PUNCT_MAX_LEN = 3   # これを超える末尾クラスタは句読点ではなく怪しいデータとみなし、
# 地の文として切り離さず本体側に残す（本体側は伏せられるため結果的に丸ごと隠れる＝下記参照）。

_URL_DATA_TAIL_CHARS = "=?#&"   # クラスタを剥がした後の本体末尾がこれらなら、そのクラスタは
# 地の文の句読点ではなく URL 側のデータ（クエリ/フラグメントの区切り直後の断片）とみなし、
# 剥がさず本体側に残す（`_split_trailing_punct_word` docstring 参照）。


def _split_trailing_punct_word(word: str) -> tuple[str, str]:
    """`word`（`_split_leading_punct` 適用後）の末尾にある句読点・閉じ括弧・引用符
    （`_TRAILING_PUNCT`）を本体から切り離す（地の文として保持するため）。

    2つの安全弁を設ける（どちらも「剥がさない＝本体側に残す」方向にだけ倒す）:
      1. クラスタ長が `_TRAILING_PUNCT_MAX_LEN` を超える場合は剥がさない（句読点の連なりに
         見せかけた URL データの残骸である可能性が高いため）。
      2. クラスタを剥がした**後**の本体の末尾文字が `_URL_DATA_TAIL_CHARS`（`= ? # &`）なら
         剥がさない（例: `?token=;;;` は `;` が `_TRAILING_PUNCT` に含まれないため元々剥がれ
         ないが、同様の理由で「句点や閉じ括弧に見えて実は query/fragment が途切れた残骸」の
         ケースを広く防ぐ）。"""
    end = len(word)
    while end > 0 and word[end - 1] in _TRAILING_PUNCT:
        end -= 1
    if len(word) - end > _TRAILING_PUNCT_MAX_LEN:
        return word, ""
    if end > 0 and word[end - 1] in _URL_DATA_TAIL_CHARS:
        return word, ""
    return word[:end], word[end:]


def _is_pure_plain_url(core: str) -> bool:
    """`core`（前後の引用符/句読点を切り離した後の本体）が percent-encoding を一切含まない
    純粋な平文 URL（`http(s)://` で始まる）かどうか。真なら `llm._redact_url_for_error()` で
    `host[:port]` へ縮約してよい＝path/query の生の中身は元々含んでおらず、host 表現だけを
    組み立て直すため「正規化した写し」の問題が生じない。

    percent-encoding を少しでも含む場合（encoded scheme・encoded path 断片・平文 scheme と
    encoded path の混在のいずれか）は偽を返す＝呼び出し側は本体を丸ごと `[URL]` にする
    （デコードして host だけ取り出す処理はしない＝percent-encoded 文字列は稀にデコード結果が
    意図と異なる〈二重エンコード等〉ことがあるため、部分的に信頼して組み立て直すより
    範囲ごと伏せる方を選ぶ）。"""
    return bool(_PLAIN_SCHEME_RE.match(core)) and not _ANY_PCT_RE.search(core)


_WORD_RE = re.compile(r"\S+")


def _pair_outer_brackets(lead: str, core: str, trail: str) -> tuple[str, str, str]:
    """`lead` の末尾（トークン先頭に最も近い開き括弧）に対応する閉じ括弧が `core` の末尾に
    残っている場合、それを `core` から `trail` 側へ移し、開き/閉じの対を両方地の文として保持する。

    `_TRAILING_PUNCT` の一般的な句読点集合には `]`/`}` を含めていない（IPv6 リテラルの閉じ角括弧
    との衝突を避けるため）ため、`[https://host/path]` のような外側の角括弧は `[` だけが
    `_split_leading_punct` で分離され、対応する `]` は本体に取り込まれたまま `[URL]`/host 表記へ
    丸め込まれて消える（片方だけ保持される非対称な欠落）。開き括弧を分離した時点で対応する閉じ
    括弧の有無を明示的に確認し、対で扱うことでこれを防ぐ。"""
    if lead and core:
        closer = _PAIRED_BRACKETS.get(lead[-1])
        if closer is not None and core.endswith(closer):
            core = core[:-1]
            trail = closer + trail
    return lead, core, trail


def _redact_reflected_urls(text: str, base_url: str | None) -> str:
    """上流（custom/Azure OpenAI 互換エンドポイント）がエラー本文へ要求 URL を echo した場合の
    最終防衛線。`_mask_secrets` と同じ「上流が何を返してもここで一括して弾く」流儀。

    契約: エラー詳細に現れる URL は、送信時スナップショットの実効 base URL かどうかを区別せず
    一律で伏せる（正当な案内 URL も含む＝診断価値より漏洩防止を優先する）。fail-closed の
    トークンマスク方式の詳細（引用符区間 pre-pass・範囲確定を先に行ってから指標判定する理由・
    指標判定・置換）はモジュール冒頭のコメント参照。

    `base_url`（送信時スナップショットの実効 base URL）は現状ここでは未使用（指標判定は
    base_url かどうかに関わらず該当パターンを一律捕捉するため）だが、呼び出し元の契約
    （送信時スナップショットを渡す）はそのまま維持する。

    空白を含まない日本語文中に URL が続く場合、URL を含む「単語」が文全体になり後続の文章ごと
    伏せられることがある（漏洩防止を優先し、この場合の可読性低下は許容する契約）。
    """
    if not text:
        return text

    text = _mask_quoted_url_spans(text)

    def _sub_word(m: re.Match) -> str:
        word = m.group(0)
        lead, rest = _split_leading_punct(word)
        # `file:/`・`mailto:`/`data:` は `rest`（末尾句読点クラスタの切り離し前）に対して検索する
        # （`_EXPLICIT_BODY_SCHEME_RE`/`_FILE_SCHEME_SINGLE_SLASH_RE` のコメント参照）: body が
        # 句読点のみ（例 `data:,,`）だと `_split_trailing_punct_word` がクラスタごと剥がしてしまい、
        # 本体（`core`）の colon 直後が空になって取りこぼす。
        has_explicit_scheme = bool(
            _EXPLICIT_BODY_SCHEME_RE.search(rest) or _FILE_SCHEME_SINGLE_SLASH_RE.search(rest))
        core, trail = _split_trailing_punct_word(rest)
        lead, core, trail = _pair_outer_brackets(lead, core, trail)
        if has_explicit_scheme:
            # 常に丸ごと `[URL]` にする（host 縮約は行わない＝`_is_pure_plain_url` の対象は
            # http/https のみ）。
            return lead + "[URL]" + trail
        if not core or not _word_has_url_indicator(core):
            return word
        if _is_pure_plain_url(core):
            replacement = llm._redact_url_for_error(core) or "[URL]"
        else:
            replacement = "[URL]"
        return lead + replacement + trail

    return _WORD_RE.sub(_sub_word, text)


def _log_masked_exception(log, context: str, e: BaseException, secret: str | None = None) -> None:
    """外部（HTTP 応答・admin API 等）へは出さない元例外の型とマスク済みメッセージだけを、
    WARNING ログへ一貫して残すための共通ヘルパー——5xx/504（またはそれに相当する内部失敗）へ
    翻訳する箇所ならどこからでも呼ぶ想定（`research_service.py`/`agentic_search.py` など）。
    生の例外オブジェクト・生の例外文字列はログへ一切出さない（`_mask_secrets`/
    `_redact_reflected_urls` を通した文字列だけを渡す）。

    `secret`（省略可）: その呼び出しで実際に使った実キー（openai/azure 等）があれば渡す
    （完全一致・断片一致の両方を見る・`_mask_secrets` 参照）。**文字列以外**（設定破損等で
    JSONB 値が想定外の型になっている場合）は `str()` 化した全体を代わりにマスク対象へ渡す
    （二重防御）——`llm.openai_headers()` は非文字列キーを送信前に拒否する契約になったが、
    それでもなお非文字列が渡ってきた場合に備える最終防衛線。dict/list 等を `f"Bearer {key}"`
    のように f-string へ直接埋め込むと Python は自動的に `str()` 化するため、後から例外文字列
    （`str(e)`）にその埋め込み結果がそのままエコーされることがある——`str(secret)` を渡せば
    `_mask_secrets` の完全一致・断片一致の両方がこの文字列化後の形をそのまま捕まえられる
    （None 扱いにして汎用パターンだけに頼ると、dict の repr はクォート文字・空白を含むため
    `_BEARER_RE` 等の正規表現が途中で打ち切られてすり抜けてしまう＝実際に再現した漏洩）。

    マスク処理自体（`_mask_secrets`/`_redact_reflected_urls`）が想定外の例外を投げても、ここで
    握り潰してから固定のプレースホルダでログに残す——素通しにすると、この関数の呼び出し元は
    大抵 `except Exception as e:` の中でこれを呼んでいるため、マスク処理の例外は Python の暗黙
    連鎖（`__context__`）で元の `e`（秘密を含みうる）にぶら下がったまま呼び出し元の外へ伝播し、
    せっかく隠した秘密が別の traceback 経由で復活してしまう。
    """
    if secret is not None and not isinstance(secret, str):
        secret = str(secret)
    try:
        masked = _redact_reflected_urls(_mask_secrets(str(e), secret), None)
    except Exception:
        masked = "<masking failed>"
    log.warning("%s: %s: %s", context, type(e).__name__, masked)


def _safe_detail(e: Exception, *, secret: str | None = None,
                 system_settings: dict | None = None) -> str:
    """LLM 呼び出し失敗の例外 → 利用者向けの安全な理由文。失敗理由を外部（設定画面の接続テスト・
    health 疎通確認等）へ返す箇所は、`_http_detail`/`_error_detail` を直接使わず必ずここを経由する
    （`_probe` が対象）。

    **マスクしてから切断する順序をここで保証する**（`_http_detail`/`_error_detail` 自体は長さを
    調整しない未加工の文字列を返す契約）: 先に切断すると、切断境界をまたいだ秘密の断片が未マスクの
    まま残ってしまう。`secret`（その呼び出しで使った実キー）が分かれば渡す＝分からない呼び出し元
    （cfg にキーが無い ollama 等）でも一般パターンのマスクは必ず通る。同じ理由で反射 URL の
    マスク（`_redact_reflected_urls`）も切断の前に行う。

    `_http_detail` の案内文（`_DEPLOYMENT_NOT_FOUND_HINT` 等）は、本文を先にマスクしたうえで
    「案内文の長さを差し引いた分」まで切ってから連結する＝上限を超えても案内文自体は必ず残る。

    `system_settings`（省略可）: `_http_detail` の 404 案内判定、および反射 URL マスクの
    「送信時に使った実効 base URL」の計算にそのまま渡す（呼び出し元は
    `cfg["openai_endpoint_override"]` ＝送信時に使ったのと同じスナップショットを渡す・省略時は
    都度読み直す）。

    `secret` が文字列でない場合（設定破損等）は `str()` 化してから使う（`_mask_secrets` の
    `text.replace(secret, ...)` は非文字列だと `TypeError` を出すため・`_log_masked_exception`
    と同じ防御）。"""
    if secret is not None and not isinstance(secret, str):
        secret = str(secret)
    try:
        base_url = llm.openai_base_url(system_settings)
    except Exception:
        base_url = None
    if isinstance(e, urllib.error.HTTPError):
        base, hint = _http_detail(e, system_settings)
        base = _mask_secrets(base, secret)
        base = _redact_reflected_urls(base, base_url)
        if hint:
            reserved = max(_DETAIL_MAX_LEN_HTTP - len(hint), 0)
            return (base[:reserved] + hint)[:_DETAIL_MAX_LEN_HTTP]
        return base[:_DETAIL_MAX_LEN_HTTP]
    text = _error_detail(e, secret=secret)
    text = _mask_secrets(text, secret)
    text = _redact_reflected_urls(text, base_url)
    return text[:_DETAIL_MAX_LEN_GENERIC]


def _probe(cfg, timeout: int | None = None) -> tuple[bool, str]:
    """LLM に1回だけ最小リクエストして到達性/認証/クォータを確認し、`(ok, 理由)` を返す。

    dead プロバイダで全文書 × timeout を避ける（RV Med#2）。失敗時は OpenAI/Ollama の**実エラー文**を理由に乗せる
    （例: 429 insufficient_quota＝残高/課金切れ）。理由にキーは含めない（`_safe_detail` で
    戻り値の境界に一律適用する＝呼び出し元ごとに個別対応しなくてよい・settings_test/health.py 等
    `_probe` の全呼び出し元に一括で効く）。
    `timeout`（省略時は既定の抽出用 `_TIMEOUT`＝90s）: システム状態画面の「再チェック」（health.py）は
    複数プロバイダを確認するため、ここを短く（既定8s・`SHERPA_HEALTH_AI_TIMEOUT`）指定して1プローブが
    全体をブロックしないようにする（RV HIGH・2026-07-03）。
    """
    try:
        complete_json("Return a JSON object only.", 'Return {"ok":true}', cfg,
                     **({"timeout": timeout} if timeout is not None else {}))
        return True, ""
    except Exception as e:
        return False, _safe_detail(e, secret=cfg.get("key") or cfg.get("api_key"),
                                   system_settings=cfg.get("openai_endpoint_override"))
