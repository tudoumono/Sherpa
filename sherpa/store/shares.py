"""会話共有と sanitized snapshot。
sanitized snapshot（`create_sanitized_snapshot` → `_safe_share_answer`）は allowlist で再構築し、通常の受領共有の読取（`get_conversation_for_read` → `_strip_shared_message`）は denylist で伏せる。重要度設定ファイルへの参照の除外は両経路が同じ共有ヘルパで独立に行う。
`accept_share` と `delete_conversation`（conversations.py）は同じ conversations 行を `SELECT ... FOR UPDATE` でロックして直列化する。
設計: docs/design/chat.md「共有」
"""
from __future__ import annotations

import hashlib
import math

from psycopg.types.json import Json

from urllib.parse import parse_qs, urlsplit

from .. import answer_shape
from .. import citations as citations_mod
from .. import stop_kind as stop_kind_mod
from ..ingest import importance
from .conversations import (
    SHARE_DEFAULT_EXPIRY_DAYS, SHARE_EFFECTIVE_EXPIRES_SQL, is_personal_tainted, _resolve_received_share_msg_src,
)
from .db import _connect, _ensure
from .feedback import get_feedback_by_message_ids_for_user
from .usage import _USAGE_LIMIT_BOOL_FIELDS, _USAGE_LIMIT_INT_FIELDS


def _share_lock_key(share_id) -> int:
    """`accept_share`/`refresh_sanitized_share` 共通の advisory lock key。
    両関数は conversation 行と conversation_shares 行を逆順にロックするため、両関数の先頭（行ロックより前）でこの xact lock を取って直列化する。share_id を sha1 で 64bit キー空間へ写像する。
    """
    return int.from_bytes(hashlib.sha1(f"share_fork_refresh:{share_id}".encode("utf-8")).digest()[:8],
                          "big", signed=True)


def _filter_importance_from_citations(cites):
    """citation 形（`{doc_id, ...}`）のリストから、重要度設定ファイル自体を指す要素を除外する（`sources[]` と `data.citations[]` が共用）。リストでなければそのまま返す。"""
    if not isinstance(cites, list):
        return cites
    return [c for c in cites
           if not (isinstance(c, dict) and importance.is_importance_control_path(c.get("doc_id") or ""))]


def _filter_importance_from_edges(edges):
    """graph edge 形（`{doc, ...}`）のリストから、重要度設定ファイル自体を来歴に持つ要素を除外する（troubleshoot 候補の `evidence.edges[]` 用）。リストでなければそのまま返す。"""
    if not isinstance(edges, list):
        return edges
    return [e for e in edges
           if not (isinstance(e, dict) and importance.is_importance_control_path(e.get("doc") or ""))]


def _filter_importance_from_candidates(candidates):
    """troubleshoot レンズの候補（`data.candidates[]`）から重要度設定ファイルへの参照を落とす。候補自身が対象なら丸ごと落とし、残った候補の `evidence.grep`/`evidence.edges` も個別に判定する。リストでなければそのまま返す。"""
    if not isinstance(candidates, list):
        return candidates
    out = []
    for cand in candidates:
        if not isinstance(cand, dict):
            out.append(cand)
            continue
        if importance.is_importance_control_path(cand.get("name") or ""):
            continue
        ev = cand.get("evidence")
        if isinstance(ev, dict):
            new_ev = dict(ev)
            if isinstance(ev.get("grep"), list):
                new_ev["grep"] = _filter_importance_from_citations(ev["grep"])
            if isinstance(ev.get("edges"), list):
                new_ev["edges"] = _filter_importance_from_edges(ev["edges"])
            if new_ev != ev:
                cand = {**cand, "evidence": new_ev}
        out.append(cand)
    return out


def _filter_importance_from_impact_items(items):
    """impact レンズの `items[]`/`presumed[]` から、重要度設定ファイルを来歴に持つ evidence だけを落とす（要素自体は残す）。リストでなければそのまま返す。"""
    if not isinstance(items, list):
        return items
    out = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("evidence"), list):
            out.append(it)
            continue
        filtered_ev = _filter_importance_from_edges(it["evidence"])
        out.append({**it, "evidence": filtered_ev} if filtered_ev != it["evidence"] else it)
    return out


def _intersect_sources_verified(sv, sources):
    """`sources_verified`（精読済み doc_id の集合）を、残った `sources[]` の doc_id 集合と再交差する。`sv` がリストでなければそのまま返す。"""
    if not isinstance(sv, list):
        return sv
    survived_ids = {s.get("doc_id") for s in sources if isinstance(s, dict)} if isinstance(sources, list) else set()
    return sorted(d for d in sv if isinstance(d, str) and d in survived_ids)


def _redact_importance_from_answer_data(data):
    """`answer.data` から重要度設定ファイル由来の citation/evidence 参照を落とす（`data.citations`・Evidence Packet の `source_path`/`matched_doc_ids`・troubleshoot 候補・impact の items/presumed）。`_safe_share_answer` と `_strip_shared_message` が共用する。`data` が dict でなければそのまま返す。"""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if isinstance(out.get("citations"), list):
        out["citations"] = _filter_importance_from_citations(out["citations"])
    if isinstance(out.get("evidence_packet"), dict):
        out["evidence_packet"] = _safe_evidence_packet(out["evidence_packet"])
    if isinstance(out.get("candidates"), list):
        out["candidates"] = _filter_importance_from_candidates(out["candidates"])
    if isinstance(out.get("items"), list):
        out["items"] = _filter_importance_from_impact_items(out["items"])
    if isinstance(out.get("presumed"), list):
        out["presumed"] = _filter_importance_from_impact_items(out["presumed"])
    if isinstance(out.get("claims"), list):
        # 区分（確定/推定/不明）と理由コードを allowlist で再構築する。
        out["claims"] = [x for x in (_safe_claim(c) for c in out["claims"]) if x is not None]
    if isinstance(out.get("reconciliation"), list):
        out["reconciliation"] = [x for x in (_safe_reconciliation_row(r) for r in out["reconciliation"]) if x is not None]
    return out


