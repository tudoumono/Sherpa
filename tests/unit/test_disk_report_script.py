"""`scripts/disk_report.sh`（容量レポート）: 名前を出さない・ハードリンクは 1 回・道具が無くても止まらない・どこからでも叩ける。"""
from __future__ import annotations

import gzip
import http.server
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import threading
import sys
import unicodedata

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "disk_report.sh"

# 実環境に似せた「出してはいけない」名前（架空）
WORLD = "機密プロジェクトXYZ"
OTHER_WORLD = "他社案件ABC"
FILE_A = "極秘設計書_ABC.docx"
FILE_B = "売上集計_QWE.xlsx"
GONE = "消えた原本_ZZZ.docx"
GONE_LEGACY = "消えた旧書類_YYY.doc"
OLD_LEGACY = "旧様式_PPP.doc"
SECRET = "TOPSECRET-pw-12345"
FORBIDDEN = [WORLD, OTHER_WORLD, "極秘設計書", "売上集計", "消えた原本", "消えた旧書類", "旧様式", "機密プロジェクト",
             "他社案件", SECRET, "QWE", "ZZZ", "YYY", "PPP"]

# 道具が無い環境を作るために PATH へ置く最小の道具
BASIC_TOOLS = ["awk", "sed", "grep", "find", "df", "stat", "cat", "ls", "sort", "cut", "tr", "head", "tail", "wc",
               "date", "mkdir", "rm", "cp", "mktemp", "dirname", "basename", "printenv", "tee", "cmp", "uname", "id",
               "env", "sleep", "xargs", "sh", "bash", "timeout", "gtimeout", "python3", "curl", "sqlite3"]


def _make_bin(tmp_path: pathlib.Path, *, extra: tuple[str, ...] = (), drop: tuple[str, ...] = ()) -> pathlib.Path:
    d = tmp_path / "toolbin"
    d.mkdir(exist_ok=True)
    for name in BASIC_TOOLS:
        if name in drop:
            continue
        p = shutil.which(name)
        if p and not (d / name).exists():
            (d / name).symlink_to(p)
    for name in extra:
        if name in drop:
            continue
        p = shutil.which(name)
        if p and not (d / name).exists():
            (d / name).symlink_to(p)
    return d


