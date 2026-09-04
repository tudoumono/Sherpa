"""登録ディレクトリ（world）のレジストリ・解決・ライフサイクル（鏡モデル・MIRROR-MODEL §3/§4/§8）。

旧 `versions.py`（版ライフサイクル）を置換。鏡では **world＝登録した1ディレクトリ**＝1グラフ＋1 ES。
world は**参照元 root_path に 1:1 バインド**（`store.worlds`）。参照先変更（rebind）＝**旧 world の派生物を
全削除して新パスから再ミラー**（即反映ライブ鏡の思想を「バインド変更」にも適用）。
別案件は**別 world として追加**（world_id で同居・検索は分離）。`world_id` は内部識別子（UI では version の語を出さない）。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import stat as stat_mod
from pathlib import Path
from typing import NamedTuple

from .grep_tool import valid_world   # 識別子の許容文字（パストラバーサル防止）


def semantic_dir(world_id: str) -> Path:
    """派生 `semantic/` ディレクトリ（`es_index.py` の埋め込みキャッシュ〔`embed_cache/` シャード群〕の
    置き場）。旧・意味層フル抽出/対応橋（`concepts.json`/`l_extract.json` 等）一式は GRAPH-SRC
    2026-09-04 で撤去済み（`SemanticFiles`/`semantic_files()` も同時撤去・復活させない）。"""
    return derived_dir(world_id) / "semantic"


def _fixtures() -> bool:
    return os.environ.get("SHERPA_USE_FIXTURES", "").lower() in ("1", "true", "yes")


# RV HIGH（2026-07-03・4頭脳比較で発覚の実機バグ）: SHERPA_KB_DIR/SHERPA_DERIVED_DIR の
# **相対既定値**は cwd 基準だと呼び出し元プロセスの cwd（例: MCP サブプロセスの cwd=authoring）に
# 引きずられて誤解決する。既定はリポジトリ基準（Path(__file__) 経由）で解決する（恒久対策・
# クラスごと潰す）。env で**明示的に**指定された値（絶対/相対いずれも）はそのまま尊重する
# （既存の cwd 相対オーバーライドの挙動は変えない＝挙動変更は「未設定時の既定」に限定）。
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _kb() -> Path:
    v = os.environ.get("SHERPA_KB_DIR")
    return Path(v) if v else _repo_root() / "data" / "kb"


def derived_dir(world_id: str) -> Path:
    """world の派生領域ルート（**READ-ONLY のソースには書かず** WSL 側に持つ）。配下: `md/`（人間用）／
    `rag/`（RAG 正本＋証跡）／`ir/`（中間表現）／`semantic/`（ES 埋め込みキャッシュ＝`embed_cache`。
    旧・意味層フル抽出 l_extract は GRAPH-SRC 2026-09-04 で撤去済み）。delete 時はこの木ごと消す。"""
    v = os.environ.get("SHERPA_DERIVED_DIR")
    base = Path(v) if v else _repo_root() / "data" / "derived"
    return base / world_id


def derived_md_dir(world_id: str) -> Path:
    """人間用 MD のミラー置き場（派生領域の `md/` 配下・§8.1 三階層）。画面表示・原本DL の根拠。
    取り込みのたび作り直す部分（semantic は残す）。"""
    return derived_dir(world_id) / "md"


def derived_rag_dir(world_id: str) -> Path:
    """RAG 正本＋証跡の置き場（`{rel}.rag.md`／`{rel}.rag_chunks.jsonl`／`{rel}.assets/`・
    `derived_md_dir` と同じ derived root の兄弟・§8.1 三階層）。grep（rag優先）・ES・グラフ（L層）が読む。"""
    return derived_dir(world_id) / "rag"


def derived_ir_dir(world_id: str) -> Path:
    """中間表現の置き場（`{rel}.document.json`／`{rel}.evidence.json`／`{rel}.derived.json`／
    `{rel}.ocr_route.json`・§8.1 三階層）。再生成可能・検索には出さない・drift 判定専用。"""
    return derived_dir(world_id) / "ir"


# ---- OCR 観測領域（任意機能・既定 OFF・2026-08-16 移植）----------------------------------------
# OCR は隔離 worker が動かす。worker には **登録ディレクトリと Canonical 派生を read-only** で渡し、
# **書けるのは観測領域だけ** に限定する。以下はその境界を fail-closed で守るための検証群で、
# 「別の bind mount 経由で同じ inode が書けてしまう」ことを防ぐため、文字列比較ではなく resolve 済み
# パスの包含関係で判定する（CLAUDE.md「登録ディレクトリ配下は読み取り専用」の実装上の担保）。

def _paths_overlap(first: Path, second: Path) -> bool:
    """2つの root が同一、または祖先/子孫の関係かを返す。

    観測領域は書き込み可、World 参照元は読み取り専用でマウントする。別 path から同じ inode へ
    書けてしまう経路を塞ぐため、文字列として違うだけでは不十分＝解決後の包含関係で見る。
    """
    try:
        left = first.resolve()
        right = second.resolve()
    except OSError as exc:
        raise ValueError("OCR観測領域のrootを安全に解決できません") from exc
    return left == right or left in right.parents or right in left.parents


def observation_base_dir() -> Path:
    """観測領域のベース（`SHERPA_OBSERVATION_DIR`＞`data/observations`）。相対 path は
    `derived_dir` と同じく cwd 基準という既存規約に従う。"""
    value = os.environ.get("SHERPA_OBSERVATION_DIR")
    return Path(value) if value else _repo_root() / "data" / "observations"


def validate_observation_source_separation(source_root: str | Path) -> None:
    """書き込み可の観測領域が World 参照元と重なっていたら止める。"""
    if _paths_overlap(observation_base_dir(), Path(source_root)):
        raise ValueError("SHERPA_OBSERVATION_DIRはWorld参照元と物理分離してください")


def validate_observation_registered_sources(*extra_source_roots: str | Path) -> None:
    """登録済みの全 World 参照元と観測領域が分離していることを確認する。

    ここはセキュリティ境界なので、registry の失敗や壊れた行は**握りつぶさず送出する**
    （「registry が空」と解釈して素通しさせない）。register/rebind は行が出来る前の候補 root を
    渡してくるため、候補と既存行の両方を検査する。
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