def _strip_shared_message(m: dict) -> dict:
    """受領共有の read path で内部情報を落とす（route/trace は常に NULL・answer 内の question/route/trace も除去）。
    所有者本人の read には使わない（呼び出し側が origin='received_share' のときだけ適用する）。
    """
    out = {**m, "route": None, "trace": None}
    a = out.get("answer")
    # usage・usage_sub・usage_subs・codex_usage_total・tool_use・call_stats も内部情報として受領共有では伏せる。
    _drop = ("question", "route", "trace", "usage", "usage_sub", "usage_subs", "codex_usage_total",
             "trimmed", "tool_use", "call_stats")
    if isinstance(a, dict):
        needs_copy = any(k in a for k in _drop)
        # 通常の受領共有は元会話の answer をそのまま読むため、`sources[]`・`data.citations[]`・Evidence Packet の `source_path`/`matched_doc_ids` から重要度設定ファイルへの参照をここでも落とす。
        srcs = a.get("sources")
        filtered_sources = _filter_importance_from_citations(srcs) if isinstance(srcs, list) else srcs
        if isinstance(srcs, list) and filtered_sources != srcs:
            needs_copy = True
        # フィルタで sources から消えた doc_id を sources_verified からも外す。
        sv = a.get("sources_verified")
        filtered_sv = _intersect_sources_verified(
            sv, filtered_sources if isinstance(srcs, list) else srcs) if isinstance(sv, list) else sv
        if isinstance(sv, list) and filtered_sv != sv:
            needs_copy = True
        data = a.get("data")
        filtered_data = _redact_importance_from_answer_data(data) if isinstance(data, dict) else data
        if isinstance(data, dict) and filtered_data != data:
            needs_copy = True
        new_fields = _share_new_shape_fields(a)
        if any(k in a for k in answer_shape.NEW_SHAPE_KEYS) and any(a.get(k) != new_fields.get(k) for k in answer_shape.NEW_SHAPE_KEYS):
            needs_copy = True
        # 調査の記録は会話の持ち主だけが取れるため、受領共有の読者には「記録あり」の旗を見せない。
        inv = a.get("investigation")
        if isinstance(inv, dict) and "recorded" in inv:
            needs_copy = True
        if needs_copy:
            new_a = {k: v for k, v in a.items() if k not in _drop and k not in answer_shape.NEW_SHAPE_KEYS}
            new_a.update(new_fields)
            if isinstance(inv, dict) and "recorded" in inv:
                new_a["investigation"] = {k: v for k, v in inv.items() if k != "recorded"}
            if isinstance(srcs, list):
                new_a["sources"] = filtered_sources
            if isinstance(sv, list):
                new_a["sources_verified"] = filtered_sv
            if isinstance(data, dict):
                new_a["data"] = filtered_data
            out["answer"] = new_a
    return out


def get_conversation_for_read(uid, cid) -> dict | None:
    """current user が読める会話（所有 or 有効な受領共有）。読めなければ None、無効共有は share_status を付す。
    受領共有はメッセージを `source_conversation_id` から返すが、`conversation.id` は wrapper（cid）を維持する。
    """
    _ensure()
    with _connect() as c:
        conv = c.execute(
            "SELECT id, user_id, version, title, codex_session_id, origin, source_conversation_id, "
            "share_id, shared_by_user_id, read_only, contains_personal_workspace, "
            "forked_from_share_id, forked_from_user_id, forked_at, created_at, updated_at "
            "FROM conversations WHERE id=%s AND deleted_at IS NULL", (cid,)).fetchone()
        if not conv or conv["user_id"] != uid:
            return None
        msg_src = conv["id"]
        if conv["origin"] == "received_share":
            # 本文は元会話から都度読む（`search_conversations` と同じ判定を共用）。
            msg_src, status = _resolve_received_share_msg_src(
                c, uid, conv["share_id"], conv["source_conversation_id"])
            if status is not None:
                return {"conversation": conv, "messages": [], "share_status": status}
        msgs = c.execute(
            "SELECT id, role, content, lens, route, trace, answer, created_at FROM messages "
            "WHERE conversation_id=%s ORDER BY id", (msg_src,)).fetchall()
    # フィードバック取得は自前で別の `_connect()` を取るため、with ブロックを抜けて 1 本目の接続を返却してから呼ぶ。
    # 読者（uid）自身のフィードバックだけを assistant メッセージに同梱する（受領共有の閲覧者には所有者の分が出ない）。
    fb_map = get_feedback_by_message_ids_for_user(
        [m["id"] for m in msgs if m["role"] == "assistant"], uid)
    # `route`/`trace` と同様、キーは常に存在し無ければ null。
    msgs = [{**m, "feedback": fb_map.get(m["id"])} for m in msgs]
    if conv["origin"] == "received_share":
        # 受領共有は答え/出典だけ見せる。route/trace と確認カード（answer.question）は内部情報を含むため常に落とす。
        msgs = [_strip_shared_message(m) for m in msgs]
    return {"conversation": conv, "messages": msgs}


# sanitized share snapshot（個人部分を除いた共有）: allowlist 再構築＋ターンごとの個人フラグで作る。個人ターン（messages.personal）は Q/A とも伏字。
_REDACTED_TEXT = "（個人ファイルを参照した回答のため、共有では非表示にしています）"
# ファイルを作成したターンは作成物（個人の作業領域）を含み得るため伏せる。理由が個人ファイルの参照とは別なので文言を分ける。
_REDACTED_FILES_TEXT = "（ファイルを作成した回答のため、共有では非表示にしています）"
_REDACTED_TEXTS = (_REDACTED_TEXT, _REDACTED_FILES_TEXT)
_SANITIZED_TITLE = "共有用（サニタイズ済み会話）"
_SHARE_SAFE_LENS = ("investigate", "qa", "impact", "troubleshoot", "chat", "clarify", "author")


_EVIDENCE_PACKET_STR_FIELDS = ("task_id", "investigation_status", "summary", "stop_reason", "next_action")
_EVIDENCE_PACKET_INT_FIELDS = ("candidates_seen", "candidates_inspected", "evidence_selected")
_EVIDENCE_ITEM_STR_FIELDS = ("evidence_id", "source_type", "source_path", "verification_method")
# 主張（`data.claims[]`）の共有用 allowlist フィールド（`evidence_refs` は `ev-N` 等の参照文字列のみ）。
_CLAIM_STR_FIELDS = ("id", "status", "text", "reason", "reason_code")
_LOCATOR_PART_MAX = 200

# bbox の要素上限（巨大値による表示崩れ防止）。
_LOCATOR_BBOX_ABS_MAX = citations_mod._LOCATOR_NUMBER_MAX


