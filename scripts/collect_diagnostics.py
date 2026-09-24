#!/usr/bin/env python3
"""解析用の「ログ回収バンドル」を作る（`make diag`）。

第一契約: **機密情報を絶対に含めない**。ナレッジ情報（資料のファイル名・相対パス・登録フォルダの
パス・world のラベル・検索語）と回答本文は機密扱い——このバンドルの目的は精度確認ではなく
性能解析（速度・件数・失敗傾向）のため、内容の中身は要らない。

含めないもの（絶対）: `messages`（会話本文・タイトル・answer JSON）・`documents` の本文・
`users` の email/display_name/password_hash・`api_keys`・`audit_log` の本文行・個人 workspace
配下のファイル・登録フォルダ配下の資料そのもの・`data/derived` の中身（サイズ数値のみ）。環境設定
ファイル（`.env`）は秘密様キーを `<set>`/`<unset>`・URL/ホスト/識別子をハッシュした上で `dotenv.json` に
含める（値の平文は出さない）。

秘密様キー（大小無視で key/token/secret/password/passwd/pw/credential/auth を含む名前）の値は
`<set>`/`<unset>` に畳む。相対パス・登録フォルダの root・world の id/label は常に sha256 先頭12桁へ
ハッシュ化する（同じ入力は同じハッシュ＝ログと統計を突き合わせられる・生のパスは一切出さない）。
ログ本文の秘密パターン（`sk-`/Bearer/api-key ヘッダ/PEM鍵ブロック）は
`sherpa.ingest.graph_extract._mask_secrets`/`_redact_reflected_urls`・複数行にまたがる鍵ブロックは
`sherpa.redact_keys.KeyBlockRedactor` で伏せる。パス様の区間（絶対パス／区切り 2 つ以上／非 ASCII／
資料の拡張子／大文字だけの段／英語の前置き for・from・source・dir・-> の直後、のいずれかで始まる語から
名前の終わりまで）とラベル付きの欄の値は `<path:ハッシュ12桁>` に、検索語（`query=`/`q=`/「検索語」）は
`<masked>` に置換する（固定文言の 1 個の `/`＝`impact/run` 等は置換しない）。

出力直前に自己検査（fail-closed）を行う: `documents` 台帳の識別子列（name/scope_path/
original_path/md_path）と `worlds` の root/label を実 DB から読み、組み立てたバンドル本文
（JSON・ログ・レポートすべて）にそれらの文字列が1つでも残っていたら **tar を作らず** 非ゼロ終了する
（見つかった値そのものは表示しない・ファイル名と件数だけ）。DB が読めない場合もスキップせず
中止する（回避オプションは無い）。

収集元は1つが失敗しても他を続ける（失敗した収集先は `{"error": "<例外の型名>"}` だけを残す・
例外の文字列化はしない＝DSN 等に埋め込まれた秘密が例外メッセージ経由で漏れることを防ぐ）。

`.venv` の Python で動かす（`sherpa` パッケージを import する・標準ライブラリ＋既存モジュールのみ）。
収集は全て読み取り専用（DB の SELECT・ファイルの読み取り）。書くのは `--out` の tar.gz だけ。
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_SCRIPTS_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sherpa import es_index, redact_keys, store, worlds as worlds_mod  # noqa: E402
from sherpa.ingest import graph_extract, world_neo4j  # noqa: E402
from sherpa.store import db as db_mod  # noqa: E402
from sherpa.store.settings import _URL_SETTINGS_KEYS  # noqa: E402

from scripts import doctor_checks, log_report  # noqa: E402

# ---------------------------------------------------------------------------
# 伏せ字・ハッシュの共通部品
# ---------------------------------------------------------------------------

_SECRET_KEY_TOKENS = ("key", "token", "secret", "password", "passwd", "pw", "credential", "auth",
                      "dsn", "salt")   # 接続文字列（DSN）は password=… を含みうる・salt は逆引きの鍵になる
# 値の中に埋め込まれた認証情報（`password=…`・`pwd=…` 等の key=value 形）。
_INLINE_CRED_RE = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|sslkey|apikey|api_key)\s*=\s*\S+")


def _is_secret_key(name: str) -> bool:
    lname = name.lower()
    return any(tok in lname for tok in _SECRET_KEY_TOKENS)


def _secret_like_entry(name: str, value) -> bool:
    """畳むべき秘密様の項目か。キー名が秘密様でも、値が数値/真偽（トークン数等の計測値）や
    `*tokens`（利用統計のトークン集計）の入れ子は秘密ではない。"""
    if not _is_secret_key(name):
        return False
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    if name.lower().endswith("tokens"):
        return False
    return True


def _present(v) -> bool:
    return v not in (None, "", [], {})


def _hash_id(raw: str) -> str:
    """相対パス・world id/label/root を突合可能なまま伏せる（同じ入力は常に同じ12桁）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _strip_url_userinfo_query(v):
    """URL は scheme と port だけ残し、host（社内の IP/ホスト名）と path（デプロイ名等の内部識別子）は
    ハッシュに、userinfo（`user:pass@`）とクエリ/フラグメントは落とす。性能解析に要るのは接続先の
    種別（scheme/port）だけで、どのホストかは要らない。URL 形式でない文字列はそのまま返す。"""
    if not isinstance(v, str) or not _URL_SCHEME_RE.match(v):
        return v
    try:
        parts = urlsplit(v)
    except ValueError:
        return v
    host = parts.hostname or ""
    host_out = f"<host:{_hash_id(host)}>" if host else ""
    netloc = f"{host_out}:{parts.port}" if parts.port else host_out
    path = parts.path or ""
    path_out = f"/<path:{_hash_id(path)}>" if path and path != "/" else path
    return urlunsplit((parts.scheme, netloc, path_out, "", ""))


# 利用者の入力（検索語・質問文）が入りうる key=value。引用符付きは引用符ごと、引用符なしは
# 次の `key=` か行末までを丸ごと伏せる（空白入りの検索語の後半を残さない）。
_QUERY_KV_RE = re.compile(
    r"(?i)(\b(?:query|q|message|question|text)\b|検索語)(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|.+?)(?=\s+\w+=|$)", re.MULTILINE)   # 複数行文字列でも行ごとに効かせる
# 資料の相対パスが入る欄（`MD化を開始します: <rel>`・`rel=`・`doc=`・`path=` 等）は欄全体を
# ハッシュにする（空白入りの資料名を語ごとに分けない）。
# 末尾の計測注記は「計測項目だけ」を `・` で並べた全角括弧（`（12.3秒・RSS 0.1G→0.2G）`・`（rc=1）`）。
# 実在書式で資料名の後ろに付く注記は秒数と RSS だけ（`（rc=…）`・`（type=…）` は資料名より前に出る）＝`key=` 形は注記とみなさない
# （`rel=顧客資料（type=ACME）` は名前）。
_SPAN_NOTE_ITEM = (r"(?:[\d.,]+秒"
                   r"|RSS [\d.,]+\s*(?:MB|MiB|G|GiB)(?:→[\d.,]+\s*(?:MB|MiB|G|GiB))?)")   # `（%.1f秒%s）`＝秒と RSS だけ（`（30%）`・`（3件）` は名前）
_PATH_FIELD_RE = re.compile(
    r"((?:(?:ます|ました|ません|でした|失敗|完了|開始)(?:（[^）]*）)?|(?<!<)\b(?:rel|rel_path|doc|doc_id|path|file|original|source))"
    r"\s*[=:：]\s*)(.+?)(?=(?:\s*（" + _SPAN_NOTE_ITEM + r"(?:・" + _SPAN_NOTE_ITEM + r")*）)*$)", re.MULTILINE)
# 計測注記（`（%.1f秒%s）`・`（RSS …）`）を付ける実在書式は MD化 の開始/完了/失敗ログだけ。それ以外の行では欄の値を
# 行末まで名前とみなす（`顧客応答時間（30秒）` のような名前の末尾を注記と誤認しない）。
_PATH_FIELD_EOL_RE = re.compile(
    r"((?:(?:ます|ました|ません|でした|失敗|完了|開始)(?:（[^）]*）)?|(?<!<)\b(?:rel|rel_path|doc|doc_id|path|file|original|source))"
    r"\s*[=:：]\s*)(.+)$", re.MULTILINE)
_NOTE_FORMAT_RE = re.compile(r"^MD化")
# ↑ 欄の値は行末まで＝名前の中の ` key=`・`（RSS …）` では切らない（実在の書式で、資料名の欄の後に別の key= 欄が
#   続くものは無い）。分離するのは行末に一致する計測注記（`（1.2秒・RSS 0.1G→0.2G）`）だけ。
# ↑ 値が構造化欄の列（`…しました: world=… detail=…`）である欄は各専用マスク（world/uid 等）に任せる。
#   構造化欄と判定するのは、実在する書式（`world=… [run_id=…] [rel=…|detail=…]`・`key_id=… world=…`・
#   `action=… world=… rel=…`）に完全一致するときだけ（world/uid の値は後段の専用マスクが札にする）。
#   それ以外の `key=` 風の先頭（`type=極秘 一覧.xlsx`・`uid=x rc=ACME …`）は資料名として欄ごとハッシュする。
#   残余＝この 3 書式そのものを名乗る資料名は受容（現実的でない）。
_STRUCTURED_FIELD_RE = re.compile(
    r"^(?:(?:world|world_id|wid)=\S+|key_id=\S+\s+world=\S+|action=\S+\s+world=\S+)"
    r"(?:\s+[a-z_]+=\S+)*(?:\s+(?:rel|detail)=.*)?$")
