"""`CodexProvider`（`sherpa/agents.py` から re-export される exec 核）。

Codex CLI サブプロセスの起動・思考イベントへの変換・実行ごとの作業領域管理・headline/progress 判定など、
Codex(gpt-5.5) を頭脳にする実行本体一式をまとめる。`sherpa/agents.py` が facade として本モジュール
から再エクスポートするため、まだ agents.py に残る `_select_provider`/`get_provider`/`provider_info`
（`AGENT_PROVIDERS`・`_UnwiredProvider` も同様）は無改修で動く。

**同時実行は uid 単位で直列化しない**: 実行ごとに専用の作業領域（`sandbox._safe_run_authoring` の
`authoring/run-<乱数>`）を割り当てるため、同一 uid の複数実行が snapshot・files/ move・
`.agents` rebuild で交差する心配が無い。同時実行数はチャットの受付上限（`chat_turns` 側・別契約）
だけで決まる。

**`CodexProvider.run`/`_run_authoring` は分割しない**: SSE 生成器の try/finally が唯一の
クリーンアップ保証（`run_dir` 後始末＝`_run_authoring` 本体を包む frame・attempt ループの finally＝
`_killpg`→`proc.wait(5)`・その外側の finally＝非永続セッションのみ `shutil.rmtree(codex_home)`／
永続セッションは `config.toml`・`auth.json` の削除）のため、関数を丸ごと移し生成器フレームを
分割するヘルパ抽出はしない。`'ws_authoring' in dir()`（台帳登録ゲート・フレーム内省が必要）、
last-message tempfile の `unlink` 2箇所（ask_user 早期 return・通常経路）もこの制約に従う。

**本モジュールは `sherpa` から2階層深い（providers→codex）**ため、パスの `parents[N]` は
agents.py 基準の N から +2 する（`sherpa/` 配下基準＝`parents[2]`・`_SKILLS_BASE`／repo root 基準＝
`parents[3]`・`sandbox.py` docstring 参照）。相対 import も
`from ... import marp_render`／`from ... import store as _store`／
`from ... import codex_agents_md, codex_skills` になる（参照先は変わらず
`sherpa.marp_render`/`sherpa.store`/`sherpa.codex_agents_md`/`sherpa.codex_skills`）。

**`_gather` は `_run_authoring` 内でのみ遅延 import する**（危険な継ぎ目）: `tests/unit/
test_agents_seams.py`・`tests/unit/test_agents_author.py::
test_gather_seam_intercepted_by_codex_provider` 等が `agents._gather` を monkeypatch して
`CodexProvider().run()`（→`_run_authoring`）経由の介入を検証する。本モジュールは agents.py が
facade re-export のためモジュールレベルで import するため、逆にモジュールレベルで
`from sherpa import agents` すると循環 import になる。そのため `_run_authoring` 内でのみ関数内
遅延 import `from sherpa import agents as _facade` して `_facade._gather(ctx)` と実行時解決する。
`CodexProvider.run`/`_run_authoring` が呼ぶ `_plain_run`・`_node`・`.sandbox` の各関数等、本モジュール内の
他の呼び出しは直接（`_plain_run`/`_node`/`_usage_meta`は base.py から直接 import）でよい
（危険な継ぎ目リストに無い）。

依存: `..base`（`Provider`/`Ctx`/`_log`/`_node`/`_plain_run`/`_usage_meta`）・`..prompts`
（`_facts`/`_kb_hint_abs`）・同一パッケージの `.sandbox`（サンドボックス/Marp バイナリ検出/
web_search 引数/authoring config 書込み）・`.mcp`（MCP env/config/neighbors/ask_user 変換）は
兄弟モジュールとして直接 import する（危険な継ぎ目リストに無い）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import threading
from pathlib import Path
from typing import Iterator

from ... import codex_agents_md, codex_skills, model_catalog
from ... import depth_profile as depth_profile_mod
from ... import layer as layer_mod
from ..base import Ctx, Provider, _log, _log_chat_usage, _node, _plain_run, _usage_meta, _verified_sources
from ..prompts import _facts, _kb_hint_abs
from .citations import parse_referenced_doc_lines, verified_referenced_docs
from .mcp import (
    _apply_codex_neighbors,
    _codex_ask_capture,
    _codex_mcp_enabled,
    _graph_schema_era_from_item,
    _mcp_config_args,
    _mcp_env,
    _mcp_neighbors_from,
)
from .sandbox import (
    _codex_clean_env,
    _codex_sandbox_enabled,
    _detect_chrome_path,
    _direct_read_roots,
    _enumerate_sensitive,
    _kb_read_roots,
    _marp_bin,
    _openai_endpoint_kind,
    _release_active_run_dir,
    _remove_dir_best_effort,
    _safe_codex_sessions_home,
    _safe_run_authoring,
    _safe_workspace_authoring,
    _scope_deny_entries,
    _venv_root,
    _web_search_c_args,
    _web_search_endpoint_note,
    _write_codex_authoring_config,
)

# 明示変更(a): skills_base（危険地雷1の5番目）。本モジュールは `sherpa` から2階層深い
# （providers→codex）ため、agents.py 基準の `Path(__file__).resolve().parent`（＝<repo>/sherpa）と
# 同じ場所を指すには `parents[2]` にする（モジュール docstring 参照）。
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"

# `--output-schema` に渡す固定スキーマファイル（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-1）。
# パッケージ内に同梱（本モジュールと同じディレクトリ）。
_OUTPUT_SCHEMA_PATH = Path(__file__).resolve().parent / "output_schema.json"


def _humanize_cmd(command: str):
    """Codex が実行したシェルコマンド → 画面の言葉＋実コマンド（detail）。"""
    inner = command
    m = re.search(r'-lc\s+"(.*)"\s*$', command) or re.search(r"-lc\s+'(.*)'\s*$", command)
    if m:
        inner = m.group(1)
    low = inner.lower()
    if "grep" in low or low.startswith("rg ") or " rg " in low:
        label = "ファイルを検索（grep）"
    elif any(k in low for k in ("cat ", "sed ", "head ", "tail ", "less ", "nl ")):
        label = "ファイルを参照"
    elif low.startswith(("ls", "find")) or " find " in low:
        label = "ファイル一覧"
    else:
        label = "コマンド実行"
    return label, inner.strip()[:140]


def _masked_run_dir_path(fp: str, run_dir: Path) -> str:
    """失敗ログに `users_dir`/uid を含むフルパスをそのまま出さない（サーバのファイルシステム配置・
    uid をログに露出させない）。`run_dir` からの相対部分だけを、run_dir 自身の識別子
    （`run-<乱数>`＝uid を含まない）に付けて返す。相対化できない（run_dir 外のパス等）場合は
    run_dir の識別子だけを返す。"""
    try:
        rel = Path(fp).resolve().relative_to(run_dir.resolve())
        return f"{run_dir.name}/{rel}"
    except (OSError, ValueError):
        return run_dir.name


def _usage_from_turn_completed(event: dict, model: str | None, *, codex_model_provider: str | None = None,
                               system_settings: dict | None = None) -> dict | None:
    """Codex `codex exec --json` の `turn.completed` イベントから usage を取り出す。

    実ログ形: `{"type":"turn.completed","usage":{"input_tokens":..,"cached_input_tokens":..,
    "output_tokens":..,"reasoning_output_tokens":..}}`。usage が無い/型不正なら None（best-effort）。

    `codex_model_provider`/`system_settings`: 呼び出し元（`_run_authoring`）が
    `self._ollama_base_url is not None` から求めた `"ollama"`/`"openai"` と、接続先解決用の
    設定スナップショットをそのまま渡す契約（`agent_constructs.is_local` の4値判定
    （local/on_prem/cloud/cloud_compat・接続先ホストの判定は `llm.endpoint_locality`）へ委ねる・
    Codex は常に `provider_id="codex"` を名乗るため、実際の接続先はここでしか分からない）。
    """
    if not isinstance(event, dict) or event.get("type") != "turn.completed":
        return None
    u = event.get("usage")
    if not isinstance(u, dict):
        return None
    from ... import agent_constructs
    return _usage_meta("codex", model,
                       input_tokens=u.get("input_tokens"),
                       cached_input_tokens=u.get("cached_input_tokens"),
                       output_tokens=u.get("output_tokens"),
                       reasoning_output_tokens=u.get("reasoning_output_tokens"),
                       is_local=agent_constructs.is_local("codex", codex_model_provider=codex_model_provider,
                                                          system_settings=system_settings))


def _accumulate_codex_usage(prev: dict | None, new: dict | None) -> dict | None:
    """自動継続で attempt をまたいだときの usage＝**最新の snapshot** を採用する（足し合わせない）。

    Codex CLI の `turn.completed.usage` はセッション累計（`last_total_token_usage.total`）で、
    `codex exec resume` はロールアウトから前回までの累計を復元してから加算する。継続 attempt の値は
    前 attempt の分を既に含むため、足すと二重計上になる。`None`（usage 無し）の attempt は無視する。
    """
    return prev if new is None else new


def _killpg(proc) -> None:
    """MCP subprocess / shell child まで確実に殺す（creds env の寿命を延ばさない）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_stop_watcher(proc, stop_event) -> "threading.Thread":
    """途中停止: `for line in proc.stdout` はブロッキング read のため、
    `stop_event` を単にチェックするだけでは（次の行が来るまで）反応できない。別スレッドで stop_event を
    監視し、立ったら即 `_killpg` で子プロセスごと殺す＝stdout を EOF にしてブロック中の read を
    即座に解放する（サブプロセスを安全に打ち切る唯一の確実な方法・EventSource.close() はサーバ側の
    ブロッキング処理を止めない＝調査済）。

    プロセスが自然終了した場合はスレッドも自分で抜ける（`proc.poll()` を短間隔でポーリング・daemon
    なのでプロセス全体の終了も妨げない）。呼び出し側（`CodexProvider.run`）は生成したスレッドを
    明示的に join する必要はない（自然終了/kill いずれでも自己終結する）。
    """
    def _watch(_proc=proc, _ev=stop_event):
        while _proc.poll() is None:
            if _ev.wait(timeout=0.3):
                _killpg(_proc)
                return
    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


_LAST_MESSAGE_MAX_BYTES = 256 * 1024   # 最終メッセージの保険読取は上限付き（256KB）


def _read_last_message_fallback(path: Path) -> str | None:
    """§3: `-o <path>` で Codex が書く最終メッセージファイルを読む（`--json` の
    `agent_message` 抽出が空だった時の保険）。無い/空/読取失敗は None（呼び出し側は既存の
    決定的回答フォールバックへ委ねる）。ファイルの削除は呼び出し側の責務（ここでは行わない）。

    `.tmp/` は authoring 配下（Codex の書込対象）＝サブプロセスや将来の
    変更で symlink が紛れ込む余地を否定できないため、`O_NOFOLLOW` で symlink を拒否（TOCTOU の無い
    アトミックな判定）・通常ファイルのみ・サイズ上限つきで読む（巨大ファイル/デバイスファイル等を
    誤って answer に取り込まない）。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0 or st.st_size > _LAST_MESSAGE_MAX_BYTES:
            return None
        data = os.read(fd, st.st_size)
    except Exception:
        return None
    finally:
        os.close(fd)
    txt = data.decode("utf-8", errors="replace").strip()
    return txt or None


# ---- 回答 headline の選び方（進行中の作業宣言を見出しにしない）----
# Codex は調査中に「これから〜する」という進行形の作業宣言を agent_message として複数回出すことがあり、
# run が途中終了すると **最後に届いた作業宣言**（実例:「…根拠の有無を切り分けます」）が
# env["headline"] になってしまう（結論でなく本文途中の一文が見出しに出る）。LLM を使わず決定的に、
# 「結論を含む最後の agent_message」を優先し、末尾の作業宣言を落として選ぶ。
# 注: 語尾は「これから調べる」という**次アクション動詞**に限定する（curated list）。汎用の「〜します」
# 全部を弾くと所見（「波及します」「影響します」等）まで落ちて結論を消してしまうため入れない。
_PROGRESS_VERBS = (
    "確認します", "切り分けます", "調べます", "特定します", "検討します", "探します",
    "洗い出します", "整理します", "確かめます", "突き止めます", "チェックします", "見ていきます",
    "精査します", "分析します", "追います", "たどります", "把握します", "収集します", "集めます",
    "比較します", "検証します", "調査します", "確認していきます", "見ます",
)
_PROGRESS_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _PROGRESS_VERBS)) + r")[。.!！\s]*$")
# 語尾が作業宣言でも「単文の事実記述」（例:「NIGHTLY は税率マスタを起動時に
# 確認します。」）を progress と誤判定して結論を捨てないよう、判定を絞る。手順マーカー（これから何をやる、
# という順序表現）で始まる文は明確に作業宣言。
_PROGRESS_MARKERS = (
    "まず", "次に", "続いて", "これから", "今から", "この後", "最後に", "では", "それでは",
)
# 「調べてから伝える」型の宣言語尾（「これから関連資料を確認し、結果を報告します」）。手順マーカーで
# 始まる文に限って次アクション扱いにする。`_PROGRESS_VERBS` には入れない: 結論の末尾文
# （「影響範囲は夜間バッチのみであることを共有します」）を `_trim_trailing_progress` が落としてしまう。
_REPORT_BACK_VERBS = ("報告します", "お伝えします", "まとめます", "回答します", "共有します")
_REPORT_BACK_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_BACK_VERBS)) + r")[。.!！\s]*$")
# 報告系語尾に付けるマーカーからは「最後に」を除く: 「最後に、影響は夜間バッチのみであることを共有します」
# は結論の締めであって次アクションではない。
_REPORT_BACK_MARKERS = tuple(m for m in _PROGRESS_MARKERS if m != "最後に")


def _is_next_action_sentence(s: str) -> bool:
    """文が次アクション宣言か（作業宣言語尾・または手順マーカー付きの「結果を報告します」型）。"""
    return bool(_PROGRESS_END_RE.search(s)) or (
        s.startswith(_REPORT_BACK_MARKERS) and bool(_REPORT_BACK_END_RE.search(s)))


def _is_progress_only(text: str) -> bool:
    """text の全ての文が次アクション宣言（作業を『これからやる』）なら True＝結論文が1つも無い。

    句点/改行で文に割り、いずれも進行形の作業宣言で終わることが前提。そのうえで High-1（RV）:
    「単文の事実記述」を巻き込まない（＝新しい方の message を残すのを安全側とする）ため、
    (a) いずれかの文が手順マーカー（まず/次に/…）で始まる、または (b) 文が2つ以上ある、
    のいずれかを満たすときだけ「作業宣言だけの message」とみなす。単文・マーカー無しは False。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return True
    if not all(_is_next_action_sentence(s) for s in sents):
        return False
    return any(s.startswith(_PROGRESS_MARKERS) for s in sents) or len(sents) >= 2


