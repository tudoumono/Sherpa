r"""C アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/§9 S6・A1）。

`.c`/`.h` を**全件受理**する。ファイル自体を主体定義（`Module`・primary）とし、トップレベルの
関数定義（`.c`）／プロトタイプ宣言（`.h`）を子定義（`Module`・`CONTAINS`）として返す（RV1 是正・
§10）——宣言のみの `.h` ファイルも主体を持てるようにするため、関数を primary にはしない。

**primary 名は拡張子込みのファイル名**（`foo.c`/`foo.h`・RV2-1 是正）: ステム名だと同じ
ディレクトリに同居する `foo.c`/`foo.h` が同一 `Module` に索引され、`#include "foo.h"` が
同距離2候補で曖昧になる。関数 children の修飾名は Copybook の `GROUP.ITEM` と同型の
`<ファイル名>.<関数名>`（`cid_key`）とする——`name` は関数名そのもの（表示名）のまま。
各 children は `extra={"c_kind": "definition"|"declaration"}` を持つ（`{` 終端か `;` 終端かの
区別・world_graph 側の単純名呼び出し解決が定義を宣言より優先するために使う索引）。

拡張子の大文字小文字は区別しない（`UTIL.H` も `.h` と同じ扱い・`PurePosixPath.suffix.lower()`
で判定する）——`registry._ext` がすでに小文字化して拡張子ルーティングしているため、本体側の
ヘッダ判定もそれに合わせる。

関数定義/プロトタイプの判定は**粗い判定**（`戻り値型 名前(引数) {` または `;`・`static`/
`inline`/`extern` 修飾は許容）で、単一物理行にマッチするものだけを見る（複数行にまたがる
シグネチャは見逃す＝安全側・Java アナライザと同じ流儀）。波括弧深度0（トップレベル）に限定し、
制御構文（`if`/`for`/`while`/`switch`/`return`/`sizeof`）を戻り値型として誤認しない。
マクロ関数定義（`#define F(x) ...`）は対象外（`#` 始まりの行は最初から構文が一致しない）。
K&R 形式（旧式・`name(params)` の次行に `型 名;` 形のパラメータ宣言が続く）は子定義としては
認識しない（検出限界のまま）が、ヘッダ行の `name(` を通常呼び出しとして誤検出しないよう
`Dropped("c_knr_definition", ...)` として申告する。

**参照**: `#include "x.h"`（ローカル・`<...>` は無視）は2段で解決する（world_graph.py 側・
§12）——本アナライザは `RefCandidate(..., extra={"via": "include", "include_path": <元の文字列>})`
を返すだけで、パス解決自体は共通層が行う。`include_path` は区切りを `\` → `/` に正規化してから
渡す（basename もその正規化後の文字列から取る）——Windows 由来のソース（`#include "..\inc\log.h"`
等）でも `world_graph._resolve_include_relpath` が同じ区切りで距離計算できるようにするため。
関数呼び出し `name(`（制御構文キーワード除外）は `via=call` で返す——解決先は他ファイルの関数
children を**単純名**（関数名のみ）で同一 top_scope 内最近傍解決する（共通層 `_register_children`
側の追加索引・§9 参照。この2段目解決は C アナライザ由来かつ `via=call` の参照だけに限定される
——他言語からの参照が同名の C 関数へ誤接続しないため）。自ファイル内定義への呼び出しも除外しない
（primary と children の cid は常に異なるため自己ループにはならない）。

関数ポインタ経由の呼び出し（`(*fp)(...)`）は `Dropped("c_dynamic_call", ...)`。宣言（`int (*fp)(int);`）
との判別は、該当物理行の前置部（行頭から `(*名)(` の手前まで）が型指定子だけの宣言形かどうかで行う
——`return (*fp)(x);` や `x = (*fp)(1);` のように前置部が制御構文キーワードや式（代入等）なら
宣言ではなく呼び出しそのものと判定し、宣言でも呼び出しでもない中途半端な判定で黙って消さない。
マクロ関数（同一ファイル内 `#define NAME(...)` で定義済みの識別子への呼び出し）は展開後にしか
実際の呼び出し先が決まらないため `Dropped("c_macro_call", ...)` として申告するだけ（推測接続はしない）。

大文字小文字は区別する（正規化しない・C は大文字小文字を区別する言語のため Java と同じ理由）。

`.h` に `class`/`namespace`/`template` 等 C++ 専用構文を検知したら `Dropped("cxx_header", ...)` を
1件申告する（primary はそのまま作る——C ヘッダとして誤解析しないことの可視化のみで、解析自体は
変えない）。

**検出限界（粗い判定の裏返し）**: シグネチャが複数物理行にまたがる関数定義/宣言は見逃す。
関数ポインタ型のフィールド/変数宣言（`int (*fp)(int);`）は関数宣言として誤認しない設計だが、
検出自体もしない（構造情報を持たない単なる見逃し）。`typedef` で定義された関数ポインタ型・
可変引数マクロ・条件コンパイル（`#ifdef`）分岐後にしか存在しない定義は考慮しない。K&R 形式の
関数定義は誤って呼び出し扱いにはしないが、子定義としても認識しない（検出限界のまま）。
"""
from __future__ import annotations

