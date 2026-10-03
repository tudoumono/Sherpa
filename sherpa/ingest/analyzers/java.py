"""Java アナライザ。`public class/interface/enum/record`（ファイル主体）を主体定義（`Module`）とし、同一ファイル内の非 public 型を子定義（`CONTAINS`）として返す。

参照（`INVOKES`・細分は `extra["via"]`）:
- `new X(...)`・`X.method(...)`（大文字始まりの修飾子＝クラス名とみなす）→ `call`、`extends`/`implements`。
- フィールド/コンストラクタ引数/メソッド引数の宣言型 → `field_type`（アノテーションに依らず常に抽出。直前が `@Autowired`/`@Inject`/`@Resource` なら `inject` に格上げ）。トップレベル型の直下（brace 深度1）・単一行のものに限る。
- `import` はエッジにせず、主体の `extra["imports"]` へヒントとして積む。

設定キー参照（`ACCESSES`→`Config`・`via=config_key`）: `@Value("${k}")`（`${k:default}` の default は捨てる・複数あれば全部）・`getProperty("k")`・`getString("k")` は `key_kind="property"`、`getBean("k")`・`@Qualifier`・`@Named`・`@Resource(name=...)` は `"bean"`。キーは識別子形のみ。同じ `(key, key_kind)` は最初の出現行にまとめる。SpEL・`@ConfigurationProperties(prefix)`・非リテラル引数は `Dropped`（`config_spel`/`config_prefix`/`config_nonliteral`）で申告する。
URL キー定義: クラスレベル `@RequestMapping` の prefix（配列は直積）とメソッドレベルのマッピング注釈を連結し、primary の children（`Config`・`cid_key="key:url:"+パス`・`key_kind="url"`）として返す。注釈は単一行で完結するものだけ検出する。

外部パーサは使わず正規表現＋行走査（コメント・文字列・text block は `_sanitize()` で空白化）。大文字小文字は区別する（`normalize_code_name()` は使わない）。標準で解釈できない構文（深い入れ子の内部クラス等）は `dropped` に記録する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import re

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

# 拡張子は本ファイルに閉じて持つ（`static_analysis.py` は COBOL/JCL/コピーブック用）。
JAVA_EXT = frozenset({".java"})

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)
_TYPE_DECL = re.compile(r"\b(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")
_PUBLIC_MODIFIER = re.compile(r"\bpublic\b")
_EXTENDS_CLAUSE = re.compile(r"\bextends\s+(.+?)(?:\bimplements\b|$)", re.S)
_IMPLEMENTS_CLAUSE = re.compile(r"\bimplements\s+(.+)$", re.S)
# `new X(...)`／`X.method(...)`（大文字始まりの修飾子＝クラス名とみなすヒューリスティック。誤りは共通層が unresolved flag に倒す）。
_CALL_LIKE = re.compile(
    r"\bnew\s+(?P<new_type>[A-Za-z_$][\w$.]*)(?:\s*<[^>{};]*>)?\s*\("
    r"|\b(?P<static_type>[A-Z][\w$]*)\.(?P<static_method>[A-Za-z_$][\w$]*)\s*\("
)
# extends/implements のヘッダをボディ開始 `{` まで前方探索する上限。
_HEADER_SCAN_LIMIT = 4000

# JDK 標準ライブラリの頻出型（ノイズ削減用の小さな既知リスト）。候補にしない。
_JDK_COMMON_TYPES = frozenset({
    "Object", "String", "CharSequence", "Number", "Boolean", "Character", "Byte", "Short",
    "Integer", "Long", "Float", "Double", "Void", "Class", "Enum", "Comparable", "Iterable",
    "Iterator", "Runnable", "Thread", "Throwable", "Exception", "RuntimeException", "Error",
    "List", "ArrayList", "LinkedList", "Map", "HashMap", "LinkedHashMap", "TreeMap",
    "Set", "HashSet", "LinkedHashSet", "TreeSet", "Collection", "Optional", "Stream",
    "Comparator", "BigDecimal", "BigInteger", "Date", "UUID", "Pattern", "Matcher",
})

# DI アノテーション（`via=inject` への分類にだけ使う）。
_DI_ANNOTATIONS = frozenset({"Autowired", "Inject", "Resource"})

# アノテーションのみの行（引数を持ってもよい）。
_ANNOTATION_ONLY_LINE = re.compile(r"^@(?P<name>[A-Za-z_$][\w$]*)(?:\s*\([^)]*\))?\s*$")
# 行頭の連続するインラインアノテーションを読み飛ばす prefix（`@Override public void f()` 等）。
_LEADING_ANNOTATIONS = re.compile(r"^(?:@[A-Za-z_$][\w$]*(?:\([^)]*\))?\s+)+")
# フィールド宣言（クラス直下＝brace 深度1）。修飾子は前置可・型は単純名/修飾名＋1段ジェネリクス。
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

# 設定キー参照: 識別子形のキーのみ（ドット/ハイフン区切りを許す）。
_CONFIG_KEY = r"[A-Za-z0-9_.\-]+"
# `@Value(...)` の文字列引数全体（中身は後段で `${...}` を全部抽出する。エスケープされた `\"` は境界にしない）。
_VALUE_CALL = re.compile(r'@Value\s*\(\s*"(?P<content>(?:\\.|[^"\\])*)"\s*\)')
# `${key}`／`${key:default}`（デフォルト部分は捨てる）。
_VALUE_PLACEHOLDER = re.compile(r'\$\{(?P<key>[^}:]*)(?::[^}]*)?\}')
# SpEL（`#{...}`）の目印（`Dropped("config_spel")` で申告する）。
_SPEL_MARKER = "#{"
# `getProperty`/`getString` の呼び出し（レシーバは高々1段の `識別子.`）。引数は丸ごと捕捉し、後段で文字列リテラル1個かを判定する（非リテラルは `Dropped` で申告する）。`recv` は、レシーバ有りならメソッド宣言ではあり得ないと判定するための named group（`_is_method_declaration`）。
_GET_PROPERTY_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getProperty\(\s*(?P<arg>[^()]*)\)')
_GET_STRING_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getString\(\s*(?P<arg>[^()]*)\)')
# `getBean("k")`（レシーバ有無を問わない）。
_GET_BEAN_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getBean\(\s*(?P<arg>[^()]*)\)')
_STRING_LITERAL_ARG = re.compile(r'^"(?:\\.|[^"\\])*"$')
# `@Qualifier("k")`／`@Named("k")`／`@Resource(name="k")`（`value=` 形も可）。属性値が識別子形の文字列リテラルの場合のみ抽出し、非リテラルは黙って対象外にする。
_QUALIFIER_ANNOTATION = re.compile(
    r'@Qualifier\(\s*(?:value\s*=\s*)?"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
_NAMED_ANNOTATION = re.compile(
    r'@Named\(\s*(?:value\s*=\s*)?"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
_RESOURCE_NAME_ANNOTATION = re.compile(
    r'@Resource\(\s*name\s*=\s*"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
# メソッド宣言を呼び出しと誤認しないための3条件（`_is_method_declaration` が全部揃った時だけ宣言と判定する）。
_DECL_RETURN_TYPE_BEFORE = re.compile(r'[A-Za-z_$][\w$.\[\]<>]*\s+$')
_DECL_AFTER_PARENS = re.compile(r'^\s*(?:\{|throws\b|;)')
# `@ConfigurationProperties(prefix="p")` の prefix は接続しない（`Dropped("config_prefix")` で申告する）。
_CONFIGURATION_PROPERTIES_PREFIX = re.compile(
    r'@ConfigurationProperties\(\s*prefix\s*=\s*"(?P<prefix>' + _CONFIG_KEY + r')"\s*\)')

# URL キー定義側（Spring MVC のマッピング注釈）: `@RequestMapping`/`@GetMapping` 等は、自身の行だけで完結するアノテーション専用行（引数は同一行内）としてのみ検出する。
_MAPPING_ANNOTATION_NAMES = frozenset({
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
})
# アノテーション専用行（`@名前` または `@名前(引数)`。引数は `)` を含まない前提）。クラスレベルの prefix 探索とメソッドレベルの検出で使う。
_ANNOTATION_ONLY_LINE_ANY_ARGS = re.compile(r'^@(?P<name>[A-Za-z_$][\w$]*)(?:\s*\((?P<args>[^)]*)\))?\s*$')
# `value=`/`path=` 属性値（単一文字列、または `{"a", "b"}` の配列）。
_MAPPING_PATH_ATTR = re.compile(r'\b(?:value|path)\s*=\s*(?P<val>\{[^}]*\}|"(?:\\.|[^"\\])*")')
_QUOTED_STRING_ITER = re.compile(r'"(?:\\.|[^"\\])*"')


def _sanitize(text: str) -> str:
    """コメントと文字列/char/text-block リテラルの中身を空白化した同じ行数の文字列を返す（偽マッチ除外用）。

    改行は保持する（`line` 番号が元テキストの行番号と1対1になる）。
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
        if text[i:i + 3] == '"""':  # text block（Java 15+）
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