def _write(path: pathlib.Path, size: int = 8192, fill: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(os.urandom(size) if fill == b"?" else fill * size)


@pytest.fixture()
def sherpa(tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / "VERSION").write_text("9.9.9\n")
    kb = root / "data" / "kb" / WORLD
    for rel in (FILE_A, f"下位/{FILE_B}", OLD_LEGACY):
        _write(kb / rel, 100)
    der = root / "data" / "derived"
    w = der / WORLD
    # 公開側と二重持ちの共有ファイル（同じ実体）。1 MiB を 2 つのパスから見せる
    big = w / "rag" / f"{FILE_A}.rag.md"
    _write(big, 1024 * 1024, b"?")
    shared = w / "_conv_cache" / f"{FILE_A}.d" / "rag" / f"{FILE_A}.rag.md"
    shared.parent.mkdir(parents=True)
    os.link(big, shared)
    _write(w / "_conv_cache" / f"{FILE_A}.key.json", 100)
    # 原本が無いキャッシュ（件数 1）
    _write(w / "_conv_cache" / f"{GONE}.key.json", 100)
    _write(w / "_conv_cache" / f"{GONE}.d" / "md" / f"{GONE}.md", 8192)
    # 二重持ち（リンク数 1）
    _write(w / "_conv_cache" / f"{FILE_A}.d" / "md" / f"{FILE_A}.md", 8192)
    _write(w / "md" / f"{FILE_A}.md", 4096)
    _write(w / "ir" / f"{FILE_A}.document.json", 4096)
    _write(w / "_legacy_cache" / f"{OLD_LEGACY}.docx", 4096)
    _write(w / "_legacy_cache" / f"{OLD_LEGACY}.docx.key", 100)
    _write(w / "_legacy_cache" / f"{GONE_LEGACY}.docx", 4096)
    _write(w / "_legacy_cache" / f"{GONE_LEGACY}.docx.key", 100)
    _write(w / "archives_work" / "x.staging" / "a.bin", 4096)
    db = w / "semantic" / "embed_cache.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("create table kv (key text primary key, vec text not null)")
    con.executemany("insert into kv values (?, ?)", [(f"k{i}", "[0.1,0.2,0.3]") for i in range(7)])
    con.commit()
    con.close()
    rows = [{"content_type": "table_record", "parent_id": "t1", "body": "あ" * 10},
            {"content_type": "table_record", "parent_id": "t1", "body": "あ" * 30},
            {"content_type": "table_record", "parent_id": "t2", "body": "あ" * 20},
            {"content_type": "paragraph", "body": "x"}]
    with gzip.open(w / "rag" / f"{FILE_B}.rag_chunks.jsonl", "wt", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    _write(w / "semantic" / "embed_cache.sqlite3-wal", 4096)
    _write(w / "ir" / "z.tmp", 4096)
    # 登録されていない派生物
    _write(der / OTHER_WORLD / "md" / "k.md", 4096)
    _write(der / OTHER_WORLD / "x.tmp", 4096)                       # 未登録側の一時ファイルは区分の行に足さない
    _write(der / OTHER_WORLD / "archives_work" / "y.staging" / "a", 4096)
    _write(der / f"{WORLD}.reconvert-bak" / "md" / "k.md", 4096)
    (root / ".env").write_text(
        f"SHERPA_DERIVED_DIR=./data/derived\nSHERPA_KB_DIR=./data/kb\nSHERPA_ES_PORT=9\nPOSTGRES_PASSWORD={SECRET}\n")
    (root / "data" / "run").mkdir(parents=True, exist_ok=True)
    _write(root / "data" / "run" / "convert-20261017-003538.log", 4096)
    return root


def _run(tmp_path, root, *args, bin_dir=None, cwd=None, shell="bash"):
    bin_dir = bin_dir or _make_bin(tmp_path)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path / "tmp"), "LANG": "C"}
    (tmp_path / "tmp").mkdir(exist_ok=True)
    out = tmp_path / "out"
    cmd = [shell, str(SCRIPT), "--root", str(root), "--out", str(out), *args]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd or tmp_path, timeout=300)
    return p, out


def _width(line: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in line)


def _fake_psql(bin_dir: pathlib.Path, root: pathlib.Path) -> None:
    f = bin_dir / "psql"
    f.write_text(
        "#!/bin/sh\n"
        'case "$*" in *"from worlds"*) printf \'%s\\t%s\\n\' "' + WORLD + '" "' + str(root / "data" / "kb" / WORLD) + '"; exit 0;; esac\n'
        "exit 1\n")
    f.chmod(0o755)


def _all_text(out: pathlib.Path, stdout: str) -> str:
    return stdout + "".join(p.read_text() for p in sorted(out.glob("disk-report-*")))


