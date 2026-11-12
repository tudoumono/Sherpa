"""資料フォルダの中のフォルダ・ファイルに付ける「優先」「除外」（管理者の設定）。検索のときにパスの条件として効かせる。
保存先は `system_settings` の 1 キー（`path_rules`）で、`{資料フォルダ: [{"path", "kind", "outside_scope"}]}` を JSON で持つ。取り込みには焼き込まない。
- 優先（`priority`）: 選んだ範囲の中で先に読み、結果の上に出す。`outside_scope` を付けると、利用者が別の範囲を選んでいてもその場所を範囲に足す。
- 除外（`exclude`）: 利用者の範囲に関わらず、どの検索経路の結果・出典にも出さない（原本は触らない）。
- 親に付けた設定は配下すべてに効く。保存の時点で、配下に付けていた個別の設定は消える。
設計: docs/design/settings.md「優先と除外」・docs/design/rag.md「優先と除外」
"""
from __future__ import annotations

import errno
import functools
import hashlib
import json
import logging
import os
import stat
import posixpath
from pathlib import PurePosixPath
import re
import time
from dataclasses import dataclass

_log = logging.getLogger("sherpa")

SETTINGS_KEY = "path_rules"
ENV_KEY = "SHERPA_MCP_PATH_RULES"
KIND_PRIORITY = "priority"
KIND_EXCLUDE = "exclude"
KINDS = (KIND_PRIORITY, KIND_EXCLUDE)

NOTE_OUTSIDE_SCOPE = "選んだ範囲の外・管理者が優先にしている資料"
EXCLUDED_MESSAGE = "管理者が検索から外している資料です"
EXCLUDED_ERROR_CODE = "path_excluded"

# 除外されたファイルから作られる生成物の接尾。書く側（取り込み・公開の差し替え・PDF のページ画像・OCR の観測）と同じ定数
# （`derived_suffixes`）から作る。ここに名前を書かない。
from . import derived_suffixes  # noqa: E402

DERIVED_SUFFIXES = derived_suffixes.ALL
MAX_RULES_PER_WORLD = 500
# 子プロセスへ 1 つの環境変数で渡す写しの上限（Linux の環境変数 1 つの上限 128 KiB に、引数への埋め込みの余裕を見て半分）
MAX_SNAPSHOT_BYTES = 64 * 1024
UNAVAILABLE_MESSAGE = "除外の設定を読めないため検索できません"
UNAVAILABLE_ERROR_CODE = "path_rules_unavailable"
_MAX_PATH_LEN = 1024
_READ_FAIL_RETRY_S = 3.0


class PathRulesError(ValueError):
    """保存しようとした設定が不正（利用者向けの平文）。"""


class PathRulesUnavailable(RuntimeError):
    """設定を一度も読めておらず今も読めない。除外が効いているか分からないので、検索はこの例外で止める（fail-closed）。"""


@dataclass(frozen=True)
class Rule:
    path: str
    kind: str
    outside_scope: bool = False
    target_type: str = ""  # "file"／"folder"（保存のときに実物で確かめて持つ。無い保存値は読むときに実物で判定する）

    def as_dict(self) -> dict:
        return {"path": self.path, "kind": self.kind, "outside_scope": self.outside_scope,
                "target_type": self.target_type}


def normalize_path(raw) -> str:
    """資料フォルダからの相対パスに正規化する（前後の空白・`/` を除く）。絶対パス・`..`・`.`・空の部品・バックスラッシュ・NUL は `PathRulesError`。"""
    if not isinstance(raw, str):
        raise PathRulesError("場所の指定が不正です")
    s = raw.strip()
    if not s or s.startswith("/") or "\\" in s or "\x00" in s or len(s) > _MAX_PATH_LEN:
        raise PathRulesError("場所は資料フォルダからの相対で指定してください")
    s = s.strip("/")
    if not s or any(part in ("", ".", "..") for part in s.split("/")):
        raise PathRulesError("場所は資料フォルダからの相対で指定してください")
    return s


def _normalize_scope(scope_paths) -> list:
    """`scope.normalize_scope_paths` と同じ（strip・空除去・重複排除）。循環 import を避けて複製している。"""
    out: list = []
    for x in scope_paths or []:
        s = (x or "").strip().strip("/")
        if s and s not in out:
            out.append(s)
    return out


