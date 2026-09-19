"""Codex authoring 用サンドボックス機構（permission profile 方式の読取封じ込め＋Marp レンダ用バイナリ
検出＋web_search 既定 OFF ポリシー）。docs/08-実行権限と隔離.md / memory
`codex-sandbox-permission-profile` と対応付け。`sherpa/agents.py` が facade として本モジュールを
再エクスポートする（`CodexProvider` は `codex/provider.py`・`_select_provider` は
`providers/__init__.py` にあり、本モジュールの利用者はどちらも providers パッケージ内の兄弟）。

本モジュール（`sherpa/providers/codex/sandbox.py`）は `sherpa/agents.py` より2階層深い
（providers→codex）ため、repo root 基準のパスは `Path(__file__).resolve().parents[3]`
（`_kb_read_roots`・`_marp_bin`・`_write_codex_authoring_config` 内の MCP サブプロセス
PYTHONPATH）、相対 import は `from ... import worlds` になる（参照先は変わらず `sherpa.worlds`）。
`tests/unit/test_agents_surface.py` の pin テストは `pathlib.Path(sherpa.agents.__file__)
.resolve().parents[1]`（facade は常に `sherpa/agents.py` を指す）と比較するため、両者が同じ
実パス（repo root）を指すことで担保される。

`_mcp_env`・`_toml_str` は兄弟モジュール `.mcp` から直接 import する（一方向 sandbox→mcp
なので循環なし）。どちらもテストが facade attribute を **patch する**名前ではない（facade 経由の
直接**呼び出し**のみ）ため patch 素通りの懸念はない（「危険な継ぎ目」リストは `_gather`／
`BedrockProvider`／`_bedrock_auth_available` のみ）。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

from ..base import _log
from .mcp import _mcp_env, _toml_str


# ---- Codex authoring の permission-profile サンドボックス（読取封じ込め）----
# `-s workspace-write` は書込を cwd に封じるが**読取が FS 全開**＝他人 workspace・秘密が読める。
# Codex 0.139 の permission profile（default_permissions）で**読取も KB(RO)＋authoring(RW) に封じ込める**。
# 検証・落とし穴は docs/notes/2026-07-01-codex-authoring-sandbox.md / memory codex-sandbox-permission-profile。
def _codex_sandbox_enabled() -> bool:
    """既定 ON。SHERPA_CODEX_SANDBOX=0 で旧 `-s workspace-write` にフォールバック（緊急時の逃げ道）。"""
    return os.environ.get("SHERPA_CODEX_SANDBOX", "1").strip().lower() not in ("0", "false", "no", "off", "")


# DEPTH-2 S3b（実機確認・§9.1）: `[agents.worker]` の `model` は Codex CLI 自身のモデルカタログ
# （`codex debug models`）にある値でなければ spawn_agent が解決エラーになる。Sherpa の
# `model_catalog.py` 既定（subsearch 用途＝`gpt-5.4-mini`）はこのカタログに無く使えないことを
# 実機で確認済み（`gpt-5.6-sol` は使えた）。S6 で `_write_codex_authoring_config(multi_agent=True)`
# がこの値を worker の config_file（`_write_codex_agent_role_configs`）へ書く。
_CODEX_WORKER_MODEL_FALLBACK = "gpt-5.6-sol"

# S6（§2.6）: `[agents].max_concurrent_threads_per_session`。新規設定は増やさない
# （提案書「設定は増やさない」）——worker/evaluator の同時起動を抑える固定値で、会話単位ロック
# （1会話1 codex exec）の内側にとどまる保守的な既定（提案書の「既定 2〜3」の下限）。
_CODEX_MAX_CONCURRENT_SUBAGENTS = 2


def codex_multi_agent_enabled(*, ollama_base_url: str | None, system_settings: dict | None = None) -> bool:
    """`[agents.worker]`/`[agents.evaluator]`（S6・§2.6）を有効にするかどうかの唯一の判定。
    `_write_codex_authoring_config` の `multi_agent` 引数・argv の `-c features.multi_agent=true`・
    `write_agents_md` の役割段落は全てこの関数を呼び、独立に条件式を複製しない。
    doctor（`check_codex_multi_agent_worker_model`）は接続先種別だけを独自に確認する。

    真になるのは次の条件を全て満たすときだけ:
      - サンドボックス（permission profile）が有効（`_codex_sandbox_enabled()`）: `[agents.*]` は
        `codex_home`（サンドボックス有効時だけ作られる per-request CODEX_HOME）配下の config.toml に
        書く。フォールバック経路（`SHERPA_CODEX_SANDBOX=0`）は config.toml 自体を書かないため、
        `-c features.multi_agent=true` だけを渡しても spawn 先の層が無い。Codex(Ollama) 構成は
        `_select_provider`（`providers/__init__.py::_codex_ollama_sandbox_disabled_reason`）が
        サンドボックス無効時にそもそも未接続を返すため、ここへ到達する Ollama 呼び出しは実質常に
        サンドボックス有効。
      - Codex(Ollama) 構成なら常に有効（worker/evaluator の `model` は `_codex_worker_model()` が
        本体 Codex と同じモデルタグへ倒す——親が到達できているモデルなら子も到達できる）。
      - Codex(OpenAI) 構成では、接続先が既定(OpenAI)／Azure、または独自エンドポイント（custom）でも
        `codex_worker_model` が明示設定済み（決定2026-09-19＝初期構成の既定＋RV是正）。Azure は
        `_codex_worker_model()` が未設定時に本体 Codex と同じデプロイ名へ倒すため常に有効だが、
        custom は本体のモデル名を流用できる保証が無く（custom は任意の OpenAI 互換 API・デプロイ名の
        規約がまちまち）、未設定のまま有効化すると worker が `_CODEX_WORKER_MODEL_FALLBACK` を
        spawn しに行き毎ターン失敗する——custom は明示設定を必須条件にする。
    """
    if not _codex_sandbox_enabled():
        return False
    if ollama_base_url is not None:
        return True
    if _openai_endpoint_kind(system_settings) == "custom":
        # `system_settings` は省略可（呼び出し側のスナップショット省略時は DB から都度読む）——
        # `_openai_endpoint_kind` と同じ解決規則（`llm._openai_endpoint_settings`）を使う。
        # `system_settings` の raw None を素朴に isinstance チェックすると、省略呼び出し
        # （`system_settings=None`）で保存済み `codex_worker_model` を見落として常に False になる。
        from ... import llm as _llm
        _resolved = _llm._openai_endpoint_settings(system_settings)
        return isinstance(_resolved, dict) and bool(
            str(_resolved.get("codex_worker_model") or "").strip())
    return True


def _codex_worker_model(system_settings: dict | None = None, *, main_model: str | None = None,
                        ollama: bool = False) -> str:
    """`[agents.worker]` の `model` に使う値（S6 で `[agents.*]` を生成するときに呼ぶ）。

    優先順位:
      1. 管理画面の system_settings `codex_worker_model`（空／未設定でなければそのまま使う）。
      2. Ollama 構成（`ollama=True`）、または Azure 接続（`llm.openai_endpoint_kind() == "azure"`）
         かつ `main_model`（本体 Codex が使うモデルタグ／デプロイ名＝呼び出し元の `self.model`）が
         渡されていれば、それをそのまま使う——Ollama は本体・worker とも同じローカルモデルタグを
         使う（親が到達できているモデルなら子も到達できる）。Azure のモデル名は管理者が登録した
         デプロイ名固有で、Sherpa 固定のカタログ値（`_CODEX_WORKER_MODEL_FALLBACK`）がそのデプロイに
         存在するとは限らない一方、本体 Codex が既に到達できているデプロイ名なら worker からも
         到達できる。
      3. それ以外（既定 OpenAI・独自エンドポイント・`main_model` 未指定）は
         `_CODEX_WORKER_MODEL_FALLBACK`（実機確認済みの安価枠。Ollama 構成では `main_model` が
         常に渡る契約なのでここへは落ちない）。
    """
    if isinstance(system_settings, dict):
        configured = system_settings.get("codex_worker_model")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
    if main_model:
        if ollama:
            return main_model
        from ... import llm as _llm
        if _llm.openai_endpoint_kind(system_settings) == "azure":
            return main_model
    return _CODEX_WORKER_MODEL_FALLBACK


def _write_codex_agent_role_configs(codex_home: Path, *, worker_model: str, worker_reasoning: str,
                                    evaluator_model: str, evaluator_reasoning: str,
                                    provider_lines: list | None = None) -> tuple[str, str]:
    """`[agents.worker]`/`[agents.evaluator]` の `config_file` 実体（役割ごとの層＝モデル・推論
    レベルだけを持つ最小 TOML）を `codex_home/agents/` 配下に書く（S6・§2.6）。

    `codex_home` は permission profile 上 `":root" = "deny"` の外側＝model-shell から不可視
    （呼び出し元 `_write_codex_authoring_config` の docstring・`sidecar_path` と同じ理由）。
    Codex CLI 自身（サンドボックスの対象外プロセス）がこのパスを直接開くだけで、model-shell が
    書き換えたり読んだりする経路には無い。戻り値は (worker の絶対パス, evaluator の絶対パス)。

    `provider_lines`（省略可・RV是正）: 親 config.toml に書いたのと同じ `model_provider = "…"`／
    `[model_providers.<name>]` 行（`_openai_compat_provider_lines` の戻り値）。role config は
    親と**別プロセス**として spawn される子 Codex の設定のため、これを書かないと子は組み込みの
    `openai` provider（本家 api.openai.com）へ出てしまい、Azure/custom 接続時に資料本文が
    意図しない宛先（本家 OpenAI）へ渡る。Codex(Ollama) 構成も同じ理由で `_ollama_provider_lines` を
    渡す。`None`（既定 OpenAI）だけ何も書かない。
    """
    d = codex_home / "agents"
    d.mkdir(parents=True, exist_ok=True)
    worker_path = d / "worker.toml"
    evaluator_path = d / "evaluator.toml"
    _provider_block = ("\n" + "\n".join(provider_lines) + "\n") if provider_lines else ""
    worker_path.write_text(
        f'model = {_toml_str(worker_model)}\n'
        f'model_reasoning_effort = {_toml_str(worker_reasoning)}\n' + _provider_block, encoding="utf-8")
    evaluator_path.write_text(
        f'model = {_toml_str(evaluator_model)}\n'
        f'model_reasoning_effort = {_toml_str(evaluator_reasoning)}\n' + _provider_block, encoding="utf-8")
    return str(worker_path), str(evaluator_path)


def _kb_read_roots(world: str) -> list:
    """permission profile に read を許す KB の絶対パス（fixtures か実 world root・無ければ data/kb 全体）。"""
    from ... import worlds
    repo_root = Path(__file__).resolve().parents[3]
    roots: list = []
    try:
        if worlds._fixtures():
            base = repo_root / "fixtures" / "corpus" / world
            if base.exists():
                roots.append(str(base.resolve()))
        else:
            wd = worlds.world_dir(world)
            if wd:
                roots.append(str(Path(wd).resolve()))
    except Exception:
        pass
    if not roots:
        roots.append(str((repo_root / "data" / "kb").resolve()))
    return roots


def _direct_read_roots(world: str, scope_paths=None) -> list:
    """原本直読（Codex がコードインタープリターで直接開く）で permission profile に read を許す
    絶対パスの一覧＝KB root（`_kb_read_roots`）＋派生ルート（`derived_md_dir`／`derived_rag_dir`・
    存在するもののみ）。正典＝docs/proposals/2026-09-10-Codex原本直読と調査スキル.md §2-1/§2-2。

    範囲（scope）の限定は read root を狭めるのではなく、`_scope_deny_entries` が「範囲の経路上に
    ない兄弟（フォルダ・ファイル）」を個別 deny することで実現する——Codex サンドボックス
    （bubblewrap）では親フォルダの deny が子フォルダの read に勝ち、部分木だけを read にしても
    辿れない（実測 2026-09-10）。`scope_paths` は互換のため受けるが本関数では使わない。
    """
    from ... import worlds

    roots = list(_kb_read_roots(world))
    for fn in (worlds.derived_md_dir, worlds.derived_rag_dir):
        try:
            d = fn(world)
            if d.exists():
                roots.append(str(d.resolve()))
        except OSError:
            continue
    return roots


def _scope_deny_entries(roots: list, scope_paths, *, max_entries: int = 2000) -> list:
    """範囲（scope）を permission profile で硬く効かせるための deny 一覧（絶対パス）。

    各 root について、選択された scope パスの経路上にある各フォルダを開き、**経路上でも選択先
    でもない兄弟エントリ**を deny にする（root 自体は read のまま＝サンドボックスは親の deny が
    子の read に勝つため、部分木の read ではなく兄弟の deny で範囲を表す）。symlink は deny に
    書かない（bubblewrap は symlink への deny マスクを作れず起動に失敗する。symlink の実体が
    範囲外なら実体側の deny で読めない・root 外なら `:root=deny` で読めない）。
    選択された scope が root 配下に 1 つも存在しない root は、root 自体を deny にする。
    scope が空（範囲なし）なら空リスト。deny 件数が `max_entries` を超えたら `RuntimeError`
    （fail-closed＝直読を許可しない）。走査中の OSError も同じく `RuntimeError`（種別のみ）。
    """
    from ...scope import normalize_scope_paths

    sel = normalize_scope_paths(scope_paths)
    if not sel:
        return []
    out: list = []

    def _fail(reason: str) -> None:
        raise RuntimeError(f"scope_enum_failed:{reason}")

    for root_s in roots:
        root = Path(root_s)
        try:
            root_r = root.resolve()
        except OSError as e:
            _fail(type(e).__name__)
        keep: list = []                                  # root 配下に実在する scope（相対の PurePath）
        for sp in sel:
            cand = root_r / sp
            try:
                cand_r = cand.resolve()
                cand_r.relative_to(root_r)              # `..`／symlink 脱出は捨てる
            except (OSError, ValueError):
                continue
            if cand.exists() and not cand.is_symlink():
                keep.append(Path(sp))
        if not keep:
            out.append(str(root_r))                      # 範囲がこの root に無い＝root ごと deny
            continue
        # 親子で選ばれた scope（A と A/sub）は親だけ残す＝A 配下は全部範囲内（A/other を deny しない）
        keep = [k for k in keep if not any(a != k and a in k.parents for a in keep)]
        # 経路上のフォルダ（root, root/a, root/a/b, ...）を重複なく列挙する
        chain_dirs: list = [Path()]
        for k in keep:
            for anc in reversed(k.parents):
                if anc != Path() and anc not in chain_dirs:
                    chain_dirs.append(anc)
        for rel_dir in chain_dirs:
            d = root_r / rel_dir
            if d.is_symlink():
                _fail("symlink_dir")                     # 経路上の symlink を跨ぐ deny は起動失敗になる
            try:
                entries = sorted(os.listdir(d))
            except OSError as e:
                _fail(type(e).__name__)
            for name in entries:
                rel = rel_dir / name
                if any(rel == k or rel in k.parents for k in keep):
                    continue                             # 選択先そのもの、または経路上
                if (d / name).is_symlink():
                    continue
                out.append(str(d / name))
                if len(out) > max_entries:
                    _fail("max_entries_exceeded")
    return out


def _venv_root() -> Path | None:
    """app の Python 実行環境（`.venv`）の絶対パス。`sys.prefix != sys.base_prefix` のとき、
    その venv を Codex へ read で見せる（Office ライブラリを Codex から使わせる）。
    venv 内で実行されていない（システム python 等）ときは None——read 行も PATH 補完も足さない。"""
    if sys.prefix != sys.base_prefix:
        try:
            return Path(sys.prefix).resolve()     # symlink 配備でも実体パス（profile は symlink を跨ぐ行で起動失敗する）
        except OSError:
            return None
    return None


def _enumerate_sensitive(roots: list, *, max_hits: int = 200, max_files: int = 200_000) -> list:
    """`roots` 配下（再帰）の秘匿名ファイルを絶対パスで列挙する（fail-closed）。秘匿の定義は
    `text_kind.is_sensitive` に一本化（判定を複数箇所に散らさない）。通常は数件・上限超過は直読を諦める。
    app の `.venv` も同じ関数で再帰する（実測 1.8 万ファイルで 0.1 秒・ライブラリ内の
    `credentials.py`／`cacert.pem` が数件 deny になるが、Codex が Office を開くのに要らない）。

    symlink は `followlinks=False` によりディレクトリとしては辿らない。symlink 自体が秘匿名なら、
    **実体**が `roots` のどれかの配下にある通常ファイルのときだけ実体のパスを deny に入れる
    （bubblewrap は symlink への deny マスクを作れず起動に失敗する・実体が root 外なら
    `:root=deny` で読めない・dangling なら読めない）。戻り値は重複なし・ソート済み。

    上限超過（`max_hits` を超える秘匿ファイル、または `max_files` を超える走査ファイル数）、または
    走査中の OSError（PermissionError 等・アクセス不能ディレクトリ）は `RuntimeError` を送出する——
    呼び出し元はこれを捕捉して直読を許可しない（MCP のみへ縮退）。例外メッセージは種別のみ
    （`sensitive_enum_failed:<種別>`）＝パス・内容はログに出さない契約。
    """
    from ...ingest import text_kind

    out: set = set()
    scanned = 0
    root_paths: list = []
    for r in roots:
        try:
            root_paths.append(Path(r).resolve())
        except OSError as e:
            raise RuntimeError(f"sensitive_enum_failed:{type(e).__name__}")

    def _fail(reason: str) -> None:
        raise RuntimeError(f"sensitive_enum_failed:{reason}")

    def _on_walk_error(exc: OSError) -> None:
        _fail(type(exc).__name__)

    def _inside_roots(p: Path) -> bool:
        for rp in root_paths:
            try:
                p.relative_to(rp)
                return True
            except ValueError:
                continue
        return False

    for root in root_paths:
        if not root.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False, onerror=_on_walk_error):
            for name in filenames:
                scanned += 1
                if scanned > max_files:
                    _fail("max_files_exceeded")
                if not text_kind.is_sensitive(name, Path(name).suffix.lower()):
                    continue
                p = Path(dirpath) / name
                if p.is_symlink():
                    try:
                        target = p.resolve(strict=True)
                    except OSError:
                        continue                          # dangling＝読めない
                    if not (target.is_file() and _inside_roots(target)):
                        continue                          # root 外＝`:root=deny` で読めない
                    p = target
                out.add(str(p))
                if len(out) > max_hits:
                    _fail("max_hits_exceeded")
    return sorted(out)


def _prune_deny_paths(deny: list, read_roots: list) -> list:
    """permission profile に書く deny 行を整える: 重複を除き、read 対象（root）の配下に無いもの・
    実在しないもの・symlink を落とし、既に deny されるフォルダの配下にある deny を落とす
    （bubblewrap は deny 済みフォルダ内や symlink・不在パスへの deny マスクで起動に失敗する）。
    read root そのものへの deny（直読不許可・範囲が root に無い）は残す。"""
    roots: list = []
    for r in read_roots:
        try:
            roots.append(Path(r).resolve())
        except OSError:
            continue
    cands: list = []
    for d in sorted(set(deny)):
        p = Path(d)
        if p.is_symlink() or not p.exists():
            continue
        if not any(p == rp or rp in p.parents for rp in roots):
            continue
        cands.append(p)
    out: list = []
    for p in cands:                                       # ソート済み＝親が先に来る
        if any(kept in p.parents for kept in out):
            continue
        out.append(p)
    return [str(p) for p in out]


# 親環境に**設定されているときだけ** Codex へ透過する変数（閉域実機の是正・2026-08-18）。
# プロキシ経由でしか外へ出られない閉域では、これが届かないと Codex（と web 検索）が OpenAI に到達できない。
# MITM 型プロキシなら社内 CA も要る。いずれも**接続経路の設定であって creds（DB/ES/KB/API キー）ではない**
# ＝「creds を渡さない」という上の契約は保たれる（プロキシ URL に認証を埋める運用は利用者の判断で、
# それは OpenAI に送るものではなくプロキシへの接続情報）。大文字・小文字の両方を見る（curl/Node は小文字も読む）。
_CODEX_PASSTHROUGH_ENV: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)


def _codex_clean_env(codex_home: Path, authoring: Path, tmpdir: Path,
                     openai_api_key: str | None = None) -> dict:
    """codex exec 用の最小 env（env -i 相当）。**DB/ES/KB creds を渡さない**・PATH 等ランタイムのみ。
    creds が要る MCP サブプロセスへは config ファイル(mcp_servers.sherpa.env)経由で渡す（プロセス env に置かない）。
    例外はプロキシ/CA の経路設定（`_CODEX_PASSTHROUGH_ENV`）で、親環境に**あるときだけ**そのまま渡す。

    `openai_api_key`（Azure OpenAI 対応）: **既定 None＝従来どおり渡さない**（回帰ゼロ・
    `test_codex_clean_env_has_no_secrets`／`test_codex_clean_env_passes_proxy_and_ca_only_when_set`
    は引数省略で呼び、親環境に `OPENAI_API_KEY` があっても env に出ないことを固定している）。

    非 None を渡すのは `_write_codex_authoring_config` が Codex(OpenAI) 構成で接続先を Azure 等の
    カスタム `model_providers.<id>` へ差し替えた時**だけ**（`_openai_compat_provider_lines` 参照）。
    その独自プロバイダは `env_key = "OPENAI_API_KEY"` で**子プロセスの環境変数**からキーを読む設計
    （Codex 公式ドキュメント確認済み・`auth.json`/ChatGPT ログインは `requires_openai_auth = true` を
    明示した provider だけが使う別経路で、本カスタム provider はそれを設定していない）。一方、既定
    （OpenAI 直結・組込み `openai` provider）は引き続き `auth.json`（実 home からの symlink）経由の
    ままで、この関数に env として渡す必要が無い＝呼び出し元（provider.py）はこの構成の時だけ
    `openai_api_key` を渡す（他の全呼び出しは省略＝この docstring 追記だけでは何も変わらない）。

    裁定: app の `.venv`（`_venv_root()`）で動いているときだけ、
    PATH の**先頭**に `<venv>/bin` を足す（Office ライブラリ入りの python を Codex が優先して
    掴む・venv が無ければ従来どおり）。"""
    _venv = _venv_root()
    _base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env = {
        "PATH": f"{_venv / 'bin'}:{_base_path}" if _venv is not None else _base_path,
        "HOME": str(authoring),
        "CODEX_HOME": str(codex_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(tmpdir),
    }
    for name in _CODEX_PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value:                                   # 空文字は「未設定」と同じ＝渡さない
            env[name] = value
    if openai_api_key:                              # 明示的に渡された時だけ（既定 None は従来どおり無し）
        env["OPENAI_API_KEY"] = openai_api_key
    return env


# ---- Marp（スライド作成スキル）レンダ用のバイナリ検出（RUNTIME-SANDBOX §9 の実証結果 / §10.3 の
#      未解決問題を踏まえた設計）----
# Codex は sandbox 内で .md を書くだけ（marp CLI を直接呼ばない）。レンダ（HTML/PDF/PPTX）は
# Codex 完了後に Sherpa 本体プロセスが marp_render.py 経由でこの marp CLI・Chromium を使って
# 実行する（sandbox の外＝permission profile の read root に marp/Chromium を足す必要が無い）。
def _marp_bin() -> str | None:
    """marp CLI 実行ファイルの絶対パス（存在＆実行可能な時だけ）。env `SHERPA_MARP_BIN` で明示上書き可。
    未解決なら None＝marp_render.render_outputs() は何もしない（.md のみが成果物）。"""
    override = os.environ.get("SHERPA_MARP_BIN")
    if override:
        # 相対パスのまま子プロセスへ渡すと Popen(cwd=authoring) 側で
        # authoring 相対に誤解釈されるため、expanduser＋絶対化して渡す（abspath＝symlink は辿らない）。
        p = Path(os.path.abspath(os.path.expanduser(override)))
        return str(p) if (p.is_file() and os.access(str(p), os.X_OK)) else None
    # repo_root は絶対（__file__.resolve()）。`.bin/marp` は npm が張る symlink（→ marp-cli.js）。
    # RUNTIME-SANDBOX §9 の実証がこの `.bin/marp` パスをそのまま使うため resolve せず返す
    # （リポジトリ管理の開発ツールで、authoring 配下の user データではない＝symlink 封じ込め対象外）。
    repo_root = Path(__file__).resolve().parents[3]
    cand = repo_root / "tools" / "marp" / "node_modules" / ".bin" / "marp"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return str(cand)
    return None


def _detect_chrome_path() -> str | None:
    """CHROME_PATH（PDF/PPTX レンダに必須の Chromium）。既存 env（CHROME_PATH/CHROMIUM_PATH）を
    尊重し、無ければ Playwright の既存 chromium を自動検出する（新規 DL しない・既存インストールを流用する）。
    見つからなければ None＝marp_render.render_outputs() は HTML のみ生成する。"""
    for k in ("CHROME_PATH", "CHROMIUM_PATH"):
        v = os.environ.get(k)
        if v:
            # 絶対化＋実行ビット確認する（非実行ファイルを渡すと Puppeteer が
            # EACCES でレンダ失敗する。相対パスは Popen(cwd=authoring) で誤解釈されるため絶対化する）。
            p = Path(os.path.abspath(os.path.expanduser(v)))
            if p.is_file() and os.access(str(p), os.X_OK):
                return str(p)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    cands = list(home.glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    if not cands:
        return None

    def _ver(p: Path) -> int:                       # chromium-1228 の数値部で最新を選ぶ（文字列比較だと桁数で誤る）
        m = re.search(r"chromium-(\d+)", str(p))
        return int(m.group(1)) if m else -1

    latest = max((c for c in cands if c.is_file() and os.access(str(c), os.X_OK)), key=_ver, default=None)
    return str(latest) if latest else None


# ---- web_search は既定 OFF。Codex CLI は web_search が既定 ON（OpenAI 管理インデックスの
# キャッシュ）で、社内資料接地の原則（04-画面の原則.md §4）と不整合のため、管理者が管理画面
# （system_settings.web_search_allowed）で明示許可した場合のみ、チャットごとの希望を尊重する。----
def _web_search_admin_allowed(system_settings: dict | None = None) -> bool:
    """管理者フラグ（`system_settings.web_search_allowed`・既定 false・管理画面「プロバイダ＋接続先」
    タブで設定）。env `SHERPA_ALLOW_WEB_SEARCH` は初回シードのみ（`sherpa.api._seed_settings_from_env`）
    で、実行時にはもう読まない（設定所有台帳の原則）。DB 不達（`system_settings` 省略時の取得失敗）は
    安全側 `False`（env フォールバックはしない）。`system_settings`（省略可）は呼び出し側が既に読んだ
    スナップショットをそのまま使う（`sherpa.llm._openai_endpoint_settings` と同じ形）。"""
    if system_settings is None:
        try:
            from ... import store
            system_settings = store.get_system_settings()
        except Exception:
            return False
    return bool(system_settings.get("web_search_allowed"))


def _web_search_disabled_value(user_enabled: bool, endpoint_kind: str = "openai",
                               system_settings: dict | None = None) -> str | None:
    """config/argv へ渡す web_search の値。管理者が許可し、かつこのチャットで希望した時だけ
    `None`（＝config へ何も書かない・Codex 既定の ON に委ねる）。それ以外は常に `"disabled"`。
    管理者未許可の間は、`user_enabled=True`（このチャットで希望）が渡されても無視する。

    `endpoint_kind`（Azure OpenAI 対応）: Codex(OpenAI) 構成の実際の接続先
    （`sherpa.llm.openai_endpoint_kind()` の値）。`"openai"`（既定・省略時もこれ）以外＝Azure 等の
    代替エンドポイントのときは、admin 許可・ユーザー設定に**関わらず常に無効化**する（Codex の
    web_search は OpenAI がホストする管理インデックスの機能。Azure OpenAI Responses API 自体は
    Web 検索ツールに対応しているが、現在の Sherpa＋Codex CLI カスタムプロバイダー経由でこの
    代替エンドポイントでも動くかは未検証のため、確認できるまで一律無効のままにする）。省略時は
    従来どおりの判定のみ（回帰ゼロ）。`_web_search_c_args`（emergency
    fallback＝`SHERPA_CODEX_SANDBOX=0` 経路）はこの引数を渡さない＝この経路は Azure 等への
    リダイレクト自体が未対応（`_write_codex_authoring_config` 参照。Codex(Ollama) 構成もこの経路
    では独自 model_provider を書けないため、`_select_provider` がサンドボックス無効時は honest
    failure を返し Codex を起動しない＝そもそもこの経路まで到達しない）ため、web_search だけ独自に
    強制 OFF すると「Azure は使えないのに web_search だけ気にする」というちぐはぐな挙動になる。

    `system_settings`（省略可）は `_web_search_admin_allowed` へそのまま転送する（呼び出し側が
    既に読んだスナップショットを使い回す・省略時は都度読み直す）。"""
    if endpoint_kind != "openai":
        return "disabled"
    if user_enabled and _web_search_admin_allowed(system_settings):
        return None
    return "disabled"


def _web_search_endpoint_note(user_enabled: bool, endpoint_kind: str,
                              system_settings: dict | None = None) -> str | None:
    """接続先が既定(OpenAI)以外（Azure 等）のせいで web_search が強制 OFF になっている時だけ、
    ユーザー向けの一言を返す（それ以外は None＝何も表示しない）。

    `_web_search_disabled_value` と条件を二重管理しない: admin 許可 or このチャットでの希望の
    どちらかが欠けている場合は、Azure と無関係にそもそも既定で OFF なので「Azure が理由」という
    説明は不要（過剰な注記を出さない）。`system_settings`（省略可）は `_web_search_admin_allowed`
    と同じ理由（呼び出し側のスナップショットをそのまま使う）。"""
    if endpoint_kind != "openai" and bool(user_enabled) and _web_search_admin_allowed(system_settings):
        return ("接続先が Azure OpenAI（または OpenAI 以外の互換エンドポイント）のため、"
                "現在の Sherpa＋Codex 構成では Web 検索は未検証として無効にしています。")
    return None


def _web_search_c_args(user_enabled: bool, system_settings: dict | None = None) -> list:
    """fallback 経路（`--strict-config` 無し・config.toml でなく `-c`）用の argv 追加分。
    `_write_codex_authoring_config` の web_search 行と同じ判定を `-c` 引数の形で返す
    （単一の真実源は `_web_search_disabled_value`・sandbox/fallback 間の判定ロジック重複を防ぐ）。
    disabled 相当なら `["-c", 'web_search="disabled"']`・有効相当なら `[]`（Codex 既定 ON に委ねる）。

    `endpoint_kind` を渡さない＝常に既定 "openai" 扱い。この emergency
    fallback 経路（`SHERPA_CODEX_SANDBOX=0`）はそもそも Azure 等へのリダイレクト自体が未対応
    （`_write_codex_authoring_config` の `ollama_base_url`/`_openai_compat_provider_lines` 分岐は
    sandbox モードのみ。Codex(Ollama) 構成は `_select_provider` がサンドボックス無効時に honest
    failure を返しこの経路まで到達しない）ので、この経路では実際に既定の api.openai.com へ繋がり
    web_search も従来どおり使える＝ここだけ強制 OFF にする理由が無い。`system_settings`（省略可）は
    呼び出し元（`CodexProvider`）が保持するスナップショットをそのまま使う。"""
    v = _web_search_disabled_value(user_enabled, system_settings=system_settings)
    return ["-c", f"web_search={_toml_str(v)}"] if v is not None else []


def _ollama_provider_lines(ollama_base_url: str) -> list[str]:
    """Codex CLI を Ollama へ向ける設定行（`model_provider` ＋ 独自プロバイダ定義）。

    実測（codex-cli 0.144.1・2026-08-15）:
      - `wire_api = "chat"` は廃止済み。Codex は OpenAI **Responses API**（`POST /v1/responses`）を使う。
        Ollama は 0.13.3 以降これに対応している（非stateful のみ）。
      - 組み込みプロバイダ id `ollama` は予約語で上書きできず、接続先が `localhost:11434` 固定になる
        （`OLLAMA_HOST` も効かない）。そのため**独自 id で定義**し、Sherpa の `ollama_url` 設定を
        常に効かせる（設定項目があるのに一部構成だけ無視される、という不整合を作らない）。

    `base`（`ollama_url`）は呼び出し側が `llm.assert_ollama_url_allowed` を通したものを渡す。
    """
    base = ollama_base_url.rstrip("/") + "/v1"
    return [
        f'model_provider = {_toml_str(_OLLAMA_PROVIDER_ID)}',
        '',
        f'[model_providers.{_OLLAMA_PROVIDER_ID}]',
        'name = "Ollama"',
        f'base_url = {_toml_str(base)}',
        'wire_api = "responses"',      # chat 方言は codex 0.144 で廃止（実測）
    ]


# 組み込み id（`ollama`）は予約語のため衝突しない名前を使う（実測で 400 相当のエラーになる）。
_OLLAMA_PROVIDER_ID = "sherpa-ollama"

# 組み込み id（`openai`/`ollama`/`lmstudio`）は予約語のため衝突しない名前を使う（Codex 公式ドキュメント
# 「Custom providers can't reuse the reserved built-in provider IDs」＝実装前に確認済み・2026-08-18）。
_OPENAI_COMPAT_PROVIDER_ID = "sherpa-openai-compat"


# `sherpa.llm` の `openai_endpoint_kind()`/`openai_base_url()` を呼ぶ単一の真実源。直接呼びにする
# （`getattr(..., None)` 等の欠落防御はしない＝関数が消えても気づかない逆効果になるため）。
def _openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_endpoint_kind()` を呼ぶ（"openai" | "azure" | "custom"）。
    `system_settings`（省略可）は `CodexProvider` が保持するスナップショットをそのまま渡す
    （省略時は `llm.py` が都度読み直す）。"""
    from ... import llm as _llm
    return _llm.openai_endpoint_kind(system_settings)


