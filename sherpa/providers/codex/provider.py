"""`CodexProvider`（Codex を頭脳にする exec 核）。

Codex CLI サブプロセスの起動・思考イベントへの変換・実行ごとの作業領域管理・headline/progress 判定をまとめる。
同時実行は uid 単位で直列化しない（実行ごとに専用の作業領域 `authoring/run-<乱数>` を割り当てる）。
`_run_authoring` は 1 ターンの段（準備・実行・後始末・登録・組み立て）の呼び出し順と、外側の try/finally（SSE 生成器の唯一のクリーンアップ保証）だけを持つ。段は `turn_prepare.py`・`codex_cli.py`・`codex_attempt.py`・`turn_candidates.py`・`turn_loop.py`・`turn_finish.py` の関数。
`_gather` は `_run_authoring` 内でのみ `from sherpa import agents as _facade` で遅延 import し `_facade._gather(ctx)` と実行時解決する（差し替えを効かせる・循環 import 回避）。
設計: docs/design/codex.md「1ターンの流れ」
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterator

from ... import depth_profile as depth_profile_mod
from ... import model_catalog
from ..base import Ctx, Provider, _log, _node, _plain_run
from ..prompts import _kb_hint_abs
from .ledger_gate import _retire_investigation_ledger
from .sandbox import (
    _release_active_run_dir,
    _remove_dir_best_effort,
    _safe_run_authoring,
    _safe_workspace_authoring,
    codex_mode,
)
from .turn_finish import assemble_result, close_session, register_created_files
from .turn_loop import run_session
from .turn_prepare import enter_conversation, prepare_run
from .turn_state import CodexTurnState

# MCP 付き Codex 経路で決まった手順の下調べ（`_gather` の `ctx.dispatch`）を省くレンズ。impact は省かない（グラフでたどる影響一覧は Codex のツールでは作れず、回答と並べて表示するため）。
_PRESEARCH_SKIP_LENSES = frozenset({"qa", "troubleshoot", "author"})
# モデルの文脈窓（`model_context_window`）は Codex CLI に渡さず、CLI 自身の判断に任せる（Sherpa 側で窓を判定・登録・上書きしない）。


# 原本と変換済みテキストの保護。読み取り専用はサンドボックス（permission profile の read）が強制し、指示は多層防御として全モード・全レンズに常置する。
_READ_ONLY_SENTENCE = (
    "原本と変換済みテキストは読むだけで、書き換え・上書き・移動・削除・名前の変更を絶対にしない。"
    "Word・Excel などの原本を自分で変換しない（変換済みテキストを読む）。"
)
# 設計書と実装の両面で確かめる（全モード共通）。AGENTS.md にも同じ規律があるが、書出し失敗でも消えないようプロンプトにも常置する。
_BOTH_SIDES_SENTENCE = (
    "設計書と実装（ソース）の両方で確かめ、それぞれの根拠（ファイル:行）を示して答える。"
    "食い違えば両方を並べて『食い違い』と書く（実装を正とする）。見たソースが画面・バッチ・SQL の"
    "どれかを明記し、一部のソースだけで全体を判断しない（確かめられなかった点はそう書く）。"
)
# 作成の依頼（lens=author）以外のターンはファイルを作らせない。作っても成果物に登録せず、作業フォルダごと消す（`_run_authoring`）。
_NO_FILES_SENTENCE = (
    "この依頼ではファイルを作らない（作業用のファイルは `.tmp/` の下に作る・終われば消える）。"
    "ファイルでほしいと頼まれたら、内容は本文に書き、『資料を作成』を選んで依頼し直すよう案内する。"
)


class CodexProvider(Provider):
    """Codex を頭脳にするエージェント中核。
    取得（Neo4j/grep）は本物のツールで実行しつつ、Codex 自身も原文を grep/参照で裏取りする。Codex の実コマンド実行・推論・回答を `--json` から拾い、1つずつ思考ノードに流す。失敗/未導入は決定的回答にフォールバックする。
    既定 reasoning は `depth_profile.CODEX_REASONING_DEFAULT`。推論レベルは調べる深さでは変えず、管理画面の基準値で固定する（`depth_profile.codex_reasoning_for`）。
    設計: docs/design/codex.md「頭脳の選択」
    """
    label, model = "Codex", "gpt-5.5"
    provider_id = "codex"

    def __init__(self, reasoning: str | None = None, model: str | None = None,
                web_search: bool | None = None, ollama_base_url: str | None = None,
                openai_api_key: str | None = None, system_settings: dict | None = None):
        self._reason = reasoning or depth_profile_mod.CODEX_REASONING_DEFAULT
        # チャットの Codex モデルは選択可。argv `-m` に渡すため、先頭ハイフン/空白/制御文字/過大長は `model_catalog.CODEX_MODEL_NAME_RE` で弾く。
        # 未指定（None/空文字）だけを既定 "gpt-5.5" へ解決する。不正な非空値は黙って別モデルへ置換せず `InvalidModelNameError`（`ValueError` のサブクラス）を送出する（`_select_provider` がこの型だけ捕捉して `_UnwiredProvider` にする）。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            raise model_catalog.InvalidModelNameError(f"不正な Codex モデル名です: {model!r}")
        self.model = model or "gpt-5.5"
        # ユーザーの希望（設定 codex_web_search）。実際に効くかは管理者フラグ次第（`_web_search_disabled_value` が admin 許可と AND する）。
        self._web_search = bool(web_search)
        # Codex(Ollama) 構成のとき、Codex CLI を向ける Ollama の接続先。None＝Codex(OpenAI)。`_select_provider` が SSRF ガード（`llm.assert_ollama_url_allowed`）を通してから渡す。
        self._ollama_base_url = ollama_base_url or None
        # Codex(OpenAI) 構成で接続先が既定以外（Azure 等）のときだけ `_select_provider` が渡す（それ以外は None）。カスタム model_provider は子プロセスの env からキーを読むため、この構成のときだけ `_codex_clean_env` にこの値を渡す。
        self._openai_api_key = openai_api_key or None
        # `_select_provider` が key/model 解決に使ったのと同じ system_settings スナップショット。config.toml 生成・web_search 注記へも渡す。省略時（`None`）は `llm.py` が都度読み直す。
        self._system_settings = system_settings
        # 既定は空（`run()` を経由せず `_prompt_mcp` を直接呼ぶ場合用）。`run()` 冒頭で `ctx.history` から設定し直される。
        self._history: list = []

    def _history_block(self) -> str:
        """直前ターンの履歴を Codex プロンプトへ前置するテキスト（会話継続）。`self._history` が空なら空文字列。"""
        if not self._history:
            return ""
        lines = [f"{'ユーザー' if h.get('role') == 'user' else 'アシスタント'}: {h.get('content', '')}"
                for h in self._history]
        return "【直前の会話（参考・新しいものが下）】\n" + "\n".join(lines) + "\n\n"

    def _prompt_mcp(self, message, lens, world, direct_read: bool = True, layer=None):
        """MCP 版プロンプト。事実を前渡しせず、Codex に MCP ツールで自律調査させる。MCP ツール固有の使い分けと、containment/grounding の短縮形を置く（共通ルールは AGENTS.md）。
        `direct_read`（既定 True）: 原本直読（permission profile で KB／派生ルートを read し、コードインタープリターで直接開く）の可否。`_run_authoring` が秘匿列挙と範囲（`_scope_deny_entries`）の成否から計算して渡し、失敗した（fail-closed）ターンだけ False（MCP のみへ縮退）。
        """
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        _read_block = (
            "**原本は直接読んでよい（読取専用・指定された資料フォルダと派生フォルダの中だけ・"
            "秘匿名のファイル（.env／鍵／credentials 等）は読まない）。"
            f"{_READ_ONLY_SENTENCE}"
            "旧形式など読取ツールで開けない原本は read_doc で変換済みテキストを読む。"
            # 主従は決めない。まず読取ツールで原本を読み、突合・集計など定型外だけ Python を使う。
            "まず読取ツール（xlsx_sheets／xlsx_range／docx_paragraphs／pptx_slides／"
            "pdf_pages／file_head）で原本を読む。複数ファイルの突合・集計など定型外の作業"
            "だけ Python（openpyxl・python-docx・python-pptx・pdfplumber。集計は pandas）"
            "で開く。テキスト・コードはそのまま読んでよい。"
            "派生 MD／rag.md は補助。台帳・検索・グラフ・出典の確定は MCP ツールで行う。"
            # 直読した資料は MCP の結果に載らないため、回答末尾に固定書式の行を書かせ、Sherpa（`citations.parse_referenced_doc_lines`）が台帳で実在確認したものだけ出典へ昇格する。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。**"
            # 質問の型に合う調査スキルへ誘導する（直読不許可のターン（direct_read=False）では入れない）。
            "**質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う"
            " `.agents/skills` の investigate-* スキルを読んで、その手順（ツールで当たり→"
            "原本の中身を確かめる→答える）どおりに進める。**"
            if direct_read else
            "**今回は原本の直接読み取りは使えない。資料の本文は MCP のツールで読む（KB 外は読まない）。**"
            f"{_READ_ONLY_SENTENCE}"
        )
        # 層（探す対象）は Codex に強制しない（直読は層に関係なく read）。限定されたターンだけ案内する。
        _layer_block = {
            "docs": "探す対象として資料（設計書・仕様書などのドキュメント）が指定されている＝直読でも資料を優先して見る。",
            "code": "探す対象としてソース（プログラム・JCL・コピーブック）が指定されている＝直読でもソースを優先して見る。",
        }.get(layer, "")
        base = (
            "あなたは社内ナレッジ調査エージェントです。MCP サーバ『sherpa』のツール"
            "（list_docs＝文書台帳の一覧/件数／ripgrep_search＝全文grep／glob_search＝ファイル名パターン／"
            "doc_outline＝見出し構造／read_doc＝通読（続きは start_line）／read_around＝周辺精読／"
            "graph_neighbors＝関係グラフの関連部品／graph_resolve＝起点の候補／graph_impact＝識別子からの影響先／es_search＝日本語全文検索／"
            "xlsx_sheets＝Excelのシート一覧／xlsx_range＝Excelのセル範囲／"
            "docx_paragraphs＝Wordの段落・表／pptx_slides＝PowerPointのスライド／"
            "pdf_pages＝PDFのページ／file_head＝テキスト・コードの先頭）を使って、"
            f"資料（{_kb_hint_abs(world)}）と関係グラフを**自分で調べてください**。"
            f"{_read_block}"
            "まずツール（台帳・全文検索・グラフ）で当たりを付けてから、原本の中身を確かめて答える。"
            f"{_BOTH_SIDES_SENTENCE}"
            f"{_layer_block}"
            "確定した事実と推定は分けて書く（詳細ルールは AGENTS.md）。"
            "検索ヒットや精読結果に text_truncated が付いていたら、その本文は途中で切れている。"
            "結論を出す前に read_around か read_doc で続きを読む。続きを取得する手段が無い打ち切り"
            "（file_truncated・pdf_pages の text_truncated・compare_documents／graph_neighbors／glob_search／doc_outline の truncated・graph_resolve／graph_impact の truncated と coverage.complete:false（理由は coverage.limits）・folder_tree の folders_truncated・xlsx_sheets／ripgrep_search／es_search の truncated＝ヒット数上限）は"
            "その範囲を未確認として明示し、全件性を主張しない。"
            "**ドキュメント数・一覧・どんな資料があるか・フォルダ構成といった台帳質問は、まず list_docs を使う**"
            "（grep は本文中の一致しか探せず件数/一覧には答えられない）。フォルダ名・ファイル名はパスに含まれる"
            "ので、名前の部分一致は list_docs の name_pattern で当てる（grep で本文からは探さない）。"
            "表記が揺れそうな語は短い部分語で試す（例:「4期更改」がヒットしなければ「4期」）。"
            "**件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを"
            "回答に明示する**（曖昧なら『4期更改』と『4期保守』のように候補フォルダ別の内訳で答える）。"
            "**一覧を求められたら該当する全件を各項目のパス付きで列挙する（省略しない・件数と一致させる）。**"
            "全件・一覧の完了は対象範囲の確認を終えてからで、検索3回や件数だけの取得では完了とせず、"
            "中断（利用者停止・通信エラー・予算到達）のときは確認済み／未確認／理由を分けて書き、"
            "部分結果を「全件」と断定しない。"
            "原因の手がかりや関連部品（呼び出し/コピー/参照/関連文書）をたどるときは graph_neighbors、"
            "変更の影響先を調べるときは graph_resolve で起点を選んでから graph_impact を使う。"
            # 影響を問う質問の分解の型。症状語で検索を乱発させず、変更対象と影響先の「接続（経路）」の有無を根拠に答えさせる。
            "**影響を問う質問（「〜を変えたら」「〜に影響ある？」「〜が落ちる？」など）では、"
            "①変更対象（例: 税率）に依存する部品・記述を特定 → ②影響先（例: 夜間バッチ＝JCL/ジョブ）を特定 → "
            "③両者の接続（COPIES／INVOKES／ACCESSES／CONTAINS＝構造的な依存の経路）を、graph_resolve で起点の"
            "候補を選び（同名は path で見分ける）その canonical_id を graph_impact へ渡して当たる（補助に graph_neighbors）。"
            "graph_impact の route と graph_neighbors の近傍は辺の種類と向き（from→to）を返す——COPIES／INVOKES／"
            "ACCESSES／CONTAINS だけで構成された経路は根拠にしてよい。影響は矢印をさかのぼる（A →COPIES→ B は"
            "B を変えると A が影響を受ける・変更対象から出ていく矢印の先は影響先ではない）。経路に DOCUMENTS"
            "（言及）・CORRESPONDS_TO の辺や unverified の辺（裏付け原本が実在確認できない）が 1 本でも含まれる"
            "近傍は候補どまり＝原本で確認する。経路の先の"
            "実際の記述を引用したいときだけ原本を開き、接続の有無を根拠として答える（向きは平易語で・"
            "内部のエッジ名は本文に出さない）。"
            "質問中の症状表現（落ちる/止まる/エラー/停止 等）をそのまま検索語にしない**"
            "（原因調査＝トラブルシュートだと明示された時のみ症状語で探してよい）。"
            # ask_user の使用条件（agentic と同じ制約）＋乱用ガード（確認ID 付きは再質問しない・1回まで）。lens 別の例を示し、ユーザー主導の確認要求も発動手段にする。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず、ここまでに確認できたことをまとめて終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
        )
        if lens == "author":
            # author は MCP ツールで根拠を集めたうえで成果物ファイルを authoring 直下に作る。仕様が曖昧な場面が多いため、着手前の確認を促す。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                # スライド/プレゼンは既定 Marp、後で PowerPoint 編集するなら python-pptx。Codex は marp の .md を書くだけでよい（レンダは Sherpa が完了後に自動実行する）。
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                # 履歴があれば【依頼】の前に前置する。
                f"{self._history_block()}【依頼】{message}")
        # 履歴があれば【質問】の前に前置する。
        return sysp + base + " " + _NO_FILES_SENTENCE + f"\n\n{self._history_block()}【質問】{message}"

    def _prompt_plain(self, message, lens, world):
        """素の Codex（`plain`）向けプロンプト。Sherpa の調べ方の上乗せ（MCP ツール一覧・list_docs 誘導・investigate スキル誘導・台帳・影響調査の手順・原因調査の症状語の指示）は持たず、Codex 本来の調べ方（シェルで直接読む）に任せる。
        containment（範囲・秘匿は読まない）・出典書式・ask_user の使い方は AGENTS.md（`codex_agents_md.AGENTS_MD_PLAIN`）と重複しても多層防御として置く。
        """
        sysp = (self.system_prompt + "\n\n") if self.system_prompt else ""
        from ... import worlds
        base = (
            "あなたは社内ナレッジ調査エージェントです。シェル（rg・sed・cat など）で次の資料"
            "フォルダを直接読んで調べてください。\n"
            f"- 原本: {_kb_hint_abs(world, layout_hint=False)}\n"
            "- 変換済みテキスト（Word・Excel・PowerPoint・PDF・画像。原本の代わりにこちらを読む。どちらも"
            "原本と同じフォルダ構成・UTF-8）: "
            f"{worlds.derived_rag_dir(world)}（「<元のファイル名>.rag.md」・正本）と "
            f"{worlds.derived_md_dir(world)}（「<元のファイル名>.md」）\n"
            "- ソース（.c・.bas・COBOL・JCL など）は Shift_JIS（CP932）のことが多い。日本語の語で"
            "探すときは CP932 と確認したソースに `rg -E sjis` を使う（英数字の名前はそのままで当たる）。"
            "rg が無ければ `grep -rn` を使い、探す語を `iconv -t CP932` で変換してから探す。"
            "cat／sed で中身を読むときも CP932 のファイルだけ `iconv -f CP932 -t UTF-8` を通す"
            "（UTF-8 のファイルには使わない）。\n"
            "**指定された資料フォルダ・変換済みテキスト以外（このディレクトリの外・ユーザー"
            "workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。**"
            f"{_BOTH_SIDES_SENTENCE}"
            f"{_READ_ONLY_SENTENCE}"
            "作業用のファイルは"
            "このディレクトリ直下に作らず `.tmp/` の下に作る。"
            "MCP サーバ『sherpa』の graph_neighbors（呼び出し／コピー／参照のつながり）の"
            "ツールも使ってよいが、必須ではない。"
            "利用者向けに整理して日本語で答えてください。確定した事実と推定は分けて書く。"
            # 出典の書き方は `_prompt_mcp` の `_read_block` と同じ文言。
            "回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、"
            "資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。"
            "Sherpaがこれを出典（原本ダウンロード）に変換する。派生MD／rag.mdを見た場合も原本のパスで書く。"
            # ask_user の使い方・確認ID の再質問禁止は `_prompt_mcp` と同じ文言。
            "調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する"
            "（例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
            "対象の絞り込みを ask_user で確認してよい）。"
            "**依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、"
            "調査より先に必ず ask_user で要件を確認してから進める**"
            "（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が残っていて「確認ID:」が"
            "無いときだけ自分で ask_user する）。"
            "（質問は1実行につき1回まで・質問後は追加調査をせず、ここまでに確認できたことをまとめて終了する）。"
            "**ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので、上の指示より再質問禁止を優先し、"
            "ask_user は使わずその回答に従って進める**（同じことを再度聞かない＝再質問ループ防止）。"
            "**途中経過だけの応答（「次に〜を調べます」など）で終えない。調査を最後まで進めてから、"
            "結論と根拠を最終回答として書く。**"
        )
        if lens == "author":
            # author 向けの成果物の作り方（marp/pptx の使い分け）は `_prompt_mcp` と同じ文言。
            return sysp + base + (
                " 調べた内容を根拠に、**成果物ファイルをこのディレクトリ（authoring 直下）に作成してください**。"
                "**仕様（列構成・粒度・対象範囲など）が曖昧で結果が大きく変わる場合は、着手前に ask_user で確認する**。"
                "Excel/Word/PowerPoint 等を作る場合は `.agents/skills` 配下のスキル（xlsx/docx/pptx の"
                " SKILL.md）を確認して活用する。"
                "**スライド・プレゼン資料は見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う**。"
                "marp スキルでは Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は"
                "この作業の完了後に Sherpa 側が自動で行う（自分でレンダコマンドを実行する必要は無い）。"
                "「あとで PowerPoint で編集したい」と明示された場合だけ、"
                "marp を使わず pptx スキル（python-pptx）で作る。"
                "最後に**作成したファイル名**と**内容の要約**を"
                "日本語で報告してください。\n\n"
                f"{self._history_block()}【依頼】{message}")
        return sysp + base + " " + _NO_FILES_SENTENCE + f"\n\n{self._history_block()}【質問】{message}"

    def _plain_text(self, message: str = "") -> str:
        # ナレッジ参照オフでは Codex CLI を起動しない（read-only でも KB を覗けてしまうため）。通常この経路には来ない（`routers/chat.py::_knowledge_for` が資料参照 ON を強制する）。内部経路や古いクライアントが knowledge=False で呼んだ場合の安全網。
        return ("Codex は常に社内資料を参照して回答します。"
                "資料を参照しない雑談は OpenAI／ローカルLLM を選んでください。")

    def run(self, ctx: Ctx) -> Iterator[dict]:
        # 分岐前に確定させる（`_prompt_mcp` が `_run_authoring` から参照する）。
        self._history = list(ctx.history or [])
        if not ctx.knowledge:  # ナレッジ参照オフ＝素の会話（Codex を grep なしで・authoring 不使用）
            yield from _plain_run(self, ctx); return
        yield from self._run_authoring(ctx)

    def _run_authoring(self, ctx: Ctx) -> Iterator[dict]:
        decision = env = None
        _turn_t0 = time.monotonic()  # `sherpa.usage` ログ 1 行の elapsed（このターン全体）
        # 素の Codex モード（`codex_mode`）はターンの最初に1回だけ決め、プロンプト・AGENTS.md・スキル配備・出力スキーマ・multi_agent・台帳・MCP env・codex.log 開始行・activity.settings で同じ値を使う。`standard`（既定）はこのフラグが常に偽。
        _plain = codex_mode(self._system_settings) == "plain"
        # 下調べ（`_gather` の `ctx.dispatch`）を省くのは、Codex を起動する見込み（CLI が有る）で、レンズが `_PRESEARCH_SKIP_LENSES` のときだけ。
        # `ws_authoring`/`run_dir`/`_codex_home_ok` はこの時点で計算できないため含めない。判定は `_gather` の前に1回だけ行い、後段（起動ガード・未応答時のノード文言）でも同じ値を使う。
        _codex_bin = shutil.which("codex")
        _skip_lenses = _PRESEARCH_SKIP_LENSES if _codex_bin else frozenset()
        # `_gather` は `from sherpa import agents as _facade` を関数内で遅延 import し、facade 属性経由で実行時解決する（差し替えを効かせる・循環 import 回避）。
        from sherpa import agents as _facade
        for ev in _facade._gather(ctx, skip_presearch_lenses=_skip_lenses):
            if isinstance(ev, dict) and ev.get("type") == "_env":
                decision, env = ev["decision"], ev["env"]
            else:
                yield ev
        if env is None:  # _gather が clarify question を出して停止＝確認待ち
            return
        _skip_presearch = decision["lens"] in _skip_lenses

        yield _node("codex", "think", "Codex が調べる", "資料を調べています", "active")
        st = CodexTurnState(ctx, turn_t0=_turn_t0, plain=_plain, skip_presearch=_skip_presearch)
        codex_created_files = st.codex_created_files
        # 専用 authoring ディレクトリを cwd にする（個人アップロード files/ から分離・KB は絶対パスでプロンプトに渡す）。symlink が混入していると封じ込めが崩れるため `_safe_workspace_authoring` で symlink 拒否＋fail-closed。
        users_dir = Path(os.environ.get("SHERPA_USERS_DIR", "data/users")).resolve()
        uid = ctx.uid or "admin"
        ws_authoring = _safe_workspace_authoring(users_dir, uid)  # None＝fail-closed（Codex 起動しない）
        # 実行ごとの専用作業領域（cwd/書込 root）。None＝run dir が作れない＝fail-closed（Codex を起動しない）。
        run_dir = _safe_run_authoring(users_dir, uid)
        # 以降の段が読む作業領域のパス（`_run_authoring` の局所名と同じ値）。
        st.users_dir, st.uid, st.run_dir = users_dir, uid, run_dir
        # 台帳登録（files/ move）まで完了した後で必ず削除する（正常終了・停止・例外のいずれでも。クライアント切断の GeneratorExit でも finally は実行される）。会話ロックの解放もこの finally で行い、成果物の move／台帳登録・最終回答（`_result`）の送出までロックを保持する。
        try:
            stop = yield from enter_conversation(self, ctx, st, decision)
            if stop:
                return
            if _codex_bin and ws_authoring is not None and run_dir is not None and st._codex_home_ok:
                stop = yield from prepare_run(self, ctx, st, decision)
                if stop:
                    return
                yield from run_session(self, ctx, st, decision, env)
                stop = yield from close_session(self, ctx, st, decision, env)
                if stop:
                    return
            # 台帳登録（authoring/ の成果物を files/ に移動して台帳登録）。files/ に移すことで既存の grep/delete/TTL 機構をそのまま使う。authoring/ に中間生成物が残らないため、次回 Codex 実行時も個人ファイルは見えない。
            if codex_created_files:
                register_created_files(st)
            yield from assemble_result(self, ctx, st, decision, env)
        finally:
            # 台帳の退避・削除が終わるまで会話ロックを保持する（先に解放すると、同じ会話の「続き」ターンが未作成またはコピー途中の退避先を復元処理で参照しうる）。内側 `try/finally` で、退避／run_dir 削除の途中で例外が起きても最後に必ずロックを解放する。
            try:
                if run_dir is not None:
                    # 調査台帳の退避: run_dir 削除より前に、完了せずターンが終わった台帳だけ永続領域へコピーする。`_investigation_verdict`（本体ループが正常に完走したときだけ埋まる）には頼らない（yield で generator が close された場合に未完了台帳が失われるため）。通常終了時は env 構築の直前で退避済み（`_investigation_retire_done`）で、ここは切断・例外で先に到達しなかった場合だけのフォールバック（二重には実行しない）。
                    if (st._investigation_dir is not None and st._ledger_home is not None
                            and not st._investigation_retire_done):
                        _retire_investigation_ledger(
                            st._investigation_dir, st._ledger_home, required_extra=st._ledger_required_extra,
                            require_review=st._ledger_require_review)
                    _release_active_run_dir(run_dir)
                    if st._created_files_failed:
                        # 保存に失敗した成果物がある run_dir は削除せず回収用に残す（`_cleanup_stale_run_dirs` の24時間しきい値で最終的に掃除される）。
                        _log.warning(
                            "codex run dir kept for recovery due to created-file save failure: %s",
                            run_dir.name)
                    else:
                        _remove_dir_best_effort(run_dir)
            finally:
                if st._conv_lock_acquired:
                    st._conv_lock.release()
