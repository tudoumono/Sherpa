"""JSP アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §13 波3 レーン A・
ユーザー裁定 2026-09-06＝画面テンプレート/JS/CSS をグラフに載せ、画面↔Struts action／URL／bean の
突合を可能にする）。

`.jsp`/`.jspx`/`.jspf`/`.tag`/`.tagx` を**全件受理**する。ファイル自体を主体定義（`Module`・
primary・拡張子込みファイル名＝C アナライザと同型）とし、children は持たない（JSP は動的言語＋
テンプレート構文であり、ファイル内の要素を構造的な子定義として抽出できるものが無いため）。

**コメント除去**（JSP コメント `<%-- ... --%>`／HTML コメント `<!-- ... -->` の中身は
空白化してから走査する・偽マッチ除外）は一方向の線形スキャナで行う（閉じていないコメントは
末尾まで1回だけ空白化する——`.*?` の遅延regexは閉じタグが無い入力で二次時間になる）。

**タグ走査**は開始タグ（`<name attr=... attr2=...>`）だけを対象にした小さな字句解析で行う
（属性順不同・引用符省略・大文字タグ名を許容）。`<script>...</script>`／`<style>...</style>` の
本文区間はタグ走査の対象から外す（本文中に偶然 `<a href="...">` のような文字列があっても
参照を作らない）。

**参照抽出**:
- `<%@ include file="x.jspf" %>`（非タグ構文・個別regex）／`<jsp:include page="…">`／
  `<c:import url="…">`／`<jsp:directive.include file="…">`（`.jspx`）／`<script src="x.js">`／
  `<link href="x.css">` → `INVOKES(via=include)`（C アナライザと同じ2段解決＝相対パス完全一致→
  拡張子込み basename 最近傍。`world_graph._resolve_include_relpath()` 側）。
  - `http:`/`https:`/`//`/`data:`/`javascript:`/`mailto:`/`#` 始まりは include 対象外
    （`Dropped("web_external_ref", line, uri)`・URL キーにもしない）。
  - `/` 始まりの include は参照元の top scope（rel_path の最初のセグメント）へ連結し、
    参照元ファイルからの相対パスへ変換してから `include_path` にする（共通層
    `_resolve_include_relpath` は参照元相対の解決しか行わないため、生成側で変換する）。
  - `<base href="...">` があるファイルでは、それ以降の相対（`/` 始まりでも外部でもない）
    include は basename 最近傍へフォールバックさせず `Dropped("web_relative_under_base", line, path)`
    にする（`<base>` の影響範囲は解析しないため安全側でファイル全体に適用する・検出限界）。
    `<base href>` 自体は `Dropped("web_base_href", line)` を1件申告する。
  - `?`/`#` 以降は basename/`include_path` 算出前に除去する（`app.js?v=1` → `app.js`）。
- `<s:form action="login">`（`/`・`.` を含まない裸名）／`href="login.action"`（`.action` 拡張子）
  → Struts action キーへ `ACCESSES(via=config_key)`（名前は `.action` と先頭 `/` を落とした裸名）。
  **裸名判定は `action=`/`href=` 属性値の形だけで決める**（タグ名は問わない）——`href="orders"` の
  ような拡張子なし相対リンクも同じ形のため action キーと誤認し得る（構造情報を見ない粗い判定の
  裏返しの検出限界・意図的な簡素化）。判定前に `?`/`#` 以降を除去する。
- `action="/orders/list"`／`href="/orders/list"`（拡張子なし・`/` 始まり）→ URL キーへ
  `ACCESSES(via=config_key)`（名前はクエリ/フラグメントを除いたパスそのまま）。外部スキーム
  （`//` 始まり等）は対象外。
- `<jsp:useBean id="x" class="FQCN">` の `class` → `INVOKES(via=bean_class, qualified=True)`
  （完全修飾名解決＝`world_graph._resolve_qualified()`）。`id` 属性は対象外（bean キーへの突合は
  `<s:property value>` 等の値スタックが曖昧なため）。EL 式（`${bean.prop}`）も同じ理由で対象外
  ——値がどの bean を指すか構文だけでは決まらない（検出限界としてそのまま見逃す）。
- `<%@ page import="a.b.C" %>` はヒントのみ（エッジ化しない・Java の `import` と同じ扱い）。
- `<%@ taglib tagdir="/WEB-INF/tags" %>` はディレクトリ指定であり単一ファイルへの参照ではない
  ——エッジ化しない（`.tag`/`.tagx` ファイル自体は拡張子登録により通常どおり `Module` primary を
  持つ・タグ使用側との突合＝プレフィックス解決は本アナライザのスコープ外）。
- スクリプトレット `<% ... %>`（`<%@`/`<%=`/`<%--` を除く）内の Java コードは解析しない——
  ファイルにつき `Dropped("jsp_scriptlet", line)` を1件申告する（複数出現しても最初の1件のみ）。

大文字小文字は区別しない（タグ名・属性名とも小文字化して判定する・HTML/JSP の実務上の慣習）。
"""
from __future__ import annotations

