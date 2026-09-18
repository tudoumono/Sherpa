"""思考プロバイダ共通のプロンプト生成（リファクタリング計画 フェーズ5 S2・`sherpa/agents.py` から純移動）。

`_facts`/`_answer_prompt`（取得済みRAGの事実整形と回答プロンプト組み立て）・`_kb_hint`/`_kb_hint_abs`
（Codex への KB パス案内）・`_PLAIN_PROMPT*`（社内資料参照オフ時の素プロンプト）・
`_AUTHOR_FALLBACK_NOTE` を集約する。`sherpa/agents.py` が facade として本モジュールから
再エクスポートするため、呼び出し側（`_GenProvider`/`CodexProvider` 等）は無改修で動く。

`agentic_search._redact`・`worlds` の遅延 import は元コードのまま関数内で行う（循環 import 回避）。
移動に伴い相対 import の深さが1段増える（`sherpa/agents.py` → `sherpa/providers/prompts.py`）ため
`from .` は `from ..` に変更した（挙動は不変・参照先モジュールは変わらない）。

`_kb_hint_abs` 内の `Path(__file__).resolve().parents[1]`（repo root 参照・危険地雷1）は、
本ファイルが `sherpa/providers/prompts.py`（`sherpa/agents.py` より1階層深い）にあるため
`parents[2]` に明示修正した（値＝実 repo root は不変。
`tests/unit/test_agents_surface.py::test_kb_hint_abs_contains_repo_root_path` が
`sherpa.agents.__file__` 基準で pin しているため、この修正が正しいことをテストで担保する）。
"""
from __future__ import annotations

from pathlib import Path


def _kind_labels(kinds) -> str:
    """根拠種別（閉集合）の平文ラベル読み下し（`investigation_state` が唯一の語彙源）。"""
    from ..investigation_state import evidence_kind_labels
    return evidence_kind_labels(kinds)


def _digest_limit_lines(env: dict) -> str:
    """清書ダイジェストの「調査の限界」行だけを取り出す（impact／troubleshoot の分岐は引用ダイジェストを
    そのまま渡さないため、限界（0件・打ち切り・保存時切断・上限到達で中断）だけは別途連結する）。"""
    digest = env.get("_synthesis_digest") or ""
    limits = [ln for ln in digest.splitlines() if ln.startswith("調査の限界: ")]
    return ("\n" + "\n".join(limits)) if limits else ""


# グラフを引けていないターンの影響調査へ渡す指示（0 件は「影響が無い」ではなく「確認できて
# いない」）。グラフ用の文面と、引用ベースの整形へ倒したときの追記で同じ一文を使う。
_GRAPH_DEGRADED_IMPACT_STEER = (
    "。ただし関係のつながり（COPY/CALL/参照）は確認できていないため、"
    "構造的な影響の有無は不明であり「影響は無い」と断定しないこと"
    "（資料とソースを直接確認して分かった範囲だけを答える）。")


