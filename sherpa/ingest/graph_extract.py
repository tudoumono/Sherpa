"""取り込みパイプライン向けの LLM 呼び出しの共通配管（プロバイダ選択・送信・秘密マスク）。

`available()`/`complete_json()`/`_probe` と、エラー詳細を外へ出す前のマスク
（`_mask_secrets`/`_redact_reflected_urls`/`_safe_detail`/`_log_masked_exception`）を持つ。
`intent_llm.py`・`ingest/llm_render.py`・`health.py`・`routers/system.py`・`providers/base.py`・`ext_api.py`・
`agentic_search.py`・`metering.py`・`doctor_checks.py` が共用する。
設計: docs/design/codex.md「頭脳の選択」
"""
from __future__ import annotations

import bisect
import json
import re
import urllib.error
from urllib.parse import quote, quote_plus

from .. import llm

_TIMEOUT = 90


def available(settings: dict | None = None, *, system_settings: dict | None = None,
             strict: bool = False, usage: str = "extract") -> dict | None:
    """取り込みパイプラインの LLM 設定を返す（無ければ None）。

    チャットの頭脳とは独立に、管理者が選んだクラウドプロバイダ（`sherpa.keys.selected_cloud_provider`）で解決する。
    クラウドを明示選択したのに解決できないときは Ollama へ倒さず未接続（`llm_unavailable`）にする
    （クラウド未選択のときだけ Ollama へ自動フォールバック・`llm.select_provider`）。OpenAI にはテキストだけ送る。
    個人設定のプロバイダ/モデルは読まない。

    `system_settings` は読み込み済みのスナップショットを渡すと、プロバイダ選択とモデル解決を同じ内容で行う。
    `strict` は `llm.select_provider(strict=...)` へ転送する（送信する呼び出し元は True）。
    `usage` は `model_catalog.resolve_model` のカタログ用途キー（`llm_render.py` は `"render"` を渡す）。
    """
    from .. import model_catalog, store as _store
    sys_s = system_settings if system_settings is not None else _store.get_system_settings()

    def O(key):
        return {"provider": "openai", "key": key,
                "model": model_catalog.resolve_model("openai", usage, None, system_settings=sys_s),
                # `complete_json` の送信時接続先解決も同じスナップショットで揃える
                "openai_endpoint_override": sys_s}

    def L(url):
        return {"provider": "ollama", "url": url,
                "model": model_catalog.resolve_model("ollama", usage, None, system_settings=sys_s)}

    return llm.select_provider(settings, openai=O, ollama=L,
                               system_settings=sys_s, strict=strict)


