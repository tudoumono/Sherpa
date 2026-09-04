"""文書の重要度（I1: 解析＋階層継承＋除外契約＋台帳への接続・docs/03-鏡モデル.md）。

`_重要度.txt`（1行1パターン＝`パターン: 高|中|低|なし  # 理由`）の解析・階層継承の解決・
除外契約（`is_importance_control_path` を全入口が呼ぶ）・台帳への接続を検証する。
経路別反映（grep/ES/Neo4j/回答生成）は別スライスの対象で、ここでは扱わない。
"""
from __future__ import annotations

import json
from pathlib import Path

from sherpa import corpus_docs, doc_ledger, documents, es_index, preview_service, scope, store, worlds
from sherpa.ingest import importance as imp
from sherpa.ingest import worker


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ===================================================================
# is_importance_control_path（§5 の単一判定関数）
# ===================================================================

def test_is_importance_control_path_matches_basename_at_any_depth():
    assert imp.is_importance_control_path("_重要度.txt")
    assert imp.is_importance_control_path("4期/02_設計/_重要度.txt")


def test_is_importance_control_path_rejects_lookalikes():
    assert not imp.is_importance_control_path("重要度.txt")           # 先頭の _ が無い
    assert not imp.is_importance_control_path("_重要度.txt.bak")      # 別名（拡張子違い）
    assert not imp.is_importance_control_path("sub/_重要度.txt.old")
    assert not imp.is_importance_control_path("")
    assert not imp.is_importance_control_path(None)


# ===================================================================
# _parse_line（1行解析・空行/コメント/構文エラーは None）
# ===================================================================

def test_parse_line_valid_with_reason():
    assert imp._parse_line("*.md: 高  # 設計書は優先") == ("*.md", "高", "設計書は優先")


def test_parse_line_reason_omitted():
    assert imp._parse_line("*.md: 中") == ("*.md", "中", None)


def test_parse_line_blank_and_comment_lines_are_none():
    assert imp._parse_line("") is None
    assert imp._parse_line("   ") is None
    assert imp._parse_line("# コメント行") is None


def test_parse_line_value_typo_is_none():
    assert imp._parse_line("*.md: 最高") is None


def test_parse_line_missing_colon_is_none():
    assert imp._parse_line("*.md 高") is None


def test_parse_line_reason_may_contain_further_hash():
    """理由に `#` を含めたい場合: 最初の `#` だけを区切りとして扱う。"""
    assert imp._parse_line("*.md: 高 # 障害 #123 対応中のみ参照") == ("*.md", "高", "障害 #123 対応中のみ参照")


def test_parse_line_clear_value_is_valid():
    assert imp._parse_line("*.md: なし  # 通常運用に戻す") == ("*.md", "なし", "通常運用に戻す")


def test_parse_line_reason_rejects_tab_and_c1_control_chars():
    """制御文字の拒否はタブ（C0）・C1（\\x80-\\x9f）も対象にする。"""
    assert imp._parse_line("*.md: 高  # 理由に\tタブが入っている") is None
    assert imp._parse_line("*.md: 高  # 理由にC1\x85制御文字が入っている") is None


def test_parse_line_reason_rejects_unicode_line_and_paragraph_separators():
    """理由は1行の契約のため、Unicode の LINE/PARAGRAPH SEPARATOR（U+2028/U+2029）も
    制御文字として拒否する（`str.splitlines()` を使わない行分割のため、これらの文字は行区切り
    として消費されず理由の中身として実際に届く＝ここで拒否しないと通ってしまう）。"""
    assert imp._parse_line("*.md: 高  # 理由\u2028続き") is None
    assert imp._parse_line("*.md: 高  # 理由\u2029続き") is None


# ===================================================================
# parse_control_file（行単位の構文エラー・上限）
# ===================================================================

def test_parse_control_file_invalid_lines_do_not_block_valid_ones(tmp_path):
    p = tmp_path / "_重要度.txt"
    _write(p, "\n".join([
        "# コメント",
        "",
        "*.md: 高  # 設計書",
        "不正な行",
        "*.cbl: 最高",
        "*.cpy: 中",
    ]))
    rules, diags = imp.parse_control_file(p, config_rel="_重要度.txt")
    assert [r.pattern for r in rules] == ["*.md", "*.cpy"]
    assert [d.line for d in diags] == [4, 5]
    assert all(d.config_path == "_重要度.txt" for d in diags)


def test_parse_control_file_pattern_too_long_is_line_error(tmp_path):
    p = tmp_path / "_重要度.txt"
    _write(p, f"{'a' * (imp._MAX_PATTERN_LEN + 1)}: 高")
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "pattern_too_long"


def test_parse_control_file_reason_too_long_is_line_error(tmp_path):
    p = tmp_path / "_重要度.txt"
    _write(p, f"*.md: 高  # {'あ' * (imp._MAX_REASON_BYTES + 1)}")
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "reason_too_long"


def test_parse_control_file_reason_limit_is_utf8_bytes_not_chars(tmp_path):
    """理由の上限は文字数ではなく UTF-8 バイト数（マルチバイト文字だと閾値がより早く来る）。"""
    p_ok = tmp_path / "ok" / "_重要度.txt"
    _write(p_ok, f"*.md: 高  # {'a' * imp._MAX_REASON_BYTES}")          # ちょうど上限バイト数（1byte文字）
    rules_ok, diags_ok = imp.parse_control_file(p_ok)
    assert len(rules_ok) == 1 and diags_ok == []

    p_over = tmp_path / "over" / "_重要度.txt"
    _write(p_over, f"*.md: 高  # {'a' * (imp._MAX_REASON_BYTES + 1)}")   # 1バイト超過
    rules_over, diags_over = imp.parse_control_file(p_over)
    assert rules_over == [] and diags_over and diags_over[0].code == "reason_too_long"