def test_names_and_secrets_are_never_printed(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    assert p.returncode == 0, p.stderr
    text = _all_text(out, p.stdout) + p.stderr
    for bad in FORBIDDEN:
        assert bad not in text, bad
    # 保存先のパス以外に Sherpa の置き場所も出さない
    assert str(sherpa) not in text and str(tmp_path) not in text      # 「詳細:」の行も含めて絶対パスを出さない
    assert "詳細: (--out で指定した場所)/disk-report-" in p.stdout
    assert "W1" in p.stdout and "U1" in p.stdout        # 登録済みは W、登録されていないものは U
    assert len(list(out.glob("disk-report-*-summary.txt"))) == 1
    assert len(list(out.glob("disk-report-*-detail.txt"))) == 1
    detail = next(out.glob("*-detail.txt")).read_text()
    assert "table_record" in detail and "中央値" in detail and "Excel 4" in detail


def test_hardlinks_counted_once_and_garbage_counted(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    detail = next(out.glob("*-detail.txt")).read_text()
    lines = {line.split("  ")[0].strip(): line for line in detail.splitlines()}
    actual = next(ln for ln in detail.splitlines() if "実際の大きさ（重複除外）" in ln)
    raw = next(ln for ln in detail.splitlines() if "重複を除かない合計" in ln)
    # 1 MiB の共有ファイルは 1 回だけ（W1）、重複を除かない側は 2 回
    assert "1.1M" in actual or "1.0M" in actual, actual
    assert "2.1M" in raw or "2.0M" in raw or "2.2M" in raw, raw
    assert float(actual.split()[-3].rstrip("MK")) < float(raw.split()[-3].rstrip("MK"))

    section = detail.split("ゴミの候補（消さずに")[1].splitlines()

    def garbage(label):
        ln = next(x for x in section if x.startswith(f" {label}"))
        return int(ln.split()[-2])
    assert garbage("*.staging") == 1
    assert garbage("*.tmp") == 1
    assert garbage("_conv_cache の原本が無いもの") == 2     # key と内容 1 件
    assert garbage("_legacy_cache の原本が無いもの") == 2
    assert garbage("登録されていない派生物のフォルダ") == 1
    assert garbage("退避済みの古いログ") == 1
    assert "リンク数 1" in detail
    assert "W1 JSON 形式: 7 行" in detail
    assert lines


def test_summary_fits_a_screen(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    lines = p.stdout.splitlines()
    assert len(lines) <= 40, len(lines)
    for ln in lines[:-1]:       # 最後の行は保存先のパス
        assert _width(ln) <= 80, (ln, _width(ln))
    assert lines[-1].startswith("詳細: ")
    assert str(tmp_path) not in p.stdout
    assert (out / lines[-1].rsplit("/", 1)[1]).is_file()


def test_missing_tools_do_not_stop_the_report(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path, drop=("python3", "curl", "sqlite3", "timeout", "gtimeout"))
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    assert p.returncode == 0, p.stderr
    assert "つながりません" in p.stdout or "調べられません" in p.stdout
    assert "[ES] 調べられません（curl が無い）" in p.stdout
    assert "[Docker] 調べられません（docker がありません）" in p.stdout
    assert "調べられません（python3 が無い）" in next(out.glob("*-detail.txt")).read_text()


def test_runs_from_any_directory_and_prunes_old_reports(tmp_path, sherpa):
    out = tmp_path / "out"
    out.mkdir()
    for i in range(12):
        for kind in ("summary", "detail"):
            (out / f"disk-report-2026010{i % 9 + 1}-0000{i:02d}-{kind}.txt").write_text("old")
    other = tmp_path / "somewhere" / "else"
    other.mkdir(parents=True)
    bin_dir = _make_bin(tmp_path)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "LANG": "C"}
    # --root なしでも、スクリプトの 1 つ上を Sherpa の置き場所とする
    app = tmp_path / "copy"
    (app / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, app / "scripts" / "disk_report.sh")
    (app / "VERSION").write_text("1.2.3\n")
    p = subprocess.run(["bash", str(app / "scripts" / "disk_report.sh"), "--out", str(out)], capture_output=True,
                       text=True, env=env, cwd=other, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "版 1.2.3" in p.stdout
    p2 = subprocess.run(["bash", str(app / "scripts" / "disk_report.sh")], capture_output=True,
                        text=True, env=env, cwd=other, timeout=120)
    assert "詳細: data/run/disk-report/disk-report-" in p2.stdout
    assert str(tmp_path) not in p2.stdout + p2.stderr
    assert len(list(out.glob("disk-report-*-detail.txt"))) == 10
    assert len(list(out.glob("disk-report-*-summary.txt"))) == 10


def test_bash_syntax_and_old_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    if sys.platform == "darwin" and pathlib.Path("/bin/bash").exists():
        assert subprocess.run(["/bin/bash", "-n", str(SCRIPT)]).returncode == 0


@pytest.mark.skipif(not (sys.platform == "darwin" and pathlib.Path("/bin/bash").exists()), reason="macOS の bash 3.2 だけ")
def test_runs_on_bash_32(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir, shell="/bin/bash")
    assert p.returncode == 0, p.stderr
    for bad in FORBIDDEN:
        assert bad not in _all_text(out, p.stdout)


class _EsStub(http.server.BaseHTTPRequestHandler):
    base = ""

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = self.base
        path = self.path.split("?")[0]
        if path == "/":
            out = '{"version":{"number":"8.19.0"}}'
        elif path == "/_cat/indices":
            out = (f"{b}-g1000 100 5000 5000\n{b}-g900 80 4000 4000\nrs_foreign_idx 5 700 700\n.hidden 1 10 10\n")
        elif path == "/_cat/aliases":
            out = f"{b} {b}-g1000\n"
        elif path.startswith("/_cat/indices/"):
            out = f"{b}-g1000\n{b}-g900\n"
        elif path == "/_nodes/stats/fs":
            out = '{"nodes":{"n":{"fs":{"total":{"total_in_bytes":1000000,"free_in_bytes":600000,"available_in_bytes":500000}}}}}'
        elif path.endswith("/_mapping"):
            out = ('{"x":{"mappings":{"_source":{"excludes":["embedding"]},"properties":'
                   '{"embedding":{"type":"dense_vector","dims":768},"ext":{"type":"keyword"}}}}}')
        else:
            self.send_error(404)
            return
        self._ok(out)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._ok('{"aggregations":{"a":{"buckets":[{"key":".xlsx","doc_count":7},{"key":".pdf","doc_count":3},{"key":".山田商事","doc_count":2}]}}}')

    def _ok(self, text):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_es_section_labels_old_generations_without_names(tmp_path, sherpa):
    from sherpa.es_index import _index
    _EsStub.base = _index(WORLD)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EsStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env_file = sherpa / ".env"
        env_file.write_text(env_file.read_text().replace("SHERPA_ES_PORT=9", f"SHERPA_ES_PORT={srv.server_address[1]}"))
        bin_dir = _make_bin(tmp_path)
        _fake_psql(bin_dir, sherpa)
        p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    finally:
        srv.shutdown()
    assert p.returncode == 0, p.stderr
    text = _all_text(out, p.stdout)
    detail = next(out.glob("*-detail.txt")).read_text()
    assert _EsStub.base not in text and "rs_foreign_idx" not in text
    assert "別名なし 1 本" in p.stdout                       # 別名が指さない古い世代
    assert "別名が指していない古い世代の索引: 1 本" in detail
    assert "768" in detail and "あり" in detail              # 次元と _source.excludes
    assert ".xlsx=7" in detail and "その他=2" in detail and "content_type: 索引に無い" in detail
    assert "山田商事" not in text
    assert "空き 488.3K" in detail
    for bad in FORBIDDEN:
        assert bad not in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root では入れないフォルダを作れない")
def test_unreadable_folders_do_not_leak_paths(tmp_path, sherpa):
    der = sherpa / "data" / "derived" / WORLD
    kb = sherpa / "data" / "kb" / WORLD
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    kb.chmod(0)
    der.chmod(0)
    try:
        p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    finally:
        kb.chmod(0o755)
        der.chmod(0o755)
    text = _all_text(out, p.stdout) + p.stderr
    assert p.returncode == 0
    assert str(tmp_path) not in text and WORLD not in text
    assert "読めません（権限）" in next(out.glob("*-detail.txt")).read_text()


def test_source_matching_only_with_deep(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, out = _run(tmp_path, sherpa, bin_dir=bin_dir)
    detail = next(out.glob("*-detail.txt")).read_text()
    assert "--deep で調べます" in detail and "--deep で行います" in detail


def test_gnu_find_printf_path(tmp_path, sherpa):
    """GNU find の -printf（所有者名を引かない）経路。手元に GNU find が無い環境では偽の find で形式だけ確かめる。"""
    bin_dir = _make_bin(tmp_path, drop=("find",))
    _fake_psql(bin_dir, sherpa)
    real = shutil.which("find")
    (bin_dir / "find").write_text(f"""#!{sys.executable}
import os, subprocess, sys
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("find (GNU findutils) 4.9"); sys.exit(0)
if "-printf" in a:
    if a[:2] == [".", "-maxdepth"]:
        sys.exit(0)
    only_files = "-type" in a and a[a.index("-type") + 1] == "f"
    for dp, dn, fn in os.walk("."):
        names = list(fn) + ([] if only_files else [d for d in dn if os.path.islink(os.path.join(dp, d))])
        for n in names:
            p = os.path.join(dp, n)
            if only_files and not os.path.isfile(p) or os.path.islink(p) and only_files:
                continue
            st = os.lstat(p)
            print(st.st_ino, st.st_blocks, st.st_nlink, "./" + os.path.relpath(p, "."))
    sys.exit(0)
os.execv({real!r}, [{real!r}] + a)
""")
    (bin_dir / "find").chmod(0o755)
    p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    detail = next(out.glob("*-detail.txt")).read_text()
    assert p.returncode == 0
    actual = next(ln for ln in detail.splitlines() if "実際の大きさ（重複除外）" in ln)
    assert "1.1M" in actual or "1.0M" in actual
    section = detail.split("ゴミの候補（消さずに")[1]
    assert any(x.startswith(" _conv_cache の原本が無いもの") and x.split()[-2] == "2" for x in section.splitlines())
    for bad in FORBIDDEN:
        assert bad not in _all_text(out, p.stdout)


@pytest.mark.skipif(os.geteuid() == 0, reason="root では入れないフォルダを作れない")
def test_unreadable_source_subfolder_is_not_counted_as_missing(tmp_path, sherpa):
    sub = sherpa / "data" / "kb" / WORLD / "下位"
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    sub.chmod(0)
    try:
        p, out = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    finally:
        sub.chmod(0o755)
    detail = next(out.glob("*-detail.txt")).read_text()
    assert "原本の一覧: 取れませんでした" in detail
    section = detail.split("ゴミの候補（消さずに")[1].splitlines()
    ln = next(x for x in section if x.startswith(" _conv_cache の原本が無いもの"))
    assert ln.split()[-2] == "0"


@pytest.mark.skipif(os.geteuid() == 0, reason="root では入れないフォルダを作れない")
def test_unreadable_derived_root_is_not_reported_as_empty(tmp_path, sherpa):
    der = sherpa / "data" / "derived"
    bin_dir = _make_bin(tmp_path)
    der.chmod(0)
    try:
        p, out = _run(tmp_path, sherpa, bin_dir=bin_dir)
    finally:
        der.chmod(0o755)
    assert "[派生物] 調べられません（権限）" in p.stdout
    assert "[ゴミ候補] 一部調べられません（権限）" in p.stdout
    assert "派生物はありません" not in p.stdout
    assert str(tmp_path) not in p.stdout


def test_garbage_total_notes_missing_source_matching_without_deep(tmp_path, sherpa):
    bin_dir = _make_bin(tmp_path)
    _fake_psql(bin_dir, sherpa)
    p, _ = _run(tmp_path, sherpa, bin_dir=bin_dir)
    assert "（原本の突き合わせは --deep）" in p.stdout
    p, _ = _run(tmp_path, sherpa, "--deep", bin_dir=bin_dir)
    assert "原本の突き合わせは --deep" not in p.stdout
