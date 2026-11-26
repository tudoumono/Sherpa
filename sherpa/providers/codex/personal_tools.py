"""Codex のターンで個人ファイルの道具（`personal_search`・`personal_read`）を使うための準備・受け渡し・後始末。
親（このプロセス）が利用者を決め、開いてよいファイルの一覧を作って道具のサーバーへ渡す。読んだ個人ファイルは出典の個人の枠へ出す。
設計: docs/06-認証と個人領域.md §3、docs/design/codex.md「MCP の道具」
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ... import mcp_personal, store
from ...parts.read import personal
from ..base import _log, _node
from ..prompts import _NO_PRESEARCH_HEADLINE
from .citations import _QUOTE_CHARS, _SPLIT_RE, normalize_doc_ref, parse_referenced_doc_lines

TOOL_NAMES = personal.TOOL_NAMES
INDEX_NAME = ".personal_index.json"
_PROMPT_NAMES_MAX = 50
_SOURCE_LABEL = "個人ファイル内ヒット"


class TurnPersonal:
    """1 ターンの個人ファイルの道具の状態。`files` は開いてよい rel_path（None＝道具を使わない）、`read_rels` は道具で読んだ rel_path。
    `only` は社内資料オフで個人ファイルの道具だけを持つ形のターン（資料フォルダの道具・直読・台帳・調べ方のスキルを使わない）。
    `world` は資料名の候補を出典の解決と同じ形に直すときの資料フォルダ。"""
    __slots__ = ("files", "files_dir", "index_path", "read_rels", "only", "world")

    def __init__(self):
        self.files: list[str] | None = None
        self.files_dir: str | None = None
        self.index_path: Path | None = None
        self.read_rels: list[str] = []
        self.only = False
        self.world = ""

    @property
    def offered(self) -> bool:
        return bool(self.files) and self.files_dir is not None


def allowed_files(uid: str) -> list[str]:
    """台帳で生きている本人のファイルのうち、秘匿名でなく読める形式のもの（名前順・上限あり）。"""
    rels = store.live_workspace_rel_paths(uid)
    return sorted(r for r in rels if personal.valid_rel(r))[:personal.INDEX_MAX_FILES]


def _files_dir(users_dir: Path, uid: str) -> str | None:
    """本人の `files/` の実パス。`files/` が symlink・無い・本人の `workspace/` の外へ出るときは None。"""
    base = Path(users_dir) / uid / "workspace"
    raw = base / "files"
    if raw.is_symlink() or not raw.is_dir():
        return None
    real = os.path.realpath(raw)
    if Path(real).parent != Path(os.path.realpath(base)):
        return None
    return real


def _names_block(files: list[str]) -> str:
    names = "\n".join(f"- {f}" for f in files[:_PROMPT_NAMES_MAX])
    more = f"\n（ほか {len(files) - _PROMPT_NAMES_MAX} 件）" if len(files) > _PROMPT_NAMES_MAX else ""
    return ("【個人ファイル（本人のみ・共有不可）】\n"
            "次の個人ファイルを personal_search（語で探す）・personal_read（読む）で探して読めます"
            "（id は名前の前に personal: を付けたもの）。\n" + names + more)


def prepare(ctx, st) -> str:
    """Codex へ渡す依頼文を返す。個人ファイルの道具を使うターンなら、開いてよい一覧を決めて `st._personal` に置き、名前の一覧と道具の案内を足す。
    事前検索のヒット（`ctx.personal_facts`・素の Codex）があれば従来どおりそのヒットを足す。"""
    msg = ctx.message
    if ctx.personal_facts:
        return f"{msg}\n\n【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}"
    if not ctx.personal or (st._plain and not st._personal.only):
        return msg
    files_dir = _files_dir(st.users_dir, st.uid)
    try:
        files = allowed_files(st.uid) if files_dir is not None else []
    except Exception as e:
        _log.warning("個人ファイルの一覧を読めませんでした: %s", type(e).__name__)
        return f"{msg}\n\n【個人ファイル（本人のみ・共有不可）】\n個人ファイルの一覧を読めなかったため、今回は個人ファイルを調べられません。"
    if not files:
        return f"{msg}\n\n【個人ファイル（本人のみ・共有不可）】\n調べられる個人ファイルはありません。"
    st._personal.files, st._personal.files_dir, st._personal.world = files, files_dir, ctx.world
    return f"{msg}\n\n{_names_block(files)}"


def mcp_env(ctx, st, index_dir: Path) -> dict:
    """道具のサーバーへ足す env。個人ファイルの道具を使うターンなら、開いてよい一覧を `index_dir`（モデルのシェルから書けない置き場）へ書き、その場所と `files/` の実パスを渡す。
    個人ファイルを参照するターンは、台帳の記録に検索語を残さない印も渡す。一覧を書けなければ道具を出さない。"""
    env: dict[str, str] = {}
    if (not st._plain or st._personal.only) and (ctx.personal or ctx.personal_facts):
        env["SHERPA_MCP_COVERAGE_NO_QUERY"] = "1"
    p = st._personal
    if not p.offered:
        return env
    path = Path(index_dir) / INDEX_NAME
    try:
        Path(index_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            os.write(fd, json.dumps({"files": p.files}, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        _log.warning("個人ファイルの一覧を書けませんでした: %s errno=%s", type(e).__name__, getattr(e, "errno", None))
        p.files = None
        return env
    p.index_path = path
    env[mcp_personal.ENV_INDEX] = os.path.realpath(path)
    env[mcp_personal.ENV_DIR] = p.files_dir
    return env


def cleanup(st) -> None:
    """このターンに書いた一覧を消す（会話ごとに使い回す置き場に残さない）。"""
    p = st._personal
    if p.index_path is not None:
        try:
            p.index_path.unlink(missing_ok=True)
        except OSError:
            pass
        p.index_path = None


def _note(st, rels) -> None:
    allowed = set(st._personal.files or ())
    for rel in rels:
        if rel in allowed and rel not in st._personal.read_rels:
            st._personal.read_rels.append(rel)


def note_parent_read(st, tool: str, args: dict, item: dict) -> None:
    """親の `--json` に出た個人ファイルの道具の成功した呼び出しから、読んだファイルを控える。"""
    try:
        result = json.loads(item["result"]["content"][0]["text"])
    except (KeyError, IndexError, TypeError, ValueError):
        return
    _note(st, personal.read_rels(tool, args, result))


def absorb_sidecar(st) -> None:
    """子（worker・evaluator）が個人ファイルの道具で読んだファイルを、サイドカーの `personal_read` の行から控える。"""
    rels = []
    try:
        with open(st._sidecar_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("kind") == "personal_read" and isinstance(entry.get("rel_path"), str):
                    rels.append(entry["rel_path"])
    except (OSError, UnicodeDecodeError):
        pass
    _note(st, rels)


def is_personal_ref(s) -> bool:
    """`personal:` で始まる候補の片（引用符を剥がした後）か。"""
    return isinstance(s, str) and s.strip().strip(_QUOTE_CHARS).strip().startswith(personal.ID_PREFIX)


def _shadow_names(st) -> set[str]:
    """道具で読んだ個人ファイルの相対パス・名前と、それを区切りの文字で分けた片（出典の解決と同じ形に直したものを含む）。"""
    if st is None or not st._personal.read_rels:
        return set()
    world = st._personal.world
    raw = {n for r in st._personal.read_rels for n in (r, r.rsplit("/", 1)[-1])}
    raw |= {x.strip() for n in list(raw) for x in _SPLIT_RE.split(n) if x.strip()}
    return raw | {d for d in (normalize_doc_ref(n, world) for n in raw) if d}


def _named_personal(s, names: set[str], world: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    return personal.ID_PREFIX in s or (bool(names) and (s.strip().strip(_QUOTE_CHARS).strip() in names
                                                    or normalize_doc_ref(s, world) in names))


def drop_unverified(st, rows: list) -> list:
    """「確認できなかった資料」の行から、`personal:` を含む行と、読んだ個人ファイルと同じ名前の行を外す。"""
    names = _shadow_names(st)
    world = st._personal.world if st is not None else ""
    return [r for r in rows if not (isinstance(r, dict) and _named_personal(r.get("path"), names, world))]


def drop_shadowed(st, detail: dict) -> dict:
    """出典の解決の結果から、読んだ個人ファイルと名前か相対パスが同じ共有資料（このターンに資料フォルダの道具で読んだものは除く）と、
    個人ファイルの名前の「確認できなかった資料」の行を外す。読んだ個人ファイルが無いターンは `personal:` を含む行だけを外す。"""
    names = _shadow_names(st)
    world, read = st._personal.world, set(st._mcp_read_docs)
    gone = {d for d in detail["verified"] if names and d not in read and (d in names or d.rsplit("/", 1)[-1] in names)}
    return {**detail, "verified": [d for d in detail["verified"] if d not in gone],
            "unverified": drop_unverified(st, detail["unverified"]),
            "mappings": [m for m in detail["mappings"]
                         if m.get("doc_id") not in gone and not _named_personal(m.get("input"), names, world)]}


def drop_refs(tokens: list, st=None) -> list:
    """資料名の候補から `personal:` で始まる片を外す（行ごとの候補群は片の位置を保ち、全部外れた行だけ消す）。"""
    def keep(t):
        if isinstance(t, list):
            kept = [keep(x) for x in t if not is_personal_ref(x)]
            return kept if any(kept) else []
        return t
    return [k for k in (keep(t) for t in tokens if not is_personal_ref(t)) if k]


def take_answer_refs(st, env: dict, answer: str) -> tuple[str, list]:
    """回答の『参照した資料:』を読み、個人ファイルの id の行を外して `(本文, 行ごとの候補群)` を返す。
    個人ファイルの道具で読んだファイルは出典の個人の枠（`personal_sources`・DL リンクなし）へ置く。"""
    body, groups = parse_referenced_doc_lines(answer)
    if st._personal.read_rels:
        env["personal_sources"] = [{"doc_id": r, "quote": "", "source": _SOURCE_LABEL} for r in st._personal.read_rels]
    return body, ([] if st._personal.only else drop_refs(groups, st))


def toolset(st) -> str:
    """素の Codex・個人ファイルだけのターンで道具のサーバーへ渡す道具の絞り込み（`SHERPA_MCP_TOOLSET`）。"""
    return mcp_personal.TOOLSET if st._personal.only else "plain"


def gather_only(ctx):
    """個人ファイルだけのターンの前段。資料フォルダの調べ方は選ばず（確認カードも出さない）、問いへの回答（qa）として `_env` を返す。"""
    yield _node("understand", "think", "質問を理解", "内容を把握しました", "done")
    decision = {"lens": "qa", "input": ctx.message, "reason": "社内資料オフ・個人ファイルだけを参照"}
    env = {"lens": "qa", "headline": _NO_PRESEARCH_HEADLINE, "summary": {"total": 0}, "data": {}, "sources": [],
           "agentic_failure": "error", "scope": {"world": ctx.world, "scope_paths": [], "source": "off"}}
    yield {"type": "_env", "decision": decision, "env": env}
