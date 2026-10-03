"""rag.md の各レコード本文を LLM で読みやすく成形する（規則版へのフォールバック付き）。

LLM が書き換えてよいのは各レコード（`<!-- chunk:{chunk_id} -->` の直後から次のアンカー直前まで）の
本文だけ。アンカー行・可視性/状態の key-value 行・`出所:` 行・「」で囲まれた原値は変えない。
機械検証で破れを検知したレコードは規則版のまま残す。
① 取り込み（sync）は規則版を即時生成し、`stamp_rule_only()` で `生成手段: 規則` を刻むだけ（LLM は呼ばない）。
② LLM 成形は取り込み後に背景で実行する（`worker.py` が `run_world_pass()` を呼び、`schedule_background()` が
   資料フォルダ単位の多重起動を防ぐ）。
③ 結果はレコード本文の内容ハッシュでキャッシュし（`ir/` 層の JSON）、同一内容では LLM を呼ばない。
プロバイダ選定・送信は `graph_extract.available()`/`complete_json()` を再利用する（用途セルは `render`・本文テキストのみ送る）。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Callable

from .. import json_io, worlds
from . import text_kind

_log = logging.getLogger("sherpa")

# ---- トグル解決（system_settings の `rag_llm_render`・既定 off）----------------------------
_DEFAULT_ON = "off"
_KNOWN_TOGGLES = ("on", "off")


def _system_toggle() -> str | None:
    """system_settings の `rag_llm_render`（"on"/"off" の非空文字列のみ）。読めない/未設定は None（既定へ倒す）。"""
    try:
        from .. import store
        val = store.get_system_settings().get("rag_llm_render")
    except Exception:
        return None
    if isinstance(val, bool):          # boolean も解釈する
        return "on" if val else "off"
    if isinstance(val, str) and val.strip():
        return val.strip().lower()
    return None


def rag_llm_render_enabled() -> bool:
    """実効トグル（system_settings > 既定 off）。未知の値は既定へ倒す。

    ON でも LLM が解決できない構成では `available()` が None を返し、`run_world_pass()` は何もせず戻る。
    """
    effective = _system_toggle()
    if effective is None:
        effective = _DEFAULT_ON
    if effective not in _KNOWN_TOGGLES:
        return _DEFAULT_ON == "on"
    return effective == "on"


def env_default_enabled() -> bool:
    """system_settings を無視した既定の実効値（設定画面の「未設定に戻すと何になるか」表示用・常に既定 off）。"""
    return _DEFAULT_ON == "on"


# ---- LLM 設定解決 ---------------------------------------------------------------------------

def available(settings: dict | None = None) -> dict | None:
    """rag.md 成形に使う LLM 設定（無ければ None）。`graph_extract.available()` を `usage="render"` で再利用する。

    廃止済み・不正な cloud_provider は `strict=True` で None にする。`render` 未設定の環境は
    `model_catalog._USAGE_FALLBACK` により旧 `extract` セルの解決結果を使う。
    """
    from . import graph_extract
    from .. import keys as _keys
    try:
        return graph_extract.available(settings, strict=True, usage="render")
    except _keys.InvalidCloudProviderConfigError:
        return None


_PROMPT_VERSION = "rag-llm-render-v2"
_TIMEOUT = 60

_SYS_PROMPT = (
    "あなたは社内検索用ドキュメントの1レコードを、意味・値を一切変えずに自然で読みやすい日本語へ"
    "整えるだけの編集者です。出力は次のスキーマの JSON オブジェクトのみ（前後に文章を付けない）: "
    '{"text": "整形後の本文（複数行は\\nを含める）"}\n'
    "制約（いずれか1つでも破ると採用されません）:\n"
    "(1) 「」で囲まれた値は一字一句そのまま text に含める（省略・要約・言い換え・削除を禁止）。\n"
    "(2) 次のいずれかで始まる行は、入力に存在すれば text 内にそのままの1行として必ず含める"
    "（削除・言い換え禁止）: 出所: / 可視性: / 状態: / 重なり: / 取り消し線: / 背面図形: / "
    "前面図形: / シートの可視性: 。\n"
    "(3) 入力に無い新しい事実・数値・固有名詞を作らない。推測を書かない。\n"
    "(4) 「参考情報」が付いている場合、それは同一文書内の AI 画像観測であり原本の確定値ではない。"
    "レコード本文（対象レコードの記述）を読みやすくする文脈理解にのみ使い、参考情報の内容を"
    "新しい事実として text 本文へ書き込まない。"
)


def _cache_key(cfg: dict, body: str, auxiliary: str = "") -> str:
    return hashlib.sha1(
        f"{cfg.get('provider')}|{cfg.get('model')}|{_PROMPT_VERSION}|{body}|{auxiliary}".encode("utf-8")
    ).hexdigest()


# ---- 保護行・原値の機械検証 -------------------------------------------------------------------

_PROTECTED_LINE_PREFIXES = (
    "出所: ", "可視性: ", "状態: ", "重なり: ", "取り消し線: ", "背面図形: ", "前面図形: ",
    "シートの可視性: ",
)
_QUOTED_VALUE_RE = re.compile(r"「([^」]*)」")


def _validate(original_body: str, candidate: str) -> bool:
    """成形結果が契約を破っていないか（保護行の逐語一致・「」原値の完全保持）。破れていれば False（規則版のまま残す）。"""
    if not isinstance(candidate, str) or not candidate.strip():
        return False
    candidate_lines = {line.rstrip() for line in candidate.splitlines()}
    for line in original_body.splitlines():
        stripped = line.rstrip()
        if stripped.startswith(_PROTECTED_LINE_PREFIXES) and stripped not in candidate_lines:
            return False
    for value in _QUOTED_VALUE_RE.findall(original_body):
        if value and value not in candidate:
            return False
    return True


# ---- rag.md のレコード分割（アンカー間の本文＝LLM が書き換えてよい唯一の部分）--------------------

_ANCHOR_RE = re.compile(r"^<!-- chunk:(\S+) -->$", re.MULTILINE)
_GENERATION_METHOD_RE = re.compile(r"^生成手段: .*\n?", re.MULTILINE)
_PROFILE_LINE_RE = re.compile(r"^変換プロファイル: .*$", re.MULTILINE)

# AI観測レコードの本文はこの行で始まる（`kind` を持たないため本文マーカーで識別する）
_AI_OBSERVATION_BODY_MARKER = "AI画像観測（原本確定値ではない）"
# フロー図レコード（Mermaid コード）は決定的成果物＝成形対象外（`evidence_render.FLOW_DIAGRAM_BODY_MARKER`）
_FLOW_DIAGRAM_BODY_MARKER = "フロー図（機械生成・Mermaid）"
# メタファイル図から取り出した文字（元の値）も成形対象外
_FIGURE_TEXT_BODY_MARKER = "図の中の文字（元の値）"


def _is_ai_observation_body(body: str) -> bool:
    return body.startswith(_AI_OBSERVATION_BODY_MARKER)


def _is_machine_artifact_body(body: str) -> bool:
    """LLM 成形の対象外（AI観測・フロー図・図の中の文字）か。"""
    return body.startswith((_AI_OBSERVATION_BODY_MARKER, _FLOW_DIAGRAM_BODY_MARKER, _FIGURE_TEXT_BODY_MARKER))


def _leading_chrome(block: str) -> str:
    """アンカー直後の見出し類（`## .../### .../原本領域: ...`・空行）を、該当しない最初の行まで消費する。"""
    length = 0
    for line in block.splitlines(keepends=True):
        text = line.rstrip("\n")
        if text == "" or text.startswith("## ") or text.startswith("### ") or text.startswith("原本領域: "):
            length += len(line)
            continue
        break
    return block[:length]


def _split_records(markdown: str) -> tuple[str, list[dict]] | None:
    """`(header, records)`。`records[i]` は `{"anchor", "chrome", "body", "trailing"}`。

    `body` が LLM の書き換え対象。連結して元の markdown と一致しなければ None（この文書の成形を丸ごと見送る）。
    """
    matches = list(_ANCHOR_RE.finditer(markdown))
    if not matches:
        return None
    header = markdown[: matches[0].start()]
    records: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        block = markdown[start:end]
        chrome = _leading_chrome(block)
        rest = block[len(chrome):]
        body = rest.rstrip("\n")
        trailing = rest[len(body):]
        records.append({"anchor": m.group(0), "chrome": chrome, "body": body, "trailing": trailing})
    rebuilt = header + "".join(r["anchor"] + r["chrome"] + r["body"] + r["trailing"] for r in records)
    if rebuilt != markdown:
        return None
    return header, records


def stamp_rule_only(markdown: str) -> str:
    """sync 時（規則版のみ）に `生成手段: 規則` を刻む。`変換プロファイル:` 行の直後に入れ、無ければ先頭に入れる。"""
    line = "生成手段: 規則\n"
    m = _PROFILE_LINE_RE.search(markdown)
    if not m:
        return line + markdown
    insert_at = m.end() + 1 if markdown[m.end():m.end() + 1] == "\n" else m.end()
    return markdown[:insert_at] + line + markdown[insert_at:]


def _set_generation_method(markdown: str, line: str) -> str:
    new_text, n = _GENERATION_METHOD_RE.subn(line, markdown, count=1)
    return new_text if n else markdown


def needs_llm_pass(markdown: str) -> bool:
    """この rag.md がまだ LLM 成形の対象か。`生成手段: 規則`（未成形・行なしを含む）のときだけ True。

    `生成手段: LLM(...)＋規則` は成形済みとして扱い、レコード単位の再試行はしない。
    """
    m = re.search(r"^生成手段: (.*)$", markdown, re.MULTILINE)
    if not m:
        return True
    return m.group(1).strip() == "規則"


# ---- world 単位キャッシュ（ir/ 層）--------------------------------------------------------------

_CACHE_FILENAME = "_llm_render_cache.json"


def _cache_path(world: str) -> Path:
    return worlds.derived_ir_dir(world) / _CACHE_FILENAME


def _load_cache(world: str) -> dict:
    raw = json_io.read_json(_cache_path(world))
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return dict(entries) if isinstance(entries, dict) else {}


def _save_cache(world: str, entries: dict) -> None:
    try:
        if entries:
            json_io.write_json_atomic(_cache_path(world), {"entries": entries})
        else:
            _cache_path(world).unlink()
    except OSError:
        pass


def clear_cache(world: str) -> None:
    """LLM 成形キャッシュを空にする（`rag_llm_render_enabled` に関係なく動く・「規則版で再生成」操作用）。"""
    try:
        _cache_path(world).unlink()
    except OSError:
        pass


# ---- 1文書の成形 ----------------------------------------------------------------------------

class DocResult:
    __slots__ = ("markdown", "changed", "llm_count", "visited_keys")

    def __init__(self, markdown: str, changed: bool, llm_count: int, visited_keys: set[str]):
        self.markdown = markdown
        self.changed = changed
        self.llm_count = llm_count
        self.visited_keys = visited_keys


def format_document(world: str, rel: str, markdown: str, cfg: dict, cache: dict) -> DocResult | None:
    """1文書の rag.md（`生成手段: 規則`）を LLM 成形する。

    ① レコードごとにキャッシュヒットならそれを採用する。
    ② キャッシュに `invalid`（過去の検証失敗）があれば規則版のまま再送しない。
    ③ それ以外は LLM を1回呼び、`_validate()` を通れば採用してキャッシュへ書く。呼び出しが例外なら
       キャッシュせず次回に再試行する。
    戻り値 `None` はレコード分割を信用できない文書（丸ごと見送り）。
    """
    parsed = _split_records(markdown)
    if parsed is None:
        _log.warning(
            "rag.md のレコード分割に失敗したため LLM 成形を skip します（規則版のまま）: world=%s rel=%s",
            world, rel)
        return None
    header, records = parsed
    from . import graph_extract
    llm_count = 0
    visited_keys: set[str] = set()
    for index, record in enumerate(records):
        original_body = record["body"]
        if _is_machine_artifact_body(original_body):
            # AI観測・フロー図レコードは成形対象外（改変もキャッシュもしない）
            continue
        # 直後のレコードが AI観測なら、補足観測として補助文脈へ渡す
        auxiliary = (
            records[index + 1]["body"]
            if index + 1 < len(records) and _is_ai_observation_body(records[index + 1]["body"])
            else None
        )
        key = _cache_key(cfg, original_body, auxiliary or "")
        visited_keys.add(key)
        cached = cache.get(key)
        if isinstance(cached, dict):
            status = cached.get("status")
            if status == "ok" and isinstance(cached.get("text"), str):
                record["body"] = cached["text"]
                llm_count += 1
                continue
            if status == "invalid":
                continue                                  # 既知の検証失敗＝呼び直さない・規則版のまま
        user_prompt = "次のレコードを整形してください:\n\n" + original_body
        if auxiliary:
            user_prompt += (
                "\n\n---\n参考情報（同一文書内のAI画像観測・原本確定値ではない。"
                "文脈理解にのみ使い、新事実として書き込まないこと）:\n" + auxiliary
            )
        try:
            raw = graph_extract.complete_json(_SYS_PROMPT, user_prompt, cfg, timeout=_TIMEOUT)
            data = json.loads(raw)
            candidate = data.get("text") if isinstance(data, dict) else None
        except Exception:
            _log.warning(
                "LLM 成形の呼び出しに失敗しました（規則版のまま・次回パスで再試行）: world=%s rel=%s",
                world, rel, exc_info=True)
            continue                                      # キャッシュしない＝次回再試行
        if isinstance(candidate, str) and _validate(original_body, candidate):
            cache[key] = {"status": "ok", "text": candidate}
            record["body"] = candidate
            llm_count += 1
        else:
            cache[key] = {"status": "invalid"}
    reassembled = header + "".join(
        r["anchor"] + r["chrome"] + r["body"] + r["trailing"] for r in records)
    method_line = (
        f"生成手段: LLM（{cfg.get('provider')}/{cfg.get('model')}）＋規則（LLM成形 {llm_count} 件）\n"
        if llm_count else "生成手段: 規則\n"
    )
    final_markdown = _set_generation_method(reassembled, method_line)
    return DocResult(
        markdown=final_markdown, changed=final_markdown != markdown,
        llm_count=llm_count, visited_keys=visited_keys)


# ---- world 単位の背景パス ---------------------------------------------------------------------

class RunResult:
    def __init__(self) -> None:
        self.docs_scanned = 0
        self.docs_changed = 0
        self.llm_records = 0
        self.changed_rels: list[str] = []
        self.provider: str | None = None
        self.model: str | None = None


def run_world_pass(world: str, *, settings: dict | None = None) -> RunResult:
    """資料フォルダ配下の未成形（`生成手段: 規則`）な `.rag.md` を LLM 成形する（背景処理の本体）。

    トグル OFF／LLM 未接続ならファイル I/O なしで即 return する。多重起動の抑止は `schedule_background()`、
    ES への反映（`.rag_sig` の無効化・再索引・確定）は呼び出し元（`worker.py`）が `changed_rels` を見て行う。
    ① 開始時の `last_sig` を保存し、`office_md.drop_rag_sig_marker` で `.rag_sig` を未確定に落とす。
    ② 書込の直前に `store.world_lock` 内で現行 `last_sig` と照合し、不一致なら書込を破棄してパスを打ち切る
       （キャッシュ剪定の `_save_cache` も呼ばない）。LLM 呼び出し中は world_lock を持たない。
    ③ `metering.acc_begin()`/`acc_end()` でパス全体を囲み、実際に LLM 応答を得たときだけ
       `kind='rag_render'` の集約1行を記録する（`user_id` は付けない）。
    """
    result = RunResult()
    if not rag_llm_render_enabled():
        return result
    cfg = available(settings)
    if not cfg:
        return result
    result.provider = cfg.get("provider")
    result.model = cfg.get("model")
    rag_dir = worlds.derived_rag_dir(world)
    if not rag_dir.exists():
        return result
    from .. import metering, store
    from . import office_md
    saved_sig = (store.get_world(world) or {}).get("last_sig")
    # 開始前に一度だけ `.rag_sig` を未確定に落とす（失敗しても続行する）
    office_md.drop_rag_sig_marker(worlds.derived_md_dir(world))
    metering.acc_begin()
    try:
        cache = _load_cache(world)
        visited: set[str] = set()
        aborted = False
        for rag_path in sorted(rag_dir.rglob("*.rag.md")):
            try:
                rel = rag_path.relative_to(rag_dir).as_posix()[: -len(".rag.md")]
                if text_kind.is_sensitive_doc_id(rel):
                    # 秘匿名の rag.md は LLM（外部 API）へ送らない
                    continue
                text = rag_path.read_text(encoding="utf-8")
            except OSError:
                continue
            result.docs_scanned += 1
            if not needs_llm_pass(text):
                continue
            doc_result = format_document(world, rel, text, cfg, cache)
            if doc_result is None:
                continue
            visited |= doc_result.visited_keys
            if doc_result.changed:
                with store.world_lock(world):
                    cur_sig = (store.get_world(world) or {}).get("last_sig")
                    if cur_sig != saved_sig:
                        _log.warning(
                            "LLM 成形の書込を破棄しパスを打ち切りました（世代が変わったため）: "
                            "world=%s rel=%s", world, rel)
                        aborted = True
                        break
                    try:
                        json_io.write_text_atomic(rag_path, doc_result.markdown)
                        result.changed_rels.append(rel)
                        result.docs_changed += 1
                        result.llm_records += doc_result.llm_count
                    except OSError:
                        _log.warning(
                            "LLM 成形版 rag.md の書込に失敗しました（次回パスで再試行）: world=%s rel=%s",
                            world, rel, exc_info=True)
        if not aborted:
            pruned = {k: v for k, v in cache.items() if k in visited}
            _save_cache(world, pruned)
    finally:
        tokens, n = metering.acc_end()
        if n:
            metering.record("rag_render", cfg["provider"], cfg["model"], tokens, world=world, calls=n)
    return result


# ---- 多重起動抑止（world 単位・単一 worker 前提） ------------------------------------------------

_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


def schedule_background(world: str, work_fn: Callable[[str], None]) -> bool:
    """同一 world の LLM 成形が実行中でなければ daemon thread で起動する（多重起動抑止）。

    実行中なら何もせず False（合流や待機はしない——呼び出し元〔`worker.sync()`〕は sync のたびに
    毎回呼ぶ想定で、取りこぼしても次回 sync が再度契機になり収束する）。`work_fn` はこの thread の
    中で `world` を引数に1回呼ばれる（例外は握って警告ログのみ・呼び出し元プロセスを落とさない）。
    """
    with _RUNNING_LOCK:
        if world in _RUNNING:
            return False
        _RUNNING.add(world)

    def _runner() -> None:
        try:
            work_fn(world)
        except Exception:
            _log.warning("LLM 成形の背景実行が失敗しました: world=%s", world, exc_info=True)
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(world)

    threading.Thread(target=_runner, daemon=True, name=f"sherpa-rag-llm-render-{world}").start()
    return True


def is_running(world: str) -> bool:
    """この world の LLM 成形が in-process レジストリ上で「実行中」か（テスト/診断用）。"""
    with _RUNNING_LOCK:
        return world in _RUNNING
