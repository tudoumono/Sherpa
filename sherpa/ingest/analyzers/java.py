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

**設定キー参照（S3'・A7 案B）**: `@Value("${k}")`（デフォルト値 `@Value("${k:default}")` は
デフォルト部分を捨てる・`@Value("${a}-${b}")` のように1つの文字列に複数現れれば分解できる限り
全部抽出する）・`getProperty("k")`（レシーバ有無を問わない・`env.getProperty(...)`/
`System.getProperty(...)` も同形）・`getString("k")`・`getBean("k")`（レシーバ有無を問わない・
`ctx.getBean(...)` も同形・Spring の `BeanFactory`/`ApplicationContext` 前提）の文字列リテラル、
および `@Qualifier("k")`／`@Qualifier(value="k")`／`@Named("k")`／`@Named(value="k")`／
`@Resource(name="k")` の属性値から設定キーを
`RefCandidate("ACCESSES", "Config", key, line, extra={"via": "config_key", "key_kind": ...})` として
返す（共通層の A9＝同一 top_scope 内の同名 `Config` キー全件へ接続——XML アナライザの
`<bean id="k">` children とも同じキー空間で突合する）。`extra["key_kind"]`（Config キーは種別で
名前空間を分ける裁定・2026-09-06）は `@Value`/`getProperty`/`getString` が `"property"`、
`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` が `"bean"`——共通層の索引はラベルに加えて
この種別でも引くため、たまたま同じ裸キー文字列を持つ bean と property が誤って同一視されない。
`@Autowired` は文字列引数を持たない注釈のため対象外（既存の DI 注釈による `via=inject` 格上げの
み・本抽出とは別軸）。`@Value("#{...}")`（SpEL 式）は分解せず `Dropped("config_spel", ...)` として
申告するだけ（推測接続はしない）。`getProperty(KEY_CONST)`／`getString(var)`／`getBean(var)` の
ような非リテラル引数も同様に黙って無視せず `Dropped("config_nonliteral", ...)` として申告する。
`getBean`/`getProperty`/`getString` の呼び出し候補自体が通常の文字列/char リテラルの中身
（例: `String s = "getBean(k)";`）に現れた場合は実際のコードではないため、参照にも `Dropped` にも
せず黙って除外する（`_quoted_string_spans()` で位置チェックする・text block の中身は
`_sanitize_comments_only()` が既に空白化済みのため自然に除外される）。`@Qualifier`/`@Named`/
`@Resource(name=...)` は非リテラル形（定数参照等）を検知する構文を持たない——文字列リテラル形に
一致しなければ黙って抽出対象外にするだけ（`@Value` の非リテラル値と同じ既存の扱いに揃える）。
`@ConfigurationProperties(prefix="p")` の `prefix` は接続せず `Dropped("config_prefix", ...)` として
申告するだけ（プレフィックス自体はキーではない・推測接続はしない）。抽出対象のキーは
`[A-Za-z0-9_.\\-]+`（識別子形）のみ——この走査は `_sanitize()`（文字列内容を空白化）ではなく、
コメントと text block だけを除去し文字列リテラルは温存する別のサニタイズ
（`_sanitize_comments_only()`）に対して行う（既存の宣言型参照/呼び出し抽出はこれまでどおり
`_sanitize()` の結果を使い、挙動を変えない）。同じ `(key, key_kind)` が複数回現れても
`RefCandidate` は1回（最初に出現した行）にまとめる。

