"""会話共有・sanitized snapshot（2026-07-01-認証と共有の提案.md MVP／§Phase2）。

`sherpa/store/__init__.py` から純移動（フェーズ4 S10）。sanitized snapshot（`create_sanitized_snapshot`
→ `_safe_share_answer`）は allowlist 再構築、通常の受領共有の読取（`get_conversation_for_read`
→ `_strip_shared_message`）は denylist（他は素通し）——契約が異なる2経路だが、重要度設定ファイル
（`_重要度.txt`）自体への参照の除外（`sources`/`sources_verified`/`data.citations`/
`data.candidates`/Evidence Packet）は両経路が同じ共有ヘルパを呼んで独立に判定する（§5）。

`accept_share`（本モジュール）と `delete_conversation`（conversations.py）は同一 conversations 行を
`SELECT ... FOR UPDATE` でロックすることで競合を直列化する契約がある（両関数の docstring 参照）。
この2関数はモジュールをまたぐが、Python の関数呼び出しで結合しているわけではなく、どちらも
同じ Postgres トランザクション機構（行ロック）を経由して直列化されるため、モジュール間の
import は不要（純移動でこの契約は変わらない）。
"""
from __future__ import annotations

import hashlib
import math

from psycopg.types.json import Json

from .. import citations as citations_mod
from ..ingest import importance
from .conversations import is_personal_tainted
from .db import _connect, _ensure
from .feedback import get_feedback_by_message_ids_for_user


def _share_lock_key(share_id) -> int:
    """`accept_share`/`refresh_sanitized_share` 共通の advisory lock key（是正3・2026-09-05）。

    両関数は同じ2種の行（共有元/対象 conversation 行・conversation_shares 行）を**逆順**で
    ロックする（`accept_share`: conversation→share／`refresh_sanitized_share`: share→conversation・
    各関数の docstring 参照）ため、同一 share を同時に対象にするとデッドロックが成立し得る。
    両関数の**先頭**（どちらの行ロックよりも前）でこの advisory lock を取ることで直列化する
    （`world_lock`/`_world_lock_key` と同じ手法＝sha1 truncate で 64bit key 空間へ写像し、
    `share_id` を裸の bigint key として使わない——`_AUDIT_CHAIN_LOCK` 等の固定小整数キーとの
    衝突を避けるため）。トランザクションスコープ（xact lock）＝commit/rollback で自動解放。
    """
    return int.from_bytes(hashlib.sha1(f"share_fork_refresh:{share_id}".encode("utf-8")).digest()[:8],
                          "big", signed=True)


def _filter_importance_from_citations(cites):
    """citation 形（`{doc_id, ...}`）のリストから、重要度設定ファイル自体を指す要素を除外する。

    `sources[]`（`_safe_share_answer`/`_strip_shared_message` 共通）・`data.citations[]`
    （qa 等の生 citation・`{doc_id, span, quote, ext}`）はどちらも `doc_id` キーを持つ同じ形
    なので、この1関数を両方の入口が共有する（§5・独立入口として双方が呼ぶ）。リストでなければ
    そのまま返す。
    """
    if not isinstance(cites, list):
        return cites
    return [c for c in cites
           if not (isinstance(c, dict) and importance.is_importance_control_path(c.get("doc_id") or ""))]


def _filter_importance_from_edges(edges):
    """graph edge 形（`{doc, ...}`）のリストから、重要度設定ファイル自体を来歴に持つ要素を除外する。

    troubleshoot 候補の `evidence.edges[]` 専用（`lens_service.neo4j_related` が返す辺は
    `doc_id` でなく `doc` キーを持つ——`_filter_importance_from_citations` とはキー名が違うため
    別 helper にする）。リストでなければそのまま返す。
    """
    if not isinstance(edges, list):
        return edges
    return [e for e in edges
           if not (isinstance(e, dict) and importance.is_importance_control_path(e.get("doc") or ""))]


def _filter_importance_from_candidates(candidates):
    """troubleshoot レンズの候補（`data.candidates[]`）から重要度設定ファイルへの参照を落とす。

    grep のみで見つかった候補（`lens_service._troubleshoot_cards` の `label="Document"` 分岐）は
    `name` フィールド自体が doc_id——それ以外の候補（グラフ由来の業務語）でも `name` が
    `_重要度.txt` という形と一致することは無いため、区別せず一律に判定してよい。候補自身が
    対象なら丸ごと落とし、生き残った候補の `evidence.grep`（`doc_id` キー）／`evidence.edges`
    （`doc` キー）もそれぞれ独立に判定する。リストでなければそのまま返す。
    """
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
    """impact レンズの `items[]`/`presumed[]`（`chat_service._answer_impact` が生の `result` を
    まるごと `data` へ埋め込む）から、重要度設定ファイルを来歴に持つ evidence だけを落とす。

    各要素の `evidence[]` は `lens_service` の辺と同じ `{doc, ...}` 形（`doc_id` ではない）
    なので `_filter_importance_from_edges` を再利用する。要素自体（`name`/`judgement` 等）は
    グラフ由来の結論であって `_重要度.txt` を指すことは無いため落とさず、根拠の対応する
    evidence だけを除外する。リストでなければそのまま返す。
    """
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
    """`sources_verified`（精読済み doc_id の集合）を、生き残った `sources[]` の doc_id 集合と
    再交差する。フィルタ（重要度設定ファイルの除外に限らず）で `sources` から消えた doc_id が
    「精読済み」として残り続けないようにする（`_safe_share_answer`／`_strip_shared_message` が
    共有する）。`sv` がリストでなければそのまま返す。
    """
    if not isinstance(sv, list):
        return sv
    survived_ids = {s.get("doc_id") for s in sources if isinstance(s, dict)} if isinstance(sources, list) else set()
    return sorted(d for d in sv if isinstance(d, str) and d in survived_ids)