import bisect
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

C_EXT = frozenset({".c", ".h"})

# 制御構文キーワード（戻り値型として誤認しない・関数呼び出しとしても除外する）。
_CONTROL_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "sizeof"})

# 関数定義／プロトタイプ宣言（単一物理行のみ・粗い判定）: [modifiers] rtype name(args) {|;
# 定義（`{`）は本文が同じ行に続いてもよい（`int f(void) { return 0; }` 形の短い定義も検出する）——
# `.match()` は先頭一致のみを要求し、末尾までの一致は要求しない。
_FUNC_SIG = re.compile(
    r'^(?:(?:static|inline|extern)\s+)*'
    r'(?P<rtype>[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*)'
    r'[\s*]+'
    r'(?P<name>[A-Za-z_]\w*)\s*'
    r'\((?P<args>[^;{}()]*)\)\s*'
    r'(?P<term>[{;])'
)

# `#include "x.h"`／`#include <x.h>`（後者は無視）。
_INCLUDE = re.compile(r'^\s*#\s*include\s*(?:"(?P<local>[^"]+)"|<[^>]+>)', re.M)

# `#define NAME(...)`（関数マクロ・名前と `(` の間に空白を許さない＝オブジェクトマクロと区別）。
_MACRO_DEFINE = re.compile(r'^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\(', re.M)

# 関数ポインタ経由の呼び出し（`(*fp)(...)`）。
_DYNAMIC_CALL = re.compile(r'\(\s*\*\s*[A-Za-z_]\w*\s*\)\s*\(')

# 通常の呼び出し（`identifier(`）。直後が `(*`（例: `int (*fp)(int);` の型名部分）は関数ポインタ型
# 宣言の戻り値型/変数型であり呼び出しではないため除外する。
_CALL = re.compile(r'\b(?P<name>[A-Za-z_]\w*)\s*\((?!\s*\*)')

# K&R 形式（旧式）の関数定義ヘッダ: `name(params)` で行末（`{`/`;` を伴わない）。
_KNR_HEADER = re.compile(
    r'^(?:(?:static|inline|extern)\s+)*'
    r'(?P<rtype>[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*)'
    r'[\s*]+'
    r'(?P<name>[A-Za-z_]\w*)\s*'
    r'\((?P<args>[^;{}()]*)\)\s*$'
)
# K&R パラメータ宣言（`型 名;` 形・ヘッダ直後の行に現れることを確認する材料）。
_KNR_PARAM_DECL = re.compile(r'^[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[\s*]+[A-Za-z_]\w*\s*;\s*$')

# `(*名)(` の前置部（該当物理行の行頭からその手前まで）が「型指定子だけの宣言形」かどうかの判定。
# `static`/`extern`/`const` 修飾＋識別子の連なりのみを許し、代入/制御構文等の式は含まない。
_POINTER_DECL_PREFIX = re.compile(
    r'^\s*(?:(?:static|extern|const)\s+)*[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[\s*]*$'
)

# `.h` に現れたら C++ 専用構文とみなす予約語（C にはこれらのキーワードは無い）。
_CXX_ONLY_HEADER = re.compile(r'\b(?:class|namespace|template)\b')