def _under(rel: str, paths) -> bool:
    rp = (rel or "").strip("/")
    if "//" in rp or "/./" in rp or rp.startswith(("./", "..")) or "/.." in rp or rp.endswith("/."):
        rp = posixpath.normpath(rp).lstrip("/")  # `A/./B` や `A/../B` で除外を迂回させない
    return any(rp == p or rp.startswith(p + "/") for p in paths)


def validate_entries(entries) -> list[Rule]:
    """保存する設定の検証（不正は `PathRulesError`）。`kind` は priority／exclude・`outside_scope` は優先だけ・同じ場所の二重指定は拒否。"""
    if not isinstance(entries, (list, tuple)):
        raise PathRulesError("設定の形が不正です")
    if len(entries) > MAX_RULES_PER_WORLD:
        raise PathRulesError(f"設定は {MAX_RULES_PER_WORLD} 件までです")
    out: list[Rule] = []
    seen: set = set()
    for e in entries:
        if not isinstance(e, dict):
            raise PathRulesError("設定の形が不正です")
        path = normalize_path(e.get("path"))
        kind = e.get("kind")
        if kind not in KINDS:
            raise PathRulesError("設定の種類は「優先」か「除外」です")
        outside = e.get("outside_scope", False)
        if not isinstance(outside, bool):
            raise PathRulesError("設定の形が不正です")
        if outside and kind != KIND_PRIORITY:
            raise PathRulesError("「範囲の外でも読む」は優先にだけ付けられます")
        if path in seen:
            raise PathRulesError("同じ場所に設定が重複しています")
        seen.add(path)
        ttype = e.get("target_type") or ""
        if ttype not in ("", "file", "folder"):
            raise PathRulesError("設定の形が不正です")
        out.append(Rule(path, kind, outside, ttype))
    return out


# Codex の permission profile に書く deny の件数の上限（`sandbox._exclude_deny_entries` の既定）と、規則 1 件が最悪で生む件数
DENY_MAX_ENTRIES = 2000
_DENY_ROOTS = 4          # 原本・取り込みの展開先・派生の md・派生の rag


def deny_entries_per_rule() -> int:
    """規則 1 件が最悪で生む deny の件数。各 root に 1 件＋派生の md／rag に、それぞれの層の接尾の数だけ（`derived_suffixes.BY_LAYER`）。"""
    return _DENY_ROOTS + len(derived_suffixes.BY_LAYER["md"]) + len(derived_suffixes.BY_LAYER["rag"])


def worst_case_deny_entries(items) -> int:
    """規則から作る deny の最悪の件数。種別は読むたびに実物で確かめ直すので、どの規則も後で file に変わり得るものとして数える。"""
    return deny_entries_per_rule() * sum(1 for r in items if r.kind == KIND_EXCLUDE)


def check_deny_budget(items) -> None:
    """Codex の deny の最悪の件数が上限を超えるなら `PathRulesError`。超えると直読の準備が失敗して全部が読めなくなるため、保存のときに断る。"""
    n = worst_case_deny_entries(items)
    if n > DENY_MAX_ENTRIES:
        per = deny_entries_per_rule()
        raise PathRulesError(f"除外が多すぎます（Codex の直読みの制限のため、除外は {DENY_MAX_ENTRIES // per} 件までです。"
                             f"今は {n // per} 件）。上のフォルダにまとめてください")


def check_snapshot_size(rules) -> None:
    """子プロセスへ渡す写し（JSON）が上限を超えるなら `PathRulesError`。保存のときに通す。"""
    n = len(json.dumps({"w": [r.as_dict() for r in rules]}, ensure_ascii=False).encode("utf-8"))
    if n > MAX_SNAPSHOT_BYTES:
        raise PathRulesError(f"設定が大きすぎます（{MAX_SNAPSHOT_BYTES // 1024} KiB まで）。フォルダ単位にまとめてください")


def collapse(rules) -> tuple[list[Rule], list[Rule]]:
    """親に設定がある場所の配下の設定を落とす。`(残す設定（path 昇順）, 落とした設定)` を返す。"""
    paths = [r.path for r in rules]
    kept, dropped = [], []
    for r in rules:
        parent = any(p != r.path and r.path.startswith(p + "/") for p in paths)
        (dropped if parent else kept).append(r)
    return sorted(kept, key=lambda r: r.path), dropped


def excluded_result() -> dict:
    """除外の資料を読もうとした道具の結果（理由を載せ、名前を挙げて聞かれたときに答えられるようにする）。"""
    return {"error": EXCLUDED_MESSAGE, "error_code": EXCLUDED_ERROR_CODE}