def _facts(lens: str, env: dict) -> str:
    """取得済みRAGの事実を LLM への根拠として簡潔に整形（ここに無いことは書かせない）。

    Feature B: env["_personal_facts"] がある場合は末尾に追記（本人のみ参照・共有 RAG には入れない）。

    qa（impact の引用フォールバックを含む）は `env["_synthesis_digest"]`
    （`agentic_search.build_synthesis_digest` の全件ダイジェスト）があればそれをそのまま使う——
    無ければ従来どおり先頭4引用×60字に整形する（ハイブリッド以外の呼び出し元は
    `_synthesis_digest` を持たないため挙動は不変）。troubleshoot／構造データありの impact は
    引用ダイジェストをそのまま渡さず、限界行だけ `_digest_limit_lines` で連結する。
    """
    d = env.get("data", {})
    # 影響調査（impact）は反復ツール検索の結果を経由することがあり、その場合の env は
    # グラフ由来の items/start ではなく**引用（citations）**や `_synthesis_digest`
    # （graph_neighbors 等の構造的根拠を含む全件ダイジェスト）を持つ。グラフ用の文面をそのまま
    # 当てると「起点『None』の影響は計0件」のように、利用者に見せない内部値が出た上、
    # グラフ確認済みの構造的根拠（digest）まで捨ててしまうため、データの形で判断し、
    # グラフ結果（items/presumed）が無ければ引用ベースの整形（qa と同じ）へ倒す
    # （citations か digest のどちらかがあれば倒す＝どちらも無ければ本当に根拠皆無の "計0件"）。
    _orig_lens = lens   # qa へ倒した後も「元がどのレンズだったか」で足す指示があるため退避する
    if (lens == "impact" and not d.get("items") and not d.get("presumed")
            and (d.get("citations") or env.get("_synthesis_digest"))):
        lens = "qa"
    # トラブルシュートも同型: グラフ不調で原因候補（candidates）を持たない縮退 env
    # （`chat_service._qa_fallback_env`）は引用ベースの整形へ倒す——そのまま当てると
    # 「原因候補なし」に潰れ、grep で拾った該当箇所が清書へ一切渡らない。
    if (lens == "troubleshoot" and not d.get("candidates")
            and (d.get("citations") or env.get("_synthesis_digest"))):
        lens = "qa"
    if lens == "impact":
        items = d.get("items", [])
        s = env.get("summary", {})
        # 起点が解決できなかった場合に `None` をそのまま文面へ出さない。
        # 起点なしの言い回しへ切り替える（利用者に内部値を見せない）。
        _start = d.get("start")
        origin = f"起点『{_start}』の" if _start else "変更対象の"
        # 構造的な影響が0＝この起点ではコード波及なし→検索へ誘導する（フォルダにコードが無いと断定しない）。
        # 症状語を追わず、変更対象（起点）と影響先の「接続（経路）」の確認へ誘導する。
        steer = ("。この起点では構造的なコードの波及は無い＝**変更対象（起点）と影響先の接続"
                 "（COPY/CALL/参照の経路）が辿れるか**を、資料の検索（仕様問い合わせ・トラブルシュート）や"
                 "関係グラフで確認するよう勧めること（症状語をそのまま探さない・フォルダにコードが無いとは断定しない）。")
        if env.get("graph_degraded"):
            # 関係グラフを引けていないターン——0 件は「影響が無い」ではなく「確認できていない」。
            steer = _GRAPH_DEGRADED_IMPACT_STEER
        if items:
            names = "、".join(f"{i['name']}({i['category']})" for i in items[:12])
            rest = f"（先頭12件・残り {len(items) - 12} 件は未提示）" if len(items) > 12 else ""
            base = f"{origin}影響: 計{s.get('total', 0)}件。対象: {names}{rest}"
        else:
            presumed = d.get("presumed", [])               # 構造的な影響0件でも資料からの関連推定があれば必ず伝える（0で突き放さない）
            if presumed:
                pn = "、".join(f"{p['name']}({p['category']})" for p in presumed[:12])
                pn += f"（先頭12件・残り {len(presumed) - 12} 件は未提示）" if len(presumed) > 12 else ""
                base = (f"{origin}確実な依存は見つからなかったが、資料からの関連（推定・要確認）が{len(presumed)}件: {pn}。"
                        "これらは推定であり確実ではない旨を明記すること" + steer)
            else:
                base = ((f"{origin}影響: 関係のつながりを確認できていないため件数は不明"
                        if env.get("graph_degraded") else f"{origin}影響: 計0件（該当なし）")
                        + steer)
        base += _digest_limit_lines(env)
    elif lens == "troubleshoot":
        from ..agentic_search import _redact            # grep 根拠本文も秘匿（ES は redact 済み・base grep の password/api_key 等を外部LLMへ流さない）
        cs = d.get("candidates", [])
        parts = []
        for c in cs[:8]:
            ev = c.get("evidence", {}) or {}
            qs = [_redact(g.get("text", ""))[:80] for g in ev.get("grep", [])[:2] if g.get("text")]
            parts.append(f"{c['name']}({c.get('role', '')})" + (f" 根拠「{' / '.join(qs)}」" if qs else ""))
        base = "原因候補: " + "、".join(parts) if parts else "原因候補なし"
        if len(cs) > 8:
            base += f"（先頭8件・残り {len(cs) - 8} 件は未提示）"
        base += _digest_limit_lines(env)
    else:
        digest = env.get("_synthesis_digest")
        if digest:
            base = digest
        else:
            cites = d.get("citations", [])
            base = ("該当箇所: " + " / ".join(f"{c['doc_id']}「{(c.get('quote') or '')[:60]}…」" for c in cites[:4])
                    if cites else "該当なし")
    if _orig_lens == "impact" and lens != "impact" and env.get("graph_degraded"):
        # 該当箇所（citations）を持つ縮退 impact は上で qa の整形へ倒れるため、影響レンズ用の
        # 「断定しない」指示がそのままでは清書へ渡らない——ここで同じ一文を足す。
        base += _GRAPH_DEGRADED_IMPACT_STEER
    # DEPTH-2 S1（RV C3）: 主張構造はレンズ分岐に関わらず必ず付加する——troubleshoot/impact は
    # 上の分岐で早期に `base` を確定するため、ここで一箇所にまとめないと清書プロンプトへ渡らない。
    claims_digest = env.get("_claims_digest")
    if claims_digest:
        base += f"\n\n【主張の構造（確定/推定/不明）】\n{claims_digest}"
    # 必要な根拠の種別が揃わなかったターンの注記（主張構造の有無に関わらず必ず渡す——
    # 主張構造が無い経路＝主張生成の失敗・単発フォールバックでも断定を抑える唯一の手がかり）。
    evidence_note = env.get("_evidence_note")
    if evidence_note:
        base += f"\n\n【根拠の不足】\n{evidence_note}"
    return base + (env.get("_personal_facts") or "")


