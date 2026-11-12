"""会話の履歴の読み取り（直近ターンの質問と回答の完全な対を、全 provider へ別チャネルで渡す）。
設計: docs/design/chat.md「履歴の持ち越し」
"""
from __future__ import annotations

import logging

from . import store

_log = logging.getLogger("sherpa")

# 会話継続＝履歴 priming。追質問が前ターンを理解しない問題を避けるため、直近ターンの (user, assistant) 完全対を `Ctx.history` として全 provider に注入する（Codex ネイティブ resume は別経路）。
# トークン計測ツールが無いため、対数キャップ＋文字予算の二重キャップで近似する。
_HISTORY_TURNS = 6  # 直近 N 対（対数キャップ）
_HISTORY_MSG_CHARS = 1200  # 1 メッセージの上限文字数
_HISTORY_CHAR_BUDGET = 6000  # 履歴全体の文字予算

def _clip_history_msg(text: str) -> str:
    """履歴の 1 メッセージを上限文字数で切り詰める（先頭を残し末尾を落とす）。"""
    t = text or ""
    if len(t) <= _HISTORY_MSG_CHARS:
        return t
    return t[:_HISTORY_MSG_CHARS] + "…（省略）"


HISTORY_UNAVAILABLE_MESSAGE = ("前のやり取りを読み取れなかったため、今回は回答していません。"
                               "しばらくしてから、もう一度送ってください。")


class HistoryUnavailable(RuntimeError):
    """会話の履歴を読み取れなかった（履歴なしで続けず、ターンを失敗として終える）。"""


class _HistoryList(list):
    """`_history_pairs` の戻り値。`note`＝履歴を減らしたことの一言（Codex への文脈に添える・空なら無し）。"""

    note: str = ""


def _history_pairs(conversation_id) -> list[dict]:
    """直近ターンの (user, assistant) 完全対を `Ctx.history` 形式で返す。
    会話は交互とは限らない（途中停止・clarify・crash 補填）ため、user 行の直後（id 順）が assistant 行のときだけ対として採用し、不対行は捨てる。
    二重キャップ: 直近 `_HISTORY_TURNS` 対＋`_HISTORY_CHAR_BUDGET`（新しい対から積み、超える対は捨てる）。メッセージ単体も `_HISTORY_MSG_CHARS` で切る。
    呼び出しは `store.add_message(現在の user)` より前（in-flight の質問を履歴に含めない）。conversation_id が None・`_HISTORY_TURNS <= 0` は `[]`。読み取りに失敗したら `HistoryUnavailable`（前のやり取りを失ったまま答えない）。
    対数・文字予算で古い対を落としたときは、戻り値の `note` にその一言を持たせる。
    """
    if conversation_id is None or _HISTORY_TURNS <= 0:
        return []
    try:
        limit = min(512, _HISTORY_TURNS * 2 + 8)
        while True:
            rows = store.recent_messages(conversation_id, limit=limit)
            pairs = []
            i = 0
            while i < len(rows) - 1:
                if rows[i]["role"] == "user" and rows[i + 1]["role"] == "assistant":
                    pairs.append((rows[i], rows[i + 1]))
                    i += 2
                else:
                    i += 1
            # 不対行が堆積すると古い完全対が窓の外に押し出されるため、取得行数が limit に張り付いている間は窓を段階的に広げて再取得する。512 行で打ち切る（best-effort・全履歴走査はしない）。
            if len(pairs) >= _HISTORY_TURNS or len(rows) < limit or limit >= 512:
                break
            limit = min(512, limit * 4)
        found_pairs = len(pairs)
        window_full = len(rows) >= limit  # 取得窓が満杯＝窓より古いやり取りが残っている
        pairs = pairs[-_HISTORY_TURNS:]  # 直近 N 対（対数キャップ）
        kept = []
        budget = _HISTORY_CHAR_BUDGET
        for u, a in reversed(pairs):  # 新しい対から積む
            u_txt, a_txt = _clip_history_msg(u["content"]), _clip_history_msg(a["content"])
            cost = len(u_txt) + len(a_txt)
            if cost > budget:  # 文字予算超過＝この対（とこれより古い対）は捨てる
                break
            budget -= cost
            kept.append((u_txt, a_txt))
        out = _HistoryList()
        for u_txt, a_txt in reversed(kept):  # 時系列順（古→新）に戻す
            out.append({"role": "user", "content": u_txt})
            out.append({"role": "assistant", "content": a_txt})
        dropped = found_pairs - len(kept)
        note = ""
        if dropped > 0:
            note = f"（これより前のやり取り{dropped}対は、長さの上限のため以下に含めていません）"
        if window_full:
            note += "（さらに古いやり取りが残っている可能性がありますが、読み取りの上限のため読んでいません）"
        out.note = note
        return out
    except Exception as e:
        _log.warning("会話履歴を読み取れませんでした: %s", type(e).__name__)
        raise HistoryUnavailable(HISTORY_UNAVAILABLE_MESSAGE) from e