def _redact_importance_from_answer_data(data):
    """`answer.data` から重要度設定ファイル由来の citation/evidence 参照を落とす
    （`data.citations[].doc_id`・Evidence Packet の `source_path`/`matched_doc_ids`・
    troubleshoot 候補 `data.candidates[].name`/`.evidence.{grep,edges}`・impact レンズの
    `data.items[]`/`data.presumed[].evidence[].doc`——`_safe_evidence_packet`/
    `_safe_evidence_item`/`_filter_importance_from_candidates`/
    `_filter_importance_from_impact_items` に委譲）。`_safe_share_answer`（sanitized
    snapshot・allowlist 再構築）と `_strip_shared_message`（通常の受領共有・他のキーは
    そのまま通す denylist）が共有する（§5）。`data` が dict でなければそのまま返す。
    """
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
    return out


def _strip_shared_message(m: dict) -> dict:
    """受領共有の read path で内部情報を落とす（route/trace は常に NULL・answer 内の question/route/trace も除去）。
    所有者本人の read には使わない（呼び出し側で origin='received_share' のときだけ適用する）。

    RV HIGH（Codex 2026-07-07）: トップレベル messages.route/trace 列だけを NULL 化しても、
    `chat_service._finalize` がレンズ・reason・path を含む `env["route"]` を answer envelope
    自身にも埋め込む（`add_message(..., answer=env, ...)`）ため、answer.route は素通しで受領共有の
    読者に届いていた（web/chat.js の route チップ描画に使われる＝ポリシー違反）。S1 以前からの
    既存バグだが、同じ関数を触ったのでここで併せて塞ぐ（trace も同じ扱いで多層防御しておく）。
    """
    out = {**m, "route": None, "trace": None}
    a = out.get("answer")
    # F3（2026-07-07）: usage（トークン使用量）も内部情報として受領共有では伏せる
    #   （route/trace/question と同格。sanitized snapshot 側は _safe_share_answer の allowlist に usage が
    #   無いため自動で落ちる＝そちらは無改修。通常の受領共有は元会話の answer をそのまま読むため明示除去）。
    # S3（2026-07-15-LLMオーケストレーション実装計画.md §5.0）: usage_sub（サブループのトークン
    # サイドカー）も usage と同格の内部情報＝受領共有の読者に見せない。
    # S4-b（同計画 §6.3）: usage_subs（複数プロファイル並用時の複数形サイドカー）も同格＝同一コミットで
    # usage_sub の隣に並べる（漏洩防止）。
    _drop = ("question", "route", "trace", "usage", "usage_sub", "usage_subs")
    if isinstance(a, dict):
        needs_copy = any(k in a for k in _drop)
        # 通常の受領共有は元会話の answer をそのまま読むため、`_safe_share_answer`（sanitized
        # snapshot）の allowlist 再構築を経由しない。旧会話（この除外が無かった頃に保存された
        # sources/data）でも重要度設定ファイルへの参照を読者に見せないよう、`sources[]` だけで
        # なく `data.citations[]`・Evidence Packet の `source_path`/`matched_doc_ids` も、
        # sanitized snapshot 側と同じ共有ヘルパでここでも独立に判定する（§5）。
        srcs = a.get("sources")
        filtered_sources = _filter_importance_from_citations(srcs) if isinstance(srcs, list) else srcs
        if isinstance(srcs, list) and filtered_sources != srcs:
            needs_copy = True
        # sanitized snapshot（`_safe_share_answer`）と同じく、フィルタで sources から消えた
        # doc_id が sources_verified に「精読済み」として残らないよう、生き残った sources と
        # 再交差する（共通ヘルパ・§5）。
        sv = a.get("sources_verified")
        filtered_sv = _intersect_sources_verified(
            sv, filtered_sources if isinstance(srcs, list) else srcs) if isinstance(sv, list) else sv
        if isinstance(sv, list) and filtered_sv != sv:
            needs_copy = True
        data = a.get("data")
        filtered_data = _redact_importance_from_answer_data(data) if isinstance(data, dict) else data
        if isinstance(data, dict) and filtered_data != data:
            needs_copy = True
        if needs_copy:
            new_a = {k: v for k, v in a.items() if k not in _drop}
            if isinstance(srcs, list):
                new_a["sources"] = filtered_sources
            if isinstance(sv, list):
                new_a["sources_verified"] = filtered_sv
            if isinstance(data, dict):
                new_a["data"] = filtered_data
            out["answer"] = new_a
    return out