def complete_json(system: str, user: str, cfg: dict, timeout: int = _TIMEOUT) -> str:
    """1回の補完（JSON 文字列）を返す。OpenAI/Ollama 対応。テストはこの関数を差し替える。

    `timeout` は呼び元で短縮できる。レスポンスは `metering.acc_add` へ渡す（計測スコープが開いていなければ no-op）。
    """
    from .. import metering
    if cfg["provider"] == "openai":
        # `cfg["openai_endpoint_override"]` があれば、DB の system_settings の代わりにそれで接続先/ヘッダを組み立てる（接続テスト用・DB は書かない）
        _endpoint_override = cfg.get("openai_endpoint_override")
        # temperature は送らない（既定値以外を拒否するモデルがあるため）。
        # 送信は OpenAI 専用の送信直前ガード付きの `llm.openai_post_json` を通す
        resp = llm.openai_post_json(llm.openai_url("chat/completions", system_settings=_endpoint_override),
                             llm.openai_headers(cfg["key"], system_settings=_endpoint_override), {
            "model": cfg["model"],
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }, timeout)
        metering.acc_add(metering.usage_from_openai_chat(resp))
        return resp["choices"][0]["message"]["content"]
    resp = llm.post_json(llm.ollama_url(cfg["url"], "/api/chat"), llm.JSON_HEADERS, {
        "model": cfg["model"], "stream": False, "format": "json", "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, timeout)
    metering.acc_add(metering.usage_from_ollama_chat(resp))
    return resp["message"]["content"]


_DEPLOYMENT_NOT_FOUND_HINT = "（Azure OpenAI: モデル名の欄にはデプロイ名を入力してください）"

_HTTP_ERROR_BODY_MAX_BYTES = 65536   # 上流のエラー本文を読むバイト数の上限（64KiB）


def _http_detail(e: urllib.error.HTTPError,
                 system_settings: dict | None = None) -> tuple[str, str | None]:
    """OpenAI の HTTP エラー本文から code/status/message を抜いて短い理由にする。戻りは `(本文, 案内文または None)`。

    未マスク・未切断のまま返すので、呼び出し元は必ず `_safe_detail` を経由する（案内文は本文と別に返し、切断で落とさない）。
    Azure OpenAI で 404・`DeploymentNotFound` のときは「モデル名にはデプロイ名を入れてください」の案内を返す。
    `system_settings` は送信時と同じスナップショットを渡す（省略時はその場で DB を読む）。
    """
    try:
        # 上流のエラー本文サイズは信用せず、読み込みバイト数に上限を設ける
        err = json.loads(e.read(_HTTP_ERROR_BODY_MAX_BYTES)).get("error", {})
        code = err.get("code") or err.get("status") or err.get("type") or ""
        detail = f"{e.code} {code}: {err.get('message', '')}".strip()
    except Exception:
        return f"HTTP {e.code}", None
    try:
        _is_azure = llm.openai_endpoint_kind(system_settings) == "azure"
    except ValueError:
        # 設定値が破損していて判定できないときは案内文を付けない
        _is_azure = False
    hint = _DEPLOYMENT_NOT_FOUND_HINT if (
        e.code == 404 and str(code) == "DeploymentNotFound" and _is_azure) else None
    return detail, hint


def _error_detail(e: Exception) -> str:
    """`urllib.error.HTTPError` 以外の例外を、型名＋メッセージの短い理由文にする（未マスク・未切断・呼び出し元は `_safe_detail` を経由する）。

    メッセージは `_HTTP_ERROR_BODY_MAX_BYTES` で切り詰める。"""
    return f"{type(e).__name__}: {str(e)[:_HTTP_ERROR_BODY_MAX_BYTES]}"


# Bearer/api-key の値は区切り（空白・カンマ・セミコロン・引用符）で止める
_BEARER_RE = re.compile(r"Bearer\s+[^\s,;\"']+", re.IGNORECASE)
_API_KEY_HEADER_RE = re.compile(r"api-key[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]+", re.IGNORECASE)
_SK_TOKEN_RE = re.compile(r"sk-[A-Za-z0-9_-]{6,}")

# 上流がエラー本文へ要求 URL を echo することがあるため、エラー詳細の URL は一律で伏せる（fail-closed）。
#   0. 引用符区間の pre-pass（`_mask_quoted_url_spans`）: `'...'`/`"..."`/`「...」`/`『...』` の区間の中身に
#      URL 指標があれば区間全体を `[URL]` にする（空白入りの request-target の反射を単語分割より先に捕まえる）。
#   1. 空白区切りの単語ごとに、前後の引用符・括弧・句読点を本体から切り離してから指標判定する
#      （`_split_leading_punct`/`_split_trailing_punct_word`/`_pair_outer_brackets`）。
#      指標＝scheme（DSN を含む）・encoded scheme・`mailto:`/`data:`/`file:/`・protocol-relative・
#      URL 構造文字の percent-escape・多段の平文絶対パス（非空セグメント2個以上）。
#   2. 置換: percent-encoding を含まない `http(s)://` の URL だけ `llm._redact_url_for_error()` で `host[:port]` に縮約し、
#      それ以外は本体全体を `[URL]` にする。
# 空白のない日本語文中に URL が続くと文全体が消えることがある（漏洩防止を優先して許容する）。
_PLAIN_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)   # host[:port] へ縮約してよい scheme

# 指標判定用の汎用 scheme（RFC3986）。http/https 以外（DSN など）は host も残さず本体全体を `[URL]` にする
_GENERIC_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# `//` を伴わない scheme（`mailto:`/`data:`/`file:/`）の指標。明示列挙に限り、左境界（直前が `\w` でない）を要求する。
# 末尾句読点の切り離し前の `rest` に対して検索する（`_sub_word`）
_EXPLICIT_BODY_SCHEME_RE = re.compile(r"(?<!\w)(?:mailto|data):\S", re.IGNORECASE)
# `file:/path`（`/` 1個の短縮形）。`file://` は `_GENERIC_SCHEME_RE` が拾うので除く
_FILE_SCHEME_SINGLE_SLASH_RE = re.compile(r"(?<!\w)file:/(?!/)", re.IGNORECASE)

# `//host` 形（scheme 省略）。userinfo(`@`)・query(`?`)・fragment(`#`) を含む、または path 無しでドットを含むホスト単体を対象にする
_PROTOCOL_RELATIVE_MARKER_CHARS = "@?#"


def _is_protocol_relative_url(word: str) -> bool:
    """`word` が `//` で始まる protocol-relative URL の指標を持つか。`_PROTOCOL_RELATIVE_MARKER_CHARS` 参照。"""
    if not word.startswith("//") or len(word) <= 2:
        return False
    rest = word[2:]
    if any(c in rest for c in _PROTOCOL_RELATIVE_MARKER_CHARS):
        return True
    return "/" not in rest and "." in rest

_PCT = r"%(?:25)?[0-9A-Fa-f]{2}"   # percent-encoding の1バイト（`%XX`・二重エンコードの `%25XX` を含む）
_ANY_PCT_RE = re.compile(_PCT)   # 位置を問わない percent-encoding 判定（`_is_pure_plain_url`）
_ENC_COLON = r"(?::|%3A|%253A)"      # ":" 相当（平文／単純encoded／二重encoded）
_ENC_SLASH = r"(?:/|%2F|%252F)"      # "/" 相当（同上）
_ENCODED_SCHEME_RE = re.compile(rf"https?{_ENC_COLON}{_ENC_SLASH}{_ENC_SLASH}", re.IGNORECASE)

# URL 構造文字（`/`・`:`・`?`・`=`・`&`）の percent-escape を1個以上含むか（単純/二重encode・hex 大小混在）
_URL_PCT_INDICATOR_RE = re.compile(r"%(?:25)?(?:2F|3A|3F|3D|26)", re.IGNORECASE)

# 日付/割合表記（`2026%2F08%2F26` 等）は本体全体がこの形のときだけ指標から除外する
_DIGIT_ENCODED_SLASH_ONLY_RE = re.compile(r"^[0-9０-９]+(?:%2[Ff][0-9０-９]+)+$")

# scheme を伴わない多段の平文絶対パス（`/openai/deployments/x` 等・stdlib `InvalidURL` が反射する request-target）。
# 非空セグメント2個以上を指標とし、1個以下（`/tmp` 等）は対象外
_PLAIN_MULTI_SEGMENT_PATH_RE = re.compile(r"^/+[^\s/]+(?:/+[^\s/]+)+")

# 単語の前後で本体から切り離す文字（全角の引用符・開き括弧を含む）。切り離した側は地の文として残す
_LEADING_PUNCT = "'\"([{<" + "「『【（〈≪"
# 末尾に切り離す文字は閉じ括弧・閉じ引用符・文末記号に限る（`;`/`!`/`?`/`#` は URL データ側に現れうるため含めず、`]`/`}` は IPv6 リテラルと衝突するため含めない）
_TRAILING_PUNCT = ".,)'\"" + "。、」』】" + "）〉≫"

# 開き括弧を分離した場合、対応する閉じ括弧が本体末尾に残っていれば地の文側へ移す（`_pair_outer_brackets`）
_PAIRED_BRACKETS = {
    "(": ")", "[": "]", "{": "}",
    "「": "」", "『": "』", "【": "】", "（": "）", "〈": "〉", "≪": "≫",
}


def _word_has_url_indicator(word: str) -> bool:
    """`word`（前後の引用符・句読点を切り離した本体）に URL 指標があるか。伏せるかどうかの判定だけに使い、範囲は決めない。

    `file:/`・`mailto:`/`data:` は切り離し前の `rest` で検索する必要があるためここには含めない（`_sub_word`）。"""
    if _GENERIC_SCHEME_RE.search(word) or _ENCODED_SCHEME_RE.search(word):
        return True
    if _PLAIN_MULTI_SEGMENT_PATH_RE.search(word) or _is_protocol_relative_url(word):
        return True
    if _URL_PCT_INDICATOR_RE.search(word):
        return not _DIGIT_ENCODED_SLASH_ONLY_RE.match(word)
    return False


_QUOTE_BOUNDARY_CHARS = frozenset("'\"「」『』")


def _is_word_internal_quote_char(content: str, i: int) -> bool:
    """`content[i]` が `don't` のような語中の記号（区間境界ではない）か。ASCII の `'`/`"` で、直前直後がともに `\\w` のときだけ真。"""
    ch = content[i]
    if ch not in ("'", '"'):
        return False
    before = content[i - 1] if i > 0 else ""
    after = content[i + 1] if i + 1 < len(content) else ""
    return bool(_WORD_CHAR_RE.match(before)) and bool(_WORD_CHAR_RE.match(after))


def _split_on_quote_boundaries(content: str) -> list[str]:
    """`content` を空白と、語中でない引用符文字で区切って断片に分ける（区切りの引用符は断片に含めない）。

    貪欲な対付けで空白なしに隣接併合された区間の継ぎ目も断片境界にする。`team's` のような語中の引用符は割らない。"""
    frags: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(content):
        if ch.isspace():
            if buf:
                frags.append("".join(buf))
                buf = []
            continue
        if ch in _QUOTE_BOUNDARY_CHARS and not _is_word_internal_quote_char(content, i):
            if buf:
                frags.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        frags.append("".join(buf))
    return frags


def _dequote_for_indicator_scan(content: str) -> str:
    """`content` から引用符文字を削除した文字列を返す（指標検査専用で出力には使わない）。

    引用符が `%2''F` のように escape シーケンスの途中に紛れても、削除して連結すれば検出できる。"""
    return "".join(c for c in content if c not in _QUOTE_BOUNDARY_CHARS)


def _quoted_content_has_url_indicator(content: str) -> bool:
    """引用符区間の中身（空白を含みうる・句読点の切り離し前）に URL 指標があるか。

    ① `_word_has_url_indicator` と同じ指標に `file:/`・`mailto:`/`data:` を加え、区間全体に対して検索する。
    ② 多段の平文絶対パス・protocol-relative は、区間を空白・引用符境界で断片に分け前後の句読点を剥がして判定する。
    ③ 引用符を削除した連結文字列にも scheme・percent-escape の検査を重ねる（ヒットしたら区間全体を伏せる）。"""
    if (_GENERIC_SCHEME_RE.search(content) or _ENCODED_SCHEME_RE.search(content)
            or _FILE_SCHEME_SINGLE_SLASH_RE.search(content) or _EXPLICIT_BODY_SCHEME_RE.search(content)):
        return True
    if _URL_PCT_INDICATOR_RE.search(content) and not _DIGIT_ENCODED_SLASH_ONLY_RE.match(content):
        return True
    for tok in _split_on_quote_boundaries(content):
        _, tok_rest = _split_leading_punct(tok)
        tok_core, _ = _split_trailing_punct_word(tok_rest)
        if _PLAIN_MULTI_SEGMENT_PATH_RE.match(tok_core) or _is_protocol_relative_url(tok_core):
            return True
    dequoted = _dequote_for_indicator_scan(content)
    if dequoted != content:
        if (_GENERIC_SCHEME_RE.search(dequoted) or _ENCODED_SCHEME_RE.search(dequoted)
                or _FILE_SCHEME_SINGLE_SLASH_RE.search(dequoted) or _EXPLICIT_BODY_SCHEME_RE.search(dequoted)):
            return True
        if _URL_PCT_INDICATOR_RE.search(dequoted) and not _DIGIT_ENCODED_SLASH_ONLY_RE.match(dequoted):
            return True
    return False


# 引用符 pre-pass が対象にする対（開き → 閉じ）。ASCII の `'`/`"` は開閉が同一文字、全角の `「」`/`『』` は非対称
_QUOTE_PAIRS = {"'": "'", '"': '"', "「": "」", "『": "』"}

# 開き位置の制約（`_is_quote_opener_position`）が要るのは自己対称な ASCII 引用符だけ。全角の対は位置を問わず開きとみなす
_SYMMETRIC_QUOTE_CHARS = frozenset({"'", '"'})

# 引用符 pre-pass の入れ子深さの上限（`_mask_quoted_url_spans`）
_MAX_QUOTE_NESTING_DEPTH = 8

_WORD_CHAR_RE = re.compile(r"\w")


def _is_quote_opener_position(text: str, i: int) -> bool:
    """位置 i の引用符文字を開き引用符とみなせるか。

    自己対称な ASCII 引用符（`'`/`"`）は、行頭または直前が `\\w` でないときだけ開きとする（`don't` のアポストロフィを除くため）。
    全角の非対称対は常に真。"""
    if text[i] not in _SYMMETRIC_QUOTE_CHARS:
        return True
    return i == 0 or not _WORD_CHAR_RE.match(text[i - 1])


def _mask_quoted_url_spans(text: str) -> str:
    """text 内の `_QUOTE_PAIRS` で囲まれた区間を1単位として扱い、中身に URL 指標があれば区間全体を `[URL]` にする（引用符自体は残す）。

    `_redact_reflected_urls` の単語分割より先に呼ぶ（空白入りの request-target を分断させないため）。
    ① 内側の対から先に評価し、入れ子の内側だけにある指標も捕捉する。
    ② 開き位置は `_is_quote_opener_position` で判定し、閉じが見つからない開きは地の文のまま後続処理に委ねる。
    ③ 同種の引用符は直後にある最後の同種閉じ文字と対付けする（貪欲）。区間が広がる分は fail-closed で安全側に倒れる。
    ④ 入れ子は明示スタックで処理し（再帰しない）、深さは `_MAX_QUOTE_NESTING_DEPTH` で打ち切る（処理量を O(n) 近くに保つ）。
    """
    positions: dict[str, list[int]] = {}
    for idx, ch in enumerate(text):
        if ch in _QUOTE_PAIRS or ch in ("」", "』"):
            positions.setdefault(ch, []).append(idx)

    def _last_pos_in_range(ch: str, lo: int, hi: int) -> int:
        """ch の出現のうち半開区間 [lo, hi) 内で最後の位置。無ければ -1。"""
        lst = positions.get(ch)
        if not lst:
            return -1
        k = bisect.bisect_left(lst, hi) - 1
        if k >= 0 and lst[k] >= lo:
            return lst[k]
        return -1

    # 明示スタックでの反復処理（各フレーム＝[hi, pos, out, pending]）。`pending` は待っている子フレームの (ch, closer, j)
    n = len(text)
    stack: list[list] = [[n, 0, [], None]]
    child_result: str | None = None
    while True:
        hi, pos, out, pending = stack[-1]
        if child_result is not None:
            ch, closer, j = pending
            inner = child_result
            if inner and _quoted_content_has_url_indicator(inner):
                out.append(ch + "[URL]" + closer)
            else:
                out.append(ch + inner + closer)
            stack[-1][1] = j + 1
            stack[-1][3] = None
            child_result = None
            continue
        pushed_child = False
        while pos < hi:
            ch = text[pos]
            closer = _QUOTE_PAIRS.get(ch)
            # 入れ子段数は root フレームを除いて数える（`len(stack) - 1`）
            if (closer is not None and len(stack) - 1 < _MAX_QUOTE_NESTING_DEPTH
                    and _is_quote_opener_position(text, pos)):
                j = _last_pos_in_range(closer, pos + 1, hi)
                if j != -1:
                    stack[-1][1] = pos
                    stack[-1][3] = (ch, closer, j)
                    stack.append([j, pos + 1, [], None])
                    pushed_child = True
                    break
            if (closer is not None and len(stack) - 1 >= _MAX_QUOTE_NESTING_DEPTH
                    and _is_quote_opener_position(text, pos)
                    and _last_pos_in_range(closer, pos + 1, hi) != -1):
                # 上限に達したら、残り部分を1単位とみなして URL 指標があれば全体を `[URL]` にする（fail-closed）
                remainder = text[pos:hi]
                if _quoted_content_has_url_indicator(remainder):
                    out.append("[URL]")
                else:
                    out.append(remainder)
                pos = hi
                stack[-1][1] = pos
                break
            out.append(ch)
            pos += 1
            stack[-1][1] = pos
        if pushed_child:
            continue
        finished = "".join(out)
        stack.pop()
        if not stack:
            return finished
        child_result = finished


# 上流がキーを「先頭数文字＋アスタリスク列＋末尾数文字」に部分マスクして echo することがある。アスタリスク列を含むトークンは丸ごと伏せる
_MASKED_TOKEN_RE = re.compile(r"\S*\*{4,}\S*")

# `[REDACTED]` が2個以上連続している箇所の隣にある短い英数字トークンも伏せる（空白入りの部分マスク echo の残存断片対策）。
# 1個だけの場合は次の文の単語を巻き込むため適用しない
_TOKEN_AFTER_REDACTED_RE = re.compile(r"(\[REDACTED\](?:\s+\[REDACTED\])+)(\s+)\b[A-Za-z0-9]{1,7}\b")
_TOKEN_BEFORE_REDACTED_RE = re.compile(r"\b[A-Za-z0-9]{1,7}\b(\s+)(\[REDACTED\](?:\s+\[REDACTED\])+)")

_SECRET_FRAGMENT_MIN_LEN = 7   # secret の接頭辞/接尾辞断片マスクの最短長（短いと正当な文言に偶然一致する）

_DETAIL_MAX_LEN_HTTP = 400          # `_http_detail` 系の理由文の表示上限
_DETAIL_MAX_LEN_GENERIC = 300       # `_error_detail` 系の理由文の表示上限


def _mask_secret_fragments(text: str, secret: str) -> str:
    """`secret` の先頭 N 文字または末尾 N 文字（`N >= _SECRET_FRAGMENT_MIN_LEN`）と一致する断片を、見つかった長さすべてで伏せる（部分エコー対策）。"""
    n = len(secret)
    if n < _SECRET_FRAGMENT_MIN_LEN:
        return text
    for length in range(n, _SECRET_FRAGMENT_MIN_LEN - 1, -1):
        frag = secret[:length]
        if frag in text:
            text = text.replace(frag, "[REDACTED]")
    for length in range(n, _SECRET_FRAGMENT_MIN_LEN - 1, -1):
        frag = secret[-length:]
        if frag in text:
            text = text.replace(frag, "[REDACTED]")
    return text


def _mask_tokens_adjacent_to_redaction(text: str) -> str:
    """`[REDACTED]` が2個以上連続している箇所の直前/直後にある短い（7字以下の）英数字トークンも伏せる。"""
    text = _TOKEN_AFTER_REDACTED_RE.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)
    text = _TOKEN_BEFORE_REDACTED_RE.sub(lambda m: "[REDACTED]" + m.group(1) + m.group(2), text)
    return text


