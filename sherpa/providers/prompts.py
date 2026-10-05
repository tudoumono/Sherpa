"""思考プロバイダ共通のプロンプト生成。

`_kb_hint_abs`（Codex への KB パス案内）を集約する。
設計: docs/design/chat.md「文脈と構成」
"""
from __future__ import annotations

from pathlib import Path

# 全利用者共通の固定の回答方針。
ANSWER_POLICY = (
    "資料を根拠に答え、根拠は資料のパス（必要なら箇所）で示してください。"
    "資料に無いことを補うときは『推定』と明示し、確定した事実と分けて書いてください。"
)


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


# 下調べを省いたレンズ（`_PRESEARCH_SKIP_LENSES`）は、`_gather` がこの headline を持つ env を返す。Codex が回答できれば上書きされる。
_NO_PRESEARCH_HEADLINE = "回答をまとめられませんでした。もう一度お試しください。"
