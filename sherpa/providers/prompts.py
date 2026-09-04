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


def _facts(lens: str, env: dict) -> str:
    """取得済みRAGの事実を LLM への根拠として簡潔に整形（ここに無いことは書かせない）。

    Feature B: env["_personal_facts"] がある場合は末尾に追記（本人のみ参照・共有 RAG には入れない）。
    """
    d = env.get("data", {})
    # 影響調査（impact）も反復ツール検索の対象になった（2026-08-15）。その経路の env は
    # グラフ由来の items/start ではなく**引用（citations）**を持つため、グラフ用の文面を当てると
    # 「起点『None』の影響は計0件」のように、利用者に見せない内部値がそのまま出てしまう（実測）。
    # データの形で判断し、グラフ結果が無ければ引用ベースの整形（qa と同じ）へ倒す。
    if lens == "impact" and not d.get("items") and not d.get("presumed") and d.get("citations"):
        lens = "qa"
    if lens == "impact":
        items = d.get("items", [])
        s = env.get("summary", {})
        # 起点が解決できなかった場合に `None` をそのまま文面へ出さない（実測 2026-08-15）。
        # 起点なしの言い回しへ切り替える（利用者に内部値を見せない）。
        _start = d.get("start")
        origin = f"起点『{_start}』の" if _start else "変更対象の"
        # 構造的な影響が0＝この起点ではコード波及なし→検索へ誘導（フォルダにコードが無いと断定しない・RV Low）。
        # F1（2026-07-07）: 症状語を追わず、変更対象（起点）と影響先の「接続（経路）」の確認へ誘導する。
        steer = ("。この起点では構造的なコードの波及は無い＝**変更対象（起点）と影響先の接続"
                 "（COPY/CALL/参照の経路）が辿れるか**を、資料の検索（仕様問い合わせ・トラブルシュート）や"
                 "関係グラフで確認するよう勧めること（症状語をそのまま探さない・フォルダにコードが無いとは断定しない）。")
        if items:
            names = "、".join(f"{i['name']}({i['category']})" for i in items[:12])
            base = f"{origin}影響: 計{s.get('total', 0)}件。対象: {names}"
        else:
            presumed = d.get("presumed", [])               # 構造的な影響0件でも資料からの関連推定があれば必ず伝える（0で突き放さない・RV High）
            if presumed:
                pn = "、".join(f"{p['name']}({p['category']})" for p in presumed[:12])
                base = (f"{origin}確実な依存は見つからなかったが、資料からの関連（推定・要確認）が{len(presumed)}件: {pn}。"
                        "これらは推定であり確実ではない旨を明記すること" + steer)
            else:
                base = f"{origin}影響: 計0件（該当なし）" + steer
        return base + (env.get("_personal_facts") or "")
    if lens == "troubleshoot":
        from ..agentic_search import _redact            # grep 根拠本文も秘匿（ES は redact 済み・base grep の password/api_key 等を外部LLMへ流さない・RV High）
        cs = d.get("candidates", [])
        parts = []
        for c in cs[:8]:
            ev = c.get("evidence", {}) or {}
            qs = [_redact(g.get("text", ""))[:80] for g in ev.get("grep", [])[:2] if g.get("text")]
            parts.append(f"{c['name']}({c.get('role', '')})" + (f" 根拠「{' / '.join(qs)}」" if qs else ""))
        base = "原因候補: " + "、".join(parts) if parts else "原因候補なし"
        return base + (env.get("_personal_facts") or "")
    cites = d.get("citations", [])
    base = "該当箇所: " + " / ".join(f"{c['doc_id']}「{(c.get('quote') or '')[:60]}…」" for c in cites[:4]) if cites else "該当なし"
    return base + (env.get("_personal_facts") or "")


def _answer_prompt(message, lens, env):
    return ("あなたは社内ナレッジの回答アシスタントです。以下の『取得済みの事実』だけを根拠に、"
            "日本語で簡潔に（2〜4文）回答してください。事実に無いことは書かない（推測しない）。"
            "件数や対象名は事実のまま。出典の列挙は不要（別途付与）。"
            "回答は Markdown（太字・箇条書き・インラインコード）で書いてよい。"
            f"\n\n【質問】{message}\n【取得済みの事実】{_facts(lens, env)}\n\n回答のみ:")


def _kb_hint(world: str) -> str:
    from .. import worlds                                       # fixtures 案内は **_fixtures() ゲートに統一**（"0"/"false" を誤って truthy にしない・RV High）
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
                 "一般的な知識の範囲で簡潔に答えてください。**出典・社内資料・ファイル名・引用に基づくとは言わない**。"
                 "資料に基づく確認が必要なら『社内資料をオンにしてください』と促す。\n\n【質問】{q}\n回答:")

# HIGH-1 fix: 個人ファイルのヒットを plain プロンプトに注入するテンプレート。
_PLAIN_PROMPT_WITH_PERSONAL = (
    "あなたは親切な日本語アシスタントです。社内資料は参照していませんが、"
    "ユーザー本人がアップロードした個人ファイルのヒットが以下にあります。これを根拠に簡潔に答えてください。"
    "**他のユーザーには共有されない個人データです**。出典の列挙は不要（別途付与）。\n\n"
    "【個人ファイル内ヒット（本人のみ参照可・共有不可）】\n{personal}\n\n【質問】{q}\n回答:")

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