def _percent_encoding_insensitive_pattern(s: str) -> str:
    """percent-encoding 形の文字列から、各 `%XX` の16進2桁を大小文字不問で照合する正規表現パターンを組み立てる。

    上流が同じ値の中で大文字/小文字の16進を混在させて echo しても、1回の照合で一致させる。"""
    out = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == "%" and i + 2 < n and re.fullmatch(r"[0-9A-Fa-f]{2}", s[i + 1:i + 3]):
            h1, h2 = s[i + 1], s[i + 2]
            out.append(f"%[{h1.upper()}{h1.lower()}][{h2.upper()}{h2.lower()}]")
            i += 3
        else:
            out.append(re.escape(s[i]))
            i += 1
    return "".join(out)


def _mask_secrets(text: str, secret: str | None) -> str:
    """`text` から、実キー（`secret`・素の値と URL エンコード形・部分一致の断片）と一般的な秘密パターン
    （`Bearer <値>`・`api-key: <値>`・`sk-` 形式・アスタリスク列の部分マスクとその隣接断片）を伏せる。
    上流がヘッダやキーをエラー本文へ echo したときの最終防衛線
    """
    if not text:
        return text
    if secret:
        text = text.replace(secret, "[REDACTED]")
        for encoded in (quote(secret, safe=""), quote_plus(secret, safe="")):
            if encoded and encoded != secret:
                # URL エンコード形は大文字小文字の16進が混在して echo されうるため、大小文字不問で照合する
                text = re.sub(_percent_encoding_insensitive_pattern(encoded), "[REDACTED]", text)
        text = _mask_secret_fragments(text, secret)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _API_KEY_HEADER_RE.sub("api-key: [REDACTED]", text)
    text = _SK_TOKEN_RE.sub("[REDACTED]", text)
    text = _MASKED_TOKEN_RE.sub("[REDACTED]", text)
    text = _mask_tokens_adjacent_to_redaction(text)
    return text


