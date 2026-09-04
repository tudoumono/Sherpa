"""EXT-4（拡張設計 §10・UI 階層表示）の e2e。

trace_version=2 のサブエージェント レーン（担当バッジ・集約表示・実行の分担サマリ・検証バッジ・
終了理由）と、trace_version=1（従来）の後方互換を固定する。ストリーミング（render.js の
TraceTreeV2 をライブで使う・stream.js）と履歴復元（同じ TraceTreeV2 を静的に使う・history.js）の
両方が同じ階層描画になることを確認する（EXT-4 の受け入れ条件）。
"""
from __future__ import annotations

from mock_api import (
    IMPACT_ANSWER, V2_BLOCKED_ANSWER, V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE,
    V2_BUCKET_REPARENT_ORDER_A_TRACE,
    V2_BUCKET_REPARENT_ORDER_B_TRACE, V2_BUCKET_REPARENT_ORDER_C_TRACE, V2_BUDGET_ANSWER,
    V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE,
    V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE,
    V2_CODEX_OLLAMA_ANSWER, V2_CONTENT_FILTERED_ANSWER, V2_LANE_ANSWER, V2_LANE_TRACE,
    V2_NOSUB_ANSWER, V2_NOSUB_TRACE, V2_NOUSAGE_ANSWER, V2_PARENT_ID_OUT_OF_ORDER_TRACE,
    V2_PARENT_ID_TRACE, V2_REFUSAL_ANSWER, V2_TOOLS_LIMIT_ANSWER, V2_TRUNCATED_ANSWER,
    V2_UNKNOWN_STOP_REASON_ANSWER, V2_UNKNOWN_STOP_REASON_TRACE, install_api_mocks,
)


def test_v2_live_stream_shows_agent_lane_aggregation_and_summary(page, web_base_url):
    """ライブ配信（trace_meta マーカー→v2 ノード列→answer）で:
    (a) サブエージェント レーンのカードが折りたたみ可能な形で出て、担当バッジ（ローカル: qwen2.5）・
        状態・平文の統計（調査の回数/調べる操作の回数）が表示される（専門用語ゼロ・内部 slug
        "researcher" ではなくサーバの表示名「下調べ役」を出す）、
    (b) 3件並んだ同種操作（資料を検索（語句そのまま））が集約表示（×3）に畳まれ、展開すると個別ノードが見える、
    (c) 「実行の分担」サマリにローカル/クラウド両方の担当が出る、
    (d) 終了理由（自然終了）が明示される、
    (e) サーバがまだ発行していない「候補」統計はレーンに出ない（0 の誤断定をしない）、
    (f) `agent_completed`（完了合図ノード）は「実行の分担」サマリの回数に数えられない
        （既に集計済みの作業の完了通知であり新しい作業ではない）、
    を確認する。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_LANE_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": V2_LANE_ANSWER, "trace": V2_LANE_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")

    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane).to_contain_text("下調べ役")               # metrics.name（サーバ表示名）を優先・内部slug非表示
    expect(lane).not_to_contain_text("researcher")         # 内部 slug（agent_run_id の profile_id）は出さない
    expect(lane).to_contain_text("ローカル: qwen2.5")        # 担当バッジ（is_local="local" をサーバから受け取る）
    expect(lane.locator(".fagent-status")).to_contain_text("完了")
    expect(lane).to_contain_text("調査の回数 1")            # 旧 "Cycle 1"（専門用語ゼロ・中8）
    expect(lane).to_contain_text("調べる操作の回数 4")         # 旧 "Tool 4"（grep×3 + 精読×1）
    expect(lane).not_to_contain_text("候補")                # サーバ未発行のため 0 の誤断定をしない（中6）
    # evidence_committed（evidence-committed ノード）は agent_run_id を持たない（providers/base.py の
    # 実際の発行位置＝根拠ゲート直後・run 全体の最終確定であり特定サブに属さない）＝main 直下に出る。
    expect(page.locator("#flow")).to_contain_text("根拠を確定")
    expect(page.locator("#flow")).to_contain_text("2 件の根拠を機械検証済みとして確定しました")

    agg = lane.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    expect(agg.locator(".fagg-body")).to_be_hidden()   # 既定は折りたたみ（<details> 既定 closed）
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body")).to_be_visible()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).to_contain_text("消費税率")
    expect(agg).to_contain_text("TAX-RATE")

    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("ローカル AI")
    expect(summary).to_contain_text("クラウド AI")
    expect(summary).to_contain_text("回答の合成")
    # `agent_completed`（下調べ役完了ノード）は集計に数えない: 「その他の処理」は
    # search-helper ノード（下調べ役に任せる）1件分のみ（1回×2件＝completed の水増しがあれば2回になる）。
    expect(summary).to_contain_text("その他の処理 1 回")

    expect(page.locator(".ftrace-stopreason")).to_contain_text("自然終了")

    # usage_sub（下調べ役の使用量）の表示名も内部 slug ではなくサーバの表示名
    # （`profile: "下調べ役"`）を出す。
    sub_meta = page.locator(".usage-sub-meta")
    expect(sub_meta).to_have_count(1)
    sub_meta.locator("summary").click()
    expect(sub_meta).to_contain_text("下調べ役: 入力 300 / 出力 40 トークン")
    expect(sub_meta).not_to_contain_text("search-helper")

    # 「進め方を計画」ノード（.fagent の外＝main 直下）を含め、#flow（思考の流れ）・#messages
    # （回答本文・usage_sub の内訳）の両方で、内部 slug（profile_id・agent_run_id）が可視テキストと
    # して一切現れないことを確認する（DOM の id 属性は inner_text に含まれないため、ここで拾えれば
    # 本物の文言漏れ）。
    visible_text = page.locator("#flow").inner_text() + page.locator("#messages").inner_text()
    for slug in ("researcher", "search-helper-openai", "search-helper-ollama", "sub:researcher:1"):
        assert slug not in visible_text, f"内部 slug {slug!r} が画面の可視テキストに出ている: {visible_text!r}"


def test_v2_parent_id_nests_child_nodes_under_parent(page, web_base_url):
    """`parent_id`（拡張設計 §2/§10）を持つノードは、レーン直下の兄弟としてではなく親ノードの
    直下（`.fchildren`）へネストして描画される（plan→step-1→step-1-detail の2段ネスト・
    親→子の到着順）。ライブ配信・履歴復元は同じ `TraceTreeV2.addOrUpdate` を使うため、ここでは
    ライブ配信で固定する（履歴側は test_v2_history_replay_has_same_hierarchy_as_live と同型で
    共通ビルダを検証済み）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_PARENT_ID_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_PARENT_ID_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    plan_step = page.locator("#flow .fstep", has_text="進め方を計画").first
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("資料を検索（語句そのまま）")
    grandchild = child.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(grandchild).to_have_count(1)
    expect(grandchild).to_contain_text("検索結果を確認")