def _trim_trailing_progress(text: str) -> str:
    """単一段落（改行なし）の平文に限り、末尾の連続する作業宣言文を落として結論で締める。

    改行や箇条書き（Markdown）を含む場合は構造を壊さないためそのまま返す（②の安全側）。
    末尾を削って空になる（＝全部が作業宣言）の場合も元文を返す（呼び出し側の _is_progress_only 判定で
    別 message が選ばれるため通常ここには来ないが、保険）。
    """
    if "\n" in text:
        return text
    parts = [p for p in re.findall(r"[^。]*。|[^。]+$", text) if p.strip()]
    while len(parts) > 1 and _PROGRESS_END_RE.search(parts[-1].strip()):
        parts.pop()
    return "".join(parts).strip() or text


def _pick_codex_headline(completed: list[str], partial: str = "") -> str:
    """集めた複数の agent_message から headline を決定的に選ぶ（LLM 不使用）。

    ①結論を含む最後の message を優先（末尾が作業宣言でも、その中の結論／それ以前の結論を拾う）。
    ②その message の末尾に連なる作業宣言文は落とす（`_trim_trailing_progress`）。
    ③どの message も作業宣言だけなら、最後の message をそのまま返す（本文先頭＝best effort）。
    `partial`＝item.updated だけ来て item.completed が来なかった未完 message（打ち切り時の保険）。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return ""
    for m in reversed(msgs):
        if not _is_progress_only(m):
            return _trim_trailing_progress(m)
    return msgs[-1]


# 「作業報告＋次アクション」型の途中経過（例:「現在、関連資料を確認しました。次に影響範囲を調べます。」）。
# 完了形の作業報告は `_is_progress_only` では結論文に見えるため、明示的な次アクション文を伴い、
# 残りの文が全部この完了形の作業報告なら途中経過とみなす（自動継続の判定専用・見出しの選び方は変えない）。
# 語尾は「調べ終えた作業の報告」に限定する curated list（「〜であることを確認しました」のような
# 所見も巻き込むが、次アクション文を伴う時だけ効くので、続けさせて損は無い）。
_REPORT_VERBS = (
    "確認しました", "確認済みです", "調べました", "調査しました", "特定しました", "把握しました",
    "整理しました", "洗い出しました", "検索しました", "取得しました", "読みました", "精読しました",
    "収集しました", "集めました", "検証しました", "比較しました", "分析しました", "チェックしました",
    "たどりました", "追いました", "見ました", "見つけました", "確かめました",
)
_REPORT_END_RE = re.compile(
    "(?:" + "|".join(map(re.escape, _REPORT_VERBS)) + r")[。.!！\s]*$")


def _is_report_with_next_action(text: str) -> bool:
    """全文が「完了形の作業報告」または「次アクション宣言」で、**両方を少なくとも1文ずつ**含む。

    次アクション宣言だけの message は `_is_progress_only` の領分（単文・マーカー無しの「〜を確認します」は
    事実記述の可能性があるため、そちらでは意図的に結論扱い）。ここで作業報告文も必須にするのは、
    その単文事実記述をこの規則で拾い直して結論を続行させないため。
    """
    sents = [s.strip() for s in re.split(r"[。\n]+", text) if s.strip()]
    if not sents:
        return False
    has_next = any(_is_next_action_sentence(s) for s in sents)
    has_report = any(_REPORT_END_RE.search(s) and not _is_next_action_sentence(s) for s in sents)
    if not (has_next and has_report):
        return False
    return all(_is_next_action_sentence(s) or _REPORT_END_RE.search(s) for s in sents)


def _needs_continuation(completed: list[str], partial: str = "") -> bool:
    """集めた agent_message が1件以上あり、それらを1本に連結したテキストが途中経過（作業宣言だけ・
    または作業報告＋次アクション）で結論文が1つも無いなら True。

    まず message 単位で保護する——**単文・手順マーカー（`_PROGRESS_MARKERS`）で始まらない・
    `_PROGRESS_END_RE` に合う** message（例:「NIGHTLY は税率マスタを起動時に確認します。」のような
    単文の事実記述）が1つでもあれば、連結を待たず False（結論あり）とする。`_is_progress_only` の
    単文保護と同じ判定を、連結前の各 message にも及ぼす（連結すると他の message の語尾に埋もれて
    見落とすため）。

    それ以外は**全 message を連結**して判定する——「資料を確認しました」（作業報告）と
    「次に調べます」（次アクション）が別 message に分かれていても、連結すれば
    `_is_report_with_next_action` が拾える（message 単位の判定だと前者が単文の結論扱いになり
    見落とす）。作業宣言だけの message は `_pick_codex_headline` が規則③（最後の1件をそのまま返す）
    に落ちる条件と同じ（空/空白のみの message は対象外・1件も無ければ False＝別経路（silent failure
    等）に任せる）。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return False
    for m in msgs:
        sents = [s.strip() for s in re.split(r"[。\n]+", m) if s.strip()]
        if (len(sents) == 1 and not sents[0].startswith(_PROGRESS_MARKERS)
                and _PROGRESS_END_RE.search(sents[0])):
            return False
    joined = "\n".join(msgs)
    return _is_progress_only(joined) or _is_report_with_next_action(joined)


_CONTINUE_PROMPT = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて最終回答（結論と根拠）を書いてください。"
)
# 出力スキーマ有効時（`_schema_on`）だけ使う継続プロンプト——AGENTS.md が構造化応答（`status`／`answer`／
# `next_step`）を求めているのはスキーマ有効時だけなので、継続の催促もその語彙に合わせる（§2-3）。
_CONTINUE_PROMPT_SCHEMA = (
    "続けてください。途中経過の報告ではなく、調査を最後まで進めて `status` を `final` にした"
    "最終回答（結論と根拠）を書いてください。ただし全件・一覧・すべての依頼で対象範囲の確認が"
    "終わっていなければ `final` にせず、`in_progress` のまま `next_step` に残りを書いてください。"
)

# ---- 出力スキーマ（`--output-schema`・docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3）----
# `_OUTPUT_SCHEMA_PATH` の3キーちょうど（strict・additionalProperties: false）と対応させる。
_STRUCTURED_KEYS = {"status", "answer", "next_step"}
_STRUCTURED_STATUSES = {"final", "in_progress"}


def _parse_structured(text: str | None) -> dict | None:
    """`--output-schema` で固定した3キー JSON かどうかを検証する（純関数）。

    キー集合の完全一致・`status` の値・`answer`/`next_step` の型が全て合うときだけ dict を返す。
    それ以外（構文エラー・途中で切れた JSON・キーの過不足・不正な型・不正な status 値）は None——
    呼び出し側はこれを「未完了」として扱う（壊れた JSON を平文ヒューリスティックへ戻さない・§2-3）。
    """
    if not text:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _STRUCTURED_KEYS:
        return None
    # `in` は set の要素比較にハッシュ化を要る——`status` が list/dict 等の非 hashable 値だと
    # `not in _STRUCTURED_STATUSES` 自体が TypeError で落ちる（壊れた入力を None に丸めるはずが
    # 例外で伝播してしまう）。先に str 型を確認してから集合照合する。
    _status = obj.get("status")
    if not isinstance(_status, str) or _status not in _STRUCTURED_STATUSES:
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    _next = obj.get("next_step")
    if _next is not None and not isinstance(_next, str):
        return None
    return obj

