"""Codex 実行前に authoring ディレクトリへ書き出す AGENTS.md（共通ルール：KB 以外を読まない・根拠ベースで答える・成果物は authoring 直下 等）。
設計: docs/design/codex.md「実行の構成」

Codex CLI は cwd 直下の AGENTS.md を自動で読む。リクエストごとに上書きし、内容は決定的な固定文字列（冪等）。機密は含まない。
"""
from __future__ import annotations

import os
from pathlib import Path

from .investigation_state import COVERAGE_KEYWORDS
from .providers.codex.sandbox import _CODEX_MAX_CONCURRENT_SUBAGENTS

# AGENTS.md の網羅要求の検知語。定義は `investigation_state.COVERAGE_KEYWORDS`（唯一の真実源）。
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
  結果に付く欠落の印（`section_truncated`・`fragment`・`unreadable_files`・`excluded_hits`・`unverified`・`excluded`・`no_text_layer`・`unread_shapes`・`unread_objects`・`formula_no_value`・`pages_remaining`・`pages_ignored`・`line_beyond_eof`・`window_clamped`・`candidates_total`・`title_truncated` など）が付いた範囲も、確かめるまで未確認として扱う。
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

# 原本直読が許可されたターンだけ足す段落（直読不許可のターンで達成不能な手順へ誘導しない）。
_INVESTIGATE_SKILLS_PARAGRAPH = """\
- 質問の型（資料一覧／仕様の問い合わせ／影響範囲／原因調査／比較）に合う `.agents/skills` の
  investigate-* スキルを読んで、その手順（ツールで当たり→原本の中身を確かめる→答える）どおりに進める。
- `src/` のソースは CP932（Shift_JIS）のことがある。日本語の語で探す・読むときは
  ripgrep_search／read_around を使う（UTF-8/CP932 を判定して読む）。シェルで探すなら、
  CP932 と確認したソースに `rg -E sjis` を使う。cat／sed で読む場合もそのファイルだけ
  `iconv -f CP932 -t UTF-8` を通す。UTF-8 のファイルには適用しない。
"""

# 本体自身のソース確認要件（深さに関わらず常時）。worker の主張を鵜呑みにせず、根拠のファイル:行を自分で開いて突き合わせる。
# 根拠が無い主張は「未確認」に分類して残す。
# `direct_read` で文言を切り替える（直読不可のターンで「直接読む」と指示しない）。
# `layer`（`docs`／`code`／`both`／`None`）も見る: 直読不可かつ `layer == "docs"` では MCP の読取がソースを拒否するため、
# 読取要求ではなく「ソース未確認のため確定不可」の告知を指示する。`direct_read=True` は層に関係なく直読で確認する。
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

# `--output-schema` 有効時だけ付け足す構造化応答の段落で共通に使う、`status`／`in_progress`／`next_step`／中断時の記述の意味。
# `providers/codex/provider.py` の自動継続（`_STRUCTURED_STATUSES`）の前提。
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
  利用者に届くのは `answer` だけ。説明のすべてを `answer` に書き、見直しや続きで書き直すときも
  前の内容を削らず、直した点を反映した完全な回答にする（短くまとめ直さない）。