**URL キー定義側（波3 統合・Spring MVC）**: クラスレベルの `@RequestMapping("/x")`（`value=`/`path=`
属性形・配列 `{…}` 対応——配列なら prefix ごとに1本ずつ、メソッドレベルパスの各要素と**直積**で
連結する）と、メソッドレベルの `@RequestMapping`/`@GetMapping`/`@PostMapping`/
`@PutMapping`/`@DeleteMapping`/`@PatchMapping` を連結したパスを、primary の children として
`DefItem("Config", name=<URL パス>, cid_key="key:url:"+パス, extra={"key_kind": "url"})` で返す
（properties/xml_config と同じ `cid_key` 接頭辞規約・続く `key_kind` を挟む形も揃える・
`key_kind="url"` は JSP/HTML/JS の `action`/`href`/URL 文字列リテラル参照と同じ名前空間）。
注釈は自身の行だけで完結する形（引数を含め同一行内・複数行にまたがる形やメソッド宣言と同一行の
形は対象外）のみ検出する。
"""
from __future__ import annotations

import bisect
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

# 設定キー参照（S3'）: 識別子形のキーのみ（ドット/ハイフン区切りを許す）。
_CONFIG_KEY = r"[A-Za-z0-9_.\-]+"
# `@Value(...)` の文字列引数全体を捕捉する（中身は後段で `${...}` を全部抽出する・エスケープされた
# `\"` は文字列境界とみなさない）。
_VALUE_CALL = re.compile(r'@Value\s*\(\s*"(?P<content>(?:\\.|[^"\\])*)"\s*\)')
# `${key}`／`${key:default}`（デフォルト部分は捨てる）——1つの `@Value` 文字列に複数現れてもよい。
_VALUE_PLACEHOLDER = re.compile(r'\$\{(?P<key>[^}:]*)(?::[^}]*)?\}')
# SpEL（`#{...}`）は分解せず Dropped("config_spel") として申告する目印。
_SPEL_MARKER = "#{"
# レシーバの有無を問わない（`getProperty("k")`／`env.getProperty("k")`／`System.getProperty("k")`
# いずれも同形で拾う——レシーバは高々1段の単純な `識別子.` のみ許す）。引数はいったん丸ごと
# 捕捉し（`(...)` を跨がない範囲）、後段で「文字列リテラル1個だけか」を判定する——
# `getProperty(KEY_CONST)`／`getString(var)` のような非リテラル引数を黙って無視しないため。
# `recv` を named group にするのは、レシーバ有りならメソッド**宣言**ではあり得ないと
# 判定できるようにするため（`_is_method_declaration` 参照）。
_GET_PROPERTY_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getProperty\(\s*(?P<arg>[^()]*)\)')
_GET_STRING_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getString\(\s*(?P<arg>[^()]*)\)')
# `getBean("k")`（Spring の `BeanFactory`/`ApplicationContext` 前提・レシーバ有無を問わない）。
_GET_BEAN_CALL = re.compile(r'\b(?:(?P<recv>[A-Za-z_$][\w$]*)\.)?getBean\(\s*(?P<arg>[^()]*)\)')
_STRING_LITERAL_ARG = re.compile(r'^"(?:\\.|[^"\\])*"$')
# `@Qualifier("k")`／`@Qualifier(value="k")`／`@Named("k")`／`@Named(value="k")`／
# `@Resource(name="k")`（属性値が識別子形の文字列リテラルの場合のみ抽出——非リテラル形を検知する
# 構文は持たず、`@Value` の非リテラル値と同様に黙って抽出対象外にする）。
_QUALIFIER_ANNOTATION = re.compile(
    r'@Qualifier\(\s*(?:value\s*=\s*)?"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
_NAMED_ANNOTATION = re.compile(
    r'@Named\(\s*(?:value\s*=\s*)?"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
_RESOURCE_NAME_ANNOTATION = re.compile(
    r'@Resource\(\s*name\s*=\s*"(?P<key>' + _CONFIG_KEY + r')"\s*\)')
# メソッド**宣言**（`String getProperty(String key) { ... }` 等）を呼び出しと誤認しないための
# 3条件（`_is_method_declaration` が全部揃った時だけ宣言と判定する）。
_DECL_RETURN_TYPE_BEFORE = re.compile(r'[A-Za-z_$][\w$.\[\]<>]*\s+$')
_DECL_AFTER_PARENS = re.compile(r'^\s*(?:\{|throws\b|;)')
# `@ConfigurationProperties(prefix="p")` の prefix は接続しない（申告のみ・Dropped("config_prefix")）。
_CONFIGURATION_PROPERTIES_PREFIX = re.compile(
    r'@ConfigurationProperties\(\s*prefix\s*=\s*"(?P<prefix>' + _CONFIG_KEY + r')"\s*\)')

# URL キー定義側（波3 統合・Spring MVC のマッピング注釈）: `@RequestMapping`/`@GetMapping`/
# `@PostMapping`/`@PutMapping`/`@DeleteMapping`/`@PatchMapping` は自身の行だけで完結する
# アノテーション専用行（引数は同一行内・複数行にまたがる形は対象外）としてのみ検出する。
_MAPPING_ANNOTATION_NAMES = frozenset({
    "RequestMapping", "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping",
})
# アノテーション専用行（`@名前` または `@名前(引数)`・引数は `)` を含まない前提＝配列 `{...}` は
# 許容するが `)` を含む式は非対応）。クラスレベルの prefix 探索・メソッドレベルの検出の両方で使う。
_ANNOTATION_ONLY_LINE_ANY_ARGS = re.compile(r'^@(?P<name>[A-Za-z_$][\w$]*)(?:\s*\((?P<args>[^)]*)\))?\s*$')
# `value=`/`path=` 属性値（単一文字列、または `{"a", "b"}` の配列）。
_MAPPING_PATH_ATTR = re.compile(r'\b(?:value|path)\s*=\s*(?P<val>\{[^}]*\}|"(?:\\.|[^"\\])*")')
_QUOTED_STRING_ITER = re.compile(r'"(?:\\.|[^"\\])*"')


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


def _sanitize_comments_only(text: str) -> str:
    """コメント（`//`・`/* */`）だけを空白化し、文字列リテラルの中身はそのまま残す
    （設定キー参照抽出専用・`_sanitize()` と違い文字列内容を読む必要があるため）。

    text block（三連続の二重引用符で囲む複数行リテラル）は本文を（改行維持で）空白化する——
    `_sanitize()` と同じ扱い。text block の本文は複数行の説明文であることが多く、単純な
    引用符スキャンに任せると開始の三連続引用符を「空文字列＋新しい文字列の開始」と誤読し、
    本文中に書かれた `getProperty("x")` のような記述を実在の参照として拾ってしまう
    （黙って誤解釈することになる）ため、本文自体を設定キー抽出の走査対象から除外する。

    行数・改行位置は元テキストと1対1のまま保つ（`_line_at()` をそのまま再利用できる）。
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
    """`getProperty(...)`/`getString(...)` の出現がメソッド**宣言**（呼び出しではない）か。

    レシーバ付き（`env.getProperty(...)`）は宣言ではあり得ないため対象外。レシーバなしでも
    「直前に戻り値型（識別子・ジェネリクス・配列可）」「`)` の直後が `{`／`throws`／`;`」の
    2条件を**両方**満たす場合だけ宣言とみなす（例: `String getProperty(String key) { ... }`）。
    加えて引数リストの各エントリが宣言形（型＋変数名・注釈/varargs 可）であることを要求する
    （`return getProperty(KEY)` の `return` が戻り値型に見える呼び出しを除外するため）。
    呼び出し `getProperty(KEY_CONST)` は before/after のどちらも満たさないため引き続き
    `config_nonliteral` を申告する。
    """
    if m.group("recv") is not None:
        return False
    before = sanitized[max(0, m.start() - 80):m.start()]
    if not _DECL_RETURN_TYPE_BEFORE.search(before):
        return False
    after = sanitized[m.end():m.end() + 20]
    if not _DECL_AFTER_PARENS.match(after):
        return False
    # `return getProperty(KEY)` のように `return`/`throw` が「戻り値型」に見える呼び出しを
    # 宣言と誤認しないよう、引数リストが全て宣言形（型＋変数名）であることも要求する。
    args = (m.group("arg") or "").strip()
    if not args:
        return True
    return all(_PARAM_ENTRY_TYPE.match(a.strip()) for a in _split_top_level_commas(args))