class Fixed(list):
    """管理者の優先を範囲へ足し込み済みの範囲（`Rules.extend_scope` は再び足さない）。`path_prefix` で狭めた範囲を下の検索が広げ直さないために使う。"""


class Rules:
    """1 つの資料フォルダの設定。判定はすべてここから行う。不変。"""

    def __init__(self, items=()):
        self.items = tuple(items)
        self._exclude = tuple(r.path for r in self.items if r.kind == KIND_EXCLUDE)
        self._priority = tuple(r.path for r in self.items if r.kind == KIND_PRIORITY)
        self._outside = tuple(r.path for r in self.items if r.kind == KIND_PRIORITY and r.outside_scope)
        # 派生物まで除外する対象: file と確かめられたもの＋種別が分からないもの（fail-closed＝派生物の側へ倒す）。folder と確かめられたものは含めない
        self._exclude_files = tuple(r.path for r in self.items if r.kind == KIND_EXCLUDE and r.target_type != "folder")

    @property
    def empty(self) -> bool:
        return not self.items

    @property
    def exclude_paths(self) -> tuple:
        return self._exclude

    def paging_signature(self) -> str:
        """結果の並びに効く規則（除外と「範囲の外でも読む」優先）を決まった形で並べて短くハッシュした印（続きの取得の `generation` 用）。無ければ空文字。"""
        if not self._exclude and not self._outside:
            return ""
        body = json.dumps([sorted(self._exclude), sorted(self._outside)], ensure_ascii=False)
        return hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]

    @property
    def file_exclude_paths(self) -> tuple:
        """除外のうち、派生物まで除外する対象（file と確かめられたもの・種別が分からないもの）。folder と確かめられたものは含めない。"""
        return self._exclude_files

    @property
    def priority_paths(self) -> tuple:
        return self._priority

    @property
    def outside_paths(self) -> tuple:
        return self._outside

    def excluded(self, rel) -> bool:
        return bool(self._exclude) and _under(rel, self._exclude)

    def derived_of_excluded(self, rel) -> bool:
        """除外された実ファイルから作られた生成物（`x.docx.rag.md`・`x.docx.assets/…` など）か。除外がフォルダ・拡張子の無い名前のとき、
        通常の資料の兄弟の名前（`A` を除外したときの `A.docs/x.md`）には当てない。"""
        rp = (rel or "").strip("/")
        for p in self._exclude_files:
            for sfx in DERIVED_SUFFIXES:
                if rp == p + sfx or rp.startswith(p + sfx + "/"):
                    return True
        return False

    def priority(self, rel) -> bool:
        return bool(self._priority) and _under(rel, self._priority)

    def extend_scope(self, scope_paths):
        """利用者の範囲に、「範囲の外でも読む」優先の場所を足す。範囲が空（資料フォルダ全体）か足すものが無ければ、渡された値をそのまま返す。"""
        if not self._outside or isinstance(scope_paths, Fixed):
            return scope_paths
        sel = _normalize_scope(scope_paths)
        if not sel:
            return scope_paths
        extra = [p for p in self._outside if not _under(p, sel)]
        return Fixed(sel + extra) if extra else scope_paths

    def fixed_scope(self, scope_paths):
        """`extend_scope` の結果を `Fixed` にする（以降の検索が足し直さない）。範囲が空なら空のまま返す。"""
        sp = self.extend_scope(scope_paths)
        return Fixed(sp) if sp else sp

    def outside_note(self, rel, scope_paths) -> str | None:
        """利用者が範囲を選んでいて、`rel` がその外にあり「範囲の外でも読む」の優先の下にあるときの出典の注記。"""
        if not self._outside:
            return None
        sel = _normalize_scope(scope_paths)
        if not sel or _under(rel, sel) or not _under(rel, self._outside):
            return None
        return NOTE_OUTSIDE_SCOPE

    def prefer(self, items, key=lambda h: h.get("doc_id")):
        """優先の下の項目を先頭へ寄せる（それぞれの中の並びは保つ）。優先が無ければ渡された列をそのまま返す。"""
        if not self._priority:
            return items
        first = [h for h in items if self.priority(key(h))]
        return first + [h for h in items if not self.priority(key(h))] if first else items

    def es_scope_clause(self, sel) -> dict:
        """ES の範囲フィルタ。`scopes` は祖先フォルダだけを持つため、「範囲の外でも読む」にファイルがあるときは `doc_id` でも拾う。"""
        base = {"terms": {"scopes": sel}}
        # 範囲の外の優先のファイルは、絞り込みの後の範囲（`sel`）に実際に残るものだけを足す（`path_prefix` で狭めた範囲を広げ直さない）
        files = [p for p in self._outside if _under(p, sel)]
        if not files:
            return base
        return {"bool": {"should": [base, {"terms": {"doc_id": files}}], "minimum_should_match": 1}}

    def es_must_not(self) -> list:
        if not self._exclude:
            return []
        return [{"terms": {"scopes": list(self._exclude)}}, {"terms": {"doc_id": list(self._exclude)}}]

    def es_should(self) -> list:
        if not self._priority:
            return []
        return [{"terms": {"scopes": list(self._priority)}}, {"terms": {"doc_id": list(self._priority)}}]