import bisect
import posixpath
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

JSP_EXT = frozenset({".jsp", ".jspx", ".jspf", ".tag", ".tagx"})

# 外部参照スキーム（include 対象外・URL キーにもしない）。
_EXTERNAL_SCHEMES = ("http:", "https:", "//", "data:", "javascript:", "mailto:", "#")


def _is_external_ref(path: str) -> bool:
    return path.startswith(_EXTERNAL_SCHEMES)


def _strip_query_fragment(val: str) -> str:
    """`?`/`#` 以降を除去する（先に出現した方で切る・action判定/拡張子判定/basename取得の前段）。"""
    cut = len(val)
    for ch in ("?", "#"):
        idx = val.find(ch)
        if idx != -1 and idx < cut:
            cut = idx
    return val[:cut]


def _scope_relative_include_path(ref_rel: str, raw_path: str) -> str:
    """`/` 始まりの include を参照元の top scope（世代フォルダ）へ連結し、参照元ファイルからの
    相対パスに変換する（`raw_path` は先頭 `/` を含む・共通層 `world_graph._resolve_include_relpath`
    は参照元相対の解決しか行わないため、生成側でこの変換を行う）。"""
    stripped = raw_path.lstrip("/")
    if "/" not in ref_rel:
        return stripped
    top_scope = ref_rel.split("/", 1)[0]
    target = f"{top_scope}/{stripped}"
    base_dir = ref_rel.rsplit("/", 1)[0]
    return posixpath.relpath(target, start=base_dir)


# JSP コメント／HTML コメントの開始マーカー（一方向の線形スキャナで空白化・偽マッチ除外専用）。
_COMMENT_MARKER_RE = re.compile(r'<%--|<!--')