def get_conversation_for_read(uid, cid) -> dict | None:
    """current user が読める会話（所有 or 有効な受領共有）。読めなければ None／無効共有は share_status を付す。

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
            return None                                    # 他人の id 直アクセスは不可（呼出側で 403/404）
        msg_src = conv["id"]
        if conv["origin"] == "received_share":
            # expires_at IS NULL = 無期限（revoke されない限り active）。
            share = c.execute(
                "SELECT (revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())) AS active "
                "FROM conversation_shares WHERE id=%s", (conv["share_id"],)).fetchone()
            invited = c.execute("SELECT 1 FROM conversation_share_invites "
                                "WHERE share_id=%s AND invitee_user_id=%s", (conv["share_id"], uid)).fetchone()
            if not share or not share["active"] or not invited:
                return {"conversation": conv, "messages": [], "share_status": "unavailable"}
            # BLOCKER 1 fix: 共有後に個人 workspace 参照が追加された場合も read をブロックする。
            # 元会話の contains_personal_workspace を確認し、TRUE ならメッセージを返さない。
            src_conv = c.execute(
                "SELECT contains_personal_workspace FROM conversations WHERE id=%s",
                (conv["source_conversation_id"],)).fetchone()
            if src_conv and src_conv["contains_personal_workspace"]:
                return {"conversation": conv, "messages": [], "share_status": "personal_blocked"}
            msg_src = conv["source_conversation_id"]       # 本文は元会話から（コピーしない＝取消/期限が効く）
        msgs = c.execute(
            "SELECT id, role, content, lens, route, trace, answer, created_at FROM messages "
            "WHERE conversation_id=%s ORDER BY id", (msg_src,)).fetchall()
    # PG コネクションプール導入後（性能台帳#17 QW2・RV代替 M1）: フィードバック取得
    # （`get_feedback_by_message_ids_for_user`）は自前で別の `_connect()` を取るため、上の
    # with ブロックの内側で呼ぶと同一リクエストが2接続を同時に保持してしまう（プール枯渇時に
    # この経路だけ実効容量を余分に消費する一斉 PoolTimeout 要因）。with を抜けて1本目を
    # 返却してから2本目を取る（PG は READ COMMITTED＝各文が実行時点の最新コミット済み値を
    # 読むため、同一トランザクション内で呼んでいた時と一貫性の保証は変わらない）。
    # 読者（uid）自身のフィードバックを assistant メッセージに同梱する（会話再読込での復元用）。
    # user_id で絞るため、受領共有の閲覧者（uid が元会話の所有者と異なる）には常に空を返す
    # ＝所有者のフィードバックが閲覧者に漏れない（閲覧者自身は投稿できないため、投稿して
    # いれば必ず自分の分だけが返る）。
    fb_map = get_feedback_by_message_ids_for_user(
        [m["id"] for m in msgs if m["role"] == "assistant"], uid)
    # `route`/`trace` と同じ既存の流儀（キーは常に存在し、無ければ null）に揃える。
    msgs = [{**m, "feedback": fb_map.get(m["id"])} for m in msgs]
    if conv["origin"] == "received_share":
        # RV HIGH fix: 受領共有は答え/出典だけ見せる（sanitized と同じ posture）。route/trace は
        # grep クエリ・doc_id・Codex 実コマンド detail 等の内部情報を含むため、共有元がそのまま
        # 見えてしまわないよう常に落とす（sanitized snapshot は最初から trace/route=NULL で保存
        # 済みだが、非 sanitize の通常共有は元会話をそのまま読むので明示的に伏せる必要がある）。
        # S1（ask_user-improvements.md）: 確認カード（answer.question）も interaction_id・options 等の
        # 内部情報を含み、読み取り専用の共有会話に対話カードを出す意味も無いため、同じ posture で
        # 落とす（sanitized snapshot 側は _safe_share_answer の allowlist に question が無く既に伏字）。
        msgs = [_strip_shared_message(m) for m in msgs]
    return {"conversation": conv, "messages": msgs}


# ==== sanitized share snapshot（2026-07-01-認証と共有の提案.md §Phase2・個人部分を除いた共有）====
# RV(DO-NOT-SHIP)反映: denylist コピーでなく **allowlist 再構築**＋**per-turn 個人フラグ**で作る。
# title/lens/route/trace/未知 answer キーからの漏洩を塞ぐ。個人ターン（messages.personal）は Q/A とも伏字。
_REDACTED_TEXT = "（個人ファイルを参照した回答のため、共有では非表示にしています）"
_SANITIZED_TITLE = "共有用（サニタイズ済み会話）"
_SHARE_SAFE_LENS = ("qa", "impact", "troubleshoot", "chat", "clarify")


_EVIDENCE_PACKET_STR_FIELDS = ("task_id", "investigation_status", "summary", "stop_reason", "next_action")
_EVIDENCE_PACKET_INT_FIELDS = ("candidates_seen", "candidates_inspected", "evidence_selected")
_EVIDENCE_ITEM_STR_FIELDS = ("evidence_id", "source_type", "source_path", "verification_method")
_LOCATOR_PART_MAX = 200   # part/object_id（str）は zip 内パス等を想定し sheet/cell_range より広め

# bbox の要素上限（citations.py の page/slide 桁上限と同じ値を流用・巨大値での DoS/表示崩れ防止）。
_LOCATOR_BBOX_ABS_MAX = citations_mod._LOCATOR_NUMBER_MAX


def _safe_locator(loc) -> dict | None:
    """`source_span` が行番号2要素ではなく構造化 locator（`sherpa/ingest/evidence_ir.py::Locator`
    由来・Office/PDF/画像の位置情報）のときの allowlist 再構築。既知キー（`page`/`slide`/`sheet`/
    `cell_range`/`part`/`object_id`/`bbox`）＋既知の値型だけを通す。`extension`（形式固有の自由な
    dict）は対象外——未知の内部表現をそのまま通さない契約に反するため。

    文字列フィールド・整数フィールドの検証は citations.py の `_clean_locator_field`/
    `_clean_locator_number`（page/slide の6桁上限・改行類の正規化＝`\\r`/U+2028/U+2029 等を含む
    Unicode 空白を単一空白へ・長さ上限）を**共通 helper として再利用**する（重複実装しない）。
    `object_id` は `str | int` のどちらでも受け付ける（`Locator.object_id` の型と同じ）。
    `bbox` は数値4要素・`math.isfinite`（NaN/Infinity を拒否）・絶対値の上限まで。
    未知キー・型不一致の値はキーごと落とす。有効なキーが1つも無ければ None。
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
        pass   # bool は int のサブクラス＝先に弾く（int 分岐に落ちない）
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
    """bbox 1要素の検証。**絶対値の上限比較を先に**行う——Python の `int` は任意精度なので、
    巨大整数（例 `10**10000`）でも `abs(x) > 上限` は float へ変換せず安全に判定できるが、
    `math.isfinite(int)` は内部で float 変換するため巨大整数で `OverflowError` を送出する
    （共有処理全体を落とす）。上限を先に確認して弾けば isfinite に到達しない。isfinite は
    float のみ・NaN/Infinity を拒否するために必要（NaN は比較が常に False なので上限比較では
    弾けない）。
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if abs(x) > _LOCATOR_BBOX_ABS_MAX:
        return False
    return not isinstance(x, float) or math.isfinite(x)


def _safe_share_list_meta(lm) -> dict | None:
    """list_docs 集計 Evidence の `list_meta`（総件数・条件・列挙範囲）の共有用 allowlist
    （`providers/base.py::_safe_list_meta` と同じ既知フィールド・既知の型のみを通す規律——共有
    経路は providers 層を import しない方針のためここで自己完結して再実装する）。
    """
    if not isinstance(lm, dict):
        return None
    out = {}
    for k in ("count", "shown"):
        v = lm.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            out[k] = v
    for k in ("prefix", "pattern"):
        v = lm.get(k)
        if isinstance(v, str):
            out[k] = v
    return out or None


def _safe_share_card_meta(cm) -> dict | None:
    """graph カード Evidence の `card_meta`（対象名・関係・カテゴリ・経路）の共有用 allowlist
    （`providers/base.py::_safe_card_meta` と同じ規律）。
    """
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
    return out or None


def _safe_evidence_item(e: dict) -> dict:
    """Evidence Packet の `evidence[]` 1件を**型検証しながら**再構築する。

    値の型を見ずにキーだけコピーすると、Packet のスキーマが将来拡張されたときや実装バグで
    `source_span` 等に秘匿情報を含む入れ子 dict（例 `{"locator":{"secret":...}}`）が紛れ込んでも
    そのまま共有経路へ通ってしまう。文字列フィールドは str（または未設定）のみ。`used`（EV-0・
    拡張設計 §4.4）は厳密に `bool` の値のときだけ通す（`1`/`"true"` 等の型不正はキーごと落とす）。
    `source_span` は行番号2要素（`int|None` を2要素持つ list/tuple）または構造化 locator
    （`_safe_locator` が扱う既知フィールドのみの dict）のどちらか——それ以外の形は**キーごと落とす**
    （値を None にすり替えるのではなく、キー自体を出さない＝未知の形を持ち込ませない）。

    `matched_doc_ids`（str のリストのときだけ）／`list_meta`／`card_meta`:
    集計/カード単位 Evidence（`source_path=None`）は `matched_doc_ids` を落とすと事実対応をほぼ
    失うため、型検証済みの形で共有経路にも保持する（`_safe_share_list_meta`/`_safe_share_card_meta`）。

    `source_path`／`matched_doc_ids` はいずれも重要度設定ファイル（`_重要度.txt`）自体を指す
    doc_id を共有経路に出さない（§5・上流の除外を信頼せずここでも独立に判定する）。
    """
    out = {}
    for k in _EVIDENCE_ITEM_STR_FIELDS:
        if k in e and (e[k] is None or isinstance(e[k], str)):
            v = e[k]
            if k == "source_path" and isinstance(v, str) and importance.is_importance_control_path(v):
                v = None
            out[k] = v
    if "used" in e and isinstance(e["used"], bool):   # EV-0（拡張設計 §4.4）: 厳密 bool のみ通す（型不正は落とす）
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


def _safe_evidence_packet(packet):
    """Evidence Packet（EXT-2・`citations.build_evidence_packet` と同じフィールド集合）を**既知
    フィールド・既知の型のみ**で再構築する（`data` 全体のブランケットコピーの唯一の例外——Packet の
    スキーマが将来拡張されても、未知キー・未知の型（locator・秘匿種別等の内部表現）が共有経路へ
    自動的に漏れないようにする）。
    """
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


def _safe_share_answer(answer):
    """非個人ターンの answer を **allowlist で再構築**（未知キー・個人由来・route/trace を持ち込まない）。
    共有可能なのは KB 由来の headline/data/summary/scope と、個人ヒットを除いた sources のみ。

    RV Med（Codex 2026-07-07）: 確認カード（`answer={"lens":"clarify","question":{...}}`）は
    `_SHARE_SAFE_LENS` に clarify が無かったため lens も丸ごと落ち、chat.js の
    `m.answer.lens === 'clarify'` プレースホルダ分岐に入れず空白/崩れ表示になっていた。
    clarify は他レンズと違って**専用の最小形のみ**を返す（question・prompt・options 等は
    一切持ち込まない＝一般 allowlist を通さない・将来 answer に新キーが増えても自動で漏れない）。
    """
    if not isinstance(answer, dict):
        return None
    if answer.get("lens") == "clarify":
        return {"lens": "clarify"}
    out = {}
    if isinstance(answer.get("headline"), str):
        out["headline"] = answer["headline"]
    if answer.get("lens") in _SHARE_SAFE_LENS:
        out["lens"] = answer["lens"]
    srcs = answer.get("sources")
    if isinstance(srcs, list):                       # 共有 KB citation のみ（個人ヒット除去＋既知キーだけ）
        non_personal = [s for s in srcs if isinstance(s, dict) and s.get("source") != "個人ファイル内ヒット"]
        # 重要度設定ファイル自体は共有 snapshot の出典にも出さない（§5・独立入口として再チェック・
        # `_strip_shared_message` と共有する実装）。
        importance_filtered = _filter_importance_from_citations(non_personal)
        # I2（2026-09-05）: `importance`/`importance_reason`（登録者重要度の表示値・J4）も allowlist へ
        # 追加する——`_strip_shared_message`（denylist・他は素通し）側は既に自然に通るため、両経路が
        # 一致する（`test_...` 参照）。`importance_source`（由来監査情報）はここでも出さない。
        out["sources"] = [{k: s[k] for k in ("doc_id", "quote", "source", "title", "path",
                                              "importance", "importance_reason") if k in s}
                          for s in importance_filtered]
    # EXT-2/EV-0（拡張設計 §4.4）: 出典の2区分表示（根拠/参考）に使う doc_id 集合。doc_id 文字列の
    # list のみ（locator 等の内部表現は元々持たせない契約）＝既知の形へ再構築するだけで安全に通せる。
    # 実際に生き残った `out["sources"]` の doc_id 集合と**必ず交差**させる（他のフィルタで sources
    # から消えた doc_id が「精読済み」として復活しないようにする）。
    sv = answer.get("sources_verified")
    if isinstance(sv, list):
        out["sources_verified"] = _intersect_sources_verified(sv, out.get("sources", []))
    if isinstance(answer.get("summary"), dict):
        out["summary"] = answer["summary"]
    if answer.get("data") is not None:               # 非個人ターンの影響カード等（KB 由来）
        data = answer["data"]
        # `data.citations[].doc_id`・Evidence Packet の `source_path`/`matched_doc_ids` からも
        # 重要度設定ファイルを除外する（§5・`_strip_shared_message` と共有する実装）。
        out["data"] = _redact_importance_from_answer_data(data) if isinstance(data, dict) else data
    if isinstance(answer.get("scope"), dict):
        out["scope"] = answer["scope"]
    return out


def _create_sanitized_snapshot_tx(c, owner_uid: str, source_cid: int) -> int | None:
    """`create_sanitized_snapshot` の本体（**呼び出し側が用意した接続 `c` 上で**実行する）。

    SH-2（再共有・スナップショット更新）が「新 snapshot の作成」と「共有/受領ラッパーの
    付け替え」を**1トランザクション**にしたいため、接続を引数で受けられる形に分離した
    （`create_sanitized_snapshot` はこれを自前の接続で包むだけの薄いラッパーに変わる・
    挙動は不変）。docstring・契約は `create_sanitized_snapshot` 側を参照。
    """
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
    # taint 判定は共通ヘルパ（`conversations.py::is_personal_tainted`）に集約
    # （改善ログエクスポートの個人情報除外と同じ基準を使う）。
    def _tainted(m):
        return is_personal_tainted(m)
    prepped = [{"m": m, "tainted": _tainted(m)} for m in msgs]
    # 2nd pass: user 質問も、対応する（直後の）assistant が taint なら伏字化
    #   （旧データは user message が未マークのため・ファイル名等の漏れ防止）。
    for i, pm in enumerate(prepped):
        if pm["m"]["role"] == "user" and not pm["tainted"]:
            nxt = next((prepped[j] for j in range(i + 1, len(prepped))
                        if prepped[j]["m"]["role"] == "assistant"), None)
            if nxt and nxt["tainted"]:
                pm["tainted"] = True
    for pm in prepped:
        m = pm["m"]
        if pm["tainted"]:                        # 個人ターン: Q/A とも伏字・answer は最小化
            content = _REDACTED_TEXT
            answer = {"headline": _REDACTED_TEXT} if m["role"] == "assistant" else None
            lens = None
        else:                                    # 非個人ターン: content 保持・answer は allowlist 再構築
            content = m["content"]
            answer = _safe_share_answer(m["answer"])
            lens = m["lens"] if m["lens"] in _SHARE_SAFE_LENS else None
        c.execute(
            "INSERT INTO messages (conversation_id, role, content, lens, route, trace, answer, personal) "
            "VALUES (%s,%s,%s,%s,NULL,NULL,%s,FALSE)",   # route/trace は常に落とす
            (new_cid, m["role"], content, lens,
             Json(answer) if answer is not None else None))
    return new_cid


def create_sanitized_snapshot(owner_uid: str, source_cid: int) -> int | None:
    """会話の sanitized コピー（共有用・凍結スナップショット）を作る。
    - **title は固定文言**（元 title のファイル名等を漏らさない）。
    - **個人ターン（messages.personal=TRUE）は Q/A とも伏字**（personal は個人ヒット/Codex書込/個人facts を
      使ったターンに chat_service が設定）。非個人ターンは content 保持＋answer を allowlist 再構築。
    - **route/trace は常に落とす**（内部情報＝検索語/パス/ツール引数の漏洩源）。lens は enum allowlist のみ。
    - origin='sanitized_snapshot'・read_only・contains_personal_workspace=FALSE。
    ※限界: 利用者が**手入力**した個人情報（システムが検出しないターン）は伏字対象外＝共有者の責任（opt-in）。
    returns 新 conversation id（元が無ければ None）。共有はこの snapshot を指すので取消/期限は snapshot に効く。
    """
    _ensure()
    with _connect() as c:
        return _create_sanitized_snapshot_tx(c, owner_uid, source_cid)


def create_share(cid, owner_uid, token_hash, expires_at, invitee_uids, created_by=None) -> int:
    """共有リンクを作成し招待を登録。返り値＝share id。`expires_at=None` は無期限。"""
    _ensure()
    with _connect() as c:
        sid = c.execute(
            "INSERT INTO conversation_shares (conversation_id, owner_user_id, token_hash, expires_at, created_by) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (cid, owner_uid, token_hash, expires_at, created_by or owner_uid)).fetchone()["id"]
        for iu in invitee_uids:
            c.execute("INSERT INTO conversation_share_invites (share_id, invitee_user_id, invited_by) "
                      "VALUES (%s,%s,%s) ON CONFLICT (share_id, invitee_user_id) DO NOTHING",
                      (sid, iu, created_by or owner_uid))
        return sid


def resolve_share_by_token(token_hash) -> dict | None:
    """token hash → share 行（`active`＝未取消・期限内（または無期限） を含む）。存在しなければ None。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT id, conversation_id, owner_user_id, expires_at, revoked_at, "
            "(revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now())) AS active "
            "FROM conversation_shares WHERE token_hash=%s", (token_hash,)).fetchone()