def _sanitize_comments_only(text: str) -> str:
    """コメントだけを空白化し、文字列リテラルの中身は残す（設定キー参照の抽出用）。

    text block は本文を（改行維持で）空白化する（本文中の `getProperty("x")` を実在の参照と誤読しないため）。行数・改行位置は元テキストと1対1。
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
        if text[i:i + 3] == '"""':  # text block（Java 15+）
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


def _is_method_declaration(sanitized: str, m: re.Match) -> bool:
    """`getProperty(...)`/`getString(...)` の出現がメソッド宣言（呼び出しではない）か。

    レシーバ付きは宣言ではあり得ない。レシーバなしでも「直前に戻り値型」「`)` の直後が `{`／`throws`／`;`」の両方を満たし、引数リストの各エントリが宣言形（型＋変数名）の場合だけ宣言とみなす。
    """
    if m.group("recv") is not None:
        return False
    before = sanitized[max(0, m.start() - 80):m.start()]
    if not _DECL_RETURN_TYPE_BEFORE.search(before):
        return False
    after = sanitized[m.end():m.end() + 20]
    if not _DECL_AFTER_PARENS.match(after):
        return False
    # `return getProperty(KEY)` のように `return`/`throw` が戻り値型に見える呼び出しを除くため、引数リストが全て宣言形であることも要求する。
    args = (m.group("arg") or "").strip()
    if not args:
        return True
    return all(_PARAM_ENTRY_TYPE.match(a.strip()) for a in _split_top_level_commas(args))


