"""思考プロバイダ共通のプロンプト生成。

`_facts`/`_answer_prompt`（取得済み事実の整形と回答プロンプト）・`_kb_hint`/`_kb_hint_abs`（Codex への KB パス案内）・
`_PLAIN_PROMPT*`（社内資料参照オフ時の素プロンプト）を集約する。
設計: docs/design/chat.md「文脈と構成」
"""
from __future__ import annotations

from pathlib import Path

# 全利用者共通の固定の回答方針。
ANSWER_POLICY = (
    "資料を根拠に答え、根拠は資料のパス（必要なら箇所）で示してください。"
    "資料に無いことを補うときは『推定』と明示し、確定した事実と分けて書いてください。"
)


def _digest_limit_lines(env: dict) -> str:
    """清書ダイジェストの「調査の限界」行（0件・打ち切り・上限到達）だけを取り出す。"""
    digest = env.get("_synthesis_digest") or ""
    limits = [ln for ln in digest.splitlines() if ln.startswith("調査の限界: ")]
    return ("\n" + "\n".join(limits)) if limits else ""


# グラフを引けていないターンの影響調査へ渡す指示（0 件は「影響が無い」ではなく「確認できていない」）。
_GRAPH_DEGRADED_IMPACT_STEER = (
    "。ただし関係のつながり（COPY/CALL/参照）は確認できていないため、"
    "構造的な影響の有無は不明であり「影響は無い」と断定しないこと"
    "（資料とソースを直接確認して分かった範囲だけを答える）。")


def _facts(lens: str, env: dict) -> str:
    """取得済みの事実を LLM への根拠として簡潔に整形する（ここに無いことは書かせない）。

    個人ファイルの事実（`_personal_facts`）は本人の回答にだけ末尾追記する。
    qa は `_synthesis_digest` があればそれを使い、無ければ先頭4引用×60字。
    troubleshoot／構造データありの impact は限界行だけ `_digest_limit_lines` で連結する。
    """
    d = env.get("data", {})
    # 引用（citations）か `_synthesis_digest` を持ち、グラフ結果（items/presumed）が無い impact は引用ベース（qa と同じ）の整形へ倒す。
    _orig_lens = lens  # 倒した後も元のレンズで足す指示があるため退避する
    if (lens == "impact" and not d.get("items") and not d.get("presumed")
            and (d.get("citations") or env.get("_synthesis_digest"))):
        lens = "qa"
    # 原因候補（candidates）を持たない縮退 env の troubleshoot も引用ベースの整形へ倒す。
    if (lens == "troubleshoot" and not d.get("candidates")
            and (d.get("citations") or env.get("_synthesis_digest"))):
        lens = "qa"
    if lens == "impact":
        items = d.get("items", [])
        s = env.get("summary", {})
        # 起点が解決できないときは内部値 `None` を出さず、起点なしの言い回しにする。
        _start = d.get("start")
        origin = f"起点『{_start}』の" if _start else "変更対象の"
        # 構造的な影響が0でも「コードが無い」と断定せず、変更対象と影響先の接続（経路）の確認へ誘導する。
        steer = ("。この起点では構造的なコードの波及は無い＝**変更対象（起点）と影響先の接続"
                 "（COPY/CALL/参照の経路）が辿れるか**を、資料の検索（仕様問い合わせ・トラブルシュート）や"
                 "関係グラフで確認するよう勧めること（症状語をそのまま探さない・フォルダにコードが無いとは断定しない）。")
        if env.get("graph_degraded"):
            # 関係グラフを引けていないターン。
            steer = _GRAPH_DEGRADED_IMPACT_STEER
        if items:
            names = "、".join(f"{i['name']}({i['category']})" for i in items[:12])
            rest = f"（先頭12件・残り {len(items) - 12} 件は未提示）" if len(items) > 12 else ""
            base = f"{origin}影響: 計{s.get('total', 0)}件。対象: {names}{rest}"
        else:
            presumed = d.get("presumed", [])  # 構造的な影響0件でも資料からの関連推定があれば必ず伝える
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
        from ..agentic_search import _redact  # grep 根拠本文も秘匿する
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
        # 該当箇所を持つ縮退 impact には影響レンズ用の「断定しない」指示を足す。
        base += _GRAPH_DEGRADED_IMPACT_STEER
    # 主張構造はレンズに関わらず必ず付加する。
    claims_digest = env.get("_claims_digest")
    if claims_digest:
        base += f"\n\n【主張の構造（確定/推定/不明）】\n{claims_digest}"
    # 必要な根拠の種別が揃わなかったターンの注記は、主張構造の有無に関わらず必ず渡す。
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
            "『X ごとに Y』のような親子二段の網羅要求は、取得済みの事実に親項目の集合（件数を含む）が"
            "そろっているか、その上で各親に子が揃っているかを確認し、欠けている親・子があれば具体的に"
            "明示する（欠けたまま『全件』と断定しない）。"
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


