"""調査ツール（索引なし grep・全文/グラフ検索・原本読取）の定義と実行環境の共通部品。
設計: docs/design/rag.md「読み取り部品の道具」
LLM に検索ツールを渡し、ripgrep_search で当たり → read_around で精読 → クエリ修正、と反復させる調べ方の土台。範囲は選択中の資料フォルダ＋scope のみ・read-only・本文テキストのみ送信。

このモジュールが持つもの: ツールの説明・引数スキーマ（MCP/簡易チャットが共有）、ツール結果のバイト予算、検索経路（grep/ES/グラフ）の可用性判定と通知文言、簡易チャット（`simple_chat.py`）が使う HTTP 送信・usage 合算の小部品。
ツールの実行は `tool_dispatch.run_tool`（読み取り部品 `parts/read/tools.py`）が担う。LLM 呼び出しの反復ループは Codex（MCP 経由）と簡易チャットが持ち、ここには無い。
"""
from __future__ import annotations

import logging
import threading
import time

from . import es_index, llm, stop_kind
from . import tools_pref as tools_pref_mod
# 呼び出し元（`agentic_search.MAX_HITS` 等）が参照する読み取り部品の名前。
from .parts.read.tools import (  # noqa: F401 -- 再公開（api・chat_service・mcp_server・providers が `agentic_search.X` で参照）
    MAX_HITS, MAX_HITS_ABS_MAX, READ_WINDOW, TOOL_RESULT_MAX_BYTES,
    _redact, _clip_utf8_bytes, GRAPH_REINGEST_ERROR_CODE, _READ_INVALID_ARGS_ERROR_CODE, verify_doc_exists,
)

# 共有ロガー（`simple_chat.py`/`ext_api.py` 等と同じ）。
_log = logging.getLogger("sherpa")


# `READ_WINDOW` の env-parse hi 引数と同じ値。`depth_profile.scaled_ratio` の `abs_max` として使う。
READ_WINDOW_ABS_MAX = 400


# `_SECRET_RE` の PRIVATE KEY ブロックは BEGIN/END が対で揃って初めてマッチする。`doc_readers.file_head` の `max_bytes` 切断や `_finish_reader_result` のクリップで END 側が失われると、鍵の断片が外部 LLM の tool 結果へ残るため、読めた範囲に残った断片自体は伏せる。
# 状態付き伏せ字（鍵ブロックの内側かを次の要素へ持ち越す）は `redact_keys.KeyBlockRedactor` が担う（`doc_readers` が切り詰める前に一次適用し、下の `_redact_deep` は結果全体を辿る多層防御としてこれを再利用する）。


def _clamped_setting_int(raw, lo: int, hi: int) -> int | None:
    """system_settings の生値を整数として検証する（型不正・範囲外は None＝呼び出し側がコード既定へ倒す）。`_env_int` と同じ lo/hi 契約。"""
    if raw is None:
        return None
    try:
        iv = int(raw)
    except (TypeError, ValueError):
        return None
    return iv if lo <= iv <= hi else None


def effective_tool_result_max_bytes(system_settings: dict | None = None) -> int:
    """ツール結果 1 件あたりのバイト予算の実効値（system_settings > コード既定）。`system_settings` 省略時は `store.get_system_settings()` を呼び、読めない/未設定はコード既定 `TOOL_RESULT_MAX_BYTES` へ倒す（fail-safe）。AI の文脈窓は Sherpa が制限しない（モデル・接続先では変えない）。
    """
    sysset = system_settings
    if sysset is None:
        try:
            from . import store
            sysset = store.get_system_settings()
        except Exception:
            sysset = {}
    v = _clamped_setting_int(sysset.get("agentic_budget_per_result"), 1024, 8 * 1024 * 1024)
    return v if v is not None else TOOL_RESULT_MAX_BYTES


# 親返し: es_search のヒットを doc_id で束ね、rag.md の領域（P2）を予算内で返す。検索自体は子チャンク単体のままで、ここは返す前の後処理のみ。
# 全文を読み込んでから切り詰める実装は禁止。P2 はアンカー単位のストリーミングで対象チャンクだけを集める（`_rag_md_region_text`・既存の安全弁 `_open_doc_stream`/`_stream_doc_lines`/`_READ_AROUND_FILE_CAP_BYTES` を再利用）。
# 全文（P3）段は無い（1 文書が予算を食い切り調査が打ち切られるため）。全文が要るときは AI が `read_doc` で個別に取得する。
# 表示側（`rag_parent_return.py`）は別実装で全文段を持つ。


# 調査台帳がある実行（Codex 標準モード・MCP 経由）で、検索・読取ツールの結果を台帳の項目ごとの記録（`mcp_server.py::_record_item_coverage`→`investigation_ledger.append_coverage_atomic`）へ結びつける任意引数。`run_tool()` はこのキーを読まず（無視）、MCP 層（`mcp_server.handle`）だけが見る。schema/description は 6 ツール共通でここに一括定義する。
_ITEM_PARAM_SCHEMA = {"type": "string",
                     "description": "調査台帳の項目 id（省略可）。台帳の項目のために探す・読むときに付ける"}
_ITEM_PARAM_NOTE = "調査台帳の項目のために探す・読むときは item にその項目の id を付ける（省略可）。"

