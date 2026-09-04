"""直接 grep ツール（read-only・鏡モデル）。

world（登録ディレクトリ）の**1つのフォルダ木**を行単位で全文検索し、根拠つき（`doc_id`＝**rel_path**＋`span`）
のヒットを返す。RAG（ES/Neo4j）を経由しない素の grep 経路で、別レンズ（仕様問い合わせ qa／
トラブルシュート troubleshoot）の材料を集める。`doc_id`＝world root 相対パス（グラフの来歴・DL キーと一致・§2.2）。
特定テーマの名前はコードに持たない（検索語は入力から）。個人 workspace も対象になり得るが MVP は共有 KB のみ。
"""
from __future__ import annotations

import heapq
import os
import re
import time
from collections import deque
from pathlib import Path

from . import layer as layer_mod
from .doc_kinds import CODE_EXT
from .ingest import text_kind

# 決定的MD（Office/PDF 由来）とソース原文（cobol/jcl/copybook）。grep は両方を対象にする。
_MD_EXT = {".md", ".markdown"}
# Evidence IR 由来の検索向け Markdown（`sherpa/ingest/evidence_render.py::render`）。
_RAG_SUFFIX = ".rag.md"
# OCR 観測の本文ファイル名（`sherpa/ingest/observation_render.py::artifact_paths`）。
_OBSERVATION_SUFFIX = ".rag_observations.md"
# 高速な事前フィルタ（`doc_kinds.CODE_EXT`＝登録拡張子の和集合＋MD/テキスト＋軽量テキスト枠の
# 第1段拡張子マップ）——最終判定ではない。「コードか資料か」の確定は `grep_search` 本体が
# `corpus_docs.classify_document` に集約する（accepts() 全滅の登録拡張子や未登録拡張子を、
# この集合の所属だけで「コード」と見なさない・§7 裁定10）。
# `.txt` はコード判定の対象外（プレーンテキスト＝どちらでもない）だがここでは従来どおり grep 対象。
# `text_kind.CODE_EXT`/`DOCUMENT_EXT`（第1段＝拡張子だけで判定可能）は事前フィルタに含める——
# 第2段（未知拡張子・拡張子なしの内容推定）はここでは対象にしない（grep は問い合わせのたびに
# 全木を歩くため、非対象拡張子ごとに内容を読む余地を持たせると検索コストが増える・§4 の設計判断。
# 台帳/scan_report/status API は `classify_document` 経由で第2段まで判定するので齟齬ではない
# ——grep だけが「未知拡張子の内容推定」に対応しない、という限定された既知の差）。
_TEXT_EXT = _MD_EXT | CODE_EXT | {".txt"} | text_kind.CODE_EXT | text_kind.DOCUMENT_EXT

# world 識別子は英数字＋限定記号のみ（`/`・`..` を含めない＝パストラバーサル防止）。
_WORLD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # fullmatch 専用（^/$ アンカー不要）


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析（`agentic_search._env_int` と同型・secRV 範囲外是正・2026-07-19）。

    `agentic_search` は本モジュールを import する側（`from . import ... grep_tool ...`）のため、
    ここで `agentic_search` を import すると循環 import になる。同じ検証ロジック（範囲外・非整数・
    負値は既定へ、既定値自体も [lo, hi] にクランプ）を独立実装する。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


# secRV 範囲外是正（2026-07-19・grep 全量ロード OOM）: `grep_search` は対象ファイルを
# `Path.read_text()` で一括ロードしていたため、共有フォルダ（world 鏡）に巨大テキスト（数GB）を
# 置ける主体が、以後の全検索でメモリ枯渇（この機のボトルネックは 7.7GB）→最悪プロセス停止を
# 誘発できた。
#
# 是正 追補（2026-09・ストリーミング走査）: 上の是正は「メモリを守る」ことと「1ファイルにつき
# cap より後ろを検索できない」ことを同じ1つの定数へ束ねていた——cap を超える派生 MD（10MB〜100MB
# 級の大きな Excel 由来）は cap より後ろが恒久的に無音になっていた。以後の実装（`_CappedStreamReader`
# 参照）は1ファイルを bounded chunk（`_SCAN_CHUNK_BYTES`）でストリーミング走査し、保持するメモリは
# 「現在の窓」と「ヒット節を復元する最小限の状態」（MD なら直近の見出し行とその行番号・その節の
# 引用テキストは `_GREP_HIT_TEXT_MAX_BYTES` で頭打ち）だけに限定する——`_GREP_FILE_CAP_BYTES` の
# 大きさにもファイル実サイズにも比例しない。この定数自体は残る（1ファイルにかける走査コスト・
# 時間の安全弁として）が、もはやメモリ安全弁ではないため、既定値を範囲上限まで引き上げた
# （実運用の 10MB〜100MB 級文書をできるだけ cap 無しで検索できるようにする）。
_GREP_FILE_CAP_BYTES = _env_int("SHERPA_GREP_FILE_CAP_BYTES", 64 * 1024 * 1024, 65536, 64 * 1024 * 1024)
# MD の見出し節引用は `_section()` が節全体を返すため、見出しのない巨大 MD だと1ヒットが文書全体に
# なり得る（`max_hits` との掛け算でヒットリスト自体も肥大）。ヒット1件あたりの引用テキストを
# UTF-8 バイト上限でクリップする（`agentic_search._clip_utf8_bytes` と同じセマンティクス）。
_GREP_HIT_TEXT_MAX_BYTES = _env_int("SHERPA_GREP_HIT_TEXT_MAX_BYTES", 64 * 1024, 1024, 8 * 1024 * 1024)


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    """UTF-8 エンコード後のバイト数が `max_bytes` を超えないよう `s` を切り詰める
    （`agentic_search._clip_utf8_bytes` と同型・マルチバイト文字の境界破壊を避ける）。
    """
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    return b[:max_bytes].decode("utf-8", errors="ignore")


