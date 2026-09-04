"""`CodexProvider`（リファクタリング計画 フェーズ5 S10・`sherpa/agents.py` から純移動・exec 核）。

Codex CLI サブプロセスの起動・思考イベントへの変換・直列化 lock・headline/progress 判定など、
Codex(gpt-5.5) を頭脳にする実行本体一式をまとめる。`sherpa/agents.py` が facade として本モジュール
から再エクスポートするため、まだ agents.py に残る `_select_provider`/`get_provider`/`provider_info`
（`AGENT_PROVIDERS`・`_UnwiredProvider` も同様）は無改修で動く。

移動した16名: `_humanize_cmd`・`_usage_from_turn_completed`・`_killpg`・`_spawn_stop_watcher`・
`_LAST_MESSAGE_MAX_BYTES`・`_read_last_message_fallback`・`_PROGRESS_VERBS`・`_PROGRESS_END_RE`・
`_PROGRESS_MARKERS`・`_is_progress_only`・`_trim_trailing_progress`・`_pick_codex_headline`・
`_AUTHORING_LOCKS`・`_AUTHORING_LOCKS_GUARD`・`_authoring_lock`・`CodexProvider`。

**危険地雷2〜5（計画書フェーズ5節）を1コミットで解消**: `_AUTHORING_LOCKS`/`_AUTHORING_LOCKS_GUARD`
はプロセス唯一のシングルトンのため、定義（本モジュール）と全利用（`_authoring_lock`・
`CodexProvider.run`）を同時に移し二重定義しない。`CodexProvider.run`/`_run_authoring` は
SSE 生成器の try/finally が唯一のクリーンアップ保証（lock 解放＝`yield from` を包む frame・
Popen ループの finally＝`killer.cancel()`→`_killpg`→`proc.wait(5)`→`shutil.rmtree(codex_home)`）
のため、関数を丸ごと移し生成器フレームを分割するヘルパ抽出は行っていない。`threading.Timer` と
`killer.cancel()` の対、`'ws_authoring' in dir()`（台帳登録ゲート・フレーム内省なので無改修で
移す必要がある）、last-message tempfile の `unlink` 2箇所（ask_user 早期 return・通常経路）も
元コードのまま保持した。

**相対 import の深さ調整（純移動の範囲内・S3〜S9 と同じ判断）**: `_plain_text` 内の
`from . import chat_router`・`_run_authoring` 内の `from . import marp_render`・
`from . import store as _store` は、本モジュールが `sherpa` から2階層深い（providers→codex）ため
`from ... import chat_router`／`from ... import marp_render`／`from ... import store as _store` に
変更した（参照先は変わらず `sherpa.chat_router`/`sherpa.marp_render`/`sherpa.store`）。
`codex_agents_md`/`codex_skills`（`_run_authoring` 内で `codex_agents_md.write_agents_md(...)`・
`codex_skills.deploy_skills(...)` として使う module import）は `from ... import codex_agents_md,
codex_skills` として移した。

**明示変更(a)：`skills_base`（危険地雷1の5番目・`tests/unit/test_agents_surface.py` が S1 時点で
pin を保留していた最後の1値）**: `_run_authoring` 内の marp テーマ探索が使っていた
`Path(__file__).resolve().parent`（agents.py 基準＝`<repo>/sherpa` を指す）は、本モジュールへの
移動で黙って `<repo>/sherpa/providers/codex` を指してしまう（`.resolve().parent` は「1段上」
＝`parents[0]` であり、`parents[N]` のような index 表記ではないため見落としやすい）。
モジュール定数 `_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"` を新設し
（本モジュールは `sherpa` から2階層深いため `parents[2]` で `<repo>/sherpa/skills_base` に戻る＝
他の4値が `parents[1]`→`parents[3]` になったのと同じ「+2」シフト）、`_run_authoring` はこの
定数を参照するだけに直した。`tests/unit/test_agents_surface.py` に `_SKILLS_BASE` の pin テストを
追加した（S1 で「`_run_authoring` の巨大な生成器フレーム内でしか評価できず単体で pin できない」
としていた除外を、定数化により回収）。

**明示変更(b)：`_gather` の facade 実行時解決（危険な継ぎ目・計画書「危険な継ぎ目」節）**:
`tests/unit/test_agents_seams.py`・`tests/unit/test_agents_author.py::
test_authoring_lock_released_on_generator_close` 等が `agents._gather` を monkeypatch して
`CodexProvider().run()`（→`_run_authoring`）経由の介入を検証する。本モジュールは agents.py が
facade re-export のためモジュールレベルで import するため、逆にモジュールレベルで
`from sherpa import agents` すると循環 import になる（base.py の `_gather` 継ぎ目・S8 の
`_mcp_env`/`_toml_str` と同じ理由）。そのため `_run_authoring` 内でのみ関数内 遅延 import
`from sherpa import agents as _facade` して `_facade._gather(ctx)` と実行時解決する。
`CodexProvider.run`/`_busy_run` が呼ぶ `_authoring_lock`・`_plain_run`・`_node` 等、本モジュール内の
他の呼び出しは直接（`_authoring_lock`は同モジュール内・`_plain_run`/`_node`/`_usage_meta`は base.py
から直接 import）でよい（危険な継ぎ目リストに無い・S3〜S9 の教訓と同じ判断）。

依存: `..base`（`Provider`/`Ctx`/`_log`/`_node`/`_plain_run`/`_usage_meta`）・`..prompts`
（`_facts`/`_kb_hint_abs`）・同一パッケージの `.sandbox`（サンドボックス/Marp バイナリ検出/
web_search 引数/authoring config 書込み）・`.mcp`（MCP env/config/neighbors/ask_user 変換）は
兄弟モジュールとして直接 import する（危険な継ぎ目リストに無い＝S3〜S9 の教訓#7・#12 と同じ判断）。
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
import threading
from pathlib import Path
from typing import Iterator

from ... import codex_agents_md, codex_skills, model_catalog
from ... import depth_profile as depth_profile_mod
from ... import layer as layer_mod
from ..base import Ctx, Provider, _log, _node, _plain_run, _usage_meta
from ..prompts import _facts, _kb_hint_abs
from .mcp import (
    _apply_codex_neighbors,
    _codex_ask_capture,
    _codex_mcp_enabled,
    _mcp_config_args,
    _mcp_env,
    _mcp_neighbors_from,
)
from .sandbox import (
    _codex_clean_env,
    _codex_sandbox_enabled,
    _detect_chrome_path,
    _kb_read_roots,
    _marp_bin,
    _openai_endpoint_kind,
    _safe_codex_sessions_home,
    _safe_workspace_authoring,
    _web_search_c_args,
    _web_search_endpoint_note,
    _write_codex_authoring_config,
)

# 明示変更(a): skills_base（危険地雷1の5番目）。本モジュールは `sherpa` から2階層深い
# （providers→codex）ため、agents.py 基準の `Path(__file__).resolve().parent`（＝<repo>/sherpa）と
# 同じ場所を指すには `parents[2]` にする（モジュール docstring 参照）。
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills_base"


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


def _usage_from_turn_completed(event: dict, model: str | None, *, codex_model_provider: str | None = None,
                               system_settings: dict | None = None) -> dict | None:
    """Codex `codex exec --json` の `turn.completed` イベントから usage を取り出す（F3）。

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


