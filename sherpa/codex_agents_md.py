"""Codex authoring 実行前に authoring ディレクトリへ書き出す AGENTS.md（docs/proposals/2026-07-02-Codex強化計画.md §2）。

`agents.py` の `CodexProvider._prompt`/`_prompt_mcp` に埋め込んでいた**共通ルール**（KB 以外を読まない・
根拠ベースで答える・成果物は authoring 直下 等）をここへ切り出し、プロンプト側は
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

- 原本は直接読んでよい（読取専用）。指定された資料フォルダ（KB）・派生フォルダ以外（このディレクトリの外・
  ユーザー workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。
  まず読取ツール（xlsx_sheets／xlsx_range／docx_paragraphs／pptx_slides／pdf_pages／file_head）で
  原本を読む。複数ファイルの突合・集計など定型外の作業だけ Python（openpyxl・python-docx・
  python-pptx・pdfplumber。集計は pandas）で開く。
  台帳・検索・グラフ・出典の確定は MCP ツールで行う。
- 回答は根拠ベースで作る。ツール・ファイル参照で最低 1 回は裏取りしてから答える。根拠は資料のパス
  （Python で開いた場合はシート名／セル範囲・段落・ページ番号も添える）で示す。資料に無いことを補うときは
  『推定』と明示する。
- 見直し（評価）は根拠の**件数**では判定しない。質問の型ごとに必要な**根拠種別**が揃っているかで
  判定する。種別はソース（`src/` のコード）／設計書（Office・PDF 由来の資料）／定義（DDL・copybook・
  設定ファイル）／ログ・設定（会話に貼られた・添付された資料）／呼出関係（グラフ、使えなければ grep
  の呼出し検索で代替）。レンズ別の必須集合: 仕様問い合わせ＝ソース＋設計書／影響調査＝ソース＋
  呼出関係（＋定義）／トラブルシュート＝ソース＋ログ・設定／作成系＝ソース＋設計書。登録範囲にその
  種別が存在しない場合は『該当なし』として不足にせず、回答にその旨を明示する。設計書とソースが
  食い違う場合はソースを正とし、食い違いを回答で報告する。
- 成果物（生成ファイル）を作る場合は、必ずこのディレクトリ（authoring 直下）に作成する。
- スライド・プレゼン資料は、見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う。marp スキルでは
  Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は完了後に Sherpa 側が自動で行う
  （自分でレンダコマンドを実行する必要は無い）。ただし「あとで PowerPoint で編集したい」と明示された
  場合だけ、marp ではなく pptx スキル（python-pptx）を使う（marp の PPTX は画像ベースで本文編集ができない）。
- 調査の途中経過（「次に〜を調べます」等の作業宣言）だけで終えない。調査を最後まで進めてから、
  結論と根拠を最終回答として書く。
- 「全件」「一覧」「すべて」「網羅」の依頼は、検索3回・根拠1件・件数だけの取得・代表例の発見では
  完了としない。対象範囲（ファイル一覧なら台帳・本文中の項目一覧なら対象資料のシート/段落/ページ総数）
  の確認を終え、該当項目が回答にそろってから完了とする。`truncated`／`text_truncated`／`file_truncated`／
  `total > start+count` は続きを取得する。続きを取得する手段が無い打ち切り（`file_truncated`・
  pdf_pages の `text_truncated`・compare_documents／graph_neighbors／glob_search／doc_outline の `truncated`・folder_tree の `folders_truncated`・xlsx_sheets／ripgrep_search／es_search の `truncated`＝ヒット数上限）は、その範囲を未確認として明示し全件性を主張しない。
  利用者停止・通信エラー・既存の反復／情報量予算への到達で
  中断するときは、確認済みの結果・未確認の範囲・理由を分けて書き、部分結果を「全件」「すべて」
  「該当なし」と断定しない。
- 最後は日本語で答える。長さを絞らない＝集めた情報は削らず、一覧を求められたら該当する全件を各項目の
  パス付きで列挙する（『など』で省略しない・件数と一致させる・要約や代表例化で項目を落とさない）。
  list_docs は count（全件数）と docs（rel_path 昇順・limit 件）を返す＝truncated:true なら next_offset
  から続きを取り（path_prefix や name_pattern での分割は補助）、取得した総数を count と照合する。
  表を指定されたら指定列を守り、項目と行・値の対応を保つ。取得できなかった値は推測で埋めず
  「未取得」と書く。確定した事実と推定は分けて書き、推定には『推定』と明示する。
  本文の中に出典の一覧は書かない（出典は Sherpa が末尾の『参照した資料:』から付与する。一覧の依頼に
  対する各項目のパス列挙は答えそのものであり、ここで言う出典の一覧ではない）。根拠の箇所（パス・
  シート名・セル範囲・段落・ページ）は本文で示してよい。回答の最後に『参照した資料:』の行を置き、
  実際に開いて根拠にした資料を1行1件、資料フォルダからの相対パス（例 `4期更改/02_設計/xxx.xlsx`）で
  列挙する。派生MD／rag.mdを見た場合も原本のパスで書く。
- 回答は Markdown（太字・箇条書き・インラインコード）で書いてよい。
- 件数を答えるときは list_docs の path_prefix でフォルダを確定してから数え、どのフォルダを数えたかを
  回答に明示する（曖昧なら候補フォルダ別の内訳で答える）。
- 調査範囲・目的・選択肢が曖昧で、確認しないと結果が大きく変わる場合だけ ask_user でユーザに確認する
  （例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけのときは、対象の絞り込みを
  確認してよい。質問は1実行につき1回まで。質問したら追加調査はせず、ここまでに確認できたことをまとめて終了する）。
- 依頼文に「確認してから進めて」（同義: 確認してから／聞いてから進めて）が含まれる場合は、調査より先に
  必ず ask_user で要件を確認してから進める（通常はシステムが先に確認カードを出すので、届いた依頼にこの句が
  残っていて「確認ID:」が無いときだけ自分で ask_user する）。ただし依頼に「確認ID:」が含まれる場合は
  前の質問への回答なので、この指示より再質問禁止を優先し、ask_user は使わずその回答に従って進める
  （同じことを再度聞かない）。
"""