# ストリーミング走査（本丸・2026-09）: `_GREP_FILE_CAP_BYTES`（最大64MiB）自体を一括ロードすると、
# 既定を引き上げた意味が薄れる（cap の大きさに比例したメモリを毎ファイル消費する）。以下は
# bounded chunk（`_SCAN_CHUNK_BYTES`・cap にもファイル実サイズにも依存しない固定値）で読み、
# 改行区切りの行を順に yield するリーダー。境界セマンティクス（1 byte 余分に読んで「ちょうど cap」
# と「cap 超過」を区別する・cap で切れた中途行は破棄する）は旧 `f.read(cap + 1)` 一括ロードと同じ。
_SCAN_CHUNK_BYTES = 64 * 1024
# 改行が来ないまま伸び続ける単一行（secRV MED-B 型の懸念＝`agentic_search` の単一行対策と同種）で
# 保持バイト数が増え続けないための、1行あたりの保持上限。cap 内であっても、この上限を超えた行は
# 内容の一部を破棄しつつ次の改行まで読み進める（行番号の同期は保つ）。
#
# env 化（2026-09・read 側のストリーミング化に合わせて）: `agentic_search`（read_around/read_doc/
# doc_outline）も本モジュールの `_CappedStreamReader`/`_logical_lines` をそのまま再利用するが、
# 1行あたりの保持上限は `_GREP_FILE_CAP_BYTES`/`SHERPA_READ_AROUND_FILE_CAP_BYTES` と同じ「経路
# ごとに別 env」の流儀に揃え、read 側は独立の env（`SHERPA_READ_LINE_MAX_BYTES`・既定値・許容
# 範囲は揃える）で調整できるようにする——`_CappedStreamReader` 自体は `line_max_bytes` を
# 呼び出し元から差し込める（省略時だけこの既定を使う）。
_GREP_LINE_MAX_BYTES = _env_int("SHERPA_GREP_LINE_MAX_BYTES", 2 * 1024 * 1024, 64 * 1024, 16 * 1024 * 1024)

# RV是正（rv-i2-importance #1・2026-09）: 隣接ヒット窓の重複排除（`grep_search` 内 `seen`）に
# 使う小さな有界窓。衝突が起こり得るのは常に直近の窓どうしだけ（`grep_search` 内コメント参照）
# のため、ファイル内の全ヒット数に比例させる必要が無い——固定サイズの小さな `deque` で十分。
_SEEN_RECENT_MAX = 16


class _CappedStreamReader:
    """open 済みバイナリファイル `f` を bounded chunk で読み、`cap` バイトまでの行
    （改行を含まない生バイト列）を順に yield する。

    メモリは `_SCAN_CHUNK_BYTES`・`_GREP_LINE_MAX_BYTES`・繰り越し中の未確定バイト列だけで
    頭打ちになり、`cap` の大きさにもファイル実サイズにも比例しない。

    属性は列挙の進行に伴って逐次更新され、呼び出し元は列挙の途中でも参照できる:
    - `total_read`: これまでに読んだ総バイト数。
    - `truncated`: `total_read > cap` になった時点で True（旧実装の「1 byte 余分に読んで
      ちょうど cap のファイルを誤って truncated 扱いしない」トリックと同じ境界）。
    - `line_overflowed`: 1行が `line_max_bytes`（既定 `_GREP_LINE_MAX_BYTES`）を超えて改行が
      来ず、内容の一部を破棄したら True（探せていない範囲がある＝呼び出し元は打切りとして扱う）。
    """

    def __init__(self, f, line_max_bytes: int | None = None):
        self._f = f
        self.total_read = 0
        self.truncated = False
        self.line_overflowed = False
        # `None`（省略）は「呼び出し時点の」`_GREP_LINE_MAX_BYTES` を使う（`__init__` 実行時に
        # 解決＝テストの `monkeypatch.setattr(grep_tool, "_GREP_LINE_MAX_BYTES", ...)` は
        # インスタンス生成より前に行われる限り効く）。呼び出し元（`agentic_search`）が独自の
        # env（`SHERPA_READ_LINE_MAX_BYTES`）由来の値を渡せば、grep 側の設定とは独立に効く。
        self._line_max_bytes = _GREP_LINE_MAX_BYTES if line_max_bytes is None else line_max_bytes

    def lines(self, cap: int):
        budget = cap + 1
        carry = b""
        line_max = self._line_max_bytes
        while budget > 0:
            chunk = self._f.read(min(_SCAN_CHUNK_BYTES, budget))
            if not chunk:
                break
            budget -= len(chunk)
            self.total_read += len(chunk)
            if self.total_read > cap:
                self.truncated = True
            data = carry + chunk
            carry = b""
            start = 0
            while True:
                nl = data.find(b"\n", start)
                if nl == -1:
                    break
                line_bytes = data[start:nl]
                if len(line_bytes) > line_max:
                    # 改行までの距離が長くても、たまたま同じ chunk 内に収まっていれば `find()` は
                    # 即座に見つかる——「複数 chunk にまたがる巨大行」だけでなく、この場合も
                    # 一律に頭打ちにする（1行の保持上限はチャンク境界に依存しない）。
                    line_bytes = line_bytes[:line_max]
                    self.line_overflowed = True
                yield line_bytes
                start = nl + 1
            tail = data[start:]
            if len(tail) > line_max:
                tail = tail[:line_max]
                self.line_overflowed = True
            carry = tail
        # cap で打ち切られた場合、繰り越し中の未完の行（改行未到達）は行の途中で切れているため
        # 丸ごと破棄する（既存仕様＝中途行を誤ヒットさせない）。cap に達さず自然に EOF に達した
        # 場合は、繰り越し中の内容は正当な最終行（改行なしで終わるファイル）として残す。
        if carry and not self.truncated:
            yield carry


