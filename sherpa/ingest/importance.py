"""文書の重要度（鏡モデル・登録フォルダ内の設定ファイルで付与・docs/03-鏡モデル.md）。

各フォルダに置ける `_重要度.txt`（1行1パターン＝`パターン: 高|中|低|なし  # 理由`）を解析し、
world 内の各 rel_path へ**階層継承**で解決する。解決規則:
「一致する規則を持つ最深の祖先」が勝つ（規則を持たない祖先の設定ファイルはスキップしてさらに
上へ遡る）。同一ファイル内は glob（`*` 以外のパターン）がフォルダ既定（`*`）より優先し、
複数一致は後に書かれた規則が勝つ（後勝ち）。`なし` は祖先の指定を打ち消し完全中立へ戻す
（それより上の祖先へは遡らない＝終端の判定）。値・理由が一切一致しなければフィールド自体を
持たない（`resolve_for_world`/`resolve_many` の戻り dict にキーが現れない）。

`_重要度.txt` 自体は文書として扱わない（`is_importance_control_path` が単一の判定関数・世界の
文書一覧／範囲ツリー／文書実在集合／グラフの文書集合など、全ての走査入口が個別にこれを呼んで
除外する）。ただし world のファイル署名（`ingest.worker.world_signature`）には残す——設定の
追加・変更・削除自体が world 内容の変化として検知される必要があるため。

構文エラーは**行単位**で無効化する（他の有効な行は生きる）。壊れた行・上限超過は
`Diagnostic` として集約し、値の解決には使わない。resolver の結果は `(world_id, 解決済み
root の実パス, 実効署名)` の3要素をキーにプロセス内キャッシュする（実効署名＝world_signature
＋ `_重要度.txt` の内容ハッシュ・`resolve_for_world` 参照。world の内容が変わればキャッシュも
自動的に無効化される）。
"""
from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .. import scope_infer, worlds

_log = logging.getLogger("sherpa")

CONTROL_FILENAME = "_重要度.txt"

# 重要度機能のスキーマ版。`ingest.worker.world_signature` の材料に畳み込む——この版を
# 上げると、ソースファイル自体が変化していない world でも署名が変わり、標準の
# 「署名不一致→全再構築」経路で自動的に full rebuild される（`build_world` は制御ファイルを
# 除外済みなので、旧コードが `_重要度.txt` を通常文書として台帳化していた world もこの
# 1回の rebuild で台帳/Neo4j から消える）。
# 2（RV2是正#a1・2026-09-01）: `store.documents` に importance/importance_reason/importance_source
# 列を追加し、ingest 時（`ingest/worker.py::_ledger_rows`）に materialize するようにした
# （GET /documents の台帳高速経路がこれを実走査せず返す）。version=1 のまま署名一致する既存
# world は通常の sync が unchanged 経路に入り `_ledger_rows` を再実行しない＝旧行は3列とも
# NULL のまま固定されてしまう。版を上げて次回 sync で強制的に再構築させる。
IMPORTANCE_SCHEMA_VERSION = 2

_VALUES = frozenset({"高", "中", "低", "なし"})

# 単一 worker を前提にした決定性・可用性の境界（インターネット向けの防御ではない）。
_MAX_TOTAL_BYTES = 64 * 1024      # 設定ファイル1個の総バイト数上限
_MAX_RULES_PER_FILE = 500         # 設定ファイル1個あたりの有効な規則数上限
_MAX_PATTERN_LEN = 260            # 1パターンの文字数上限
_MAX_REASON_BYTES = 600           # 理由1行の UTF-8 バイト数上限（日本語で概ね200文字相当）

# 制御文字（タブ含む C0＝\x00-\x1F・DEL＝\x7F・C1＝\x80-\x9F・Unicode 行/段落区切り＝U+2028/U+2029）
# は理由に使えない（理由は1行の契約のため、行区切りとして解釈されうる文字は種類を問わず拒否する）。
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_CACHE_MAX = 64   # プロセス内 resolver キャッシュの上限つき LRU（複数 world を扱う開発環境向けの控えめな上限）
_CACHE: "OrderedDict[tuple[str, str, str], dict[str, Resolution]]" = OrderedDict()


@dataclass(frozen=True)
class Rule:
    """`_重要度.txt` の有効な1行（構文エラーではない行）。"""
    pattern: str
    value: str                # 高/中/低/なし
    reason: str | None
    line: int


