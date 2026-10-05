"""`scripts/doc_lint.py`（`make doc-lint`）の単体テスト。"""
from __future__ import annotations

from pathlib import Path

import scripts.doc_lint as doc_lint


def test_slugify_heading_follows_github_style_rules():
    """小文字化・記号（全角括弧・句点）の除去・空白のハイフン化・日本語はそのまま・ アンダースコアは単語構成文字として保持する（GitHub の見出しアンカー規則と同じ）。"""
    assert doc_lint.slugify_heading("範囲（scope）") == "範囲scope"
    assert doc_lint.slugify_heading("6. インターフェース（契約ブロック）") == "6-インターフェース契約ブロック"
    assert doc_lint.slugify_heading("unconfirmed_items") == "unconfirmed_items"
    assert doc_lint.slugify_heading("Codex 経路") == "codex-経路"


def test_extract_heading_anchors_dedupes_with_numeric_suffix():
    """同じ見出しが複数回出た文書は、2回目以降に `-1`・`-2`… を付ける（GitHub と同じ規則）。"""
    lines = ["## 見出し", "本文", "## 見出し", "## 見出し"]
    anchors = doc_lint.extract_heading_anchors(lines)
    assert anchors == {"見出し", "見出し-1", "見出し-2"}


_CONTRACT_BODY = "\n".join(
    [
        "- **方式**: x",
        "- **前提**: x",
        "- **入力**: x",
        "- **出力**: x",
        "- **エラー**: x",
        "- **不変条件**: x",
        "- **検証**: x",
        "- **正本**: x",
    ]
)


def test_broken_samples_are_each_detected(tmp_path: Path, capsys):
    """壊れたサンプル1本に、README が約束する3つの機械検査（アンカー切れ・契約 ID の形式・ 契約 ID の重複）を同時に仕込み、`main()` が非0で終わり、各検査名が出力に現れることを確認する。"""
    broken = tmp_path / "broken.md"
    broken.write_text(
        "\n\n".join(
            [
                "# タイトル",
                "[壊れたアンカー](#存在しない見出し)",
                "## 実在する見出し",
                f"#### 契約: サンプル `C-bad-id`（実装済み）\n\n{_CONTRACT_BODY}",
                f"#### 契約: サンプル2 `C-EXT-DUP-01`（実装済み）\n\n{_CONTRACT_BODY}",
                f"#### 契約: サンプル3 `C-EXT-DUP-01`（実装済み）\n\n{_CONTRACT_BODY}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = doc_lint.main(["doc_lint", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 1
    assert " — anchor — " in out
    assert " — contract-id-format — " in out
    assert " — contract-id-duplicate — " in out


def test_valid_sample_with_matching_anchor_and_ids_passes(tmp_path: Path, capsys):
    """壊れていないサンプル（実在するアンカー・正しい形式で重複しない契約 ID）は緑になることの対照実験。"""
    ok = tmp_path / "ok.md"
    ok.write_text(
        "\n\n".join(
            [
                "# タイトル",
                "[実在するアンカー](#実在する見出し)",
                "## 実在する見出し",
                f"#### 契約: サンプル `C-EXT-SAMPLE-01`（実装済み）\n\n{_CONTRACT_BODY}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rc = doc_lint.main(["doc_lint", str(tmp_path)])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert out.startswith("OK: ")