_PARAMS_SEARCH = {"type": "object", "properties": {
    "query": {"type": "string", "description": "検索キーワード（型番・関数名・固有名詞など具体語が有効）"},
    "offset": {"type": "integer",
              "description": "ヒットの開始位置（既定0）。truncated:true のときは next_offset をそのまま渡すと続きが取れる"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["query"]}
# es_search は offset を持たない（kNN の候補集合はページ間で固定できず、ページを跨ぐと順位の入れ替わりで重複/欠落が起きる）。続きが必要なら max_hits（絶対上限まで）を増やす。
_PARAMS_ES_SEARCH = {"type": "object", "properties": {
    "query": {"type": "string", "description": "検索キーワード（型番・関数名・固有名詞など具体語が有効）"},
    "mode": {"type": "string", "enum": ["hybrid", "keyword", "vector"],
             "description": "検索方式。hybrid（既定）＝語の一致＋意味の近さ・keyword＝語の一致だけ・vector＝意味の近さだけ"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["query"]}
_PARAMS_LIST_DOCS = {"type": "object", "properties": {
    "path_prefix": {"type": "string",
                    "description": "フォルダで絞る（rel_path の先頭一致・例: '4期保守'）。省略可＝範囲全体"},
    "name_pattern": {"type": "string",
                     "description": "パス（フォルダ名/ファイル名どちらでも）の部分一致で絞る（例: '4期'）。省略可"},
    "doctype": {"type": "string",
               "description": "文書種別の完全一致で絞る（大文字小文字は無視。値は docs[].doctype の表示名と同じ・"
                              "例: 'Excel'・'cobol'・'設計書'。旧形式は 'Excel(旧)'／'Word(旧)'／'PowerPoint(旧)' の別値＝"
                              "'Excel' では .xls を含まない。値が不明なら doctype なしで1回呼んで確認する）。省略可"},
    "state": {"type": "string",
             "description": "状態の完全一致で絞る（'ready'=使える・'unreadable'=読み取れない・"
                            "'unknown'=直近確認が未実施）。省略可"},
    "limit": {"type": "integer", "description": "一覧に含める最大件数（既定200・上限500）。件数(count)は limit/offset と無関係に全件を返す"},
    "offset": {"type": "integer",
              "description": "一覧の開始位置（既定0）。truncated:true のときは next_offset をそのまま渡すと続きが取れる"}},
    "required": []}
_PARAMS_FOLDER_TREE = {"type": "object", "properties": {
    "path_prefix": {"type": "string",
                    "description": "この配下のフォルダ階層だけを見る（rel_path の先頭一致・例: '4期保守'）。省略可＝範囲全体"},
    "depth": {"type": "integer", "description": "列挙するフォルダの深さ上限（既定3・1〜10にクランプ）"}},
    "required": []}
_PARAMS_READ = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "ripgrep_search が返した doc_id（資料の相対パス）"},
    "line": {"type": "integer", "description": "精読の中心行（ヒット行）"},
    # 実際の既定値（`READ_WINDOW`）を埋め込む（env で変えたときにモデルへの通知も追随する）。
    "window": {"type": "integer", "description": f"前後に読む行数（既定 {READ_WINDOW}）"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["doc_id", "line"]}
_PARAMS_READ_DOC = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "list_docs/ripgrep_search 等が返した doc_id（資料の相対パス）"},
    "start_line": {"type": "integer",
                  "description": "読み始める行（既定1）。続きが必要なら前回の返却が示す次の行を指定して呼び直す"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["doc_id"]}
_PARAMS_OUTLINE = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "list_docs/ripgrep_search 等が返した doc_id（資料の相対パス）"}},
    "required": ["doc_id"]}
_PARAMS_ASK = {"type": "object", "properties": {
    "prompt": {"type": "string", "description": "ユーザに確認したい短い質問文"},
    "mode": {"type": "string", "enum": ["single", "multiple"],
             "description": "single=ラジオボタン、multiple=チェックボックス"},
    "options": {"type": "array", "minItems": 2, "maxItems": 8, "items": {"type": "object", "properties": {
        "id": {"type": "string", "description": "選択肢ID（省略可）"},
        "label": {"type": "string", "description": "表示ラベル"},
        "description": {"type": "string", "description": "補足説明（省略可）"}},
        "required": ["label"]}},
    "allow_free_text": {"type": "boolean", "description": "自由入力も許可するか"}},
    "required": ["prompt", "mode", "options"]}
_DESC_SEARCH = ("社内資料を全文 grep して当たりを付ける（doc_id と行番号つきのヒットを返す）。完全一致・固有名詞に強い。"
                "file_truncated が付くヒットは、その文書がまだ検索し切れていない可能性がある——"
                "read_doc で続きを確認する。text_truncated が付くヒットは本文が途中で切れている——"
                "read_around（周辺）か read_doc（続き）で読む。"
                "truncated:true はヒット数が上限に達した＝母集団の一部しか見ていない——"
                "next_offset をそのまま offset に渡せば続きが取れる（範囲を絞る・別の語で探すのも有効）。"
                f"{_ITEM_PARAM_NOTE}")
_DESC_READ = ("ヒット箇所の周辺行だけを精読する（全文は読まない）。doc_id と line を渡す。"
             "text_truncated が付いたら本文が上限で切れている——read_doc で続き（次の開始行）を読む。"
             f"{_ITEM_PARAM_NOTE}")
_DESC_READ_DOC = ("文書を開始行から連続して読む（通読向け・全文を一度には読まない）。"
                  "doc_id と start_line（省略時1）を渡す。1回の返却行数には上限があり、"
                  "「全◯行中 X〜Y行目」を返すので、続きが必要なら次の開始行（end_line+1）を"
                  "指定して再度呼び出す（range 外の start_line はエラーで明示）。"
                  "text_truncated が付くときは行内容が大きすぎて途中で切れている・"
                  "file_truncated が付くときは文書自体が大きすぎて total_lines が過小申告の可能性がある。"
                  f"{_ITEM_PARAM_NOTE}")
