"""ユーザー設定＋全体設定（system_settings・キャッシュ含む）。

フェーズ4 S7（2026-07-02-リファクタリング計画.md）: `sherpa/store/__init__.py` から純移動。
`set_system_settings` 内の `_audit_insert` 呼び出しは facade 属性経由の実行時解決（詳細は
同関数の docstring 参照・計画の「危険な継ぎ目 (a)-3」方式(i)）。
"""
from __future__ import annotations

import hashlib
import math
import time

from psycopg.types.json import Json

from .db import _connect, _ensure

# 既定のシステムプロンプト（回答方針・テーマ非依存）。行が無いときはこれを使う。
DEFAULT_SYSTEM_PROMPT = (
    "憶測で回答しないでください。不明な点は不明と伝えてください。"
    "根拠のある情報と推測を明確に分けてください。"
    "事実確認が必要な内容については、確認できた情報をもとに回答してください。"
    "回答では、結論・理由・補足を分かりやすく整理してください。"
)

# 設定の既定値（行が無いときに使う）。agent=None＝未設定→呼び出し側が env SHERPA_AGENT にフォールバック。
# RV MED（2026-07-16 Codex RV 1巡目 F1/F4/F6 是正）: `bedrock_verified_models` はここに**含めない**
# （`user_settings` の列でも `_SETTINGS_FIELDS` の一員でもない＝専用テーブル `bedrock_verified_models`
# に分離した・詳細は `add_bedrock_verified_models` の docstring と sherpa/store/db.py の該当 DDL コメント
# 参照）。`update_settings(uid, bedrock_verified_models=[...])` は素通りせず単に無視される
# （`_SETTINGS_FIELDS` に無い kwarg は upd に入らない）＝F6 の不変条件（記録は verify/列挙成功のみ）が
# コードの構造そのもので保証される。
# openai_model/gemini_model/ollama_model/codex_model/ollama_url の既定は空文字（未設定）。理由は
# `agent` フィールドと同じ罠（`update_settings` の docstring 参照）: ハードコード既定を
# _SETTINGS_DEFAULT に置くと、一度でも settings が保存された瞬間にその既定値が DB へ焼き付き、
# 以後カタログ既定・中央既定（`sherpa/model_catalog.py`／`sherpa/keys.py::resolve_ollama_url`・
# 管理者が編集する）を変更しても反映されない。空文字は「未設定＝カタログ/中央既定に従う」を意味し、
# 実際の解決は `model_catalog.resolve_model`／`keys.resolve_ollama_url` が
# 「利用者の選択 → 管理者の既定 → 組み込み既定」の順で行う（呼び出し箇所は各モジュール参照）。
_SETTINGS_DEFAULT = {"agent": None, "codex_reasoning": "low", "codex_model": "",
                     "codex_web_search": False,
                     "openai_api_key": None, "openai_model": "",
                     "ollama_url": "", "ollama_model": "",
                     "gemini_api_key": None, "gemini_model": "",
                     "bedrock_model": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                     "bedrock_api_key": None,
                     "extract_provider": "auto", "intent_model": None,
                     "graph_provider": "", "intent_provider": "", "embed_provider": "",
                     "codex_model_provider": "",
                     "search_helper": "", "search_helper_model": "",
                     "system_prompt": DEFAULT_SYSTEM_PROMPT}
_SETTINGS_FIELDS = tuple(_SETTINGS_DEFAULT)

# RV MED（F3・2026-07-16再検証）: bedrock_verified_models の上限（無制限成長を防ぐ）。実アカウントの
# anthropic 推論プロファイル数の一桁上に取り、1回の列挙（GET /settings/bedrock-models）が cap を
# 超えて「取得できたのに保存できない」ID が出る現実的な可能性を消す。
# RV MED（N3・2026-07-16 Codex RV 3巡目再検証）: それでも cap を超えるケースはゼロではない
# （AWS `ListInferenceProfiles` は API 仕様上 `maxResults` に最大 1000 を指定でき、本実装は
# ページングをせず1回の応答をそのまま使うため、大きなアカウントでは1回の列挙だけで cap=200 を
# 超えうる）。cap 値をいくつに取っても原理的に再発しうるため、根治は cap の調整ではなく
# `add_bedrock_verified_models` の返り値（実際に保持された ID）で応答/キャッシュ側を
# フィルタすること（呼び出し側＝sherpa/routers/system.py 参照）。
# RV MED（R4-1・2026-07-16 Codex RV 4巡目再検証・最重要）: 当初は cap 超過時に「古い方（LRU）から
# 捨てる」方式だったが、それだと**一度「保存可能」と返した ID を後から取り消しうる**実害が実際に
# 成立した（repro: cap ちょうど満杯の状態で新規 verify V が成功→最も古い1件 L1 が evict されて V が
# 記録される→次のキャッシュヒット再記録で「列挙キャッシュに残っている旧 L1..L200」が再送され、
# それらが「最近」側へ寄ることで今度は V が押し出される→直前に verify 成功した V の PUT が 422 になる）。
# 「返した ID は必ず保存できる」という中核契約への直接違反であり、cap 値をどこに置いても原理的に
# 再発しうる（LRU 方式そのものが問題の根）。是正: **単調（monotonic）保持**に変更する＝既存 ID は
# 絶対に evict しない・新規 ID は容量に空きがある分だけ追加する（順序も「追加された順」のまま・
# 再確認（列挙のキャッシュヒット再記録等）による並び替えはしない＝実質 LRU を廃止）。per-user 200件は
# 実アカウントの anthropic 推論プロファイル数から見て実運用で到達し得ない上限だが、万一満杯になっても
# 新規 verify は ok:false（保存枠不足）で正直に失敗する（`_bedrock_model_id_valid` の正本を偽らない）。
_BEDROCK_VERIFIED_MODELS_MAX = 200


# 固定 advisory lock key（"PKEY"）＝A6（personal_api_keys_allowed）の判定と個人キー書込みを
# `purge_personal_api_keys()` と直列化するための境界。settings_put の A6 事前チェックは
# 別トランザクションで読んだスナップショットに基づくため、それだけでは
# 「事前チェック後に admin が無効化＋一括削除した個人キーが、古いスナップショットに基づく
# 書込みで復活する」競合窓を閉じられない。`update_settings()` が個人キーを実際に書く場合だけ
# この lock を取り、書込み直前に A6 を同一トランザクションで再確認する。
_PERSONAL_KEY_LOCK = 0x504B4559


class PersonalKeysDisallowedError(Exception):
    """個人キーの書込み直前に再確認した結果、A6（personal_api_keys_allowed）が偽だった
    （事前チェックの後に admin が無効化した競合）。"""


class OpenAIEndpointSettingsConflict(ValueError):
    """`openai_endpoint_kind`/`openai_base_url` の書込み直前（advisory lock 取得後）に同一
    コネクションから読み直した実効値が、kind が openai 以外なら base_url も必要という整合を
    満たさなかった（並行 PUT が互いの事前検証をすり抜けて競合した場合を含む）。
    `sherpa/routers/system_extras.py::admin_settings_put` がこれを 422 に変換する。"""


def _bedrock_key_fingerprint(key: str | None) -> str:
    """キー値そのものを持たない fingerprint（sha256 先頭16桁）。キャッシュ/記録 entry が
    どのキーに対する結果かを識別するためだけに使う（値の復元は不可能・十分な衝突耐性）。

    RV MED（N1・2026-07-16 Codex RV 3巡目再検証）: `sherpa/routers/system.py` から本モジュールへ
    移設（`add_bedrock_verified_models` が同一トランザクション内で使う必要があるため・facade
    経由で `sherpa.store._bedrock_key_fingerprint`／`sherpa.routers.system._bedrock_key_fingerprint`
    としても引き続き参照できる＝既存 import・テストの互換を保つ）。
    """
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]