def _safe_locator(loc) -> dict | None:
    """`source_span` が構造化 locator（`sherpa/ingest/evidence_ir.py::Locator` 由来）のときの allowlist 再構築。
    既知キー（`page`/`slide`/`sheet`/`cell_range`/`part`/`object_id`/`bbox`）と既知の値型だけを通し、`extension` は通さない。検証は citations.py の `_clean_locator_field`/`_clean_locator_number` を再利用する。`bbox` は数値4要素・有限・絶対値上限まで。未知キー・型不一致はキーごと落とし、有効なキーが無ければ None。
    """
    if not isinstance(loc, dict):
        return None
    out = {}
    for k in ("page", "slide"):
        v = citations_mod._clean_locator_number(loc.get(k))
        if v is not None:
            out[k] = v
    for k in ("sheet", "cell_range"):
        v = citations_mod._clean_locator_field(loc.get(k))
        if v is not None:
            out[k] = v
    part = citations_mod._clean_locator_field(loc.get("part"), limit=_LOCATOR_PART_MAX)
    if part is not None:
        out["part"] = part
    object_id = loc.get("object_id")
    if isinstance(object_id, bool):
        pass  # bool は int のサブクラスのため先に弾く。
    elif isinstance(object_id, str):
        v = citations_mod._clean_locator_field(object_id, limit=_LOCATOR_PART_MAX)
        if v is not None:
            out["object_id"] = v
    elif isinstance(object_id, int):
        v = citations_mod._clean_locator_number(object_id)
        if v is not None:
            out["object_id"] = v
    bbox = loc.get("bbox")
    if (isinstance(bbox, (list, tuple)) and len(bbox) == 4
            and all(_valid_bbox_component(x) for x in bbox)):
        out["bbox"] = list(bbox)
    return out or None