def _logical_lines(reader, cap: int):
    """ストリーム読みの生バイト行（`\n` 区切り）→ **`str.splitlines()` と同一の論理行**の列。

    行番号の定義は旧実装（全体 decode → `splitlines()`）であり、read_around/read_doc も同じ
    `splitlines()` で行を数える（`agentic_search.run_tool` の read_around/read_doc/doc_outline
    分岐——`_stream_doc_lines` 経由で本関数を再利用する）。grep 側だけ `\n` 限定で
    数えると、`\r` 単独・`\f`（改ページ＝COBOL/JCL リストに実在する）・`\x85`（NEL＝EBCDIC 変換由来）・
    `\u2028` 等を含む文書で**ヒットの行番号と精読の行番号がズレ、引用と read_around が食い違う**。

    `\n`（0x0A）は UTF-8 のマルチバイト列の続きバイトになり得ないため、`\n` 区切りの生バイト行を
    個別に decode → `splitlines()` した結果を連結すると、全体を decode → `splitlines()` した結果と
    完全に一致する（各セグメントは `\n` を含まず、他の区切り文字はセグメント内で完結する）。
    空セグメント（連続改行）は空行1本として数える（`"".splitlines()` は `[]` を返すため明示の補正）。
    """
    for raw_line in reader.lines(cap=cap):
        decoded = raw_line.decode("utf-8", errors="replace")
        yield from (decoded.splitlines() or [""])


def valid_world(v: str) -> bool:
    """world 識別子の許容文字。worlds/scope/api が共用する単一の検証。

    `fullmatch()` を使う（`match()`＋`$` アンカーだと `$` が「末尾の改行の直前」にもマッチするため、
    末尾に LF が付いた値を誤って通してしまう・X-Request-Id の検証で踏んだのと同じ抜け穴）。
    """
    return bool(_WORLD_RE.fullmatch(v or ""))


def rag_grep_enabled() -> bool:
    """grep の検索対象・read_around の精読対象の双方が rag 表現（`{rel}.rag.md`）を優先するか。

    常時 True（TOGGLE-RM・2026-09-03: グローバルな系統切替トグル `SHERPA_SEARCH_RAG_GREP` を撤去
    し常時ONへ固定）。呼び出し元は本関数の戻り値を経由せず直接 `preferred_derived_name` の
    ファイル実在チェックへ委ねてよいが、既存の呼び出し形を変えない最小変更として関数自体は残す。
    rag ファイルがその文書について実在しない場合の per-file legacy フォールバック（`{rel}.md` を
    使う）はこの関数と無関係の別契約として維持する（`preferred_derived_name` 参照）。
    """
    return True


def strip_derived_suffix(name: str) -> str:
    """派生ファイルの物理名（rel）→ 原本 rel。`.rag.md` → `.rag_observations.md` → 一般の `.md` の順に、
    最初に一致した1つだけを剥がす（`.rag.md`/`.rag_observations.md` はいずれも `.md` でも終わるため、
    より具体的な拡張を先に判定する必要がある）。どれにも一致しなければ変更せず返す。"""
    if name.endswith(_RAG_SUFFIX):
        return name[: -len(_RAG_SUFFIX)]
    if name.endswith(_OBSERVATION_SUFFIX):
        return name[: -len(_OBSERVATION_SUFFIX)]
    if name.endswith(".md"):
        return name[:-3]
    return name


def preferred_derived_name(rag_root: Path, rel: str) -> str:
    """原本 rel（拡張子込み・例 `report.docx`）→ 検索/精読対象の派生ファイル名（rag 優先・legacy フォールバック）。

    rag が有効かつ `{rel}.rag.md` が `rag_root`（`worlds.derived_rag_dir`）に実在すればそちらを、
    それ以外は従来どおり `{rel}.md` を返す（**legacy 側の**実在確認はしない＝呼び出し元が
    confinement 検証込みで open/stat する。rag 側は「優先すべきか」の判定に実在確認そのものが
    必要なのでここで行う）。返す名前が `.rag.md` で終わるかどうかで、呼び出し元は物理ルートが
    `rag_root`（`.rag.md`）か md 層のルート（`.md`）かを判別する（§8.1 三階層・フォルダ分離＝
    `.rag.md`/`.md` は別ディレクトリに物理配置される）。
    `grep_search`（どちらの物理ファイルを検索対象にするか）と `agentic_search._safe_doc_path`
    （read_around がどちらを開くか）が本関数を共有することで、両者は常に同じ1ファイルを見る
    （食い違うとヒット位置と精読内容が一致しなくなる）。
    """
    if rag_grep_enabled() and (rag_root / (rel + _RAG_SUFFIX)).is_file():
        return rel + _RAG_SUFFIX
    return rel + ".md"