_DESC_OUTLINE = ("文書の見出し構造（Markdown の #/##/### 見出し・派生MDの表/シート見出しを含む）を"
                 "行番号つきで返す。read_doc/read_around で読む箇所の当たりを付けるのに使う。"
                 "見出しが無い文書は総行数だけを返す。file_truncated が付くときは文書自体が"
                 "大きすぎて total_lines/見出し一覧が過小申告の可能性がある。"
                 "見出しが上限で切られたときは truncated:true と count（総数）が付く＝続きは取れないので、その範囲は未確認として扱う。")
_DESC_LIST_DOCS = ("文書台帳の一覧・件数を返す（本文は読まない・grep しない）。"
                   "「ドキュメント数」「どんな資料があるか」「フォルダ構成」等の台帳質問はこれで答える。"
                   "path_prefix でフォルダ配下に絞り、name_pattern でパス（フォルダ名/ファイル名）の部分一致に絞れる。"
                   "doctype（種別の完全一致・大文字小文字は無視）・state（'ready'/'unreadable'/'unknown' の"
                   "完全一致）でも絞れる。docs は常に rel_path の昇順で並ぶ（offset を進めても順序は変わらない）。"
                   "count は絞り込み後の全件数（limit/offset と無関係）、docs は offset から limit 件までの"
                   "一覧（rel_path/doctype/state）。truncated:true なら残りがある——次回呼び出しの offset に"
                   "next_offset をそのまま渡して続きを取る。")
# list_docs（フラット一覧）に対する tree 相当のツール。フォルダ名の意味解釈はせず、クエリ時に呼び出し元の LLM が解釈する。
_DESC_FOLDER_TREE = ("world のフォルダ階層を、深さ上限つき・フォルダごとの件数つきで俯瞰する"
                     "（本文は読まない・grep しない・list_docs のフラット一覧では階層の形が掴めない"
                     "大規模な範囲で使う）。path_prefix でフォルダ配下に絞り、depth（既定3）で列挙する深さを決める。"
                     "フォルダごとに直下ファイル数・配下（再帰）ファイル数・直下サブフォルダ数を返す。"
                     "深さ上限でまだ配下があるフォルダは truncated:true（depth を上げて掘り下げる）。"
                     "フォルダ件数自体が多すぎるときは folders_truncated:true（count が打ち切り前の総数）＝続きは取れないので、その範囲は未確認として扱う。")
_DESC_ES = ("社内資料を日本語の全文＋ベクトル検索（形態素・意味の近さ・関連度ランキング）。"
            "言い回しが揺れる概念・日本語の同義語・自然文クエリに強い。"
            "ripgrep_search が0件/空振りのときはまずこれを試す。doc_id と抜粋を関連度順で返す。"
            "返す本文は該当箇所の周辺まで（文書全体は返さない）——文書全体を確認したいときは "
            "doc_id を渡して read_doc で読む。"
            "text_truncated が付くヒットは本文が途中で切れている——read_around（周辺）か "
            "read_doc（続き）で読む。"
            "truncated:true はヒット数が上限に達した＝母集団の一部しか見ていない（続きは取れない・範囲を絞るか別の語で探し、残りは未確認として扱う）。"
            "es_search は候補の発見用——全件が必要な列挙は ripgrep_search"
            "（truncated:true なら next_offset を offset に渡して続きを取る）・list_docs・原本の読取で行う。"
            "識別子・コード名・完全一致で探すときは mode:\"keyword\"（または ripgrep_search）、言い回しが揺れる概念・自然文は既定の hybrid、意味だけで広く集めたいときは mode:\"vector\"。各ヒットの keyword_match:false は、その語を含まない＝意味が近いだけ（根拠にする前に本文で確認する）。結果の mode_used が実際に使った方式（埋め込みが使えないときは keyword に縮退し degrade_reason が付く）。"
            f"{_ITEM_PARAM_NOTE}")
_DESC_ASK = ("回答や検索条件を確定する前にユーザへ確認する。結果が大きく変わる曖昧さがある場合だけ使う。"
             "例: 影響分析で起点や影響先が複数候補に割れるとき、確実な波及が0件で要確認だけになったときは、"
             "対象の絞り込みを確認してよい。依頼に「確認してから進めて」とあるときは調査より先に確認する。"
             "ただし依頼に「確認ID:」が含まれる場合は前の質問への回答なので再質問しない。"
             "選択肢はラジオボタンまたはチェックボックスとして表示される。")
_DESC_GRAPH = ("関係グラフから、ある名前（プログラム/コピーブック/ジョブ/データ項目/テーブルなど）の**関連部品**をたどる"
               "（コピー・呼び出し・参照・関連文書（言及）などの近傍を、つながりの経路つきで返す）。"
               "各近傍の経路は**辺ごとの種類と向き（from→to）**付き。COPIES／INVOKES／ACCESSES／CONTAINS だけの"
               "経路は構造的な依存＝根拠にしてよい（影響は矢印をさかのぼる: A →COPIES→ B は B を変えると A が"
               "影響を受ける）。DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）を含む経路や `unverified` の辺（裏付け原本が"
               "実在確認できない）を含む経路は候補＝原本で確認する。"
               "名前が一つでも判明したら、その関連の広がりは grep を反復するより先にこれで辿るほうが早い。"
               "原因の手がかり集め（トラブルシュート）に有効。grep で正確な名前を見つけてから渡すと精度が上がる。"
               "近傍が上限で切られたときは truncated:true と count（総数）が付く＝続きは取れないので、その範囲は未確認として扱う。"
               "coverage.complete が false のとき（時間切れ・件数の上限・文書探索の打ち切り・カード数の上限）は、近傍が空でも"
               "「近傍なし」とは言えない＝未確認（理由は coverage.limits[].kind）。"
               "unresolved は、この名前を参照しているのに接続先を決められなかった箇所（曖昧・未解決）の一覧＝"
               "接続が確認できていない参照として原本で確かめる（available が false なら申告を保存していない旧グラフ。candidates が null は 0 ではなく候補数を数えられなかった）。"
               "これは関連の近傍用（無向・言及を含む）。**変更の影響先を調べるときは graph_resolve で起点を選び graph_impact を使う**"
               "（構造の依存だけを矢印の逆向きにたどり、同名の別ノードを混ぜない）。"
               f"{_ITEM_PARAM_NOTE}")