def test_parse_control_file_total_bytes_over_limit_invalidates_whole_file(tmp_path):
    p = tmp_path / "_重要度.txt"
    p.write_bytes(("*.md: 高\n" * 20000).encode("utf-8"))                # 64KiB を超える
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and len(diags) == 1 and diags[0].code == "file_too_large"


def test_parse_control_file_rule_count_over_limit_stops_and_flags(tmp_path):
    p = tmp_path / "_重要度.txt"
    _write(p, "\n".join(f"f{i}.md: 高" for i in range(imp._MAX_RULES_PER_FILE + 5)))
    rules, diags = imp.parse_control_file(p)
    assert len(rules) == imp._MAX_RULES_PER_FILE
    assert any(d.code == "too_many_rules" for d in diags)


def test_parse_control_file_handles_crlf_line_endings(tmp_path):
    """`\\r\\n` は正しく1行として扱う（`\\n`/`\\r\\n` のみを行区切りにする実装の回帰確認）。"""
    p = tmp_path / "_重要度.txt"
    p.write_text("*.md: 高\r\n*.cbl: 低\r\n", encoding="utf-8", newline="")
    rules, diags = imp.parse_control_file(p)
    assert diags == []
    assert [(r.pattern, r.value) for r in rules] == [("*.md", "高"), ("*.cbl", "低")]


def test_parse_control_file_bare_trailing_cr_without_lf_is_not_treated_as_crlf(tmp_path):
    """許可される行区切りは `\\n`／`\\r\\n` のみ——ファイルが `\\n` で終端せず生の `\\r` で
    終わっている場合、その `\\r` は CRLF の一部ではなく理由フィールドに混入した制御文字
    として扱う（黙って剥がして CRLF 扱いにすると、制御文字チェックを迂回してしまう）。"""
    p = tmp_path / "_重要度.txt"
    p.write_bytes("*.md: 高  # 理由\r".encode("utf-8"))   # 末尾が \n で終端しない生の \r
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "reason_control_char"


def test_parse_control_file_nel_in_reason_does_not_bypass_control_char_check(tmp_path):
    """`str.splitlines()` は NEL（`\\x85`）等も行区切りとして扱うため、
    理由に埋め込まれた NEL が『行の分割』によって消費され、制御文字チェックまで届かなかった
    （迂回）。`\\n`/`\\r\\n` のみを行区切りにすることでこの迂回を防ぐ。"""
    p = tmp_path / "_重要度.txt"
    p.write_text("*.md: 高  # 理由の前半\x85理由の後半\n", encoding="utf-8")
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "reason_control_char"


def test_parse_control_file_reason_edge_control_char_not_hidden_by_strip(tmp_path):
    """理由の先頭/末尾のタブは `str.strip()` が『空白』として黙って
    落としてしまう（タブは `splitlines()` の行区切りではないため、行分割の修正だけでは
    防げない）。制御文字チェックを `strip()` の前に行うことでこの迂回を防ぐ。"""
    p = tmp_path / "_重要度.txt"
    p.write_text("*.md: 高  #\t理由本体\t\n", encoding="utf-8")
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "reason_control_char"


def test_parse_control_file_invalid_utf8_is_diagnosed_not_silently_replaced(tmp_path):
    """不正な UTF-8 バイト列は
    `errors='replace'` で黙って置換せず、行番号つきの診断にする（壊れたファイルの中身を
    気付かせずに読み進めない）。デコードは行単位（§8「エラー行だけ無効・他の有効行は
    生きる」）なので、不正な行の**前後**にある正しい行は巻き添えにせず解析結果に残す。
    column は `UnicodeDecodeError.start`（その行内でのバイトオフセット）から算出する——
    `"*.cbl: "` は7バイトの ASCII なので、直後の不正バイトは offset 7＝column 8。"""
    p = tmp_path / "_重要度.txt"
    p.write_bytes("*.md: 高\n".encode("utf-8") + b"*.cbl: \xff\xfe\n" + "*.cpy: 低\n".encode("utf-8"))
    rules, diags = imp.parse_control_file(p)
    assert [(r.pattern, r.value) for r in rules] == [("*.md", "高"), ("*.cpy", "低")]
    assert len(diags) == 1
    assert diags[0].column == 8
    assert diags[0].code == "invalid_encoding" and diags[0].line == 2


def test_parse_control_file_rechecks_actual_bytes_read_against_size_limit(tmp_path, monkeypatch):
    """`stat()` と実際の読み取りの間にファイルが増量した場合（TOCTOU）
    でも、実際に読めたバイト数で上限を再検査する（規則は適用しない）。"""
    p = tmp_path / "_重要度.txt"
    p.write_text("*.md: 高\n", encoding="utf-8")

    class _FakeStat:
        st_size = 10   # stat() は上限以下と報告する

    class _FakeFile:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"x" * (imp._MAX_TOTAL_BYTES + 1)

    monkeypatch.setattr(Path, "stat", lambda self: _FakeStat())
    monkeypatch.setattr(Path, "open", lambda self, mode="r", *a, **kw: _FakeFile())

    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags and diags[0].code == "file_too_large"


