"""grep_tool（直接 grep ツール）の単体テスト。

secRV 範囲外是正（2026-07-19・grep 全量ロード OOM）: `grep_search` はヒット判定の前にファイル全体を
`Path.read_text()` で一括ロードしていた。共有フォルダ（world 鏡）に巨大テキストを置ける主体が、
以後の全検索でメモリ枯渇を誘発できた点の是正を検証する（読み込みバイト上限・ヒット引用バイト上限・
env 検証・正常系回帰）。

secRV 範囲外是正 追補（2026-07-19・RV指摘 MED-2）: 上記の上限判定が `len(raw) >= cap` の単純比較
だったため、サイズがちょうど cap の**完全な**ファイルや、cap が改行直後に落ちるファイルでも
最終行を不当に落としていた（false negative）。1 byte 余分に読んで truncated かどうかを判定する
是正（境界3ケース＝ちょうど cap／改行直後／行の途中）を検証する。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa import grep_tool as G


def _write(tmp_path: pathlib.Path, name: str, content) -> pathlib.Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return p


# ===== 上限超えファイル =====

def test_file_cap_bounds_read_hit_before_cap_returned_after_not_searched(monkeypatch, tmp_path):
    """`_GREP_FILE_CAP_BYTES` を小さくすると、上限より前のヒットは返り、上限より後の内容は
    検索されない（仕様として許容）。かつ実装はファイル全体を `read_text()` で一括ロードしない。
    """
    line1 = "NEEDLE line one\n"
    filler = "x" * 200 + "\n"
    line3 = "NEEDLE line far beyond cap\n"
    content = line1 + filler + line3
    _write(tmp_path, "doc.txt", content)

    # 上限を line1 の直後（改行含む）+ filler の一部までに設定（line3 には到達しない）。
    cap = len(line1.encode("utf-8")) + 10
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)

    # プロセスは全量を読まない（read_text が呼ばれない実装であることの検証）。
    def _boom(*a, **kw):
        raise AssertionError("read_text はもう呼ばれない実装であるべき")
    monkeypatch.setattr(pathlib.Path, "read_text", _boom)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert hits[0]["line"] == 1
    assert "NEEDLE line one" in hits[0]["text"]


# ===== 打切りの申告（file_truncated・呼び出し元へ伝える） =====

def test_truncated_hit_reports_file_truncated_true(monkeypatch, tmp_path):
    """cap を超えて実際に切り詰められたファイル由来のヒットには `file_truncated: True` が付く
    （`agentic_search._read_doc_full_text` の `file_truncated` と同じ語彙・同じ意味）。"""
    line1 = "NEEDLE line one\n"
    filler = "x" * 200 + "\n"
    content = line1 + filler
    _write(tmp_path, "doc.txt", content)
    cap = len(line1.encode("utf-8")) + 10   # filler の途中まで＝本当に打ち切られる
    assert cap < len(content.encode("utf-8"))
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert hits[0]["file_truncated"] is True


def test_normal_file_under_cap_has_no_file_truncated_key(tmp_path):
    """打切りが起きていない通常のファイルでは、戻り値が従来と完全に同じ形（`file_truncated`
    キー自体が無い＝加算的変更で既存の消費者を壊さない）。"""
    content = "# 見出し\n本文中に NEEDLE を含む一行\n"
    _write(tmp_path, "doc.md", content)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert "file_truncated" not in hits[0]
    assert set(hits[0].keys()) == {"doc_id", "path", "ext", "line", "span", "text", "match"}


# ===== 中途行破棄 =====

def test_truncated_mid_line_not_returned_as_hit(monkeypatch, tmp_path):
    """上限がちょうど行の途中に落ちる時、切れた最終行は誤ヒットしない（丸ごと捨てる）。

    line2（最終行）に query がフルで含まれる位置まで読めていても、上限で切れた行である以上
    ヒットとして採用しない（部分一致・破損した中途行を根拠にしない）。
    """
    line1 = "header no match\n"
    line2 = "NEEDLE tail padding padding padding padding\n"   # 末尾に改行なしでも良いが揃える
    content = line1 + line2
    _write(tmp_path, "doc.txt", content)

    # line1 は完全に読める。line2 は "NEEDLE" を含み終えた直後で切る（それでも破棄されるはず）。
    needle_end = len(line1.encode("utf-8")) + len("NEEDLE".encode("utf-8"))
    cap = needle_end + 5   # "NEEDLE" の後、まだ line2 の途中（末尾より手前）
    assert cap < len(content.encode("utf-8"))   # 前提: 本当に file 全体より小さい
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert hits == []   # 中途行は丸ごと捨てるため、読めていた分にヒットがあっても採用しない


def test_truncated_mid_line_full_file_regression_without_cap(tmp_path):
    """回帰確認: 上限を適用しない（既定 8MiB）通常サイズの同じ内容なら、line2 のヒットは普通に返る。"""
    line1 = "header no match\n"
    line2 = "NEEDLE tail padding padding padding padding\n"
    content = line1 + line2
    _write(tmp_path, "doc.txt", content)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == 2


# ===== 上限境界のオフバイワン是正（secRV 範囲外是正 追補・2026-07-19・RV指摘 MED-2） =====

def test_cap_exactly_equal_to_full_file_size_keeps_last_line(monkeypatch, tmp_path):
    """サイズがちょうど cap の**完全な**ファイルは切れていない＝最終行を落としてはいけない
    （旧実装は `len(raw) >= cap` だけで判定したため、この完全ファイルまで誤って最終行を捨てていた）。
    """
    content = "header no match\nNEEDLE full line exactly at cap\n"
    _write(tmp_path, "doc.txt", content)
    cap = len(content.encode("utf-8"))   # ファイル全体のバイト数＝cap（超過ではない）
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == 2   # 最終行のヒットが落ちない
    # 本当は全部読めている（cap ちょうど＝超過ではない）ので打切り扱いにしない（false negative 対策）。
    assert "file_truncated" not in hits[0]


def test_cap_falling_right_after_newline_keeps_last_readable_line(monkeypatch, tmp_path):
    """cap が改行直後（＝1行分ちょうど）に落ちる時、その最終行は途中で切れていないので残す。"""
    line1 = "header no match\n"
    line2 = "NEEDLE tail padding padding padding padding\n"
    content = line1 + line2
    _write(tmp_path, "doc.txt", content)
    cap = len(line1.encode("utf-8"))   # ちょうど line1 の改行直後（line2 の手前）
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", cap)

    hits = G.grep_search("header", world="v1", roots=[tmp_path])
    assert len(hits) == 1 and hits[0]["line"] == 1   # line1 は完全に読めており落とさない


# ===== 引用クリップ =====

def test_hit_text_clipped_to_max_bytes_and_valid_utf8(monkeypatch, tmp_path):
    """見出しの無い巨大 MD は1ヒットが文書全体（1節）になり得るが、`text` は
    `_GREP_HIT_TEXT_MAX_BYTES` 以下に切り詰められ、UTF-8 として valid（文字境界を壊さない）。
    """
    monkeypatch.setattr(G, "_GREP_HIT_TEXT_MAX_BYTES", 100)   # 小さい上限で即座に発火させる
    # 見出し無し・マルチバイト文字混じりの長文（1節＝ファイル全体になる）。
    content = "NEEDLE " + ("あ" * 5000)
    _write(tmp_path, "big.md", content)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    text = hits[0]["text"]
    assert len(text.encode("utf-8")) <= 100
    text.encode("utf-8")   # 例外を出さず正しくエンコードできる（壊れた文字が残らない）


def test_hit_text_not_clipped_when_under_default_limit(tmp_path):
    """通常サイズのヒットは既定の 64KiB を大きく下回るため、クリップの影響を受けない。"""
    content = "# 見出し\n本文中に NEEDLE を含む一行\n"
    _write(tmp_path, "doc.md", content)

    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert hits[0]["text"] == content.strip()


# ===== env 検証 =====

def test_env_int_falls_back_on_invalid_values(monkeypatch):
    """負値/非整数/範囲外は既定へ、既定値自体も [lo, hi] にクランプ（`agentic_search._env_int` と同型）。"""
    monkeypatch.setenv("SHERPA_TEST_GREP_LIMIT", "-1")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_GREP_LIMIT", "0")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_GREP_LIMIT", "999999")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_GREP_LIMIT", "abc")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 16
    monkeypatch.setenv("SHERPA_TEST_GREP_LIMIT", "32")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 32
    monkeypatch.delenv("SHERPA_TEST_GREP_LIMIT")
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 16, 1, 256) == 16


def test_env_int_clamps_dynamic_default(monkeypatch):
    """既定値自体が hi を超える場合も hi にクランプする（既定値が上限を素通りしない）。"""
    monkeypatch.delenv("SHERPA_TEST_GREP_LIMIT", raising=False)
    assert G._env_int("SHERPA_TEST_GREP_LIMIT", 1_000_000, 4096, 65536) == 65536


def test_clip_utf8_bytes_does_not_break_multibyte_boundary():
    s = "あ" * 100   # 各文字3バイト（UTF-8）
    clipped = G._clip_utf8_bytes(s, 10)
    assert len(clipped.encode("utf-8")) <= 10
    clipped.encode("utf-8")   # 例外を出さず正しくエンコードできる


# ===== 正常系回帰 =====

def test_normal_small_file_unaffected(tmp_path):
    """正常系（小さい通常ファイル）: 既存動作（doc_id/span/text/line/match）は変わらない。"""
    content = "# 見出しA\n消費税率テスト値を含む一行\n本文2行目\n"
    _write(tmp_path, "sub/doc.md", content)

    hits = G.grep_search("消費税率", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    h = hits[0]
    assert h["doc_id"] == "sub/doc.md"
    assert h["ext"] == ".md"
    assert h["line"] == 2
    assert h["span"] == [1, 3]
    assert h["text"] == content.strip()
    assert h["match"] == "消費税率"


def test_normal_source_file_context_window_unaffected(tmp_path):
    """正常系（COBOL 等ソース）: 前後数行の window 抽出は変わらない。"""
    lines = [f"line {i}" for i in range(1, 8)]
    lines[3] = "line 4 TAX-RATE"   # 0-based index 3 = line 4
    content = "\n".join(lines) + "\n"
    _write(tmp_path, "PROG.cbl", content)

    hits = G.grep_search("TAX-RATE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert hits[0]["line"] == 4
    assert hits[0]["span"] == [2, 6]
    assert hits[0]["text"] == "\n".join(lines[1:6])


# ===== 資料判定の一元化（classify_document・§7 裁定10） =====

def test_grep_search_excludes_declined_registered_code_extension_not_solely_by_code_ext(monkeypatch, tmp_path):
    """登録拡張子（`CODE_EXT`）でも `accepts()` が全滅し、既存の資料種別（md/office/txt/画像等）にも
    該当しなければ「未対応」＝grep 対象外にする（`corpus_docs.classify_document` を実行ゲートに
    集約——`CODE_EXT` の集合だけで「コード」と見なさない）。"""
    from sherpa.ingest.analyzers import registry
    from sherpa.ingest.analyzers._base import Analyzer, DefResult, RefResult

    class _AlwaysDeclineCobol(Analyzer):
        name = "decline_cobol"
        extensions = frozenset({".cbl"})

        def accepts(self, rel_path, head_text=""):
            return False

        def collect_defs(self, text, rel_path):
            return DefResult()

        def extract_refs(self, text, rel_path):
            return RefResult()

    monkeypatch.setattr(registry, "_ANALYZERS", (_AlwaysDeclineCobol(),))
    _write(tmp_path, "PROG.cbl", "line 1\nline 2 TAX-RATE\nline 3\n")

    assert G.grep_search("TAX-RATE", world="v1", roots=[tmp_path]) == []


# ===== 共有ヘルパー: rag 優先・legacy フォールバック =====

def test_rag_grep_enabled_always_true_no_env_toggle():
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・常時 True（env に一切
    左右されない）。"""
    assert G.rag_grep_enabled() is True


