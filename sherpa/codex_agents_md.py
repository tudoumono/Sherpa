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

from .investigation_state import COVERAGE_KEYWORDS
from .providers.codex.sandbox import _CODEX_MAX_CONCURRENT_SUBAGENTS

# AGENTS.md の網羅要求の検知語（キーワードの定義そのものは `investigation_state.COVERAGE_KEYWORDS`
# が唯一の真実源——ここでは文言生成のためその集合をそのまま読み下すだけ）。
_COVERAGE_KEYWORD_LIST = "「" + "」「".join(COVERAGE_KEYWORDS) + "」"

AGENTS_MD = f"""\
# Sherpa 共通ルール（Codex 実行時）

- 原本は直接読んでよい（読取専用）。指定された資料フォルダ（KB）・派生フォルダ以外（このディレクトリの外・
  ユーザー workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。
  原本と変換済みテキスト（派生 MD／rag.md）は読むだけで、書き換え・上書き・移動・削除・名前の変更を
  絶対にしない。Word・Excel などの原本を自分で変換しない（旧形式（.doc／.xls／.ppt）など読取ツールで
  開けない原本は read_doc で変換済みテキストを読む）。作業用のファイルは `.tmp/` の下に作る。
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
  種別が存在しない場合は『該当なし』として不足にせず、回答にその旨を明示する。設計書と実装（ソース）の
  両方で確かめ、それぞれの根拠（ファイル:行）を示す。食い違えば両方を並べて『食い違い』と書く（実装を
  正とする）。見たソースが画面・バッチ・SQL のどれかを明記し、一部のソースだけで全体を判断しない
  （確かめられなかった点はそう書く）。
- 候補や主張は、根拠が不足していても捨てない。各項目を『ソース確認済み』『設計書のみ』『設計書とソースが不一致』『未確認』のいずれかに分類して
  回答へ残す（登録範囲にソースがあるなら、『設計書のみ』はソースを探した後にだけ使う）。設計書とソースが食い違う場合はソースを現行事実とし、両方のファイル:行を示す。ファイル:行の
  ない主張は事実として断定せず『根拠未提示・未確認』と明記する。全件依頼では、未確認項目が残る限り
  『全件確認済み』としない。
- 成果物（生成ファイル）は作成の依頼のときだけ作り、このディレクトリ（authoring 直下）に置く。
  調べる依頼（仕様問い合わせ・影響調査・トラブルシュート）ではファイルを作らない（作業用のファイルは
  `.tmp/` の下に作る・終われば消える）。
- スライド・プレゼン資料は、見た目重視の marp スキル（HTML/PDF/PPTX）を既定で使う。marp スキルでは
  Marp 形式の `.md` を書くだけでよく、レンダ（HTML/PDF/PPTX への変換）は完了後に Sherpa 側が自動で行う
  （自分でレンダコマンドを実行する必要は無い）。ただし「あとで PowerPoint で編集したい」と明示された
  場合だけ、marp ではなく pptx スキル（python-pptx）を使う（marp の PPTX は画像ベースで本文編集ができない）。
- 調査の途中経過（「次に〜を調べます」等の作業宣言）だけで終えない。調査を最後まで進めてから、
  結論と根拠を最終回答として書く。
- {_COVERAGE_KEYWORD_LIST}の依頼は、検索3回・根拠1件・件数だけの取得・代表例の発見では
  完了としない。対象範囲（ファイル一覧なら台帳・本文中の項目一覧なら対象資料のシート/段落/ページ総数）
  の確認を終え、該当項目が回答にそろってから完了とする。`truncated`／`text_truncated`／`file_truncated`／
  `total > start+count` は続きを取得する。続きを取得する手段が無い打ち切り（`file_truncated`・
  pdf_pages の `text_truncated`・compare_documents／graph_neighbors／glob_search／doc_outline の `truncated`・folder_tree の `folders_truncated`・xlsx_sheets／es_search の `truncated`＝ヒット数上限）は、その範囲を未確認として明示し全件性を主張しない。
  ripgrep_search の `truncated`（ヒット数上限）は続きを取得できる——`next_offset` を
  そのまま次回呼び出しの `offset` に渡す。
  利用者停止・通信エラー・既存の反復／情報量予算への到達で
  中断するときは、確認済みの結果・未確認の範囲・理由を分けて書き、部分結果を「全件」「すべて」
  「該当なし」と断定しない。
- 「X ごとに Y」のような親子二段の網羅要求（列挙する軸が2つある依頼）は2段階で調べる。
  (1) まず親項目 X の集合を確定し、件数を明示する（根拠は台帳・目次・見出し・コードの分岐・表の
  ヘッダ等）。(2) 次に各 X について Y を調べる。(3) 回答前に「親 N 件すべてに子が付いているか」を
  自分で数え、欠けている親があれば具体的に明示する（欠けたまま完了としない）。
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

# 本体自身のソース確認要件（深さに関わらず常時）: worker の主張を鵜呑みにせず、根拠に示された
# ファイル:行を自分で開いて突き合わせる（全文の読み直しは要求しない・根拠が無い主張は『未確認』に
# 分類して残す＝base AGENTS_MD 冒頭の分類原則に従う。捨てない）。
# `direct_read`（`_direct_read_ok`・provider.py が秘匿列挙／範囲の解決に失敗したときは偽）で
# 文言を切り替える——直読不可ターンで「直接読む」と指示すると、同じターンの他の指示
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
        verify_how = "自分で `src/` を直接開いて"
        graph_fallback = "必ず `src/`・原本を ripgrep／読取ツールで直接読んで確認する。"
    else:
        verify_how = "MCP の読取ツール（ripgrep_search／read_around 等）で自分で"
        graph_fallback = "必ず MCP の読取ツール（ripgrep_search／read_around 等）で `src/` を確認する。"
    return f"""\