# 成果物の move／台帳登録に1件でも失敗したとき、回答本文の末尾に付ける固定文
# （`_created_files_failed` 判定・headline がどの分岐で組み立てられていても一律に付く）。
_CREATED_FILES_FAILURE_NOTE = "（作成したファイルの一部を保存できませんでした。管理者に確認してください）"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """env の整数解析（`agentic_search._env_int` と同型・循環 import 回避のため独立実装）。

    範囲 [lo, hi] 外・非整数は既定値へ戻す（既定値自体も [lo, hi] にクランプ）。呼び出し側
    （`_run_authoring`）が実行のたびに呼ぶため、`monkeypatch.setenv` 後の値にも追随する。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# 永続 CODEX_HOME（`.codex-sessions/{conversation_id}`）は同一会話の複数ターンにまたがって
# 同じ固定パスを共有する。同一会話の2実行が重なると、config.toml の
# unlink→再作成が競合し（直前の unlink は「今すぐ空いている」ことしか保証せず、もう一方の
# プロセスの書込と時間的に競合しうる＝O_EXCL は排他にならない）、別ターンの world/layer/MCP 設定
# で起動しうる・session JSONL への同時書込・終了時の finally が相手の config.toml/auth.json を
# 巻き添えで削除しうる。run dir（実行ごとに専用ディレクトリ）とは別に、conversation_id をキーに
# した非ブロッキング lock で「同一会話の永続 CODEX_HOME を使う実行」だけを直列化する（別会話・
# 非永続セッションは対象外＝互いに無関係な run dir／使い捨て CODEX_HOME を使うため衝突しない）。
_CONVERSATION_LOCKS: dict = {}
_CONVERSATION_LOCKS_GUARD = threading.Lock()


def _conversation_lock(conversation_id) -> threading.Lock:
    with _CONVERSATION_LOCKS_GUARD:
        lk = _CONVERSATION_LOCKS.get(conversation_id)
        if lk is None:
            lk = _CONVERSATION_LOCKS[conversation_id] = threading.Lock()
        return lk


class CodexProvider(Provider):
    """Codex(gpt-5.5) を**エージェント中核**に（設計どおり）。

    取得（Neo4j/grep）は本物のツールで実行しつつ、**Codex 自身も原文を grep/参照で裏取り**する。
    Codex の **実コマンド実行（grep 等）・推論・回答**を `--json` から拾い **1つずつ思考ノードに流す**
    （ユーザは Codex の作業を逐次見られる）。失敗/未導入は決定的回答にフォールバック。
    既定 reasoning=low（`SHERPA_CODEX_REASONING` で変更可。RV依頼の xhigh とは別運用）。
    調べる深さ（調べ方ブロック §3.2）が「深く」「最大」のとき、ターンごとに high/xhigh へ
    per-turn 上書きする（`_prompt_mcp`/`_prompt` 呼び出し直前の `_reason` 計算箇所を参照）。
    """
    label, model = "Codex", "gpt-5.5"
    provider_id = "codex"

    def __init__(self, reasoning: str | None = None, model: str | None = None,
                web_search: bool | None = None, ollama_base_url: str | None = None,
                openai_api_key: str | None = None, system_settings: dict | None = None):
        self._reason = reasoning or os.environ.get("SHERPA_CODEX_REASONING", "low")
        # チャットの Codex モデルは選択可（RV/委譲の固定運用とは別）。argv `-m` に渡すので
        # 先頭ハイフン/空白/制御文字/過大長は弾く（flag 混同・不正値の防止）。
        # `model_catalog.CODEX_MODEL_NAME_RE` を使う（`sherpa/model_catalog.py::validate_catalog` が
        # 管理者カタログへ課す文法と同じパターン＝管理画面で保存できるモデル名と揃える）。
        # 未指定（None/空文字）だけを既定 "gpt-5.5" へ解決する。
        # **不正な非空値**（grandfather された旧値・破損 DB・接続確認の直接入力等）は黙って
        # 別モデルへ置換しない＝ honest failure として `InvalidModelNameError`（`ValueError` の
        # サブクラス）を送出する（呼び出し側 `sherpa/providers/__init__.py::_select_provider` が
        # モデル名専用のこの型だけを捕捉し `_UnwiredProvider` として正直に失敗を伝える）。
        # 表示したモデルと実行モデルが食い違う事故を防ぐ。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            raise model_catalog.InvalidModelNameError(f"不正な Codex モデル名です: {model!r}")
        self.model = model or "gpt-5.5"
        # §5-1: ユーザーの希望（設定 codex_web_search）。実際に効くかは管理者フラグ次第
        # （_web_search_disabled_value が admin 許可と AND する）。
        self._web_search = bool(web_search)
        # Codex(Ollama) 構成（`agent_constructs`）のとき、Codex CLI を Ollama へ向ける接続先。
        # None＝Codex(OpenAI)＝従来どおり Codex の既定プロバイダ（OpenAI）を使う。
        # 値は `providers/__init__.py::_select_provider` が SSRF ガード（llm.assert_ollama_url_allowed）
        # を通してから渡す＝ここでは検証済みの前提。
        self._ollama_base_url = ollama_base_url or None
        # Codex(OpenAI) 構成で、接続先が既定(api.openai.com)以外
        # （Azure 等）にリダイレクトされている時**だけ** `_select_provider` が解決して渡す（それ以外は
        # 常に None のまま＝既定の Codex(OpenAI)・Codex(Ollama) は無改修・回帰ゼロ）。カスタム
        # model_provider（`sandbox._openai_compat_provider_lines`）は `env_key` で子プロセスの env から
        # キーを読む設計のため、この構成の時だけ `_codex_clean_env` にこの値を渡して env に注入する
        # （既定は引き続き auth.json 経由・env にキーを置かない現行方針を維持）。
        self._openai_api_key = openai_api_key or None
        # `_select_provider` が key/model 解決に使ったのと同じ system_settings スナップショットを、
        # config.toml 生成（`_write_codex_authoring_config`）・web_search 注記
        # （`_web_search_endpoint_note`）へもそのまま渡す。省略時（`None`）は従来どおり呼び出しごとに
        # `llm.py` が都度読み直す。
        self._system_settings = system_settings
        # 既定は空（`run()` を経由せず `_prompt`/`_prompt_mcp` を直接叩くテスト向けの安全な
        # フォールバック・`_history` は `run()` 冒頭で `ctx.history` から設定し直される）。
        self._history: list = []

    def _history_block(self) -> str:
        """直前ターンの履歴を Codex プロンプトへ前置するテキスト（会話継続）。

        `self._history` が空なら空文字列を返す＝呼び出し側の出力は従来と完全同一になる。
        """
        if not self._history:
            return ""
        lines = [f"{'ユーザー' if h.get('role') == 'user' else 'アシスタント'}: {h.get('content', '')}"
                for h in self._history]
        return "【直前の会話（参考・新しいものが下）】\n" + "\n".join(lines) + "\n\n"

    def _prompt(self, message, lens, env, world):
        sys = (self.system_prompt + "\n\n") if self.system_prompt else ""   # 回答方針（#2）を前置
        # cwd が workspace/authoring/ のため KB パスは絶対パスで渡す。
        # §2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（質問固有部分のみここに残す）。
        # ただし containment/grounding（KB 以外を読まない・確定と推定を分ける）は
        # AGENTS.md 書込失敗時（fail-open）でも消えないよう、短縮形をここにも常置する（多層防御・
        # AGENTS.md と重複しても害はない＝独立性を優先）。
        # 探す対象（層フィルタ）が限定されているターンは、この直接 grep 経路（MCP 無効時）自体を
        # 呼び出し元（_run_authoring）が実行しない契約——ここはプロンプト指示による迂回可能な
        # ソフト制御を持たない（正典 §3.4「範囲と同じ硬いフィルタ」・MCP 経由のときだけ実行する）。
        base = (
            "あなたは社内ナレッジ調査エージェントです。以下の資料フォルダ"
            f"（{_kb_hint_abs(world)}）を **grep やファイル参照で実際に調べてください**。"
            "Excel/Word/PowerPoint/PDF は Python（openpyxl・python-docx・python-pptx・pdfplumber）で"
            "開いて読んでよい。"
            "**指定資料フォルダ以外は読まない。確定した事実と推定は分けて書く**（詳細ルールは AGENTS.md）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
            # この経路（MCP 無効・直接 grep/ファイル参照）で読んだ資料も
            # 同様に、回答末尾の固定書式で Sherpa（citations.parse_referenced_doc_lines）に出典（原本DL）へ
            # 変換させる。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。"
        )
        if lens == "author":
            # author は回答でなく成果物ファイルを作る。
            return sys + base + (
                "調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。下の『参考（構造化済みの事実）』は補助に使ってよいが、"
                "件数・対象名は事実のまま。最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")
        return sys + base + (
            "ユーザの質問に答えてください。"
            "下の『参考（構造化済みの事実）』は補助に使ってよいが、件数・対象名は事実のまま。\n\n"
            # 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
            f"{self._history_block()}【質問】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")

    def _prompt_mcp(self, message, lens, world, direct_read: bool = True, layer=None):
        """MCP 版プロンプト。事実を前渡しせず、Codex に MCP ツールで自律調査させる。
        §2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（ここは MCP ツール固有の使い分け
        ＋ containment/grounding の短縮形を常置＝AGENTS.md 書込失敗時の多層防御）。

        `direct_read`（既定 True・提案書 2026-09-10-Codex原本直読と調査スキル §2-4）: 原本直読
        （permission profile で KB／派生ルートを read し、範囲は兄弟 deny・秘匿は個別 deny で表したうえで
        コードインタープリターで直接開く）の可否。`_run_authoring` が秘匿列挙と範囲（`_scope_deny_entries`）
        の成否から計算して渡す——失敗した（fail-closed）ターンだけ False（MCP のみへ縮退）。省略時
        （既存呼び出し・単体テスト）は True＝現行の主経路（直読可）を案内する。"""
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        _read_block = (
            "**原本は直接読んでよい（読取専用・指定された資料フォルダと派生フォルダの中だけ・"
            "秘匿名のファイル（.env／鍵／credentials 等）は読まない）。"
            # 主従は決めない——「まず読取ツールで原本を読む→
            # 突合・集計など定型外だけ Python」の順。毎回 Python を書かせない＝トークンと
            # 実行時間を削り、再現性を上げる。
            "まず読取ツール（xlsx_sheets／xlsx_range／docx_paragraphs／pptx_slides／"
            "pdf_pages／file_head）で原本を読む。複数ファイルの突合・集計など定型外の作業"
            "だけ Python（openpyxl・python-docx・python-pptx・pdfplumber。集計は pandas）"
            "で開く。テキスト・コードはそのまま読んでよい。"
            "派生 MD／rag.md は補助。台帳・検索・グラフ・出典の確定は MCP ツールで行う。"
            # 直読した資料は MCP の結果に載らず出典（原本DL）に
            # 自動では出ない——回答末尾に固定書式の行を書かせ、Sherpa（citations.parse_referenced_doc_lines）
            # が台帳で実在確認したものだけ出典へ昇格する。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。**"
            # 質問の型に合う調査スキルへ誘導する（「まずツールで
            # 当たりを付ける→原本の中身を確かめる」の順を具体化した手順書。読ませても直読不許可の
            # ターン（direct_read=False）ではノイズ＝else 側には入れない）。
            "**質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う"
            " `.agents/skills` の investigate-* スキルを読んで、その手順（ツールで当たり→"
            "原本の中身を確かめる→答える）どおりに進める。**"
            if direct_read else
            "**今回は原本の直接読み取りは使えない。資料の本文は MCP のツールで読む（KB 外は読まない）。**"
        )
        # 層（探す対象）は Codex に強制しない（直読は層に関係なく read）——限定されたターンだけ案内する。
        _layer_block = {
            "docs": "探す対象として資料（設計書・仕様書などのドキュメント）が指定されている＝直読でも資料を優先して見る。",
            "code": "探す対象としてソース（プログラム・JCL・コピーブック）が指定されている＝直読でもソースを優先して見る。",
        }.get(layer, "")
        base = (
            "あなたは社内ナレッジ調査エージェントです。MCP サーバ『sherpa』のツール"
            "（list_docs＝文書台帳の一覧/件数／ripgrep_search＝全文grep／glob_search＝ファイル名パターン／"
            "doc_outline＝見出し構造／read_doc＝通読（続きは start_line）／read_around＝周辺精読／"
            "graph_neighbors＝関係グラフの関連部品／es_search＝日本語全文検索／"
            "xlsx_sheets＝Excelのシート一覧／xlsx_range＝Excelのセル範囲／"
            "docx_paragraphs＝Wordの段落・表／pptx_slides＝PowerPointのスライド／"
            "pdf_pages＝PDFのページ／file_head＝テキスト・コードの先頭）を使って、"
            f"資料（{_kb_hint_abs(world)}）と関係グラフを**自分で調べてください**。"
            f"{_read_block}"
            "まずツール（台帳・全文検索・グラフ）で当たりを付けてから、原本の中身を確かめて答える。"
            f"{_layer_block}"
            "確定した事実と推定は分けて書く（詳細ルールは AGENTS.md）。"
            "検索ヒットや精読結果に text_truncated が付いていたら、その本文は途中で切れている。"
            "結論を出す前に read_around か read_doc で続きを読む。続きを取得する手段が無い打ち切り"
            "（file_truncated・pdf_pages の text_truncated・compare_documents／graph_neighbors／glob_search／doc_outline の truncated・folder_tree の folders_truncated・xlsx_sheets／ripgrep_search／es_search の truncated＝ヒット数上限）は"
            "その範囲を未確認として明示し、全件性を主張しない。"
            "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
            "（grep は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれる"
            "ので、名前の部分一致は list_docs の name_pattern で当てる（grep で本文からは探さない）。"
            "表記が揺れそうな語は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。"
            "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
            "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。"
            "**一覧を求められたら該当する全件を各項目のパス付きで列挙する（省略しない・件数と一致させる）。**"
            "全件・一覧の完了は対象範囲の確認を終えてからで、検索3回や件数だけの取得では完了とせず、"
            "中断（利用者停止・通信エラー・予算到達）のときは確認済み／未確認／理由を分けて書き、"
            "部分結果を「全件」と断定しない。"
            "原因の手がかりや関連部品（呼び出し/コピー/参照/関連文書）をたどるときは graph_neighbors を使う。"
            # 影響を問う質問の分解の型。表層の症状語で検索を乱発させず、変更対象と
            # 影響先の「接続（経路）」の有無を根拠に答えさせる。
            "**影響を問う質問（「〜を変えたら」「〜に影響ある？」「〜が落ちる？」など）では、"
            "①変更対象（例: 税率）に依存する部品・記述を特定 → ②影響先（例: 夜間バッチ＝JCL/ジョブ）を特定 → "
            "③両者の接続（COPIES／INVOKES／ACCESSES／CONTAINS＝構造的な依存の経路）を graph_neighbors で"
            "当たる。graph_neighbors は近傍ごとに辺の種類と向き（from→to）を返す——COPIES／INVOKES／"
            "ACCESSES／CONTAINS だけで構成された経路は根拠にしてよい。影響は矢印をさかのぼる（A →COPIES→ B は"
            "B を変えると A が影響を受ける・変更対象から出ていく矢印の先は影響先ではない）。経路に DOCUMENTS"
            "（言及）・CORRESPONDS_TO の辺や unverified の辺（裏付け原本が実在確認できない）が 1 本でも含まれる"
            "近傍は候補どまり＝原本で確認する。経路の先の"
            "実際の記述を引用したいときだけ原本を開き、接続の有無を根拠として答える（向きは平易語で・"
            "内部のエッジ名は本文に出さない）。"
            "質問中の症状表現（落ちる/止まる/エラー/停止 等）をそのまま検索語にしない**"
            "（原因調査＝トラブルシュートだと明示された時のみ症状語で探してよい）。"
            # ask_user の使用条件（agentic と同じ制約）＋乱用ガード（確認ID 付きは再質問しない・1回まで）。
            # 発動基準を具体化（lens 別の例）＋ユーザー主導の確認要求を確実な発動手段にする。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず、ここまでに確認できたことをまとめて終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
        )
        if lens == "author":
            # author は MCP ツールで根拠を集めたうえで成果物ファイルを authoring 直下に作る。
            # author は列構成・粒度など仕様が曖昧な場面が多い＝着手前の確認が「作ってから直す」より安い。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                # スライド/プレゼンは既定 Marp（見た目重視）・後で PowerPoint 編集なら python-pptx。
                # Codex は marp の .md を書くだけでよい（レンダは Sherpa 側が完了後に自動実行するので、
                # marp CLI の有無をここで判断する必要は無い）。
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}")
        # R1a: 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
        return sysp + base + f"\n\n{self._history_block()}【質問】{message}"

    def _plain_text(self, message: str = "") -> str:
        # ナレッジ参照オフでは Codex CLI を起動しない（read-only でも grep/ファイル読取が可能で
        # KB を覗けてしまうため）。
        # Codex 構成は資料参照ON固定になったため、通常この経路には来ない
        # （画面はトグルをON固定・`routers/chat.py::_knowledge_for` がサーバ側でも強制）。
        # 内部経路や古いクライアントが knowledge=False で呼んだ場合の安全網としてだけ残す。
        return ("Codex は常に社内資料を参照して回答します。"
                "資料を参照しない雑談は OpenAI／ローカルLLM を選んでください。")

    def run(self, ctx: Ctx) -> Iterator[dict]:
        # `_GenProvider.run()` と同じく分岐前に確定させる（`_prompt`/`_prompt_mcp` が
        # `_run_authoring` から参照する）。
        self._history = list(ctx.history or [])
        if not ctx.knowledge:                          # ナレッジ参照オフ＝素の会話（Codex を grep なしで・authoring 不使用）
            yield from _plain_run(self, ctx); return
        yield from self._run_authoring(ctx)

    def _run_authoring(self, ctx: Ctx) -> Iterator[dict]:
        decision = env = None
        _turn_t0 = time.monotonic()   # `sherpa.usage` ログ 1 行の elapsed（このターン全体）
        # シーム規則（モジュール docstring 参照）: `_gather` は「危険な継ぎ目」（複数テストが
        # `agents._gather` を monkeypatch して介入を検証する）。本モジュールは agents.py（facade）
        # からモジュールレベルで import されるため、逆にモジュールレベルで `from sherpa import agents`
        # すると循環 import になる → 関数内で遅延 import し facade 属性経由で実行時解決する。
        from sherpa import agents as _facade
        for ev in _facade._gather(ctx):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:                                # _gather が clarify question を出して停止＝確認待ち
            return

        yield _node("codex", "think", "Codex が調べる", "資料を調べています", "active")
        answer, ran = None, False
        # 閉域キットが Codex CLI を同梱している場合（scripts/install_offline_kit.sh 7b）、
        # 「CLI はあるが認証が無い」状態が起こりうる。
        # このとき codex exec は即座に非ゼロ終了・stdout に JSON を1行も出さない（実測）。
        # 起動前ガード（shutil.which 不在・config書込み例外・.codex-sessions symlink 等）で
        # 一度も codex exec を起動していないケースと区別するため、if ブロック内でだけ True にする
        # （if ブロックが丸ごとスキップされた経路ではこの既定値 False のまま＝既存の決定的回答
        # フォールバックを維持・tests/unit/test_codex_resume.py の pinned "dispatch-headline" と非衝突）。
        _codex_silent_failure = False
        # 同じ理由（if ブロックが丸ごとスキップされる経路がある）で、自動継続の
        # `env["codex_stopped_early"]` 判定用フラグも既定 False にしておく——`_agent_msgs` 等が
        # 存在するのは if ブロック内だけのため、実測値への上書きもそこでだけ行う。
        _codex_stopped_early = False
        # if ブロックが丸ごとスキップされる経路（`ws_authoring`/`run_dir` が None・shutil.which 不在等）
        # では Popen 自体を試みていない＝技術的失敗ではないため既定 False（第3分岐で参照するため
        # ここで定義しておく必要がある・if ブロック内だけで代入すると NameError になる）。
        _stream_error = False
        codex_question = None                                    # ask_user 由来の question（出たら env/_result を出さずターン終了）
        codex_usage = None                                       # turn.completed の usage（best-effort・出なければ None）
        # resume 試行が失敗し新規セッションへ切り替わったら True にする（if ブロックが丸ごと
        # スキップされる経路もあるためここで既定 False・usage のターン差分判定に使う）。
        _resume_fallback_happened = False
        # `graph_neighbors` の mcp_tool_call item が旧世代
        # グラフの構造化エラー（`_graph_schema_era_from_item`）を運んできたら、ここへ捕まえておく。
        # `for line in proc.stdout:` を包む2重の `except Exception:`（_attempt 自身・呼び出し元の
        # `_run_authoring`）は技術的失敗を `_stream_error` へ丸めてしまうため、その中で直接 raise
        # しても握り潰される——両方の try/except/finally を抜けた後（下の `if codex_question is
        # not None:` の直前）でこのフラグを見て改めて raise する。
        _graph_schema_era_error = None
        # ガード: 確認ID 付き再送（前の質問への回答）では ask_user を無視＝再質問ループ防止
        # （chat.js が回答再送に `確認ID: {interaction_id}` を必ず含める・chat_router の marker と同流儀）。
        _ask_disabled = bool(re.search(r"確認ID[:：]", ctx.message or ""))
        mcp_neighbors: list = []                                 # Codex が graph_neighbors で引いた近傍（UI カードに反映）
        # MCP の read 系ツール（read_doc/read_around/
        # doc_outline/compare_documents）の引数から集めた doc_id。attempt をまたいで合算する（自動継続の
        # 複数 codex exec プロセスにまたがるため）。最終 answer の「参照した資料:」ブロックの解析結果に
        # 合流させ、機械検証してから env["sources"] へ足す（原本直読は MCP の結果に載らず出典に出ない穴の
        # 補完）。
        _mcp_read_docs: list = []
        # `xlsx_sheets`（シート一覧のみ・本文は読んでいない）は上と分けて集める——
        # sources（参照候補）には合流させるが、根拠ゲート（sources_verified）には数えない
        # （`xlsx_sheets` の呼び出しだけを根拠に「精読済み」を偽装させない）。
        _mcp_listed_docs: list = []
        # MCP ツール呼び出しの並走計測（run 全体の合算値）。item id は codex exec プロセスごとに
        # 振り直される（`item_0` 等が採番し直される）ため、id の集合（`seen`/`open`）は `_attempt`
        # （1プロセス=1回の codex exec）内のローカル変数として毎回作り直し、attempt 終了時（finally）
        # にこの run-level dict へ合算する——resume 失敗時のフォールバック再試行・自動継続
        # （`_CONTINUE_PROMPT` ループ）はいずれも複数プロセスにまたがるため、id をまたいで共有すると
        # 別プロセスの同名 id を同一呼び出しと誤認し、総数を過少計上する。attempt は逐次実行（同時に
        # 走らない）ため、`max_in_flight` は attempt ごとの最大値の**最大**（合計ではない）を取る。
        _mcp_calls = {"total": 0, "max_in_flight": 0}
        codex_created_files: list[str] = []                      # 実行後に台帳登録する新規ファイルの絶対パス
        _any_new_ws = False                                       # codex 未インストール時の NameError 防止
        _created_file_rows: list[dict] = []                       # 台帳登録に成功した行（env["created_files"] 用）
        # move／台帳登録が1件でも失敗したら True（run_dir を消さず回収用に残す・
        # 回答本文へ注記を足す判定に使う）。
        _created_files_failed = False
        # 専用 authoring ディレクトリを cwd に。個人アップロード(files/)から分離。
        # KB は絶対パスでプロンプトに渡す。authoring/workspace に symlink が
        #   混入していると封じ込めが崩れるため、_safe_workspace_authoring で symlink 拒否＋fail-closed。
        users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
        uid = ctx.uid or "admin"
        ws_authoring = _safe_workspace_authoring(users_dir, uid)   # None＝fail-closed（Codex 起動しない）
        # 実行ごとの専用作業領域（cwd/書込 root）。同一 uid の複数実行が別々の run dir を使うため、
        # 直列化 lock は不要（sandbox._safe_run_authoring 参照）。None＝run dir が作れない＝
        # ws_authoring is None と同じ fail-closed（Codex を起動しない）。
        run_dir = _safe_run_authoring(users_dir, uid)
        # 会話単位ロック（`_session_persistence_enabled` の時だけ後段で実値になる）。ここで
        # 既定値を確定しておく——ブロック内の代入より前で例外が起きても finally が参照できるよう
        # にする（`_conv_lock_acquired` は実際に自分が取得できた時だけ True＝busy 早期 return では
        # 他者が保持するロックを誤って解放しない）。
        _conv_lock = None
        _conv_lock_acquired = False
        # 台帳登録（files/ move）まで完了した後で必ず削除する（正常終了・停止・例外の
        # いずれでも同様＝GeneratorExit（クライアント切断相当の generator.close()）が本体のどの
        # yield 点で飛んできても finally は必ず実行される）。会話ロックの解放もこの finally で行い、
        # 成果物の move／台帳登録・最終回答（`_result`）の送出までロックを保持する
        # （途中で解放すると、同じ会話の次ターンが古い `codex_session_id`／履歴のまま
        # 割り込める窓ができる）。
        try:
            # 会話継続（Codex ネイティブ resume）: conversation_id があるターンだけセッションを
            # 永続化する（chat_service 経由のチャット呼び出しは常に有り。conversation_id 無しの直接呼出し
            # ＝既存テスト等は従来どおり per-request 使い捨て CODEX_HOME＋`--ephemeral` のまま・無改修）。
            _persist_session = ctx.conversation_id is not None
            resume_sid = ctx.codex_session_id if _persist_session else None
            thread_id = None   # 捕捉した Codex session/thread id（_session_persistence_enabled の時だけ env に載せる）
            # `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral`
            # 実行のため、そこで捕捉した thread_id は resume 不能（ディスクに残らない）。この専用フラグで
            # 「DB へ永続化してよいか」を判定する（`_persist_session` 単独だと fallback 経路の使い捨て
            # thread_id まで DB に保存し、サンドボックス復帰後の resume が永久に失敗し続ける穴があった）。
            _session_persistence_enabled = _persist_session and _codex_sandbox_enabled()
            # 永続 CODEX_HOME（`.codex-sessions/{cid}`）は固定パスのため、
            # 事前に symlink を仕込まれると（未検証のまま書込むと）封じ込めが崩れる。`ws_authoring` と
            # 同じ fail-closed 契約＝安全確認できなければ Codex を起動しない（このターンは決定的回答へ）。
            _safe_persistent_codex_home = None
            _codex_home_ok = True
            if _session_persistence_enabled:
                _safe_persistent_codex_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
                _codex_home_ok = _safe_persistent_codex_home is not None
            # 永続 CODEX_HOME を使う実行だけ、同一会話単位で非ブロッキング lock を取る
            # （run dir 自体は実行ごとに独立なので対象外）。
            _conv_lock = _conversation_lock(ctx.conversation_id) if _session_persistence_enabled else None
            if _conv_lock is not None:
                _conv_lock_acquired = _conv_lock.acquire(blocking=False)
            if _conv_lock is not None and not _conv_lock_acquired:
                msg = "この会話の別の回答を実行中です。終わってからもう一度お試しください。"
                yield _node("codex", "think", "Codex が調べる",
                           "（この会話の別の回答を実行中のため今回は実行しません）", "done")
                yield {"type": "answer_delta", "text": msg}
                sm = layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens=decision["lens"])
                sm["source"] = "busy"
                env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                      "data": {}, "sources": [], "busy": True, "scope": sm}
                yield {"type": "_result", "env": env,
                      "decision": {"lens": decision["lens"], "input": ctx.message,
                                  "reason": "同一会話の Codex 実行が進行中"}}
                return
            if shutil.which("codex") and ws_authoring is not None and run_dir is not None and _codex_home_ok:
                # agent_message は run 中に複数届く（作業宣言＋結論）。最後の1件を鵜呑みに
                # せず全部集めて後で結論を選ぶ（`_pick_codex_headline`）。try の外で初期化＝Popen 失敗の
                # except 経路でも NameError にしない。
                _agent_msgs: list[str] = []
                _agent_partial = ""
                # stream 読取が途中例外で終わったか。例外時は集めた _agent_msgs が
                # 進行中の作業宣言だけの可能性があるため、完全版が入り得る `-o` 最終メッセージファイルを先に試す。
                _stream_error = False
                # 出力スキーマ有効時（`_schema_on`）だけ使う状態（§2-3）: `_latest_structured` は最新
                # attempt の最終出力を検証した結果（合格した dict・不合格/欠落は None）。`_structured_answers`
                # は attempt をまたいで合格した dict を積む（見出し選択・§2-5 用）。
                _latest_structured: dict | None = None
                _structured_answers: list[dict] = []
                mcp = _codex_mcp_enabled()                          # MCP ツールで自律調査（既定ON）
                sp = (ctx.scope_meta or {}).get("scope_paths")
                # Codex 自身の追加探索（MCP／直接grep）への層フィルタは qa レンズだけに渡す（探す対象）。
                # author は Codex の追加探索が正典 §1.8 の既知の非対称性（agentic_search.run_tool を
                # 経由しない構成）のため対象外・impact/troubleshoot は非適用（layer.applies_to_lens と
                # 同じ結論だが author も除外するためここでは共通ヘルパーを使わず明示判定する）。
                _layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None
                _layer_restricted = _layer not in (None, "both")
                # 層のフィルタは MCP ツール側（run_tool）だけが担う（直読は層に関係なく read・Codex に層は
                # 強制しない）。MCP 無効・sandbox 無効の構成では層の指定をツールに渡す経路が無いため、
                # 黙って無視せず実行前に正直に失敗する。
                _layer_enforcement_ready = mcp and _codex_sandbox_enabled()
                if _layer_restricted and not _layer_enforcement_ready:
                    # 黙って層を無視した回答を返さず、実行せず正直に失敗を伝える（未計測＝Codex CLI を
                    # 一度も起動しない）。利用者向け文言・進捗表示は専門用語ゼロ（MCP/sandbox を出さない・
                    # docs/04 §6）——具体的な理由は decision.reason（監査・管理者ログ専用）にだけ残す。
                    msg = "この構成では探す対象の限定はできません。管理者に設定の確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "探す対象の限定に対応していません", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",   # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    _reason = ("MCP 無効時は探す対象の限定に対応できません" if not mcp
                              else "sandbox 無効時は探す対象の限定に対応できません")
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message, "reason": _reason}}
                    return
                # authoring/ = Codex の書込先（cwd）。files/ = ユーザーアップロード（cwd 外・Codex から隔離）。
                # BLOCKER-2: files/ ディレクトリ自体が symlink でも authoring/ は分離されているので安全。
                # files/ の symlink チェックはアップロード grep 側（chat_service._personal_grep_hits）で行う。
                ws_files = users_dir / uid / "workspace" / "files"
                if ws_files.is_symlink():
                    ws_files = None  # type: ignore[assignment]
                else:
                    ws_files.mkdir(parents=True, exist_ok=True)
                # 実行前の run_dir スナップショット（新規ファイル検出用）。
                _before_ws_files: set = set()
                _before_ledger_files: set = set()
                if ws_files is not None and ws_files.is_dir():
                    _before_ledger_files = set(ws_files.iterdir())
                if run_dir.is_dir():
                    # `.agents`（配備したスキル）配下も `.tmp` 同様に台帳登録スキャン対象外。
                    # ルート直下の AGENTS.md も対象外: スナップショット後に write_agents_md() が書くため、
                    # 除外しないと初回実行で「新規ファイル」誤認 → files/ へ move（run_dir から消える）→
                    # 次回また書かれて再検出…と毎回 AGENTS_N.md が台帳に蓄積する。
                    _before_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) != Path("AGENTS.md")
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                # reasoning=minimal は image_gen/web_search と非互換で API 400 になる（実証済）→ low へ引き上げ。
                # author（作成）のときは intent 連動パラメータ `SHERPA_CODEX_REASONING_AUTHOR`
                # （既定 medium）を使う。通常レンズは現行のまま（低負荷優先）。
                _is_author = decision["lens"] == "author"
                # 調べる深さ（調べ方ブロック §3.2）: 通常レンズの基準値だけ管理画面の基準値編集
                # （system_settings）を反映する（author 専用の env は別軸のため対象外・§1.6 の
                # `SHERPA_CODEX_REASONING` に対応する基準値のみ）。標準=基準値のまま・深く=high・
                # 最大=xhigh の per-turn 上書きは author を含む全レンズに一律適用する。
                _base_reason = (os.environ.get("SHERPA_CODEX_REASONING_AUTHOR", "medium") if _is_author
                               else depth_profile_mod.effective_base(
                                   self._system_settings, "codex_reasoning", self._reason))
                _reason_raw = depth_profile_mod.codex_reasoning_for(
                    _base_reason, (ctx.scope_meta or {}).get("depth_profile"))
                _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
                # 利用統計の拡充: usage メタへ足す「実際に codex exec へ渡した
                # model_reasoning_effort」（`_reason`）と、深さ倍率の上書き前の基準値
                # （`_base_reason`）。一致（標準プロファイルの通常ケース）なら `reasoning_base` は
                # 省略する（`usage_reasoning_extras` の契約）。
                _usage_depth_extra = depth_profile_mod.usage_reasoning_extras(
                    (ctx.scope_meta or {}).get("depth_profile"), _base_reason, _reason)
                # 原本直読の read root と秘匿 deny の列挙は、プロンプトの文言（direct_read フラグ）と
                # permission profile（_write_codex_authoring_config・後段）の両方が使うため、
                # プロンプト組立の前に1回だけ計算する（提案書 2026-09-10-Codex原本直読と調査スキル）。列挙失敗（RuntimeError＝fail-closed）時は両方とも
                # 「直読不可」に揃える——プロンプトだけ楽観的、profile だけ悲観的、という食い違いを防ぐ。
                _base_roots = _direct_read_roots(ctx.world)
                _direct_roots, _deny_roots = _base_roots, []
                _venv_for_deny = _venv_root()
                try:
                    _scope_deny = _scope_deny_entries(_base_roots, sp)
                    if _base_roots and all(r in _scope_deny for r in _base_roots):
                        raise RuntimeError("scope_enum_failed:no_scope_in_roots")   # 範囲がどの root にも無い
                    _sensitive_deny = _scope_deny + _enumerate_sensitive(
                        _base_roots + ([str(_venv_for_deny)] if _venv_for_deny else []))
                    _direct_read_ok = True
                except RuntimeError as e:
                    _direct_roots, _deny_roots, _sensitive_deny, _direct_read_ok = [], _base_roots, [], False
                    _log.warning("codex direct read disabled: %s", e)
                if not _direct_read_ok and not mcp:
                    # MCP 無効の構成は直接参照だけが調べる手段＝直読を許可しないと何も調べられない。
                    # 黙って空振りの回答を返さず、実行前に正直に失敗する（利用者向け文言は専門用語ゼロ）。
                    msg = "この資料フォルダは今回読み取りの準備ができませんでした。管理者に確認を依頼してください。"
                    yield _node("codex", "think", "Codex が調べる", "資料の読み取り準備に失敗", "done")
                    yield {"type": "answer_delta", "text": msg}
                    env = {"lens": decision["lens"], "headline": msg, "summary": {"total": 0},
                          "data": {}, "sources": [],
                          "agentic_failure": "error",   # 実行していないターン＝完了として数えない
                          "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world,
                                                              lens=decision["lens"])}
                    yield {"type": "_result", "env": env,
                          "decision": {"lens": decision["lens"], "input": ctx.message,
                                       "reason": "MCP 無効の構成で直読の準備（秘匿ファイル列挙／範囲）に失敗"}}
                    return
                # MCP でも FS でも同じプロンプト組み立て（personal_facts を注入）。
                if mcp:
                    _codex_msg = ctx.message
                    if ctx.personal_facts:
                        _codex_msg = (f"{ctx.message}\n\n"
                                      f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}")
                    prompt = self._prompt_mcp(_codex_msg, decision["lens"], ctx.world,
                                              direct_read=_direct_read_ok, layer=_layer)
                else:
                    prompt = self._prompt(ctx.message, decision["lens"], env, ctx.world)
                # §3: --ephemeral（セッションをディスクに残さない）と -o（最終メッセージのファイル
                # 出力＝JSON 抽出が空だった時の保険）は sandbox/fallback どちらでも共通。.tmp/ は既存の
                # run_dir 新規ファイル走査（台帳登録スキャン）から除外済みのディレクトリ（既存の挙動を流用）。
                # 正典 §3.4「範囲と同じ硬いフィルタ」: run_dir は実行ごとの新規作成（`mkdir(exist_ok=False)`）
                # のため前ターンの残存はあり得ないが、symlink にすり替わっていた場合は rmtree が
                # 例外を送出する＝fail-closed のまま残す（多層防御）。
                _tmp = run_dir / ".tmp"
                if _tmp.exists() or _tmp.is_symlink():
                    shutil.rmtree(_tmp)
                _tmp.mkdir(parents=True, exist_ok=True)
                _last_message_path = _tmp / f"last-message-{hashlib.sha1(os.urandom(8)).hexdigest()[:12]}.txt"
                codex_home = None
                if _codex_sandbox_enabled():
                    # 検証済 recipe: permission profile で読取を KB(RO)＋authoring(RW) に封じ込め＋env 洗浄。
                    # CODEX_HOME は authoring の外（workspace 直下・`:root=deny` で shell から不可視）。
                    # conversation_id があるターンは会話ごとの固定ディレクトリ
                    # （`workspace/.codex-sessions/{cid}`）を CODEX_HOME にして毎ターン再利用する
                    # （`sessions/` 配下の JSONL が resume の実体＝下の finally では削除しない）。
                    # 無い場合（conversation_id 無しの直接呼出し・既存テスト等）は従来どおり per-request
                    # 使い捨て（実行後 rmtree・`--ephemeral`）のまま無改修。
                    # `_safe_persistent_codex_home` は外側で既に symlink/workspace外
                    # 逸脱を検証済み（ここで再計算しない＝検証と使用の間で別パスを組み立てて TOCTOU を
                    # 生まない）。ここに来ている時点で `_session_persistence_enabled` かつ `_codex_home_ok`
                    # （＝`_safe_persistent_codex_home is not None`）は保証済み。
                    if _session_persistence_enabled:
                        codex_home = _safe_persistent_codex_home
                    else:
                        _rand = hashlib.sha1(os.urandom(8)).hexdigest()[:12]
                        codex_home = users_dir / uid / "workspace" / f".codexhome-{_rand}"
                    argv_base = ["codex", "exec", "--json", "--strict-config", "--skip-git-repo-check",
                                "-o", str(_last_message_path),
                                "-C", str(run_dir), "-m", self.model,
                                "-c", f"model_reasoning_effort={_reason}"]
                    if not _session_persistence_enabled:
                        argv_base.append("--ephemeral")
                    # `self._openai_api_key` は Codex(OpenAI) 構成で接続先が Azure 等の時だけ
                    # `_select_provider` が解決して渡す（それ以外は常に None＝在来どおり env に渡さない）。
                    popen_env = _codex_clean_env(codex_home, run_dir, _tmp,
                                                 openai_api_key=self._openai_api_key)
                else:
                    # フォールバック（SHERPA_CODEX_SANDBOX=0）＝旧 `-s workspace-write`（読取全開・多層防御は OS ユーザ分離に依存）。
                    # この緊急避難経路は対象外＝resume 非対応のまま（既存どおり常に使い捨て）。
                    # `_session_persistence_enabled` は既に False（サンドボックス無効
                    # なので）＝ここで捕捉する thread_id は env に載らない（下の env 組立部分を参照）。
                    resume_sid = None
                    argv_base = ["codex", "exec", "--json", "--skip-git-repo-check",
                                "--ephemeral", "-o", str(_last_message_path),
                                "-s", "workspace-write", "-C", str(run_dir),
                                "-m", self.model, "-c", f"model_reasoning_effort={_reason}"]
                    # §5-1: --strict-config が無い経路（config.toml でなく -c）なので同等をここで足す。
                    argv_base += _web_search_c_args(self._web_search, self._system_settings)
                    if mcp:
                        argv_base += _mcp_config_args(ctx.world, sp, _ask_disabled, layer=_layer)
                        popen_env = {**os.environ, **_mcp_env(ctx.world, sp, _ask_disabled, layer=_layer)}
                    else:
                        popen_env = None
                # 出力スキーマ（§2-1）: OpenAI 系構成のみ（Codex(Ollama) は未確認のため対象外）・
                # 退避口 env `SHERPA_CODEX_OUTPUT_SCHEMA=0` で無効化できる（既定 ON）。
                _schema_on = (self._ollama_base_url is None
                             and _env_int("SHERPA_CODEX_OUTPUT_SCHEMA", 1, 0, 1) == 1)
                if _schema_on:
                    argv_base += ["--output-schema", str(_OUTPUT_SCHEMA_PATH)]

                def _build_argv(use_resume: bool, prompt_text: str | None = None) -> list:
                    """resume 分岐は `codex exec resume [SESSION_ID] [PROMPT]` の位置引数どおり、
                    exec 共通オプションの後・末尾プロンプトの前に `resume <sid>` を挿む。resume 先 id は
                    `thread_id`（`thread.started` で捕捉した最新値）を優先し、未捕捉なら呼び出し時点の
                    `resume_sid` に落ちる（自動継続はフレッシュ実行で捕捉した thread_id で resume する）。
                    `prompt_text` 省略時は通常の質問プロンプト（`prompt`）を使う（継続 attempt だけ別文言）。"""
                    av = list(argv_base)
                    if use_resume:
                        sid = thread_id or resume_sid
                        if sid:
                            av += ["resume", sid]
                    av.append(prompt if prompt_text is None else prompt_text)
                    return av

                got_any_line = False   # resume 試行で1行も --json イベントを受け取れなければ resume 失敗とみなす
                attempt_returncode = None   # fallback 判定の将来耐性（下の呼出側コメント参照）
                # 自動継続がツール未実行のまま宣言だけを繰り返す（正常な手順説明相手に無駄打ちする）のを
                # 打ち切るための per-attempt フラグ（attempt 開始ごとに False へ戻す）。
                _attempt_ran_tools = False
                # item id は codex exec プロセスごとに振り直される（`item_0` 等）ため、継続 attempt が
                # 初回 attempt と同じ id を使うとノードを上書きし、保存ログから前回分の履歴が消える。
                # 2回目以降の attempt でだけ node id に付ける接頭辞の元になる連番（初回=1・以降 _attempt
                # 呼び出しごとに +1）。
                _attempt_no = 0
                # `_needs_continuation`／`codex_stopped_early` の判定を「最新 attempt の message だけ」
                # に絞るための境界（`_agent_msgs` へのこの attempt 開始時点の長さ）。`_pick_codex_headline`
                # は従来どおり `_agent_msgs` 全件を見る（headline の選び方は変えない）。
                _attempt_msgs_start = 0
                # attempt 開始時に消せなかった前 attempt の `-o` 本文（吸収で読み飛ばす対象・無ければ None）。
                _stale_last_message = None
                # §2-10: トップレベル `turn.failed`／`error` イベントを見た attempt かどうか（agent_message
                # が1つも無いこの種の失敗は、既存の「stdout に JSON が1行も無い」判定では拾えない）。
                # 診断コード（`error.code`。無ければ None）だけ控える——本文はログにも利用者向け文言にも貼らない。
                _turn_failed = False
                _turn_failed_code = None

                def _attempt(use_resume: bool, prompt_text: str | None = None):
                    """1回分の codex exec 実行（node/answer_delta を yield）。proc はこの1回限りの
                    ローカル状態（呼出側は再試行のたびに新しい Popen を張るだけでよい）。`prompt_text` は
                    自動継続用（省略時は通常プロンプト）。"""
                    nonlocal got_any_line, ran, codex_question, codex_usage, thread_id, attempt_returncode
                    nonlocal _agent_partial, _stream_error, _graph_schema_era_error
                    nonlocal _attempt_ran_tools, _attempt_no, _attempt_msgs_start, _stale_last_message
                    nonlocal _turn_failed, _turn_failed_code
                    got_any_line = False
                    attempt_returncode = None
                    _attempt_ran_tools = False
                    _turn_failed = False
                    _turn_failed_code = None
                    _attempt_no += 1
                    # 前 attempt の未完 message（item.updated だけで completed が来なかった分）は履歴へ
                    # 退避してから境界を引く。残したままだと最新 attempt の判定に前 attempt の途中経過が
                    # 混ざり、完成した回答でも継続／`codex_stopped_early` になる。
                    if _agent_partial.strip():
                        _agent_msgs.append(_agent_partial)
                    _agent_partial = ""
                    _attempt_msgs_start = len(_agent_msgs)
                    # `-o` は attempt をまたいで同じパス。前 attempt の内容を残すと、この attempt が
                    # 何も書かずに終わったとき古い文を最新 attempt の回答として吸収してしまう。消せない
                    # ときは残った本文を控え、終了後の吸収でその本文だけは読み飛ばす。
                    _stale_last_message = None
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except OSError as exc:
                        _stale_last_message = _read_last_message_fallback(_last_message_path)
                        _log.warning("codex last-message cleanup failed: %s errno=%s conv=%s uid=%s",
                                     type(exc).__name__, getattr(exc, "errno", None), ctx.conversation_id, uid)
                    # このプロセス（1回の codex exec）内だけで完結する id 集合（run-level `_mcp_calls`
                    # への合算は finally で行う）。
                    _attempt_mcp_seen: set = set()
                    _mcp_read_done: set = set()      # 収集済み item id（同じ item の再送で二重に数えない・
                                                     # item id は attempt（codex exec プロセス）ごとに振り直される）
                    _attempt_mcp_open: set = set()
                    _attempt_mcp_max_in_flight = 0
                    argv = _build_argv(use_resume, prompt_text)
                    proc = None
                    try:
                        # Popen 直前の最終防衛線（`_select_provider` の選択時チェックを迂回する経路が
                        # あっても、実際にプロセスを起動する直前でもう一度確認する・多層防御）。
                        # Codex(Ollama) 構成（`self._ollama_base_url` あり）は OpenAI 系 I/O ではないため
                        # 対象外。
                        if self._ollama_base_url is None:
                            from ... import llm
                            llm.assert_openai_io_allowed()
                        # start_new_session で独立プロセスグループにし、停止/後始末で
                        #   MCP subprocess / shell child まで group ごと確実に殺す（creds env の寿命を延ばさない）。
                        proc = subprocess.Popen(
                            argv, env=popen_env, cwd=str(run_dir), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            start_new_session=True)
                        if ctx.stop_event is not None:                # 途中停止（_spawn_stop_watcher 参照）
                            _spawn_stop_watcher(proc, ctx.stop_event)
                        node_n = 0
                        for line in proc.stdout:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                e = json.loads(line)
                            except ValueError:
                                continue
                            got_any_line = True
                            if e.get("type") == "thread.started":       # session/thread id 捕捉（resume 先の id）
                                thread_id = e.get("thread_id") or thread_id
                                continue
                            if e.get("type") == "turn.completed":            # ターンのトークン使用量（item ではない）
                                _u = _usage_from_turn_completed(
                                    e, self.model,
                                    codex_model_provider="ollama" if self._ollama_base_url is not None else "openai",
                                    system_settings=self._system_settings)
                                # 自動継続の attempt をまたいで合算する（単発 attempt のみの run では
                                # 従来どおり最初で唯一の値がそのまま codex_usage になる）。
                                codex_usage = _accumulate_codex_usage(codex_usage, _u)
                                continue
                            if e.get("type") in ("turn.failed", "error"):        # §2-10: 失敗終了の明示
                                _turn_failed = True
                                if _turn_failed_code is None:
                                    _err = e.get("error")
                                    _turn_failed_code = (
                                        (_err.get("code") if isinstance(_err, dict) else None)
                                        or e.get("code"))
                                continue
                            item = e.get("item") or {}
                            it = item.get("type")
                            iid = item.get("id")
                            if not iid:                                      # id 無し item でも node を上書き衝突させない
                                iid = f"cx-auto-{node_n}"
                                node_n += 1
                            if _attempt_no > 1:                               # 2回目以降の attempt は id 空間を分離（前 attempt のノードを上書きしない）
                                iid = f"a{_attempt_no}-{iid}"
                            if it in ("web_search", "file_change"):     # ネイティブ Web 検索／ファイル変更もツール実行（継続打ち切り判定用・表示ノードは追加しない）
                                _attempt_ran_tools = True
                            if it == "command_execution":                       # Codex 自身の grep/参照を逐次表示
                                ran = True
                                _attempt_ran_tools = True
                                label, detail = _humanize_cmd(item.get("command", ""))
                                if item.get("status") == "completed" or e.get("type") == "item.completed":
                                    ec = item.get("exit_code")
                                    yield _node(f"cx-{iid}", "tool", label,
                                                detail + (f"  → exit {ec}" if ec is not None else ""), "done")
                                else:
                                    yield _node(f"cx-{iid}", "tool", label, detail, "active")
                            elif it == "mcp_tool_call":                          # Codex の MCP ツール呼びを可視化＋近傍を収集
                                ran = True
                                _attempt_ran_tools = True
                                tool = item.get("tool", "")
                                a = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}  # 非 dict 引数で落とさない
                                done = e.get("type") == "item.completed" or item.get("status") in ("completed", "failed")
                                # 並走計測（このプロセス内のみ・run 全体への合算は _attempt の finally）。
                                # id が無い item は開始/完了を対応付けられないため対象外。初見かつ未完了の
                                # ときだけ in-flight に加える（初見でいきなり完了した item は total には
                                # 数えるが in-flight 幅には寄与しない）。2回目以降の見た目（例: item.updated
                                # の再送）は seen 済みなので無視され、二重に数えない。
                                _mcp_id = item.get("id")
                                # read 系ツールの引数から実際に読んだ資料の doc_id を集める（「参照した
                                # 資料:」の記載漏れの補完）。**読取が成功して完了した** item だけ（失敗・
                                # エラー結果・進行中は読めていない＝出典にも「根拠」にも載せない）。
                                _read_ok = (e.get("type") == "item.completed"
                                            and item.get("status") not in ("failed", "error")
                                            and not (isinstance(item.get("result"), dict) and item["result"].get("isError"))
                                            and not item.get("error"))
                                if _read_ok and (not _mcp_id or _mcp_id not in _mcp_read_done):
                                    if _mcp_id:
                                        _mcp_read_done.add(_mcp_id)
                                    # 原本読取ツール（xlsx_range/docx_paragraphs/
                                    # pptx_slides/pdf_pages/file_head）も doc_id 引数を取る読取
                                    # ツール——これらを収集対象に含めないと、Codex が MCP 経由で
                                    # 原本を直接読んでも出典収集から漏れる。
                                    # `xlsx_sheets` はシート一覧を返すだけで本文を
                                    # 読んでいない——本文精読ツールと同じ扱いにすると「シート一覧を
                                    # 見ただけ」で `sources_verified`（根拠ゲート）へ数えられて
                                    # しまうため、`_mcp_listed_docs`（sources には合流するが
                                    # sources_verified には数えない）へ分ける。
                                    if tool in ("read_doc", "read_around", "doc_outline",
                                               "xlsx_range", "docx_paragraphs",
                                               "pptx_slides", "pdf_pages", "file_head"):
                                        _d = a.get("doc_id")
                                        if isinstance(_d, str) and _d:
                                            _mcp_read_docs.append(_d)
                                    elif tool == "xlsx_sheets":
                                        _d = a.get("doc_id")
                                        if isinstance(_d, str) and _d:
                                            _mcp_listed_docs.append(_d)
                                    elif tool == "compare_documents":
                                        for _k in ("left_doc_id", "right_doc_id", "source_doc_id"):
                                            _d = a.get(_k)
                                            if isinstance(_d, str) and _d:
                                                _mcp_read_docs.append(_d)
                                if _mcp_id and _mcp_id not in _attempt_mcp_seen:
                                    _attempt_mcp_seen.add(_mcp_id)
                                    if not done:
                                        _attempt_mcp_open.add(_mcp_id)
                                        _attempt_mcp_max_in_flight = max(
                                            _attempt_mcp_max_in_flight, len(_attempt_mcp_open))
                                elif _mcp_id and done:
                                    _attempt_mcp_open.discard(_mcp_id)
                                if tool == "ask_user":
                                    # ask_user は question 優先（agentic の {"question":..}→return と同じ意味論）。
                                    # ガード②確認ID 付き再送では無視／③1実行1回（codex_question is None で enforce）。
                                    # 質問を捕まえたらループを抜け、finally で proc を後始末してから emit → ターン終了する。
                                    if codex_question is None:
                                        codex_question = _codex_ask_capture(item, _ask_disabled)
                                    # 捕捉して break する場合は item.completed を待たずに
                                    # ループを抜けるため、実際の done フラグに関わらずノードを "done" で確定表示する
                                    # （さもないと「ユーザに確認」が実行中表示のまま履歴保存される）。
                                    node_done = done or (codex_question is not None)
                                    yield _node(f"cx-{iid}", "tool", "ユーザに確認",
                                                f"「{str(a.get('prompt') or '確認が必要です')[:60]}」",
                                                "done" if node_done else "active")
                                    if codex_question is not None:
                                        break
                                    continue
                                # folder_tree/compare_documents は MCP 経由で
                                # Codex にも公開済み（`mcp_server.py::_tool_defs`）のため、この表示用ラベル
                                # 辞書にも対応を持たせる（`improvement_log._TOOL_CALL_LABELS` の集計対象でもある）。
                                tlabel = {"graph_neighbors": "関係グラフをたどる", "ripgrep_search": "資料を検索（語句そのまま）",
                                          "es_search": "資料を検索（全文）", "read_around": "該当箇所を精読",
                                          "list_docs": "資料の一覧を確認", "folder_tree": "フォルダ構成を確認",
                                          "compare_documents": "世代間の差分を比較",
                                          "read_doc": "文書を通読", "doc_outline": "見出し構造を確認",
                                          "glob_search": "ファイル名で検索",
                                          # agentic_search
                                          # の `_ORIGINAL_READ_LABELS`／改善ログの `_TOOL_CALL_LABELS` と
                                          # 同じ文言（`xlsx_sheets` はシート一覧のみ＝本文精読の
                                          # `xlsx_range` とは別ラベル・`_FILES_READ_LABEL` からも外れる）。
                                          "xlsx_sheets": "原本のシート一覧を確認", "xlsx_range": "原本を読む（Excel）",
                                          "docx_paragraphs": "原本を読む（Word）",
                                          "pptx_slides": "原本を読む（PowerPoint）",
                                          "pdf_pages": "原本を読む（PDF）",
                                          "file_head": "原本を読む（先頭）"}.get(tool, "その他の処理")
                                detail = (a.get("name") or a.get("query") or a.get("doc_id")
                                         or a.get("path_prefix") or a.get("name_pattern") or a.get("pattern") or "")
                                if done and tool == "graph_neighbors" and item.get("status") == "completed":
                                    # 旧世代グラフの構造化エラー
                                    # （`mcp_server.py::handle` が isError で返す）を先に見る——
                                    # 検知したら `_mcp_neighbors_from` は呼ばない（近傍データではない）。
                                    _era_err = _graph_schema_era_from_item(
                                        item, ctx.world, decision.get("lens") if decision else None)
                                    if _era_err is not None:
                                        _graph_schema_era_error = _era_err
                                    else:
                                        mcp_neighbors.extend(_mcp_neighbors_from(item))
                                yield _node(f"cx-{iid}", "tool", tlabel, f"「{detail}」", "done" if done else "active")
                                if _graph_schema_era_error is not None:
                                    # 検知後は以降の Codex 自身の調査を待たない（このターンの答えは
                                    # どのみち `_degrade_overload` の固定文言に置き換わるため）。
                                    break
                            elif it == "reasoning" and e.get("type") == "item.completed":
                                txt = (item.get("text") or "").strip().splitlines()
                                if txt:
                                    yield _node(f"cx-{iid}", "think", "考える", txt[-1][:80], "done")
                            elif it == "agent_message" and e.get("type") in ("item.completed", "item.updated"):
                                # 最後の1件で上書きせず集める（完了分はリストへ・未完分は partial に保持）。
                                # 結論の選択は loop 後に `_pick_codex_headline` で決定的に行う。
                                _txt = (item.get("text") or "").strip()
                                if e.get("type") == "item.completed":
                                    if _txt:
                                        _agent_msgs.append(_txt)
                                    _agent_partial = ""
                                else:                                    # item.updated＝成長中の未完 message（打ち切り保険）
                                    _agent_partial = _txt
                    except Exception:
                        _stream_error = True
                    finally:
                        # このプロセスで観測した分だけ run-level へ合算する（例外で打ち切られても、
                        # それまでに実際に見えていた分は計測に残す）。
                        _mcp_calls["total"] += len(_attempt_mcp_seen)
                        _mcp_calls["max_in_flight"] = max(_mcp_calls["max_in_flight"], _attempt_mcp_max_in_flight)
                        if proc:
                            try:
                                _killpg(proc)                        # group ごと（MCP child 含む）確実に後始末
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                            attempt_returncode = proc.returncode   # RV再検証 LOW-4: fallback 判定の材料

                def _absorb_last_message_fallback() -> None:
                    """attempt が `--json` に agent_message を出さず `-o` 最終メッセージファイルにだけ
                    結論を書いたケースを拾う（毎 attempt 終了直後に呼ぶ）。`_last_message_path` は
                    attempt をまたいで同じパスを使い回す（Codex が上書きする）ため、直前に既に
                    `_agent_msgs` へ入っている内容と同一（strip 比較）なら追加しない——正常系では
                    `-o` の内容は最後の agent_message と一致するので二重追加にならない。重複判定は
                    **最新 attempt の分（`_agent_msgs[_attempt_msgs_start:]`）だけ**と比べる: 過去の
                    attempt と同文だからと落とすと、この attempt の結論が判定対象から消える。
                    """
                    _fb = _read_last_message_fallback(_last_message_path)
                    if _fb and _fb == _stale_last_message:
                        return
                    if _fb and _fb.strip() not in {m.strip() for m in _agent_msgs[_attempt_msgs_start:]}:
                        _agent_msgs.append(_fb)

                def _continuation_msgs() -> list[str]:
                    """`_needs_continuation` へ渡す completed message＝最新 attempt の分
                    （`_agent_msgs[_attempt_msgs_start:]`）。ただしその attempt が agent_message を
                    1つも出さず（`_agent_partial` も空＝crash・無出力終了）に終わった場合は新しい
                    情報が無い＝それ以前の蓄積（`_agent_msgs` 全件）で判定する（直前の attempt が
                    作業宣言だけで止まっていたなら、その状態がまだ有効という意味）。
                    """
                    latest = _agent_msgs[_attempt_msgs_start:]
                    return latest if (latest or _agent_partial) else _agent_msgs

                def _update_structured_state() -> None:
                    """§2-3: `_schema_on` のとき、最新 attempt の最終出力（`-o` を第一候補・無ければ
                    最新 attempt の最後の完了 agent_message）を `_parse_structured` で検証し、
                    `_latest_structured` を更新する（継続要否・§2-4 の判定はこの1件だけを見る）。
                    呼び出しは毎 attempt 終了直後（`_absorb_last_message_fallback` と同じ場所）。
                    `_schema_on` が偽なら何もしない（`_latest_structured` は使われない）。

                    見出し候補（`_structured_answers`・§2-5）には、最終候補だけでなく最新 attempt の
                    agent_message 全件を順に検証して合格したものを積む——同一 attempt 内で先に有効な
                    `final`（や `in_progress`）が出ていても、後続の message が壊れた JSON だと最終候補
                    （末尾）だけを見る判定ではその final を拾えず失う。最終候補（`-o` 優先）はこの全件
                    ループに含まれない別ソースのときだけ追加で積む——最後の agent_message と同一テキスト
                    なら、全件ループで既に1回積んでいるため二重に積まない。
                    """
                    nonlocal _latest_structured
                    if not _schema_on:
                        return
                    _latest_msgs = _agent_msgs[_attempt_msgs_start:]
                    for _m in _latest_msgs:
                        _parsed = _parse_structured(_m)
                        if _parsed is not None:
                            _structured_answers.append(_parsed)
                    _fb = _read_last_message_fallback(_last_message_path)
                    if _fb and _fb == _stale_last_message:
                        _fb = None   # `_absorb_last_message_fallback` と同じ staleness 規則
                    _last_msg = _latest_msgs[-1] if _latest_msgs else None
                    _final_text = _fb or _last_msg
                    _latest_structured = _parse_structured(_final_text) if _final_text else None
                    if _latest_structured is not None and _final_text != _last_msg:
                        _structured_answers.append(_latest_structured)

                def _continuation_pending() -> bool:
                    """継続要否（§2-4）。`_schema_on` は最新 attempt の構造化出力の status で判定
                    （無し/不正/`in_progress` はすべて未完了）。無効時は現行の平文ヒューリスティックのまま。"""
                    if _schema_on:
                        return _latest_structured is None or _latest_structured["status"] == "in_progress"
                    return _needs_continuation(_continuation_msgs(), _agent_partial)

                def _pick_structured_headline() -> str | None:
                    """§2-3/5: `_schema_on` の見出し——構造化 message に `final` があれば最後の
                    `final` の `answer`／無ければ最後の構造化 message の `answer`／構造化 message が
                    一度も無ければ、何かしら出力はあった（`_agent_msgs`/`_agent_partial` が非空）ときだけ
                    固定文言（不正な最終出力のまま尽きたケース＝`_codex_stopped_early` が立つ）。
                    出力そのものが無ければ None（無出力失敗の判定へ委ねる・§2-10）。
                    """
                    # `answer` が空の構造化 message は「本文なし」＝固定文言に落とす（空文字を返すと
                    # 後段の `if answer:` から外れて dispatch の決定的見出しが正常回答として出てしまう）。
                    _empty = "回答を取り出せませんでした。もう一度お試しください。"
                    for s in reversed(_structured_answers):
                        if s["status"] == "final":
                            return s["answer"].strip() or _empty
                    if _structured_answers:
                        return _structured_answers[-1]["answer"].strip() or _empty
                    if _agent_msgs or _agent_partial:
                        return _empty
                    return None

                try:
                    # AGENTS.md はベストエフォート（書けなくても Codex 実行自体は継続・fail-open）。
                    # fail-open でも気づけるよう warning は残す（containment/grounding の短縮形は
                    # _prompt/_prompt_mcp に常置済みなので、書込失敗時も丸裸にはならない＝多層防御）。
                    try:
                        codex_agents_md.write_agents_md(run_dir, output_schema=_schema_on,
                                                        direct_read=_direct_read_ok)
                    except Exception as e:
                        _log.warning("AGENTS.md write failed (fail-open, prompt still has containment): %s", e)
                    # スキル配備（案A′ ベース＋個人オーバーレイ）も同じくベストエフォート（fail-open）。
                    # knowledge=ON の Codex 実行全部で配備する（author レンズに限定しない・progressive disclosure）。
                    try:
                        codex_skills.deploy_skills(run_dir, uid, users_dir)
                    except Exception as e:
                        _log.warning("skills deploy failed (fail-open): %s", e)
                    # profile config はここで書く（FileExistsError 等は fail-closed で
                    #   例外→except で answer=None→finally で CODEX_HOME 削除→決定的回答へ。古い config での起動を防ぐ）。
                    # marp/Chromium を read root に追加する必要は無い
                    # （Codex は .md を書くだけ・レンダは Sherpa 本体側で行う。marp_render.py 参照）。
                    # 会話ごとの CODEX_HOME は毎ターン再利用するため、前ターンの config.toml
                    #   （creds を含む・毎ターン即時削除している＝下の finally 参照）が残骸として
                    #   居ないことをまず確認してから書く（`_write_codex_authoring_config` 自体の
                    #   O_EXCL fail-closed は変更しない＝正規のターン跨ぎ再利用のための cleanup）。
                    if codex_home is not None:
                        try:
                            (codex_home / "config.toml").unlink(missing_ok=True)
                        except Exception:
                            pass
                        _write_codex_authoring_config(
                            codex_home, _kb_read_roots(ctx.world), _reason,
                            mcp, ctx.world, sp, self._web_search, _ask_disabled,
                            ollama_base_url=self._ollama_base_url, system_settings=self._system_settings,
                            layer=_layer, direct_read_roots=_direct_roots, sensitive_deny=_sensitive_deny,
                            deny_roots=_deny_roots)
                        # Azure OpenAI 対応: 接続先が Azure 等へリダイレクトされていて、そのせいで
                        # web_search が強制 OFF になっている時だけ、理由を1回（このターンにつき1回・
                        # `_write_codex_authoring_config` 呼び出しはこの1箇所だけで resume 再試行でも
                        # 再呼出されない）伝える。Codex(Ollama) 構成（`_ollama_base_url` あり）は対象外。
                        if self._ollama_base_url is None:
                            _ws_note = _web_search_endpoint_note(
                                self._web_search, _openai_endpoint_kind(self._system_settings),
                                self._system_settings)
                            if _ws_note:
                                yield _node("web_search_endpoint", "think", "Web検索の制限", _ws_note, "done")
                    yield from _attempt(bool(resume_sid))
                    _absorb_last_message_fallback()
                    _update_structured_state()
                    # resume を試みて1行も --json イベントが出なかった（＝セッション消失等で resume
                    # 失敗・実機確認済み: `codex exec resume <消失id>` は空 stdout・exit 1）場合、
                    # R1a 履歴 priming（プロンプトには self._history が既に前置済み）で新規セッションへ
                    # 即座にフォールバックする。ask_user 確認で終了した/途中停止されたターンは再試行しない。
                    # RV再検証 LOW-4: 将来の Codex CLI が失敗時に何らかの JSON（例: エラー系 item）を
                    # 1行以上出すようになっても取りこぼさないよう、「非ゼロ終了かつ agent_message が
                    # 1つも無い」場合も resume 失敗とみなす（`got_any_line` 単独判定の将来耐性・
                    # retry はこれまでどおり resume 試行時に1回だけ）。
                    _stopped = ctx.stop_event is not None and ctx.stop_event.is_set()
                    _no_agent_output = not _agent_msgs and not _agent_partial
                    _resume_attempt_failed = (not got_any_line) or (
                        attempt_returncode not in (0, None) and _no_agent_output)
                    if resume_sid and _resume_attempt_failed and codex_question is None and not _stopped:
                        _log.warning(
                            "codex resume failed (no output) sid=%s conv=%s uid=%s; falling back to a fresh session",
                            resume_sid, ctx.conversation_id, uid)
                        _agent_msgs.clear()
                        _agent_partial, _stream_error = "", False
                        mcp_neighbors.clear()
                        codex_usage, ran, codex_question, thread_id = None, False, None, None
                        # 失敗した resume attempt の構造化状態（古い session の final/in_progress）を
                        # 新規セッションへ持ち越さない——`_latest_structured` を残すとフォールバック後
                        # 最初の `_continuation_pending()` 判定が旧 session の値で決まってしまい、
                        # `_structured_answers` を残すと `_pick_structured_headline` が旧 final を
                        # 見出しに選び直してしまう。
                        _structured_answers.clear()
                        _latest_structured = None
                        _resume_fallback_happened = True
                        yield from _attempt(False)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                    # 自動継続: 正常終了（returncode 0）で agent_message が「作業宣言だけ」（結論文が
                    # 1つも無い＝_pick_codex_headline が規則③に落ちる）なら、Codex セッションの続きを
                    # 自動で呼ぶ（利用者の「続けて」連投をシステム側で肩代わりする）。ask_user 確認待ち・
                    # 利用者の明示停止・前 attempt の無出力・セッション非永続のいずれかなら
                    # 回さない（それぞれ後段の既存処理に委ねる）。`got_any_line` を要求するのは、継続
                    # attempt 自身が無出力で終わった場合に古い（蓄積済みの）作業宣言だけを根拠に
                    # 空振りを繰り返さないため。判定は `_continuation_msgs()`＝**直前の attempt の
                    # message だけ**（前の attempt の作業宣言と連結して誤判定しないため）。
                    _continue_limit = _env_int("SHERPA_CODEX_AUTO_CONTINUE", 3, 0, 5)
                    for n in range(1, _continue_limit + 1):
                        _stopped_for_continue = ctx.stop_event is not None and ctx.stop_event.is_set()
                        if not (codex_question is None and not _stopped_for_continue
                               and attempt_returncode == 0 and got_any_line
                               and _continuation_pending()
                               and _session_persistence_enabled and (thread_id or resume_sid)):
                            break
                        yield _node(f"cx-continue-{n}", "think", "続きを実行",
                                   f"途中経過で止まったため続きを調べます（{n}/{_continue_limit}）", "done")
                        yield from _attempt(
                            True, prompt_text=_CONTINUE_PROMPT_SCHEMA if _schema_on else _CONTINUE_PROMPT)
                        _absorb_last_message_fallback()
                        _update_structured_state()
                        if not _attempt_ran_tools:                # ツールを1つも呼ばずに終わった continuation は打ち切る（同じ宣言の空振りを繰り返さない）
                            break
                except Exception:
                    answer = None
                    _stream_error = True
                finally:
                    if codex_home is not None:
                        if _persist_session:
                            # セッション実体（`sessions/` の JSONL）は次ターンの resume の
                            # ために保持する。creds を含む config.toml だけ即時削除し露出窓を1ターン分に
                            # 限定する（retention のスイープはディレクトリ全体を対象にする＝別途 api.py）。
                            # `auth.json`（実 `~/.codex/auth.json` への
                            # symlink・`_write_codex_authoring_config` が張る）も同じ理由で毎ターン削除する
                            # （放置すると永続 CODEX_HOME に無期限残存＝次ターンは `_write_codex_authoring_config`
                            # が `dst.exists()` を見て再作成するので消しても実害は無い）。
                            try:
                                (codex_home / "config.toml").unlink(missing_ok=True)
                            except Exception:
                                pass
                            try:
                                (codex_home / "auth.json").unlink(missing_ok=True)
                            except Exception:
                                pass
                        else:
                            # per-request CODEX_HOME（profile＋auth symlink）を後始末（symlink target は消えない）。
                            try:
                                shutil.rmtree(codex_home, ignore_errors=True)
                            except Exception:
                                pass
                # サブプロセス後始末の直後・以降のどの分岐（schema-era エラーの re-raise・ask_user の
                # 早期 return・通常終了）を通っても必ず1回だけ出す（実環境で「モデルが複数の MCP ツールを
                # 同時に呼ぶか」を見るための計測。UI・env には載せない・「1実行あたり1行」を保つため
                # 早期 return より前に置く）。
                _log.info("codex mcp calls: total=%d max_in_flight=%d conv=%s uid=%s",
                          _mcp_calls["total"], _mcp_calls["max_in_flight"], ctx.conversation_id, uid)
                if _graph_schema_era_error is not None:
                    # 検知した専用例外を、それを飲み込む2重の
                    # try/except（`_attempt` 自身・この呼び出し元）を両方抜けた後でようやく re-raise
                    # する——`run()` から uncaught のまま伝播させ、`chat_service._degrade_overload`
                    # （provider.run() 全体を包む既存の縮退）に固定文言（再取り込み案内）への変換を
                    # 委ねる（`GraphQueryOverloadError` と同じ既存の fail-loud 経路）。
                    raise _graph_schema_era_error
                # ask_user が出たターンは question 優先＝env/_result・成果物台帳登録を出さずここで終了する
                # （agentic の {"question":..}→return と同じ意味論・回答は chat.js の整形再送＝新 codex exec で拾う）。
                # proc は直上の finally で後始末済み。chat_service はこの question を answer.question として保存する。
                if codex_question is not None:
                    # 親ノード（"Codex が調べる"）も冒頭で "active" のまま止まっているので、
                    # 通常経路の完了 yield（下の if answer/else ブロック）と同様にここで "done" に確定させる。
                    yield _node("codex", "think", "Codex が調べる", "ユーザに確認するため終了しました", "done")
                    # 早期 return が `-o` 一時ファイル（last-message-*.txt）の削除を
                    # バイパスして .tmp/ に蓄積し得た。通常経路（下の unlink）と同じ best-effort で先に消す。
                    try:
                        _last_message_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                    yield codex_question
                    return
                # §2-3/5: `_schema_on` は構造化 message から見出しを選ぶ（生 JSON をそのまま出さない・
                # 平文ヒューリスティックへは戻さない）。無効時は現行どおりの選び方（下記）。
                if _schema_on:
                    answer = _pick_structured_headline()
                else:
                    # 集めた agent_message から結論を優先して headline を選ぶ
                    # （進行中の作業宣言を見出しにしない・最後の1件を鵜呑みにしない）。
                    _picked = _pick_codex_headline(_agent_msgs, _agent_partial) or None
                    # §3: -o は保険。--json の agent_message から拾えなかった時だけ最終メッセージ
                    # ファイルを読む（既存の JSON 経路が主）。読んでも読まなくても使い終わったら必ず削除する
                    # （.tmp/ は台帳登録スキャン対象外＝放置すると溜まり続けるため）。
                    # 途中例外時は集めた _agent_msgs が進行中の作業宣言だけの可能性が
                    # あるため、完全版が入り得る `-o` 最終メッセージを**先に**試し、空/無いときだけ pick に委ねる。
                    # 正常終了時は現行どおり pick が主・`-o` は従（fallback）。
                    if _stream_error:
                        answer = _read_last_message_fallback(_last_message_path) or _picked
                    else:
                        answer = _picked or _read_last_message_fallback(_last_message_path)
                try:
                    _last_message_path.unlink(missing_ok=True)
                except Exception:
                    pass
                # codex exec を実際に起動した（attempt_returncode is not None＝Popen が完走した）
                # にもかかわらず stdout に JSON を1行も出さず（got_any_line=False）、answer も得られない
                # 場合だけ「正直に伝える」文言へ切り替える対象とする。ユーザーの stop_event による打ち切り
                # は失敗ではないため対象外（途中で殺しただけで agent_message が無いのは想定内の挙動）。
                _stopped_final = ctx.stop_event is not None and ctx.stop_event.is_set()
                # §2-10: 最新 attempt（継続 attempt を含む）が `turn.failed`／`error` で閉じ、
                # その attempt 自身は agent_message を1つも出さなかった（`_agent_msgs[_attempt_msgs_start:]`
                # も `_agent_partial` も空）場合、過去 attempt の（古い）回答が `answer` に残っていても
                # 明示失敗として扱う——`_pick_structured_headline`／`_pick_codex_headline` は attempt を
                # またいだ蓄積から拾うため、放置すると「実際には答えられなかった」ターンで古い途中経過
                # （in_progress の answer 等）をそのまま見出しにしてしまう。利用者の明示停止は対象外。
                _turn_failed_no_new_message = (
                    _turn_failed and not _agent_msgs[_attempt_msgs_start:] and not _agent_partial
                    and not _stopped_final)
                if _turn_failed_no_new_message:
                    answer = None
                # §2-10: `turn.failed`／`error`（トップレベルイベント）で閉じた attempt は、JSON 自体は
                # 読めていても（got_any_line=True）agent_message が無いままの失敗——`not got_any_line`
                # 単独では拾えないため `_turn_failed` を OR で加える。
                if (not answer and (not got_any_line or _turn_failed) and attempt_returncode is not None
                        and not _stopped_final):
                    _codex_silent_failure = True
                # 自動継続を尽くしてもなお（上限0・セッション非永続で1回も継続できなかった場合・継続
                # attempt が無出力/異常終了で終わった場合を含む）作業宣言だけなら、本文（headline）は
                # 書き換えず印だけ立てる＝「途中までの結果」と伝えて続きを促す。利用者の明示停止は
                # 途中結果として扱わない。
                _codex_stopped_early = (
                    _continuation_pending()
                    and not _stopped_final and not _turn_failed_no_new_message)
                # Feature A: run_dir の新規ファイルを検出して台帳登録する。
                # Codex の cwd = run_dir のため、personal アップロード（files/）は読み取り・書き込み不可。
                # 台帳登録: run_dir の新規ファイルを personal_workspace_files に登録（ES/Neo4j には一切書かない）。
                if run_dir.is_dir():
                    # `.tmp`（TMPDIR）配下は Codex の一時ファイル＝台帳登録しない（成果物のみ登録）。
                    # `.agents`（配備したスキル）配下も同様に対象外（スキルコピーが
                    # 成果物として files/ に誤って登録されないように・毎回作り直しなので前後で常に差分が出る）。
                    # ルート直下の AGENTS.md も対象外（before 側と対・理由はそちらのコメント参照）。
                    _after_ws_files = {
                        p for p in run_dir.rglob("*")
                        if p.is_file() and not p.is_symlink()
                        and p.relative_to(run_dir) != Path("AGENTS.md")
                        and not ({".tmp", ".agents"} & set(p.relative_to(run_dir).parts))
                    }
                    new_authoring = sorted(_after_ws_files - _before_ws_files)
                    for fp in new_authoring:
                        codex_created_files.append(str(fp))
                    _any_new_ws = bool(new_authoring)
                    # Marp レンダは sandbox の外＝Sherpa 本体が network 隔離
                    # （unshare）下で実行する。Codex は .md を書くだけ（sandbox から marp/Chromium を
                    # 見せる必要が無くなり攻撃面も縮小・RUNTIME-SANDBOX §10.3 の未解決問題を回避）。
                    # ベストエフォート（fail-open）: 失敗しても .md 自体は既に台帳登録対象に入っている。
                    try:
                        from ... import marp_render
                        _mds = [p for p in new_authoring if p.suffix == ".md"]
                        _rendered = marp_render.render_outputs(
                            [p for p in _mds if marp_render.is_marp_markdown(p)],
                            marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                            theme_dirs=[run_dir / ".agents" / "skills" / "marp" / "themes",
                                        _SKILLS_BASE / "marp" / "themes"],
                            containment_root=run_dir)   # 入出力を run_dir 内実体に強制
                        codex_created_files.extend(str(p) for p in _rendered)
                        _any_new_ws = _any_new_ws or bool(_rendered)
                    except Exception as e:
                        _log.warning("marp_render: レンダ処理が例外で終了（fail-open）: %s", e)
            # 台帳登録（Codex が authoring/ に置いたファイルを files/ に移動して台帳登録）。
            # Codex 生成物を authoring/ → files/ に移動することで、既存の grep/delete/TTL 機構をそのまま使う。
            # authoring/ に中間生成物が残らないため、次回 Codex 実行時も個人ファイルは見えない。
            if codex_created_files and 'ws_authoring' in dir():
                try:
                    from ... import store as _store
                    import datetime as _dt
                    import shutil as _shutil
                    _ttl_days = int(os.environ.get("SHERPA_WORKSPACE_TTL_DAYS", "90") or 0)
                    _expires = (
                        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=_ttl_days)
                        if _ttl_days > 0 else None
                    )
                    # ws_files が有効（非 symlink）なら files/ に移動して登録。
                    # MEDIUM fix: ws_files が symlink の場合は登録スキップ（fail-closed）。
                    # HIGH fix: files/ 移動時に同名ファイルが存在する場合は別名化（上書き禁止）。
                    _dest_dir = ws_files if (ws_files is not None and ws_files.is_dir()) else None
                    if _dest_dir is None:
                        # symlink or files/ が使えない → fail-closed（登録なし・grep/delete 対象外）。
                        # 成果物は run_dir に残ったまま（move していない）なので、保存失敗として
                        # run_dir を消さず残す・回答へ注記する（黙って削除して見せかけの成功にしない）。
                        _created_files_failed = True
                        _log.warning("codex created files could not be registered: files/ unavailable "
                                    "(run_dir=%s)", run_dir.name)
                    else:
                        for _fp in codex_created_files:
                            try:
                                _p = Path(_fp)
                                if not _p.is_file():
                                    continue
                                _stem, _suf = _p.stem, _p.suffix
                                # 同名回避の**名前確定も lock 内**で行う（並行 HTTP upload と衝突して
                                #   live ファイルを move で上書きするのを防ぐ）。候補名ごとに lock を取り、
                                #   lock 内で「物理未存在かつ生きた台帳なし」を確認できた名前にだけ move+登録する。
                                _i = 0
                                while _i <= 10000:                       # 無限ループ防止
                                    _rel = _p.name if _i == 0 else f"{_stem}_{_i}{_suf}"
                                    _dst = _dest_dir / _rel
                                    with _store.workspace_file_lock(uid, _rel):
                                        if _dst.exists() or not _store.no_live_upload_for_path(uid, _rel):
                                            _i += 1
                                            continue                     # この名前は埋まっている → 次 suffix へ
                                        _shutil.move(str(_p), str(_dst))
                                        try:
                                            _data = _dst.read_bytes()
                                            _sha = hashlib.sha256(_data).hexdigest()
                                            _row = _store.record_workspace_file(
                                                uid, _rel, str(_dst), len(_data), _sha, expires_at=_expires)
                                            _created_file_rows.append(_row)   # P1-c: created_files カード用
                                        except Exception:
                                            # move は成功したが台帳登録に失敗＝files/ に台帳の無い孤児を
                                            # 残さない。同じ lock 内で run_dir 側へ戻す（回収は run_dir
                                            # 保持側の責務に一本化する）。
                                            try:
                                                _shutil.move(str(_dst), str(_p))
                                            except Exception as _move_back_err:
                                                # 差し戻し（2回目の move）にも失敗＝台帳の無い
                                                # ファイルが files/ に取り残る最終形。黙って握り
                                                # 潰さず明示的に記録する（相対パスのみ）。例外を
                                                # そのまま文字列化すると `OSError`/`shutil.Error` は
                                                # 失敗した絶対パスを本文に含むため、型と errno だけ
                                                # 残す（フルパスは出さない）。
                                                _created_files_failed = True
                                                _log.warning(
                                                    "codex created file could not be moved back to "
                                                    "run_dir after registration failure (orphaned in "
                                                    "files/): %s: type=%s errno=%s",
                                                    _masked_run_dir_path(_fp, run_dir),
                                                    type(_move_back_err).__name__,
                                                    getattr(_move_back_err, "errno", None))
                                            raise
                                    break
                            except Exception as e:
                                # 通常の move 失敗（`shutil.move`/`shutil.Error` は失敗した src/dst の
                                # 絶対パスを文字列表現に含む）も、差し戻し失敗と同じく型と errno だけ
                                # 記録する（フルパスは出さない）。
                                _created_files_failed = True
                                _log.warning("codex created file move/registration failed for %s: type=%s errno=%s",
                                            _masked_run_dir_path(_fp, run_dir),
                                            type(e).__name__, getattr(e, "errno", None))
                except Exception as e:
                    # 個別ファイルのループへ入る前の設定段階（store import・_expires 計算等）の失敗。
                    # この時点ではどの成果物も files/ へ移されていない＝全件が run_dir に残ったまま。
                    _created_files_failed = True
                    _log.warning("codex created files registration setup failed (run_dir=%s): %s",
                                run_dir.name, e)
            # A2: troubleshoot は Codex が実際に引いた近傍を UI カードにする（_gather 由来を Codex 実調査由来で上書き）。
            _apply_codex_neighbors(env, mcp_neighbors, decision.get("lens") if decision else None)
            # turn.completed から拾った usage は Codex CLI の契約でセッション累計
            # （last_total_token_usage.total・`codex exec resume` は前回までの累計を復元してから加算する）。
            # 累計値そのものは env["codex_usage_total"] に必ず残す（次ターンの差分計算の元になる・
            # usage が取れなければ両方載せない）。resume が効いた（フォールバックしていない）ターンは、
            # 前ターンの累計（ctx.codex_usage_prev_total）との差分を answer.usage にする——session_id が
            # 今回の resume 先と一致する時だけ（新規セッション・フォールバック・prev 無し・session_id
            # 不一致はいずれも新規セッション相当として累計をそのまま使う＝そのセッションでの初回は
            # 累計と差分が一致する）。
            if codex_usage:
                env["codex_usage_total"] = {
                    "session_id": thread_id,
                    "input_tokens": codex_usage.get("input_tokens"),
                    "cached_input_tokens": codex_usage.get("cached_input_tokens"),
                    "output_tokens": codex_usage.get("output_tokens"),
                    "reasoning_output_tokens": codex_usage.get("reasoning_output_tokens"),
                }
                _prev_total = ctx.codex_usage_prev_total
                if (resume_sid and not _resume_fallback_happened and _prev_total
                        and _prev_total.get("session_id") == resume_sid):
                    env["usage"] = _usage_meta(
                        "codex", codex_usage.get("model"),
                        input_tokens=max(0, (codex_usage.get("input_tokens") or 0)
                                         - (_prev_total.get("input_tokens") or 0)),
                        cached_input_tokens=max(0, (codex_usage.get("cached_input_tokens") or 0)
                                                - (_prev_total.get("cached_input_tokens") or 0)),
                        output_tokens=max(0, (codex_usage.get("output_tokens") or 0)
                                          - (_prev_total.get("output_tokens") or 0)),
                        reasoning_output_tokens=max(0, (codex_usage.get("reasoning_output_tokens") or 0)
                                                   - (_prev_total.get("reasoning_output_tokens") or 0)),
                        is_local=codex_usage.get("is_local"))
                else:
                    env["usage"] = codex_usage
                # 差分計算／累計そのものの両方に同じ深さメタを載せる
                # （`_usage_depth_extra` はこのターンの `_reason`/`_base_reason` 確定時に計算済み）。
                env["usage"].update(_usage_depth_extra)
                # Codex 経路も `sherpa.usage` ログ 1 行（kind=chat・深さ・推論レベル付き）を出す。
                _log_chat_usage(env["usage"], time.monotonic() - _turn_t0, ctx.world)
            # 捕捉した session/thread id を env に載せる（chat_service が `store.set_session_id` で永続化・
            # 次ターンの resume 判定に使う）。ゲートは `_persist_session` 単独ではなく
            # `_session_persistence_enabled`（=conversation_id あり **かつ** サンドボックス有効）を使う。
            # `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral` 実行＝ディスクに残らない使い捨て
            # thread_id なので、ここで DB に保存すると次回サンドボックス復帰後の resume が必ず失敗する
            # （その thread_id は永遠に resume 不能）。conversation_id 無しの直接呼出しでも当然載せない。
            if _session_persistence_enabled and thread_id:
                env["codex_session_id"] = thread_id
            # Feature A/C: Codex がファイルを作成した場合は env に記録（chat_service が contains_personal を立てる）。
            # HIGH 3 fix: files/ 外への書き込みも含めて codex_wrote_files フラグを立てる。
            if codex_created_files or _any_new_ws:
                env["codex_wrote_files"] = [Path(f).name for f in codex_created_files] or True
            # P1-c: 台帳登録に成功したファイルを UI の「作成したファイル」カード用に env へ載せる
            # （既存の /workspace/files DL API を再利用・rel_path は同名衝突回避後の最終名）。
            if _created_file_rows:
                env["created_files"] = [
                    {"name": r["rel_path"], "download_url": f"/workspace/files/{r['id']}/download"}
                    for r in _created_file_rows
                ]
            if answer:
                # 直読した資料は MCP の結果に載らず
                # env["sources"] に反映されない——回答末尾の「参照した資料:」ブロックを解析し、read 系
                # MCP ツール引数から拾った doc_id（参照ブロックの記載漏れの補完・出現順で後ろに合流）と
                # 合わせて機械検証（実在・文書種別・scope・秘匿名除外）を通ったものだけを sources の
                # 先頭へ足す（`_gather` 由来の既存 sources は後ろに残す・doc_id 重複は除外）。
                # verified が0件なら参照ブロックの記載を消しても出典が何も出ない＝本文はそのまま残す
                # （記載が消えて何も出典に出ないより、本文に根拠パスが残るほうを優先）。
                _body, _listed_lines = parse_referenced_doc_lines(answer)
                _ref_candidates: list = list(_listed_lines)
                _ref_seen: set[str] = set()
                for _r in _mcp_read_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                # `xlsx_sheets`（シート一覧のみ）の doc_id も参照候補（sources）
                # には合流させる——実在する資料を出典から隠す理由はない。ただし「参照した資料:」
                # にも書かれておらず、本文精読ツール（`_mcp_read_docs`）でも読まれていない doc_id
                # は、シート一覧を見ただけで根拠ゲート（sources_verified）に数えない——ここまでの
                # 候補（参照ブロック＋本文精読ツール）だけで確定する「精読済み」集合を先に確定させ、
                # `xlsx_sheets` を足した後の verified との差分（`_listed_only_ids`）として区別する。
                _verified_before_listed = set(verified_referenced_docs(_ref_candidates, ctx.world, sp))
                for _r in _mcp_listed_docs:
                    if _r and _r not in _ref_seen:
                        _ref_seen.add(_r)
                        _ref_candidates.append(_r)
                _verified_refs = verified_referenced_docs(_ref_candidates, ctx.world, sp)
                _listed_only_ids = set(_verified_refs) - _verified_before_listed
                if _verified_refs and ctx.make_sources:
                    _ref_sources, _ = _verified_sources(ctx.make_sources, set(_verified_refs), ctx.world, sp)
                    # 参照ブロックの記載順（→ MCP 引数の順）に並べ直し、既存（_gather 由来）と重複する
                    # 資料は先頭側（参照した資料）を残す。
                    _order = {d: i for i, d in enumerate(_verified_refs)}
                    _ref_sources = sorted(_ref_sources, key=lambda s: _order.get(s.get("doc_id"), len(_order)))
                    _ref_ids = {s.get("doc_id") for s in _ref_sources}
                    env["sources"] = _ref_sources + [s for s in (env.get("sources") or [])
                                                     if s.get("doc_id") not in _ref_ids]
                    # 実際に開いて根拠にした資料＝API 経路の「精読済み」と同じ意味＝出典の 2 区分
                    # （根拠／参考）に載せる（画面・共有・改善ログは既存の sources_verified の扱いのまま）。
                    # `xlsx_sheets` だけで到達した doc_id（`_listed_only_ids`）は除く。
                    env["sources_verified"] = sorted(_ref_ids - _listed_only_ids)
                env["codex_referenced_docs"] = {"listed": len(_ref_candidates), "verified": len(_verified_refs)}
                env["headline"] = _body if (_verified_refs and _body.strip()) else answer   # 空本文には差し替えない
                # 実際に回答を生成できたターン＝`_dispatch` がツール遮断時に立てた
                # `agentic_failure`（`agentic_search.tools_blocked_env`）が残っていれば消す
                # （Codex は遮断状態を見ずに調査を続行し得るため、結果が出た後の事実で上書きする）。
                env.pop("agentic_failure", None)
                # 自動継続を尽くしてもなお進行中の宣言文（「次に○○します」等）がそのまま headline に
                # 残ったターン——本文は書き換えない（`answer` は既存どおりそのまま使う）。`_codex_stopped_early`
                # だけを根拠に envelope へ印を付け、chat_service._finalize が予算到達時の途中結果・出典0件時の案内と同形式
                # （headline 直下の独立注記＋案内ボタン）で UI に出す（`stop_reason` の閉じた語彙とは
                # 無関係の別マーカー＝Codex CLI はここを経由しない agentic_search とは別の実行系のため）。
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                yield _node("codex", "think", "Codex が調べる",
                            "調べて回答をまとめました" if ran else "回答をまとめました", "done")
            elif _codex_silent_failure:
                # 利用統計の終了理由分布（`stop_kind_mod.resolve`）がこの分岐を
                # `codex_silent` と判定できるよう印を立てる（値の意味づけは chat_service 側）。
                env["codex_silent_failure"] = True
                # `_gather` が組み立てた決定的回答をそのまま返さない＝利用者に「AI が答えていない」
                # ことが伝わるよう `_UnwiredProvider` と同じ文体の正直な文言に上書きする
                # （summary/sources は `_gather` の実結果のまま残すが、sources が空なら data も
                # `{}` へ揃える＝`chat_service._no_genuine_results` の honest failure 規約と一致させ、
                # 通常の0件検索結果と誤認されて retry_hints・確定文言が付かないようにする）。
                # 同じ無出力失敗はプロキシ/CA 証明書の不備・sandbox の起動失敗・
                # CLI 自体のクラッシュでも起きるため、認証だけに断定しない（閉域ではむしろ
                # プロキシ/ネットワーク要因の方が現実的で、認証と決め打つと現場を誤誘導する）。
                # 観測事実（応答を返す前に終了）を
                # 述べたうえで、考えられる原因を複数挙げる（断定しない）。判別材料（returncode）は
                # 意味が伝わらない利用者向け本文には出さず、ログにだけ残す。stderr は現状 DEVNULL で破棄
                # している（先頭行を出すには stdout/stderr 同時 PIPE 読み取りが要り、デッドロック回避の
                # 追加実装が必要になるため今回のスコープでは見送り＝秘密が混ざり得る文言を利用者へ出さない
                # という制約自体は満たしたまま）。
                # §2-10: `turn.failed`／`error` で閉じた attempt（agent_message 無し）は
                # 「回答を返せずに終了しました」——認証/ネットワーク以外にスキーマ違反
                # （`invalid_json_schema` 等）も原因になり得るため、既存の無出力失敗と文言を分ける。
                _reason = ("回答を返せずに終了しました" if _turn_failed
                          else "応答を返す前に終了しました")
                env["headline"] = (
                    f"Codex に接続できませんでした（Codex CLI が{_reason}）。考えられる原因はいくつかあります: "
                    "認証が設定されていない（`codex login`）／プロキシや CA 証明書などのネットワーク設定が"
                    "不足している／サンドボックスの起動に失敗した／Codex CLI 自体が異常終了した、のいずれかです。"
                    "管理者にログの確認を依頼してください。"
                )
                if not env.get("sources"):
                    env["data"] = {}
                _log.warning(
                    "codex silent failure: returncode=%s turn_failed=%s code=%s conv=%s uid=%s",
                    attempt_returncode, _turn_failed, _turn_failed_code,
                    ctx.conversation_id, uid)
                yield _node("codex", "think", "Codex が調べる",
                            "応答がありませんでした（原因未特定・決定的回答は使いません）", "done")
            else:
                # 本文（answer）は空だが、上の `_codex_silent_failure`（応答を1行も返さない完全な
                # 沈黙）には該当しないケース——command_execution 等は実行できたが結論の
                # agent_message が無いまま（利用者の明示停止等で）打ち切られた。silent failure 分岐は
                # headline 自体で「Codex に接続できませんでした」と既に告知しているため対象外のまま、
                # ここは env["headline"] が `_gather` の決定的回答のままの場合にも注記を出す。
                # `_stream_error` は Popen 完走前（authoring 設定書き込み等）の例外でも立つため、
                # `attempt_returncode is None` のまま `_codex_silent_failure` が計算されずここに落ちても
                # 終了理由の分布から漏らさない（`stop_kind.resolve` の codex_silent 判定に必要な印）。
                if _stream_error:
                    env["codex_silent_failure"] = True
                if _codex_stopped_early:
                    env["codex_stopped_early"] = True
                yield _node("codex", "think", "Codex が調べる", "（未応答のため決定的回答に切替）", "done")
            if _created_files_failed:
                # headline がどの分岐（answer/silent_failure/未応答）で組み立てられていても、
                # 保存できなかった成果物がある事実は一律に伝える。
                env["headline"] = f"{env['headline']}\n\n{_CREATED_FILES_FAILURE_NOTE}"
            yield {"type": "answer_delta", "text": env["headline"]}   # Codex は一括→フロントで段階表示
            yield {"type": "_result", "env": env, "decision": decision}
        finally:
            if _conv_lock_acquired:
                _conv_lock.release()
            if run_dir is not None:
                _release_active_run_dir(run_dir)
                if _created_files_failed:
                    # 保存に失敗した成果物がある run_dir は削除せず回収用に残す
                    # （`_cleanup_stale_run_dirs` の24時間しきい値で最終的に掃除される）。
                    _log.warning("codex run dir kept for recovery due to created-file save failure: %s",
                                run_dir.name)
                else:
                    _remove_dir_best_effort(run_dir)