def observation_current_dir(world_id: str) -> Path | None:
    """いま公開されている OCR 観測のディレクトリ（無ければ None）。

    観測は Canonical（決定的に変換した MD/Evidence）とは別の木に置く。検索はここも読むため、
    「画像の中の文字」で資料に辿り着ける。どの世代が公開中かは観測領域の pointer が正で、
    Canonical と食い違う（取り込みが先に進んだ）場合は None＝古い観測を読ませない。
    """
    from .ingest import derived_generation, observation_render

    try:
        base = observation_dir(world_id)
    except ValueError:                       # 保存先の設定不備は検索を止めずに「観測なし」とする
        return None
    canonical = derived_generation.active_generation_id(derived_dir(world_id))
    if not canonical:
        return None
    return observation_render.active_observation_dir(base, canonical_generation_id=canonical)


def _configured_ocr_world_root() -> Path:
    """OCR worker へ read-only で渡す、明示された参照元 root。

    OCR は任意機能なので、呼ぶのは有効化された取り込み/worker の境界だけ。`/mnt` のような広い
    既定値は**意図的に持たない**＝未設定・相対・symlink・到達不可はすべて設定エラーとして落とす。
    """
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
    """登録 World が、明示した OCR 読み取り専用 root 経由でしか辿れないことを確認する。"""
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
    """登録済み/候補の全 World が明示 root 配下に無ければ止める（fail-closed）。"""
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
    """OCR 補助観測だけを置く world 別領域。

    Canonical Evidence/MD の `derived_dir` とは**物理 root を分ける**。これにより隔離 OCR worker へ
    Canonical 派生を read-only で渡しつつ、検索用の別観測にだけ書き込みを許せる。
    """
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


# ---- 解決済み root の request-scope pin（PART-4・TOCTOU 対策）----------------------------------
# `/ext/v1/research`（`sherpa/research_service.py`）は呼び出し元（`ext_api.py`）の preflight 解決
# とは別に、共有 advisory lock を保持した状態で `resolve_external_world()` を改めて strict に呼び、
# その解決結果**だけ**を使う。以降 agentic search の複数ターン（数十秒〜規定の反復上限ぶん）に
# わたって `agentic_search.run_tool` 経由で `world_dir()` を何度も間接的に再解決する
# （`grep_tool.grep_search`／`corpus_docs.iter_world_documents`／`agentic_search._safe_doc_path`／
# `verify_citation`／`verify_doc_exists` など、いずれもこの関数が唯一の真実源）。pin が無ければ、
# この authoritative な解決と実際のツール実行の間に registry の rebind（同じ world_id が別
# root_path に差し替わる）が起きたとき、確認した world と実際に検索する world が食い違いうる。
#
# `ContextVar` にする理由: `world_dir()` を直接呼ぶ全箇所（上記4モジュール以上）に `root=` 引数を
# 追加して呼び出し元まで貫通させると、`providers/base.py`（PART-4 では変更しない契約——
# `sherpa/research_service.py` docstring 冒頭「読み取り専用で再利用する」参照）が呼ぶ
# `agentic_search.verify_citation`/`_dedupe_citations_and_evidence` の経路にも改修が要る。
# ここで唯一の真実源（`world_dir()`）自体に「このリクエストの間だけ、この world_id は
# この root で答える」という pin を持たせれば、呼び出し階層のどこにも手を入れずに TOCTOU を
# 閉じられる（下流の関数シグネチャは1つも変えない）。`contextvars.ContextVar` は
# `sherpa/ext_api.py::_request_id_ctx` と同じ機構——FastAPI の同期 def ハンドラ（`run_in_threadpool`
# 経由）でもリクエストのコンテキストがそのままコピーされてワーカースレッドへ渡るため、
# 並行リクエスト間で混線しない（`os.environ` ベースの `SHERPA_MCP_WORLD_ROOT` と違いプロセス全体を
# 汚染しない）。`world_id` が完全一致した時だけ使う（他 world の解決には影響しない＝MCP override と
# 同じスコープ限定の設計）。
_pinned_root: "contextvars.ContextVar[tuple[str, Path] | None]" = contextvars.ContextVar(
    "sherpa_pinned_world_root", default=None)


@contextlib.contextmanager
def pin_world_root(world_id: str, root):
    """このスコープ内（同一コンテキスト＝同一リクエスト/スレッド）の `world_dir(world_id)` 呼び出しを
    すべて `root` に固定する（registry への再解決を行わない）。ネスト時は内側の pin が優先され、
    スコープを抜けると外側の状態（pin 無し、または外側の pin）へ自動的に戻る。
    """
    token = _pinned_root.set((world_id, Path(root)))
    try:
        yield
    finally:
        _pinned_root.reset(token)


