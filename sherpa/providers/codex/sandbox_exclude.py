"""Codex の permission profile へ、管理者の除外（`path_rules`）を deny として効かせる部品（`sandbox` が使う。名前はそちらからも引ける）。
設計: docs/design/rag.md「優先と除外」
"""
from __future__ import annotations

import errno
import stat
from pathlib import Path

from ... import derived_suffixes, path_rules


def _derived_read_roots(world: str, strict: bool = False) -> dict:
    """`_direct_read_roots` のうち、派生の md／rag の置き場の絶対パス→層（除外されたファイルの生成物がある場所）。
    `strict`（除外の設定があるとき）は、置き場の確認を例外を隠さない `lstat` で行い、ENOENT・ENOTDIR 以外の失敗は `RuntimeError`（直読み全体を止める）。
    そうしないと、read に採った置き場の接尾の列挙を飛ばして、除外のファイルの派生物を読めてしまう。"""
    from ... import worlds

    out = {}
    for fn, layer in ((worlds.derived_md_dir, "md"), (worlds.derived_rag_dir, "rag")):
        try:
            d = fn(world)
            if strict:
                try:
                    present = path_rules.lstat_or_none(d) is not None
                except OSError as e:
                    raise RuntimeError(f"derived_roots_failed:{type(e).__name__}") from e
            else:
                present = d.exists()
            if present:
                out[str(d.resolve())] = layer
        except OSError:
            if strict:
                raise RuntimeError("derived_roots_failed:resolve")
            continue
    return out


def _exclude_deny_entries(roots: list, rules, scope_deny=(), *, max_entries: int = path_rules.DENY_MAX_ENTRIES,
                          derived_roots=()) -> list:
    """管理者の除外（`path_rules`）を permission profile で効かせるための deny 一覧（絶対パス）。
    各 root（原本・派生の md／rag・展開先）で、除外の場所そのもの（フォルダ・ファイル）のうち実在するものを deny にする。除外の対象がファイルと確かめられているときだけ、`derived_roots`（派生の md／rag）で、その層の `<名前><生成物の接尾>`（`derived_suffixes.BY_LAYER`）も deny にする。symlink は書かない（bubblewrap が起動に失敗する）。
    範囲の deny（`scope_deny`）の配下は、親の deny で読めないため重ねて書かない（子の deny は親が見えない場所を指して起動に失敗するため）。
    件数が `max_entries` を超える・走査中の OSError は `RuntimeError`（fail-closed＝直読を許可しない）。設定が無ければ空リスト。
    """
    paths = list(getattr(rules, "exclude_paths", ()) or ())
    if not paths:
        return []
    denied = [Path(d) for d in scope_deny]
    # 派生の置き場ごとの接尾（`derived_roots` が {置き場: 層} なら層の分だけ。列なら md・rag の両方の層）
    layers = derived_roots if isinstance(derived_roots, dict) else {r: None for r in derived_roots}
    derived = {str(r): (derived_suffixes.BY_LAYER[layer] if layer else derived_suffixes.BY_LAYER["md"] + derived_suffixes.BY_LAYER["rag"])
               for r, layer in layers.items()}
    out: list = []

    def _add(cand: Path) -> None:
        try:
            st = path_rules.lstat_or_none(cand)  # 例外を隠さない確認（ENOENT・ENOTDIR だけが「無い」）
            if st is None:
                return
            if stat.S_ISLNK(st.st_mode):
                # 除外の対象（や派生物）が symlink に置き換わった: deny に書けず、リンク先を除外のパス経由で読めてしまうので、直読み全体を止める（fail-closed）
                raise RuntimeError("exclude_enum_failed:symlink")
            if cand.resolve() != cand:
                raise RuntimeError("exclude_enum_failed:symlink")  # 途中の階層が symlink（別名経由）
        except RuntimeError:
            raise
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ENOTDIR):
                return  # 無いと確かめられた
            # 確かめられない（権限・入出力の失敗）: その対象を deny せずに直読みを許さない（fail-closed）
            raise RuntimeError(f"exclude_enum_failed:{type(e).__name__}") from e
        if any(d == cand or d in cand.parents for d in denied):
            return
        if str(cand) not in out:
            out.append(str(cand))
        if len(out) > max_entries:
            raise RuntimeError("exclude_enum_failed:max_entries_exceeded")

    for root_s in roots:
        try:
            root_r = Path(root_s).resolve()
        except OSError as e:
            raise RuntimeError(f"exclude_enum_failed:{type(e).__name__}")
        for rel in paths:
            target = root_r.joinpath(*rel.split("/"))
            _add(target)  # 原本（または派生の同じ場所）
            # 除外がファイルのとき、派生の置き場（md／rag）にある生成物（`<名前><接尾>`）も deny にする。フォルダ・拡張子の無い名前には当てない
            if root_s in derived and rel in rules.file_exclude_paths:
                for sfx in derived[root_s]:
                    _add(target.parent / (target.name + sfx))
    kept = [p for p in out if not any(Path(q) in Path(p).parents for q in out)]  # 親の deny があれば子は書かない
    return sorted(kept)


def turn_deny(world: str, base_roots: list, scope_paths, venv, rules) -> list:
    """1 回の依頼の準備で、直読みで deny にする絶対パスの一覧（範囲の外・管理者の除外・秘匿ファイル）を作る。列挙できなければ `RuntimeError`（直読みを許可しない）。
    除外の設定がある資料フォルダでは、最終の整形（permission profile を書くときの `_prune_deny_paths`）で確かめられずに落ちる deny が無いことも、ここで先に確かめる。"""
    from . import sandbox  # 遅延 import（循環回避）
    scope_deny = sandbox._scope_deny_entries(base_roots, rules.extend_scope(scope_paths))
    if base_roots and all(r in scope_deny for r in base_roots):
        raise RuntimeError("scope_enum_failed:no_scope_in_roots")  # 範囲がどの root にも無い
    scope_deny = scope_deny + _exclude_deny_entries(
        base_roots, rules, scope_deny, derived_roots=_derived_read_roots(world, strict=bool(rules.exclude_paths)))
    roots = base_roots + ([str(venv)] if venv else [])
    deny = scope_deny + sandbox._enumerate_sensitive(roots)
    if rules.exclude_paths:
        sandbox._prune_deny_paths(deny, roots, strict=True)
    return deny


def strict_for(world: str, rules=None) -> bool:
    """permission profile の deny を strict に整えるか（除外の設定がある資料フォルダ。`rules` は 1 回の依頼で読んだもの・読めないときは strict）。"""
    try:
        return bool((rules if rules is not None else path_rules.rules_for(world)).exclude_paths)
    except path_rules.PathRulesUnavailable:
        return True