def _answer_prompt(message, lens, env):
    return ("あなたは社内ナレッジの回答アシスタントです。以下の『取得済みの事実』だけを根拠に、"
            "日本語で回答してください（長さは絞らない＝取得済みの事実は削らず、事実に載っている項目は"
            "パス付きで省略せず列挙する。要約や代表例化で項目を落とさない。事実に無いパス・対象名は補わない）。"
            "表を指定されたら指定列を守り、項目と行・値の対応を保つ。取得できなかった値は推測で埋めず"
            "『未取得』と書く。取得済みの事実に無いことを補うときは『推定』と明示し、確定した事実と分けて書く。"
            "『調査の限界』行のうち対象範囲に関わるもの（本文の切断・未取得・未確認の範囲）があれば"
            "回答で未確認範囲として明示し『全件』『すべて』とは断定しない（一覧の取得総数が件数と一致して"
            "いれば、その一覧は全件として書いてよい。同じ条件の一覧行が複数あるとき（ページ送り）は、"
            "その条件で取得したパスを重複除去した集合の件数と総数を照合し、集合の件数が総数に満たない"
            "ときだけ未取得分の件数を明示して『全件』と書かない（列挙件数の単純合計は、重複して取得した"
            "ページの分だけ欠落を相殺してしまうため使わない）。"
            "パスが未提示の一覧行（『他 N 件のパスは未提示』）があれば、その一覧は全件として書かず未提示の件数を示す。"
            "途中の空振り検索の『0件』は一覧の完了を否定しない）。"
            "『主張の構造（確定/推定/不明）』があれば、それに沿って回答を組み立てる: 確定は断定してよい、"
            "推定は『推定』と明示して理由を添える、不明はその主張がなぜ答えられないか（理由コード）を"
            "明示する（不明な部分があるからといって回答全体を『不明』でひとまとめにしない・答えられる"
            "部分は答える）。"
            "『根拠の不足』があれば、そこに書かれた範囲については言い切らず、確認できていない旨を"
            "明示する（確定できる根拠が無いことを断定的な書き方で覆い隠さない）。"
            "設計書とソースの記述が食い違う場合はソースを正とし、食い違いがあったこと自体も書く。"
            "件数や対象名は事実のまま。出典（原本 DL）は Sherpa が付与するが、本文中でも根拠のパスを示してよい。"
            "回答は Markdown（太字・箇条書き・インラインコード）で書いてよい。"
            f"\n\n【質問】{message}\n【取得済みの事実】{_facts(lens, env)}\n\n回答のみ:")


