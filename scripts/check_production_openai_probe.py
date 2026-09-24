"""`scripts/check-production.sh` の OpenAI 接続先「検査モード」判定。

env（=初回シードの候補値）と system_settings（=起動後の実効値）を明示的にモード分離するための
判定ロジック（`sherpa/store`／DB へ実際に触れる部分・env 読取）だけを、bash heredoc から切り出した
独立モジュール。bash から `python3 scripts/check_production_openai_probe.py` として1回呼ばれ、
標準出力へ判定結果を複数行出力する契約（`check-production.sh` 側がこの出力を読んで分岐する）。

出力の1行目（ステータス）:
  - `UNAVAILABLE`    : `sherpa` を import できない（依存未導入等）。
  - `DB_UNREACHABLE` : `store.get_system_settings()` が例外。
  - `NO_MARKER`      : DB には到達できたが初回シード未実行（`openai_endpoint_seed_version` 無し）。
  - `MARKER_FOUND`   : 初回シード済み＝system_settings が唯一の真実源。続く4行が
                       `endpoint_kind` / `scheme` / `host` / `port`（`port` 未設定は `-`）。
  - `DB_ENDPOINT_INVALID` : 初回シード済みだが、DB 実効の **kind または base URL** が不正
                       （`openai_endpoint_kind` の生値が `openai`/`azure`/`custom` のいずれでもない、
                       または base_url が runtime と同じ `llm.assert_openai_base_url_allowed()` を
                       通らない＝userinfo/query 混入・不正 scheme/port 等）。理由・生の URL は
                       出力しない。呼び出し元は env 候補モードへフォールバックせず fail として
                       扱う（DB が真実源のため）。

`MARKER_FOUND`／`DB_ENDPOINT_INVALID` 以外（DB 未到達／マーカー無し／sherpa 未導入）の場合は、
続けて env 候補の検証結果
（本番の起動時シード resolver（`sherpa.llm.openai_endpoint_seed_candidate`）をそのまま呼ぶ・自前で
再実装しない＝未知 kind/auth_header・Azure 等で base_url 欠落・userinfo/query 混入は全部これが
検出する）を追加の行として出力する:
  - `ENV_CANDIDATE_INVALID` + 理由（1行・生の env 値は含まない固定文言/reason code のみ）。
  - `ENV_CANDIDATE_OK` + `endpoint_kind` / `scheme` / `host` / `port`（`MARKER_FOUND` と同じ4フィールド・
    `openai_endpoint_kind()`/`openai_base_url()` 経由＝kind 欠落時の host 推定も runtime と同じ
    ロジックを共有する）。

**生の URL は一切出力しない**（`sherpa/llm.py::_redact_url_for_error` と同じ方針）: scheme/host/port
だけを分解して返す＝呼び出し元（bash）が組み立て直す文字列にも path/query/フラグメント/params が
混入しない。**env の値をこのスクリプトの argv に載せない**（`OPENAI_BASE_URL` 等はこのプロセスが
`os.environ` から直接読む＝呼び出し元 bash は env 変数をそのまま子プロセスへ継承させるだけで、
コマンドライン引数として渡さない＝`ps`/`/proc/<pid>/cmdline` 経由の露出を避ける）。

独立モジュールに切り出した理由: bash heredoc に埋め込んだままだと、`store.get_system_settings()`／
`sherpa.llm.openai_endpoint_seed_candidate()` をモックした単体テストが書けない（bash プロセス
境界を越えられない）。`probe()`/`env_candidate_status()` を分離することで
`tests/unit/test_check_production_openai_probe.py` が実 DB・実 env 抜きで判定ロジックを固定できる。
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

# ファイルとして直接実行される（`python3 scripts/check_production_openai_probe.py`）と sys.path[0] は
# このファイル自身のディレクトリ（scripts/）になり、リポジトリ直下の `sherpa` パッケージが見えない
# （`scripts/azure_smoke.py` と同じ事情・同じ対処）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _scheme_host_port(base: str) -> tuple[str, str, str]:
    """`base`（有効な URL）を `(scheme, host, port)` へ分解する（`port` 未設定は `"-"`）。
    解析不能なら空文字列3つ（呼び出し元は host 空を「解析できません」として扱う）。

    `host` は**角括弧を付けない生の hostname**（`urlsplit().hostname` そのまま）を返す。
    `check-production.sh` はこの `host` を `getent`（DNS 解決）／`/dev/tcp/<host>/<port>`（TCP 疎通）
    へそのまま渡す＝どちらも角括弧を含まない生ホストを要求するため、ここで角括弧を付けると
    正当な IPv6 接続先が名前解決に失敗し hard fail する。表示用（人が読むメッセージに埋め込む
    `scheme://[host]:port` 形式）の角括弧付け足しは呼び出し側（bash の `ok`/`fail` 表示専用）の
    責務にする＝この関数は接続用の生値だけを返す。
    """
    if not base:
        return "", "", "-"
    try:
        u = urlsplit(base)
    except (ValueError, TypeError):
        return "", "", "-"
    port = str(u.port) if u.port else "-"
    host = u.hostname or ""
    return u.scheme or "", host, port


def validate_endpoint_settings(s: dict) -> tuple[str, str, str, str] | None:
    """`s`（system_settings 相当の dict）の `openai_endpoint_kind`／`openai_base_url` が矛盾しない
    かを検証し、妥当なら `(kind, scheme, host, port)`（`port` 未設定は `"-"`）を返す。不正なら
    `None`。

    kind は `sherpa.llm.openai_endpoint_kind()` をそのまま呼ぶ（自前で
    `s.get("openai_endpoint_kind") or "openai"` のように再実装しない＝kind が未設定（admin が明示
    選択していない・base_url だけ古い形で残っている等）のとき無条件で "openai" 扱いにしてしまうと、
    runtime の「kind 未指定なら base_url の host から推定」（`.openai.azure.com` 等のサフィックス
    判定）と食い違う）。

    base_url は **`llm.openai_base_url()` の縮退後URLではなく `s` の生値**（`s.get("openai_base_url")`）
    を検証する。`openai_base_url()` は kind!=openai なのに base が空の場合に本家既定へ黙って
    fail-safe する契約（`assert_openai_endpoint_consistent` docstring 参照）を持つため、生値のまま
    検証しないと「azure 選択・base_url 欠落」という不整合状態が本家既定へ縮退した後の値
    （`https://api.openai.com/v1`）を通してしまう（実際の呼び出しは fail-safe で本家へ送られ、
    Azure 向け資格情報が誤送信されうる）。

    生の kind/base へ先に `llm.assert_openai_endpoint_consistent()` を適用し（kind!=openai なのに
    base が空なら拒否）、kind!=openai なら生 base をさらに `llm.assert_openai_base_url_allowed()`
    へ通す（userinfo/query 混入・不正 scheme/port 等）。

    `probe()`（DB の実効値＝初回シード済みの `system_settings`）と、`sherpa.scripts.doctor_checks.
    _openai_endpoint_status()`（初回シード前＝DB の既存値と env 候補を「DB 優先・欠損だけ env で
    補完」で合成した後の実効値）の両方がこの同じ判定を共有する（判定条件を複製しない）。
    """
    # 非空の生 kind は先に openai/azure/custom の列挙として検証する（`llm.openai_endpoint_kind()`
    # は既知の enum に含まれない文字列値でも黙って base host 推定へフォールバックするため、
    # "bogus" のような破損値（文字列だが未知）を見逃してしまう）。空（未設定）のときだけ
    # host 推定へ委ねる（管理者が明示選択していない・env シードの host 推定に委ねた状態は正当）。
    # 非文字列（型そのものの破損）は `llm.openai_endpoint_kind()`/`llm.openai_base_url()` 自身が
    # 判定分岐より先に検査し `ValueError` を送出する契約（`str(... or "")` のような素朴な
    # falsy 潰しで先読みしない）ため、ここでは文字列である場合の enum チェックだけを行う。
    raw_kind = s.get("openai_endpoint_kind")
    if isinstance(raw_kind, str) and raw_kind.strip() and raw_kind.strip().lower() not in (
            "openai", "azure", "custom"):
        return None
    from sherpa import llm
    try:
        kind = llm.openai_endpoint_kind(s)
        raw_base = s.get("openai_base_url")
        if raw_base is not None and not isinstance(raw_base, str):
            raise ValueError("接続先 URL の保存値が不正です（文字列ではありません）")
        raw_base = (raw_base or "").strip()
        llm.assert_openai_endpoint_consistent(kind, raw_base)
        if kind != "openai":
            llm.assert_openai_base_url_allowed(raw_base)
    except ValueError:
        return None
    base = raw_base if kind != "openai" else ""
    scheme, host, port = _scheme_host_port(base)
    return kind, scheme, host, port


def probe(get_system_settings) -> list[str]:
    """`get_system_settings`（`sherpa.store.get_system_settings` 相当の0引数 callable）を1回呼び、
    出力行のリストを返す（I/O は呼び出し元の callable に閉じる＝本関数自体はテスト容易）。

    検証条件自体は `validate_endpoint_settings()` に委ねる（`DB_ENDPOINT_INVALID` はそちらが
    `None` を返した場合・理由文言は出力しない＝preflight の標準出力はログに残りうる）。呼び出し元
    （`check-production.sh`）はこれを **fail** として扱う（env 候補モードへのフォールバックはしない
    ＝マーカーは確定済みで DB が真実源のため、別経路の env 値を検査しても実際の問題を見逃す）。
    """
    try:
        s = get_system_settings()
    except Exception:
        return ["DB_UNREACHABLE"]
    if s.get("openai_endpoint_seed_version") is None:
        return ["NO_MARKER"]
    result = validate_endpoint_settings(s)
    if result is None:
        return ["DB_ENDPOINT_INVALID"]
    kind, scheme, host, port = result
    return ["MARKER_FOUND", kind, scheme, host, port]

def env_candidate_status(seed_candidate_fn) -> list[str]:
    """`seed_candidate_fn`（`sherpa.llm.openai_endpoint_seed_candidate` 相当の0引数 callable・
    I/O なし・env を読むだけ）を1回呼び、env 候補の検証結果を返す。

    本番の起動時シード resolver をそのまま再利用する（`scripts/azure_smoke.py` と同じ方針）。
    bash 側で https/host/port の形式だけを独自に検査する簡易チェックは行わない＝
    `SHERPA_OPENAI_ENDPOINT_KIND`/`SHERPA_OPENAI_AUTH_HEADER` の enum 妥当性・「kind が openai
    以外なのに base_url が無い」というクロス検証・userinfo/query の混入検出は resolver に委ねる
    （env 候補モードの preflight を本番の判定と同じ厳密さに揃える）。
    """
    try:
        candidate = seed_candidate_fn()
    except ValueError as e:
        return ["ENV_CANDIDATE_INVALID", str(e)]
    base = str(candidate.get("openai_base_url") or "")
    if not base:
        return ["ENV_CANDIDATE_OK", "openai", "", "", "-"]
    from sherpa import llm
    kind = llm.openai_endpoint_kind(candidate)   # 明示 kind 優先・未指定なら host から推定（runtime と同じ）
    scheme, host, port = _scheme_host_port(llm.openai_base_url(candidate))
    return ["ENV_CANDIDATE_OK", kind, scheme, host, port]


_CONNECT_TIMEOUT = 3.0   # 秒。check-production.sh 側の外枠 `timeout 5` より短くする（余裕を持って
                         # DB_UNREACHABLE を自ら報告できるように＝外枠に kill される前に判定を返す）。


def _db_reachable() -> bool:
    """`store.get_system_settings()`（timeout 引数を持たない）を呼ぶ前の軽い到達性チェック。

    psycopg のデフォルト接続は OS レベルの TCP connect タイムアウト（到達不能アドレスによっては
    数十秒〜）に依存するため、外枠の bash `timeout 5` に process ごと kill されると、この
    スクリプト自身は `DB_UNREACHABLE` を一度も出力できないまま終わる（呼び出し元は「判定不能」の
    汎用メッセージへ倒れるだけで、動作としては安全だが理由が不正確になる）。ここで明示的に短い
    `connect_timeout` を付けて先に1回だけ繋ぎ、失敗なら早期に `DB_UNREACHABLE` を確定して返す
    （`store._dsn()` は本番と同じ DSN 組み立てをそのまま再利用・再実装しない）。
    """
    try:
        import psycopg
        from sherpa.store.db import _dsn
    except Exception:
        return False
    try:
        with psycopg.connect(_dsn(), connect_timeout=_CONNECT_TIMEOUT):
            pass
        return True
    except Exception:
        return False


def main() -> int:
    try:
        from sherpa import store
    except Exception:
        print("UNAVAILABLE")
        _print_env_candidate()
        return 0
    if not _db_reachable():
        print("DB_UNREACHABLE")
        _print_env_candidate()
        return 0
    lines = probe(store.get_system_settings)
    for line in lines:
        print(line)
    # `DB_ENDPOINT_INVALID` はマーカー確定済み＝DB が唯一の真実源（`probe()` docstring 参照）。
    # env 候補は別経路の値のため、ここで検査しても実際の問題（DB の不正値）を見逃す＝
    # `MARKER_FOUND` と同様、env 候補モードへは倒さない。
    if lines[0] not in ("MARKER_FOUND", "DB_ENDPOINT_INVALID"):
        _print_env_candidate()
    return 0


def _print_env_candidate() -> None:
    """DB モードが使えない（`MARKER_FOUND` 以外）場合の締めくくり＝env 候補モードの検証結果を
    追加の行として出力する。

    `sherpa.llm`（`sherpa.api` ではない）から検証関数を読む＝`sherpa/api.py` は FastAPI アプリ全体
    （`fastapi`/`anthropic` 等の重い依存）を import するため、依存未導入の環境で `UNAVAILABLE` に
    倒れた直後にさらに `sherpa.api` を import しようとすると同じ理由でまた失敗する（`sherpa.llm` は
    stdlib のみの軽量モジュール＝依存未導入でも import できる）。失敗時は候補検証自体をスキップ
    （bash 側は env 未検証のまま従来の "OPENAI_BASE_URL is not set" 等へ委ねる）。
    """
    try:
        from sherpa import llm as _llm
    except Exception:
        return
    for line in env_candidate_status(_llm.openai_endpoint_seed_candidate):
        print(line)


if __name__ == "__main__":
    sys.exit(main())