def _valid_bbox_component(x) -> bool:
    """bbox 1要素の検証。巨大整数で `math.isfinite` が `OverflowError` を出さないよう、絶対値の上限比較を先に行う。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if abs(x) > _LOCATOR_BBOX_ABS_MAX:
        return False
    return not isinstance(x, float) or math.isfinite(x)


def _safe_share_list_meta(lm) -> dict | None:
    """list_docs 集計 Evidence の `list_meta`（総件数・条件・列挙範囲）の共有用 allowlist（既知フィールド・既知の型のみ）。"""
    if not isinstance(lm, dict):
        return None
    out = {}
    for k in ("count", "shown"):
        v = lm.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    for k in ("prefix", "pattern", "doctype", "state"):
        v = lm.get(k)
        if isinstance(v, str):
            out[k] = v
    return out or None


def _safe_share_card_meta(cm) -> dict | None:
    """graph カード Evidence の `card_meta`（対象名・関係・カテゴリ・経路）の共有用 allowlist。"""
    if not isinstance(cm, dict):
        return None
    out = {}
    for k in ("name", "role", "category"):
        v = cm.get(k)
        if isinstance(v, str):
            out[k] = v
    path = cm.get("path")
    if isinstance(path, list) and all(isinstance(p, str) for p in path):
        out["path"] = list(path)
    edges = cm.get("edges")
    if isinstance(edges, list) and all(isinstance(e, str) for e in edges):
        out["edges"] = list(edges)
    return out or None


def _safe_evidence_item(e: dict) -> dict:
    """Evidence Packet の `evidence[]` 1件を型検証しながら再構築する。
    文字列フィールドは str のみ、`used` は厳密に bool のときだけ通す。`source_span` は行番号2要素または構造化 locator のどちらかで、それ以外の形はキーごと落とす。
    `matched_doc_ids`（str のリスト）・`list_meta`・`card_meta` は型検証済みの形で保持する。`source_path`/`matched_doc_ids` は重要度設定ファイルを指す doc_id を出さない。
    """
    out = {}
    for k in _EVIDENCE_ITEM_STR_FIELDS:
        if k in e and (e[k] is None or isinstance(e[k], str)):
            v = e[k]
            if k == "source_path" and isinstance(v, str) and importance.is_importance_control_path(v):
                v = None
            out[k] = v
    if "used" in e and isinstance(e["used"], bool):
        out["used"] = e["used"]
    if "source_span" in e:
        span = e["source_span"]
        if span is None:
            out["source_span"] = None
        elif (isinstance(span, (list, tuple)) and len(span) == 2
              and all(x is None or (isinstance(x, int) and not isinstance(x, bool)) for x in span)):
            out["source_span"] = list(span)
        else:
            loc = _safe_locator(span)
            if loc is not None:
                out["source_span"] = loc
    matched = e.get("matched_doc_ids")
    if isinstance(matched, list) and all(isinstance(d, str) for d in matched):
        out["matched_doc_ids"] = [d for d in matched if not importance.is_importance_control_path(d)]
        lm = _safe_share_list_meta(e.get("list_meta"))
        if lm is not None:
            out["list_meta"] = lm
        cm = _safe_share_card_meta(e.get("card_meta"))
        if cm is not None:
            out["card_meta"] = cm
    return out


def _safe_claim(c) -> dict | None:
    """主張1件（`data.claims[]`）を既知フィールド・既知の型のみで再構築する。`id`/`status`/`text` を欠く・型が合わなければ None（行ごと落とす）。"""
    if not isinstance(c, dict):
        return None
    out = {}
    for k in _CLAIM_STR_FIELDS:
        if isinstance(c.get(k), str):
            out[k] = c[k]
    if not {"id", "status", "text"} <= out.keys():
        return None
    refs = c.get("evidence_refs")
    if isinstance(refs, list) and all(isinstance(r, str) for r in refs):
        out["evidence_refs"] = refs
    # 根拠種別が未申告（None）ならキーごと落とす。
    kinds = c.get("evidence_kinds")
    if isinstance(kinds, list) and all(isinstance(k, str) for k in kinds):
        out["evidence_kinds"] = kinds
    return out


_RECON_VERDICTS = ("match", "mismatch", "spec_missing", "source_missing", "unverified")


def _safe_reconciliation_side(side) -> dict:
    """照らし合わせの片側（`{text, doc_id?, line?}`）を既知の形だけで再構築する。重要度設定ファイルを指す参照は落とす。"""
    out = {"text": side["text"] if isinstance(side, dict) and isinstance(side.get("text"), str) else ""}
    if isinstance(side, dict) and isinstance(side.get("doc_id"), str) and side["doc_id"] \
            and not importance.is_importance_control_path(side["doc_id"]):
        out["doc_id"] = side["doc_id"]
        line = side.get("line")
        if isinstance(line, int) and not isinstance(line, bool) and line > 0:
            out["line"] = line
            end = side.get("line_end")
            if isinstance(end, int) and not isinstance(end, bool) and end > line:
                out["line_end"] = end
    return out


def _safe_reconciliation_row(r) -> dict | None:
    """設計書とソースの照らし合わせ1行（`data.reconciliation[]`）を既知フィールドだけで再構築する。`item`／`verdict` が合わなければ None（行ごと落とす）。"""
    if not isinstance(r, dict) or not isinstance(r.get("item"), str) or r.get("verdict") not in _RECON_VERDICTS:
        return None
    return {"item": r["item"], "verdict": r["verdict"],
            "spec": _safe_reconciliation_side(r.get("spec")), "source": _safe_reconciliation_side(r.get("source"))}


def _safe_evidence_packet(packet):
    """Evidence Packet を既知フィールド・既知の型のみで再構築する（未知キー・未知の型を共有経路へ通さない）。"""
    if not isinstance(packet, dict):
        return None
    out = {}
    for k in _EVIDENCE_PACKET_STR_FIELDS:
        if isinstance(packet.get(k), str):
            out[k] = packet[k]
    for k in _EVIDENCE_PACKET_INT_FIELDS:
        v = packet.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    for k in ("claims", "remaining_gaps", "conflicts"):
        v = packet.get(k)
        if isinstance(v, list):
            out[k] = [x for x in v if isinstance(x, str)]
    ev = packet.get("evidence")
    if isinstance(ev, list):
        out["evidence"] = [_safe_evidence_item(e) for e in ev if isinstance(e, dict)]
    return out


# 項目名（AI が書いた自由な文を含む）は共有の複製に載せず、件数だけにする。
_SHARE_COUNT_ONLY_LABELS = ("確認できなかった項目", "壊れていた項目")


def _share_summary(summary: dict) -> dict:
    """調べた範囲を共有向けにする。確認できなかった項目・壊れた項目は名前を出さず件数だけ。"""
    import re
    items, counts = [], {}
    for it in summary["items"]:
        label, text = it["label"], it["text"]
        if label not in _SHARE_COUNT_ONLY_LABELS:
            items.append(it)
            continue
        m = re.match(r"^(?:ほか |名前を表示できない項目 )?(\d+) 件", text)
        if label == "壊れていた項目":
            n = int(m.group(1)) if m else 1
        elif m and text.startswith(("ほか ", "名前を表示できない項目 ")):
            n = int(m.group(1))
        else:
            n = 1
        counts[label] = counts.get(label, 0) + n
    for label, n in counts.items():
        items.append({"label": label, "text": f"{n} 件（項目名は共有では表示しません）"})
    return {"v": summary["v"], "items": items}


def _share_sources_unverified(answer: dict) -> dict:
    """確認できなかった資料を共有向けに写す。資料パスは秘匿名でないものだけ、それ以外は件数に回す。"""
    from ..ingest import text_kind
    rows = answer.get("sources_unverified")
    if not isinstance(rows, list):
        return {}
    kept, hidden = [], 0
    for r in rows:
        if (isinstance(r, dict) and isinstance(r.get("path"), str) and isinstance(r.get("reason"), str)
                and not text_kind.is_sensitive_doc_id(r["path"])):
            kept.append({"path": r["path"], "reason": r["reason"]})
        else:
            hidden += 1
    for key in ("sources_unverified_hidden",):
        v = answer.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            hidden += v
    out: dict = {"sources_unverified": kept}
    if hidden:
        out["sources_unverified_hidden"] = hidden
    more = answer.get("sources_unverified_more")
    if isinstance(more, int) and not isinstance(more, bool) and more > 0:
        out["sources_unverified_more"] = more
    return out


def _share_new_shape_fields(answer: dict) -> dict:
    """新しい欄を共有向けにする（秘匿の資料の名前・パスは出さず件数に回す・`answer_shape.safe_new_fields`）。"""
    out = answer_shape.safe_new_fields(answer)
    if isinstance(out.get("referenced_docs"), list):  # 共有の出典から外れる資料（重要度設定ファイル）は参照した資料の行からも外す
        out["referenced_docs"] = [r for r in out["referenced_docs"] if not importance.is_importance_control_path(r["path"])]
    return out


def _safe_download_url(url, doc_id, world) -> str | None:
    """出典の原本ダウンロードリンクを、`/documents/download?world=…&rel=<doc_id>` の形に限って通す（それ以外の URL・別資料を指すリンクは落とす）。取得側のエンドポイントが利用者ごとに権限・秘匿を再判定する。リンクの world は回答の `scope.world` と一致するものだけ（別の資料フォルダの文書を指すリンク・world 不明は落とす）。"""
    if not isinstance(url, str) or not isinstance(doc_id, str) or not isinstance(world, str) or not world:
        return None
        return None
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or parts.path != "/documents/download":
        return None
    q = parse_qs(parts.query, keep_blank_values=True)
    if set(q) - {"world", "rel"} or q.get("rel") != [doc_id] or q.get("world") != [world]:
        return None
    return url


_RETRY_LABEL_MAX = 200
_RETRY_DEPTHS = ("quick", "standard", "deep", "max")


def _safe_retry_hint(h) -> dict | None:
    """続きのボタン 1 件（`chat_service._retry_hints`／resume が作る既知の 5 種）を、種類ごとの既知の形だけで再構築する。形が合わなければ None。"""
    if not isinstance(h, dict) or not isinstance(h.get("label"), str) or not h["label"].strip():
        return None
    kind, act = h.get("kind"), h.get("action")
    if not isinstance(act, dict):
        return None
    if kind == "scope" and act == {"scope_paths": []}:
        action = {"scope_paths": []}
    elif kind == "layer" and set(act) == {"layer"} and act["layer"] in ("both", "docs", "code"):
        action = {"layer": act["layer"]}
    elif kind == "depth" and set(act) == {"depth_profile"} and act["depth_profile"] in _RETRY_DEPTHS:
        action = {"depth_profile": act["depth_profile"]}
    elif (kind == "tools" and set(act) == {"tools"} and isinstance(act["tools"], dict)
          and all(isinstance(k, str) and isinstance(v, bool) for k, v in act["tools"].items())):
        action = {"tools": dict(act["tools"])}
    elif kind == "resume" and act == {"message": "続きを調べて"}:
        action = {"message": "続きを調べて"}
    else:
        return None
    return {"kind": kind, "label": h["label"][:_RETRY_LABEL_MAX], "action": action}


# 打ち切りの旗（利用統計の 12 項目＋調べた範囲の組み立てが読む 3 項目）。値は非負整数か真偽だけ通す。
_SHARE_LIMIT_KEYS = frozenset(_USAGE_LIMIT_INT_FIELDS + _USAGE_LIMIT_BOOL_FIELDS
                              + ("wall_clock_hit", "ledger_incomplete", "claims_unmatched"))


def _safe_share_limits(limits) -> dict | None:
    if not isinstance(limits, dict):
        return None
    out = {k: v for k, v in limits.items()
           if k in _SHARE_LIMIT_KEYS and (isinstance(v, bool) or (isinstance(v, int) and v >= 0))}
    return out or None


def _safe_created_files(files) -> list[dict] | None:
    """作成したファイルは名前だけ残す（中身・ダウンロードリンクは出さない）。"""
    if not isinstance(files, list):
        return None
    out = [{"name": f["name"]} for f in files
           if isinstance(f, dict) and isinstance(f.get("name"), str) and f["name"]]
    return out or None


def _redaction_text(answer) -> str:
    """個人由来のターンを伏せるときの文言。個人ファイルの参照が無く、ファイルの作成だけが理由のターンは専用の文言にする。"""
    if (isinstance(answer, dict) and answer.get("codex_wrote_files")
            and not (answer.get("personal_sources") or answer.get("_personal_facts"))):
        return _REDACTED_FILES_TEXT
    return _REDACTED_TEXT


def _safe_share_answer(answer):
    """非個人ターンの answer を allowlist で再構築する（未知キー・個人由来・route/trace を持ち込まない）。共有できるのは KB 由来の headline/data/summary/scope と、個人ヒットを除いた sources のみ。
    確認カード（lens=clarify）は `{"lens":"clarify"}` の最小形だけを返す。`_SHARE_SAFE_LENS` に clarify を含めるのは、複製する `messages.lens` を NULL に落とさないため。
    """
    if not isinstance(answer, dict):
        return None
    if answer.get("lens") == "clarify":
        return {"lens": "clarify"}
    out = {}
    if isinstance(answer.get("headline"), str):
        out["headline"] = answer["headline"]
    # 本文・注記・完了状態・調べた範囲は型を確かめて写す（旧形式の行は headline だけが残る）。
    if isinstance(answer.get("body"), str):
        out["body"] = answer["body"]
        out["notices"] = answer_shape.notices_of(answer)
    if answer.get("completion") in answer_shape.COMPLETIONS:
        out["completion"] = answer["completion"]
    if isinstance(answer.get("answer_schema"), int) and not isinstance(answer["answer_schema"], bool):
        out["answer_schema"] = answer["answer_schema"]
    summary = answer_shape.investigation_summary_of(answer)
    if summary is not None:
        out["investigation_summary"] = _share_summary(summary)
    unverified = _share_sources_unverified(answer)
    out.update(unverified)
    out.update(_share_new_shape_fields(answer))
    if answer.get("lens") in _SHARE_SAFE_LENS:
        out["lens"] = answer["lens"]
    srcs = answer.get("sources")
    if isinstance(srcs, list):  # 共有 KB citation のみ（個人ヒット除去＋既知キーだけ）。
        non_personal = [s for s in srcs if isinstance(s, dict) and s.get("source") != "個人ファイル内ヒット"]
        # 重要度設定ファイル自体は共有 snapshot の出典にも出さない（`_strip_shared_message` と共有する実装）。
        importance_filtered = _filter_importance_from_citations(non_personal)
        # `importance`/`importance_reason` は通し、`importance_source` は出さない。
        out["sources"] = [{k: s[k] for k in ("doc_id", "quote", "source", "title", "path",
                                              "importance", "importance_reason") if k in s}
                          for s in importance_filtered]
        for row, src in zip(out["sources"], importance_filtered):
            url = _safe_download_url(src.get("download_url"), src.get("doc_id"),
                                      (answer.get("scope") or {}).get("world") if isinstance(answer.get("scope"), dict) else None)
            if url:
                row["download_url"] = url
    # 出典の2区分表示（根拠/参考）に使う doc_id 集合。実際に残った `out["sources"]` の doc_id 集合と必ず交差させる。
    sv = answer.get("sources_verified")
    if isinstance(sv, list):
        out["sources_verified"] = _intersect_sources_verified(sv, out.get("sources", []))
    # 停止／途中・続きのボタン・打ち切り・作成したファイル（名前のみ）は既知の安全な形だけ写す。
    if answer.get("codex_stopped_early") is True:
        out["codex_stopped_early"] = True
    if answer.get("stop_kind") in stop_kind_mod.STOP_KINDS:
        out["stop_kind"] = answer["stop_kind"]
    if isinstance(answer.get("retry_hints"), list):
        hints = [h for h in (_safe_retry_hint(x) for x in answer["retry_hints"]) if h is not None]
        if hints:
            out["retry_hints"] = hints
    limits = _safe_share_limits(answer.get("limits"))
    if limits is not None:
        out["limits"] = limits
    created = _safe_created_files(answer.get("created_files"))
    if created is not None:
        out["created_files"] = created
    if isinstance(answer.get("summary"), dict):
        out["summary"] = answer["summary"]
    if answer.get("data") is not None:  # 非個人ターンの影響カード等（KB 由来）。
        data = answer["data"]
        # `data.citations[].doc_id`・Evidence Packet の `source_path`/`matched_doc_ids` からも重要度設定ファイルを除外する。
        out["data"] = _redact_importance_from_answer_data(data) if isinstance(data, dict) else data
    if isinstance(answer.get("scope"), dict):
        out["scope"] = answer["scope"]
    return out


def _create_sanitized_snapshot_tx(c, owner_uid: str, source_cid: int) -> int | None:
    """`create_sanitized_snapshot` の本体（呼び出し側が用意した接続 `c` 上で実行する）。新 snapshot の作成と共有/受領ラッパーの付け替えを1トランザクションにするために分離している。契約は `create_sanitized_snapshot` を参照。"""
    conv = c.execute(
        "SELECT version FROM conversations WHERE id=%s AND deleted_at IS NULL",
        (source_cid,)).fetchone()
    if not conv:
        return None
    new = c.execute(
        "INSERT INTO conversations (user_id, version, title, origin, read_only, "
        "  source_conversation_id, contains_personal_workspace) "
        "VALUES (%s,%s,%s,'sanitized_snapshot', TRUE, %s, FALSE) RETURNING id",
        (owner_uid, conv["version"], _SANITIZED_TITLE, source_cid)).fetchone()
    new_cid = new["id"]
    msgs = c.execute(
        "SELECT role, content, lens, answer, personal FROM messages "
        "WHERE conversation_id=%s ORDER BY id", (source_cid,)).fetchall()
    # taint 判定は `conversations.py::is_personal_tainted` に集約する。
    def _tainted(m):
        return is_personal_tainted(m)
    prepped = [{"m": m, "tainted": _tainted(m), "redaction": _redaction_text(m["answer"])} for m in msgs]
    # 2nd pass: user 質問も、直後の assistant が taint なら伏字化する。
    for i, pm in enumerate(prepped):
        if pm["m"]["role"] == "user":
            nxt = next((prepped[j] for j in range(i + 1, len(prepped))
                        if prepped[j]["m"]["role"] == "assistant"), None)
            if nxt and nxt["tainted"]:
                pm["tainted"] = True
                pm["redaction"] = nxt["redaction"]
    for pm in prepped:
        m = pm["m"]
        if pm["tainted"]:  # 個人ターン: Q/A とも伏字・answer は最小化。
            content = pm["redaction"]
            answer = {"headline": content} if m["role"] == "assistant" else None
            lens = None
        else:  # 非個人ターン: content 保持・answer は allowlist 再構築。
            content = m["content"]
            answer = _safe_share_answer(m["answer"])
            lens = m["lens"] if m["lens"] in _SHARE_SAFE_LENS else None
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, route, trace, answer, personal) "
            "VALUES (%s,%s,%s,%s,NULL,NULL,%s,FALSE)",  # route/trace は常に落とす。
            (new_cid, m["role"], content, lens,
             Json(answer) if answer is not None else None))
    return new_cid


def create_sanitized_snapshot(owner_uid: str, source_cid: int) -> int | None:
    """会話の sanitized コピー（共有用・凍結スナップショット）を作る。
    - title は固定文言（元 title を漏らさない）。
    - 個人ターン（messages.personal=TRUE）は Q/A とも伏字。非個人ターンは content 保持＋answer を allowlist 再構築。
    - route/trace は常に落とす。lens は enum allowlist のみ。
    - origin='sanitized_snapshot'・read_only・contains_personal_workspace=FALSE。
    利用者が手入力した個人情報は伏字対象外（共有者の責任）。新 conversation id を返す（元が無ければ None）。共有はこの snapshot を指すので取消/期限は snapshot に効く。
    """
    _ensure()
    with _connect() as c:
        return _create_sanitized_snapshot_tx(c, owner_uid, source_cid)


def create_share(cid, owner_uid, token_hash, expires_at, invitee_uids, created_by=None) -> int:
    """共有リンクを作成し招待を登録して share id を返す。`expires_at=None` は作成から既定日数後。"""
    _ensure()
    with _connect() as c:
        sid = c.execute(
            "INSERT INTO conversation_shares (conversation_id, owner_user_id, token_hash, expires_at, created_by) "
            "VALUES (%s,%s,%s,COALESCE(%s, now() + make_interval(days => %s)),%s) RETURNING id",
            (cid, owner_uid, token_hash, expires_at, SHARE_DEFAULT_EXPIRY_DAYS,
             created_by or owner_uid)).fetchone()["id"]
        for iu in invitee_uids:
            c.execute("INSERT INTO conversation_share_invites (share_id, invitee_user_id, invited_by) "
                      "VALUES (%s,%s,%s) ON CONFLICT (share_id, invitee_user_id) DO NOTHING",
                      (sid, iu, created_by or owner_uid))
        return sid


def resolve_share_by_token(token_hash) -> dict | None:
    """token hash → share 行（`active`＝未取消・期限内 を含む）。存在しなければ None。`expires_at` は実効期限。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, conversation_id, owner_user_id, "
            f"{SHARE_EFFECTIVE_EXPIRES_SQL} AS expires_at, revoked_at, "
            f"(revoked_at IS NULL AND {SHARE_EFFECTIVE_EXPIRES_SQL}>now()) AS active "
            "FROM conversation_shares WHERE token_hash=%s", (token_hash,)).fetchone()