def is_invited(share_id, uid) -> bool:
    _ensure()
    with _connect() as c:
        return bool(c.execute("SELECT 1 FROM conversation_share_invites "
                              "WHERE share_id=%s AND invitee_user_id=%s", (share_id, uid)).fetchone())


def accept_share(share_id, uid) -> int:
    """クリックした uid の履歴に受領ラッパー行を作る（同 uid×share は1行・冪等）。wrapper id を返す。

    RV HIGH: `delete_conversation` との競合防止。新規 wrapper を作る前に共有元 conversation 行を
    `SELECT ... FOR UPDATE OF c` でロックする（delete_conversation 側も同じ行をロックするため直列化）。
    ロックを取った時点で共有元が既に無ければ（同時に物理削除された等）ValueError を送出する
    （壊れた wrapper: source_conversation_id が実体の無い/後で消える id を指す状態を作らない）。

    是正3（2026-09-05）: `refresh_sanitized_share` と共有 conversation 行／share 行を**逆順**で
    ロックするため、関数の先頭（下の行ロックより前）で `_share_lock_key` の advisory lock を
    取って直列化する（`_share_lock_key` の docstring 参照）。
    """
    _ensure()
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_share_lock_key(share_id),))
        existing = c.execute(
            "SELECT id FROM conversations WHERE user_id=%s AND share_id=%s "
            "AND origin='received_share' AND deleted_at IS NULL", (uid, share_id)).fetchone()
        if existing:
            wid = existing["id"]
        else:
            src = c.execute(
                "SELECT c.id FROM conversation_shares s JOIN conversations c ON c.id=s.conversation_id "
                "WHERE s.id=%s FOR UPDATE OF c", (share_id,)).fetchone()
            if not src:
                raise ValueError(f"共有元の会話が見つかりません（share_id={share_id}）")
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
        return wid