_PARAMS_GRAPH = {"type": "object", "properties": {
    "name": {"type": "string", "description": "関連をたどる起点の名前（プログラム名/データ項目名など・具体名）"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["name"]}
_DESC_GRAPH_RESOLVE = ("関係グラフから、名前（の一部）・種別・所属パス（の一部）に合う**起点の候補**を並べる。"
                       "同名の別ノード（別フォルダ・別 package の同名のクラスなど）はまとめず別々の候補として返し、"
                       "各候補は canonical_id・kind（種別）・path（所属パス）・name（表示名）・qualified_name（修飾名がある場合だけ）・"
                       "match（exact＝名前が一致／partial＝部分一致）を持つ。"
                       "影響を調べる前に、候補の path などから正しい起点を選び、その canonical_id を graph_impact へ渡す"
                       "（候補が複数あって選べないときは、ユーザーに ask_user で確認してよい）。"
                       "候補が limit より多いときは count（総数）と coverage（complete:false・limits[].kind=result_cap・omitted）が付く"
                       "＝path や kind で絞って呼び直す。coverage.complete が false のとき、候補が空でも「該当なし」とは言えない"
                       "（理由は coverage.limits[].kind）。"
                       f"{_ITEM_PARAM_NOTE}")
_PARAMS_GRAPH_RESOLVE = {"type": "object", "properties": {
    "name": {"type": "string", "description": "名前の一部（大文字小文字を区別しない・修飾名も対象）。name と path のどちらかは必須"},
    "kind": {"type": "string", "description": "種別で絞る（Module／Copybook／Batch／DataItem／Table／Document／Config）"},
    "path": {"type": "string", "description": "所属パスの一部で絞る（例: 'billing/'）"},
    "limit": {"type": "integer", "description": "返す候補の最大数（既定 20・最大 50）"},
    "item": _ITEM_PARAM_SCHEMA}}
_DESC_GRAPH_IMPACT = ("**起点の canonical_id**（graph_resolve で選んだもの）から、構造の依存（COPIES／CONTAINS／INVOKES／ACCESSES）だけを"
                      "矢印の逆向きにたどり、起点を変えたときに影響を受ける部品（影響先）を返す。名前は受け取らない"
                      "（同名の別ノードを混ぜないため）。各影響先は canonical_id・kind・path・distance（起点からの段数）・"
                      "trace（影響先から起点までのノード名）・route（辺ごとの種類 type と向き from→to・参照元の資料 doc と行 line）を持つ。"
                      "DOCUMENTS（言及）は影響に数えず、related_documents（起点と影響先を言及する資料・層の限定がないときだけ）として別に返す"
                      "＝影響先ではなく関連資料。evidence は route と同じ並びで辺ごとの根拠（via＝関係の種類・line・rule＝接続を決めた解決規則・"
                      "sources＝参照元の資料 doc_id と行の先頭 evidence_limit 件・sources_overflow_count＝返さなかった件数）を持つ"
                      "＝確かめるときは原本のその行を開く。unresolved は起点の名前に一致する「解決できなかった参照」"
                      "（available:false なら未解決の情報が保存されていない旧いグラフ）で、ある場合は影響先が欠けている可能性＝未確認として扱う。"
                      "depth（既定 5・最大 10）と limit（既定 50・最大 200）で範囲を決める。"
                      "coverage.complete が false のとき（時間切れ・件数の天井・depth の先に影響先が残る・limit で切った）は、"
                      "影響先が空でも「影響なし」とは言えない＝未確認（理由は coverage.limits[].kind・depth の先が残るときは "
                      "coverage.depth.truncated が true）。depth や limit を上げて呼び直すか、範囲を未確認と明記する。"
                      f"{_ITEM_PARAM_NOTE}")
_PARAMS_GRAPH_IMPACT = {"type": "object", "properties": {
    "canonical_id": {"type": "string", "description": "起点の識別子（graph_resolve の candidates[].canonical_id）"},
    "depth": {"type": "integer", "description": "たどる深さの上限（既定 5・最大 10）"},
    "limit": {"type": "integer", "description": "返す影響先の最大数（既定 50・最大 200）"},
    "evidence_limit": {"type": "integer", "description": "辺ごとに返す根拠（sources）の最大件数（既定 3・0〜10）"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["canonical_id"]}
_PARAMS_GLOB = {"type": "object", "properties": {
    "pattern": {"type": "string",
               "description": ("ファイル名/パスのワイルドカードパターン。`*`/`?`/`[seq]` は1階層内のみ・"
                               "`**` は複数階層をまたぐ。スラッシュを含まなければファイル名として"
                               "どの階層でも探す（例: '*.jcl'・'*請求書*.xlsx'・'**/障害対応/*.md'）")}},
    "required": ["pattern"]}
_DESC_GLOB = ("ファイル名・フォルダ名のパターンで対象範囲内のファイルを列挙する（中身は読まない・パスのみ）。"
             "大文字小文字は区別しない。該当パス一覧と総件数を返す（上限200件・超過分は打ち切り＝truncated:true。"
             "続きは取れないので、その範囲は未確認として扱い count との差を全件と断定しない）。"
             "『x/**』は x 自体にも一致する（配下だけに絞るなら『x/**/*』）。")
# grep と同格の素朴な決定的ツール。2 文書の RAG 正本（.rag.md）の unified diff を返すだけで、レコード同定・業務キー対応付け・要約は呼び出し元の LLM が diff を読んで行う。
_DESC_COMPARE = ("2つの文書のRAG正本（.rag.md）を突き合わせ、追加/削除/変更行の unified diff を返す"
                 "（grepと同格の決定的な文字列比較——要約や業務レコードの対応付けはしない・"
                 "diffを読んで説明するのは呼び出し側の仕事）。"
                 "left_doc_id/right_doc_id で比較したい2文書を明示するか、"
                 "source_doc_id（片方の doc_id）と target_generation（比べたい世代＝トップフォルダ名）で"
                 "対応する文書を自動発見する。世代を除いた相対パスが完全一致すれば1件に決まる。"
                 "決まらないときは status: needs_disambiguation と candidates（doc_id 一覧）を返すので、"
                 "会話で利用者にどちらか確認してから left_doc_id/right_doc_id で呼び直す。"
                 "片方以上が rag.md を持たない文書（コード原文等）のときは status: unsupported を返す。"
                 "diff が上限で切られたときは truncated:true が付く＝続きは取れないので、その範囲は未確認として扱う。")
_PARAMS_COMPARE = {"type": "object", "properties": {
    "left_doc_id": {"type": "string", "description": "比較する片方の doc_id（省略時は right_doc_id も無視される）"},
    "right_doc_id": {"type": "string", "description": "比較するもう片方の doc_id（left_doc_id とセットで指定）"},
    "source_doc_id": {"type": "string", "description": "対応文書を自動発見する起点の doc_id（left_doc_id/right_doc_id 省略時）"},
    "target_generation": {"type": "string",
                          "description": "比べたい世代（トップフォルダ名・例 '5期'）。source_doc_id とセットで指定"}},
    "required": []}

# 原本読取ツール。Codex（MCP 経由）と API 経路の頭脳が同じ関数（`doc_readers.py`）で原本の中身を読む。毎回 Python を書かせず、トークンと実行時間を削り、再現性を上げる（突合・集計など定型外の作業だけ Python に任せる）。
_DESC_XLSX_SHEETS = ("Excel（.xlsx）原本のシート一覧と大きさを返す（原本を直接読む・派生ではない）。"
                    "doc_id はシート名・行数・列数を先に確認してから xlsx_range で読む範囲を絞るのに使う。"
                    "大きすぎて時間内に数えられないシートは dims_estimated=true で、大きさは記録値（推定）か不明。"
                    "シート一覧が上限で切られたときは truncated:true＝続きは取れないので、その範囲（残りのシート）は未確認として扱う。")
_PARAMS_XLSX_SHEETS = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .xlsx）"}},
    "required": ["doc_id"]}
_DESC_XLSX_RANGE = ("Excel（.xlsx）原本のセル範囲を表で返す（原本を直接読む・派生ではない）。"
                    "range 省略時は先頭から max_rows×max_cols（既定200行×50列）。"
                    "範囲が上限を超えたら切り詰めて truncated:true（range は実際に返した範囲）。"
                    "引用するときはシート名とセル範囲（例 'Sheet1!B3:D10'）で示す。")
_PARAMS_XLSX_RANGE = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .xlsx）"},
    "sheet": {"type": "string", "description": "シート名（xlsx_sheets が返す name）"},
    "range": {"type": "string", "description": "セル範囲（A1形式・例 'B3:D10'）。省略可＝先頭から既定サイズ"},
    "max_rows": {"type": "integer", "description": "返す最大行数（既定200）"},
    "max_cols": {"type": "integer", "description": "返す最大列数（既定50）"}},
    "required": ["doc_id", "sheet"]}
_DESC_DOCX_PARAGRAPHS = ("Word（.docx）原本の段落と表を返す（原本を直接読む・派生ではない）。"
                        "start（既定0）・count（既定200）で段落をページングする。表は先頭20表・各50行まで。"
                        "表が大きく結果が予算を超える場合は表の行も削られる（row_truncated:true）。"
                        "引用するときは段落番号（i）・見出し（style）、表なら表番号・行で示す。")
_PARAMS_DOCX_PARAGRAPHS = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .docx）"},
    "start": {"type": "integer", "description": "読み始める段落インデックス（既定0）"},
    "count": {"type": "integer", "description": "読む段落数（既定200）"},
    "table_start": {"type": "integer", "description": "表の開始インデックス（既定0・1回20表）。tables が total_tables に足りなければ進めて呼び直す"},
    "table_row_start": {"type": "integer", "description": "各表の開始行（既定0・1回50行）。rows が total_rows に足りなければ進めて呼び直す"}},
    "required": ["doc_id"]}