def is_invited(share_id, uid) -> bool:
    _ensure()
    with _connect() as c:
        return bool(c.execute("SELECT 1 FROM conversation_share_invites "
                              "WHERE share_id=%s AND invitee_user_id=%s", (share_id, uid)).fetchone())


class ShareUnavailableError(Exception):
    """受領時の再確認で共有が使えなくなっていた（取消・期限切れ・招待外）。`args[0]` が reason。"""


def accept_share(share_id, uid, *, audit: dict | None = None) -> int:
    """クリックした uid の履歴に受領ラッパー行を作る（同 uid×share は1行・冪等）。wrapper id を返す。
    `audit`（`{"ip_hash","user_agent"}`）を渡すと `share.accepted` の監査を同一トランザクションで書く（失敗すればラッパー作成ごと rollback）。
    取消・有効期限・招待は同一トランザクション内で再確認し、使えなければ `ShareUnavailableError`（状態も監査も書かない）。
    新規 wrapper を作る前に共有元 conversation 行を `SELECT ... FOR UPDATE OF c` でロックし（`delete_conversation` と直列化）、共有元が既に無ければ ValueError。
    `refresh_sanitized_share` とロック順が逆になるため、先頭で `_share_lock_key` の advisory lock を取る。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_share_lock_key(share_id),))
        existing = c.execute(
            "SELECT id FROM conversations WHERE user_id=%s AND share_id=%s "
            "AND origin='received_share' AND deleted_at IS NULL", (uid, share_id)).fetchone()
        # ロック順は 共有元会話 → 共有行（delete_conversation と同順）。
        src = c.execute(
            "SELECT c.id FROM conversation_shares s JOIN conversations c ON c.id=s.conversation_id "
            "WHERE s.id=%s FOR UPDATE OF c", (share_id,)).fetchone()
        if not src:
            raise ValueError(f"共有元の会話が見つかりません（share_id={share_id}）")
        # 共有行を FOR SHARE で押さえたまま、取消/期限切れ/招待外になっていないかを再確認する。
        st = c.execute(
            f"SELECT (revoked_at IS NULL) AS live, ({SHARE_EFFECTIVE_EXPIRES_SQL}>now()) AS fresh "
            "FROM conversation_shares WHERE id=%s FOR SHARE", (share_id,)).fetchone()
        if not st or not st["live"] or not st["fresh"]:
            raise ShareUnavailableError("revoked" if st and not st["live"] else "expired")
        if not c.execute("SELECT 1 FROM conversation_share_invites "
                         "WHERE share_id=%s AND invitee_user_id=%s", (share_id, uid)).fetchone():
            raise ShareUnavailableError("not_invited")
        if existing:
            wid = existing["id"]
        else:
            row = c.execute(
                "INSERT INTO conversations (user_id, version, title, origin, source_conversation_id, "
                "  share_id, shared_by_user_id, received_at, read_only) "
                "SELECT %s, c.version, c.title, 'received_share', c.id, s.id, s.owner_user_id, now(), true "
                "FROM conversation_shares s JOIN conversations c ON c.id=s.conversation_id WHERE s.id=%s "
                "RETURNING id", (uid, share_id)).fetchone()
            wid = row["id"]
        c.execute("UPDATE conversation_share_invites SET accepted_at=now() "
                  "WHERE share_id=%s AND invitee_user_id=%s AND accepted_at IS NULL", (share_id, uid))
        c.execute("UPDATE conversation_shares SET last_used_at=now() WHERE id=%s", (share_id,))
        if audit is not None:
            from sherpa import store as _facade
            srow = c.execute("SELECT conversation_id FROM conversation_shares WHERE id=%s",
                             (share_id,)).fetchone()
            _facade._audit_insert(
                c, uid, "share.accepted", "share", f"share:{share_id}",
                detail={"wrapper_conversation_id": wid, "source_conversation_id": srow["conversation_id"]},
                outcome="success", ip_hash=audit.get("ip_hash"), user_agent=audit.get("user_agent"))
        return wid


def revoke_share(share_id, owner_uid, *, audit: dict | None = None) -> bool:
    """所有者が共有を取消す（行は消さず revoked_at を立てる）。
    `audit` を渡すと、取消が成立したときだけ `share.revoked` の監査を同一トランザクションで書く（失敗すれば取消ごと rollback）。
    """
    _ensure()
    with _connect() as c:
        n = c.execute("UPDATE conversation_shares SET revoked_at=now() "
                      "WHERE id=%s AND owner_user_id=%s AND revoked_at IS NULL",
                      (share_id, owner_uid)).rowcount
        if n > 0 and audit is not None:
            from sherpa import store as _facade
            _facade._audit_insert(c, owner_uid, "share.revoked", "share", f"share:{share_id}",
                                  outcome="success", ip_hash=audit.get("ip_hash"),
                                  user_agent=audit.get("user_agent"))
    return n > 0


SHARE_EXPIRY_NOTICE_DAYS = 7  # 所有者へ「もうすぐ期限切れ」を通知する残り日数。


def extend_share(share_id, owner_uid, days: int, *, audit: dict | None = None):
    """所有者が共有の期限を `now()+days` 日へ延ばす（取消済みは不可・期限切れは可）。
    成立すれば新しい実効期限（tz-aware datetime）を返す。存在しない/所有者不一致/取消済みは None。`audit` を渡すと `share.extended` の監査を同一トランザクションで書く（失敗すれば rollback）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "UPDATE conversation_shares SET expires_at=now() + make_interval(days => %s) "
            "WHERE id=%s AND owner_user_id=%s AND revoked_at IS NULL RETURNING expires_at",
            (days, share_id, owner_uid)).fetchone()
        if row is None:
            return None
        if audit is not None:
            from sherpa import store as _facade
            _facade._audit_insert(c, owner_uid, "share.extended", "share", f"share:{share_id}",
                                  detail={"days": days, "expires_at": row["expires_at"].isoformat()},
                                  outcome="success", ip_hash=audit.get("ip_hash"),
                                  user_agent=audit.get("user_agent"))
        return row["expires_at"]