def world_dir(world_id: str):
    """world（登録ディレクトリ）の実パス。**pin（同一リクエスト内固定）＞ MCP override ＞ レジストリ binding ＞ fixtures ＞ KB**。無ければ None。

    レジストリに**行があれば root_path だけが正**＝参照元が消失/マウント不可なら **None（fail-closed）**で
    fixtures/旧KB へは落とさない（別内容を同 world として誤読しない・MIRROR §4・RV High#3）。
    DB 不可、または行が無い（未登録の dev world）ときのみ fixtures（`fixtures/corpus/{id}`）／`data/kb/{id}` へ。

    RV HIGH（2026-07-03・4頭脳比較で発覚の実機バグ）: MCP サブプロセスは PG creds を持たない
    （`agents._MCP_PASSTHROUGH` に含めない設計）ため、サンドボックス下では registry 解決
    （Postgres 接続）に頼れない/信頼できない可能性がある。`SHERPA_MCP_WORLD`（対象 world_id）と
    `SHERPA_MCP_WORLD_ROOT`（サーバプロセスが registry 込みで解決した絶対パス・`agents._mcp_env()`
    が設定）が**要求された world_id に一致する時だけ**それを使い、registry への再解決を避ける
    （他 world には効かないスコープ限定 override）。絶対パス・存在・ディレクトリ・非symlink を
    検証し、壊れていれば override 自体を無視して通常の解決（registry→fixtures→KB）へフォールバックする
    （override が信頼できないという理由だけで fail-closed にはしない＝既存の多段フォールバック方針を維持）。
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
    if db_ok and row:                                # 登録済み world → 参照元 root のみ（無効なら unavailable）
        p = Path(row["root_path"])
        return p if (p.is_dir() and not p.is_symlink()) else None
    cands = []                                       # 未登録 or DB 不可 → dev fixtures / 後方互換 KB
    if _fixtures():
        # テスト専用 world_id エイリアス（2026-07-03 インシデント対応 HIGH#2）: 固定 world_id 'v1' を
        # 共有 Neo4j/ES のラベルに使うと実登録 world と衝突しうるため、テストは専用 id（例
        # 'pytest-v1'・`SHERPA_TEST_WORLD_ID` で指定）をラベルに使う一方、fixture データ源は
        # 従来どおり `fixtures/corpus/v1` を再利用する（最小の写像・データを複製しない）。
        src = "v1" if world_id == os.environ.get("SHERPA_TEST_WORLD_ID") else world_id
        cands.append(Path("fixtures/corpus") / src)
    cands.append(_kb() / world_id)
    return next((d for d in cands if d.is_dir() and not d.is_symlink()), None)


class ExternalResolverError(Exception):
    """外部 API（/ext/v1）専用 resolver が registry/KB へ到達できなかった（呼び出し側は 503 にする）。"""


class ExternalWorldResolution(NamedTuple):
    """`resolve_external_world` の結果。`status`: "ok"（`path` が有効）／"not_found"（world が実在しない）。
    到達不可（registry 不達・登録済み root 不達）は `status` ではなく `ExternalResolverError` で表す
    （呼び出し側が「存在しない」と取り違えないよう、正常系の戻り値と区別する）。
    """
    status: str
    path: Path | None


_UNSET = object()   # registry_row 省略の判別用（None＝「未登録と確認済み」と区別する）


def resolve_external_world(world_id: str, *, registry_row=_UNSET,
                           connect_timeout: float | None = None,
                           statement_timeout_ms: int | None = None) -> ExternalWorldResolution:
    """外部 API（`/ext/v1`）専用の world 解決。`world_dir()`（UI/取込向け・DB 不達を fixtures/KB へ
    フォールバックする多段解決）とは異なり、**registry 到達不可・登録済み root 到達不可は
    `ExternalResolverError` で明示する**（この2つを「存在しない」に潰すと、外部呼び出し元が
    一時的な不達を「未登録/削除済み」と取り違え、同名の別内容（dev fixtures 等）を実体だと
    誤解しかねない）。fixtures/dev KB へのフォールバックは **registry に到達できて、かつ
    その world_id の行が無い**ときだけ行う（registry 到達不可時に同名 dev root を誤って配信しない）。

    パス確認は `_is_dir_strict()` を使う（`Path.is_dir()`/`Path.is_symlink()` は内部で任意の
    `OSError` を握って False を返すため使わない）——登録済み root・未登録候補（fixtures/KB）の
    どちらも、ENOENT（存在しない）は「無い」として扱うが、それ以外の `OSError`（権限エラー等）は
    `ExternalResolverError` として伝播させる。権限エラー等を「存在しない」に取り違えて 404 相当
    （`not_found`）に潰すと、一時的な確認不能を「削除済み」と誤解しかねないため。

    `registry_row`: 呼び出し側が `store.list_worlds_db()` を一括取得済みなら渡す（world ごとに
    `store.get_world()` を引き直さない・discovery の N+1 回避）。省略時はここで1回引く。

    `connect_timeout`/`statement_timeout_ms`（`registry_row` 省略時のみ意味を持つ・両方省略可・
    既定 None＝無期限＝既存呼び出し元は無変更）: `store.get_world()` へそのまま転送する
    （PART-4 が残り時間ベースで渡す・同関数 docstring 参照）。
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
            continue   # この候補は単に存在しない＝次の候補（または not_found）へ
        except OSError as e:
            # `Path.is_dir()`/`Path.is_symlink()` は内部で任意の OSError を握って False を
            # 返す仕様のため、ここでは使わず `_is_dir_strict()` に統一している——ENOENT 以外
            # （権限エラー等）を「存在しない」（404相当）に取り違えず 503 にする。
            raise ExternalResolverError(f"cannot stat {d}") from e
        if ok:
            return ExternalWorldResolution("ok", d)
    return ExternalWorldResolution("not_found", None)


def _is_dir_strict(p: Path) -> bool:
    """`os.lstat()`（symlink は辿らない＝`Path.is_symlink()` 相当も一度に判定できる）で
    ディレクトリかどうかを判定する。この関数自体は何も握り潰さない——`FileNotFoundError`
    （ENOENT）も他の `OSError`（権限エラー等）も区別せずそのまま呼び出し元へ伝播させる。
    「ENOENT だけは無視して次の候補へ・それ以外は 503 にする」という判断は呼び出し元の
    責務（各呼び出し元が個別に `except FileNotFoundError: continue`／
    `except OSError: raise ExternalResolverError` のように分けて処理する）。

    `Path.is_dir()`／`Path.is_symlink()` は内部で任意の `OSError` を握って False を返す
    （ドキュメント上の仕様）ため、strict 経路では使えない——「見えなかった」（権限エラー等）を
    「無かった」に取り違えて候補から静かに落としてしまう。
    """
    st = os.lstat(p)   # FileNotFoundError/OSError は呼び出し元が処理する
    return stat_mod.S_ISDIR(st.st_mode)