- worker（サブエージェント）を使う場合、worker の主張は鵜呑みにせず、根拠として示された箇所
  （ファイル:行）を{verify_how}突き合わせる。全文を読み直す必要はなく、根拠の行とその前後を
  確認すれば足りる。根拠（ファイル:行）が示されていない主張は事実と断定せず『未確認』として
  回答に残す（分類の扱いは冒頭の分類原則に従う）。
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

# `SHERPA_CODEX_OUTPUT_SCHEMA=2` のときだけ足す段落。
# `_STRUCTURED_RESPONSE_PARAGRAPH`（3項目）の代わりに使う——v2 は `claims` を4件目のキーとして
# 追加するため、Codex 自身に「4項目で返す・claims の意味と閉じた語彙」を伝えないと、CLI 側の
# スキーマ強制があっても Codex は3項目のつもりのまま埋めた形式的な claims しか返さず（または
# 常に空のまま）、`data.claims` が無言で空になる（`provider.py::_parse_claim` が空の confirmed を
# 拒否するため、実際には「claims 無し」より「主張構造の恩恵が一切効かない」形で顕在化する）。
# S1b: `claims` 各要素に `evidence_kinds`（7キー目）を足した——Codex 自身の MCP/直読の履歴は
# `investigation_state.Evidence` のような機械的に検証できる調査内台帳を持たないため、API 経路
# （根拠から種別を導く）と違い Codex は主張ごとに「実際に確認した根拠種別」を自己申告する必要が
# ある。申告が閉じた語彙に合わないと主張構造ごと空になる（`_parse_claim`）ため、語彙・判定基準を
# 具体的に書く。
_STRUCTURED_RESPONSE_PARAGRAPH_V2 = f"""\
- 最終応答は `status`／`answer`／`next_step`／`claims` の4項目で返す。{_STATUS_FIELD_MEANING}
  `claims` は回答の主張を1件ずつ構造化した配列（`id`／`status`／`text`／
  `evidence_refs`／`reason`／`reason_code`／`evidence_kinds` の7キーちょうど）で、`status` は
  次の3種のどれか: `confirmed`（確定・裏付けとなる資料の根拠を最低1件 `evidence_refs` に書く。
  裏付けが無いなら confirmed にしない）／`inferred`（推定・断定できる根拠が無いが妥当と考える
  理由を `reason` に空でなく書く）／`unknown`（不明・`reason_code` を `not_found_in_scope`／
  `unexplored`／`insufficient`／`conflict`／`budget`／`unreadable` のどれか1つにする。該当なしと
  未探索を区別する）。`reason_code` は `unknown` のときだけ使う（他の `status` では空文字にする）。
  答えられる部分と不明な部分が混在する依頼は、全体を `unknown` でひとまとめにせず、答えられる
  主張は `confirmed`／`inferred` のまま個別に残す。回答本文で候補を『ソース確認済み』『設計書のみ』
  『設計書とソースが不一致』『未確認』に分類した場合、項目の分類（4分類）と主張の確度（`status`）は
  別物として対応させる——『ソース確認済み』の主張、および『設計書とソースが不一致』のうちソースで
  裏付けた現行事実の主張は `confirmed`（根拠にソースの行を書き、`reason` に設計書側の行と不一致の
  内容を書く。不一致そのものから先の解釈・推測は `inferred`）。『設計書のみ』は `inferred`
  （`reason` に設計書側の該当箇所を書く）。『未確認』は `unknown` にする。
  `evidence_kinds` は、この主張の根拠として**実際に開いて確認した**資料の種別を列挙する配列
  （`source`／`spec_doc`／`definition`／`log_config`／`callgraph` の閉集合・複数可・無ければ
  空配列）。`source` は `src/` のコード本文を実際に読んだときだけ入れる（一覧・件数だけの取得は
  含めない）。`callgraph` はグラフ照会（graph_neighbors 等）で確認したとき、グラフが使えなければ
  ripgrep 等の呼出し検索で代替確認したときに入れる。`confirmed` にするのは、この質問の型が必要と
  する根拠種別（仕様問い合わせ＝ソース＋設計書／影響調査＝ソース＋呼出関係／トラブルシュート＝
  ソース＋ログ・設定／作成系＝ソース＋設計書。登録範囲に無い種別は対象外）が `evidence_kinds` に
  揃っているときだけ——揃わない場合は `inferred` にし、`reason` に確認できていない種別を書く。
"""