def _sanitize_comments(text: str) -> str:
    """`<%-- ... --%>`／`<!-- ... -->` を線形1パスで空白化する（改行は保持・閉じていない
    コメントは末尾まで1回で打ち切る——`.*?` の遅延regexは閉じタグが無い入力で二次時間になる）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        m = _COMMENT_MARKER_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        if m.group(0) == "<%--":
            end = text.find("--%>", m.end())
            close_len = 4
        else:
            end = text.find("-->", m.end())
            close_len = 3
        j = end + close_len if end != -1 else n
        out.append("".join("\n" if c == "\n" else " " for c in text[m.start():j]))
        i = j
    return "".join(out)


_INCLUDE_DIRECTIVE = re.compile(r'<%@\s*include\s+file\s*=\s*["\'](?P<path>[^"\']+)["\']')

# スクリプトレット（`<%@`/`<%=`/`<%--` を除く `<% ... %>`）。
_SCRIPTLET = re.compile(r'<%(?!@|=|--)(?P<body>.*?)%>', re.S)


def _newline_offsets(text: str) -> list:
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


_BARE_ACTION_NAME = re.compile(r'^[A-Za-z0-9_-]+$')


def _config_key_from_action_or_href(val: str) -> tuple[str, str] | None:
    """`action`/`href` 属性値から `(名前, 種別)` を返す（種別は `extra["key_kind"]` として
    `RefCandidate` にそのまま渡す・Config キーの名前空間分離）。

    `?`/`#` 以降は判定前に除去する（`/login.action?next=/` → action キー `login`・
    `/orders/list#tab` → URL キー `/orders/list`）。外部参照スキーム（`//` 始まり等）は対象外。
    `.action` 拡張子＝Struts action キー（拡張子と先頭 `/` を落とす）。`/` 始まり＋最終セグメントに
    `.` を含まない＝URL キー。`/`/`.` を含まない裸名（英数字/`_`/`-` のみ）＝Struts action キー
    そのまま（`<s:form action="login">` 形）。どれにも該当しなければ `None`
    （外部 URL・`#`・`javascript:...`・拡張子付き静的資産等は対象外）。
    """
    if not val:
        return None
    val = _strip_query_fragment(val)
    if not val or _is_external_ref(val):
        return None
    if val.endswith(".action"):
        bare = val[: -len(".action")]
        if bare.startswith("/"):
            bare = bare[1:]
        return (bare, "action") if bare else None
    if val.startswith("/"):
        last_seg = val.rsplit("/", 1)[-1]
        if "." not in last_seg:
            return val, "url"
        return None
    if _BARE_ACTION_NAME.match(val):
        return val, "action"
    return None


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list,
                  has_base: bool) -> None:
    """include 系参照候補1件の処理（外部参照除外・`/` 始まりの scope 相対化・`<base>` 配下の
    相対 include の抑止・basename 抽出）。"""
    path = raw_path.replace("\\", "/")
    if _is_external_ref(path):
        dropped.append(Dropped("web_external_ref", line, path))
        return
    path = _strip_query_fragment(path)
    if not path:
        return
    if path.startswith("/"):
        path = _scope_relative_include_path(ref_rel, path)
    elif has_base:
        dropped.append(Dropped("web_relative_under_base", line, path))
        return
    basename = PurePosixPath(path).name
    if basename:
        refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                 extra={"via": "include", "include_path": path}))


# --- 開始タグの字句解析（属性順不同・引用符省略・大文字タグ名を許容） -----------------------

# 開始/終了タグ本体（属性文字列は「引用符区間 or `>`/引用符以外の1文字」の繰り返しとして消費し、
# 属性値中の `>` に惑わされない）。
_TAG_RE = re.compile(
    r'<(?P<slash>/?)(?P<name>[A-Za-z][A-Za-z0-9:._-]*)(?P<attrs>(?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>'
)
_ATTR_RE = re.compile(
    r'([A-Za-z_:][A-Za-z0-9_:.-]*)'
    r'(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'=<>`]+)))?'
)
_SCRIPT_STYLE_OPEN_RE = re.compile(
    r'<(script|style)\b(?:"[^"]*"|\'[^\']*\'|[^>"\'])*>', re.I
)
_SCRIPT_CLOSE_RE = re.compile(r'</script\s*>', re.I)
_STYLE_CLOSE_RE = re.compile(r'</style\s*>', re.I)


def _parse_attrs(attrs_str: str) -> dict:
    out: dict = {}
    for m in _ATTR_RE.finditer(attrs_str):
        value = m.group(2)
        if value is None:
            value = m.group(3)
        if value is None:
            value = m.group(4)
        if value is not None:
            out[m.group(1).lower()] = value
    return out


def _script_style_body_spans(text: str) -> list:
    """`<script>...</script>`／`<style>...</style>` の本文区間（開始タグの直後〜対応する終了
    タグの直前）。属性走査の対象から外す（本文中の偽タグ／属性値誤検出を防ぐ）。

    終了タグ検索は `text[body_start:]` を切り出さず、コンパイル済みパターンの `search(text, pos)`
    に位置だけを渡す——スライスは残り全文のコピーを毎回作るため、`<script>`/`<style>` が多い
    ファイルで二次時間になる。"""
    spans: list = []
    pos, n = 0, len(text)
    while pos < n:
        m = _SCRIPT_STYLE_OPEN_RE.search(text, pos)
        if not m:
            break
        tag = m.group(1).lower()
        body_start = m.end()
        close_re = _SCRIPT_CLOSE_RE if tag == "script" else _STYLE_CLOSE_RE
        close_m = close_re.search(text, body_start)
        body_end = close_m.start() if close_m else n
        spans.append((body_start, body_end))
        pos = body_end
    return spans


class JspAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。children は持たない。
    include/script/link → `INVOKES(via=include)`。action/href の設定キー相当 → `ACCESSES
    (via=config_key)`。`useBean class` → `INVOKES(via=bean_class, qualified)`。"""

    name = "jsp"
    extensions = JSP_EXT
    doctype = "jsp"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize_comments(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        exclude_spans = _script_style_body_spans(sanitized)
        # `_TAG_RE.finditer` はタグ出現順（位置昇順）で返し、`exclude_spans` も左から右への
        # 線形走査で作られるため位置昇順——両方を単調ポインタで1回だけ突合する（タグごとに
        # `exclude_spans` 全件を再スキャンする二次時間を避ける）。
        tag_matches = []
        excl_idx, n_excl = 0, len(exclude_spans)
        for m in _TAG_RE.finditer(sanitized):
            pos = m.start()
            while excl_idx < n_excl and exclude_spans[excl_idx][1] <= pos:
                excl_idx += 1
            in_excluded = excl_idx < n_excl and exclude_spans[excl_idx][0] <= pos
            if m.group("slash") or in_excluded:
                continue
            tag_matches.append((pos, m.group("name").lower(), _parse_attrs(m.group("attrs"))))

        has_base = any(tag == "base" and "href" in attrs for _pos, tag, attrs in tag_matches)
        if has_base:
            base_pos = next(pos for pos, tag, attrs in tag_matches
                           if tag == "base" and "href" in attrs)
            dropped.append(Dropped("web_base_href", _line_at(newline_offsets, base_pos), ""))

        for m in _INCLUDE_DIRECTIVE.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            _emit_include(m.group("path"), line, rel_path, refs, dropped, has_base)

        for pos, tag, attrs in tag_matches:
            if tag == "base":
                continue
            line = _line_at(newline_offsets, pos)
            if tag == "script" and attrs.get("src"):
                _emit_include(attrs["src"], line, rel_path, refs, dropped, has_base)
            elif tag == "link" and attrs.get("href"):
                _emit_include(attrs["href"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:include" and attrs.get("page"):
                _emit_include(attrs["page"], line, rel_path, refs, dropped, has_base)
            elif tag == "c:import" and attrs.get("url"):
                _emit_include(attrs["url"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:directive.include" and attrs.get("file"):
                _emit_include(attrs["file"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:usebean" and attrs.get("class"):
                refs.append(RefCandidate("INVOKES", "Module", attrs["class"], line,
                                         extra={"via": "bean_class", "qualified": True}))

            for key_attr in ("action", "href"):
                if key_attr in attrs:
                    resolved = _config_key_from_action_or_href(attrs[key_attr])
                    if resolved is not None:
                        key, kind = resolved
                        refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                                 extra={"via": "config_key", "key_kind": kind}))

        first_scriptlet = _SCRIPTLET.search(sanitized)
        if first_scriptlet:
            line = _line_at(newline_offsets, first_scriptlet.start())
            lines = text.splitlines()
            snippet = lines[line - 1].strip()[:120] if line - 1 < len(lines) else ""
            dropped.append(Dropped("jsp_scriptlet", line, snippet))

        return RefResult(refs=refs, dropped=dropped)