@dataclass(frozen=True)
class Diagnostic:
    """設定ファイルの構文診断（台帳の `control_diagnostics` として表示）。"""
    config_path: str
    line: int | None          # ファイル全体に対する診断（総バイト数超過等）は None
    column: int
    code: str
    message: str


@dataclass(frozen=True)
class Resolution:
    """1つの rel_path に対する解決結果（値が存在する場合のみ・§2 truth table の「無ければ無い」）。"""
    value: str                # 高/中/低（「なし」に解決した場合は Resolution を返さず None にする）
    reason: str | None
    config_path: str          # 勝者となった `_重要度.txt` の world 相対 rel_path（監査用）
    rule_line: int            # 勝者となった規則の行番号（監査用）


# I2（2026-09-05・経路別反映）: 検索/影響（grep のヒット優先順位・影響一覧の並び順）だけが使う
# **順位専用**のスケール（表示値ではない）。「未設定（`resolve_for_world` の戻り dict にキーが
# 無い）」は「中」と同格に扱う（旧提案 §7/§10 の「高>中/無>低」序列）——`_重要度.txt` を置いて
# いない world・登録者が明示的に「中」と書いた文書は同じ優先度で並ぶ。表示（`public_fields`）は
# これとは独立に「無ければ無い」の従来契約のまま（未設定を「中」と偽って見せない）。
RANK: dict[str, int] = {"高": 2, "中": 1, "低": 0}
RANK_UNSET = 1   # 未設定（dict にキーが無い rel）の rank（＝「中」と同格）


def rank_of(res: "Resolution | None") -> int:
    """順位専用スケール（`RANK`）での rank。未解決（`res is None`）は `RANK_UNSET`。"""
    return RANK.get(res.value, RANK_UNSET) if res is not None else RANK_UNSET


def public_fields(res: "Resolution | None") -> dict:
    """grep ヒット／ES メタ／影響一覧／出典など**外部へ見せる**経路が共通で使う2キー表示形。

    `importance`/`importance_reason` のみ（J4・2026-09-05）——`importance_source`（`_重要度.txt`
    の rel_path・行番号）は台帳/管理画面専用の由来監査情報であり、検索/影響/出典には出さない。
    `res is None`（値なし）なら空 dict（キー自体を持たない・§2 truth table）。
    """
    if res is None:
        return {}
    out = {"importance": res.value}
    if res.reason:
        out["importance_reason"] = res.reason
    return out


def is_importance_control_path(rel_path: str) -> bool:
    """`rel_path`（world root 相対 POSIX）が重要度設定ファイルか（単一の判定関数・全入口が呼ぶ）。"""
    if not rel_path:
        return False
    return PurePosixPath(rel_path).name == CONTROL_FILENAME


def _parent_rel(rel: str) -> str:
    """rel_path → それを含むフォルダの rel_path（root 直下は `""`）。"""
    return "/".join(rel.split("/")[:-1])


def _ancestor_folders_deepest_first(rel: str) -> list[str]:
    """rel_path の祖先フォルダを深い順に列挙する（root=`""` を含む・§3 の遡り順）。"""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _match_segment_glob(pattern: str, rel: str) -> bool:
    """セグメント単位の glob マッチ（`*`/`?`/`[seq]` は1セグメント内のみ・`**` だけが複数セグメントを跨ぐ）。

    `fnmatch.fnmatch()` は使わない（OS 依存の大小文字畳み込み・`*` がセグメントを跨いでしまうため）。
    """
    return _match_segments(pattern.split("/"), rel.split("/"))


def _match_segments(pat_segs: list[str], rel_segs: list[str]) -> bool:
    """`pat_segs` が `rel_segs` に一致するかを動的計画法で判定する（`**` の再帰を素朴な二分探索に
    しない＝連続する `**` を含むパターンで指数時間になるのを避ける・O(len(pat_segs)×len(rel_segs))）。

    `dp[j]` は「現在処理中の `i` 番目以降の pattern セグメントが `rel_segs[j:]` に一致するか」
    （末尾 `i=len(pat_segs)` を起点に `i` を減らしながら更新）。
    """
    n, m = len(pat_segs), len(rel_segs)
    dp = [False] * (m + 1)
    dp[m] = True                                        # 両方尽きた（i=n・j=m）＝一致
    for i in range(n - 1, -1, -1):
        seg = pat_segs[i]
        new_dp = [False] * (m + 1)
        if seg == "**":
            new_dp[m] = dp[m]                            # ** が残り0セグメントを消費
            for j in range(m - 1, -1, -1):
                new_dp[j] = dp[j] or new_dp[j + 1]       # 0個消費してiを進める／1個以上消費してiは据え置き
        else:
            for j in range(m):                           # rel が尽きていれば非**セグメントは一致し得ない
                new_dp[j] = fnmatch.fnmatchcase(rel_segs[j], seg) and dp[j + 1]
        dp = new_dp
    return dp[0]