# 原本直読が許可されたターンだけ足す段落（調査スキルは原本を Python で開く前提＝直読不許可のターンでは
# 達成不能な手順書へ誘導しない）。
_INVESTIGATE_SKILLS_PARAGRAPH = """\
- 質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う `.agents/skills` の
  investigate-* スキルを読んで、その手順（ツールで当たり→原本の中身を確かめる→答える）どおりに進める。
"""

# 本体自身のソース確認要件（実装ベース探索の回復 §0・S1・裁定④＝毎主張ごとに強制・深さに関わらず
# 常時）。`direct_read`（`_direct_read_ok`・provider.py が秘匿列挙／範囲の解決に失敗したときは偽）
# で文言を切り替える——直読不可ターンで「直接読む」と指示すると、同じターンの他の指示
# （`_prompt`/`_prompt_mcp` の「今回は原本の直接読み取りは使えない」）と矛盾し、Codex に実行不能な
# 手順を指示することになる。要件そのもの（本体自身の確認・グラフ不調でも止めない）は両分岐で同じ。
#
# `layer`（探す対象＝`docs`／`code`／`both`／`None`）も見る: 直読不可（MCP 読取のみ）かつ
# `layer == "docs"` の組合せでは、MCP の `ripgrep_search`/`read_around` 自体が層制限でソース
# （code 種別）を拒否する（`agentic_search.py::in_layer_code`）——この組合せで「必ずソースを読む」を
# 要求すると実行不能な指示になる。提案書 §3 S1「必須種別は許可範囲内で解釈」・裁定⑥（部分回答＋
# 「ソース未確認のため確定不可」）どおり、この組合せだけは読取要求ではなく確定不可の告知を指示する。
# direct_read=True のときは層に関係なく直読でソース確認する（`sandbox.py:633-638` の裁定＝
# Codex は層の指定を強制しない・直読は層に関係なく許可）。
def _source_verification_paragraph(direct_read: bool, layer: str | None = None) -> str:
    if not direct_read and layer == "docs":
        return """\
- 今回の探す対象は資料のみ（ソースは対象外）のため、実装に関する主張のソース裏取りはしない
  （MCP の読取ツールで `src/` のコードを読もうとしても層制限で拒否される）。実装に関する主張は
  確定にせず、回答冒頭で『ソースを確認していないため確定できません』と明示したうえで、資料から
  分かる範囲の部分回答にする。
- グラフ検索・全文検索（ES）が空・不調・未構築のときは、それを理由に回答を止めない。資料（MCP の
  読取ツール）で確認できる範囲で答える。
"""
    if direct_read:
        how = "`src/` の原本を直接開いて"
        graph_fallback = "必ず `src/`・原本を ripgrep／読取ツールで直接読んで確認する。"
    else:
        how = "MCP の読取ツール（ripgrep_search／read_around 等）で `src/` のソースを"
        graph_fallback = "必ず MCP の読取ツール（ripgrep_search／read_around 等）で `src/` を確認する。"
    return f"""\
- 実装に関する主張は、自分（本体）が{how}確認してから採る。worker（サブエージェント）を使う場合、
  worker の一次判断はソースの根拠（ファイル:行）が付いているものだけを採り、確認したファイル:行は
  最終回答の出典に残す。
- グラフ検索・全文検索（ES）が空・不調・未構築のときは、それを理由に回答を止めない。{graph_fallback}
- 必要な根拠の種別（ソース・設計書・定義・ログ/設定・呼出関係）が揃わないと判断したら、次の巡
  または再調査では調べる量を一段引き上げる（読む範囲・確認するファイルを広げる）。
"""

