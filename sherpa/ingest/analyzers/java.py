"""Java アナライザ（docs/05-グラフ語彙.md §4 トラック S・CODE-1d＝新言語1つでの手順検証・
CODE-2/JAVA-2＝宣言型参照の一般抽出）。

`public class/interface/enum/record`（ファイル主体）を主体定義（`Module`）とし、同一ファイル内の
非 public 型を子定義（`Module`・`primary -CONTAINS-> child`）として返す。`new X(...)`・
`X.method(...)`（静的呼び出し・大文字始まりの修飾子＝クラス名の慣習で変数呼び出しと区別する
ヒューリスティック）・`extends X`・`implements X`・フィールド/コンストラクタ引数/メソッド引数の
**宣言型**を参照候補（`INVOKES`）として返す。細分は `RefCandidate.extra["via"]`
（`call`/`extends`/`implements`/`field_type`/`inject`）で持つ（docs/05 §2 一般化・エッジ型は増やさない）。

**フレームワークに依存しない設計**（裁定2026-09-03）: 宣言型参照はアノテーションの有無に関わらず
常に抽出する——「プロジェクト内の型をフィールド/引数に宣言していること自体が依存」であり、DI
（Spring/Guice/手書き）は全てこの形に落ちる。`@Autowired`/`@Inject`/`@Resource` が直前に付く
フィールドは `via=field_type` を `via=inject` へ**格上げ**するだけ（検出手段ではなく分類の改善・
アノテーションが無くても同じ依存は `field_type` で拾える）。

`import` 文はエッジにせず、主体の `extra["imports"]` に解決ヒントとして積むだけ（共通層の
名前解決には使わない＝同一 top_scope 内最近傍のまま）。

外部パーサは使わない（COBOL/JCL/コピーブックと同じ流儀＝正規表現＋行走査で確実に取れるものだけ
取る）。コメント（`//`・`/* */`）と文字列/char/text-block リテラル（`"..."`・`'...'`・三連続の
二重引用符で囲む複数行リテラル）は `_sanitize()` で中身を空白化してから走査し、偽マッチを
除外する。標準で安全に解釈できない構文
（内部クラスの深い入れ子等）は解析せず `dropped` に記録して落とす（黙って誤解釈しない）。

宣言型抽出（フィールド/引数）は**トップレベル型の直下（brace 深度=1）に限定**する——ローカル変数
（メソッド本体内＝深度2以上）は対象にしない（依頼のスコープ外・ノイズ増を避ける）。深度はファイル
先頭からの `{`/`}` 累積カウントで判定する（`_iter_top_level_type_decls` と同じ手法）。複数行に
またがるフィールド宣言・メソッド/コンストラクタの引数リストは対象外（安全に取れる単一行のみ・
見逃しは許容するが誤った候補は作らない）。

`identifiers.normalize_code_name()`（COBOL 前提の大文字化＋末尾ドット除去）は使わない——Java は
大文字小文字を区別する言語であり、正規化すると別クラスを同一視してしまう（CODE-1d の検証で
判明した既存正規化ヘルパの言語依存性・詳細は docs/proposals/2026-08-29 の CODE-1d 節）。
正規表現の捕捉結果（識別子）はそのまま使う。
"""
from __future__ import annotations

import re

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

# 拡張子は本ファイルに閉じて持つ（`static_analysis.py` は docstring 上 COBOL/JCL/コピーブック
# 構文専用のプリミティブ置き場と明言されており、Java 用の正規表現をそこへ混ぜると自身の
# スコープ宣言と矛盾する。新言語アナライザは自己完結させる、という選択——詳細は CODE-1d 節）。
JAVA_EXT = frozenset({".java"})

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
_TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
_PUBLIC_MODIFIER = re.compile(r"\bpublic\b")
_EXTENDS_CLAUSE = re.compile(r"\bextends\s+(.+?)(?:\bimplements\b|$)", re.S)
_IMPLEMENTS_CLAUSE = re.compile(r"\bimplements\s+(.+)$", re.S)
# `new X(...)`／`X.method(...)`（大文字始まりの修飾子＝クラス名という Java の命名慣習で
# インスタンス変数呼び出しと区別するヒューリスティック・enum 定数の連鎖参照等で偽陽性の余地は
# あるが、共通層の名前解決が「見つからなければ unresolved flag」に倒すため誤ったエッジは作らない）。
_CALL_LIKE = re.compile(
    r"\bnew\s+(?P<new_type>[A-Za-z_$][\w$.]*)(?:\s*<[^>{};]*>)?\s*\("
    r"|\b(?P<static_type>[A-Z][\w$]*)\.(?P<static_method>[A-Za-z_$][\w$]*)\s*\("
)
# extends/implements のヘッダをボディ開始 `{` まで前方探索する上限（暴走防止・整形が崩れた
# ファイルでも無限にスキャンしない）。
_HEADER_SCAN_LIMIT = 4000