# 調査台帳（`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §3・§6「AGENTS.md で済む箇所」・
# 台帳ファイル自体の読み書き契約は `sherpa/investigation_ledger.py` が正典）の作り方・使い方を
# 伝える段落。機械的な完了判定（provider.py 側・別契約）は `.tmp/investigation/` 配下の台帳の
# 非終端有無を見て `status=final` を拒否する——この段落が無いまま機械判定だけ効くと、Codex が
# 台帳を一切作らないのに機械判定は「未完了」を返し続ける食い違いが起きる。`_schema_v2`（構造化
# 応答が4項目版で有効）と同じ条件（`output_schema` と `output_schema_v2` が両方真）のときだけ
# 足す——`output_schema`/`output_schema_v2` の既存2引数の組合せで判定する（有効化そのものは
# 増やさない）。docs_only（direct_read=False かつ layer=="docs"）はソースが対象外のため、母集団の
# 確定元・item の初期状態・列挙元を設計書側に書き分ける（`pending` 経由の作成や「ソースから発見」を
# 出すと、同じ段落内で「ソースは対象外」と矛盾する）。キー名・状態語彙・完了条件は両分岐で共通。
# `source_required`（呼び出し元が渡す・provider.py の required_extra と同じ判定＝MCP へ実際に
# 渡す実効の層で決める）は、item に source を required_checks へ含めさせる文を出すかどうかだけを
# 切り替える——docs_only とは別の軸（docs_only=False でも、範囲に実在しない・層が docs の場合は
# source_required=False になりうる。両者を混同すると、この段落だけが「source を宣言しろ」と言い、
# Sherpa 側の完了判定（required_extra）は求めていない食い違いが起き、モデル自身の宣言のせいで
# spec_only が未充足のまま終わらなくなる）。
def _investigation_ledger_paragraph(direct_read: bool = True, layer: str | None = None,
                                    source_required: bool = False) -> str:
    _docs_only = not direct_read and layer == "docs"
    # 母集団がゼロ件だと items=[] のまま manifest が無効になり完了できない——走査した範囲そのものを
    # 1 item として登録し、理由付き終端（not_found_in_scope）にする逃げ道を明示する
    # （どちらの分岐でも起こりうるため共通文言）。
    _zero_population = (
        "母集団がゼロ件のときは、走査した範囲そのものを1 item（例: `id: \"scope-check\"`・"
        "`kind: \"scope\"`・`subject`: 走査した範囲）として登録し、走査結果を `reason` に書いた "
        "`not_found_in_scope` で終端にする（`items` が空のままでは manifest が無効になり完了できない）。"
    )
    # 明示継続で復元された台帳を初期化しないよう、作成前にMCPで状態を確認する。
    _resume_check = (
        "まず親が `ledger_status` で台帳の状態を確認する"
        "（作成系の依頼・1つの事実を答えるだけの単純な質問では台帳自体を作らなくてよい。"
        "台帳を作らなかった依頼は、この段落の残り——item との対応・全 item 終端まで final を"
        "返さない等——には従わない）。`manifest_invalid` が true かつ `items` が 0 の場合だけ、"
        "以下の手順で新規に作る。それ以外は既存の manifest と終端の item をそのまま保持し、"
        "非終端の item から調査を再開する（`pending`／`in_progress`・根拠種別が未充足の item。"
        "母集団の再確定や item の作り直しをしない）。"
    )
    if _docs_only:
        creation_block = (
            "今回の探す対象は資料のみ（ソースは対象外）のため、母集団は設計書から確定する。"
            f"{_zero_population}"
            "母集団の各要素を1 item として、該当の設計書箇所を `evidence` に1件以上付けた "
            "`spec_only` で `ledger_item_put` に登録し、親が `ledger_manifest_set` に "
            "`question_kind`（`list`／`compare`／`impact`／`troubleshoot`／`other`）と "
            "`items`（全 id の配列）を渡す。質問型ごとの item の単位: "
            "一覧＝設計書から発見した項目／比較＝比較対象×比較軸／影響調査＝未探索のノード・"
            "呼出辺（見つかった辺は都度 manifest に追加する）／トラブルシュート＝仮説・観測・検証結果。"
        )
    else:
        # `source_required` が偽（層が docs・または範囲にソースが無いと判定済み）のときは足さない
        # ——item 自身にも source を宣言させると、Sherpa 側は求めていない未充足をモデル自身の
        # 宣言だけで作ってしまう。
        _source_required_block = (
            "登録範囲にソースがあるなら、各 item の `required_checks` に `source` を必ず含める"
            "（Sherpa 側も完了判定で source を必須として確かめるため、宣言を省いても完了しない）。"
            "ソースを探しても該当が見つからない item は `spec_only` ではなく、探した内容を "
            "`reason` に書いた `not_found_in_scope` で終端にする。"
        ) if source_required else ""
        creation_block = (
            "最初の手順として、質問の型（`question_kind`）を決め、母集団を設計書と実装（ソース）の"
            "両方から確定する（設計書の一覧・表と、ソースの分岐・定数・列挙を突き合わせ、どちらか一方に"
            "しか無い項目も落とさない。見たソースが画面・バッチ・SQL のどれかを確かめ、一部のソースだけで"
            "母集団を決めない）。登録範囲にソースが無い場合は設計書から確定し `spec_only` で始める"
            f"（該当の設計書箇所を `evidence` に付ける）。{_zero_population}"
            f"{_source_required_block}"
            "母集団の各要素を1 item として `pending` で `ledger_item_put` に登録し、親が "
            "`ledger_manifest_set` に `question_kind`（`list`／`compare`／`impact`／"
            "`troubleshoot`／`other`）と `items`（全 id の配列）を渡す。"
            "質問型ごとの item の単位: 一覧＝設計書・ソースから発見した項目／比較＝比較対象×"
            "比較軸／影響調査＝未探索のノード・呼出辺（見つかった辺は都度 manifest に追加する）／"
            "トラブルシュート＝仮説・観測・検証結果。"
        )
    return f"""\
- 調べる依頼（仕様問い合わせ・影響調査・トラブルシュート・比較・一覧）では、{_resume_check}
  {creation_block}
- 台帳のファイルを直接書かない。保存と状態確認には台帳ツールを使う。`ledger_manifest_set` は
  親だけが呼ぶ。`created_at` はサーバが設定・保持する。`ledger_item_put` はその item の owner
  （`owner` は `parent` または `worker-<n>`）だけが呼ぶ。親が各 worker に item id を割り当て、
  worker は `ledger_item_put` だけを使い、自分に割り当てられた item だけを更新する。
  worker を使わない場合は親が全 item を登録・更新する（`owner: "parent"`）。同じ id を2つの
  プロセスが同時に更新しない。`error` が返ったら `problems` を読み、入力を直して再呼び出しする。
- item の欄は `id`／`kind`／`subject`／`required_checks`／`evidence`／`status`／`reason`／`owner`
  の8キーちょうど（余分なキーは書かない）。`id` は英数字・ハイフン・アンダースコアのみ。`subject`・
  `reason` は2,000文字まで。`evidence` は `kind`（`source`／`spec_doc`／`definition`／
  `log_config`／`callgraph`）・`path`・`line` の3キーだけの配列——資料の本文・引用・要約は
  `evidence` にも他の欄にも書かない（path と line だけ）。本文は最終回答の側で出典として示す。
- 状態は規約の語彙だけを使う。非終端は `pending`／`in_progress`。終端は `source_confirmed`／
  `spec_only`／`conflict`／`not_found_in_scope`／`unreadable`／`unavailable`。
  `not_found_in_scope`／`unreadable`／`unavailable` にするときは「何を試して届かなかったか」を
  `reason` に書く（必須）。`source_confirmed`／`spec_only`／`conflict` にするときは `evidence` を
  1件以上付ける（必須）。回答本文の4分類（冒頭の分類原則）と対応する: 『ソース確認済み』=
  `source_confirmed`、『設計書のみ』=`spec_only`、『設計書とソースが不一致』=`conflict`、
  『未確認』=`pending`／`in_progress`（まだ非終端のまま）または理由付きの `not_found_in_scope`／
  `unreadable`／`unavailable`。
- 台帳を作った依頼では、最終回答（`claims`）に書く主張は台帳のいずれかの item に対応する。
  `evidence` を持つ終端（`source_confirmed`／`spec_only`／`conflict`）の item に対応する主張は、
  その item の `evidence` にある path:line を根拠にする。`evidence` を持たない終端
  （`not_found_in_scope`／`unreadable`／`unavailable`）の item に対応する主張は `status` を
  `unknown` にし、その item の `reason` を claim の `reason` へそのまま写し、`reason_code` を item
  の状態に対応させる（`not_found_in_scope`→`not_found_in_scope`、`unreadable`→`unreadable`、
  `unavailable`→`insufficient`）——根拠の無い主張を事実と断定せず『未確認』として残す（冒頭の分類
  原則）ことと矛盾しない。台帳に無い新しい主張を最終回答で作らない。親が `ledger_status` を呼び、
  全 item が終端となり、根拠が必要な状態での未充足も解消して `complete` が true になるまで
  `status=final` は返さない——未完了の item があるのに `final` を返すと、Sherpa から「未完了:
  {{id...}}」と差し戻され、続きを求められる。差し戻されたら最初からやり直さず、指定された id の
  item から調査を再開する。調査中に新しい対象（影響先・比較軸・仮説）を見つけたら、worker は
  親に報告し、親が `ledger_manifest_set` で manifest に追加して割り当て、その item も終端になるまで
  調べる——未登録のまま放置しない。
  どうしても終端にできない item（登録範囲に無い・読めない）は理由付きの終端にして進み、
  「一旦ここまでで」と途中で閉じない。
  台帳を作らなかった依頼（作成系・単純な質問）ではこの対応関係は適用せず、`claims` は通常の
  判定基準（上の構造化応答の段落）だけに従う。
- 深さ（クイック／標準／深く／最大）は「どれだけ読んでいいか」ではなく「どれだけ検証するか」。
  台帳を作った依頼では、クイックでも母集団は全部終端にする。深さで変わるのは見直し
  （evaluator）の巡数と反証確認の厚みだけ。
"""