# `--output-schema`（docs/proposals/2026-09-08-Codex出力スキーマ.md §2-3）有効時だけ付け足す段落。
# スキーマ無効時にこの構造化応答の要求を出すと、Codex が実際には守れない形式を約束させられるだけで
# 実害がある（`--output-schema` が無ければ CLI 側の強制も無い）ため、`write_agents_md` の
# `output_schema` 引数が真のときだけ本文に足す。
# `status`／`in_progress`／`next_step`／中断時の記述の意味（v1・v2 共通）。v2 段落は元々「3項目版と
# 同じ意味」と v1 段落を参照する形だったが、`output_schema_v2=True` のときは v1 段落自体が出力され
# ず参照先が無い（下の `structured_paragraph` 選択が排他のため）——`providers/codex/provider.py` の
# 自動継続（`_STRUCTURED_STATUSES`・全件確認前は `in_progress` にする・`final` を受けると継続を
# 終了する）の前提となるこの記述を1か所に括り出し、両段落から使う。
_STATUS_FIELD_MEANING = (
    "`status` が `final` になるのは、依頼の調査が完了した（全件要求なら対象範囲の確認を終え、"
    "該当項目が answer にそろった）ときだけ。調べる作業がまだ残っているなら、同じ応答内で続けて"
    "調査するか、`in_progress` にして `next_step` へ次に何を調べるかを書く（`final` のときの "
    "`next_step` は null）。手順や計画の説明を求められた依頼は、その説明を書き終えた時点で "
    "`final`。利用者停止・通信エラー・既存の予算到達で中断した状態のまま応答を返すときは、"
    "`answer` に確認済みの結果・未確認の範囲・中断理由を分けて書く（新しい `status` 値は使わない）。"
)

_STRUCTURED_RESPONSE_PARAGRAPH = f"""\
- 最終応答は `status`／`answer`／`next_step` の3項目で返す。{_STATUS_FIELD_MEANING}
"""