def test_v2_parent_id_child_arriving_before_parent_is_reparented_on_arrival(page, web_base_url):
    """子（`parent_id` 参照先）が親より先に届いても、その時点では一時的にレーン直下（フラット）へ
    置き、親が届いた時点で実 DOM 要素ごと子コンテナへ付け替わる（親が来ない可能性を考慮した
    フォールバック＝一時置きのままでも表示自体は壊れない・親到着後はネストが正しく成立する）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_PARENT_ID_OUT_OF_ORDER_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_PARENT_ID_OUT_OF_ORDER_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    parent_step = page.locator("#flow .fstep", has_text="進め方を計画").first
    child = parent_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("資料を検索（語句そのまま）")


def test_v2_bucket_reparent_order_a_full_cleanup_before_aggregation(page, web_base_url):
    """child(P) → P → 兄弟×2 の順序: 親到着時点でバケットは子1件だけ＝取り除くと0件になり
    バケットごと削除される。残りの兄弟2件は汚れていない新規バケットとして始まり、3件に
    届かないため集約枠は作られない（是正前は次に3件目が来た時に古いバケットの残骸を
    `insertBefore` の anchor に使って `NotFoundError` になっていた）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_BUCKET_REPARENT_ORDER_A_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_BUCKET_REPARENT_ORDER_A_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    plan_step = page.locator("#flow .fstep", has_text="進め方を計画").first
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    expect(page.locator("#flow .fagg")).to_have_count(0)   # 実兄弟2件のみ＝集約閾値(3)に届かない


def test_v2_bucket_reparent_order_b_partial_cleanup_then_later_aggregation(page, web_base_url):
    """child(P) → 兄弟1 → P → 兄弟2 → 兄弟3 の順序: 親到着時点でバケットは集約前の2件
    （child・兄弟1）で、子は先頭（index 0）を占めている——`leafEls` から途中要素として
    splice する経路を通る。残った兄弟1件から後で2件追加されて正しく3件集約される
    （是正前は集約発火時の anchor が既に別要素の子になっていて `insertBefore` が失敗しうる
    経路）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_BUCKET_REPARENT_ORDER_B_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_BUCKET_REPARENT_ORDER_B_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    plan_step = page.locator("#flow .fstep", has_text="進め方を計画").first
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    agg = page.locator("#flow .fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")   # child は集約枠の外（P の下）にいる


def test_v2_bucket_reparent_order_c_detach_from_existing_aggregation_frame(page, web_base_url):
    """child(P) → 兄弟×2（この時点で3件に達し集約枠が既にできている）→ P → 兄弟3 の順序: 親到着時に
    既存の集約枠から1件（child）を取り除き、残り件数が AGG_MIN_RUN(3) 未満になった時点で集約枠
    自体を解体して個別2件表示へ戻す（「AGG_MIN_RUN 未満は常に個別表示」という不変条件を件数の
    増減どちらでも保つ・集約枠を残したまま「×2」のような閾値未満の集約表示が居座ることを
    許さない）。その後に届く4件目（兄弟3）で件数が再び3件に達し、新しく集約枠が作られる
    （「×3」へ再集約）。

    SSE モック（`_sse`）は全イベントを一括配信するため、「detach 直後・4件目到着前」という
    途中経過を、実際のチャット送信フロー（ライブ配信）のテストでは単独で観測できない（EXT-4の
    既知の制約＝一括配信の限界）。そのためこのテストは `web/chat/render.js` の `TraceTreeV2`
    （ライブ配信・履歴復元の両方が使う共通クラス）をブラウザ内で直接 import し、`addOrUpdate`
    を1件ずつ呼びながら各段階の DOM を検査する（production コードと同じメソッド・同じ引数を
    呼ぶだけなので、実際の描画ロジックをそのまま検証できる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("""async () => {
        const { TraceTreeV2 } = await import('/chat/render.js');
        const container = document.createElement('div');
        container.id = 'test-order-c-tree';
        document.body.appendChild(container);
        window.__testTree = new TraceTreeV2(container, { live: false });
    }""")

    def feed(event):
        page.evaluate("(e) => window.__testTree.addOrUpdate(e)", event)

    for e in V2_BUCKET_REPARENT_ORDER_C_TRACE[:4]:   # child-c, sib-c1, sib-c2, plan-c
        feed(e)

    root = page.locator("#test-order-c-tree")
    plan_step = root.locator(".fstep", has_text="進め方を計画").first
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    expect(root.locator(".fagg")).to_have_count(0)          # 閾値未満へ解体済み＝集約枠が無い
    expect(root.locator("> .fstep.tool")).to_have_count(2)  # 兄弟2件（B・C）が個別表示のまま残る

    feed(V2_BUCKET_REPARENT_ORDER_C_TRACE[4])   # 兄弟3（sib-c3）→ 再び3件で集約

    agg = root.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")   # child は集約枠から取り除かれ P の下にいたまま
    expect(child).to_have_count(1)         # 再集約後も P の下の子は引き続き1件のまま