def test_strip_derived_suffix_priority_and_passthrough():
    """`.rag.md` と `.rag_observations.md` はどちらも `.md` で終わるため、より具体的な拡張を
    先に剥がす。どれにも一致しない名前はそのまま返す。"""
    assert G.strip_derived_suffix("report.docx.rag.md") == "report.docx"
    assert G.strip_derived_suffix("image.png.rag_observations.md") == "image.png"
    assert G.strip_derived_suffix("report.docx.md") == "report.docx"
    assert G.strip_derived_suffix("設計/資料.pdf.md") == "設計/資料.pdf"
    assert G.strip_derived_suffix("PROG.cbl") == "PROG.cbl"


def test_preferred_derived_name_rag_priority_and_fallback(monkeypatch, tmp_path):
    """rag が有効かつ実在すれば rag 版、無効または rag 版が無ければ legacy 版を返す。"""
    _write(tmp_path, "report.docx.md", "legacy")
    _write(tmp_path, "report.docx.rag.md", "rag")

    assert G.preferred_derived_name(tmp_path, "report.docx") == "report.docx.rag.md"

    # rag_grep_enabled() は常時 True（TOGGLE-RM）だが、`and` の右辺（実在チェック）は
    # 今も生きたコードのため、内部シームとして直接差し替えて左辺 False の分岐も検証する。
    monkeypatch.setattr(G, "rag_grep_enabled", lambda: False)
    assert G.preferred_derived_name(tmp_path, "report.docx") == "report.docx.md"

    monkeypatch.setattr(G, "rag_grep_enabled", lambda: True)
    assert G.preferred_derived_name(tmp_path, "no-rag-doc.xlsx") == "no-rag-doc.xlsx.md"   # rag 版が無い文書は legacy へ（縮退吸収）


