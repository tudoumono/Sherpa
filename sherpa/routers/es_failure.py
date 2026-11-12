"""全文検索の反映（ES の段）の失敗を、画面に出す平文へ直す部品。"""
from __future__ import annotations

# ES 反映の失敗理由（`es_index.index_world` の error 値）→ 利用者向けの平文。未知の値は内部の
# 例外名や接続先を含みうるため原文を出さず汎用文にする。
ES_ERROR_TEXT = {
    "embedding_cloud_unavailable": "検索に必要な AI に接続できませんでした",
    "embed_cache_write_failed": "一時データの保存に失敗しました（ディスクの空きを確認してください）",
    "delete_failed": "古い検索用データの入れ替えに失敗しました",
    "create_failed": "検索用データを作れませんでした（ディスクの空きを確認してください）",
    "bulk_failed": "検索用データを書き込めませんでした（ディスクの空きを確認してください）",
    "bulk_errors": "検索用データの一部を書き込めませんでした（ディスクの空きを確認してください）",
    "no_chunks": "検索に使う本文を準備できませんでした",
    "insufficient_space": "ディスクの空きが足りないため、検索の索引を作り直せませんでした。直前の索引はそのまま使えます",
    "analyzer_unavailable": "日本語の検索に使う部品（kuromoji）が使えないため、索引を作れませんでした。管理者に連絡してください",
    "alias_swap_unknown": "検索の索引の切り替えが終わったか確かめられませんでした。もう一度取り込むと確かめ直します。直前の索引は消していません",
}
# 旧索引に触れる前に打ち切られる失敗＝直前までの索引が残る。delete_failed は削除の成否を
# 証明できず、bulk 失敗は索引を空へ戻し、create 失敗・no_chunks は空のため「残る」と言えない。
ES_ERRORS_OLD_INDEX_KEPT = frozenset({"embedding_cloud_unavailable", "embed_cache_write_failed",
                                       "human_md_es_sig_marker_drop_failed", "insufficient_space",
                                       "alias_swap_unknown"})


def es_index_chunks(es: dict):
    """索引全体の切れ端の数（1 ファイルの再変換の記録は `index_chunks`・ほかは `chunks`）。"""
    return es.get("index_chunks", es.get("chunks"))


def es_succeeded(es) -> bool:
    return (isinstance(es, dict) and es.get("available") is True and not es.get("error")
            and (es_index_chunks(es) or 0) > 0)


def old_index_kept(earlier) -> bool:
    """最新の失敗より前の記録（新しい順）をたどり、索引に触れる前の失敗は読み飛ばし、最初に出会った
    それ以外が「件数 > 0 の成功」のときだけ true（索引を消す・状態が不確定な記録に先に出会えば false）。"""
    for a in earlier:
        if es_succeeded(a):
            return True
        err = a.get("error") if isinstance(a, dict) else None
        if not (isinstance(err, str) and err in ES_ERRORS_OLD_INDEX_KEPT):
            return False
    return False


def es_error_text(key: str, es: dict | None = None) -> str:
    """失敗理由の平文。空き不足で足りない量が記録にあるときは「あと約 X GB」を添える。"""
    text = ES_ERROR_TEXT.get(key, "原因は管理者向けの記録を確認してください")
    short = es.get("shortfall_bytes") if isinstance(es, dict) else None
    if key == "insufficient_space" and isinstance(short, (int, float)) and short > 0:
        text = text.replace("作り直せませんでした。", f"作り直せませんでした。あと約 {short / 1024 ** 3:.1f} GB 空けてください。", 1)
    return text