# RV C5（DEPTH-2 S1・output_schema_v2.json）: `SHERPA_CODEX_OUTPUT_SCHEMA=2` のときだけ足す段落。
# `_STRUCTURED_RESPONSE_PARAGRAPH`（3項目）の代わりに使う——v2 は `claims` を4件目のキーとして
# 追加するため、Codex 自身に「4項目で返す・claims の意味と閉じた語彙」を伝えないと、CLI 側の
# スキーマ強制があっても Codex は3項目のつもりのまま埋めた形式的な claims しか返さず（または
# 常に空のまま）、`data.claims` が無言で空になる（`provider.py::_parse_claim` が空の confirmed を
# 拒否するため、実際には「claims 無し」より「主張構造の恩恵が一切効かない」形で顕在化する）。
_STRUCTURED_RESPONSE_PARAGRAPH_V2 = f"""\
- 最終応答は `status`／`answer`／`next_step`／`claims` の4項目で返す。{_STATUS_FIELD_MEANING}
  `claims` は回答の主張を1件ずつ構造化した配列（`id`／`status`／`text`／
  `evidence_refs`／`reason`／`reason_code` の6キーちょうど）で、`status` は次の3種のどれか:
  `confirmed`（確定・裏付けとなる資料の根拠を最低1件 `evidence_refs` に書く。裏付けが無いなら
  confirmed にしない）／`inferred`（推定・断定できる根拠が無いが妥当と考える理由を `reason` に
  空でなく書く）／`unknown`（不明・`reason_code` を `not_found_in_scope`／`unexplored`／
  `insufficient`／`conflict`／`budget`／`unreadable` のどれか1つにする。該当なしと未探索を
  区別する）。`reason_code` は `unknown` のときだけ使う（他の `status` では空文字にする）。
  答えられる部分と不明な部分が混在する依頼は、全体を `unknown` でひとまとめにせず、答えられる
  主張は `confirmed`／`inferred` のまま個別に残す。
"""