# 構造化欄の末尾の `rel=`（登録フォルダの相対パス＝日本語だけの相対パスもある）は欄の中で値ごと伏せる。
# `detail=` は診断文（件数の dict 等）なので、非 ASCII かパス区切りを含むときだけ伏せる。
_STRUCTURED_TAIL_RE = re.compile(r"(\s+(rel|detail)=)(.*)$")
#   資料名として二重ハッシュしない（ログと統計の world ハッシュを突合できるように）。
# 末尾の全角括弧欄にパスが入る書式（`…に失敗しました（/mnt/kb/…/report.pdf）`）は括弧の中身全体をハッシュ。
_PAREN_PATH_RE = re.compile(r"（([^（）]*[/\\][^（）]*)）")
# ↑ ラベル直後の補足括弧（「（failed として継続）」）は欄に含めず、末尾の性能欄「（1.2秒・RSS …）」だけを
#   資料名から分離して残す（資料名内の括弧は欄に含める）。
# 利用者識別子（uid=…/user=…）は突合可能なハッシュにする。
_UID_KV_RE = re.compile(r"(\b(?:uid|user_id|user|owner|actor|by)\s*[=:]\s*)([^\s,:・)）\]]+)")


# パス様の語（`/`・`\\` を含む）から始まる区間（`… failed for run-abc/役員 極秘 一覧.pptx: type=…`・
# `control file /x/役員 一覧.xlsx`・`run_dir=/tmp/run 1/x`）。秘密パターン処理は `/` を含む語だけを `[URL]` に
# 潰して空白の後半を残すので、その前に区間全体を 1 つの `<path:…>` にする。名前の内側の括弧・数字・
# 空白は区切らない（名前の形は予測できない＝行末まで名前とみなす）。区間の終わりは `key=` の語・`->`・
# 引用符/山括弧/`|`・`: `（欄の区切り）だけで、末尾の計測注記（`（1.2秒）`・`（RSS 0.1G）`・`（rc=1）`）は残す。
# 数値の分数（`100/200`）と ASCII を含まない日本語の区切り（`エラー/警告`）は対象外。
_PATH_START_RE = re.compile(r"(?<![\w<>|])((?:\w+=)?)([^\s\"'<>|=・、（]*[/\\][^\s\"'<・、]*)")   # 区切りより前に `（` を含めない（`スキップ（html/.md` の固定文言）
# 引用符は、区間が開いた引用符の内側にあるとき（`'/x/役員 一覧/AGENTS.md'`・`"POST /api/chat HTTP/1.1" 200`＝
# 区間より前にその引用符が奇数個）だけ同じ引用符で終端する。それ以外は名前の中の引用符（`役員' 極秘 一覧`）で
# 切らず行末まで名前とみなす。
# 山括弧は収集器が生成した札（`<path:…>`・`<world:…>`・`<url:…>`・`<set>` 等）の開始だけを終端にする
# （生の `<極秘顧客>` は名前の一部）。`|` も名前の一部でありうる。
# 札は実際の形（ハッシュ 12 桁・`<url:scheme|`）に限る＝生の `<path:極秘>` は名前の一部。
_MASK_TAG = r"<(?:path|host|ip|world|uid|key|id|p):[0-9a-f]{12}>|<url:[A-Za-z][A-Za-z0-9+.\-]*\||<(?:set|unset|masked)>"
_MASK_TAG_RE = re.compile(_MASK_TAG)
# `->` が区切りなのは HTTP 状態（`/v1/x -> 200 (12.0ms)`）か札が続くときだけ（名前の中の ` -> ` では切らない＝
# `copy failed for <A> -> <B>: <例外>` は行末まで 1 つの名前として伏せる）。
_ARROW_RE = re.compile(r"\s+->\s(?=\d{3} \(\d[\d.]*ms\)|" + _MASK_TAG + r")")   # ext_api 行（本文が `ext_api ` で始まる）だけに適用
_COLON_SP_RE = re.compile(r":\s")
# 名前の後ろに欄を持つ実在書式の固定接頭辞（本文の行頭に錨＝名前の中の `failed for` では一致しない）。
# `codex_skills: copy failed for %s -> %s: %s` は例外文（%s）に対象パスが再掲されうるので対象外＝行末まで伏せる。
_TAIL_FORMAT_RE = re.compile(
    r"^(?:codex created file move/registration failed for |stale codex run dir cleanup failed for )")
_STRUCTURED_TAIL_AFTER_COLON_RE = re.compile(r"(?:[A-Za-z_][\w.\-]*=|[A-Za-z_][\w.]*(?:Error|Exception|Timeout|Warning)\b|\[Errno \d+\]|\[WinError \d+\]|HTTP \d{3}|\d{3}\b)")
# 引用符の内側は閉じ引用符まで名前。Python の例外表現（`'…\'s "x"'`）のエスケープ済み引用符 `\'` は閉じではない。
_PATH_SPAN_END_QUOTED_RE = {q: re.compile(_MASK_TAG + r"|(?<!\\)" + q + r"|$") for q in ("'", '"')}
# 欄の値（`ラベル: <パス>` の直後・行末が欄）は名前の中の `key=`・`: `・`->` で切らず行末まで（札の開始だけで終端）。
_PATH_SPAN_END_FIELD_RE = re.compile(_MASK_TAG + r"|$")
# `key=<パス>` の構造化行: パス系のキー（`rel=`・`file=`・`run_dir=` 等＝実在の書式では常に行末の欄）は
# 行末まで名前。それ以外（利用ログの `model=BAAI/bge-m3 in=52340 …`）は次の `key=` で終端する。
# `key=<パス>` として扱う実在のログキー（それ以外の `key=` は名前の一部）。
_KNOWN_KV_KEY_RE = re.compile(r"(?i)^(?:rel|rel_path|doc|doc_id|path|file|original|source|run_dir|root|dst|src|dir|model|world|world_id|wid|uid|user|detail|reason|err|target|from|to|stored)=$")
_PATH_KEY_PREFIX_RE = re.compile(r"(?i)^(?:rel|rel_path|doc|doc_id|path|file|original|source|run_dir|root|dst|src|dir)=$")
_PATH_SPAN_END_KV_RE = re.compile(r"\s+[A-Za-z_][\w.\-]*=|" + _MASK_TAG + r"|:\s|$")
_PATH_SPAN_NOTE_RE = re.compile(r"(?:\s*（" + _SPAN_NOTE_ITEM + r"(?:・" + _SPAN_NOTE_ITEM + r")*）)+$")


# 名前の始まりになりうる位置（英語の前置き・`: `・行頭からの最後のもの）と、名前の続きとみなせない文字。
_NAME_ANCHOR_RE = re.compile(r"(?:\bfor|\bfile|\bfrom|\bsource|\bdir|->)\s+|:\s+|：\s*")
# 後方拡張の打ち切りは収集器が生成した札だけ（生の山括弧・`|`・全角括弧・`: `・`=`・引用符は名前の一部でありうる。
# 実在の `key=<パス>` は区切り語に隣接するので prefix として別に扱われる）。
_HTTP_REQ_LEAD_RE = re.compile(r"[\"'](?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)$")
# 英語の前置きの直後が `key=` でなく名前の語で始まり、その後に `key=` が来る形（`control file ACME rel=…`）＝
# `key=` は名前の一部。前置き直後が `key=`（`for uid=… rel=…`）なら実在の構造化欄。
_LEAD_THEN_NAME_RE = re.compile(r"(?:\bfor|\bfile|\bfrom|\bsource|\bdir|->)\s+(?![A-Za-z_]+=).*\S\s+$")   # 名前の中の `: ` も許す
_JA_PATH_LEAD_RE = re.compile(r"(?:(?:\bfor|\bfile|\bfrom|\bsource|\bdir|->)\s+|:\s+|：\s*)$")   # `<ラベル>: <日本語だけの相対パス>` も対象（固定文言は閉集合で除外）
_PATH_LEAD_WORD_RE = re.compile(r"(?:\bfor|\bfile|\bfrom|\bsource|\bdir|->)\s+$")   # `control file <パス>` も前置き（`file move/registration` は過剰マスクを許容）


def _looks_like_path_start(head: str, before: str) -> bool:
    if head[:1] in "/\\~." or re.match(r"[A-Za-z]:[\\/]", head):
        return True
    if len(re.findall(r"[/\\]", head)) >= 2:
        return True
    if not head.isascii():
        return True
    if head.rsplit(".", 1)[-1].lower() in _PATH_EXTS and "." in head:
        return True
    if re.fullmatch(r"[A-Z0-9_\-]+(?:[/\\][A-Z0-9_\-]+)+", head):
        return True                              # 大文字だけの段（COBOL/JCL 系の `SRC/COMMON`）はパス
    return bool(_PATH_LEAD_WORD_RE.search(before))


# ログ固定文言に実在する `/` 入りの語の閉集合（sherpa/ 配下のログ文字列から採取・`AP/COMMON` 等の大文字パスは含めない）。
# 先頭一致＋以降に区切りが無いものだけ（`pdf/pptx-archive/x.pptx` のような実パスは対象外）。
_FIXED_SLASH_WORDING_RE = re.compile(
    r"(?:VLM/OCR|OCR/VLM|専用設定/一時上書き|non-dir/symlink|pdf/pptx|html/\.md|impact/run|troubleshoot/run|graph/search"
    r"|audit/bootstrap|RAG/Evidence|move/registration|DROP/CREATE|unchanged/unresolved|submit\(\)/start\(\)"
    r"|読込/検証|失敗/空|ローカル/私有)")