def test_v2_bucket_dismantle_preserves_arrival_order_of_interleaved_siblings(page, web_base_url):
    """child(K,P) → 別種ノードX → 兄弟B(K) → 兄弟C(K)（この時点で3件集約）→ P の順序: 親到着で
    集約枠が解体される時、残った兄弟（B・C）を「枠の旧位置へまとめて insertBefore」すると、
    枠の外側に挟まっていた別種ノードXとの到着順が壊れる（本来 X,B,C,P の順であるべきところ
    B,C,X,P になっていた）。各要素は自分の到着順（`_seq`）が指す正しい兄弟位置へ個別に
    戻るべきで、解体後は再集約せず個別2件のまま（3件目は来ない）ことも合わせて確認する。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#flow .fagg")).to_have_count(0)   # 兄弟2件のみ＝再集約はしない
    # ライブ配信の実 bodyEl は `#flow details.fturn .fturn-body`（TraceTreeV2 のコンテナ・
    # stream.js::_liveBodyEl）——`#flow` 自体の直接の子ではない。
    top = page.locator("#flow details.fturn .fturn-body > .fstep")
    expect(top).to_have_count(4)
    expect(top.nth(0)).to_contain_text("念のため確認")
    expect(top.nth(1)).to_contain_text("B")
    expect(top.nth(2)).to_contain_text("C")
    expect(top.nth(3)).to_contain_text("進め方を計画")
    child = top.nth(3).locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def test_v2_bucket_surviving_frame_repositions_on_detach(page, web_base_url):
    """child(K,P) → 別種ノードX → 兄弟B(K) → 兄弟C(K)（集約発火）→ 兄弟D(K)（4件目・枠は解体
    されず存続）→ P の順序: 親到着で枠内の最古参（child）を取り除いても枠自体は存続する
    （残り3件で AGG_MIN_RUN を満たすため）。枠の到着順（`_seq`）と DOM 上の位置を残存メンバーの
    最古参へ更新し直さないと、取り除かれた最古参の位置（X より前）に取り残され、本来
    X, 枠(B・C・D), P であるべき順序が 枠, X, P になっていた。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    body = "#flow details.fturn .fturn-body"
    top = page.locator(f"{body} > .fstep, {body} > .fagg")
    expect(top).to_have_count(3)
    expect(top.nth(0)).to_contain_text("念のため確認")
    agg = top.nth(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    expect(top.nth(2)).to_contain_text("進め方を計画")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")   # child は集約枠から取り除かれ P の下にいる
    plan_step = top.nth(2)
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def test_v2_bucket_dismantle_repositions_around_subagent_lane(page, web_base_url):
    """child(K,P) → 兄弟B(K) → サブエージェント レーン開始 → 兄弟C(K)（この時点で3件集約）→ P
    の順序: レーン直下に実コンテンツとして置かれる `.fagent`（サブエージェントのレーン枠）にも
    到着順（`_seq`）が要る——無いと集約枠の解体時の兄弟位置比較から漏れ、本来
    B, レーン, C, P であるべき順序が レーン, B, C, P になっていた。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": IMPACT_ANSWER, "trace": V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#flow .fagg")).to_have_count(0)   # 兄弟2件のみ＝解体後は再集約しない
    body = "#flow details.fturn .fturn-body"
    top = page.locator(f"{body} > .fstep, {body} > .fagent")
    expect(top).to_have_count(4)
    expect(top.nth(0)).to_contain_text("B")
    expect(top.nth(1)).to_contain_text("下調べ役")   # サブエージェント レーン（.fagent）
    expect(top.nth(2)).to_contain_text("C")
    expect(top.nth(3)).to_contain_text("進め方を計画")
    plan_step = top.nth(3)
    child = plan_step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def test_v2_nosub_conversation_summary_says_all_cloud(page, web_base_url):
    """下調べ役 OFF（サブレーン無し）の trace_version=2 会話は「実行の分担」サマリが
    「すべてクラウド AI が担当しました」に縮退する（`answer.usage.is_local="cloud"` をサーバから
    受け取る）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_NOSUB_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": V2_NOSUB_ANSWER, "trace": V2_NOSUB_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".fagent")).to_have_count(0)   # サブレーンは無い
    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("すべてクラウド AI が担当しました")


def test_v2_codex_ollama_usage_is_not_misclassified_as_cloud(page, web_base_url):
    """Codex(Ollama) 構成（`answer.usage.provider == "codex"`・`is_local: "local"`）を
    「すべてローカル AI が担当しました」と正しく表示する固定（Codex は常に provider="codex" を
    名乗るため、provider 文字列だけで判定すると「すべてクラウド」に誤分類されうる）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101,
              "message": {"answer": V2_CODEX_OLLAMA_ANSWER, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("すべてローカル AI が担当しました")
    expect(summary).not_to_contain_text("クラウド")


def test_v2_missing_usage_shows_unknown_not_a_guess(page, web_base_url):
    """`answer.usage` が無い（旧メッセージ・heuristic 相当）ときは「担当不明」と誠実に表示し、
    「すべてローカル」「すべてクラウド」のどちらにも決め打たない（誤断定より不明の方が
    安全という方針）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101,
              "message": {"answer": V2_NOUSAGE_ANSWER, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("担当不明")
    expect(summary).not_to_contain_text("すべてローカル")
    expect(summary).not_to_contain_text("すべてクラウド")


def test_v2_stop_reason_blocked_and_budget_categories(page, web_base_url):
    """終了理由は `answer.data.evidence_packet.stop_reason`（クローズド語彙）だけを根拠にする
    （trace 配列を漁って推測しない）。クローズド語彙の各値が対応する平文へ変換されることを固定する。"""
    from playwright.sync_api import expect

    for answer, expected in (
        (V2_BLOCKED_ANSWER, "根拠不足で中断"),
        (V2_BUDGET_ANSWER, "調査の上限に到達"),
        (V2_TOOLS_LIMIT_ANSWER, "調べる操作の回数の上限に到達"),
        (V2_REFUSAL_ANSWER, "AI が回答を控えた"),
        (V2_TRUNCATED_ANSWER, "出力上限で途中終了"),
        (V2_CONTENT_FILTERED_ANSWER, "内容の制限で終了"),
    ):
        events = [{"type": "trace_meta", "trace_version": 2},
                 {"type": "answer", "conversation_id": 101, "message": {"answer": answer, "trace": []}}]
        install_api_mocks(page, stream_events=events)
        page.goto(f"{web_base_url}/chat.html")
        page.locator("#input").fill("消費税率を変えたい。影響は？")
        page.locator("#send").click()
        expect(page.locator("#messages")).to_contain_text("影響範囲分析")
        expect(page.locator(".ftrace-stopreason")).to_contain_text(expected)


def test_v2_unknown_stop_reason_is_honest_not_a_guess(page, web_base_url):
    """対応表に無い stop_reason（将来の新しい値・壊れたデータ等）は「終了理由を確認できません
    でした」へ誠実に落ちる。回答まで到達した経路（evidence_packet 経由）は既知/未知を問わず
    「中断」扱いにしない契約なので、実行中（`active`）のまま answer を迎えたレーンも
    aborted 表示にならず「完了」になる（未知＝不明であって失敗ではない・レーンが無いと
    この契約自体を検証できないため active レーンを含むフィクスチャを使う）。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2}] + V2_UNKNOWN_STOP_REASON_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": V2_UNKNOWN_STOP_REASON_ANSWER, "trace": V2_UNKNOWN_STOP_REASON_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".ftrace-stopreason")).to_contain_text("終了理由を確認できませんでした")
    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane.locator(".fagent-status")).to_contain_text("完了")
    expect(lane.locator(".fagent-status.aborted")).to_have_count(0)
    expect(page.locator(".fagent-status.aborted")).to_have_count(0)


def test_v2_question_pause_shows_no_stop_reason_note(page, web_base_url):
    """確認カード（ask_user）でターンが一時停止したときは、Evidence Packet を持たない正常な
    一時停止であり「終了理由」note を出さない（誤って「終了理由を確認できませんでした」等を
    出さない）。ティックは確実に止まる。"""
    from playwright.sync_api import expect

    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "node", "id": "u1", "kind": "think", "label": "質問を理解",
              "detail": "内容を把握しました", "status": "done"},
             {"type": "question", "conversation_id": 101, "interaction_id": "q1", "mode": "single",
              "prompt": "対象範囲を選んでください", "options": [{"id": "a", "label": "全体"},
                                                        {"id": "b", "label": "一部"}],
              "allow_free_text": False}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator(".askcard")).to_be_visible()
    expect(page.locator(".ftrace-stopreason")).to_have_count(0)


def test_v2_lane_status_becomes_aborted_when_connection_drops_mid_run(page, web_base_url):
    """サブエージェントが実行中（`active`）のまま接続が切れた（SSE の応答本文が終わり
    `EventSource.onerror` が発火する＝停止要求の無い予期しない切断）場合、レーンの状態は
    「完了」ではなく「中断」になる（自然終了/上限到達/根拠不足は回答へたどり着いているため
    「完了」のまま、という区別＝`TraceTreeV2.finalize` の `stopInfo.interrupted`）。

    注記（e2e 設計）: Playwright の `route.fulfill` は SSE 応答を一括配信し、配信し終えると
    ブラウザ側は接続終了とみなして自動的に `onerror` を発火させる（このモック機構の既知の限界＝
    `stop` POST 経由の明示停止と "stopped" イベント到着の間の一瞬だけを狙って観測するテストは
    作れない）。ここでは「サブが調査中のまま接続が切れる」という、その限界の中でも決定的に
    再現できる実際のシナリオ（ネットワーク瞬断等）をそのまま検証対象にする。"""
    from playwright.sync_api import expect

    # サブが調査中（still active・agent_completed 未到達）のまま応答本文が終わる
    # （route.fulfill は一括配信のため、これだけで EventSource は「切断」と見なし onerror が飛ぶ）。
    active_lane_events = [
        {"type": "trace_meta", "trace_version": 2},
        {"type": "node", "id": "search-helper", "kind": "think", "label": "下調べ役に任せる",
         "detail": "qwen2.5 が資料を探して読みます", "status": "done", "agent_run_id": "sub:researcher:1",
         "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
        {"type": "node", "id": "sub:researcher:1:grep-1", "kind": "tool", "label": "資料を検索（語句そのまま）",
         "detail": "「消費税率」", "status": "active", "agent_run_id": "sub:researcher:1",
         "metrics": {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}},
    ]

    install_api_mocks(page, stream_events=active_lane_events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane.locator(".fagent-status")).to_contain_text("中断")
    expect(page.locator(".ftrace-stopreason")).to_contain_text("エラー")


def _wait_until(predicate, timeout_ms=5000, interval_ms=20, page=None, message="条件が満たされなかった"):
    """`predicate()` が真になるまで `page.wait_for_timeout` で短間隔ポーリングする（Python 側
    ハンドラが route を保留し終えるタイミングを確定的に待つための本ファイル内ローカルヘルパ）。"""
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(interval_ms)


def _settle(page):
    """保留していた route を解放した直後、その Promise 継続（fetch 応答→JSON 解析→後続処理）が
    実際に最後まで走り切るのを待ってから戻る（本物の fetch 往復を待つことで空振り assert を防ぐ・
    test_chat_ui.py の同名ヘルパと同じ手法）。"""
    page.evaluate("async () => { try { await fetch('/chat/turns/running'); } catch (e) {} }")


def test_v2_send_discards_stale_turn_start_response_after_conversation_switch(page, web_base_url):
    """開始 POST（`POST /chat/turns`）の応答待ち中に会話を切り替えた場合、後から届くその応答は
    もう関係ない旧世代として破棄される（`send()` の `turnGen` を `await` の前後で捕捉・照合する
    契約＝停止フローの `turnGen` と同型）。破棄しないと、旧ターンの応答が `S.cid`/`S.turnId` を
    上書きし、切替先の画面に旧ターンの購読（EventSource）や v2 のティックが復活してしまう。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    held: dict = {}

    page.route("**/chat/turns", lambda route: held.__setitem__("turn_start_route", route))
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "turn_start_route" in held, page=page,
                message="開始 POST（POST /chat/turns）が届かなかった")

    # 開始 POST の応答がまだ届いていない間に、新規チャットへ切り替える（=turnGen を進める経路）。
    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")   # 新しい画面（welcome）に切り替わった
    expect(page.locator("#rt")).to_contain_text("待機中")

    # 保留していた旧ターンの開始 POST の応答を今ごろ返す（切替後に遅れて届く想定の再現）。
    held.pop("turn_start_route").fulfill(
        content_type="application/json",
        body=json.dumps({"turn_id": "turn-stale", "conversation_id": 999}))
    _settle(page)

    # 世代が進んでいるため、この遅延応答は無視され、EventSource による購読は張られない
    # （張られていれば #rt が「リアルタイム」等へ変わってしまう＝新規チャット画面のまま「待機中」）。
    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(page.locator("#messages")).to_contain_text("ようこそ")   # welcome 画面のまま（旧ターンの再送等が起きていない）


def test_v2_stop_pending_then_post_failure_corrects_trace_stop_reason(page, web_base_url):
    """停止 POST の結果待ち中に SSE が切れ（`onerror` が先着し「停止操作」を暫定表示）、後から
    停止 POST 自体が失敗（＝停止起因ではない本物の接続断）と判明した場合、v2 トレースの
    「終了理由」note も `#rt`/思考枠と同じく「停止操作」→「エラー」へ訂正される
    （`TraceTreeV2.correctStopReason`・`pendingStopTraceTree` 契約）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route   # fulfill せず保留＝送信中の状態を作る

    def handle_stop(route):
        held["stop_route"] = route   # 停止 POST の応答は保留（先に onerror 側の暫定表示を確認する）
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            # trace_meta ＋ 1ノードだけを配信して終える＝ route.fulfill の一括配信の性質上、
            # これだけで EventSource は「切断」と見なし onerror が飛ぶ（"stopped" 等の終端
            # イベントは送らない＝停止起因ではない予期しない切断を再現する）。
            body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in [
                {"type": "trace_meta", "trace_version": 2},
                {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
                 "detail": "内容を把握しました", "status": "active"},
            ])
            stream_route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    page.locator("#send").click()   # 停止操作 → 停止 POST 発行と同時に上の handle_stop が SSE を切る
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")

    expect(page.locator(".ftrace-stopreason")).to_contain_text("停止操作")   # onerror 先着の暫定表示

    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))

    expect(page.locator(".ftrace-stopreason")).to_contain_text("エラー")   # 停止 POST 失敗後の訂正
    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")


def test_v2_history_replay_has_same_hierarchy_as_live(page, web_base_url):
    """保存済みターン（trace_version=2・V2_LANE_TRACE・会話111）を開くと、ライブ時と同じ
    TraceTreeV2（render.js）で階層描画される（サブエージェント レーン・集約・終了理由）。
    ストリーミングと履歴復元が同じ描画経路を共有していることの固定（EXT-4 受け入れ条件）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(111)")

    turn = page.locator(".fturn").first
    expect(turn).to_contain_text("進め方を計画")

    lane = turn.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane).to_contain_text("下調べ役")
    expect(lane).to_contain_text("ローカル: qwen2.5")
    expect(lane.locator(".fagent-status")).to_contain_text("完了")

    agg = lane.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")

    expect(turn.locator(".ftrace-stopreason")).to_contain_text("自然終了")

    # 履歴復元でも「進め方を計画」等の内部 slug 漏れが無いことを固定する（ライブ側の同名アサーションと対）。
    turn_text = turn.inner_text()
    for slug in ("researcher", "search-helper-openai", "search-helper-ollama", "sub:researcher:1"):
        assert slug not in turn_text, f"内部 slug {slug!r} が履歴表示の可視テキストに出ている: {turn_text!r}"