def _continuation_prompt(message: str, lens: str, env: dict, tail: str) -> str:
    """DEPTH-2 S2（§2.7）: 清書本文が `length`（出力上限）で打ち切られたときの追記継続プロンプト。

    `_answer_prompt` と同じ『取得済みの事実』（`_facts`）を渡しつつ、**直前の本文の続きだけ**を
    書かせる（見出し・前置き・直前までの内容の繰り返しを禁じる）。`tail`（直前の本文の末尾断片）を
    示して「ここから自然に続ける」よう指示する——全文を往復させない（清書予算と同じ発想）。
    主張構造（claims JSON・§2.5）はこの関数の対象外（呼び出し元が清書本文にだけ使う契約）。
    """
    return ("あなたは社内ナレッジの回答アシスタントです。直前の回答が出力の上限で途中で切れました。"
            "**直前までに書いた内容を繰り返さず、続きだけ**を日本語で書いてください"
            "（見出し・前置き・『続きです』のような案内文は付けない）。"
            "直前の文が体言止め・句読点なしで終わっている等、不完全なまま切れている場合は"
            "その文を完成させてから続ける。直前の末尾（これより前は省略済み）:"
            f"\n『…{tail}』\n\n"
            f"【質問】{message}\n【取得済みの事実】{_facts(lens, env)}\n\n続きのみ:")


def _kb_hint(world: str) -> str:
    from .. import worlds                                       # fixtures 案内は **_fixtures() ゲートに統一**（"0"/"false" を誤って truthy にしない）
    base = f"fixtures/corpus/{world}" if worlds._fixtures() else f"data/kb/*/{world}"
    return f"{base}/md（設計書・仕様の決定的MD）と {base}/src（COBOL/JCL/コピーブック原文）"


def _kb_hint_abs(world: str) -> str:
    """MEDIUM-1 fix: Codex の cwd が workspace/authoring/ のため絶対パスで KB を指示する。
    MEDIUM fix2: fixtures モードか実 world registry の root を使う（固定 repo パスでなく実 world root）。
    """
    from .. import worlds
    repo_root = Path(__file__).resolve().parents[2]
    if worlds._fixtures():
        base = repo_root / "fixtures" / "corpus" / world
        return f"{base}（設計書・仕様の決定的MD は {base}/md/、COBOL/JCL は {base}/src/）"
    # 実登録 world からパスを解決（world_id=world が多い。見つからなければ data/kb 以下全体を案内）。
    try:
        wd = worlds.world_dir(world)
        if wd:
            return f"{wd}（設計書・仕様の決定的MD は {wd}/md/、COBOL/JCL は {wd}/src/）"
    except Exception:
        pass
    base = repo_root / "data" / "kb"
    return f"{base}/**/{world}/（設計書・仕様の決定的MD は md/ 配下、COBOL/JCL は src/ 配下）"


# 社内資料参照オフのときの素のプロンプト（検索結果＝事実を渡さない＝出典なしの一般回答）。
_PLAIN_PROMPT = ("あなたは親切な日本語アシスタントです。社内資料は参照していません。"
                 "一般的な知識の範囲で答えてください。**出典・社内資料・ファイル名・引用に基づくとは言わない**。"
                 "資料に基づく確認が必要なら『社内資料をオンにしてください』と促す。\n\n【質問】{q}\n回答:")

# HIGH-1 fix: 個人ファイルのヒットを plain プロンプトに注入するテンプレート。
_PLAIN_PROMPT_WITH_PERSONAL = (
    "あなたは親切な日本語アシスタントです。社内資料は参照していませんが、"
    "ユーザー本人がアップロードした個人ファイルのヒットが以下にあります。これを根拠に答えてください（長さは絞らない）。"
    "**他のユーザーには共有されない個人データです**。個人ファイルは出典として一覧に出さない"
    "（共有 RAG の引用元とは別扱い）。\n\n"
    "【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n{personal}\n\n【質問】{q}\n回答:")