# S6（§2.6）: multi_agent 有効時に本体（orchestrator）へ役割の使い方を伝える段落。`review_rounds`
# は選ばれた深さが許す evaluator の巡数（`depth_profile.review_rounds_for` の戻り値＝標準 0／
# 深く 2／最大は管理画面の設定値）——0 のときは evaluator を使わないことを明示する（標準は今までの
# 挙動と同じ・巡を増やさない）。何体をどう使うか自体は Codex の判断のまま固定の手順にはしない。
# `direct_read`／`layer` は `_source_verification_paragraph` と同じ理由・同じ組合せで文言を
# 切り替える（multi_agent は常時有効のため、直読不可・資料のみターンでもこの段落は出る＝矛盾を
# 避けるにはどちらも渡す必要がある）。
def _multi_agent_role_paragraph(review_rounds: int, direct_read: bool = True,
                                layer: str | None = None) -> str:
    _docs_only = not direct_read and layer == "docs"
    if review_rounds <= 0:
        # `_docs_only` はこの段落自体が「ソース裏取りはしない」を既に指示しているため、
        # rounds_note に「必ず自分でソースを確認」を続けると同一段落内で矛盾する
        # （RV是正: 標準深さでも復活していた実行不能な指示）。巡数の告知だけにする。
        rounds_note = ("今回の見直しの回数は 0 回＝evaluator は使わない。" if _docs_only else
                       "今回の見直しの回数は 0 回＝evaluator は使わない。worker の一次判断を鵜呑み"
                       "にせず、実装に関する主張は必ず自分でソースを確認してから最終回答に含める。")
    else:
        rounds_note = (f"今回の見直しの回数は {review_rounds} 回まで。spawn_agent(evaluator) は"
                       f"最大 {review_rounds} 回までとし、十分と判定できたらそれ以上は呼ばない。")
    if _docs_only:
        return f"""\
- worker（資料の検索・精読と一次判断だけを担当し、最終回答は書かない）と evaluator（根拠と
  一次判断を別観点で査読し、反証・条件例外・回答漏れ・未探索の範囲を指摘する。書き直さない）の
  サブエージェントが使える。観点に分解して調べる観点ごとに spawn_agent(worker) で調査させ、
  一次判断（確定／推定／不明の主張）を受け取る。今回の探す対象は資料のみ（ソースは対象外）のため、
  worker の一次判断に実装に関する主張が含まれていてもソース裏取りはしない——確定にせず、回答冒頭で
  『ソースを確認していないため確定できません』と明示したうえで、資料から分かる範囲の部分回答に
  する。グラフ・ES が空・不調・未構築のときも、それを理由に止めず資料（MCP の読取ツール）で確認
  できる範囲で答える。{rounds_note}
  evaluator の指摘は send_input で worker へ戻し、次の一次判断を待つ。何体をどう使うか（観点の
  分け方・worker の数）はあなた自身の判断でよい。最後に全体を統合し、指定された出力形式で
  最終回答を返す（成果物は各巡では作らず、最後に一度だけ作る）。
"""
    if direct_read:
        how = "`src/` の原本を開いて"
        graph_fallback = "`src/` を ripgrep／読取ツールで直接読む。"
    else:
        how = "MCP の読取ツール（ripgrep_search／read_around 等）で"
        graph_fallback = "MCP の読取ツール（ripgrep_search／read_around 等）で `src/` を確認する。"
    return f"""\
- worker（資料の検索・精読と一次判断だけを担当し、最終回答は書かない）と evaluator（根拠と
  一次判断を別観点で査読し、反証・条件例外・回答漏れ・未探索の範囲を指摘する。書き直さない）の
  サブエージェントが使える。観点に分解して調べる観点ごとに spawn_agent(worker) で調査させ、
  一次判断（確定／推定／不明の主張）を受け取る。worker の一次判断のうち実装に関する主張は、
  ソースの根拠（ファイル:行）が付いているものだけを採る。根拠が無い、または設計書・資料の根拠
  しか無い主張は、必ず自分（本体）が{how}確認してから採否を決める——worker が既に十分な根拠を
  持っているように見えても確認を省略しない。確認したファイル:行は最終回答の出典に残す。
  グラフ・ES が空・不調・未構築のときも、それを理由に止めず{graph_fallback}{rounds_note}
  evaluator の指摘は send_input で worker へ戻し、次の一次判断を待つ。何体をどう使うか（観点の
  分け方・worker の数）はあなた自身の判断でよい。最後に全体を統合し、指定された出力形式で
  最終回答を返す（成果物は各巡では作らず、最後に一度だけ作る）。
"""


# resume 直後は名前付きロール（worker/evaluator）での spawn_agent が
# "Full-history forked agents inherit the parent agent type" エラーで失敗しうる。`multi_agent`
# 有効時だけ足す段落（`write_agents_md` の `multi_agent` 引数は既定 False で本文に現れない）。
_MULTI_AGENT_RESUME_FALLBACK_PARAGRAPH = """\
- サブエージェント（worker／evaluator）を spawn するとき、このセッションを resume した直後に
  指定したロールでの spawn が失敗したら、ロールを指定しない spawn に切り替えて続ける
  （resume 直後は名前付きロールの委任が失敗することがある既知の制約）。
"""