def discover_fs_world_ids_strict() -> list:
    """fixtures/dev KB 直下の world_id 一覧（**ファイルシステム列挙のみ・DB を触らない**）。

    登録済みかどうかは問わない（呼び出し側が registry の集合と突き合わせて重複排除する）。
    列挙時の予期しない例外（権限エラー等）は握り潰さず `ExternalResolverError` で通知する
    （`_is_dir_strict()` により ENOENT だけを skip・それ以外の OSError は伝播させる）。
    `discover_world_ids_strict()` から分離しているのは、呼び出し側（`/ext/v1/capabilities`）が
    registry 行を自前で1回だけ取得し使い回すため（同じスナップショットから ID・root・
    最終同期時刻を導出し、2回目の DB 往復・DB 例外の取りこぼしを避ける）。
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
                continue   # iterdir()〜stat() の間に消えた（レース・実在しないので単純に skip）
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
    """外部 API 専用の world 実在一覧（registry ∪ fixtures/dev KB）。`discover_world_ids()`
    （レジストリ不達を空扱いで黙って続行する・UI 隣接用途向け）とは異なり、registry 不達・
    KB/fixtures 列挙時の予期しない例外（権限エラー等）を握り潰さず `ExternalResolverError` で
    通知する。単発呼び出し用（registry 行を使い回したい呼び出し元は `discover_fs_world_ids_strict()`
    と `store.list_worlds_db()` を自前で1回ずつ呼ぶこと）。
    """
    try:
        from . import store
        registered = {r["world_id"] for r in store.list_worlds_db()}
    except Exception as e:
        raise ExternalResolverError("registry unreachable") from e
    fs_ids = discover_fs_world_ids_strict()
    return sorted(registered | set(fs_ids))


# 旧・意味層フル抽出/対応橋の world配下フォールバック位置（`concepts.json`／`l_extract.json`）。
# 実体の解決機構（旧 `semantic_paths()`）自体は GRAPH-SRC 2026-09-04 で撤去済み（単一の真実源・
# `is_semantic_control_path` 用に相対パスの定数だけ残す）。
_SEMANTIC_CONTROL_RELPATHS = frozenset({"semantic/concepts.json", "semantic/l_extract.json"})


def is_semantic_control_path(rel_path: str) -> bool:
    """`rel_path`（world root 相対 POSIX）が旧・意味層機構（手動意味層 concepts／L抽出 l_extract）の
    world配下フォールバック位置か。**撤去済み機構の残置ガード**（GRAPH-SRC 2026-09-04・K9-K11）:
    生成側（旧 `semantic_paths()`／`graph_extract.extract_world` 等）は撤去済みだが、既存 world の
    ディスク上に過去の取り込みで置かれたこれらのファイルが残っていることがある。

    `importance.is_importance_control_path`（`_重要度.txt`）と同じ性質の内部制御ファイル——
    軽量テキスト枠（`ingest.text_kind`）が `.json` を汎用コード扱いにしたことで、`semantic/`
    配下に置かれたこれらのファイルが偶然「ただの文書」として grep/ES/台帳に露出しないよう
    `corpus_docs._classify_generic_text()` が呼ぶ（2026-09-02・実 fixture `fixtures/corpus/v1/
    semantic/concepts.json` で発覚）。ここは**厳密な相対パス一致**（`importance` 側の「ファイル名
    一致ならどの階層でも」とは違う）——`semantic/` 配下の別名ファイルや、無関係フォルダの同名
    ファイルまで巻き込まない。
    """
    return rel_path in _SEMANTIC_CONTROL_RELPATHS


def _lstat_kind(p) -> str | None:
    """`os.lstat()` ベースで種別を返す（`"dir"`/`"file"`/`"symlink"`/`None`）。パスが存在しない
    （`FileNotFoundError`／ENOENT）だけは `None`（skip 対象）とし、それ以外の `OSError`
    （権限エラー等）は呼び出し元へ伝播させる（`scope_infer._lstat_kind` と同じ設計）。

    `Path.is_dir()`/`is_file()`/`is_symlink()`、`os.DirEntry.is_dir()`/`is_file()`/`is_symlink()`
    は内部で任意の `OSError` を握って False を返すため strict 経路では使えない——「見えなかった」
    （権限エラー等）を「無かった」に取り違えて候補から静かに落としてしまう。
    """
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
    """root 配下に実ファイルが1つでもあるか（sort なし・最初の1件で即 return）。

    `scope_infer.safe_files` は grep/DL 用の**完全な**安全列挙（各階層 sorted・rel_path 生成込み）だが、
    ここでは「候補として出す価値があるか」の存在確認だけで十分＝順序も rel_path も要らない。
    `/world-options` はチャット初期化のたび（画面を開くたび）呼ばれる hot path なので、未登録候補ごとに
    `safe_files` の完全列挙を回すのは無駄が大きい。symlink は辿らない（safe_files と同じ脱出防止の方針）。

    `strict=False`（既定・`list_worlds()`/`discover_world_ids()` 等の UI 隣接用途）: 列挙中の
    `OSError` は黙って False 扱いにする（従来どおり）。`strict=True`（`discover_fs_world_ids_strict()`
    専用）: 同じ `OSError` を re-raise する——「見えなかった」を「実ファイル無し」に取り違えて
    候補から静かに落としてはいけない（呼び出し側が `ExternalResolverError`／503 にする）。
    `_lstat_kind()`（`os.lstat`＋`stat.S_ISDIR` 等）を使う——`Path`/`os.DirEntry` の便利メソッドは
    内部で OSError を握るため strict 経路には使えない。
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
        # `list(os.scandir(cur))` で一括材料化すると、最初の1件で return できるはずの最適化が
        # 死ぬ（巨大ディレクトリで全件を先に読み切ってしまう）。`with` で確実にクローズしつつ、
        # 1件ずつ逐次 `next()` する——`for entry in it:` だと反復自体が投げる OSError（列挙途中の
        # 消失等）を個々の entry の `_lstat_kind` 失敗と区別できず、1エントリの権限エラーで
        # ディレクトリ全体を放棄してしまう（strict=False 時の挙動）か直接 raise（strict=True 時）に
        # なるかの分岐を this 関数レベルで保てない——`next()` を手動で呼び分離する。
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError:
                    if strict:
                        raise
                    break   # このディレクトリの残りは諦める（list() が丸ごと失敗するのと同じ挙動）
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
                    return True                 # 最初の1件で即終了（TTL キャッシュ等は過剰設計＝不要）
    return False