# worker を使う条件・並列数・依頼の形（網羅性の強化と、クイックを本当に速くする §変更A）。
# 「使うかどうか」は委ねない（観点が2つ以上に分かれる依頼では必ず使う）——「観点の分け方・
# worker の数」だけが Codex の判断（下の rounds_note 手前の一文）。並列数は sandbox.py の
# `[agents].max_concurrent_threads_per_session` と同じ値（ハードコードせず定数を import）。
_WORKER_USAGE_CONDITION = (
    "調べる観点が2つ以上に分かれる依頼（『〜ごと』『すべての』『各』『それぞれ』『一覧』『比較』"
    "など）では、観点ごとに spawn_agent(worker) を必ず使う。1つの観点で完結する単純な質問だけ"
    f"自分で調べてよい。観点ごとの worker は同時に {_CODEX_MAX_CONCURRENT_SUBAGENTS} 体まで起動して"
    "よい（逐次に待たない）。worker には観点を1つだけ渡し、主張とその根拠（ファイル:行）を"
    "返させる。")

# S6（§2.6）: multi_agent 有効時に本体（orchestrator）へ役割の使い方を伝える段落。`review_rounds`
# は選ばれた深さが許す evaluator の巡数（`depth_profile.review_rounds_for` の戻り値＝クイック 0／
# 標準 2／深く 4／最大は管理画面の設定値）——0 のときは evaluator を使わないことを明示する
# （クイックは確認 1 回だけで答える・巡を増やさない）。
# `direct_read`／`layer` は `_source_verification_paragraph` と同じ理由・同じ組合せで文言を
# 切り替える（multi_agent は常時有効のため、直読不可・資料のみターンでもこの段落は出る＝矛盾を
# 避けるにはどちらも渡す必要がある）。`ledger_enabled`（`write_agents_md` が
# `_investigation_ledger_paragraph` と同じ条件で渡す）が真のときだけ、worker への指示に
# 割り当てられた item id とその item だけを書く旨を足す——台帳が無いターンに item id の話をしても
# 実行不能な指示になる。
def _multi_agent_role_paragraph(review_rounds: int, direct_read: bool = True,
                                layer: str | None = None, escalate: bool = False,
                                ledger_enabled: bool = False) -> str:
    _docs_only = not direct_read and layer == "docs"
    _ledger_note = (
        " 調査台帳がある場合、worker には割り当てられた item id を伝え、worker は自分に割り当て"
        "られた item だけを `ledger_item_put` で更新する。worker が使う台帳ツールは "
        "`ledger_item_put` だけ（`ledger_manifest_set` は親だけが呼ぶ）。ファイルを直接書かない。"
    ) if ledger_enabled else ""
    if review_rounds <= 0:
        # `_docs_only` はこの段落自体が「ソース裏取りはしない」を既に指示しているため、
        # rounds_note に全主張の直読要求を続けると同一段落内で矛盾する——巡数の告知だけにする。
        # 非 docs_only 側は worker の主張を鵜呑みにしない要件を残しつつ、確認は根拠のファイル:行
        # の突き合わせで足りる（全主張の直読までは要求しない・§変更A③）。
        rounds_note = ("今回の見直しの回数は 0 回＝evaluator は使わない。" if _docs_only else
                       "今回の見直しの回数は 0 回＝evaluator は使わない。worker の一次判断を鵜呑み"
                       "にせず、根拠として示された箇所（ファイル:行）を自分で開いて突き合わせる"
                       "（全文を読み直す必要はない）。根拠が示されていない主張は事実と断定せず"
                       "『未確認』として回答に残す（分類の扱いは冒頭の分類原則に従う）。")
    else:
        rounds_note = (f"今回の見直しの回数は {review_rounds} 回まで。spawn_agent(evaluator) は"
                       f"最大 {review_rounds} 回までとし、十分と判定できたらそれ以上は呼ばない。")
        if escalate:
            # S1b（実装ベース探索の回復・深さの1段引き上げ）: 共通上限（管理画面の設定値）に
            # 余地があるターンだけ、本体の自己判断で見直しをもう1回だけ足してよい——強制ではなく
            # 「必須の根拠種別が揃わないと判断したとき」に限る許可（Sherpa 側は spawn_agent の
            # 呼出数を数えて止める仕組みを持たないため、指示のみ）。
            rounds_note += (f"ただし、必須の根拠種別が揃わないと判断したときに限り、見直しを"
                           f"もう 1 回だけ追加してよい（合計 {review_rounds + 1} 回まで）。")
    if _docs_only:
        return f"""\
- worker（資料の検索・精読と一次判断だけを担当し、最終回答は書かない）と evaluator（根拠と
  一次判断を別観点で査読し、反証・条件例外・回答漏れ・未探索の範囲を指摘する。書き直さない）の
  サブエージェントが使える。{_WORKER_USAGE_CONDITION}{_ledger_note}
  一次判断（確定／推定／不明の主張）を受け取る。今回の探す対象は資料のみ（ソースは対象外）のため、
  worker の一次判断に実装に関する主張が含まれていてもソース裏取りはしない——確定にせず、回答冒頭で
  『ソースを確認していないため確定できません』と明示したうえで、資料から分かる範囲の部分回答に
  する。グラフ・ES が空・不調・未構築のときも、それを理由に止めず資料（MCP の読取ツール）で確認
  できる範囲で答える。{rounds_note}
  evaluator の指摘は send_input で worker へ戻し、次の一次判断を待つ。観点の分け方はあなた自身の
  判断でよい。最後に全体を統合し、指定された出力形式で最終回答を返す（成果物は各巡では作らず、
  最後に一度だけ作る）。
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
  サブエージェントが使える。{_WORKER_USAGE_CONDITION}{_ledger_note}
  一次判断（確定／推定／不明の主張）を受け取る。worker の一次判断のうち実装に関する主張を
  確定扱いにするには、必ず自分（本体）が{how}ソースを確認する——worker が既に十分な根拠を
  持っているように見えても確認を省略しない。確認できない候補も捨てず、分類の扱いは冒頭の
  分類原則に従う（設計書の根拠しか無ければ『設計書のみ』、根拠が無ければ『未確認』として残す）。
  確認したファイル:行は最終回答の出典に残す。
  グラフ・ES が空・不調・未構築のときも、それを理由に止めず{graph_fallback}{rounds_note}
  evaluator の指摘は send_input で worker へ戻し、次の一次判断を待つ。観点の分け方はあなた自身の
  判断でよい。最後に全体を統合し、指定された出力形式で最終回答を返す（成果物は各巡では作らず、
  最後に一度だけ作る）。
"""


