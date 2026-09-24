"""S3: mock_api.py 契約ドリフト対策（フェーズ7・docs/proposals/2026-07-02-リファクタリング計画.md）。

`tests/e2e/mock_api.py` は Playwright e2e が使う偽装 API 応答（実 DB/Neo4j を使わない）。実 API の
応答形状が変わっても mock 側は自動追随しないため、乖離（実ドリフト）に気付かないまま e2e が
「見せかけの緑」になりうる（S3 事前分析で 10 件の実ドリフトを発見・是正済み）。

このファイルは3つを検査する:
  (a) 主要 GET を `TestClient(sherpa.api.app)` + `auth_disabled` で実際に叩き、mock_api.py の
      対応する定数と形状（キー集合）を突合する。
  (b) `mock_api.MOCKED`（機械可読レジストリ）の各 (method, path) が実ルート表 golden
      （`tests/api/goldens/routes.txt`）に実在することを検査する（旧ルート撤去・改名の検知）。
  (c)（フェーズ7-1・response_model 実測）主要な mock 定数を `sherpa/schemas.py` の応答モデルで
      `TypeAdapter` 検証する。(a) の粒度（トップレベルのみ・ネスト1段の list は mock⊆実）より
      深い階層（nested dict の中身・2段目以降の list 要素）まで固定できるため、(a) では
      検知できない mock drift をここで拾う（実装時に発見: `GET /graph` の `counts` が
      `{"documents": 3}` のみで実の10フィールドを欠いていた等・是正済み）。

比較の粒度（(a)(b) は意図的に緩い＝mock がテスト対象の全内部実装に追随する必要はない）:
  - トップレベル: キー集合の**完全一致**（mock だけのキー／実だけのキーの両方を検知）。
  - ネスト1段の list 値: 先頭要素のキー集合が **mock ⊆ 実**（mock が実に無い架空キーを
    持っていないかだけを見る・実がキーを増やすのは許容）。dict 値・2段目以降は対象外。
  - 値そのもの（内容・件数）は比較しない＝fixtures/dev DB の実データ量に依存しない。

対象 world は fixtures 既定の "v1"（mock の世界 "w1" とは別物・比較するのは形状のみ）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from sherpa import schemas as sc
from sherpa.api import app

# tests/e2e/mock_api.py を bare import する（tests/_corpus_expect.py と同じ流儀・
# tests/conftest.py が ROOT/tests を sys.path に載せるが tests/e2e 自体は載らないため個別追加）。
_E2E_DIR = pathlib.Path(__file__).resolve().parents[1] / "e2e"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))

import mock_api  # noqa: E402

_GOLDEN_ROUTES = pathlib.Path(__file__).resolve().parent / "goldens" / "routes.txt"


def _list_key_diff(mock_list, real_list, label: str) -> list[str]:
    """ネスト1段の list 値: 先頭要素のキー集合が mock ⊆ 実であることを確認する。"""
    if not (isinstance(mock_list, list) and isinstance(real_list, list) and mock_list and real_list):
        return []   # どちらかが空/list でない＝比較不能（実データ0件は失敗にしない）
    m0, r0 = mock_list[0], real_list[0]
    if not (isinstance(m0, dict) and isinstance(r0, dict)):
        return []
    extra = set(m0) - set(r0)
    return [f"{label}[0]: mock にあり実に無いキー: {sorted(extra)}"] if extra else []


def _shape_diff(mock_val: dict, real_val: dict, label: str) -> list[str]:
    """トップレベル dict のキー集合完全一致＋ネスト1段 list の mock⊆実。"""
    errors: list[str] = []
    mk, rk = set(mock_val), set(real_val)
    if mk != rk:
        errors.append(f"{label}: トップレベルキーが不一致（mock のみ={sorted(mk - rk)} 実のみ={sorted(rk - mk)}）")
    for k in mk & rk:
        errors += _list_key_diff(mock_val[k], real_val[k], f"{label}.{k}")
    return errors


@pytest.fixture
def client(auth_disabled):
    return TestClient(app)


# (path, params, mock 定数) — 副作用の無い GET のみ。mock 定数は tests/e2e/mock_api.py 側で
# S3 是正済み（ホイスト済みの定数を参照する＝二重管理しない）。
_CASES = [
    ("/scopes", {"world": "v1"}, mock_api.SCOPES),
    ("/graph", {"world": "v1"}, mock_api.GRAPH),
    ("/graph/facets", {}, mock_api.GRAPH_FACETS_RESP),
    ("/world-options", {}, mock_api.WORLD_OPTIONS_RESP),
    ("/config", {}, mock_api.CONFIG_RESP),
    ("/settings", {}, mock_api.SETTINGS_RESP),
    ("/admin/settings", {}, mock_api.SYSTEM_SETTINGS_VIEW),
    ("/admin/users", {}, {"users": mock_api.USERS}),
    ("/admin/usage/stats", {}, mock_api.USAGE_STATS_DEFAULT),
    ("/admin/audit", {}, {"rows": mock_api.AUDIT_ROWS, "count": 0, "offset": 0, "limit": 0}),
    ("/announcements", {}, {"announcements": mock_api.ANNOUNCEMENTS}),
    ("/chat/turns/running", {}, mock_api.CHAT_TURNS_RUNNING_EMPTY),
    ("/ingest/preview", {"world": "v1"}, mock_api.PREVIEW),
]


@pytest.mark.parametrize("path,params,mock_val", _CASES, ids=[c[0] for c in _CASES])
def test_get_response_shape_matches_mock(client, path, params, mock_val):
    r = client.get(path, params=params)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"
    errors = _shape_diff(mock_val, r.json(), path)
    assert not errors, "\n".join(errors)


def test_auth_me_shape_matches_mock(client):
    """/auth/me は current_user 依存の動的応答のため、mock_api.py 側は USER_ADMIN 定数を土台に
    ハンドラ内で auth_disabled を解決して返す。RV LOW（2026-07-14 フェーズ7 1巡目）: ハードコードの
    キー集合ではなく **mock 側の実応答形**（USER_ADMIN ∪ ハンドラ付与キー）と実 API を突合する
    （mock が auth_disabled 等のキーを落とす退行をこのテストで検知できるように）。"""
    r = client.get("/auth/me")
    assert r.status_code == 200
    # RV LOW（2巡目）: mock の**実応答関数そのもの**（auth_me_response・handler が使う唯一の組み立て）
    # を実行して比較する＝mock 側がキーを落とす drift を確実に検知できる。
    mock_shape = set(mock_api.auth_me_response(mock_api.USER_ADMIN))
    assert set(r.json()) == mock_shape, (
        f"実 /auth/me と mock 応答形が不一致: 実のみ={set(r.json()) - mock_shape} "
        f"mockのみ={mock_shape - set(r.json())}"
    )


def test_conversations_list_and_detail_shape_matches_mock(client):
    """/conversations・/conversations/{cid} の形状突合。RV LOW（2026-07-14 フェーズ7 1巡目）:
    「dev DB に会話が無ければ skip」だとクリーン DB の CI で契約保証が消えるため、
    無ければ store 直で最小の会話＋メッセージを seed してから比較する（形状のみの比較なので
    seed 内容は任意・uid は auth_disabled の合成 admin と同じ 'admin'）。"""
    from sherpa import store
    seeded_cid: int | None = None
    r = client.get("/conversations")
    assert r.status_code == 200
    rows = r.json()
    if not rows:
        # RV LOW（2巡目）: seed した会話は finally で必ず削除する（クリーン DB の初回実行後に
        # 行が残ると、後続実行が「任意の既存先頭行」へ依存する順序依存を作るため）。
        # RV LOW（3巡目）: create 直後から try に入れる＝add_message や再 GET が失敗しても
        # finally が必ず掃除する（try 範囲外での失敗＝行残り、の穴を塞ぐ）。
        conv = store.create_conversation(user_id="admin", world="v1", title="mock契約seed")
        seeded_cid = conv["id"]
    try:
        if seeded_cid is not None:
            store.add_message(seeded_cid, "user", "mock契約テスト用 seed メッセージ")
            r = client.get("/conversations")
            assert r.status_code == 200
            rows = r.json()
            assert rows, "seed 後も /conversations が空（seed 経路の前提が崩れている）"
        extra = set(mock_api.CONVERSATIONS_LIST[0]) - set(rows[0])
        assert not extra, f"/conversations[0]: mock にあり実に無いキー: {sorted(extra)}"

        cid = rows[0]["id"]
        r2 = client.get(f"/conversations/{cid}")
        assert r2.status_code == 200
        detail = r2.json()
        assert set(detail) == {"conversation", "messages"}

        # mock_api.py の各 cid ブランチが "conversation" に持たせるキー（共通部分・S3 是正で追加した
        # version/read_only/contains_personal_workspace 含む）。
        mock_conv_keys = {"id", "title", "origin", "version", "read_only", "contains_personal_workspace"}
        extra_conv = mock_conv_keys - set(detail["conversation"])
        assert not extra_conv, f"/conversations/{{cid}}.conversation: mock にあり実に無いキー: {sorted(extra_conv)}"

        if detail["messages"]:
            # mock_api.py の message dict が使う最大のキー集合（role/content/answer/trace/created_at）。
            mock_msg_keys = {"role", "content", "answer", "trace", "created_at"}
            extra_msg = mock_msg_keys - set(detail["messages"][0])
            assert not extra_msg, f"/conversations/{{cid}}.messages[0]: mock にあり実に無いキー: {sorted(extra_msg)}"
    finally:
        if seeded_cid is not None:
            store.delete_conversation(seeded_cid, user_id="admin")


def _golden_route_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for line in _GOLDEN_ROUTES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        methods, path = cols[0], cols[1]
        for m in methods.split(","):
            keys.add((m, path))
    return keys


def test_mocked_routes_exist_in_route_table():
    """MOCKED（tests/e2e/mock_api.py）の各 (method, path) が実ルート表 golden に存在すること
    （旧ルート撤去・改名で mock だけが偽装し続ける事態を検知する）。"""
    golden = _golden_route_keys()
    missing = [rp for rp in mock_api.MOCKED if rp not in golden]
    assert not missing, (
        "mock_api.MOCKED に実ルート表（tests/api/goldens/routes.txt）へ存在しない (method, path) がある: "
        f"{missing}"
    )


# ===================================================================================
# (c) フェーズ7-1: mock 定数の sherpa/schemas.py 突合（実測担保済みスキーマでの深い形状検査）
# ===================================================================================
# `_shape_diff`（トップレベル＋ネスト1段 list のみ）より深く固定できる mock 定数をここで検証する。
# 対象は「実 API 応答から書き起こしたスキーマがある」もの（sherpa/schemas.py 参照）。すべて
# response_model 付与有無に関わらず検証する（TypeAdapter は response_model と無関係に動く）。
_MOCK_SCHEMA_CASES = [
    (sc.ScopesResponse, mock_api.SCOPES, "SCOPES"),
    (sc.GraphResponse, mock_api.GRAPH, "GRAPH"),
    (sc.GraphFacetsResponse, mock_api.GRAPH_FACETS_RESP, "GRAPH_FACETS_RESP"),
    (sc.WorldOptionsResponse, mock_api.WORLD_OPTIONS_RESP, "WORLD_OPTIONS_RESP"),
    (sc.ConfigResponse, mock_api.CONFIG_RESP, "CONFIG_RESP"),
    (sc.SettingsResponse, mock_api.SETTINGS_RESP, "SETTINGS_RESP"),
    (sc.AdminSettingsView, mock_api.SYSTEM_SETTINGS_VIEW, "SYSTEM_SETTINGS_VIEW"),
    (list[sc.UserRow], mock_api.USERS, "USERS"),
    (sc.AdminUsageStatsResponse, mock_api.USAGE_STATS_DEFAULT, "USAGE_STATS_DEFAULT"),
    (list[sc.AuditRow], mock_api.AUDIT_ROWS, "AUDIT_ROWS"),
    (sc.WorldStatusResponse, mock_api.WORLD_STATUS_RESP, "WORLD_STATUS_RESP"),
    (list[sc.WorkspaceFileRow], mock_api.WORKSPACE_FILES, "WORKSPACE_FILES"),
    (list[sc.AnnouncementOut], mock_api.ANNOUNCEMENTS, "ANNOUNCEMENTS"),
    (sc.IngestPreviewResponse, mock_api.PREVIEW, "PREVIEW"),
    (sc.ChatTurnsRunningResponse, mock_api.CHAT_TURNS_RUNNING_EMPTY, "CHAT_TURNS_RUNNING_EMPTY"),
    (list[sc.ConversationSummary], mock_api.CONVERSATIONS_LIST, "CONVERSATIONS_LIST"),
    # フェーズ7-1 再RV（Codex MEDIUM・2026-07-16）: 定数だけでなく handler が動的に組み立てる応答
    # （個人ワークスペース・ナレッジグラフ・world 系）も、実関数（auth_me_response と同じ流儀で
    # mock_api.py 側が抽出済み）を代表引数で呼んで検証する（literal のままだと drift を検知できない）。
    (sc.WorkspaceFileUploadResponse, mock_api.workspace_upload_response(501, "note.md"),
     "workspace_upload_response"),
    (sc.WorkspaceFileDeleteResponse, mock_api.workspace_delete_response(501, "note.md"),
     "workspace_delete_response"),
    (sc.WorkspaceSearchResponse, mock_api.workspace_search_response("消費税率"),
     "workspace_search_response"),
    (sc.GraphAskResponse, mock_api.graph_ask_response("消費税率について"), "graph_ask_response"),
    (sc.GraphSearchResponse,
     mock_api.graph_search_response(
         [{**n, "em": "static", "phase": None, "category": None} for n in mock_api.GRAPH["nodes"][:2]],
         mock_api.GRAPH["edges"][:1], {"nodes": 2, "edges": 1}),
     "graph_search_response"),
    (sc.WorldDiffResponse, mock_api.world_diff_response("/mnt/c/ProjectA"), "world_diff_response(未登録)"),
    (sc.WorldDiffResponse,
     mock_api.world_diff_response("/mnt/c/ProjectA", world_id="w1", label="4期更改",
                                  registered=True, added=[], total=3, indexed=3),
     "world_diff_response(登録済み)"),
    (sc.WorldIngestAcceptedResponse, mock_api.world_ingest_accepted_response("w1"),
     "world_ingest_accepted_response"),
    (sc.WorldIngestAcceptedResponse, mock_api.world_ingest_accepted_response("w1", run_id=501, joined=True),
     "world_ingest_accepted_response(joined)"),
]


@pytest.mark.parametrize("model,payload,name", _MOCK_SCHEMA_CASES, ids=[c[2] for c in _MOCK_SCHEMA_CASES])
def test_mock_constant_matches_schema(model, payload, name):
    """mock 定数を対応する応答モデルで検証する（nested dict の中身・2段目以降の list 要素も含む）。"""
    try:
        TypeAdapter(model).validate_python(payload)
    except Exception as e:
        pytest.fail(f"mock_api.{name} が sherpa.schemas と一致しません（mock drift）:\n{e}")


def test_recompute_construct_id_normalizes_unselected_cloud_agent_like_real_server():
    """mock_api._recompute_construct_id は実サーバ `agent_constructs.construct_id`/`effective_agent`
    と同じ A7 正規化を行う（選択中でないクラウド系 agent は ollama へ倒す）。"""
    resp = {
        "agent": "openai", "cloud_provider": "gemini", "codex_model_provider": "",
        "constructs_available": mock_api.SETTINGS_RESP["constructs_available"],
    }
    assert mock_api._recompute_construct_id(resp) == "ollama_only"

    # 選択中のクラウドと一致すれば正規化しない。
    resp2 = {**resp, "cloud_provider": "openai"}
    assert mock_api._recompute_construct_id(resp2) == "openai_only"

    # クラウド系以外（ollama/codex）は cloud_provider に関わらず対象外。
    resp3 = {"agent": "ollama", "cloud_provider": "gemini",
             "constructs_available": mock_api.SETTINGS_RESP["constructs_available"]}
    assert mock_api._recompute_construct_id(resp3) == "ollama_only"

    # 有効化されていない agent（constructs_available に無い＝実サーバでは SHERPA_EXTRA_AGENTS
    # 未指定）は、cloud_provider と不一致でも正規化しない＝生値のまま保持する
    # （実サーバ `effective_agent()` は非有効な raw_agent を A7 判定に進めず素通りさせる）。
    resp4 = {"agent": "bedrock", "cloud_provider": "openai",
             "constructs_available": mock_api.SETTINGS_RESP["constructs_available"]}
    assert mock_api._recompute_construct_id(resp4) == "bedrock"

    # 有効化されている（constructs_available に含まれる）bedrock は、通常どおり A7 正規化の対象。
    resp5 = {"agent": "bedrock", "cloud_provider": "openai",
             "constructs_available": mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS["constructs_available"]}
    assert mock_api._recompute_construct_id(resp5) == "ollama_only"
