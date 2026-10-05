"""文書の重要度を、登録フォルダ内の設定ファイル `_重要度.txt` から解決する。

各フォルダの `_重要度.txt`（1行1パターン＝`パターン: 高|中|低|なし  # 理由`）を解析し、
資料フォルダ内の各 rel_path へ階層継承で解決する。
- 一致する規則を持つ最深の祖先が勝つ（規則を持たない祖先は飛ばして上へ遡る）。
- 同一ファイル内は glob がフォルダ既定（`*`）より優先し、複数一致は後勝ち。
- `なし` は祖先の指定を打ち消して中立に戻す（それより上へは遡らない）。
- どれにも一致しなければ値を持たない（戻り dict にキーが現れない）。
`_重要度.txt` 自体は文書として扱わない（`is_importance_control_path`）が、ファイル署名
（`ingest.worker.world_signature`）には残す。構文エラーは行単位で無効化し `Diagnostic` に集約する。
解決結果は `(world_id, root の実パス, 実効署名)` をキーにプロセス内でキャッシュする。
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

# 重要度機能のスキーマ版。`ingest.worker.world_signature` の材料にするため、上げると全再構築される
IMPORTANCE_SCHEMA_VERSION = 2

_VALUES = frozenset({"高", "中", "低", "なし"})

# 単一 worker 前提の上限
_MAX_TOTAL_BYTES = 64 * 1024      # 設定ファイル1個の総バイト数上限
_MAX_RULES_PER_FILE = 500         # 設定ファイル1個あたりの有効な規則数上限
_MAX_PATTERN_LEN = 260            # 1パターンの文字数上限
_MAX_REASON_BYTES = 600           # 理由1行の UTF-8 バイト数上限（日本語で概ね200文字相当）

# 制御文字（C0・DEL・C1・Unicode 行/段落区切り）は理由に使えない
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_CACHE_MAX = 64   # プロセス内 resolver キャッシュ（LRU）の上限
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
    """1つの rel_path に対する解決結果（値があるときだけ作る）。"""
    value: str                # 高/中/低（「なし」に解決した場合は Resolution を返さず None にする）
    reason: str | None
    config_path: str          # 勝者となった `_重要度.txt` の world 相対 rel_path（監査用）
    rule_line: int            # 勝者となった規則の行番号（監査用）


# 検索/影響の並び順だけが使う順位専用スケール（表示値ではない）。未設定は「中」と同格
RANK: dict[str, int] = {"高": 2, "中": 1, "低": 0}
RANK_UNSET = 1   # 未設定（dict にキーが無い rel）の rank（＝「中」と同格）


def rank_of(res: "Resolution | None") -> int:
    """順位専用スケール（`RANK`）での rank。未解決（`res is None`）は `RANK_UNSET`。"""
    return RANK.get(res.value, RANK_UNSET) if res is not None else RANK_UNSET


def public_fields(res: "Resolution | None") -> dict:
    """grep ヒット／ES メタ／影響一覧／出典など外部へ見せる経路の共通表示形（`importance`/`importance_reason`）。

    由来（`importance_source`）は台帳/管理画面専用で出さない。値なしなら空 dict。
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
    """rel_path の祖先フォルダを深い順に列挙する（root=`""` を含む）。"""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _match_segment_glob(pattern: str, rel: str) -> bool:
    """セグメント単位の glob マッチ（`*`/`?`/`[seq]` は1セグメント内のみ・`**` だけが複数セグメントを跨ぐ）。"""
    return _match_segments(pattern.split("/"), rel.split("/"))