def _sanitize(text: str) -> str:
    """コメント（`//`・`/* */`）と文字列/char リテラルの中身を空白化した、同じ行数の文字列を返す
    （偽マッチ除外専用・改行は保持し行番号が原本と1対1のまま）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text[i:i + 2] == "/*":
            out.append("  ")
            i += 2
            while i < n and text[i:i + 2] != "*/":
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch == "/" and text[i:i + 2] == "//":
            out.append("  ")
            i += 2
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(" ")
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sanitize_comments_only(text: str) -> str:
    """コメントだけを空白化し、文字列リテラルの中身は残す（`#include "path"` のパス文字列を
    読む必要があるため・Java アナライザの同名ヘルパと同じ役割）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text[i:i + 2] == "/*":
            out.append("  ")
            i += 2
            while i < n and text[i:i + 2] != "*/":
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch == "/" and text[i:i + 2] == "//":
            out.append("  ")
            i += 2
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i < n:
                out.append(quote)
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _newline_offsets(text: str) -> list:
    """`text` 内の全改行位置（昇順）。`extract_refs` 1回につき1回だけ作り、`_line_at` は
    これを `bisect` で引く（大規模ファイルでも対数時間・sql.py と同じ流儀）。"""
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


def _preprocessor_line_numbers(text: str) -> set:
    """`#` で始まる（プリプロセッサ指令の）物理行番号の集合。関数定義/呼び出し検出の対象外にする
    （`#define`/`#include`/`#if defined(...)` 等を誤って関数の宣言/呼び出しと解釈しないため）。"""
    return {i for i, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("#")}