def discover_world_ids() -> list:
    """登録 world の実在一覧（レジストリ ∪ fixtures/corpus ∪ data/kb 直下・**フォールバック無し**）。

    `list_worlds()` が UI 向けに付ける「1件も無ければ `["v1"]` を返す」という空でない保証は
    含まない＝呼び出し元が「本当に何が存在するか」を必要とする場合（外部公開 discovery・
    キーの world スコープ検証等）はこちらを使う。レジストリ登録済みは無条件に含め、
    fixtures/data/kb 直下の未登録候補は実ファイルが1つも無いものを除外する（`list_worlds()` と
    同じ選別ロジック・単一の真実源）。
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
            if not _has_any_file(d):                    # 実ファイル無し＝選ぶ意味の無い候補は出さない
                continue
            out.add(d.name)
    return sorted(out)


def list_worlds() -> list:
    """登録 world の一覧（レジストリ ∪ fixtures/corpus ∪ data/kb 直下）。UI の取込ディレクトリ選択用。

    1件も無ければ `["v1"]` を返す（2026-07 修正・S2の経緯）: 旧レイアウト（`data/kb/{layer}/
    {version}/…`）の空ディレクトリが残っていると世界として混入し、アルファベット順で先頭に来て
    世界セレクタの既定選択を奪う＝実質そのディレクトリの空の範囲ツリーが出て「範囲セレクタが
    消えた」ように見える不具合があった（scope_tree 自体は無傷）。この保証が不要（＝実在しない
    world を実在するかのように返してはいけない）呼び出し元は `discover_world_ids()` を使うこと。
    """
    return discover_world_ids() or ["v1"]


def accessible_world_ids(uid: str) -> list[str] | None:
    """uid がアクセス可能な world_id の一覧。None＝全 world（現状の方針）。

    利用者による API キー自己発行の world スコープは「本人がアクセスできる範囲 ⊆」に強制する
    契約になっている。現状は KB が全社1つ＝全ユーザーが全 world にアクセスできるため
    None（無制限）を返すが、将来
    部門/管理者スコープ（CLAUDE.md「KB は全社1つ＋利用者登録可（将来：管理者/部門スコープ）」）
    を実装する際は、呼び出し側（`sherpa/routers/system_extras.py::_enforce_self_world_scope`）
    が「全員全 world」をハードコードしなくて済むよう、この関数だけを差し替えればよい。
    """
    del uid   # 現状は uid によらず None（全 world）
    return None


def default_world() -> str:
    """既定 world（API クエリ既定値・env 未指定時の解決）。リテラル `"v1"` の散在を1箇所に集約する単一の真実源。

    現状は後方互換の `"v1"` 固定（`list_worlds()` の最終 fallback と一致）＝**挙動不変**。
    レジストリ先頭を動的に返す案は、既定 world を `"v1"` に固定する現行挙動（テストで担保）を変えるため採らない。
    """
    return "v1"


def world_label(world_id: str) -> str:
    """world の表示名（レジストリ label ＞ 識別子）。鏡では UI に version の語を出さない（§8）。"""
    row = None
    try:
        from . import store
        row = store.get_world(world_id)
    except Exception:
        row = None
    return (row or {}).get("label") or world_id


# ---- ライフサイクル（register / rebind / delete）。worker は遅延 import（循環回避）----

class WorldConflict(ValueError):
    """既存 world と衝突（同名 / 同一参照元の二重登録）。API は 409 にマップ。"""


def register(world_id: str, root_path: str, label=None, storage_mode="external_reference",
             reflect=True, run_id=None, on_run_id=None) -> dict:
    """空のレジストリへ1本の参照元 root_path を登録して取り込む。

    `run_id`（ING-3）＝呼び出し元が受付時に O(1) で確保済みの `ingest_runs` 行を
    `_run_locked` へそのまま転送する。`on_run_id`＝旧経路（後方互換）のコールバック
    （`_run_locked` が確保した run_id が判明した瞬間に呼ばれる）。

    標準MVPは登録元フォルダを全体で1本に固定する。既存行が1件でもあれば、同じ root/world_id を含めて
    **更新せず失敗**する（内容更新は refresh、参照先変更は rebind）。既存データの自動選択・削除はしない。

    secRV round-3（HIGH-B・2026-07-14）: 行作成（`upsert_world`）〜失敗時 cleanup（行削除＋派生 rmtree）
    までを**単一の world_lock 区間**に収める。旧実装は行作成〜cleanup が lock の外にあり、register が
    失敗して lock を持たないまま cleanup する前に、並行 sync/rebind が同じ world_id で run を開始でき、
    その run 完了後（＝行がまだ存在する間に確定した last_sig）に register 側の cleanup が行を削除すると
    「registry 無し・グラフ/台帳あり」の孤児状態が残り得た（並行 sync 側は自分の `set_world_sig` を
    UPDATE 0行のまま成功扱いする）。事前チェック（既存 world_id 拒否・同一 root 1:1 拒否）も lock 取得
    **後**に行う（lock 待ちの間に他プロセスが同じ world_id/root を登録し得るため）。異なる world_id 同士の
    同時登録も直列化するため、固定 `world_registry_lock` → `world_lock(world_id)` の順で取得し、登録件数の
    確認から失敗時 cleanup まで両方を保持する。
    取り込みは `worker.run`（自前 lock）ではなく lock-free の `worker._run_locked` を直接呼ぶ
    （rebind/delete と同じ確立済みパターン・session-level advisory lock は別コネクション再入不可のため
    `run` を呼ぶと自己デッドロックする）。署名の確定/無効化は `_run_locked` 内部（lock 保持中）に一任する。
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
        # registry 全体 lock 内の防御的再確認。将来もこの関数を経由しない行作成を許可しない。
        if store.get_world(world_id):
            raise WorldConflict(f"world '{world_id}' は既に存在します（参照先変更は rebind）")
        other = store.world_by_root(root_path)
        if other:
            raise WorldConflict(f"その参照元は既に world '{other['world_id']}' に登録済みです")
        try:
            store.upsert_world(world_id, root_path, label=label, storage_mode=storage_mode)
        except psycopg.errors.UniqueViolation:                 # 同一 root を同時に別 world_id で新規登録した競合（RV Med#2）
            raise WorldConflict(f"その参照元は既に別 world に登録済みです（同時登録の競合）")
        import shutil
        registered_ok = False
        try:
            # `_run_locked` が「failed」を返す場合だけでなく、途中で例外を bare raise する場合
            # （PG/Neo4j 接続断等）も同じ扱いにする——try/finally で両方の失敗経路を一本化し、
            # registry 行だけが残って取り込みは一度も成功していない孤児状態を作らない
            # （bare raise は except で個別に握り潰さず、finally の cleanup 後にそのまま伝播させる）。
            res = worker._run_locked(world_id, reflect=reflect, created_by="admin", scan_root=None,
                                     run_id=run_id, on_run_id=on_run_id)
            if res["status"] == "failed":
                raise RuntimeError(f"register 失敗（取り込みエラー）: {res.get('flags')}")
            registered_ok = True
            return res
        finally:
            if not registered_ok:                              # 取り込み失敗＝行も派生残骸も残さない（fail-closed）
                # `_run_locked` は Neo4j load を先に commit してから台帳・ES・実行記録・署名を
                # 個別に進めるため、Neo4j 成功後に台帳書込（`replace_documents`）等が失敗すると
                # 「registry 行は無いのに Neo4j/台帳/ES に部分的な残骸が残る」状態になりうる。
                # `DELETE /worlds`（`worlds.delete`/`worker._wipe_locked`）が使うのと同じ削除
                # 伝播（Neo4j 削除・台帳クリア・ES 削除）を、cleanup 専用に best-effort で
                # 補償的に実行する。各段を独立した try で包み、どれか1つが失敗しても他は必ず
                # 試みる。finally 内で例外を飛ばすと（Python の仕様で）呼び出し元へ伝播中の
                # 元例外（取り込み失敗の詳細）を丸ごと置き換えてしまうため、cleanup 自身の
                # 失敗はログのみに留め re-raise しない。派生ディレクトリ・registry 行の削除は
                # 最後に行う（DB 行削除が PG 障害等で失敗して止まっても、逆順だと到達できない
                # 手前の補償削除を先に済ませておく）。
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
                    # `ignore_errors=True` は削除失敗を無音で握り潰すため、契約どおり
                    # warning ログに残しつつ（re-raise しない＝他ファイルの削除は継続する）。
                    # ただし `FileNotFoundError`（派生ディレクトリがそもそも作られていない
                    # 早期失敗）は「削除すべきものが無かっただけ」の正常系なので警告しない
                    # ——実際の削除失敗（権限等）だけを警告に残す。
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
    """`worker._run_locked(..., finalize=False)` の保留分（`_pending_finalize`）を使って、
    受付 run の確定を一度だけ行う（`rebind` の新root試行/旧root復旧いずれの内部段でも書かず、
    最終結末が判明してからここでまとめて書く）。`status`/`extraction_snapshot` を渡すと保留分の
    その値だけ上書きする（内部段は成功していても利用者向けの結末は失敗、という場合に使う）。
    他フィールド（published_snapshot/source_doc_ids・world 確定用の sig 等）は保留分のまま
    温存し、NULL 上書きで Graph 件数等を消さない。"""
    from . import store, webhooks
    st = status if status is not None else pending.get("status", "failed")
    snap = extraction_snapshot if extraction_snapshot is not None else pending.get("extraction_snapshot")
    if pending.get("confirm_sig") is not None:
        rec = store.finish_ingest_run_and_confirm_world(
            run_id, world_id, status=st, extraction_snapshot=snap,
            published_snapshot=pending.get("published_snapshot"),
            source_doc_ids=pending.get("source_doc_ids"),
            sig=pending["confirm_sig"], manifest=pending.get("confirm_manifest"),
            doc_count=pending.get("confirm_doc_count"), scan_report=pending.get("confirm_scan_report"))
    else:
        rec = store.finish_ingest_run(
            run_id, status=st, extraction_snapshot=snap,
            published_snapshot=pending.get("published_snapshot"),
            source_doc_ids=pending.get("source_doc_ids"))
    # PART-6: rebind の内部多段（`finalize=False`）は terminal 化がここ1回だけ（モジュール
    # docstring 参照）なので、通知もここ1点に集約する（`worker._record` の hook は
    # `finalize=False` の間 `store.finish_ingest_run*` を呼ばないため到達しない＝自然に重複しない）。
    try:
        webhooks.notify_run_terminal(world_id, run_id, "rebind", st,
                                     doc_count=len(pending.get("source_doc_ids") or []) or None)
    except Exception:
        logging.getLogger(__name__).warning(
            "Webhook 通知の起動に失敗しました（rebind 自体は継続）: world_id=%s", world_id, exc_info=True)
    return rec