def _kb_hint(world: str) -> str:
    from .. import worlds  # fixtures 案内は `_fixtures()` ゲートに統一する
    base = f"fixtures/corpus/{world}" if worlds._fixtures() else f"data/kb/*/{world}"
    return f"{base}/md（設計書・仕様の決定的MD）と {base}/src（COBOL/JCL/コピーブック原文）"


def _kb_hint_abs(world: str, layout_hint: bool = True) -> str:
    """Codex の cwd は workspace/authoring/ のため、KB を絶対パスで指示する。
    fixtures モードなら fixtures、そうでなければ実 world registry の root を使う。
    `layout_hint=False` はパスだけを返す。
    """
    from .. import worlds
    repo_root = Path(__file__).resolve().parents[2]
    if worlds._fixtures():
        base = repo_root / "fixtures" / "corpus" / world
        if not layout_hint:
            return str(base)
        return f"{base}（設計書・仕様の決定的MD は {base}/md/、COBOL/JCL は {base}/src/）"
    # 実登録 world からパスを解決する（見つからなければ data/kb 以下全体）。
    try:
        wd = worlds.world_dir(world)
        if wd:
            if not layout_hint:
                return str(wd)
            return f"{wd}（設計書・仕様の決定的MD は {wd}/md/、COBOL/JCL は {wd}/src/）"
    except Exception:
        pass
    base = repo_root / "data" / "kb"
    if not layout_hint:
        return f"{base}/**/{world}/"
    return f"{base}/**/{world}/（設計書・仕様の決定的MD は md/ 配下、COBOL/JCL は src/ 配下）"


# 社内資料参照オフのときの素のプロンプト（事実を渡さない＝出典なしの一般回答）。
_PLAIN_PROMPT = ("あなたは親切な日本語アシスタントです。社内資料は参照していません。"
                 "一般的な知識の範囲で答えてください。**出典・社内資料・ファイル名・引用に基づくとは言わない**。"
                 "資料に基づく確認が必要なら『社内資料をオンにしてください』と促す。\n\n【質問】{q}\n回答:")

# 個人ファイルのヒットを plain プロンプトに注入するテンプレート。
_PLAIN_PROMPT_WITH_PERSONAL = (
    "あなたは親切な日本語アシスタントです。社内資料は参照していませんが、"
    "ユーザー本人がアップロードした個人ファイルのヒットが以下にあります。これを根拠に答えてください（長さは絞らない）。"
    "**他のユーザーには共有されない個人データです**。個人ファイルは出典として一覧に出さない"
    "（共有 RAG の引用元とは別扱い）。\n\n"
    "【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n{personal}\n\n【質問】{q}\n回答:")


# 下調べを省いたレンズ（`_PRESEARCH_SKIP_LENSES`）は、`_gather` がこの headline を持つ env を返す。Codex が回答できれば上書きされる。
_NO_PRESEARCH_HEADLINE = "回答をまとめられませんでした。もう一度お試しください。"