def revoke_share(share_id, owner_uid) -> bool:
    """所有者が共有を取消（行は消さず revoked_at を立てる）。"""
    _ensure()
    with _connect() as c:
        n = c.execute("UPDATE conversation_shares SET revoked_at=now() "
                      "WHERE id=%s AND owner_user_id=%s AND revoked_at IS NULL",
                      (share_id, owner_uid)).rowcount
    return n > 0


# ==== SH-1: フォーク（「この会話を引き継いで質問」・2026-08-23-共有フォーク.md）====

class ForkNotAllowedError(Exception):
    """フォーク不可（403 相当）。`args[0]` に reason（監査 detail・エラーメッセージ用）を持つ:
    `"not_received_share"`（対象が受領共有ラッパーでない）・`"share_unavailable"`（共有が
    取消/期限切れ/招待外）・`"personal_blocked"`（元会話が個人 workspace を参照）。"""


def _fork_title(conv: dict, messages: list) -> str:
    """フォーク先タイトル（是正5・2026-09-05）。

    通常共有（ライブ参照）からのフォークは元 title をそのまま複製する（従来どおり）。
    サニタイズ共有（title が固定文言 `_SANITIZED_TITLE`）からのフォークは、固定文言のままだと
    利用者が新しい会話を一覧で識別できないため、複製する `messages`（＝読者に見えている形・
    伏字済み）のうち最初の**伏字でない**（`content` が `_REDACTED_TEXT` でない）user 発言の
    先頭40文字を title にする（`chat_service._ensure_conversation` と同じ `strip()[:40]` 規則）。
    該当が無ければ「引き継いだ会話」。**元会話（sanitized snapshot のさらに元）の title は
    一切参照しない**（ファイル名等を漏らさないという sanitized snapshot の契約を壊さないため）。
    """
    if conv["title"] != _SANITIZED_TITLE:
        return conv["title"]
    for m in messages:
        if m["role"] == "user" and m["content"] != _REDACTED_TEXT:
            t = (m["content"] or "").strip()[:40]
            if t:
                return t
    return "引き継いだ会話"