def _killpg(proc) -> None:
    """RV MEDIUM: MCP subprocess / shell child まで確実に殺す（creds env の寿命を延ばさない）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_stop_watcher(proc, stop_event) -> "threading.Thread":
    """UI フィードバック1（途中停止・2026-07-03）: `for line in proc.stdout` はブロッキング read のため、
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


_LAST_MESSAGE_MAX_BYTES = 256 * 1024   # RV MEDIUM: 最終メッセージの保険読取は上限付き（256KB）


def _read_last_message_fallback(path: Path) -> str | None:
    """Phase0・§3: `-o <path>` で Codex が書く最終メッセージファイルを読む（`--json` の
    `agent_message` 抽出が空だった時の保険）。無い/空/読取失敗は None（呼び出し側は既存の
    決定的回答フォールバックへ委ねる）。ファイルの削除は呼び出し側の責務（ここでは行わない）。

    RV MEDIUM（2026-07-03）: `.tmp/` は authoring 配下（Codex の書込対象）＝サブプロセスや将来の
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


# ---- F4（2026-07-07）: 回答 headline の選び方（進行中の作業宣言を見出しにしない）----
# Codex は調査中に「これから〜する」という進行形の作業宣言を agent_message として複数回出すことがあり、
# run が timeout/stop で途中終了すると **最後に届いた作業宣言**（実例:「…根拠の有無を切り分けます」）が
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
# High-1（RV・2026-07-07）: 語尾が作業宣言でも「単文の事実記述」（例:「NIGHTLY は税率マスタを起動時に
# 確認します。」）を progress と誤判定して結論を捨てないよう、判定を絞る。手順マーカー（これから何をやる、
# という順序表現）で始まる文は明確に作業宣言。
_PROGRESS_MARKERS = (
    "まず", "次に", "続いて", "これから", "今から", "この後", "最後に", "では", "それでは",
)


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
    if not all(_PROGRESS_END_RE.search(s) for s in sents):
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
    """F4: 集めた複数の agent_message から headline を決定的に選ぶ（LLM 不使用）。

    ①結論を含む最後の message を優先（末尾が作業宣言でも、その中の結論／それ以前の結論を拾う）。
    ②その message の末尾に連なる作業宣言文は落とす（`_trim_trailing_progress`）。
    ③どの message も作業宣言だけなら、最後の message をそのまま返す（本文先頭＝best effort）。
    `partial`＝item.updated だけ来て item.completed が来なかった未完 message（timeout 時の保険）。
    """
    msgs = [m.strip() for m in [*completed, partial] if m and m.strip()]
    if not msgs:
        return ""
    for m in reversed(msgs):
        if not _is_progress_only(m):
            return _trim_trailing_progress(m)
    return msgs[-1]


# RV MEDIUM（Phase1）: 同一 uid の Codex 実行は共有 authoring/ を使うため直列化が必要。
# 並行すると before/after スナップショットと files/ move が交差し、別ターンの成果物を
# created_files として台帳登録・カード表示したり、書込途中のファイルを move で破損させたり、
# `.agents/skills` の毎回 rebuild が実行中のもう片方が読むスキルを消したりする。
# uvicorn workers=1（現行前提・background-chat-turns 提案にも明記）なのでプロセス内 lock で足りる。
_AUTHORING_LOCKS: dict[str, threading.Lock] = {}
_AUTHORING_LOCKS_GUARD = threading.Lock()


def _authoring_lock(uid: str) -> threading.Lock:
    with _AUTHORING_LOCKS_GUARD:
        lk = _AUTHORING_LOCKS.get(uid)
        if lk is None:
            lk = _AUTHORING_LOCKS[uid] = threading.Lock()
        return lk


class CodexProvider(Provider):
    """Codex(gpt-5.5) を**エージェント中核**に（設計どおり）。

    取得（Neo4j/grep）は本物のツールで実行しつつ、**Codex 自身も原文を grep/参照で裏取り**する。
    Codex の **実コマンド実行（grep 等）・推論・回答**を `--json` から拾い **1つずつ思考ノードに流す**
    （ユーザは Codex の作業を逐次見られる）。失敗/未導入/タイムアウトは決定的回答にフォールバック。
    既定 reasoning=low（`SHERPA_CODEX_REASONING` で変更可。RV依頼の xhigh とは別運用）。
    調べる深さ（調べ方ブロック §3.2・SC-6c）が「深く」「最大」のとき、ターンごとに high/xhigh へ
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
        # 重大バグ是正（RV 3巡目 #9）: 未指定（None/空文字）だけを既定 "gpt-5.5" へ解決する。
        # **不正な非空値**（grandfather された旧値・破損 DB・接続確認の直接入力等）は黙って
        # 別モデルへ置換しない＝ honest failure として `InvalidModelNameError`（`ValueError` の
        # サブクラス）を送出する（呼び出し側 `sherpa/providers/__init__.py::_select_provider` が
        # モデル名専用のこの型だけを捕捉し `_UnwiredProvider` として正直に失敗を伝える。他の理由
        # （下記 `SHERPA_CODEX_TIMEOUT` の数値パース失敗等）の `ValueError` と混同しない）。
        # 表示したモデルと実行モデルが食い違う事故を防ぐ。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            raise model_catalog.InvalidModelNameError(f"不正な Codex モデル名です: {model!r}")
        self.model = model or "gpt-5.5"
        self._timeout = float(os.environ.get("SHERPA_CODEX_TIMEOUT", "180"))
        # Phase0・§5-1: ユーザーの希望（設定 codex_web_search）。実際に効くかは管理者フラグ次第
        # （_web_search_disabled_value が admin 許可と AND する）。
        self._web_search = bool(web_search)
        # Codex(Ollama) 構成（`agent_constructs`）のとき、Codex CLI を Ollama へ向ける接続先。
        # None＝Codex(OpenAI)＝従来どおり Codex の既定プロバイダ（OpenAI）を使う。
        # 値は `providers/__init__.py::_select_provider` が SSRF ガード（llm.assert_ollama_url_allowed）
        # を通してから渡す＝ここでは検証済みの前提。
        self._ollama_base_url = ollama_base_url or None
        # S2（Azure OpenAI 対応・2026-08-18）: Codex(OpenAI) 構成で、接続先が既定(api.openai.com)以外
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
        # R1a: 既定は空（`run()` を経由せず `_prompt`/`_prompt_mcp` を直接叩くテスト向けの安全な
        # フォールバック・`_history` は `run()` 冒頭で `ctx.history` から設定し直される）。
        self._history: list = []

    def _history_block(self) -> str:
        """R1a（会話継続）: 直前ターンの履歴を Codex プロンプトへ前置するテキスト。

        `self._history` が空なら空文字列を返す＝呼び出し側の出力は従来と完全同一になる。
        """
        if not self._history:
            return ""
        lines = [f"{'ユーザー' if h.get('role') == 'user' else 'アシスタント'}: {h.get('content', '')}"
                for h in self._history]
        return "【直前の会話（参考・新しいものが下）】\n" + "\n".join(lines) + "\n\n"

    def _prompt(self, message, lens, env, world):
        sys = (self.system_prompt + "\n\n") if self.system_prompt else ""   # 回答方針（#2）を前置
        # MEDIUM-1 fix: cwd が workspace/authoring/ のため KB パスは絶対パスで渡す。
        # Phase0・§2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（質問固有部分のみここに残す）。
        # RV HIGH（2026-07-03）: ただし containment/grounding（KB 以外を読まない・推測しない）は
        # AGENTS.md 書込失敗時（fail-open）でも消えないよう、短縮形をここにも常置する（多層防御・
        # AGENTS.md と重複しても害はない＝独立性を優先）。
        # 探す対象（層フィルタ）が限定されているターンは、この直接 grep 経路（MCP 無効時）自体を
        # 呼び出し元（_run_authoring）が実行しない契約——ここはプロンプト指示による迂回可能な
        # ソフト制御を持たない（正典 §3.4「範囲と同じ硬いフィルタ」・MCP 経由のときだけ実行する）。
        base = (
            "あなたは社内ナレッジ調査エージェントです。以下の資料フォルダ"
            f"（{_kb_hint_abs(world)}）を **grep やファイル参照で実際に調べてください**。"
            "**指定資料フォルダ以外は読まない。事実に無いことは書かない（推測しない）**（詳細ルールは AGENTS.md）。"
        )
        if lens == "author":
            # P1-c（Codex 強化計画 Phase1）: author は回答でなく成果物ファイルを作る。
            return sys + base + (
                "調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。下の『参考（構造化済みの事実）』は補助に使ってよいが、"
                "件数・対象名は事実のまま。最後に**作成したファイル名**と**内容の要約（2〜4文）**を"
                "日本語で報告してください。\n\n"
                # R1a: 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")
        return sys + base + (
            "ユーザの質問に答えてください。"
            "下の『参考（構造化済みの事実）』は補助に使ってよいが、件数・対象名は事実のまま。\n\n"
            # R1a: 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
            f"{self._history_block()}【質問】{message}\n【参考（構造化済みの事実）】{_facts(lens, env)}")

    def _prompt_mcp(self, message, lens, world):
        """MCP 版プロンプト（Phase2b）。事実を前渡しせず、Codex に MCP ツールで自律調査させる。
        Phase0・§2: 出典列挙/文体等の共通ルールは AGENTS.md へ移した（ここは MCP ツール固有の使い分け
        ＋ RV HIGH: containment/grounding の短縮形を常置＝AGENTS.md 書込失敗時の多層防御）。"""
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        base = (
            "あなたは社内ナレッジ調査エージェントです。MCP サーバ『sherpa』のツール"
            "（list_docs＝文書台帳の一覧/件数／ripgrep_search＝全文grep／read_around＝周辺精読／"
            "graph_neighbors＝関係グラフの関連部品／es_search＝日本語全文検索）を使って、"
            f"資料（{_kb_hint_abs(world)}）と関係グラフを**自分で調べてください**。"
            "**MCP ツール以外でのファイル直接読み取りは禁止。事実に無いことは書かない（推測しない）**"
            "（詳細ルールは AGENTS.md）。"
            "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
            "（grep は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれる"
            "ので、名前の部分一致は list_docs の name_pattern で当てる（grep で本文からは探さない）。"
            "表記が揺れそうな語は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。"
            "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
            "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。"
            "原因の手がかりや関連部品（呼び出し/コピー/参照/関連文書）をたどるときは graph_neighbors を使う。"
            # F1（2026-07-07）: 影響を問う質問の分解の型。表層の症状語で検索を乱発させず、変更対象と
            # 影響先の「接続（経路）」の有無を根拠に答えさせる。
            "**影響を問う質問（「〜を変えたら」「〜に影響ある？」「〜が落ちる？」など）では、"
            "①変更対象（例: 税率）に依存する部品・記述を特定 → ②影響先（例: 夜間バッチ＝JCL/ジョブ）を特定 → "
            "③両者の接続（COPY/CALL/REFERENCES の経路）を graph_neighbors で最優先に調べ、経路の有無を"
            "根拠として答える。質問中の症状表現（落ちる/止まる/エラー/停止 等）をそのまま検索語にしない**"
            "（原因調査＝トラブルシュートだと明示された時のみ症状語で探してよい）。"
            # S2: ask_user の使用条件（agentic と同じ制約）＋乱用ガード（確認ID 付きは再質問しない・1回まで）。
            # F2（2026-07-07）: 発動基準を具体化（lens 別の例）＋ユーザー主導の確認要求を確実な発動手段にする。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず現状を簡潔に要約して終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
        )
        if lens == "author":
            # P1-c: author は MCP ツールで根拠を集めたうえで成果物ファイルを authoring 直下に作る。
            # S2: author は列構成・粒度など仕様が曖昧な場面が多い＝着手前の確認が「作ってから直す」より安い。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                # M3 案2: スライド/プレゼンは既定 Marp（見た目重視）・後で PowerPoint 編集なら python-pptx。
                # Codex は marp の .md を書くだけでよい（レンダは Sherpa 側が完了後に自動実行するので、
                # marp CLI の有無をここで判断する必要は無い）。
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約（2〜4文）**を"
                "日本語で報告してください。\n\n"
                # R1a: 履歴があれば【依頼】の前に前置（空文字なら従来と完全同一の出力）。
                f"{self._history_block()}【依頼】{message}")
        # R1a: 履歴があれば【質問】の前に前置（空文字なら従来と完全同一の出力）。
        return sysp + base + f"\n\n{self._history_block()}【質問】{message}"

    def _plain_text(self, message: str = "") -> str:
        # ナレッジ参照オフでは Codex CLI を起動しない（read-only でも grep/ファイル読取が可能で
        # KB を覗けてしまうため・RV High）。
        # 2026-08-15 決定: Codex 構成は資料参照ON固定になったため、通常この経路には来ない
        # （画面はトグルをON固定・`routers/chat.py::_knowledge_for` がサーバ側でも強制）。
        # 内部経路や古いクライアントが knowledge=False で呼んだ場合の安全網としてだけ残す。
        return ("Codex は常に社内資料を参照して回答します。"
                "資料を参照しない雑談は OpenAI／ローカルLLM を選んでください。")

    def run(self, ctx: Ctx) -> Iterator[dict]:
        # R1a: `_GenProvider.run()` と同じく分岐前に確定させる（`_prompt`/`_prompt_mcp` が
        # `_run_authoring` から参照する）。
        self._history = list(ctx.history or [])
        if not ctx.knowledge:                          # ナレッジ参照オフ＝素の会話（Codex を grep なしで・authoring 不使用＝lock 不要）
            yield from _plain_run(self, ctx); return
        # RV MEDIUM（Phase1）: 同一 uid の knowledge=ON 実行を直列化（authoring 共有・_authoring_lock 参照）。
        # 非ブロッキング＝実行中なら待たせず正直に伝える（author の timeout は 600s＝ブロック待ちは不可）。
        # `yield from` を try/finally で包む＝呼び元が途中で generator を close しても確実に解放される。
        _lk = _authoring_lock(ctx.uid or "admin")
        if not _lk.acquire(blocking=False):
            yield from self._busy_run(ctx)
            return
        try:
            yield from self._run_authoring(ctx)
        finally:
            _lk.release()

    def _busy_run(self, ctx: Ctx) -> Iterator[dict]:
        """同一ユーザーの別の Codex 回答が実行中のときの応答（直列化の busy 側・RV MEDIUM）。"""
        yield _node("understand", "think", "質問を理解", "内容を把握しました", "done")
        msg = ("いま、あなたの別の回答（ファイル作成・調査）を実行中です。"
               "同時に実行できるのは1件のため、実行中の回答が終わってからもう一度お試しください。")
        yield _node("codex", "think", "Codex が調べる", "（別の回答を実行中のため今回は実行しません）", "done")
        yield {"type": "answer_delta", "text": msg}
        # "busy": True は chat_service へのマーカー（RV r2 MEDIUM: busy 応答には personal_sources を
        # 添付しない＝実行しなかったターンに個人ファイル抜粋を残さない）。UI は未知キーを無視する。
        # layer_applied を含む scope 契約を早期失敗経路でも保持する（黙って欠落させない）。
        # "busy" マーカー自体は既存どおり（scope_paths/layer の実値は ctx.scope_meta から引き継ぐ）。
        sm = layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")
        sm["source"] = "busy"
        env = {"lens": "qa", "headline": msg, "summary": {"total": 0}, "data": {}, "sources": [],
               "busy": True, "scope": sm}
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": "同一ユーザーの Codex 実行が進行中"}}

    def _run_authoring(self, ctx: Ctx) -> Iterator[dict]:
        decision = env = None
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
        if env is None:                                # _gather が clarify question を出して停止＝確認待ち（RV High）
            return

        yield _node("codex", "think", "Codex が調べる", "資料を調べています", "active")
        answer, ran = None, False
        # T2（2026-08-18・実機報告⑥の隣接ケース）: 閉域キットが Codex CLI を同梱するようになり
        # （scripts/install_offline_kit.sh 7b）、「CLI はあるが認証が無い」状態が現実的になった。
        # このとき codex exec は即座に非ゼロ終了・stdout に JSON を1行も出さない（実測 T1）。
        # 起動前ガード（shutil.which 不在・config書込み例外・.codex-sessions symlink 等）で
        # 一度も codex exec を起動していないケースと区別するため、if ブロック内でだけ True にする
        # （if ブロックが丸ごとスキップされた経路ではこの既定値 False のまま＝既存の決定的回答
        # フォールバックを維持・tests/unit/test_codex_resume.py の pinned "dispatch-headline" と非衝突）。
        _codex_silent_failure = False
        # 同じ理由（起動前ガードで下の if ブロックが丸ごとスキップされる経路がある）で、
        # `env["codex_timed_out"]` を立てる判定用フラグもここで既定 False にしておく——
        # if ブロック内（実際に codex exec を起動できた経路）でだけ実測値へ上書きする。
        _codex_timeout_confirmed = False
        codex_question = None                                    # S2: ask_user 由来の question（出たら env/_result を出さずターン終了）
        codex_usage = None                                       # F3: turn.completed の usage（best-effort・出なければ None）
        # S2 ガード②: 確認ID 付き再送（前の質問への回答）では ask_user を無視＝再質問ループ防止
        # （chat.js が回答再送に `確認ID: {interaction_id}` を必ず含める・chat_router の marker と同流儀）。
        _ask_disabled = bool(re.search(r"確認ID[:：]", ctx.message or ""))
        mcp_neighbors: list = []                                 # A2: Codex が graph_neighbors で引いた近傍（UI カードに反映）
        codex_created_files: list[str] = []                      # Feature A: 実行後に台帳登録する新規ファイルの絶対パス
        _any_new_ws = False                                       # MEDIUM-2 fix: codex 未インストール時の NameError 防止
        _created_file_rows: list[dict] = []                       # P1-c: 台帳登録に成功した行（env["created_files"] 用）
        # Feature A: 専用 authoring ディレクトリを cwd に。BLOCKER-2: 個人アップロード(files/)から分離。
        # MEDIUM-1: KB は絶対パスでプロンプトに渡す。RV BLOCKER: authoring/workspace に symlink が
        #   混入していると封じ込めが崩れるため、_safe_workspace_authoring で symlink 拒否＋fail-closed。
        users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
        uid = ctx.uid or "admin"
        ws_authoring = _safe_workspace_authoring(users_dir, uid)   # None＝fail-closed（Codex 起動しない）
        # R1b（会話継続・Codex ネイティブ resume）: conversation_id があるターンだけセッションを
        # 永続化する（chat_service 経由のチャット呼び出しは常に有り。conversation_id 無しの直接呼出し
        # ＝既存テスト等は従来どおり per-request 使い捨て CODEX_HOME＋`--ephemeral` のまま・無改修）。
        _persist_session = ctx.conversation_id is not None
        resume_sid = ctx.codex_session_id if _persist_session else None
        thread_id = None   # R1b: 捕捉した Codex session/thread id（_session_persistence_enabled の時だけ env に載せる）
        # RV再検証 MEDIUM-2（2026-07-15）: `SHERPA_CODEX_SANDBOX=0`（緊急避難経路）は常に `--ephemeral`
        # 実行のため、そこで捕捉した thread_id は resume 不能（ディスクに残らない）。この専用フラグで
        # 「DB へ永続化してよいか」を判定する（`_persist_session` 単独だと fallback 経路の使い捨て
        # thread_id まで DB に保存し、サンドボックス復帰後の resume が永久に失敗し続ける穴があった）。
        _session_persistence_enabled = _persist_session and _codex_sandbox_enabled()
        # RV再検証 MEDIUM-3（2026-07-15）: 永続 CODEX_HOME（`.codex-sessions/{cid}`）は固定パスのため、
        # 事前に symlink を仕込まれると（未検証のまま書込むと）封じ込めが崩れる。`ws_authoring` と
        # 同じ fail-closed 契約＝安全確認できなければ Codex を起動しない（このターンは決定的回答へ）。
        _safe_persistent_codex_home = None
        _codex_home_ok = True
        if _session_persistence_enabled:
            _safe_persistent_codex_home = _safe_codex_sessions_home(users_dir, uid, ctx.conversation_id)
            _codex_home_ok = _safe_persistent_codex_home is not None
        if shutil.which("codex") and ws_authoring is not None and _codex_home_ok:
            # F4（2026-07-07）: agent_message は run 中に複数届く（作業宣言＋結論）。最後の1件を鵜呑みに
            # せず全部集めて後で結論を選ぶ（`_pick_codex_headline`）。try の外で初期化＝Popen 失敗の
            # except 経路でも NameError にしない。
            _agent_msgs: list[str] = []
            _agent_partial = ""
            # Med-2（RV・2026-07-07）: stream 読取が途中例外で終わったか。例外時は集めた _agent_msgs が
            # 進行中の作業宣言だけの可能性があるため、完全版が入り得る `-o` 最終メッセージファイルを先に試す。
            _stream_error = False
            mcp = _codex_mcp_enabled()                          # Phase2b: MCP ツールで自律調査（既定ON）
            sp = (ctx.scope_meta or {}).get("scope_paths")
            # Codex 自身の追加探索（MCP／直接grep）への層フィルタは qa レンズだけに渡す（探す対象）。
            # author は Codex の追加探索が正典 §1.8 の既知の非対称性（agentic_search.run_tool を
            # 経由しない構成）のため対象外・impact/troubleshoot は非適用（layer.applies_to_lens と
            # 同じ結論だが author も除外するためここでは共通ヘルパーを使わず明示判定する）。
            _layer = (ctx.scope_meta or {}).get("layer") if decision["lens"] == "qa" else None
            _layer_restricted = _layer not in (None, "both")
            # 層を技術的に強制できるのは「MCP 有効（run_tool が層を検証する）」かつ「sandbox 有効
            # （permission profile から KB ルートを外せる・`_write_codex_authoring_config` 参照）」の
            # 組み合わせだけ——sandbox 無効時の fallback（`-s workspace-write`）は読取全開で、MCP を
            # 有効にしていても直接ファイル参照で層を迂回できる（正典 §3.4「範囲と同じ硬いフィルタ」）。
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
            # 実行前の authoring/ スナップショット（新規ファイル検出用）。
            _before_ws_files: set = set()
            _before_ledger_files: set = set()
            if ws_files is not None and ws_files.is_dir():
                _before_ledger_files = set(ws_files.iterdir())
            if ws_authoring.is_dir():
                # P1-b: `.agents`（配備したスキル）配下も `.tmp` 同様に台帳登録スキャン対象外。
                # ルート直下の AGENTS.md も対象外: スナップショット後に write_agents_md() が書くため、
                # 除外しないと初回実行で「新規ファイル」誤認 → files/ へ move（authoring から消える）→
                # 次回また書かれて再検出…と毎回 AGENTS_N.md が台帳に蓄積する（P1-b RV 前修正）。
                _before_ws_files = {
                    p for p in ws_authoring.rglob("*")
                    if p.is_file() and not p.is_symlink()
                    and p.relative_to(ws_authoring) != Path("AGENTS.md")
                    and not ({".tmp", ".agents"} & set(p.relative_to(ws_authoring).parts))
                }
            # reasoning=minimal は image_gen/web_search と非互換で API 400 になる（実証済）→ low へ引き上げ。
            # P1-a: author（作成）のときは intent 連動パラメータ `SHERPA_CODEX_REASONING_AUTHOR`／
            # `SHERPA_CODEX_TIMEOUT_AUTHOR`（既定 medium／600秒）を使う。通常レンズは現行のまま（低負荷優先）。
            _is_author = decision["lens"] == "author"
            # 調べる深さ（調べ方ブロック §3.2・SC-6c）: 通常レンズの基準値だけ管理画面の基準値編集
            # （system_settings）を反映する（author 専用の env は別軸のため対象外・§1.6 の
            # `SHERPA_CODEX_REASONING` に対応する基準値のみ）。標準=基準値のまま・深く=high・
            # 最大=xhigh の per-turn 上書きは author を含む全レンズに一律適用する。
            _base_reason = (os.environ.get("SHERPA_CODEX_REASONING_AUTHOR", "medium") if _is_author
                           else depth_profile_mod.effective_base(
                               self._system_settings, "codex_reasoning", self._reason))
            _reason_raw = depth_profile_mod.codex_reasoning_for(
                _base_reason, (ctx.scope_meta or {}).get("depth_profile"))
            _reason = "low" if str(_reason_raw).lower() == "minimal" else _reason_raw
            _timeout = (float(os.environ.get("SHERPA_CODEX_TIMEOUT_AUTHOR", "600"))
                       if _is_author else self._timeout)
            # MCP でも FS でも同じプロンプト組み立て（personal_facts を注入）。
            if mcp:
                _codex_msg = ctx.message
                if ctx.personal_facts:
                    _codex_msg = (f"{ctx.message}\n\n"
                                  f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}")
                prompt = self._prompt_mcp(_codex_msg, decision["lens"], ctx.world)
            else:
                prompt = self._prompt(ctx.message, decision["lens"], env, ctx.world)
            # Phase0・§3: --ephemeral（セッションをディスクに残さない）と -o（最終メッセージのファイル
            # 出力＝JSON 抽出が空だった時の保険）は sandbox/fallback どちらでも共通。.tmp/ は既存の
            # authoring 新規ファイル走査（台帳登録スキャン）から除外済みのディレクトリ（RV 済み挙動を流用）。
            # 正典 §3.4「範囲と同じ硬いフィルタ」: authoring/ は uid 単位で複数ターンをまたいで
            # 再利用されるため、.tmp に前ターンの残存ファイルがあると、今ターンで層（探す対象）が
            # 限定されていても cwd の直接読取で読めてしまう（層フィルタの迂回路）。ターン開始ごとに
            # 必ず空にし、前ターンの残存を持ち越さない（symlink にすり替わっていれば rmtree が
            # 例外を送出する＝fail-closed）。
            _tmp = ws_authoring / ".tmp"
            if _tmp.exists() or _tmp.is_symlink():
                shutil.rmtree(_tmp)
            _tmp.mkdir(parents=True, exist_ok=True)
            _last_message_path = _tmp / f"last-message-{hashlib.sha1(os.urandom(8)).hexdigest()[:12]}.txt"
            codex_home = None
            if _codex_sandbox_enabled():
                # 検証済 recipe: permission profile で読取を KB(RO)＋authoring(RW) に封じ込め＋env 洗浄。
                # CODEX_HOME は authoring の外（workspace 直下・`:root=deny` で shell から不可視）。
                # R1b（Codex強化計画 決定5）: conversation_id があるターンは会話ごとの固定ディレクトリ
                # （`workspace/.codex-sessions/{cid}`）を CODEX_HOME にして毎ターン再利用する
                # （`sessions/` 配下の JSONL が resume の実体＝下の finally では削除しない）。
                # 無い場合（conversation_id 無しの直接呼出し・既存テスト等）は従来どおり per-request
                # 使い捨て（実行後 rmtree・`--ephemeral`）のまま無改修。
                # RV再検証 MEDIUM-3: `_safe_persistent_codex_home` は外側で既に symlink/workspace外
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
                            "-C", str(ws_authoring), "-m", self.model,
                            "-c", f"model_reasoning_effort={_reason}"]
                if not _session_persistence_enabled:
                    argv_base.append("--ephemeral")
                # S2: `self._openai_api_key` は Codex(OpenAI) 構成で接続先が Azure 等の時だけ
                # `_select_provider` が解決して渡す（それ以外は常に None＝在来どおり env に渡さない）。
                popen_env = _codex_clean_env(codex_home, ws_authoring, _tmp,
                                             openai_api_key=self._openai_api_key)
            else:
                # フォールバック（SHERPA_CODEX_SANDBOX=0）＝旧 `-s workspace-write`（読取全開・多層防御は OS ユーザ分離に依存）。
                # R1b: この緊急避難経路は対象外＝resume 非対応のまま（既存どおり常に使い捨て）。
                # RV再検証 MEDIUM-2: `_session_persistence_enabled` は既に False（サンドボックス無効
                # なので）＝ここで捕捉する thread_id は env に載らない（下の env 組立部分を参照）。
                resume_sid = None
                argv_base = ["codex", "exec", "--json", "--skip-git-repo-check",
                            "--ephemeral", "-o", str(_last_message_path),
                            "-s", "workspace-write", "-C", str(ws_authoring),
                            "-m", self.model, "-c", f"model_reasoning_effort={_reason}"]
                # Phase0・§5-1: --strict-config が無い経路（config.toml でなく -c）なので同等をここで足す。
                argv_base += _web_search_c_args(self._web_search, self._system_settings)
                if mcp:
                    argv_base += _mcp_config_args(ctx.world, sp, _ask_disabled, layer=_layer)
                    popen_env = {**os.environ, **_mcp_env(ctx.world, sp, _ask_disabled, layer=_layer)}
                else:
                    popen_env = None

            def _build_argv(use_resume: bool) -> list:
                """R1b: resume 分岐は `codex exec resume [SESSION_ID] [PROMPT]` の位置引数どおり、
                exec 共通オプションの後・末尾プロンプトの前に `resume <sid>` を挿む。"""
                av = list(argv_base)
                if use_resume and resume_sid:
                    av += ["resume", resume_sid]
                av.append(prompt)
                return av

            got_any_line = False   # R1b: resume 試行で1行も --json イベントを受け取れなければ resume 失敗とみなす
            attempt_returncode = None   # RV再検証 LOW-4: fallback 判定の将来耐性（下の呼出側コメント参照）
            # RV MED（2026-08-18 Codex RV 指摘4）: 無出力失敗の文言を「認証」に決め打ちしないための判別材料。
            # killer（threading.Timer）が実際に発火して group を殺したかどうかを区別する
            # （timeout/kill と CLI の即時異常終了は別の障害系統＝閉域ではプロキシ/CA 不備の方が現実的）。
            _codex_timed_out = False

            def _attempt(use_resume: bool):
                """1回分の codex exec 実行（node/answer_delta を yield）。proc/killer はこの1回限りの
                ローカル状態（呼出側は再試行のたびに新しい Popen を張るだけでよい）。"""
                nonlocal got_any_line, ran, codex_question, codex_usage, thread_id, attempt_returncode
                nonlocal _agent_partial, _stream_error, _codex_timed_out
                got_any_line = False
                attempt_returncode = None
                _codex_timed_out = False
                argv = _build_argv(use_resume)
                proc = killer = None
                try:
                    # Popen 直前の最終防衛線（`_select_provider` の選択時チェックを迂回する経路が
                    # あっても、実際にプロセスを起動する直前でもう一度確認する・多層防御）。
                    # Codex(Ollama) 構成（`self._ollama_base_url` あり）は OpenAI 系 I/O ではないため
                    # 対象外。
                    if self._ollama_base_url is None:
                        from ... import llm
                        llm.assert_openai_io_allowed()
                    # RV MEDIUM: start_new_session で独立プロセスグループにし、timeout/後始末で
                    #   MCP subprocess / shell child まで group ごと確実に殺す（creds env の寿命を延ばさない）。
                    proc = subprocess.Popen(
                        argv, env=popen_env, cwd=str(ws_authoring), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                        start_new_session=True)
                    def _on_timeout():                          # RV MED 指摘4: 発火＝実際に timeout kill したことの記録
                        nonlocal _codex_timed_out
                        _codex_timed_out = True
                        _killpg(proc)
                    killer = threading.Timer(_timeout, _on_timeout)  # 固まっても group ごと打ち切る
                    killer.start()
                    if ctx.stop_event is not None:                # UI フィードバック1: 途中停止（_spawn_stop_watcher 参照）
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
                        if e.get("type") == "thread.started":       # R1b: session/thread id 捕捉（resume 先の id）
                            thread_id = e.get("thread_id") or thread_id
                            continue
                        if e.get("type") == "turn.completed":            # F3: ターンのトークン使用量（item ではない）
                            _u = _usage_from_turn_completed(
                                e, self.model,
                                codex_model_provider="ollama" if self._ollama_base_url is not None else "openai",
                                system_settings=self._system_settings)
                            if _u:
                                codex_usage = _u
                            continue
                        item = e.get("item") or {}
                        it = item.get("type")
                        iid = item.get("id")
                        if not iid:                                      # id 無し item でも node を上書き衝突させない（RV LOW）
                            iid = f"cx-auto-{node_n}"
                            node_n += 1
                        if it == "command_execution":                       # Codex 自身の grep/参照を逐次表示
                            ran = True
                            label, detail = _humanize_cmd(item.get("command", ""))
                            if item.get("status") == "completed" or e.get("type") == "item.completed":
                                ec = item.get("exit_code")
                                yield _node(f"cx-{iid}", "tool", label,
                                            detail + (f"  → exit {ec}" if ec is not None else ""), "done")
                            else:
                                yield _node(f"cx-{iid}", "tool", label, detail, "active")
                        elif it == "mcp_tool_call":                          # A2: Codex の MCP ツール呼びを可視化＋近傍を収集
                            ran = True
                            tool = item.get("tool", "")
                            a = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}  # 非 dict 引数で落とさない（RV LOW）
                            done = e.get("type") == "item.completed" or item.get("status") in ("completed", "failed")
                            if tool == "ask_user":
                                # S2: ask_user は question 優先（agentic の {"question":..}→return と同じ意味論）。
                                # ガード②確認ID 付き再送では無視／③1実行1回（codex_question is None で enforce）。
                                # 質問を捕まえたらループを抜け、finally で proc を後始末してから emit → ターン終了する。
                                if codex_question is None:
                                    codex_question = _codex_ask_capture(item, _ask_disabled)
                                # RV Low-2（2026-07-07）: 捕捉して break する場合は item.completed を待たずに
                                # ループを抜けるため、実際の done フラグに関わらずノードを "done" で確定表示する
                                # （さもないと「ユーザに確認」が実行中表示のまま履歴保存される）。
                                node_done = done or (codex_question is not None)
                                yield _node(f"cx-{iid}", "tool", "ユーザに確認",
                                            f"「{str(a.get('prompt') or '確認が必要です')[:60]}」",
                                            "done" if node_done else "active")
                                if codex_question is not None:
                                    break
                                continue
                            tlabel = {"graph_neighbors": "関係グラフをたどる", "ripgrep_search": "資料を検索（語句そのまま）",
                                      "es_search": "資料を検索（全文）", "read_around": "該当箇所を精読",
                                      "list_docs": "資料の一覧を確認"}.get(tool, "その他の処理")
                            detail = (a.get("name") or a.get("query") or a.get("doc_id")
                                     or a.get("path_prefix") or a.get("name_pattern") or "")
                            if done and tool == "graph_neighbors" and item.get("status") == "completed":
                                mcp_neighbors.extend(_mcp_neighbors_from(item))
                            yield _node(f"cx-{iid}", "tool", tlabel, f"「{detail}」", "done" if done else "active")
                        elif it == "reasoning" and e.get("type") == "item.completed":
                            txt = (item.get("text") or "").strip().splitlines()
                            if txt:
                                yield _node(f"cx-{iid}", "think", "考える", txt[-1][:80], "done")
                        elif it == "agent_message" and e.get("type") in ("item.completed", "item.updated"):
                            # F4: 最後の1件で上書きせず集める（完了分はリストへ・未完分は partial に保持）。
                            # 結論の選択は loop 後に `_pick_codex_headline` で決定的に行う。
                            _txt = (item.get("text") or "").strip()
                            if e.get("type") == "item.completed":
                                if _txt:
                                    _agent_msgs.append(_txt)
                                _agent_partial = ""
                            else:                                    # item.updated＝成長中の未完 message（timeout 保険）
                                _agent_partial = _txt
                except Exception:
                    _stream_error = True
                finally:
                    if killer:
                        killer.cancel()
                    if proc:
                        try:
                            _killpg(proc)                        # group ごと（MCP child 含む）確実に後始末
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        attempt_returncode = proc.returncode   # RV再検証 LOW-4: fallback 判定の材料

            try:
                # AGENTS.md はベストエフォート（書けなくても Codex 実行自体は継続・fail-open）。
                # RV HIGH: fail-open でも気づけるよう warning は残す（containment/grounding の短縮形は
                # _prompt/_prompt_mcp に常置済みなので、書込失敗時も丸裸にはならない＝多層防御）。
                try:
                    codex_agents_md.write_agents_md(ws_authoring)
                except Exception as e:
                    _log.warning("AGENTS.md write failed (fail-open, prompt still has containment): %s", e)
                # P1-b: スキル配備（案A′ ベース＋個人オーバーレイ）も同じくベストエフォート（fail-open）。
                # knowledge=ON の Codex 実行全部で配備する（author レンズに限定しない・progressive disclosure）。
                try:
                    codex_skills.deploy_skills(ws_authoring, uid, users_dir)
                except Exception as e:
                    _log.warning("skills deploy failed (fail-open): %s", e)
                # profile config はここで書く（RV MEDIUM: FileExistsError 等は fail-closed で
                #   例外→except で answer=None→finally で CODEX_HOME 削除→決定的回答へ。古い config での起動を防ぐ）。
                # M3（2026-07-12）: marp/Chromium を read root に追加する必要は無くなった
                # （Codex は .md を書くだけ・レンダは Sherpa 本体側で行う。marp_render.py 参照）。
                # R1b: 会話ごとの CODEX_HOME は毎ターン再利用するため、前ターンの config.toml
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
                        layer=_layer)
                    # S2（Azure OpenAI 対応）: 接続先が Azure 等へリダイレクトされていて、そのせいで
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
                # R1b: resume を試みて1行も --json イベントが出なかった（＝セッション消失等で resume
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
                    yield from _attempt(False)
            except Exception:
                answer = None
                _stream_error = True
            finally:
                if codex_home is not None:
                    if _persist_session:
                        # R1b（決定5）: セッション実体（`sessions/` の JSONL）は次ターンの resume の
                        # ために保持する。creds を含む config.toml だけ即時削除し露出窓を1ターン分に
                        # 限定する（retention のスイープはディレクトリ全体を対象にする＝別途 api.py）。
                        # RV再検証 HIGH-1（2026-07-15）: `auth.json`（実 `~/.codex/auth.json` への
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
            # S2: ask_user が出たターンは question 優先＝env/_result・成果物台帳登録を出さずここで終了する
            # （agentic の {"question":..}→return と同じ意味論・回答は chat.js の整形再送＝新 codex exec で拾う）。
            # proc は直上の finally で後始末済み。chat_service はこの question を answer.question として保存する（S1）。
            if codex_question is not None:
                # RV Low-2: 親ノード（"Codex が調べる"）も冒頭で "active" のまま止まっているので、
                # 通常経路の完了 yield（下の if answer/else ブロック）と同様にここで "done" に確定させる。
                yield _node("codex", "think", "Codex が調べる", "ユーザに確認するため終了しました", "done")
                # RV Low-1（2026-07-07）: 早期 return が `-o` 一時ファイル（last-message-*.txt）の削除を
                # バイパスして .tmp/ に蓄積し得た。通常経路（下の unlink）と同じ best-effort で先に消す。
                try:
                    _last_message_path.unlink(missing_ok=True)
                except Exception:
                    pass
                yield codex_question
                return
            # F4（2026-07-07）: 集めた agent_message から結論を優先して headline を選ぶ
            # （進行中の作業宣言を見出しにしない・最後の1件を鵜呑みにしない）。
            _picked = _pick_codex_headline(_agent_msgs, _agent_partial) or None
            # Phase0・§3: -o は保険。--json の agent_message から拾えなかった時だけ最終メッセージ
            # ファイルを読む（既存の JSON 経路が主）。読んでも読まなくても使い終わったら必ず削除する
            # （.tmp/ は台帳登録スキャン対象外＝放置すると溜まり続けるため）。
            # Med-2（RV・2026-07-07）: 途中例外時は集めた _agent_msgs が進行中の作業宣言だけの可能性が
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
            # T2: codex exec を実際に起動した（attempt_returncode is not None＝Popen が完走した）
            # にもかかわらず stdout に JSON を1行も出さず（got_any_line=False）、answer も得られない
            # 場合だけ「正直に伝える」文言へ切り替える対象とする。ユーザーの stop_event による打ち切り
            # は失敗ではないため対象外（途中で殺しただけで agent_message が無いのは想定内の挙動）。
            _stopped_final = ctx.stop_event is not None and ctx.stop_event.is_set()
            if (not answer and not got_any_line and attempt_returncode is not None
                    and not _stopped_final):
                _codex_silent_failure = True
            # `_codex_timed_out` 単独では、stdout の for ループが EOF で自然終了した直後・
            # `killer.cancel()` が呼ばれる前のわずかな窓で Timer が発火した場合（プロセスは
            # 既に正常終了済み・`_killpg` は既に居ないプロセスへの no-op）も「タイムアウトした」
            # と誤検知しうる。正常終了ならほぼ常に `attempt_returncode == 0`・強制 kill なら
            # 通常非0（Unix ではシグナル由来の負値）になることを併せて要求し、この競合を除外する。
            # 利用者の明示停止（`_stopped_final`）は「時間切れ」ではないため、これも重ねて除外する
            # （if/else 両分岐が参照する唯一の判定値としてここで一度だけ計算する）。
            _codex_timeout_confirmed = (
                _codex_timed_out and attempt_returncode != 0 and not _stopped_final)
            # Feature A: authoring/ の新規ファイルを検出して台帳登録する。
            # Codex の cwd = authoring/ のため、personal アップロード（files/）は読み取り・書き込み不可。
            # 台帳登録: authoring/ の新規ファイルを personal_workspace_files に登録（ES/Neo4j には一切書かない）。
            if ws_authoring.is_dir():
                # `.tmp`（TMPDIR）配下は Codex の一時ファイル＝台帳登録しない（成果物のみ登録）。
                # P1-b: `.agents`（配備したスキル）配下も同様に対象外（スキルコピーが
                # 成果物として files/ に誤って登録されないように・毎回作り直しなので前後で常に差分が出る）。
                # ルート直下の AGENTS.md も対象外（before 側と対・理由はそちらのコメント参照）。
                _after_ws_files = {
                    p for p in ws_authoring.rglob("*")
                    if p.is_file() and not p.is_symlink()
                    and p.relative_to(ws_authoring) != Path("AGENTS.md")
                    and not ({".tmp", ".agents"} & set(p.relative_to(ws_authoring).parts))
                }
                new_authoring = sorted(_after_ws_files - _before_ws_files)
                for fp in new_authoring:
                    codex_created_files.append(str(fp))
                _any_new_ws = bool(new_authoring)
                # M3 案2（2026-07-12）: Marp レンダは sandbox の外＝Sherpa 本体が network 隔離
                # （unshare）下で実行する。Codex は .md を書くだけ（sandbox から marp/Chromium を
                # 見せる必要が無くなり攻撃面も縮小・RUNTIME-SANDBOX §10.3 の未解決問題を回避）。
                # ベストエフォート（fail-open）: 失敗しても .md 自体は既に台帳登録対象に入っている。
                try:
                    from ... import marp_render
                    _mds = [p for p in new_authoring if p.suffix == ".md"]
                    _rendered = marp_render.render_outputs(
                        [p for p in _mds if marp_render.is_marp_markdown(p)],
                        marp_bin=_marp_bin(), chrome_path=_detect_chrome_path(),
                        theme_dirs=[ws_authoring / ".agents" / "skills" / "marp" / "themes",
                                    _SKILLS_BASE / "marp" / "themes"],
                        containment_root=ws_authoring)   # RV BLOCKER: 入出力を authoring 内実体に強制
                    codex_created_files.extend(str(p) for p in _rendered)
                    _any_new_ws = _any_new_ws or bool(_rendered)
                except Exception as e:
                    _log.warning("marp_render: レンダ処理が例外で終了（fail-open）: %s", e)
        # Feature A: 台帳登録（Codex が authoring/ に置いたファイルを files/ に移動して台帳登録）。
        # HIGH fix: Codex 生成物を authoring/ → files/ に移動することで、既存の grep/delete/TTL 機構をそのまま使う。
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
                    pass
                else:
                    for _fp in codex_created_files:
                        try:
                            _p = Path(_fp)
                            if not _p.is_file():
                                continue
                            _stem, _suf = _p.stem, _p.suffix
                            # RV HIGH: 同名回避の**名前確定も lock 内**で行う（並行 HTTP upload と衝突して
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
                                    _data = _dst.read_bytes()
                                    _sha = hashlib.sha256(_data).hexdigest()
                                    _row = _store.record_workspace_file(
                                        uid, _rel, str(_dst), len(_data), _sha, expires_at=_expires)
                                    _created_file_rows.append(_row)   # P1-c: created_files カード用
                                break
                        except Exception:
                            pass
            except Exception:
                pass
        # A2: troubleshoot は Codex が実際に引いた近傍を UI カードにする（_gather 由来を Codex 実調査由来で上書き）。
        _apply_codex_neighbors(env, mcp_neighbors, decision.get("lens") if decision else None)
        # F3（2026-07-07）: turn.completed から拾った usage を answer メタへ（best-effort・停止/ask_user で無ければ載せない）。
        if codex_usage:
            env["usage"] = codex_usage
        # R1b: 捕捉した session/thread id を env に載せる（chat_service が `store.set_session_id` で永続化・
        # 次ターンの resume 判定に使う）。RV再検証 MEDIUM-2: ゲートは `_persist_session` 単独ではなく
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
            env["headline"] = answer
            # タイムアウトで kill され、進行中の宣言文（「次に○○します」等）がそのまま headline に
            # 残ったターン——本文は書き換えない（`answer` は既存どおりそのまま使う）。`_codex_timed_out`
            # （threading.Timer が実際に発火したかという機械的事実・文面マッチはしない）だけを根拠に
            # envelope へ印を付け、chat_service._finalize が STOP-1/SC-6d と同形式（headline 直下の
            # 独立注記＋案内ボタン）で UI に出す（`stop_reason` の閉じた語彙とは無関係の別マーカー
            # ＝Codex CLI はここを経由しない agentic_search とは別の実行系のため）。
            if _codex_timeout_confirmed:
                env["codex_timed_out"] = True
            yield _node("codex", "think", "Codex が調べる",
                        "調べて回答をまとめました" if ran else "回答をまとめました", "done")
        elif _codex_silent_failure:
            # T2: `_gather` が組み立てた決定的回答をそのまま返さない＝利用者に「AI が答えていない」
            # ことが伝わるよう `_UnwiredProvider` と同じ文体の正直な文言に上書きする
            # （summary/sources は `_gather` の実結果のまま残すが、sources が空なら data も
            # `{}` へ揃える＝`chat_service._no_genuine_results` の honest failure 規約と一致させ、
            # 通常の0件検索結果と誤認されて retry_hints・確定文言が付かないようにする・RV2 #2）。
            # RV MED（2026-08-18 Codex RV 指摘4）: 以前は「認証されていない可能性があります」と
            # 断定していたが、同じ無出力失敗は timeout kill・プロキシ/CA 証明書の不備・sandbox の
            # 起動失敗・CLI 自体のクラッシュでも起きる。閉域ではむしろプロキシ/ネットワーク要因の方が
            # 現実的で、認証と決め打つと現場を誤誘導する。観測事実（応答を返す前に終了／timeout）を
            # 述べたうえで、考えられる原因を複数挙げる（断定しない）。判別材料（timeout・returncode）は
            # 意味が伝わらない利用者向け本文には出さず、ログにだけ残す。stderr は現状 DEVNULL で破棄
            # している（先頭行を出すには stdout/stderr 同時 PIPE 読み取りが要り、デッドロック回避の
            # 追加実装が必要になるため今回のスコープでは見送り＝秘密が混ざり得る文言を利用者へ出さない
            # という制約自体は満たしたまま）。
            _reason = "タイムアウトしました" if _codex_timed_out else "応答を返す前に終了しました"
            env["headline"] = (
                f"Codex に接続できませんでした（Codex CLI が{_reason}）。考えられる原因はいくつかあります: "
                "認証が設定されていない（`codex login`）／プロキシや CA 証明書などのネットワーク設定が"
                "不足している／サンドボックスの起動に失敗した／Codex CLI 自体が異常終了した、のいずれかです。"
                "管理者にログの確認を依頼してください。"
            )
            if not env.get("sources"):
                env["data"] = {}
            _log.warning(
                "codex silent failure: timed_out=%s returncode=%s conv=%s uid=%s",
                _codex_timed_out, attempt_returncode, ctx.conversation_id, uid)
            yield _node("codex", "think", "Codex が調べる",
                        "応答がありませんでした（原因未特定・決定的回答は使いません）", "done")
        else:
            # 本文（answer）は空だが、上の `_codex_silent_failure`（応答を1行も返さない完全な
            # 沈黙）には該当しないケース——command_execution 等は実行できたが結論の
            # agent_message が無いままタイムアウトで打ち切られた。silent failure 分岐は headline
            # 自体で「Codex に接続できませんでした」と既に告知しているため対象外のまま、
            # ここは env["headline"] が `_gather` の決定的回答のままの場合にも注記を出す
            # （`_codex_timeout_confirmed` は明示停止・returncode 0 競合を既に除外済み）。
            if _codex_timeout_confirmed:
                env["codex_timed_out"] = True
            yield _node("codex", "think", "Codex が調べる", "（未応答のため決定的回答に切替）", "done")
        yield {"type": "answer_delta", "text": env["headline"]}   # Codex は一括→フロントで段階表示
        yield {"type": "_result", "env": env, "decision": decision}