def _isolate_derived_world(monkeypatch, world_root, derived_root, obs_root=None, rag_root=None):
    """`grep_search`（roots=None）が読む `worlds.world_dir`/`derived_md_dir`/`derived_rag_dir`/
    `observation_current_dir` を tmp_path 配下へ直接差し替える（DB・fixtures 不要・
    `_safe_doc_path` のテストと同じ手法）。`rag_root`（省略可）＝§8.1 三階層の rag 層ルート。
    省略時は `derived_root` と同じ親配下の `rag/`（本番の兄弟ディレクトリ構成と同じ形）。"""
    from sherpa import worlds as W
    monkeypatch.setattr(W, "world_dir", lambda w: world_root)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: derived_root)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: rag_root if rag_root is not None else derived_root.parent / "rag")
    monkeypatch.setattr(W, "observation_current_dir", lambda w: obs_root)


def test_grep_search_prefers_rag_over_legacy_when_enabled(monkeypatch, tmp_path):
    """ON かつ rag.md が実在するとき、legacy 版は検索対象から外れ、rag 版の内容だけが1件ヒットする
    （二重ヒットを作らない）。doc_id は原本 rel のまま。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der_rag = tmp_path / "derived" / "rag"
    _write(der, "report.docx.md", "legacy NEEDLE body\n")
    _write(der_rag, "report.docx.rag.md", "## 見出し\nrag NEEDLE body\n")
    _isolate_derived_world(monkeypatch, world_root, der, rag_root=der_rag)

    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1
    h = hits[0]
    assert h["doc_id"] == "report.docx" and h["ext"] == ".docx"
    assert "rag NEEDLE" in h["text"] and "legacy NEEDLE" not in h["text"]


def test_grep_search_falls_back_to_legacy_when_rag_missing_even_if_enabled(monkeypatch, tmp_path):
    """ON でも rag.md が存在しない文書は従来どおり legacy 版がヒットする（縮退吸収）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    _write(der, "onlylegacy.xlsx.md", "legacy-only NEEDLE body\n")
    _isolate_derived_world(monkeypatch, world_root, der)

    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1
    assert hits[0]["doc_id"] == "onlylegacy.xlsx"
    assert "legacy-only NEEDLE" in hits[0]["text"]


def test_grep_search_ignores_rag_when_disabled_matches_current_behavior(monkeypatch, tmp_path):
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・env では OFF にできない。
    `rag_grep_enabled` は今も `preferred_derived_name` の内部シーム（`and` の左辺）として生きて
    いるため、直接差し替えて False 分岐（rag.md の実在に関わらず legacy 版だけがヒットする）を
    引き続き検証する。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der_rag = tmp_path / "derived" / "rag"
    _write(der, "report.docx.md", "legacy NEEDLE body\n")
    _write(der_rag, "report.docx.rag.md", "## 見出し\nrag NEEDLE body\n")
    _isolate_derived_world(monkeypatch, world_root, der, rag_root=der_rag)
    monkeypatch.setattr(G, "rag_grep_enabled", lambda: False)

    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1
    assert hits[0]["doc_id"] == "report.docx"
    assert "legacy NEEDLE" in hits[0]["text"] and "rag NEEDLE" not in hits[0]["text"]


def test_grep_search_rag_legacy_and_observations_coexist(monkeypatch, tmp_path):
    """同一 world に rag/legacy 併存の文書・legacy のみの文書・OCR 観測を統合した rag.md が同居しても
    互いに干渉しない（rag 優先の判定は文書ごと）。OCR 観測は `rag.md` へ統合済み（O1・§8.1 一本化）
    のため、観測専用ツリー（隔離 OCR worker が書く別成果物・`{rel}.rag_observations.md`）に同じ語が
    残っていても grep はそちらを走査しない＝二重ヒットにならない。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der_rag = tmp_path / "derived" / "rag"
    obs = tmp_path / "observations" / "gen1"
    _write(der, "report.docx.md", "legacy NEEDLE in report\n")
    _write(der_rag, "report.docx.rag.md", "## 見出し\nrag NEEDLE in report\n")
    _write(der, "plain.xlsx.md", "legacy-only NEEDLE in plain\n")
    # OCR観測はVLMと合流したAI観測レコードとしてrag.md自体に含まれる（office_md._build_observation_set）。
    _write(der_rag, "scan.png.rag.md", "## AI観測\nOCR NEEDLE in scan\n")
    # 未統合の生成物（観測専用ツリー）が残っていても、grepの検索ルートには含まれない。
    _write(obs, "scan.png.rag_observations.md", "OCR NEEDLE in scan（観測専用ツリー・grep対象外）\n")
    _isolate_derived_world(monkeypatch, world_root, der, obs_root=obs, rag_root=der_rag)

    hits = {h["doc_id"]: h for h in G.grep_search("NEEDLE", world="anyworld", max_hits=10)}
    assert set(hits) == {"report.docx", "plain.xlsx", "scan.png"}   # 3文書=3ヒット・二重ヒットなし
    assert "rag NEEDLE" in hits["report.docx"]["text"] and "legacy NEEDLE in report" not in hits["report.docx"]["text"]
    assert "legacy-only NEEDLE" in hits["plain.xlsx"]["text"]
    assert "OCR NEEDLE" in hits["scan.png"]["text"]


def test_grep_search_uses_preferred_derived_name_helper(monkeypatch, tmp_path):
    """grep_search は rag/legacy 優先順位を独自に再実装せず、共有ヘルパー preferred_derived_name を
    実際に経由する（spy で呼び出しを確認。安定 fixture 上の結果一致だけでは、ヘルパーを迂回して
    同じ結果を出す別実装への drift を検出できない）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der_rag = tmp_path / "derived" / "rag"
    _write(der, "report.docx.md", "legacy NEEDLE\n")
    _write(der_rag, "report.docx.rag.md", "## 見出し\nrag NEEDLE\n")
    _isolate_derived_world(monkeypatch, world_root, der, rag_root=der_rag)

    calls = []
    orig = G.preferred_derived_name

    def spy(root, rel):
        calls.append(rel)
        return orig(root, rel)

    monkeypatch.setattr(G, "preferred_derived_name", spy)

    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1 and hits[0]["doc_id"] == "report.docx"
    assert "report.docx" in calls   # ヘルパーを実際に経由した証跡