# 固定文言の除外は英語の前置き（`control file `・`failed for `）の直後には適用しない（そこは必ずパス引数）。
# 前置きの直後だけでなく、前置きから名前の語が続いている途中（`control file ACME VLM/OCR 極秘/…`）も同様。
# `<ラベル>: ` から名前の語が続いている途中（`symlink: ACME 2026/09 極秘/…`）。実在の書式で `: ` の直後に分数が来るものは無い。
_COLON_THEN_NAME_RE = re.compile(r"(?::\s+|：\s*)(?![A-Za-z_]+=)\S.*\s$")
_ENGLISH_LEAD_RE = re.compile(r"(?:\bfor|\bfile|\bfrom|\bsource|\bdir|->)\s+(?![A-Za-z_]+=)(?:.*\s)?$")   # 名前の中の `:` も許す


class _FixedWording:
    """固定文言の判定: 語頭一致でなく「語中に固定文言を含み、語中の区切りがその固定文言の 1 つだけ」
    （日本語ログは空白が無く `計画呼び出しが失敗/空のため縮退します` のように固定文言が語頭に来ない）。"""

    @staticmethod
    def fullmatch(word: str):
        if word.count("/") + word.count("\\") != 1:
            return None
        return _FIXED_SLASH_WORDING_RE.search(word)


_ACRONYM_PAIR_RE = _FixedWording


def _rest_has_path(rest: str) -> bool:
    """行の残りに、固定文言ではないパスと分かる語（`極秘/役員.png`）があるか（`html/.md` のような固定文言は数えない）。"""
    for mm in _PATH_START_RE.finditer(rest):
        tok = mm.group(2)
        if _ACRONYM_PAIR_RE.fullmatch(tok) or _NUMERIC_FRACTION_RE.fullmatch(tok):
            continue
        if not tok.isascii() or _looks_like_path_start(tok, ""):
            return True
    return False


def _mask_path_spans(text: str) -> str:
    out = []
    pos = 0
    while True:
        m = _PATH_START_RE.search(text, pos)
        if not m:
            break
        head = m.group(2)
        prefix = m.group(1)
        head_start = m.start(2)
        if prefix and (not _KNOWN_KV_KEY_RE.match(prefix) or _LEAD_THEN_NAME_RE.search(text[:m.start()])):
            # 実在のログキーでない `key=`（名前の先頭 `cfg=dept/役員: …`）や、英語の前置き直後の `key=`
            # （`control file ACME rel=…` は名前）は欄扱いしない
            prefix = ""
            head_start = m.start()
            head = text[head_start:m.end(2)]
        # 先頭語が数値の分数（`100/200`）や、最初の区切りより前に ASCII を含まない日本語の区切り
        # （`エラー/警告`・固定文言の `読込/検証に失敗しました（VLM`）なら対象外（日本語だけの相対パスは
        # ラベル付きの欄（_mask_path_fields）が先に欄ごと伏せる）。
        in_name = bool(_ENGLISH_LEAD_RE.search(text[:head_start])) or (
            bool(_COLON_THEN_NAME_RE.search(text[:head_start])) and _rest_has_path(text[m.end(2):]))
        fixed_wording = bool(_ACRONYM_PAIR_RE.fullmatch(head)) and not in_name   # 前置き/`: ` から続く名前の中の `VLM/OCR` は固定文言ではない
        if (_NUMERIC_FRACTION_RE.fullmatch(head.split("（", 1)[0].rstrip(_PATH_TRAIL_PUNCT + "）"))
                and not _ENGLISH_LEAD_RE.search(text[:head_start])
                and not (_COLON_THEN_NAME_RE.search(text[:head_start]) and _rest_has_path(text[m.end(2):]))):
            # 前置きから続く名前の中の `2026/09` は分数ではない。`: ` から続く場合は、後ろにさらに区切りがあるとき
            # （`symlink: ACME 2026/09 極秘/役員.png`）だけ名前とみなす（`embed 進捗 100/200 チャンク（…）` は分数）。
            out.append(text[pos:m.end()])       # 進捗 `100/200（3件）` の分数は対象外
            pos = m.end()
            continue
        first = re.split(r"[/\\]", head, 1)[0]   # 絶対パス（`/mnt/…`）は先頭が区切り＝first が空＝対象
        # 英語の前置き（`control file <パス>`・`failed for <パス>`）や `: ` の後ろから区切りを含む語までの間に
        # 空白入りの語（`極秘 案件/資料`＝先頭フォルダ名の空白）があれば、そこから名前とみなす。
        if not prefix and head_start > pos and not fixed_wording:
            anchors = list(_NAME_ANCHOR_RE.finditer(text, pos, head_start))
            # 拡張前の語か、拡張後の候補（前置きの直後から区切りを含む語まで＝`Shared Docs/Board`）がパスと分かる形のときだけ拡張
            # （固定文言の `non-dir/symlink` は拡張しない）。
            cand_ok = _looks_like_path_start(head, text[:head_start])
            if anchors and not cand_ok:
                leads_ = [x for x in anchors if not (x.group(0).startswith(":") or x.group(0).startswith("："))]
                lead_ = leads_[0] if leads_ else anchors[-1]
                cand = text[lead_.end():m.end(2)]
                cand_ok = bool(cand.strip()) and _looks_like_path_start(cand.strip(), text[:lead_.end()])
            if anchors and cand_ok:
                # 最初の前置き語（`control file ACME for 案件/…`・`control file ACME: 案件/…` の `file `）を名前の
                # 開始にする（実在書式では前置き語は名前より前に 1 つ＝名前の中の `for`/`: ` で開始位置を更新しない）。
                # 前置き語が無ければ最後の `: `。
                leads = [x for x in anchors if not (x.group(0).startswith(":") or x.group(0).startswith("："))]
                lead = leads[0] if leads else anchors[-1]
                between = text[lead.end():head_start]
                bt = between.strip()
                # 間の語が引用符で始まる（`denied: '役員 一覧/AGENTS.md'`）なら、名前は開き引用符の直後から（閉じ引用符まで）。
                # それ以外の引用符（`ACME'社外秘 案件/…`・`ACME社外秘' 案件/…`）は名前の一部。
                colon_anchor = lead.group(0).startswith((":", "："))
                if bt and bt[0] in "'\"" and colon_anchor and not _MASK_TAG_RE.search(between):
                    # `: ` の後の引用符だけが囲み（`denied: '役員 一覧/…'`）。前置き語の後の引用符（`control file 'ACME' 社外秘 …`）は名前の一部。
                    q_at = between.index(bt[0])
                    head_start = lead.end() + q_at + 1
                    head = text[head_start:m.end(2)]
                    first = head.split()[0] if head.split() else first
                elif (bt and not _MASK_TAG_RE.search(between)
                        and not _HTTP_REQ_LEAD_RE.search(bt)):   # access ログの `"POST /path HTTP/1.1"` は引用符内の要求行
                    head_start = lead.end()
                    head = text[head_start:m.end(2)]
                    first = between.split()[0]
        after_lead = bool(_JA_PATH_LEAD_RE.search(text[:head_start])) or text[head_start - 1:head_start] in ("'", '"')   # 開き引用符の直後も前置き
        if fixed_wording or (first and not after_lead and not any(
                ch.isascii() and (ch.isalnum() or ch == ".") for ch in first)):
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        before = text[:head_start]
        opening = next((q for q in ("'", '"') if before.count(q) % 2 == 1), "")
        if not opening and not prefix and not before.endswith((": ", "：")) and not _looks_like_path_start(head, before):
            # ラベル無し・引用符無し・`key=` 無しの区間は、固定文言の 1 個の `/`（`impact/run が…`・`RAG/Evidence IR`・
            # `move/registration`）から始めない。パスと分かる形（絶対パス・区切り 2 つ以上・非 ASCII・資料拡張子・
            # 直前が for/file/from/to/-> の英語の前置き）のときだけ区間にする。
            out.append(text[pos:m.end()])
            pos = m.end()
            continue
        if opening:
            end_re = _PATH_SPAN_END_QUOTED_RE[opening]
        elif prefix:
            end_re = _PATH_SPAN_END_FIELD_RE if _PATH_KEY_PREFIX_RE.match(prefix) else _PATH_SPAN_END_KV_RE
        elif before.endswith(": ") or before.endswith("：") or before.endswith("：  "):
            end_re = _PATH_SPAN_END_FIELD_RE
        else:
            end_re = None
        if end_re is not None:
            e = end_re.search(text, head_start)
            span_end = e.start() if e else len(text)
        else:
            # ラベル無しの区間（`… failed for <パス>: type=… errno=…`・`copy failed for <A> -> <B>: <例外>`）:
            # 名前の中の ` key=`・` -> `・`: ` では切らず、HTTP 状態へ続く `->` か、無ければ最後の `: `
            # （後続の構造化欄の直前）まで名前とみなす。札の開始はどの場合も終端。
            rest = text[head_start:]
            cut = len(rest)
            tag = _MASK_TAG_RE.search(rest)
            if tag:
                cut = tag.start()
            arrow = None
            if text.startswith("ext_api "):
                # ext_api 行: `-> 200 (12.0ms) request_id=…`／例外行 `-> unhandled exception request_id=…`
                arrow = _ARROW_RE.search(rest[:cut]) or re.search(r"\s+->\s(?=[a-z ]*request_id=)", rest[:cut])
            if arrow:
                cut = arrow.start()
            else:
                # 最後の `: ` で切るのは、その後ろが構造化欄（`type=… errno=…`）か例外表現（`OSError`・`[Errno 13] …`）
                # のときだけ。名前の中の `: `（`役員: 極秘 一覧`）では切らず、行末まで名前とみなす。
                # さらに、後続欄を実際に持つ実在の書式（`… failed for %s: type=%s errno=%s`・`copy failed for %s -> %s: %s`・
                # `(run_dir=%s): %s`・`gc_orphan: failed uid=%s file=%s: %s`）のときだけ切る。`control file %s` 等の
                # 行末が名前の書式では、名前の中の `: type=…` も名前の一部として行末まで伏せる。
                colons = [x.start() for x in _COLON_SP_RE.finditer(rest[:cut])]
                if colons and _TAIL_FORMAT_RE.match(text) and _STRUCTURED_TAIL_AFTER_COLON_RE.match(rest[colons[-1] + 2:cut]):
                    cut = colons[-1]
            span_end = head_start + cut
        span = text[head_start:span_end]
        note = _PATH_SPAN_NOTE_RE.search(span) if _NOTE_FORMAT_RE.match(text) else None
        if note:
            span = span[:note.start()]
        core = span.rstrip(_PATH_TRAIL_PUNCT)
        trail = span[len(core):]
        out.append(text[pos:head_start - len(prefix)] + prefix + f"<path:{_hash_id(core)}>" + trail)
        pos = head_start + len(span)
    out.append(text[pos:])
    return "".join(out)