EMPTY = Rules()

_fail_until = 0.0


def _parse_items(raw) -> list[Rule]:
    """保存済みの値から設定を読む（壊れた行は読み飛ばす）。"""
    out: list[Rule] = []
    if not isinstance(raw, list):
        return out
    try:
        for r in validate_entries([e for e in raw if isinstance(e, dict)]):
            out.append(r)
    except PathRulesError:
        for e in raw:
            try:
                out.extend(validate_entries([e]))
            except PathRulesError:
                _log.warning("path_rules: 読めない設定を読み飛ばしました")
    return out


_last_good: dict | None = None  # このプロセスで最後に正常に読めた設定（読めない間はこれを使う）


_TYPE_CACHE: dict = {}


def detect_target_type(world: str, path: str) -> str:
    """`path` が資料フォルダの中で実際にファイルかフォルダかを調べる（"file"／"folder"。確かめられなければ ""）。
    原本のツリー、取り込みの展開先、取り込みの台帳の順に見る。拡張子の有無は種別の代わりに使わない。"""
    from . import doc_ledger, worlds
    parts = path.split("/")
    for base in (worlds.world_dir(world), worlds.archives_dir(world)):
        if not base:
            continue
        p = os.path.join(str(base), *parts)
        try:
            st = lstat_or_none(p)
        except OSError:
            return ""  # 確かめられない: 種別は空（file 側＝派生物も除外する側に倒れる）
        if st is None or stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_ISDIR(st.st_mode):
            return "folder"
        if stat.S_ISREG(st.st_mode):
            return "file"
    try:
        names = doc_ledger.ledger_rels(world) or []
    except Exception:  # noqa: BLE001
        names = []
    if path in names:
        return "file"
    if any(n.startswith(path + "/") for n in names):
        return "folder"
    return ""


_TYPE_TTL_S = 3.0  # 実物の種別を覚える長さ（設定の読み取りキャッシュと同じ）


def _resolve_types(world: str, items: list) -> list:
    """除外の規則の種別を、読むたびに今の実物で確かめ直す（保存値は使わない）。除外したフォルダが同じパスのファイルに置き換わっても、
    派生物（`x.docx.rag.md` など）を除外する側が外れない。実物が判定できないときは、種別を空にして file 側（派生物も除外）へ倒す。
    実物の判定は短い間だけ覚える（規則は最大 500 件・判定できなかった結果は覚えず次に再判定する）。"""
    now = time.monotonic()
    out = []
    for r in items:
        if r.kind != KIND_EXCLUDE:
            out.append(r)
            continue
        key = (world, r.path)
        hit = _TYPE_CACHE.get(key)
        if hit and now - hit[0] < _TYPE_TTL_S:
            t = hit[1]  # 覚えた判定を使うだけ（時刻は更新しない＝実物を確かめた時刻から数える）
        else:
            t = detect_target_type(world, r.path)
            if t:
                _TYPE_CACHE[key] = (now, t)  # 実物を確かめたときだけ時刻を入れる
            else:
                _TYPE_CACHE.pop(key, None)
        out.append(r if r.target_type == t else Rule(r.path, r.kind, r.outside_scope, t))
    return out


def with_target_types(world: str, items) -> list:
    """保存のときに、各規則の対象の種別を実物で確かめて持たせる（クライアントの申告は使わない）。"""
    return [Rule(r.path, r.kind, r.outside_scope, detect_target_type(world, r.path)) for r in items]


def _read_db() -> dict:
    from . import store
    raw = store.get_system_settings().get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {w: _resolve_types(w, _parse_items(v)) for w, v in raw.items() if isinstance(w, str)}


