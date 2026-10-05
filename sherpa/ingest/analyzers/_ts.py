"""Tree-sitter による読み取りの共通部品（提案書 2026-10-04 アナライザとグラフの改善・段階 2a の土台）。

各言語アナライザは「読む」部分だけをここ経由で Tree-sitter に置き換える。契約（`DefResult`・`RefResult`・`Dropped`）と
共通層の名前解決は変えない。

規則:
- 行番号は 1 始まり。行は `\\n` だけで区切る（`_base` の行数えと同じ）。`\\r\\n` の `\\r` は行末の文字として残る。
- ソースは UTF-8 の bytes にして解析する。ノードの位置は バイトのオフセットで返るので、テキストは `Parsed.text(node)` で取り出す
  （不正なバイトは置換して読む）。改行の正規化・タブ展開はしない。
- 文法のパッケージが無い・本体と文法の ABI が合わないときは `TreeSitterUnavailable` で失敗する
  （旧アナライザへ黙って倒さない）。取り込みの入口（`world_graph.build_world`）が最初に `require()` を呼び、無ければその取り込みを
  失敗させる。アナライザのモジュールの import では要求しない（`sherpa` を読むだけのスクリプト〔本番の事前検査など〕を依存に縛らない）。
- 構文エラー（`ERROR`・`MISSING` ノード）は `syntax_errors()` が `Dropped("syntax_error", line, snippet)` にする
  （同じ行は 1 件にまとめる）。
- `Parser` はスレッドごとに 1 つ（スレッドセーフではない）・`Language` と `Query` は共有する。
"""
from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from functools import lru_cache

from ._base import Dropped