def test_read_control_bytes_caps_actual_read_at_limit_plus_one(tmp_path, monkeypatch):
    """旧実装は `path.read_bytes()` で無条件に EOF まで読んでから上限を
    検査していたため、`stat()` の判定を回避する巨大ファイルでは丸ごとメモリに載ってしまい、
    上限保護が実質無意味だった。読み取り**操作自体**を `_MAX_TOTAL_BYTES + 1` バイトに
    制限することで、`stat()` がどんな値を報告していても実際に読むバイト数を固定する。

    巨大な実ファイルを用意し、`stat()` だけ小さく偽装する（TOCTOU で stat 後に増量した
    場合の再現）ことで、上限保護の実体が読み取り操作そのものにあることを固定する。
    """
    p = tmp_path / "_重要度.txt"
    p.write_bytes(b"x" * (imp._MAX_TOTAL_BYTES * 4))   # 実ファイルは上限の4倍超（内容自体は無関係）

    class _FakeStat:
        st_size = 10   # stat() は上限以下と偽って報告する（stat 早期判定を素通りさせる）

    real_stat = Path.stat
    real_open = Path.open

    def _fake_stat(self, *a, **kw):
        return _FakeStat() if self == p else real_stat(self, *a, **kw)

    requested = []

    def _tracking_open(self, mode="r", *a, **kw):
        f = real_open(self, mode, *a, **kw)
        if self == p and mode == "rb":
            real_read = f.read

            def _tracking_read(n=-1):
                requested.append(n)
                return real_read(n)
            f.read = _tracking_read
        return f

    monkeypatch.setattr(Path, "stat", _fake_stat)          # 対象ファイルだけ偽装（他の Path 操作は素通し）
    monkeypatch.setattr(Path, "open", _tracking_open)

    raw, diag = imp._read_control_bytes(p, "_重要度.txt")

    assert requested == [imp._MAX_TOTAL_BYTES + 1]     # 読み取り要求は上限+1に固定（無制限に読まない）
    assert raw is None                                 # 実バイト数の再検査で上限超過と判定される
    assert diag is not None and diag.code == "file_too_large"


def test_resolve_for_world_reads_each_control_file_exactly_once(tmp_path, monkeypatch):
    """`resolve_for_world` は制御ファイルを world 全体で**1回だけ**読み
    （署名計算用・解析用で別々に読まない）、その同じバイト列から署名と解析結果の両方を導く。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)

    reads = []
    real_read_control_bytes = imp._read_control_bytes

    def _counting(path, cfg):
        reads.append(cfg)
        return real_read_control_bytes(path, cfg)

    monkeypatch.setattr(imp, "_read_control_bytes", _counting)

    res = imp.resolve_for_world("test-importance-w-single-read", sig="sig-single")

    assert reads == ["_重要度.txt"]           # 1回だけ読む（署名用・解析用で別々に読まない）
    assert res["a.md"].value == "高"


def test_compute_for_world_uses_passed_contents_not_a_fresh_read(tmp_path):
    """`control_contents` を渡した場合、`_compute_for_world` はディスクを
    再度読まず、渡されたバイト列だけを解析に使う——署名計算時に読んだ内容と解析時の内容が
    必ず一致する（片方だけ内容/失敗が食い違う、という構造的な穴を無くす）ことの核となる保証。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")

    contents, errors = imp._read_all_control_contents(root)
    assert errors == {}

    # 読み取り後にディスク上のファイルを別内容へ書き換える（再読していればここが解析に反映される）。
    _write(root / "_重要度.txt", "*.md: 低")

    res = imp._compute_for_world(root, control_contents=contents)
    assert res["a.md"].value == "高"          # 渡された（読み取り時点の）内容のまま＝再読していない


# ===================================================================
# _match_segment_glob（`*`/`?`/`[seq]` は1セグメント内のみ・`**` だけが複数セグメントを跨ぐ）
# ===================================================================

def test_match_segment_glob_star_does_not_cross_segments():
    assert imp._match_segment_glob("*.md", "a.md")
    assert not imp._match_segment_glob("*.md", "sub/a.md")


def test_match_segment_glob_doublestar_crosses_segments():
    assert imp._match_segment_glob("**", "a.md")
    assert imp._match_segment_glob("**", "sub/deep/a.md")
    assert imp._match_segment_glob("sub/**", "sub/deep/a.md")
    assert not imp._match_segment_glob("sub/**", "other/deep/a.md")
    assert imp._match_segment_glob("**/*.cbl", "sub/a.cbl")


def test_match_segment_glob_question_and_charclass():
    assert imp._match_segment_glob("?.md", "a.md")
    assert not imp._match_segment_glob("?.md", "ab.md")
    assert imp._match_segment_glob("[ab].md", "a.md")
    assert not imp._match_segment_glob("[ab].md", "c.md")


# ===================================================================
# resolve_one（階層継承・§3 のケース網羅）
# ===================================================================

def test_resolve_one_deepest_ancestor_with_matching_rule_wins(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "**: 中")
    _write(root / "sub" / "_重要度.txt", "*.md: 高")
    paths = [root / "_重要度.txt", root / "sub" / "_重要度.txt"]
    res = imp.resolve_one(root, "sub/a.md", paths)
    assert res.value == "高" and res.config_path == "sub/_重要度.txt"


def test_resolve_one_ancestor_without_matching_rule_is_skipped(tmp_path):
    """深い祖先に `_重要度.txt` はあるが一致規則が無い→無視してさらに上の祖先へ遡る。"""
    root = tmp_path
    _write(root / "_重要度.txt", "**/*.cbl: 高")
    _write(root / "mid" / "_重要度.txt", "*.cpy: 低")           # .cbl には一致しない
    paths = [root / "_重要度.txt", root / "mid" / "_重要度.txt"]
    res = imp.resolve_one(root, "mid/deep.cbl", paths)
    assert res.value == "高" and res.config_path == "_重要度.txt"


def test_resolve_one_glob_beats_folder_default_in_same_file(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "*: 中\n*.cbl: 高")
    res = imp.resolve_one(root, "a.cbl", [root / "_重要度.txt"])
    assert res.value == "高"