# JDK 標準ライブラリの頻出型（小さな既知リスト・ノイズ削減用）。プロジェクト内クラスの可能性が
# ある大文字始まりの型のうち、これらは候補にしない——実運用で最も出現しやすいものだけに絞る
# （網羅は目指さない・漏れたJDK型は従来どおり unresolved flag として無害に処理される）。
_JDK_COMMON_TYPES = frozenset({
    "Object", "String", "CharSequence", "Number", "Boolean", "Character", "Byte", "Short",
    "Integer", "Long", "Float", "Double", "Void", "Class", "Enum", "Comparable", "Iterable",
    "Iterator", "Runnable", "Thread", "Throwable", "Exception", "RuntimeException", "Error",
    "List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap", "TreeMap",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "Collection", "Optional", "Stream",
    "Comparator", "BigDecimal", "BigInteger", "Date", "UUID", "Pattern", "Matcher",
})

# DI アノテーション（付加情報のみ・検出手段にはしない——無くても `field_type` で同じ依存が拾える）。
_DI_ANNOTATIONS = frozenset({"Autowired", "Inject", "Resource"})

# アノテーションのみの行（引数を持ってもよい・sanitize 後は文字列引数の中身が空白になる）。
_ANNOTATION_ONLY_LINE = re.compile(r"^@(?P<name>[A-Za-z_$][\w$]*)(?:\s*\([^)]*\))?\s*$")
# 行頭の連続するインラインアノテーションを読み飛ばすための prefix（`@Override public void f()` 等）。
_LEADING_ANNOTATIONS = re.compile(r"^(?:@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s+)+")
# フィールド宣言（クラス直下＝brace 深度1限定で使う）。修飾子は前置可・型は単純名/修飾名＋1段ジェネリクス。
_FIELD_DECL_LINE = re.compile(
    r"^(?:(?:public|private|protected|static|final|transient|volatile)\s+)*"
    r"(?P<type>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"(?P<generics><[^<>{};]*>)?"
    r"(?:\s*\[\])*"
    r"\s+[A-Za-z_$][\w$]*\s*[=;]"
)
# メソッド/コンストラクタ引数リストの先頭候補（`識別子(`・`new` は除外）。
_METHOD_NAME_PAREN = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
# 引数リストの1エントリ（`final`/引数アノテーションは読み飛ばす・型＋1段ジェネリクス＋変数名）。
_PARAM_ENTRY_TYPE = re.compile(
    r"^(?:final\s+)?"
    r"(?:@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s+)*"
    r"(?P<type>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
    r"(?P<generics><[^<>]*>)?"
    r"(?:\s*\[\])*"
    r"(?:\s*\.\.\.)?"
    r"\s+[A-Za-z_$][\w$]*$"
)


def _sanitize(text: str) -> str:
    """コメント（`//`・`/* */`）と文字列/char/text-block リテラルの中身を空白化した、
    同じ行数の文字列を返す（偽マッチ除外専用・実際の解析はこの結果に対して行う）。

    改行はすべてそのまま保持する——`line` 番号（`sanitized.count("\\n", 0, pos) + 1`）が
    元テキストの行番号と1対1で対応する契約を保つため。
    """
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
        if text[i:i + 3] == '"""':                       # text block（Java 15+）
            out.append("   ")
            i += 3
            while i < n and text[i:i + 3] != '"""':
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("   ")
                i += 3
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


def _line_at(sanitized: str, pos: int) -> int:
    return sanitized.count("\n", 0, pos) + 1


def _strip_generics(s: str) -> str:
    """balanced `<...>` を除去する（ネストにも対応・除去できない不整合は安全側でそのまま残す）。"""
    out: list = []
    depth = 0
    for ch in s:
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _split_type_list(raw: str) -> list:
    """`extends`/`implements` 節の型リストを単純名のリストへ（ジェネリクス・パッケージ修飾を落とす）。"""
    names = []
    for part in _strip_generics(raw).split(","):
        simple = re.sub(r"[^\w$.]", "", part).rsplit(".", 1)[-1]
        if simple:
            names.append(simple)
    return names