_V1_FLAT_NODES = [
    {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
     "detail": "内容を把握しました", "status": "done"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "label": "関係グラフを照会",
     "detail": "「消費税率」", "status": "done"},
]


def test_v1_conversations_render_flat_without_lanes_or_summary(page, web_base_url):
    """trace_version=1 は、サブエージェント レーン・集約・「実行の分担」サマリのいずれも出ない
    （後方互換・§2.3）。実サーバ（`chat_service.stream_message`）は TOGGLE-RM（2026-09-03）で
    v1 退避トグル（旧 `SHERPA_EXEC_EVENT_V2=0`）を撤去済みのため、新規ターンで v1 の
    `trace_meta`/`trace_version:1` を送ることはもう無い——ここでの v1 イベント列は、撤去前に
    既に保存済みの過去メッセージ（`messages.trace`）を模したもの。フロントが「未知の type を
    無視する」契約（`onmessage` の if チェーンに引っかからないイベントは無視）を含め、
    v1 形式の後方互換描画自体は引き続き固定する価値があるため両方のケースを確認する。"""
    from playwright.sync_api import expect

    answer_event = {"type": "answer", "conversation_id": 101, "message": {"answer": IMPACT_ANSWER}}
    for events in (
        [{"type": "trace_meta", "trace_version": 1}] + _V1_FLAT_NODES + [answer_event],   # 実サーバの実際の配信形
        _V1_FLAT_NODES + [answer_event],   # マーカー自体が無い場合の後方互換も併せて固定する
    ):
        install_api_mocks(page, stream_events=events)
        page.goto(f"{web_base_url}/chat.html")
        page.locator("#input").fill("消費税率を変えたい。影響は？")
        page.locator("#send").click()

        expect(page.locator("#messages")).to_contain_text("影響範囲分析")
        expect(page.locator("#flow .fstep")).to_have_count(2)
        expect(page.locator(".fagent")).to_have_count(0)
        expect(page.locator(".fagg")).to_have_count(0)
        expect(page.locator(".provider-summary")).to_have_count(0)
        expect(page.locator(".ftrace-stopreason")).to_have_count(0)