_DESC_PPTX_SLIDES = ("PowerPoint（.pptx）原本のスライドのテキスト・表・ノートを返す"
                    "（原本を直接読む・派生ではない）。pages（例 '3'・'2-5'・'1,3,5'・既定 '1-10'）で"
                    "スライドを指定する（1回20枚まで）。引用するときはスライド番号（no）で示す。")
_PARAMS_PPTX_SLIDES = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .pptx）"},
    "pages": {"type": "string", "description": "スライド指定（例 '3'・'2-5'・'1,3,5'）。省略時 '1-10'"}},
    "required": ["doc_id"]}
_DESC_PDF_PAGES = ("PDF 原本のページのテキストを返す（原本を直接読む・派生ではない）。"
                  "pages（例 '3'・'2-5'・'1,3,5'・既定 '1-5'）でページを指定する（1回10ページまで）。"
                  "1ページの文字量だけで結果が予算を超える場合でもページ自体は残し、本文を"
                  "切り詰めて text_truncated:true にする（ページ番号は保つ）。"
                  "引用するときはページ番号（no）で示す。")
_PARAMS_PDF_PAGES = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（拡張子 .pdf）"},
    "pages": {"type": "string", "description": "ページ指定（例 '3'・'2-5'・'1,3,5'）。省略時 '1-5'"}},
    "required": ["doc_id"]}