def _quoted_string_spans(text: str) -> list:
    """通常の `"..."`／`'...'` リテラルの `(start, end)` 区間を開始位置の昇順で返す（`getBean` 等の候補が文字列の中身にある場合に除外する位置チェック用）。text block は `_sanitize_comments_only()` で空白化済みなので含めない。"""
    spans: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text[i:i + 2] == "/*":
            i += 2
            while i < n and text[i:i + 2] != "*/":
                i += 1
            if i < n:
                i += 2
            continue
        if ch == "/" and text[i:i + 2] == "//":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i:i + 3] == '"""':
            i += 3
            while i < n and text[i:i + 3] != '"""':
                i += 1
            if i < n:
                i += 3
            continue
        if ch == '"' or ch == "'":
            start = i
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
            if i < n:
                i += 1
            spans.append((start, i))
            continue
        i += 1
    return spans


def _in_quoted_string(spans: list, starts: list, pos: int) -> bool:
    """`pos` が `_quoted_string_spans()` の区間に含まれるか（`starts` は各区間の開始位置・二分探索）。"""
    idx = bisect.bisect_right(starts, pos) - 1
    if idx < 0:
        return False
    start, end = spans[idx]
    return start <= pos < end


def _collect_config_key_refs(text: str) -> tuple:
    """`@Value`/`getProperty`/`getString`/`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` から設定キー参照候補を返す（`(refs, dropped)`）。

    - 1つの `@Value` 文字列に複数の `${key}` があれば全部抽出する。SpEL は `Dropped("config_spel")`。
    - 引数が文字列リテラルでない呼び出しは `Dropped("config_nonliteral")`。ただし文字列/char リテラルの中身に現れた候補は黙って除外する。
    - `key_kind`: `@Value`/`getProperty`/`getString` は `"property"`、`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` は `"bean"`。
    - 同じ `(key, key_kind)` は最初の出現行のみ1回にまとめる。
    - `@ConfigurationProperties(prefix=...)` は接続せず `dropped` に申告する。
    """
    sanitized = _sanitize_comments_only(text)
    string_spans = _quoted_string_spans(text)
    string_starts = [s for s, _ in string_spans]
    hits: list = []
    dropped: list = []

    for m in _VALUE_CALL.finditer(sanitized):
        content = m.group("content")
        if _SPEL_MARKER in content:
            snippet = content[2:-1] if content.startswith("#{") and content.endswith("}") else content
            dropped.append(Dropped("config_spel", _line_at(sanitized, m.start()), snippet[:120]))
            continue
        for pm in _VALUE_PLACEHOLDER.finditer(content):
            key = pm.group("key")
            if re.fullmatch(_CONFIG_KEY, key):
                hits.append((m.start(), key, "property"))

    for pattern in (_GET_PROPERTY_CALL, _GET_STRING_CALL, _GET_BEAN_CALL):
        key_kind = "bean" if pattern is _GET_BEAN_CALL else "property"
        for m in pattern.finditer(sanitized):
            if _in_quoted_string(string_spans, string_starts, m.start()):
                continue  # 文字列/char リテラルの中身（実コードではない）
            if _is_method_declaration(sanitized, m):
                continue  # メソッド宣言（例: `String getProperty(String key) { ... }`）
            arg = m.group("arg").strip()
            if _STRING_LITERAL_ARG.match(arg):
                key = arg[1:-1]
                if re.fullmatch(_CONFIG_KEY, key):
                    hits.append((m.start(), key, key_kind))
                continue
            if arg:
                dropped.append(Dropped("config_nonliteral", _line_at(sanitized, m.start()), arg[:120]))

    for pattern in (_QUALIFIER_ANNOTATION, _NAMED_ANNOTATION, _RESOURCE_NAME_ANNOTATION):
        for m in pattern.finditer(sanitized):
            hits.append((m.start(), m.group("key"), "bean"))

    hits.sort(key=lambda h: h[0])

    refs: list = []
    seen: set = set()
    for pos, key, key_kind in hits:
        if (key, key_kind) in seen:
            continue
        seen.add((key, key_kind))
        refs.append(RefCandidate("ACCESSES", "Config", key, _line_at(sanitized, pos),
                                 extra={"via": "config_key", "key_kind": key_kind}))

    dropped.extend(Dropped("config_prefix", _line_at(sanitized, m.start()), m.group("prefix"))
                   for m in _CONFIGURATION_PROPERTIES_PREFIX.finditer(sanitized))
    return refs, dropped