def _parse_line_full(line: str):
    """1行を解析する単一の実装。戻り値は次のいずれか:

    - `((pattern, value, reason), None, None)` — 有効な規則行。
    - `(None, None, None)` — 空行／`#` で始まるコメント行（構文エラーではない）。
    - `(None, code, message)` — 構文エラー（呼び出し元が `Diagnostic` を組み立てる）。

    理由の制御文字チェックは**`strip()` する前の生テキスト**に対して行う——`str.strip()` は
    NEL（`\\x85`）等の一部の制御文字も「空白」として端から黙って落としてしまうため、`strip()`
    してから検査すると理由の先頭・末尾に置かれた制御文字がすり抜ける（迂回）。行頭の空白は
    `lstrip()` だけで落とし（行末側は理由の生テキストを保持するため触らない）、パターン/値は
    それぞれの部分文字列ごとに `strip()` する。
    """
    blank_check = line.strip()
    if not blank_check or blank_check.startswith("#"):
        return None, None, None
    s = line.lstrip()
    if ":" not in s:
        return None, "no_colon", "書き方が正しくありません。「パターン: 高」のように、コロン（:）で区切って書いてください"
    pattern_part, rest = s.split(":", 1)
    pattern = pattern_part.strip()
    if not pattern:
        return None, "empty_pattern", "パターンが空です。対象にするファイル名やフォルダ名を書いてください"
    if len(pattern) > _MAX_PATTERN_LEN:
        return None, "pattern_too_long", f"パターンが長すぎます（{_MAX_PATTERN_LEN}文字まで）。短く書き直してください"
    if "#" in rest:                                    # 最初の # で分離＝理由自体に # を含められる
        value_part, reason_raw = rest.split("#", 1)
    else:
        value_part, reason_raw = rest, None
    value = value_part.strip()
    if value not in _VALUES:
        # 診断メッセージは画面にそのまま表示するため、入力値そのものは反射しない固定文言にする。
        return None, "invalid_value", "値が正しくありません。「高」「中」「低」「なし」のいずれかにしてください"
    reason = None
    if reason_raw is not None:
        if _CONTROL_CHAR_RE.search(reason_raw):          # strip() する前の生テキストを検査（迂回防止）
            return None, "reason_control_char", "理由に使えない文字が含まれています。改行やタブは使わずに書いてください"
        reason = reason_raw.strip() or None
        if reason is not None and len(reason.encode("utf-8")) > _MAX_REASON_BYTES:
            return None, "reason_too_long", "理由が長すぎます。短くまとめてください"
    return (pattern, value, reason), None, None


def _parse_line(line: str) -> tuple[str, str, str | None] | None:
    """1行を `(pattern, value, reason)` に解析する。空行・コメント行・構文エラーは `None`。"""
    parsed, _code, _message = _parse_line_full(line)
    return parsed