_DESC_FILE_HEAD = ("テキスト・コード原本の先頭バイトをそのまま返す（原本を直接読む・派生ではない・"
                  "Office/PDF は対象外＝xlsx_sheets/docx_paragraphs/pptx_slides/pdf_pages を使う）。"
                  "max_bytes（既定65536）まで読み、上限で切れていたら truncated:true。"
                  f"{_ITEM_PARAM_NOTE}")
_PARAMS_FILE_HEAD = {"type": "object", "properties": {
    "doc_id": {"type": "string", "description": "資料フォルダからの相対パス（テキスト・コード）"},
    "max_bytes": {"type": "integer", "description": "読む最大バイト数（既定65536）"},
    "item": _ITEM_PARAM_SCHEMA},
    "required": ["doc_id"]}

# `graph_neighbors`（`lens_service.neighbor_cards`）が内部で捕捉した障害の固定コード（本文・例外メッセージは持たない）。`_record_tool_result_error_code` が拾って `InvestigationState` へ反映する。
_GRAPH_NEIGHBORS_RECOVERABLE_ERROR_CODE = "graph_unavailable"
# グラフが使えないまま調べ続けたターンの通知（平文・専門用語ゼロ・資料名や本文を含まない）。
# 3 状態（入口で使えない＝未構築/OFF/不達／世代が古い／調査の途中で接続できなくなった）は、利用者の次の一手が違うため別の文言にする。API 経路・Codex 経路・非 agentic（`chat_service._finalize`）が同じ文言を共有する。
_GRAPH_DEGRADED_BLOCKED = "blocked"
# world にグラフが未構築（`:Entity{world_id}` が 0 件）。障害ではないため統計フラグは立てず、通知文言のみ。
GRAPH_EMPTY_CODE = "graph_empty"
GRAPH_DEGRADED_NOTICES = {
    _GRAPH_DEGRADED_BLOCKED: "関係のつながりをたどる検索が使えないため、資料とソースを直接調べて回答します。",
    GRAPH_REINGEST_ERROR_CODE: ("関係のつながりの情報が古いため今回は使わず、資料とソースを直接調べて"
                                "回答します（管理者に『今すぐ更新』を依頼してください）。"),
    # 入口で不達だったターンと調査の途中で切れたターンで同じ文言を使う（利用者にとってはどちらも「つながりを見に行けなかった」）。
    _GRAPH_NEIGHBORS_RECOVERABLE_ERROR_CODE: ("関係のつながりをたどる検索に接続できなかったため、"
                                              "資料とソースを直接調べた結果で回答します。"),
    GRAPH_EMPTY_CODE: ("関係のつながりの情報がまだ作られていないため、資料とソースを直接調べて"
                       "回答します（管理者に『今すぐ更新』を依頼してください）。"),
}


def graph_degraded_notice(code: str | None) -> str:
    """縮退コード（閉集合）に対応する通知文言。未知・None は空文字（通知しない）。"""
    return GRAPH_DEGRADED_NOTICES.get(code or "", "")


# 思考ノード（agents.py に依存しない＝循環回避）

_seq = [0]


def _nid() -> str:
    _seq[0] += 1
    return f"as-{_seq[0]}"


def _clip(s, n: int) -> str:
    return str(s or "").strip()[:n]


def _question_from_args(args: dict) -> dict:
    """ask_user の tool args をフロントに出せる安全な質問イベントへ丸める。"""
    args = args or {}
    mode = args.get("mode") if args.get("mode") in ("single", "multiple") else "single"
    prompt = _clip(args.get("prompt"), 300) or "確認したいことがあります。"
    options = []
    for i, opt in enumerate((args.get("options") or [])[:8]):
        if not isinstance(opt, dict):
            continue
        label = _clip(opt.get("label"), 120)
        if not label:
            continue
        oid = _clip(opt.get("id") or opt.get("value") or label, 80) or f"opt-{i + 1}"
        options.append({"id": oid, "label": label, "description": _clip(opt.get("description"), 180)})
    if len(options) < 2:
        options = [{"id": "yes", "label": "はい", "description": ""},
                   {"id": "no", "label": "いいえ", "description": ""}]
    return {"type": "question", "interaction_id": _nid(), "mode": mode, "prompt": prompt,
            "options": options, "allow_free_text": bool(args.get("allow_free_text"))}


# search_truncated（利用統計「打ち切りの内訳」対象）: 検索系ツールが母集団の一部しか返さなかった呼び出し。`result["truncated"]` をそのまま数える（計測のみ・制限は変えない）。
_SEARCH_TRUNCATED_TOOLS = frozenset({
    "ripgrep_search", "es_search", "glob_search", "graph_neighbors", "graph_resolve", "graph_impact",
    "list_docs", "doc_outline"})
# tool_result_clipped（同）: 1 件あたりのバイト予算で本文が切り詰められた呼び出し。判定キーは `text_truncated`（read_around/read_doc の逐次クリップ・ripgrep_search/es_search のヒット単位クリップ）と `byte_clipped`（`_finish_reader_result` の原本読取ツール・compare_documents の diff）。読取ツールの `truncated` は行数/ページ数の上限でも立つため使わない。
# ripgrep_search/es_search は `search_truncated` と `tool_result_clipped` が独立に起こりうる。doc_outline/graph_neighbors/list_docs/glob_search は `search_truncated` のみで数える。
_BYTE_CLIP_TOOLS = frozenset({
    "read_around", "read_doc", "xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides",
    "pdf_pages", "file_head", "compare_documents", "ripgrep_search", "es_search"})