def _extract_mapping_paths(args: str) -> list:
    """`@GetMapping`/`@RequestMapping` 等の引数から URL パスのリストを返す（`value=`/`path=` または裸の引数・単一文字列／配列に対応。属性なしは空リスト）。"""
    args = args.strip()
    if not args:
        return []
    am = _MAPPING_PATH_ATTR.search(args)
    val = am.group("val") if am else (args if args.startswith(("{", '"')) else None)
    if not val:
        return []
    if val.startswith("{"):
        return [m.group(0)[1:-1] for m in _QUOTED_STRING_ITER.finditer(val)]
    m = _QUOTED_STRING_ITER.match(val)
    return [m.group(0)[1:-1]] if m else []


def _join_mapping_path(prefix: str, path: str) -> str:
    """クラスレベル prefix とメソッドレベル path を連結する（重複スラッシュを畳む）。"""
    if not path:
        return prefix
    if not prefix:
        return path if path.startswith("/") else f"/{path}"
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def _leading_mapping_class_prefixes(comments_only_lines: list, decl_line_no: int) -> list:
    """`decl_line_no`（1-based・型宣言の行）の直前に連続するアノテーション専用行から、クラスレベル `@RequestMapping` の prefix 一覧を返す（配列は全件・単一は1件・無ければ `[""]`）。"""
    i = decl_line_no - 2  # 直前行の 0-based index
    while i >= 0:
        stripped = comments_only_lines[i].strip()
        if not stripped:
            i -= 1
            continue
        m = _ANNOTATION_ONLY_LINE_ANY_ARGS.match(stripped)
        if not m:
            break
        if m.group("name") == "RequestMapping":
            paths = _extract_mapping_paths(m.group("args") or "")
            if paths:
                return paths
        i -= 1
    return [""]


