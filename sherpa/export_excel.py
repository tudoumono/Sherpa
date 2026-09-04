"""影響一覧の Excel 出力（15-初期MVP詳細.md §5.1・R7）。openpyxl で workspace/outputs に生成。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from openpyxl import Workbook

from sherpa.documents import resolve

HEADERS = ["種別", "対象", "経路", "根拠文書", "根拠DL"]


def build_xlsx(result: dict, path) -> Path:
    """run_impact の結果 → .xlsx。各影響行に 経路・根拠文書・根拠DL を付ける。

    K12（2026-09-04-グラフのソース正典化.md §4）: 「判定」（確実/要確認）「method」列は撤去
    （全件同格・機構ごと撤去）。「△推定」（grep 共起の推定）だけは経路列に明示して別枠のまま残す。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "影響一覧"
    ws.append(HEADERS)
    world = result.get("world_id") or result.get("world") or ""
    def _dl(docs):
        return "; ".join(f"/documents/download?world={quote(world)}&rel={quote(d)}"
                         for d in docs if resolve(d, world))

    for item in result["items"]:
        docs = sorted({e["doc"] for e in item.get("evidence", []) if e.get("doc")})
        ws.append([
            item["category"],
            item["name"],
            " → ".join(item.get("trace", [])),     # 経路＝ノード名列（鏡: item['trace']）
            "; ".join(docs),
            _dl(docs),
        ])
    # 構造的な影響が無いとき: 資料からの関連推定も出す（チャット/APIの二段回答と一致・RV Low）。
    for p in (result.get("presumed") or []):
        docs = sorted({e["doc"] for e in p.get("evidence", []) if e.get("doc")})
        quote_txt = next((e.get("quote", "") for e in p.get("evidence", [])), "")
        ws.append([p["category"], p["name"], f"△推定（要確認）: {quote_txt}", "; ".join(docs), _dl(docs)])
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out