_DOC_FIELD_HINT_RE = re.compile(r"MD化|legacy|office_com|Office|VLM|OCR|画像|原本|変換|読込|合流|観測|grep_search|es_search")


# 英語ラベルの直後から行末までがパス引数である実在書式の閉集合（sherpa/ 配下のログ文字列から採取）。
# 名前の中身（`: `・`key=`・引用符・分数・固定文言風の語）に依らず、ラベルの直後から行末（末尾の計測注記を除く）を
# 1 つの `<path:…>` にする。
_ENGLISH_PATH_LABEL_RE = re.compile(
    r"(asset inventory contains symlink: |symlink inside skill source rejected: |skip non-dir/symlink skill source: "
    r"|created-file save failure: |failed to (?:read|stat) control file |outside files_dir uid=\S+ rel=|symlink rejected uid=\S+ rel="
    r"|symlink rejected for uid=\S+ rel=|confined_path failed for uid=\S+ rel=)(.+)$")


# 接続先の値（`接続先（inference01）が…`＝vision_arm の拒否ログ）。URL でない単一ラベルのホストは他の判定に掛からないので
# 値を常にホストの札にする（既に札なら触らない）。
_CONNECT_TARGET_RE = re.compile(r"(接続先（)([^（）]+)(）)")


def _mask_connect_targets(text: str) -> str:
    def _sub(m: re.Match) -> str:
        v = m.group(2).strip()
        if _MASK_TAG_RE.match(v):
            return m.group(0)
        return m.group(1) + f"<host:{_hash_id(v)}>" + m.group(3)
    return _CONNECT_TARGET_RE.sub(_sub, text)


def _mask_english_path_labels(text: str) -> str:
    m = _ENGLISH_PATH_LABEL_RE.search(text)
    if not m or not m.group(2).strip():
        return text
    value = m.group(2)   # これらの書式に計測注記は無い＝末尾まで名前（`顧客資料（type=ACME）` も名前）
    if _MASK_TAG_RE.match(value.strip()):
        return text
    return text[:m.end(1)] + f"<path:{_hash_id(value.strip())}>"


def _mask_path_fields(text: str) -> str:
    def _field(m: re.Match) -> str:
        value = m.group(2)
        if _MASK_TAG_RE.match(value.strip()):
            return m.group(0)                 # 既に札（URL の縮約 `<url:…>` 等）＝欄ごと再ハッシュしない（接続先の種別を残す）
        # 英語の前置き（`control file ACME rel=…`）の直後の `key=` は名前の一部＝欄扱いせず区間処理（後方拡張）に任せる
        if _LEAD_THEN_NAME_RE.search(text[:m.start()]) and m.group(1).strip().rstrip("=:：").isascii():
            return m.group(0)
        # 資料名を出すログ（MD化/legacy/office_com/VLM/OCR…）の欄は、資料名が `key=` 風でも常に欄ごと伏せる
        if _STRUCTURED_FIELD_RE.match(value.strip()) and not _DOC_FIELD_HINT_RE.search(text[:m.start()]):
            def _tail(t: re.Match) -> str:
                v = t.group(3)
                if t.group(2) == "detail" and v.isascii() and "/" not in v and "\\" not in v:
                    return t.group(0)
                return t.group(1) + f"<path:{_hash_id(v.strip())}>"
            return m.group(1) + _STRUCTURED_TAIL_RE.sub(_tail, value, count=1)
        # `file=%s: %s`・`path=%s: %s` の例外欄も含めて行末まで伏せる（例外文に対象パスが再掲されるため分離しない＝
        # 失敗理由の可読性より漏えい防止を優先する受容記録）。
        return m.group(1) + f"<path:{_hash_id(value.strip())}>"
    return (_PATH_FIELD_RE if _NOTE_FORMAT_RE.match(text) else _PATH_FIELD_EOL_RE).sub(_field, text)


def _mask_paren_paths(text: str) -> str:
    """末尾の全角括弧欄 `（/path/…）`。world/uid 等の専用マスクの後に掛ける（`（world=…・/x/y.json）` の world を先に落とす）。"""
    def _sub(m: re.Match) -> str:
        inner = m.group(1)
        # 区切りが数値の分数（`試行 2/3`・`100/200`）だけの括弧はパスではない（再試行回数・進捗を残す）
        if "/" not in _NUMERIC_FRACTION_RE.sub("", inner) and "\\" not in inner:
            return m.group(0)
        return f"（<path:{_hash_id(inner.strip())}>）"
    return _PAREN_PATH_RE.sub(_sub, text)


def _mask_uid_values(text: str) -> str:
    return _UID_KV_RE.sub(lambda m: m.group(1) + f"<uid:{_hash_id(m.group(2))}>", text)


# repr/JSON 形の識別子欄（例外文字列に flags の dict がそのまま載るログ）: `'name': 'CUSTMAST-REC'`。
_REPR_ID_RE = re.compile(
    r"(['\"])(name|from|doc|rel|rel_path|path|file|target|source|world|world_id|label|root)\1(\s*:\s*)(['\"])(.*?)\4")


def _mask_repr_identifiers(text: str) -> str:
    return _REPR_ID_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(1)}{m.group(3)}{m.group(4)}<id:{_hash_id(m.group(5))}>{m.group(4)}", text)


# 失敗理由の `例外型@接続先:port`（`graph_reflect_failed:ServiceUnavailable@graph.internal:7687`）。
_AT_HOST_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9.\-]*)(:\d{1,5})?(?![\w.])")


def _mask_at_hosts(text: str) -> str:
    return _AT_HOST_RE.sub(lambda m: f"@<host:{_hash_id(m.group(1))}>{m.group(2) or ''}", text)


# 裸の `host.domain:port`（`不正な接続先 URL です: llm.internal:11434`）。ドット区切りのホスト名＋ポート。
# 単一ラベル（`inference01:11434`）・数字始まり（`01-llm.internal:443`）も対象。ホストに英字を 1 つは要求
# （`10:00` のような時刻を除外）。
_HOST_PORT_RE = re.compile(r"(?<![\w./@:<-])((?=[A-Za-z0-9.-]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*):(\d{2,5})(?![\w.])")


def _mask_host_ports(text: str) -> str:
    return _HOST_PORT_RE.sub(lambda m: f"<host:{_hash_id(m.group(1))}>:{m.group(2)}", text)


# ポート無しの裸のホスト名（`connection failed llm.internal`＝既存の URL 縮約が生成する形）。
# ロガー名（`sherpa.ingest.worker`・`uvicorn.access`）と資料の拡張子で終わる語は除く。
_BARE_HOST_RE = re.compile(r"(?<![\w./@:<-])([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z0-9-]*)\.?(?![\w/:-])")   # 末尾ラベルは英字始まり（版番号 1.2.3 を除外）
# ログ行のヘッダ（`%(asctime)s %(levelname)s %(name)s: `）。ロガー名の欄はここでだけ除外し、
# 本文中のドット区切り語は全てホスト名候補として扱う（`sherpa.internal` のような社内名も伏せる）。
_LOG_HEADER_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} [A-Z]+ )([\w.]+)(: )")


def _mask_bare_hosts(text: str) -> str:
    def _sub(m):
        word = m.group(1)
        if word.rsplit(".", 1)[-1].lower() in _PATH_EXTS:
            return word
        return f"<host:{_hash_id(word)}>"
    return _BARE_HOST_RE.sub(_sub, text)


def _mask_inline_credentials(text: str) -> str:
    return _INLINE_CRED_RE.sub(lambda m: m.group(1) + "=<masked>", text)
_WORLD_KV_RE = re.compile(r"(\b(?:world|world_id|wid)\s*[=:]\s*)([^\s,:・)）\]]+)")
# 接続元 IP（uvicorn の access ログ `192.168.10.25:54321 - "POST …"`・IPv6 の `[::1]:port`）は利用者情報。
_IP_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F]*:[0-9a-fA-F:]*\])(?::\d{1,5})?(?![\w.])")   # 角括弧内はコロン必須（PID の [12345] は IP ではない）
# 角括弧なしの IPv6（`fd12:3456:789a::25:54321`）。時刻 `10:00:00` と区別するため、`::` を含むか
# 16 進の英字を含むか 5 群以上のときだけ IPv6 とみなす。
_IPV6_BARE_RE = re.compile(r"(?<![\w.:])((?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})(?![\w.])")


def _looks_ipv6(s: str) -> bool:
    return ("::" in s) or any(ch in "abcdefABCDEF" for ch in s) or s.count(":") >= 5


def _mask_ip_values(text: str) -> str:
    t = _IP_RE.sub(lambda m: f"<ip:{_hash_id(m.group(0))}>", text)
    return _IPV6_BARE_RE.sub(lambda m: f"<ip:{_hash_id(m.group(1))}>" if _looks_ipv6(m.group(1)) else m.group(0), t)