def _match_segments(pat_segs: list[str], rel_segs: list[str]) -> bool:
    """`pat_segs` が `rel_segs` に一致するかを動的計画法で判定する（`**` を含むパターンで指数時間にしない）。"""
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
    """1行を解析する。戻り値は次のいずれか。

    - `((pattern, value, reason), None, None)` — 有効な規則行
    - `(None, None, None)` — 空行／`#` で始まるコメント行
    - `(None, code, message)` — 構文エラー（呼び出し元が `Diagnostic` を組み立てる）

    理由の制御文字チェックは `strip()` 前の生テキストに対して行う（`strip()` が制御文字を落とすため）。
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
        # 診断メッセージは画面に出すため、入力値は反射しない固定文言にする
        return None, "invalid_value", "値が正しくありません。「高」「中」「低」「なし」のいずれかにしてください"
    reason = None
    if reason_raw is not None:
        if _CONTROL_CHAR_RE.search(reason_raw):          # strip() する前の生テキストを検査（迂回防止）
            return None, "reason_control_char", "理由に使えない文字が含まれています。改行やタブは使わずに書いてください"
        reason = reason_raw.strip() or None
        if reason is not None and len(reason.encode("utf-8")) > _MAX_REASON_BYTES:
            return None, "reason_too_long", "理由が長すぎます。短くまとめてください"
    return (pattern, value, reason), None, None


def _read_control_bytes(path: Path, cfg: str) -> tuple[bytes | None, Diagnostic | None]:
    """`_重要度.txt` を安全に読む（stat で事前判定し、読み取り自体も上限＋1バイトに制限して再検査する）。

    解析と署名計算が同じ読み取り経路を共有する。読めれば `(raw, None)`、読めない/上限超過なら `(None, Diagnostic)`。
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
    """読み込み済みのバイト列を解析する純関数（I/O なし）。

    行区切りは `\\n`／`\\r\\n` のみで、デコード前のバイト列で分割し各行を個別に strict デコードする
    （不正な行だけ `invalid_encoding` 診断にして他の行は生かす・`splitlines()` は制御文字を行区切りにするため使わない）。
    `\\r` は `\\n` の直前のものだけ取り除く（本文中の `\\r` は制御文字として検出させる）。
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
    """`_重要度.txt` 1個を読み取って解析する（行単位の構文エラーは他の行に影響しない）。

    `config_rel` は診断・監査に載せる rel_path（省略時は `path` の文字列）。
    """
    cfg = config_rel if config_rel is not None else str(path)
    raw, diag = _read_control_bytes(path, cfg)
    if raw is None:
        return [], [diag]
    return _parse_control_bytes(raw, cfg)


def _pick_winner(rules: list[Rule], rel_from_owner: str) -> Rule | None:
    """1つの設定ファイル内で `rel_from_owner` に一致する規則から勝者を選ぶ（glob 優先・後勝ち）。"""
    matched = [r for r in rules if _match_segment_glob(r.pattern, rel_from_owner)]
    if not matched:
        return None
    globs = [r for r in matched if r.pattern != "*"]
    pool = globs if globs else matched
    return pool[-1]


_UNRESOLVABLE_DIAGNOSTIC_CODES = frozenset({"read_error"})


def _resolve_rel(rel: str, control_by_folder: dict[str, tuple[list[Rule], list[Diagnostic]]]) -> Resolution | None:
    """`rel` を階層継承で解決する（一致規則を持つ最深の祖先が勝つ・`なし` は終端）。

    `read_error`（権限・共有ドライブ切断等）の祖先は判定不能として遡りを打ち切る。
    """
    for folder in _ancestor_folders_deepest_first(rel):
        entry = control_by_folder.get(folder)
        if not entry:
            continue
        rules, diags = entry
        if any(d.code in _UNRESOLVABLE_DIAGNOSTIC_CODES for d in diags):
            return None                                  # 読み取れない＝判定不能（祖先へは遡らない）
        rel_from_owner = rel[len(folder) + 1:] if folder else rel
        winner = _pick_winner(rules, rel_from_owner)
        if winner is None:
            continue                                    # 一致規則なし→さらに上へ
        if winner.value == "なし":
            return None                                  # 明示解除（上へは遡らない）
        config_path = f"{folder}/{CONTROL_FILENAME}" if folder else CONTROL_FILENAME
        return Resolution(value=winner.value, reason=winner.reason,
                          config_path=config_path, rule_line=winner.line)
    return None


def _compute_for_world(wd: Path, control_contents: dict[str, bytes] | None = None,
                       control_errors: dict[str, Diagnostic] | None = None, *, files=None) -> dict[str, Resolution]:
    """資料フォルダ内の全 rel_path を解決する。

    `control_contents`/`control_errors` は事前に1回だけ読んだ結果で、渡せば再読しない。
    `files` は列挙済みの list を渡す（2回走査するため generator は不可）。
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
    """資料フォルダ内の全 `_重要度.txt` を1回ずつ読み、成功分を `{rel: raw_bytes}`、失敗分を `{rel: Diagnostic}` で返す。

    署名計算と解析が同じバイト列を使う。`files` は列挙済みなら渡す。
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
    """`_重要度.txt` の内容ハッシュを集約した署名（純関数）。

    `world_signature` はメタデータだけで内容を見ないため、内容の変化や root 差し替えを検知できない分をここで補う。
    `file_too_large` の診断は固定マーカーとして含める（`read_error` はキャッシュを経由しない）。
    """
    parts = sorted((rel, hashlib.sha1(raw).hexdigest()) for rel, raw in control_contents.items())
    parts += sorted((rel, f"<{diag.code}>") for rel, diag in (control_errors or {}).items())
    parts.sort()
    return hashlib.sha1(repr(parts).encode("utf-8")).hexdigest()


def _files_rel_signature(files) -> str:
    """列挙済み `files` の rel 集合だけから作る決定的署名（純関数・追加 I/O なし）。

    明示 `sig` を渡す経路では、ファイルの追加・削除でキャッシュキーが変わらなくなるため、これを実効署名へ畳み込む。
    """
    rels = sorted(rel for _rp, rel in files)
    return hashlib.sha1(repr(rels).encode("utf-8")).hexdigest()


def resolve_for_world(world_id: str, *, root=None, sig: str | None = None, files=None) -> dict[str, Resolution]:
    """資料フォルダ内の全 rel_path を解決した dict を返す（値がある rel だけを持つ）。

    ① 設定ファイルを1回だけ読む（読めた分は署名と解析の両方で使い回す）。
    ② `read_error` が1つでもあればキャッシュを使わず直接計算する（一時的な失敗をキャッシュしない）。
       `file_too_large` は決定的なので署名に含めて通常どおりキャッシュする。
    ③ キャッシュキーは `(world_id, root の実パス, 実効署名)`。キャッシュヒット時は木を歩かない。

    `root` は解決済みなら渡す（文書列挙側と同じ root を共有するため）。`files` は列挙済みなら渡す。
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
        # rebind をまたがないよう、解決済みの `wd` から署名を計算する
        sig = worker.world_signature_of_root(wd)
    effective_sig = f"{sig or ''}:{content_sig}"
    if files is not None:
        # 列挙済み `files` の rel 集合もキーへ畳み込む
        effective_sig = f"{effective_sig}:{_files_rel_signature(files)}"
    key = (world_id, str(Path(wd).resolve()), effective_sig)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)                          # LRU: ヒット時に最近使用へ
        return cached
    result = _compute_for_world(wd, control_contents=control_contents, control_errors=control_errors, files=files)
    _CACHE[key] = result
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)                       # 最も古いキーを追い出す
    return result


def resolve_many(world_id: str, rels, *, root=None, sig: str | None = None, files=None) -> dict[str, Resolution]:
    """`resolve_for_world` の結果から指定した rel だけを取り出す。`root`/`sig`/`files` はそのまま転送する。"""
    all_res = resolve_for_world(world_id, root=root, sig=sig, files=files)
    return {r: all_res[r] for r in rels if r in all_res}


def diagnostics_for_world(world_id: str, *, root=None, files=None) -> list[dict]:
    """資料フォルダ内の全 `_重要度.txt` の構文診断（台帳の `control_diagnostics` 用）。`root`/`files` は解決・列挙済みなら渡す。"""
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