def _split_top_level_commas(s: str) -> list:
    """`<...>` の中を跨がないトップレベルのカンマで分割する（ジェネリクス引数リスト・引数リスト用）。"""
    parts: list = []
    depth = 0
    buf: list = []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _find_param_list(line: str) -> str | None:
    """行から最初の「`識別子(`」（`new` を除く）を探し、対応する `)` までのバランス済み中身を返す。

    メソッド/コンストラクタのシグネチャ行を引数リストへ分解する用途——`new Foo(...)`（コンストラクタ
    呼び出し）は対象外にする。対応する `)` が同一行に無い（複数行シグネチャ）場合は `None`
    （安全に取れる単一行のみ・見逃しは許容）。
    """
    for m in _METHOD_NAME_PAREN.finditer(line):
        if m.group(1) == "new":
            continue
        pre = line[:m.start()].rstrip()
        if pre.endswith("new") and (len(pre) == 3 or not pre[-4].isalnum()):
            continue                                      # 直前トークンが `new`＝コンストラクタ呼び出し
        open_pos = m.end() - 1
        depth = 0
        for j in range(open_pos, len(line)):
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
                if depth == 0:
                    return line[open_pos + 1:j]
        return None
    return None


def _emit_declared_type_refs(refs: list, type_token: str, generics_token: str | None,
                             line: int, via: str) -> None:
    """宣言型（＋1段のジェネリクス型引数）を `INVOKES(via=...)` 候補として積む。

    JDK 頻出型（`_JDK_COMMON_TYPES`）・小文字始まり（プリミティブ/変数名紛れ）は候補にしない。
    ジェネリクス型引数はさらに1段深いネストを除去する（`_strip_generics`）——2段目以降は見逃す
    （誤検出しないための安全側の設計・docs/proposals/2026-08-29 CODE-1d 節の方針を踏襲）。
    """
    simple = type_token.rsplit(".", 1)[-1]
    if simple[:1].isupper() and simple not in _JDK_COMMON_TYPES:
        refs.append(RefCandidate("INVOKES", "Module", simple, line, extra={"via": via}))
    if not generics_token:
        return
    inner = generics_token.strip("<>")
    for arg in _split_top_level_commas(inner):
        arg = _strip_generics(arg).strip()
        arg = re.sub(r"\[\]\s*$", "", arg).strip()
        simple_arg = arg.rsplit(".", 1)[-1]
        if (simple_arg[:1].isupper() and simple_arg not in _JDK_COMMON_TYPES
                and re.fullmatch(r"[A-Za-z_$][\w$]*", simple_arg)):
            # ジェネリクス型引数は常に field_type（inject 格上げは宣言型本体のみに適用）。
            refs.append(RefCandidate("INVOKES", "Module", simple_arg, line, extra={"via": "field_type"}))