# resume 直後は名前付きロール（worker/evaluator）での spawn_agent が
# "Full-history forked agents inherit the parent agent type" エラーで失敗しうる。`multi_agent`
# 有効時だけ足す段落（`write_agents_md` の `multi_agent` 引数は既定 False で本文に現れない）。
_MULTI_AGENT_RESUME_FALLBACK_PARAGRAPH = """\
- サブエージェント（worker／evaluator）を spawn するとき、このセッションを resume した直後に
  指定したロールでの spawn が失敗したら、ロールを指定しない spawn に切り替えて続ける
  （resume 直後は名前付きロールの委任が失敗することがある既知の制約）。
"""


# 素の Codex モード（`plain`・docs/proposals/2026-09-24-素のCodexモード.md §1.2）専用の最小形。
# 台帳・分類原則の長文・出力スキーマ・worker/evaluator・investigate スキル誘導・グラフ不調時の
# 段落は出さない——残すのは containment（読取専用・範囲・秘匿は読まない・書き込みは authoring
# だけ）・出典の書き方・成果物の置き場所・ソースを正とする一文だけ。
AGENTS_MD_PLAIN = """\
# Sherpa 共通ルール（Codex 実行時・素のモード）

- 原本は直接読んでよい（読取専用）。指定された資料フォルダ（KB）・派生フォルダ以外（このディレクトリの外・
  ユーザー workspace・秘匿名のファイル（.env／鍵／credentials 等）等）は絶対に読まない。書き込みは
  このディレクトリ（authoring 直下）だけに行う。原本と変換済みテキストは読むだけで、書き換え・上書き・
  移動・削除・名前の変更を絶対にしない。
- 設計書と実装（ソース）の両方で確かめ、それぞれの根拠（ファイル:行）を示す。食い違えば両方を並べて
  『食い違い』と書く（実装を正とする）。
- 成果物（生成ファイル）は作成の依頼のときだけ作り、このディレクトリ（authoring 直下）に置く。
  調べる依頼ではファイルを作らない（作業用のファイルは `.tmp/` の下に作る・終われば消える）。
- 回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした資料を1行1件、資料フォルダからの
  相対パス（例 `4期更改/02_設計/xxx.xlsx`）で列挙する。派生MD／rag.mdを見た場合も原本のパスで書く。
"""


