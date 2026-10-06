"""文書の重要度（解析＋階層継承＋除外契約＋台帳への接続・docs/03-鏡モデル.md）。

`_重要度.txt`（`パターン: 高|中|低|なし  # 理由`）の解析・階層継承の解決・除外契約
（`is_importance_control_path` を全入口が呼ぶ）・台帳への接続を検証する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherpa import corpus_docs, doc_ledger, documents, es_index, preview_service, scope, store, worlds
from sherpa.ingest import importance as imp
from sherpa.ingest import worker, world_neo4j

# `.md`＝「設計書」の doctype を前提にするため、登録簿を上流限定に固定する。
pytestmark = pytest.mark.usefixtures("upstream_only_registry")

CTRL = "_重要度.txt"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _count_computes(monkeypatch) -> dict:
    calls = {"n": 0}
    orig = imp._compute_for_world

    def _counting(wd, **kw):
        calls["n"] += 1
        return orig(wd, **kw)

    monkeypatch.setattr(imp, "_compute_for_world", _counting)
    return calls


def _fail_on_open(monkeypatch):
    def _boom(self, *a, **kw):
        raise AssertionError("上限超過ファイルで open() が呼ばれた")

    monkeypatch.setattr(Path, "open", _boom)


def _fail_open_of_sub_control(monkeypatch, on_read=None):
    real_open = Path.open

    def _flaky(self, mode="r", *a, **kw):
        if self.name == imp.CONTROL_FILENAME:
            if on_read:
                on_read(self)
            if self.parent.name == "sub":
                raise OSError("simulated transient failure")
        return real_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _flaky)


# ===== is_importance_control_path =====

@pytest.mark.parametrize("path, expected", [
    (CTRL, True),
    ("4期/02_設計/_重要度.txt", True),
    ("重要度.txt", False),
    ("_重要度.txt.bak", False),
    ("sub/_重要度.txt.old", False),
    ("", False),
    (None, False),
])
def test_is_importance_control_path(path, expected):
    assert bool(imp.is_importance_control_path(path)) is expected


# ===== _parse_line_full =====

@pytest.mark.parametrize("line, expected", [
    ("*.md: 高  # 設計書は優先", ("*.md", "高", "設計書は優先")),
    ("*.md: 中", ("*.md", "中", None)),
    ("*.md: 高 # 障害 #123 対応中のみ参照", ("*.md", "高", "障害 #123 対応中のみ参照")),
    ("*.md: なし  # 通常運用に戻す", ("*.md", "なし", "通常運用に戻す")),
    ("", None),
    ("   ", None),
    ("# コメント行", None),
    ("*.md: 最高", None),
    ("*.md 高", None),
    ("*.md: 高  # 理由に\tタブが入っている", None),
    ("*.md: 高  # 理由にC1\x85制御文字が入っている", None),
    ("*.md: 高  # 理由 続き", None),
    ("*.md: 高  # 理由 続き", None),
])
def test_parse_line(line, expected):
    assert imp._parse_line_full(line)[0] == expected


# ===== parse_control_file =====

def test_parse_control_file_invalid_lines_do_not_block_valid_ones(tmp_path):
    p = tmp_path / CTRL
    _write(p, "\n".join(["# コメント", "", "*.md: 高  # 設計書", "不正な行", "*.cbl: 最高", "*.cpy: 中"]))
    rules, diags = imp.parse_control_file(p, config_rel=CTRL)
    assert [r.pattern for r in rules] == ["*.md", "*.cpy"]
    assert [d.line for d in diags] == [4, 5]
    assert all(d.config_path == CTRL for d in diags)


@pytest.mark.parametrize("raw, code", [
    pytest.param(f"{'a' * (imp._MAX_PATTERN_LEN + 1)}: 高".encode(), "pattern_too_long", id="pattern-too-long"),
    pytest.param(f"*.md: 高  # {'あ' * (imp._MAX_REASON_BYTES + 1)}".encode(), "reason_too_long",
                 id="reason-too-long-multibyte"),
    pytest.param(f"*.md: 高  # {'a' * (imp._MAX_REASON_BYTES + 1)}".encode(), "reason_too_long",
                 id="reason-one-byte-over"),
    # 許可する行区切りは \n／\r\n のみ。strip・splitlines による制御文字チェックの迂回を許さない
    pytest.param("*.md: 高  # 理由\r".encode(), "reason_control_char", id="bare-trailing-cr"),
    pytest.param("*.md: 高  # 理由の前半\x85理由の後半\n".encode(), "reason_control_char", id="nel-in-reason"),
    pytest.param("*.md: 高  #\t理由本体\t\n".encode(), "reason_control_char", id="edge-tab-not-stripped"),
])
def test_parse_control_file_line_error(tmp_path, raw, code):
    p = tmp_path / CTRL
    p.write_bytes(raw)
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == code


def test_parse_control_file_reason_limit_is_utf8_bytes_exactly(tmp_path):
    p = tmp_path / CTRL
    _write(p, f"*.md: 高  # {'a' * imp._MAX_REASON_BYTES}")
    rules, diags = imp.parse_control_file(p)
    assert len(rules) == 1 and diags == []


def test_parse_control_file_total_bytes_over_limit_invalidates_whole_file(tmp_path):
    p = tmp_path / CTRL
    p.write_bytes(("*.md: 高\n" * 20000).encode("utf-8"))
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and len(diags) == 1 and diags[0].code == "file_too_large"


def test_parse_control_file_rule_count_over_limit_stops_and_flags(tmp_path):
    p = tmp_path / CTRL
    _write(p, "\n".join(f"f{i}.md: 高" for i in range(imp._MAX_RULES_PER_FILE + 5)))
    rules, diags = imp.parse_control_file(p)
    assert len(rules) == imp._MAX_RULES_PER_FILE
    assert any(d.code == "too_many_rules" for d in diags)


def test_parse_control_file_handles_crlf_line_endings(tmp_path):
    p = tmp_path / CTRL
    p.write_text("*.md: 高\r\n*.cbl: 低\r\n", encoding="utf-8", newline="")
    rules, diags = imp.parse_control_file(p)
    assert diags == []
    assert [(r.pattern, r.value) for r in rules] == [("*.md", "高"), ("*.cbl", "低")]


def test_parse_control_file_invalid_utf8_is_diagnosed_per_line(tmp_path):
    # 不正な行だけ診断にし、前後の正しい行は残す。column は行内バイトオフセット（"*.cbl: " は 7 バイト→8）。
    p = tmp_path / CTRL
    p.write_bytes("*.md: 高\n".encode("utf-8") + b"*.cbl: \xff\xfe\n" + "*.cpy: 低\n".encode("utf-8"))
    rules, diags = imp.parse_control_file(p)
    assert [(r.pattern, r.value) for r in rules] == [("*.md", "高"), ("*.cpy", "低")]
    assert len(diags) == 1
    assert diags[0].column == 8
    assert diags[0].code == "invalid_encoding" and diags[0].line == 2


def test_read_control_bytes_caps_actual_read_at_limit_plus_one(tmp_path, monkeypatch):
    # stat() が小さく偽装されても（TOCTOU）読み取り操作自体を上限+1 に制限し、実バイト数で再検査する。
    p = tmp_path / CTRL
    p.write_bytes(b"x" * (imp._MAX_TOTAL_BYTES * 4))

    class _FakeStat:
        st_size = 10

    real_stat = Path.stat
    real_open = Path.open
    requested = []

    def _fake_stat(self, *a, **kw):
        return _FakeStat() if self == p else real_stat(self, *a, **kw)

    def _tracking_open(self, mode="r", *a, **kw):
        f = real_open(self, mode, *a, **kw)
        if self == p and mode == "rb":
            real_read = f.read

            def _tracking_read(n=-1):
                requested.append(n)
                return real_read(n)
            f.read = _tracking_read
        return f

    monkeypatch.setattr(Path, "stat", _fake_stat)
    monkeypatch.setattr(Path, "open", _tracking_open)

    raw, diag = imp._read_control_bytes(p, CTRL)
    assert requested == [imp._MAX_TOTAL_BYTES + 1]
    assert raw is None
    assert diag is not None and diag.code == "file_too_large"

    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "file_too_large"


def test_parse_control_file_checks_size_via_stat_before_reading(tmp_path, monkeypatch):
    p = tmp_path / CTRL
    p.write_bytes(("*.md: 高\n" * 20000).encode("utf-8"))
    _fail_on_open(monkeypatch)
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags[0].code == "file_too_large"


def test_parse_control_file_os_error_on_read_is_diagnosed_not_silently_empty(tmp_path, monkeypatch):
    p = tmp_path / CTRL
    _write(p, "*.md: 高")

    def _boom(self, *a, **kw):
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "open", _boom)
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and len(diags) == 1 and diags[0].code == "read_error"


def test_invalid_value_message_does_not_reflect_raw_input(tmp_path):
    p = tmp_path / CTRL
    _write(p, "*.md: <script>絶対に表示されない値</script>")
    _rules, diags = imp.parse_control_file(p)
    assert diags[0].code == "invalid_value"
    assert "<script>" not in diags[0].message and "絶対に表示されない値" not in diags[0].message


# ===== 制御ファイルの読み取り（world 単位） =====

def test_resolve_for_world_reads_each_control_file_exactly_once(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    reads = []
    real = imp._read_control_bytes

    def _counting(path, cfg):
        reads.append(cfg)
        return real(path, cfg)

    monkeypatch.setattr(imp, "_read_control_bytes", _counting)
    res = imp.resolve_for_world("test-importance-w-single-read", sig="sig-single")
    assert reads == [CTRL]
    assert res["a.md"].value == "高"


def test_compute_for_world_uses_passed_contents_not_a_fresh_read(tmp_path):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    contents, errors = imp._read_all_control_contents(tmp_path)
    assert errors == {}
    _write(tmp_path / CTRL, "*.md: 低")   # 再読していればここが反映される
    res = imp._compute_for_world(tmp_path, control_contents=contents)
    assert res["a.md"].value == "高"


def test_read_all_control_contents_does_not_read_oversized_file(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高\n")
    monkeypatch.setattr(imp, "_MAX_TOTAL_BYTES", 1)
    _fail_on_open(monkeypatch)
    contents, errors = imp._read_all_control_contents(tmp_path)
    assert contents == {}
    assert set(errors) == {CTRL} and errors[CTRL].code == "file_too_large"


def test_read_all_control_contents_preserves_successful_reads_when_another_file_fails(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高\n")
    _write(tmp_path / "sub" / CTRL, "*.cbl: 低\n")
    _fail_open_of_sub_control(monkeypatch)
    contents, errors = imp._read_all_control_contents(tmp_path)
    assert set(contents) == {CTRL}
    assert contents[CTRL].decode("utf-8") == "*.md: 高\n"
    assert set(errors) == {"sub/_重要度.txt"} and errors["sub/_重要度.txt"].code == "read_error"


# ===== _match_segment_glob =====

@pytest.mark.parametrize("pattern, rel, expected", [
    ("*.md", "a.md", True),
    ("*.md", "sub/a.md", False),
    ("**", "a.md", True),
    ("**", "sub/deep/a.md", True),
    ("sub/**", "sub/deep/a.md", True),
    ("sub/**", "other/deep/a.md", False),
    ("**/*.cbl", "sub/a.cbl", True),
    ("?.md", "a.md", True),
    ("?.md", "ab.md", False),
    ("[ab].md", "a.md", True),
    ("[ab].md", "c.md", False),
])
def test_match_segment_glob(pattern, rel, expected):
    assert imp._match_segment_glob(pattern, rel) is expected


def test_match_segment_glob_many_doublestars_is_fast():
    import time

    pattern = "/".join(["**"] * 9)
    rel = "/".join(f"seg{i}" for i in range(20))
    t0 = time.perf_counter()
    assert imp._match_segment_glob(pattern, rel) is True
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 10, f"想定より遅い: {elapsed_ms:.2f}ms"


# ===== _resolve_rel（階層継承） =====

def _resolve_one(tmp_path, rel, paths):
    by_folder = {imp._parent_rel(p.relative_to(tmp_path).as_posix()): imp.parse_control_file(
        p, config_rel=p.relative_to(tmp_path).as_posix()) for p in paths}
    return imp._resolve_rel(rel, by_folder)


@pytest.mark.parametrize("files, rel, value, config", [
    pytest.param({CTRL: "**: 中", f"sub/{CTRL}": "*.md: 高"}, "sub/a.md", "高", "sub/_重要度.txt",
                 id="deepest-ancestor-with-matching-rule-wins"),
    pytest.param({CTRL: "**/*.cbl: 高", f"mid/{CTRL}": "*.cpy: 低"}, "mid/deep.cbl", "高", CTRL,
                 id="ancestor-without-matching-rule-is-skipped"),
    pytest.param({CTRL: "*: 中\n*.cbl: 高"}, "a.cbl", "高", CTRL, id="glob-beats-folder-default"),
    pytest.param({CTRL: "*.cbl: 高\n?.cbl: 低"}, "a.cbl", "低", CTRL, id="last-matching-glob-wins"),
    pytest.param({CTRL: "**: 高", f"sub/{CTRL}": "*.md: なし"}, "sub/a.md", None, None,
                 id="none-value-clears-ancestor"),
    pytest.param({CTRL: "*.MD: 高"}, "a.md", None, None, id="case-sensitive"),
    pytest.param({CTRL: "*.cbl: 高"}, "a.md", None, None, id="no-matching-rule"),
    # 上限超過（file_too_large）はそのファイルだけ無効にして祖父母へ遡る
    pytest.param({CTRL: "**: 高", f"sub/{CTRL}": "# padding\n" * 20000 + "*.md: 低\n"}, "sub/a.md", "高", CTRL,
                 id="oversized-ancestor-falls-back-to-grandparent"),
])
def test_resolve_one(tmp_path, files, rel, value, config):
    for name, text in files.items():
        _write(tmp_path / name, text)
    paths = [tmp_path / n for n in files if n.endswith(CTRL)]
    res = _resolve_one(tmp_path, rel, paths)
    if value is None:
        assert res is None
    else:
        assert res.value == value and res.config_path == config


def test_resolve_one_read_error_is_terminal_not_fallback_to_ancestor(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "**: 高")          # 誤って使われてはいけない祖父母
    _write(tmp_path / "sub" / CTRL, "*.md: 低")
    _fail_open_of_sub_control(monkeypatch)
    assert _resolve_one(tmp_path, "sub/a.md", [tmp_path / CTRL, tmp_path / "sub" / CTRL]) is None


# ===== resolve_for_world / resolve_many / diagnostics_for_world =====

def test_resolve_for_world_applies_hierarchy_and_excludes_control_files(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "**: 中")
    _write(tmp_path / "sub" / CTRL, "*.md: 高  # 重要")
    _write(tmp_path / "sub" / "a.md", "x")
    _write(tmp_path / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    res = imp.resolve_for_world("test-importance-w1", sig="sig-1")
    assert set(res) == {"sub/a.md", "b.cbl"}
    assert res["sub/a.md"].value == "高" and res["sub/a.md"].reason == "重要"
    assert res["b.cbl"].value == "中"


def test_resolve_for_world_caches_when_content_unchanged(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    calls = _count_computes(monkeypatch)
    res1 = imp.resolve_for_world("test-importance-w2", sig="sig-x")
    res2 = imp.resolve_for_world("test-importance-w2", sig="sig-x")
    assert calls["n"] == 1
    assert res1 is res2


def test_resolve_for_world_files_signature_invalidates_cache_on_new_document(tmp_path, monkeypatch):
    # 明示 sig が変わらなくても、files=（rel 集合）をキャッシュキーへ畳み込むので追加/rename に追随する。
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)

    def _resolve():
        return imp.resolve_for_world("test-importance-files-sig", sig="fixed-sig",
                                     files=list(imp.scope_infer.safe_files(tmp_path)))

    assert set(_resolve()) == {"a.md"}
    _write(tmp_path / "b.md", "y")
    res2 = _resolve()
    assert set(res2) == {"a.md", "b.md"}
    assert res2["b.md"].value == "高"
    (tmp_path / "a.md").rename(tmp_path / "a_renamed.md")
    res3 = _resolve()
    assert set(res3) == {"a_renamed.md", "b.md"}
    assert "a.md" not in res3


def test_resolve_for_world_files_signature_still_caches_when_files_unchanged(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    calls = _count_computes(monkeypatch)
    files = list(imp.scope_infer.safe_files(tmp_path))
    res1 = imp.resolve_for_world("test-importance-files-sig-stable", sig="sig-x", files=files)
    res2 = imp.resolve_for_world("test-importance-files-sig-stable", sig="sig-x", files=list(files))
    assert calls["n"] == 1
    assert res1 is res2


def test_resolve_for_world_cache_invalidated_when_control_file_content_changes(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    res1 = imp.resolve_for_world("test-importance-w2c", sig="sig-x")
    _write(tmp_path / CTRL, "*.md: 低")   # sig 引数は同じ値のまま
    res2 = imp.resolve_for_world("test-importance-w2c", sig="sig-x")
    assert res1["a.md"].value == "高"
    assert res2["a.md"].value == "低"


def test_resolve_for_world_cache_key_includes_world_id(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    res_a = imp.resolve_for_world("test-importance-world-a", sig="same-sig")
    _write(tmp_path / CTRL, "*.md: 低")
    res_b = imp.resolve_for_world("test-importance-world-b", sig="same-sig")
    assert res_a["a.md"].value == "高"
    assert res_b["a.md"].value == "低"


def test_resolve_for_world_root_and_signature_come_from_one_world_dir_call(tmp_path, monkeypatch):
    # root 解決と署名計算で world_dir() を 2 回呼ぶと rebind 競合で旧 root の結果を新署名で保存しうる。
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return tmp_path

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    res = imp.resolve_for_world("test-importance-rebind")
    assert calls["n"] == 1
    assert res["a.md"].value == "高"


def test_resolve_for_world_treats_oversized_control_file_as_unresolvable_and_caches(tmp_path, monkeypatch):
    # file_too_large は決定的な事実＝判定不能（未設定）になり、read_error と違いキャッシュされる。
    _write(tmp_path / CTRL, "*.md: 高\n")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(imp, "_MAX_TOTAL_BYTES", 1)
    assert imp.resolve_for_world("test-importance-toolarge") == {}

    calls = _count_computes(monkeypatch)
    res1 = imp.resolve_for_world("test-importance-toolarge-cache", sig="sig-fixed")
    res2 = imp.resolve_for_world("test-importance-toolarge-cache", sig="sig-fixed")
    assert calls["n"] == 1
    assert res1 is res2


def test_resolve_for_world_partial_failure_does_not_reread_successful_file(tmp_path, monkeypatch):
    # 1 件でも読み取り失敗があれば fail-closed でキャッシュを使わないが、成功分は再読しない。
    _write(tmp_path / CTRL, "*.md: 高\n")
    _write(tmp_path / "sub" / CTRL, "*.cbl: 低\n")
    _write(tmp_path / "a.md", "x")
    _write(tmp_path / "sub" / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    reads = []
    _fail_open_of_sub_control(monkeypatch, on_read=lambda p: reads.append(p.parent.name or "root"))
    res = imp.resolve_for_world("test-importance-partial-fail")
    assert sorted(reads) == sorted([tmp_path.name, "sub"])
    assert res.get("a.md") is not None and res["a.md"].value == "高"
    assert "sub/b.cbl" not in res


def test_resolve_for_world_transient_signature_failure_does_not_poison_cache(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高\n")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    orig_open = Path.open
    state = {"fail": True}

    def _flaky(self, mode="r", *a, **kw):
        if state["fail"] and self.name == imp.CONTROL_FILENAME:
            raise OSError("simulated transient failure")
        return orig_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _flaky)
    assert imp.resolve_for_world("test-importance-flaky") == {}
    assert not any(k[0] == "test-importance-flaky" for k in imp._CACHE)

    state["fail"] = False
    assert imp.resolve_for_world("test-importance-flaky")["a.md"].value == "高"


def test_resolve_for_world_cache_is_true_lru_updates_order_on_hit(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)

    def _cached(world_id):
        return any(k[0] == world_id for k in imp._CACHE)

    first = "test-importance-lru-first"
    imp.resolve_for_world(first, sig="s0")
    for i in range(1, imp._CACHE_MAX):
        imp.resolve_for_world(f"test-importance-lru-{i}", sig=f"s{i}")
    assert _cached(first)
    imp.resolve_for_world(first, sig="s0")                                 # ヒットで最近使用へ
    imp.resolve_for_world("test-importance-lru-extra", sig="s-extra")      # 1 件追い出す
    assert _cached(first)


def test_resolve_many_filters_to_requested_rels(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    _write(tmp_path / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    assert set(imp.resolve_many("test-importance-w3", ["a.md", "b.cbl", "missing.md"])) == {"a.md"}


def test_resolve_many_forwards_sig_and_files_to_resolve_for_world(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高")
    _write(tmp_path / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    calls = _count_computes(monkeypatch)
    files = list(imp.scope_infer.safe_files(tmp_path))
    out1 = imp.resolve_many("test-importance-many-sig", ["a.md"], sig="sig-x", files=files)
    out2 = imp.resolve_many("test-importance-many-sig", ["a.md"], sig="sig-x", files=list(files))
    assert calls["n"] == 1
    assert set(out1) == set(out2) == {"a.md"}
    assert out1["a.md"].value == "高" and out1["a.md"] is out2["a.md"]


def test_diagnostics_for_world_reports_syntax_errors(tmp_path, monkeypatch):
    _write(tmp_path / CTRL, "*.md: 高\n不正な行\n")
    monkeypatch.setattr(worlds, "world_dir", lambda w: tmp_path)
    diags = imp.diagnostics_for_world("test-importance-w4")
    assert len(diags) == 1 and diags[0]["line"] == 2


def test_resolve_for_world_unknown_world_is_empty(monkeypatch):
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)
    assert imp.resolve_for_world("nope") == {}


# ===== 除外契約の配線 =====

def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")
    return wd


def test_control_file_is_excluded_from_every_entrypoint(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    _write(wd / CTRL, "*.md: 高")
    monkeypatch.setattr(worlds, "default_world", lambda: "wtest")
    assert {d["name"] for d in corpus_docs.world_documents("wtest")} == {"a.md"}
    assert scope._content_rels("wtest", root=wd) == ["a.md"]
    assert documents.world_rel_set(root=wd) == {"a.md"}
    assert documents.resolve(CTRL, "wtest") is None
    # scanned（走査総数）には含めてよく、indexed・by_doctype には数えない
    report = corpus_docs.scan_report("wtest")
    assert report["scanned"] == 2
    assert report["indexed"] == 1
    assert report["by_doctype"] == {"設計書": 1}


def test_status_document_doctype_and_manifest_count_exclude_control_file():
    assert corpus_docs.status_document_doctype(CTRL, "wtest") is None
    assert corpus_docs.status_document_doctype("4期/_重要度.txt", "wtest") is None
    assert corpus_docs.status_document_doctype("a.md", "wtest") == "設計書"
    manifest = {"a.md": [1, 2, 3], CTRL: [1, 2, 3], "b.cbl": [1, 2, 3]}
    assert corpus_docs.manifest_doctype_count(manifest, "wtest") == 2


def test_doc_ledger_public_and_preview_documents_carry_importance_and_exclude_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / CTRL, "*.md: 高  # 一次資料")
    _write(wd / "a.md", "x")
    _write(wd / "b.cbl", "x")   # 一致規則なし

    pub = {d["name"]: d for d in doc_ledger.public_documents("wtest")}
    assert set(pub) == {"a.md", "b.cbl"}
    assert pub["a.md"]["importance"] == "高"
    assert pub["a.md"]["importance_reason"] == "一次資料"
    assert pub["a.md"]["importance_source"] == "_重要度.txt:1行目"
    assert "importance" not in pub["b.cbl"]

    prev = {d["name"]: d for d in doc_ledger.preview_documents("wtest")}
    assert prev["a.md"]["importance"] == "高"
    assert "importance" not in prev["b.cbl"]


def test_public_documents_and_preview_documents_share_one_root_resolution(monkeypatch, tmp_path):
    # 文書列挙と重要度解決で root を別々に解決すると、rebind の間隔で旧 root の一覧に新 root の重要度が付く。
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    _write(root_a / CTRL, "*.md: 高")
    _write(root_a / "a.md", "x")
    _write(root_b / CTRL, "*.md: 低")
    _write(root_b / "a.md", "x")
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return root_a if calls["n"] == 1 else root_b

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    docs = {d["name"]: d for d in doc_ledger.public_documents("wtest")}
    assert calls["n"] == 1
    assert docs["a.md"]["importance"] == "高"


@pytest.mark.parametrize("fn", [doc_ledger.public_documents, doc_ledger.preview_documents])
def test_documents_fail_closed_when_root_unresolved(monkeypatch, fn):
    # root 未解決なら打ち切り、下流で root=None を「省略」と誤解釈して再解決しない。
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return None

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    assert fn("wtest") == []
    assert calls["n"] == 1


def test_doc_ledger_control_diagnostics_surfaces_syntax_errors(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / CTRL, "*.md: 高\n不正な行\n")
    diags = doc_ledger.control_diagnostics("wtest")
    assert len(diags) == 1 and diags[0]["code"] == "no_colon"


def test_preview_service_build_preview_includes_importance_diagnostics(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / CTRL, "不正な行\n")
    monkeypatch.setattr(worlds, "world_label", lambda w: w)
    pv = preview_service.build_preview("wtest")
    assert pv["importance_diagnostics"] and pv["importance_diagnostics"][0]["code"] == "no_colon"


# ===== スキーマ版は world 署名の材料（旧世代の台帳化は標準の署名不一致→全再構築に乗せる） =====

def test_world_signature_changes_when_importance_schema_version_bumped(monkeypatch, tmp_path):
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 1)
    sig_v1 = worker.world_signature_of_root(wd)
    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 2)
    assert worker.world_signature_of_root(wd) != sig_v1
    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 1)
    assert worker.world_signature_of_root(wd) == sig_v1


def test_sync_stays_on_unchanged_path_when_signature_matches(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    sig = worker.world_signature("wtest")
    monkeypatch.setattr(store, "get_world",
                        lambda w: {"last_sig": sig, "last_manifest": {"a.md": [1, 2, 3]}, "last_doc_count": 1})
    monkeypatch.setattr(worker, "_derived_stale", lambda w: False)
    monkeypatch.setattr(es_index, "needs_reindex", lambda w, s: False)
    monkeypatch.setattr(world_neo4j, "load_world", lambda *a, **kw: None)   # 実 Neo4j との境界だけを外す
    monkeypatch.setattr(world_neo4j, "check_graph_counts", lambda *a, **kw: None)
    run_calls = []
    monkeypatch.setattr(worker, "run", lambda w, reflect=True: run_calls.append(w) or {})
    res = worker.sync("wtest")
    assert run_calls == []
    assert res == {"world": "wtest", "changed": False, "status": "unchanged", "ledger": 0}
