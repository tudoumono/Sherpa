"""道具の呼び出しの記録（呼び出し 1 回 = 1 行・書き込む人（プロセス）ごとに別のファイル）の書き込みと、ターンの終わりの 1 本へのまとめ。
設計: docs/design/codex.md「道具の呼び出しの記録」

- 書くのは Sherpa の道具（MCP サーバー）の入口だけ。ターンの作業フォルダの `toolcalls/` の中にプロセスごとの `calls-<プロセス番号>-<起動時刻>.jsonl` を作る。
- 資料の中身・グラフの行は書かない。秘匿の資料名は書かない。
- グラフの道具（`graph_resolve`・`graph_impact`）の結果の行だけは、同じ置き場の別ファイル `graph-<プロセス番号>-<起動時刻>.jsonl` へ書く（影響一覧の素材・ターンの終わりに置き場ごと消す・ログには出さない）。
- まとめる側（Sherpa のアプリ）は時刻→プロセス→通し番号の順に 1 本へ並べ、違うターンの行は取り込まず、同じ ID は 1 つにし、壊れた行・読めないファイル・通し番号の抜けは件数で残す。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

ENV_DIR = "SHERPA_MCP_CALL_LOG_DIR"
FILE_PREFIX = "calls-"
FILE_SUFFIX = ".jsonl"
GRAPH_PREFIX = "graph-"
GRAPH_TOOLS = ("graph_resolve", "graph_impact")
GRAPH_MAX_ROWS = 200  # 1 回の呼び出しの結果から残す影響先の行の上限（道具の返却上限と同じ）
GRAPH_BYTES_CAP = 4 * 1024 * 1024  # 1 プロセスのグラフの結果のファイルの上限（超えたら書かない）
SCHEMA_VERSION = 1

MAX_DOCS = 20  # 1 呼び出しに残す資料の上限（超えた分は `docs_omitted` の件数）
STR_MAX = 200  # 検索語・資料・範囲の 1 項目の最大文字数
DETAIL_BYTES_CAP = 1024 * 1024  # 1 プロセスのファイルがこれを超えたら、以降は引数・資料の中身を落として件数だけ残す
HARD_BYTES_CAP = 4 * DETAIL_BYTES_CAP  # これを超えたら書かない（捨てた件数は印の行で残し、通し番号の抜けとして数える）
MAX_FILE_BYTES = HARD_BYTES_CAP + DETAIL_BYTES_CAP  # まとめるときに読む 1 ファイルの上限（超えたら読まずに欠けとして数える）
MAX_FILES = 64  # まとめるときに見るファイル数の上限（超えた分は欠けとして数える）
_FILE_NAME_RE = re.compile(r"^calls-\d+-\d+\.jsonl$")
_GRAPH_FILE_NAME_RE = re.compile(r"^graph-\d+-\d+\.jsonl$")

_TRUNC_FLAGS = ("truncated", "truncated_docs", "text_truncated", "file_truncated", "byte_clipped",
                "partial_hit", "partial_line")
_COUNT_LIST_KEYS = ("hits", "impact", "candidates", "neighbors")
_COUNT_FALLBACK_LIST_KEYS = ("docs", "paths")
_ERROR_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_STUB_KEYS = ("v", "turn", "conv", "attempt", "proc", "seq", "call_id", "ts", "tool", "status", "count",
              "ms", "error_kind", "truncated")
_SALVAGE_RE_SEQ = re.compile(r'"seq":\s*(\d+)')
_SALVAGE_RE_PROC = re.compile(r'"proc":\s*(\d+)')


def clip(value, limit: int = STR_MAX):
    """文字列を `limit` 文字で切る。文字列でなければそのまま返す。"""
    return value[:limit] if isinstance(value, str) else value


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def summarize_result(name: str, args, result, is_error: bool, detail: dict) -> dict:
    """道具の結果から、記録に残す要点（結果・件数・返した資料と行の範囲・切り詰め・エラーの種類）を取り出す。
    `detail` は `mcp_server._coverage_detail` の戻り値（秘匿の資料名を除いた doc・range）。本文・グラフの行は入れない。
    """
    from .ingest import text_kind

    out: dict = {}
    res = result if isinstance(result, dict) else None
    truncated = False
    if res is not None:
        truncated = any(res.get(k) for k in _TRUNC_FLAGS)
        cov = res.get("coverage")
        if isinstance(cov, dict) and cov.get("complete") is False:
            truncated = True
    out["status"] = "error" if is_error else ("truncated" if truncated else "ok")
    out["truncated"] = bool(truncated)
    if is_error:
        kind = "other"
        if res is not None:
            err = res.get("error")
            code = res.get("error_code")
            if isinstance(code, str) and _ERROR_KIND_RE.match(code):
                kind = code
            elif isinstance(err, str) and _ERROR_KIND_RE.match(err):
                kind = err
        out["error_kind"] = kind
    if res is None:
        return out
    count = None
    for key in _COUNT_LIST_KEYS:
        if isinstance(res.get(key), list):
            count = len(res[key])
            break
    if count is None and _is_int(res.get("count")):
        count = res["count"]
    if count is None:
        for key in _COUNT_FALLBACK_LIST_KEYS:
            if isinstance(res.get(key), list):
                count = len(res[key])
                break
    if count is not None:
        out["count"] = count
    docs: list[dict] = []
    hidden = 0
    hits = res.get("hits")
    if isinstance(hits, list):
        for h in hits:
            if not isinstance(h, dict) or not isinstance(h.get("doc_id"), str):
                continue
            if text_kind.is_sensitive_doc_id(h["doc_id"]):
                hidden += 1
                continue
            row = {"doc": clip(h["doc_id"])}
            if _is_int(h.get("line")):
                row["range"] = str(h["line"])
            docs.append(row)
    elif detail.get("doc") and not (name == "compare_documents" and res.get("status") != "comparable"):
        names = [d for d in str(detail["doc"]).split(" ⇔ ") if d]
        rng = None
        if len(names) == 1:
            s, e = res.get("start_line"), res.get("end_line")
            rng = f"{s}-{e}" if _is_int(s) and _is_int(e) else detail.get("range")
        for n in names:
            row = {"doc": clip(n)}
            if rng:
                row["range"] = clip(str(rng))
            docs.append(row)
    if docs:
        out["docs"] = docs[:MAX_DOCS]
        if len(docs) > MAX_DOCS:
            out["docs_omitted"] = len(docs) - MAX_DOCS
    if hidden:
        out["docs_hidden"] = hidden
    return out


def _graph_limits(cov) -> list[dict]:
    out = []
    for lim in (cov.get("limits") if isinstance(cov, dict) and isinstance(cov.get("limits"), list) else []):
        if isinstance(lim, dict) and isinstance(lim.get("kind"), str):
            item = {"kind": lim["kind"]}
            for k in ("stage", "plugin"):
                if isinstance(lim.get(k), str):
                    item[k] = clip(lim[k])
            if _is_int(lim.get("count")):
                item["count"] = lim["count"]
            out.append(item)
    return out


def graph_view(name: str, result) -> dict | None:
    """`graph_resolve`／`graph_impact` の結果から、影響一覧の素材（起点・返った行・coverage・未解決の件数・エラー）を取り出す。秘匿名のファイルの行は入れない。道具が違う・結果が辞書でないときは None。"""
    from .ingest import text_kind

    if name not in GRAPH_TOOLS or not isinstance(result, dict):
        return None
    out: dict = {}
    err = result.get("error_code") or result.get("error")
    if isinstance(err, str) and _ERROR_KIND_RE.match(err):
        out["error"] = err
    cov = result.get("coverage")
    out["complete"] = (isinstance(cov, dict) and cov.get("complete") is True
                       and result.get("truncated") is not True and cov.get("truncated") is not True)
    out["limits"] = _graph_limits(cov)
    if name == "graph_resolve":
        cands = result.get("candidates")
        out["count"] = len(cands) if isinstance(cands, list) else None
        return out

    def row(n) -> dict | None:
        if not isinstance(n, dict) or not isinstance(n.get("name"), str) or not n["name"].strip():
            return None
        path = n.get("path") if isinstance(n.get("path"), str) else ""
        if path and text_kind.is_sensitive_doc_id(path):
            return None
        r = {"name": clip(n["name"]), "path": clip(path, 400)}
        if isinstance(n.get("canonical_id"), str):
            r["cid"] = clip(n["canonical_id"], 400)
        if isinstance(n.get("kind"), str):
            r["kind"] = clip(n["kind"], 40)
        if _is_int(n.get("distance")):
            r["distance"] = n["distance"]
        return r

    start = row(result.get("start"))
    if start is not None:
        out["start"] = start
    items = result.get("impact")
    rows = [r for r in (row(n) for n in (items if isinstance(items, list) else [])) if r is not None]
    out["rows"] = rows[:GRAPH_MAX_ROWS]
    out["count"] = result["count"] if _is_int(result.get("count")) else None
    if isinstance(cov, dict) and isinstance(cov.get("depth"), dict):
        out["depth_truncated"] = cov["depth"].get("truncated")
    un = result.get("unresolved")
    if isinstance(un, dict) and isinstance(un.get("items"), list):
        out["unresolved"] = len(un["items"]) + (un["omitted"] if _is_int(un.get("omitted")) and un["omitted"] > 0 else 0)
    return out


class CallLogWriter:
    """1 つの MCP サーバープロセスが自分専用のファイルへ 1 行ずつ追記する。書けなくても道具の呼び出しは失敗させない（False を返す）。"""

    def __init__(self, directory: str | os.PathLike, *, pid: int | None = None):
        self.directory = Path(directory)
        self.pid = os.getpid() if pid is None else pid
        stamp = time.time_ns()
        self._path = self.directory / f"{FILE_PREFIX}{self.pid}-{stamp}{FILE_SUFFIX}"
        self._graph_path = self.directory / f"{GRAPH_PREFIX}{self.pid}-{stamp}{FILE_SUFFIX}"
        self._graph_bytes = 0
        self._seq = 0
        self._bytes = 0
        self._marker_dirty = False
        atexit.register(self.flush_marker)

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def append(self, entry: dict) -> bool:
        ok = self._append(entry)
        if ok:
            if self._marker_dirty:
                self.flush_marker()
        else:
            self._marker_dirty = True  # 捨てた呼び出しは、次に書けるときかプロセスの終わりに印の行（最後の通し番号）で残す
        return ok

    def flush_marker(self) -> None:
        """最後の通し番号だけの小さい行を書く。まとめる側は、この番号までの抜けを「記録の欠け」に数える。"""
        if not self._marker_dirty:
            return
        if self._write_line({"v": SCHEMA_VERSION, "kind": "seq_marker", "turn": os.environ.get("SHERPA_MCP_TURN_ID", ""),
                             "proc": self.pid, "last_seq": self._seq, "ts": time.time()}):
            self._marker_dirty = False

    def _append(self, entry: dict) -> bool:
        if self._bytes >= HARD_BYTES_CAP:
            return False
        if self._bytes >= DETAIL_BYTES_CAP:
            entry = {k: entry[k] for k in _STUB_KEYS if k in entry} | {"detail_dropped": True}
        return self._write_line(entry)

    def append_graph(self, entry: dict) -> bool:
        """グラフの道具の結果（`graph_view`）の行を、グラフ専用のファイルへ足す。上限を超えたら書かない（False）。"""
        if self._graph_bytes >= GRAPH_BYTES_CAP:
            return False
        return self._write_line(entry, self._graph_path, cap=GRAPH_BYTES_CAP)

    def _write_line(self, entry: dict, path: Path | None = None, cap: int | None = None) -> bool:
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
        graph = path is not None
        if cap is not None and self._graph_bytes + len(line) > cap:
            return False
        path = path or self._path
        try:
            if self.directory.is_symlink():
                return False
            self.directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    return False
                os.write(fd, line)
            finally:
                os.close(fd)
        except OSError:
            return False
        if graph:
            self._graph_bytes += len(line)
        else:
            self._bytes += len(line)
        return True


def remove_dir(directory) -> None:
    """ターンの終わりに記録の置き場を消す（読めなくても例外は投げない）。"""
    if directory is not None:
        shutil.rmtree(directory, ignore_errors=True)


class MergedCallLog:
    """ターンの終わりにまとめた記録。`rows` は時刻→プロセス→通し番号の順。欠けは `missing`（壊れた行・読めないファイル・通し番号の抜け）。"""
    __slots__ = ("rows", "broken_lines", "unreadable_files", "seq_gaps", "foreign_rows", "duplicate_rows",
                 "detail_dropped_rows", "parent_shortfall", "graph", "graph_broken")

    def __init__(self):
        self.rows: list[dict] = []
        self.broken_lines = 0
        self.unreadable_files = 0
        self.seq_gaps = 0
        self.foreign_rows = 0
        self.duplicate_rows = 0
        self.detail_dropped_rows = 0
        self.parent_shortfall = 0
        self.graph: list[dict] = []  # グラフの道具の結果（`graph_view` ＋ call_id・tool・role）。時刻の順
        self.graph_broken = 0  # グラフの結果のファイルで読めなかった行・ファイルの数（影響一覧の「記録の欠け」）

    @property
    def missing(self) -> int:
        """「記録の欠け N 行」の N。"""
        return self.broken_lines + self.unreadable_files + self.seq_gaps + self.parent_shortfall


def _valid_row(row) -> bool:
    return (isinstance(row, dict) and isinstance(row.get("turn"), str) and isinstance(row.get("call_id"), str)
            and _is_int(row.get("seq")) and _is_int(row.get("proc")) and isinstance(row.get("tool"), str)
            and isinstance(row.get("ts"), (int, float)) and not isinstance(row.get("ts"), bool))


def merge_call_logs(directory: str | os.PathLike | None, turn_id: str) -> MergedCallLog:
    """`directory` の `calls-*.jsonl` を 1 本にまとめる。違うターンの行は取り込まず、同じ呼び出し ID は 1 つにする。例外は投げない。"""
    merged = MergedCallLog()
    if directory is None:
        return merged
    directory = Path(directory)
    if directory.is_symlink():
        merged.unreadable_files += 1
        return merged
    try:
        files = sorted(p for p in directory.iterdir() if _FILE_NAME_RE.match(p.name))
    except FileNotFoundError:
        return merged
    except OSError:
        merged.unreadable_files += 1
        return merged
    if len(files) > MAX_FILES:
        merged.unreadable_files += len(files) - MAX_FILES
        files = files[:MAX_FILES]
    seen: set[str] = set()
    present: dict[int, set[int]] = {}
    last_seq: dict[int, int] = {}
    turn_token = f'"turn": {json.dumps(turn_id)}'
    for path in files:
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
                raise OSError("not a regular file or too large")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                data = os.read(fd, MAX_FILE_BYTES + 1)
            finally:
                os.close(fd)
            if len(data) > MAX_FILE_BYTES:
                raise OSError("too large")
        except OSError:
            merged.unreadable_files += 1
            continue
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                text = raw.decode("utf-8")
                row = json.loads(text)
            except ValueError:
                text = raw.decode("utf-8", errors="replace")
                row = None
            if (isinstance(row, dict) and row.get("kind") == "seq_marker" and row.get("turn") == turn_id
                    and _is_int(row.get("proc")) and _is_int(row.get("last_seq"))):
                last_seq[row["proc"]] = max(last_seq.get(row["proc"], 0), row["last_seq"])
                continue
            if not _valid_row(row):
                if isinstance(row, dict) and isinstance(row.get("turn"), str) and row["turn"] != turn_id:
                    merged.foreign_rows += 1
                    continue
                if turn_token in text:
                    m_seq, m_proc = _SALVAGE_RE_SEQ.search(text), _SALVAGE_RE_PROC.search(text)
                    if m_seq and m_proc:
                        present.setdefault(int(m_proc.group(1)), set()).add(int(m_seq.group(1)))
                merged.broken_lines += 1
                continue
            if row["turn"] != turn_id:
                merged.foreign_rows += 1
                continue
            if row["call_id"] in seen:
                merged.duplicate_rows += 1
                continue
            seen.add(row["call_id"])
            present.setdefault(row["proc"], set()).add(row["seq"])
            if row.get("detail_dropped"):
                merged.detail_dropped_rows += 1
            merged.rows.append(row)
    merged.rows.sort(key=lambda r: (r["ts"], r["proc"], r["seq"]))
    for proc in set(present) | set(last_seq):
        seqs = present.get(proc, set())
        merged.seq_gaps += max(max(seqs, default=0), last_seq.get(proc, 0)) - len(seqs)
    return merged


def assign_roles(merged: MergedCallLog, parent_tools_by_attempt: dict[int, list[str]], *,
                 parent_record_reliable: bool) -> None:
    """各行に `role`（parent / child / undetermined）を付ける。
    ① 同じ実行（attempt）の中でプロセスごとの道具の名前の並びを作る
    ② 親の `--json` の mcp_tool_call の並びと完全に一致するプロセスを探す（一致が無ければ、並列呼び出しの入れ替わりを許して名前の集まりで比べる）
    ③ 一致が 1 つなら親・残りは子／2 つ以上なら一致した全部を判定不能・残りは子／0 なら全部判定不能
    親の呼び出しが 0 回と分かっていて（親の記録が信用できる）プロセスがあるなら、全部子。
    """
    by_attempt: dict = {}
    for r in merged.rows:
        by_attempt.setdefault(r.get("attempt") if _is_int(r.get("attempt")) else None, {}).setdefault(r["proc"], []).append(r["tool"])
    roles: dict[tuple, str] = {}
    for attempt, procs in by_attempt.items():
        if attempt is None:
            for proc in procs:
                roles[(attempt, proc)] = "undetermined"
            continue
        parent_seq = list(parent_tools_by_attempt.get(attempt, []))
        if not parent_seq:
            for proc in procs:
                roles[(attempt, proc)] = "child" if parent_record_reliable else "undetermined"
            continue
        matches = [p for p, seq in procs.items() if seq == parent_seq]
        if not matches:
            matches = [p for p, seq in procs.items() if sorted(seq) == sorted(parent_seq)]
        for proc in procs:
            if proc in matches:
                roles[(attempt, proc)] = "parent" if len(matches) == 1 else "undetermined"
            else:
                roles[(attempt, proc)] = "child" if matches else "undetermined"
    for r in merged.rows:
        key = (r.get("attempt") if _is_int(r.get("attempt")) else None, r["proc"])
        r["role"] = roles.get(key, "undetermined")


def count_parent_shortfall(merged: MergedCallLog, parent_tools_by_attempt: dict[int, list[str]]) -> None:
    """実行（attempt）ごとに、親の `--json` で見えた Sherpa の道具の呼び出し（`ask_user` を除く）の数が、その実行のまとめた行の数より多い分を記録の欠けに数える。
    親か子かの判定に頼らない（判定不能の行を欠けと数えない）。親の呼び出しの数が取れない構成（空）では何もしない。
    """
    rows_by_attempt: dict = {}
    for r in merged.rows:
        if _is_int(r.get("attempt")) and r.get("tool") != "ask_user":
            rows_by_attempt[r["attempt"]] = rows_by_attempt.get(r["attempt"], 0) + 1
    merged.parent_shortfall = sum(
        max(0, sum(1 for t in tools if t != "ask_user") - rows_by_attempt.get(attempt, 0))
        for attempt, tools in parent_tools_by_attempt.items())


# ===== ターンの終わりにまとめた記録からの要約（調査の記録・回答の見つかった資料・利用統計の累計） =====

FOUND_TOOLS = ("ripgrep_search", "es_search")  # 見つかった資料の対象（grep と全文検索）。一覧（list_docs・glob_search・folder_tree）は名前を並べただけなので対象外
OPEN_TOOLS = ("read_doc", "read_around", "doc_outline", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages",
              "file_head", "compare_documents")  # 資料を開いた記録になる道具（シート名の一覧 `xlsx_sheets` は開いた扱いにしない）
ROUTE_MAX_ROWS = 2000  # 調査の記録に残す呼び出しの行の上限（超えた分は `omitted` の件数）
FOUND_RECORD_MAX = 200  # 調査の記録に残す見つかった資料の上限（超えた分は `more` の件数）
FOUND_ANSWER_MAX = 20  # 回答の `found_docs` に出す資料の上限（超えた分は `found_docs_more`）
FOUND_LINES_MAX = 5  # 1 資料に残す行・検索語の上限
ROLES = ("parent", "child", "undetermined")


def _visible_docs(row: dict) -> tuple[list[dict], int]:
    """行の資料のうち秘匿でないものと、秘匿として伏せた件数（記録を作る側でも念のため判定し直す）。"""
    from .ingest import text_kind

    out, hidden = [], _count(row.get("docs_hidden"))
    for d in row.get("docs") or []:
        name = d.get("doc") if isinstance(d, dict) else None
        if not isinstance(name, str) or not name:
            continue
        if text_kind.is_sensitive_doc_id(name):
            hidden += 1
            continue
        out.append(d)
    return out, hidden


def _count(v) -> int:
    return v if _is_int(v) and v > 0 else 0


def route_rows(merged: MergedCallLog) -> tuple[list[dict], int]:
    """調べた経路: 全部の呼び出しの 1 行（道具・検索語・件数・読んだ資料と行の範囲・時間・台帳の項目・親か子か）。戻り値は `(行, 上限で省いた件数)`。資料の中身・グラフの行は持たない。"""
    rows = []
    for r in merged.rows[:ROUTE_MAX_ROWS]:
        args = r.get("args") if isinstance(r.get("args"), dict) else {}
        docs, hidden = _visible_docs(r)
        out = {"call": r["call_id"], "attempt": r.get("attempt"), "role": r.get("role", "undetermined"),
               "tool": r["tool"], "status": r.get("status"), "count": r.get("count"), "ms": r.get("ms")}
        if r.get("error_kind"):
            out["error"] = r["error_kind"]
        for key in ("query", "range", "item"):
            if args.get(key) not in (None, ""):
                out[key] = args[key]
        if docs:
            out["docs"] = [{k: d[k] for k in ("doc", "range") if k in d} for d in docs]
        if _count(r.get("docs_omitted")):
            out["docs_omitted"] = r["docs_omitted"]
        if hidden:
            out["docs_hidden"] = hidden
        if r.get("detail_dropped"):
            out["detail_dropped"] = True
        rows.append({k: v for k, v in out.items() if v is not None})
    return rows, max(0, len(merged.rows) - ROUTE_MAX_ROWS)


def opened_docs(merged: MergedCallLog) -> set[str]:
    """資料を開く道具（本文の読み取り・見出し・比べ読み）で成功して読んだ資料（親・子とも）。"""
    out: set[str] = set()
    for r in merged.rows:
        if r["tool"] in OPEN_TOOLS and r.get("status") != "error":
            for d in r.get("docs") or []:
                if isinstance(d, dict) and isinstance(d.get("doc"), str):
                    out.add(d["doc"])
    return out


def opened_incomplete(merged: MergedCallLog) -> bool:
    """開いた資料の記録が完全でない（資料名を落とした読み取り系の行か、記録の欠けがある）。"""
    return bool(merged.missing) or any(r.get("detail_dropped") and r.get("tool") in OPEN_TOOLS for r in merged.rows)


def found_docs(merged: MergedCallLog, opened: set[str]) -> tuple[list[dict], int, int]:
    """検索の道具（`FOUND_TOOLS`）が返した資料を資料ごとにまとめる。戻り値は `(行, 秘匿で伏せた件数, 呼び出しの上限で取り込めなかったヒット数)`。
    行は `{doc, hits, lines, queries, opened}`（本文は持たない）。並びは、見つかった検索語の種類が多い順 → ヒット数が多い順 → 最初に見つかった順。
    """
    acc: dict[str, dict] = {}
    hidden = 0
    omitted = 0
    for r in merged.rows:
        if r["tool"] not in FOUND_TOOLS or r.get("status") == "error":
            continue
        docs, h = _visible_docs(r)
        hidden += h
        omitted += _count(r.get("docs_omitted"))
        query = (r.get("args") or {}).get("query") if isinstance(r.get("args"), dict) else None
        for d in docs:
            e = acc.setdefault(d["doc"], {"doc": d["doc"], "hits": 0, "lines": [], "queries": [], "first": len(acc)})
            e["hits"] += 1
            line = d.get("range")
            if isinstance(line, str) and line.isdigit() and int(line) not in e["lines"]:
                e["lines"].append(int(line))
            if isinstance(query, str) and query and query not in e["queries"]:
                e["queries"].append(query)
    rows = sorted(acc.values(), key=lambda e: (-len(e["queries"]), -e["hits"], e["first"]))
    for e in rows:
        e["lines"] = sorted(e["lines"])[:FOUND_LINES_MAX]
        e["queries"] = e["queries"][:FOUND_LINES_MAX]
        e["opened"] = e["doc"] in opened
        del e["first"]
    return rows, hidden, omitted


def tool_totals(merged: MergedCallLog) -> list[dict]:
    """道具×親/子/判定不能ごとの累計 `{tool, role, calls, found, ms, errors, truncated}`（中身は持たない）。`found` は件数が分かった呼び出しの件数の合計。"""
    acc: dict[tuple, dict] = {}
    for r in merged.rows:
        key = (r["tool"], r.get("role") if r.get("role") in ROLES else "undetermined")
        e = acc.setdefault(key, {"tool": key[0], "role": key[1], "calls": 0, "found": 0, "ms": 0, "errors": 0, "truncated": 0})
        e["calls"] += 1
        e["found"] += _count(r.get("count"))
        e["ms"] += _count(r.get("ms"))
        e["errors"] += 1 if r.get("status") == "error" else 0
        e["truncated"] += 1 if r.get("status") == "truncated" else 0
    return [acc[k] for k in sorted(acc)]


def merge_graph_results(directory: str | os.PathLike | None, turn_id: str, merged: MergedCallLog) -> None:
    """`directory` の `graph-*.jsonl`（グラフの道具の結果）を読み、`merged.graph` に時刻の順で入れる（`role` は `merged.rows` の同じ call_id から。無ければ判定不能）。
    違うターンの行は取り込まず、同じ call_id は 1 つにする。壊れた行・読めないファイルは `merged.graph_broken` に数える。例外は投げない。
    """
    if directory is None:
        return
    directory = Path(directory)
    if directory.is_symlink():
        merged.graph_broken += 1
        return
    try:
        files = sorted(p for p in directory.iterdir() if _GRAPH_FILE_NAME_RE.match(p.name))
    except FileNotFoundError:
        return
    except OSError:
        merged.graph_broken += 1
        return
    if len(files) > MAX_FILES:
        merged.graph_broken += len(files) - MAX_FILES
        files = files[:MAX_FILES]
    roles = {r["call_id"]: r.get("role", "undetermined") for r in merged.rows}
    seen: set[str] = set()
    out: list[dict] = []
    for path in files:
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_size > GRAPH_BYTES_CAP + DETAIL_BYTES_CAP:
                raise OSError("not a regular file or too large")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                data = os.read(fd, GRAPH_BYTES_CAP + DETAIL_BYTES_CAP + 1)
            finally:
                os.close(fd)
            if len(data) > GRAPH_BYTES_CAP + DETAIL_BYTES_CAP:
                raise OSError("too large")
        except OSError:
            merged.graph_broken += 1
            continue
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw.decode("utf-8"))
            except ValueError:
                merged.graph_broken += 1
                continue
            if not (isinstance(row, dict) and isinstance(row.get("call_id"), str) and isinstance(row.get("tool"), str)
                    and row["tool"] in GRAPH_TOOLS and isinstance(row.get("ts"), (int, float))):
                merged.graph_broken += 1
                continue
            if row.get("turn") != turn_id or row["call_id"] in seen:
                continue
            seen.add(row["call_id"])
            row["role"] = roles.get(row["call_id"], "undetermined")
            out.append(row)
    out.sort(key=lambda r: (r["ts"], r["call_id"]))
    merged.graph = out
