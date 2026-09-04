"""アーム＝vision（視覚読み取り・VLM）。画像・スキャン PDF を **AI（視覚モデル）が見て**文字/内容を読み取る。

⑤アーム再定義（feedback-batch-2026-07-08 ⑤・2026-07-08）の②。tesseract の `ocr` アーム（LLM を使わない軽量代替）
とは別に、**LLM 指定可能な視覚読み取り**を担当する。エンジンは Vision-LLM（VLM）:

- **既定＝ローカル（Ollama の視覚モデル）**。既定モデルは `qwen2.5vl`（Qwen2.5-VL＝日本語を含む多言語の文書/OCR に
  強く、Ollama で入手可・7B 既定でローカル実用的）。接続先は env `SHERPA_VLM_OLLAMA_URL`＞`http://localhost:11434`。
- **クラウド（OpenAI）は管理者がシステム管理画面で明示許可（`system_settings.vlm.cloud_allowed=true`）した場合のみ**。
  cloud_allowed が false（既定）なら provider=openai が指定されていても**絶対に画像を送らない**（fail-safe で
  無効扱い＋warning ログ・INGEST-MD 決定3 の「既定ローカル・管理者オプトインでクラウド」に一致）。

対象（accepts）:
- (a) ラスタ画像（`office_md.IMAGE_EXT`）＝そのまま VLM へ。
- (b) テキスト層ゼロの PDF（`office_md.pdf_escalation_target == "vision"`）＝officeCOM の忠実 PDF レンダは
  「PDF 入力はそのまま」なので、ここでは PDFium でページ画像化してから VLM に渡す（ラスタ化ヘルパは `raster` を共用）。
- (c) **Office → PDF レンダ → VLM の視覚照合はやらない**（スコープ肥大防止）: proposal ⑤ の主フローは PDF/画像の
  視覚読み取りで、Office の視覚照合（`legacy_convert.render_pdf` 経由の A4 secondary 追加）は将来。今回は (a)(b) のみ
  を単独担当する（Office の値は① OOXML を権威とし、視覚モデルによるOffice文書の解釈は追加しない）。

出力（`ocr` と同じ非対称メタ）: 先頭に `> [OCR抽出（AI視覚読み取り）: 信頼度=低〜中]`・`method="vision"`・
confidence 低め（0.4）・notes に `vlm_provider`/`vlm_model`/**`numeric_verified=false`**（VLM は数値をハルシネートし
うる＝数値クリティカル文書で単独ソースにしない・INGEST-MD §5）。到達不可/失敗/未対応は None（正直表示・失敗計上）。

fail-safe（決定3・スコープ）: 未導入/未設定/到達不可/例外は None。**クラウド遮断は二層**: (1) `resolve_vlm()`
（convert() 開始時の1回きりの判定・provider=openai は cloud_allowed+キー必須／provider=ollama は cloud_allowed
または接続先がローカル/私有アドレス帯（`_is_local_url`）必須）、(2) 送信直前（`_read_openai`/`_read_ollama`）で
**`_cloud_allowed_now()`（system_settings を毎回読み直す・stale cfg 対策）**を再検査する（belt-and-braces・
RV High #1/#2・2026-07-08）。長い PDF ページループの途中で管理者が許可を取り消しても、次ページの送信直前
チェックで確実に止まる（cfg 引数に持ち回った古い cloud_allowed の値は信用しない）。

**社内 LAN の Ollama を使う場合**: 接続先を IP アドレス（例 `192.168.1.50`）で指定すればクラウド許可は不要。
**ドメイン名（`localhost` 以外のホスト名）は DNS で外部へ解決されうるため許可が必要**（`_is_local_url`）。
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from . import ArmResult
from .. import ai_observation, evidence_ir, ocr_router

_log = logging.getLogger(__name__)

_LABEL = "> [OCR抽出（AI視覚読み取り）: 信頼度=低〜中]"     # 出力 MD 先頭の出所ラベル（proposal 準拠）
_DEFAULT_PROVIDER = "ollama"
_DEFAULT_MODEL = "qwen2.5vl"                     # Qwen2.5-VL（日本語含む文書/OCR に強い・Ollama で入手可）
_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_KNOWN_PROVIDERS = ("ollama", "openai")
_DEFAULT_VLM_TIMEOUT_SEC = 180.0                 # 1 ファイル（画像1枚 or PDF 全ページ合計）あたりの VLM 総予算（既定）
_DEFAULT_MAX_IMAGE_MB = 20.0                     # 1 画像あたりの base64 化サイズ上限（既定・RV Med #4）

# ローカル/私有アドレス帯（RFC1918）。ループバック（127.0.0.0/8・::1）は ipaddress.is_loopback で判定。
_PRIVATE_V4_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

# 視覚読み取りの指示（決定的・数値をでっち上げないよう明示）。日本語資料前提。
_VLM_PROMPT = ("この画像に写っている文字と内容を、見えるとおりに日本語で書き起こしてください。"
               "表は行と列の構造を保ち、読めない箇所は［判読不可］と書いてください。"
               "見えていない数値や文言を推測で補わないでください。説明や前置きは不要で、書き起こしのみを返してください。")


# ---- VLM 設定の解決（system_settings > 既定・cloud はオプトイン・provider/model の env フォール
#      バックは UI 昇格に伴い ENV-CLEAN で撤去済み。ollama_url は UI 項目が無いため env のまま）----

def _system_vlm() -> dict:
    """全体設定 `vlm`（dict）を返す（読めない/未設定/不正型は空 dict＝既定へ・fail-safe）。

    `arms._system_arms_enabled` と同じ fail-safe（store を読めない MCP サブプロセス等では例外を握って空 dict）。
    """
    try:
        from sherpa import store
        val = store.get_system_settings().get("vlm")
    except Exception:
        return {}
    return val if isinstance(val, dict) else {}


def vlm_config() -> dict:
    """VLM の**実効設定**（provider/model/cloud_allowed/ollama_url）を解決する。

    provider＝system `vlm.provider` ＞ 既定 `ollama`（既知外は既定へ）。
    model＝system `vlm.model` ＞ 既定 `qwen2.5vl`。
    **cloud_allowed＝system `vlm.cloud_allowed`（bool）のみ・既定 false**（env フォールバックを持たせない＝
    クラウド許可は管理者の明示操作でのみ true になる・INGEST-MD 決定3）。ollama_url＝env `SHERPA_VLM_OLLAMA_URL`
    ＞ 既定 `http://localhost:11434`（UI 項目が無いため env のまま）。この関数は**遮断前の生の解決結果**
    （openai×未許可でも provider=openai を返す）。
    """
    sysv = _system_vlm()
    provider = str(sysv.get("provider") or _DEFAULT_PROVIDER).strip().lower()
    if provider not in _KNOWN_PROVIDERS:
        provider = _DEFAULT_PROVIDER
    model = str(sysv.get("model") or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    cloud_allowed = bool(sysv.get("cloud_allowed"))       # system のみ・既定 false（env フォールバックなし）
    ollama_url = (os.environ.get("SHERPA_VLM_OLLAMA_URL") or _DEFAULT_OLLAMA_URL).strip() or _DEFAULT_OLLAMA_URL
    return {"provider": provider, "model": model, "cloud_allowed": cloud_allowed, "ollama_url": ollama_url}


def env_default_vlm() -> dict:
    """system_settings を無視した既定の実効設定（管理画面「未設定に戻すと何になるか」表示用・cloud は常に false・
    provider/model の env フォールバックは撤去済み＝常にコード既定）。"""
    return {"provider": _DEFAULT_PROVIDER, "model": _DEFAULT_MODEL, "cloud_allowed": False}


def _openai_key(system_settings: dict | None = None) -> str | None:
    """VLM（クラウド）用の OpenAI API キー（取り込みは全体処理＝**中央設定**・per-user 設定は使わない）。

    A7（クラウドプロバイダ排他選択）: `sherpa.keys.resolve_api_key` 経由＝選択中のクラウド
    プロバイダが openai でなければ常に None（VLM のクラウド送信先も選択プロバイダのみに従う）。
    env は読まない（`sherpa/keys.py` 参照・初回シードのみ）。

    `system_settings`（省略可）: `_read_openai` が送信直前に1回だけ読んだスナップショットを渡す
    （省略時はここで自分で読む）。診断の要否を区別したい呼び出し元は `_openai_key_with_reason`
    を使う（本関数はそれの薄いラッパー・既存呼び出し元は挙動不変）。
    """
    key, _invalid = _openai_key_with_reason(system_settings)
    return key


def _openai_key_with_reason(system_settings: dict | None = None) -> tuple[str | None, bool]:
    """`_openai_key` と同じキー解決＋「不正な cloud_provider が原因で無効化したか」を返す。

    戻り値 `(key, invalid)`: `invalid=True` のときだけ、この呼び出しの中で診断ログ（下記）を
    実際に残している。呼び出し元（`resolve_vlm`）はこの bit を見て「詳細は直前のログを参照」の
    ような案内文言を、ログが実在する経路だけに限定する（未設定など他の理由で None になった
    場合にまで「直前のログを参照」と案内すると、参照先が無い誤案内になるため）。
    """
    from sherpa import keys
    # 意図しない課金の是正: `cloud_provider`（A7）が非空の不正値のときは黙って既定（openai）へ
    # 倒れたキーで画像を送信しない。既存契約（キー無し＝ None ＝送信 OFF）へ寄せる。
    try:
        key = (keys.resolve_api_key("openai", None, system_settings=system_settings, strict=True) or "").strip()
    except keys.InvalidCloudProviderConfigError as e:
        # 利用者向けにはしない（既存契約どおり None＝送信 OFF へ静かに縮退）が、呼び出し元
        # （`resolve_vlm`）の「OPENAI_API_KEY が未設定」という決め打ちの誤記録を避けるため、
        # 実際の理由を診断できるログをここで残す（strict 例外の黙殺の是正）。
        _log.warning("VLM._openai_key: cloud_provider が不正なため無効化しました: %s", e)
        return None, True
    return (key or None), False


def _cloud_allowed_now(system_settings: dict | None = None) -> bool:
    """**送信直前**に system_settings から生の cloud_allowed を読み直す（RV High #1・stale cfg 回避）。

    `resolve_vlm()`（convert() 開始時の1回きりの判定）の結果を `cfg` として持ち回ると、長い PDF ページループの
    処理中や `resolve_vlm()` を経由しない直接呼び出しで、管理者が cloud_allowed を取り消した後も古い許可のまま
    送信し続けうる。送信直前に毎回この関数で読み直すことで、次の送信から確実に反映される。store には短TTL
    （3秒）のプロセス内キャッシュがあるため I/O 負荷は無視できる。**store 到達不可/例外/型不正は False へ倒す**
    （fail-closed＝「許可されていない」として扱う）。

    `system_settings`（省略可）: 呼び出し側（`_read_openai`）が送信直前に1回だけ読んだスナップ
    ショットを渡すと、それを使う（この1回の送信判断の中で cloud_allowed と接続先/鍵を別々の
    読み直しで食い違わせない。省略時はここで自分で読む＝従来どおり）。
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