def claims_prompt(question: str, digest: str, existing_claims_text: str = "",
                  findings_text: str = "") -> str:
    """DEPTH-2 S1/S4b 共通の主張構造プロンプト——査読後の最終手段（`providers/base.py::
    _claims_synthesis`）と worker の一次判断（`agentic_search.openai_style` の
    `final_synthesis=False` 経路）が同じ語彙・同じ出力契約（`investigation_state.parse_claims`
    が検証する `_CLAIM_KEYS`/`_CLAIM_STATUSES`/`_CLAIM_UNKNOWN_REASON_CODES` と一致）を使う。

    `existing_claims_text`（省略可・既定空文字＝節を付けない）: 再調査（2回目以降の worker 一次判断
    要求）で、直前までに確定した worker 由来の主張（id・status・本文のみ・根拠参照は含まない——
    今回のローカル調査状態とは別採番のため）を渡す。id の再利用規約（同じ論点は既存 id を使って
    内容を更新・別論点は未使用の新しい id を付ける）をあわせて伝える——渡さない（呼び出し元が
    査読を一度も通していない初回等）場合は毎回 "c1" から採番されても構わない。

    `findings_text`（省略可・既定空文字＝節を付けない）: 未解決の指摘
    （`investigation_state.render_findings`）。反証された論点を確定として書き直させないために
    渡す——採否自体は `InvestigationState.set_claims` が反証を再適用して機械的に守る。"""
    prompt = (
        "あなたは調査結果から主張を構造化する担当です。以下の質問と収集済みの根拠から、"
        "回答を構成する主張を1つずつ分解し、次の JSON 1個だけを出力してください"
        "（他の文章を書かない・コードブロックで囲まない）:\n"
        '{"claims": [{"id": "c1", "status": "confirmed", "text": "…", '
        '"evidence_refs": ["ev-1"], "reason": "", "reason_code": ""}]}\n\n'
        f"【質問】\n{question}\n\n【収集済みの根拠（digest）】\n{digest or '(なし)'}\n\n"
        "各主張の `status` は次のいずれか: "
        "`confirmed`（根拠で裏付けられる・`evidence_refs` に該当する ev-N を必ず入れる）／"
        "`inferred`（根拠は薄いが妥当な推定・`reason` に理由を書く）／"
        "`unknown`（判断できない・`reason_code` を次の語彙から選ぶ: "
        "not_found_in_scope=検索した範囲で見つからない、unexplored=未探索の範囲がある、"
        "insufficient=情報不足、conflict=資料間で矛盾、budget=打ち切り、unreadable=原本を読めない）。"
        "`reason`/`reason_code` は該当しない区分では空文字にする。")
    if existing_claims_text:
        prompt += (
            "\n\n【前回までの主張（この再調査の前に確定していたもの）】\n"
            f"{existing_claims_text}\n\n"
            "同じ論点を今回改めて述べる場合は、上記と同じ id をそのまま使い、内容を今回の内容で"
            "更新してください（新旧を別々の主張として両方出さない）。今回新たに分かった別の論点は、"
            "上記に無い未使用の id を付けてください。上記の id を別の論点に使い回さないでください。")
    if findings_text:
        prompt += (
            "\n\n【査読の未解決の指摘（反証を含む）】\n"
            f"{findings_text}\n\n"
            "「（反証）」の付いた指摘の対象になっている論点は、`confirmed` として出さないでください"
            "（`unknown` に `reason_code` を付けるか、根拠が薄い理由を添えて `inferred` にする）。")
    return prompt