def test_grep_search_rag_enabled_but_no_derived_files_yields_no_hits(monkeypatch, tmp_path):
    """rag も legacy も存在しない（派生 root が空）場合、ON でも 0 件（受入条件の直接固定）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    der.mkdir(parents=True)   # 空の派生 root（rag/legacy とも無し）
    _isolate_derived_world(monkeypatch, world_root, der)

    assert G.grep_search("NEEDLE", world="anyworld") == []


# ===== deadline（列挙中の期限確認・PART-4） =====

def test_grep_search_deadline_none_is_unbounded_default(tmp_path):
    """`deadline` 省略時（既定 None）は従来どおり無期限——既存呼び出し元は無変更。"""
    _write(tmp_path, "a.md", "NEEDLE here\n")
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1


def test_grep_search_deadline_already_past_raises_before_returning(monkeypatch, tmp_path):
    """`deadline`（`time.monotonic()` 系の絶対期限）が既に過ぎていれば、列挙を`_DEADLINE_CHECK_ENTRIES`
    件処理した時点で `GrepDeadlineExceeded` を送出する。"""
    for i in range(G._DEADLINE_CHECK_ENTRIES + 10):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")
    import time as time_mod
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=time_mod.monotonic() - 1))


def test_grep_search_deadline_checked_mid_enumeration_for_huge_entry_count(monkeypatch, tmp_path):
    """単一ルートに大量のファイルがあっても、`sorted(root.rglob("*"))` が全件列挙し終えるのを
    待たず、列挙段階で`_DEADLINE_CHECK_ENTRIES`件ごとに`deadline`を再確認して打ち切る
    （`sorted()`完了後の1回だけの確認では、その完了自体が長時間ブロックしうる）。1回目のチェック
    （`_DEADLINE_CHECK_ENTRIES`件目）は通し、2回目（`2 * _DEADLINE_CHECK_ENTRIES`件目）で
    超過させることで、最初の1回だけでなく継続的に確認していることを固定する
    （ファイル総数はその2回目のチェック地点を優に超える件数にする）。"""
    n = G._DEADLINE_CHECK_ENTRIES * 3 + 10
    for i in range(n):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        return 0.0 if calls["n"] <= 1 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=50.0))
    assert calls["n"] >= 2


def test_grep_search_deadline_already_past_raises_immediately_regardless_of_entry_count(tmp_path):
    """RV11 是正の固定: 列挙段階の間引き閾値（`_DEADLINE_CHECK_ENTRIES`=256件ごと）未満の小規模な
    ディレクトリ（実際のバグ報告は48件）でも、deadline が既に過ぎていれば列挙・ファイル読込を
    一切せず即座に例外にする——関数開始直後のチェックがこれを保証する（従来は列挙件数が
    間引き間隔未満だとチェックが一度も発火せず、期限切れでも最後まで検索してヒットを返して
    いた）。"""
    n = 48   # 実際のバグ再現件数（_DEADLINE_CHECK_ENTRIES=256 未満）
    for i in range(n):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")
    import time as time_mod
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=time_mod.monotonic() - 1))


def test_grep_search_deadline_checked_during_per_file_processing(monkeypatch, tmp_path):
    """RV11 是正の固定: 列挙段階のチェックを通過するごく小規模なディレクトリでも、ソート後の
    各エントリ処理（ファイル読込・全文走査を含む）ごとに独立して `deadline` を再確認する——
    列挙段階の間引きチェックだけに頼ると、列挙後のファイル読込/全文走査自体で予算を使い切る
    ケースを検知できない。"""
    n = 5
    for i in range(n):
        _write(tmp_path, f"f{i:04d}.md", "NEEDLE\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 最初の数回（関数開始直後・root 境界のチェック）は通す。ファイル処理ループに入った後
        # （3回目以降）で超過させる——列挙段階のチェックは件数不足（n=5<256）で一度も発火しない。
        return 0.0 if calls["n"] <= 2 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=50.0))
    assert calls["n"] >= 3


def test_grep_search_deadline_checked_right_after_file_read_before_scan(monkeypatch, tmp_path):
    """RV12 是正の固定: ファイル読込直後（decode・全文走査に入る前）にも `deadline` を確認する
    ——8MiB 級の読込・decode 自体に時間がかかるケースは、読込前のチェックだけでは検知できない。"""
    _write(tmp_path, "a.md", "NEEDLE\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 1〜3回目（関数開始直後・root 境界・各エントリ処理直前）は通す。4回目（ファイル
        # 読込直後）で超過させる。
        return 0.0 if calls["n"] <= 3 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=50.0))
    assert calls["n"] == 4


def test_grep_search_deadline_checked_within_line_scan_loop(monkeypatch, tmp_path):
    """RV12 是正の固定: 1ファイルの行走査ループ内でも `_DEADLINE_CHECK_LINES` 行ごとに `deadline`
    を再確認する——巨大ファイル1件の全文走査自体がデッドラインを食い潰すケースを検知する。"""
    lines = ["x" for _ in range(G._DEADLINE_CHECK_LINES + 10)]
    _write(tmp_path, "a.md", "\n".join(lines) + "\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 1〜4回目（関数開始直後・root 境界・各エントリ処理直前・ファイル読込直後）は通す。
        # 5回目（行走査ループ内の間引きチェック・i=`_DEADLINE_CHECK_LINES`）で超過させる。
        return 0.0 if calls["n"] <= 4 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], deadline=50.0))
    assert calls["n"] == 5


def test_grep_search_deadline_checked_right_before_early_max_hits_return(monkeypatch, tmp_path):
    """RV12 是正の固定: `max_hits` 到達による早期 `return` の直前にも `deadline` を確認する——
    有効なヒットが既に見つかっていても、その時点で既に期限を超えていれば黙って返さず例外にする
    （「超過は一貫して例外」の契約・部分的な成功結果も例外の対象）。"""
    _write(tmp_path, "a.md", "NEEDLE\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 1〜4回目（関数開始直後・root 境界・各エントリ処理直前・ファイル読込直後）は通す
        # （行走査ループ内チェックは1行目=i=0のため発火しない）。5回目（return 直前）で超過させる。
        return 0.0 if calls["n"] <= 4 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=1, deadline=50.0))
    assert calls["n"] == 5


def test_grep_search_deadline_already_past_raises_even_for_empty_query():
    """RV12 是正の固定: 空 query による早期 `[]` return（従来の分岐順序）も、開始時の deadline
    確認の**後**に位置する——deadline が既に過ぎていれば、query/world の検証結果に関わらず
    `GrepDeadlineExceeded` を送出する（黙って `[]` を返さない）。"""
    import time as time_mod
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("", world="v1", deadline=time_mod.monotonic() - 1))


def test_grep_search_deadline_already_past_raises_even_for_invalid_world():
    import time as time_mod
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="../bad", deadline=time_mod.monotonic() - 1))


def test_grep_search_deadline_not_exceeded_keeps_existing_sorted_order(tmp_path):
    """`deadline`を渡しても超過しなければ、列挙・ヒット順序は`deadline`省略時（ソート順）と
    完全に同一（列挙段階のチェック追加が正常系の挙動を変えない）。"""
    for name in ("b.md", "a.md", "c.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    import time as time_mod
    without = [h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path])]
    withd = [h["doc_id"] for h in G.grep_search(
        "NEEDLE", world="v1", roots=[tmp_path], deadline=time_mod.monotonic() + 3600)]
    assert without == withd == ["a.md", "b.md", "c.md"]


# ===== valid_world（識別子検証・fullmatch 化） =====

def test_valid_world_accepts_normal_identifiers():
    assert G.valid_world("v1") is True
    assert G.valid_world("test_world-2") is True
    assert G.valid_world("a" * 64) is True   # 上限ちょうど（{0,63} なので先頭1文字+63文字=64文字）


def test_valid_world_rejects_trailing_newline():
    """`.match(r"^...$")` は `$` が「文字列末尾の直前の改行」にもマッチするため、末尾に LF が
    付いた値を誤って通してしまう（X-Request-Id 検証で踏んだのと同じ抜け穴・`.fullmatch()` かつ
    アンカー無しパターンで防ぐ）。"""
    assert G.valid_world("v1\n") is False
    assert G.valid_world("v1\r\n") is False


def test_valid_world_rejects_empty_and_invalid_chars():
    assert G.valid_world("") is False
    assert G.valid_world(None) is False
    assert G.valid_world("../etc") is False
    assert G.valid_world("a b") is False
    assert G.valid_world("a" * 65) is False   # 上限超過


# ===== layer（探す対象・調べ方ブロック §3.4） =====

def test_grep_search_layer_both_or_omitted_matches_current_behavior(tmp_path):
    """既定（省略・"both"）はフィルタなし＝現状の挙動と完全に同一。"""
    _write(tmp_path, "設計/仕様.md", "# 見出し\nNEEDLE を含む資料\n")
    _write(tmp_path, "src/PROG.cbl", "NEEDLE を含む行\n" + "\n" * 3)
    omitted = {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path])}
    both = {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], layer="both")}
    assert omitted == both == {"設計/仕様.md", "src/PROG.cbl"}


def test_grep_search_layer_code_only_keeps_code_ext_docs(tmp_path):
    _write(tmp_path, "設計/仕様.md", "# 見出し\nNEEDLE を含む資料\n")
    _write(tmp_path, "src/PROG.cbl", "NEEDLE を含む行\n" + "\n" * 3)
    hits = {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], layer="code")}
    assert hits == {"src/PROG.cbl"}


def test_grep_search_layer_docs_excludes_code_ext(tmp_path):
    _write(tmp_path, "設計/仕様.md", "# 見出し\nNEEDLE を含む資料\n")
    _write(tmp_path, "src/PROG.cbl", "NEEDLE を含む行\n" + "\n" * 3)
    hits = {h["doc_id"] for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], layer="docs")}
    assert hits == {"設計/仕様.md"}


def test_grep_search_layer_invalid_value_raises(tmp_path):
    """不正な layer 値は ValueError（黙って both へ丸めない・fail-loud）。"""
    import pytest
    _write(tmp_path, "src/PROG.cbl", "NEEDLE\n")
    with pytest.raises(ValueError):
        G.grep_search("NEEDLE", world="v1", roots=[tmp_path], layer="bogus")


def test_grep_search_layer_code_with_derived_office_md_is_always_zero_hits(monkeypatch, tmp_path):
    """決定的MD（Office/PDF 由来・is_derived）は常に docs 判定——layer="code" では0件になる
    （§3.4: 決定的MDは code 判定にならない）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    _write(der, "report.docx.md", "NEEDLE 本文\n")
    _isolate_derived_world(monkeypatch, world_root, der)

    assert G.grep_search("NEEDLE", world="anyworld", layer="code") == []
    docs_hits = G.grep_search("NEEDLE", world="anyworld", layer="docs")
    assert len(docs_hits) == 1 and docs_hits[0]["doc_id"] == "report.docx"