def _split_leading_punct(word: str) -> tuple[str, str]:
    """`word` の先頭にある引用符・開き括弧（`_LEADING_PUNCT`）を本体から切り離す。"""
    end = 0
    while end < len(word) and word[end] in _LEADING_PUNCT:
        end += 1
    return word[:end], word[end:]


_TRAILING_PUNCT_MAX_LEN = 3   # これを超える末尾クラスタは句読点ではなく URL データの残骸とみなし、本体側に残す

_URL_DATA_TAIL_CHARS = "=?#&"   # 剥がした後の本体末尾がこれらなら、クラスタは URL データの一部とみなし剥がさない


def _split_trailing_punct_word(word: str) -> tuple[str, str]:
    """`word` の末尾にある句読点・閉じ括弧・引用符（`_TRAILING_PUNCT`）を本体から切り離す。

    次の2つのときは剥がさず本体側に残す: ① クラスタ長が `_TRAILING_PUNCT_MAX_LEN` を超える。
    ② 剥がした後の本体末尾が `_URL_DATA_TAIL_CHARS`。"""
    end = len(word)
    while end > 0 and word[end - 1] in _TRAILING_PUNCT:
        end -= 1
    if len(word) - end > _TRAILING_PUNCT_MAX_LEN:
        return word, ""
    if end > 0 and word[end - 1] in _URL_DATA_TAIL_CHARS:
        return word, ""
    return word[:end], word[end:]