def review_prompt(question: str, digest: str, claims_text: str = "",
                  findings_text: str = "", round_no: int = 1, total_rounds: int = 1,
                  required_kinds: tuple = (), unavailable_kinds: tuple = (),
                  unreachable_kinds: tuple = (),
                  require_source_read: bool = False) -> str:
    """1 巡分の査読プロンプト。orchestrator（確認）と evaluator（判定）の
    役割を文言で分ける——読み直し（`read_around`/`read_doc`/`list_docs`）は orchestrator が自分で
    必要箇所を確認する枠、最後の JSON は evaluator の判定。巡の入力は毎巡ここで組み直す（過去巡の
    全文は積まない・呼び出し元が最新の調査状態と未解決の指摘だけを渡す）。

    判定は根拠の**量**（引用の増分）ではなく、質問の型ごとに必要な根拠の**種別**が揃っているか
    で行う（`required_kinds`・平文ラベル）。`unavailable_kinds` はそのターンの登録範囲に存在
    しない種別＝「該当なし」として不足に数えない旨を、`unreachable_kinds` は範囲にはあるが
    今回の探す対象（層）では読めない種別＝確定させない旨を伝える。

    `require_source_read` が真のとき、判定の前に**必ず**ソース種別のファイル本文を自分で読むよう
    指示する（一覧取得・設計書の読取・空振りでは成立しない——成立条件は呼び出し元が機械的に
    判定する）。

    `claims_text`: worker の一次判断（`investigation_state.render_claims`）。
    `findings_text`: 前巡までの未解決の指摘（`investigation_state.render_findings`）。
    """
    prompt = (
        "あなたは調査の統括役（orchestrator）と評価役（evaluator）を兼ねます。"
        f"これは{total_rounds}巡中の{round_no}巡目の見直しです。回答本文は書かないこと。\n"
        f"【質問】\n{question}\n\n【収集済みの根拠（digest）】\n{digest or '(なし)'}\n\n")
    if required_kinds:
        prompt += (
            "【判定の基準＝必要な根拠の種別】\n"
            "根拠の件数や引用の増え方では判定しないでください。この質問には次の種別の根拠が"
            f"揃っている必要があります: {_kind_labels(required_kinds)}。"
            "設計書とソースが食い違う場合はソースを正とし、その食い違い自体を不足の観点として"
            "書いてください。\n")
        if unavailable_kinds:
            prompt += (
                f"次の種別は今回の範囲に存在しません: {_kind_labels(unavailable_kinds)}。"
                "これらは「該当なし」として扱い、不足には数えないでください。\n")
        if unreachable_kinds:
            prompt += (
                f"次の種別は範囲にはありますが、今回の探す対象では読めません: "
                f"{_kind_labels(unreachable_kinds)}。これらを理由に不足へ倒す必要はありませんが、"
                "確認できていない以上その点を確定として扱わないでください。\n")
        prompt += "\n"
    if claims_text:
        prompt += (
            "【下調べ役の一次判断（確定/推定/不明・鵜呑みにせず必要な箇所は自分で確認すること）】\n"
            f"{claims_text}\n\n")
    if findings_text:
        prompt += ("【前巡までの未解決の指摘（解決したものは今回の findings に含めない）】\n"
                   f"{findings_text}\n\n"
                   "括弧内が指摘の id です。同じ指摘を今回も出す場合は同じ id をそのまま使い"
                   "（内容だけ今回の表現へ更新してよい）、別の指摘には上記に無い未使用の id を"
                   "付けてください。上記の id を別の指摘に使い回さないでください。\n\n")
    prompt += (
        "まず統括役として、原文を自分で確かめるために、次のいずれかの JSON を"
        "1個だけ出力してください（他の文章を書かない）:\n"
        '{"action": "read_around", "doc_id": "…", "line": 行番号}\n'
        '{"action": "read_doc", "doc_id": "…", "start_line": 行番号}\n'
        '{"action": "list_docs", "path_prefix": "…"}\n')
    if require_source_read:
        prompt += (
            "この質問は必要な根拠の種別にソースを含みます。判定の前に**必ず** `read_around` か "
            "`read_doc` でソース種別のファイル（プログラム本体のコード）の本文を読んでください。"
            "一覧取得（`list_docs`）だけ・設計書だけの読取・行番号が範囲外で本文が空だった読取は"
            "確認したことになりません。下調べ役の一次判断に既にソースの根拠が付いていても、"
            "自分で本文を読むまで判定へ進まないでください。\n")
    prompt += (
        "確認を終えたら評価役として、別観点（反証・条件例外・回答漏れ・未探索の範囲）から"
        "判定し、次の JSON 1個だけを出力してください（他の文章を書かない・書き直しはしない）:\n"
        '{"verdict": "sufficient" | "insufficient" | "undecidable", '
        '"missing": "不足している観点を具体的に（十分なら空文字）", '
        '"missing_codes": ["insufficient"], '
        '"findings": [{"id": "f1", "claim_id": "c1", "text": "指摘", "refutes": true}]}\n'
        "`verdict` は `sufficient`（この根拠で正確に回答できる）／`insufficient`（不足がある）／"
        "`undecidable`（十分とも不足とも判断できない）のいずれか——判断できないものを"
        "`insufficient` や `sufficient` に丸めないこと。\n"
        "`missing_codes` は `missing` の分類（集計用・任意）——該当する軸があれば次の語彙から"
        "1つ以上選ぶ: source_missing=ソースを確認できていない、"
        "spec_missing=設計書を確認できていない、definition_missing=定義を確認できていない、"
        "log_missing=ログ・設定を確認できていない、callgraph_missing=呼出関係を確認できていない、"
        "not_found_in_scope=検索した範囲で見つからない、"
        "unexplored=未探索の範囲がある、insufficient=情報不足、conflict=資料間で矛盾、"
        "budget=打ち切り、unreadable=原本を読めない。無ければ空配列にする。"
        "この語彙以外の語・資料名・自由文は入れないでください。\n"
        "`findings` は主張 ID（`claim_id`）単位の指摘で、根拠と矛盾する主張には `refutes` を true に"
        "してください（その主張はこの巡で採用不可になります）。主張に紐づかない不足は `claim_id` を"
        "空文字にします。指摘が無ければ空配列にしてください。")
    return prompt