# ===== 重要度設定ファイル（_重要度.txt）の除外契約（§5） =====

def test_grep_search_excludes_importance_control_file(tmp_path):
    _write(tmp_path, "a.md", "NEEDLE here\n")
    _write(tmp_path, "_重要度.txt", "NEEDLE: 高\n")   # 検索語を値側にも含め、除外漏れなら誤ってヒットする
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    doc_ids = {h["doc_id"] for h in hits}
    assert doc_ids == {"a.md"}


# ===== 重要度優先の top-K 選抜（I2・2026-09-05・早期打切り撤去＋heap 化） =====

def test_grep_search_no_control_file_preserves_legacy_order_and_set(tmp_path):
    """`_重要度.txt` の無い world（`roots` 明示指定＝importance 解決自体をしない経路）は、
    早期打切りしていた旧実装と完全に同じ集合・同じ順序（＝発見順で先頭 max_hits 件）を返す
    （受け入れ条件の直接固定）。ヒットに `importance` キー自体も付かない。"""
    for name in ("d.md", "b.md", "a.md", "c.md", "e.md"):
        _write(tmp_path, name, "NEEDLE here\n")
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=3)
    assert [h["doc_id"] for h in hits] == ["a.md", "b.md", "c.md"]   # sorted() 発見順の先頭3件
    assert all("importance" not in h and "importance_reason" not in h for h in hits)