def _is_pure_plain_url(core: str) -> bool:
    """`core` が percent-encoding を含まない純粋な平文 URL（`http(s)://` 始まり）か。

    真なら `llm._redact_url_for_error()` で `host[:port]` へ縮約してよい。percent-encoding を含むときは偽で、本体全体を `[URL]` にする。"""
    return bool(_PLAIN_SCHEME_RE.match(core)) and not _ANY_PCT_RE.search(core)


_WORD_RE = re.compile(r"\S+")


def _pair_outer_brackets(lead: str, core: str, trail: str) -> tuple[str, str, str]:
    """`lead` 末尾の開き括弧に対応する閉じ括弧が `core` の末尾に残っていれば `trail` 側へ移し、開き/閉じを対で地の文に保つ。"""
    if lead and core:
        closer = _PAIRED_BRACKETS.get(lead[-1])
        if closer is not None and core.endswith(closer):
            core = core[:-1]
            trail = closer + trail
    return lead, core, trail


def _redact_reflected_urls(text: str, base_url: str | None) -> str:
    """上流がエラー本文へ echo した URL を伏せる最終防衛線（`_mask_secrets` と同じ位置づけ）。

    送信時の base URL かどうかを区別せず一律で伏せる（診断価値より漏洩防止を優先する）。方式はモジュール内の URL 伏せ方のコメント参照。
    `base_url` は現状未使用だが、呼び出し側の契約（送信時のスナップショットを渡す）は維持する。
    """
    if not text:
        return text

    text = _mask_quoted_url_spans(text)

    def _sub_word(m: re.Match) -> str:
        word = m.group(0)
        lead, rest = _split_leading_punct(word)
        # `file:/`・`mailto:`/`data:` は末尾句読点の切り離し前の `rest` に対して検索する
        has_explicit_scheme = bool(
            _EXPLICIT_BODY_SCHEME_RE.search(rest) or _FILE_SCHEME_SINGLE_SLASH_RE.search(rest))
        core, trail = _split_trailing_punct_word(rest)
        lead, core, trail = _pair_outer_brackets(lead, core, trail)
        if has_explicit_scheme:
            # 常に丸ごと `[URL]` にする（host 縮約は http/https のみ）
            return lead + "[URL]" + trail
        if not core or not _word_has_url_indicator(core):
            return word
        if _is_pure_plain_url(core):
            replacement = llm._redact_url_for_error(core) or "[URL]"
        else:
            replacement = "[URL]"
        return lead + replacement + trail

    return _WORD_RE.sub(_sub_word, text)