def _load_all() -> dict:
    """`{資料フォルダ: [Rule]}`。調べものの道具の子プロセス（`SHERPA_MCP_WORLD` あり）は DB を読まず、親が渡した env の写しだけを見る。
    守ること: DB を読めないときは最後に読めた写しを使う。一度も読めていないなら `PathRulesUnavailable`（除外が全解除になる fail-open にしない）。
    """
    global _fail_until, _last_good
    raw = os.environ.get(ENV_KEY)
    if raw is None and os.environ.get("SHERPA_MCP_WORLD") is not None:
        return {}
    if raw is not None:
        try:
            raw = json.loads(raw) if raw else None
        except ValueError:
            _log.warning("path_rules: 親から渡された設定が壊れています")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {w: _parse_items(v) for w, v in raw.items() if isinstance(w, str)}
    if not (_fail_until and time.monotonic() < _fail_until):
        try:
            _last_good = _read_db()
            _fail_until = 0.0
            return _last_good
        except Exception as e:  # noqa: BLE001
            _fail_until = time.monotonic() + _READ_FAIL_RETRY_S
            _log.warning("path_rules: 設定を読めません（%s）: %s", "最後に読めた写しを使います" if _last_good is not None
                         else "検索を止めます", type(e).__name__)
    if _last_good is None:
        raise PathRulesUnavailable(UNAVAILABLE_MESSAGE)
    # 読めないときも、種別は今の実物で確かめ直す（保存値・読めた時点の種別を使い続けない）
    return {w: _resolve_types(w, items) for w, items in _last_good.items()}


def rules_for(world: str) -> Rules:
    items = _load_all().get(world)
    return Rules(items) if items else EMPTY


def snapshot_env(world: str, rules: "Rules | None" = None) -> dict:
    """調べものの道具の子プロセスへ渡す env（設定があるときだけ）。`rules` を渡すと、読み直さずにその規則を写す（1 回の依頼の中で同じスナップショットを使う）。"""
    items = rules.items if rules is not None else _load_all().get(world)
    if not items:
        return {}
    return {ENV_KEY: json.dumps({world: [r.as_dict() for r in items]}, ensure_ascii=False)}


def lstat_or_none(path):
    """`os.lstat` を、例外を隠さずに呼ぶ。ENOENT・ENOTDIR だけを「無い」（None）とし、それ以外の `OSError` は送出する。
    除外に関わる実在・種別・symlink の確認は、`Path.exists`／`is_file`／`is_dir`／`is_symlink`・`os.path.*` ではなくこれを使う（それらは I/O エラーを「無い」に丸める）。"""
    try:
        return os.lstat(path)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise


class ExistenceUnknown(OSError):
    """実在を確かめられなかった（権限・入出力の失敗など）。「無い」とは区別する。"""


_GONE_ERRNOS = (errno.ENOENT, errno.ENOTDIR)


def _exists(world: str, path: str) -> bool:
    """原本と取り込みの展開先の今の実在だけを見る（取り込みの台帳の旧い行は見ない＝消した直後に画面を開いても、消えた場所の設定が残らない）。
    三値: あれば True、無いと確かめられた（ENOENT・ENOTDIR）ときだけ False。それ以外の `OSError` は `ExistenceUnknown`（無いとはみなさない）。"""
    from . import worlds
    for base in (worlds.world_dir(world), worlds.archives_dir(world)):
        if not base:
            continue
        try:
            if lstat_or_none(os.path.join(str(base), *path.split("/"))) is not None:
                return True
        except OSError as e:
            raise ExistenceUnknown(e.errno, f"実在を確かめられません: {type(e).__name__}") from e
    return False


def prune_missing(world: str, items) -> tuple[list[Rule], list[Rule]]:
    """もう存在しない場所の設定を分ける。`(残す, 取り除く)`。資料フォルダの根を確かめられないときは何も取り除かない。
    実在を確かめられない場所（権限・入出力の失敗）があれば `ExistenceUnknown` を送出する（取り除かない・保存しない）。"""
    from . import worlds
    wd = worlds.world_dir(world)
    if not wd:
        return list(items), []
    try:
        st = lstat_or_none(str(wd))
        root_ok = st is not None and (stat.S_ISDIR(st.st_mode) or (stat.S_ISLNK(st.st_mode) and os.path.isdir(str(wd))))
    except OSError as e:
        raise ExistenceUnknown(e.errno, f"資料フォルダの根を確かめられません: {type(e).__name__}") from e
    if not root_ok:
        return list(items), []
    kept, gone = [], []
    for r in items:
        (kept if _exists(world, r.path) else gone).append(r)
    return kept, gone