def test_grep_search_prioritizes_high_importance_hit_discovered_after_max_hits(monkeypatch, tmp_path):
    """全量走査化（I2）の本旨: 発見順が `max_hits` の外（`z_` で最後に走査される）でも、
    `_重要度.txt` で `高` を付けた文書は heap 選抜で優先的に残る——旧・早期打切り実装なら
    取りこぼしていたケース。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    for name in ("a.md", "b.md", "z_important.md"):   # sorted() で z_important.md が最後に発見される
        _write(world_root, name, "NEEDLE here\n")
    _write(world_root, "_重要度.txt", "z_important.md: 高\n")
    _isolate_derived_world(monkeypatch, world_root, tmp_path / "no_derived_md")

    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=2)
    doc_ids = [h["doc_id"] for h in hits]
    assert len(doc_ids) == 2
    assert doc_ids[0] == "z_important.md"          # 高＝最優先で先頭
    assert hits[0]["importance"] == "高"
    assert "a.md" in doc_ids or "b.md" in doc_ids   # 残り1枠は発見順どおり


def test_grep_search_low_importance_hit_dropped_first_when_over_capacity(monkeypatch, tmp_path):
    """`低` を付けた文書は、他に空き枠を争う中立文書がある限り真っ先に heap から追い出される
    （`高>中/無>低` の序列・§I2）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    _write(world_root, "a_low.md", "NEEDLE here\n")     # sorted() で最初に見つかるが低優先度
    _write(world_root, "b.md", "NEEDLE here\n")
    _write(world_root, "c.md", "NEEDLE here\n")
    _write(world_root, "_重要度.txt", "a_low.md: 低\n")
    _isolate_derived_world(monkeypatch, world_root, tmp_path / "no_derived_md")

    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=2)
    doc_ids = [h["doc_id"] for h in hits]
    assert doc_ids == ["b.md", "c.md"]   # 低優先の a_low.md は上限外へ追い出される


def test_grep_search_importance_reason_is_conditional_key(monkeypatch, tmp_path):
    """理由（`# 理由`）を書いた行だけ `importance_reason` が付く（無ければキー自体を作らない・§2 truth table）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    _write(world_root, "a.md", "NEEDLE here\n")
    _write(world_root, "b.md", "NEEDLE here\n")
    _write(world_root, "_重要度.txt", "a.md: 高  # 契約書\nb.md: 低\n")
    _isolate_derived_world(monkeypatch, world_root, tmp_path / "no_derived_md")

    hits = {h["doc_id"]: h for h in G.grep_search("NEEDLE", world="anyworld", max_hits=10)}
    assert hits["a.md"]["importance"] == "高" and hits["a.md"]["importance_reason"] == "契約書"
    assert hits["b.md"]["importance"] == "低" and "importance_reason" not in hits["b.md"]


def test_grep_search_stops_scanning_at_max_hits_when_imp_map_empty(monkeypatch, tmp_path):
    """コーディネータ裁定（rv-i2-importance #2・2026-09-05・再判定・二経路化採用）: `_重要度.txt`
    の無い world（`imp_map` 空・`roots=` 明示指定も同様）は、旧実装（I2以前）と同じ2つの打切り点
    （ファイル内 break／ファイル境界 return）で `max_hits` 到達時に走査を終える——`deadline` の
    消費も旧水準（5回）に戻り、2文書目（b.md）には一切到達しない（`_check_deadline` はファイル
    境界のチェック止まりで、2文書目の「エントリ処理直前」「ファイル読込直後」チェックは発火
    しない）。sorted() で a.md が先に見つかるため、確実に a.md だけで打ち切れる max_hits=1 を使う。"""
    _write(tmp_path, "a.md", "NEEDLE\n")
    _write(tmp_path, "b.md", "NEEDLE\n")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        return 0.0   # 一度も超過させない——それでも b.md には進まないことが本題

    monkeypatch.setattr(G.time, "monotonic", _clock)
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=1, deadline=50.0)
    assert [h["doc_id"] for h in hits] == ["a.md"]   # b.md には到達しない（早期終了）
    # 開始直後・root境界・a.md のエントリ処理直前・ファイル読込直後・最終return前の5回のみ
    # （旧実装の「早期打切りの check_deadline は5回目」と同水準・b.md 分の追加チェックが無い）。
    assert calls["n"] == 5


def test_grep_search_continues_scanning_past_max_hits_when_imp_map_nonempty(monkeypatch, tmp_path):
    """`_重要度.txt` がある world（`imp_map` 非空）は、後から見つかった `高` 文書がヒープ最下位を
    上書きしうるため上の早期終了条件が成立せず、`max_hits` 到達後も**全量走査**を続ける——旧実装
    （I2以前）なら1文書目の処理直後に成功で返っていた場面（呼び出し回数5回で return）でも、
    2文書目（b.md）の走査へ進むため、その途中/末尾で deadline を超えれば例外になる。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    _write(world_root, "a.md", "NEEDLE\n")
    _write(world_root, "b.md", "NEEDLE\n")
    _write(world_root, "_重要度.txt", "a.md: 中\n")   # imp_map を非空にするためだけの最小規則
    _isolate_derived_world(monkeypatch, world_root, tmp_path / "no_derived_md")

    calls = {"n": 0}

    def _clock():
        calls["n"] += 1
        # 6回目まで（1文書目の走査完了相当）は通す。7回目（2文書目の処理に入ってから
        # 最終チェックに至るまでの間）で超過させる。
        return 0.0 if calls["n"] <= 6 else 100.0

    monkeypatch.setattr(G.time, "monotonic", _clock)
    with pytest.raises(G.GrepDeadlineExceeded):
        list(G.grep_search("NEEDLE", world="anyworld", max_hits=1, deadline=50.0))
    assert calls["n"] >= 7   # 全量走査（早期終了しない）ことの証跡


# ===== 打切りの申告: ヒット0件の打切り文書も報告する（検収是正） =====

def test_truncated_docs_reports_file_with_no_hits(monkeypatch, tmp_path):
    """**ヒットを1件も出さなかった打切り文書**も `truncated_docs` に載る。

    ヒットへ付ける `file_truncated` だけでは、cap より後ろにしか一致が無い文書が完全に無音になる
    （＝「検索したのに出てこない」の正体）。これが打切り申告の本命のケース。
    """
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    # cap を超える本文で、一致語は cap より後ろにしか無い。
    _write(der, "big.docx.md", "x" * 400 + "\nNEEDLE_TAIL\n")
    _isolate_derived_world(monkeypatch, world_root, der)
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 100)

    truncated: list = []
    hits = G.grep_search("NEEDLE_TAIL", world="anyworld", truncated_docs=truncated)
    assert hits == []                       # cap より後ろなので1件も引けない
    assert truncated == ["big.docx"]        # それでも「探せていない文書がある」ことは伝わる