def _post(url: str, headers: dict, body: dict, timeout: int = 90) -> dict:
    """HTTP POST(JSON)→JSON（共通層へ委譲・単発・リトライなし）。テストはこの関数を差し替える。
    同一プロバイダ内の限定リトライ（429・5xx・接続断のみ・別プロバイダへは切り替えない）は、呼び出し元（`openai_style` の `_send`）が本関数を物理送信のたびに 1 回ずつ呼んで組み立てる（呼び出し予算・usage 計測・stop_event・OpenAI 送信ガードの内側で「1 物理送信=1 消費」にするため）。
    """
    return llm.post_json(url, headers, body, timeout)


# トークン使用量の合算（ツールループの全ターン分）。生トークンだけを合算し、provider/model の付与は呼び元が行う。`final` イベントに `usage` を載せる（無ければ None）。
def _new_usage_acc() -> dict:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}


def _usage_or_none(acc: dict):
    return acc if any(acc.values()) else None


def _n(v) -> int:
    try:
        return max(int(v or 0), 0)
    except (ValueError, TypeError):
        return 0


def _acc_openai_usage(acc: dict, resp: dict, ollama: bool) -> None:
    u = (resp or {}).get("usage") or {}
    if ollama and not u:  # Ollama /api/chat（stream=false）はトップレベルの eval_count 系
        acc["input_tokens"] += _n(resp.get("prompt_eval_count"))
        acc["output_tokens"] += _n(resp.get("eval_count"))
        return
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    acc["input_tokens"] += _n(u.get("prompt_tokens"))
    acc["cached_input_tokens"] += _n(pd.get("cached_tokens"))
    acc["output_tokens"] += _n(u.get("completion_tokens"))
    acc["reasoning_output_tokens"] += _n(cd.get("reasoning_tokens"))


# `es_index.available()` の接続タイムアウトと同じ桁数に揃える。不達時に lock を握ったまま待つ時間の上限でもあるため短く抑える（秒）。
_GRAPH_AVAILABLE_TIMEOUT = 1.0


def _graph_available() -> bool:
    """関係グラフ(Neo4j)ツール `graph_neighbors` を AI に提示するか。`es_index.available()` と対称に実接続を確認する（URI の有無だけでは Neo4j 未起動でも常に True になる・`health._ping_neo4j` と同じ接続確認）。"""
    try:
        from neo4j import GraphDatabase

        from .ingest import world_neo4j
        env = world_neo4j._env()
        with GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]),
                                  connection_timeout=_GRAPH_AVAILABLE_TIMEOUT,
                                  connection_acquisition_timeout=_GRAPH_AVAILABLE_TIMEOUT) as driver:
            driver.verify_connectivity()
        return True
    except Exception:
        return False


# 短 TTL（既定 20 秒）の process-local キャッシュ。ES/Neo4j が即時拒否せずタイムアウトする環境で、1 ターン内の複数箇所の可用性チェックが直列加算されるのを防ぐ。`health.py::snapshot()` と同じ「lock 内で丸ごと計算」方式（同時 miss は先着 1 本だけがチェックし、後続は新鮮なキャッシュを読む＝single-flight）。
_TOOLS_AVAILABILITY_TTL = 20.0
_tools_availability_lock = threading.Lock()
_tools_availability_cache: dict = {"at": 0.0, "data": None}


def tool_availability(force: bool = False) -> dict:
    """検索経路 3 種（grep／全文・ベクトル(ES)／グラフ）の実接続に基づく可用性。grep は外部依存が無いため常に True。
    UI（チップの表示可否・`GET /chat/tools-availability`）と実行側（`chat_service._dispatch`/`providers/base._gather`・agentic の既定ツールセット構築）が同じ判定関数を共有する単一の真実源。
    短 TTL（`_TOOLS_AVAILABILITY_TTL`）でキャッシュする。呼び出し元はできる限り 1 ターンにつき 1 回だけ呼び、結果（snapshot）を `tools_availability` 引数として下流へ明示的に渡す（`toolset` を明示指定した呼び出しは本関数を呼ばない）。
    `force=True` は TTL キャッシュを無視して再計算する。
    キャッシュの `at` はプローブ完了後に記録する（開始前の時刻だと TTL がプローブ所要時間以下のとき待機側が「期限切れ」と誤判定して single-flight が成立しない）。
    呼び出し側はロック取得前に開始時刻 `call_start` を記録し、ロック内では「今から見て TTL 以内」または「`cache["at"] >= call_start`（自分の待機中に完成した世代）」のどちらかを満たせばキャッシュを共有する（ロックの受け渡し遅延だけで再 probe しない）。`call_start` より前に完成した古い世代は通常の TTL 判定に委ねる。
    """
    call_start = time.monotonic()  # ロック取得前に記録（この caller が要求した時刻）
    with _tools_availability_lock:
        cached = _tools_availability_cache["data"]
        at = _tools_availability_cache["at"]
        fresh_by_ttl = cached is not None and time.monotonic() - at < _TOOLS_AVAILABILITY_TTL
        # 自分が呼び出した後（ロック待機中を含む）に完成した世代なら、TTL 超過に見えても共有する（待機側がロック受け渡しの遅延だけで再 probe しない）。
        fresh_for_caller = cached is not None and at >= call_start
        if not force and (fresh_by_ttl or fresh_for_caller):
            return cached
        data = {"grep": True, "fulltext": es_index.available(), "graph": _graph_available()}
        _tools_availability_cache["at"] = time.monotonic()  # プローブ完了後に記録（上記 docstring 参照）
        _tools_availability_cache["data"] = data
        return data


def effective_tools_pref(tools_pref: dict | None, availability: dict | None = None) -> dict:
    """希望（`tools_pref`・省略=全 ON）と可用性（`availability`・省略=全て利用可能扱い）の AND。`dispatch_tools_for_lens` と provider の `_agentic_loop`/`_sub_loop` が共有する単一の計算。"""
    req = tools_pref_mod.normalize_tools_pref(tools_pref)
    avail = availability if availability is not None else dict(tools_pref_mod.DEFAULT_TOOLS_PREF)
    return {k: req[k] and avail.get(k, True) for k in req}