def _log_masked_exception(log, context: str, e: BaseException, secret: str | None = None) -> None:
    """外部へ出さない元例外の型とマスク済みメッセージだけを WARNING ログへ残す共通ヘルパー。

    生の例外オブジェクト・文字列はログに出さず、`_mask_secrets`/`_redact_reflected_urls` を通した文字列だけを渡す。
    `secret` は実際に使った実キー（文字列以外は `str()` 化してマスク対象にする）。
    マスク処理自体が例外を投げても握り潰して固定のプレースホルダを残す（元の例外の `__context__` 経由で秘密が出るのを防ぐ）。
    """
    if secret is not None and not isinstance(secret, str):
        secret = str(secret)
    try:
        masked = _redact_reflected_urls(_mask_secrets(str(e), secret), None)
    except Exception:
        masked = "<masking failed>"
    log.warning("%s: %s: %s", context, type(e).__name__, masked)


def _safe_detail(e: Exception, *, secret: str | None = None,
                 system_settings: dict | None = None) -> str:
    """LLM 呼び出し失敗の例外を、利用者向けの安全な理由文にする。失敗理由を外へ返す箇所は必ずここを経由する（`_probe` が対象）。

    ① 先にマスク（`_mask_secrets`・`_redact_reflected_urls`）してから切断する（切断境界をまたぐ秘密を残さないため）。
    ② `_http_detail` の案内文は、本文を案内文の長さ分だけ切ってから連結し、必ず残す。
    `system_settings`（送信時と同じスナップショット・`cfg["openai_endpoint_override"]`）は 404 案内判定と反射 URL の base URL 計算に使う。
    `secret` が文字列でない場合は `str()` 化して使う。"""
    if secret is not None and not isinstance(secret, str):
        secret = str(secret)
    try:
        base_url = llm.openai_base_url(system_settings)
    except Exception:
        base_url = None
    if isinstance(e, urllib.error.HTTPError):
        base, hint = _http_detail(e, system_settings)
        base = _mask_secrets(base, secret)
        base = _redact_reflected_urls(base, base_url)
        if hint:
            reserved = max(_DETAIL_MAX_LEN_HTTP - len(hint), 0)
            return (base[:reserved] + hint)[:_DETAIL_MAX_LEN_HTTP]
        return base[:_DETAIL_MAX_LEN_HTTP]
    text = _error_detail(e)
    text = _mask_secrets(text, secret)
    text = _redact_reflected_urls(text, base_url)
    return text[:_DETAIL_MAX_LEN_GENERIC]


def _probe(cfg, timeout: int | None = None) -> tuple[bool, str]:
    """LLM に最小リクエストを1回だけ送り、到達性/認証/クォータを確認して `(ok, 理由)` を返す。

    失敗時は実エラー文を理由に乗せる（`_safe_detail` でキーをマスク）。
    `timeout` の省略時は `_TIMEOUT`。システム状態画面の再チェック（health.py）は短い値を指定して全体を止めない。
    """
    try:
        complete_json("Return a JSON object only.", 'Return {"ok":true}', cfg,
                     **({"timeout": timeout} if timeout is not None else {}))
        return True, ""
    except Exception as e:
        return False, _safe_detail(e, secret=cfg.get("key") or cfg.get("api_key"),
                                   system_settings=cfg.get("openai_endpoint_override"))