def write_agents_md(authoring: Path, output_schema: bool = False, direct_read: bool = True,
                    output_schema_v2: bool = False, multi_agent: bool = False,
                    review_rounds: int = 0, layer: str | None = None) -> None:
    """authoring 直下へ AGENTS.md を書く（per-request・冪等・上書き）。

    呼び出し側で try/except すること（AGENTS.md はあくまで補助・書込に失敗しても Codex 実行自体は
    継続してよい＝fail-open。プロンプト側には containment/grounding の短縮形を常置してあるので、
    失敗時もプロンプトの質問固有部分＋短縮ルールだけで動くことを前提にする）。

    `direct_read`（既定 True）と `layer`（既定 None＝both・`docs`／`code`／`both`）は、常時付く
    本体自身のソース確認要件（`_source_verification_paragraph`・`multi_agent` 有効時は
    `_multi_agent_role_paragraph` にも）の文言を一緒に切り替える——`direct_read=True` なら層に
    関係なく「原本を直接開いて確認する」（`sandbox.py:633-638` の裁定＝Codex は層の指定を強制
    しない）、`direct_read=False and layer != "docs"` なら「MCP の読取ツールで確認する」に言い換え
    る（実行不能な手順を指示しない）。`direct_read=False and layer == "docs"` だけは読取要求ではなく
    「ソースは対象外・確定不可を告知して部分回答」を指示する——この組合せは MCP の `ripgrep_search`/
    `read_around` 自体が層制限でソース（code 種別）を拒否するため、ソース裏取りが原理的に不可能
    （提案書 §3 S1「必須種別は許可範囲内で解釈」・裁定⑥）。要件自体（本体自身の確認・グラフ不調でも
    止めない）はどの分岐でも維持する。`direct_read` が偽のときは調査スキル（原本を Python で開く
    前提）への誘導段落も落とす。
    `output_schema`（既定 False）が真のときだけ、構造化最終応答
    （`status`／`answer`／`next_step`）を求める段落を付け足す（§2-3・呼び出し側は `--output-schema` を付ける判定＝`_schema_on` と同じ値を渡す）。
    `output_schema_v2`（既定 False）が真のときは3項目版の代わりに4項目版
    （`_STRUCTURED_RESPONSE_PARAGRAPH_V2`・`claims` の意味と閉じた語彙を含む）を使う——
    `output_schema` が偽なら `output_schema_v2` が真でも段落を足さない（`--output-schema`
    自体が無効なターンへ、CLI が強制しない構造化応答を約束させない・既存の `output_schema` 契約と
    同じ理由）。呼び出し側は `_schema_v2`（`_schema_on and _schema_level == 2`）をそのまま渡す。
    `multi_agent`（既定 False）が真のときだけ、役割の使い方（worker／evaluator・`review_rounds`
    が埋め込む見直しの回数）と resume 直後の名前付きロール spawn 失敗へのフォールバック指示を
    付け足す（`features.multi_agent` を明示有効化する側で使う・既定では本文に現れない）。
    `review_rounds`（既定 0）は `multi_agent=True` のときだけ意味を持つ（`depth_profile.
    review_rounds_for` の戻り値をそのまま渡す契約・`multi_agent=False` なら無視される）。

    単純な `Path.write_text()` は既存の `AGENTS.md` が symlink だった場合にその**指す先へ**書き込んで
    しまう（authoring 配下の想定外の場所を書き換え得る）ため、一時ファイルを
    `O_CREAT|O_EXCL|O_NOFOLLOW` で新規作成し、`os.replace()` で置換する
    （`rename`/`replace` はディレクトリエントリの張替えでシンボリックリンクを一切追従しない＝
    既存 AGENTS.md が symlink でも安全に「通常ファイルの AGENTS.md」へ置き換わる）。
    """
    structured_paragraph = ""
    if output_schema:
        structured_paragraph = (_STRUCTURED_RESPONSE_PARAGRAPH_V2 if output_schema_v2
                                else _STRUCTURED_RESPONSE_PARAGRAPH)
    content = (AGENTS_MD + _source_verification_paragraph(direct_read, layer)
               + (_INVESTIGATE_SKILLS_PARAGRAPH if direct_read else "")
               + structured_paragraph
               + (_multi_agent_role_paragraph(review_rounds, direct_read, layer) if multi_agent else "")
               + (_MULTI_AGENT_RESUME_FALLBACK_PARAGRAPH if multi_agent else ""))
    target = authoring / "AGENTS.md"
    tmp = authoring / f".AGENTS.md.tmp-{os.urandom(6).hex()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp), flags, 0o644)
    try:
        os.write(fd, content.encode("utf-8"))
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
