"""Codex authoring 用サンドボックス機構（permission profile による読取封じ込め・Marp/Chrome バイナリ検出・web_search ポリシー）。
`mcp.py` の `_mcp_env`・`_toml_str` を直接 import する（依存は一方向 sandbox → mcp）。
設計: docs/design/safety.md「Codex サンドボックス（permission profile）」
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
# `-s workspace-write` は読取が FS 全開のため、permission profile（default_permissions）で読取も KB(RO)＋authoring(RW) に封じ込める。
def _codex_sandbox_enabled() -> bool:
    """既定 ON。SHERPA_CODEX_SANDBOX=0 で `-s workspace-write` にフォールバック（緊急時の逃げ道）。"""
    return os.environ.get("SHERPA_CODEX_SANDBOX", "1").strip().lower() not in ("0", "false", "no", "off", "")


# 素の Codex モード（管理画面の設定 `codex_mode`）。
# "standard"＝Sherpa の標準構成・"plain"＝台帳・出力スキーマ・multi_agent・検索/読取ツール一式を外し、Codex 本来の調べ方に任せる。
CODEX_MODES = ("standard", "plain")


def codex_mode(system_settings: dict | None = None) -> str:
    """`codex_mode` の実効値。未設定・想定外の保存値は "standard"。呼び出し元はすべてこの1関数の結果だけを見る。"""
    v = system_settings.get("codex_mode") if isinstance(system_settings, dict) else None
    if v in CODEX_MODES:
        return v
    if v is not None:
        _log.warning("codex_mode: unexpected stored value type=%s, falling back to standard",
                    type(v).__name__)
    return "standard"


# `[agents.worker]` の `model` は Codex CLI 自身のモデルカタログ（`codex debug models`）にある値でなければ spawn_agent が解決エラーになる。
_CODEX_WORKER_MODEL_FALLBACK = "gpt-5.6-sol"

# `[agents].max_concurrent_threads_per_session`。worker/evaluator の同時起動を抑える固定値。
_CODEX_MAX_CONCURRENT_SUBAGENTS = 2


def codex_multi_agent_enabled(*, ollama_base_url: str | None, system_settings: dict | None = None) -> bool:
    """`[agents.worker]`/`[agents.evaluator]` を有効にするかどうかの唯一の判定。呼び出し元は条件式を複製しない。
    真になるのは次を全て満たすときだけ:
      - サンドボックスが有効（`[agents.*]` は per-request の `codex_home` の config.toml に書くため）。
      - Codex(Ollama) 構成なら常に有効（worker/evaluator の `model` は `_codex_worker_model()` が本体と同じタグへ倒す）。
      - Codex(OpenAI) 構成では、接続先が既定／Azure、または custom でも `codex_worker_model` が明示設定済み（custom は本体のモデル名を流用できないため必須）。
    """
    if not _codex_sandbox_enabled():
        return False
    if ollama_base_url is not None:
        return True
    if _openai_endpoint_kind(system_settings) == "custom":
        # `system_settings` は省略可（省略時は DB から読む）。`_openai_endpoint_kind` と同じ解決規則（`llm._openai_endpoint_settings`）を使う。
        from ... import llm as _llm
        _resolved = _llm._openai_endpoint_settings(system_settings)
        return isinstance(_resolved, dict) and bool(
            str(_resolved.get("codex_worker_model") or "").strip())
    return True


# 役割ファイルの developer_instructions（Codex(Ollama) 構成のみ）。
_WORKER_ROLE_INSTRUCTIONS = (
    "あなたは下調べ役です。親から渡された項目について資料とソースを検索・精読し、"
    "見つけた事実を根拠（資料・箇所）付きで返してください。推測は推測と明記してください。")
_EVALUATOR_ROLE_INSTRUCTIONS = (
    "あなたは見直し役です。親の下書きと根拠を別の観点から確かめ、根拠と食い違う主張や"
    "確かめていない項目を具体的に指摘してください。")


def _codex_worker_model(system_settings: dict | None = None, *, main_model: str | None = None,
                        ollama: bool = False) -> str:
    """`[agents.worker]` の `model` に使う値。
    優先順位: ① 管理画面の `codex_worker_model` ② Ollama 構成または Azure 接続で `main_model` が渡されていればそれ ③ それ以外は `_CODEX_WORKER_MODEL_FALLBACK`。
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
    """`[agents.worker]`/`[agents.evaluator]` の `config_file` 実体（モデル・推論レベルだけの最小 TOML）を `codex_home/agents/` 配下に書く。戻り値は (worker の絶対パス, evaluator の絶対パス)。
    `codex_home` は `":root" = "deny"` の外側＝model-shell から不可視。
    `provider_lines`（省略可）: 親 config.toml と同じ `model_provider` 行。子 Codex が既定の `openai` provider へ出て資料本文が意図しない宛先へ渡らないよう、Azure/custom/Ollama 構成では必ず渡す。
    """
    d = codex_home / "agents"
    d.mkdir(parents=True, exist_ok=True)
    worker_path = d / "worker.toml"
    evaluator_path = d / "evaluator.toml"
    _provider_block = ("\n" + "\n".join(provider_lines) + "\n") if provider_lines else ""
    # 役割ファイルに developer_instructions が無いと Codex は役割を黙って捨てる（multi_agent が効かない）。全接続先で常に書く。
    _worker_role = f'developer_instructions = {_toml_str(_WORKER_ROLE_INSTRUCTIONS)}\n'
    _evaluator_role = f'developer_instructions = {_toml_str(_EVALUATOR_ROLE_INSTRUCTIONS)}\n'
    worker_path.write_text(
        f'model = {_toml_str(worker_model)}\n'
        f'model_reasoning_effort = {_toml_str(worker_reasoning)}\n' + _worker_role + _provider_block,
        encoding="utf-8")
    evaluator_path.write_text(
        f'model = {_toml_str(evaluator_model)}\n'
        f'model_reasoning_effort = {_toml_str(evaluator_reasoning)}\n' + _evaluator_role + _provider_block,
        encoding="utf-8")
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
    """原本直読で permission profile に read を許す絶対パスの一覧＝KB root（`_kb_read_roots`）＋存在する派生ルート（`derived_md_dir`／`derived_rag_dir`／`archives_dir`）。
    このリストに無い派生サブディレクトリは読めない。
    範囲（scope）は read root を狭めずに、`_scope_deny_entries` が経路上にない兄弟を個別 deny して表す（親フォルダの deny が子の read に勝つため）。`scope_paths` は互換のため受けるが使わない。
    """
    from ... import worlds

    roots = list(_kb_read_roots(world))
    for fn in (worlds.derived_md_dir, worlds.derived_rag_dir, worlds.archives_dir):
        try:
            d = fn(world)
            if d.exists():
                roots.append(str(d.resolve()))
        except OSError:
            continue
    return roots