"""

# `SHERPA_CODEX_OUTPUT_SCHEMA=2` のときだけ、3 項目版の代わりに使う段落。`claims`（各要素に `evidence_kinds`）の意味と閉じた語彙を伝える。
# Codex は根拠種別を自己申告する必要があり、語彙に合わないと主張構造ごと空になる（`_parse_claim`）。
_STRUCTURED_RESPONSE_PARAGRAPH_V2 = f"""\
- 最終応答は `status`／`answer`／`next_step`／`claims`／`reconciliation` の5項目で返す。{_STATUS_FIELD_MEANING}
  利用者に届くのは `answer` と `reconciliation`（画面に表で出る）だけ。`claims` は機械用の索引で画面に
  出ないため、`claims` にだけ書いた説明は利用者に届かない——説明のすべてを `answer` に書く。見直しや続きで書き直すときも
  前の内容を削らず、直した点を反映した完全な回答にする（短くまとめ直さない）。
  `claims` は回答の主張を1件ずつ構造化した配列（`id`／`status`／`text`／
  `evidence_refs`／`reason`／`reason_code`／`evidence_kinds`／`item_ids` の8キーちょうど）で、`status` は
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
  含めない）。`callgraph` はグラフ照会（graph_neighbors・graph_impact 等）で確認したとき、グラフが使えなければ
  ripgrep 等の呼出し検索で代替確認したときに入れる。`confirmed` にするのは、この質問の型が必要と
  する根拠種別（仕様問い合わせ＝ソース＋設計書／影響調査＝ソース＋呼出関係／トラブルシュート＝
  ソース＋ログ・設定／作成系＝ソース＋設計書。登録範囲に無い種別は対象外）が `evidence_kinds` に
  揃っているときだけ——揃わない場合は `inferred` にし、`reason` に確認できていない種別を書く。
  `reconciliation` は設計書とソースの照らし合わせの表で、1行ずつ `item`（項目名）／`spec_text`（設計書の
  記述）／`spec_ref`／`source_text`（ソースの実装）／`source_ref`／`verdict` の6キーちょうどで書く。
  設計書とソースの両方を**実際に開いて確かめた項目だけ**を書き、確かめていない項目・片方しか見ていない
  項目は書かない（該当が無ければ空配列）。`spec_ref`・`source_ref` は `資料のパス:行`（根拠の無い側は空文字）。
  `verdict` は `match`（一致）／`mismatch`（食い違い・ソースを正とする）／`spec_missing`（設計書に記述なし・
  `source_ref` 必須）／`source_missing`（ソースに見当たらない・`spec_ref` 必須）の4つ。一致も書く。
  根拠の資料が登録範囲に無い・読めない行は、画面で「未確認」に下げられる。
"""

# 主張の `item_ids` の書き方。台帳を使えるときは台帳の段落と組で出し、使えないときは空配列にさせる。
_CLAIM_ITEM_IDS_LEDGER = """\
  `item_ids` は、この主張が対応する調査台帳の項目の `id` を列挙する配列（調査台帳を作らなかった
  依頼や、対応する項目が無い主張は空配列）。`confirmed` の主張は、`item_ids` に挙げた項目が確認済みで、
  `evidence_refs` がその項目の根拠と一致しているときだけ確定として残る（別の項目の根拠を流用しない）。
  確認した項目は、回答のいずれかの主張の `item_ids` に必ず挙げる。
"""
_CLAIM_ITEM_IDS_EMPTY = """\
  `item_ids` は空配列にする。