class GrepDeadlineExceeded(Exception):
    """`grep_search(deadline=...)` がツリー列挙中にデッドラインを超えたことを示す（呼び出し元が
    翻訳する・PART-4 経由の呼び出しは既存のデッドライン優先の再分類で `ResearchTimeout`/504 に
    なる・`scope_infer.ScopeWalkDeadlineExceeded` と同型）。"""


_DEADLINE_CHECK_ENTRIES = 256   # ツリー列挙中に `deadline` を再確認する間隔（`grep_search`
# docstring 参照・単一ルートに大量のファイルがあっても列挙完了・ソート開始前に打ち切れるように
# する）。
_DEADLINE_CHECK_LINES = 256   # 1ファイルの行走査ループ中に `deadline` を再確認する間隔（`grep_search`
# docstring 参照・巨大ファイル1件の全文走査自体がデッドラインを食い潰すケースの防御）。


def grep_search(query: str, world: str = "v1", roots=None, max_hits: int = 50,
                scope_paths=None, deadline: float | None = None, layer=None,
                truncated_docs: list | None = None):
    """`query` を含む行を world のフォルダ木から探し、根拠つきヒットを返す（read-only）。

    各ヒット: `{doc_id(=rel_path), path(内部用・API非露出), ext, line, span:[start,end], text, match}`
    ＋登録者が `_重要度.txt`（`ingest.importance`）で付けた重要度があれば `importance`/
    `importance_reason` を条件付きで追加（無ければキー自体を持たない・I2・2026-09-05）。
    MD は**該当見出し節**を `text`/`span`（qa の引用）、ソースは該当行＋前後数行。同一 (doc, 節) は1集約。
    `scope_paths`（フォルダ prefix）を渡すと、**その範囲の文書だけ** grep する（範囲外は読まない・MIRROR §3）。

    **ヒットの選抜（I2・二経路化＝rv-i2-importance #2・コーディネータ裁定2026-09-05再判定）**:
    返すのは上限 `max_hits` 件の **top-K**——優先度は `(重要度rank降順, 発見順昇順)`（重要度＝
    `高`>`中`/未設定>`低`・同 rank は先に見つかった方を残す）。`_重要度.txt` が無い world（または
    `roots` 明示指定の呼び出し）は `imp_map` が空＝全ヒットの rank が揃う。この場合、ヒープが
    `max_hits` で満杯になった時点で**以後どのヒットも数学的に二度と採用され得ない**
    （min-heap のキー `(rank, -seq)` は rank 一様なら新エントリの `-seq` が既存最小値より必ず
    小さくなるため、`entry > heap[0]` が恒に False になる）——この事実を使い、旧実装（I2以前）と
    同じ2つの打切り点で走査を早期終了する: **ファイル内**（行走査ループの各行の後・MD 最終節／
    未確定 pending 行の flush は行わない＝旧実装と同じ取りこぼし挙動）と**ファイル境界**
    （1ファイルを終えるたびに判定・満たせば以後のファイル・root を一切開かない）。結果として
    選抜は「発見順で先頭 `max_hits` 件」＝早期打切りしていた旧実装と完全に同じ集合・同じ順序に
    なり、`deadline` の消費（`_check_deadline` の呼び出し頻度）も旧実装の水準に戻る。
    一方、`_重要度.txt` がある world（`imp_map` が非空）は、後から見つかった `高` 文書が現在の
    ヒープ最下位を上書きしうるため、この早期終了条件は成立せず**常に全量走査**する（`max_hits`
    到達後も走査を続けるぶん `deadline` 消費は増える——既存の周期チェックが引き続き効くことで
    ハングしないことがテストの固定対象）。

    **打切りの申告**（`agentic_search.run_tool` の read_doc/doc_outline 分岐が返す `file_truncated`
    と同じ語彙・同じ意味＝
    読み込みが `_GREP_FILE_CAP_BYTES` に達し、cap より後ろは検索できていない可能性がある）を2経路で行う:

    - ヒット元が打ち切られていたら、そのヒットにだけ `file_truncated: True` を付ける。打切りが無い
      通常のヒットにはキー自体を作らない（`degrade_reason` と同じ流儀＝戻り値の形は不変）。
    - `truncated_docs`（省略可）にリストを渡すと、**打ち切られた文書の `doc_id`** を重複なく追記する。
      **ヒットを1件も出さなかった打切り文書もここに載る**——ヒット経由の申告だけでは「cap より
      後ろにしか一致が無い文書」が完全に無音になる（＝『検索したのに出てこない』の正体）。
      呼び出し元がリストを渡さなければ何もしない（既存呼び出し元は無変更）。ただし上記の早期終了
      （`imp_map` が空かつヒープ満杯）が発生した場合、そこから先は文書を一切開かないため、その
      時点より後にある打切り文書は報告されない——旧実装（I2以前）と同じ意味論に戻るだけであり、
      `_重要度.txt` がある world（常に全量走査）ではこの限定は無い。

    軽量テキスト枠（`ingest.text_kind`＝未登録拡張子のテキストファイル）だけは、台帳/ES と同じ基準
    （`text_kind.MAX_BYTES`＝8MiB）でサイズ超過を丸ごと対象外にし、これも `truncated_docs` へ
    載せる（TEXT-ALL L-1 是正・2026-09）——台帳側が `size_exceeded` として除外している文書を、
    grep だけ `_GREP_FILE_CAP_BYTES`（64MiB）内の先頭部分でヒットさせてしまう矛盾を避けるため。
    登録拡張子コード・Office 派生 MD・`.md`/`.txt` はこの対象外（従来どおり `_GREP_FILE_CAP_BYTES`
    まで検索する）。

    `layer`（省略可・`"docs"|"code"|"both"`・既定 `None`＝`"both"`＝フィルタなし＝既存呼び出し元は
    無変更）: 探す対象（調べ方ブロック §3.4）。`scope_paths` と同じ場所（範囲外はそもそも読まない）で
    判定する——`classify_document` の確定結果（`layer_mod.in_layer_code`）に一致しない文書は
    そもそも読まない（拡張子だけの近似（`layer_mod.in_layer`）は使わない・§7 裁定10）。

    `deadline`（省略可・`time.monotonic()` 系の絶対期限。既定 None＝無期限＝既存呼び出し元は
    無変更）: 指定時、以下の**すべての境界**で確認し、超えていれば `GrepDeadlineExceeded` を
    送出して打ち切る——一貫して例外にする（部分的なヒット集合を黙って返さない・空 query/不正
    world による早期 `[]` も含め、いかなる `return` も期限超過を検知した後は行わない）:
    **関数の開始直後**（query/world の検証より前）・**ルートごとの走査開始時**（複数 root 間の
    境界）・**ツリー列挙中**（`root.rglob("*")` を `_DEADLINE_CHECK_ENTRIES` 件処理するごと・
    `sorted(...)` は列挙完了まで戻らないためソートの**前**に確認する）・**各エントリの処理直前**
    （ソート済み集合を1件処理するごと）・**ファイル読込直後**（ストリーミング走査に入る前）・
    **行走査ループ内**（`_DEADLINE_CHECK_LINES` 行ごと）・**各 `return` の直前**。
    超過していない通常経路の最終的な列挙順序（ソート結果）・ヒット内容はこれまでと変わらない
    （`deadline is None` の分岐は追加の `time.monotonic()` 呼び出しをしない）。PART-4
    （`agentic_search.run_tool`経由）が残り時間ベースで渡す・通常チャット経路は渡さない。
    """
    def _check_deadline() -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")

    _check_deadline()
    from . import corpus_docs, scope, worlds           # 遅延 import（循環回避）
    from .ingest import importance                     # 同上（importance が worlds を import するため）
    q = (query or "").strip()
    if not q or not valid_world(world):
        return []
    ql = q.lower()
    # (root, is_derived) のリスト。derived＝Office→決定的MD の置き場（rel は元 Office に対応＝末尾 .md を剥がす）。
    imp_map: dict = {}
    if roots is not None:
        roots_spec = [(Path(r), False) for r in roots]
        # `roots` 明示指定（テスト／個別パス指定の呼び出し）は世界の登録 root と一致する保証が
        # 無いため重要度は解決しない（`imp_map` は空のまま＝全ヒット rank 均一・§I2）。
    else:
        wd = worlds.world_dir(world)
        roots_spec = [(wd, False)] if wd else []
        # rag（RAG 正本）と md（人間用・legacy 縮退）は§8.1 三階層のフォルダ分離で別ディレクトリ
        # ——両方を is_derived ルートとして歩く。優先順位判定（下の `preferred_derived_name`）は
        # `der_rag` を固定で参照するため、どちらのルートを走査中でも同じ判定になる。
        der_rag = worlds.derived_rag_dir(world)
        if der_rag.is_dir():
            roots_spec.append((der_rag, True))
        der_md = worlds.derived_md_dir(world)
        if der_md.is_dir():
            roots_spec.append((der_md, True))
        # OCR 観測（画像の中の文字）は`rag.md`へ統合済み（O1・§8.1一本化）——VLMと合流した
        # AI観測レコードとして`.rag.md`自体に含まれるため、ここで観測専用ツリー
        # （`worlds.observation_current_dir`・`{rel}.rag_observations.md`）を別途歩く必要はない
        # （二重ヒットを作らない）。観測ツリー自体は generation GC の対象として引き続き存在しうる。
        # I2（2026-09-05）: ヒットの優先順位付け（`_offer` 参照）用に world の重要度を1回だけ解決する
        # （`_重要度.txt` が無い world は空 dict＝以下のヒープ処理が rank 均一のまま完全にno-op化する）。
        if wd:
            # RV是正（rv-i2-importance #3・2026-09）: `sig` を渡さないと `resolve_for_world` は
            # `worker.world_signature_of_root(wd)` で world 全体をもう一度全木走査してキャッシュ
            # キー用の署名を作ってしまう（`_read_all_control_contents` 自身の走査とは別の、もう1回の
            # 走査）。grep は1回のチャット往復（agentic ループ）で何度も呼ばれうるため、この二重
            # 走査コストを毎回払うのは無駄——registry の `last_sig`（狭い1行 SELECT・
            # `store.get_world_status_row` 参照・`last_manifest` を含まないため O(1)）を渡せば1回
            # 省ける（`doc_ledger.preview_documents`/`preview_service.build_preview` と同じ流儀）。
            # 取得できなくても（DB 不達・未登録 world 等）fail-closed にはせず `sig=None` のまま
            # 従来どおり自前計算へフォールバックする（grep 自体は DB 不要で動く契約を壊さない・
            # 空文字は渡さない＝`resolve_for_world` 側の署名キャッシュ契約を固定してしまわない）。
            sig = None
            try:
                from . import store                     # 遅延 import（循環回避）
                row = store.get_world_status_row(world)
                sig = (row or {}).get("last_sig") or None
            except Exception:
                sig = None
            imp_map = importance.resolve_for_world(world, root=wd, sig=sig)
    # ---- top-K（優先度つき）ヒット選抜（I2）----
    # 早期打切り（旧: ファイル内でヒット数到達時に break／ファイル境界でヒット数到達時に return）は
    # 撤去し、常に対象を全量走査する。ヒットは `_offer` を通じて上限 `max_hits` の有界ヒープへ
    # 出し入れし、`(rank, -seq)` の昇順（＝重要度が高いほど・同rankは発見順が早いほど）で最下位を
    # 追い出す——メモリは常に高々 `max_hits` 件。`seq` は全ルート・全ファイルを通した発見順の
    # 単調増加カウンタ（同一 rank 内の tie-break・heapq の比較がタプル要素だけで完結する保証にも使う
    # ＝dict である hit 本体同士の比較には決して落ちない）。`imp_map` が空なら全ヒット rank が
    # `importance.RANK_UNSET` で揃うため、選抜結果は「発見順で先頭 max_hits 件」＝旧実装と完全に
    # 同じ集合・同じ順序になる（受け入れ条件＝`_重要度.txt` の無い world で出力不変）。
    heap: list[tuple[int, int, dict]] = []
    seq = 0

    def _offer(hit: dict) -> None:
        nonlocal seq
        seq += 1
        if max_hits <= 0:
            return
        res = imp_map.get(hit["doc_id"])
        rank = importance.rank_of(res)
        hit.update(importance.public_fields(res))   # importance/importance_reason（条件付き・§I2 実装1）
        entry = (rank, -seq, hit)
        if len(heap) < max_hits:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            heapq.heapreplace(heap, entry)

    stop_scan = False   # コーディネータ裁定（rv-i2-importance #2・2026-09-05・再判定・ファイル境界の打切り点）
    for root, is_derived in roots_spec:
        _check_deadline()
        if not root.is_dir():
            continue
        rootr = root.resolve()
        entries = []
        for i, p in enumerate(root.rglob("*")):
            if (deadline is not None and i > 0 and i % _DEADLINE_CHECK_ENTRIES == 0
                    and time.monotonic() > deadline):
                raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
            entries.append(p)
        for p in sorted(entries):
            # 各エントリの処理（ファイル読込・全文走査を含む）ごとに確認する——列挙段階の
            # 間引きチェック（`_DEADLINE_CHECK_ENTRIES` 件ごと）だけでは、列挙後のファイル
            # 読込/走査自体で予算を使い切るケース（列挙件数が間引き間隔未満の小規模ディレクトリ
            # を含む）を検知できない。
            _check_deadline()
            ext = p.suffix.lower()
            ok_ext = (ext in _MD_EXT) if is_derived else (ext in _TEXT_EXT)
            if not (p.is_file() and not p.is_symlink() and ok_ext):
                continue
            try:
                rel = p.resolve().relative_to(rootr).as_posix()
            except ValueError:
                continue
            if importance.is_importance_control_path(rel):   # 重要度設定ファイル自体は検索対象外（§5）
                continue
            if is_derived and (rel.endswith(_RAG_SUFFIX) or rel.endswith(".md")):
                # `.rag.md`（Evidence IR 由来の検索向け Markdown）と legacy `{原本rel}.md` は
                # 同じ原本 rel に対する2つの物理ファイルになり得る。`preferred_derived_name()`
                # （grep と read_around の共有ヘルパー）が選ぶ側だけを検索対象にし、選ばれない側は
                # 除外する（1文書につき検索対象は常に1ファイルのみ＝二重ヒットを作らない）。
                origin_rel = strip_derived_suffix(rel)
                if preferred_derived_name(der_rag, origin_rel) != rel:
                    continue
                rel = origin_rel
            if not scope.in_scope(rel, scope_paths):  # 範囲外の文書はそもそも読まない
                continue
            is_code = False
            if not is_derived:
                # コード解析層と同じ単一の判定（`corpus_docs.classify_document`）を実行ゲートにする
                # （`_TEXT_EXT` は高速な事前フィルタに留め、最終判定はここに集約する・§7 裁定10）——
                # accepts() 内容判定に必要なヘッダが読み取れない文書は除外し（この1件だけ skip・
                # 検索全体は継続）、accepts() が全滅した登録拡張子は既存の資料種別（doctype）に
                # 該当する場合だけ資料として扱う（該当しなければ未対応＝CODE_EXT の集合だけで
                # 「コード」と見なさない）。ほとんどの拡張子（登録済みコード拡張子でも既定 accepts
                # のみなら）は候補が無い/内容を読まないので判定コストは増えない。
                verdict = corpus_docs.classify_document(
                    rel, Path(rel).suffix.lower(), lambda p=p: corpus_docs._read_head(p))
                if verdict["kind"] == "unreadable":
                    continue
                if verdict["kind"] != "code" and verdict.get("doctype") is None:
                    continue
                # TEXT-ALL L-1 是正（2026-09）: 軽量テキスト枠（`ingest.text_kind`）だけは、台帳/ES
                # と同じ基準（`text_kind.MAX_BYTES`＝8MiB・`corpus_docs._text_oversize` と同一の
                # 判定式）でサイズ超過を grep からも除外する。是正前は台帳側が `size_exceeded` で
                # `unreadable` にしていても grep 側は `classify_document` の判定だけをゲートに
                # ファイルサイズを見ておらず、`_GREP_FILE_CAP_BYTES`（64MiB）内の先頭部分で
                # ヒットを返せてしまっていた（「grep はヒットするのに台帳/引用検証には存在しない」
                # 矛盾窓・受容記録 TEXT-ALL L-1）。登録拡張子コード・Office 派生 MD・`.md`/`.txt` は
                # `verdict["doctype"]` がこの2値と一致しないため対象外のまま（従来どおり
                # `_GREP_FILE_CAP_BYTES` まで検索する）。黙って消さず、`truncated_docs`
                # （打ち切られた文書 doc_id の既存申告流儀）へ伝える——ヒットが1件も無い打切りも
                # 無音にしない、という既存契約と同じ扱い。
                if verdict.get("doctype") in (text_kind.CODE_DOCTYPE_LABEL, text_kind.DOCUMENT_DOCTYPE_LABEL):
                    try:
                        oversize = p.stat().st_size > text_kind.MAX_BYTES
                    except OSError:
                        oversize = False
                    if oversize:
                        if truncated_docs is not None and rel not in truncated_docs:
                            truncated_docs.append(rel)
                        continue
                is_code = verdict["kind"] == "code"
            # 層判定（§3.4）は上の classify_document 確定結果を使う（`layer_mod.in_layer`＝拡張子
            # だけの近似は使わない・§7 裁定10 と同じ確定判定に揃える）。派生 MD（Office/画像）は
            # 常に docs 層（is_code=False のまま）。
            if not layer_mod.in_layer_code(is_code, layer):
                continue
            # ストリーミング走査（本丸・2026-09）: `_CappedStreamReader` が bounded chunk で読み、
            # 改行区切りの行を順に yield する（境界セマンティクス＝「1 byte 余分に読んでちょうど
            # cap のファイルを誤って truncated 扱いしない」「cap で切れた中途行は破棄する」は
            # 旧 `f.read(cap + 1)` 一括ロードと同じ）。保持するのは現在の窓（MD なら直近の見出し行と
            # その行番号・節の引用は `_GREP_HIT_TEXT_MAX_BYTES` で頭打ち／ソースは前後2行の小窓）
            # だけで、`_GREP_FILE_CAP_BYTES` の大きさにもファイル実サイズにも比例しない。
            #
            # 持続的な OSError（権限変更・デバイス障害等）で検索全体を失敗させない——この1件だけ
            # skip して他の文書の検索を続ける（ストリーミング中の途中失敗はここまでに見つかった
            # ヒットを残したまま次のファイルへ進む＝全量ロード一発読みには無かった部分成功だが、
            # 打切り申告と同じ「探せていない範囲がある」の一種として許容する）。
            try:
                f = p.open("rb")
            except OSError:
                continue
            try:
                # 「ファイル読込直後（全文走査に入る前）」の確認（旧実装からの位置は保つ——
                # ストリーミングでは巨大 decode は起きないが、open 直後〜走査開始前の境界として
                # 引き続き確認する）。
                _check_deadline()
                reader = _CappedStreamReader(f)
                is_md = is_derived or ext in _MD_EXT
                out_ext = Path(rel).suffix.lower()      # doc_id（元ファイル）の拡張子で表示
                # RV是正（rv-i2-importance #1・2026-09）: `seen`（隣接ヒット窓の同一 span 重複排除）を
                # ファイル内の全ヒット数に比例して肥大する `set` のまま持たない——1ファイルに
                # マッチが大量にある病的ケース（secRV 2026-07-19 が対策した「共有フォルダに巨大
                # テキストを置ける主体」と同種の攻撃面）では、この1ファイル内だけで `seen` が
                # ヒット総数ぶん際限なく増える。重複が起こり得るのは常に**直近**の窓どうし
                # （非MD: `pending` は「まだ2行分の確認猶予中」のヒットしか保持しない設計のため
                # 定常時は高々2〜3件・MD: セクション境界は単調増加するため遠く離れた節どうしが
                # 衝突することは無い）——`maxlen` 付き `deque` による小さな有界窓で十分に同じ
                # 重複排除効果が得られる（`key in seen` は O(_SEEN_RECENT_MAX) の線形走査だが
                # 窓が小さいため無視できるコスト）。
                seen: deque = deque(maxlen=_SEEN_RECENT_MAX)
                line_i = 0
                if is_md:
                    section_start = 1
                    section_has_hit = False
                    section_hit_line = 0
                    section_buf: list[str] = []
                    section_buf_bytes = 0
                    section_capped = False
                else:
                    recent: deque = deque(maxlen=5)   # 直近5行（前後2行窓の復元に必要な最小限）
                    pending: list[int] = []           # まだ確定していないヒット行（1-based）

                def _add_hit(hit_line: int, s: int, e: int, text: str) -> None:
                    key = (str(p), s, e)
                    if key in seen:
                        return
                    seen.append(key)
                    # RV是正（rv-i2-importance #1）: ファイル単位のバッファ（旧 `file_hits`）へ溜めず、
                    # 見つけ次第すぐ世界全体の top-K ヒープへ供せる（`_offer` は既に有界＝高々
                    # `max_hits` 件しか保持しない）。`file_truncated` の付与（この1ファイルの走査を
                    # 終えるまで確定しない）は、ヒープに残っている（このファイル由来の）エントリを
                    # 事後に見つけて付与する形にする（下の該当箇所参照・ヒープは有界なのでこの事後
                    # 走査も高々 `max_hits` 件で終わる）。
                    _offer({
                        "doc_id": rel,                # world root 相対パス（来歴・DL キー・§2.2）
                        "path": str(p),                # 内部用（物理パス）。API 露出は lens 層で除去。
                        "ext": out_ext,
                        "line": hit_line,
                        "span": [s, e],
                        "text": text,
                        "match": q,
                    })

                def _emit_md_section(end_line: int) -> None:
                    nonlocal section_has_hit
                    if not section_has_hit:
                        return
                    text = "\n".join(section_buf)
                    if not section_capped:
                        text = text.strip()
                    text = _clip_utf8_bytes(text, _GREP_HIT_TEXT_MAX_BYTES)
                    _add_hit(section_hit_line, section_start, end_line, text)
                    section_has_hit = False

                hit_limit_reached = False   # コーディネータ裁定（rv-i2-importance #2・2026-09-05・再判定）
                try:
                    for t in _logical_lines(reader, _GREP_FILE_CAP_BYTES):
                        if (deadline is not None and line_i > 0 and line_i % _DEADLINE_CHECK_LINES == 0
                                and time.monotonic() > deadline):
                            raise GrepDeadlineExceeded("grep 走査がデッドラインを超えました")
                        line_no = line_i + 1
                        if is_md:
                            is_heading = t.lstrip().startswith("#")
                            if is_heading:
                                _emit_md_section(line_no - 1)
                                section_start = line_no
                                section_buf = []
                                section_buf_bytes = 0
                                section_capped = False
                            if not section_capped:
                                sep = 1 if section_buf else 0
                                section_buf.append(t)
                                section_buf_bytes += sep + len(t.encode("utf-8"))
                                if section_buf_bytes >= _GREP_HIT_TEXT_MAX_BYTES:
                                    section_capped = True
                            if ql in t.lower() and not section_has_hit:
                                section_has_hit = True
                                section_hit_line = line_no
                        else:
                            recent.append((line_no, t))
                            if ql in t.lower():
                                pending.append(line_no)
                            while pending and pending[0] <= line_no - 2:
                                h = pending.pop(0)
                                s, e = max(1, h - 2), h + 2
                                text = "\n".join(txt for (ln, txt) in recent if s <= ln <= e)
                                _add_hit(h, s, e, _clip_utf8_bytes(text, _GREP_HIT_TEXT_MAX_BYTES))
                        line_i += 1
                        # RV是正 再判定（rv-i2-importance #2・コーディネータ裁定2026-09-05）: `imp_map`
                        # が空（rank一様）でヒープが `max_hits` で満杯なら、以後どのファイル・どの行の
                        # ヒットも数学的に二度とヒープへ採用されない（min-heap の比較キー `(rank, -seq)`
                        # は rank が一様なとき新エントリの `-seq` が既存最小値より必ず小さくなるため
                        # `entry > heap[0]` が恒に False になる証明・モジュール docstring 参照）。
                        # ファイル内break＝旧実装と同じ打切り点（この時点で MD 最終節／未確定 pending
                        # 行の flush は**行わない**＝旧実装の取りこぼし挙動を再現する）。
                        if not imp_map and len(heap) >= max_hits:
                            hit_limit_reached = True
                            break
                    if not hit_limit_reached:
                        if is_md:
                            _emit_md_section(line_i)
                        else:
                            for h in pending:
                                s, e = max(1, h - 2), min(line_i, h + 2)
                                text = "\n".join(txt for (ln, txt) in recent if s <= ln <= e)
                                _add_hit(h, s, e, _clip_utf8_bytes(text, _GREP_HIT_TEXT_MAX_BYTES))
                except OSError:
                    pass
            finally:
                f.close()
            # ファイル全体（cap まで）の走査を終えた時点で「探せていない範囲があるか」が確定する。
            # この1ファイル由来でヒープに**現に残っている**エントリへ一律に適用する
            # （`agentic_search.run_tool` の read_doc/doc_outline 分岐が返す `file_truncated` と
            # 同じ語彙・同じ意味）。ヒットは既に `_add_hit`→`_offer` で見つけ次第ヒープへ供給済み
            # （RV是正 #1・file_hits バッファは撤去）——この時点で他ファイルに追い出されず残って
            # いるものだけが最終的に出力されうるため、ヒープ（高々 `max_hits` 件・有界）を1回
            # 走査して `path` が一致するものにだけ付ける。理由が無ければキーを作らない既存の
            # 流儀に合わせ、打切りが無い通常のヒットは従来どおりキー無し＝戻り値の形が完全に不変。
            effective_truncated = reader.truncated or reader.line_overflowed
            if effective_truncated:
                p_str = str(p)
                for _rank, _neg_seq, h in heap:
                    if h["path"] == p_str:
                        h["file_truncated"] = True
                if truncated_docs is not None and rel not in truncated_docs:
                    truncated_docs.append(rel)
            # RV是正 再判定（rv-i2-importance #2・コーディネータ裁定2026-09-05）: ファイル境界の
            # 打切り点（旧実装と同じ「ヒット数到達時の return」の意味論）——`imp_map` が空（rank一様）
            # でヒープが `max_hits` で満杯なら、以後のファイル・root を一切開かない（`_check_deadline()`
            # を含む以降の周期チェックも実行されない＝旧実装の deadline 消費水準に戻る）。
            if not imp_map and len(heap) >= max_hits:
                stop_scan = True
                break
        if stop_scan:
            break
    _check_deadline()
    # heap は有界（高々 max_hits 件）——最終順序だけ `(-rank, seq)` 昇順（重要度が高いほど先・
    # 同rankは発見順）へ並べ替えて返す。`entry`=(rank, -seq, hit) なので、
    # 望む並び順のキーは (-rank, seq) == (-entry[0], -entry[1])。
    heap.sort(key=lambda entry: (-entry[0], -entry[1]))
    return [hit for _rank, _neg_seq, hit in heap]