def fork_received_share(uid, wid, *, ip_hash=None, user_agent=None) -> int:
    """受領共有ラッパー `wid` を、`uid` 自身の新しい会話として複製する（SH-1）。

    複製元は「読者に見えている形」＝`get_conversation_for_read(uid, wid)` と**同じ検証・
    同じ本文**（伏字済み・route/trace 除去済み・確認カード payload 除去済み）。元スナップショット/
    元会話は一切変更しない（読むだけ）。新会話は `origin='own'`（既存の書込可判定 `owns_conversation`
    をそのまま満たす）・`read_only=FALSE`・`contains_personal_workspace=FALSE`・
    `forked_from_share_id`/`forked_from_user_id`/`forked_at` を持つ。同じラッパーから
    何度でもフォークできる（冪等にしない＝呼ぶたびに新しい会話ができる）。

    RV 是正1（2026-09-05）: 複製（会話＋messages の INSERT）と監査（`share.forked`）を
    **同一トランザクション**で書く（`settings.set_system_settings` と同じ方式）。監査 INSERT の
    例外は psycopg のトランザクション契約に従い `with _connect()` を抜ける際に自動 rollback される
    ため、複製もまとめて取り消される（呼び出し側で別接続の再読取・補償削除は不要）。
    `_audit_insert` は facade（`sherpa.store`）属性経由で実行時解決する（settings.py と同じ理由・
    テストの `monkeypatch.setattr(store, "_audit_insert", …)` シームを保つため）。

    returns 新会話 id。
    raises `LookupError`（`wid` が存在しない/自分のものでない/削除済み＝404 相当）、
    `ForkNotAllowedError`（受領共有ラッパーでない・共有が無効・個人ブロック＝403 相当）。
    監査 INSERT が失敗した場合はその例外がそのまま伝播する（呼び出し側で 500 に変換）。
    """
    _ensure()
    from sherpa import store as _facade   # settings.py と同じ理由: 実行時解決（monkeypatch シーム維持）
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
                "VALUES (%s,%s,%s,%s,NULL,NULL,%s,FALSE)",   # route/trace は常に NULL・personal=FALSE
                (new_cid, m["role"], m["content"], m.get("lens"),
                 Json(m["answer"]) if m.get("answer") is not None else None))
        _facade._audit_insert(
            c, uid, "share.forked", "share",
            f"share:{conv['share_id']}" if conv.get("share_id") is not None else None,
            detail={"wrapper_conversation_id": wid, "new_conversation_id": new_cid,
                    "source_conversation_id": conv.get("source_conversation_id")},
            outcome="success", severity="info", ip_hash=ip_hash, user_agent=user_agent)
        return new_cid


