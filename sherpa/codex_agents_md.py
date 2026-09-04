"""Codex authoring 実行前に authoring ディレクトリへ書き出す AGENTS.md（Codex 強化計画 Phase0・§2）。

`agents.py` の `CodexProvider._prompt`/`_prompt_mcp` に埋め込んでいた**共通ルール**（KB 以外を読まない・
根拠ベースで推測しない・成果物は authoring 直下・出典列挙不要 等）をここへ切り出し、プロンプト側は
質問固有の部分だけに痩せさせる（docs/proposals/2026-07-02-Codex強化計画.md §5-1 決定）。

Codex CLI は cwd 直下（＝ authoring/）の AGENTS.md を実行時に自動的に読み込む（公式仕様）。per-request で
毎回上書きするため内容は決定的固定文字列（冪等）。実行後も authoring に残ってよい
（ユーザー本人が見えるだけの無害なファイル・機密は含まない）。
"""
from __future__ import annotations

import os
from pathlib import Path

AGENTS_MD = """\
# Sherpa 共通ルール（Codex 実行時）

- 資料の参照は、指定された経路（grep・ファイル参照、または MCP ツール）に限る。指定された資料フォルダ
  （KB）以外（このディレクトリの外・ユーザー workspace 等）は絶対に読まない。
- 回答は根拠ベースで作る。ツール・ファイル参照で最低1回は裏取りしてから答え、事実に無いことは書かない
  （推測しない）。
- 成果物（生成ファイル）を作る場合は、必ずこのディレクトリ（authoring 直下）に作成する。
- スライド・プレゼン資料は、見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う。marp スキルでは
  Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は完了後に Sherpa 側が自動で行う
  （自分でレンダコマンドを実行する必要は無い）。ただし「あとで PowerPoint で編集したい」と明示された
  場合だけ、marp ではなく pptx スキル（python-pptx）を使う（marp の PPTX は画像ベースで本文編集ができない）。
- 最後は日本語で簡潔に回答する。出典の列挙は不要（Sherpa 側で別途付与する）。
- 回答は Markdown（太字・箇条書き・インラインコード）で書いてよい。
- 件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを
  回答に明示する（曖昧なら候補フォルダ別の内訳で答える）。
- 調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する
  （例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけのときは、対象の絞り込みを
  確認してよい。質問は1実行につき1回まで。質問したら追加調査はせず、現状を簡潔に要約して終了する）。
- 依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、調査より先に
  必ず ask_user で要件を確認してから進める（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が
  残っていて「確認ID:」が無いときだけ自分で ask_user する）。ただし依頼に「確認ID:」が含まれる場合は
  前の質問への回答なので、この指示より再質問禁止を優先し、ask_user は使わずその回答に従って進める
  （同じことを再度聞かない）。
"""


def write_agents_md(authoring: Path) -> None:
    """authoring 直下へ AGENTS.md を書く（per-request・冪等・上書き）。

    呼び出し側で try/except すること（AGENTS.md はあくまで補助・書込に失敗しても Codex 実行自体は
    継続してよい＝fail-open。プロンプト側には containment/grounding の短縮形を常置してあるので、
    失敗時もプロンプトの質問固有部分＋短縮ルールだけで動くことを前提にする）。

    RV MEDIUM（2026-07-03）: 単純な `Path.write_text()` は既存の `AGENTS.md` が symlink だった場合に
    その**指す先へ**書き込んでしまう（authoring 配下の想定外の場所を書き換え得る）。
    一時ファイルを `O_CREAT|O_EXCL|O_NOFOLLOW` で新規作成し、`os.replace()` で置換する
    （`rename`/`replace` はディレクトリエントリの張替えでシンボリックリンクを一切追従しない＝
    既存 AGENTS.md が symlink でも安全に「通常ファイルの AGENTS.md」へ置き換わる）。
    """
    target = authoring / "AGENTS.md"
    tmp = authoring / f".AGENTS.md.tmp-{os.urandom(6).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, 0o644)
    try:
        os.write(fd, AGENTS_MD.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.replace(str(tmp), str(target))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)   # 置換に失敗したら一時ファイルを残さない（台帳スキャン汚染防止）
        except Exception:
            pass
        raise