def _scope_deny_entries(roots: list, scope_paths, *, max_entries: int = 2000) -> list:
    """範囲（scope）を permission profile で効かせるための deny 一覧（絶対パス）。
    各 root で、選択された scope パスの経路上にあり、かつ経路上でも選択先でもない兄弟エントリを deny にする。symlink は deny に書かない（bubblewrap が起動に失敗する）。
    選択された scope が root 配下に無い root は root ごと deny、scope が空なら空リスト。deny 件数が `max_entries` を超える・走査中の OSError は `RuntimeError`（fail-closed＝直読を許可しない）。
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
        keep: list = []  # root 配下に実在する scope（相対の PurePath）
        for sp in sel:
            cand = root_r / sp
            try:
                cand_r = cand.resolve()
                cand_r.relative_to(root_r)  # `..`／symlink 脱出は捨てる
            except (OSError, ValueError):
                continue
            if cand.exists() and not cand.is_symlink():
                keep.append(Path(sp))
        if not keep:
            out.append(str(root_r))  # 範囲がこの root に無い＝root ごと deny
            continue
        # 親子で選ばれた scope（A と A/sub）は親だけ残す
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
                _fail("symlink_dir")  # 経路上の symlink を跨ぐ deny は起動失敗になる
            try:
                entries = sorted(os.listdir(d))
            except OSError as e:
                _fail(type(e).__name__)
            for name in entries:
                rel = rel_dir / name
                if any(rel == k or rel in k.parents for k in keep):
                    continue  # 選択先そのもの、または経路上
                if (d / name).is_symlink():
                    continue
                out.append(str(d / name))
                if len(out) > max_entries:
                    _fail("max_entries_exceeded")
    return out


def _venv_root() -> Path | None:
    """app の Python 実行環境（`.venv`）の絶対パス。venv 内で動いているときだけ返し、Codex へ read で見せる（Office ライブラリを使わせる）。それ以外は None。"""
    if sys.prefix != sys.base_prefix:
        try:
            return Path(sys.prefix).resolve()  # symlink 配備でも実体パスを返す（profile は symlink を跨ぐ行で起動失敗する）
        except OSError:
            return None
    return None


def _enumerate_sensitive(roots: list, *, max_hits: int = 200, max_files: int = 200_000) -> list:
    """`roots` 配下（再帰）の秘匿名ファイルを絶対パスで列挙する（fail-closed）。秘匿の定義は `text_kind.is_sensitive` に一本化。app の `.venv` も同じ関数で再帰する。
    symlink はディレクトリとして辿らない。symlink 自体が秘匿名なら、実体が `roots` 配下の通常ファイルのときだけ実体のパスを deny に入れる。戻り値は重複なし・ソート済み。
    秘匿ファイルが `max_hits`、走査が `max_files` を超える・走査中の OSError は `RuntimeError`（メッセージは種別のみ `sensitive_enum_failed:<種別>`・パスや内容はログに出さない）。呼び出し元はこれを捕捉して直読を許可しない（MCP のみへ縮退）。
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
                        continue  # dangling＝読めない
                    if not (target.is_file() and _inside_roots(target)):
                        continue  # root 外＝`:root=deny` で読めない
                    p = target
                out.add(str(p))
                if len(out) > max_hits:
                    _fail("max_hits_exceeded")
    return sorted(out)