def _mask_world_values(text: str) -> str:
    """`world=<id>` 形の値（登録フォルダの識別子＝ナレッジ情報）を突合可能なハッシュに置換する。"""
    return _WORLD_KV_RE.sub(lambda m: m.group(1) + f"<world:{_hash_id(m.group(2))}>", text)


def _mask_query_values(text: str) -> str:
    """`query=`/`q=`/「検索語」形の key=value は利用者の検索語を含みうる（ナレッジ情報）ため
    値だけを `<masked>` にする。"""
    return _QUERY_KV_RE.sub(lambda m: m.group(1) + m.group(2) + "<masked>", text)


# 資料の拡張子（登録フォルダの相対パスの典型的な終端）。値そのものは CLAUDE.md の資料種別と対応する。
_PATH_EXTS = frozenset({
    "md", "xlsx", "xlsm", "docx", "pptx", "pdf", "csv", "txt", "cbl", "cob", "cpy", "jcl", "proc",
    "java", "xml", "properties", "yaml", "yml", "sql", "js", "jsp", "html", "css", "vb", "bas",
    "cls", "c", "h", "cs", "sh", "bat",
    "xls", "doc", "ppt", "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff",   # 旧 Office・画像（台帳の doctype に載る）
})
_PATH_TOKEN_RE = re.compile(r"[^\s（）、。]+")   # 空白なしの日本語行を丸ごと 1 語にしない（全角括弧・句読点でも区切る）
_NUMERIC_FRACTION_RE = re.compile(r"(?:\w+=)*\d+(?:[.,]\d+)?/(?:\w+=)*\d+(?:[.,]\d+)?")   # 100/200・turns=3/tools=5
_PATH_LEAD_PUNCT = "([\"'（「"
_PATH_TRAIL_PUNCT = ")],.;:\"'）」"


def _mask_path_tokens(text: str) -> str:
    """`/` を含む、または資料拡張子で終わる空白区切りトークンを `<path:ハッシュ12桁>` に置換する
    （ログ行に混じる登録フォルダの相対パスを伏せる・ベストエフォート＝完全な保証は自己検査が担う）。"""
    def _sub(m: re.Match) -> str:
        word = m.group(0)
        lead = ""
        while word and word[0] in _PATH_LEAD_PUNCT:
            lead += word[0]
            word = word[1:]
        trail = ""
        while word and word[-1] in _PATH_TRAIL_PUNCT:
            trail = word[-1] + trail
            word = word[:-1]
        if not word:
            return m.group(0)
        # `/` を含む語はパス扱い。ただし ASCII の英数字も `.` も無い語（例: 「エラー/警告」の見出し）は
        # 日本語の区切りであってパスではないので残す（資料名は自己検査でも照合される）。
        is_path_like = "/" in word and any(ch.isascii() and (ch.isalnum() or ch == ".") for ch in word)
        if is_path_like and not _looks_like_path_start(word, m.string[:m.start()]):
            is_path_like = False                 # 固定文言の 1 個の `/`（`impact/run`・`RAG/Evidence`）はパスではない
        before_w = m.string[:m.start()]
        in_name_w = bool(_ENGLISH_LEAD_RE.search(before_w)) or (
            bool(_COLON_THEN_NAME_RE.search(before_w)) and _rest_has_path(m.string[m.end():]))
        if is_path_like and (_NUMERIC_FRACTION_RE.fullmatch(word) or (_ACRONYM_PAIR_RE.fullmatch(word) and not in_name_w)):
            is_path_like = False                 # 進捗 `100/200` の分数・固定文言の `VLM/OCR` はパスではない
        if not is_path_like:
            dot = word.rfind(".")
            if dot > 0:
                is_path_like = word[dot + 1:].lower() in _PATH_EXTS
        if not is_path_like:
            return m.group(0)
        return lead + f"<path:{_hash_id(word)}>" + trail
    return _PATH_TOKEN_RE.sub(_sub, text)


def _mask_secret_patterns(text: str) -> str:
    """秘密パターンだけを伏せる（`doctor_checks._sanitize_text` と同じ組み合わせ・同じ順序）。"""
    t = graph_extract._mask_secrets(text, None)
    t = graph_extract._redact_reflected_urls(t, None)
    return t


# 行末が名前の札（＋マスクが残す末尾の句読点）で終わる＝次のヘッダ無し行は名前の続きでありうる。
_NAME_END_RE = re.compile(
    r"<path:[0-9a-f]{12}>[" + re.escape(_PATH_TRAIL_PUNCT) + r"]*"
    r"(?:\s*（" + _SPAN_NOTE_ITEM + r"(?:・" + _SPAN_NOTE_ITEM + r")*）)*\s*$")   # 末尾の計測注記が付いていても名前の終わり


class _LogLineMasker:
    """ログ本文を 1 行ずつマスクする状態付きの呼び出し（1 ファイル＝1 インスタンス）。
    名前に改行が含まれると、欄の値の後半がヘッダ無しの継続行として次行に現れる。直前行が名前の札で
    終わっていて、継続行が traceback（`Traceback…`/`  File "…"`）でなければ、継続行全体を同じ名前の
    続きとみなして 1 つの札にする。traceback は通常のマスクだけ掛けて残す（性能解析の根拠）。
    `redact_keys.KeyBlockRedactor` の base_clean としても使う（行末の改行を保って返す）。"""

    def __init__(self) -> None:
        self._prev_name_end = False
        self._in_traceback = False

    def line(self, line: str) -> str:
        has_hdr = bool(_LOG_HEADER_RE.match(line))
        if has_hdr:
            self._in_traceback = False
        elif line.startswith("Traceback (most recent call last)") or line.startswith("  File \""):
            self._in_traceback = True
        if not has_hdr and not self._in_traceback and self._prev_name_end:
            if not line.strip():
                return line                       # 空行（名前の中の連続改行）では継続状態を保つ
            masked = f"<path:{_hash_id(line.strip())}>"
        else:
            masked = _mask_line(line)
        self._prev_name_end = bool(_NAME_END_RE.search(masked))
        return masked

    def __call__(self, seg: str) -> str:
        out = []
        for piece in seg.splitlines(keepends=True):
            body = piece.rstrip("\r\n")
            out.append(self.line(body) + piece[len(body):])
        return "".join(out)


def _mask_line(text: str) -> str:
    hdr = _LOG_HEADER_RE.match(text)
    if hdr:
        return hdr.group(0) + _mask_body(text[hdr.end():])
    return _mask_body(text)


def _mask_text(text: str) -> str:
    """秘密パターン＋検索語＋パス様トークンをまとめて伏せる（ログ行・設定値の汎用マスク）。
    ログ行のヘッダ（時刻・レベル・ロガー名）は残し、本文だけをマスクする。複数行でヘッダ付きの行を
    含む（ログ本文）ときは継続行の扱い（_LogLineMasker）を適用し、ヘッダの無い複数行の値は行ごとに扱う。"""
    if "\n" in text:
        lines = text.split("\n")
        if any(_LOG_HEADER_RE.match(x) for x in lines):
            masker = _LogLineMasker()
            return "\n".join(masker.line(x) for x in lines)
        return "\n".join(_mask_line(x) for x in lines)
    return _mask_line(text)


# 本文中の URL（`connection failed http://inference01/v1`）。既存の秘密パターン処理（`_redact_reflected_urls`）は
# URL を裸のホスト名へ縮約するだけで、単一ラベル・ポート無しのホストはその後のホスト判定に掛からない。
# 縮約より前に URL 全体を scheme・host ハッシュ・port・path ハッシュの自己完結した札へ置き換える。
# 途中の区切りは空白・引用符・山括弧・全角句読点だけ（`,`/`()` は URL として有効＝途中で切ると後半が残る）。
# 末尾に付く句読点は `_PATH_TRAIL_PUNCT` で剥がす。
_INLINE_URL_RE = re.compile(r"(?<!\w)[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s\"'<>（）「」、。]+")   # `<http://…>` も対象（置換結果は再走査されない）


def _mask_inline_urls(text: str) -> str:
    def _sub(m: re.Match) -> str:
        raw = m.group(0)
        trail = ""
        while raw and raw[-1] in _PATH_TRAIL_PUNCT:
            trail = raw[-1] + trail
            raw = raw[:-1]
        try:
            parts = urlsplit(raw)
        except ValueError:
            return f"<url:{_hash_id(raw)}>" + trail
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
        out = f"<url:{parts.scheme}|<host:{_hash_id(host)}>" if host else f"<url:{parts.scheme}|"
        if port:
            out += f":{port}"
        path = parts.path or ""
        if path and path != "/":
            out += f"|<p:{_hash_id(path)}>"
        return out + ">" + trail
    return _INLINE_URL_RE.sub(_sub, text)


def _mask_body(text: str) -> str:
    t = _mask_inline_urls(text)
    t = _mask_connect_targets(t)
    t = _mask_english_path_labels(t)
    t = _mask_path_fields(t)      # ラベル付きの欄（`…しました: <資料の相対パス>`）を先に欄ごと伏せる＝固定文言の
    t = _mask_path_spans(t)       # `/`（`読込/検証`）から始まる汎用区間がラベルごと飲み込むのを防ぐ
    t = _mask_secret_patterns(t)
    t = _mask_inline_credentials(t)
    t = _mask_query_values(t)
    t = _mask_world_values(t)     # 名前の中の `world=`/`uid=` を先に札にするとパス区間が途中で切れるので、区間処理の後に掛ける
    t = _mask_uid_values(t)
    t = _mask_ip_values(t)
    t = _mask_at_hosts(t)
    t = _mask_host_ports(t)
    t = _mask_bare_hosts(t)
    t = _mask_repr_identifiers(t)
    t = _mask_paren_paths(t)
    t = _mask_path_tokens(t)
    return t