def test_truncated_docs_misses_files_past_the_early_exit_point_when_imp_map_empty(monkeypatch, tmp_path):
    """コーディネータ裁定（rv-i2-importance #2・再判定）: `imp_map` が空で `max_hits` 到達により
    早期終了した場合、それより後の（sorted() 順で後にある）打切り文書は `truncated_docs` に
    載らない——旧実装（I2以前）と同じ意味論に戻るだけであり契約劣化ではない（`_重要度.txt` が
    ある world は常に全量走査するためこの限定は無い・上の
    `test_truncated_docs_reports_file_with_no_hits` が引き続き固定する）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    _write(der, "a.docx.md", "NEEDLE\n")                              # sorted() で先に見つかる・1ヒット
    _write(der, "z_big.docx.md", "x" * 400 + "\nNEEDLE\n")             # sorted() で後・cap 超過で打切り
    _isolate_derived_world(monkeypatch, world_root, der)
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 100)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="anyworld", max_hits=1, truncated_docs=truncated)
    assert [h["doc_id"] for h in hits] == ["a.docx"]   # 早期終了・z_big.docx には到達しない
    assert truncated == []                             # z_big.docx の打切りは報告されない（旧実装と同じ）


def test_truncated_docs_omitted_when_caller_passes_nothing(monkeypatch, tmp_path):
    """`truncated_docs` を渡さない既存呼び出し元は完全に無変更（副作用なし）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    _write(der, "big.docx.md", "x" * 400 + "\nNEEDLE_TAIL\n")
    _isolate_derived_world(monkeypatch, world_root, der)
    monkeypatch.setattr(G, "_GREP_FILE_CAP_BYTES", 100)
    assert G.grep_search("NEEDLE_TAIL", world="anyworld") == []


# ===== ストリーミング走査（本丸・2026-09）: cap 引き上げ・メモリの非比例・単一巨大行 =====