def _prune_deny_paths(deny: list, read_roots: list) -> list:
    """permission profile に書く deny 行を整える。重複・read 対象（root）配下に無いもの・実在しないもの・symlink・既に deny されるフォルダ配下の deny を落とす（bubblewrap が起動に失敗するため）。
    read root そのものへの deny（直読不許可・範囲が root に無い）は残す。
    """
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


# 親環境に設定されているときだけ Codex へ透過する変数（プロキシ・社内 CA）。接続経路の設定であって creds ではない。大文字・小文字の両方を見る。
_CODEX_PASSTHROUGH_ENV: tuple[str, ...] = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
)


def _tool_home(tmpdir: Path) -> Path:
    home = Path(tmpdir) / "home"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _codex_clean_env(codex_home: Path, authoring: Path, tmpdir: Path,
                     openai_api_key: str | None = None) -> dict:
    """codex exec 用の最小 env（env -i 相当）。DB/ES/KB creds を渡さない（PATH 等ランタイムのみ）。
    creds が要る MCP サブプロセスへは config ファイル(mcp_servers.sherpa.env)経由で渡す。プロキシ/CA の経路設定（`_CODEX_PASSTHROUGH_ENV`）だけ、親環境にあるときそのまま渡す。
    `openai_api_key`: 既定 None＝渡さない。Codex(OpenAI) 構成で接続先を Azure 等のカスタム provider（`env_key = "OPENAI_API_KEY"`）へ差し替えたときだけ渡す。
    app の `.venv`（`_venv_root()`）で動いているときは PATH の先頭に `<venv>/bin` を足す。
    """
    _venv = _venv_root()
    _base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Codex 同梱の ripgrep（`<導入先>/**/vendor/<triple>/codex-path/rg`）を PATH に足す（サーバに rg が無くても Codex のシェルで使えるように）。
    _rg_dir = _codex_bundled_rg_dir()
    if _rg_dir is not None:
        _base_path = f"{_base_path}:{_rg_dir}"
    env = {
        "PATH": f"{_venv / 'bin'}:{_base_path}" if _venv is not None else _base_path,
        # ツールが HOME に書く設定・キャッシュを成果物の走査対象外の `.tmp/` に置く。
        "HOME": str(_tool_home(tmpdir)),
        "CODEX_HOME": str(codex_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TMPDIR": str(tmpdir),
    }
    for name in _CODEX_PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value:  # 空文字は「未設定」と同じ＝渡さない
            env[name] = value
    if openai_api_key:  # 明示的に渡された時だけ
        env["OPENAI_API_KEY"] = openai_api_key
    return env


# ---- Marp（スライド作成スキル）レンダ用のバイナリ検出 ----
# Codex は sandbox 内で .md を書くだけ。レンダ（HTML/PDF/PPTX）は Codex 完了後に Sherpa 本体が marp_render.py 経由で marp CLI・Chromium を使う。
def _marp_bin() -> str | None:
    """marp CLI 実行ファイルの絶対パス（存在＆実行可能な時だけ）。env `SHERPA_MARP_BIN` で上書き可。未解決なら None。"""
    override = os.environ.get("SHERPA_MARP_BIN")
    if override:
        # expanduser＋絶対化して渡す（abspath＝symlink は辿らない）。
        p = Path(os.path.abspath(os.path.expanduser(override)))
        return str(p) if (p.is_file() and os.access(str(p), os.X_OK)) else None
    # `.bin/marp` は npm が張る symlink（→ marp-cli.js）。resolve せず返す。
    repo_root = Path(__file__).resolve().parents[3]
    cand = repo_root / "tools" / "marp" / "node_modules" / ".bin" / "marp"
    if cand.is_file() and os.access(str(cand), os.X_OK):
        return str(cand)
    return None


def _detect_chrome_path() -> str | None:
    """CHROME_PATH（PDF/PPTX レンダに必須の Chromium）。既存 env（CHROME_PATH/CHROMIUM_PATH）を尊重し、無ければ Playwright の既存 chromium を自動検出する。見つからなければ None。"""
    for k in ("CHROME_PATH", "CHROMIUM_PATH"):
        v = os.environ.get(k)
        if v:
            # 絶対化＋実行ビット確認する。
            p = Path(os.path.abspath(os.path.expanduser(v)))
            if p.is_file() and os.access(str(p), os.X_OK):
                return str(p)
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    cands = list(home.glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    if not cands:
        return None

    def _ver(p: Path) -> int:  # chromium-1228 の数値部で最新を選ぶ
        m = re.search(r"chromium-(\d+)", str(p))
        return int(m.group(1)) if m else -1

    latest = max((c for c in cands if c.is_file() and os.access(str(c), os.X_OK)), key=_ver, default=None)
    return str(latest) if latest else None


# ---- web_search は既定 OFF。管理者が管理画面（system_settings.web_search_allowed）で許可した場合のみ、チャットごとの希望を尊重する。----
def _web_search_admin_allowed(system_settings: dict | None = None) -> bool:
    """管理者フラグ `system_settings.web_search_allowed`（既定 false）。DB 不達は安全側 `False`。`system_settings`（省略可）は呼び出し側のスナップショットをそのまま使う。"""
    if system_settings is None:
        try:
            from ... import store
            system_settings = store.get_system_settings()
        except Exception:
            return False
    return bool(system_settings.get("web_search_allowed"))


def _web_search_disabled_value(user_enabled: bool, endpoint_kind: str = "openai",
                               system_settings: dict | None = None) -> str | None:
    """config/argv へ渡す web_search の値。管理者が許可し、かつこのチャットで希望した時だけ `None`（Codex 既定に委ねる）。それ以外は常に `"disabled"`。
    `endpoint_kind`: Codex(OpenAI) 構成の実際の接続先。`"openai"`（既定）以外（Azure 等）のときは admin 許可・ユーザー設定に関わらず常に無効化する。
    `_web_search_c_args`（fallback 経路）はこの引数を渡さない。`system_settings`（省略可）は `_web_search_admin_allowed` へ転送する。
    """
    if endpoint_kind != "openai":
        return "disabled"
    if user_enabled and _web_search_admin_allowed(system_settings):
        return None
    return "disabled"


def _web_search_endpoint_note(user_enabled: bool, endpoint_kind: str,
                              system_settings: dict | None = None) -> str | None:
    """接続先が既定以外（Azure 等）のせいで web_search が強制 OFF になっている時だけ、ユーザー向けの一言を返す（それ以外は None）。条件は `_web_search_disabled_value` と二重管理しない。"""
    if endpoint_kind != "openai" and bool(user_enabled) and _web_search_admin_allowed(system_settings):
        return ("接続先が Azure OpenAI（または OpenAI 以外の互換エンドポイント）のため、"
                "現在の Sherpa＋Codex 構成では Web 検索は未検証として無効にしています。")
    return None


def _web_search_c_args(user_enabled: bool, system_settings: dict | None = None) -> list:
    """fallback 経路（config.toml でなく `-c`）用の argv 追加分。`_web_search_disabled_value` と同じ判定を `-c` 引数の形で返す（disabled 相当なら `["-c", 'web_search="disabled"']`・有効相当なら `[]`）。
    `endpoint_kind` は渡さない（この経路は接続先リダイレクト未対応で既定の api.openai.com に繋がるため）。
    """
    v = _web_search_disabled_value(user_enabled, system_settings=system_settings)
    return ["-c", f"web_search={_toml_str(v)}"] if v is not None else []


def _ollama_provider_lines(ollama_base_url: str) -> list[str]:
    """Codex CLI を Ollama へ向ける設定行（`model_provider` ＋ 独自プロバイダ定義）。
    Codex は OpenAI Responses API（`POST /v1/responses`）を使う。組み込みプロバイダ id `ollama` は予約語で接続先が `localhost:11434` 固定になるため、独自 id で定義して `ollama_url` 設定を常に効かせる。
    `base`（`ollama_url`）は呼び出し側が `llm.assert_ollama_url_allowed` を通したものを渡す。
    """
    base = ollama_base_url.rstrip("/") + "/v1"
    return [
        f'model_provider = {_toml_str(_OLLAMA_PROVIDER_ID)}',
        '',
        f'[model_providers.{_OLLAMA_PROVIDER_ID}]',
        'name = "Ollama"',
        f'base_url = {_toml_str(base)}',
        'wire_api = "responses"',  # Responses API を使う
    ]


# 組み込み id（`ollama`）は予約語のため衝突しない名前を使う。
_OLLAMA_PROVIDER_ID = "sherpa-ollama"

# 組み込み id（`openai`/`ollama`/`lmstudio`）は予約語のため衝突しない名前を使う。
_OPENAI_COMPAT_PROVIDER_ID = "sherpa-openai-compat"


# `sherpa.llm` の `openai_endpoint_kind()`/`openai_base_url()` を直接呼ぶ。
def _openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_endpoint_kind()`（"openai" | "azure" | "custom"）を呼ぶ。`system_settings`（省略可）は `CodexProvider` のスナップショットを渡す。"""
    from ... import llm as _llm
    return _llm.openai_endpoint_kind(system_settings)


def _openai_compat_base_url(system_settings: dict | None = None) -> str:
    """`sherpa.llm.openai_base_url()` を呼び、base URL の妥当性（`llm.assert_openai_base_url_allowed`）も検証する。
    config.toml へ書く／子プロセス env にキーを渡す直前の最終防衛線。不正なら `ValueError`（呼び出し元の既存 broad except で安全に degrade する）。呼ばれるのは接続先が既定以外のときだけ。
    """
    from ... import llm as _llm
    base = _llm.openai_base_url(system_settings)
    _llm.assert_openai_base_url_allowed(base)
    return base


def _openai_compat_provider_lines(base_url: str, *, api_version: str | None, auth_header: str) -> list[str]:
    """Codex CLI を OpenAI 互換エンドポイント（主用途は Azure OpenAI）へ向ける設定行。`_ollama_provider_lines` と同型。
    `_write_codex_authoring_config` が接続先を既定以外と判定したときだけ呼ばれる。
    `env_key` は Codex 子プロセスの環境変数からキーを読む（`auth.json` は使わない）ため、この構成のときだけ `_codex_clean_env` に `OPENAI_API_KEY` を渡す。
    既定は `Authorization: Bearer`。旧来の `api-key: <値>` ヘッダが要る環境だけ `env_http_headers`（env 変数名を書く・値そのものは書かない）で追加する。`http_headers`（静的値）は使わない。
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


def _codex_install_root() -> Path | None:
    """サンドボックスの中で読ませる Codex の導入先。npm 導入はパッケージの根、単体の実行ファイルはそのフォルダ。見つからなければ None。"""
    exe = shutil.which("codex")
    if not exe:
        return None
    real = Path(os.path.realpath(exe))
    if real.suffix == ".js" and real.parent.name == "bin":
        pkg = real.parent.parent
        # プラットフォーム別の実行ファイルのパッケージは pkg の下にも同じ @openai スコープ直下にも置かれうるため、スコープごと読ませる。
        return pkg.parent if pkg.parent.name == "@openai" else pkg
    # package 一式（`<root>/bin/codex`・codex-package.json・codex-path/rg・codex-resources/bwrap）は付属物ごと読めるよう `<root>` を返す。
    if real.parent.name == "bin" and (real.parent.parent / "codex-package.json").is_file():
        return real.parent.parent
    return real.parent


def _codex_launcher_dir() -> Path | None:
    """macOS で `codex` が symlink（Homebrew Cask 等）のとき、その symlink を置いたフォルダ。Seatbelt が symlink 側も読めないと拒むため。Linux（bubblewrap）は対象外。"""
    if sys.platform != "darwin":
        return None
    exe = shutil.which("codex")
    if not exe or not Path(exe).is_symlink():
        return None
    d = Path(exe).parent
    if d.resolve() != d or d == _codex_install_root():
        return None
    return d


def _codex_bundled_rg_dir() -> Path | None:
    """Codex の導入先に同梱された ripgrep のフォルダ（`<root>/codex-path` または `vendor/<triple>/codex-path`）。無ければ None。"""
    root = _codex_install_root()
    if root is None:
        return None
    direct = root / "codex-path" / "rg"
    if direct.is_file() and os.access(direct, os.X_OK):
        return direct.parent
    for cand in sorted(root.glob("**/vendor/*/codex-path/rg")):
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand.parent
    return None


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
                                  orchestrator_model: str | None = None,
                                  extra_mcp_env: dict | None = None,
                                  link_auth: bool = True) -> None:
    """per-request CODEX_HOME に permission profile（＋任意で MCP 設定）を書く。creds は config ファイル内に閉じる（コマンドライン `-c` に出さない）。auth.json は実 home から symlink。
    `layer`: `_mcp_env` へ転送するだけ。Codex は層の指定を強制せず、層のフィルタは MCP ツール側が担う。
    `direct_read_roots`: 渡されたとき `kb_roots` の代わりにそれを read する。明示的な空リスト `[]` は「直読は許可しない（MCP のみ）」で、`None`（従来どおり `kb_roots`）と区別する。
    `sensitive_deny`: 個別 `deny` にする絶対パス（秘匿ファイルと範囲外エントリ）。read 行より後に書き、`_prune_deny_paths` で整形する。
    `deny_roots`: `direct_read_roots == []` のときに明示 deny する root。
    `extra_mcp_env`: `_mcp_env` の結果に上書きマージする追加 env（`mcp` が偽なら無視）。
    `sidecar_path`: MCP サーバ env に `SHERPA_MCP_SIDECAR` を足す（子の読取記録の唯一の観測経路）。codex_home 配下（`":root" = "deny"` の外側）に置くこと（run_dir 直下は shell が偽の行を書ける）。
    Python 実行環境（`_venv_root()`）は venv で動いているときだけ常に read で足す。
    `multi_agent`: 真のとき `[agents]`／`[agents.worker]`／`[agents.evaluator]` を足す（worker の model は `_codex_worker_model`、evaluator は `orchestrator_model`、推論レベルは `reason`）。層の実体は `_write_codex_agent_role_configs` が書く。
    """
    codex_home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(codex_home, 0o700)  # creds を含む CODEX_HOME を同ホスト他プロセス/ユーザから守る
    except OSError:
        pass
    # Codex(OpenAI) 構成（`ollama_base_url` なし）だけ、auth.json を受け渡す直前に再確認する。止まれば auth.json の symlink も作らない（呼び出し元の既存 broad except で fail-closed）。
    if ollama_base_url is None:
        from ... import llm as _llm
        _llm.assert_openai_io_allowed()
    real_home = Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    src = real_home / "auth.json"
    dst = codex_home / "auth.json"
    try:
        # `link_auth=False`（点検用）: 認証ファイルに触れない。
        if link_auth and src.exists() and not dst.exists():
            dst.symlink_to(src.resolve())
    except Exception:
        pass
    lines = [
        'default_permissions = "sherpa-authoring"',
        'approval_policy = "never"',
    ]
    # Codex(OpenAI) 構成のときだけ、実際の接続先が既定以外かを見る。
    _endpoint_kind = "openai" if ollama_base_url else _openai_endpoint_kind(system_settings)
    # web_search は Codex(Ollama) 構成では使えないため、可否判定にだけ "ollama"（openai 以外）を渡して常に無効化する。
    _web_search_endpoint_kind = "ollama" if ollama_base_url else _endpoint_kind
    _ws_value = _web_search_disabled_value(web_search_enabled, _web_search_endpoint_kind, system_settings)
    if _ws_value is not None:  # 既定は必ず disabled を明示的に書く
        lines.append(f'web_search = {_toml_str(_ws_value)}')
    # role config（worker/evaluator）にも同じ provider 行を書けるよう控える（`None`＝既定 OpenAI）。
    _role_provider_lines: list | None = None
    if ollama_base_url:  # Codex(Ollama) 構成のときだけ接続先を差し替える
        _role_provider_lines = _ollama_provider_lines(ollama_base_url)
        lines += _role_provider_lines
    elif _endpoint_kind != "openai":  # Codex(OpenAI) 構成で接続先が Azure 等のときだけ
        from ... import llm as _llm
        # kind・base_url・auth_header・api_version はすべて同じ `system_settings` から読む。
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
        '":root" = "deny"',  # FS 全体の読取を遮断
        '":minimal" = "read"',  # /usr,/bin,libs 等 実行最小限
    ]
    # Codex はサンドボックス内で自分の実行ファイルを起動し直すため、導入先だけを read にする。
    _codex_root = _codex_install_root()
    if _codex_root is not None:
        lines.append(f'{_toml_str(str(_codex_root))} = "read"')
    _codex_launcher = _codex_launcher_dir()
    if _codex_launcher is not None:
        lines.append(f'{_toml_str(str(_codex_launcher))} = "read"')
    # Codex は層の指定を強制せず、直読は層に関係なく read。`direct_read_roots is not None` のときは `kb_roots` の代わりにそちらを read する（空リスト `[]` は直読を許可しない）。
    _read_roots = list(kb_roots) if direct_read_roots is None else list(direct_read_roots)
    _venv = _venv_root()
    if direct_read_roots == []:
        # 直読不許可: KB root・派生 root・venv を明示 deny する（read 行の省略ではなく deny を書く）。
        _deny = list(kb_roots) + list(deny_roots or []) + ([str(_venv)] if _venv is not None else [])
        _kept: list = []
        for r in sorted(set(_deny)):  # 親が deny 済みの root は書かない
            rp = Path(r)
            if not rp.exists() or rp.is_symlink():  # 不在・symlink への deny 行は起動失敗になるため落とす
                continue
            if any(Path(k) in rp.parents for k in _kept):
                continue
            _kept.append(r)
            lines.append(f'{_toml_str(r)} = "deny"')
    else:
        _all_roots = _read_roots + ([str(_venv)] if _venv is not None else [])
        # read 行より後＝範囲外の兄弟・秘匿ファイルは個別 deny（具体パスほど優先）。整形は `_prune_deny_paths`。
        _deny_lines = _prune_deny_paths(list(sensitive_deny or []), _all_roots)
        _deny_set = set(_deny_lines)
        for r in _all_roots:  # root ごと deny する root には read 行を書かない
            if r not in _deny_set:
                lines.append(f'{_toml_str(r)} = "read"')
        for p in _deny_lines:
            lines.append(f'{_toml_str(p)} = "deny"')
    lines += [
        '',
        '[permissions.sherpa-authoring.filesystem.":workspace_roots"]',
        '"." = "write"',  # authoring（cwd）だけ読書
        '',
        '[permissions.sherpa-authoring.network]',
        'enabled = false',  # model-shell の egress 遮断
    ]
    if mcp:
        py = sys.executable or "python3"
        menv = _mcp_env(world, scope_paths, ask_disabled, layer=layer)
        if extra_mcp_env:
            menv.update(extra_mcp_env)
        if sidecar_path:
            # 子エージェント（mcp_servers.sherpa を継承する）も同じサイドカーへ書く。
            menv["SHERPA_MCP_SIDECAR"] = str(sidecar_path)
        # クリーン env 下でも MCP サブプロセスが動くよう PATH/PYTHONPATH を補う。
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
        # `main_model=orchestrator_model`: Ollama／Azure で worker 未設定のとき本体と同じモデルへ倒す。
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
    # creds を含むため 0600 で書く（O_CREAT|O_EXCL|O_NOFOLLOW）。
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    # 既存 config が居たら fail-closed（raise）。
    fd = os.open(str(cfg), flags, 0o600)
    try:
        os.write(fd, ("\n".join(lines) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _safe_workspace_authoring(users_dir: Path, uid: str):
    """`workspace`/`authoring` の各コンポーネントを symlink 拒否＋実体が workspace 配下に収まることを確認して返す。異常時は None＝fail-closed（Codex を起動しない）。uid slug も再検証する。"""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", uid or ""):
        return None
    base = users_dir / uid
    ws = base / "workspace"
    authoring = ws / "authoring"
    for comp in (base, ws, authoring):
        if comp.is_symlink():  # symlink 混入 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():  # dir 以外が居る → fail-closed
            return None
    try:
        authoring.mkdir(parents=True, exist_ok=True)
        authoring.resolve().relative_to(ws.resolve())  # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return authoring


_RUN_DIR_TTL_SECONDS = 24 * 60 * 60  # 実行ごとの作業領域の掃除しきい値（クラッシュ等で残存した場合のみ対象）

# 稼働中の run dir は mtime に関わらず掃除対象から除外する。`_safe_run_authoring` が登録し、`CodexProvider._run_authoring` の finally が解除する。
_ACTIVE_RUN_DIRS: set = set()
_ACTIVE_RUN_DIRS_GUARD = threading.Lock()


def _register_active_run_dir(run_dir: Path) -> None:
    with _ACTIVE_RUN_DIRS_GUARD:
        _ACTIVE_RUN_DIRS.add(run_dir)


def _release_active_run_dir(run_dir: Path) -> None:
    """実行終了時（`_run_authoring` の finally）に呼ぶ。未登録・二重解除でも例外にしない。"""
    with _ACTIVE_RUN_DIRS_GUARD:
        _ACTIVE_RUN_DIRS.discard(run_dir)


def _mask_path_relative_to(fp, root: Path) -> str:
    """失敗ログにフルパス（uid を含む）を出さず、`root` からの相対部分だけを `run-<乱数>` に付けて返す。"""
    try:
        rel = Path(fp).resolve().relative_to(root.resolve())
        return f"{root.name}/{rel}"
    except (OSError, ValueError):
        return root.name


def _chmod_if_not_symlink(path) -> None:
    """symlink には絶対に chmod しない（リンク先の権限を変えないため）。`follow_symlinks=False` が使えない環境では chmod 自体を諦める。"""
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
    """`root` 配下（root 自身を含む）の非 symlink ディレクトリだけ、削除に必要な権限（書込＋実行）へ戻す。ファイルには chmod しない（run_dir 外とハードリンク共有している場合があるため）。
    `root` 自身は `os.walk` の前に chmod する（読めないと配下を列挙できないため）。
    """
    _chmod_if_not_symlink(str(root))
    for dirpath, dirnames, _filenames in os.walk(str(root), topdown=True, followlinks=False):
        # os.walk は symlink ディレクトリに入らないが dirnames に残るため、chmod 対象から外す。
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in dirnames:
            _chmod_if_not_symlink(os.path.join(dirpath, name))


def _remove_dir_best_effort(root: Path) -> None:
    """`root` を削除する（best-effort）。失敗したら非 symlink エントリの権限を戻して1回だけ再試行する。それでも残れば相対パスと例外型・errno だけを warning に記録する（フルパスは出さない・呼び出し元は止めない）。"""
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
    """`authoring/` 直下の `run-*` のうち mtime が古く、かつ稼働中でないものを best-effort で削除する。稼働中（`_ACTIVE_RUN_DIRS` 登録済み）と symlink は対象外。それ以外（AGENTS.md・`.codexhome-*` 等）には触れない。"""
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
            # `p` は絶対パスのため、相対名と例外の型・errno だけを記録する（フルパス・例外の文字列は出さない）。
            _log.warning("stale codex run dir cleanup failed for %s: type=%s errno=%s",
                        p.name, type(e).__name__, getattr(e, "errno", None))
            continue
        if now - mtime <= _RUN_DIR_TTL_SECONDS:
            continue
        _remove_dir_best_effort(p)


def _safe_run_authoring(users_dir: Path, uid: str) -> "Path | None":
    """実行ごとに専用の作業領域（`authoring/run-<乱数>`）を作る。同一 uid の複数実行が交差しない。
    `_safe_workspace_authoring` と同じ封じ込め（uid 形式・symlink 拒否・非 dir 拒否・実体が workspace 配下）を満たした `authoring/` の下に、乱数名のディレクトリを `mkdir(exist_ok=False)` で作り、実体が workspace 配下であることを再確認して返す。異常時は None＝fail-closed。
    成功時は `_register_active_run_dir` で稼働中集合へ登録する（呼び出し側は終了時に `_release_active_run_dir` で解除する）。期限切れ（かつ非稼働）の `run-*` を best-effort で掃除する。
    """
    authoring = _safe_workspace_authoring(users_dir, uid)
    if authoring is None:
        return None
    _cleanup_stale_run_dirs(authoring)
    ws = users_dir / uid / "workspace"
    run_dir = authoring / f"run-{os.urandom(6).hex()}"
    try:
        run_dir.mkdir(exist_ok=False)
        run_dir.resolve().relative_to(ws.resolve())  # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    _register_active_run_dir(run_dir)
    return run_dir


def _safe_codex_sessions_home(users_dir: Path, uid: str, conversation_id) -> "Path | None":
    """会話ごとの永続 CODEX_HOME（`workspace/.codex-sessions/{cid}`）の安全確認。`_safe_workspace_authoring` と同じ契約（symlink 混入・非ディレクトリ・workspace 外逸脱は None＝fail-closed）。
    `{cid}` は固定パスで symlink 事前設置の標的になりやすいため、`.codex-sessions` とその下の `{cid}` を個別に検証する。
    """
    ws = users_dir / uid / "workspace"
    try:
        cid_str = str(int(conversation_id))
    except (TypeError, ValueError):
        return None
    sessions_root = ws / ".codex-sessions"
    codex_home = sessions_root / cid_str
    for comp in (sessions_root, codex_home):
        if comp.is_symlink():  # symlink 混入 → fail-closed
            return None
        if comp.exists() and not comp.is_dir():  # dir 以外が居る → fail-closed
            return None
    try:
        codex_home.mkdir(parents=True, exist_ok=True)
        codex_home.resolve().relative_to(ws.resolve())  # 最終確認: 実体が workspace 配下
    except (OSError, ValueError):
        return None
    return codex_home