def test_resolve_one_last_glob_wins_when_multiple_match(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "*.cbl: 高\n?.cbl: 低")
    res = imp.resolve_one(root, "a.cbl", [root / "_重要度.txt"])
    assert res.value == "低"


def test_resolve_one_none_value_clears_ancestor_setting(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "**: 高")
    _write(root / "sub" / "_重要度.txt", "*.md: なし")
    paths = [root / "_重要度.txt", root / "sub" / "_重要度.txt"]
    assert imp.resolve_one(root, "sub/a.md", paths) is None


def test_resolve_one_pattern_matching_is_case_sensitive(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "*.MD: 高")
    assert imp.resolve_one(root, "a.md", [root / "_重要度.txt"]) is None


def test_resolve_one_no_matching_rule_anywhere_is_none(tmp_path):
    root = tmp_path
    _write(root / "_重要度.txt", "*.cbl: 高")
    assert imp.resolve_one(root, "a.md", [root / "_重要度.txt"]) is None


# ===================================================================
# resolve_for_world / resolve_many / diagnostics_for_world（world 単位・キャッシュ）
# ===================================================================

def test_resolve_for_world_applies_hierarchy_and_excludes_control_files(tmp_path, monkeypatch):
    root = tmp_path
    _write(root / "_重要度.txt", "**: 中")
    _write(root / "sub" / "_重要度.txt", "*.md: 高  # 重要")
    _write(root / "sub" / "a.md", "x")
    _write(root / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    res = imp.resolve_for_world("test-importance-w1", sig="sig-1")
    assert set(res) == {"sub/a.md", "b.cbl"}
    assert res["sub/a.md"].value == "高" and res["sub/a.md"].reason == "重要"
    assert res["b.cbl"].value == "中"
    assert "_重要度.txt" not in res and "sub/_重要度.txt" not in res


def test_resolve_for_world_caches_when_content_unchanged(tmp_path, monkeypatch):
    """sig（引数指定）・`_重要度.txt` の中身とも変わらなければ、2回目はキャッシュヒット
    （`_compute_for_world` を再実行しない）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    calls = {"n": 0}
    orig = imp._compute_for_world

    def _counting(wd, **kw):
        calls["n"] += 1
        return orig(wd, **kw)

    monkeypatch.setattr(imp, "_compute_for_world", _counting)
    res1 = imp.resolve_for_world("test-importance-w2", sig="sig-x")
    res2 = imp.resolve_for_world("test-importance-w2", sig="sig-x")
    assert calls["n"] == 1                                # 2回目は再計算しない
    assert res1 is res2                                    # キャッシュから同一オブジェクトを返す


def test_resolve_for_world_files_signature_invalidates_cache_on_new_document(tmp_path, monkeypatch):
    """RV2是正#a2: 呼び出し側が明示 `sig`（例: 登録済み world の `last_sig`）を渡す経路では、
    `sig` 自体は次回 sync まで変わらない。その間に非制御ファイルを追加/rename しても、`files=`
    （materialize 済み rel 集合）を渡していればキャッシュキーへ畳み込まれるため、新しい文書へも
    解決が付く（`_files_rel_signature` は既に受け取った list を並べ替えるだけ＝追加 walk なし）。
    """
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)

    files1 = list(imp.scope_infer.safe_files(root))
    res1 = imp.resolve_for_world("test-importance-files-sig", sig="fixed-sig", files=files1)
    assert set(res1) == {"a.md"}

    _write(root / "b.md", "y")                            # 同一 sig・同一 _重要度.txt のまま文書追加
    files2 = list(imp.scope_infer.safe_files(root))
    res2 = imp.resolve_for_world("test-importance-files-sig", sig="fixed-sig", files=files2)
    assert set(res2) == {"a.md", "b.md"}                  # 新文書にも解決が付く（キャッシュに阻まれない）
    assert res2["b.md"].value == "高"

    (root / "a.md").rename(root / "a_renamed.md")         # rename も同様（同一 sig・同一設定内容）
    files3 = list(imp.scope_infer.safe_files(root))
    res3 = imp.resolve_for_world("test-importance-files-sig", sig="fixed-sig", files=files3)
    assert set(res3) == {"a_renamed.md", "b.md"}
    assert "a.md" not in res3                             # 消えた旧名の解決を古いキャッシュから引きずらない


def test_resolve_for_world_files_signature_still_caches_when_files_unchanged(tmp_path, monkeypatch):
    """`files=` を渡していても、rel 集合が変わらなければ通常どおりキャッシュヒットする
    （粒度が細かくなっただけで、無関係な理由での再計算は増やさない）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    calls = {"n": 0}
    orig = imp._compute_for_world

    def _counting(wd, **kw):
        calls["n"] += 1
        return orig(wd, **kw)

    monkeypatch.setattr(imp, "_compute_for_world", _counting)
    files = list(imp.scope_infer.safe_files(root))
    res1 = imp.resolve_for_world("test-importance-files-sig-stable", sig="sig-x", files=files)
    res2 = imp.resolve_for_world("test-importance-files-sig-stable", sig="sig-x", files=list(files))
    assert calls["n"] == 1
    assert res1 is res2


def test_resolve_for_world_cache_invalidated_when_control_file_content_changes(tmp_path, monkeypatch):
    """`world_signature` はファイルのメタデータ（rel/mtime/ctime/size）
    のみで内容を見ないため、呼び出し元が渡す `sig` が同じ値のままでも `_重要度.txt` の中身が
    変われば別キャッシュエントリになる（内容ハッシュを実効署名に合成しているため）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    res1 = imp.resolve_for_world("test-importance-w2c", sig="sig-x")
    _write(root / "_重要度.txt", "*.md: 低")               # 中身だけ変える（sig 引数はあえて同じ値のまま）
    res2 = imp.resolve_for_world("test-importance-w2c", sig="sig-x")
    assert res1["a.md"].value == "高"
    assert res2["a.md"].value == "低"                       # 内容ハッシュが変わったため再計算される


def test_resolve_for_world_cache_key_includes_world_id(tmp_path, monkeypatch):
    """同一 signature でも別 world_id の解決結果を取り違えない（§7.2 のキャッシュキー要件）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    res_a = imp.resolve_for_world("test-importance-world-a", sig="same-sig")
    _write(root / "_重要度.txt", "*.md: 低")
    res_b = imp.resolve_for_world("test-importance-world-b", sig="same-sig")
    assert res_a["a.md"].value == "高"
    assert res_b["a.md"].value == "低"


def test_resolve_for_world_root_and_signature_come_from_one_world_dir_call(tmp_path, monkeypatch):
    """root 解決と署名計算が別々に `worlds.world_dir()` を呼ぶと、
    その間隔で rebind（root 差し替え）が起きた場合に「古い root の走査結果」を「新しい root の
    署名」でキャッシュしてしまう。`resolve_for_world` は `world_dir()` を1回だけ呼び、その wd から
    直接署名を作ることでこの race を構造的に閉じる（呼び出し回数=1で検証）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return root

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    res = imp.resolve_for_world("test-importance-rebind")
    assert calls["n"] == 1
    assert res["a.md"].value == "高"


def test_read_all_control_contents_does_not_read_oversized_file(tmp_path, monkeypatch):
    """署名計算の読み取り元（`_read_all_control_contents`）も解析と同じ `_read_control_bytes`
    （stat 先判定）を通る——巨大ファイルをキャッシュ参照の前段（毎回呼ばれる）で無条件に
    開いて読むと、単一 worker がブロックしうる。上限超過ファイルは成功分ではなくエラー辞書に
    振り分けられる。"""
    p = tmp_path / "_重要度.txt"
    p.write_text("*.md: 高\n", encoding="utf-8")
    monkeypatch.setattr(imp, "_MAX_TOTAL_BYTES", 1)   # 実ファイルが必ず上限超過になるよう極端に下げる

    def _boom(self, *a, **kw):
        raise AssertionError("上限超過ファイルで open() が呼ばれた")

    monkeypatch.setattr(Path, "open", _boom)
    contents, errors = imp._read_all_control_contents(tmp_path)
    assert contents == {}
    assert set(errors) == {"_重要度.txt"} and errors["_重要度.txt"].code == "file_too_large"


def test_resolve_for_world_treats_oversized_control_file_as_unresolvable(tmp_path, monkeypatch):
    """上限超過は（read_error と異なりキャッシュを妨げないが）`parse_control_file` 側と
    同じ「判定不能」（未設定）になる（この rel の唯一の祖先が上限超過のため、遡る先の
    祖先が無い）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    monkeypatch.setattr(imp, "_MAX_TOTAL_BYTES", 1)
    res = imp.resolve_for_world("test-importance-toolarge")
    assert res == {}


def test_resolve_for_world_caches_result_even_when_control_file_is_oversized(tmp_path, monkeypatch):
    """`file_too_large`（上限超過）はキャッシュを抑止しない——`read_error` と違い決定的な
    事実（ファイルサイズ）であり一時的な障害ではないため、署名に固定マーカーとして
    含めた上で通常どおりキャッシュされる（2回目は `_compute_for_world` を再実行しない）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    monkeypatch.setattr(imp, "_MAX_TOTAL_BYTES", 1)
    calls = {"n": 0}
    orig = imp._compute_for_world

    def _counting(wd, **kw):
        calls["n"] += 1
        return orig(wd, **kw)

    monkeypatch.setattr(imp, "_compute_for_world", _counting)
    res1 = imp.resolve_for_world("test-importance-toolarge-cache", sig="sig-fixed")
    res2 = imp.resolve_for_world("test-importance-toolarge-cache", sig="sig-fixed")
    assert calls["n"] == 1                  # 2回目は再計算しない（上限超過でもキャッシュされる）
    assert res1 is res2


def test_resolve_one_oversized_ancestor_falls_back_to_grandparent(tmp_path):
    """`file_too_large`（上限超過）は正典どおりそのファイル自体の
    構文エラー扱い——`read_error`（I/O 失敗）と違って判定不能として遡りを打ち切らず、
    そのファイルを無効にして直近の有効な祖先（祖父母）へ遡る。"""
    root = tmp_path
    _write(root / "_重要度.txt", "**: 高")                       # 祖父母: これが効くはず
    # 実際に 64KiB 超のファイルを用意する（グローバル定数は変えず対象ファイルだけ肥大化させる）。
    _write(root / "sub" / "_重要度.txt", "# padding\n" * 20000 + "*.md: 低\n")
    res = imp.resolve_one(root, "sub/a.md", [root / "_重要度.txt", root / "sub" / "_重要度.txt"])
    assert res.value == "高" and res.config_path == "_重要度.txt"


def test_read_all_control_contents_preserves_successful_reads_when_another_file_fails(tmp_path, monkeypatch):
    """1件の読み取り失敗で他の成功分まで捨てない——失敗したファイルだけ
    エラー辞書に振り分け、成功したファイルのバイト列はそのまま保持する（再読で作り直さない）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n")               # 成功する想定
    _write(root / "sub" / "_重要度.txt", "*.cbl: 低\n")      # 読み取り失敗させる

    real_open = Path.open

    def _flaky(self, mode="r", *a, **kw):
        if self.name == imp.CONTROL_FILENAME and self.parent.name == "sub":
            raise OSError("simulated transient failure")
        return real_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _flaky)
    contents, errors = imp._read_all_control_contents(root)
    assert set(contents) == {"_重要度.txt"}
    assert contents["_重要度.txt"].decode("utf-8") == "*.md: 高\n"
    assert set(errors) == {"sub/_重要度.txt"} and errors["sub/_重要度.txt"].code == "read_error"