def test_default_cap_finds_hit_beyond_old_8mib_default(tmp_path):
    """既定 cap を 8MiB→64MiB へ引き上げたことの直接固定（受入条件の本命）: 旧既定なら
    見つからなかった（旧8MiBより後ろにしか無い）一致が、既定設定のまま見つかる。"""
    old_default = 8 * 1024 * 1024
    unit = "x" * 999 + "\n"
    filler = unit * ((old_default // len(unit)) + 100)   # 旧既定を確実に超える分量
    content = filler + "NEEDLE_BEYOND_OLD_CAP\n"
    # `.txt`（source 扱い）にする——`.md` は見出しが無いと1ヒットがファイル全体1節になり、
    # `text` が `_GREP_HIT_TEXT_MAX_BYTES` で先頭側だけクリップされて末尾の一致が text に
    # 現れなくなる（本テストの主眼＝cap 引き上げの確認とは無関係な別の仕様に引っかかる）。
    _write(tmp_path, "big.txt", content)
    assert len(content.encode("utf-8")) > old_default   # 前提: 本当に旧既定を超えている

    hits = G.grep_search("NEEDLE_BEYOND_OLD_CAP", world="v1", roots=[tmp_path])
    assert len(hits) == 1
    assert "NEEDLE_BEYOND_OLD_CAP" in hits[0]["text"]
    assert "file_truncated" not in hits[0]   # 新既定(64MiB)内に収まっているので打切りなし


def test_large_file_normal_lines_bounded_memory(tmp_path):
    """通常の改行を含む大きめファイル（20MB超）でも、ヒット探索中の Python 側ピーク割当は
    ファイルサイズに比例しない（節/窓の最小限しか保持しないストリーミング走査の直接固定・
    旧実装なら `raw`/`lines` がファイルサイズ相当を保持していた）。"""
    import tracemalloc
    line = "x" * 200 + "\n"
    n = (20 * 1024 * 1024) // len(line) + 10   # 端数切り捨てを見込んで少し多めに
    content = line * n + "NEEDLE_TAIL_LINE\n"
    _write(tmp_path, "large.txt", content)
    file_size = len(content.encode("utf-8"))
    assert file_size > 20 * 1024 * 1024

    tracemalloc.start()
    try:
        hits = G.grep_search("NEEDLE_TAIL_LINE", world="v1", roots=[tmp_path])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(hits) == 1
    assert peak < 2 * 1024 * 1024   # 20MB超のファイルに対しピーク割当は2MB未満（比例しない）


def test_many_matches_in_single_file_bounded_memory_and_respects_max_hits(tmp_path):
    """RV是正（rv-i2-importance #1・2026-09）: 1ファイルに大量の一致（`max_hits` を大きく超える件数）
    があっても、ヒット発見〜top-K ヒープ供給までのピーク割当は `max_hits` に比例する程度で収まり、
    ファイル内の一致総数には比例しない（旧 `file_hits`/世界規模の `seen` がヒット総数ぶん際限なく
    肥大していた secRV 2026-07-19 型の攻撃面の是正）。`_重要度.txt` が無い world（`roots=` 明示指定）
    なので、返るのは発見順の先頭 `max_hits` 件（既存の I2 契約と完全に同一）。"""
    import tracemalloc
    n = 200_000
    _write(tmp_path, "many.txt", "NEEDLE\n" * n)

    tracemalloc.start()
    try:
        hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=5)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert [h["line"] for h in hits] == [1, 2, 3, 4, 5]   # 発見順の先頭5件（旧早期打切りと同じ集合）
    assert peak < 3 * 1024 * 1024   # 20万件の一致に対しピーク割当は3MB未満（max_hits に比例・一致数に非比例）


def test_single_huge_line_bounded_memory(tmp_path, monkeypatch):
    """改行が来ないまま数十MB続く単一行（secRV MED-B 型の懸念）でも、Python 側のピーク割当は
    `_GREP_LINE_MAX_BYTES` で頭打ちになりファイルサイズに比例しない。"""
    import tracemalloc
    size = 30 * 1024 * 1024
    _write(tmp_path, "huge_single_line.txt", "x" * size)   # 改行なし・NEEDLE も含まない

    tracemalloc.start()
    try:
        hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert hits == []
    assert peak < 15 * 1024 * 1024   # 30MBの行に対しピーク割当はずっと小さい（行サイズに非比例）


def test_line_overflow_reports_truncation_even_without_file_cap(monkeypatch, tmp_path):
    """1行が `_GREP_LINE_MAX_BYTES` を超えて改行が来ない場合、ファイル全体は cap 内でも
    「探せていない範囲がある」として打切りを申告する（黙って取りこぼさない）。"""
    monkeypatch.setattr(G, "_GREP_LINE_MAX_BYTES", 100)
    content = "x" * 150 + "NEEDLE_TAIL\n"   # NEEDLE は保持される先頭100バイトより後ろ
    _write(tmp_path, "bigline.txt", content)

    truncated: list = []
    hits = G.grep_search("NEEDLE_TAIL", world="v1", roots=[tmp_path], truncated_docs=truncated)
    assert hits == []                          # 破棄された範囲にしか一致が無いため見つからない
    assert truncated == ["bigline.txt"]        # それでも打切りは申告される


def test_streaming_source_multiple_hits_adjacent_and_far_apart(tmp_path):
    """ソース（非MD）のストリーミング窓復元: 隣接するヒット・離れたヒットの双方で
    `span`/`text` が旧実装の式（`s=max(1,h-2), e=h+2`）と一致する。"""
    lines = [f"line {i}" for i in range(1, 21)]
    lines[2] = "line 3 NEEDLE"     # 1-based line 3
    lines[3] = "line 4 NEEDLE"     # 隣接（1-based line 4）
    lines[14] = "line 15 NEEDLE"   # 離れている（1-based line 15）
    content = "\n".join(lines) + "\n"
    _write(tmp_path, "PROG.cbl", content)

    hits = {h["line"]: h for h in G.grep_search("NEEDLE", world="v1", roots=[tmp_path], max_hits=10)}
    assert set(hits) == {3, 4, 15}
    assert hits[3]["span"] == [1, 5] and hits[3]["text"] == "\n".join(lines[0:5])
    assert hits[4]["span"] == [2, 6] and hits[4]["text"] == "\n".join(lines[1:6])
    assert hits[15]["span"] == [13, 17] and hits[15]["text"] == "\n".join(lines[12:17])


def test_truncated_docs_deduped_and_empty_when_not_truncated(monkeypatch, tmp_path):
    """打切りが無ければ空のまま。同一文書が複数回当たっても重複させない。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    _write(der, "small.docx.md", "## 節\nNEEDLE ここ\n\n## 節2\nNEEDLE そこ\n")
    _isolate_derived_world(monkeypatch, world_root, der)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="anyworld", truncated_docs=truncated)
    assert hits and truncated == []
    assert all("file_truncated" not in h for h in hits)


# ===== 行番号の定義は splitlines() と同一（検収是正・read_around との整合） =====

def test_line_numbers_match_splitlines_for_exotic_separators(monkeypatch, tmp_path):
    """`\f`（改ページ＝COBOL/JCLリストに実在）・`\r` 単独・`\x85`（NEL）を含む文書でも、
    grep の行番号は旧実装＝`str.splitlines()` の行数え方と一致する。

    read_around/read_doc は `splitlines()` で行を数えるため、grep 側だけ `\n` 限定で数えると
    ヒットの行番号と精読の行番号がズレ、引用と精読が食い違う。
    """
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    body = "先頭行\f2行目のはず\r3行目のはず\x854行目 NEEDLE を含む\n6行目...ではなく5行目\n"
    _write(der, "doc.docx.md", body)
    _isolate_derived_world(monkeypatch, world_root, der)

    expected_lines = body.splitlines()
    expected_line_no = next(i + 1 for i, ln in enumerate(expected_lines) if "NEEDLE" in ln)
    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1
    assert hits[0]["line"] == expected_line_no == 4


def test_consecutive_newlines_count_as_blank_lines(monkeypatch, tmp_path):
    """連続改行（空行）は splitlines() と同じく1行として数える（空セグメントの補正）。"""
    world_root = tmp_path / "world"
    world_root.mkdir()
    der = tmp_path / "derived" / "md"
    body = "1行目\n\n\n4行目 NEEDLE\n"
    _write(der, "doc.docx.md", body)
    _isolate_derived_world(monkeypatch, world_root, der)
    hits = G.grep_search("NEEDLE", world="anyworld")
    assert len(hits) == 1 and hits[0]["line"] == 4


# ===== TEXT-ALL L-1 是正: 軽量テキスト枠のサイズ超過を台帳/ES と同じ基準で grep からも除外 =====

def test_grep_search_excludes_oversized_light_text_document_and_reports_truncated(monkeypatch, tmp_path):
    """未登録拡張子の軽量テキスト枠（`.csv` ＝ `text_kind.DOCUMENT_EXT`）が `text_kind.MAX_BYTES`
    超過なら、`_GREP_FILE_CAP_BYTES`（既定64MiB）内であってもヒットを返さず（台帳/ES の
    `size_exceeded` 除外と一致させる）、`truncated_docs` へ申告する（黙って消さない）。"""
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 50)   # 小さい上限で即座に発火させる
    content = "x" * 100 + "NEEDLE\n"
    assert len(content.encode("utf-8")) > 50
    _write(tmp_path, "doc.csv", content)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)
    assert hits == []
    assert truncated == ["doc.csv"]


def test_grep_search_excludes_oversized_light_text_code_and_reports_truncated(monkeypatch, tmp_path):
    """未登録拡張子の軽量テキスト枠（`.py` ＝ `text_kind.CODE_EXT`・登録アナライザなし）でも
    同様にサイズ超過は除外される（`code`/`document` の両 doctype を対象にする）。"""
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 50)
    content = "x = 1\n" * 20 + "NEEDLE = 1\n"
    assert len(content.encode("utf-8")) > 50
    _write(tmp_path, "script.py", content)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)
    assert hits == []
    assert truncated == ["script.py"]


def test_grep_search_keeps_light_text_within_max_bytes(monkeypatch, tmp_path):
    """`text_kind.MAX_BYTES` 以内の軽量テキストは従来どおり検索される（除外は超過分だけ）。"""
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 1024)
    content = "small NEEDLE content\n"
    assert len(content.encode("utf-8")) <= 1024
    _write(tmp_path, "doc.csv", content)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)
    assert len(hits) == 1
    assert truncated == []


def test_grep_search_size_exclusion_does_not_apply_to_registered_doc_extensions(monkeypatch, tmp_path):
    """軽量テキスト枠の是正は `.md`/`.txt`（既存の資料種別＝`text_kind` の対象外）には適用しない
    ——`_GREP_FILE_CAP_BYTES` 内なら `text_kind.MAX_BYTES` を超えていても従来どおり検索される
    （基準の二重適用や既存動作の後退が無いことの固定）。"""
    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", 50)   # 軽量テキスト枠なら発火する値
    content = "x" * 100 + "NEEDLE\n"
    assert len(content.encode("utf-8")) > 50
    _write(tmp_path, "doc.txt", content)

    truncated: list = []
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path], truncated_docs=truncated)
    assert len(hits) == 1
    assert truncated == []


def test_grep_search_size_exclusion_single_source_of_truth_via_text_kind_max_bytes(monkeypatch, tmp_path):
    """基準は `text_kind.MAX_BYTES` の1箇所——値を上げれば同じファイルが再び検索対象に戻る
    （台帳側 `corpus_docs._text_oversize` と grep 側の判定が同じ定数を参照している契約の固定）。"""
    content = "x" * 100 + "NEEDLE\n"
    size = len(content.encode("utf-8"))
    _write(tmp_path, "doc.csv", content)

    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", size - 1)   # 超過＝除外
    assert G.grep_search("NEEDLE", world="v1", roots=[tmp_path]) == []

    monkeypatch.setattr("sherpa.ingest.text_kind.MAX_BYTES", size + 1)   # 以内＝再び検索対象
    hits = G.grep_search("NEEDLE", world="v1", roots=[tmp_path])
    assert len(hits) == 1
