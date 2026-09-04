"""`make doctor` の検査本体。

導入先で1コマンドの統合セットアップ検査を行う。**本番コードをそのまま呼ぶ**方針（HTTP／DB 接続／
プロバイダ判定を再実装しない）:

  - `sherpa.health`: `COMPONENTS`／`_check_one`（Postgres・Neo4j 疎通・例外の安全な分類）・
    `_ai_check_bedrock`（AWS Bedrock 最小プローブ）・`_ai_check_codex`（Codex CLI ログイン確認）を
    そのまま使う。OpenAI／Gemini の最小プローブは `health._ai_check_openai`／`_ai_check_gemini` を
    経由せず、`_run_raw_llm_probe()` が `graph_extract.complete_json()` を直接呼ぶ（失敗理由を
    自由文へ変換する `_probe`／`_safe_detail` を経由すると fail-closed な構造化分類ができない
    ため・`_classify_llm_probe_failure()` 参照）。Ollama も `_ai_check_ollama` を使わず
    `sherpa.llm.urlopen_no_redirect`／`ollama_url` を直接呼ぶ＝下記参照。中央既定 URL の疎通
    だけでなく、個人の接続先上書き・用途別モデルの実在確認まで必要なため。対象は
    chat／Codex(Ollama backing)／検索ヘルパーの3用途のみ＝embed 用途（`nomic-embed-text` 等）は
    対象外。埋め込みが解決できない場合はベクトル検索が BM25 のみへ縮退するだけで会話自体は
    継続できるため、他の3用途と同列の疎通確認は行わない）
  - `check_production_openai_probe.probe()`：system_settings 側の接続先（`openai_endpoint_kind`／
    `openai_base_url`）妥当性判定（`scripts/check-production.sh` と共有）。env 候補
    （初回シード前）の妥当性判定・以後のチェックが使う実効値の算出は `sherpa.llm.
    openai_endpoint_seed_candidate()` を `_openai_endpoint_status()` が直接呼ぶ（本番の起動時
    シード関数と同じ resolver）。
  - `sherpa.llm`: `ollama_url()`／`urlopen_no_redirect()`／`_redact_url_for_error()`（Ollama への
    SSRF 許可判定込みの URL 組み立て・redirect 非追跡・匿名化表示の唯一の真実源）・
    `openai_endpoint_kind()`（Codex の接続先種別判定に使う）
  - `sherpa.keys`: `selected_cloud_provider()`／`resolve_api_key()`／`resolve_ollama_url()`／
    `personal_keys_allowed()`（A7 排他選択・A6 個人キー許可・接続先解決の唯一の真実源）
  - `sherpa.agent_constructs`: `effective_agent()`（現在有効な頭脳）・`codex_model_provider()`・
    `is_real_api_key()`
  - `sherpa.agents._bedrock_auth_available()`: Bedrock の認証手掛かり（中央キーまたは AWS SigV4）
  - `sherpa.providers._codex_openai_compat_block_reason()`: Codex(Azure/custom) 構成の可否判定
    （`POST /settings/test` の Codex 分岐と同じ判定部品）
  - `sherpa.search_helper.resolve()`: 検索ヘルパー（下調べ役）が実際にどの provider/URL/モデルを
    使うかの解決（`user_settings.search_helper` 経由の Ollama 利用を見落とさないための再利用。
    実行時に配線されるのは主頭脳が openai のときだけ＝`sherpa/providers/__init__.py::get_provider`
    と同じゲートを合わせる）
  - `sherpa.model_catalog.resolve_model()`: プロバイダ／用途ごとの実効モデル名
  - `sherpa.store.db._connect()`: Postgres 接続（DSN／`row_factory` の唯一の真実源。ただし
    `sherpa.store.get_system_settings()` 等の高水準 API は未初期化時に `_ensure()`→`init_schema()`
    （DDL）を実行する契約のため使わない＝下記「読み取り専用」参照。`sherpa.llm` の SSRF 許可判定
    （`_allowlisted_hosts()`）も省略時は同じ高水準 API を読むため、`system_settings` を明示的に
    渡すこと）
  - `sherpa.es_index`: `_url()`（ES 接続先の唯一の真実源。バージョン／kuromoji プラグイン確認は
    ES REST を直接読むだけの新規コード＝`health`/`es_index` に既存の同等ロジックが無いため）

**読み取り専用契約**: DB・ES・Neo4j への書き込み・DDL 実行は一切行わない。system_settings／
user_settings の読み取りは `_connect()` を直接使った `SELECT` のみで行い、スキーマ初期化
（`_ensure()`）を経由する高水準 API は使わない（DDL 権限を持たない読み取り専用ロールでも安全に
実行できる）。読み取り自体が失敗した場合（DDL 権限が無い・テーブル未作成等）は `skip` にせず
`ng` にする（読めないことと「使っていないので問題ない」を混同しない）。

**秘密の表示境界**: すべての `CheckResult.detail` は構築時に自動で `_sanitize_text()`（ANSI/制御
文字の除去→秘密マスクの順・`CheckResult.__post_init__` 参照）を通る。加えて、`run_all()` の実行中は
`_LOG_REDACTION_TARGET_ROOTS`（`sherpa`／`anthropic`／`httpx`／`httpcore`／`botocore`）配下の全ロガーが出す
レコードの本文（と `exc_info`/`exc_text`/`stack_info`）を固定文言へ差し替える
（`_log_redaction_active()` 参照・`logging.Logger.callHandlers` を差し替える実装＝
ハンドラが1つも設定されていない裸のスクリプト実行（`logging.lastResort` が実際の出力先になる）でも、
逆に呼び出し元プロセスが既に root ハンドラを設定済みの環境でも、どちらでも確実に効く。参照カウント＋
ロックで再入・並行呼び出しにも安全）。呼び出し先モジュール自体は変更しない（`CheckResult` を経由
しない側路だけを doctor 実行中に限定して塞ぐ）。

各検査関数は例外を投げず `CheckResult`（`status` は `"ok"`／`"ng"`／`"skip"` の3値のみ）を返す。
`"skip"` は「失敗ではない」項目にのみ使う:
  - 前提の疎通確認（DB 等）が失敗していて後続を検証できない（前提自体は別項目で NG 済み）
  - 課金プロバイダの実接続プローブ（既定 OFF・`PROBE_CLOUD=1` で有効化）
  - A7 排他選択で選ばれていないクラウドプロバイダ、または現在の構成でそもそも消費されない
    クラウドプロバイダ（`ollama_only`／`codex_ollama` のみの構成で `cloud_provider` が既定値の
    まま放置されている場合等）
  - Ollama／Codex が現在の構成（システム既定＋**有効な**（`users.status='active'`）利用者の
    保存設定のいずれか）で使われていない
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_ES_TIMEOUT = 5.0
_CODEX_TIMEOUT = 10.0
_PG_READONLY_TIMEOUT = 5.0
# health._ping_postgres と同じ方式・値: connect_timeout は接続確立のみをカバーするため、
# 発行した SELECT 自体（フェイルオーバ中の PG 等でクエリが詰まるケース）にも上限を課す。
_PG_READONLY_OPTIONS = f"-c statement_timeout={int(_PG_READONLY_TIMEOUT * 1000)}"


# ---------------------------------------------------------------------------
# 共通境界: 秘密マスク・制御文字/ANSI除去・長さ制限
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_MAX_DETAIL_CHARS = 500


def _sanitize_text(text: object) -> str:
    """`CheckResult.detail` に使う全文字列（外部コマンド出力・例外文字列を含む）へ適用する共通境界。

    (1) ANSI エスケープ除去。
    (2) 制御文字を空白へ置換。
    (3) 秘密マスク（`graph_extract._mask_secrets`／`_redact_reflected_urls`・**パターンベース**の
        一般的な検出のみ）。
    (4) 長さ制限（想定外に長い出力でレポートが読めなくなることを防ぐ）。

    **実キーが関わる失敗理由（クラウド LLM 最小プローブ・Codex(Azure/custom) 実プローブ）は
    この関数を経由しない**。上流が実キーをどんな区切り文字でエコーしても検出しきれるとは限らない
    ため、この種の呼び出し元は自由文をこの関数へ渡すこと自体をせず、**fail-closed**（例外の型・
    HTTP ステータス等の構造化属性だけを見る・文字列化した理由文は一切生成しない）で失敗理由を
    組み立てる（`_classify_llm_probe_failure` 参照）。この関数はそれ以外の自由文（Postgres/
    Neo4j/ES 等、実キーを含まない失敗理由）向けの一般的な保険としてのみ使う。
    """
    from sherpa.ingest.graph_extract import _mask_secrets, _redact_reflected_urls
    s = str(text)
    s = _ANSI_ESCAPE_RE.sub("", s)
    s = "".join(ch if (ch == " " or ch.isprintable()) else " " for ch in s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _mask_secrets(s, None)
    s = _redact_reflected_urls(s, None)
    if len(s) > _MAX_DETAIL_CHARS:
        s = s[:_MAX_DETAIL_CHARS].rstrip() + "…（省略）"
    return s


@dataclass(frozen=True)
class CheckResult:
    id: str
    label: str
    status: str          # "ok" / "ng" / "skip"
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _sanitize_text(self.detail))


def _guarded_check(cid: str, label: str):
    """`CheckResult` を1件返すチェック関数を、未捕捉の例外から守るデコレータ。

    doctor の全検査関数は「例外を投げず `CheckResult` を返す」契約（モジュール冒頭 docstring
    参照）だが、判定の途中で呼ぶ本番コード（`model_catalog.resolve_model()` 等）は壊れた
    `system_settings`（型不正な JSONB 値等）に対して `TypeError` 等を投げうる。この種の想定外の
    例外を関数の**外側**（呼び出し元・最終的には `run_all()`）まで伝播させると、1項目の設定不備で
    doctor 全体が未捕捉の traceback で中断する（診断ツールとしての価値を損なう）。ここでラップした
    関数本体で起きた例外は、そのチェック単独の `ng`（固定文言・自由文は出さない）へ変換する。
    """
    def _decorator(fn):
        @functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                return CheckResult(cid, label, "ng",
                                    "この検査自体が予期しないエラーで失敗しました（設定を確認してください）")
        return _wrapper
    return _decorator


_LOG_REDACTION_REPLACEMENT = "（doctor 実行中のため詳細は省略。レポート本文の該当項目を確認してください）"

# 再入・並行呼び出し安全のための状態（`_log_redaction_active()` 参照）。`_log_redaction_lock` 保持中
# にしか読み書きしない＝実際のログ出力（ラッパー関数の呼び出し）とは競合しない。ラッパー関数自身は
# この original をクロージャに固定して持つため、ここが None に戻ってもラッパーの動作には影響しない。
_log_redaction_lock = threading.Lock()
_log_redaction_depth = 0
_log_redaction_captured_original = None


# doctor 実行中に本文を差し替える対象のロガー名前空間（ルート名・子は "<root>." prefix で一致）。
# `sherpa.*` に加え、Bedrock 実プローブが使う `anthropic` SDK とその下請け HTTP ライブラリ
# （`httpx`／`httpcore`）を含める: `anthropic` パッケージは import 時に無条件で
# `_utils/_logs.py::setup_logging()` を呼び、環境変数 `ANTHROPIC_LOG=debug`（運用者が調査目的で
# 設定しうる一般的な SDK デバッグフラグ）が立っていると `anthropic`／`httpx` の各ロガーを
# DEBUG へ引き上げ、リクエスト/レスポンス（ヘッダー・実キーを含みうる Authorization 等）を
# そのままログへ出す。`botocore`（`anthropic` の Bedrock 実装が SigV4 署名に使う
# `botocore.auth.SigV4Auth`）も同様のリスクを持つ: `botocore/auth.py` は DEBUG レベルで
# `CanonicalRequest`（署名対象の生ヘッダー一式・一時セッショントークン `X-Amz-Security-Token` を
# 含みうる）をそのままログへ出す契約（`logging.getLogger(__name__)` 経由・"botocore.auth" は
# "botocore" の子ロガー）。`sherpa.*` だけを対象にした差し替えだとこれらの経路は素通りする。
_LOG_REDACTION_TARGET_ROOTS = ("sherpa", "anthropic", "httpx", "httpcore", "botocore")


def _is_log_redaction_target(name: str) -> bool:
    return any(name == root or name.startswith(root + ".") for root in _LOG_REDACTION_TARGET_ROOTS)


def _make_redacting_call_handlers(original):
    """`logging.Logger.callHandlers` の差し替え本体を、その回の `original` を**クロージャに固定**
    して作る（モジュールグローバルを実行時に読みには行かない）。

    グローバル変数を実行時参照する設計だと、深さが 0 に戻った瞬間にそのグローバルを None化する
    実装（`_log_redaction_active()` 参照）と、既に `logging.Logger.callHandlers` 経由でこの関数を
    呼び出し中の別スレッドが `original` を読みに行くタイミングとが競合し、後者が None を呼び出そう
    として `TypeError` になりうる。`original` を各インストール世代のクロージャへ固定すれば、
    グローバルの状態が変わっても実行中の呼び出しはそのクロージャが捕まえた値をそのまま使い続ける。
    """
    def _redacting_call_handlers(self: logging.Logger, record: logging.LogRecord):
        """`msg`／`args` だけでなく `exc_info`／`exc_text`／`stack_info` も消す:
        `logging.Formatter.format()` は `record.exc_info` が立っていれば `msg` の内容に関わらず
        独立してトレースバックを整形して追記する契約のため、`msg`／`args` だけの差し替えでは
        `logger.exception(...)` 等のトレースバック付き警告から秘密が漏れうる。"""
        if _is_log_redaction_target(self.name):
            record.msg = _LOG_REDACTION_REPLACEMENT
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return original(self, record)
    return _redacting_call_handlers


@contextlib.contextmanager
def _log_redaction_active():
    """`run_all()` の実行中だけ、`_LOG_REDACTION_TARGET_ROOTS` 配下の**全ロガー**が出すレコードの本文（と
    例外情報）を丸ごと固定文言へ差し替える。

    再入・並行呼び出し安全（参照カウント＋ロック＋世代ごとのクロージャ）: `run_all()` の複数
    呼び出しが重なっても（同一スレッドの再入・別スレッドの並行のどちらでも）、実際に
    `logging.Logger.callHandlers` を差し替えるのは深さが 0→1 になる最初の1回だけで、元へ戻すのは
    対応する最後の呼び出し（深さが 1→0 になる時）だけにする。差し替え本体は `_log_redaction_lock`
    保持中に作った専用のクロージャ（`_make_redacting_call_handlers`）を割り当てる＝実行中の呼び出しが
    参照するのはそのクロージャに固定された `original` であり、復元処理側のグローバル状態を
    実行時に読みに行かない。

    `sherpa.health._check_one`（Postgres/Neo4j/ES の ping）は、返り値（`CheckResult` へ渡す方）は
    `health._classify()` で安全に分類済みだが、同時に `_logger.warning("...: %s", comp_id, e)` で
    **生の例外**をログへ出す独立した経路を持つ（`sherpa.agent_constructs` 等、他の `sherpa.*` モジュール
    にも同種の warning ログがある）。

    `logging.Logger.addFilter()`（フィルタをロガーに付ける方式）はそのロガー**自身**の `.filter()`
    呼び出し時にしか効かない（`Logger.handle()` は自分の `self.filters` だけを見る・祖先ロガーへ
    伝播する際にその祖先の filters を経由しない）ため、`sherpa.*` の任意のロガーを一括では塞げない。
    `logging.Logger.callHandlers()` はログ発生ごとに必ず一度だけ呼ばれる通過点で、伝播経路に
    ハンドラがあろうと無かろうと（`logging.lastResort` 経由でも既存の root ハンドラ経由でも）
    確実に通るため、ここを一時的に差し替える。

    `_sanitize_text()`（パターンベースの秘密マスク）はここでは使わない: 対象の秘密の値そのものを
    知らないまま「Bearer/api-key/sk-/URL らしい形」だけを頼りにマスクするため、URL 構造を伴わない
    平文のパスワード等（例: 区切り文字の扱いを誤った DSN のパスワード片）は検出できず素通りしうる。
    ここで必要なのは「この経路のログ本文に診断価値は無い（同じ情報は安全に分類済みの
    `CheckResult.detail` 側に既にある）」という事実そのものなので、パターン一致に頼らず本文を
    丸ごと固定文言へ差し替える（確実性を優先する）。呼び出し先モジュール自体は変更しない
    （サーバ運用時はハンドラをファイル等へ向けて使う既存の診断ログのまま・doctor 実行中だけこの
    差し替えで限定的に塞ぐ）。
    """
    global _log_redaction_depth, _log_redaction_captured_original
    with _log_redaction_lock:
        _log_redaction_depth += 1
        if _log_redaction_depth == 1:
            original = logging.Logger.callHandlers
            _log_redaction_captured_original = original
            logging.Logger.callHandlers = _make_redacting_call_handlers(original)
    try:
        yield
    finally:
        with _log_redaction_lock:
            _log_redaction_depth -= 1
            if _log_redaction_depth == 0:
                logging.Logger.callHandlers = _log_redaction_captured_original
                _log_redaction_captured_original = None


def _truthy(v: object) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def probe_cloud_enabled() -> bool:
    """`PROBE_CLOUD=1`（既定 OFF）。課金の可能性がある実接続プローブを有効化する明示フラグ。"""
    return _truthy(os.environ.get("PROBE_CLOUD"))


# ---------------------------------------------------------------------------
# 1. ストア疎通
# ---------------------------------------------------------------------------

def _check_component(comp_id: str, label: str) -> CheckResult:
    """`sherpa.health.COMPONENTS`（Postgres／Neo4j／ES の ping・例外の安全な分類）をそのまま使う。

    `health` 自身は「落ちたときの影響」（down/degraded/none）で状態ドットの色を決めるが、
    doctor は導入直後の**セットアップ検査**なので、ストア3種はどれも接続できて当然という前提で
    一律 NG 扱いにする（health の運用時 degrade 許容とは目的が異なる）。
    """
    from sherpa import health
    comp = next(c for c in health.COMPONENTS if c[0] == comp_id)
    out = health._check_one(*comp)
    if out["ok"]:
        return CheckResult(comp_id, label, "ok", "接続できました")
    return CheckResult(comp_id, label, "ng", f"{out['detail']}。{out['hint']}")


def check_postgres() -> CheckResult:
    return _check_component("postgres", "PostgreSQL 疎通")


def check_neo4j() -> CheckResult:
    return _check_component("neo4j", "Neo4j 疎通")


def _es_get(path: str, timeout: float = _ES_TIMEOUT):
    from sherpa import es_index
    with urllib.request.urlopen(es_index._url() + path, timeout=timeout) as r:
        return json.loads(r.read())


def check_es_connect() -> CheckResult:
    """ES 接続＋バージョン確認（`health._ping_es` は疎通のみで版を返さないため、ここは新規の
    最小 GET。接続先の解決（`es_index._url()`）は既存を使う＝ホスト/ポートの再実装はしない）。

    診断ツールとして、バージョンが取得できない応答（JSON null／数値／`version.number` 欠落等）は
    「不明だが OK」にせず NG にする。応答の形の検証も同じ try に収め、想定外の応答形でも例外を
    外へ漏らさず必ず CheckResult を返す。
    """
    cid, label = "elasticsearch", "Elasticsearch 疎通・バージョン"
    from sherpa import health
    try:
        info = _es_get("/")
        if not isinstance(info, dict):
            raise ValueError("ES のルート応答が予期しない形式です（JSON オブジェクトではありません）")
        version = info.get("version")
        number = version.get("number") if isinstance(version, dict) else None
        if not isinstance(number, str) or not number:
            raise ValueError("ES のバージョン情報（version.number）を取得できません")
    except Exception as e:
        return CheckResult(cid, label, "ng", f"{health._classify(e)}（{type(e).__name__}）")
    return CheckResult(cid, label, "ok", f"接続できました（バージョン {number}）")


def check_es_kuromoji(es_ok: bool) -> CheckResult:
    """`analysis-kuromoji` プラグインの導入確認。プラグイン名（`component` フィールド）の**完全一致**
    で判定する（部分一致だと将来の無関係な別プラグイン名に誤って反応しうる）。"""
    cid, label = "es_kuromoji", "Elasticsearch 日本語検索プラグイン（analysis-kuromoji）"
    if not es_ok:
        return CheckResult(cid, label, "skip", "Elasticsearch に接続できないため確認できません")
    from sherpa import health
    try:
        plugins = _es_get("/_cat/plugins?format=json")
        if not isinstance(plugins, list):
            raise ValueError("ES のプラグイン一覧応答が予期しない形式です（配列ではありません）")
        found = any(isinstance(p, dict) and p.get("component") == "analysis-kuromoji" for p in plugins)
    except Exception as e:
        return CheckResult(cid, label, "ng", f"{health._classify(e)}（{type(e).__name__}）")
    if found:
        return CheckResult(cid, label, "ok", "導入されています")
    return CheckResult(cid, label, "ng",
                        "未導入です（動作はしますが日本語の検索精度が下がります・"
                        "analysis-kuromoji プラグインの導入を推奨）")


# ---------------------------------------------------------------------------
# 2. 設定の妥当性
# ---------------------------------------------------------------------------

def _fetch_system_settings_readonly() -> dict:
    """`system_settings` を DDL 抜きで読む。

    `sherpa.store.get_system_settings()` は未初期化なら `_ensure()`→`init_schema()`（全テーブルの
    CREATE／列追加／是正 UPDATE）を実行してから読む契約で、doctor の読み取り専用契約に反する
    （DDL 権限を持たない読み取り専用ロールで動かせない・advisory lock も取る）。ここでは
    `sherpa.store.db._connect()`（DSN／`row_factory` の組み立てだけを再利用）で新規のショート接続を
    開き、`SELECT key, value FROM system_settings` だけを実行する。テーブル未作成・権限不足は
    そのまま例外を呼び出し元へ返す（握り潰さない＝呼び出し元は NG として扱う契約）。
    """
    from sherpa.store.db import _connect
    with _connect(connect_timeout=_PG_READONLY_TIMEOUT, options=_PG_READONLY_OPTIONS) as c:
        rows = c.execute("SELECT key, value FROM system_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _read_active_user_configs_readonly() -> list[dict]:
    """有効な（`users.status='active'`）利用者の `user_settings` を DDL 抜きで読む（読み取り専用
    SELECT のみ・理由は `_fetch_system_settings_readonly` と同じ）。無効化済み利用者の残存設定を
    要否判定に混入させない。`user_id` は読まない（個々の利用者名は出力に使わない・件数のみ扱う）。

    `ollama_url`／`search_helper` も併せて読む（`_resolve_ollama_usages` が個人の接続先上書き・
    検索ヘルパー経由の Ollama 利用を判定するために使う）。

    個人 API キーの**値そのものは読まない**（`check_selected_provider_key` が A6 個人キー許可時の
    「中央キーは無いが個人キーを持つ利用者が n 人いる」を判定するために使うのは真偽値だけで十分
    ＝キー列そのものを SELECT せず、`IS NOT NULL AND <> ''` を SQL 側で真偽値へ畳んでから返す）。
    この真偽値は**本番の truthy 判定**（`sherpa.keys.resolve_api_key()` の `if personal: return
    personal`）と完全に一致させる: プレースホルダ値（`sk-REPLACE_ME` 等）・空白のみの値も本番は
    「値あり」として実際に使おうとするため、ここで `NULL`／空文字列以外を一律「あり」として畳む
    （プレースホルダ・空白を除外する処理は入れない＝除外すると、本番が実際に送信を試みて必ず
    失敗する構成を doctor だけが「未使用」と誤認する）。
    """
    from sherpa.store.db import _connect
    with _connect(connect_timeout=_PG_READONLY_TIMEOUT, options=_PG_READONLY_OPTIONS) as c:
        rows = c.execute(
            "SELECT us.agent, us.codex_model_provider, us.ollama_url, us.search_helper, "
            "(us.openai_api_key IS NOT NULL AND us.openai_api_key <> '') AS has_openai_key, "
            "(us.gemini_api_key IS NOT NULL AND us.gemini_api_key <> '') AS has_gemini_key, "
            "(us.bedrock_api_key IS NOT NULL AND us.bedrock_api_key <> '') AS has_bedrock_key "
            "FROM user_settings us JOIN users u ON u.uid = us.user_id "
            "WHERE u.status = 'active'",
        ).fetchall()
    return [dict(r) for r in rows]


def _load_system_settings(pg_ok: bool) -> tuple[CheckResult, dict | None]:
    """system_settings の読み取り結果を1つの `CheckResult` として明示する（読み取り失敗を黙って
    `sys_s = None` にすると、以後の各項目が理由を示さないまま `skip` だらけになるため）。"""
    cid, label = "system_settings_read", "system_settings 読み取り（読み取り専用 SELECT・DDL なし）"
    if not pg_ok:
        return CheckResult(cid, label, "skip", "PostgreSQL に接続できないため確認できません"), None
    try:
        sys_s = _fetch_system_settings_readonly()
    except Exception as e:
        from sherpa import health
        detail = (f"{health._classify(e)}（{type(e).__name__}）。DDL 権限が無い構成、または"
                  "アプリを一度も起動していない（スキーマ未作成）可能性があります")
        return CheckResult(cid, label, "ng", detail), None
    return CheckResult(cid, label, "ok", "読み取れました"), sys_s


def _load_active_user_configs(pg_ok: bool) -> tuple[CheckResult, list[dict] | None]:
    """有効な利用者の `user_settings` 読み取り結果を1つの `CheckResult` として明示する（同上の理由）。
    Codex／Ollama の要否判定（`_codex_required`／`_resolve_ollama_usages`）が使う。"""
    cid, label = "user_settings_read", "user_settings 読み取り（有効利用者のみ・読み取り専用 SELECT）"
    if not pg_ok:
        return CheckResult(cid, label, "skip", "PostgreSQL に接続できないため確認できません"), None
    try:
        rows = _read_active_user_configs_readonly()
    except Exception as e:
        from sherpa import health
        detail = (f"{health._classify(e)}（{type(e).__name__}）。"
                  "Codex/Ollama の要否判定は安全側（必須扱い）に倒します")
        return CheckResult(cid, label, "ng", detail), None
    return CheckResult(cid, label, "ok", f"読み取れました（{len(rows)} 件）"), rows


_OPENAI_ENDPOINT_CID = "openai_endpoint"
_OPENAI_ENDPOINT_LABEL = "接続先設定（openai_endpoint_kind／openai_base_url）の妥当性"


def _openai_endpoint_status(sys_s: dict | None) -> dict:
    """接続先設定（`openai_endpoint_kind`／`openai_base_url`）の判定結果と、以後の OpenAI/Gemini/
    Bedrock 系チェックが使うべき**実効** `system_settings` を1回でまとめて返す
    （`{"status": "ok"/"ng"/"skip", "detail": str, "effective_sys_s": dict | None}`）。

    `sys_s` に起動時シードのマーカー（`openai_endpoint_seed_version`）がまだ無い（`NO_MARKER`）
    場合、`sys_s` 自体は接続先関連キーを一切持たない（＝`openai_endpoint_kind()` は既定 "openai"
    へフォールバックする）。しかし env に妥当な起動時シード候補（`OPENAI_BASE_URL` 等）が既に
    存在するなら、実際にアプリを起動した瞬間にその候補が system_settings へ取り込まれ、以後の
    リクエストは Azure/custom 接続先を使う（`sherpa/api.py::_seed_openai_endpoint_from_env` 参照）。
    doctor がこの取り込みを反映しないまま生の `sys_s`（既定 "openai" のまま）で実キーを使って
    プローブすると、Azure 専用に発行されたキーが `api.openai.com` へ誤って `Authorization: Bearer`
    付きで送られてしまう。ここで env 候補を検証し、妥当なら「`sys_s`（DB の既存値）優先・欠損
    しているキーだけ候補で補完」した**コピー**を `effective_sys_s` として返す（`sys_s` 自体は
    書き換えない・本番のシードが既存行を上書きしない契約と一致させる＝下記参照）ことで、以後の
    チェックが「一度起動すればこうなる」設定を使って判定できるようにする。

    `status` が `"ng"`（`DB_ENDPOINT_INVALID`／env 候補が不正で確定できない）のときは
    `effective_sys_s` を `None` にする＝呼び出し元（`run_all`）はこれをそのまま
    `check_selected_provider_key`／`check_cloud_llm_probes` へ渡すことで、既存の「`sys_s is None`
    なら送信ゼロで全 SKIP」という契約にそのまま乗せられる（接続先が確定できない状態で、どの
    キーをどこへ送ってよいか判定できないため、送信を伴う確認は一律停止する）。
    """
    if sys_s is None:
        return {"status": "skip",
                "detail": "system_settings を読み取れないため確認できません"
                          "（「system_settings 読み取り」の項目を確認してください）",
                "effective_sys_s": None}
    import check_production_openai_probe as _probe_mod
    lines = _probe_mod.probe(lambda: sys_s)
    status0 = lines[0]
    if status0 == "MARKER_FOUND":
        return {"status": "ok", "detail": f"妥当です（接続先種別: {lines[1]}）", "effective_sys_s": sys_s}
    if status0 == "DB_ENDPOINT_INVALID":
        return {"status": "ng",
                "detail": "接続先の設定が不正です（管理画面の「接続先」欄を確認してください）",
                "effective_sys_s": None}
    if status0 == "NO_MARKER":
        # 初回シード前（system_settings に接続先マーカーが無い）状態でも、次回起動時に取り込まれる
        # env 候補（OPENAI_BASE_URL 等）自体が不正なら、それを見逃さず検出する（Azure/custom の
        # base_url 欠落・危険な URL・未知 kind 等）。
        from sherpa import llm
        try:
            candidate = llm.openai_endpoint_seed_candidate()
        except ValueError as e:
            return {"status": "ng",
                    "detail": f"初回起動前です。次回起動時に取り込まれる環境変数の接続先候補が不正です: {e}",
                    "effective_sys_s": None}
        # 本番のシード（`store.seed_system_settings_once`）は行単位の `WHERE NOT EXISTS`＝
        # 「そのキーの行が既に system_settings に存在するか」だけで判定し、既存の行は絶対に
        # 上書きしない（`sys_s` は該当キーの行が無ければそもそも辞書にキー自体を持たない＝
        # `_fetch_system_settings_readonly` は `{key: value for row in rows}` なので、行の有無と
        # dict のキー有無が一致する）。したがって合成は「DB（`sys_s`）優先・欠損しているキーだけ
        # env 候補で補完」（`dict.setdefault`）でなければならない＝逆（env 優先）だと、admin が
        # 既に保存した値を env が上書きしたかのような、本番では起こらない実効値を作ってしまう。
        effective = dict(sys_s)
        for k, v in candidate.items():
            effective.setdefault(k, v)
        # 合成後の kind/base URL が矛盾しないか、初回シード済み（`MARKER_FOUND`）の判定と
        # **同じ検証**へ通す（`validate_endpoint_settings` 参照）。候補単体の検証（`openai_endpoint_
        # seed_candidate()` 内部）は候補の4キー同士のクロス検証だけで、`sys_s` に既存の行がある
        # 場合の組合せ（例: DB に kind=azure の行だけがあり base_url の行が無い状態へ、
        # base_url を含まない候補を補完しても直らない）までは見ていない。
        if _probe_mod.validate_endpoint_settings(effective) is None:
            return {"status": "ng",
                    "detail": "初回起動前です。既存の system_settings と環境変数の接続先候補を"
                              "合成した結果が不正です（管理画面の「接続先」欄・環境変数の両方を確認してください）",
                    "effective_sys_s": None}
        return {"status": "ok",
                "detail": "初回起動前です（次回起動時に環境変数から自動設定されます・環境変数の候補も妥当です）",
                "effective_sys_s": effective}
    return {"status": "skip", "detail": "確認できません", "effective_sys_s": None}


def check_openai_endpoint(sys_s: dict | None) -> CheckResult:
    r = _openai_endpoint_status(sys_s)
    return CheckResult(_OPENAI_ENDPOINT_CID, _OPENAI_ENDPOINT_LABEL, r["status"], r["detail"])


def _as_key_str(value: object) -> str | None:
    """`system_settings`／`user_settings` の JSONB から読んだキー値を、`agent_constructs.
    is_real_api_key()` へ渡す前に**文字列以外を無効値として弾く**共通の正規化。

    JSONB 列は型を保証しないため、キー欄に数値・真偽値・オブジェクト・配列等の非文字列値が
    入っていることがありうる（破損した system_settings・直接の DB 編集・移行時の型変換ミス等）。
    `is_real_api_key()` は `value.strip()` を呼ぶ契約（`(value or "").strip()`）のため、非文字列の
    truthy な値（`42`／`{"a": 1}`／`[1]` 等）を渡すと `AttributeError` を送出し、呼び出し元を
    経由して `run_all()` 全体を未捕捉の traceback で中断させうる。ここで非文字列を一律 `None`
    （＝「無効キー」）へ丸めることで、後続の `is_real_api_key(None)` は安全に `False` を返す
    （「キーが読めない・型がおかしい」を「未設定」と同じ扱いにする＝fail-closed）。
    """
    return value if isinstance(value, str) else None


def _personal_key_holder_count(provider: str, sys_s: dict | None, rows: list[dict] | None) -> int:
    """A6: 中央キーが無くても、`personal_api_keys_allowed` が真で有効な利用者の誰かが
    `provider` の個人キーを保存済みなら、その人数分は動く。個人キーの値そのものは一切読まない
    （`_read_active_user_configs_readonly` が SQL 側で真偽値へ畳んだ列だけを見る＝本番の truthy
    判定と同じ生値判定で、プレースホルダ・空白のみの値も「あり」に数える・除外はしない）。

    `keys.resolve_api_key(provider, ...)` は A7（排他選択）ゲートを持ち、`provider` が現在の
    `cloud_provider`（システム選択）と一致しない限り、保存済みの個人キーがあっても常に `None` を
    返す（温存されるだけで解決されない）。ここでも同じゲートを適用しないと、選択されていない
    provider の残存キーを「その人数分は動く」と誤って数えてしまう（例: `cloud_provider=gemini` の
    利用者が過去に保存した `openai_api_key` は、選択が gemini である限り誰も解決できない）。
    """
    from sherpa import keys
    if sys_s is None or rows is None or not keys.personal_keys_allowed(sys_s):
        return 0
    if keys.selected_cloud_provider(sys_s) != provider:
        return 0
    field = f"has_{provider}_key"
    return sum(1 for row in rows if row.get(field))


def _central_auth_available(provider: str, sys_s: dict) -> bool:
    """中央（システム既定）の認証情報だけで `provider` が実際に動くか。

    Bedrock だけは中央 API キー以外に AWS SigV4（`AWS_ACCESS_KEY_ID`／`AWS_PROFILE`／
    `~/.aws/credentials`）でも正当に動く（`sherpa/providers/bedrock.py::_bedrock_auth_available`
    が唯一の真実源）。中央キーが実在するプレースホルダ値（`sk-REPLACE_ME` 等）のときは
    `is_real_api_key()` で弾いてから `_bedrock_auth_available` へ渡す（同関数は単純な truthy
    判定しかしないため、プレースホルダ文字列も「キーあり」として通してしまう）。中央キーは
    JSONB 由来で型を保証しないため、`is_real_api_key()` へ渡す前に `_as_key_str()` で
    非文字列を弾く（`is_real_api_key()` は `.strip()` を呼ぶ契約＝非文字列だと `AttributeError`）。
    """
    from sherpa import agent_constructs, keys
    central_key = keys.resolve_api_key(provider, None, system_settings=sys_s)
    is_real_central_key = agent_constructs.is_real_api_key(_as_key_str(central_key))
    if provider == "bedrock":
        from sherpa.agents import _bedrock_auth_available
        return _bedrock_auth_available(central_key if is_real_central_key else None)
    return is_real_central_key


_INDETERMINATE = object()   # `effective_agent()` 等が例外を投げ、値そのものを判定できなかったことを表す番人


def _chat_or_codex_consumes(selected: str, sys_s: dict, rows: list[dict] | None) -> bool:
    """chat／Codex 経由（`agent_constructs.effective_agent()`）で `selected` が実際に消費されるか
    （第2経路＝`sherpa.llm.select_provider()` 系は含まない・狭義）。`_cloud_provider_consumed`／
    `_consumed_llm_purposes` の "chat" 判定の共有実装。

    `ollama_only`／`codex_ollama` のみで構成された環境では、触っていない既定値（`openai`）の
    キー欄が空でも実害が無い。判定は実行時契約（`sherpa/providers/__init__.py::get_provider`／
    `_select_provider`）と一致させる:
      - システム既定（未設定利用者が使う経路）・有効な利用者いずれかの実効頭脳が `selected` と
        一致する。
      - `selected == "openai"` かつ、**その同じスコープ**（システム既定／該当利用者）の実効頭脳が
        実際に `"codex"` で、かつ Codex の接続先が Azure/custom（`codex_model_provider` が
        `ollama` でない）＝この構成は `resolve_api_key("openai", ...)` を必ず使う
        （`sherpa.providers._codex_openai_compat_block_reason` 参照）。実効頭脳が `codex` で
        ないのに `codex_model_provider` の残存値**だけ**で消費扱いにしない（実際には使われていない
        過去の設定の残骸を誤検出しない）。
      - 検索ヘルパー（`search_helper`）は単独の判定材料にしない: `get_provider()` が実際に
        `search_helper.resolve()` を配線するのは主頭脳が `"openai"`（`provider_id == "openai"`）の
        ときだけなので、その場合は既に「実効頭脳が selected と一致」で捕捉済み（`search_helper` を
        主頭脳と無関係に評価すると、主頭脳が codex/ollama の利用者の残存設定を誤って
        「openai を消費している」扱いにしてしまう）。
      - `effective_agent()` が gemini/bedrock を返しても、`agent_constructs.runtime_blocked()`
        が真（`SHERPA_EXTRA_AGENTS` に含まれず現在の環境では無効）なら実行時は
        `_DisabledProvider` に差し替わり、キーは一切参照されない（`sherpa/providers/__init__.py::
        _select_provider` 参照）。この構成は「保存されているが無効」であって「selected を消費して
        いる」わけではない（無効な理由は `_disabled_agent_configs` が別途 NG として報告する）。

    読み取り不能（`rows is None`）・`effective_agent()` の例外（設定解決不能）は、いずれも
    「判定不能」を「消費していない（一致しない）」へ丸めず fail-closed（消費している扱い）にする
    （`_INDETERMINATE` 番人を経由し、`_actually_routes_to` が一律 `True` を返す一箇所に集約する＝
    例外を握り潰して「一致しない」扱いにする経路を残さない）。
    """
    from sherpa import agent_constructs, llm

    def _codex_consumes_openai(eff: str | None, cmp: str) -> bool:
        if eff != "codex" or cmp == "ollama":
            return False
        try:
            return llm.openai_endpoint_kind(sys_s) != "openai"
        except ValueError:
            return True   # 接続先の設定自体が壊れている＝安全側で「消費している」扱い

    def _actually_routes_to(eff) -> bool:
        if eff is _INDETERMINATE:
            return True   # 判定不能＝安全側で「消費している」扱い
        if eff != selected:
            return False
        try:
            return not agent_constructs.runtime_blocked(eff)
        except Exception:
            return True   # 判定できなければ安全側で「消費している」扱い

    try:
        default_eff = agent_constructs.effective_agent(None, system_settings=sys_s)
    except Exception:
        default_eff = _INDETERMINATE
    if _actually_routes_to(default_eff):
        return True
    if (selected == "openai" and default_eff is not _INDETERMINATE
            and _codex_consumes_openai(default_eff, agent_constructs.codex_model_provider(None))):
        return True
    if rows is None:
        return True
    for row in rows:
        settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider")}
        try:
            eff = agent_constructs.effective_agent(settings, system_settings=sys_s)
        except Exception:
            eff = _INDETERMINATE
        if _actually_routes_to(eff):
            return True
        if (selected == "openai" and eff is not _INDETERMINATE
                and _codex_consumes_openai(eff, agent_constructs.codex_model_provider(settings))):
            return True
    return False


_AGENT_RESOLUTION_FAILED_DETAIL = "設定の解決に失敗しました（利用者設定から実効頭脳を判定できません）"


def _agent_resolution_indeterminate(sys_s: dict, rows: list[dict] | None) -> bool:
    """`agent_constructs.effective_agent()` がシステム既定または有効な利用者いずれかで例外を
    投げるか（＝以後の「実効頭脳が何を指すか」に依存する判定全般が根本的に信頼できない状態か）。

    `_chat_or_codex_consumes`／`_agent_actually_used`／`_codex_required` は、この種の例外を
    fail-closed な**真偽値**（「消費している」／「必須」）へ丸めるだけで、判定不能だったこと
    自体を独立した NG として利用者に示さない。真偽値だけを見ると、その真偽値がたまたま
    「消費されている」「必須」側に転んでも、後続の判定（認証情報の有無・CLI の有無等）が
    たまたま問題なしに見えて `ok`／`skip` になり得る＝設定解決そのものが壊れているという根本
    原因が握り潰され、無関係な別の検査（例: Ollama の要否判定・そちらは `_resolve_ollama_usages`
    が独自に `type_error` で NG にする）にだけ症状が現れて誤帰属する。

    呼び出し元（`check_selected_provider_key`／`check_cloud_llm_probes`／`check_codex` 系）は、
    通常の判定へ進む**前**にこの関数を呼び、真なら自身の検査項目を固定文言
    （`_AGENT_RESOLUTION_FAILED_DETAIL`）の NG として報告する。`_chat_or_codex_consumes` 等の
    fail-closed な真偽値そのものは変更しない（この関数が見逃した場合の二重の安全網として残す）。

    `rows is None` はここでは対象外（`user_settings_read` 項目が別途 NG を報告する契約・この
    関数は `effective_agent()` 自体が例外を投げるかどうかだけを見る）。
    """
    from sherpa import agent_constructs
    try:
        agent_constructs.effective_agent(None, system_settings=sys_s)
    except Exception:
        return True
    if rows is None:
        return False
    for row in rows:
        settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider")}
        try:
            agent_constructs.effective_agent(settings, system_settings=sys_s)
        except Exception:
            return True
    return False


def _second_path_purposes(provider: str) -> tuple[str, ...]:
    """`provider` が openai／gemini のとき、chat／Codex とは独立した「第2の消費経路」
    （`sherpa.llm.select_provider()`／`resolve_auto_provider()` を共有実装として使う自動解決）が
    実際に対応する用途一覧。

    intent（`sherpa/intent_llm.py::_cfg`）・embed（`sherpa/embeddings.py::cfg`）はどちらも
    `select_provider()` へ `bedrock=` factory を渡していない＝Bedrock 非対応
    （`resolve_auto_provider(..., bedrock_capable=False)`）。render（`sherpa/ingest/llm_render.py`
    経由の `graph_extract.available(usage="render")`・rag.md の LLM 成形・GRAPH-SRC 2026-09-04 で
    旧 extract 用途の実消費先を継承）は Bedrock 対応の factory を渡すが、この関数は従来どおり
    openai／gemini だけを対象にする（Bedrock をこの第2経路の自動検出対象に含めない・チャット/Codex
    経由の `_chat_or_codex_consumes` のみで判定する＝過剰消費扱いを避ける・挙動は変更しない）。

    `SHERPA_DISABLE_EMBED`（`sherpa/embeddings.py::cfg` が最初に見るキルスイッチ）が設定されている
    環境では、embed は provider に関わらず実際には一切自動解決されない＝ここで対象から除く
    （設定されていても "intent"／"render" は引き続き対象＝キルスイッチは embed 専用）。
    """
    if provider not in ("openai", "gemini"):
        return ()
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return ("intent", "render")
    return ("intent", "render", "embed")


def _second_path_truthy(provider: str, sys_s: dict, rows: list[dict] | None) -> bool:
    """第2経路（intent／extract／embed）が `provider` を実際に選ぶか（`sherpa.llm.
    resolve_auto_provider()` と同じ truthy 判定・`is_real_api_key()` ではない）。

    本番はプレースホルダ・空白等の truthy な非文字列値でも実際に送信を試みる（`resolve_auto_
    provider` は `bool(...)` しか見ない）ため、ここを `is_real_api_key()`（認証有効性）で判定すると
    「送信は試みるが認証が無効」な構成を doctor だけが見逃す（消費判定と認証有効性の判定は別物＝
    認証が無効なら NG にするのは呼び出し側（`_central_auth_available`／`_run_raw_llm_probe` の
    送信前ガード）の責務）。

    中央キーが無くても、A6（個人 API キー許可）が有効で A7 選択が `provider` と一致するなら、
    本番はリクエストを行った**利用者自身**の `user_settings`（値は doctor が読まないため
    `has_{provider}_key` の真偽列だけを見る）を渡して `resolve_api_key()` を呼ぶ（`intent_llm.py::
    _cfg` 等は per-user 呼び出し）。したがって、中央キーが無くても有効な利用者の誰かが個人キーを
    保存済みなら、その利用者の操作では実際に消費される扱いにする（`_personal_key_holder_count` と
    同じ A6／A7 ゲートを適用するが、`rows is None`（読み取り不能）時は「0人」ではなく fail-closed
    （消費している扱い）にする点が異なる＝あちらは「中央検査を SKIP に緩和してよいか」の判定、
    こちらは「そもそも消費されているか」の判定で、不明時に倒す向きが逆になる）。
    """
    from sherpa import keys
    if keys.resolve_api_key(provider, None, system_settings=sys_s):
        return True
    if not keys.personal_keys_allowed(sys_s) or keys.selected_cloud_provider(sys_s) != provider:
        return False
    if rows is None:
        return True
    field = f"has_{provider}_key"
    return any(row.get(field) for row in rows)


def _consumed_llm_purposes(provider: str, sys_s: dict, rows: list[dict] | None) -> list[str]:
    """`provider` が現在の構成で実際に消費されている用途一覧（"chat"／"intent"／"render"／
    "embed"）。空リストなら一切消費されていない（`check_selected_provider_key`／
    `check_cloud_llm_probes` の SKIP 判定にそのまま使う）。

    "chat" は `_chat_or_codex_consumes`（chat／Codex 経由・fail-closed）で判定する。第2経路
    （intent／render／embed）は `_second_path_truthy`（中央キー truthy＋A6 個人キーの両方を見る）
    で判定する。
    """
    purposes: list[str] = []
    if _chat_or_codex_consumes(provider, sys_s, rows):
        purposes.append("chat")
    if provider in ("openai", "gemini") and _second_path_truthy(provider, sys_s, rows):
        purposes.extend(_second_path_purposes(provider))
    return purposes


def _cloud_provider_consumed(selected: str, sys_s: dict, rows: list[dict] | None) -> bool:
    """`selected`（`cloud_provider` の選択値）が現在の構成で何らかの用途に実際に消費されるか
    （`_consumed_llm_purposes` の非空判定・判定基準の詳細は同関数の docstring 参照）。"""
    return bool(_consumed_llm_purposes(selected, sys_s, rows))


def check_selected_provider_key(sys_s: dict | None, rows: list[dict] | None) -> CheckResult:
    """選択中クラウドプロバイダ（A7 排他選択）の認証情報があるか。

    認証情報が実際にあれば（現在の構成でそれが消費されるかに関わらず）常に `ok` にする＝
    有効な設定を「使っていないかもしれないから」という理由で隠さない。中央キーが無い場合は、
    まず A6（個人 API キー許可）を見る: `personal_api_keys_allowed` が真で、有効な利用者の誰かが
    個人キーを保存済みなら（値は読まない・読み取り専用 SELECT が真偽値へ畳んだ列だけを見る）、
    その人数分は実際に動く構成として `ok`（情報表示で人数のみ示す）。それも無い場合だけ
    `_cloud_provider_consumed` を見て、現在の構成で実際に消費されないなら（`ollama_only`／
    `codex_ollama` のみの構成で `cloud_provider` が既定値のまま放置されている等）`skip` に、
    消費されるなら `ng` にする。

    Bedrock だけは中央 API キー以外に AWS SigV4（`AWS_ACCESS_KEY_ID`／`AWS_PROFILE`／
    `~/.aws/credentials`）でも正当に動く（`sherpa/providers/bedrock.py::_bedrock_auth_available`
    が唯一の真実源）。ここを `is_real_api_key` だけで判定すると、SigV4 構成の正常な環境を
    誤って NG にしてしまう。

    `_agent_resolution_indeterminate()` が真（`effective_agent()` が例外を投げる＝実効頭脳が
    判定不能）なら、上記の通常判定へは進まず本項目自身を固定文言の NG にする（`_cloud_provider_
    consumed` 内部の fail-closed な真偽値だけに頼ると、認証情報がたまたま実在する構成では
    `ok` になってしまい、判定不能だったこと自体が握り潰される）。
    """
    label = "選択中クラウドプロバイダの認証情報"
    cid = "selected_provider_key"
    if sys_s is None:
        return CheckResult(cid, label, "skip",
                            "system_settings を読み取れないため確認できません"
                            "（「system_settings 読み取り」の項目を確認してください）")
    if _agent_resolution_indeterminate(sys_s, rows):
        return CheckResult(cid, label, "ng", _AGENT_RESOLUTION_FAILED_DETAIL)
    from sherpa import keys
    selected = keys.selected_cloud_provider(sys_s)

    if _central_auth_available(selected, sys_s):
        return CheckResult(cid, label, "ok", f"選択中の {selected} は認証情報が確認できます")

    n = _personal_key_holder_count(selected, sys_s, rows)
    if n:
        return CheckResult(cid, label, "ok",
                            f"中央キーは未設定ですが個人キーの利用が許可されており、"
                            f"有効な利用者のうち {n} 人が個人キーを保存済みです"
                            "（値は確認しません。中央キーが無い他の利用者は動きません）")
    # RV1（FBK-1・境界回帰#7・2026-09-01）: `cloud_provider` の生の保存値がある（＝admin が明示選択
    # 済み）なら、チャット/Codex の実効頭脳が Ollama 等で「今は」消費していなくても NG にする——
    # fail-loud（`llm.resolve_auto_provider`）の下では、intent/extract/embed（第2経路）は明示選択
    # 済みのクラウドを試みてキー不足で失敗するだけで Ollama へは倒れないため、`_cloud_provider_
    # consumed`（現在の実効消費のみを見る）だけでは「使われていません」という誤った skip になる。
    # 生の保存値が無い（既定値がそのまま残っているだけ）ときは、従来どおり実消費有無で判定する。
    if _cloud_provider_consumed(selected, sys_s, rows) or keys.cloud_provider_explicitly_selected(sys_s):
        detail_ng = (
            "選択中のクラウドプロバイダ（bedrock）の認証情報がありません"
            "（管理画面でキーを設定するか、AWS_ACCESS_KEY_ID/AWS_PROFILE/"
            "~/.aws/credentials のいずれかを用意してください）"
            if selected == "bedrock" else
            f"選択中のクラウドプロバイダ（{selected}）のキーが未設定です（管理画面で設定してください）"
        )
        return CheckResult(cid, label, "ng", detail_ng)
    return CheckResult(cid, label, "skip",
                        f"選択中のクラウドプロバイダ（{selected}）は現在の構成では使われていません"
                        "（既定値が残っているだけの可能性があります）")


def _disabled_agent_configs(sys_s: dict | None, rows: list[dict] | None) -> CheckResult:
    """保存された頭脳構成（システム既定／有効な利用者）が gemini/bedrock を指しているが、
    現在の環境では `SHERPA_EXTRA_AGENTS` に含まれておらず無効（`agent_constructs.runtime_blocked()`
    が真）な件数を報告する。

    `_select_provider`（`sherpa/providers/__init__.py`）はキー解決より先に `runtime_blocked` を
    見て `_DisabledProvider` に差し替えるため、この構成は「キーが無くて動かない」のではなく
    「そもそも実行時に無効化されている」＝原因が異なる。キー消費の判定（`_cloud_provider_consumed`）
    からは除外した上で、ここで独立の NG として理由を明示する（実行時のエラー表示と一致させ、
    キーを設定しても解決しないことを利用者が誤解しないようにする）。
    """
    cid, label = "disabled_agent_configs", "無効化された頭脳構成（SHERPA_EXTRA_AGENTS 未設定）"
    if sys_s is None or rows is None:
        return CheckResult(cid, label, "skip",
                            "system_settings／user_settings を読み取れないため確認できません")
    from sherpa import agent_constructs
    count = 0
    try:
        if agent_constructs.runtime_blocked(agent_constructs.effective_agent(None, system_settings=sys_s)):
            count += 1
    except Exception:
        pass
    for row in rows:
        settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider")}
        try:
            eff = agent_constructs.effective_agent(settings, system_settings=sys_s)
        except Exception:
            continue
        if agent_constructs.runtime_blocked(eff):
            count += 1
    if count:
        return CheckResult(cid, label, "ng",
                    f"{count} 件の構成（システム既定または有効な利用者）が gemini/bedrock を"
                    "選んでいますが、現在の環境では無効です（SHERPA_EXTRA_AGENTS に含まれていない）。"
                    "実行時はエラー表示になります（鍵の有無とは無関係）")
    return CheckResult(cid, label, "ok", "該当する構成はありません")


# ---------------------------------------------------------------------------
# 3. LLM 最小プローブ
# ---------------------------------------------------------------------------

_CLOUD_LLM_CHECKS = ("openai", "gemini", "bedrock")
_CLOUD_LLM_LABELS = {
    "openai": "OpenAI / Azure OpenAI 最小プローブ",
    "gemini": "Gemini 最小プローブ",
    "bedrock": "AWS Bedrock 最小プローブ",
}


_LLM_PROBE_SYSTEM = "Return a JSON object only."
_LLM_PROBE_USER = 'Return {"ok":true}'


def _agent_actually_used(target_agent: str, sys_s: dict, rows: list[dict] | None) -> bool:
    """`target_agent`（`agent_constructs.effective_agent()` が返す値の1つ・"openai"／"codex"／
    "ollama"／"gemini"／"bedrock"）が、システム既定または有効な利用者のいずれかで**実際に**
    実効頭脳として使われているか。`agent_constructs.runtime_blocked()` で無効化されている構成は
    「使われている」に数えない（`_select_provider` がキー解決より先に `_DisabledProvider` へ
    差し替えるため）。読み取り不能（`rows is None`）・判定不能な例外は fail-closed（使われている
    扱い）にする。

    `_cloud_provider_consumed`（A7 の `cloud_provider` 選択が消費されるか）とは別物: こちらは
    Codex が Azure 経由で同じ `resolve_api_key("openai", ...)` を間接的に使うケース（`codex_model_
    provider` 経由の消費）を「使われている」に含めない＝`effective_agent()` そのものが
    `target_agent` と一致する場合だけを見る（狭義の判定）。

    `effective_agent()` 自体が例外を投げた場合も、`_INDETERMINATE` 番人を経由して fail-closed
    （使われている扱い）にする（`_chat_or_codex_consumes` と同じ契約・例外を「一致しない」に
    丸めて見逃さない）。"""
    from sherpa import agent_constructs

    def _matches(eff) -> bool:
        if eff is _INDETERMINATE:
            return True   # 判定不能＝安全側で「使われている」扱い
        if eff != target_agent:
            return False
        try:
            return not agent_constructs.runtime_blocked(eff)
        except Exception:
            return True

    try:
        default_eff = agent_constructs.effective_agent(None, system_settings=sys_s)
    except Exception:
        default_eff = _INDETERMINATE
    if _matches(default_eff):
        return True
    if rows is None:
        return True
    for row in rows:
        settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider")}
        try:
            eff = agent_constructs.effective_agent(settings, system_settings=sys_s)
        except Exception:
            eff = _INDETERMINATE
        if _matches(eff):
            return True
    return False


_PURPOSE_LABELS = {"chat": "チャット", "render": "検索用文書の整形", "intent": "意図分類", "embed": "埋め込み"}


def _openai_azure_deployment_reason(purpose: str, sys_s: dict) -> str | None:
    """OpenAI の接続先が既定(openai)以外（Azure/custom）のときに、`purpose` 用途のモデル
    （デプロイ名）が実際に登録されているかを**ネットワーク I/O なしで**静的に確認する。未登録
    なら理由文字列を、問題なければ（対象外の場合を含め）`None` を返す。

    "chat" 用途は `sherpa/providers/__init__.py::_select_provider` の `agent == "openai"` 分岐と
    **同じ判定条件**（`_eff_kind != "openai"` かつ解決モデルが空または組み込み既定
    `hardcoded_fallback` のまま）をここでミラーする。判定に使う値の解決自体
    （`llm.openai_endpoint_kind()`／`model_catalog.resolve_model()`／
    `model_catalog.hardcoded_fallback()`）は共有関数をそのまま呼ぶが、`_select_provider` 側には
    この判定条件だけを取り出して呼べる共有関数が無い（判定と `_UnwiredProvider` の組み立てが
    1つの分岐に混在している）ため、条件式そのものは意図的にここへ複製している＝実行時契約と
    食い違わせないよう、条件式を変えるときは両方を同時に直すこと。

    呼び出し元（`check_cloud_llm_probes`）が実際に渡す `purpose` は "chat"（`_agent_actually_used`
    で絞った狭義の直結消費のときのみ）と "embed"（`_embed_static_check` 経由・実プローブを持たない
    埋め込み用途の代替の静的検査）の2つに限る。"intent"／"render" にはこの「デプロイ名が組み込み
    既定のままなら NG」という判定条件を裏付ける実行時ガードが本番に存在しない（`_select_provider`
    の `_UnwiredProvider` 分岐は "openai"（chat 直結）専用・intent/render の呼び出し元
    （`intent_llm.py`／`ingest/llm_render.py`）は解決したモデル名をそのまま送るだけで、組み込み既定の
    ままかどうかを事前に検査して止める分岐を持たない）ため、この関数を intent/render へは適用
    しない（実際の接続確認は実プローブ側に委ねる）。

    本番側は "chat" のこの構成を `_UnwiredProvider`（実行時に必ず失敗）へ倒すため、doctor はこれを
    **課金を伴う実プローブとは独立に**（`PROBE_CLOUD` の設定に関わらず）検出する必要がある
    （`_check_codex_azure_compat` が同様に PROBE_CLOUD 非依存で確認しているのと同じ位置付け・
    なお Codex(Azure/custom) 自身のデプロイ名確認は本関数の対象外＝`_check_codex_azure_compat`／
    `_codex_openai_compat_block_reason` が "codex" 用途で別途担当する）。

    接続先が既定(openai)ならこのガードの対象外（`None`）。`openai_endpoint_kind()` 自体が不正な
    値で失敗する場合も、別の既存チェック（`selected_provider_key` 等）が拾う前提でここでは
    `None` を返す。`model_catalog` 側の解決が壊れた設定（型不正な JSONB 値等）で例外を投げても
    `run_all()` 全体を巻き込まないよう、この関数の外へは伝播させず理由文字列（NG 扱い）へ変換する。
    """
    from sherpa import llm
    try:
        eff_kind = llm.openai_endpoint_kind(sys_s)
    except ValueError:
        return None
    if eff_kind == "openai":
        return None
    label = _PURPOSE_LABELS.get(purpose, purpose)
    try:
        from sherpa import model_catalog
        model = model_catalog.resolve_model("openai", purpose, None, system_settings=sys_s)
        fallback = model_catalog.hardcoded_fallback("openai", purpose)
    except Exception:
        return (f"OpenAI 接続先の{label}用モデル設定を確認できませんでした"
                "（管理画面の「使えるモデル」を確認してください）")
    if not model or model == fallback:
        return (f"管理画面の「使えるモデル」で OpenAI に接続先（Azure 等）の{label}用デプロイ名を"
                f"登録してください（{fallback} のままでは送信できません）")
    return None


def _embed_static_check(provider: str, sys_s: dict) -> str | None:
    """embed 用途を**ネットワーク I/O なしで**静的に確認する。埋め込みは失敗してもベクトル検索が
    BM25 のみへ縮退するだけで会話自体は継続できる（モジュール冒頭の docstring・
    `hardcoded_fallback` 周辺の既存スコープ判断と同じ理由）ため、実接続の確認は行わない。

    (1) `model_catalog.resolve_model(provider, "embed", ...)` が例外を投げない・空文字列を
    返さないことを確認する（`system_settings` が壊れている・カタログの型が不正等を検出する）。
    (2) `provider == "openai"` かつ接続先が Azure/custom のときは、`_openai_azure_deployment_reason`
    （"chat" と同じ判定条件）で埋め込み用デプロイ名の登録有無も確認する。Gemini には Azure の
    ような別接続先の概念が無いため対象外。

    問題なければ（対象外の場合を含め）`None` を返す。呼び出し元（`check_cloud_llm_probes`）は、
    "embed" が `_consumed_llm_purposes` に含まれているときだけこの関数を呼ぶ。
    """
    try:
        from sherpa import model_catalog
        model = model_catalog.resolve_model(provider, "embed", None, system_settings=sys_s)
    except Exception:
        return "埋め込み用モデル設定を確認できませんでした（管理画面の「使えるモデル」を確認してください）"
    if not model:
        return "埋め込み用モデルが設定されていません（管理画面の「使えるモデル」を確認してください）"
    if provider != "openai":
        return None
    return _openai_azure_deployment_reason("embed", sys_s)


def _sanitized_sys_s_for_bedrock_probe(sys_s: dict) -> dict:
    """`health._ai_check_bedrock` へ渡す `system_settings` のスナップショットから、無効な中央
    Bedrock キー（プレースホルダ・空文字列）を取り除いた**コピー**を返す（`sys_s` 自体は
    書き換えない）。

    `_ai_check_bedrock` 自身は内部で `keys.resolve_api_key("bedrock", {}, system_settings=...)`
    により `bedrock_api_key` を独自に再解決するが、`resolve_api_key` は中央キーの値をそのまま
    返す契約で `is_real_api_key()` によるプレースホルダ除外を行わない（呼び出し側の責務）。
    プレースホルダのまま `BedrockProvider(..., api_key="sk-REPLACE_ME")` を組み立てると、
    Anthropic SDK は**明示キーが与えられていればそれを優先し**、有効な SigV4
    （`AWS_ACCESS_KEY_ID`／`AWS_PROFILE`／`~/.aws/credentials`）が実在していてもそちらへは
    進まない。これは doctor 自身の判定（`_central_auth_available` は `is_real_api_key()` で
    プレースホルダを弾いてから `_bedrock_auth_available` へ渡すため、SigV4 が有効なら `ok` と
    正しく判定する）と食い違い、SigV4 が正しく設定されている環境でも実際の送信はプレースホルダの
    まま行われてしまう（意味の無い認証情報での実送信・doctor の既定 `false OK` の一因）。
    ここで中央キーを事前に検証し、無効なら `None` へ差し替えることで、`_ai_check_bedrock` 内部の
    再解決結果を doctor 自身の判定と一致させる。中央キーは JSONB 由来で型を保証しないため、
    `is_real_api_key()` へ渡す前に `_as_key_str()` で非文字列を弾く（`is_real_api_key()` は
    `.strip()` を呼ぶ契約＝非文字列だと `AttributeError`）。
    """
    from sherpa import agent_constructs
    if agent_constructs.is_real_api_key(_as_key_str(sys_s.get("bedrock_api_key"))):
        return sys_s
    sanitized = dict(sys_s)
    sanitized["bedrock_api_key"] = None
    return sanitized


class _MissingApiKeyError(RuntimeError):
    """`_run_raw_llm_probe` が有効な実キーを解決できず、**何も送信せずに**打ち切ったことを示す
    合成例外（ネットワーク I/O は一切発生していない）。中央キー未設定のまま `Authorization:
    Bearer None` のような壊れたリクエストを実際に送ってしまうことを防ぐ送信前ガード。"""


# 例外クラス→固定ラベルの許可リスト（`type(e).__name__` をそのまま使わない＝クラス名は
# Python の識別子として自由に書けるため、動的生成されたクラスであれば理論上任意の文字列を
# 運びうる。ここに列挙した既知クラスだけを固定文字列へ写像し、それ以外は一律 "UnknownError"）。
_KNOWN_EXC_TYPE_LABELS: tuple[tuple[type, str], ...] = (
    (_MissingApiKeyError, "MissingApiKey"),
    (urllib.error.HTTPError, "HTTPError"),
    (socket.gaierror, "DNSError"),
    (TimeoutError, "TimeoutError"),
    (ConnectionRefusedError, "ConnectionRefusedError"),
    (ConnectionError, "ConnectionError"),
    (OSError, "OSError"),
    (RuntimeError, "RuntimeError"),
    (ValueError, "ValueError"),
)


def _exc_type_label(e: BaseException) -> str:
    """`e` の型を固定の許可リスト（`_KNOWN_EXC_TYPE_LABELS`）へ照合し、対応する固定ラベル文字列を
    返す（許可リスト外は一律 `"UnknownError"`）。"""
    labels = _KNOWN_EXC_TYPE_LABELS
    try:
        from anthropic import APIConnectionError as _AnthropicAPIConnectionError
        from anthropic import APIStatusError as _AnthropicAPIStatusError
        labels = ((_AnthropicAPIStatusError, "AnthropicAPIStatusError"),
                  (_AnthropicAPIConnectionError, "AnthropicAPIConnectionError")) + labels
    except Exception:
        pass   # SDK 未対応環境でも import 失敗で落とさない
    for cls, label in labels:
        try:
            if isinstance(e, cls):
                return label
        except Exception:
            continue
    return "UnknownError"


def _safe_http_status(candidate: object) -> int | None:
    """`candidate` が**厳密に** `int` 型（`bool` 等のサブクラス／文字列／`None` は除外）かつ
    HTTP ステータスとして妥当な範囲（100〜599）のときだけその値を返す。属性 getter が例外を
    投げる可能性にも備え、呼び出し側は本関数を必ず try で包む。"""
    if type(candidate) is int and 100 <= candidate <= 599:
        return candidate
    return None


def _classify_llm_probe_failure(e: BaseException) -> str:
    """LLM 最小プローブの失敗を、自由文（例外メッセージ・HTTP 応答本文）を一切含まない安全な
    短い分類へ丸める（fail-closed）。

    ここでは例外の**文字列表現には一切触れず**、型（`_exc_type_label`・固定の許可リストへの
    写像のみ）と構造化属性（HTTP ステータスコード等・`_safe_http_status` で厳密な `int` 型と
    妥当な範囲のみ受理）だけを見て分類する。分類ラベル自体も許可リスト（timeout／dns／auth／
    http_4xx／http_5xx／http_other／connection_refused／error）に限定する。

    属性アクセス（`.code`／`.status_code` 等）は動的なオブジェクト（`property` の getter が
    任意の例外を投げうる）に対して行うため、関数全体を try で包み、分類処理自体が失敗した場合は
    個々の分類を諦めて固定文字列 `"error"` を返す（`e` について一切の情報を信用できない状況でも
    確実に安全な値を返す）。
    """
    try:
        if isinstance(e, _MissingApiKeyError):
            return f"auth（{_exc_type_label(e)}）"
        status: int | None = None
        for c in (e, getattr(e, "__cause__", None), getattr(e, "__context__", None)):
            if c is None:
                continue
            if isinstance(c, urllib.error.HTTPError):
                status = _safe_http_status(c.code)
            else:
                status = _safe_http_status(getattr(c, "status_code", None))
            if status is not None:
                break
        if status is not None:
            if status in (401, 403):
                label = "auth"
            elif 400 <= status < 500:
                label = "http_4xx"
            elif 500 <= status < 600:
                label = "http_5xx"
            else:
                label = "http_other"
            return f"{label} status={status}（{_exc_type_label(e)}）"
        for c in (e, getattr(e, "__cause__", None), getattr(e, "__context__", None), getattr(e, "reason", None)):
            if c is None:
                continue
            if isinstance(c, (TimeoutError, socket.timeout)):
                return f"timeout（{_exc_type_label(e)}）"
            if isinstance(c, socket.gaierror):
                return f"dns（{_exc_type_label(e)}）"
            if isinstance(c, ConnectionRefusedError):
                return f"connection_refused（{_exc_type_label(e)}）"
        return f"error（{_exc_type_label(e)}）"
    except Exception:
        return "error"


def _run_raw_llm_probe(provider: str, sys_s: dict, *, purpose: str = "chat") -> BaseException | None:
    """`provider`（openai／gemini）へ最小リクエストを1回投げる。成功なら `None`、失敗なら
    **生の例外オブジェクト**を返す（文字列化しない＝呼び出し元は `_classify_llm_probe_failure` で
    型・構造化属性だけを見て分類する）。

    `graph_extract.complete_json()` を`_probe`／`_safe_detail` を経由せず**直接**呼ぶ（`_probe` は
    失敗時に理由を自由文へ変換してから返す設計のため、そちらを経由すると生の例外が手に入らない）。
    `complete_json` 自体は `health._ai_check_openai`／`_ai_check_gemini` が内部で使っているのと
    同じ本番関数＝挙動の重複実装ではない（cfg の組み立てだけをここで行う）。

    `purpose`（`model_catalog.resolve_model` の用途区分）: `check_cloud_llm_probes` は "chat"
    （既定）、`_check_codex_azure_compat` は Codex 用のデプロイ名解決が必要なため "codex" を渡す
    （`model_catalog.resolve_model("codex", "codex", ...)` と同じ組合せ・元の `_probe` 呼び出しの
    挙動を維持する）。

    bedrock は対象外（`sherpa.providers.bedrock.BedrockProvider.probe()` が `_safe_bedrock_detail`
    で理由を自由文化してから返す設計のため、同様に生の例外を取り出すには production 側の変更が
    要る。現状は `health._ai_check_bedrock` をそのまま使い、`RuntimeError`（構造化属性なし）として
    分類する＝HTTP ステータスは得られないが、自由文が出力に混ざることは無い）。

    キー解決・モデル解決・cfg 組み立て・実際の送信のすべてを**1つの `try` で包む**（準備処理が
    `try` の外側にあると、壊れた `system_settings`／`model_catalog` 等で例外が本関数の外まで
    伝播し、`run_all()` 全体を未捕捉の traceback で中断させる＝1項目の不具合が全体を巻き込む）。
    キー解決の結果が `agent_constructs.is_real_api_key()` を満たさない（未設定・プレースホルダ）
    場合は、`complete_json` を一切呼ばず（＝ネットワーク送信ゼロで）`_MissingApiKeyError` を返す
    （送信前ガード。ガード無しだと `Authorization: Bearer None` のような壊れたリクエストを
    実際に上流へ送ってしまう）。
    """
    try:
        from sherpa import agent_constructs, keys, model_catalog
        from sherpa.ingest.graph_extract import complete_json
        key = keys.resolve_api_key(provider, None, system_settings=sys_s)
        if not agent_constructs.is_real_api_key(_as_key_str(key)):
            return _MissingApiKeyError("no real API key resolved; probe not sent")
        if purpose == "codex":
            model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
        else:
            model = model_catalog.resolve_model(provider, purpose, None, system_settings=sys_s)
        cfg = {"provider": provider, "key": key, "model": model}
        if provider == "openai":
            cfg["openai_endpoint_override"] = sys_s   # Azure/custom 接続先も対象にする（_ai_check_openai と同じ）
        complete_json(_LLM_PROBE_SYSTEM, _LLM_PROBE_USER, cfg, timeout=_CODEX_TIMEOUT)
        return None
    except Exception as e:
        return e


def check_cloud_llm_probes(sys_s: dict | None, rows: list[dict] | None, probe_cloud: bool) -> list[CheckResult]:
    """選択中クラウドプロバイダについて、最小リクエストで実接続を確認する。

    中央キー（や Bedrock の SigV4）が無くても、個人キーの利用が許可されておりかつ有効な利用者の
    誰かが個人キーを保存済みなら、中央の認証情報だけで実プローブすると誤って NG になる（個人キー
    の値は一切読まないため、doctor 自身がそのキーで接続することはできない＝実プローブ不能）。
    その場合は接続を試みず、`selected_provider_key` と同じ判定で SKIP に収束させる。

    失敗理由は自由文（例外メッセージ）を一切出力しない＝fail-closed（`_classify_llm_probe_failure`
    参照）。

    中央認証が使えない（`_central_auth_available` が偽）と判定できたのに個人キー保有者も
    いない構成では、`_run_raw_llm_probe`（openai／gemini）は内部の送信前ガード
    （`is_real_api_key`）により実送信せず NG を返すが、Bedrock はここを経由せず
    `health._ai_check_bedrock` を直接呼ぶ。`health._ai_check_bedrock` 内部の認証ゲート
    （`sherpa.providers.bedrock._bedrock_auth_available`）は**単純な truthy 判定のみ**で
    プレースホルダ値（`sk-REPLACE_ME` 等）を弾かない契約（呼び出し元が事前に
    `is_real_api_key()` で弾いてから渡す前提の設計）だが、`_ai_check_bedrock` 自身はそのキーを
    弾かずにそのまま渡すため、プレースホルダのまま実際の SDK 送信（`messages.create()`）まで
    到達しうる。ここで既に「中央認証は使えない・個人キーも無い」と判定済みなので、Bedrock は
    `health._ai_check_bedrock` を呼ばず直接 NG にする（fail-closed・送信ゼロ）。

    `selected`（A7 の `cloud_provider` 選択）が現在の構成で**実際に消費されていない**場合
    （`_consumed_llm_purposes` が空・例: 全員の実効頭脳が `ollama` で `cloud_provider` に
    `openai` が既定値のまま残っているだけ）は、`PROBE_CLOUD=1` でも実プローブを試みず SKIP に
    する（`check_selected_provider_key` と同じ判定・誰も使わない構成へ課金を伴う実送信をする
    実害を避ける）。

    `sys_s` が `None`（system_settings 読み取り不能、または呼び出し元が接続先設定（`openai_
    endpoint_kind`／`openai_base_url`）を検証できなかったため意図的に渡された `None`）の場合は
    無条件で全プロバイダを SKIP にする（送信ゼロ）＝`check_openai_endpoint` の判定が確定するまで
    実際のキー・接続先を使った送信を一切行わない。

    openai／gemini は chat とは独立の第2消費経路（intent／render／embed・
    `_second_path_purposes` 参照）を持つため、実際に消費されている用途**ごと**に検査する。
    "chat" 用途は `_agent_actually_used("openai", ...)`（狭義＝実効頭脳が直接 openai であること）
    で対象を絞り、静的なデプロイ名検査・実プローブの両方に**同じ**絞り込みを適用する
    （Codex(Azure/custom) が間接的に openai の認証情報を使う構成（`_chat_or_codex_consumes` が
    "chat" を広義に含める理由の1つ）は Codex 自身の別チェック（`_check_codex_azure_compat`）の
    対象であり、ここで二重に検査・二重に実送信しない）。"intent"／"render" は静的なデプロイ名
    検査の対象外（`_openai_azure_deployment_reason` docstring 参照・実行時ガードが存在しない）
    だが、実プローブは通常どおり行う。"embed" は実プローブを持たず（`_embed_static_check` による
    静的検査のみ）、静的検査に通れば `PROBE_CLOUD`／認証可否に関わらず `ok` にする（そもそも
    ネットワーク送信が発生しない用途のため、課金ゲートの対象にしない）。

    選択中プロバイダについて `_agent_resolution_indeterminate()` が真（`effective_agent()` が
    例外を投げる＝実効頭脳が判定不能）なら、通常判定（`_consumed_llm_purposes` 等）へは進まず
    本項目自身を固定文言の NG にする（`check_selected_provider_key` と同じ理由・fail-closed な
    真偽値だけに頼ると判定不能だったこと自体が握り潰される）。
    """
    from sherpa import health, keys
    out = []
    selected = keys.selected_cloud_provider(sys_s) if sys_s is not None else None
    for provider in _CLOUD_LLM_CHECKS:
        cid, label = f"llm_{provider}", _CLOUD_LLM_LABELS[provider]
        if sys_s is None:
            out.append(CheckResult(cid, label, "skip",
                                    "system_settings を読み取れないため確認できません"))
            continue
        if selected != provider:
            out.append(CheckResult(cid, label, "skip", "選択中のクラウドプロバイダではありません（cloud_provider）"))
            continue
        if _agent_resolution_indeterminate(sys_s, rows):
            out.append(CheckResult(cid, label, "ng", _AGENT_RESOLUTION_FAILED_DETAIL))
            continue
        purposes = _consumed_llm_purposes(provider, sys_s, rows)
        openai_chat_direct = (provider == "openai" and "chat" in purposes
                              and _agent_actually_used("openai", sys_s, rows))
        # 実プローブ（ネットワーク送信）の対象用途。"chat" は狭義（直接消費）のときだけ含める
        # （静的検査と同じ絞り込み・docstring 参照）。"embed" は実プローブを持たない（常に除外）。
        probe_purposes = [p for p in purposes if p not in ("chat", "embed")]
        if openai_chat_direct or (provider != "openai" and "chat" in purposes):
            probe_purposes.append("chat")

        static_ng_reason = None
        if "embed" in purposes:
            static_ng_reason = _embed_static_check(provider, sys_s)
        if static_ng_reason is None and provider == "openai" and openai_chat_direct:
            static_ng_reason = _openai_azure_deployment_reason("chat", sys_s)
        if static_ng_reason is not None:
            # ネットワーク I/O を伴わない静的検査＝PROBE_CLOUD ゲート・「未使用」判定の手前で
            # 判定する（`_openai_azure_deployment_reason`／`_embed_static_check` docstring 参照）。
            out.append(CheckResult(cid, label, "ng", static_ng_reason))
            continue

        if purposes and all(p == "embed" for p in purposes):
            # 消費されている用途が "embed" だけ＝実プローブ（ネットワーク送信）は一切発生しない
            # （静的検査は既に上で通過済み）。課金ゲート（`PROBE_CLOUD`）の対象にもしない。
            out.append(CheckResult(cid, label, "ok", "設定を静的に確認しました（実接続は行いません）"))
            continue

        if not probe_cloud:
            out.append(CheckResult(cid, label, "skip",
                                    "課金される可能性があるため既定でスキップしました"
                                    "（make doctor PROBE_CLOUD=1 で実行）"))
            continue
        if not purposes:
            out.append(CheckResult(cid, label, "skip",
                                    f"選択中のクラウドプロバイダ（{provider}）は現在の構成では"
                                    "使われていません（既定値が残っているだけの可能性があります）"))
            continue
        if not _central_auth_available(provider, sys_s):
            n = _personal_key_holder_count(provider, sys_s, rows)
            if n:
                out.append(CheckResult(cid, label, "skip",
                                        f"個人キー利用中の利用者が {n} 人います（値は確認しないため"
                                        "doctor からは実接続できません）・中央検査は対象外です"))
                continue
            if provider == "bedrock":
                e = _MissingApiKeyError("no bedrock auth available (no real central key, no SigV4 hints)")
                out.append(CheckResult(cid, label, "ng", f"接続に失敗しました: {_classify_llm_probe_failure(e)}"))
                continue
        if provider == "bedrock":
            # システム既定モデルのみ疎通確認する（利用者別 `bedrock_model` 上書き・個人キーは
            # doctor では検査しない＝値を読まない設計上、doctor 自身ではその利用者の構成を
            # 再現できないため。該当する利用者は各自の設定画面の接続テストで確認する）。
            # `max_retries=0`: SDK 既定のリトライ（既定2回・実 HTTP 送信が最大3回になりうる）を
            # 無効化し、「1回だけ最小リクエストを送る」という本チェックの前提と実際の送信回数を
            # 一致させる。
            try:
                health._ai_check_bedrock({}, _sanitized_sys_s_for_bedrock_probe(sys_s), max_retries=0)
            except Exception as e:
                out.append(CheckResult(cid, label, "ng", f"接続に失敗しました: {_classify_llm_probe_failure(e)}"))
            else:
                out.append(CheckResult(cid, label, "ok",
                            "最小リクエストで疎通確認できました"
                            "（利用者別モデル・個人キーは対象外＝各自の設定画面の接続テストで確認してください）"))
            continue
        if not probe_purposes:
            # ここに到達する時点で purposes は非空（"not purposes" は上で弾き済み）だが、"chat" が
            # Codex 経由の間接消費だけで、この構成では実プローブ対象が無い（Codex 自身の接続確認は
            # `_check_codex_azure_compat` が別途担当・二重に送信しない）。
            out.append(CheckResult(cid, label, "ok",
                        "この用途は Codex 経由でのみ間接的に消費されています"
                        "（実接続の確認は「Codex 認証・設定の整合」の項目を参照してください）"))
            continue
        failure = None
        for p in probe_purposes:
            e = _run_raw_llm_probe(provider, sys_s, purpose=p)
            if e is not None:
                failure = e
                break
        if failure is not None:
            out.append(CheckResult(cid, label, "ng", f"接続に失敗しました: {_classify_llm_probe_failure(failure)}"))
        else:
            out.append(CheckResult(cid, label, "ok", "最小リクエストで疎通確認できました"))
    return out


def _codex_required(sys_s: dict | None, rows: list[dict] | None) -> tuple[bool, bool, str]:
    """Codex CLI が現在の構成で**必須**かどうか、かつ OpenAI/Azure 側の認証確認
    （`codex login status`／`_codex_openai_compat_block_reason`）が必要かどうかを判定する。

    `agent`／`codex_model_provider` は per-user 設定（`user_settings`）であり、システム既定
    （未設定利用者が使う経路・`agent_constructs.effective_agent(None, ...)`）だけを見ると実際に
    構成済みの利用者を見落とす。ここではシステム既定に加えて、**有効な**（`users.status='active'`）
    利用者の保存設定を走査し、それぞれの実効頭脳が `codex` かどうかと、そのときの
    `codex_model_provider`（実効評価・`agent_constructs.codex_model_provider()`）を集める
    （個々の `user_id` は見ない・出さない＝件数のみ扱う）。

    戻り値 `(required, needs_openai_auth, note)`:
      - `required`: Codex を実効的に使っている構成（システム既定／有効な利用者）が1つでもあるか。
      - `needs_openai_auth`: そのうち `codex_model_provider` が `"ollama"` でないものが1つでも
        あるか。全て `"ollama"` 構成（Codex(Ollama) 利用者のみ）なら偽＝OpenAI/Azure 側の認証は
        誰も必要としていない（Ollama 側の疎通は `llm_ollama` 系の項目が別途担う・Codex(Ollama)
        利用者に OpenAI の認証情報を要求しない）。

    読み取れない場合は fail-closed（両方とも必須扱い）にする。`effective_agent()` がシステム既定・
    利用者行いずれかで例外を投げた場合も同様に fail-closed にする（判定不能を「codex を使って
    いない」に丸めると、`needs_openai_auth` が偽になり `check_codex` が委譲先の認証確認
    （`_check_codex_auth`）自体を SKIP してしまう）。
    """
    from sherpa import agent_constructs
    if sys_s is None or rows is None:
        return True, True, "（構成を読み取れないため安全側で必須扱い）"
    _INDETERMINATE_NOTE = "（設定解決が例外を投げたため安全側で必須扱い）"
    backing_kinds: set[str] = set()
    try:
        if agent_constructs.effective_agent(None, system_settings=sys_s) == "codex":
            backing_kinds.add(agent_constructs.codex_model_provider(None))
    except Exception:
        return True, True, _INDETERMINATE_NOTE
    for row in rows:
        settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider")}
        try:
            if agent_constructs.effective_agent(settings, system_settings=sys_s) == "codex":
                backing_kinds.add(agent_constructs.codex_model_provider(settings))
        except Exception:
            return True, True, _INDETERMINATE_NOTE
    required = bool(backing_kinds)
    needs_openai_auth = bool(backing_kinds - {"ollama"})
    return required, needs_openai_auth, f"（有効な利用者設定 {len(rows)} 件を確認）"


def _resolve_ollama_usages(sys_s: dict | None, rows: list[dict] | None) -> list[dict] | None:
    """Ollama が実際に必要とされる用途を (URL, モデル, 用途) 単位で列挙する。

    中央既定 URL への `/api/tags` 疎通だけでは、個人の `ollama_url` 上書き・検索ヘルパー
    （`user_settings.search_helper == "ollama"`）経由の利用を見落とし、false OK（別の URL/モデルが
    壊れているのに見えない）と false NG（使っていない中央既定が単に未起動なだけ）の両方を招く。
    システム既定と有効な利用者全員の保存設定の両方から、実際に使われる (URL, モデル) の組を集める
    （`sherpa.keys.resolve_ollama_url`／`sherpa.search_helper.resolve` を再利用し、解決ロジックを
    再実装しない）。

    戻り値: 判定できない（`sys_s`／`rows` 不明・`model_catalog` 解決が壊れた設定で例外を投げた・
    `ollama_url` が JSONB 由来の非文字列値等）場合は `None`（呼び出し元は安全側で `ng` にする）。
    判定できて実際にはどこからも使われていなければ空リスト。

    `url`／`model` は `(url, model)` のタプルを辞書キーとして使う（`usages` の集約）ため、
    非文字列（`dict`／`list` 等・ハッシュ不能）が紛れ込むと `TypeError` で本関数の外まで
    伝播しかねない。`_add()` を唯一の登録経路にし、そこで型を検証する（文字列以外は
    `type_error` フラグを立てて無視＝「未設定」と同じ黙殺ではなく、最終的に関数全体の
    戻り値を `None` へ倒す＝他の「判定できない」失敗と同じ契約に揃える）。

    システム既定・利用者行いずれの `effective_agent()`／`resolve_ollama_url()` も、例外を
    「その用途は使われていない」（黙って追加しない）へ丸めず `type_error` を立てる（判定不能→
    最終的に関数全体を `None` へ倒す）。判定できないことを「使っていない」と区別できないまま
    黙殺すると、設定解決が壊れているだけの環境が「Ollama は未使用（任意構成）」という無関係な
    SKIP に化けてしまう（`check_ollama_probes` の fail-closed 契約と食い違う）。
    """
    if sys_s is None or rows is None:
        return None
    from sherpa import agent_constructs, keys, model_catalog, search_helper
    usages: dict[tuple[str, str], set[str]] = {}
    type_error = False

    def _add(url: object, model: object, purpose: str) -> None:
        nonlocal type_error
        if not url or not model:
            return
        if not isinstance(url, str) or not isinstance(model, str):
            type_error = True
            return
        usages.setdefault((url, model), set()).add(purpose)

    try:
        chat_model = model_catalog.resolve_model("ollama", "chat", None, system_settings=sys_s)
        codex_model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
    except Exception:
        # 壊れた `system_settings`（型不正な JSONB 値等）で `model_catalog` 側が例外を投げても、
        # `run_all()` 全体を未捕捉の traceback で巻き込まない＝この関数自身の「判定できない」
        # 契約（`None`）にそのまま乗せる。
        return None

    try:
        if agent_constructs.effective_agent(None, system_settings=sys_s) == "ollama":
            _add(keys.resolve_ollama_url(None, system_settings=sys_s), chat_model, "システム既定（チャット）")
    except Exception:
        # システム既定が実際に ollama を使うか自体を判定できない＝「使っていない」に丸めず
        # 判定不能（`type_error`）にする（本 docstring 末尾の段落参照）。
        type_error = True

    for row in rows:
        row_settings = {"agent": row.get("agent"), "codex_model_provider": row.get("codex_model_provider"),
                        "ollama_url": row.get("ollama_url"), "search_helper": row.get("search_helper")}
        try:
            url = keys.resolve_ollama_url(row_settings, system_settings=sys_s)
        except Exception:
            type_error = True
            url = None
        try:
            eff = agent_constructs.effective_agent(row_settings, system_settings=sys_s)
        except Exception:
            # この利用者行が実際に ollama／Codex(Ollama) を使うか自体を判定できない＝「使っていない」
            # に丸めず判定不能（`type_error`）にする（本 docstring 末尾の段落参照）。
            type_error = True
            eff = None
        if eff == "ollama":
            _add(url, chat_model, "チャット（利用者設定）")
        elif eff == "codex" and row.get("codex_model_provider") == "ollama":
            _add(url, codex_model, "Codex(Ollama) 実行モデル")
        # 検索ヘルパーは主頭脳が openai（`provider_id == "openai"`）のときだけ実際に配線される
        # （`sherpa/providers/__init__.py::get_provider` 参照）。主頭脳が codex/ollama 等の利用者の
        # `search_helper` 列は runtime では一切評価されないため、ここでも `eff == "openai"` の
        # ときだけ解決する（残存設定を誤って「使っている」扱いにしない）。
        if eff == "openai":
            try:
                sh = search_helper.resolve(row_settings, system_settings=sys_s)
            except Exception:
                # `search_helper.resolve()` は本番では例外を捕捉しない（呼び出し元が壊れた設定を
                # そのまま検出する契約）。ここで「検索ヘルパーは使っていない」に丸めず判定不能
                # （`type_error`）にする（本関数の docstring 末尾の段落と同じ契約）。
                type_error = True
                sh = None
            if sh and sh.get("provider") == "ollama":
                _add(sh.get("url"), sh.get("model"), "検索ヘルパー（下調べ）")

    if type_error:
        return None
    return [{"url": u, "model": m, "purposes": sorted(p)} for (u, m), p in usages.items()]


_OLLAMA_DEFAULT_HOST = "registry.ollama.ai"
_OLLAMA_DEFAULT_NAMESPACE = "library"
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")

# Ollama 公式 grammar（`types/model/name.go`・v0.31.2 時点）に準拠:
#   - いずれの part（host／namespace／model／tag）も**先頭文字は英数字または `_` のみ**
#     （先頭の `-` は不可・host／model／tag は先頭の `.` も不可＝`.`／`-` は2文字目以降でのみ
#     許可）。内部/末尾は下記のとおり広く許可する。
#   - host は英数字／`_`／`.`／`-`／`:`（先頭以外は `:` も許可・IPv6 表記等を含め特定の
#     ポート番号形式に限定しない＝数字 port 限定にしない）。
#   - namespace は英数字／`_`／`-` のみ（`.` は不可）。
#   - model／tag は英数字／`_`／`.`／`-`。
#   - `/`・空白等はいずれの部位にも含められない。
#   - 長さ上限: host 350 文字・namespace／model／tag 各80文字。
# いずれも `fullmatch()` で照合する（`match()` ＋ 末尾 `$` は対象文字列の**末尾に改行が1つ
# 付いていても**マッチしてしまう＝`$` は「文字列末尾」だけでなく「末尾の改行の直前」にも
# マッチする Python 正規表現の仕様。`fullmatch()` は文字列全体の消費を要求するため、
# 末尾に改行が紛れ込んだ値を正しく拒否する）。
_OLLAMA_HOST_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
_OLLAMA_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_OLLAMA_MODEL_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_OLLAMA_HOST_MAX_LEN = 350
_OLLAMA_NAME_PART_MAX_LEN = 80   # namespace／model／tag


def _normalize_ollama_ref(ref: str) -> tuple[str, str, str, str] | None:
    """モデル参照を Ollama 流の正規形 `(host, namespace, model, tag)`（すべて小文字）へ分解する。
    戻り値 `None` は「不正な参照・何とも一致しようがない」ことを示す。

    形式は `[scheme://]host[:port]/namespace/model[:tag]`（scheme 付きは常に3要素フル修飾のみ）
    または scheme 無しの `[[host[:port]/]namespace/]model[:tag]`。先頭の `scheme://`
    （`http://`／`https://` 等）は接続先の記法として許容し、取り除いてから残りを解析する
    （Ollama は OCI 互換レジストリからの取得を scheme 付き参照でも受け付ける・scheme を理由に
    完全修飾な参照全体を不正扱いにしない）。scheme を取り除いた後に `://` がまだ残る場合は不正。

    **scheme 検出時は host/namespace/model の3要素そろっていない参照を不正として拒否する**
    （公式 parser 準拠）。`http://qwen2.5`（0要素）や `https://library/qwen2.5`（2要素）を
    scheme 無しの短縮形と同一視し「既定 host・既定/指定 namespace の省略」として受理すると、
    実行時に Codex/Ollama クライアントへ渡るのは正規化前の**生の参照文字列そのもの**（doctor は
    表示・比較用の正規化しか行わず、実際に送信される値はここでは書き換えない）であり、doctor が
    「一致」と判定しても実際には解決できない参照を「モデルを確認できました」と誤って OK にする
    false-green を生む。

    タグの区切りは**最後の `/` より後**にある最後の `:`（レジストリのポート番号 `host:port/...`
    をタグ区切りと誤認しない）。タグ省略時は Ollama 自身が `:latest` を補って解決する契約のため
    こちらも補う。明示的な `:` の**直後にタグが無い**参照（例 `"qwen2.5:"`）は、暗黙の `:latest`
    補完とは別物の不正な参照として扱う（Ollama 自身もこの欠落パートを解決できない）。

    host/namespace/model の判定は Ollama 自身の grammar と同じ**完全に位置ベース**の規則
    （スラッシュの個数だけで決まる・先頭要素がドメインやポートを含む見た目でも host とは
    見なさない）:
      - スラッシュ0個: model のみ（host・namespace は既定値）。
      - スラッシュ1個: `namespace/model`（host は既定値）。**この形では host を指定できない**
        （取り違えると `registry.ollama.ai/qwen2.5` のような2要素参照を「既定 host ＋既定
        namespace＝裸の qwen2.5」と誤って同一視してしまう＝実際には namespace が
        `"registry.ollama.ai"` という別のモデルを指す）。
      - スラッシュ2個: `host[:port]/namespace/model`。**custom host を指定するには必ず
        3要素そろえる**必要がある。
      - それ以外（3個以上）: 不正。
    既定 host（`registry.ollama.ai`）・既定 namespace（`library`）は、省略時だけでなく明示指定時も
    正規化後の値が既定値と一致すれば自然に同一の正規形（小文字化した文字列そのもの）へ畳み込まれる
    ため、追加の特別扱いは不要。

    **要素数がそろっていても文字種・長さが不正なら受理しない**（`_OLLAMA_HOST_RE`／
    `_OLLAMA_NAMESPACE_RE`／`_OLLAMA_MODEL_TAG_RE`／`_OLLAMA_HOST_MAX_LEN`／
    `_OLLAMA_NAME_PART_MAX_LEN` 参照）。位置（スラッシュの個数）だけを見て要素数の解釈を決める
    設計のため、例えば `registry.example.com:5000/qwen2.5`（スラッシュ1個）は「namespace が
    `registry.example.com:5000`」と解釈されるが、`:` は namespace の構成文字として無効（host の
    ポート区切りとしてのみ有効・かつ3要素形でしか host を指定できない）。namespace はさらに `.`
    も無効（model／tag とは異なる文字集合）。ここで弾かずに受理すると、実際には解決不能な参照を
    「見つからない＝pull すれば取得できる」と誤案内してしまう（`_probe_ollama_usage` は `None` を
    「不正な参照です」と正しく案内する分岐と、見つからない場合の「pull してください」という分岐を
    明確に分けているため、この判定漏れは後者の誤った案内に直結する）。
    """
    ref = str(ref)
    m = _URL_SCHEME_RE.match(ref)
    had_scheme = bool(m)
    if m:
        ref = ref[m.end():]
    if "://" in ref:
        return None
    slash_idx = ref.rfind("/")
    head = ref[:slash_idx] if slash_idx >= 0 else ""
    tail = ref[slash_idx + 1:] if slash_idx >= 0 else ref
    if ":" in tail:
        repo_tail, tag = tail.rsplit(":", 1)
        if not tag:
            return None
    else:
        repo_tail, tag = tail, "latest"
    if not repo_tail:
        return None
    parts = (head.split("/") if head else []) + [repo_tail]
    if had_scheme and len(parts) != 3:
        return None
    if len(parts) == 1:
        host, namespace, model = _OLLAMA_DEFAULT_HOST, _OLLAMA_DEFAULT_NAMESPACE, parts[0]
    elif len(parts) == 2:
        host, namespace, model = _OLLAMA_DEFAULT_HOST, parts[0], parts[1]
    elif len(parts) == 3:
        host, namespace, model = parts[0], parts[1], parts[2]
    else:
        return None
    if not host or not namespace or not model:
        return None
    for part, max_len, pattern in (
        (host, _OLLAMA_HOST_MAX_LEN, _OLLAMA_HOST_RE),
        (namespace, _OLLAMA_NAME_PART_MAX_LEN, _OLLAMA_NAMESPACE_RE),
        (model, _OLLAMA_NAME_PART_MAX_LEN, _OLLAMA_MODEL_TAG_RE),
        (tag, _OLLAMA_NAME_PART_MAX_LEN, _OLLAMA_MODEL_TAG_RE),
    ):
        if len(part) > max_len or not pattern.fullmatch(part):
            return None
    return host.lower(), namespace.lower(), model.lower(), tag.lower()


def _ollama_ref_display(parsed: tuple[str, str, str, str]) -> str:
    """`_normalize_ollama_ref` の戻り値から、既定値を省いた最短表記の表示文字列を組み立てる
    （利用者が入力した表記に近い見た目で案内する・内部の比較は常にフル修飾の4要素で行う）。"""
    host, namespace, model, tag = parsed
    if host == _OLLAMA_DEFAULT_HOST.lower() and namespace == _OLLAMA_DEFAULT_NAMESPACE.lower():
        repo = model
    elif host == _OLLAMA_DEFAULT_HOST.lower():
        repo = f"{namespace}/{model}"
    else:
        repo = f"{host}/{namespace}/{model}"
    return f"{repo}:{tag}"


def _probe_ollama_usage(url: str, model: str, sys_s: dict | None) -> tuple[bool, str]:
    """1つの (url, model) について疎通＋必要モデルの有無を確認する。表示する接続先は host[:port]
    のみ（`sherpa.llm._redact_url_for_error` と同じ縮約＝匿名化表示・userinfo/path/query は出さない）。

    `sys_s`（読み取り専用 SELECT で既に取得済みの system_settings）を `llm.ollama_url()` へ明示的に
    渡す。省略すると `assert_ollama_url_allowed()`（`ollama_url()` が内部で呼ぶ SSRF 許可判定）が
    `sherpa.store.get_system_settings()`（未初期化なら `_ensure()`→`init_schema()` を実行する高水準
    API）を自分で読みに行き、doctor の読み取り専用契約（DDL を発火させない）に違反する。

    接続失敗・応答解析失敗のいずれも、生の例外文字列をそのまま埋め込まない
    （`SsrfBlocked`／`ValueError` 等は不正な URL 自体を repr で含みうる＝パターンベースの秘密
    マスクでは検出できない値を再連結してしまう恐れがあるため、`health._classify()` と同じ安全な
    分類ラベルだけを使う）。応答が想定外の形（JSON でない・`models` が配列でない等）でも例外を
    外へ漏らさず、必ず `(False, ...)` を返す。`url` が想定外の型（設定破損等）でも表示用の解析で
    落ちないよう try で囲む。
    """
    from sherpa import health, llm
    try:
        display = llm._redact_url_for_error(url) or "（解析できません）"
    except Exception:
        display = "（解析できません）"
    try:
        with llm.urlopen_no_redirect(llm.ollama_url(url, "/api/tags", system_settings=sys_s),
                                     timeout=_ES_TIMEOUT) as r:
            data = json.loads(r.read())
        if not isinstance(data, dict):
            raise ValueError("Ollama のタグ一覧応答が予期しない形式です（JSON オブジェクトではありません）")
        raw_models = data.get("models") if data.get("models") is not None else []
        if not isinstance(raw_models, list):
            raise ValueError("Ollama のタグ一覧応答が予期しない形式です（models が配列ではありません）")
        names: set[str] = set()
        for entry in raw_models:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
    except Exception as e:
        return False, f"{display} に接続できません: {health._classify(e)}（{type(e).__name__}）"

    configured = _normalize_ollama_ref(model)
    if configured is None:
        return False, f"{display}: モデル参照 {model!r} が不正です（管理画面の設定を確認してください）"
    display_ref = _ollama_ref_display(configured)
    same_repo_tags: list[str] = []
    for n in names:
        parsed = _normalize_ollama_ref(n)
        if parsed is None:
            continue   # Ollama 応答側の不正な参照（通常は起きない）は安全側で無視する
        if parsed[:3] != configured[:3]:   # host/namespace/model の一致（タグ抜き）
            continue
        if parsed[3] == configured[3]:
            return True, f"{display}: モデル {display_ref} を確認できました"
        same_repo_tags.append(_ollama_ref_display(parsed))
    if same_repo_tags:
        return False, (f"{display}: モデル {display_ref} は見つかりませんが、モデル名一致・タグ違いが"
                       f"あります（{', '.join(sorted(same_repo_tags))}）。ollama pull {model} で "
                       f"{display_ref} を取得してください")
    return False, f"{display}: モデル {display_ref} が見つかりません（ollama pull {model} が必要な可能性）"


def check_ollama_probes(sys_s: dict | None, rows: list[dict] | None) -> list[CheckResult]:
    """Ollama はローカル接続でありクラウド排他（A7）の対象外・課金も発生しないため既定で実施する。

    用途（`_resolve_ollama_usages`）が1つも見つからなければ、`docker-compose.yml` の Ollama
    サービスが既定でコメントアウトされていることと整合する任意構成として `skip` にする。用途を
    判定できない場合（設定を読めない）は、未導入と紛れないよう `ng` にする（fail-closed）。
    """
    cid_base, label_base = "llm_ollama", "ローカルLLM（Ollama）"
    usages = _resolve_ollama_usages(sys_s, rows)
    if usages is None:
        return [CheckResult(cid_base, f"{label_base} 最小プローブ", "ng",
                            "構成を読み取れないため Ollama の要否を確認できません（安全側で失敗扱い）")]
    if not usages:
        return [CheckResult(cid_base, f"{label_base} 最小プローブ", "skip",
                            "現在の構成では使われていません（任意構成のため問題ありません）")]
    out = []
    multiple = len(usages) > 1
    for i, usage in enumerate(usages, start=1):
        ok, detail = _probe_ollama_usage(usage["url"], usage["model"], sys_s)
        purpose = "・".join(usage["purposes"])
        cid = f"{cid_base}_{i}" if multiple else cid_base
        out.append(CheckResult(cid, f"{label_base}（{purpose}）", "ok" if ok else "ng", detail))
    return out


# ---------------------------------------------------------------------------
# 4. Codex 経路
# ---------------------------------------------------------------------------


# `_codex_openai_compat_block_reason` の「キー実在」チェックだけを強制的に通すためのダミー値。
# サンドボックス／URL／デプロイ名の各検査はこの値の中身に依存しない（`is_real_api_key()` に通る
# 非空文字列でありさえすればよい）。ここでの呼び出しは常に `system_settings=sys_s` を明示するため
# `store.get_system_settings()` への読み直しは発生しない（`system_settings` 省略時は自分で読みに
# 行く契約＝関数自体が無条件に I/O なしとは限らない）。この値が実際の接続・保存・監査ログの
# いずれにも使われることはない。
_CODEX_KEY_PRESENCE_PROBE = "sk-doctor-key-presence-probe-only-never-sent-anywhere"


@_guarded_check("codex_auth", "Codex 認証・設定の整合")
def _check_codex_azure_compat(sys_s: dict, rows: list[dict] | None, required: bool,
                               probe_cloud: bool) -> CheckResult:
    """Codex(Azure/custom) の認証・設定を確認する。

    `_codex_openai_compat_block_reason` は判定順（サンドボックス有効性→base URL 妥当性→実キー→
    デプロイ名）で**早い者勝ち**に理由を返す。個人キーの値は読まない設計のため、中央 openai キーが
    無い構成では「キー未設定」を理由に返すが、A6（個人 API キー許可）が真で有効な利用者の誰かが
    個人キーを保存済みなら、その構成は実際には動く（doctor からは値を確認できないだけ）。

    早い者勝ちの判定順のため、中央キーが欠けている構成では「キー未設定」以外の不備
    （サンドボックス無効・URL 不正・デプロイ名未設定）が**同時に**存在していても、`reason` には
    先に見つかった「キー未設定」しか現れない。個人キー救済をこの `reason` の文字列一致だけで
    判定すると、後段の不備を検出できないまま SKIP にしてしまう。これを避けるため、`explicit_
    openai_api_key`（本来は接続テスト画面が未保存のキーを試すための差し替え口）へダミーの実在
    キーを渡して**キー実在チェックだけを強制的に通した状態**で同じ判定をもう一度呼び、それでも
    理由が返るなら（＝キー以外の不備が実在する）そちらを優先して返す。ロジックを重複実装せず、
    共有 helper 自身に「キー以外は本当に大丈夫か」を問い合わせる形にする。
    """
    cid, label = "codex_auth", "Codex 認証・設定の整合"
    from sherpa.providers import _codex_openai_compat_block_reason
    reason = _codex_openai_compat_block_reason({}, system_settings=sys_s)
    if reason is not None:
        non_key_reason = _codex_openai_compat_block_reason(
            {}, explicit_openai_api_key=_CODEX_KEY_PRESENCE_PROBE, system_settings=sys_s)
        if non_key_reason is not None:
            # キーの有無に関わらず残る不備＝個人キー運用でも解消しない実在の設定不備。
            return CheckResult(cid, label, "ng" if required else "skip", non_key_reason)
        n = _personal_key_holder_count("openai", sys_s, rows)
        if n:
            return CheckResult(cid, label, "skip",
                                f"個人キー利用中の利用者が {n} 人います（値は確認しないため"
                                "doctor からは実接続できません）・中央検査は対象外です")
        return CheckResult(cid, label, "ng" if required else "skip", reason)
    if not probe_cloud:
        return CheckResult(cid, label, "skip",
                            "設定形式は妥当です（実接続は課金の可能性があるため既定スキップ・"
                            "make doctor PROBE_CLOUD=1 で確認）")
    e = _run_raw_llm_probe("openai", sys_s, purpose="codex")
    if e is not None:
        return CheckResult(cid, label, "ng", f"接続に失敗しました: {_classify_llm_probe_failure(e)}")
    return CheckResult(cid, label, "ok", "接続OK")


def _check_codex_auth(sys_s: dict, rows: list[dict] | None, required: bool, note: str,
                       probe_cloud: bool) -> CheckResult:
    """Codex の OpenAI/Azure 側の認証・接続先の整合を、実行時契約と同じ判定部品で確認する。

    呼び出し元（`check_codex`）が `_codex_required` の `needs_openai_auth` を見て、Codex を
    使っている構成が全て Ollama backing（`codex_model_provider == "ollama"`）なら、この関数自体を
    呼ばない（OpenAI/Azure の認証情報を持たない Codex(Ollama) 専用の利用者に、無関係な認証確認を
    要求しない・Ollama 側の疎通は `llm_ollama` 系の項目が別途担う）。`sys_s` が `None`（接続先が
    未確定・system_settings 読み取り不能）の場合も呼ばない（`check_codex` が事前に SKIP へ倒す）。

    決定木は `POST /settings/test`（`sherpa/routers/system.py::settings_test`）の Codex 分岐、
    および実際にプロバイダを組み立てる `sherpa/providers/__init__.py::_select_provider` の Codex
    分岐と同じ（重複実装しない・実行時契約と食い違わせない）:
      - 接続先種別（`llm.openai_endpoint_kind`）が openai（既定）なら `codex login status`
        （auth.json の有無・有効性）で確認する。
      - それ以外（Azure/custom）は `sherpa.providers._codex_openai_compat_block_reason`
        （サンドボックス・キー・base URL・デプロイ名の形式確認）で判定する。実際に1回だけ
        最小リクエストする実接続は課金の可能性があるため `PROBE_CLOUD` ゲートに従う。
    """
    cid, label = "codex_auth", "Codex 認証・設定の整合"
    try:
        from sherpa import llm
        kind = llm.openai_endpoint_kind(sys_s)
    except ValueError:
        return CheckResult(cid, label, "ng" if required else "skip",
                            "接続先の設定が不正です（管理画面で確認してください）")
    if kind != "openai":
        return _check_codex_azure_compat(sys_s, rows, required, probe_cloud)

    from sherpa import health
    failure_status = "ng" if required else "skip"
    try:
        health._ai_check_codex({}, {})
    except Exception as e:
        detail = (str(e) if required else
                  f"未ログインの可能性があります（現在の構成では Codex を使わないため問題ありません{note}）: {e}")
        return CheckResult(cid, label, failure_status, detail)
    return CheckResult(cid, label, "ok", "ログイン済み、または鍵で接続できます")


def check_codex(sys_s: dict | None, rows: list[dict] | None, required: bool, needs_openai_auth: bool,
                 note: str, probe_cloud: bool, *, indeterminate: bool = False) -> list[CheckResult]:
    """CLI 存在・バージョン・認証/設定の整合（実行はドライラン級＝`codex --version`／
    `codex login status`／設定形式確認のみ・重い `codex exec` は起動しない）。

    `required`（`_codex_required` 参照）が偽の間、CLI 未導入／バージョン取得失敗は失敗ではなく
    `skip`（任意構成のため）。`needs_openai_auth` が偽（Codex を使う構成が全て Ollama backing）
    の間は、OpenAI/Azure 側の認証確認（`codex_auth`）自体を行わず `skip` にする
    （Codex(Ollama) 利用者に OpenAI の認証情報を要求しない）。

    `sys_s` は呼び出し元（`run_all`）が `_openai_endpoint_status()` で確定した**実効**
    `system_settings`（接続先が未確定／不正なら `None`）を渡す契約＝`check_selected_provider_key`／
    `check_cloud_llm_probes` と同じゲート。接続先が確定できない間に生の（未反映・未確定の）
    `sys_s` で `kind` を判定すると、Azure/custom 環境変数が妥当なのに反映されず「既定 openai」
    と誤判定したり、逆に接続先が不正な構成のまま Azure/custom 分岐（`_check_codex_azure_compat`）
    が `PROBE_CLOUD=1` で実送信してしまう。`sys_s is None` の間はこの実送信の可能性がある分岐へ
    一切進まず `codex_auth` を SKIP にする（送信ゼロ）。

    `indeterminate`（`run_all` が `_agent_resolution_indeterminate()` で判定して渡す）が真なら、
    `required`／`needs_openai_auth`（`_codex_required` が fail-closed で `True` に丸めた値）を
    使った通常判定へは進まず、3項目すべてを固定文言の NG にする。`required=True` のまま通常
    判定へ進むと、CLI が実際に導入・ログイン済みの環境ではたまたま `ok` になってしまい、
    「実効頭脳を判定できず本来は codex が必要かどうか自体が不明」という根本原因が握り潰される
    （`_agent_resolution_indeterminate` docstring 参照）。
    """
    if indeterminate:
        return [CheckResult("codex_cli", "Codex CLI 導入", "ng", _AGENT_RESOLUTION_FAILED_DETAIL),
                CheckResult("codex_version", "Codex CLI バージョン", "ng", _AGENT_RESOLUTION_FAILED_DETAIL),
                CheckResult("codex_auth", "Codex 認証・設定の整合", "ng", _AGENT_RESOLUTION_FAILED_DETAIL)]
    out: list[CheckResult] = []

    found = bool(shutil.which("codex"))
    if not found:
        status = "ng" if required else "skip"
        detail = ("codex コマンドが見つかりません（PATH を確認してください）" if required else
                  f"codex コマンドが見つかりません（現在の構成では Codex を使わないため問題ありません{note}）")
        out.append(CheckResult("codex_cli", "Codex CLI 導入", status, detail))
        out.append(CheckResult("codex_version", "Codex CLI バージョン", "skip", "CLI が無いため確認できません"))
        out.append(CheckResult("codex_auth", "Codex 認証・設定の整合", "skip", "CLI が無いため確認できません"))
        return out
    out.append(CheckResult("codex_cli", "Codex CLI 導入", "ok", "codex コマンドが見つかりました"))

    failure_status = "ng" if required else "skip"
    optional_note = "" if required else f"（現在の構成では Codex を使わないため問題ありません{note}）"
    try:
        r = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=_CODEX_TIMEOUT)
        text = (r.stdout or r.stderr or "").strip()
        if r.returncode == 0 and text:
            out.append(CheckResult("codex_version", "Codex CLI バージョン", "ok", text))
        else:
            out.append(CheckResult("codex_version", "Codex CLI バージョン", failure_status,
                                    (text or "バージョンを取得できませんでした") + optional_note))
    except Exception as e:
        out.append(CheckResult("codex_version", "Codex CLI バージョン", failure_status,
                                f"{type(e).__name__}{optional_note}"))

    if not needs_openai_auth:
        out.append(CheckResult("codex_auth", "Codex 認証・設定の整合", "skip",
                                "Codex は Ollama 構成でのみ使われています（OpenAI/Azure 側の認証は"
                                "不要・Ollama 側の疎通は「ローカルLLM」の項目を確認してください）"))
    elif sys_s is None:
        out.append(CheckResult("codex_auth", "Codex 認証・設定の整合", "skip",
                                "接続先設定が確認できないため確認できません"
                                "（「接続先設定」の項目を確認してください）"))
    else:
        out.append(_check_codex_auth(sys_s, rows, required, note, probe_cloud))
    return out


# ---------------------------------------------------------------------------
# 統合
# ---------------------------------------------------------------------------

def run_all(*, probe_cloud: bool) -> list[CheckResult]:
    with _log_redaction_active():
        results: list[CheckResult] = []

        pg = check_postgres()
        results.append(pg)
        results.append(check_neo4j())
        es = check_es_connect()
        results.append(es)
        results.append(check_es_kuromoji(es.status == "ok"))

        settings_check, sys_s = _load_system_settings(pg.status == "ok")
        results.append(settings_check)
        users_check, rows = _load_active_user_configs(pg.status == "ok")
        results.append(users_check)

        # `llm_sys_s`（接続先が確定できないときは `None`）は openai/gemini/bedrock/Codex の
        # OpenAI 側（`_check_codex_azure_compat` 経由の実送信を含む）の全ての送信を伴う確認
        # （`check_selected_provider_key`／`check_cloud_llm_probes`／`check_codex`）へ渡す＝
        # 接続先が不正な間はこれらを一律 SKIP にする（`_openai_endpoint_status` docstring 参照）。
        # `_disabled_agent_configs`／Ollama／`_codex_required`（Codex が必須かどうかの判定自体は
        # 接続先の妥当性と無関係）は元の `sys_s` をそのまま使う。
        endpoint_status = _openai_endpoint_status(sys_s)
        results.append(CheckResult(_OPENAI_ENDPOINT_CID, _OPENAI_ENDPOINT_LABEL,
                                    endpoint_status["status"], endpoint_status["detail"]))
        llm_sys_s = endpoint_status["effective_sys_s"]

        results.append(check_selected_provider_key(llm_sys_s, rows))
        results.append(_disabled_agent_configs(sys_s, rows))

        results.extend(check_cloud_llm_probes(llm_sys_s, rows, probe_cloud))

        codex_required, codex_needs_openai_auth, codex_note = _codex_required(sys_s, rows)
        # `sys_s`／`rows` 自体が読めているのに `effective_agent()` が例外を投げるケースだけを
        # 対象にする（`sys_s is None`／`rows is None` は `_codex_required` の早期 fail-closed
        # 分岐が既に処理済み＝その根本原因は `system_settings_read`／`user_settings_read` 側の
        # 独立した NG として別途表面化するため、ここで重ねて判定不能扱いにしない）。
        codex_indeterminate = (sys_s is not None and rows is not None
                               and _agent_resolution_indeterminate(sys_s, rows))
        results.extend(check_ollama_probes(sys_s, rows))
        results.extend(check_codex(llm_sys_s, rows, codex_required, codex_needs_openai_auth,
                                    codex_note, probe_cloud, indeterminate=codex_indeterminate))

        return results


_STATUS_MARK = {"ok": "OK  ", "ng": "NG  ", "skip": "SKIP"}


def format_report(results: list[CheckResult]) -> str:
    lines = [f"[{_STATUS_MARK[r.status]}] {r.label}: {r.detail}" for r in results]
    ok = sum(1 for r in results if r.status == "ok")
    ng = sum(1 for r in results if r.status == "ng")
    skip = sum(1 for r in results if r.status == "skip")
    lines.append("")
    lines.append(f"合計: OK={ok} NG={ng} SKIP={skip}")
    if ng:
        lines.append("NG の項目を確認してください（閉域LAN等で意図的に到達できない項目は SKIP と区別されます）。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    results = run_all(probe_cloud=probe_cloud_enabled())
    print(format_report(results))
    return 1 if any(r.status == "ng" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