def _scan_func_decls(sanitized: str, pp_lines: set) -> list:
    """波括弧深度0の関数定義/プロトタイプ宣言を `(name, line, name_start, term)` のリストで返す
    （`term` は `"{"`＝定義／`";"`＝宣言（プロトタイプ）そのもの）。

    `name_start` は `extract_refs` 側が同じ出現位置を「呼び出しではなく定義/宣言」として
    通常の呼び出し走査から除外するために使う（`_CALL` は `identifier(` という定義/宣言のヘッダとも
    重なる形のため、二重検出を避ける）。`term` は `collect_defs` 側が children 化を `.c`（定義＝`{`
    のみ）／`.h`（宣言＝`;` も含む）で使い分けるために使う。
    """
    out: list = []
    depth = 0
    pos = 0
    for i, raw_line in enumerate(sanitized.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        line_start = pos
        pos += len(raw_line) + 1                          # +1＝改行分（次行の開始位置へ進める）
        if i in pp_lines or line_depth != 0:
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = _FUNC_SIG.match(stripped)
        if not m:
            continue
        if m.group("rtype").strip() in _CONTROL_KEYWORDS:
            continue                                      # `return X(...);` 等の誤爆防止
        # `stripped` は行内の先頭空白を除去済み——元の `raw_line` 内でのオフセットへ補正する。
        offset_in_line = len(raw_line) - len(raw_line.lstrip())
        name_start = line_start + offset_in_line + m.start("name")
        out.append((m.group("name"), i, name_start, m.group("term")))
    return out


def _scan_knr_definitions(sanitized: str, pp_lines: set) -> list:
    """波括弧深度0の K&R 形式（旧式）関数定義ヘッダを `(name, line, name_start)` のリストで返す。

    `name(params)` 単独行（`{`/`;` を伴わない）の直後の非空行が `型 名;` 形のパラメータ宣言で
    あることまで確認してから採用する——単なる複数行にまたがる呼び出し式まで拾わないため。
    採用した出現位置は `extract_refs` が通常呼び出し（`_CALL`）走査から除外し、代わりに
    `Dropped("c_knr_definition", ...)` として申告する（子定義としては認識しない＝検出限界のまま）。
    """
    out: list = []
    depth = 0
    pos = 0
    lines = sanitized.split("\n")
    for i, raw_line in enumerate(lines, 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        line_start = pos
        pos += len(raw_line) + 1
        if i in pp_lines or line_depth != 0:
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = _KNR_HEADER.match(stripped)
        if not m:
            continue
        if m.group("rtype").strip() in _CONTROL_KEYWORDS:
            continue
        nxt = None
        for j in range(i, len(lines)):
            candidate = lines[j].strip()
            if candidate:
                nxt = candidate
                break
        if nxt is None or not _KNR_PARAM_DECL.match(nxt):
            continue                                      # 次行がパラメータ宣言形でなければ K&R と断定しない
        offset_in_line = len(raw_line) - len(raw_line.lstrip())
        name_start = line_start + offset_in_line + m.start("name")
        out.append((m.group("name"), i, name_start))
    return out


def _looks_like_pointer_decl_prefix(line_prefix: str) -> bool:
    """`(*名)(` の前置部（同一物理行内・行頭からその手前まで）が関数ポインタの宣言形
    （型指定子だけ）かどうかを判定する。`return`/`if` 等の制御構文キーワードは字面上
    「識別子1語」と区別が付かないため、先頭トークンが制御構文キーワードなら宣言とはみなさない
    （`return (*fp)(x);` を宣言と誤認しない）。"""
    if not _POINTER_DECL_PREFIX.match(line_prefix):
        return False
    tokens = line_prefix.split()
    return bool(tokens) and tokens[0] not in _CONTROL_KEYWORDS


class CAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。トップレベル関数定義/プロトタイプ
    → `Module`（children・`<ファイル名>.<関数名>` 修飾）。`#include`/呼び出し → `INVOKES` 候補。"""

    name = "c"
    extensions = C_EXT
    resolves_calls_by_simple_name = True
    doctype = "c"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        is_header = PurePosixPath(rel_path).suffix.lower() == ".h"   # 拡張子は大文字小文字を区別しない
        sanitized = _sanitize(text)
        lines_raw = text.splitlines()
        pp_lines = _preprocessor_line_numbers(text)
        # `.c` は定義（`{` 終端）だけを children 化する——`;` 終端は外部宣言（プロトタイプ）であり、
        # 別ファイルの実体を指すだけの宣言を自ファイルの偽 child にしない。`.h` は従来どおり
        # 宣言（`;`）も children 化する。
        decls = _scan_func_decls(sanitized, pp_lines)
        if is_header:
            child_decls = decls
        else:
            child_decls = [d for d in decls if d[3] == "{"]
        # `c_kind`（定義/宣言）を children の索引に持たせる（world_graph._register_children が
        # `simple_name_defs` へそのまま転記し、単純名の呼び出し解決が定義側を宣言側より優先できる
        # ようにするため・§9）。
        children = [
            DefItem(label="Module", name=name, cid_key=f"{filename}.{name}", line=line,
                    extra={"c_kind": "definition" if term == "{" else "declaration"})
            for name, line, _start, term in child_decls
        ]
        dropped: list = []
        if is_header:
            cxx_m = _CXX_ONLY_HEADER.search(sanitized)
            if cxx_m:
                line = sanitized.count("\n", 0, cxx_m.start()) + 1
                snippet = lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else ""
                dropped.append(Dropped("cxx_header", line, snippet))
        return DefResult(primary=DefItem(label="Module", name=filename), children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        comments_only = _sanitize_comments_only(text)
        pp_lines = _preprocessor_line_numbers(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        for m in _INCLUDE.finditer(comments_only):
            line = _line_at(newline_offsets, m.start())
            local = m.group("local")
            if not local:
                continue                                  # `<...>` システムヘッダは対象外
            local = local.replace("\\", "/")               # Windows 区切り正規化（analyzer 入口・§4(a)）
            basename = PurePosixPath(local).name
            if basename:
                refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                         extra={"via": "include", "include_path": local}))

        for m in _DYNAMIC_CALL.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            if line in pp_lines:
                continue
            line_start = sanitized.rfind("\n", 0, m.start()) + 1
            line_prefix = sanitized[line_start:m.start()]
            if _looks_like_pointer_decl_prefix(line_prefix):
                continue                                  # 前置部が型指定子だけの宣言形＝関数ポインタ型の
                                                            # 変数宣言（`int (*fp)(int);`）——呼び出しではなく
                                                            # 検出限界として黙って見逃す（docstring 参照）
            dropped.append(Dropped("c_dynamic_call", line, sanitized.splitlines()[line - 1].strip()[:120]))

        decl_positions = {start for _name, _line, start, _term in _scan_func_decls(sanitized, pp_lines)}
        knr_defs = _scan_knr_definitions(sanitized, pp_lines)
        knr_positions = {start for _name, _line, start in knr_defs}
        for name, line, _start in knr_defs:
            dropped.append(Dropped("c_knr_definition", line, sanitized.splitlines()[line - 1].strip()[:120]))
        macro_names = {m.group("name") for m in _MACRO_DEFINE.finditer(sanitized)}

        for m in _CALL.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            if line in pp_lines or m.start("name") in decl_positions or m.start("name") in knr_positions:
                continue
            name = m.group("name")
            if name in _CONTROL_KEYWORDS:
                continue
            if name in macro_names:
                dropped.append(Dropped("c_macro_call", line, name))
                continue
            refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "call"}))

        return RefResult(refs=refs, dropped=dropped)