def test_resolve_for_world_partial_failure_does_not_reread_successful_file(tmp_path, monkeypatch):
    """fail-closed 経路（1件でも読み取り失敗があればキャッシュを使わず
    直接計算）でも、成功したファイルは再読しない——失敗したファイルだけ診断を作り直す。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n")
    _write(root / "sub" / "_重要度.txt", "*.cbl: 低\n")
    _write(root / "a.md", "x")
    _write(root / "sub" / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)

    reads = []
    real_open = Path.open
    sub = root / "sub"

    def _tracking(self, mode="r", *a, **kw):
        if self.name == imp.CONTROL_FILENAME:
            if self.parent == root:
                reads.append("root")
            elif self.parent == sub:
                reads.append("sub")
                raise OSError("simulated transient failure")
        return real_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _tracking)
    res = imp.resolve_for_world("test-importance-partial-fail")

    assert sorted(reads) == ["root", "sub"]               # 各ファイル1回だけ読む（成功分の再読なし）
    assert res.get("a.md") is not None and res["a.md"].value == "高"   # 成功した祖先は正しく効く
    assert "sub/b.cbl" not in res                        # 読めない祖先配下は判定不能


def test_resolve_for_world_transient_signature_failure_does_not_poison_cache(tmp_path, monkeypatch):
    """署名計算中の一時的な OSError を固定プレースホルダ（例: `b"<unreadable>"`）で署名化して
    キャッシュしてしまうと、以後ずっとそのプレースホルダにヒットし続け、実際には正しく読める
    ようになった後も古い（あるいは判定不能の）結果を返し続ける。fail-closed によりこれを防ぐ:
    障害時はキャッシュに一切書き込まない・復旧後は即座に正しい結果へ戻る。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)

    orig_open = Path.open
    state = {"fail": True}

    def _flaky(self, mode="r", *a, **kw):
        if state["fail"] and self.name == imp.CONTROL_FILENAME:
            raise OSError("simulated transient failure")
        return orig_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _flaky)

    res1 = imp.resolve_for_world("test-importance-flaky")
    assert res1 == {}                                       # 障害中は判定不能
    assert not any(k[0] == "test-importance-flaky" for k in imp._CACHE)   # キャッシュに書かれていない

    state["fail"] = False                                   # 復旧
    res2 = imp.resolve_for_world("test-importance-flaky")
    assert res2["a.md"].value == "高"                        # 復旧後は正しい結果に即座に戻る