def _collect_declared_type_refs(sanitized: str) -> list:
    """フィールド宣言・コンストラクタ引数・メソッド引数の宣言型を参照候補として抽出する。

    トップレベル型の直下（brace 深度=1・ファイル先頭からの累積カウントで判定）に限定し、メソッド
    本体内のローカル変数（深度2以上）は対象にしない。直前（連続してもよい）が `@Autowired`/
    `@Inject`/`@Resource` のみの行ならフィールドを `via=inject` へ格上げする（他の行を挟んだら
    `pending_inject` はリセット＝「直前」の判定）。
    """
    refs: list = []
    depth = 0
    pending_inject = False
    for i, raw_line in enumerate(sanitized.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        stripped = raw_line.strip()
        if not stripped:
            continue
        am = _ANNOTATION_ONLY_LINE.match(stripped)
        if am:
            if am.group("name") in _DI_ANNOTATIONS:
                pending_inject = True
            continue
        if line_depth != 1:
            pending_inject = False
            continue
        body = _LEADING_ANNOTATIONS.sub("", stripped)
        fm = _FIELD_DECL_LINE.match(body)
        if fm:
            via = "inject" if pending_inject else "field_type"
            _emit_declared_type_refs(refs, fm.group("type"), fm.group("generics"), i, via)
            pending_inject = False
            continue
        params = _find_param_list(body)
        if params is not None:
            for entry in _split_top_level_commas(params):
                entry = entry.strip()
                if not entry:
                    continue
                pm = _PARAM_ENTRY_TYPE.match(entry)
                if pm:
                    _emit_declared_type_refs(refs, pm.group("type"), pm.group("generics"), i, "field_type")
        pending_inject = False
    return refs


def _iter_top_level_type_decls(sanitized: str):
    """波括弧深度0（トップレベル）の型宣言を `(match, line, is_public)` で返す。

    深度>0（内部クラスの入れ子等）は `nested` として別途 (match, line) を返す——正規表現で
    安全に解釈できない構文として `Dropped` 記録の材料にする（ノード化はしない）。
    """
    depth = 0
    pos = 0
    top: list = []
    nested: list = []
    for m in _TYPE_DECL.finditer(sanitized):
        depth += sanitized.count("{", pos, m.start()) - sanitized.count("}", pos, m.start())
        pos = m.end()
        line = _line_at(sanitized, m.start())
        if depth > 0:
            nested.append((m, line))
            continue
        line_start = sanitized.rfind("\n", 0, m.start()) + 1
        is_public = bool(_PUBLIC_MODIFIER.search(sanitized[line_start:m.start()]))
        top.append((m, line, is_public))
    return top, nested


def _header_of(sanitized: str, decl_end: int) -> str:
    """型宣言（`class Foo` 等）の直後からボディ開始 `{` 直前までのヘッダ文字列
    （`extends`/`implements` 節を含み得る・複数行に及んでもよい・上限あり）。"""
    window_end = min(len(sanitized), decl_end + _HEADER_SCAN_LIMIT)
    brace = sanitized.find("{", decl_end, window_end)
    return sanitized[decl_end:brace if brace != -1 else window_end]


class JavaAnalyzer(Analyzer):
    """`public class/interface/enum/record` → `Module`（primary）。同一ファイル内の非 public 型
    → `Module`（children・`CONTAINS`）。`new`/静的呼び出し/`extends`/`implements` → `INVOKES` 候補。
    """

    name = "java"
    extensions = JAVA_EXT
    doctype = "java"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        sanitized = _sanitize(text)
        lines_raw = text.splitlines()
        top, nested = _iter_top_level_type_decls(sanitized)
        dropped = [Dropped("nested_type", line,
                           (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else ""))
                   for _m, line in nested]
        if not top:
            return DefResult(dropped=dropped)

        # primary＝最初の public 型。public が1つも無ければ最初の型宣言を primary に採る
        # （非public型だけのファイルでもノードを黙って消さないための既定挙動・§7 裁定10の
        # 「黙って倒さない」精神を踏襲した実装判断・CODE-1d 節で報告）。
        primary_idx = next((i for i, (_m, _l, pub) in enumerate(top) if pub), 0)
        primary_m, _primary_line, _pub = top[primary_idx]
        primary_name = primary_m.group(1)

        pm = _PACKAGE.search(sanitized)
        package = pm.group(1) if pm else None
        qualified = f"{package}.{primary_name}" if package else primary_name
        imports = [m.group(1) for m in _IMPORT.finditer(sanitized)]

        extra = {}
        if package:
            extra["qualified_name"] = qualified                # cid_key は現行 world_graph では
        if imports:                                             # primary に対し未消費（後述コメント参照）
            extra["imports"] = imports
        primary = DefItem(label="Module", name=primary_name, cid_key=qualified, extra=extra)

        children = [DefItem(label="Module", name=m.group(1), line=line)
                    for i, (m, line, _pub) in enumerate(top) if i != primary_idx]

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        refs: list = []

        top, _nested = _iter_top_level_type_decls(sanitized)
        for m, line, _pub in top:
            header = _header_of(sanitized, m.end())
            em = _EXTENDS_CLAUSE.search(header)
            if em:
                for name in _split_type_list(em.group(1)):
                    refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "extends"}))
            im = _IMPLEMENTS_CLAUSE.search(header)
            if im:
                for name in _split_type_list(im.group(1)):
                    refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "implements"}))

        for m in _CALL_LIKE.finditer(sanitized):
            line = _line_at(sanitized, m.start())
            if m.group("new_type"):
                name = _strip_generics(m.group("new_type")).rsplit(".", 1)[-1]
            else:
                name = m.group("static_type")
            if name:
                refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "call"}))

        # 宣言型参照（フィールド/コンストラクタ引数/メソッド引数・JAVA-2＝フレームワーク非依存の
        # 一般抽出）。DI アノテーションが直前に付くフィールドは via=inject へ格上げ済み。
        refs.extend(_collect_declared_type_refs(sanitized))

        # nested type（内部クラス）は collect_defs 側の dropped で既に記録済み——ここで
        # 二重記録しない。
        return RefResult(refs=refs, dropped=[])