def list_expiring_shares_for_owner(owner_uid, within_days: int = SHARE_EXPIRY_NOTICE_DAYS) -> list[dict]:
    """`owner_uid` が所有し、実効期限が今から `within_days` 日以内に来る（未取消・未失効の）共有を期限が近い順に返す。
    サニタイズ共有は元会話のタイトル・id を返す。返す列: share_id・conversation_id・title・expires_at。
    """
    _ensure()
    eff = SHARE_EFFECTIVE_EXPIRES_SQL.replace("expires_at", "s.expires_at").replace("created_at", "s.created_at")
    with _connect() as c:
        return c.execute(
            f"SELECT s.id AS share_id, src.id AS conversation_id, src.title, {eff} AS expires_at "
            "FROM conversation_shares s JOIN conversations t ON t.id=s.conversation_id "
            "JOIN conversations src ON src.id = COALESCE(t.source_conversation_id, t.id) "
            "WHERE s.owner_user_id=%s AND s.revoked_at IS NULL AND src.deleted_at IS NULL "
            f"  AND {eff}>now() AND {eff}<=now() + make_interval(days => %s) "
            f"ORDER BY {eff}", (owner_uid, within_days)).fetchall()


# フォーク（「この会話を引き継いで質問」）。

class ForkNotAllowedError(Exception):
    """フォーク不可。`args[0]` に reason: `"not_received_share"`（受領共有ラッパーでない）・`"share_unavailable"`（共有が取消/期限切れ/招待外）・`"personal_blocked"`（元会話が個人 workspace を参照）。"""