"""

# 調査台帳の作り方・使い方を伝える段落（台帳の読み書き契約は `investigation_ledger.py`）。
# `output_schema` と `output_schema_v2` が両方真のときだけ足す。
# docs_only（`direct_read=False` かつ `layer=="docs"`）は母集団の確定元・item の初期状態・列挙元を設計書側に書き分ける。
# `source_required`（provider.py の `required_extra` と同じ判定）は、item の `required_checks` に source を含めさせる文を出すかだけを切り替える。
def _investigation_ledger_paragraph(direct_read: bool = True, layer: str | None = None,
                                    source_required: bool = False) -> str:
    _docs_only = not direct_read and layer == "docs"
    # 母集団がゼロ件のときは、走査した範囲そのものを 1 item として登録し、理由付き終端（not_found_in_scope）にする。
    _zero_population = (
        "母集団がゼロ件のときは、走査した範囲そのものを1 item（例: `id: \"scope-check\"`・"
        "`kind: \"scope\"`・`subject`: 走査した範囲）として登録し、走査結果を `reason` に書いた "
        "`not_found_in_scope` で終端にする（`items` が空のままでは manifest が無効になり完了できない）。"
    )
    # 明示継続で復元された台帳を初期化しないよう、作成前に MCP で状態を確認する。
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
        # `source_required` が偽のときは足さない（item 側にも source を宣言させない）。
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
- 台帳の項目のために探す・読むとき（ripgrep_search／es_search／read_doc／read_around／file_head／
  graph_neighbors／graph_resolve／graph_impact）は、その項目の item id を `item` 引数に付ける。
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
  原則）ことと矛盾しない。台帳に無い新しい主張を最終回答で作らない（この規則は正しさのためで、その範囲内で台帳の各
  項目の内容・条件・例外・根拠は `answer` に詳しく書く）。親が `ledger_status` を呼び、
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

# 中間の見直し。調査の途中（最初の item が 1 つでも終端になった時点）から complete になる前に、目的・観点を最低 1 回確かめ直させる。
# `_investigation_ledger_paragraph` と同じ条件（`_ledger_enabled`）のときだけ足す。
# complete 後に走る最終点検（provider.py の `_LEDGER_REVIEW_PROMPT`）とは別の仕組み。
_INVESTIGATION_REVIEW_PARAGRAPH = """\
- 台帳を作った依頼では、最初の item が1つでも終端になった後、最終回答（`status: final`）を
  返す前に `ledger_review_put` で中間の見直しを最低1回書く（本体だけが呼ぶ。worker は呼ばない）。
  書く内容は7つ: `purpose`（この依頼の目的を自分の言葉で1〜2文に言い直したもの）／
  `perspectives`（必要な観点の一覧。例: 画面・バッチ・DB・帳票・設定。1件以上）／
  `summary`（ここまでで分かったことの要約）／`added_items`・`removed_items`（この見直しで台帳に
  足した・外した item の id とその理由。無ければ空配列。実際に item を足す・外す操作自体は
  `ledger_manifest_set`／該当 item への `ledger_item_put` で別途行う）／`verdict`
  （`insufficient`＝求める答えに対してまだ全然足りない、`mostly_answered`＝求める答えはおおむね
  出た、のどちらか）／`extra_perspectives`（`verdict` が `mostly_answered` のときだけ、まだ調べ
  られる観点があれば挙げる配列。無ければ空配列。`insufficient` のときは必ず空配列）。
  見直しで item を `added_items` に挙げたら、その item が終端になるまで調べる——足したと申告した
  のに終端にしない item があると `ledger_status` は `complete` を true にしない
  （`review_pending` に残る）。
  `verdict` が `insufficient` なら調査を続け、調べたことをもとに必要ならもう一度見直しを書いて
  よい（上限は今の調べる深さ・継続回数の範囲内）。`mostly_answered` ならそのまま次へ進んでよい。
  `ledger_status` の `review_missing`（見直しが1件も無い）または `review_pending`（見直しで
  足した項目が未終端）が立っている間は、台帳の他の item が全て終端でも `complete` は true に
  ならない——`status=final` を返さず、見直しを書く（または足した item を終端にする）。
"""


# worker を使う条件・並列数・依頼の形。観点が 2 つ以上に分かれる依頼では必ず使い、観点の分け方・数だけ Codex が決める。並列数は `sandbox.py` の `[agents].max_concurrent_threads_per_session` と同じ値。
_WORKER_USAGE_CONDITION = (
    "調べる観点が2つ以上に分かれる依頼（『〜ごと』『すべての』『各』『それぞれ』『一覧』『比較』"
    "など）では、観点ごとに spawn_agent(worker) を必ず使う。1つの観点で完結する単純な質問だけ"
    f"自分で調べてよい。観点ごとの worker は同時に {_CODEX_MAX_CONCURRENT_SUBAGENTS} 体まで起動して"
    "よい（逐次に待たない）。worker には観点を1つだけ渡し、主張とその根拠（ファイル:行）を"
    "返させる。worker・evaluator が見つけた事実と根拠は、統合のときに落とさず最終回答に含める。")

# multi_agent 有効時に本体（orchestrator）へ役割の使い方を伝える段落。
# `review_rounds` は深さが許す evaluator の巡数（0 なら evaluator を使わない旨を明示）。
# `direct_read`／`layer` は `_source_verification_paragraph` と同じ組合せで文言を切り替える。
# `ledger_enabled` が真のときだけ、worker に割り当てられた item id とその item だけを書く旨を足す。
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
        # `_docs_only` では全主張の直読を要求せず、巡数の告知だけにする。それ以外は worker の主張を鵜呑みにせず、根拠のファイル:行の突き合わせで確認する。
        rounds_note = ("今回の見直しの回数は 0 回＝evaluator は使わない。" if _docs_only else
                       "今回の見直しの回数は 0 回＝evaluator は使わない。worker の一次判断を鵜呑み"
                       "にせず、根拠として示された箇所（ファイル:行）を自分で開いて突き合わせる"
                       "（全文を読み直す必要はない）。根拠が示されていない主張は事実と断定せず"
                       "『未確認』として回答に残す（分類の扱いは冒頭の分類原則に従う）。")
    else:
        rounds_note = (f"今回の見直しの回数は {review_rounds} 回まで。spawn_agent(evaluator) は"
                       f"最大 {review_rounds} 回までとし、十分と判定できたらそれ以上は呼ばない。")
        if escalate:
            # 共通上限に余地があるターンだけ、必須の根拠種別が揃わないと判断したときに限り、見直しをもう 1 回足してよい（指示のみ・強制しない）。
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
  最後に一度だけ作る）。最終回答は利用者の元の質問への回答だけを書き、worker・evaluator・点検・
  答え直しの経緯は本文に書かない。
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
  最後に一度だけ作る）。最終回答は利用者の元の質問への回答だけを書き、worker・evaluator・点検・
  答え直しの経緯は本文に書かない。
"""