def test_parse_control_file_checks_size_via_stat_before_reading(tmp_path, monkeypatch):
    """64KiB 上限は `stat()` で判定し、超過ファイルは開いて読まない。"""
    p = tmp_path / "_重要度.txt"
    p.write_bytes(("*.md: 高\n" * 20000).encode("utf-8"))       # 64KiB 超

    def _boom(self, *a, **kw):
        raise AssertionError("上限超過ファイルで open() が呼ばれた")

    monkeypatch.setattr(Path, "open", _boom)
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and diags[0].code == "file_too_large"


def test_parse_control_file_os_error_on_read_is_diagnosed_not_silently_empty(tmp_path, monkeypatch):
    """読み取り失敗（OSError）は空設定として黙って通さず `read_error` 診断を残す。"""
    p = tmp_path / "_重要度.txt"
    _write(p, "*.md: 高")

    def _boom(self, *a, **kw):
        raise OSError("simulated read failure")

    monkeypatch.setattr(Path, "open", _boom)
    rules, diags = imp.parse_control_file(p)
    assert rules == [] and len(diags) == 1 and diags[0].code == "read_error"


def test_resolve_one_read_error_is_terminal_not_fallback_to_ancestor(tmp_path, monkeypatch):
    """読み取れない祖先の設定は「一致規則なし」と混同せず、祖先へは遡らず
    未設定として扱う（誤って祖父母の値へ静かにフォールバックしない）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "**: 高")            # 祖父母: これが誤って使われてはいけない
    _write(root / "sub" / "_重要度.txt", "*.md: 低")   # 直近の親: 読み取り不能にする

    orig_open = Path.open

    def _boom(self, mode="r", *a, **kw):
        if self.name == imp.CONTROL_FILENAME and self.parent.name == "sub":
            raise OSError("simulated read failure")
        return orig_open(self, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", _boom)
    res = imp.resolve_one(root, "sub/a.md", [root / "_重要度.txt", root / "sub" / "_重要度.txt"])
    assert res is None


def test_invalid_value_message_does_not_reflect_raw_input(tmp_path):
    """不正な値を診断メッセージへ反射しない（固定文言のみ）。"""
    p = tmp_path / "_重要度.txt"
    _write(p, "*.md: <script>絶対に表示されない値</script>")
    _rules, diags = imp.parse_control_file(p)
    assert diags[0].code == "invalid_value"
    assert "<script>" not in diags[0].message and "絶対に表示されない値" not in diags[0].message


def test_match_segment_glob_many_doublestars_is_fast(tmp_path):
    """`**` の連続を含むパターンでも DP のため高速（旧・素朴な二分再帰は指数時間）。"""
    import time

    pattern = "/".join(["**"] * 9)
    rel = "/".join(f"seg{i}" for i in range(20))
    t0 = time.perf_counter()
    assert imp._match_segment_glob(pattern, rel) is True
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 10, f"想定より遅い: {elapsed_ms:.2f}ms"


def test_resolve_for_world_cache_is_true_lru_updates_order_on_hit(tmp_path, monkeypatch):
    """キャッシュヒット時にも最近使用として順序更新する真の LRU。

    上限 `_CACHE_MAX` 件までダミーエントリで埋めた後、最初に入れたキーへヒットさせてから
    さらに1件追加しても、直前にヒットさせたキーは（最も古いままではないため）追い出されない。
    """
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)

    def _cached(world_id):                               # キーは (world_id, root実パス, 実効署名) の3-tuple
        return any(k[0] == world_id for k in imp._CACHE)

    first_key_id = "test-importance-lru-first"
    imp.resolve_for_world(first_key_id, sig="s0")
    for i in range(1, imp._CACHE_MAX):
        imp.resolve_for_world(f"test-importance-lru-{i}", sig=f"s{i}")
    assert _cached(first_key_id)                          # 上限ちょうどでまだ追い出されていない

    imp.resolve_for_world(first_key_id, sig="s0")          # ヒット＝最近使用として更新
    imp.resolve_for_world("test-importance-lru-extra", sig="s-extra")   # 上限超過で1件追い出す
    assert _cached(first_key_id)                           # 直前にヒットしたので追い出されない


def test_resolve_many_filters_to_requested_rels(tmp_path, monkeypatch):
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    _write(root / "b.cbl", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    out = imp.resolve_many("test-importance-w3", ["a.md", "b.cbl", "missing.md"])
    assert set(out) == {"a.md"}


def test_resolve_many_forwards_sig_and_files_to_resolve_for_world(tmp_path, monkeypatch):
    """I2是正#2（2026-09-05）: `resolve_many` は `sig`/`files` を `resolve_for_world` へそのまま
    転送する——クエリ経路（grep/impact/出典）が既に world の `last_sig`／materialize 済み一覧を
    持っているなら、これを渡すことで `resolve_for_world` のキャッシュキーが安定し、以後の同一
    world 呼び出しが `_compute_for_world`（規則の解決本体）を再実行しない（`resolve_for_world`
    docstring 参照・`test_resolve_for_world_files_signature_still_caches_when_files_unchanged` と
    同じ手法で `resolve_many` 経由でも成立することを固定する）。"""
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高")
    _write(root / "a.md", "x")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    calls = {"n": 0}
    orig = imp._compute_for_world

    def _counting(wd, **kw):
        calls["n"] += 1
        return orig(wd, **kw)

    monkeypatch.setattr(imp, "_compute_for_world", _counting)
    files = list(imp.scope_infer.safe_files(root))
    out1 = imp.resolve_many("test-importance-many-sig", ["a.md"], sig="sig-x", files=files)
    out2 = imp.resolve_many("test-importance-many-sig", ["a.md"], sig="sig-x", files=list(files))
    assert calls["n"] == 1              # 2回目はキャッシュヒット＝再計算なし
    assert set(out1) == set(out2) == {"a.md"}
    assert out1["a.md"].value == "高" and out1["a.md"] is out2["a.md"]   # 同一キャッシュ由来


def test_diagnostics_for_world_reports_syntax_errors(tmp_path, monkeypatch):
    root = tmp_path
    _write(root / "_重要度.txt", "*.md: 高\n不正な行\n")
    monkeypatch.setattr(worlds, "world_dir", lambda w: root)
    diags = imp.diagnostics_for_world("test-importance-w4")
    assert len(diags) == 1 and diags[0]["line"] == 2


def test_resolve_for_world_unknown_world_is_empty(monkeypatch):
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)
    assert imp.resolve_for_world("nope") == {}


# ===================================================================
# 除外契約の配線（scope / documents / corpus_docs / world_graph / doc_ledger / preview_service）
# ===================================================================

def _world(monkeypatch, tmp_path):
    wd = tmp_path / "world"; wd.mkdir()
    der = tmp_path / "derived"; der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    # derived_dir も隔離する（未指定だと worlds.semantic_dir 等が実リポジトリの data/derived/ を
    # 触ってしまう）。derived_md_dir とは別のディレクトリで良い
    # （テストが両者の親子関係に依存することは無い）。
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived_root")
    return wd


def test_corpus_docs_excludes_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    _write(wd / "_重要度.txt", "*.md: 高")
    names = {d["name"] for d in corpus_docs.world_documents("wtest")}
    assert names == {"a.md"}


def test_scan_report_excludes_control_file_from_indexed_and_by_doctype(monkeypatch, tmp_path):
    """`_重要度.txt` は検索可能数（`indexed`）・拡張子内訳（`by_doctype`）には数えない。
    `scanned`（走査した総ファイル数）には含めてよい（除外契約は検索可否の集計対象のみ）。"""
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    _write(wd / "_重要度.txt", "*.md: 高")
    report = corpus_docs.scan_report("wtest")
    assert report["scanned"] == 2
    assert report["indexed"] == 1
    assert report["by_doctype"] == {"設計書": 1}


def test_status_document_doctype_excludes_control_file():
    """`_重要度.txt` は状態API/`/ext/v1/doc`/`document_count` の対象にしない
    （`status_document_doctype()` が単一の判定点・§5）。"""
    assert corpus_docs.status_document_doctype("_重要度.txt", "wtest") is None
    assert corpus_docs.status_document_doctype("4期/_重要度.txt", "wtest") is None
    assert corpus_docs.status_document_doctype("a.md", "wtest") == "設計書"


def test_manifest_doctype_count_excludes_control_file():
    manifest = {"a.md": [1, 2, 3], "_重要度.txt": [1, 2, 3], "b.cbl": [1, 2, 3]}
    assert corpus_docs.manifest_doctype_count(manifest, "wtest") == 2


def test_scope_content_rels_excludes_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    _write(wd / "_重要度.txt", "*.md: 高")
    assert scope._content_rels("wtest", root=wd) == ["a.md"]


def test_documents_world_rel_set_excludes_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    _write(wd / "_重要度.txt", "*.md: 高")
    assert documents.world_rel_set(root=wd) == {"a.md"}


def test_documents_resolve_rejects_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "_重要度.txt", "*.md: 高")
    monkeypatch.setattr(worlds, "default_world", lambda: "wtest")
    assert documents.resolve("_重要度.txt", "wtest") is None


def test_doc_ledger_public_and_preview_documents_carry_importance_and_exclude_control_file(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "_重要度.txt", "*.md: 高  # 一次資料")
    _write(wd / "a.md", "x")
    _write(wd / "b.cbl", "x")               # 重要度の対象外（一致規則なし）

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
    """文書列挙（corpus_docs）と重要度解決（importance）が別々に
    `worlds.world_dir()` を解決すると、rebind の間隔で「旧 root の文書一覧」に「新 root の
    重要度」を誤って付けてしまいうる。`doc_ledger` は1回の root 解決を両者で共有する
    （`world_dir()` の呼び出し回数=1で検証）。"""
    root_a = tmp_path / "a"; root_a.mkdir()
    root_b = tmp_path / "b"; root_b.mkdir()
    _write(root_a / "_重要度.txt", "*.md: 高")
    _write(root_a / "a.md", "x")
    _write(root_b / "_重要度.txt", "*.md: 低")
    _write(root_b / "a.md", "x")
    der = tmp_path / "derived"; der.mkdir()
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)

    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return root_a if calls["n"] == 1 else root_b   # 1回目=A（rebind前）、以降はB（rebind後）と偽装

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)

    docs = {d["name"]: d for d in doc_ledger.public_documents("wtest")}
    assert calls["n"] == 1                              # world_dir() は1回だけ＝root共有の証拠
    assert docs["a.md"]["importance"] == "高"            # A の文書一覧に一貫して A の重要度が付く


def test_public_documents_fails_closed_when_root_unresolved(monkeypatch):
    """root が解決できない（`worlds.world_dir()` が `None`）場合はそこで打ち切り、
    `documents_for`/`resolve_for_world` 側で `root=None` を「省略」と誤解釈させて
    独立に再解決させない（fail-closed・再解決間の rebind 競合を閉じる・呼び出し回数=1で検証）。"""
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return None

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    assert doc_ledger.public_documents("wtest") == []
    assert calls["n"] == 1


def test_preview_documents_fails_closed_when_root_unresolved(monkeypatch):
    calls = {"n": 0}

    def fake_world_dir(w):
        calls["n"] += 1
        return None

    monkeypatch.setattr(worlds, "world_dir", fake_world_dir)
    assert doc_ledger.preview_documents("wtest") == []
    assert calls["n"] == 1


def test_doc_ledger_control_diagnostics_surfaces_syntax_errors(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "_重要度.txt", "*.md: 高\n不正な行\n")
    diags = doc_ledger.control_diagnostics("wtest")
    assert len(diags) == 1 and diags[0]["code"] == "no_colon"


def test_preview_service_build_preview_includes_importance_diagnostics(monkeypatch, tmp_path):
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "_重要度.txt", "不正な行\n")
    monkeypatch.setattr(worlds, "world_label", lambda w: w)
    pv = preview_service.build_preview("wtest")
    assert pv["importance_diagnostics"] and pv["importance_diagnostics"][0]["code"] == "no_colon"



# ===================================================================
# 既存データ移行（旧世代の `_重要度.txt` 台帳化）は専用の移行機構を持たない。重要度機能の
# スキーマ版（`IMPORTANCE_SCHEMA_VERSION`）を world 署名の材料に畳み込むことで、標準の
# 「署名不一致→全再構築」経路に乗せる（`build_world` は制御ファイルを除外済みなので、この
# 1回の rebuild で旧世代の台帳行・Neo4j データが消える）。
# ===================================================================

def test_world_signature_changes_when_importance_schema_version_bumped(monkeypatch, tmp_path):
    """`IMPORTANCE_SCHEMA_VERSION` は world 署名の材料に含まれる——版を上げると、ソース
    ファイル自体が不変でも署名が変わる。同じ版・同じ内容なら署名は再現する。"""
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "a.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 1)
    sig_v1 = worker.world_signature_of_root(wd)

    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 2)
    sig_v2 = worker.world_signature_of_root(wd)
    assert sig_v2 != sig_v1

    monkeypatch.setattr(imp, "IMPORTANCE_SCHEMA_VERSION", 1)
    assert worker.world_signature_of_root(wd) == sig_v1, "同じ版・同じ内容なら署名は再現する"


def test_sync_stays_on_unchanged_path_when_signature_matches(monkeypatch, tmp_path):
    """署名が前回と一致する world は unchanged の高速パスに入る（標準契約）。"""
    wd = _world(monkeypatch, tmp_path)
    _write(wd / "a.md", "x")
    sig = worker.world_signature("wtest")
    monkeypatch.setattr(store, "get_world",
                        lambda w: {"last_sig": sig, "last_manifest": {"a.md": [1, 2, 3]}, "last_doc_count": 1})
    monkeypatch.setattr(worker, "_derived_stale", lambda w: False)   # 派生MD の drift 判定は本テストの対象外
    monkeypatch.setattr(es_index, "needs_reindex", lambda w, s: False)
    run_calls = []
    monkeypatch.setattr(worker, "run", lambda w, reflect=True: run_calls.append(w) or {})

    res = worker.sync("wtest")

    assert run_calls == []
    assert res == {"world": "wtest", "changed": False, "status": "unchanged", "ledger": 0}