def _is_local_url(url: str) -> bool:
    """`url` のホストが**ループバック**（`localhost`／127.0.0.0/8／`::1`）または**私有アドレス帯**
    （RFC1918: 10.0.0.0/8・172.16.0.0/12・192.168.0.0/16）の **IP リテラル** か判定する（決定的・RV High #2）。

    社内 LAN の Ollama は IP 指定（例 `192.168.1.50`）なら許可（cloud_allowed 不要）。**`localhost` 以外の
    ドメイン名/ホスト名は DNS で外部へ解決されうるため不許可**（cloud_allowed=true が必要）。URL パース
    （`urllib.parse`）＋ `ipaddress` モジュールのみで判定し、ネットワーク I/O は行わない。URL 不正/ホスト欠落/
    非 IP ホスト名は False（fail-safe）。
    """
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False                                  # ホスト名（DNS 解決されうる）＝ローカル確証なし
    if ip.is_loopback:                                 # 127.0.0.0/8・::1
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _PRIVATE_V4_NETS)
    return False                                       # IPv6 の非ループバックは対象外（RFC1918 相当の判定なし）


def _vlm_usable_override() -> bool | None:
    """MCP サブプロセス向けスナップショット（env `SHERPA_VLM_USABLE`・`agents._mcp_env` が設定）。

    RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: MCP サブプロセスは PG creds を持たない（`_MCP_PASSTHROUGH`
    に非含）ため system_settings の `vlm`（cloud_allowed 等）を読めず、常に env/既定（ローカル ollama＝
    usable）にフォールバックしてしまう。親が `provider=openai・cloud_allowed=false`（実際は unusable）でも
    MCP 側は既定ローカルで usable と誤判定し、画像/PDF を convertible 扱いにする等の view 乖離が起きる。

    親プロセス（API リクエスト時点）が計算した `resolve_vlm() is not None` の1bit スナップショットを
    `SHERPA_VLM_USABLE`（`"1"`/`"0"`）として渡し、**設定されていれば最優先で信じる**（実際の provider/model/
    キー等の secrets は渡さない・変換自体は MCP では実行されない読み取り専用の view 整合が目的）。
    未設定/不正値は None（＝通常の system_settings/env 解決へフォールバック）。
    """
    raw = (os.environ.get("SHERPA_VLM_USABLE") or "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    return None


def resolve_vlm() -> dict | None:
    """**実際に使える** VLM 設定（provider/model/cloud_allowed/ollama_url）を返す（使えない/遮断は None）。

    - provider=openai → **cloud_allowed=true（管理者が明示許可）かつ OPENAI_API_KEY 有**のときだけ使える。
      cloud_allowed=false なら None を返す（＝無効扱い・画像を絶対に送らない・warning ログ）。
    - provider=ollama → **cloud_allowed=true、または接続先がローカル/私有アドレス帯**（`_is_local_url`）の
      ときだけ使える（RV High #2）。公開 IP・ドメイン名（`localhost` 以外）は DNS/経路で外部へ出うるため
      cloud_allowed が要る。URL 判定はネットワーク I/O を伴わない（URL パース＋ ipaddress のみ）ためここで
      判定してよい（接続そのものの実到達性は convert 時の fail-safe に委ねる）。

    ここでの判定は convert() 開始時点のスナップショット。長い PDF ページループ中の許可取り消しは、送信直前の
    `_cloud_allowed_now()` 再検査（`_read_openai`/`_read_ollama`）で別途拾う（belt-and-braces）。

    **`SHERPA_VLM_USABLE` が設定されていれば最優先**（MCP サブプロセスの view 整合スナップショット・
    `_vlm_usable_override`）: `"0"` は無条件で None、`"1"` は `vlm_config()` の生値をそのまま usable として返す
    （MCP は変換を実行しないため provider 個別のクラウド遮断ロジックは通らなくてよい）。
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
            # 「詳細は直前のログを参照」は _openai_key_with_reason() が実際に診断ログを残した
            # 経路（cloud_provider 不正）だけに限定する（未設定等の他の理由まで「直前のログを
            # 参照」と案内すると、参照先が無い誤案内になるため）。
            if _key_invalid:
                _log.warning("VLM: provider=openai・クラウド許可済みですが OpenAI キーを解決できないため"
                            "無効化します（詳細は直前のログを参照）。")
            else:
                _log.warning("VLM: provider=openai・クラウド許可済みですが OpenAI キーが未設定のため"
                            "無効化します（画像はクラウドへ送信しません）。")
            return None
    elif cfg["provider"] == "ollama":
        if not cfg["cloud_allowed"] and not _is_local_url(cfg["ollama_url"]):
            _log.warning("VLM: provider=ollama の接続先（%s）がローカル/私有アドレスと確認できず、クラウド許可も"
                         "無いため無効化します（画像は送信しません）。", cfg["ollama_url"])
            return None
    return cfg


def vlm_usable() -> bool:
    """VLM が実効的に使える設定か（未導入案内・convertible_exts・escalation・arms_sig 用・ネットワーク I/O なし）。"""
    return resolve_vlm() is not None


def sig_value(names) -> str:
    """arms_sig 用の VLM 署名値（RV Med #3: **実効可用性**ベース・クラウド許可・provider/model の変化で
    drift 再ビルドを誘発する）。

    `vision` が有効アームに無い、または**実際に使えない構成**（`resolve_vlm()` が None を返す＝
    openai でキー未設定・cloud_allowed 不足・ollama の接続先が非ローカルでクラウド許可も無い 等）→ `"none"`。
    使える構成のときだけ `"<provider>:<model>:cloud=<on|off>"`（provider/model・cloud_allowed の変更に加え、
    **OpenAI キーの追加/削除・Ollama 接続先の許可状態の変化でも drift を誘発する**＝旧実装は生の `vlm_config()`
    を使っており、キーだけ後から追加/削除しても署名が変わらず抽出源の変化を見逃していた）。
    エンジンの版は含めない（版更新での不要な全リビルドを避ける・ocr/legacy と同じ扱い）。
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
    """画像・スキャン PDF を VLM（視覚モデル）で読み取るアーム（既定ローカル Ollama・クラウドは管理者オプトイン）。"""
    name = "vision"

    def available(self) -> bool:
        return vlm_usable()

    def accepts(self, path) -> bool:
        ext = Path(path).suffix.lower()
        from .. import office_md
        if ext in office_md.IMAGE_EXT:
            return vlm_usable()                            # 画像は VLM だけで読める（ラスタ化不要）
        if ext == ".pdf":
            return office_md.pdf_escalation_target(path) == "vision"
        return False

    def convert(self, path) -> ArmResult | None:
        cfg = resolve_vlm()
        if cfg is None:
            return None                                    # 未設定/到達不可/クラウド遮断（fail-safe・warning は resolve 側）
        p = Path(path)
        ext = p.suffix.lower()
        from .. import office_md
        from sherpa import metering
        extra_notes: list[str] = []
        # S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 読み取り〜返却全体を acc スコープで
        # 囲み、`kind='vlm'` で1ファイル1行に集約（calls＝ページ/画像数）。user_id/world は付けない
        # （システムレベルの取り込み・uid が無い・world は office_md 経由で配線されておらずスコープ外）。
        metering.acc_begin()
        try:
            try:
                if ext in office_md.IMAGE_EXT:
                    text, pages = self._read_image(p, cfg), 1
                elif ext == ".pdf":
                    text, pages, extra_notes = self._read_pdf(p, cfg)
                else:
                    return None
            except Exception:                              # 想定外は未対応に倒す（fail-safe）
                _log.warning("VLM 視覚読み取りに失敗しました（%s）", p, exc_info=False)
                return None
            if not text or not text.strip():
                return None                                # 文字が取れない＝未対応（正直表示・失敗計上）
            md = _LABEL + "\n\n" + text.strip()
            notes = [f"vlm_provider={cfg['provider']}", f"vlm_model={cfg['model']}", "numeric_verified=false"]
            if ext == ".pdf":
                notes.append(f"pages={pages}")
            notes.extend(extra_notes)                      # タイムアウト予算切れの truncated 記録
            return ArmResult(md=md, method="vision", confidence=0.4, notes=notes)
        finally:
            tokens, n = metering.acc_end()
            if n:
                metering.record("vlm", cfg["provider"], cfg["model"], tokens, calls=n)

    def _read_image(self, image_path: Path, cfg: dict) -> str | None:
        """1 枚の画像を VLM で読み取る（失敗/空は None）。1 ファイル総予算＝画像1枚分の全時間。"""
        return _vlm_read(image_path, cfg, _vlm_timeout_sec())

    def _read_pdf(self, pdf_path: Path, cfg: dict) -> tuple[str | None, int, list[str]]:
        """テキスト層ゼロ PDF を PDFium でページ画像化→各ページ VLM 読み取り（ラスタ化は `raster` を共用）。

        返値 `(結合テキスト|None, 処理ページ数, 追加notes)`。**1 ファイル総予算**（`SHERPA_VLM_TIMEOUT`）: 各ページは
        残り時間で実行し、予算切れは残ページを打ち切って `truncated=pages N/M` を notes に残す（それまでの結果は返す）。
        ページ上限（`SHERPA_OCR_MAX_PAGES`）・ラスタ化ピクセル上限（`SHERPA_OCR_MAX_PIXELS`）は `raster` を共用。
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
    """1 画像あたりの base64 化サイズ上限 MB（env `SHERPA_VLM_MAX_IMAGE_MB`・不正/未設定は既定 20・RV Med #4）。"""
    raw = os.environ.get("SHERPA_VLM_MAX_IMAGE_MB")
    if not raw:
        return _DEFAULT_MAX_IMAGE_MB
    try:
        v = float(raw)
    except ValueError:
        return _DEFAULT_MAX_IMAGE_MB
    return v if v > 0 else _DEFAULT_MAX_IMAGE_MB


def _image_too_large(image_path: Path) -> bool:
    """画像ファイルサイズが上限を超えるか（RV Med #4・暴走/コスト防止）。

    stat 不可（存在しない等）は「超過ではない」扱い（呼び出し元の後続読み込みでどのみち失敗する・ここで
    fail-safe 側に倒す必要はない）。ページ画像化（`_read_pdf`）は既存のピクセル上限クランプ（`raster.
    _rasterize_page`）で通常この上限を超えないが、単独画像ファイル（accepts (a)）にはそのクランプが効かない
    ため、送信前にファイルサイズで一律ガードする（単独画像の事後縮小再エンコードは行わない＝実装の単純さを
    優先。上限超過は「未対応」として正直に表示する）。
    """
    try:
        size = Path(image_path).stat().st_size
    except OSError:
        return False
    return size > _max_image_mb() * 1024 * 1024


def _vlm_read(image_path: Path, cfg: dict, timeout: float) -> str | None:
    """1 枚の画像を VLM（provider 別）で読み取り、テキストを返す（失敗/到達不可は None・fail-safe）。"""
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
    """Ollama `/api/chat`（stream=false・images に base64）で画像1枚を読み取る（本文テキストを返す）。

    **cloud_allowed=false のときは接続先がローカル/私有アドレス帯（`_is_local_url`）であることを送信直前に
    毎回検証する**（RV High #2）。cloud_allowed は**送信直前に再読み**（`_cloud_allowed_now`・RV High #1・
    stale cfg 回避）＝`resolve_vlm()` は convert() 開始時の1回きりの判定のため、長い PDF ページループの途中で
    管理者が許可を取り消しても、次ページの送信直前チェックで確実に止まる（cfg 引数の cloud_allowed は見ない）。
    """
    url = cfg["ollama_url"]
    if not _cloud_allowed_now() and not _is_local_url(url):
        _log.warning("VLM(ollama): 接続先（%s）がローカル/私有アドレスと確認できず、クラウド許可も無いため"
                     "送信しません（fail-safe）。", url)
        return None
    from sherpa import llm, metering
    body = {"model": cfg["model"], "stream": False,
            "messages": [{"role": "user", "content": _VLM_PROMPT, "images": [_read_image_b64(image_path)]}]}
    # VLM 専用の接続先はこの呼び出しにだけ閉じて許可する（一般の Ollama 許可リスト
    # `llm._allowlisted_hosts()` へは加算しない＝個人 ollama_url の権限へ流用させない）。
    # 上の local/cloud_allowed チェックを既に通過済みの接続先なのでここでの追加許可は安全。
    vlm_hp = llm._canonical_host_port(url)
    resp = llm.post_json(llm.ollama_url(url, "/api/chat", extra_allowed={vlm_hp} if vlm_hp else None),
                         llm.JSON_HEADERS, body, timeout=max(1, int(timeout)))
    metering.acc_add(metering.usage_from_ollama_chat(resp))
    return ((resp or {}).get("message") or {}).get("content")


def _read_openai(image_path: Path, cfg: dict, timeout: float) -> str | None:
    """OpenAI Chat Completions（image_url に base64 data URL）で画像1枚を読み取る。

    **cloud_allowed を送信直前に system_settings から再読み**（`_cloud_allowed_now`・RV High #1）して許可
    されていない送信を防ぐ（stale cfg 回避・belt-and-braces）: `resolve_vlm()` 迂回の直接呼び出しや、長い
    PDF ページループの処理中に管理者が cloud_allowed を false へ戻したケースでも、次ページの送信直前チェック
    で確実に止まる（**`cfg["cloud_allowed"]` の値は見ない**＝古い許可のまま送り続ける穴を塞ぐ）。

    `system_settings` はこの関数の冒頭で1回だけ読み、cloud_allowed 判定・鍵解決・接続先
    （`llm.openai_url`/`openai_headers`）まで同じスナップショットで揃える（「送信直前に読み直す」
    という stale cfg 対策の意図は保ったまま＝`resolve_vlm()` の古い値を使い回すわけではない・1回の
    送信判断の中で3箇所が別々の読みで食い違うのを防ぐだけ）。
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
    # 送信は `llm.openai_post_json`（OpenAI 専用の送信直前ガード付き・`post_json` は Gemini/Ollama
    # とも共用のため一律遮断しない）。
    resp = llm.openai_post_json(llm.openai_url("chat/completions", system_settings=sys_s),
                         llm.openai_headers(key, system_settings=sys_s), body, timeout=max(1, int(timeout)))
    metering.acc_add(metering.usage_from_openai_chat(resp))
    choices = (resp or {}).get("choices") or []
    if not choices:
        return None
    return (choices[0].get("message") or {}).get("content")


# ---- 補足観測（L8・§8.2）: canonical が読めない画像要素だけを VLM で観測する ----------------------
# ここまでの `VisionArm.convert()` は「文書/画像**全体**を vision が単独担当する」経路（① OOXML/
# ④ pdf_text が使えない入力の全面代替）。ここから下は別の役割で、① OOXML が既に読めている文書の
# **一部**（picture 等・OOXML は画素の中身を持たない＝coverage=metadata_only）だけを補足する。
# 出力先も別（`ai_observation.AIObservationSet` として Canonical Evidence と分離保存・原値を
# 上書きしない・office_md がこの Set を `evidence_render.render(observation_set=...)` へ渡す）。
VISION_OBSERVATION_PROMPT_SCHEMA_VERSION = "vision-observation-image-v1"
VISION_OBSERVATION_PREPROCESSING_PROFILE = "embedded-asset-original-v1"
# 単独ソースの画像内容観測（canonicalに競合する値が無い＝上書きの懸念がない）に対する固定confidence。
# `ai_observation.MIN_ANSWER_CONFIDENCE`（0.70）以上にして`use_for_answer=True`を許可し、rag.mdの
# AI観測レコードとして出す（既存の使用可否ルールはそのまま・値だけ本用途向けに定義）。全体変換時の
# `convert()` の confidence=0.4（他アームの代替という別文脈）とは意図的に別値。
VISION_OBSERVATION_CONFIDENCE = 0.75


def build_asset_observations(
    ir: evidence_ir.EvidenceIR,
    *,
    decisions: list[ocr_router.OCRRouteDecision],
    asset_root: Path,
) -> ai_observation.AIObservationSet | None:
    """canonical が読み取れない画像要素（`metadata_only`/`image_content_uninterpreted` 等）を
    VLM で補足観測する（第二アーム観測の一般化・OCR の Set 契約と同じ器に乗る）。

    `decisions` は呼び出し側（`office_md`）が `ocr_router.build_manifest()` で選定済みの
    ``status == "selected" かつ input_kind == "asset"`` の候補に限定して渡すこと——ここでは
    再判定しない（router の決定＝「読む/読まない」を尊重し、二重の意味推定をしない）。
    ``page_render``（PDF ページ全体のスキャン救済）は対象外のまま渡ってきても無視する
    （PDF 全体の視覚読み取りは `VisionArm.convert()` 単独の担当・二重コストを避ける）。

    VLM が実効利用不可（`resolve_vlm()` が None）なら None を返す（既定コストゼロ・呼び出し元は
    `evidence_render.render(observation_set=None)` と同じ挙動になる）。1画像の読み取り失敗/空応答は
    その画像だけ黙ってスキップする（1件の失敗が資料全体の観測生成を止めない・`convert()` の
    fail-safe と同じ思想）。全候補が失敗/空なら None を返す。
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