def prune_world(uid, world: str) -> tuple[list[Rule], list[Rule]]:
    """存在しない場所の設定を取り除いて保存する。最新の値の読み直し・取り除き・書き込みを `store.modify_system_setting` の同じトランザクションで行い、
    同時の保存（PUT）の変更を旧い値で上書きしない。`(残す, 取り除いた)` を返す。取り除くものが無ければ書かない。"""
    from . import store
    box: dict = {"kept": [], "gone": []}

    def _mutate(cur):
        allv = dict(cur) if isinstance(cur, dict) else {}
        kept, gone = prune_missing(world, _resolve_types(world, _parse_items(allv.get(world))))
        box["kept"], box["gone"] = kept, gone
        if not gone:
            return cur
        if kept:
            allv[world] = [r.as_dict() for r in kept]
        else:
            allv.pop(world, None)
        return allv or None

    store.modify_system_setting(uid, SETTINGS_KEY, _mutate)
    if box["gone"]:
        global _last_good, _fail_until
        _last_good, _fail_until = None, 0.0
    return box["kept"], box["gone"]


def save_world(uid, world: str, items) -> list[Rule]:
    """資料フォルダ 1 つぶんの設定を保存する。読み取り・資料フォルダ単位のマージ・書き込みを 1 つのトランザクションで行い
    （`store.modify_system_setting`）、同時の保存で別の資料フォルダの設定を消さない。空なら資料フォルダの欄を消す。残す設定を返す。"""
    from . import store

    def _merge(cur):
        allv = dict(cur) if isinstance(cur, dict) else {}
        if items:
            allv[world] = [r.as_dict() for r in items]
        else:
            allv.pop(world, None)
        return allv or None

    store.modify_system_setting(uid, SETTINGS_KEY, _merge)
    global _last_good, _fail_until
    _last_good, _fail_until = None, 0.0
    for k in [k for k in _TYPE_CACHE if k[0] == world]:
        _TYPE_CACHE.pop(k, None)
    return list(items)


# ---- 結果の組み立ての最後に通す、除外の資料の落とし込み（影響調査・原因候補・未解決の参照） ----

def hidden_doc(rules, doc) -> bool:
    return isinstance(doc, str) and rules.excluded(doc)


def source_hidden(rules, src) -> bool:
    if not isinstance(src, dict):
        return False
    fd = src.get("from_def")
    return (hidden_doc(rules, src.get("doc_id")) or hidden_doc(rules, src.get("file"))
            or (isinstance(fd, dict) and hidden_doc(rules, fd.get("file"))))


def _scrub_edges(rules, edges):
    """辺の列から、参照元の資料が除外の辺を落とし、残る辺の根拠からも除外の資料を落とす。"""
    out = []
    for e in edges or []:
        if not isinstance(e, dict):
            out.append(e)
            continue
        if hidden_doc(rules, e.get("doc")):
            continue
        if isinstance(e.get("sources"), list):
            e = {**e, "sources": [x for x in e["sources"] if not source_hidden(rules, x)]}
        out.append(e)
    return out


def scrub_unresolved(unresolved, rules):
    """未解決の参照（`{available, items, omitted}`）から、除外の資料が書いた参照を落とす（落とした分は数えない）。"""
    if rules.empty or not isinstance(unresolved, dict) or not rules.exclude_paths:
        return unresolved
    items = [i for i in unresolved.get("items") or []
             if not (isinstance(i, dict) and (hidden_doc(rules, i.get("path")) or source_hidden(rules, i)))]
    out = {**unresolved, "items": items}
    if isinstance(out.get("omitted"), int) and out["omitted"] > 0:
        out["omitted"] = None  # 省略された分に除外の資料の参照が混ざっているか分からない＝件数で存在を示さない
    return out


def _item_hidden(rules, it) -> bool:
    if not isinstance(it, dict):
        return False
    if hidden_doc(rules, it.get("path")) or any(hidden_doc(rules, p) for p in it.get("trace_paths") or []):
        return True
    return False