def _read_control_bytes(path: Path, cfg: str) -> tuple[bytes | None, Diagnostic | None]:
    """`_重要度.txt` の安全な読み取り（stat→上限判定→読み込み上限つき読み取り→実バイト数再検査）。

    解析（`parse_control_file`）と署名計算（`_read_all_control_contents`）が**同じ読み取り経路を
    共有する**単一の実装。`stat()` による事前判定は最適化（大きいと分かっているファイルを開かずに
    済ませる）であって、安全性の保証はそれ自体ではない——`stat()` と実際の読み取りの間にファイルが
    増量した場合（TOCTOU）に、事前判定を素通りしてから無制限に読み込んでしまうと、事前判定は
    意味を失う。**読み取り操作自体を `_MAX_TOTAL_BYTES + 1` バイトに上限する**ことで、実際に
    メモリへ載るバイト数を常にこの上限内に固定する（`stat()` がどんな値を報告していても）。
    読み取り自体が失敗した場合（権限変更・共有ドライブの切断等）も診断にする（ログにも記録）。

    読めた場合は `(raw, None)`、読めない/上限超過の場合は `(None, Diagnostic)`。呼び出し元は
    後者を「判定不能」として扱う（`parse_control_file` は空設定として黙って通さず診断を返す・
    `_read_all_control_contents` はキャッシュ不能として扱う）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        _log.warning("importance: failed to stat control file %s", cfg)
        return None, Diagnostic(cfg, None, 1, "read_error",
                                "設定ファイルを読み取れませんでした。アクセス権限や共有状態を確認してください")
    if size > _MAX_TOTAL_BYTES:
        return None, Diagnostic(cfg, None, 1, "file_too_large",
                                f"ファイルが大きすぎます（{_MAX_TOTAL_BYTES // 1024}KBまで）。行数を減らしてください")
    try:
        with path.open("rb") as f:
            raw = f.read(_MAX_TOTAL_BYTES + 1)           # 読み取り自体を上限＋1に制限（TOCTOU でも安全）
    except OSError:
        _log.warning("importance: failed to read control file %s", cfg)
        return None, Diagnostic(cfg, None, 1, "read_error",
                                "設定ファイルを読み取れませんでした。アクセス権限や共有状態を確認してください")
    if len(raw) > _MAX_TOTAL_BYTES:                      # stat 後に増量した場合も実バイト数で再検査
        return None, Diagnostic(cfg, None, 1, "file_too_large",
                                f"ファイルが大きすぎます（{_MAX_TOTAL_BYTES // 1024}KBまで）。行数を減らしてください")
    return raw, None


def _parse_control_bytes(raw: bytes, cfg: str) -> tuple[list[Rule], list[Diagnostic]]:
    """既に読み込んだバイト列を解析する純関数（I/O はしない）。

    `parse_control_file`（読み取り＋解析）と、事前に world 単位で1回だけ読んだバイト列を
    使い回す `_compute_for_world` の両方から呼ばれる——同じファイルを2回読まない（署名計算
    （`_read_all_control_contents`）で読めたのに解析だけ別内容/失敗、という乖離を構造的に
    無くす）。

    デコードは**行単位**で `errors="strict"` を使う（`"replace"` は不正なバイト列を黙って
    U+FFFD へ置き換えてしまい、壊れたファイルを気付かせずに読み進めてしまう）。ファイル
    全体を1回で decode すると、どこか1行にでも不正なバイト列があれば正しい他の行まで
    巻き添えで全規則が破棄されてしまい、§8「エラー行だけ無効・他の有効行は生きる」に反する
    ——行区切り（`\\n`/`\\r\\n` のみ）は decode 前の**バイト列のまま**検出し（UTF-8 の
    継続バイトは常に `0x80` 以上のため `\\n`（`0x0A`）がマルチバイト文字の一部になることは無く、
    バイト列のまま安全に分割できる）、各行を個別にデコードする——不正な行だけ `invalid_encoding`
    診断にして無効化し、他の正しくデコードできた行は普通に解析する（`str.splitlines()` は
    NEL（`\\x85`）や LINE/PARAGRAPH SEPARATOR（`\\u2028`/`\\u2029`）等も行区切りとして
    扱ってしまうため使わない——理由に埋め込まれたこれらの制御文字が「行の分割」によって
    消費され、制御文字チェック（`_parse_line_full`）まで届かない迂回を許してしまう）。

    許可される行区切りは `\\n`／`\\r\\n` のみ——`\\n` に**先行する** `\\r` だけを取り除く
    （`raw.split(b"\\n")` の断片のうち、末尾以外は必ず直後に `\\n` があったので安全に取り除ける。
    末尾の断片はファイルが `\\n` で終端していれば空文字列、終端していなければ「まだ改行されて
    いない最後の行」であり、そこに現れる `\\r` は CRLF の一部ではなく本文中の生の `\\r`
    （制御文字）——黙って CRLF 扱いで剥がすと、理由フィールドに混入した制御文字を検出する
    `_parse_line_full` の制御文字チェックを迂回してしまう）。
    """
    rules: list[Rule] = []
    diagnostics: list[Diagnostic] = []
    capped = False
    byte_lines = raw.split(b"\n")
    last_index = len(byte_lines) - 1
    for i, byte_line in enumerate(byte_lines):
        line_no = i + 1
        if i != last_index and byte_line.endswith(b"\r"):
            byte_line = byte_line[:-1]                   # \r\n 対応（\n 直前の \r だけを取り除く）
        try:
            raw_line = byte_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            diagnostics.append(Diagnostic(cfg, line_no, e.start + 1, "invalid_encoding",
                                          "文字コードが正しくありません。UTF-8で保存し直してください"))
            continue
        parsed, code, message = _parse_line_full(raw_line)
        if parsed is None and code is None:
            continue                                    # 空行・コメント行
        if parsed is None:
            diagnostics.append(Diagnostic(cfg, line_no, 1, code, message))
            continue
        if len(rules) >= _MAX_RULES_PER_FILE:
            if not capped:
                diagnostics.append(Diagnostic(
                    cfg, line_no, 1, "too_many_rules",
                    f"規則の数が多すぎます（{_MAX_RULES_PER_FILE}行まで）。以降の行は読み込まれません"))
                capped = True
            continue
        pattern, value, reason = parsed
        rules.append(Rule(pattern=pattern, value=value, reason=reason, line=line_no))
    return rules, diagnostics


def parse_control_file(path: Path, *, config_rel: str | None = None) -> tuple[list[Rule], list[Diagnostic]]:
    """`_重要度.txt` 1個を読み取って解析する（行単位の構文エラー＝他の有効な行は生きる・§6/§8）。

    `config_rel`（省略可）: 診断・監査に載せる world 相対 rel_path。省略時は `path` の文字列表現
    （テスト等、world に紐付かない直接呼び出し向け）。読み取り（サイズ上限・失敗）は
    `_read_control_bytes`、解析（デコード・行分割・行ごとの構文チェック）は `_parse_control_bytes`
    を参照。バイト列を既に読み込み済みの呼び出し元（`_compute_for_world` が事前読み取り分を
    使う場合）は `_parse_control_bytes` を直接呼び、ここを経由してファイルを二重に読まない。
    """
    cfg = config_rel if config_rel is not None else str(path)
    raw, diag = _read_control_bytes(path, cfg)
    if raw is None:
        return [], [diag]
    return _parse_control_bytes(raw, cfg)


def _pick_winner(rules: list[Rule], rel_from_owner: str) -> Rule | None:
    """1つの設定ファイル内で `rel_from_owner` に一致する規則から勝者を選ぶ（§3.2: glob 優先・後勝ち）。"""
    matched = [r for r in rules if _match_segment_glob(r.pattern, rel_from_owner)]
    if not matched:
        return None
    globs = [r for r in matched if r.pattern != "*"]
    pool = globs if globs else matched
    return pool[-1]


_UNRESOLVABLE_DIAGNOSTIC_CODES = frozenset({"read_error"})


def _resolve_rel(rel: str, control_by_folder: dict[str, tuple[list[Rule], list[Diagnostic]]]) -> Resolution | None:
    """`rel` を階層継承で解決する（§3: 一致規則を持つ最深の祖先が勝つ・`なし` は終端）。

    `read_error`（I/O 失敗＝権限/共有ドライブ切断等）は判定不能として遡りを打ち切る——
    環境要因で「読めていないだけ」の祖先を、規則が無い祖先と混同して素通りしない
    （読めていれば `なし` だったかもしれない祖先を誤って飛び越え、祖父母の値を誤って
    適用しない・§8）。一方 `file_too_large`（上限超過）は正典どおりそのファイル自体の
    構文エラー扱い——ファイルサイズという決定的な事実であり一時的な障害ではないため、
    `parse_control_file`/`_read_control_bytes` が既に `rules=[]` を返している（下の
    `_pick_winner([], ...)` が自然に「一致規則なし」として次の祖先へ遡る＝§3 の通常経路）。
    """
    for folder in _ancestor_folders_deepest_first(rel):
        entry = control_by_folder.get(folder)
        if not entry:
            continue
        rules, diags = entry
        if any(d.code in _UNRESOLVABLE_DIAGNOSTIC_CODES for d in diags):
            return None                                  # 読み取れない＝判定不能・未設定（祖先へは遡らない）
        rel_from_owner = rel[len(folder) + 1:] if folder else rel
        winner = _pick_winner(rules, rel_from_owner)
        if winner is None:
            continue                                    # この設定ファイルに一致規則なし→さらに上へ遡る
        if winner.value == "なし":
            return None                                  # 明示解除＝完全中立（それより上へは遡らない）
        config_path = f"{folder}/{CONTROL_FILENAME}" if folder else CONTROL_FILENAME
        return Resolution(value=winner.value, reason=winner.reason,
                          config_path=config_path, rule_line=winner.line)
    return None


def resolve_one(world_dir: Path, rel: str, control_paths: list) -> Resolution | None:
    """`rel` 1件だけを、既知の `_重要度.txt` パス集合から解決する（小規模呼び出し・テスト用）。

    world 全体を対象にする場合は `resolve_for_world`（キャッシュ付き）を使う。
    """
    wd = Path(world_dir).resolve()
    control_by_folder: dict[str, tuple[list[Rule], list[Diagnostic]]] = {}
    for cp in control_paths:
        cp = Path(cp)
        try:
            crel = cp.resolve().relative_to(wd).as_posix()
        except (OSError, ValueError):
            continue
        if not is_importance_control_path(crel):
            continue
        control_by_folder[_parent_rel(crel)] = parse_control_file(cp, config_rel=crel)
    return _resolve_rel(rel, control_by_folder)


def _compute_for_world(wd: Path, control_contents: dict[str, bytes] | None = None,
                       control_errors: dict[str, Diagnostic] | None = None, *, files=None) -> dict[str, Resolution]:
    """world 内の全 rel_path を解決する。

    `control_contents`（`_read_all_control_contents` が world 単位で事前に1回だけ読んだ
    `{rel: raw_bytes}`）を渡せば、その場でファイルを再度読まずに `_parse_control_bytes` で
    解析する（`resolve_for_world` のキャッシュミス経路が使う・§重複読み取りをしない）。
    `control_errors`（同じく事前に得た `{rel: Diagnostic}`）を渡せば、読み取れなかった
    ファイルも再読して同じ診断を作り直したりせず、その診断をそのまま使う。どちらにも
    無い rel（省略時＝`resolve_one` 相当の単発呼び出し）だけ `parse_control_file` で読む。
    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(wd)` を1回
    materialize（`list(...)`）済みなら渡す（2回イテレートするのでここで消費される generator
    ではなく list であること・§③ 2026-09-01・`resolve_for_world`/`preview_service` 参照）。
    """
    files = files if files is not None else list(scope_infer.safe_files(wd))
    control_by_folder: dict[str, tuple[list[Rule], list[Diagnostic]]] = {}
    for rp, rel in files:
        if is_importance_control_path(rel):
            if control_errors is not None and rel in control_errors:
                control_by_folder[_parent_rel(rel)] = ([], [control_errors[rel]])
            elif control_contents is not None and rel in control_contents:
                control_by_folder[_parent_rel(rel)] = _parse_control_bytes(control_contents[rel], rel)
            else:
                control_by_folder[_parent_rel(rel)] = parse_control_file(rp, config_rel=rel)
    if not control_by_folder:
        return {}
    out: dict[str, Resolution] = {}
    for _rp, rel in files:
        if is_importance_control_path(rel):
            continue
        res = _resolve_rel(rel, control_by_folder)
        if res is not None:
            out[rel] = res
    return out


def _read_all_control_contents(wd: Path, *, files=None) -> tuple[dict[str, bytes], dict[str, Diagnostic]]:
    """world 内の全 `_重要度.txt` を**1回ずつ**読み、成功分を `{rel: raw_bytes}`、失敗分
    （読み取り不能/上限超過）を `{rel: Diagnostic}` に振り分けて返す。

    1件の失敗で他の成功分まで捨てない——`resolve_for_world` の fail-closed 経路
    （キャッシュを使わず直接計算に回す）でも、既に読めた分は再読せず、失敗分も同じ診断を
    作り直したりしない（`_compute_for_world` へそのまま引き継ぐ）。ここで読んだバイト列/
    診断は署名計算（`_control_content_signature`）と解析（`_compute_for_world` →
    `_parse_control_bytes`）の両方にそのまま渡され、ファイルを2回読まない。
    `files`（省略可・キーワード専用）: `_compute_for_world` と同じ意味（既に列挙済みなら渡す・
    与えられれば `scope_infer.safe_files` を呼ばない）。
    """
    contents: dict[str, bytes] = {}
    errors: dict[str, Diagnostic] = {}
    entries = files if files is not None else scope_infer.safe_files(wd)
    for rp, rel in entries:
        if is_importance_control_path(rel):
            raw, diag = _read_control_bytes(rp, rel)
            if raw is None:
                errors[rel] = diag
            else:
                contents[rel] = raw
    return contents, errors


def _control_content_signature(control_contents: dict[str, bytes],
                               control_errors: dict[str, Diagnostic] | None = None) -> str:
    """`_重要度.txt` の内容ハッシュを集約した署名（純関数・I/O はしない）。

    `world_signature`（`ingest.worker`）はファイルの rel/mtime/ctime/size という**メタデータのみ**
    で内容そのものは読まないため、(a) メタデータが変わらないまま設定ファイルの中身だけ書き換わった
    場合や、(b) rebind で別 root（別内容）に差し替わったのに偶然メタデータ署名が一致した場合を
    検知できない。resolver 専用にこの内容ハッシュを合成し、キャッシュキーへ反映する。

    `control_contents` は `_read_all_control_contents` が world 単位で1回だけ読んだバイト列。
    `control_errors`（省略可）は非致命的な診断（`file_too_large` のみ——`read_error` は
    呼び出し元がキャッシュ自体を経由しない fail-closed 経路に回すため、ここに来る時点で
    含まれない）を固定マーカーとして署名に含める。上限超過は内容を読んでいないため実ハッシュは
    作れないが、その値の違いを区別する必要も無い（常に「一致規則なし」として扱われるだけ）——
    ファイルサイズ自体の変化は `world_signature`（メタデータ署名）が既に検知するため、
    固定マーカーで十分（key に含めておけば、他の理由でキャッシュキーが偶然衝突しても
    上限超過中/解消後を取り違えない）。
    """
    parts = sorted((rel, hashlib.sha1(raw).hexdigest()) for rel, raw in control_contents.items())
    parts += sorted((rel, f"<{diag.code}>") for rel, diag in (control_errors or {}).items())
    parts.sort()
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()


def _files_rel_signature(files) -> str:
    """呼び出し側が渡した `files`（materialize 済み `(Path, rel)` の list）の rel 集合だけを
    対象にした決定的署名（純関数・追加 I/O なし・既に受け取った list を並べ替えるだけ）。

    RV2是正#a2: `resolve_for_world` へ呼び出し側が明示 `sig`（登録済み world の `last_sig`）を
    渡す経路（`preview_service.build_preview` 参照）では、`sig` は次回 sync まで変わらない。
    その間に非制御ファイルを追加/削除/rename しても、`sig`（メタデータ由来）にも
    `_control_content_signature`（`_重要度.txt` の内容 hash）にも現れず、キャッシュキーが
    変わらないまま古い解決結果を返してしまう——「グラフ以外は毎回フレッシュ」という契約
    （`build_preview` は文書一覧を毎回列挙する）に反する。この署名を実効署名へ畳み込むことで、
    対象ファイル集合が変われば（同一 `sig`・同一 `_重要度.txt` でも）キャッシュを確実に無効化する。
    """
    rels = sorted(rel for _rp, rel in files)
    return hashlib.sha1(repr(rels).encode("utf-8")).hexdigest()


def resolve_for_world(world_id: str, *, root=None, sig: str | None = None, files=None) -> dict[str, Resolution]:
    """world 内の全 rel_path を解決した dict（値が存在する rel だけを持つ・§2 truth table）。

    `root`（省略可）: 呼び出し側が既に world root を解決済みなら渡す（`worlds.world_dir()` を
    再度呼ばない）——文書列挙側（`corpus_docs`）と重要度解決側が別々に root を解決すると、その
    間隔の rebind で「旧 root の文書一覧」に「新 root の重要度」を付けてしまいうる
    （`doc_ledger.public_documents`/`preview_documents` が同一 root を共有して呼ぶ）。
    `files`（省略可・キーワード専用）: 呼び出し側が既に `scope_infer.safe_files(wd)` を1回
    materialize（`list(...)`）済みなら渡す（`_read_all_control_contents`/`_compute_for_world`
    双方へそのまま転送し、キャッシュミス時でも二重の全木走査を避ける・§③ 2026-09-01・
    `preview_service.build_preview` 参照）。キャッシュ**ヒット**時はそもそも歩かない（従来どおり）。

    キャッシュキーは `(world_id, 解決済み root の実パス, 実効署名)`。実効署名は `sig`
    （省略時は root から直接計算した world_signature）に `_重要度.txt` の内容ハッシュ
    （`_control_content_signature`）を合成したもの——メタデータ由来の署名だけでは検知できない
    「内容だけ変わった」「別 root なのに署名が偶然一致」を区別するため（root の実パスをキーに
    含めることで、万一署名が一致しても別 root の結果を取り違えない）。

    `_重要度.txt` は世界内で**1回だけ読む**（`_read_all_control_contents`）——読んだバイト列を
    内容ハッシュと解析（キャッシュミス時の `_compute_for_world`）の両方にそのまま渡し、
    2回目の読み取りをしない（署名は読めたのに解析だけ別内容/失敗、という乖離を構造的に無くす）。
    `read_error`（I/O 失敗＝権限/共有ドライブ切断等・一時的でありうる）が1つでもあれば、
    キャッシュを参照も保存もせず `_compute_for_world` を直接呼ぶ（fail-closed・一時的な
    読み取り失敗を固定のプレースホルダで署名化してキャッシュすると、以後同じプレースホルダに
    恒久的にヒットし続け、復旧後の中身を反映できなくなる）。**`file_too_large`（上限超過）は
    キャッシュを抑止しない**——ファイルサイズという決定的な事実であり一時的な障害ではないため、
    署名に固定マーカーとして含めた上で通常どおりキャッシュする（`_control_content_signature`
    参照）。いずれの場合も**既に読めた分は再読しない**——失敗したファイルだけ
    `_read_all_control_contents` が返した診断をそのまま使い、成功したファイルは同じバイト列を
    解析にも使い回す。
    """
    wd = root if root is not None else worlds.world_dir(world_id)
    if not wd:
        return {}
    control_contents, control_errors = _read_all_control_contents(wd, files=files)
    if any(d.code in _UNRESOLVABLE_DIAGNOSTIC_CODES for d in control_errors.values()):
        return _compute_for_world(wd, control_contents=control_contents, control_errors=control_errors, files=files)
    content_sig = _control_content_signature(control_contents, control_errors)
    if sig is None:
        from . import worker                            # 遅延 import（corpus_docs との循環回避）
        # `worker.world_signature(world_id)` は内部で `worlds.world_dir()` を再度呼ぶため、この
        # 呼び出しとの間で rebind（root 差し替え）が起きると「古い wd の走査結果」を「新しい root の
        # 署名」でキャッシュしてしまう。既に解決済みの `wd` から直接署名を計算し、2回目の
        # world_dir() 解決を発生させない。
        sig = worker.world_signature_of_root(wd)
    effective_sig = f"{sig or ''}:{content_sig}"
    if files is not None:
        # RV2是正#a2: `files`（呼び出し側が既に列挙済み）の rel 集合もキーへ畳み込む——追加 walk
        # なし（既に受け取った list を並べ替えるだけ）。`_files_rel_signature` docstring 参照。
        effective_sig = f"{effective_sig}:{_files_rel_signature(files)}"
    key = (world_id, str(Path(wd).resolve()), effective_sig)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)                          # 真の LRU: ヒット時にも最近使用として順序更新
        return cached
    result = _compute_for_world(wd, control_contents=control_contents, control_errors=control_errors, files=files)
    _CACHE[key] = result
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)                       # 最も長く使われていないキーを追い出す
    return result


def resolve_many(world_id: str, rels, *, root=None, sig: str | None = None, files=None) -> dict[str, Resolution]:
    """`resolve_for_world` の結果から、指定した rel だけを取り出す薄いラッパー。

    `sig`/`files`（省略可・キーワード専用）: `resolve_for_world` へそのまま転送する（I2是正・
    2026-09-05）。呼び出し側（grep/impact/出典など query 経路）が既に world の `last_sig` や
    materialize 済みファイル一覧を持っているなら渡すことで、`resolve_for_world` のキャッシュ
    キーが安定し、以後の同一 world への呼び出しが全木走査なしでキャッシュヒットする
    （`resolve_for_world` docstring 参照）。どちらも省略時は従来どおり（`resolve_for_world` が
    自前で署名計算・列挙する）。
    """
    all_res = resolve_for_world(world_id, root=root, sig=sig, files=files)
    return {r: all_res[r] for r in rels if r in all_res}


def diagnostics_for_world(world_id: str, *, root=None, files=None) -> list[dict]:
    """world 内の全 `_重要度.txt` の構文診断（台帳の `control_diagnostics` 用）。

    `root`/`files`（省略可・キーワード専用）: 呼び出し側が既に root を解決・列挙
    （`scope_infer.safe_files` の materialize 済み list）済みなら渡す——与えられれば再解決/再走査
    しない（§③ 2026-09-01・`preview_service.build_preview` が文書列挙・重要度解決と同じ list を
    共有するのに使う）。
    """
    wd = root if root is not None else worlds.world_dir(world_id)
    if not wd:
        return []
    out: list[dict] = []
    entries = files if files is not None else scope_infer.safe_files(wd)
    for rp, rel in entries:
        if not is_importance_control_path(rel):
            continue
        _rules, diags = parse_control_file(rp, config_rel=rel)
        out.extend(asdict(d) for d in diags)
    return out
