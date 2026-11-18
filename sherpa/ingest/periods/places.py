"""期の置き場の見分け方（変更一覧の名前・置き方と名前）とフォルダの木。"""
from __future__ import annotations

import bisect
from collections import defaultdict

from .names import ordering, period_number


def _walk_key(rel: str) -> tuple:
    parts = rel.split("/")
    return tuple((1, p) for p in parts[:-1]) + ((0, parts[-1]),)


class FolderTree:
    """相対パスの一覧から作るフォルダの木。空のフォルダは見えない。files は「フォルダのファイル → 子フォルダ」の順。"""

    def __init__(self, rels):
        self.files = sorted(set(rels), key=_walk_key)
        dirs: set = set()
        for rel in self.files:
            parts = rel.split("/")
            dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
        self.dirs = dirs
        self.kids: dict = defaultdict(list)
        for d in sorted(dirs):
            self.kids[d.rsplit("/", 1)[0] if "/" in d else ""].append(d.rsplit("/", 1)[-1])
        self.kids = dict(self.kids)
        self._sorted = sorted(self.files)

    def children(self, path: str) -> list:
        return self.kids.get(path, [])

    def tails_by_child(self, d: str, kids) -> dict:
        """d の子のフォルダ kids の下のファイルを、子を除いた相対パスごとに集める（相対パス → 子の名前の集合）。"""
        prefix, kidset, seen = d + "/", set(kids), {}
        for k in range(bisect.bisect_left(self._sorted, prefix), len(self._sorted)):
            rel = self._sorted[k]
            if not rel.startswith(prefix):
                break
            child, _, tail = rel[len(prefix):].partition("/")
            if tail and child in kidset:
                seen.setdefault(tail, set()).add(child)
        return seen


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def recorded_containers(tree: FolderTree, period_names: set) -> set:
    """変更一覧に出る期・基準の名前のフォルダの親（記録から見分けた置き場）。"""
    return {parent_of(d) for d in tree.dirs if d.rsplit("/", 1)[-1] in period_names}


def layout_containers(tree: FolderTree, recorded: set) -> dict:
    """置き方と名前から見分けた置き場（パス → 子の名前）。子が 2 つ以上・子の名前の全部から重ならない番号が取れ、
    子どうしで期を除いた相対パスが同じファイルが 1 組以上あるフォルダを、浅い方から探す。期のフォルダの下は探さない。"""
    accepted, found = set(recorded), {}
    for d in sorted(tree.dirs, key=lambda x: (x.count("/"), x)):
        parts = d.split("/")
        if any("/".join(parts[:k - 1]) in accepted for k in range(1, len(parts) + 1)):
            continue
        kids = tree.children(d)
        if ordering(kids) is None:
            continue
        if any(len(v) >= 2 for v in tree.tails_by_child(d, kids).values()):
            accepted.add(d)
            found[d] = kids
    return found


class Placement:
    """どのフォルダが期の置き場かを決めたあとの、パスの読み方（期のフォルダの位置・期を除いたパス）。"""

    def __init__(self, containers):
        self.containers = frozenset(containers)
        self._cache: dict = {}

    def period_of(self, rel: str):
        """パスの中の期のフォルダ名と、その位置。期のフォルダが無ければ (None, -1)。最も外側の置き場の子だけが期になる。"""
        if rel not in self._cache:
            parts = rel.split("/")[:-1]
            out = (None, -1)
            for i, comp in enumerate(parts):
                if "/".join(parts[:i]) in self.containers:
                    out = (comp, i)
                    break
            self._cache[rel] = out
        return self._cache[rel]

    def rel_path(self, rel: str) -> str:
        """期のフォルダを除いた相対パス。期のフォルダが無ければそのまま。"""
        p, i = self.period_of(rel)
        parts = rel.split("/")
        return "/".join(parts[:i] + parts[i + 1:]) if p is not None else rel

    def outer_of(self, path: str):
        """置き場 path が、別の置き場の期のフォルダの下にあるなら (外側の置き場, その期のフォルダ名)。"""
        parts = path.split("/")
        for k in range(len(parts)):
            if "/".join(parts[:k]) in self.containers:
                return "/".join(parts[:k]), parts[k]
        return None

    def split_files(self, files):
        """期のあるファイルを 置き場 → 期 → ファイル名 → [期を除いた相対パス] に、期の無いファイルを別に集める。"""
        by_period: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        outside = []
        for rel in files:
            parts = rel.split("/")
            period, i = self.period_of(rel)
            if period is None:
                outside.append(rel)
            else:
                by_period["/".join(parts[:i])][period][parts[-1]].append("/".join(parts[i + 1:]))
        return by_period, outside