def scrub_impact(result, rules):
    """影響調査の結果（`run_world_impact`＋推定）から、除外の資料に由来する起点・影響・候補・推定・未解決の参照を落とす。
    起点がすべて除外の資料なら、影響・候補・推定は空にする（存在を示さない）。設定が無ければ渡された結果をそのまま返す。"""
    if rules.empty or not rules.exclude_paths or not isinstance(result, dict):
        return result
    out = dict(result)
    starts = result.get("starts")
    if isinstance(starts, list):
        out["starts"] = [x for x in starts if not _item_hidden(rules, x)]
        if starts and not out["starts"]:
            for k in ("items", "candidates", "presumed"):
                if k in out:
                    out[k] = []
            if "unresolved" in out:
                out["unresolved"] = {"available": False, "items": [], "omitted": 0}
            return out
    for k in ("items", "candidates", "presumed"):
        if k not in out:
            continue
        kept = []
        for it in out[k] or []:
            if _item_hidden(rules, it):
                continue
            if isinstance(it, dict) and isinstance(it.get("evidence"), list) and it["evidence"]:
                ev = _scrub_edges(rules, it["evidence"])
                if not ev:
                    continue  # 根拠がすべて除外の資料＝存在を示さない
                it = {**it, "evidence": ev}
            kept.append(it)
        out[k] = kept
    if "unresolved" in out:
        out["unresolved"] = scrub_unresolved(out["unresolved"], rules)
    return out


# ---- 出口の一括の濾過（外へ出る境界で通す最後の網） ----
#
# 決定: 除外は「検索と回答から外す」まで。あることを隠す網ではないので、本文の文字列は一切消さず、見た目の推測もしない。
# 構造で決める: 結果の要素（dict）が資料を指す欄（`_PATH_KEYS`＝doc_id・path・file・doc・rel_path など）の値で除外の場所
# （またはその配下・派生名 `x.rag.md`・グラフの識別子 `種類:資料フォルダ:パス#名前` の中のパス）に当たるときだけ、その要素を落とす。
# パスの列を持つ欄（`_PATH_LIST_KEYS`＝paths・trace_paths・files など）は、当たった文字列だけを落とす。
# 落としたときだけ、その dict の数の欄（count・total・omitted・overflow などを名前に含む欄）を直す:
# 直前の list の長さと同じだった `count`／`total` は見える数に数え直し、それ以外（省略・超過）は null にする。
# 設定が無ければ入力をそのまま返す（同一のオブジェクト）。

# 資料のパスを値に持つ欄だけ。分類の語が入り得る欄（`source`＝es／graph／grep など）や名前の欄（`name`）・関係の語（`rel`）は入れない。
# パスを持つ構造（出典の中の `doc_id`・`file`、`from_def.file`）は、その構造の中の欄で判定する。
_PATH_KEYS = frozenset({"doc_id", "path", "file", "doc", "rel_path", "source_rel_path", "origin_doc",
                        "cid", "canonical_id", "source_path", "source_doc_id", "left_doc_id", "right_doc_id",
                        "original_path", "_target_path"})
_PATH_LIST_KEYS = frozenset({"paths", "trace_paths", "files", "membership_paths", "doc_ids", "trace_cids",
                             "matched_doc_ids", "sources_verified", "source_doc_ids", "candidate_paths",
                             "plan_only_paths", "binding_paths", "_verified_doc_ids"})
# 派生物（md／rag／ir の物理パス）を指す欄。派生名（`x.rag.md`・`x.pages/…`）の判定は、この欄と派生の置き場の物理パスだけに当てる。
# 論理の原本のパスの欄（`_PATH_KEYS`）には、`excluded()` だけを当てる（原本 `A` を除外しても、別の原本 `A.md` を見える資料のまま残す）。
_DERIVED_PATH_KEYS = frozenset({"md_path", "rag_path", "ir_path", "derived_path", "assets_dir", "pages_dir", "rag_md_path"})
_DERIVED_PATH_LIST_KEYS = frozenset({"derived_paths", "md_paths", "rag_paths"})
_REF_LIST_KEYS = frozenset({"evidence_refs"})  # `パス:行` 形の参照の列（主張の根拠）
_REF_LINE_RE = re.compile(r":\d+(?:-\d+)?$")
_COUNT_KEY_RE = re.compile(r"(count|total|omitted|overflow|more|remaining)", re.I)
_RECOUNT_KEYS = ("count", "total")
_CID_RE = re.compile(r"^[A-Za-z_]+:([^:]+):(.+?)(?:#.*)?$", re.S)
_MISSING = object()


def _path_value_hit(rules, s, world: str | None, derived: bool = False) -> bool:
    if not isinstance(s, str) or not s or len(s) > 1024:
        return False
    cands = [s]
    m = _CID_RE.match(s)
    if m and (world is None or m.group(1) == world):
        cands.append(m.group(2))  # グラフの識別子（種類:資料フォルダ:パス#名前）の中のパス
    for v in cands:
        rp = v.strip("/")
        if rules.excluded(rp):
            return True
        if derived and rules.derived_of_excluded(rp):  # 派生物を指す値だけ: 除外されたファイルの生成物（`x.rag.md`・`x.pages/…`）
            return True
    return False