def write_agents_md(authoring: Path, output_schema: bool = False, direct_read: bool = True,
                    output_schema_v2: bool = False, multi_agent: bool = False,
                    review_rounds: int = 0, layer: str | None = None,
                    review_rounds_escalation: bool = False, mcp: bool = True,
                    source_required: bool = False, plain: bool = False) -> None:
    """authoring 直下へ AGENTS.md を書く（per-request・冪等・上書き）。

    `plain`（既定 False）が真なら、他の引数を一切見ず `AGENTS_MD_PLAIN`（最小形）だけを書く
    （素の Codex モード・§1.2）。

    `mcp`: MCP 接続（台帳ツールを含む）があるか。台帳はツールでしか書けないため、無ければ
    台帳の段落を出さない（本体の完了ゲートも同じ条件で無効になる）。

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
    `review_rounds_escalation`（既定 False）は `multi_agent=True` かつ `review_rounds > 0`
    のときだけ意味を持つ——共通上限（`depth_profile.effective_max_review_rounds`）に余地が
    あるターンに限り、本体が必要な根拠種別の不足を自己判断したときだけ見直しをもう1回だけ
    足してよい旨を段落へ足す（呼び出し側は `_review_rounds < 共通上限` を渡す契約）。

    `output_schema` と `output_schema_v2` が両方真（呼び出し側の `_schema_v2` と同じ判定）のときだけ、
    調査台帳（`.tmp/investigation/`）の作り方・使い方の段落（`_investigation_ledger_paragraph`）を
    足す——機械的な完了判定（provider.py 側）が台帳の非終端有無で `status=final` を拒否するため、
    Codex が台帳を作らないまま構造化応答だけ有効という食い違いを避ける。`multi_agent=True` のとき
    は `_multi_agent_role_paragraph` にも同じ判定（`ledger_enabled`）を渡し、worker への指示に
    割り当てられた item id の扱いを足す。

    `source_required`（既定 False）: 呼び出し側（provider.py）がそのターンの `required_extra` と
    同じ判定で決めた「今回 source を必須にするか」をそのまま渡す——`_investigation_ledger_paragraph`
    の docs_only（`direct_read`/`layer` の組合せ）とは別の軸（例: 層が code／both でも、範囲に
    ソースが実在しないと判定されれば False になりうる）。この値で「item の `required_checks` に
    source を必ず含める」文を出すかどうかを切り替える——渡さない（または実際とずれた）ターンでは、
    Sherpa 側は求めていない source をモデル自身の宣言だけで要求し、spec_only が完了できなくなる。

    単純な `Path.write_text()` は既存の `AGENTS.md` が symlink だった場合にその**指す先へ**書き込んで
    しまう（authoring 配下の想定外の場所を書き換え得る）ため、一時ファイルを
    `O_CREAT|O_EXCL|O_NOFOLLOW` で新規作成し、`os.replace()` で置換する
    （`rename`/`replace` はディレクトリエントリの張替えでシンボリックリンクを一切追従しない＝
    既存 AGENTS.md が symlink でも安全に「通常ファイルの AGENTS.md」へ置き換わる）。
    """
    if plain:
        content = AGENTS_MD_PLAIN
    else:
        structured_paragraph = ""
        if output_schema:
            structured_paragraph = (_STRUCTURED_RESPONSE_PARAGRAPH_V2 if output_schema_v2
                                    else _STRUCTURED_RESPONSE_PARAGRAPH)
        _ledger_enabled = bool(output_schema and output_schema_v2 and mcp)
        content = (AGENTS_MD + _source_verification_paragraph(direct_read, layer)
                   + (_INVESTIGATE_SKILLS_PARAGRAPH if direct_read else "")
                   + structured_paragraph
                   + (_investigation_ledger_paragraph(direct_read, layer, source_required)
                      if _ledger_enabled else "")
                   + (_multi_agent_role_paragraph(review_rounds, direct_read, layer,
                                                  escalate=review_rounds_escalation,
                                                  ledger_enabled=_ledger_enabled)
                      if multi_agent else "")
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