def test_v1_answer_never_shows_verification_badge_even_with_evidence_packet(page, web_base_url):
    """検証バッジは trace_version=2 の回答に限定する。trace_version が無い（v1）回答は、
    たとえ `data.evidence_packet.evidence` を持っていてもバッジを出さない（従来どおり
    EV-0 の根拠/参考2区分のみ・byte-identical を保つ）。"""
    from playwright.sync_api import expect

    v1_answer_with_packet = {
        "lens": "qa", "headline": "確認しました。",   # trace_version キー無し＝v1
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                          "quote": "消費税率は10%", "span": [3, 3]}],
            "evidence_packet": {"evidence": [
                {"evidence_id": "ev-1", "source_type": "document",
                 "source_path": "4期/02_設計/01_基本設計/税計算仕様書.md",
                 "source_span": [3, 3], "verification_method": "span_verified", "used": True},
            ]},
        },
        "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                    "download_url": "/documents/download?world=w1&rel=x"}],
        "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
    }
    events = [{"type": "answer", "conversation_id": 101, "message": {"answer": v1_answer_with_packet}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("確認しました。")
    expect(page.locator(".verif-badge")).to_have_count(0)


def test_thinking_placeholder_ticks_while_waiting_for_first_event(page, web_base_url):
    """待ち時間（LLM 応答待ち・最初のイベントが来るまでの無音区間）を「止まって見えない」ように
    ティックする（利用者決定 2026-08-28・v1/v2 共通の改善＝trace_version 判定を待たずに動く）。
    `/chat/turns/*/stream` への最初の購読をわざと保留し（route を fulfill しない＝サーバがまだ
    何も返していない状態を模す）、Playwright の仮想クロックで経過させると「AI が考えています
    （n秒）」に切り替わることを固定する。"""
    from playwright.sync_api import expect

    def handle_turn_stream(route):
        return   # fulfill しない＝保留（最初のイベントがまだ来ていない無音区間を作る）

    install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator(".thinking")).to_contain_text("回答を準備しています")
    page.clock.fast_forward(3000)
    expect(page.locator(".thinking")).to_contain_text("AI が考えています（3秒）")
    page.clock.fast_forward(4000)
    expect(page.locator(".thinking")).to_contain_text("AI が考えています（7秒）")


def test_thinking_ticker_stops_after_answer_arrives(page, web_base_url):
    """ターン終端（answer 到着）後は「考え中」ティックが確実に止まる——thinking プレースホルダ
    自体が回答カードに差し替わって DOM から外れるため、以後どれだけ仮想時間を進めてもエラーや
    テキストの巻き戻りが起きないことを固定する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # 既定 stream_events（即座に answer が返る）
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".thinking")).to_have_count(0)   # 回答カードに差し替わっている
    page.clock.fast_forward(10000)   # ティックが残っていてもエラーにならず、何も表示に影響しない
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")


def test_v2_verification_badge_shown_on_grounded_source(page, web_base_url):
    """検証バッジ（§4.6・EV-0 の最小版を超える粒度）: Evidence Packet の verification_method を
    持つ出典に、方式に応じたバッジ（機械検証済み（該当箇所一致）等）が付く。"""
    from playwright.sync_api import expect

    qa_answer = {
        "lens": "qa", "headline": "確認しました。", "trace_version": 2,
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                          "quote": "消費税率は10%", "span": [3, 3]}],
            "evidence_packet": {"evidence": [
                {"evidence_id": "ev-1", "source_type": "document",
                 "source_path": "4期/02_設計/01_基本設計/税計算仕様書.md",
                 "source_span": [3, 3], "verification_method": "span_verified", "used": True},
            ]},
        },
        "sources": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
             "download_url": "/documents/download?world=w1&rel=x"},
        ],
        "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
    }
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": qa_answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("確認しました。")
    badge = page.locator(".verif-badge")
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("機械検証済み（該当箇所一致）")


def test_v2_verification_badge_unknown_method_is_neutral_not_verified(page, web_base_url):
    """対応表に無い verification_method（将来の新しい値・壊れたデータ等）は「機械検証済み」
    （緑）へフォールバックしない——実際には検証方法が分からないのに検証済みだと誤断定しない。
    中立の「検証方法不明」バッジを出す（緑の "ok" クラスにはならない）。"""
    from playwright.sync_api import expect

    qa_answer = {
        "lens": "qa", "headline": "確認しました。", "trace_version": 2,
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                          "quote": "消費税率は10%", "span": [3, 3]}],
            "evidence_packet": {"evidence": [
                {"evidence_id": "ev-1", "source_type": "document",
                 "source_path": "4期/02_設計/01_基本設計/税計算仕様書.md",
                 "source_span": [3, 3], "verification_method": "future_unknown_method", "used": True},
            ]},
        },
        "sources": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
             "download_url": "/documents/download?world=w1&rel=x"},
        ],
        "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
    }
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": qa_answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("確認しました。")
    badge = page.locator(".verif-badge")
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("検証方法不明")
    expect(badge).not_to_contain_text("機械検証済み")
    expect(badge).to_have_class("verif-badge unknown")


def test_v2_verification_badge_constructor_value_does_not_hit_object_prototype(page, web_base_url):
    """`verification_method: "constructor"`（プレーンオブジェクトの継承プロパティ名と衝突する値）
    でも `Object.prototype.constructor`（truthy な関数）を誤って引き当てず、通常の未知値と同じ
    中立の「検証方法不明」バッジになる（`VERIFICATION_BADGE_LABEL` が `Object.create(null)` で
    prototype を持たないことの固定）。"""
    from playwright.sync_api import expect

    qa_answer = {
        "lens": "qa", "headline": "確認しました。", "trace_version": 2,
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                          "quote": "消費税率は10%", "span": [3, 3]}],
            "evidence_packet": {"evidence": [
                {"evidence_id": "ev-1", "source_type": "document",
                 "source_path": "4期/02_設計/01_基本設計/税計算仕様書.md",
                 "source_span": [3, 3], "verification_method": "constructor", "used": True},
            ]},
        },
        "sources": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
             "download_url": "/documents/download?world=w1&rel=x"},
        ],
        "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
    }
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": qa_answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("確認しました。")
    badge = page.locator(".verif-badge")
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("検証方法不明")
    expect(badge).not_to_contain_text("機械検証済み")
    expect(badge).to_have_class("verif-badge unknown")


def test_v2_stop_reason_constructor_value_does_not_hit_object_prototype(page, web_base_url):
    """`stop_reason: "constructor"` でも `Object.prototype.constructor` を誤って引き当てず、
    通常の未知値と同じ「終了理由を確認できませんでした」になる（`STOP_REASON_TOKEN_LABEL` が
    `Object.create(null)` で prototype を持たないことの固定）。"""
    from playwright.sync_api import expect

    answer = {**IMPACT_ANSWER, "trace_version": 2,
             "data": {**IMPACT_ANSWER["data"], "evidence_packet": {"stop_reason": "constructor"}},
             "usage": {"provider": "openai", "model": "gpt-5.5",
                      "input_tokens": 300, "output_tokens": 40,
                      "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "cloud"}}
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".ftrace-stopreason")).to_contain_text("終了理由を確認できませんでした")


def test_v2_is_local_constructor_value_does_not_hit_object_prototype(page, web_base_url):
    """`usage.is_local: "constructor"` でも `Object.prototype.constructor`（truthy な関数）を
    誤って引き当てず、通常の未知値と同じ「担当不明」バッジになる（`LOCALITY_LABEL`/
    `_LOCALITY_BADGE_CLASS` が `Object.create(null)` で prototype を持たないことの固定）。"""
    from playwright.sync_api import expect

    answer = {**IMPACT_ANSWER, "trace_version": 2,
             "data": {**IMPACT_ANSWER["data"], "evidence_packet": {"stop_reason": "no_tool_calls"}},
             "usage": {"provider": "openai", "model": "gpt-5.5",
                      "input_tokens": 300, "output_tokens": 40,
                      "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": "constructor"}}
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("担当不明")
    expect(summary).not_to_contain_text("function")   # Object.prototype.constructor を誤って表示しない


def test_v2_verification_badge_matches_via_matched_doc_ids(page, web_base_url):
    """検証バッジは citation 単体の `source_path` だけでなく、list_docs/graph_neighbors 由来の
    集計 Evidence（`source_path: null`・`matched_doc_ids` に複数 doc を持つ）にも対応する。
    集計 Evidence が裏付ける doc の出典にもバッジが付くことを固定する。"""
    from playwright.sync_api import expect

    qa_answer = {
        "lens": "qa", "headline": "確認しました。", "trace_version": 2,
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [],
            "evidence_packet": {"evidence": [
                {"evidence_id": "ev-1", "source_type": "document", "source_path": None,
                 "verification_method": "list_docs_verified", "used": True,
                 "matched_doc_ids": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
                 "list_meta": {"count": 1, "prefix": "4期/02_設計"}},
            ]},
        },
        "sources": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
             "download_url": "/documents/download?world=w1&rel=x"},
        ],
        "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
    }
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101, "message": {"answer": qa_answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("確認しました。")
    badge = page.locator(".verif-badge")
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("機械検証済み（一覧確認）")


def test_v2_lane_labels_and_badges_stay_single_line_in_narrow_pane(page, web_base_url):
    """右ペイン（既定幅 300px・.pane.right）でレーン内のステップ名（.flabel）・レーン名
    （.fagent-name）が、幅固定の担当バッジと同じ行に押し込まれて1文字ずつ縦に折り返す不具合の
    回帰固定。折返し発生時は bounding box の高さが行数分だけ
    大きくなる（1行 ≈ 20px 前後）ため、2行分の余裕を持たせた閾値（32px）を明らかに超えないことで
    「1行に収まっている」ことを検証する。担当バッジがペイン右端からはみ出していないことも併せて
    確認する（is_in_viewport ではなく bounding box の右端座標をペインの右端と比較）。"""
    from playwright.sync_api import expect

    page.set_viewport_size({"width": 1366, "height": 900})   # 右ペインは既定 300px 固定幅の列
    events = [{"type": "trace_meta", "trace_version": 2}] + V2_LANE_TRACE + [
        {"type": "answer", "conversation_id": 101,
         "message": {"answer": V2_LANE_ANSWER, "trace": V2_LANE_TRACE}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator(".fagent")).to_have_count(1)
    pane_box = page.locator(".pane.right").bounding_box()
    assert pane_box is not None
    right_edge = pane_box["x"] + pane_box["width"]

    # 1行 ≈ 20px 前後（font-size 13.5px・line-height 1.6）。長いラベル（例:「下調べ役が完了しました」）
    # は2行に折り返すこと自体は正常（word-break:keep-all の意図どおり）——ここで検知したいのは
    # 「1文字ずつ縦積み」の病的な折返し（元の不具合は height≈170px だった）なので、2〜3行分の
    # 余裕（50px）を許容しつつ、それを明らかに超える異常だけを拾う。
    ONE_LINE_MAX_PX = 50
    labels = page.locator(".fagent .flabel")
    for i in range(labels.count()):
        box = labels.nth(i).bounding_box()
        assert box is not None and box["height"] <= ONE_LINE_MAX_PX, (
            f".flabel[{i}] が複数行に折り返している（1文字ずつ縦積みの再発）: {box}")

    name_box = page.locator(".fagent-name").first.bounding_box()
    assert name_box is not None and name_box["height"] <= ONE_LINE_MAX_PX, (
        f".fagent-name が複数行に折り返している: {name_box}")

    badges = page.locator(".provider-badge")
    for i in range(badges.count()):
        box = badges.nth(i).bounding_box()
        assert box is not None and (box["x"] + box["width"]) <= right_edge + 1, (
            f".provider-badge[{i}] がペイン右端からはみ出している: {box} (right_edge={right_edge})")