def rerun_instruction(missing: str, findings_text: str = "") -> str:
    """次巡の worker への指示（不足の軸＋未解決の指摘だけ・解決済みは呼び出し元が落とす）。"""
    out = f"【前回の調査で不足していた観点（重点的に調べ直す）】\n{missing}"
    if findings_text:
        out += f"\n\n【査読の未解決の指摘】\n{findings_text}"
    return out


# P1-a（Codex 強化計画 Phase1）: author 判定でも実行頭脳が Codex でない場合はファイルを作らず、
# 従来 qa 相当の下書きで回答する（HeuristicProvider/_GenProvider 共通で headline 冒頭に前置）。
_AUTHOR_FALLBACK_NOTE = "ファイル作成は頭脳=Codexのみ対応（設定で切替可）。以下は内容の下書きです。\n\n"

# STOP-1: 調査予算到達（`agentic_search._BUDGET_EXHAUSTED_STOP_REASONS`）で反復ツール検索が
# 打ち切られたとき（本文が空のまま終わった場合／本文はあるが根拠ゲートを自力で満たせない場合の
# 両方）、単発 grep へフォールバック（＝Evidence Packet ごと失う）せずにこの固定文言を headline
# として使う（追加 LLM 呼び出しをしない・平文・専門用語ゼロ）。本文とは別に
# `web/chat/render.js::budgetNoteHTML` が「範囲を絞る／続きを調べて」の案内を独立要素で表示する
# ため、ここでは事実（途中で打ち切ったこと）だけを述べる。
_BUDGET_EXHAUSTED_HEADLINE = "調査が上限に達したため、ここまでに確認できた内容のみをお伝えします。"

# 作成系（author）の根拠ゲート免除は「実際に成果物を登録できたターン」だけ。登録に至らなかった
# ターンは根拠0件のまま＝未検証の生成本文を回答として残さず、この固定文言へ差し替える
# （追加 LLM 呼び出しをしない・平文・専門用語ゼロ）。
_AUTHOR_NO_EVIDENCE_HEADLINE = ("必要な根拠を確認できなかったため、内容をお伝えできません。"
                                "範囲を変えるか、依頼を具体的にしてもう一度お試しください。")


# 作成系（author）の成果物ファイル名と marp 指定を、清書した本文を見て決めさせる 1 回の呼び出し。
# ツール呼び出しの方言に依存しない自前 JSON プロトコル（`claims_prompt` と同じ流儀）で、
# `agentic_search.write_output_file`（`_run_write_output_file`）へ渡す引数のうち content 以外
# （filename・marp）だけを決めさせる＝本文は既に確定しているので作り直させない。
_AUTHOR_OUTPUT_FILENAME_DEFAULT = "回答.md"


def author_output_prompt(question: str, body: str, max_body: int) -> str:
    """成果物として保存するファイル名と marp 指定を 1 個の JSON で返させるプロンプト。"""
    return (
        "あなたは作成の依頼に対する成果物を保存するファイル名を決める担当です。\n"
        f"【依頼】\n{question}\n\n"
        f"【保存する本文（先頭のみ）】\n{body[:max_body]}\n\n"
        "次の JSON 1個だけを出力してください（他の文章を書かない）:\n"
        '{"filename": "依頼の内容が分かる日本語のファイル名（拡張子つき・フォルダ区切りを含まない）", '
        '"marp": true か false}\n'
        "本文が Markdown のスライド形式（先頭に `---` で囲んだ `marp: true` の行がある）のときだけ "
        "`marp` を true にしてください（PDF と PowerPoint も自動生成されます）。"
        "それ以外は false にし、拡張子は本文の形式に合うもの（通常は .md）にしてください。")


# 作成系（author）の成果物をファイルとして登録できなかったときに本文の末尾へ付ける注記
# （本文自体は破棄しない・平文・専門用語ゼロ）。
def author_save_failed_note(reason: str) -> str:
    return f"\n\n※ 作成した内容をファイルとして保存できませんでした（{reason}）。上の本文はそのままご利用いただけます。"