def _json_safe(v):
    if isinstance(v, dict):
        return {k: _json_safe(vv) for k, vv in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _json_bytes(obj) -> bytes:
    return (json.dumps(_json_safe(obj), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _safe_section(builder):
    """収集元1つぶんを実行し、例外は型名だけへ畳む（値・DSN等が例外メッセージに紛れて漏れることを防ぐ）。"""
    try:
        return builder()
    except Exception as e:
        return {"error": type(e).__name__}


# ---------------------------------------------------------------------------
# settings.json / env.json
# ---------------------------------------------------------------------------

def _dynamic_key(k: str) -> bool:
    """固定スキーマ名ではない動的キー（モデル名 `ollama:registry.internal:5000/team/model` 等）は
    社内ホストやデプロイ名を含みうる＝キー自体をハッシュにする。"""
    return isinstance(k, str) and (":" in k or "/" in k or "@" in k)   # パッケージ名 `pdfminer.six` は動的キーではない


def _sanitize_generic(value, scalar_fn):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key_out = f"<key:{_hash_id(k)}>" if _dynamic_key(k) else k
            if isinstance(k, str) and _secret_like_entry(k, v):
                out[key_out] = "<set>" if _present(v) else "<unset>"
            else:
                out[key_out] = _sanitize_generic(v, scalar_fn)
        return out
    if isinstance(value, list):
        return [_sanitize_generic(v, scalar_fn) for v in value]
    if isinstance(value, str):
        return scalar_fn(value)
    return value


def _sanitize_settings_value(key: str, value):
    if _is_secret_key(key):
        return "<set>" if _present(value) else "<unset>"
    if key in _URL_SETTINGS_KEYS:
        if isinstance(value, str) and value and not _URL_SCHEME_RE.match(value):
            # scheme なしの正規保存形式（`host:port`・`[IPv6]:port`）: host をハッシュ・port は残す
            try:
                parts = urlsplit("//" + value)
                host, port = parts.hostname or "", parts.port
            except ValueError:
                host, port = value, None
            return f"<host:{_hash_id(host)}>" + (f":{port}" if port else "")
        return _strip_url_userinfo_query(value)
    return _sanitize_generic(value, _mask_text)


# 値そのものが業務知識・社内ネットワーク情報になりうる設定（質問例・プロンプト文・許可リスト・
# Webhook 宛先）は性能解析に不要＝キーごと載せない（存在だけ "<omitted>" で示す）。
_KNOWLEDGE_SETTING_TOKENS = ("example", "prompt", "allowlist", "webhook", "announce")


def _is_knowledge_key(name: str) -> bool:
    lname = name.lower()
    return any(tok in lname for tok in _KNOWLEDGE_SETTING_TOKENS)


def build_settings_snapshot() -> dict:
    raw = store.get_system_settings()
    return {k: ("<omitted>" if _is_knowledge_key(k) else _sanitize_settings_value(k, v))
            for k, v in sorted(raw.items())}


_ENV_PREFIXES = ("SHERPA_", "PG", "OPENAI_", "NEO4J_", "ES_", "CODEX_")
_ENV_EXACT_NAMES = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"})


def _env_name_included(name: str) -> bool:
    return name in _ENV_EXACT_NAMES or name.startswith(_ENV_PREFIXES)


def _sanitize_env_scalar(v: str) -> str:
    if _INLINE_CRED_RE.search(v):
        return "<set>"                       # 接続文字列に埋め込まれた認証情報は値ごと畳む
    if _URL_SCHEME_RE.match(v):
        return _strip_url_userinfo_query(v)
    masked = _mask_text(v)
    return "<masked>" if masked != v else v


_DOTENV_PATH = Path(os.environ.get("SHERPA_ENV_FILE") or (_ROOT / ".env"))   # diag.sh と同じ既定/差し替え


def build_dotenv_snapshot() -> dict:
    """リポジトリ直下の環境設定ファイル（インフラ/シード用）を、プロセス環境変数と同じ規則で写す。
    `make diag` はアプリと違ってこのファイルを読み込まずに走るため、実環境の設定値はこちらで拾う。
    含めるのは同じ接頭辞のキーだけ・秘密様の名前は <set>/<unset>・値の秘密パターンは <masked>。
    ファイルが無ければ空 dict。"""
    out = {}
    try:
        text = _DOTENV_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not _env_name_included(name):
            continue
        out[name] = _sanitize_env_pair(name, value)
    return dict(sorted(out.items()))


def _is_identity_key(name: str) -> bool:
    """利用者識別子を持つ変数名（`SHERPA_UID`・`PGUSER`・`*_USER` 等）＝値はハッシュにする。"""
    lname = name.lower()
    return lname.endswith(("uid", "user", "user_id", "owner", "username"))


def _is_host_key(name: str) -> bool:
    """社内ホスト名/アドレスを持つ変数名（`PGHOST`・`NO_PROXY`・`*_ADDR` 等）＝値（カンマ区切り可）はハッシュにする。"""
    lname = name.lower()
    return any(tok in lname for tok in ("host", "proxy", "server", "addr", "endpoint"))


def _is_path_key(name: str) -> bool:
    """ディレクトリ/ファイルの場所を持つ変数名（`SHERPA_USERS_DIR`・`SHERPA_ENV_FILE`・`*_ROOT` 等）＝
    値は区切りの有無に依らず常にハッシュにする（単一階層の相対名 `staff` も部署名等でありうる）。"""
    lname = name.lower()
    return (lname.endswith(("_dir", "_path", "_file", "_root", "_roots", "_home", "_cache", "_bin", "_venv"))
            or lname in ("home", "pwd", "oldpwd", "tmpdir", "path"))


def _is_list_path_key(name: str) -> bool:
    """OS の PATH 系（`PATH`・`*_PATH`）は `:`/`;` 区切りのリストでもある。"""
    return name.lower() == "path" or name.lower().endswith("_path")


def _is_world_key(name: str) -> bool:
    """world id を持つ変数名（`SHERPA_MCP_WORLD`・`SHERPA_TEST_WORLD_ID`）＝統計と同じ world ハッシュにする。"""
    return "world" in name.lower() and not _is_path_key(name)


def _sanitize_env_pair(name: str, v: str) -> str:
    if _is_secret_key(name):
        return "<set>" if v else "<unset>"
    if _is_identity_key(name):
        return f"<uid:{_hash_id(v)}>" if v else "<unset>"
    if _is_path_key(name):
        # `*_ROOTS`・PATH 系はカンマ/区切り文字のリスト＝要素ごとにハッシュ
        parts = [x for x in re.split(r"[,:;]" if _is_list_path_key(name) else ",", v) if x.strip()]
        return ",".join(f"<path:{_hash_id(x.strip())}>" for x in parts) if parts else "<unset>"
    if _is_world_key(name):
        return f"<world:{_hash_id(v)}>" if v else "<unset>"
    if _is_host_key(name) and not _URL_SCHEME_RE.match(v):
        return ",".join(f"<host:{_hash_id(part.strip())}>" for part in v.split(",") if part.strip()) if v else "<unset>"
    return _sanitize_env_scalar(v)


def build_env_snapshot() -> dict:
    out = {}
    for name, v in os.environ.items():
        if not _env_name_included(name):
            continue
        out[name] = _sanitize_env_pair(name, v)
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# stats/usage_stats.json
# ---------------------------------------------------------------------------

def _strip_usage_stats(v):
    if isinstance(v, dict):
        out = {}
        for k, vv in v.items():
            if k == "display_name":
                continue
            if k == "uid" and isinstance(vv, str):
                out[k] = _hash_id(vv)
            elif k == "world" and isinstance(vv, str):
                out[k] = _hash_id(vv)
            elif k == "worlds" and isinstance(vv, list) and all(isinstance(x, str) for x in vv):
                out[k] = [_hash_id(x) for x in vv]
            else:
                out[k] = _strip_usage_stats(vv)
        return out
    if isinstance(v, list):
        return [_strip_usage_stats(x) for x in v]
    return v


def build_usage_stats_snapshot(days: int) -> dict:
    stats = store.usage_stats(days)
    return _strip_usage_stats(stats)


# ---------------------------------------------------------------------------
# stats/ingest_runs.json
# ---------------------------------------------------------------------------

def build_ingest_runs_snapshot() -> list:
    rows = store.list_ingest_runs(limit=50)
    out = []
    for r in rows:
        snap = r.get("extraction_snapshot") or {}
        flags = snap.get("flags") or []
        flags_out = []
        for f in flags:
            if not isinstance(f, dict):
                continue
            # 許可キーだけ写す（`name`（未解決参照名＝資料本文由来）・`snippet`（コード本文）・
            # `target` 等は載せない）。パス系はハッシュ・reason/why は自由文なのでマスクを通す。
            f2 = {}
            for key in ("doc", "from"):
                v = f.get(key)
                if isinstance(v, str) and v:
                    f2[key] = _hash_id(v)
            for key in ("reason", "action", "analyzer", "why"):
                v = f.get(key)
                if isinstance(v, str):
                    f2[key] = _mask_text(v)
            if isinstance(f.get("line"), int):
                f2["line"] = f["line"]
            flags_out.append(f2)
        world = r.get("version")
        out.append({
            "id": r.get("id"),
            "world": _hash_id(world) if isinstance(world, str) else world,
            "layer": r.get("layer"),
            "status": r.get("status"),
            "counts": snap.get("counts"),
            "stage_timings": snap.get("stage_timings"),
            "flags": flags_out,
            "started_at": r.get("created_at"),
            "finished_at": r.get("published_at"),
        })
    return out


# ---------------------------------------------------------------------------
# stats/worlds.json
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def build_worlds_snapshot() -> list:
    rows = store.list_worlds_db()
    out = []
    for r in rows:
        wid = r.get("world_id")
        root = r.get("root_path")
        label = r.get("label")
        try:
            size = _dir_size(worlds_mod.derived_dir(wid)) if isinstance(wid, str) else None
        except Exception:
            size = None
        out.append({
            "world_id": _hash_id(wid) if isinstance(wid, str) else wid,
            "label": _hash_id(label) if isinstance(label, str) and label else None,
            "root": _hash_id(root) if isinstance(root, str) and root else None,
            "document_count": r.get("last_doc_count"),
            "last_synced_at": r.get("last_synced_at"),
            "synced": bool(r.get("last_sig")),
            "derived_size_bytes": size,
        })
    return out


# ---------------------------------------------------------------------------
# stats/db_counts.json
# ---------------------------------------------------------------------------

_PG_TABLES = ("conversations", "messages", "documents", "ingest_runs", "usage_events",
             "audit_log", "users")


def _collect_pg_counts() -> dict:
    try:
        store._ensure()
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    out = {}
    try:
        with store._connect() as c:
            for t in _PG_TABLES:
                try:
                    row = c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()  # noqa: S608 固定語彙のみ
                    out[t] = row["n"] if row else 0
                except Exception as e:
                    out[t] = {"error": type(e).__name__}
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    return out


def _world_ids() -> list:
    try:
        return [r.get("world_id") for r in store.list_worlds_db() if r.get("world_id")]
    except Exception:
        return []


def _collect_es_counts(world_ids: list) -> dict:
    try:
        es_index._req("GET", "/_cluster/health", timeout=5)
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    out = {}
    for wid in world_ids:
        idx = es_index._index(wid)
        entry: dict = {}
        try:
            resp = es_index._req("GET", f"/{idx}/_count")
            entry["doc_count"] = resp.get("count")
        except Exception as e:
            entry["doc_count"] = {"error": type(e).__name__}
        try:
            meta = es_index._index_meta(wid) or {}
            if isinstance(meta.get("world_id"), str):
                meta = {**meta, "world_id": _hash_id(meta["world_id"])}
            entry["meta"] = meta
        except Exception as e:
            entry["meta"] = {"error": type(e).__name__}
        out[_hash_id(wid)] = entry
    return out


def _collect_neo4j_counts(world_ids: list) -> dict:
    try:
        from neo4j import GraphDatabase
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    env = world_neo4j._env()
    try:
        driver = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
        driver.verify_connectivity()
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    out = {}
    try:
        with driver.session() as s:
            for wid in world_ids:
                try:
                    n = s.run("MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=wid).single()["c"]
                    e_ = s.run("MATCH ()-[r {world_id:$w}]->() RETURN count(r) AS c", w=wid).single()["c"]
                    out[_hash_id(wid)] = {"nodes": n, "edges": e_}
                except Exception as ex:
                    out[_hash_id(wid)] = {"error": type(ex).__name__}
    finally:
        driver.close()
    return out


def build_db_counts_snapshot() -> dict:
    wids = _world_ids()
    return {
        "postgres": _collect_pg_counts(),
        "elasticsearch": _collect_es_counts(wids),
        "neo4j": _collect_neo4j_counts(wids),
    }


# ---------------------------------------------------------------------------
# doctor.json
# ---------------------------------------------------------------------------

def build_doctor_snapshot() -> list:
    results = doctor_checks.run_all(probe_cloud=False)
    out = []
    for r in results:
        d = dataclasses.asdict(r)
        # 自由文の detail（接続先ホスト・例外文言を含みうる）は載せない。載せるのは構造化された
        # 判定結果だけ（どの検査が通った/落ちたかは性能解析に十分）。
        d.pop("detail", None)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# versions.json
# ---------------------------------------------------------------------------

def build_versions_snapshot() -> dict:
    pkgs = {}
    for d in importlib.metadata.distributions():
        name = d.metadata.get("Name") if d.metadata else None
        if name:
            pkgs[name] = d.version
    out: dict = {"python": dict(sorted(pkgs.items()))}
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                          capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            out["docker"] = [line.split("\t", 1) for line in r.stdout.splitlines() if line.strip()]
    except Exception:
        pass   # docker が無い/使えない環境は既定（失敗は無視）
    return out


# ---------------------------------------------------------------------------
# logs/ + log_report.txt
# ---------------------------------------------------------------------------

_ROTATED_TS_RE = re.compile(r"-(\d{8})-(\d{6})(?:-\d+)?\.log$")


def _log_file_within_days(path: Path, days: int, now: datetime) -> bool:
    m = _ROTATED_TS_RE.search(path.name)
    if not m:
        return True   # 現行ファイルは常に含める
    try:
        ts = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return True
    return (now - ts).days <= days


def build_logs_bundle(log_dir: Path, days: int, max_mb: float) -> list:
    """`(arcpath, masked_bytes)` の list。世代 × ログ種別を新しい順に走査し、`max_mb` を
    超えた時点で以降（＝より古いもの）を打ち切る（新しい順に残す契約）。"""
    names = log_report._default_names(log_dir)
    now = datetime.now()
    paths = []
    for name in names:
        for p in log_report.list_generations(log_dir, name, include_rotated=True):
            if _log_file_within_days(p, days, now):
                paths.append(p)

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    paths.sort(key=_mtime, reverse=True)

    out = []
    total = 0
    cap = int(max_mb * 1024 * 1024)
    skipped = []
    for p in paths:
        try:
            raw_lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError as e:
            out.append((f"logs/{p.name}.error.json", _json_bytes({"error": type(e).__name__})))
            continue
        redactor = redact_keys.KeyBlockRedactor(_LogLineMasker())   # 1ファイル=1使い捨てインスタンス（継続行の状態も持つ）
        data = "".join(redactor(line) for line in raw_lines).encode("utf-8")
        if total + len(data) > cap:
            skipped.append(p.name)            # 上限超え＝この世代は載せない（より小さい過去世代は続けて見る）
            continue
        out.append((f"logs/{p.name}", data))
        total += len(data)
    if skipped:
        # 「ログが無い」と「サイズ上限で打ち切った」を区別できるように記録する（ファイル名は固定の世代名のみ）
        out.append(("logs/truncated.json", _json_bytes({"skipped_files": skipped, "max_log_mb": max_mb})))
    return out


def build_log_report_text(masked_logs: list) -> str:
    """集計レポートは**マスク済みのログ本文**から作る（生ログから作ると資料名の一部が集計文言に残る）。"""
    truncated = [data for arcpath, data in masked_logs if arcpath == "logs/truncated.json"]
    if not any(arcpath.endswith(".log") for arcpath, _d in masked_logs):
        if truncated:
            n = len(json.loads(truncated[0].decode("utf-8")).get("skipped_files") or [])
            return f"ログはサイズ上限（--max-log-mb）で打ち切られました（{n} 本）。\n"
        return "ログがありません。\n"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        for arcpath, data in masked_logs:
            name = Path(arcpath).name
            if name.endswith(".log"):
                (tdir / name).write_bytes(data)
        names = log_report._default_names(tdir)
        if not names:
            return "ログがありません。\n"
        return _mask_text(log_report.render_report(tdir, names, True)) + "\n"


# ---------------------------------------------------------------------------
# 自己検査（fail-closed）
# ---------------------------------------------------------------------------

_SELFCHECK_MIN_LEN = 3   # これ未満は誤検知率が高すぎるため対象外（判断: 極端に短い識別子は非現実的）


def _collect_selfcheck_needles() -> list:
    needles = set()
    with store._connect() as c:
        rows = c.execute("SELECT name, scope_path, original_path, md_path FROM documents").fetchall()
    for r in rows:
        for col in ("name", "scope_path", "original_path", "md_path"):
            v = r.get(col)
            if isinstance(v, str) and len(v.strip()) >= _SELFCHECK_MIN_LEN:
                needles.add(v)
                # 空白・区切り（/ \）で割った片も照合する（マスクが語単位で漏らしても止める側へ寄せる）。
                # ASCII だけの片は 8 文字以上、非 ASCII を含む片は 3 文字以上（短い日本語名も拾う）。
                for piece in re.split(r"[\s/\\]+", v):
                    piece = piece.strip()
                    if not piece:
                        continue
                    if (len(piece) >= 8) or (len(piece) >= _SELFCHECK_MIN_LEN and not piece.isascii()):
                        needles.add(piece)
    for w in store.list_worlds_db():
        for col in ("world_id", "root_path", "label"):
            v = w.get(col)
            if isinstance(v, str) and len(v.strip()) >= _SELFCHECK_MIN_LEN:
                needles.add(v)
    return sorted(needles)


def _json_string_values(v) -> list:
    if isinstance(v, dict):
        out = []
        for vv in v.values():
            out.extend(_json_string_values(vv))
        return out
    if isinstance(v, list):
        out = []
        for vv in v:
            out.extend(_json_string_values(vv))
        return out
    return [v] if isinstance(v, str) else []


_SELFCHECK_TOKEN_RE = re.compile(r"[\s/\\、。（）()\[\]{}:=,;\"'<>|]+")


def _selfcheck_tokens(text: str) -> set:
    """本文を区切り（空白・パス区切り・句読点・括弧・引用符・`=`/`:`）で割った語の集合（重複を畳む）。"""
    return {tok for tok in _SELFCHECK_TOKEN_RE.split(text) if tok}


def _run_selfcheck(bundle: dict) -> tuple:
    """`bundle`（arcpath -> bytes）を、実 DB から読んだ相対パス/root/label の生文字列で走査する。
    戻り値 `(ok, hits, needle_count)`。`hits` は `{arcpath: 一致した識別子の件数}`（値そのものは含めない）。
    照合は部分一致（識別子が語の内側に残る `極秘顧客向け` も止める）。ただし本文全体ではなく、区切りで
    割った語の重複を畳んだ集合に対して行う（マスク後のログは定型文とハッシュ札が大半で語の種類は少ない＝
    識別子数 × 本文長の全走査を避けて既定 200MB でも使える速さ）。空白入りの識別子（資料名の全体）は
    語をまたぐので、その片が全て語集合に含まれるときだけ本文への部分一致で確かめる。"""
    needles = _collect_selfcheck_needles()
    if not needles:
        return True, {}, 0
    single = [n for n in needles if not _SELFCHECK_TOKEN_RE.search(n)]
    multi = [(n, [p for p in _SELFCHECK_TOKEN_RE.split(n) if p]) for n in needles if _SELFCHECK_TOKEN_RE.search(n)]
    hits = {}
    for arcpath, data in bundle.items():
        text = data.decode("utf-8", errors="replace")
        if arcpath.endswith(".json"):
            # JSON は固定スキーマのキー（`documents` 等）を照合せず、文字列値だけを見る
            # （キーの語がパス断片と偶然一致して正常なバンドルまで止めないため）。
            try:
                text = "\n".join(_json_string_values(json.loads(text)))
            except ValueError:
                pass
        tokens = _selfcheck_tokens(text)
        joined = "\n".join(tokens)
        count = sum(1 for n in single if n in joined)
        for n, pieces in multi:
            if pieces and all(any(p in t for t in tokens) if not p.isascii() else p in tokens for p in pieces) and n in text:
                count += 1
        if count:
            hits[arcpath] = count
    return (not hits), hits, len(needles)


# ---------------------------------------------------------------------------
# manifest・tar 組み立て
# ---------------------------------------------------------------------------

_APPLIED_RULES = [
    "ナレッジ情報（資料のファイル名・相対パス・登録フォルダの root・world の id/label・検索語）と"
    "回答本文・利用者情報は含めない（目的は性能解析であって精度確認ではない）。",
    "system_settings/env: キー名に key/token/secret/password/passwd/pw/credential/auth を含む値は "
    "<set>/<unset> に畳む。",
    "settings.json の残りの文字列値・ログ本文: 秘密パターン"
    "（sk-/Bearer/api-keyヘッダ/PEM鍵ブロック）をマスク"
    "（sherpa.ingest.graph_extract._mask_secrets/_redact_reflected_urls・"
    "複数行にまたがる鍵ブロックは sherpa.redact_keys.KeyBlockRedactor）。",
    "env.json: 秘密パターンを検出した非秘密様キーの値は <masked> に全置換する"
    "（settings.json は該当箇所だけの部分マスクで保持）。",
    "URL（openai_base_url/ollama_url・env の URL 値・ログ本文中の URL）は userinfo（user:pass@）とクエリを除去し、"
    "scheme と port だけ残して host と path はハッシュにする。",
    "doctor.json は検査 id・ラベル・判定（ok/ng/skip）だけを載せ、自由文の detail（接続先・例外文言）は載せない。",
    "ログ行のパス様の区間（絶対パス／区切り 2 つ以上／非 ASCII／資料の拡張子／大文字だけの段／英語の前置きの直後"
    "のいずれかで始まる語から名前の終わりまで）とラベル付きの欄の値は <path:ハッシュ12桁> に、"
    "検索語（query=/q=/「検索語」）は <masked> に置換する。",
    "相対パス（ingest_runs.flags.doc）・world の id/label/root は常に sha256 先頭12桁へハッシュ化する"
    "（同じ入力は同じハッシュ＝ファイル間の突合は可能）。",
    "usage_stats.json: uid を sha256 先頭12桁へハッシュ化（無効化オプションは無い）・"
    "display_name は含めない。",
    "messages/documents/users の本文・email・display_name・password_hash・api_keys・audit_log 本文・"
    "個人 workspace・登録フォルダ配下の資料そのものは収集対象に含めない。環境設定ファイルは秘密様キーを "
    "<set>/<unset>・URL/ホスト/識別子をハッシュした上で dotenv.json に含める（値の平文は出さない）。",
    "出力前に documents/worlds の識別子で自己検査（fail-closed）を行い、残存が見つかれば tar を作らず中止する。",
]


def build_manifest(included_files: list, selfcheck_info: dict) -> dict:
    git_sha = None
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(_ROOT),
                          capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            git_sha = r.stdout.strip()
    except Exception:
        git_sha = None
    return {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha,
        "python_version": sys.version.split()[0],
        "os": platform.platform(),
        "included_files": sorted(included_files),
        "rules_applied": _APPLIED_RULES,
        "self_check": selfcheck_info,
    }


_PRESANITIZED_SECTIONS = frozenset({"settings.json", "env.json", "dotenv.json"})


def _json_sections(args) -> list:
    return [
        ("settings.json", build_settings_snapshot),
        ("env.json", build_env_snapshot),
        ("dotenv.json", build_dotenv_snapshot),
        ("stats/usage_stats.json", lambda: build_usage_stats_snapshot(args.days)),
        ("stats/ingest_runs.json", build_ingest_runs_snapshot),
        ("stats/worlds.json", build_worlds_snapshot),
        ("stats/db_counts.json", build_db_counts_snapshot),
        ("doctor.json", build_doctor_snapshot),
        ("versions.json", build_versions_snapshot),
    ]


def _dry_run_report(args) -> str:
    lines = ["含める予定のファイル:"]
    for arcpath, _builder in _json_sections(args):
        lines.append(f"  {arcpath}")
    lines.append("  log_report.txt")
    lines.append(f"  logs/*.log（{args.log_days}日以内の世代・合計 {args.max_log_mb}MB まで）")
    lines.append("  MANIFEST.json")
    lines.append("")
    lines.append("適用ルール:")
    for r in _APPLIED_RULES:
        lines.append(f"  - {r}")
    return "\n".join(lines)


def build_args_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=None, help="出力先 tar.gz（既定: dist/diag/sherpa-diag-<日時>.tar.gz）")
    ap.add_argument("--days", type=int, default=30, help="利用統計（usage_stats）の集計期間（日・既定30）")
    ap.add_argument("--log-days", type=int, default=7, help="ログの退避世代を遡る日数（既定7）")
    ap.add_argument("--max-log-mb", type=float, default=200.0, help="ログ合計サイズの上限MB（既定200）")
    ap.add_argument("--dry-run", action="store_true", help="含める予定と規則だけ表示して終了する")
    return ap


def main(argv=None) -> int:
    args = build_args_parser().parse_args(argv)
    # 読み取り専用契約: store の遅延初期化（スキーマ作成/移行を伴う `init_schema`）を走らせない。
    # 既に運用中の DB を前提に「初期化済み」として SELECT だけ行う（表が無ければ各節が error になる）。
    db_mod._inited = True

    if args.dry_run:
        print(_dry_run_report(args))
        return 0

    log_dir = Path(os.environ.get("SHERPA_LOG_DIR", "data/run"))

    bundle: dict = {}
    for arcpath, builder in _json_sections(args):
        section = _safe_section(builder)
        if arcpath not in _PRESANITIZED_SECTIONS:
            # 統計/件数/版の節に共通の最終マスク（モデル名や ES メタに混じる社内ホスト等・各節の
            # 個別処理が取りこぼしても文字列値はここで必ず汎用マスクを通る）。設定/環境変数の節は
            # 専用規則（<set>/<unset>・URL の縮約）を既に適用済みなので二重に掛けない。
            section = _sanitize_generic(section, _mask_text)
        bundle[arcpath] = _json_bytes(section)

    masked_logs: list = []
    try:
        masked_logs = build_logs_bundle(log_dir, args.log_days, args.max_log_mb)
        for arcpath, data in masked_logs:
            bundle[arcpath] = data
    except Exception as e:
        bundle["logs/error.json"] = _json_bytes({"error": type(e).__name__})

    try:
        report_text = build_log_report_text(masked_logs)
    except Exception as e:
        report_text = f"収集に失敗しました: {type(e).__name__}\n"
    bundle["log_report.txt"] = report_text.encode("utf-8")

    # 自己検査は省略できない（回避オプションは無い）。DB が読めなければ検査できない＝中止。
    try:
        ok, hits, needle_count = _run_selfcheck(bundle)
    except Exception as e:
        print(f"自己検査できないため中止しました（{type(e).__name__}）。DB へ接続できる状態で再実行してください。",
              file=sys.stderr)
        return 2
    if not ok:
        print("自己検査で登録フォルダの相対パス/root/label の残存を検出したため、"
              "tar を作らず中止しました。該当ファイルと件数（値は表示しません）:", file=sys.stderr)
        for arcpath, count in sorted(hits.items()):
            print(f"  {arcpath}: {count}件", file=sys.stderr)
        return 2
    selfcheck_info = {"performed": True, "needle_count": needle_count}

    out_path = Path(args.out) if args.out else (
        _ROOT / "dist" / "diag" / f"sherpa-diag-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(list(bundle.keys()), selfcheck_info)
    bundle["MANIFEST.json"] = _json_bytes(manifest)

    mtime = int(time.time())
    with tarfile.open(out_path, "w:gz") as tar:
        for arcpath, data in bundle.items():
            info = tarfile.TarInfo(arcpath)
            info.size = len(data)
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))

    print(f"作成しました: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