def _quoted_string_spans(text: str) -> list:
    """`"..."`／`'...'`（通常の文字列/char リテラル）の `(start, end)` 区間を、開始位置の昇順で
    返す——`getBean`/`getProperty`/`getString` の呼び出し候補が、通常の文字列/char リテラルの
    **中身に書かれた記述**（例: `String example = "getBean(k)";`）を実際の呼び出しと誤認しない
    ための位置チェック専用（`_collect_config_key_refs` だけが使う）。text block（三連続引用符）は
    `_sanitize_comments_only()` 側で既に本文ごと空白化されているため対象に含めない——ここでは
    コメントと text block を読み飛ばすだけで、通常の引用符の対だけを区間として記録する。
    エスケープ扱いは `_sanitize()` と同じ。
    """
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
    """`pos` が `_quoted_string_spans()` の区間のいずれかに含まれるか（`starts` は各区間の開始位置
    だけを昇順で抜き出したもの・二分探索で候補区間を1つに絞る）。"""
    idx = bisect.bisect_right(starts, pos) - 1
    if idx < 0:
        return False
    start, end = spans[idx]
    return start <= pos < end


def _collect_config_key_refs(text: str) -> tuple:
    """`@Value`/`getProperty`/`getString`/`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)`
    から設定キー参照候補を返す（`(refs, dropped)`）。

    - `@Value("${a}-${b}")` のように1つの文字列に複数の `${key}` が現れる場合は分解できる限り
      全部抽出する（`${key:default}` はデフォルト部分を捨てる）。`@Value("#{...}")`（SpEL 式）は
      分解せず `Dropped("config_spel", ...)` として申告するだけ（推測接続はしない）。
    - `getProperty(KEY_CONST)`／`getString(var)`／`getBean(var)` のように引数が文字列リテラルで
      ない場合は黙って無視せず `Dropped("config_nonliteral", ...)` として申告する。ただし呼び出し
      候補自体が通常の文字列/char リテラルの中身（`_quoted_string_spans`）に現れた場合は、実際の
      コードではないため参照にも `Dropped` にもしない（黙って除外するだけ・申告不要）。
    - 種別（`extra["key_kind"]`・Config キーの名前空間分離）: `@Value`/`getProperty`/`getString`
      は `"property"`、`getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` は `"bean"`。
    同じ `(key, key_kind)` が複数回現れても `refs` は最初の出現行のみ1回にまとめる（出現順は
    ファイル内の物理位置順）。
    `@ConfigurationProperties(prefix=...)` は接続せず `dropped` に申告するだけ。
    """
    sanitized = _sanitize_comments_only(text)
    string_spans = _quoted_string_spans(text)
    string_starts = [s for s, _ in string_spans]
    hits: list = []                            # (pos, key, key_kind)
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
                continue                   # 文字列/char リテラルの中身（実コードではない）
            if _is_method_declaration(sanitized, m):
                continue                   # 宣言（例: `String getProperty(String key) { ... }`）
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
    """`@GetMapping`/`@RequestMapping` 等の引数から URL パスのリストを返す（`value=`/`path=` 属性、
    または裸の引数のいずれか・単一文字列／配列 `{"a", "b"}` の両方に対応・属性なしは空リスト）。"""
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
    """`decl_line_no`（1-based・型宣言自体の行）の直前に連続するアノテーション専用行から、
    クラスレベルの `@RequestMapping` の prefix 一覧を返す（配列 `{"/v1","/v2"}` は全件・
    単一文字列は1件・見つからなければ prefix なしを表す `[""]`）。
    """
    i = decl_line_no - 2                                       # 直前行の 0-based index
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
    """クラス直下（brace 深度=1）のマッピング注釈専用行から URL キー `Config` children を返す
    （`cid_key="key:url:"+URL`・`extra={"key_kind": "url"}`・properties/yaml/xml_config と同じ
    `cid_key` 接頭辞規約）。クラスレベル prefix が配列（複数）の場合は各 prefix とメソッドレベル
    パスの直積を1本ずつ返す（`{"/v1","/v2"}`×`/x` → `/v1/x`・`/v2/x`）。注釈は
    自身の行だけで完結する形（メソッド宣言と同一行の場合は対象外）のみ検出する——
    `_collect_declared_type_refs` と同じ brace 深度カウント手法を流用する。"""
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
        primary_m, primary_line, _pub = top[primary_idx]
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

        # URL キー定義側（波3 統合・Spring MVC マッピング注釈）: クラスレベル @RequestMapping の
        # prefix とメソッドレベルのマッピング注釈を連結し、`Config` children として返す。
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

        # 宣言型参照（フィールド/コンストラクタ引数/メソッド引数・JAVA-2＝フレームワーク非依存の
        # 一般抽出）。DI アノテーションが直前に付くフィールドは via=inject へ格上げ済み。
        refs.extend(_collect_declared_type_refs(sanitized))

        # 設定キー参照（S3'・A7 案B）。文字列リテラルの中身を読む必要があるため、上の
        # `sanitized`（文字列内容を空白化済み）ではなく元テキストを別途サニタイズして走査する。
        config_refs, config_dropped = _collect_config_key_refs(text)
        refs.extend(config_refs)

        # nested type（内部クラス）は collect_defs 側の dropped で既に記録済み——ここで
        # 二重記録しない。
        return RefResult(refs=refs, dropped=config_dropped)
