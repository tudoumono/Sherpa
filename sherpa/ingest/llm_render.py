"""rag.md の LLM 成形＋規則フォールバック（D2・§8.3/§8.6・
`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`）。

各レコード（`<!-- chunk:{chunk_id} -->` アンカーの直後から次のアンカー直前まで）の本文を LLM で
読みやすく成形する。**LLM が書き換えてよいのは各レコードの本文テキストだけ**——アンカー行そのもの・
アンカーの数と順序、可視性・廃止の key-value 行（`可視性:`/`状態:`/`重なり:`/`取り消し線:`/
`背面図形:`/`前面図形:`/`シートの可視性:`）、`出所:` 行、「」で囲まれた原値（数値・識別子・名称）は
1バイトも変えない。機械検証で破れを検知したら **その record は規則版のまま**（fail-closed）。

契機は2段（§8.6-4）:
1. 取り込み（sync）は規則版を**即時**生成する。本モジュールが sync 経路で行うのは
   `stamp_rule_only()` による `生成手段: 規則` の申告だけ（LLM は呼ばない・検索は直後から動く）。
2. LLM 成形は取り込み後に**バックグラウンドで後追い**実行する（`worker.py` が `run_world_pass()` を
   daemon thread で呼ぶ・`schedule_background()` が world 単位の多重起動を防ぐ）。

LLM 生成は**レコード本文の内容ハッシュでキャッシュ**する（`ir/` 層の world 単位 JSON・
`es_index._embed_cached` と同じ「現存分だけに剪定する鏡」の流儀）。同一内容の再取込は LLM を
呼ばない。プロバイダ選定・送信は `graph_extract.available()`/`complete_json()` をそのまま再利用する
——`available()` は GRAPH-SRC（2026-09-04）で旧・意味層フル抽出が撤去された後の、取り込み
パイプライン向け LLM 呼び出しの共通配管として残る。model_catalog の独立用途セル `render` を使う
（「設定駆動・テキストのみ送信」経路・OpenAI へファイルは送らない）。
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

_log = logging.getLogger("sherpa")

# ---- トグル解決（system_settings > 既定 on・`legacy_convert.py` の流儀と同型・
#      env フォールバックは UI 昇格に伴い ENV-CLEAN で撤去済み）--------------------------------

# 既定 OFF（2026-09-05 裁定）: test2 実測で成形の実効果が「です・ます化・空行削除・ラッパー剥がし」の
# 文体整形のみと判明（情報の追加ゼロ・値の欠落ゼロ）。1ファイル約3.5円/38秒＝1万ファイル外挿で
# 3.5万円/直列100時間級はコストに見合わない。実文書で構造回復の実益が実測できたら再裁定する。
_DEFAULT_ON = "off"
_KNOWN_TOGGLES = ("on", "off")


def _system_toggle() -> str | None:
    """system_settings の `rag_llm_render`（"on"/"off"の非空文字列のみ）。読めない/未設定は None
    （既定へ倒す・`legacy_convert._system_legacy_backend` と同じ fail-safe）。"""
    try:
        from .. import store
        val = store.get_system_settings().get("rag_llm_render")
    except Exception:
        return None
    if isinstance(val, bool):          # 旧版が書いた boolean を黙って無視しない（意図保持）
        return "on" if val else "off"
    if isinstance(val, str) and val.strip():
        return val.strip().lower()
    return None


def rag_llm_render_enabled() -> bool:
    """実効トグル（system_settings > 既定 off）。未知の値は既定へ倒す（fail-safe）。

    ON でも LLM が解決できない構成（キー未設定・閉域等）では `available()` が None を返し、
    `run_world_pass()` はファイル I/O 無しで即 return する＝「解決できない構成では自然に何も
    起きない」契約と両立する。
    """
    effective = _system_toggle()
    if effective is None:
        effective = _DEFAULT_ON
    if effective not in _KNOWN_TOGGLES:
        return _DEFAULT_ON == "on"
    return effective == "on"


def env_default_enabled() -> bool:
    """system_settings を無視した既定の実効値（設定画面の「未設定に戻すと何になるか」表示用・
    `legacy_convert.env_default_backend` と同型・env フォールバックは撤去済み＝常に既定 off）。"""
    return _DEFAULT_ON == "on"


# ---- LLM 設定解決 ---------------------------------------------------------------------------

def available(settings: dict | None = None) -> dict | None:
    """rag.md 成形に使う LLM 設定（無ければ None）。`graph_extract.available()` をそのまま再利用する
    （`strict=False`＝管理者が明示選択したプロバイダの構成不備でも例外化しない。本処理は背景処理で
    利用者への即時エラー通知が不要なため、常に「自然に何も起きない」側へ倒す）。

    `usage="render"`（model_catalog の独立カタログ用途・L5 残課題の是正／GRAPH-SRC 2026-09-04 で
    `USAGES` の一級市民に）: 旧・意味層フル抽出（`extract` 用途）を撤去した後もモデル解決が壊れない
    よう、`render` 未設定の環境は `model_catalog._USAGE_FALLBACK` の後方互換読み取りで旧 `extract`
    セルの解決結果をそのまま使い続ける（管理者が明示的に `render` を設定すればそちらに従う）。
    """
    from . import graph_extract
    from .. import keys as _keys
    try:
        return graph_extract.available(settings, strict=False, usage="render")
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
    """成形結果が契約を破っていないか（保護行の逐語一致・「」原値の完全保持）。
    破れていたら False（呼び出し側はこの record を規則版のまま残す＝fail-closed）。
    """
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


# ---- rag.md のレコード分割（アンカー間の本文＝LLM が書き換えてよい唯一の部分） --------------------

_ANCHOR_RE = re.compile(r"^<!-- chunk:(\S+) -->$", re.MULTILINE)
_GENERATION_METHOD_RE = re.compile(r"^生成手段: .*\n?", re.MULTILINE)
_PROFILE_LINE_RE = re.compile(r"^変換プロファイル: .*$", re.MULTILINE)

# AI観測レコード（`evidence_render._ai_observation_records`）の本文は必ずこの行で始まる
# （`kind="ai_observation"`・`record_keys` を持たないため見出しは付かず、body の先頭行が
# そのままこの固定文言になる）。llm_render は record を rag.md の平文からしか見ないため、
# 構造化された `kind` の代わりにこの本文マーカーで観測レコードを識別する。
_AI_OBSERVATION_BODY_MARKER = "AI画像観測（原本確定値ではない）"
# L9 のフロー図レコード（Mermaid コード＝決定的成果物）。AI観測と同じく成形対象外——
# 保護行検証は固定 prefix 行と「」原値しか見ないため、Mermaid フェンス内は素通しになり、
# ここでスキップしないと LLM 成形が図のコードを書き換えうる（evidence_render.FLOW_DIAGRAM_BODY_MARKER）。
_FLOW_DIAGRAM_BODY_MARKER = "フロー図（機械生成・Mermaid）"


def _is_ai_observation_body(body: str) -> bool:
    return body.startswith(_AI_OBSERVATION_BODY_MARKER)


def _is_machine_artifact_body(body: str) -> bool:
    """LLM 成形の対象外（生の記録・決定的成果物）か。AI観測とフロー図の両マーカーを束ねる。"""
    return body.startswith((_AI_OBSERVATION_BODY_MARKER, _FLOW_DIAGRAM_BODY_MARKER))


def _leading_chrome(block: str) -> str:
    """アンカー直後の見出し類（`## .../### .../原本領域: ...`・空行）を、最初の非該当行まで
    貪欲に消費する。`evidence_render._markdown` がこの並びでしか見出しを出さないため、
    「先頭から該当パターンが続く限り」で record 本文（`markdown_text`）と決定的に切り分けられる。
    """
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

    `body` が LLM の書き換え対象（`record["markdown_text"]` と同一）。`chrome`（見出し類）・
    `trailing`（次アンカー直前までの空行）は非対象。header+全record（anchor+chrome+body+trailing）
    を連結して元の markdown と完全一致することを確認し、不一致なら None を返す（想定外の形式は
    安全側でこの世代の LLM 成形を丸ごと skip する＝parse を信用できない文書には手を出さない）。
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
    """sync 時（規則版のみ）に `生成手段: 規則` を刻む（§8.3-1・rag.md は必ずこの申告を持つ）。
    `変換プロファイル:` 行（`_markdown()` が必ず出す固定行）の直後へ挿入する。見つからない
    （想定外の形式）場合は先頭へ挿入する——生成手段の申告を欠かさない方を優先する。
    """
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
    """この rag.md がまだ LLM 成形の対象か。`生成手段: 規則`（未成形・行不在も含む＝fail-open で
    対象に含める）のみ True。`生成手段: LLM(...)＋規則` は既に settled（§8.3-3「後退しない」の裏で、
    一度混在に達した文書は record 単位の個別再試行をしない設計・再試行させたければ「規則版で
    再生成」で明示的に一掃してから次回 sync に委ねる）。
    """
    m = re.search(r"^生成手段: (.*)$", markdown, re.MULTILINE)
    if not m:
        return True
    return m.group(1).strip() == "規則"


# ---- world 単位キャッシュ（ir/ 層・`_embed_cached` と同じ鏡剪定） --------------------------------

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
    """LLM 成形キャッシュを強制的に空にする（`rag_llm_render_enabled` に関係なく・
    「規則版で再生成」管理操作専用・§8.6-2）。"""
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
    """1文書の rag.md（規則版・`生成手段: 規則`）を LLM 成形する。

    record ごとに: キャッシュヒットならそのテキストを採用（LLM 不呼び出し）。キャッシュに
    `invalid`（過去に検証失敗）が記録済みならそのまま規則版を維持し再送しない。それ以外は
    LLM を1回呼び、`_validate()` を通れば採用してキャッシュへ書く。呼び出し自体が例外（ネット
    ワーク/認証/クォータ等の一時的失敗）を送出した場合は**キャッシュしない**（次回パスで再試行・
    fail-closed の定石）。

    戻り値 `None`＝この文書はレコード分割を信用できない（`_split_records` 失敗）＝丸ごと skip。
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
            # AI観測レコード（生の記録）とフロー図レコード（決定的なMermaid）は LLM成形の対象外
            # （内容を改変しない・キャッシュもしない）。
            continue
        # 直後のrecordがAI観測なら、対応する画像要素の補足観測として補助文脈へ渡す
        # （`_ai_observation_records`のsort_keyが対象要素の直後に置く設計に依拠・§8.2 D3）。
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
    """world 配下の未成形（`生成手段: 規則`）な `.rag.md` を LLM 成形する（後追い背景処理の本体・
    §8.6-4）。トグル OFF／LLM 未接続なら**ファイル I/O 無しで**即 return（「自然に何も起きない」）。

    world 単位の多重起動抑止は `schedule_background()` が担う（ここは単発実行のみを行う）。
    ES への反映（`.rag_sig` の無効化・再索引・確定）は呼び出し元（`worker.py`）が
    `changed_rels` を見て行う——本関数はファイル書込までの責務に留める。

    **世代競合の是正**（2026-09-05・rv-oom-resume item5）: 本関数は LLM 呼び出しを含み長時間
    （実測 1ファイル約38秒）かかりうるため `store.world_lock` を通し取りしない（ファイル書込ごとに
    握る・docstring 下部参照）——その間に rebind/削除/register で world の世代（`last_sig`）が
    変わると、パス開始時点で読んだ（古い世代の）本文から成形した結果を、既に別世代になった
    `derived_rag_dir` へ書き戻してしまう穴があった。パス開始時点の `last_sig` を保存し、
    **書込直前**に `store.world_lock` 内で現行 `last_sig` と再照合する——不一致ならその書込を
    破棄し、以降の書込も同じ理由で無効になりうるためパス自体を打ち切る（キャッシュ剪定＝
    `_save_cache` も呼ばない——`visited` が新世代に対して不完全なまま保存すると、まだ見ていない
    新世代の record のキャッシュを誤って刈ってしまうため）。

    **`.rag_sig` マーカーの是正**（同上）: 従来はこのパス自体が `.rag_sig` に一切触れず、
    呼び出し元（`worker._reindex_after_rag_rewrite`）がパス**完了後**に初めて落とす／確定する
    だけだった——パス実行中（ファイルを書き終えた後・`_reindex_after_rag_rewrite` に到達する前）に
    プロセスが落ちると、ディスク上の rag.md は既に LLM 成形版になっているのに `.rag_sig` は
    旧確定値のまま残り、`rag_sig_drift()` が偽（未変化）と誤判定して ES が永久に旧本文のまま
    取り残される（自己修復の効かない穴）。本関数の書込開始**前**（1回だけ）に
    `office_md.drop_rag_sig_marker` でマーカーを未確定へ落としておく——`.rag_sig` 自体は
    `_reindex_after_rag_rewrite` が ES 反映成功後に確定する既存の保留方式（提案書の順序どおり）に
    そのまま乗る（本関数はここでは確定し直さない＝反映の成否を確認できるのは呼び出し元だけ）。

    M1: `metering.acc_begin()`/`acc_end()` でこの1回のパス全体（world 内の全文書・全 record）を
    囲み、実行1回につき集約1行を `kind='rag_render'` で記録する（計測有効時のみ・
    「world 単位パス＝1回の意味のある呼び出し単位」の粒度）。
    `format_document` の呼び出しは `graph_extract.complete_json` を再利用しており、これが実際に
    HTTP 応答を得たときだけ `metering.acc_add` する——record 本文がキャッシュヒットのみで LLM を
    1回も呼ばなければ `acc_end()` の `n` は 0 のままで `record()` は呼ばれない（実費用が無い呼び出し
    は計上しない）。背景処理（`worker.py` からの起動）に利用者コンテキストは無いため `user_id` は
    付けない（`vision_arm.VisionArm.convert` の VLM 計測と同じ理由）。
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
    # パス開始前に一度だけ落とす（ES 反映前の保留方式・失敗はベストエフォートで続行——
    # 本関数自身は ES に触れないため、drop 失敗は「自己修復の安全網が今回だけ弱い」に留まる）。
    office_md.drop_rag_sig_marker(worlds.derived_md_dir(world))
    metering.acc_begin()
    try:
        cache = _load_cache(world)
        visited: set[str] = set()
        aborted = False
        for rag_path in sorted(rag_dir.rglob("*.rag.md")):
            try:
                rel = rag_path.relative_to(rag_dir).as_posix()[: -len(".rag.md")]
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