def _fork_title(conv: dict, messages: list) -> str:
    """フォーク先タイトル。通常共有からは元 title をそのまま複製する。
    サニタイズ共有（固定文言）からは、複製する messages のうち最初の伏字でない user 発言の先頭40文字（`strip()[:40]`）を title にし、無ければ「引き継いだ会話」。元会話の title は参照しない。
    """
    if conv["title"] != _SANITIZED_TITLE:
        return conv["title"]
    for m in messages:
        if m["role"] == "user" and m["content"] not in _REDACTED_TEXTS:
            t = (m["content"] or "").strip()[:40]
            if t:
                return t
    return "引き継いだ会話"


def fork_received_share(uid, wid, *, ip_hash=None, user_agent=None) -> int:
    """受領共有ラッパー `wid` を、`uid` 自身の新しい会話として複製する。
    複製元は `get_conversation_for_read(uid, wid)` と同じ検証・同じ本文（伏字済み）。新会話は `origin='own'`・`read_only=FALSE`・`contains_personal_workspace=FALSE` で、`forked_from_share_id`/`forked_from_user_id`/`forked_at` を持つ。同じラッパーから何度でもフォークできる。
    複製と監査（`share.forked`）は同一トランザクションで書く（監査が失敗すれば複製も rollback）。`_audit_insert` は facade 属性経由で実行時に解決する。
    returns 新会話 id。raises `LookupError`（`wid` が存在しない/自分のものでない/削除済み）、`ForkNotAllowedError`（受領共有でない・共有が無効・個人ブロック）。監査 INSERT の失敗はそのまま伝播する。
    """
    _ensure()
    from sherpa import store as _facade
    got = get_conversation_for_read(uid, wid)
    if got is None:
        raise LookupError(f"会話が見つかりません（wid={wid}）")
    conv = got["conversation"]
    if conv["origin"] != "received_share":
        raise ForkNotAllowedError("not_received_share")
    if got.get("share_status") in ("unavailable", "personal_blocked"):
        raise ForkNotAllowedError(got["share_status"])
    with _connect() as c:
        new = c.execute(
            "INSERT INTO conversations (user_id, version, title, origin, read_only, "
            "  contains_personal_workspace, forked_from_share_id, forked_from_user_id, forked_at) "
            "VALUES (%s,%s,%s,'own', FALSE, FALSE, %s,%s, now()) RETURNING id",
            (uid, conv["version"], _fork_title(conv, got["messages"]), conv["share_id"], conv["shared_by_user_id"])
        ).fetchone()
        new_cid = new["id"]
        for m in got["messages"]:
            c.execute(
                "INSERT INTO messages (conversation_id, role, content, lens, route, trace, answer, personal) "
                "VALUES (%s,%s,%s,%s,NULL,NULL,%s,FALSE)",  # route/trace は常に NULL・personal=FALSE。
                (new_cid, m["role"], m["content"], m.get("lens"),
                 Json(m["answer"]) if m.get("answer") is not None else None))
        _facade._audit_insert(
            c, uid, "share.forked", "share",
            f"share:{conv['share_id']}" if conv.get("share_id") is not None else None,
            detail={"wrapper_conversation_id": wid, "new_conversation_id": new_cid,
                    "source_conversation_id": conv.get("source_conversation_id")},
            outcome="success", severity="info", ip_hash=ip_hash, user_agent=user_agent)
        return new_cid