# ==== SH-2: 再共有（「スナップショットを更新」・2026-08-23-共有フォーク.md）====

class ShareNotSanitizedError(Exception):
    """通常共有（元会話をライブ参照＝更新の概念が無い）への refresh 要求（409 相当）。"""


def refresh_sanitized_share(owner_uid, share_id) -> dict:
    """サニタイズ共有のスナップショットを最新の内容へ取り直す（SH-2）。リンク・招待・期限は不変。

    対象は **サニタイズ共有だけ**（`conversation_shares.conversation_id` が
    `origin='sanitized_snapshot'` の会話）。通常共有（元会話をライブ参照）には
    `ShareNotSanitizedError` を送出する（更新の概念が無い＝呼び出し側で 409 に変換）。

    手順（**1トランザクション**）: 対象 share 行を `FOR UPDATE` でロック（同時 refresh/revoke と
    直列化）→ 所有者確認 → 現行 snapshot の `source_conversation_id`（常に元会話を指す・
    refresh を重ねても付け替わらない）から元会話が生きているか確認 → 新 snapshot を
    `_create_sanitized_snapshot_tx` で同一トランザクション上に作る →
    `conversation_shares.conversation_id`/`refreshed_at` を新 snapshot へ →
    受領ラッパー（`origin='received_share' AND share_id=当該`）の `source_conversation_id` を
    新 snapshot へ付け替える（**先に付け替えてから**旧 snapshot を消す） →
    旧 snapshot 行は `deleted_at=now()`（物理削除しない・既存の soft delete と同じ）。

    期限切れでも更新自体は許す（`expires_at` は変えない）。存在しない/取消済み（`revoked_at`
    設定済み）は `LookupError`（404 相当）。所有者不一致は `PermissionError`（403 相当・
    存在確認より先に判定すると所有権の有無が漏れるため、行が存在することが分かった後で判定する）。
    元会話が削除済みなら `LookupError`。

    returns `{"share_id", "old_snapshot_id", "new_snapshot_id", "source_conversation_id", "refreshed_at"}`。

    是正3（2026-09-05）: `accept_share` と share 行／共有 conversation 行を**逆順**でロックするため、
    関数の先頭（下の `FOR UPDATE` より前）で `_share_lock_key` の advisory lock を取って直列化する
    （`_share_lock_key` の docstring 参照）。
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
        if new_snapshot_id is None:   # 直前チェックと同じ条件を見ているため通常到達しない防御的分岐
            raise LookupError(f"元会話が見つかりません（source_conversation_id={source_cid}）")
        refreshed = c.execute(
            "UPDATE conversation_shares SET conversation_id=%s, refreshed_at=now() "
            "WHERE id=%s RETURNING refreshed_at", (new_snapshot_id, share_id)).fetchone()
        c.execute(
            "UPDATE conversations SET source_conversation_id=%s "
            "WHERE origin='received_share' AND share_id=%s AND deleted_at IS NULL",
            (new_snapshot_id, share_id))
        c.execute("UPDATE conversations SET deleted_at=now() WHERE id=%s", (old_snapshot["id"],))
        return {"share_id": share_id, "old_snapshot_id": old_snapshot["id"],
                "new_snapshot_id": new_snapshot_id, "source_conversation_id": source_cid,
                "refreshed_at": refreshed["refreshed_at"]}


def list_shares_for_conversation(owner_uid, cid) -> list[dict]:
    """`cid`（所有者の元会話）を対象にした共有一覧（SH-2・所有者専用）。

    通常共有（`share.conversation_id=cid`）とサニタイズ共有（現行 snapshot の
    `source_conversation_id=cid`）の両方を対象にする——refresh で snapshot が差し替わった後も
    `conversation_shares.conversation_id` は常に「現在の」snapshot を指すため、この JOIN だけで
    最新状態が拾える（置き換え済みの旧 snapshot は refresh 後どの share からも参照されなくなる
    ため二重に出ない）。招待者一覧（`invitees`）は表示名を `shared_by_name` と同じ流儀
    （`users.display_name` の LEFT JOIN）で解決する。
    """
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT s.id AS share_id, (t.origin='sanitized_snapshot') AS sanitized, "
            "  s.created_at, s.expires_at, s.revoked_at, s.refreshed_at, s.last_used_at "
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