def _collect_url_config_children(comments_only: str, class_prefixes: list) -> list:
    """クラス直下（brace 深度1）のマッピング注釈専用行から URL キー `Config` children を返す（`cid_key="key:url:"+URL`・`key_kind="url"`）。クラスレベル prefix が複数なら各 prefix とメソッドレベルパスの直積を返す。メソッド宣言と同一行の注釈は対象外。"""
    children: list = []
    depth = 0
    for i, raw_line in enumerate(comments_only.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        stripped = raw_line.strip()
        if not stripped or line_depth != 1:
            continue
        m = _ANNOTATION_ONLY_LINE_ANY_ARGS.match(stripped)
        if not m or m.group("name") not in _MAPPING_ANNOTATION_NAMES:
            continue
        paths = _extract_mapping_paths(m.group("args") or "") or [""]
        for class_prefix in class_prefixes:
            for path in paths:
                full = _join_mapping_path(class_prefix, path)
                if not full:
                    continue
                children.append(DefItem(label="Config", name=full, line=i,
                                        cid_key=f"key:url:{full}",
                                        extra={"key_kind": "url"}))
    return children


def _strip_generics(s: str) -> str:
    """balanced `<...>` を除去する（ネスト対応・不整合はそのまま残す）。"""
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
    """行から最初の「`識別子(`」（`new` を除く）を探し、対応する `)` までの中身を返す。メソッド/コンストラクタのシグネチャ行を引数リストへ分解する用途。`)` が同一行に無い（複数行）場合は `None`。"""
    for m in _METHOD_NAME_PAREN.finditer(line):
        if m.group(1) == "new":
            continue
        pre = line[:m.start()].rstrip()
        if pre.endswith("new") and (len(pre) == 3 or not pre[-4].isalnum()):
            continue
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

    JDK 頻出型・小文字始まり（プリミティブ/変数名紛れ）は候補にしない。ジェネリクスは1段まで（`_strip_generics`）。
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
            # ジェネリクス型引数は常に field_type（inject 格上げは宣言型本体のみ）。
            refs.append(RefCandidate("INVOKES", "Module", simple_arg, line, extra={"via": "field_type"}))


def _collect_declared_type_refs(sanitized: str) -> list:
    """フィールド宣言・コンストラクタ引数・メソッド引数の宣言型を参照候補として抽出する。

    トップレベル型の直下（brace 深度1）に限り、メソッド本体内のローカル変数は対象にしない。直前が `@Autowired`/`@Inject`/`@Resource` のみの行ならフィールドを `via=inject` へ格上げする（他の行を挟んだらリセット）。
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
    """波括弧深度0の型宣言を `(match, line, is_public)` で返す。深度>0（内部クラス等）は `nested` として別途返す（`Dropped` の材料）。"""
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
    """型宣言の直後からボディ開始 `{` 直前までのヘッダ文字列（`extends`/`implements` 節を含み得る・複数行可・上限あり）。"""
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

        # primary＝最初の public 型。public が無ければ最初の型宣言を primary にする（ノードを黙って消さない）。
        primary_idx = next((i for i, (_m, _l, pub) in enumerate(top) if pub), 0)
        primary_m, primary_line, _pub = top[primary_idx]
        primary_name = primary_m.group(1)

        pm = _PACKAGE.search(sanitized)
        package = pm.group(1) if pm else None
        qualified = f"{package}.{primary_name}" if package else primary_name
        imports = [m.group(1) for m in _IMPORT.finditer(sanitized)]

        extra = {}
        if package:
            extra["qualified_name"] = qualified  # cid_key は現行 world_graph では
        if imports:  # primary に対し未消費
            extra["imports"] = imports
        primary = DefItem(label="Module", name=primary_name, cid_key=qualified, extra=extra)

        children = [DefItem(label="Module", name=m.group(1), line=line)
                    for i, (m, line, _pub) in enumerate(top) if i != primary_idx]

        # URL キー定義側: クラスレベル @RequestMapping の prefix とメソッドレベルのマッピング注釈を連結し、`Config` children として返す。
        comments_only_lines = _sanitize_comments_only(text).split("\n")
        class_prefixes = _leading_mapping_class_prefixes(comments_only_lines, primary_line)
        children.extend(_collect_url_config_children("\n".join(comments_only_lines), class_prefixes))

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

        # 宣言型参照（フィールド/コンストラクタ引数/メソッド引数）。
        refs.extend(_collect_declared_type_refs(sanitized))

        # 設定キー参照。文字列リテラルの中身を読むため、`sanitized` ではなく元テキストを別途サニタイズして走査する。
        config_refs, config_dropped = _collect_config_key_refs(text)
        refs.extend(config_refs)

        # nested type は collect_defs 側で記録済み（二重記録しない）。
        return RefResult(refs=refs, dropped=config_dropped)