# resume 直後は名前付きロールの spawn が失敗しうる。`multi_agent` 有効時だけ足す段落。
_MULTI_AGENT_RESUME_FALLBACK_PARAGRAPH = """\
- サブエージェント（worker／evaluator）を spawn するとき、このセッションを resume した直後に
  指定したロールでの spawn が失敗したら、ロールを指定しない spawn に切り替えて続ける
  （resume 直後は名前付きロールの委任が失敗することがある既知の制約）。
"""


# 素の Codex モード（`plain`）専用の最小形。containment（読取専用・範囲・秘匿は読まない・書き込みは authoring だけ）・出典の書き方・成果物の置き場所・ソースを正とする一文だけを出す。
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
    """authoring 直下へ AGENTS.md を書く（リクエストごと・冪等・上書き）。呼び出し側で try/except すること（書込失敗でも Codex 実行は継続できる＝fail-open）。
    - `plain`: 真なら他の引数を見ず `AGENTS_MD_PLAIN` だけを書く。
    - `mcp`: MCP 接続（台帳ツール）があるか。無ければ台帳の段落を出さない。
    - `direct_read`／`layer`: ソース確認要件（`_source_verification_paragraph`・multi_agent 時は `_multi_agent_role_paragraph` も）の文言を切り替える。
      `direct_read` が偽のときは調査スキルへの誘導段落も落とす。
    - `output_schema`: 真のときだけ構造化最終応答の段落を足す。`output_schema_v2` が真ならその 4 項目版を使う（`output_schema` が偽なら足さない）。
    - `multi_agent`: 真のときだけ役割の使い方と resume 後の spawn 失敗フォールバックを足す。`review_rounds` は `multi_agent=True` のときだけ意味を持つ。
      `review_rounds_escalation` は `review_rounds > 0` かつ共通上限に余地があるときだけ、見直しをもう 1 回足してよい旨を足す。
    - 調査台帳の段落は `output_schema` と `output_schema_v2` が両方真のときだけ足す（multi_agent では `ledger_enabled` も渡す）。
    - `source_required`: provider.py の `required_extra` と同じ判定の値を渡す（ずれると完了できなくなる）。
    書込は一時ファイルを `O_CREAT|O_EXCL|O_NOFOLLOW` で作り `os.replace()` で置換する（既存 AGENTS.md が symlink でも指す先へ書かない）。
    """
    if plain:
        content = AGENTS_MD_PLAIN
    else:
        structured_paragraph = ""
        if output_schema:
            structured_paragraph = (_STRUCTURED_RESPONSE_PARAGRAPH_V2 if output_schema_v2
                                    else _STRUCTURED_RESPONSE_PARAGRAPH)
        _ledger_enabled = bool(output_schema and output_schema_v2 and mcp)
        if output_schema and output_schema_v2:
            structured_paragraph += _CLAIM_ITEM_IDS_LEDGER if _ledger_enabled else _CLAIM_ITEM_IDS_EMPTY
        content = (AGENTS_MD + _source_verification_paragraph(direct_read, layer)
                   + (_INVESTIGATE_SKILLS_PARAGRAPH if direct_read else "")
                   + structured_paragraph
                   + (_investigation_ledger_paragraph(direct_read, layer, source_required)
                      + _INVESTIGATION_REVIEW_PARAGRAPH
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
            tmp.unlink(missing_ok=True)  # 置換に失敗したら一時ファイルを残さない（台帳スキャンを汚さない）
        except Exception:
            pass
        raise