# 再共有（「スナップショットを更新」）。

class ShareNotSanitizedError(Exception):
    """通常共有（元会話をライブ参照）への refresh 要求（409 相当）。"""


def refresh_sanitized_share(owner_uid, share_id, *, audit: dict | None = None) -> dict:
    """サニタイズ共有のスナップショットを最新の内容へ取り直す。リンク・招待・期限は不変。
    `audit` を渡すと `share.refreshed` の監査を同一トランザクションで書く（失敗すれば更新ごと rollback）。
    通常共有には `ShareNotSanitizedError` を送出する。
    手順（1トランザクション）:
    ① 対象 share 行を `FOR UPDATE` でロックする。
    ② 所有者確認と、現行 snapshot の `source_conversation_id` から元会話が生きているかの確認を行う。
    ③ 新 snapshot を `_create_sanitized_snapshot_tx` で作る。
    ④ `conversation_shares.conversation_id`/`refreshed_at` を新 snapshot へ更新する。
    ⑤ 受領ラッパーの `source_conversation_id` を新 snapshot へ付け替える。
    ⑥ 旧 snapshot 行を `deleted_at=now()`（soft delete）にする。⑤は⑥より先に行う。
    期限切れでも更新は許す。存在しない/取消済み/元会話が削除済みは `LookupError`、所有者不一致は `PermissionError`（行の存在を確かめた後で判定する）。
    returns `{"share_id", "old_snapshot_id", "new_snapshot_id", "source_conversation_id", "refreshed_at"}`。
    `accept_share` とロック順が逆のため、先頭で `_share_lock_key` の advisory lock を取る。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_share_lock_key(share_id),))
        share = c.execute(
            "SELECT id, conversation_id, owner_user_id, revoked_at "
            "FROM conversation_shares WHERE id=%s FOR UPDATE", (share_id,)).fetchone()
        if not share:
            raise LookupError(f"共有が見つかりません（share_id={share_id}）")
        if share["owner_user_id"] != owner_uid:
            raise PermissionError("所有者のみ更新できます")
        if share["revoked_at"] is not None:
            raise LookupError(f"共有が見つかりません（share_id={share_id}）")
        old_snapshot = c.execute(
            "SELECT id, origin, source_conversation_id FROM conversations "
            "WHERE id=%s AND deleted_at IS NULL", (share["conversation_id"],)).fetchone()
        if not old_snapshot:
            raise LookupError(f"共有対象の会話が見つかりません（share_id={share_id}）")
        if old_snapshot["origin"] != "sanitized_snapshot":
            raise ShareNotSanitizedError("この共有は常に最新の内容を表示します")
        source_cid = old_snapshot["source_conversation_id"]
        source_conv = c.execute(
            "SELECT id FROM conversations WHERE id=%s AND deleted_at IS NULL",
            (source_cid,)).fetchone() if source_cid is not None else None
        if not source_conv:
            raise LookupError(f"元会話が見つかりません（source_conversation_id={source_cid}）")
        new_snapshot_id = _create_sanitized_snapshot_tx(c, owner_uid, source_cid)
        if new_snapshot_id is None:
            raise LookupError(f"元会話が見つかりません（source_conversation_id={source_cid}）")
        refreshed = c.execute(
            "UPDATE conversation_shares SET conversation_id=%s, refreshed_at=now() "
            "WHERE id=%s RETURNING refreshed_at", (new_snapshot_id, share_id)).fetchone()
        c.execute(
            "UPDATE conversations SET source_conversation_id=%s "
            "WHERE origin='received_share' AND share_id=%s AND deleted_at IS NULL",
            (new_snapshot_id, share_id))
        c.execute("UPDATE conversations SET deleted_at=now() WHERE id=%s", (old_snapshot["id"],))
        if audit is not None:
            from sherpa import store as _facade
            _facade._audit_insert(
                c, owner_uid, "share.refreshed", "share", f"share:{share_id}",
                detail={"old_snapshot_id": old_snapshot["id"], "new_snapshot_id": new_snapshot_id,
                        "source_conversation_id": source_cid},
                outcome="success", severity="info", ip_hash=audit.get("ip_hash"),
                user_agent=audit.get("user_agent"))
        return {"share_id": share_id, "old_snapshot_id": old_snapshot["id"],
                "new_snapshot_id": new_snapshot_id, "source_conversation_id": source_cid,
                "refreshed_at": refreshed["refreshed_at"]}


def list_shares_for_conversation(owner_uid, cid) -> list[dict]:
    """`cid`（所有者の元会話）を対象にした共有一覧（所有者専用）。
    通常共有（`share.conversation_id=cid`）とサニタイズ共有（現行 snapshot の `source_conversation_id=cid`）の両方を返す。招待者一覧（`invitees`）の表示名は `users.display_name` の LEFT JOIN で解決する。
    """
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT s.id AS share_id, (t.origin='sanitized_snapshot') AS sanitized, "
            "  s.created_at, "
            f"  COALESCE(s.expires_at, s.created_at + interval '{SHARE_DEFAULT_EXPIRY_DAYS} days') AS expires_at, "
            "  s.revoked_at, s.refreshed_at, s.last_used_at "
            "FROM conversation_shares s JOIN conversations t ON t.id = s.conversation_id "
            "WHERE s.owner_user_id=%s AND (t.id=%s OR t.source_conversation_id=%s) "
            "ORDER BY s.created_at DESC",
            (owner_uid, cid, cid)).fetchall()
        out = []
        for r in rows:
            invitees = c.execute(
                "SELECT i.invitee_user_id AS uid, u.display_name AS name, i.accepted_at "
                "FROM conversation_share_invites i LEFT JOIN users u ON u.uid=i.invitee_user_id "
                "WHERE i.share_id=%s ORDER BY i.created_at", (r["share_id"],)).fetchall()
            out.append({**r, "invitees": invitees})
        return out
