"""vision アーム（視覚読み取り・VLM）。画像・スキャン PDF を視覚モデルが見て文字/内容を読み取り、Markdown を返す。

- 既定はローカル（Ollama の視覚モデル・既定モデル `qwen2.5vl`・接続先は管理画面の中央の Ollama 接続先 `ollama_url` ＞ `http://localhost:11434`・許可リストの判定も同じ）。
- クラウド（OpenAI）は管理者が `system_settings.vlm.cloud_allowed=true` にした場合のみ。false（既定）なら provider=openai が指定されていても画像を送らない。
- Ollama の接続先は loopback か管理画面の Ollama 許可一覧にある宛先（IP・DNS 名とも）ならクラウド許可は不要。許可されていない宛先へは送らない（`_ollama_url_permitted`）。

対象（`accepts`）: (a) ラスタ画像（`office_md.IMAGE_EXT`）をそのまま VLM へ。(b) テキスト層ゼロの PDF（`office_md.pdf_escalation_target == "vision"`）を PDFium でページ画像化して VLM へ（`raster` を共用）。Office 文書は担当しない。
出力: 先頭に `> [OCR抽出（AI視覚読み取り）: 信頼度=低〜中]`・`method="vision"`・confidence 0.4・notes に `vlm_provider`/`vlm_model`/`numeric_verified=false`（VLM は数値を誤りうる）。到達不可/失敗/未対応は None。
クラウド遮断は二層: ① `resolve_vlm()`（`convert()` 開始時の1回の判定）、② 送信直前（`_read_openai`/`_read_ollama`）に `_cloud_allowed_now()` が system_settings を読み直して再検査する（長い PDF の途中で許可が取り消されても次ページで止まる）。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import ArmResult
from .. import ai_observation, evidence_ir, ocr_router

_log = logging.getLogger(__name__)

_LABEL = "> [OCR抽出（AI視覚読み取り）: 信頼度=低〜中]"  # 出力 MD 先頭の出所ラベル
_DEFAULT_PROVIDER = "ollama"
_DEFAULT_MODEL = "qwen2.5vl"  # Qwen2.5-VL（日本語を含む文書/OCR に強い）
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_KNOWN_PROVIDERS = ("ollama", "openai")
_DEFAULT_VLM_TIMEOUT_SEC = 180.0  # 1 ファイル（画像1枚 or PDF 全ページ合計）あたりの VLM 総予算（既定）
_DEFAULT_MAX_IMAGE_MB = 20.0  # 1 画像あたりの base64 化サイズ上限（既定）

# 視覚読み取りの指示（決定的・数値をでっち上げないよう明示）。日本語資料前提。
_VLM_PROMPT = ("この画像に写っている文字と内容を、見えるとおりに日本語で書き起こしてください。"
               "表は行と列の構造を保ち、読めない箇所は［判読不可］と書いてください。"
               "見えていない数値や文言を推測で補わないでください。説明や前置きは不要で、書き起こしのみを返してください。")


# VLM 設定の解決（system_settings > 既定。ollama_url は中央の Ollama 接続先を使う）

def _system_vlm() -> dict:
    """全体設定 `vlm`（dict）を返す。読めない/未設定/不正型は空 dict（既定へ）。store を読めない文脈（MCP サブプロセス等）では例外を握って空 dict。"""
    try:
        from sherpa import store
        val = store.get_system_settings().get("vlm")
    except Exception:
        return {}
    return val if isinstance(val, dict) else {}


def vlm_config() -> dict:
    """VLM の実効設定（provider/model/cloud_allowed/ollama_url）を解決する。

    provider＝system `vlm.provider` ＞ 既定 `ollama`、model＝system `vlm.model` ＞ 既定 `qwen2.5vl`。cloud_allowed＝system `vlm.cloud_allowed`（bool）のみ・既定 false（env フォールバックなし＝管理者の明示操作でのみ true）。ollama_url＝system `ollama_url`（中央の Ollama 接続先）＞ 既定。遮断前の生の解決結果を返す（openai×未許可でも provider=openai を返す）。
    """
    sysv = _system_vlm()
    provider = str(sysv.get("provider") or _DEFAULT_PROVIDER).strip().lower()
    if provider not in _KNOWN_PROVIDERS:
        provider = _DEFAULT_PROVIDER
    model = str(sysv.get("model") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    cloud_allowed = bool(sysv.get("cloud_allowed"))  # system のみ・既定 false（env フォールバックなし）
    try:
        from sherpa import keys
        ollama_url = str(keys.resolve_ollama_url(None) or "").strip() or _DEFAULT_OLLAMA_URL
    except Exception:  # 中央の接続先を読めないときは空にして送信させない（`resolve_vlm` が止める）
        ollama_url = ""
    return {"provider": provider, "model": model, "cloud_allowed": cloud_allowed, "ollama_url": ollama_url}


def env_default_vlm() -> dict:
    """system_settings を無視した既定の実効設定（管理画面「未設定に戻すと何になるか」表示用・cloud は常に false）。"""
    return {"provider": _DEFAULT_PROVIDER, "model": _DEFAULT_MODEL, "cloud_allowed": False}


def _openai_key(system_settings: dict | None = None) -> str | None:
    """VLM（クラウド）用の OpenAI API キー（取り込みは全体処理なので中央設定を使い、per-user 設定は使わない）。

    `sherpa.keys.resolve_api_key` 経由で、選択中のクラウドプロバイダが openai でなければ常に None。env は読まない。`system_settings`（省略可）は `_read_openai` が送信直前に1回読んだスナップショット（省略時は自分で読む）。
    """
    key, _invalid = _openai_key_with_reason(system_settings)
    return key


def _openai_key_with_reason(system_settings: dict | None = None) -> tuple[str | None, bool]:
    """`_openai_key` と同じキー解決＋「不正な cloud_provider が原因で無効化したか」を返す。

    戻り値 `(key, invalid)`: `invalid=True` のときだけ診断ログを残している。呼び出し元（`resolve_vlm`）はこれを見て、ログが実在する経路だけ「詳細は直前のログを参照」と案内する。
    """
    from sherpa import keys
    # `cloud_provider` が非空の不正値のときは、既定（openai）へ倒れたキーで画像を送信しない（キー無し＝ None＝送信 OFF へ寄せる）。
    try:
        key = (keys.resolve_api_key("openai", None, system_settings=system_settings, strict=True) or "").strip()
    except keys.InvalidCloudProviderConfigError as e:
        # 利用者向けには出さず None（送信 OFF）へ縮退するが、実際の理由を診断できるログを残す。
        _log.warning("VLM._openai_key: cloud_provider が不正なため無効化しました: %s", e)
        return None, True
    return (key or None), False


def _cloud_allowed_now(system_settings: dict | None = None) -> bool:
    """送信直前に system_settings から生の cloud_allowed を読み直す（stale cfg 回避）。

    `resolve_vlm()` の結果を持ち回ると、長い PDF ループ中や直接呼び出しで、許可が取り消された後も古い許可のまま送信しうる。毎回読み直すことで次の送信から反映される（store には短TTLのキャッシュがある）。store 到達不可/例外/型不正は False（fail-closed）。
    `system_settings`（省略可）: 呼び出し側が送信直前に1回読んだスナップショットを渡すと、それを使う（1回の送信判断の中で読みが食い違わない）。
    """
    try:
        if system_settings is not None:
            val = system_settings.get("vlm")
        else:
            from sherpa import store
            val = store.get_system_settings().get("vlm")
    except Exception:
        return False
    if not isinstance(val, dict):
        return False
    return bool(val.get("cloud_allowed"))


def _ollama_url_permitted(url: str) -> bool:
    """`url` が Ollama の接続許可ポリシー（loopback＋管理画面の許可一覧＝回答用の Ollama と同じ `llm.assert_ollama_url_allowed`）を満たすか。I/O なし。不許可・不正 URL は False。"""
    from sherpa import llm
    hp = llm._canonical_host_port(url)
    if hp is not None and llm.is_loopback_host(hp[0]):
        return True  # loopback は常に許可（許可一覧を読まない）
    try:
        llm.assert_ollama_url_allowed(url)
    except Exception:
        return False
    return True


def _vlm_usable_override() -> bool | None:
    """MCP サブプロセス向けスナップショット（env `SHERPA_VLM_USABLE`・`agents._mcp_env` が設定）。

    MCP サブプロセスは PG creds を持たず `vlm` 設定を読めないため、親プロセスが計算した `resolve_vlm() is not None` の1bit（`"1"`/`"0"`）を渡し、設定されていれば最優先で信じる（secrets は渡さない）。未設定/不正値は None（通常の system_settings/env 解決へ）。
    """
    raw = (os.environ.get("SHERPA_VLM_USABLE") or "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    return None


def resolve_vlm() -> dict | None:
    """実際に使える VLM 設定（provider/model/cloud_allowed/ollama_url）を返す（使えない/遮断は None）。

    - provider=openai → cloud_allowed=true かつ API キー有のときだけ使える。cloud_allowed=false なら None（画像を送らない・warning ログ）。
    - provider=ollama → cloud_allowed=true、または接続先が Ollama の許可ポリシーを満たす（`_ollama_url_permitted`）ときだけ使える。判定はネットワーク I/O を伴わない。
    ここでの判定は `convert()` 開始時のスナップショット。送信直前の再検査は `_cloud_allowed_now()`。
    `SHERPA_VLM_USABLE` が設定されていれば最優先（`"0"` は無条件で None、`"1"` は `vlm_config()` の生値を返す）。
    """
    override = _vlm_usable_override()
    if override is not None:
        return vlm_config() if override else None
    cfg = vlm_config()
    if cfg["provider"] == "openai":
        if not cfg["cloud_allowed"]:
            _log.warning("VLM: provider=openai が指定されていますが、クラウド許可（cloud_allowed）が無いため"
                         "無効化します（画像はクラウドへ送信しません）。")
            return None
        _key, _key_invalid = _openai_key_with_reason()
        if not _key:
            # 「詳細は直前のログを参照」は `_openai_key_with_reason()` が診断ログを残した経路（cloud_provider 不正）だけに限定する。
            if _key_invalid:
                _log.warning("VLM: provider=openai・クラウド許可済みですが OpenAI キーを解決できないため"
                            "無効化します（詳細は直前のログを参照）。")
            else:
                _log.warning("VLM: provider=openai・クラウド許可済みですが OpenAI キーが未設定のため"
                            "無効化します（画像はクラウドへ送信しません）。")
            return None
    elif cfg["provider"] == "ollama":
        if not cfg["ollama_url"]:
            _log.warning("VLM: 中央の Ollama 接続先を読めないため無効化します（画像は送信しません）。")
            return None
        if not cfg["cloud_allowed"] and not _ollama_url_permitted(cfg["ollama_url"]):
            _log.warning("VLM: provider=ollama の接続先（%s）が Ollama の許可先と確認できず、クラウド許可も"
                         "無いため無効化します（画像は送信しません）。", cfg["ollama_url"])
            return None
    return cfg


def vlm_usable() -> bool:
    """VLM が実効的に使える設定か（未導入案内・convertible_exts・escalation・arms_sig 用・ネットワーク I/O なし）。"""
    return resolve_vlm() is not None


def sig_value(names) -> str:
    """arms_sig 用の VLM 署名値（実効可用性ベース）。

    `vision` が有効アームに無い、または使えない構成（`resolve_vlm()` が None）なら `"none"`。使える構成のときだけ `"<provider>:<model>:cloud=<on|off>"`（キーの追加/削除・接続先の許可状態の変化でも変わる）。エンジンの版は含めない。
    """
    if "vision" not in set(names):
        return "none"
    cfg = resolve_vlm()
    if cfg is None:
        return "none"
    return f"{cfg['provider']}:{cfg['model']}:cloud={'on' if cfg['cloud_allowed'] else 'off'}"


def pdf_rasterize_available() -> bool:
    """PDF をページ画像へラスタライズできるか（`raster` の PDFium 判定を共用）。"""
    from . import raster
    return raster.pdf_rasterize_available()


def _vlm_timeout_sec() -> float:
    """1 ファイルあたりの VLM 総予算秒（env `SHERPA_VLM_TIMEOUT`・不正/未設定は既定 180s）。"""
    raw = os.environ.get("SHERPA_VLM_TIMEOUT")
    if not raw:
        return _DEFAULT_VLM_TIMEOUT_SEC
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_VLM_TIMEOUT_SEC
    return v if v > 0 else _DEFAULT_VLM_TIMEOUT_SEC


class VisionArm:
    """画像・スキャン PDF を VLM で読み取るアーム（既定ローカル Ollama・クラウドは管理者オプトイン）。"""
    name = "vision"

    def available(self) -> bool:
        return vlm_usable()

    def accepts(self, path) -> bool:
        ext = Path(path).suffix.lower()
        from .. import office_md
        if ext in office_md.IMAGE_EXT:
            return vlm_usable()  # 画像は VLM だけで読める（ラスタ化不要）
        if ext == ".pdf":
            return office_md.pdf_escalation_target(path) == "vision"
        return False

    def convert(self, path) -> ArmResult | None:
        cfg = resolve_vlm()
        if cfg is None:
            return None  # 未設定/到達不可/クラウド遮断（warning は resolve 側）
        p = Path(path)
        ext = p.suffix.lower()
        from .. import office_md
        from sherpa import metering
        extra_notes: list[str] = []
        # 読み取り〜返却全体を acc スコープで囲み、`kind='vlm'` で1ファイル1行に集約する（calls＝ページ/画像数）。user_id/world は付けない（システムレベルの取り込み）。
        metering.acc_begin()
        try:
            try:
                if ext in office_md.IMAGE_EXT:
                    text, pages = self._read_image(p, cfg), 1
                elif ext == ".pdf":
                    text, pages, extra_notes = self._read_pdf(p, cfg)
                else:
                    return None
            except Exception:  # 想定外は未対応に倒す
                _log.warning("VLM 視覚読み取りに失敗しました（%s）", p, exc_info=False)
                return None
            if not text or not text.strip():
                return None  # 文字が取れない＝未対応（失敗計上）
            md = _LABEL + "\n\n" + text.strip()
            notes = [f"vlm_provider={cfg['provider']}", f"vlm_model={cfg['model']}", "numeric_verified=false"]
            if ext == ".pdf":
                notes.append(f"pages={pages}")
            notes.extend(extra_notes)  # タイムアウト予算切れの truncated 記録
            return ArmResult(md=md, method="vision", confidence=0.4, notes=notes)
        finally:
            tokens, n = metering.acc_end()
            if n:
                metering.record("vlm", cfg["provider"], cfg["model"], tokens, calls=n)

    def _read_image(self, image_path: Path, cfg: dict) -> str | None:
        """1 枚の画像を VLM で読み取る（失敗/空は None）。1 ファイル総予算＝画像1枚分の全時間。"""
        return _vlm_read(image_path, cfg, _vlm_timeout_sec())

    def _read_pdf(self, pdf_path: Path, cfg: dict) -> tuple[str | None, int, list[str]]:
        """テキスト層ゼロ PDF を PDFium でページ画像化し、各ページを VLM で読み取る（ラスタ化は `raster` を共用）。

        返値 `(結合テキスト|None, 処理ページ数, 追加notes)`。1 ファイル総予算（`SHERPA_VLM_TIMEOUT`）の残り時間で各ページを実行し、予算切れは残ページを打ち切って `truncated=pages N/M` を notes に残す。ページ上限（`SHERPA_OCR_MAX_PAGES`）・ピクセル上限は `raster` を共用する。
        """
        import pypdfium2 as pdfium

        from . import raster
        tmp = Path(tempfile.mkdtemp(prefix="sherpa-vlmocr-"))
        parts: list[str] = []
        pages_done = 0
        extra_notes: list[str] = []
        deadline = time.monotonic() + _vlm_timeout_sec()
        try:
            doc = pdfium.PdfDocument(str(pdf_path))
            try:
                total = min(len(doc), raster._max_pages())
                for i in range(total):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        extra_notes.append(f"truncated=pages {pages_done}/{total}（タイムアウト予算超過）")
                        break
                    img = tmp / f"page_{i + 1}.png"
                    page = doc[i]
                    try:
                        rendered = raster._rasterize_page(page)  # ピクセル上限クランプ（raster 共用）
                        try:
                            rendered.save(str(img), format="PNG")
                        finally:
                            rendered.close()
                    finally:
                        page.close()
                    page_text = _vlm_read(img, cfg, remaining)
                    pages_done += 1
                    if page_text and page_text.strip():
                        parts.append(f"## ページ {i + 1}\n\n{page_text.strip()}")
            finally:
                doc.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return ("\n\n".join(parts) if parts else None), pages_done, extra_notes


_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
                ".tiff": "image/tiff", ".webp": "image/webp"}


def _read_image_b64(image_path: Path) -> str:
    """画像を base64 文字列で読む（VLM の images/data URL 用）。"""
    return base64.b64encode(Path(image_path).read_bytes()).decode("ascii")


def _max_image_mb() -> float:
    """1 画像あたりの base64 化サイズ上限 MB（env `SHERPA_VLM_MAX_IMAGE_MB`・不正/未設定は既定 20）。"""
    raw = os.environ.get("SHERPA_VLM_MAX_IMAGE_MB")
    if not raw:
        return _DEFAULT_MAX_IMAGE_MB
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_MAX_IMAGE_MB
    return v if v > 0 else _DEFAULT_MAX_IMAGE_MB


def _image_too_large(image_path: Path) -> bool:
    """画像ファイルサイズが上限を超えるか（暴走/コスト防止）。

    stat 不可は「超過ではない」扱い（後続の読み込みで失敗する）。単独画像ファイルにはピクセル上限クランプが効かないため、送信前にファイルサイズで一律ガードする。上限超過は「未対応」として扱う。
    """
    try:
        size = Path(image_path).stat().st_size
    except OSError:
        return False
    return size > _max_image_mb() * 1024 * 1024


def _vlm_read(image_path: Path, cfg: dict, timeout: float) -> str | None:
    """1 枚の画像を VLM（provider 別）で読み取り、テキストを返す（失敗/到達不可は None）。"""
    if _image_too_large(image_path):
        _log.warning("VLM: 画像サイズが上限（%sMB）を超えるため読み取りをスキップします: %s",
                     _max_image_mb(), image_path)
        return None
    provider = cfg["provider"]
    try:
        if provider == "ollama":
            return _read_ollama(image_path, cfg, timeout)
        if provider == "openai":
            return _read_openai(image_path, cfg, timeout)
    except Exception as e:
        _log.warning("VLM(%s) 読み取りに失敗しました（%s）: %s", provider, e.__class__.__name__, image_path)
        return None
    return None


def _read_ollama(image_path: Path, cfg: dict, timeout: float) -> str | None:
    """Ollama `/api/chat`（stream=false・images に base64）で画像1枚を読み取り、本文テキストを返す。

    cloud_allowed=false のときは、接続先が Ollama の許可ポリシーを満たす（`_ollama_url_permitted`）ことを送信直前に毎回検証する。cloud_allowed は送信直前に読み直す（`_cloud_allowed_now`）。cfg 引数の cloud_allowed は見ない。
    """
    url = cfg["ollama_url"]
    if not _cloud_allowed_now() and not _ollama_url_permitted(url):
        _log.warning("VLM(ollama): 接続先（%s）が Ollama の許可先と確認できず、クラウド許可も無いため"
                     "送信しません（fail-safe）。", url)
        return None
    from sherpa import llm, metering
    body = {"model": cfg["model"], "stream": False,
            "messages": [{"role": "user", "content": _VLM_PROMPT, "images": [_read_image_b64(image_path)]}]}
    # 接続先は中央の Ollama 接続先と同じ許可リスト（loopback＋管理画面の許可一覧）で検証する。
    resp = llm.post_json(llm.ollama_url(url, "/api/chat"),
                         llm.JSON_HEADERS, body, timeout=max(1, int(timeout)))
    metering.acc_add(metering.usage_from_ollama_chat(resp))
    return ((resp or {}).get("message") or {}).get("content")


def _read_openai(image_path: Path, cfg: dict, timeout: float) -> str | None:
    """OpenAI Chat Completions（image_url に base64 data URL）で画像1枚を読み取る。

    cloud_allowed を送信直前に system_settings から読み直し（`_cloud_allowed_now`）、許可されていない送信を防ぐ（`cfg["cloud_allowed"]` は見ない）。`system_settings` は冒頭で1回だけ読み、cloud_allowed 判定・鍵解決・接続先（`llm.openai_url`/`openai_headers`）を同じスナップショットで揃える。
    """
    try:
        from sherpa import store
        sys_s = store.get_system_settings()
    except Exception:
        sys_s = {}
    if not _cloud_allowed_now(sys_s):
        _log.warning("VLM(openai): クラウド許可が無いため画像を送信しません（fail-safe）。")
        return None
    from sherpa import llm, metering
    key = _openai_key(sys_s)
    if not key:
        return None
    mime = _MIME_BY_EXT.get(Path(image_path).suffix.lower(), "image/png")
    data_url = f"data:{mime};base64,{_read_image_b64(image_path)}"
    body = {"model": cfg["model"], "messages": [{"role": "user", "content": [
        {"type": "text", "text": _VLM_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}]}
    # 送信は `llm.openai_post_json`（OpenAI 専用の送信直前ガード付き）を使う。`post_json` は Ollama と共用のため一律遮断しない。
    resp = llm.openai_post_json(llm.openai_url("chat/completions", system_settings=sys_s),
                         llm.openai_headers(key, system_settings=sys_s), body, timeout=max(1, int(timeout)))
    metering.acc_add(metering.usage_from_openai_chat(resp))
    choices = (resp or {}).get("choices") or []
    if not choices:
        return None
    return (choices[0].get("message") or {}).get("content")


# 補足観測: canonical が読めない画像要素だけを VLM で観測する
# `convert()` は文書/画像全体を vision が単独で担当する経路。ここから下は、① OOXML が既に読めている文書の一部（picture 等）だけを補足する別の役割で、出力は `ai_observation.AIObservationSet` として Canonical Evidence と分離して保存する（原値を上書きしない）。
VISION_OBSERVATION_PROMPT_SCHEMA_VERSION = "vision-observation-image-v1"
VISION_OBSERVATION_PREPROCESSING_PROFILE = "embedded-asset-original-v1"
# 単独ソースの画像内容観測に対する固定 confidence。`ai_observation.MIN_ANSWER_CONFIDENCE`（0.70）以上にして `use_for_answer=True` を許可する。全体変換時の `convert()` の confidence=0.4 とは別値。
VISION_OBSERVATION_CONFIDENCE = 0.75


def build_asset_observations(
    ir: evidence_ir.EvidenceIR,
    *,
    decisions: list[ocr_router.OCRRouteDecision],
    asset_root: Path,
) -> ai_observation.AIObservationSet | None:
    """canonical が読み取れない画像要素（`metadata_only`/`image_content_uninterpreted` 等）を VLM で補足観測する。

    `decisions` は呼び出し側（`office_md`）が `ocr_router.build_manifest()` で選定した `status == "selected" かつ input_kind == "asset"` の候補に限って渡すこと（ここでは再判定しない）。`page_render` は渡されても無視する（PDF 全体の読み取りは `VisionArm.convert()` が担当）。
    VLM が使えない（`resolve_vlm()` が None）なら None。1画像の失敗/空応答はその画像だけスキップする。全候補が失敗/空なら None。
    """
    cfg = resolve_vlm()
    if cfg is None:
        return None
    asset_decisions = [
        item for item in decisions if item.input_kind == "asset" and item.status == "selected"
    ]
    if not asset_decisions:
        return None
    timeout = _vlm_timeout_sec()
    inputs: list[dict] = []
    observations: list[dict] = []
    for decision in asset_decisions:
        if not decision.asset_rel_path or not decision.asset_sha256 or not decision.media_type:
            continue
        image_path = asset_root / decision.asset_rel_path
        if not image_path.is_file():
            continue
        text = _vlm_read(image_path, cfg, timeout)
        if not text or not text.strip():
            continue
        inputs.append({
            "input_id": decision.route_input_id,
            "target_evidence_id": decision.target_evidence_id,
            "asset_sha256": decision.asset_sha256,
            "media_type": decision.media_type,
            "pixel_size": decision.pixel_size,
            "input_kind": "asset",
        })
        observations.append({
            "input_id": decision.route_input_id,
            "kind": "summary",
            "text": text.strip(),
            "confidence": VISION_OBSERVATION_CONFIDENCE,
            "searchable": True,
            "use_for_answer": True,
            "numeric_verified": False,
            "attributes": {"vlm_provider": cfg["provider"], "vlm_model": cfg["model"]},
        })
    if not observations:
        return None
    raw_response = json.dumps(observations, ensure_ascii=False, sort_keys=True)
    return ai_observation.build(
        ir=ir,
        provider=cfg["provider"],
        model=cfg["model"],
        execution_mode=("external" if cfg["provider"] == "openai" else "local"),
        prompt_schema_version=VISION_OBSERVATION_PROMPT_SCHEMA_VERSION,
        preprocessing_profile=VISION_OBSERVATION_PREPROCESSING_PROFILE,
        raw_response=raw_response,
        inputs=inputs,
        observations=observations,
    )