_REBIND_PENDING_FALLBACK = {
    "status": "failed", "extraction_snapshot": {}, "published_snapshot": None,
    "source_doc_ids": None, "confirm_sig": None, "confirm_manifest": None,
    "confirm_doc_count": None, "confirm_scan_report": None}


def _select_rebind_pending(recovery_pending: dict | None, attempt_pending: dict | None) -> dict:
    """rebind 失敗確定に使う保留分（`_pending_finalize`）を選ぶ——新root試行・旧root復旧の
    どちらが実際に Graph へ反映済みだったか（`published_snapshot is not None`）を最優先する。

    複合失敗ケース（新root試行が Neo4j load まで成功→PG replace で失敗、続く旧root復旧が
    それより早い段階〔office_md 等〕で力尽きる）では、復旧側の `published_snapshot` は
    `None`（反映未到達）のまま、Neo4j には**新root分が実際に反映済み**という食い違いが起きる。
    復旧を無条件優先すると、実在する Graph の件数を `None` で消してしまう（`get_latest_
    published_run_summary` が古い run の件数を返し続ける・status の graph_nodes/edges が
    実態と乖離する）。優先順位: ①復旧が反映済みならそれ ②復旧が反映未到達なら新root試行の方が
    反映済みならそちら ③どちらも反映済みでなければ詳細情報として復旧側（旧rootへ戻そうとした
    直接の顛末）④復旧の情報が無ければ新root試行側 ⑤どちらも無ければ最小限のフォールバック。
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
    """参照先パス変更＝**その world を全削除 → 新パスから再作成**（差分でなく破棄→再作成・他 world は無傷）。

    手順: バインドを新 root へ更新 → `worker._run_locked`（`load_world` が world_id 単位の delete+load を**1 tx**で置換）。
    取り込み失敗時は旧状態へ一貫復元（fail-closed・RV BLOCKER/High#2）: バインド・派生は旧へ戻し、
    **Neo4j は失敗段階に依存**する — Neo4j load 段階で失敗なら tx ロールバックで旧グラフが残る（台帳も未変更）が、
    R3-S1 以降は **Neo4j load 成功後に PG replace 段階で失敗しうる**（Neo4j＝新・台帳＝旧のまま tx ロールバック）ため、
    except 節で旧 root から即時再構築して Neo4j を旧へ戻す（失敗時は `last_sig` 無効化で次回 sync に self-heal を強制）。
    `label=None` は既存 label を保持。

    **world_lock は外側で1回だけ取得**（R3-S3）: `worker.run`（lock を自前で取る）を呼ぶと、session-level
    advisory lock は別コネクション再入不可のため自己デッドロックする。lock-free の `_run_locked` を直接呼ぶ。

    secRV round-3（HIGH-A・2026-07-14）: 署名（last_sig/manifest）の**確定**は**`_run_locked` 内部**
    （lock 保持中・かつ `_run_locked` 自身が呼び出し直前に取り直す新しいスキャン）に一任する。ただし
    **無効化**は例外が1つ: 復旧経路の `restore_bind_invalidate_sig` は bind 復元と同一 tx で last_sig を
    直接無効化する（確定はしない＝古い署名を有効化する方向の書き込みではないので ABA の穴にならない）。
    旧実装は外側（このフレーム）でも取り込み**前**にスキャンし、成功時・復旧時ともにその外側の署名で
    改めて `set_world_sig` を上書きしていた（当時のコメントは「`_run_locked` 側の確定は保持中の呼び出し
    なので無害」としていたが誤り）。world_lock は**このプロセス内の rebind/run/delete 同士**しか直列化
    せず、参照元フォルダ自体への外部変更（例: 別プロセス・利用者による書き換え）は防がない。そのため
    「外側スキャン(A) → 内側スキャン・確定(B) → 外側が(A)の古い署名で上書き」という順序が起き得て、
    その後にソースが(A)へ戻ると「グラフ=B・署名=A・ソース=A」で `sync` が unchanged と誤判定する
    **恒久的な不整合**になる（ABA 問題）。よって外側の事前/事後スキャンと `set_world_sig` 呼び出しは
    行わない（成功パス・復旧パスとも）。

    `run_id`（ING-3）＝呼び出し元が受付時に確保済みの `ingest_runs` 行。新root試行・
    （失敗時の）旧root復旧のどちらも `_run_locked` を `finalize=False` で呼ぶ——受付run自身の
    terminal 化はこの関数の最後で一度だけ行う（複数回 terminal 化すると、後段の書込が前段の
    `published_snapshot`/`source_doc_ids` を NULL/空で上書きし、Graph 件数 0 表示等の不整合に
    なる）。最終結末が rebind 失敗の場合、実際に採用した内部段（旧root復旧が成功していれば
    その結果・失敗/未実施なら新root試行の結果）の snapshot を温存したまま、status だけ
    `failed` へ・reason を `rebind_failed_rolled_back`（bind 復元＋旧root再構築の成功を
    確認できた時だけ）または `rebind_rollback_failed`（それ以外＝復旧自体が不完全）で
    上書きする。省略時（直接呼び出し・テスト用）は各 `_run_locked` 呼び出しがそれぞれ独立に
    新しい行を確保・terminal 化する旧来の動作のまま。`on_run_id`＝旧経路（後方互換）の
    コールバック。
    """
    from . import store
    from .ingest import worker
    from .store.worlds import rebind_bind_invalidate_sig   # 内部専用・facade に re-export しない
    with store.world_lock(world_id):
        old = store.get_world(world_id)
        if not old:
            raise ValueError(f"world '{world_id}' は未登録です（新規は register）")
        other = store.world_by_root(new_root)
        if other and other["world_id"] != world_id:
            raise WorldConflict(f"その参照元は既に world '{other['world_id']}' に登録済みです")
        # 新 root へのバインドと last_sig/last_doc_count 無効化を同一 tx で確定する（label None は
        # 既存保持）——`upsert_world()` 単体だと `_run_locked` 冒頭の全木スキャンが終わるまで
        # pre-invalidate されず、旧世代の件数・時刻が新 root に結び付いて見える窓ができる。
        # `storage_mode` も既存値を明示的に引き継ぐ（渡さないと既定 "external_reference" に
        # 化けてしまう）。
        rebind_bind_invalidate_sig(world_id, new_root, label=label,
                                   storage_mode=old.get("storage_mode"))
        import os as _os
        import shutil
        der = derived_dir(world_id)
        # 退避先は **`.` 始まり**＝`valid_world` が False＝自動リコンサイル(reconcile)の対象外（rebind 中に旧派生バックアップを孤児削除させない・RV High）
        backup = der.with_name("." + der.name + ".rebind-bak") if der.exists() else None
        # run_id 指定時（受付run）は新root試行・旧root復旧のどちらも非terminalな内部段として扱う。
        defer_finalize = run_id is not None
        attempt_pending = None
        res = None
        try:
            if backup is not None:                                   # 旧 root の派生は**消さず退避**（失敗時に復元・rv-full2 #1 high）
                # R3-S2（Codex finding #6）: 退避（os.replace）自体も try 内＝bind 更新後にここで失敗しても
                # rollback されず bind=新/derived=旧のまま放置されることはない（except の
                # restore_bind_invalidate_sig で必ず旧へ戻す・sig 無効化で次回 sync に self-heal を委ねる）。
                shutil.rmtree(backup, ignore_errors=True)           # 前回失敗の残骸を掃除してから
                _os.replace(der, backup)                            # 脇へ移す＝新 root の build はまっさらから（混入防止は維持）
            res = worker._run_locked(world_id, reflect=reflect,     # 新 root から build＋atomic 置換（lock-free 版）。
                                      created_by="admin", scan_root=None,  # 署名の確定は内部が行う（HIGH-A・ABA 対策）
                                      run_id=run_id, on_run_id=on_run_id, finalize=not defer_finalize,
                                      op="rebind")
            if defer_finalize:
                attempt_pending = res.get("_pending_finalize")
            if res["status"] == "failed":
                raise RuntimeError(f"rebind 失敗（取り込みエラー）: {res.get('flags')}")
        except Exception as e:                                      # 失敗＝旧状態へ復元。**復元経路は全て guard**し
            # 末尾の bare raise で**元例外を必ず伝播**する（RV HIGH: 復元側の二次例外で元例外を握り潰さない）。
            # R3-S1 で「Neo4j load 成功後に PG replace 段階で失敗して例外化」する経路ができたため、失敗が
            # Neo4j commit 後だと Neo4j＝新 root のまま残る。PG replace は単一 tx なので失敗時ロールバックで
            # 台帳は旧のまま無傷＝直すべきは Neo4j のみ。復元の順序と原子性が肝（RV round-2）:
            #  ① **bind を旧へ＋last_sig 無効化を同一 tx で先に確定**（`restore_bind_invalidate_sig`）。
            #     これで「bind=旧なら sig は必ず無効化済み」＝次回 sync が必ず self-heal する不変条件を保証。
            #     tx 失敗（PG 断）時はどちらも未適用＝bind=新のまま＝PG 復旧後の sync が新へ収束（整合）。
            #  ② 旧派生を復元（best-effort）。 ③ 旧 root から即時再構築で Neo4j を旧へ戻し sig を正しい値へ
            #     （best-effort・冪等）。失敗しても ① で sig は無効化済み＝次回 sync が self-heal。
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
            # RV HIGH round-3（2026-07-14）: 旧派生の復元と旧 root 再構築は **bind が旧へ戻った時だけ**行う。
            # bind 復元（＝sig 無効化と同一 tx）が失敗（PG 断）した場合は bind＝新 root のままで、その世界の
            # 正しい終状態は「新 root で整合」＝旧派生 A を戻すと新 root B に A の semantic 等が混入して逆に
            # 不整合になる（RV 指摘）。よって bind 未復元時は**何も足さず**、bind＝新・派生＝新のまま次回
            # sync が新 root へ収束するのに委ねる。
            recovery_pending = None
            recovery_ok = False
            if bind_restored:
                if backup is not None:
                    try:
                        shutil.rmtree(der, ignore_errors=True)     # 途中まで作った新派生を捨て
                        _os.replace(backup, der)                   # 旧派生を完全復元
                    except Exception:
                        pass
                try:
                    # 旧 root から即時再構築（Neo4j を旧へ戻す・best-effort）。署名の確定/無効化は
                    # `_run_locked` 内部が行う（HIGH-A: ここで外側スキャンを再確定に使うと ABA 恒久
                    # 不整合の穴になる＝呼び出しの副作用だけを使い、戻り値の sig 系は参照しない）。
                    res2 = worker._run_locked(world_id, reflect=reflect,
                                              created_by="admin", scan_root=None,
                                              run_id=run_id, on_run_id=on_run_id, finalize=not defer_finalize,
                                              op="rebind")
                    recovery_ok = res2["status"] != "failed"
                    if defer_finalize:
                        recovery_pending = res2.get("_pending_finalize")
                except Exception as e2:
                    if defer_finalize:
                        recovery_pending = getattr(e2, "_sherpa_ingest_run_pending", None)
            # 受付run自身の終端確定は復旧結果が判明した後にここで一度だけ行う。復旧（旧root再構築）が
            # 成功していればその snapshot を、そうでなければ新root試行の snapshot を温存し、
            # status だけ failed へ・reason で bind 復元＋旧root再構築の成否を区別する（利用者向けの
            # 意味は常に「rebind は失敗し旧状態へ戻した／戻せなかった」であり、`_run_locked` が
            # 内部的に書いた「成功」をそのまま見せると rebind が成功したかのように誤解させる）。
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
        if backup is not None:                                      # 成功＝退避した旧派生を破棄（新 root に混入させない・RV BLOCKER#2）
            shutil.rmtree(backup, ignore_errors=True)
        # 署名（last_sig/manifest）の確定は `_run_locked` 内部が既に行っている（HIGH-A）＝ここで改めて
        # 確定/上書きしない（外側の古い署名で上書きすると ABA 恒久不整合の穴になる）。
        if defer_finalize:
            res["run"] = _finalize_pending_run(run_id, world_id, res["_pending_finalize"])
        return res


def delete(world_id: str, reflect=True, run_id=None) -> bool:
    """world を完全削除（派生物 wipe ＋ レジストリ行削除）。参照元（外部フォルダ）は消さない。

    `_wipe_locked` は Neo4j 削除失敗時に例外を投げる（fail-closed）ので、**グラフ削除に成功した時だけ**行を削除する。

    **world_lock は外側で1回だけ取得**（R3-S3）: wipe とレジストリ行削除を同じロック区間に収め、
    同時実行の run/rebind と直列化する（`worker.wipe_world` を経由すると自己デッドロックするため
    lock-free の `_wipe_locked` を直接呼ぶ）。

    `run_id`（ING-3）＝呼び出し元が受付時に O(1) で確保済みの `ingest_runs` 行。指定時は
    world レジストリ行の DELETE と run 完了 UPDATE を**同一トランザクション**（
    `store.finish_ingest_run_and_confirm_world` ...ではなく専用の
    `store.finish_ingest_run_and_delete_world`）で確定する——「world 行は消えたが run はまだ
    'extracting' のまま」という中間状態を作らない。省略時（直接呼び出し・テスト用）は従来どおり
    `store.delete_world_row` を単独で呼ぶ。

    行削除に成功したら `preview_service` のグラフ view キャッシュも即座に破棄する——
    `last_sig` が空になる次回読み取りでも自然に無効化されるが、同じ world_id を直後に再登録して
    たまたま同じ内容（同じ sig）に確定した場合の取り違えを待たずに断つ（防御的二重化）。
    この破棄は `preview_service._GRAPH_VIEW_LOCK` を取ってから行う——素の pop だと、並行中の
    `graph_view()` 側が削除**前**に読んだ世代で構築を終えて `_GRAPH_VIEW_CACHE` へ書き込む瞬間と
    競合し、pop の**後**にその古い view が再挿入されて残ってしまう窓ができる（DB 行削除自体は
    この時点で既に確定済みなので、同じロックの下でどちらが先でも最終的に正しい状態になる——
    後から入る構築側は世代不一致で公開しない、後から入る pop 側は単に消す）。
    `build_preview` はこの**同じ** `_GRAPH_VIEW_CACHE` を共有する（RV1是正#4・2026-09-01・
    `preview_service._get_graph_bundle` 参照）ため、この破棄1本で両方に効く。
    """
    from . import store
    from .ingest import worker
    with store.world_lock(world_id):
        worker._wipe_locked(world_id, reflect=reflect)          # 失敗なら例外＝行も run の terminal 化もしない
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
        from . import reconcile                                 # 削除後に孤児派生物を自動掃除（ES delete が落ちていた等の取りこぼしを回収・不可視）
        reconcile.reconcile_derivatives(reflect=reflect)
    except Exception:
        pass
    return ok