# 検索経路トグルで、このレンズの実行に必須なツールが全て OFF/不達のときの固定文言。非 agentic（`chat_service._dispatch`）・agentic（`providers/base._agentic_run`）が共有する。`_DISPATCH_REQUIRES_GRAPH` と対になる 2 値のみで、他は共通の既定文へ丸める。
_TOOLS_BLOCKED_HEADLINE = {
    "impact": "影響分析はグラフ検索が必要です（現在OFFまたは利用できません）。"
             "「詳細」で使う検索のグラフをONにしてください。",
    "troubleshoot": "トラブルシュートはグラフ検索が必要です（現在OFFまたは利用できません）。"
                   "「詳細」で使う検索のグラフをONにしてください。",
}
_TOOLS_BLOCKED_HEADLINE_DEFAULT = ("資料の「使う検索」がすべてOFF/利用できません"
                                  "（「詳細」で grep・全文のいずれかを有効にしてください）。")


def tools_blocked_env(lens: str) -> dict:
    """このレンズを実行できない（必須ツールが全て OFF/不達）ときの honest-failure envelope。`data: {}`（空 dict）で `chat_service._no_genuine_results` の契約と同じ形（再検索案内・断定 headline 上書きの対象から外れる）。呼び出し元が `env["scope"]` を追加して返す。
    `_tools_blocked`（内部専用サイドカー）: `providers/base.py::_gather`（非 agentic の trace）が env を受け取った直後に pop して読み、実行できなかったことを trace ノードにも反映する（「N 件を確認」という誤った完了表示にしない）。公開 `answer`/永続化には残さない。agentic 経路（`_agentic_run`）は自分で pop して捨てる。
    """
    headline = _TOOLS_BLOCKED_HEADLINE.get(lens, _TOOLS_BLOCKED_HEADLINE_DEFAULT)
    return {"headline": headline, "summary": {"total": 0}, "data": {}, "sources": [],
            "agentic_failure": "error",  # 実行できなかったターン＝終了理由の分布で完了扱いにしない
            "_tools_blocked": True}


def unavailable_explicit_tools(tools_raw: dict | None, availability: dict | None = None) -> list:
    """`tools_raw`（HTTP 入口の生値）のうち、明示的に `True` を指定したが実接続で到達不可なツール名（`tools_pref.TOOLS_PREF_KEYS` の正準順）。空リストは問題なし（省略/False のキーは可用分だけ黙って使う）。HTTP 入口（`routers/chat.py`）がこれで 422（ツール名つき）を返す。
    `availability`（省略可）: ターン先頭で 1 回計算した `tool_availability()` の snapshot。`routers/chat.py::_validate_tools_availability` は 422 判定と実行本体へ同じ snapshot を渡す（別々に呼ぶと TTL 境界で可用性が食い違い、明示 `graph:true` が 422 を素通りした直後にグラフが黙って無効化される）。
    """
    if not tools_raw:
        return []
    avail = availability if availability is not None else tool_availability()
    return [k for k in tools_pref_mod.TOOLS_PREF_KEYS
            if tools_raw.get(k) is True and not avail.get(k, True)]


# 非 agentic 経路のレンズ→必須ツール対応。impact/troubleshoot はグラフ traversal が実装そのもののためグラフ必須。qa/author は grep（ripgrep_search）と ES（fulltext）のどちらか一方があればよい。
_DISPATCH_REQUIRES_GRAPH = frozenset({"impact", "troubleshoot"})


def dispatch_tools_for_lens(lens: str, tools_pref: dict | None, availability: dict | None = None) -> tuple:
    """非 agentic 経路（`chat_service._dispatch`）が使う実効ツール判定。LLM が動的にツールを選ぶ agentic 経路とは独立に、grep/ES/グラフを「呼ぶか呼ばないか」の二値で判定する。
    `availability`（省略可）: 呼び出し元がターンにつき 1 回計算した `tool_availability()` の結果（ここでは計算しない）。省略時は全て利用可能として `tools_pref` の希望どおりに決まる。
    返り値 `(effective, blocked)`。`effective` は `effective_tools_pref(tools_pref, availability)`。`blocked` はこのレンズが実行不能（どの経路も残らない）かどうかで、真なら呼び出し元は OFF のツールへ黙ってフォールバックせず `tools_blocked_env` を返す。
    """
    effective = effective_tools_pref(tools_pref, availability)
    if lens in _DISPATCH_REQUIRES_GRAPH:
        blocked = not effective["graph"]
    else:
        blocked = not (effective["grep"] or effective["fulltext"])
    return effective, blocked


# 調査予算（ターン数／呼び出し予算／1 応答あたりの調べる操作の回数）到達で打ち切られた 3 値。`providers/base.py::_agentic_run` が一般的な失敗から分離し、固定文言の headline と既存 Evidence Packet を最終 envelope に載せる根拠にする。値は `stop_kind._BUDGET_STOP_REASONS` が唯一の真実源（`web/chat/render.js::BUDGET_EXHAUSTED_STOP_REASONS` は表示側の別実装）。
_BUDGET_EXHAUSTED_STOP_REASONS = stop_kind._BUDGET_STOP_REASONS
def _openai_style_text(msg: dict) -> str:
    """OpenAI/Ollama 方言の `message` から表示すべき本文を取り出す。
    OpenAI の refusal 応答は `content=null`・`refusal="<理由>"`・`finish_reason="stop"` の形（エラーではない）。`content` が空/欠落なら `refusal` へフォールバックし、無ければ空文字列。
    """
    return (msg.get("content") or msg.get("refusal") or "").strip()