# 言語名 → (文法のモジュール, `Language` を返す関数名)。`embedded_template` は ERB/EJS 系（JSP の分割用）。
_GRAMMARS: dict[str, tuple[str, str]] = {
    "java": ("tree_sitter_java", "language"),
    "c_sharp": ("tree_sitter_c_sharp", "language"),
    "c": ("tree_sitter_c", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "bash": ("tree_sitter_bash", "language"),
    "css": ("tree_sitter_css", "language"),
    "embedded_template": ("tree_sitter_embedded_template", "language"),
}

SNIPPET_MAX = 80
SYNTAX_ERROR_MAX = 20

_local = threading.local()


class TreeSitterUnavailable(RuntimeError):
    """`tree-sitter` 本体または文法のパッケージが読み込めない（未導入・ABI 不一致）。"""


@lru_cache(maxsize=None)
def language(name: str):
    """言語名の `tree_sitter.Language`（初回に読み込み・以後は共有）。"""
    if name not in _GRAMMARS:
        raise KeyError(f"未対応の Tree-sitter 言語: {name}")
    mod_name, fn = _GRAMMARS[name]
    try:
        import tree_sitter
        mod = importlib.import_module(mod_name)
        return tree_sitter.Language(getattr(mod, fn)())
    except (ImportError, ValueError, AttributeError) as e:
        raise TreeSitterUnavailable(
            f"Tree-sitter の文法 {name}（{mod_name}）を読み込めません: {e}。"
            " requirements.txt の tree-sitter と文法パッケージの版（ABI）を確認してください") from e


def require(*names: str) -> None:
    """指定の言語（省略時は全部）を読み込めることを確かめる。読めなければ `TreeSitterUnavailable`。"""
    for n in (names or tuple(_GRAMMARS)):
        language(n)


def _parser(name: str):
    parsers = getattr(_local, "parsers", None)
    if parsers is None:
        parsers = _local.parsers = {}
    p = parsers.get(name)
    if p is None:
        import tree_sitter
        p = parsers[name] = tree_sitter.Parser(language(name))
    return p


@dataclass
class Parsed:
    """解析結果。`src` は UTF-8 の bytes（ノードの位置はこの バイトのオフセット）。"""

    lang: str
    src: bytes
    tree: object

    @property
    def root(self):
        return self.tree.root_node

    def text(self, node) -> str:
        return self.src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def parse(lang: str, text: str | bytes) -> Parsed:
    src = text if isinstance(text, bytes) else text.encode("utf-8")
    return Parsed(lang, src, _parser(lang).parse(src))


def start_line(node) -> int:
    """ノードの開始行（1 始まり）。"""
    return node.start_point[0] + 1


def end_line(node) -> int:
    """ノードの終了行（1 始まり・終端が行頭ちょうどのときは直前の行）。"""
    row, col = node.end_point
    return row + 1 if col > 0 or row == node.start_point[0] else row


class SyntaxErrorSink:
    """構文エラーの申告の入れ物。行ごとに最初の 1 件へまとめ、行の小さい方から `SYNTAX_ERROR_MAX` 件だけ持つ。

    超えた分は件数だけ数え（`result()` が最後に `Dropped("syntax_error", 省略の最初の行, "ほか N 件")` を 1 件足す）、壊れたファイルでメモリも申告も際限なく増えない。
    複数の木（JSP のテンプレートと埋め込みの Java）で 1 つの入れ物を共有できる。省略の件数は、直前に数えた行の重複だけをまとめる概数。
    """

    def __init__(self) -> None:
        self._kept: dict[int, Dropped] = {}
        self._omitted = 0
        self._first_omitted: int | None = None
        self._last_omitted: int | None = None

    def _omit(self, line: int) -> None:
        if line != self._last_omitted:
            self._omitted += 1
        self._last_omitted = line
        if self._first_omitted is None or line < self._first_omitted:
            self._first_omitted = line

    def add(self, d: Dropped) -> None:
        if d.line in self._kept:
            return
        if len(self._kept) >= SYNTAX_ERROR_MAX:
            worst = max(self._kept)
            if d.line > worst:
                self._omit(d.line)
                return
            del self._kept[worst]
            self._omit(worst)
        self._kept[d.line] = d

    def result(self) -> list[Dropped]:
        out = [self._kept[k] for k in sorted(self._kept)]
        if self._omitted:
            out.append(Dropped("syntax_error", self._first_omitted, f"ほか {self._omitted} 件"))
        return out


def collect_syntax_errors(parsed: Parsed, sink: SyntaxErrorSink) -> None:
    """`ERROR`・`MISSING` ノードを `Dropped("syntax_error", 行, 抜粋)` にして `sink` に足す。同じ行は最初の 1 件だけ。

    `ERROR` ノードの内側の `MISSING` も別の行なら申告する（同じ行の重複だけまとめる）。
    """
    root = parsed.root
    if not root.has_error:
        return
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            line = start_line(node)
            sink.add(Dropped("syntax_error", line, f"missing {node.type}"))
        elif node.is_error:
            line = start_line(node)
            first = parsed.text(node).split("\n", 1)[0].strip()
            sink.add(Dropped("syntax_error", line, first[:SNIPPET_MAX]))
            stack.extend(reversed(node.children))
        elif node.has_error:
            stack.extend(reversed(node.children))


def syntax_errors(parsed: Parsed) -> list[Dropped]:
    """`collect_syntax_errors` の結果を、先頭 `SYNTAX_ERROR_MAX` 件＋省略の件数（`SyntaxErrorSink` の契約）で返す。"""
    sink = SyntaxErrorSink()
    collect_syntax_errors(parsed, sink)
    return sink.result()


@lru_cache(maxsize=None)
def query(lang: str, pattern: str):
    """Tree-sitter のクエリ（S 式）をコンパイルして共有する。"""
    import tree_sitter
    return tree_sitter.Query(language(lang), pattern)


def captures(parsed: Parsed, pattern: str, node=None) -> dict[str, list]:
    """`pattern` の捕捉名 → ノードの列（文書順＝開始位置の昇順）。`node` を渡すとその部分木だけ。
    Tree-sitter の QueryCursor が返す列は文書順とは限らないので、ここで並べ直す。"""
    import tree_sitter
    caps = tree_sitter.QueryCursor(query(parsed.lang, pattern)).captures(node or parsed.root)
    return {name: sorted(nodes, key=lambda n: (n.start_byte, n.end_byte)) for name, nodes in caps.items()}


def matches(parsed: Parsed, pattern: str, node=None) -> list[dict[str, list]]:
    """`pattern` の一致ごとの捕捉（捕捉名 → ノードの列）。一致の組を保ちたいとき用。"""
    import tree_sitter
    return [caps for _i, caps in tree_sitter.QueryCursor(query(parsed.lang, pattern)).matches(node or parsed.root)]
