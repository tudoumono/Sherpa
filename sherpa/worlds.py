"""資料フォルダ（登録ディレクトリ）のレジストリ・解決・ライフサイクル。
資料フォルダは参照元 `root_path` に 1:1 でバインドする（`store.worlds`）。参照先変更（rebind）は旧資料フォルダの派生物を全削除して新パスから再ミラーする。
別案件は別の資料フォルダとして追加する（`world_id` は内部識別子）。
設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import stat as stat_mod
from pathlib import Path
from typing import NamedTuple

from .grep_tool import valid_world  # 識別子の許容文字（パストラバーサル防止）
from .ingest import text_kind


def semantic_dir(world_id: str) -> Path:
    """派生 `semantic/` ディレクトリ（ES の埋め込みキャッシュ〔SQLite・`embed_cache.sqlite3`〕の置き場）。"""
    return derived_dir(world_id) / "semantic"


def _fixtures() -> bool:
    return os.environ.get("SHERPA_USE_FIXTURES", "").lower() in ("1", "true", "yes")


# `SHERPA_KB_DIR`／`SHERPA_DERIVED_DIR` の未設定時の既定はリポジトリ基準で解決する（呼び出し元の cwd に依存させない）。
# env で明示された値はそのまま尊重する
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _kb() -> Path:
    v = os.environ.get("SHERPA_KB_DIR")
    return Path(v) if v else _repo_root() / "data" / "kb"


def derived_dir(world_id: str) -> Path:
    """資料フォルダの派生領域ルート（READ-ONLY のソースには書かない）。配下: `md/`（人間用）／`rag/`（RAG 正本＋証跡）／`ir/`（中間表現）／`semantic/`（ES 埋め込みキャッシュ）。削除時はこの木ごと消す。"""
    v = os.environ.get("SHERPA_DERIVED_DIR")
    base = Path(v) if v else _repo_root() / "data" / "derived"
    return base / world_id


def derived_md_dir(world_id: str) -> Path:
    """人間用 MD のミラー置き場（派生領域の `md/`）。画面表示・原本 DL の根拠。取り込みのたび作り直す（semantic は残す）。"""
    return derived_dir(world_id) / "md"


def derived_rag_dir(world_id: str) -> Path:
    """RAG 正本＋証跡の置き場（`{rel}.rag.md`／`{rel}.rag_chunks.jsonl`／`{rel}.assets/`）。grep・ES・グラフが読む。"""
    return derived_dir(world_id) / "rag"


def rag_md_path(world_id: str, doc_id: str) -> Path | None:
    """`doc_id` の `{rel}.rag.md`（RAG 正本）の実パス。無効な doc_id・秘匿名・範囲外・不在・経路上の symlink は None。
    ツール引数を直接受けるため、字面パスと `resolve()` の一致で symlink を検知する（厳格側に統一）。
    """
    if not isinstance(doc_id, str) or not doc_id or doc_id.startswith("/") or "\\" in doc_id or "\x00" in doc_id:
        return None
    parts = doc_id.split("/")
    if ".." in parts or "" in parts:
        return None
    if text_kind.is_sensitive_doc_id(doc_id):
        return None  # 秘匿名は rag.md を持たない契約。残っていても読ませない
    root = derived_rag_dir(world_id)
    if not root:
        return None
    root = Path(root)
    lexical_rel = doc_id + ".rag.md"
    try:
        rr = root.resolve()
        rp = (root / lexical_rel).resolve()
        if not (rp == rr or rp.is_relative_to(rr)):
            return None
        if rp != rr / lexical_rel:  # 字面パスと不一致＝経路上に symlink がある
            return None
        if not rp.is_file():
            return None
    except OSError:
        return None
    return rp


def derived_ir_dir(world_id: str) -> Path:
    """中間表現の置き場（`{rel}.document.json`／`{rel}.evidence.json`／`{rel}.derived.json`／`{rel}.ocr_route.json`）。再生成可能・検索には出さない・drift 判定専用。"""
    return derived_dir(world_id) / "ir"


def archives_dir(world_id: str) -> Path:
    """zip/tar(.gz)/tgz の展開先（`derived_dir` の兄弟 `archives/`）。原本（登録ディレクトリ）には書かない。
    木の構成がそのまま doc_id（`<アーカイブの相対パス>/<中のパス>`）になる（`scope_infer.safe_files(..., also=archives_dir(world_id))` で列挙に合流する）。
    """
    return derived_dir(world_id) / "archives"


def archives_work_dir(world_id: str) -> Path:
    """アーカイブ展開の作業領域（ステージング・退避・異常終了時の掃除専用。`archives_dir` の外）。
    `ingest.archive_extract` だけが使い、他のどの読み取り経路（`also=`／grep の roots_spec／Codex の `_direct_read_roots`）にも登場させない（書きかけの中身が列挙・検索に漏れないため）。
    """
    return derived_dir(world_id) / "archives_work"


def archive_manifest_path(world_id: str) -> Path:
    """アーカイブごとの展開結果サマリ（`ingest.archive_extract.sync_world_archives` が書く）。
    `archives_dir` の外に置く（中に置くとサマリ自身が展開済み文書として列挙される）。
    """
    return derived_dir(world_id) / "archive_manifest.json"


# ---- OCR 観測領域（任意機能・既定 OFF）----
# OCR は隔離 worker が動かす。worker には登録ディレクトリと Canonical 派生を read-only で渡し、書けるのは観測領域だけにする。
# 以下はその境界を fail-closed で守る検証群で、別の bind mount 経由で同じ inode に書けてしまわないよう、resolve 済みパスの包含関係で判定する

def _paths_overlap(first: Path, second: Path) -> bool:
    """2 つの root が同一、または祖先／子孫の関係かを返す（解決後の包含関係で見る）。"""
    try:
        left = first.resolve()
        right = second.resolve()
    except OSError as exc:
        raise ValueError("OCR観測領域のrootを安全に解決できません") from exc
    return left == right or left in right.parents or right in left.parents


def observation_base_dir() -> Path:
    """観測領域のベース（`SHERPA_OBSERVATION_DIR`＞`data/observations`）。"""
    value = os.environ.get("SHERPA_OBSERVATION_DIR")
    return Path(value) if value else _repo_root() / "data" / "observations"


def validate_observation_source_separation(source_root: str | Path) -> None:
    """書き込み可の観測領域が資料フォルダの参照元と重なっていたら止める。"""
    if _paths_overlap(observation_base_dir(), Path(source_root)):
        raise ValueError("SHERPA_OBSERVATION_DIRはWorld参照元と物理分離してください")


def validate_observation_registered_sources(*extra_source_roots: str | Path) -> None:
    """登録済みの全資料フォルダの参照元と観測領域が分離していることを確認する。
    セキュリティ境界なので registry の失敗・壊れた行は握りつぶさず送出する。候補 root（register／rebind）と既存行の両方を検査する。
    """
    from . import store

    roots = list(extra_source_roots)
    for row in store.list_worlds_db():
        root_path = row.get("root_path") if isinstance(row, dict) else None
        if not isinstance(root_path, str) or not root_path:
            raise ValueError("登録済みWorld参照元を安全に検証できません")
        roots.append(root_path)
    for root in roots:
        validate_observation_source_separation(root)


def _fs_chain_ids(path: Path) -> set[tuple[int, int]]:
    """path とその全祖先の (st_dev, st_ino)。大文字小文字を区別しない FS・symlink・bind でも同一性で比べる。
    stat できないものがあれば OSError を送出する（呼出側は判定不能として拒否する）。"""
    resolved = path.resolve()
    return {(st.st_dev, st.st_ino) for st in (os.stat(p) for p in (resolved, *resolved.parents))}


def _fs_overlap(first: Path, second: Path) -> bool:
    """2つの path が同一、または祖先/子孫の関係かをファイルシステム上の同一性で判定する。
    判定できないとき（stat 不能）は重なりありとして True を返す（fail-closed）。
    ただし片方が存在しないときだけは、存在しない側は存在する側の祖先にも子孫にもなり得ないので、
    大文字小文字を無視した解決後 path の包含で代わりに判定する。"""
    try:
        a_ids, b_ids = _fs_chain_ids(first), _fs_chain_ids(second)
        a_self, b_self = os.stat(first.resolve()), os.stat(second.resolve())
        return (a_self.st_dev, a_self.st_ino) in b_ids or (b_self.st_dev, b_self.st_ino) in a_ids
    except FileNotFoundError:
        try:
            a = first.resolve().as_posix().casefold().rstrip("/") + "/"
            b = second.resolve().as_posix().casefold().rstrip("/") + "/"
        except OSError:
            return True
        return a.startswith(b) or b.startswith(a)
    except OSError:
        return True


def observation_removal_target(world_id: str) -> Path | None:
    """削除してよい観測の置き場（`observation_base_dir()/{world_id}`）。無ければ None。

    **何かを消す前に呼ぶ**（検証失敗は ValueError＝呼出側は何も消さずに止まる・fail-closed）。
    観測の置き場・対象が、登録済みの全 World 参照元（読み取り専用の原本）と祖先/子孫/同一で重ならないことを
    ファイルシステム上の同一性（`_fs_overlap`）で確かめる。対象が symlink、置き場の厳密な配下でない、
    派生物と重なる、registry を読めない、のいずれも拒否する。
    """
    from . import store

    base = observation_base_dir()
    target = base / world_id
    try:
        os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:                                 # 確かめられないときは消さない
        raise ValueError(f"観測ディレクトリの有無を確かめられません: {exc.__class__.__name__}") from exc
    observation_dir(world_id)                              # 派生物との分離
    roots = []
    for row in store.list_worlds_db():
        root_path = row.get("root_path") if isinstance(row, dict) else None
        if not isinstance(root_path, str) or not root_path:
            raise ValueError("登録済みWorld参照元を安全に検証できません")
        roots.append(Path(root_path))
    if target.is_symlink() or not target.is_dir():
        raise ValueError("OCR観測領域が実ディレクトリでないため削除できません")
    try:
        resolved, base_resolved = target.resolve(), base.resolve()
        if (os.stat(resolved).st_dev, os.stat(resolved).st_ino) in {
            (st.st_dev, st.st_ino) for st in (os.stat(base_resolved),)
        }:
            raise ValueError("OCR観測領域が置き場そのものです")
        if (os.stat(base_resolved).st_dev, os.stat(base_resolved).st_ino) not in {
            (st.st_dev, st.st_ino) for st in (os.stat(p) for p in resolved.parents)
        }:
            raise ValueError("OCR観測領域が置き場の配下にありません")
    except OSError as exc:
        raise ValueError("OCR観測領域を安全に解決できません") from exc
    for root in roots:
        if _fs_overlap(base, root) or _fs_overlap(target, root):
            raise ValueError("OCR観測領域がWorld参照元と重なるため削除できません")
    return target


def observation_current_dir(world_id: str) -> Path | None:
    """いま公開されている OCR 観測のディレクトリ（無ければ None）。
    観測は Canonical（決定的に変換した MD／Evidence）とは別の木に置く。どの世代が公開中かは観測領域の pointer が正で、Canonical と食い違う場合は None（古い観測を読ませない）。
    """
    from .ingest import derived_generation, observation_render

    try:
        base = observation_dir(world_id)
    except ValueError:  # 保存先の設定不備は検索を止めずに「観測なし」とする
        return None
    canonical = derived_generation.active_generation_id(derived_dir(world_id))
    if not canonical:
        return None
    return observation_render.active_observation_dir(base, canonical_generation_id=canonical)


def _configured_ocr_world_root() -> Path:
    """OCR worker へ read-only で渡す、明示された参照元 root。広い既定値は持たない（未設定・相対・symlink・到達不可は設定エラー）。"""
    raw = os.environ.get("SHERPA_OCR_WORLD_ROOT", "").strip()
    if not raw:
        raise ValueError("OCR有効時はSHERPA_OCR_WORLD_ROOTの明示が必要です")
    configured = Path(raw)
    if not configured.is_absolute():
        raise ValueError("SHERPA_OCR_WORLD_ROOTは絶対pathで指定してください")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        raise ValueError("SHERPA_OCR_WORLD_ROOTにアクセスできません") from exc
    if configured != resolved or configured.is_symlink() or not resolved.is_dir():
        raise ValueError("SHERPA_OCR_WORLD_ROOTはsymlinkを含まない実在directoryにしてください")
    if resolved == Path(resolved.anchor):
        raise ValueError("SHERPA_OCR_WORLD_ROOTにfilesystem rootは指定できません")
    return resolved


def validate_ocr_source_root(source_root: str | Path, *, allowed_root: Path | None = None) -> Path:
    """登録済みの資料フォルダが、明示した OCR 読み取り専用 root 経由でしか辿れないことを確認する。"""
    allowed = allowed_root if allowed_root is not None else _configured_ocr_world_root()
    source = Path(source_root)
    if not source.is_absolute():
        raise ValueError("登録済みWorld参照元は絶対pathである必要があります")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError("登録済みWorld参照元にアクセスできません") from exc
    if source != resolved or source.is_symlink() or not resolved.is_dir():
        raise ValueError("登録済みWorld参照元を安全に検証できません")
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise ValueError("登録済みWorldがSHERPA_OCR_WORLD_ROOT配下にありません") from exc
    return resolved


def validate_ocr_registered_sources(*extra_source_roots: str | Path) -> Path:
    """登録済み／候補の全資料フォルダが明示 root 配下に無ければ止める（fail-closed）。"""
    from . import store

    allowed = _configured_ocr_world_root()
    roots = list(extra_source_roots)
    for row in store.list_worlds_db():
        root_path = row.get("root_path") if isinstance(row, dict) else None
        if not isinstance(root_path, str) or not root_path:
            raise ValueError("登録済みWorld参照元を安全に検証できません")
        roots.append(root_path)
    for root in roots:
        validate_ocr_source_root(root, allowed_root=allowed)
    return allowed


def observation_dir(
    world_id: str,
    *,
    source_root: str | Path | None = None,
    validate_registered: bool = False,
) -> Path:
    """OCR 補助観測だけを置く資料フォルダ別領域。Canonical の `derived_dir` とは物理 root を分ける（OCR worker へ Canonical を read-only で渡しつつ、観測にだけ書き込みを許す）。"""
    base = observation_base_dir()
    derived_value = os.environ.get("SHERPA_DERIVED_DIR")
    derived_base = Path(derived_value) if derived_value else _repo_root() / "data" / "derived"
    if _paths_overlap(base, derived_base):
        raise ValueError("SHERPA_OBSERVATION_DIRはSHERPA_DERIVED_DIRと分離してください")
    if validate_registered:
        extra_roots = () if source_root is None else (source_root,)
        validate_observation_registered_sources(*extra_roots)
    elif source_root is not None:
        validate_observation_source_separation(source_root)
    return base / world_id


# ---- 解決済み root の request-scope pin（TOCTOU 対策）----
# 外部 API・簡易チャットは入口で共有 advisory lock を保持して `resolve_external_world()` を strict に解決し、その結果だけを使う。
# 以降のツール実行（`world_dir()` を何度も間接的に再解決する）の間に rebind が起きても、確認した資料フォルダと検索する資料フォルダが食い違わないよう、
# `world_dir()` 自体に「このリクエストの間だけ、この world_id はこの root で答える」pin を `ContextVar` で持たせる（下流のシグネチャは変えない）。
# `world_id` が完全一致したときだけ使う。リクエスト間で混線しない
_pinned_root: "contextvars.ContextVar[tuple[str, Path] | None]" = contextvars.ContextVar(
    "sherpa_pinned_world_root", default=None)


@contextlib.contextmanager
def pin_world_root(world_id: str, root):
    """このスコープ内（同一コンテキスト）の `world_dir(world_id)` をすべて `root` に固定する（registry へ再解決しない）。ネスト時は内側の pin が優先される。"""
    token = _pinned_root.set((world_id, Path(root)))
    try:
        yield
    finally:
        _pinned_root.reset(token)


def world_dir(world_id: str):
    """資料フォルダ（登録ディレクトリ）の実パス。優先順は pin（同一リクエスト内固定）＞ MCP override ＞ レジストリ binding ＞ fixtures ＞ KB。無ければ None。
    レジストリに行があれば `root_path` だけが正で、参照元が消失・マウント不可なら None（fixtures／旧 KB へは落とさない）。
    DB 不可または行が無い（未登録の dev 資料フォルダ）ときだけ fixtures（`fixtures/corpus/{id}`）／`data/kb/{id}` を見る。
    MCP サブプロセスは PG creds を持たないため、`SHERPA_MCP_WORLD_ROOT`（`agents._mcp_env()` が設定した絶対パス）を `SHERPA_MCP_WORLD` と一致する資料フォルダにだけ使う。
    絶対パス・存在・ディレクトリ・非 symlink を検証し、壊れていれば override を無視して通常の解決へ進む。
    """
    if not valid_world(world_id):
        return None
    pinned = _pinned_root.get()
    if pinned is not None and pinned[0] == world_id:
        return pinned[1]
    mcp_root = os.environ.get("SHERPA_MCP_WORLD_ROOT")
    if mcp_root and world_id == os.environ.get("SHERPA_MCP_WORLD"):
        p = Path(mcp_root)
        if p.is_absolute() and p.is_dir() and not p.is_symlink():
            return p
    row, db_ok = None, True
    try:
        from . import store
        row = store.get_world(world_id)
    except Exception:
        db_ok = False
    if db_ok and row:  # 登録済み → 参照元 root のみ（無効なら None）
        p = Path(row["root_path"])
        return p if (p.is_dir() and not p.is_symlink()) else None
    cands = []  # 未登録 or DB 不可 → dev fixtures／後方互換 KB
    if _fixtures():
        # テスト専用 world_id エイリアス（`SHERPA_TEST_WORLD_ID`）は fixtures の `v1` を再利用する
        src = "v1" if world_id == os.environ.get("SHERPA_TEST_WORLD_ID") else world_id
        cands.append(Path("fixtures/corpus") / src)
    cands.append(_kb() / world_id)
    return next((d for d in cands if d.is_dir() and not d.is_symlink()), None)


class ExternalResolverError(Exception):
    """外部 API（/ext/v1）専用 resolver が registry／KB へ到達できなかった（呼び出し側は 503 にする）。"""


class ExternalWorldResolution(NamedTuple):
    """`resolve_external_world` の結果。`status`: "ok"（`path` が有効）／"not_found"（資料フォルダが実在しない）。到達不可は `ExternalResolverError` で表す（「存在しない」と区別する）。"""
    status: str
    path: Path | None


_UNSET = object()  # registry_row 省略の判別用（None＝「未登録と確認済み」と区別する）


def resolve_external_world(world_id: str, *, registry_row=_UNSET,
                           connect_timeout: float | None = None,
                           statement_timeout_ms: int | None = None) -> ExternalWorldResolution:
    """外部 API（`/ext/v1`）専用の資料フォルダ解決。`world_dir()`（DB 不達を fixtures／KB へ落とす）と違い、registry 到達不可・登録済み root 到達不可は `ExternalResolverError` で明示する（「存在しない」に潰さない）。
    fixtures／dev KB へのフォールバックは、registry に到達できて、その world_id の行が無いときだけ行う。
    パス確認は `_is_dir_strict()` を使い、ENOENT は「無い」、それ以外の `OSError` は `ExternalResolverError` として伝播させる。
    `registry_row`: 呼び出し側が `store.list_worlds_db()` を取得済みなら渡す（N+1 回避）。
    `connect_timeout`／`statement_timeout_ms`（`registry_row` 省略時のみ有効）は `store.get_world()` へそのまま渡す。
    """
    if not valid_world(world_id):
        return ExternalWorldResolution("not_found", None)
    if registry_row is not _UNSET:
        row = registry_row
    else:
        try:
            from . import store
            row = store.get_world(world_id, connect_timeout=connect_timeout,
                                  statement_timeout_ms=statement_timeout_ms)
        except Exception as e:
            raise ExternalResolverError(f"registry unreachable for world {world_id!r}") from e
    if row:
        p = Path(row["root_path"])
        try:
            ok = _is_dir_strict(p)
        except FileNotFoundError:
            ok = False
        except OSError as e:
            raise ExternalResolverError(f"registered root unreachable for world {world_id!r}: {p}") from e
        if ok:
            return ExternalWorldResolution("ok", p)
        raise ExternalResolverError(f"registered root unreachable for world {world_id!r}: {p}")
    cands = []
    if _fixtures():
        src = "v1" if world_id == os.environ.get("SHERPA_TEST_WORLD_ID") else world_id
        cands.append(Path("fixtures/corpus") / src)
    cands.append(_kb() / world_id)
    for d in cands:
        try:
            ok = _is_dir_strict(d)
        except FileNotFoundError:
            continue  # この候補は存在しない＝次の候補へ
        except OSError as e:
            # ENOENT 以外（権限エラー等）は「存在しない」にせず 503 にする
            raise ExternalResolverError(f"cannot stat {d}") from e
        if ok:
            return ExternalWorldResolution("ok", d)
    return ExternalWorldResolution("not_found", None)


def _is_dir_strict(p: Path) -> bool:
    """`os.lstat()` でディレクトリかどうかを判定する（symlink は辿らない）。何も握りつぶさず、`FileNotFoundError` も他の `OSError` も呼び出し元へ伝播させる（扱いの分岐は呼び出し元の責務）。"""
    st = os.lstat(p)
    return stat_mod.S_ISDIR(st.st_mode)


def discover_fs_world_ids_strict() -> list:
    """fixtures／dev KB 直下の資料フォルダ ID 一覧（ファイルシステム列挙のみ・DB を触らない）。登録済みかは問わない。
    予期しない例外（権限エラー等）は `ExternalResolverError` で通知する（ENOENT だけ skip）。
    `/ext/v1/capabilities` が registry 行を自前で 1 回だけ取得して使い回すため、`discover_world_ids_strict()` から分離してある。
    """
    out = set()
    bases = []
    if _fixtures():
        bases.append(Path("fixtures/corpus"))
    bases.append(_kb())
    for base in bases:
        try:
            if not _is_dir_strict(base):
                continue
        except FileNotFoundError:
            continue
        except OSError as e:
            raise ExternalResolverError(f"cannot stat {base}") from e
        try:
            entries = list(base.iterdir())
        except OSError as e:
            raise ExternalResolverError(f"cannot enumerate {base}") from e
        for d in entries:
            try:
                if not _is_dir_strict(d):
                    continue
            except FileNotFoundError:
                continue  # 列挙〜stat の間に消えた
            except OSError as e:
                raise ExternalResolverError(f"cannot stat {d}") from e
            try:
                if not valid_world(d.name):
                    continue
                if not _has_any_file(d, strict=True):
                    continue
            except OSError as e:
                raise ExternalResolverError(f"cannot stat {d}") from e
            out.add(d.name)
    return sorted(out)


def discover_world_ids_strict() -> list:
    """外部 API 専用の資料フォルダ実在一覧（registry ∪ fixtures／dev KB）。registry 不達・列挙時の予期しない例外は `ExternalResolverError` で通知する。
    単発呼び出し用（registry 行を使い回すなら `discover_fs_world_ids_strict()` と `store.list_worlds_db()` を自前で呼ぶ）。
    """
    try:
        from . import store
        registered = {r["world_id"] for r in store.list_worlds_db()}
    except Exception as e:
        raise ExternalResolverError("registry unreachable") from e
    fs_ids = discover_fs_world_ids_strict()
    return sorted(registered | set(fs_ids))


# 旧・意味層のフォールバック位置（`concepts.json`／`l_extract.json`）。`is_semantic_control_path` 用に相対パスの定数だけ残す
_SEMANTIC_CONTROL_RELPATHS = frozenset({"semantic/concepts.json", "semantic/l_extract.json"})


def is_semantic_control_path(rel_path: str) -> bool:
    """`rel_path`（資料フォルダ root 相対 POSIX）が旧・意味層のフォールバック位置か。既存の資料フォルダに残っているこれらのファイルが、文書として grep／ES／台帳に露出しないための残置ガード
    （`corpus_docs._classify_generic_text()` が呼ぶ）。厳密な相対パス一致で判定する。
    """
    return rel_path in _SEMANTIC_CONTROL_RELPATHS


def _lstat_kind(p) -> str | None:
    """`os.lstat()` ベースで種別（`"dir"`／`"file"`／`"symlink"`／`None`）を返す。ENOENT だけは `None`、それ以外の `OSError` は呼び出し元へ伝播させる。"""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    if stat_mod.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return None


def _has_any_file(root: Path, *, strict: bool = False) -> bool:
    """root 配下に実ファイルが 1 つでもあるか（sort なし・最初の 1 件で即 return）。symlink は辿らない。
    `/world-options` が呼ぶ hot path のため、完全列挙（`safe_files`）は使わない。
    `strict=False`（既定）は列挙中の `OSError` を False 扱いにし、`strict=True`（`discover_fs_world_ids_strict()` 用）は re-raise する（「見えなかった」を「無かった」にしない）。
    """
    try:
        kind = _lstat_kind(root)
    except OSError:
        if strict:
            raise
        return False
    if kind != "dir":
        return False
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError:
            if strict:
                raise
            continue
        # `os.scandir` は一括材料化せず `next()` で 1 件ずつ取る（最初の 1 件で return する最適化を保ち、反復自体の `OSError` と各 entry の失敗を分けて扱うため）
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    if strict:
                        raise
                    break  # このディレクトリの残りは諦める
                p = Path(entry.path)
                try:
                    kind = _lstat_kind(p)
                except OSError:
                    if strict:
                        raise
                    continue
                if kind == "dir":
                    stack.append(p)
                elif kind == "file":
                    return True
    return False


def discover_world_ids() -> list:
    """登録資料フォルダの実在一覧（レジストリ ∪ fixtures/corpus ∪ data/kb 直下・フォールバック無し）。
    `list_worlds()` の「1 件も無ければ `["v1"]`」という保証は含まない。レジストリ登録済みは無条件に含め、未登録候補は実ファイルが 1 つも無いものを除く。
    """
    out = set()
    registered = set()
    try:
        from . import store
        registered = {r["world_id"] for r in store.list_worlds_db()}
    except Exception:
        pass
    out |= registered
    bases = []
    if _fixtures():
        bases.append(Path("fixtures/corpus"))
    bases.append(_kb())
    for base in bases:
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not (d.is_dir() and valid_world(d.name)) or d.name in registered:
                continue
            if not _has_any_file(d):  # 実ファイル無し＝選ぶ意味の無い候補は出さない
                continue
            out.add(d.name)
    return sorted(out)


def list_worlds() -> list:
    """登録資料フォルダの一覧（レジストリ ∪ fixtures/corpus ∪ data/kb 直下）。UI の取込ディレクトリ選択用。
    1 件も無ければ `["v1"]` を返す（空の旧レイアウトの残骸が既定選択を奪わないため）。実在しないものを実在するかのように返してはいけない呼び出し元は `discover_world_ids()` を使う。
    """
    return discover_world_ids() or ["v1"]


def accessible_world_ids(uid: str) -> list[str] | None:
    """uid がアクセス可能な資料フォルダ ID の一覧。None＝全資料フォルダ（現状は全員が全資料フォルダにアクセスできる）。
    API キー自己発行の範囲をこの一覧の部分集合に制限するため（`sherpa/routers/system_extras.py::_enforce_self_world_scope`）、将来の部門／管理者スコープはこの関数だけを差し替える。
    """
    del uid  # 現状は uid によらず None
    return None


def default_world() -> str:
    """既定の資料フォルダ（API クエリ既定値・env 未指定時の解決）。リテラル `"v1"` を 1 箇所に集約する単一の真実源（`list_worlds()` の最終 fallback と一致）。"""
    return "v1"


def world_label(world_id: str) -> str:
    """資料フォルダの表示名（レジストリ label ＞ 識別子）。"""
    row = None
    try:
        from . import store
        row = store.get_world(world_id)
    except Exception:
        row = None
    return (row or {}).get("label") or world_id


# ---- ライフサイクル（register / rebind / delete）。worker は循環回避のため遅延 import ----

class WorldConflict(ValueError):
    """既存の資料フォルダと衝突（同名／同一参照元の二重登録）。API は 409 にマップする。"""


def register(world_id: str, root_path: str, label=None, storage_mode="external_reference",
             reflect=True, run_id=None, on_run_id=None) -> dict:
    """空のレジストリへ 1 本の参照元 `root_path` を登録して取り込む。
    登録元フォルダは全体で 1 本に固定する。既存行が 1 件でもあれば更新せず失敗する（内容更新は refresh、参照先変更は rebind）。
    `run_id`: 呼び出し元が受付時に確保済みの `ingest_runs` 行を `_run_locked` へ渡す。`on_run_id`: 確保された run_id が判明した時点で呼ばれるコールバック。
    ロック: 固定 `world_registry_lock` → `world_lock(world_id)` の順で取り、行作成から失敗時 cleanup までを単一の区間に収める。事前チェック（既存 world_id・同一 root の拒否）はロック取得後に行う。
    取り込みは `worker.run`（自前 lock）ではなく `worker._run_locked` を直接呼ぶ（`run` を呼ぶと自己デッドロックする）。署名の確定／無効化は `_run_locked` 内部に任せる。
    """
    import psycopg
    from . import es_index, store
    from .ingest import worker, world_neo4j
    with store.world_registry_lock(), store.world_lock(world_id):
        registered = store.list_worlds_db()
        if registered:
            if len(registered) == 1 and registered[0].get("world_id") == world_id:
                raise WorldConflict(
                    "資料フォルダは既に登録済みです（内容更新は refresh、参照先変更は rebind を使用してください）"
                )
            raise WorldConflict(
                "資料フォルダは1本だけ登録できます。"
                "別のフォルダに変更する場合は、先に登録済みのフォルダを削除してください。"
            )
        # registry 全体 lock 内の防御的再確認（この関数を経由しない行作成を許さない）
        if store.get_world(world_id):
            raise WorldConflict(f"world '{world_id}' は既に存在します（参照先変更は rebind）")
        other = store.world_by_root(root_path)
        if other:
            raise WorldConflict(f"その参照元は既に world '{other['world_id']}' に登録済みです")
        try:
            store.upsert_world(world_id, root_path, label=label, storage_mode=storage_mode)
        except psycopg.errors.UniqueViolation:  # 同一 root を同時に別 world_id で新規登録した競合
            raise WorldConflict(f"その参照元は既に別 world に登録済みです（同時登録の競合）")
        import shutil
        registered_ok = False
        try:
            # `_run_locked` の失敗（`failed` の返却・途中の例外）はどちらも `finally` の cleanup 後に伝播させる（registry 行だけが残る孤児状態を作らない）
            res = worker._run_locked(world_id, reflect=reflect, created_by="admin", scan_root=None,
                                     run_id=run_id, on_run_id=on_run_id)
            if res["status"] == "failed":
                raise RuntimeError(f"register 失敗（取り込みエラー）: {res.get('flags')}")
            registered_ok = True
            return res
        finally:
            if not registered_ok:  # 取り込み失敗＝行も派生残骸も残さない（fail-closed）
                # 補償削除（Neo4j・台帳・ES）は `worlds.delete` と同じ伝播を best-effort で行う。各段を独立した try で包み、cleanup 自身の失敗はログのみで re-raise しない
                # （元の取り込み失敗の例外を置き換えない）。派生ディレクトリ・registry 行の削除は最後に行う
                try:
                    env = world_neo4j._env()
                    world_neo4j.delete_world(world_id, env["uri"], env["user"], env["pw"])
                except Exception:
                    logging.getLogger(__name__).warning(
                        "register 失敗時の Neo4j グラフ削除に失敗しました world_id=%s",
                        world_id, exc_info=True)
                try:
                    store.replace_documents(world_id, [])
                except Exception:
                    logging.getLogger(__name__).warning(
                        "register 失敗時の documents 台帳クリアに失敗しました world_id=%s",
                        world_id, exc_info=True)
                try:
                    es_index.delete_world(world_id)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "register 失敗時の ES 索引削除に失敗しました world_id=%s",
                        world_id, exc_info=True)

                def _log_rmtree_error(function, path, exc):
                    # 削除失敗は warning に残す（re-raise しない）。`FileNotFoundError` は正常系なので警告しない
                    if isinstance(exc, FileNotFoundError):
                        return
                    logging.getLogger(__name__).warning(
                        "register 失敗時の派生ディレクトリ削除でエラー path=%s: %s", path, exc)

                try:
                    shutil.rmtree(derived_dir(world_id), onexc=_log_rmtree_error)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "register 失敗時の派生ディレクトリ削除に失敗しました world_id=%s",
                        world_id, exc_info=True)
                try:
                    store.delete_world_row(world_id)
                except Exception:
                    logging.getLogger(__name__).warning(
                        "register 失敗時の registry 行削除に失敗しました world_id=%s",
                        world_id, exc_info=True)


def _finalize_pending_run(run_id, world_id: str, pending: dict, *, status: str | None = None,
                          extraction_snapshot: dict | None = None) -> dict:
    """`worker._run_locked(..., finalize=False)` の保留分（`_pending_finalize`）を使って、受付 run の確定を一度だけ行う（`rebind` の最終結末が判明してからまとめて書く）。
    `status`／`extraction_snapshot` を渡すと保留分のその値だけ上書きする。他フィールドは保留分のまま温存する（Graph 件数等を NULL で消さない）。
    """
    from . import store, webhooks
    st = status if status is not None else pending.get("status", "failed")
    snap = extraction_snapshot if extraction_snapshot is not None else pending.get("extraction_snapshot")
    if pending.get("confirm_sig") is not None:
        rec = store.finish_ingest_run_and_confirm_world(
            run_id, world_id, status=st, extraction_snapshot=snap,
            published_snapshot=pending.get("published_snapshot"),
            source_doc_ids=pending.get("source_doc_ids"),
            sig=pending["confirm_sig"], manifest=pending.get("confirm_manifest"),
            doc_count=pending.get("confirm_doc_count"), scan_report=pending.get("confirm_scan_report"),
            resolve_sig=pending.get("confirm_resolve_sig"),
            **({"failed_docs": pending["failed_docs"]} if pending.get("failed_docs") is not None else {}))
        if pending.get("failed_docs") is not None:
            from .ingest import worker
            worker._write_failed_docs_marker(world_id)
    else:
        rec = store.finish_ingest_run(
            run_id, status=st, extraction_snapshot=snap,
            published_snapshot=pending.get("published_snapshot"),
            source_doc_ids=pending.get("source_doc_ids"))
    # rebind の内部多段は terminal 化がここ 1 回だけなので、通知もここ 1 点に集約する
    try:
        # `source_doc_ids` の空リスト（0 件・既知）と None（未算出）を区別する
        ids = pending.get("source_doc_ids")
        doc_count = len(ids) if ids is not None else None
        webhooks.notify_run_terminal(world_id, run_id, "rebind", st, doc_count=doc_count)
    except Exception:
        logging.getLogger(__name__).warning(
            "Webhook 通知の起動に失敗しました（rebind 自体は継続）: world_id=%s", world_id, exc_info=True)
    return rec


_REBIND_PENDING_FALLBACK = {
    "status": "failed", "extraction_snapshot": {}, "published_snapshot": None,
    "source_doc_ids": None, "confirm_sig": None, "confirm_manifest": None,
    "confirm_doc_count": None, "confirm_scan_report": None}


def _select_rebind_pending(recovery_pending: dict | None, attempt_pending: dict | None) -> dict:
    """rebind 失敗確定に使う保留分（`_pending_finalize`）を選ぶ。新 root 試行・旧 root 復旧のどちらが Graph へ反映済みか（`published_snapshot is not None`）を最優先する。
    優先順位: ① 復旧が反映済みなら復旧 ② 復旧が未反映で新 root 試行が反映済みなら新 root 試行 ③ どちらも未反映なら復旧 ④ 復旧の情報が無ければ新 root 試行 ⑤ どちらも無ければ最小限のフォールバック。
    """
    if recovery_pending is not None and recovery_pending.get("published_snapshot") is not None:
        return recovery_pending
    if attempt_pending is not None and attempt_pending.get("published_snapshot") is not None:
        return attempt_pending
    if recovery_pending is not None:
        return recovery_pending
    if attempt_pending is not None:
        return attempt_pending
    return dict(_REBIND_PENDING_FALLBACK)


def rebind(world_id: str, new_root: str, label=None, reflect=True, run_id=None, on_run_id=None) -> dict:
    """参照先パス変更＝その資料フォルダを全削除し、新パスから再作成する（差分でなく破棄→再作成・他の資料フォルダは無傷）。
    設計: docs/design/scope.md「資料フォルダの登録・解決・付け替え（registry）」
    手順: ① バインドを新 root へ更新 ② `worker._run_locked`（`load_world` が world_id 単位の delete＋load を 1 tx で置換）で再構築 ③ 失敗時は旧状態への復元を試みる（fail-closed）。復元にも失敗した場合は旧状態を保証しない（`rebind_rollback_failed`）。
    復元: バインド・派生を旧へ戻す。Neo4j は失敗段階に依存し、load 段階の失敗なら tx ロールバックで旧グラフが残る。load 成功後に PG replace で失敗した場合は、旧 root から即時再構築して Neo4j を旧へ戻す（失敗時は `last_sig` を無効化して次回 sync の self-heal に委ねる）。
    `label=None` は既存 label を保持する。
    ロック: `world_lock` は外側で 1 回だけ取り、lock-free の `_run_locked` を直接呼ぶ（`worker.run` を呼ぶと自己デッドロックする）。
    署名（last_sig／manifest）の確定は `_run_locked` 内部に任せる。外側で取り込み前にスキャンして `set_world_sig` で上書きしない（`world_lock` は参照元の外部変更を防がないため、古い署名で上書きすると `sync` が unchanged と誤判定する ABA 不整合になる）。例外は復旧経路の `restore_bind_invalidate_sig`（bind 復元と同一 tx で `last_sig` を無効化するだけ）。
    `run_id`: 呼び出し元が受付時に確保済みの `ingest_runs` 行。新 root 試行・旧 root 復旧はどちらも `_run_locked` を `finalize=False` で呼び、受付 run の terminal 化はこの関数の最後で 1 回だけ行う。
    rebind 失敗時は採用した内部段の snapshot を残し、status を `failed`、reason を `rebind_failed_rolled_back`（bind 復元＋旧 root 再構築が成功）または `rebind_rollback_failed`（それ以外）にする。`on_run_id` は確保された run_id が判明した時点で呼ばれるコールバック。
    """
    from . import store
    from .ingest import worker
    from .store.worlds import rebind_bind_invalidate_sig  # 内部専用・facade に re-export しない
    with store.world_lock(world_id):
        old = store.get_world(world_id)
        if not old:
            raise ValueError(f"world '{world_id}' は未登録です（新規は register）")
        other = store.world_by_root(new_root)
        if other and other["world_id"] != world_id:
            raise WorldConflict(f"その参照元は既に world '{other['world_id']}' に登録済みです")
        # 新 root へのバインドと `last_sig`／`last_doc_count` の無効化を同一 tx で確定する（label None は既存保持）。`storage_mode` は既存値を引き継ぐ
        rebind_bind_invalidate_sig(world_id, new_root, label=label,
                                   storage_mode=old.get("storage_mode"))
        import os as _os
        import shutil
        der = derived_dir(world_id)
        # 退避先は `.` 始まり（`valid_world` が False＝自動リコンサイルの対象外。rebind 中に旧派生バックアップを孤児削除させない）
        backup = der.with_name("." + der.name + ".rebind-bak") if der.exists() else None
        # `run_id` 指定時は新 root 試行・旧 root 復旧のどちらも非 terminal な内部段として扱う
        defer_finalize = run_id is not None
        attempt_pending = None
        res = None
        try:
            if backup is not None:  # 旧 root の派生は消さず退避する（失敗時に復元）
                # 退避自体も try 内に置く（bind 更新後に失敗しても except の `restore_bind_invalidate_sig` で旧へ戻す）
                shutil.rmtree(backup, ignore_errors=True)  # 前回失敗の残骸を掃除
                _os.replace(der, backup)  # 脇へ移す＝新 root の build はまっさらから
            res = worker._run_locked(world_id, reflect=reflect,  # 新 root から build＋atomic 置換（lock-free 版）
                                      created_by="admin", scan_root=None,  # 署名の確定は内部が行う
                                      run_id=run_id, on_run_id=on_run_id, finalize=not defer_finalize,
                                      op="rebind")
            if defer_finalize:
                attempt_pending = res.get("_pending_finalize")
            if res["status"] == "failed":
                raise RuntimeError(f"rebind 失敗（取り込みエラー）: {res.get('flags')}")
        except Exception as e:  # 失敗＝旧状態へ復元（元例外は末尾の bare raise で必ず伝播する）
            # 復元の順序: ① bind を旧へ戻すのと `last_sig` 無効化を同一 tx で先に確定する（`restore_bind_invalidate_sig`・bind=旧なら sig は必ず無効化済み）。
            # tx が失敗（PG 断）したらどちらも未適用で bind=新のまま、PG 復旧後の sync が新へ収束する。② 旧派生を復元（best-effort）。③ 旧 root から即時再構築して Neo4j を旧へ戻す（best-effort・冪等）。
            # 復元側の二次例外で元例外を握りつぶさない
            if attempt_pending is None:
                attempt_pending = getattr(e, "_sherpa_ingest_run_pending", None)
            bind_restored = False
            try:
                store.restore_bind_invalidate_sig(
                    world_id, old["root_path"],
                    label=old.get("label"), storage_mode=old.get("storage_mode"))
                bind_restored = True
            except Exception:
                pass
            # ② ③ は bind が旧へ戻った時だけ行う（bind 未復元で旧派生を戻すと新 root に旧派生が混入する）
            recovery_pending = None
            recovery_ok = False
            if bind_restored:
                if backup is not None:
                    try:
                        shutil.rmtree(der, ignore_errors=True)  # 途中まで作った新派生を捨てる
                        _os.replace(backup, der)  # 旧派生を完全復元
                    except Exception:
                        pass
                try:
                    # 旧 root から即時再構築（Neo4j を旧へ戻す）。署名の確定／無効化は `_run_locked` 内部が行う（戻り値の sig 系は参照しない）
                    res2 = worker._run_locked(world_id, reflect=reflect,
                                              created_by="admin", scan_root=None,
                                              run_id=run_id, on_run_id=on_run_id, finalize=not defer_finalize,
                                              op="rebind")
                    # 全文検索の索引を作り直せなかった復旧は「戻せた」と扱わない。
                    recovery_ok = res2["status"] != "failed" and not any(
                        str(f.get("reason") or "").startswith("es_index_failed")
                        for f in res2.get("flags") or [] if isinstance(f, dict))
                    if defer_finalize:
                        recovery_pending = res2.get("_pending_finalize")
                except Exception as e2:
                    if defer_finalize:
                        recovery_pending = getattr(e2, "_sherpa_ingest_run_pending", None)
            e._sherpa_rebind_restored = bool(bind_restored and recovery_ok)  # 呼び出し側が失敗文を復元の成否で分ける
            # 受付 run の終端確定は復旧結果が判明した後に 1 回だけ行う。採用した内部段の snapshot を残して status だけ `failed` にし、reason で復元の成否を区別する
            if defer_finalize:
                reason = ("rebind_failed_rolled_back" if (bind_restored and recovery_ok)
                         else "rebind_rollback_failed")
                pending = _select_rebind_pending(recovery_pending, attempt_pending)
                snap = dict(pending.get("extraction_snapshot") or {})
                snap["flags"] = list(snap.get("flags", [])) + [
                    {"doc": None, "action": "blocked", "reason": reason}]
                try:
                    _finalize_pending_run(run_id, world_id, pending, status="failed",
                                          extraction_snapshot=snap)
                    e._sherpa_ingest_run_recorded = True
                except Exception:
                    logging.getLogger(__name__).warning(
                        "rebind 失敗の run 記録に失敗しました（best-effort）: world_id=%s",
                        world_id, exc_info=True)
            raise
        if backup is not None:  # 成功＝退避した旧派生を破棄
            shutil.rmtree(backup, ignore_errors=True)
        # 署名の確定は `_run_locked` 内部が済ませている（ここで上書きしない）
        if defer_finalize:
            res["run"] = _finalize_pending_run(run_id, world_id, res["_pending_finalize"])
        return res


def delete(world_id: str, reflect=True, run_id=None) -> bool:
    """資料フォルダを完全削除する（派生物 wipe ＋ レジストリ行削除）。参照元（外部フォルダ）は消さない。
    `_wipe_locked` は Neo4j 削除失敗時に例外を投げる（fail-closed）ので、グラフ削除に成功した時だけ行を削除する。
    `world_lock` は外側で 1 回だけ取り、wipe と行削除を同じ区間に収める（lock-free の `_wipe_locked` を直接呼ぶ）。
    `run_id`: 受付時に確保済みの `ingest_runs` 行。指定時は行の DELETE と run 完了 UPDATE を `store.finish_ingest_run_and_delete_world` で同一トランザクションにする。省略時は `store.delete_world_row`。
    行削除に成功したら、`preview_service._GRAPH_VIEW_LOCK` を取ってグラフ view キャッシュ（`build_preview` と共有）を破棄する（並行中の構築が古い view を再挿入するのを防ぐ）。
    """
    from . import store
    from .ingest import worker
    with store.world_lock(world_id):
        worker._wipe_locked(world_id, reflect=reflect)  # 失敗なら例外＝行も run の terminal 化もしない
        if run_id is not None:
            _rec, ok = store.finish_ingest_run_and_delete_world(
                run_id, world_id, status="auto_published",
                extraction_snapshot={"docs": 0, "nodes": 0, "edges": 0, "deleted": True})
        else:
            ok = store.delete_world_row(world_id)
    if ok:
        from . import preview_service
        with preview_service._GRAPH_VIEW_LOCK:
            preview_service._GRAPH_VIEW_CACHE.pop(world_id, None)
    try:
        from . import reconcile  # 削除後に孤児派生物を自動掃除する
        reconcile.reconcile_derivatives(reflect=reflect)
    except Exception:
        pass
    return ok