def _openai_compat_base_url(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_base_url()` を呼び、base URL の
    妥当性（`llm.assert_openai_base_url_allowed`）も検証する。

    呼ばれるのは呼び出し側（`_write_codex_authoring_config`）が既に `_openai_endpoint_kind() !=
    "openai"` と確認した後だけ。`_select_provider`（`providers/__init__.py`）が既に同じ検証を通した
    上で `CodexProvider` を組み立てる契約だが、ここでも検証する＝config.toml へ書く／子プロセス env
    にキーを渡す**直前**の最終防衛線（`_select_provider` の判定を迂回する経路があっても、不正な
    base URL がそのまま書かれてキーが誤った宛先へ渡ることを防ぐ）。不正なら `ValueError` を送出し、
    呼び出し元（`provider.py` の実行ループ）の既存 broad except に乗って安全に degrade する
    （`_openai_endpoint_kind` 冒頭のコメント参照）。`system_settings`（省略可）は `_openai_endpoint_kind` と同じ理由。"""
    from ... import llm as _llm
    base = _llm.openai_base_url(system_settings)
    _llm.assert_openai_base_url_allowed(base)
    return base


def _openai_compat_provider_lines(base_url: str, *, api_version: str | None, auth_header: str) -> list[str]:
    """Codex CLI を OpenAI 互換エンドポイント（主用途は Azure OpenAI）へ向ける設定行。
    `_ollama_provider_lines` と同型（`model_provider` ＋ 独自プロバイダ定義）。呼ばれるのは
    `_write_codex_authoring_config` が「Codex(OpenAI) 構成で、接続先が既定(api.openai.com)以外」と
    判定した時だけ＝既定のときは**この関数自体が呼ばれない**＝回帰ゼロ。

    実装根拠（Codex `config-advanced` 公式ドキュメント確認済み・codex-cli 0.144.1）:
      - Azure 公式サンプルはそのまま `[model_providers.azure]` に `env_key`＋`query_params`
        （`api-version`）＋`wire_api = "responses"` を書く。`openai_base_url`（トップレベル・組込み
        `openai` provider の base_url だけを差し替える簡易版）は `wire_api`/`query_params`/`env_key`
        を変えられないため Azure（v1 API 以外・旧方式）や独自ヘッダが要る構成には使えない
        （ビルトイン `openai` provider 自体は上書き不可＝予約語）。
      - `env_key` は Codex **子プロセスの環境変数**からキーを読む（`auth.json`/ChatGPT ログインは
        provider 側で `requires_openai_auth = true` を明示した時だけ使われる別経路で、本カスタム
        provider はそれを設定しない＝env_key 一本）。そのため Sherpa 側は、この構成の時**だけ**
        `_codex_clean_env` に `OPENAI_API_KEY` を渡す必要がある（`provider.py` 呼び出し側・
        `_codex_clean_env` の `openai_api_key` 引数を参照。既定(OpenAI 直結)は auth.json 経由の
        ままで変更なし＝env にキーを置かない現行方針を維持）。
      - `env_key` だけなら Codex は既定で `Authorization: Bearer <値>` を送る。Microsoft 公式の
        REST 例は Azure API キーを `api-key` ヘッダ、Entra ID トークンを `Authorization: Bearer`
        ヘッダで案内しており、Azure API キーの Bearer 送出そのものを公式に保証したものではない。
        ただし実機の Azure v1 エンドポイントで疎通確認済みのため、既定はこのまま Bearer とする
        （`auth_header="bearer"` はこの既定のまま何も追加しない）。
      - 旧来の `api-key: <値>` ヘッダ形式が要る環境だけ `env_http_headers`（env 変数名を書く・
        **値そのものは書かない**）で追加する。`env_key` はそのまま残す（Bearer と api-key を同時に
        送る構成＝この組み合わせ自体は未検証。Azure 側がどちらを優先する／片方を無視するかは
        確認していない）。`http_headers`（静的値を書く方）は使わない＝キーの値が 0600 の
        config.toml とはいえ literal で残ってしまう理由が無いため。
    """
    lines = [
        f'model_provider = {_toml_str(_OPENAI_COMPAT_PROVIDER_ID)}',
        '',
        f'[model_providers.{_OPENAI_COMPAT_PROVIDER_ID}]',
        'name = "OpenAI 互換エンドポイント"',
        f'base_url = {_toml_str(base_url)}',
        'env_key = "OPENAI_API_KEY"',
        'wire_api = "responses"',
    ]
    if api_version:
        lines.append('query_params = { "api-version" = ' + _toml_str(api_version) + ' }')
    if auth_header == "api-key":
        lines.append('env_http_headers = { "api-key" = "OPENAI_API_KEY" }')
    return lines


def _write_codex_authoring_config(codex_home: Path, kb_roots: list, reason: str,
                                  mcp: bool, world: str, scope_paths,
                                  web_search_enabled: bool = False,
                                  ask_disabled: bool = False,
                                  ollama_base_url: str | None = None,
                                  system_settings: dict | None = None,
                                  layer=None,
                                  direct_read_roots: list | None = None,
                                  sensitive_deny: list | None = None,
                                  deny_roots: list | None = None,
                                  sidecar_path: str | None = None,
                                  multi_agent: bool = False,
                                  orchestrator_model: str | None = None) -> None:
    """per-request CODEX_HOME に permission profile（＋任意で MCP 設定）を書く。
    **creds は config ファイル内に閉じる**（`:root=deny` 下では model-shell から CODEX_HOME 不可視・
    コマンドライン `-c` に creds を出さない＝`/proc/<pid>/cmdline` 漏洩も無い）。auth.json は実 home から symlink。

    `layer`（省略可・既定 `None`＝both）: `_mcp_env` へそのまま転送する（探す対象・MCP サーバ側の
    フィルタ）だけに使う。裁定: **Codex は層の指定を強制しない**——
    直読（この関数が書く permission profile の read）は層に関係なく許可し、層のフィルタは MCP
    ツール側（`run_tool`・`_mcp_env` の `SHERPA_MCP_LAYER`）だけが担う。旧: `mcp=True` かつ層限定
    のときに KB ルートを明示 `deny` していた挙動は撤去した（契約変更・
    `test_codex_authoring_config_keeps_kb_root_read_even_when_layer_restricted` 参照）。

    `direct_read_roots`（省略可・既定 `None`）: 渡されたとき、read するのは `kb_roots` ではなく
    こちら（KB root／派生ルート＝`_direct_read_roots` の戻り値。範囲は `sensitive_deny` 側の兄弟 deny で
    表す）。**明示的な空リスト `[]` は `None` と区別**——秘匿列挙／範囲の解決に失敗、または範囲が
    どの root にも無いときに呼び出し元が「直読は許可しない（MCP のみ）」を表すために渡す。省略時
    （既存呼び出し・単体テスト）は従来どおり `kb_roots` を read する＝回帰ゼロ。

    `sensitive_deny`（省略可）: 個別 `deny` にする絶対パス一覧（`_enumerate_sensitive` の秘匿ファイル
    と `_scope_deny_entries` の範囲外エントリ）。read 行より**後**に書く（具体パスほど優先）。
    `_prune_deny_paths` で整形してから書く（symlink・不在・deny 済みフォルダ配下への deny は
    bubblewrap の起動失敗になる＝実測）。

    `deny_roots`（省略可）: `direct_read_roots == []` のときに KB root と併せて明示 deny する
    root（派生ルート等）。

    `sidecar_path`（省略可・DEPTH-2 S3b）: 渡されたとき、MCP サーバ env に `SHERPA_MCP_SIDECAR` を
    足す（`mcp` が偽なら無視される＝MCP 自体を起動しないので意味が無い）。子エージェント
    （`spawn_agent`）が読んだ doc_id・ask_user の質問をこのファイルへ本文なしで記録させる——
    子の MCP 呼出は親の `--json` に構造化イベントとして現れないため
    （`docs/notes/2026-09-17-DEPTH-2-S3-Codex-multi_agent-実機確認.md` (d)(e)）、唯一の観測経路。
    呼び出し元（provider.py）は codex_home 配下（この関数が書く permission profile 上
    `":root" = "deny"` の外側＝model-shell から不可視・"." の workspace_write の外）にこのパスを
    置く契約——run_dir 直下（`":workspace_roots"` `"." = "write"`）に置くと Codex の shell ツールが
    偽の読取／ask_user 行を追記できてしまう。

    Python 実行環境（`_venv_root()`）は、渡された read 対象とは独立に、venv で動いているときだけ
    常に read で足す（裁定）。

    `multi_agent`（省略可・既定 `False`・S6・§2.6）: 真のとき `[agents]`／`[agents.worker]`／
    `[agents.evaluator]` を config.toml へ足す。`-c features.multi_agent=true` 自体は呼び出し元
    （provider.py の argv）が付ける——ここでは CLI 機能フラグではなくサブエージェントの層
    （モデル・推論レベル・同時実行数）だけを書く。worker の `model` は
    `_codex_worker_model(system_settings, main_model=orchestrator_model)`（system_settings
    `codex_worker_model` があればその値、無ければ Azure 接続時は `orchestrator_model`＝本体と
    同じデプロイ名、それ以外は Codex 自身のカタログにある安価枠）。evaluator の `model` は
    `orchestrator_model`（省略時は
    worker と同じモデルへ倒す＝本体のモデル名が取れない呼び出し元でも config 生成自体は壊さない）
    ・推論レベルは `reason`（本体へ実際に渡す `model_reasoning_effort` と同じ基準値）。
    role ごとの層の実体（`config_file` が指す TOML）は `_write_codex_agent_role_configs`
    （codex_home 配下＝model-shell 不可視）に書く。
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(codex_home, 0o700)                 # creds を含む CODEX_HOME を同ホスト他プロセス/ユーザから守る
    except OSError:
        pass
    # Codex(OpenAI) 構成（`ollama_base_url` なし）だけ、auth.json（実 home の OpenAI 資格情報）を
    # 受け渡す**直前**に再確認する。Popen 直前（provider.py）より手前のチョークポイント＝ここで
    # 止まれば auth.json の symlink 自体を作らない（Codex(Ollama) は OpenAI 系 I/O ではないため
    # 対象外）。呼び出し元（provider.py）の既存 broad except に乗り、「profile config 書込失敗→
    # answer=None→決定的回答」という既存の fail-closed 経路へそのまま合流する。
    if ollama_base_url is None:
        from ... import llm as _llm
        _llm.assert_openai_io_allowed()
    real_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    src = real_home / "auth.json"
    dst = codex_home / "auth.json"
    try:
        if src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
    except Exception:
        pass
    lines = [
        'default_permissions = "sherpa-authoring"',
        'approval_policy = "never"',
    ]
    # Codex(OpenAI) 構成のときだけ、実際の接続先（`sherpa.llm.openai_endpoint_kind()`）が既定
    # (api.openai.com) 以外かを見る＝既定なら "openai" が返り、以降の判定・分岐は全部素通り。
    # Azure/custom 分岐（下の `elif`）は `ollama_base_url` が無い時だけ通るため、Ollama 構成側の値には無関係。
    _endpoint_kind = "openai" if ollama_base_url else _openai_endpoint_kind(system_settings)
    # web_search（OpenAI がホストする管理インデックス）は Codex(Ollama) 構成では原理的に
    # 使えない——`_endpoint_kind` を Azure/custom 判定用に "openai" のまま保つのとは別に、
    # web_search の可否判定にだけ "ollama"（openai 以外）を渡し、管理者許可・ユーザー希望に
    # 関わらず常に無効化する（`_web_search_disabled_value` の endpoint_kind != "openai" 分岐）。
    _web_search_endpoint_kind = "ollama" if ollama_base_url else _endpoint_kind
    _ws_value = _web_search_disabled_value(web_search_enabled, _web_search_endpoint_kind, system_settings)
    if _ws_value is not None:                        # 既定は必ず disabled を明示的に書く
        lines.append(f'web_search = {_toml_str(_ws_value)}')
    # RV是正(2): role config（worker/evaluator）にも同じ provider 行を書けるよう、ここで
    # 生成した行を控えておく（`None`＝既定 OpenAI＝role config 側も何も書かない）。
    _role_provider_lines: list | None = None
    if ollama_base_url:                              # Codex(Ollama) 構成のときだけ接続先を差し替える
        _role_provider_lines = _ollama_provider_lines(ollama_base_url)
        lines += _role_provider_lines
    elif _endpoint_kind != "openai":                 # Codex(OpenAI) 構成で接続先が Azure 等のときだけ
        from ... import llm as _llm
        # kind・base_url・auth_header・api_version をすべて同じ `system_settings` から読む
        # （呼び出しごとに個別へ都度読み直すと、この1回の config.toml 生成の中で admin 保存が
        # 挟まった場合に組が食い違い得る）。
        _role_provider_lines = _openai_compat_provider_lines(
            _openai_compat_base_url(system_settings),
            api_version=_llm.openai_api_version(system_settings) or None,
            auth_header=_llm.openai_auth_header_style(system_settings))
        lines += _role_provider_lines
    lines += [
        '',
        '[permissions.sherpa-authoring]',
        'extends = ":workspace"',
        '',
        '[permissions.sherpa-authoring.filesystem]',
        '":root" = "deny"',       # FS 全体の読取を遮断（他人領域・秘密が見えない）
        '":minimal" = "read"',    # /usr,/bin,libs 等 実行最小限
    ]
    # 裁定: Codex は層の指定を強制しない——直読は層に関係なく read（旧: mcp=True
    # かつ層限定のとき KB ルートを明示 deny していたが撤去。層のフィルタは MCP ツール側のみ）。
    # `direct_read_roots is not None` のときは `kb_roots` の代わりにそちらを read する
    # （空リスト `[]` は「秘匿列挙が失敗し直読を許可しない」の明示・docstring 参照）。
    _read_roots = list(kb_roots) if direct_read_roots is None else list(direct_read_roots)
    _venv = _venv_root()
    if direct_read_roots == []:
        # 直読不許可（秘匿列挙の失敗など）: KB root・派生 root・venv を明示 deny する（`":minimal"`
        # 配下に来る配置でも読めないよう、read 行の省略ではなく deny を書く）。
        _deny = list(kb_roots) + list(deny_roots or []) + ([str(_venv)] if _venv is not None else [])
        _kept: list = []
        for r in sorted(set(_deny)):                      # 親が deny 済みの root は書かない（deny 済み配下の deny 行は起動失敗）
            rp = Path(r)
            if not rp.exists() or rp.is_symlink():        # 不在・symlink への deny 行も起動失敗（読めない場所＝落として境界は緩まない）
                continue
            if any(Path(k) in rp.parents for k in _kept):
                continue
            _kept.append(r)
            lines.append(f'{_toml_str(r)} = "deny"')
    else:
        _all_roots = _read_roots + ([str(_venv)] if _venv is not None else [])
        # read 行より後＝範囲外の兄弟・秘匿ファイルは個別 deny（具体パスほど優先）。
        # 整形（重複・symlink・不在・deny 済みフォルダ配下の除去）は `_prune_deny_paths`。
        _deny_lines = _prune_deny_paths(list(sensitive_deny or []), _all_roots)
        _deny_set = set(_deny_lines)
        for r in _all_roots:                  # root ごと deny する root（範囲が無い等）には read 行を書かない（同一キー重複）
            if r not in _deny_set:
                lines.append(f'{_toml_str(r)} = "read"')
        for p in _deny_lines:
            lines.append(f'{_toml_str(p)} = "deny"')
    lines += [
        '',
        '[permissions.sherpa-authoring.filesystem.":workspace_roots"]',
        '"." = "write"',          # authoring（cwd）だけ読書
        '',
        '[permissions.sherpa-authoring.network]',
        'enabled = false',        # model-shell の egress 遮断（codex 自身の API/MCP は codex 機構側で通る）
    ]
    if mcp:
        py = sys.executable or "python3"
        menv = _mcp_env(world, scope_paths, ask_disabled, layer=layer)
        if sidecar_path:
            # DEPTH-2 S3b: 子エージェント（同じ config.toml の mcp_servers.sherpa を継承する）も
            # 同じサイドカーへ書く——サイドカーは caller（親/子いずれの MCP プロセスか）を区別しない。
            menv["SHERPA_MCP_SIDECAR"] = str(sidecar_path)
        # クリーン env 下でも MCP サブプロセス（python -m sherpa.mcp_server）が動くよう PATH/PYTHONPATH を補う。
        menv.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
        menv.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[3]))
        env_toml = "{" + ", ".join(f"{k} = {_toml_str(v)}" for k, v in menv.items()) + "}"
        lines += [
            '',
            '[mcp_servers.sherpa]',
            f'command = {_toml_str(py)}',
            'args = ["-m", "sherpa.mcp_server"]',
            'default_tools_approval_mode = "approve"',
            f'env = {env_toml}',
        ]
    if multi_agent:
        # main_model=orchestrator_model: Ollama／Azure で worker 未設定のとき、本体 Codex と同じ
        # モデルタグ／デプロイ名へ倒す（`_codex_worker_model` docstring 参照）。
        _worker_model = _codex_worker_model(
            system_settings, main_model=orchestrator_model, ollama=bool(ollama_base_url))
        _worker_path, _evaluator_path = _write_codex_agent_role_configs(
            codex_home, worker_model=_worker_model, worker_reasoning="medium",
            evaluator_model=orchestrator_model or _worker_model, evaluator_reasoning=reason,
            provider_lines=_role_provider_lines)
        lines += [
            '',
            '[agents]',
            f'default_subagent_model = {_toml_str(_worker_model)}',
            'default_subagent_reasoning_effort = "medium"',
            f'max_concurrent_threads_per_session = {_CODEX_MAX_CONCURRENT_SUBAGENTS}',
            '',
            '[agents.worker]',
            'description = "資料の検索・精読と一次判断だけを担当。最終回答は書かない。"',
            f'config_file = {_toml_str(_worker_path)}',
            '',
            '[agents.evaluator]',
            'description = "根拠と一次判断を別観点で査読し、反証・条件例外・回答漏れ・未探索の範囲を返す。書き直さない。"',
            f'config_file = {_toml_str(_evaluator_path)}',
        ]
    cfg = codex_home / "config.toml"
    # creds(mcp env) を含むため symlink/race を避けて 0600 で書く（O_CREAT|O_EXCL|O_NOFOLLOW）。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    # 既存 config が居たら **fail-closed**（握り潰さず raise）＝古い/細工された config での起動を防ぐ。
    fd = os.open(str(cfg), flags, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _safe_workspace_authoring(users_dir: Path, uid: str):
    """RV BLOCKER: `workspace`/`authoring` に symlink が混入していると cwd/書込 root が個人 files 等へずれ、
    読取封じ込めが崩れる。各コンポーネントを symlink 拒否＋実体が workspace 配下に収まることを確認して返す。
    異常時は None＝fail-closed（Codex を起動しない）。uid slug も再検証（パス注入防御）。"""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", uid or ""):
        return None
    base = users_dir / uid
    ws = base / "workspace"
    authoring = ws / "authoring"
    for comp in (base, ws, authoring):
        if comp.is_symlink():                       # symlink 混入＝封じ込め崩壊 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():     # dir 以外が居る → fail-closed
            return None
    try:
        authoring.mkdir(parents=True, exist_ok=True)
        authoring.resolve().relative_to(ws.resolve())   # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return authoring


_RUN_DIR_TTL_SECONDS = 24 * 60 * 60   # 実行ごとの作業領域の掃除しきい値（クラッシュ等で rmtree されず残存した場合のみ対象）

# 24時間しきい値は「mtime が古い」ことしか見ないため、実行時間が長いターン（timeout 延長・
# author の長時間実行等）の run dir を「稼働中のまま」誤って掃除しうる。プロセス内の
# 「現在稼働中の run dir」集合を持ち、掃除対象から常に除外する（`_safe_run_authoring` が
# 作成直後に登録し、呼び出し元＝`CodexProvider._run_authoring` の finally が実行終了時に解除する）。
_ACTIVE_RUN_DIRS: set = set()
_ACTIVE_RUN_DIRS_GUARD = threading.Lock()


def _register_active_run_dir(run_dir: Path) -> None:
    with _ACTIVE_RUN_DIRS_GUARD:
        _ACTIVE_RUN_DIRS.add(run_dir)


def _release_active_run_dir(run_dir: Path) -> None:
    """呼び出し側（`_run_authoring` の finally）が実行終了時に呼ぶ。未登録・二重解除でも例外にしない
    （fail-open＝解除漏れがあっても次回以降の掃除が効かなくなるだけで、実害は「掃除が遅れる」のみ）。"""
    with _ACTIVE_RUN_DIRS_GUARD:
        _ACTIVE_RUN_DIRS.discard(run_dir)


def _mask_path_relative_to(fp, root: Path) -> str:
    """失敗ログに `users_dir`/uid を含むフルパスをそのまま出さない。`root` からの相対部分だけを、
    root 自身の識別子（`run-<乱数>`＝uid を含まない）に付けて返す。相対化できなければ root の
    識別子だけを返す。"""
    try:
        rel = Path(fp).resolve().relative_to(root.resolve())
        return f"{root.name}/{rel}"
    except (OSError, ValueError):
        return root.name


def _chmod_if_not_symlink(path) -> None:
    """symlink には絶対に chmod しない——chmod は既定で symlink を追従し、リンク先（root 外の
    任意のディレクトリでありうる）の権限を変えてしまう。`follow_symlinks=False` が使える環境
    ではそれで確実に symlink 自体に限定し、未対応環境（Linux の多くはここで
    `NotImplementedError`）では、その環境で symlink 自体にだけ作用させる手段が無い以上
    chmod 自体を諦める（`follow_symlinks=True` へのフォールバックはしない＝
    symlink 先を書き換える経路を残さない）。"""
    try:
        if os.path.islink(path):
            return
    except OSError:
        return
    try:
        os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
    except NotImplementedError:
        pass
    except OSError:
        pass


def _restore_removable_permissions(root: Path) -> None:
    """`root` 配下（root 自身を含む）の非 symlink **ディレクトリ**だけ、削除に必要な権限
    （書込＋実行）へ戻す。`rmtree` が実際に必要とするのはディレクトリ側の書込/実行権だけ
    （エントリの削除は親ディレクトリの権限で決まり、ファイル自体の権限は無関係）——ファイルには
    一切 chmod しない。ファイルは同一ファイルシステム上の run_dir 外のパスとハードリンク
    （同一 inode＝同一の権限ビットを共有）していることがあり、ファイルへ chmod すると
    run_dir 外の共有先まで権限が変わってしまう（symlink とは別種の封じ込め漏れ）。

    `root` 自身は **`os.walk` の前に** chmod する——`root` が読み書き不可（例: 000）だと
    `os.walk(root)` はその中身を一切列挙できず（listdir 自体が権限で失敗する）、配下の子を
    発見できないまま素通りしてしまう。`root` を先に書込/実行可能へ戻せば、`os.walk` は
    以後は各階層の子を chmod してから次の階層へ降りる（`topdown=True` の遅延評価により、
    ある階層を chmod した後で `os.walk` がその階層へ実際に降りる＝毎階層で自然に連鎖する）。"""
    _chmod_if_not_symlink(str(root))
    for dirpath, dirnames, _filenames in os.walk(str(root), topdown=True, followlinks=False):
        # os.walk は symlink ディレクトリの中には入らない（followlinks=False）が、
        # dirnames にはその名前自体が残るため、chmod 対象からも明示的に外す。
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in dirnames:
            _chmod_if_not_symlink(os.path.join(dirpath, name))


def _remove_dir_best_effort(root: Path) -> None:
    """`root` を削除する（best-effort）。素の `rmtree` が失敗したら、非 symlink エントリの権限を
    戻して `root` 全体をもう1回だけ再試行する（symlink には絶対に chmod しない＝
    `_chmod_if_not_symlink` 参照）。それでも残れば、相対パスと例外型・errno だけを warning に
    記録する（フルパスは出さない・呼び出し元の処理は止めない）。"""
    try:
        shutil.rmtree(str(root))
        return
    except OSError:
        pass
    _restore_removable_permissions(root)

    def _onexc(func, path, exc):
        _log.warning("run dir cleanup left an entry: %s type=%s errno=%s",
                    _mask_path_relative_to(path, root), type(exc).__name__, getattr(exc, "errno", None))

    shutil.rmtree(str(root), onexc=_onexc)


def _cleanup_stale_run_dirs(authoring: Path) -> None:
    """`authoring/` 直下の `run-*` のうち mtime がこれより古い**かつ稼働中でない**ものを
    best-effort で削除する（クラッシュ・強制終了で `_run_authoring` 側の finally が走らず残った
    前回実行の作業領域の掃除。`_remove_dir_best_effort` の権限回復付き再試行により、0500 等の
    権限制限で残った残骸も回収する）。稼働中（`_ACTIVE_RUN_DIRS` 登録済み）は mtime に関わらず
    対象外——実行時間が24時間を超えても掃除で消さない。symlink は削除せずそのまま無視する
    （symlink の指す先を巻き込まないため）。それ以外（`authoring/` 直下の既存ファイル・
    AGENTS.md・`.codexhome-*` 等）には触れない。"""
    try:
        entries = list(authoring.iterdir())
    except OSError:
        return
    now = time.time()
    with _ACTIVE_RUN_DIRS_GUARD:
        active_snapshot = set(_ACTIVE_RUN_DIRS)
    for p in entries:
        if p.is_symlink() or not p.name.startswith("run-") or not p.is_dir():
            continue
        if p in active_snapshot:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError as e:
            # `p` は絶対パス（users_dir/uid を含む）——相対名（run-* 自身の識別子）と
            # 例外の型・errno だけを記録する（フルパス・例外の文字列表現は出さない）。
            _log.warning("stale codex run dir cleanup failed for %s: type=%s errno=%s",
                        p.name, type(e).__name__, getattr(e, "errno", None))
            continue
        if now - mtime <= _RUN_DIR_TTL_SECONDS:
            continue
        _remove_dir_best_effort(p)


def _safe_run_authoring(users_dir: Path, uid: str) -> "Path | None":
    """実行ごとに専用の作業領域（`authoring/run-<乱数>`）を作る（RV MEDIUM「同一 uid 直列化」の
    撤去に伴う置き換え: 実行ごとに cwd/書込 root を分ければ、同一 uid の複数実行が snapshot・
    files/ move・`.agents` rebuild で交差する心配がなくなり、直列化 lock 自体が不要になる）。

    `_safe_workspace_authoring` と同じ封じ込め（uid 形式・symlink 拒否・非 dir 拒否・実体が
    workspace 配下）を満たした `authoring/` を土台にしたうえで、衝突しない乱数名のディレクトリを
    非再入（`mkdir(exist_ok=False)`）で新規作成し、実体が workspace 配下に収まることを再確認して
    返す。異常時（authoring 自体が不正・作成先が既存/symlink/OSError）はいずれも None＝fail-closed
    （呼び出し側は Codex を起動しない）。成功時は返す前に `_register_active_run_dir` で稼働中集合へ
    登録する——呼び出し側は実行終了時に必ず `_release_active_run_dir` で解除すること。

    副作用として、`authoring/` 直下に残った期限切れ（かつ非稼働）の `run-*`
    （`_cleanup_stale_run_dirs` 参照）を best-effort で掃除する。"""
    authoring = _safe_workspace_authoring(users_dir, uid)
    if authoring is None:
        return None
    _cleanup_stale_run_dirs(authoring)
    ws = users_dir / uid / "workspace"
    run_dir = authoring / f"run-{os.urandom(6).hex()}"
    try:
        run_dir.mkdir(exist_ok=False)
        run_dir.resolve().relative_to(ws.resolve())     # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    _register_active_run_dir(run_dir)
    return run_dir


def _safe_codex_sessions_home(users_dir: Path, uid: str, conversation_id) -> "Path | None":
    """会話ごとの永続 CODEX_HOME（`workspace/.codex-sessions/{cid}`）の安全確認（Codex ネイティブ
    resume による会話継続用）。`_safe_workspace_authoring` と同じ契約
    （symlink混入・非ディレクトリ・workspace 外逸脱は fail-closed で None を返す＝呼び出し側は
    Codex を起動しない）。

    `{cid}` は conversation_id 由来の**固定パス**（毎ターン同じ場所を再利用する）ため、
    per-request 乱数名の旧 CODEX_HOME（`.codexhome-<rand>`）以上に symlink 事前設置（write-what-
    where）の標的になりやすい＝本関数で個別に検証する。uid 自体の形式検証・`workspace` の
    symlink 拒否は呼び出し側が既に `_safe_workspace_authoring` で済ませている前提
    （本関数は `.codex-sessions` とその下の `{cid}` だけを追加検証する）。
    """
    ws = users_dir / uid / "workspace"
    try:
        cid_str = str(int(conversation_id))
    except (TypeError, ValueError):
        return None
    sessions_root = ws / ".codex-sessions"
    codex_home = sessions_root / cid_str
    for comp in (sessions_root, codex_home):
        if comp.is_symlink():                       # symlink 混入＝封じ込め崩壊 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():      # dir 以外が居る → fail-closed
            return None
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        codex_home.resolve().relative_to(ws.resolve())   # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return codex_home