def _element_hit(rules, d: dict, world) -> bool:
    for k, v in d.items():
        if k in _PATH_KEYS and _path_value_hit(rules, v, world):
            return True
        if k in _DERIVED_PATH_KEYS and _path_value_hit(rules, v, world, derived=True):
            return True
        if k == "from_def" and isinstance(v, dict) and _path_value_hit(rules, v.get("file"), world):
            return True
    return False


def _filter(obj, rules, world, top=False, key=None):
    """`(濾過後, 変えたか)`。落とす要素自体は `_MISSING` で返す。"""
    if isinstance(obj, list):
        out, changed = [], False
        for x in obj:
            if key in _PATH_LIST_KEYS and _path_value_hit(rules, x, world):
                changed = True
                continue
            if key in _DERIVED_PATH_LIST_KEYS and _path_value_hit(rules, x, world, derived=True):
                changed = True
                continue
            if key in _REF_LIST_KEYS and isinstance(x, str) and _path_value_hit(rules, _REF_LINE_RE.sub("", x), world):
                changed = True
                continue
            y, c = _filter(x, rules, world)
            if y is _MISSING:
                changed = True
                continue
            changed |= c
            out.append(y)
        return (out if changed else obj), changed
    if isinstance(obj, dict):
        if not top and _element_hit(rules, obj, world):
            return _MISSING, True
        out, changed, shrunk = {}, False, {}
        for k, v in obj.items():
            y, c = _filter(v, rules, world, key=k if isinstance(k, str) else None)
            if y is _MISSING:
                changed = True
                continue
            if c:
                changed = True
                if isinstance(v, list) and isinstance(y, list):
                    shrunk[k] = len(v)
            out[k] = y
        if not changed:
            return obj, False
        if shrunk:
            old_lens = set(shrunk.values())
            for k, v in list(out.items()):
                if isinstance(v, bool) or not (isinstance(v, int) or v is None):
                    continue
                if isinstance(k, str) and _COUNT_KEY_RE.search(k):
                    if k in _RECOUNT_KEYS and v in old_lens and len(shrunk) == 1:
                        out[k] = len(out[next(iter(shrunk))])
                    else:
                        out[k] = None
        return out, True
    return obj, False


def filter_output(obj, world: str):
    """結果の JSON（dict／list）から、資料を指す欄が除外の場所に当たる要素を落とす（外へ出る境界の最後の網）。本文の文字列は消さない。
    設定が無ければ入力をそのまま返す。読めないときは `PathRulesUnavailable`。"""
    rules = rules_for(world)
    if not rules.exclude_paths:
        return obj
    out, _ = _filter(obj, rules, world, top=True)
    return obj if out is _MISSING else out


def filtered_tool(fn):
    """道具の実行 `fn(name, args, world, ...)`（戻りは `(結果, 触れた資料, 引用候補, 候補カード)`）を包む。除外の設定を読めないときは実行せず止め（fail-closed）、
    外へ出る境界の最後の網として、結果・触れた資料・引用候補・候補カードから除外の資料を落とす（設定が無ければ何も変えない）。"""
    @functools.wraps(fn)
    def wrapper(name, args, world, *a, **kw):
        try:
            rules = rules_for(world)
        except PathRulesUnavailable:
            return {"error": UNAVAILABLE_MESSAGE, "error_code": UNAVAILABLE_ERROR_CODE}, set(), [], []
        result, docs, cites, cards = fn(name, args, world, *a, **kw)
        if not rules.exclude_paths:
            return result, docs, cites, cards
        return (recount_returned_docs(filter_output(result, world)), {d for d in docs if not rules.excluded(d)},
                filter_output(cites, world), filter_output(cards, world))
    return wrapper


def recount_returned_docs(result):
    """`returned_docs` を持つ結果は、残った `hits` の一意の doc_id の数へ数え直して返す（取得時点の `fetched_chunks`・`fetch_cap` は変えない）。
    AI へ返す出口（`filtered_tool` の除外の濾過の後・MCP の最終のクリップの後）で必ず通す。"""
    if isinstance(result, dict) and "returned_docs" in result and isinstance(result.get("hits"), list):
        result["returned_docs"] = len({h["doc_id"] for h in result["hits"] if isinstance(h, dict) and h.get("doc_id")})
    return result