def get_settings(user_id="admin") -> dict:
    """ユーザの頭脳/モデル/キー設定（行が無ければ既定）。**キーも含む＝サーバ内部用**。

    `bedrock_verified_models`（実在確認済み Bedrock モデルID一覧）は専用テーブルへの追加 SELECT で
    合成する（`user_settings` には列を持たない・F1/F4/F6 是正・`add_bedrock_verified_models` 参照）。
    呼び出し側 API（返り値に `bedrock_verified_models` キーが入ること）は不変。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT agent, codex_reasoning, codex_model, codex_web_search, openai_api_key, openai_model, "
            "ollama_url, ollama_model, gemini_api_key, gemini_model, bedrock_model, bedrock_api_key, "
            "extract_provider, intent_model, graph_provider, intent_provider, embed_provider, "
            "codex_model_provider, search_helper, search_helper_model, "
            "system_prompt "
            "FROM user_settings WHERE user_id=%s", (user_id,)).fetchone()
        verified_row = c.execute(
            "SELECT ids FROM bedrock_verified_models WHERE user_id=%s", (user_id,)).fetchone()
    s = {**_SETTINGS_DEFAULT, **(row or {})}
    s["bedrock_verified_models"] = list(verified_row["ids"]) if verified_row and verified_row["ids"] else []
    if s.get("system_prompt") is None:                 # 未設定(NULL)＝既定を使う（空文字""はユーザが消した＝そのまま）
        s["system_prompt"] = DEFAULT_SYSTEM_PROMPT
    return s


def update_settings(user_id="admin", **fields) -> dict:
    """設定を upsert。許可フィールドのみ。`openai_api_key` は None/未指定なら変更しない（書込専用）。

    個人キー3列（openai/gemini/bedrock_api_key）はこの呼び出しが明示的に触れた列だけを SQL 上
    UPDATE する（触れていない列は `ON CONFLICT DO UPDATE` の SET から丸ごと除外し、既存の DB 値を
    そのまま残す）。読取時点のスナップショット（`cur`）を無条件で書き戻す全列 UPSERT だと、
    「キーを触らない保存」の実行中に管理者の一括削除（`purge_personal_api_keys`）が割り込んだ場合、
    読取後・書込前に消えたはずのキーを書き戻して復活させてしまう（タイミング依存の競合）。列を
    SET から除外すれば、この保存が実際に何を書くかはそもそもタイミングに依存しない。

    `bedrock_verified_models` は `_SETTINGS_FIELDS` に無い＝`fields` に渡しても無視される（専用テーブル
    は `add_bedrock_verified_models` だけが書く・F6 是正）。"""
    cur = get_settings(user_id)
    upd = {k: v for k, v in fields.items() if k in _SETTINGS_FIELDS and v is not None}
    # 空文字＝クリア指示（openai/gemini/bedrock は書込専用キー・intent_model は既定モデルに戻す＝S0）
    for k in ("openai_api_key", "gemini_api_key", "bedrock_api_key", "intent_model"):
        if k in fields and fields[k] == "":
            upd[k] = None
    merged = {**cur, **upd}
    # RV HIGH（2026-08-18 Codex RV 2巡目 指摘1）: 以前は未設定（行が無い＝agent=None）のまま `agent` を
    # 含まない PUT /settings を1回でも踏むと、無条件で "heuristic" が永続化されていた（RV1是正）。
    # 続く RV1是正の `merged["agent"] or agent_constructs.default_agent()` は "heuristic" 直書きより
    # マシだが、**その瞬間の PATH/env に依存する値を DB へ焼き付ける**問題は残る＝実引き金は
    # `web/chat/menus.js::saveModel()` が `{codex_model: v}` だけを PUT すること（利用者は頭脳を
    # 選んでいないのに保存のたびに `agent` が確定してしまう）。後から Codex CLI が消えた／
    # OPENAI_API_KEY を入れた／起動方法で PATH が変わった、といった環境変化があっても、DB に
    # 焼き付いた古い選択がそのまま使われ続ける（症状の形は違うが RV1是正が塞ごうとしたものと同じ
    # 「一度書き込まれると抜けられない」問題が値を変えて再発する）。
    #
    # 直し方: `agent` は**明示された値**（今回の `fields` に含まれていた、または既存行に既に
    # 入っていた）だけを保存し、一度も選ばれていないなら DB 上も「未設定」のままにする。列は
    # NOT NULL なので何かは入れる必要があるが、埋める値は解決済みの頭脳名ではなく**空文字 `''`**
    # にする（`sherpa/providers/__init__.py::_select_provider` も `sherpa/agent_constructs.py::
    # construct_id` も `s.get("agent") or default_agent()` の形で読んでおり、`''` は falsy＝
    # 呼び出し時点の環境でそのつど自動選択される。DB には何も焼き付けない）。
    merged["agent"] = merged["agent"] or ""   # 列は NOT NULL（未設定は空文字＝読み出し時に自動選択）
    merged["bedrock_model"] = merged["bedrock_model"] or "jp.anthropic.claude-haiku-4-5-20251001-v1:0"   # 列は NOT NULL
    merged["codex_model_provider"] = merged["codex_model_provider"] or ""   # 列は NOT NULL（''＝未設定＝openai）
    merged["search_helper"] = merged["search_helper"] or ""             # 列は NOT NULL（''＝使わない）
    merged["search_helper_model"] = merged["search_helper_model"] or ""
    # 列は NOT NULL（''＝未設定＝model_catalog.resolve_model がカタログ既定/組み込み既定へ解決する）。
    merged["openai_model"] = merged["openai_model"] or ""
    merged["gemini_model"] = merged["gemini_model"] or ""
    merged["ollama_model"] = merged["ollama_model"] or ""
    merged["codex_model"] = merged["codex_model"] or ""
    # 列は NOT NULL（''＝未設定＝keys.resolve_ollama_url が中央既定/組み込み既定（localhost）へ解決する）。
    merged["ollama_url"] = merged["ollama_url"] or ""
    # 個人キー3列は、この呼び出しの `fields` に明示的に含まれていた（クリアの "" も含む）列だけを
    # 「触れた」列として扱う。触れていない列は SQL の SET から除外する（部分更新・上の docstring
    # 参照）。列名は固定タプルの静的文字列のみを SQL へ埋め込む（利用者入力は一切含めない）。
    _key_cols = ("openai_api_key", "gemini_api_key", "bedrock_api_key")
    _key_touched = {k: (k in fields and fields[k] is not None) for k in _key_cols}
    # 個人キーを実際にセットする（クリアではなく非空の値を渡された）呼び出しだけ、書込み直前に
    # A6 を同一トランザクションで再確認する（呼び出し元＝settings_put の事前チェックは別
    # トランザクションのスナップショットに基づくため、それだけでは競合窓を閉じられない）。
    _writing_personal_key = any(fields.get(k) for k in _key_cols)
    set_fragments = [
        "agent=EXCLUDED.agent", "codex_reasoning=EXCLUDED.codex_reasoning",
        "codex_model=EXCLUDED.codex_model", "codex_web_search=EXCLUDED.codex_web_search",
        "openai_model=EXCLUDED.openai_model",
        "ollama_url=EXCLUDED.ollama_url", "ollama_model=EXCLUDED.ollama_model",
        "gemini_model=EXCLUDED.gemini_model",
        "bedrock_model=EXCLUDED.bedrock_model",
        "extract_provider=EXCLUDED.extract_provider", "intent_model=EXCLUDED.intent_model",
        "graph_provider=EXCLUDED.graph_provider", "intent_provider=EXCLUDED.intent_provider",
        "embed_provider=EXCLUDED.embed_provider", "codex_model_provider=EXCLUDED.codex_model_provider",
        "search_helper=EXCLUDED.search_helper", "search_helper_model=EXCLUDED.search_helper_model",
        "system_prompt=EXCLUDED.system_prompt",
    ]
    set_fragments += [f"{k}=EXCLUDED.{k}" for k in _key_cols if _key_touched[k]]
    set_fragments.append("updated_at=now()")
    set_sql = ", ".join(set_fragments)
    with _connect() as c:
        if _writing_personal_key:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_PERSONAL_KEY_LOCK,))
            _a6_row = c.execute(
                "SELECT value FROM system_settings WHERE key='personal_api_keys_allowed'").fetchone()
            if not bool(_a6_row["value"] if _a6_row else False):
                raise PersonalKeysDisallowedError(
                    "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
        c.execute(
            "INSERT INTO user_settings (user_id, agent, codex_reasoning, codex_model, codex_web_search, "
            "  openai_api_key, openai_model, "
            "  ollama_url, ollama_model, gemini_api_key, gemini_model, bedrock_model, bedrock_api_key, "
            "  extract_provider, intent_model, graph_provider, intent_provider, embed_provider, "
            "  codex_model_provider, search_helper, search_helper_model, "
            "  system_prompt, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            f"ON CONFLICT (user_id) DO UPDATE SET {set_sql}",
            (user_id, merged["agent"], merged["codex_reasoning"], merged["codex_model"],
             bool(merged["codex_web_search"]), merged["openai_api_key"],
             merged["openai_model"], merged["ollama_url"], merged["ollama_model"],
             merged["gemini_api_key"], merged["gemini_model"],
             merged["bedrock_model"], merged["bedrock_api_key"],
             merged["extract_provider"], merged["intent_model"],
             merged["graph_provider"], merged["intent_provider"], merged["embed_provider"],
             merged["codex_model_provider"],
             merged["search_helper"], merged["search_helper_model"],
             merged["system_prompt"]))
    return merged


def add_bedrock_verified_models(user_id: str, ids: list[str],
                                expected_key_fp: str | None = None) -> list[str] | None:
    """検証（`POST /settings/bedrock-models/verify`）や列挙（`GET /settings/bedrock-models`）で
    実在確認できたモデルIDを専用テーブル `bedrock_verified_models` へ記録する。

    RV MED（2026-07-15→2026-07-16再検証）: `_bedrock_model_id_valid`（sherpa/routers/system.py）の
    membership 判定の正本＝ここに記録が無ければ、形式が正しいだけの ID は `PUT /settings` で保存
    できない（BEDROCK_MODEL_ID_RE.fullmatch のみで許可していた旧実装の穴を塞ぐ）。

    **`user_settings` には一切触れない**（F1/F4/F6 是正・db.py の該当 DDL コメント参照）。
    「INSERT ... ON CONFLICT DO NOTHING で行確保 → SELECT ... FOR UPDATE → （fingerprint 確認）→
    （容量が空いている分だけ）UPDATE」の流れで完全直列化する:
      1. まず空配列で行を確保する（無ければ作る・あれば no-op）。
      2. 直後の `SELECT ... FOR UPDATE` は、1で行の存在が保証されているため**必ず行をロックできる**
         （F1 是正のかなめ＝行が無い状態で `FOR UPDATE` しても何もロックできず、2並行の初回呼び出しが
         両方 `[]` を読んで後勝ちで片方の記録が消える、という競合を防ぐ）。
      3. ロックした状態で読み→（`expected_key_fp` 指定時のみ）現在の `bedrock_api_key` を
         同一トランザクション内で再読取して fingerprint 比較→重複除去→**単調追加**（既存 ID は
         絶対に維持したまま、容量に空きがある分だけ新規 ID を末尾に追加）→（追加が無ければ
         `UPDATE` 自体を省略）。
    この間、他の並行呼び出しは 2 の `FOR UPDATE` でブロックされ、1の `commit` 後に自分の読み取りへ
    進む＝直列化される。重複は除く。上限 `_BEDROCK_VERIFIED_MODELS_MAX` 件（超過分の新規 ID は
    **追加しない**＝単調保持・`_BEDROCK_VERIFIED_MODELS_MAX` のコメントの R4-1 参照。既存 ID を
    evict する LRU 方式は「一度『保存可能』と返した ID を後から取り消しうる」実害があったため廃止した）。

    `expected_key_fp`（RV MED N1・2026-07-16 Codex RV 3巡目再検証）: 呼び出し側が「検証/列挙を
    開始した時点」で観測した `bedrock_api_key` の fingerprint（`_bedrock_key_fingerprint`）。
    行ロック取得**後**・書込実行**前**に、同一トランザクション内で現在の `bedrock_api_key` を
    読み直して比較する（呼び出し側で「開始前 fp」と「完了後に別途 SELECT した fp」を比べる旧方式は、
    比較後・記録前の間に別トランザクションのキー変更がコミットされる TOCTOU を埋め切れなかった＝
    ここでの再読取が「実際に記録する」操作に最も近く、これより後に長時間ブロックする処理が無いため
    実質的に原子的とみなせる）。不一致なら**何も記録せず** `None` を返す。`expected_key_fp=None`
    （既定）なら fingerprint 検証をスキップする（内部/テスト用途・従来どおり無条件で記録する）。
    env キー運用（per-user キー未設定・env/SigV4 委譲）は呼び出し側・ここの双方で `key=None` →
    fingerprint("") 同士の比較になり常に一致する＝従来どおり素通りする（env はランタイム中に
    変わらない前提）。

    返り値（RV MED N3・2026-07-16 Codex RV 3巡目再検証→R4-1・4巡目再検証で単調保持に対応）: `ids`
    のうち、この呼び出し後にテーブルへ実際に存在する ID のサブセット（既存分は無条件でここに含まれる
    ＝単調保持なので消えることが無い。新規分は容量に空きがあって実際に追加できた分だけ含まれる）。
    `None` は fingerprint 不一致で何も記録していない場合。呼び出し側（sherpa/routers/system.py）は
    これで列挙/verify の**応答**を絞り込み、「このエンドポイントが返す ID は必ず保存できる」という
    中核契約を cap 値・LRU 的な入れ替わりに依らず構造的に保証する。
    """
    new_ids = list(dict.fromkeys(i for i in ids if i))   # 空/重複除去・順序維持
    if not new_ids:
        return []
    _ensure()
    with _connect() as c:
        c.execute(
            "INSERT INTO bedrock_verified_models (user_id, ids) VALUES (%s, '[]'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING", (user_id,))
        row = c.execute(
            "SELECT ids FROM bedrock_verified_models WHERE user_id=%s FOR UPDATE",
            (user_id,)).fetchone()
        if expected_key_fp is not None:
            cur = c.execute(
                "SELECT bedrock_api_key FROM user_settings WHERE user_id=%s", (user_id,)).fetchone()
            cur_fp = _bedrock_key_fingerprint(cur["bedrock_api_key"] if cur else None)
            if cur_fp != expected_key_fp:
                return None
        current = list(row["ids"] or []) if row else []
        current_set = set(current)
        # RV MED（R4-1）: 単調保持。既存 ID には一切触れない（evict しない・並び替えない）。新規
        # ID のみ、容量（cap - 既存件数）に収まる分だけ末尾に追加する。容量が無ければ追加しない
        # （evict して押し込むのではなく、素直に「入らない」として retained から除外する）。
        room = _BEDROCK_VERIFIED_MODELS_MAX - len(current)
        to_add = [x for x in new_ids if x not in current_set][:max(room, 0)]
        if to_add:
            merged = current + to_add
            c.execute(
                "UPDATE bedrock_verified_models SET ids=%s, updated_at=now() WHERE user_id=%s",
                (Json(merged), user_id))
        else:
            merged = current
        merged_set = set(merged)
        return [x for x in new_ids if x in merged_set]


# ==== 全体設定（system_settings・admin 書込のみ・監査つき）====
# docs/proposals/2026-07-08-設定分離とUI整備.md S1。全ユーザーに効くシステム全体設定。
# 優先順は system_settings > env > コード既定（呼び出し側＝arms/api 層で解決する）。
# 認可（admin）は呼び出し側（api.py の `_require_admin`）で済ませてから set_system_settings を呼ぶ前提。

# 短TTLの読み取りキャッシュ（プロセス内・取り込みホットループでの DB 打鍵を避ける）。
# 呼び出し頻度は per-build/per-request で限定的（convertible_exts/drift は per-file ループではない）だが、
# 念のためごく短い TTL で連続読取を平滑化する。set_system_settings は必ずこのキャッシュを無効化する。
# 複数ワーカー構成ではワーカーごとに独立キャッシュ（TTL が短いため実害は小さい・bedrock models cache と同方針）。
_SYSTEM_SETTINGS_CACHE_TTL = 3.0
_system_settings_cache: dict | None = None
_system_settings_cache_ts: float = 0.0


def _invalidate_system_settings_cache() -> None:
    global _system_settings_cache, _system_settings_cache_ts
    _system_settings_cache = None
    _system_settings_cache_ts = 0.0


def _read_system_settings_fresh(*, connect_timeout: float | None = None,
                                statement_timeout_ms: int | None = None) -> dict:
    """system_settings 全件を DB から直接読む（private・共有キャッシュ `_system_settings_cache`
    を一切参照も更新もしない）。

    `get_system_settings()` のキャッシュミス時の実体（挙動・timeout 予算配分は完全に同一——
    後方互換のため呼び出し順の `time.monotonic()` 回数も揃えている）。WEB-1（`providers/__init__.py::
    get_provider` の1ターン唯一の読取点）はこちらを直接呼ぶ——`get_system_settings()` を
    「invalidate してから呼ぶ」方式だと、並行ターン（`/chat` の threadpool・`/chat/turns` の
    background thread）が invalidate 直後にキャッシュを再加熱し、その後 DB が落ちても
    再加熱された値をこちらが読んでしまう TOCTOU が残る。共有キャッシュに一切触れないこの関数
    なら、他スレッドのキャッシュ状態に依らず常にこの呼び出し自身が実際に読んだ値（または
    その場の接続失敗）だけを返す。
    """
    budget_started = time.monotonic()   # `_ensure()` の消費分も差し引くため、その呼び出し前から計測する
    _ensure(connect_timeout=connect_timeout)
    connect_kwargs = {}
    if connect_timeout is not None:
        remaining = connect_timeout - (time.monotonic() - budget_started)
        if remaining <= 0:
            # `_ensure()` だけで予算を使い切った——最低1秒へクランプして新規接続を試みない。
            raise TimeoutError("_read_system_settings_fresh: budget exhausted before connecting")
        connect_kwargs["connect_timeout"] = max(1, math.ceil(remaining))
    with _connect(**connect_kwargs) as c:
        if statement_timeout_ms is not None:
            elapsed_ms = (time.monotonic() - budget_started) * 1000
            remaining_ms = max(1, int(statement_timeout_ms - elapsed_ms))
            # SET LOCAL（session-level ではなく）: プール導入後（性能台帳#17 QW2）、この
            # with ブロック＝単一トランザクションの間だけ有効にし、返却後の接続に
            # statement_timeout が残らないようにする（GUC 汚染防止・commit/rollback で自動消滅）。
            c.execute(f"SET LOCAL statement_timeout = '{remaining_ms}ms'")
        rows = c.execute("SELECT key, value FROM system_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def get_system_settings(*, connect_timeout: float | None = None,
                        statement_timeout_ms: int | None = None) -> dict:
    """全体設定（system_settings 全件）を `{key: value}` で返す（未設定キーは含まれない）。

    短TTLキャッシュ付き（返り値は都度コピー＝呼び出し側の破壊的変更が漏れない）。
    admin 未設定なら空 dict。value は JSONB の論理値（list/dict/float/None 等）。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）: PART-4（外部 API のリクエスト全体デッドライン）がこの読み取り自体を無期限に
    ブロックさせないために残り時間ベースで渡す——キャッシュ命中時は DB I/O が発生しないため
    どちらも無関係（渡しても無視される）。`store.worlds.get_world`/`store.db.world_lock_shared`
    と同じ理由・同じ方式（`connect_timeout` は整数秒へ切り上げ・最小1秒でクランプ、
    `statement_timeout` は接続確立**後**に `SET` で発行し、接続に要した時間ぶんを差し引く）。
    実体（未初期化時の `_ensure()` 消費分の差し引き含む）は `_read_system_settings_fresh()` 参照。

    キャッシュを一切参照・更新せず必ず DB から読みたい呼び出し元（WEB-1・`providers/__init__.py::
    get_provider` 参照）は、この関数のシグネチャを変えず（多数の呼び出し元が `lambda: {...}`
    という固定引数無しの形で丸ごと monkeypatch しているため引数追加は互換性を壊す）、
    `_read_system_settings_fresh()` を直接呼ぶ（このキャッシュには一切触れない・TOCTOU なし）。
    """
    global _system_settings_cache, _system_settings_cache_ts
    now = time.monotonic()
    cached = _system_settings_cache
    if cached is not None and now - _system_settings_cache_ts < _SYSTEM_SETTINGS_CACHE_TTL:
        return dict(cached)
    data = _read_system_settings_fresh(connect_timeout=connect_timeout,
                                       statement_timeout_ms=statement_timeout_ms)
    _system_settings_cache = dict(data)
    _system_settings_cache_ts = now
    return data


def _system_settings_snapshot(conn, keys) -> dict:
    """指定キーの現在値スナップショット（`{key: value|None}`・監査 before/補償用）。"""
    snap: dict = {}
    for k in keys:
        row = conn.execute("SELECT value FROM system_settings WHERE key=%s", (k,)).fetchone()
        snap[k] = row["value"] if row else None
    return snap


def _system_settings_apply(conn, updates: dict, uid) -> None:
    """updates を適用（value=None は行削除＝未設定へ戻す・それ以外は upsert）。"""
    for k, v in updates.items():
        if v is None:
            conn.execute("DELETE FROM system_settings WHERE key=%s", (k,))
        else:
            conn.execute(
                "INSERT INTO system_settings (key, value, updated_by) VALUES (%s,%s,%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now(), "
                "  updated_by=EXCLUDED.updated_by",
                (k, Json(v), uid))


# 接続先 URL を持つキーは、監査（audit_log・JSONB・平文）には host 表現（`llm._redact_url_for_error`）
# だけを残す（path にリソース名/デプロイ名等の内部識別子を含みうるため）。secret_keys の有無に
# 関わらず常に畳む（`openai_base_url`/`ollama_url` 単体の変更等、`secret_keys` が空の PUT でも
# 生 URL を audit_log へ残さないため）。
#
# `ollama_url` は env シード直後に `sherpa.api._seed_ollama_url_from_env` が host:port へ正規化して
# 保存する（userinfo/path/query は既に弾かれている）ため、他キーと同列に畳んでよい。
# `catchup_ollama_allowlist_for_env_seeded_url_v2` の tamper 検知（env シードが挿入した値と
# 現在の DB 値の厳密一致）は、この畳んだ表現ではなく専用の `ollama_url_fingerprint`
# （`llm.ollama_url_fingerprint`・正規化 host:port）を `system_settings.env_seeded` 監査へ別途
# 記録して比較する（`seed_system_settings_once`／`catchup_ollama_allowlist_for_env_seeded_url_v2`
# 参照）＝生 URL を監査に残す必要がない。
_URL_SETTINGS_KEYS = frozenset({"openai_base_url", "ollama_url"})


def _redact_secret_settings(state: dict, secret_keys: frozenset | None = None) -> dict:
    """監査用: `secret_keys` に含まれるキーは値を `<set>`/`<cleared>` に畳んで、system_settings の
    キー値（openai/gemini/bedrock の中央 API キー等）を audit_log（JSONB・平文）へ書かない。
    `routers/system.py::_audit_settings_update`（per-user 設定の秘密フィールド監査）と同じ流儀。
    `_URL_SETTINGS_KEYS`（`openai_base_url`／`ollama_url`）は host 表現のみへ畳む
    （`ollama_url` を畳んでも tamper 検知が壊れない理由は定数の docstring 参照）。
    DB への実際の書込（`_system_settings_apply`）はこの関数を経由しない＝畳むのは監査記録のみ。

    `openai_base_url` は `None`／空文字列（真の未設定）・非文字列（型そのものの破損）・
    文字列だが URL として不正、の3種類を区別する固定文字列に畳む:
    - `None`／空文字列 → `<cleared>`。
    - 非文字列（`{}`/`[]`/`0`/`False`/数値/配列/辞書等・値の混入や破損した before-state）→
      `(不正な保存値)`。一操作復旧（`_assert_openai_endpoint_update_consistent`）が拾う対象の
      破損値はここを通る際に `None` へ書き換えられる前の before-state としても現れるため、
      `<cleared>` に畳んでしまうと監査記録から「実際には破損値が保存されていた」という事実が
      消えてしまう（型チェックを先に行い `assert_openai_base_url_allowed` へは渡さない＝文字列
      前提の処理（`for c in base` 等）に非文字列を渡すと `ValueError` 以外の例外
      （`TypeError` 等）を送出しうるため）。
    - 文字列だが `llm.assert_openai_base_url_allowed()` の形式検証に落ちる → `<不正なURL>`。
      `llm._redact_url_for_error()` は `urlparse` の構造区切り認識に依存するため、空白・バック
      スラッシュ混入を含む netloc をそのまま `hostname` として返してしまう（内部パスの断片が
      監査へ生で残る）ことの対策で、不合格なら host 表現を作らず固定文字列に畳む。
    どの分岐も例外を上げない（監査記録＝修復目的の PUT 自体は、どんな値が混入していても
    必ず成功する契約）。"""
    from sherpa import llm
    secret_keys = secret_keys or frozenset()
    out: dict = {}
    for k, v in state.items():
        if k in secret_keys:
            out[k] = "<set>" if v else "<cleared>"
        elif k == "openai_base_url":
            if v is None or v == "":
                out[k] = "<cleared>"
            elif not isinstance(v, str):
                # 非文字列（`{}`/`[]`/`0`/`False` 等）は `<cleared>`（=「未設定」）と区別する:
                # 一操作復旧（`_assert_openai_endpoint_update_consistent`）の対象になった破損値は
                # `None` へ書き換えられる前の before-state としてここを通るため、`<cleared>` に
                # 畳んでしまうと監査記録から「実際には破損値が保存されていた」という事実が
                # 消えてしまう（復旧 PUT の証跡として before に残す必要がある）。
                out[k] = "(不正な保存値)"
            else:
                try:
                    llm.assert_openai_base_url_allowed(v)
                except Exception:
                    out[k] = "<不正なURL>"
                else:
                    out[k] = llm._redact_url_for_error(v) or "<不正なURL>"
        elif k in _URL_SETTINGS_KEYS:
            out[k] = (llm._redact_url_for_error(v) or "<不正なURL>") if v else "<cleared>"
        else:
            out[k] = v
    return out


def _assert_openai_endpoint_update_consistent(conn, updates: dict) -> None:
    """`openai_endpoint_kind`/`openai_base_url` の実効値（この更新後に有効になる値）を、
    advisory lock 取得後の**同一コネクション**から読み直して整合検証する（kind が openai 以外
    なら base_url も必要・`llm.assert_openai_endpoint_consistent` が唯一の真実源）。

    呼び出し元（`sherpa/routers/system_extras.py::admin_settings_put`）が PUT 受理時点で行っていた
    旧チェックは、3秒 TTL キャッシュ（`get_system_settings()`）の現在値に対して行っており、
    lock 取得より前の読み取りだった。初期状態 kind=openai・base=B のとき、並行して「kind を
    azure へ」と「base を null へ」の2つの PUT が互いにこのキャッシュ越しの旧状態を読めば、
    双方の事前検証は通ってしまい、最終的に azure かつ base なしが確定し得た（`llm.openai_base_url()`
    の fail-safe で本家 URL へ黙って縮退し、Azure 向け資格情報が本家へ送られる）。ここで advisory
    lock 取得後・書込み適用前に同一コネクションから読み直すことで、この更新が確定させる実効値を
    直列化された時点で検証する。不整合なら `OpenAIEndpointSettingsConflict`（呼び出し元が 422 へ
    変換）を送出し、書込みは行わない（例外は `with _connect()` を抜ける際にロールバックする）。

    一操作復旧（既存の `openai_base_url` が非文字列に破損した状態からの復旧）: 管理画面は
    「本家」選択時に `openai_base_url` を PUT ボディへ含めない（azure→openai→azure と往復しても
    値を保持するため・`web/admin-settings.js::collectOpenaiEndpoint` 参照）。そのため
    `openai_endpoint_kind` を明示的に `"openai"` へ保存する PUT が、この関数だけで完結して
    破損値を残したままだと（`updates["openai_base_url"]` を素通しで加工しない場合）、DB の
    `openai_base_url` は破損したまま残り、読み取り側の型検査（`llm._assert_openai_endpoint_settings_types_valid`）
    が引き続き `ValueError` を送出し続ける＝復旧に「kind を openai へ」「base_url を明示クリア」の
    2回の PUT が要ることになる。この関数はその場で `updates["openai_base_url"] = None` を補って
    1回の PUT で完結させる（kind=openai では base_url は使われないため副作用が無い）。
    """
    from sherpa import llm
    row_kind = conn.execute("SELECT value FROM system_settings WHERE key=%s",
                            ("openai_endpoint_kind",)).fetchone()
    row_base = conn.execute("SELECT value FROM system_settings WHERE key=%s",
                            ("openai_base_url",)).fetchone()
    cur_kind = row_kind["value"] if row_kind else None
    cur_base = row_base["value"] if row_base else None
    if (updates.get("openai_endpoint_kind") == "openai"
            and "openai_base_url" not in updates
            and cur_base is not None and not isinstance(cur_base, str)):
        updates["openai_base_url"] = None
    eff_kind = (updates["openai_endpoint_kind"] if "openai_endpoint_kind" in updates
               else cur_kind) or "openai"
    eff_base = (updates["openai_base_url"] if "openai_base_url" in updates
               else cur_base)
    # `None`（真の未設定）だけを「未設定」として扱う。falsy な非文字列（`{}`/`[]`/`0`/`False`）を
    # `or ""` で潰すと、上の一操作復旧の判定対象になり得た破損値がここで見えなくなり、
    # `assert_openai_endpoint_consistent` へは無条件で空文字列として渡ってしまう
    # （kind が openai 以外なら「URL が要る」検証を素通りしてしまいかねない）。
    if eff_base is not None and not isinstance(eff_base, str):
        raise OpenAIEndpointSettingsConflict(
            "接続先 URL（openai_base_url）の値が不正です（文字列ではありません）")
    try:
        llm.assert_openai_endpoint_consistent(eff_kind, eff_base or "")
    except ValueError as e:
        raise OpenAIEndpointSettingsConflict(str(e)) from e


def set_system_settings(uid, updates: dict, secret_keys: frozenset | None = None, *,
                        in_txn=None) -> dict:
    """全体設定を部分更新（admin 認可は呼び出し側前提）。

    `updates` は `{key: value}`。`value=None` は「未設定へ戻す」（該当行を削除＝env/既定へフォールバック）。
    それ以外は upsert する。**変更を監査**（`system_settings.updated`・before/after・severity=warning）。
    `secret_keys`（省略可）に指定したキーは、実際の DB 書込は平文のまま行うが、監査記録（before/after）
    だけ `<set>`/`<cleared>` に畳む（中央 API キーを audit_log に平文で残さないため）。

    `in_txn`（省略可）: `(conn, uid, updates)` を受け取る callable。設定変更・監査 INSERT と
    **同一トランザクション**で追加の書込みを行いたい呼び出し元向けのフック（例:
    `api_keys.apply_system_settings_and_revoke_if_disabled` が「トグル OFF で利用者発行キーを
    一括失効」をここに載せる）。例外はそのまま伝播し、設定変更ごとロールバックされる。
    **`system_settings.updated` の監査 INSERT より前に呼ぶ**（下の実装参照）——`in_txn` が
    独自の advisory lock を取る場合、その lock は `_audit_insert` が内部で取る
    `_AUDIT_CHAIN_LOCK` より**先に**取得される必要がある。同じ lock を取る独立の書込み経路
    （`in_txn` を経由しない単体呼び出し）が「lock→（その中の監査 INSERT で）
    `_AUDIT_CHAIN_LOCK`」の順で回るなら、ここで逆順にすると並行実行でデッドロックが
    成立し得る。新しい `in_txn` フックを追加する時は、この順序契約（lock→更新→監査）を
    崩さないこと。

    2026-07-08 RV High 対応: 設定変更（before スナップショット→適用）と監査行 INSERT を**同一トランザクション**
    で実行する（旧: commit→別接続 audit→失敗時に compensate で復元、という方式は (a) commit〜復元の間に
    未監査の値が `/config` 等から見える (b) その間のプロセスが落ちると未監査変更が残留する (c) 並行更新を
    補償復元が上書きする、という3つの穴を抱えていた）。`_audit_insert` の例外は `with _connect()` を抜ける際に
    psycopg のトランザクション契約に従い自動 rollback される＝設定変更もまとめて取り消される（fail-closed を
    「補償」ではなく「原子性」で実現＝announcement CRUD の compensate 方式とは異なる）。監査失敗はそのまま
    呼び出し側（api.py）へ例外として伝播し、500 に変換される。返り値は適用した updates（失敗時は返らない）。
    キャッシュ無効化は commit 成功後（with を抜けた後）に1回だけ行う。

    フェーズ4 S7（2026-07-02-リファクタリング計画.md「危険な継ぎ目 (a)-3」方式(i)）: 直下の `_audit_insert`
    呼び出しは `from .audit import _audit_insert` のようなモジュールレベル import を**使わず**、
    facade（`sherpa.store` パッケージ）の属性 `_facade._audit_insert` 経由で**実行時解決**する。
    理由: tests/api/test_system_settings.py（:295/:318 付近）が
    `monkeypatch.setattr(store, "_audit_insert", _boom)` で `sherpa.store._audit_insert`
    （facade の re-export 属性）を差し替えて fail-closed（監査失敗→500・rollback）を検証している。
    もしここで `from .audit import _audit_insert` と束縛していたら、settings.py の名前空間は
    audit モジュールの関数オブジェクトを直接指すため、facade 側の属性を差し替えても
    この関数からは古い（未 patch の）関数が呼ばれ続けてしまう（Python の名前束縛はコピーで
    別名参照ではないため）。`from sherpa import store as _facade` を関数内（呼び出し時点）に
    置くのは、パッケージ初期化中（`sherpa/store/__init__.py` が本モジュールを import する途中）に
    `sherpa.store` はまだ完成していないため、モジュールレベルで import すると循環 import に
    なるのを避けるため（テストの patch 先＝`store._audit_insert` は変更しない＝facade シームを
    本物のまま保つ）。
    """
    _ensure()
    from sherpa import store as _facade   # 上記 docstring 参照: 実行時解決（monkeypatch シーム維持）
    with _connect() as c:
        # RV 是正（4巡目 #3）: `_ENV_SEED_LOCK` を env シード・追いつき移行と共有する（上の定数の
        # docstring 参照）。system_settings への複数行書込みを直列化し、`ollama_url`/
        # `ollama_allowlist` の行ロック取得順序が経路ごとに異なっていてもデッドロックしない。
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        if "openai_endpoint_kind" in updates or "openai_base_url" in updates:
            _assert_openai_endpoint_update_consistent(c, updates)
        # `keys` は上の一操作復旧（`_assert_openai_endpoint_update_consistent` が `updates` へ
        # `openai_base_url` を補うことがある）の**後**に確定させる: 先に確定させると、補われた
        # キーの before スナップショットが撮れず、監査記録の before/after が食い違う。
        keys = list(updates)
        before = _system_settings_snapshot(c, keys)
        _system_settings_apply(c, updates, uid)
        # in_txn を監査 INSERT より前に呼ぶ（ロック順序の契約）。`_audit_insert` は
        # 内部で `_AUDIT_CHAIN_LOCK` を取得するため、ここより後で in_txn を呼ぶと
        # 「(ENV_SEED_LOCK→)AUDIT_CHAIN_LOCK→in_txn 側のロック」の順になる。一方 in_txn を
        # 使わない独立の書込み経路（例: api_keys.revoke_self_issued_api_keys 単体呼び出し）は
        # 「in_txn 側のロック→（その中の監査 INSERT で）AUDIT_CHAIN_LOCK」の順で回る。この2経路が
        # 逆順で AUDIT_CHAIN_LOCK と in_txn 側のロックを取り合うと、並行実行時にデッドロックが
        # 成立し得る（Postgres は自動検出して片方を失敗させるが、外部APIの監査書込みを道連れに
        # timeout させかねない）。in_txn を先に呼ぶことで、全経路を「in_txn 側のロック→
        # AUDIT_CHAIN_LOCK」の一方向に統一し、構造的にサイクルが起こらないようにする。
        if in_txn is not None:
            in_txn(c, uid, updates)
        # secret_keys の有無に関わらず常に畳む（`_redact_secret_settings` が URL キーの host 化も
        # 担うため・openai_base_url 単体の変更等 secret_keys が空の PUT でも生 URL を audit_log へ
        # 残さない）。
        audit_before = _redact_secret_settings(before, secret_keys)
        audit_after = _redact_secret_settings(updates, secret_keys)
        _facade._audit_insert(c, uid, "system_settings.updated", "system_settings", None,
                      before_state=audit_before, after_state=audit_after, severity="warning")
    _invalidate_system_settings_cache()
    return updates


# 固定 advisory lock key（"SEED"）＝env→system_settings シード試行の直列化（`_AUDIT_CHAIN_LOCK` と同型）。
# RV 是正（4巡目 #3）: 当初は「無くても安全な最適化」だったが、`ollama_url`/`ollama_allowlist` の
# 2行にまたがる読み書き（seed の `ollama_allowlist_merge`・catch-up）が admin の
# `set_system_settings`（`ollama_allowlist`→`ollama_url` の順で書く・`system_extras.py`
# `admin_settings_put` 参照）と異なる順序（`ollama_url`→`ollama_allowlist`）で行ロックを取得すると
# デッドロックし得る。`set_system_settings` にもこの同じ advisory lock を取らせることで、
# system_settings への複数行書込みを常に直列化し（admin 保存は頻度が低いため実害は無い）、
# 行ロックの取得順序が異なっていてもデッドロックが構造的に起こらないようにする。
_ENV_SEED_LOCK = 0x53454544


def migrate_marker_if_legacy_exists(guard_key: str, legacy_key: str, guard_value: object = True) -> bool:
    """`legacy_key`（分離前のコードが書いていた旧共有マーカー・例 `"env_seed_version"`）が存在するかを
    advisory lock 取得後にフレッシュに確認する（呼び出し側の事前チェック＝`get_system_settings()`
    のキャッシュ経由の確認は古い情報に基づくことがあるため、正しさの根拠にしない・
    `seed_system_settings_once` と同じ理由）。

    存在すれば `guard_key` だけを「移行済み」として確定して `True` を返す＝呼び出し元はこれ以上
    env を再読んで何かを書いてはならない（admin が削除した値を残存 env から復活させないため）。
    存在しなければ何も書かず `False` を返す＝呼び出し元は通常どおりの候補構築・
    `seed_system_settings_once` 呼び出しを続けてよい（「新規導入環境」の1回目の判定として使う）。

    `_seed_settings_from_env`/`_seed_ollama_url_from_env` の冒頭で必ず1回呼ぶ（`seed_system_settings_once`
    本体の呼び出しとは別のトランザクション＝厳密には単一の advisory lock 保持区間ではないが、
    `legacy_key` は新規コードが一度確定させたら二度と変化しない静的な事実（`_confirm_legacy_env_seed_marker`
    が両方の新マーカー確定後にしか書かない＝このチェックの直前に「今」ここへ割り込んで書かれることは
    構造的に起こらない）ため、2回の lock 取得に分けても TOCTOU にならない）。
    """
    _ensure()
    from sherpa import store as _facade
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        legacy_row = c.execute("SELECT 1 FROM system_settings WHERE key=%s", (legacy_key,)).fetchone()
        if legacy_row is None:
            return False
        row = c.execute(
            "INSERT INTO system_settings (key, value, updated_by) "
            "SELECT %s, %s, %s WHERE NOT EXISTS ("
            "  SELECT 1 FROM system_settings WHERE key = %s"
            ") ON CONFLICT (key) DO NOTHING RETURNING key",
            (guard_key, Json(guard_value), "system", guard_key)).fetchone()
        if row is not None:
            audit_after = _redact_secret_settings({guard_key: guard_value}, None)
            _facade._audit_insert(c, "system", "system_settings.env_seeded", "system_settings",
                          None, before_state=None, after_state=audit_after, severity="info")
    _invalidate_system_settings_cache()
    return True


def seed_system_settings_once(updates: dict, guard_key: str,
                              secret_keys: frozenset | None = None, *,
                              ollama_allowlist_merge: tuple[str, str] | None = None) -> tuple[dict, dict]:
    """env→system_settings の**初回シード専用**の書込み（`sherpa.api._seed_settings_from_env`／
    `_seed_ollama_url_from_env` 専用）。

    `set_system_settings` と違い、既存の行は**絶対に上書きしない**。呼び出し側の事前チェック
    （`get_system_settings` のキャッシュ経由のマーカー確認）は古い情報に基づくことがあるため、
    正しさの根拠にはしない。本関数の各 INSERT は `guard_key`（完了マーカーのキー名）の行が
    **その INSERT を実行する瞬間に存在しない場合に限り**実行する（`WHERE NOT EXISTS (...)` を
    同一トランザクション内の INSERT 文そのものに埋め込む・READ COMMITTED は文ごとに最新状態を
    見るため、事前チェックがどれだけ古くても実際の書込みは常に「今」のマーカー有無で判定される）。
    これにより「マーカーはとっくに存在するのに、管理者が削除した特定のキーだけが env から
    再挿入されてしまう」（マーカー確認とは無関係にキー単体の ON CONFLICT だけで判定していた
    旧設計の穴）が構造的に起こらない。**本関数自身の単キー INSERT の正しさ**（上記）は
    advisory xact lock の有無に依存しない（`WHERE NOT EXISTS`＋`ON CONFLICT` が単体で原子的）。
    ただし `_ENV_SEED_LOCK` は**単なる最適化ではない**: `ollama_allowlist_merge`／
    `catchup_ollama_allowlist_for_env_seeded_url_v2` の複数行書込みが `set_system_settings`
    と異なる行ロック取得順序になり得るため、lock を `set_system_settings` とも共有して
    system_settings への複数行書込み全体を直列化し、行ロック順序差によるデッドロックを構造的に
    防ぐ境界として load-bearing（上記 `_ENV_SEED_LOCK` の定義コメント参照）。

    `ollama_allowlist_merge`（省略可）: `(url_key, host_entry)`。**`url_key`（通常 "ollama_url"）が
    このトランザクションで実際に新規 INSERT できた場合だけ**、`host_entry`（正規化済み host:port
    文字列）を `ollama_allowlist` の**現在値へ追記**する（`SELECT ... FOR UPDATE` で最新値を読み、
    追記して upsert・admin が既に登録した他のホストは失わない）。`updates` 自体に `ollama_allowlist`
    を含めてはならない（この経路が唯一の書込み元になる・呼び出し側は含めた場合 `ValueError`）。
    RV 是正: 以前は `ollama_allowlist` を候補 dict の1キーとして独立に `ON CONFLICT DO NOTHING`
    していたため、「allowlist 行だけ既存なら env URL は挿入されても allowlist 追記だけ競合で
    落ちる」「URL 行だけ既存なら env URL は無視される一方、使われない host だけが認可される」という
    非原子的な組合せが起こり得た。ここでは URL の実際の挿入結果を見てから allowlist を扱うため、
    この2つは常にペアとして確定する（advisory xact lock が既にトランザクション全体を直列化して
    いるため、行ロックは主に読み取り一貫性のための防御）。

    戻り値: `(applied, conflicts)`。`applied` は実際に新規 INSERT／マージされた `{key: value}`
    （呼び出し側のログ用・`ollama_allowlist_merge` でマージした場合は `applied["ollama_allowlist"]`
    に追記後の一覧が入る）。`conflicts` は INSERT がスキップされた `{key: 現在の DB 値（無ければ
    None）}`（マーカーが既にあり書込み自体が起きなかった場合も含む・呼び出し側が env 値との
    食い違い警告を出すのに使う）。
    """
    _ensure()
    if ollama_allowlist_merge is not None and "ollama_allowlist" in updates:
        raise ValueError("ollama_allowlist_merge 使用時は updates に ollama_allowlist を含められません")
    from sherpa import store as _facade   # set_system_settings と同じ理由（monkeypatch シーム維持）
    applied: dict = {}
    conflicts: dict = {}
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        for k, v in updates.items():
            row = c.execute(
                "INSERT INTO system_settings (key, value, updated_by) "
                "SELECT %s, %s, %s WHERE NOT EXISTS ("
                "  SELECT 1 FROM system_settings WHERE key = %s"
                ") ON CONFLICT (key) DO NOTHING RETURNING key",
                (k, Json(v), "system", guard_key)).fetchone()
            if row is not None:
                applied[k] = v
            else:
                cur = c.execute("SELECT value FROM system_settings WHERE key=%s", (k,)).fetchone()
                conflicts[k] = cur["value"] if cur else None
        before_allowlist = None
        if ollama_allowlist_merge is not None:
            url_key, host_entry = ollama_allowlist_merge
            if url_key in applied and host_entry:
                # RV 是正（4巡目 #3）: 先に行を確保（ON CONFLICT DO NOTHING）してから `FOR UPDATE` する。
                # 行が未作成のまま `FOR UPDATE` しても何もロックできず、並行した admin の初回 INSERT を
                # 古い（空配列前提の）値で上書きしてしまい得た（`add_bedrock_verified_models` と同じ
                # 「確保→ロック」の型）。
                c.execute(
                    "INSERT INTO system_settings (key, value, updated_by) VALUES "
                    "('ollama_allowlist', '[]'::jsonb, 'system') ON CONFLICT (key) DO NOTHING")
                al_row = c.execute(
                    "SELECT value FROM system_settings WHERE key='ollama_allowlist' FOR UPDATE").fetchone()
                current = list((al_row["value"] if al_row else None) or [])
                before_allowlist = current
                if host_entry not in current:
                    merged = [*current, host_entry]
                    c.execute(
                        "UPDATE system_settings SET value=%s, updated_at=now(), updated_by='system' "
                        "WHERE key='ollama_allowlist'", (Json(merged),))
                    applied["ollama_allowlist"] = merged
        if applied:
            audit_before = {"ollama_allowlist": before_allowlist} if before_allowlist is not None else None
            # secret_keys の有無に関わらず常に畳む（`set_system_settings` と同じ理由・env シードで
            # 取り込む openai_base_url/ollama_url も生 URL を audit_log へ残さない）。
            audit_after = _redact_secret_settings(applied, secret_keys)
            if "ollama_url" in applied:
                # `ollama_url` は上の畳みで host 表現になった＝tamper 検知の厳密一致には使えない
                # （`_redact_url_for_error` はポート省略時に補わないため表記ゆれで不一致になり得る）。
                # 専用の指紋（正規化 host:port）を別フィールドで残す
                # （`catchup_ollama_allowlist_for_env_seeded_url_v2` 専用・`llm.ollama_url_fingerprint`
                # docstring 参照）。
                from sherpa import llm as _llm_fp
                fp = _llm_fp.ollama_url_fingerprint(str(applied["ollama_url"]))
                if fp is not None:
                    audit_after["ollama_url_fingerprint"] = fp
            _facade._audit_insert(c, "system", "system_settings.env_seeded", "system_settings", None,
                          before_state=audit_before, after_state=audit_after, severity="info")
    if applied:
        _invalidate_system_settings_cache()
    return applied, conflicts


def catchup_ollama_allowlist_for_env_seeded_url_v2(guard_key: str) -> str:
    """既に env シード済みの環境（`seed_system_settings_once` の `ollama_allowlist_merge` 是正
    より前に一度でも起動した環境）向けの、一度きりの追いつき評価（v2・簡素化裁定・RV 4巡目）。

    裁定（4巡目コーディネータ）: この救済は「このセッション以前に旧版 seed を踏んだ既存展開」
    のための一度きりのもので、対象は実質 dev 環境のみ。複雑化するより **fail-closed** に倒す。
    v1（値一致だけを provenance とみなす方式）は、admin が URL を変えずに allowlist からその
    host だけを削除した操作を復活させてしまう穴があった（値の一致は「env 由来のまま」の証明に
    ならない）。ここでは `system_settings.env_seeded` 監査から「env シードが実際に `ollama_url`
    を挿入した」ことを、かつ「それ以降に `ollama_url`／`ollama_allowlist` への admin 操作
    （`system_settings.updated`）が無い」ことを**両方**証明できた場合だけ追加する。証明できない
    （env_seeded 監査が無い＝旧マーカーのみで監査が残っていない・その後に admin が触っている等）
    場合は一切書かず、呼び出し側（`sherpa.api._catchup_ollama_allowlist_for_central_url`）が
    healthz のたびに警告ログで管理者へ手動追加を促す（自動修復はしない）。

    単一トランザクションで: 1) marker（`guard_key`）の存在確認（既にあれば何もしない）。
    2) 監査ログから証明を試みる。3) `ollama_allowlist` の行を先に確保してから `FOR UPDATE`
    （`seed_system_settings_once` と同じ「確保→ロック」の型・並行 admin 初期化との競合を防ぐ）。
    4) 判定理由を持つ監査を**必ず** marker 挿入の直前に書く（`added`／`already_present`／
    `skipped_unproven` のいずれか）。5) marker は監査insert成功後、同一トランザクションで挿入
    （監査insertが例外を投げれば marker 挿入前にロールバックされる＝監査記録の無い marker 確定が
    起こらない）。

    戻り値: 判定理由（`"added"`／`"already_present"`／`"skipped_unproven"`）。
    """
    _ensure()
    from sherpa import store as _facade
    reason = "skipped_unproven"
    added_host: str | None = None
    before_allowlist: list | None = None
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_ENV_SEED_LOCK,))
        marker_row = c.execute("SELECT 1 FROM system_settings WHERE key=%s", (guard_key,)).fetchone()
        if marker_row is not None:
            return "already_present"   # 既に評価済み（このトランザクションでは何も書いていない）

        # 証明の材料: system_settings に対する env シード／admin 更新の監査を全件見る。
        # 重大バグ是正（RV 5巡目 #2）: `created_at` は「トランザクション**開始**時刻」
        # （db.py の `DEFAULT now()`）であり、確定（advisory lock 解放＝commit）の順序とは限らない。
        # advisory lock を先に取ったが後に開始した（＝created_at が新しい）トランザクションが先に
        # commit することはあり得ないが、逆に「先に開始したが lock 待ちで後から commit した」
        # トランザクションは created_at が古いまま commit が遅れる＝`id`（BIGSERIAL・INSERT 実行
        # 順＝lock 獲得順と一致）でなければ実際の確定順序を判定できない。`created_at` 基準では、
        # この種の admin 更新を「seed より前」と誤認して見逃し得た（同一 created_at＝ミリ秒未満の
        # 差で `>` が偽になるケースも同様に見逃す）。ここでは `id` の大小関係だけで判定する。
        rows = c.execute(
            "SELECT id, action, after_state FROM audit_log "
            "WHERE resource_type='system_settings' "
            "  AND action IN ('system_settings.env_seeded', 'system_settings.updated') "
            "ORDER BY id ASC"
        ).fetchall()
        seed_id = None
        seed_had_url = False
        seed_had_allowlist = False
        seed_url_fingerprint: str | None = None
        for r in rows:
            if r["action"] == "system_settings.env_seeded":
                after = r["after_state"] or {}
                # 指紋フィールドで判定する（`ollama_url` そのものは host 表現へ畳まれており、
                # `_redact_url_for_error` の表記ゆれ・上書きにより厳密一致には使えない・
                # `_URL_SETTINGS_KEYS`／`llm.ollama_url_fingerprint` の docstring 参照）。この
                # フィールドを持たない監査行（`ollama_url_fingerprint` 導入前の形式）は
                # `seed_had_url` が False のまま＝下の `proven` が成立せず fail-closed に
                # `skipped_unproven` へ縮退する（移行措置は不要・関数 docstring の裁定どおり）。
                if "ollama_url_fingerprint" in after:
                    seed_id = r["id"]
                    seed_had_url = True
                    seed_had_allowlist = "ollama_allowlist" in after
                    seed_url_fingerprint = after.get("ollama_url_fingerprint")
        proven = False
        if seed_had_url and not seed_had_allowlist and seed_id is not None:
            tampered = any(
                r["action"] == "system_settings.updated" and r["id"] > seed_id
                and isinstance(r["after_state"], dict)
                and ("ollama_url" in r["after_state"] or "ollama_allowlist" in r["after_state"])
                for r in rows)
            proven = not tampered

        cur_url = ""
        if proven:
            from sherpa import llm
            cur_row = c.execute(
                "SELECT value FROM system_settings WHERE key='ollama_url' FOR UPDATE").fetchone()
            cur_url = str((cur_row["value"] if cur_row else None) or "")
            # 重大バグ是正（RV 5巡目 #2）: 「以降に admin 更新が無い」ことの証明だけでは、audit を
            # 経由しない書込み経路が将来増えた場合に穴になる（二重の安全網）。現在の `ollama_url`
            # の指紋が seed 監査に記録された指紋と一致することも必須にする（同じ接続先を指して
            # いれば表記ゆれ＝ポート省略の有無等があっても一致する・生 URL の文字列一致ではない）。
            cur_fingerprint = llm.ollama_url_fingerprint(cur_url) if cur_url else None
            if cur_fingerprint is None or cur_fingerprint != seed_url_fingerprint:
                proven = False
        if proven:
            hp = llm._canonical_host_port(cur_url) if cur_url else None
            if hp is not None and not llm.is_loopback_host(hp[0]):
                host_entry = llm.format_host_port(hp[0], hp[1])
                # 行を先に確保してから FOR UPDATE（上の docstring 参照）。
                c.execute(
                    "INSERT INTO system_settings (key, value, updated_by) VALUES "
                    "('ollama_allowlist', '[]'::jsonb, 'system') ON CONFLICT (key) DO NOTHING")
                al_row = c.execute(
                    "SELECT value FROM system_settings WHERE key='ollama_allowlist' FOR UPDATE").fetchone()
                current = list((al_row["value"] if al_row else None) or [])
                before_allowlist = current
                if host_entry in current:
                    reason = "already_present"
                else:
                    merged = [*current, host_entry]
                    c.execute(
                        "UPDATE system_settings SET value=%s, updated_at=now(), updated_by='system' "
                        "WHERE key='ollama_allowlist'", (Json(merged),))
                    reason = "added"
                    added_host = host_entry

        # 監査は marker 挿入の直前に必ず書く（4）。
        _facade._audit_insert(
            c, "system", "system_settings.env_seed_catchup", "system_settings", None,
            before_state={"ollama_allowlist": before_allowlist} if before_allowlist is not None else None,
            after_state={"reason": reason, "added": added_host}, severity="info")
        c.execute(
            "INSERT INTO system_settings (key, value, updated_by) VALUES (%s, %s, %s) "
            "ON CONFLICT (key) DO NOTHING", (guard_key, Json(1), "system"))
    _invalidate_system_settings_cache()
    return reason


def count_users_with_personal_keys() -> int:
    """個人秘密キー（openai/gemini/bedrock のいずれか）を保存中のユーザー数。

    管理画面が `personal_api_keys_allowed` を OFF で保存する前に、削除対象件数を確認ダイアログへ
    表示するためのプレビュー用（`purge_personal_api_keys` を実際に呼ぶ前に件数だけ知りたい場合）。
    """
    _ensure()
    with _connect() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM user_settings WHERE "
            "openai_api_key IS NOT NULL OR gemini_api_key IS NOT NULL OR bedrock_api_key IS NOT NULL"
        ).fetchone()
    return int(row["n"]) if row else 0


def purge_personal_api_keys(actor: str = "system") -> int:
    """A6（個人 API キー原則）が偽のとき、全ユーザーの個人秘密キー（openai/gemini/bedrock）を
    NULL へ一括削除する（OFF のとき個人キーは保存しない、という契約を DB 状態としても保つ）。
    呼び出し側（管理画面の保存時・起動時）は `personal_api_keys_allowed` が偽のときに本関数を呼ぶ。

    冪等: 既に NULL の行は WHERE 句で対象外＝実際に変更した行数だけが `RETURNING` に乗る。
    変更が無い（0件）ときは監査行も作らない＝呼び出しのたびに空の監査ノイズを出さない。

    `_PERSONAL_KEY_LOCK` を `update_settings()` の個人キー書込みと共有する（このロックを保持している
    間は個人キーの書込みが完了してから一括削除するか、一括削除の完了を待ってから書込みが拒否
    されるかのどちらかになり、事前チェック後に無効化された個人キーが書込み側で復活しない）。
    """
    _ensure()
    from sherpa import store as _facade   # set_system_settings と同じ理由（monkeypatch シーム維持）
    with _connect() as c:
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_PERSONAL_KEY_LOCK,))
        rows = c.execute(
            "UPDATE user_settings SET openai_api_key=NULL, gemini_api_key=NULL, bedrock_api_key=NULL, "
            "  updated_at=now() "
            "WHERE openai_api_key IS NOT NULL OR gemini_api_key IS NOT NULL OR bedrock_api_key IS NOT NULL "
            "RETURNING user_id").fetchall()
        count = len(rows)
        if count:
            _facade._audit_insert(c, actor, "user_settings.personal_keys_purged", "user_settings", None,
                          detail={"count": count}, severity="warning")
    return count
