"""レート制限（プロセス内メモリ・監査台帳 2026-07-10-監査対応台帳.md #4）。

対象は3つに限定した最小実装（過剰な一般化はしない）:
  1. ログイン失敗バックオフ: 同一 uid（正規化しない・文字列そのもの）に対する連続ログイン失敗が
     `LOGIN_FAIL_THRESHOLD` 回に達したら、以後 `LOGIN_LOCKOUT_SECONDS` 秒間はそのuidへのログイン
     試行を拒否する（正しいパスワードでも問答無用で拒否＝仕様）。成功でカウンタをリセット。
  2. ext_api キー単位のレート制限: APIキー（`key_id`）ごとに `EXT_API_RATE_LIMIT_PER_MINUTE`
     リクエスト/60秒（sliding window）。超過は拒否。
  3. ext_api キー単位の日次クォータ（オプトイン）: キーごとに管理者/本人が設定した
     `daily_quota` 回/24時間（固定窓）。未設定（None）のキーは対象外（既存キーは全て無制限のまま）。

**状態はプロセス内メモリ（dict + threading.Lock）に置く。DB/Redis 化はしない（中期課題・監査台帳 #6
でスコープ外）。** これは `sherpa.chat_turns`（背景チャットターンのプロセス内レジストリ）と同じ前提に
乗っている: `sherpa/api.py` の `_warn_multi_worker_chat_turns()` が明記する通り、本プロジェクトの
起動は uvicorn workers=1 前提（`docs/proposals/2026-07-03-チャット背景実行.md` §制約）。複数 worker 構成に
すると、リクエストを受けた worker ごとに別プロセスの state になり、レート制限がすり抜けられる
（＝workers=1 前提が崩れると本モジュールのレート制限は保証を失う。将来 Redis 等の共有ストアへの
移行までの割り切り）。

しきい値は定数（env化しない・監査台帳の決定通り最小実装）。
"""
from __future__ import annotations

import threading
import time

# ==== しきい値（定数・env化しない） ====

LOGIN_FAIL_THRESHOLD = 5          # 連続失敗がこの回数に達したらロックアウト
LOGIN_LOCKOUT_SECONDS = 60.0      # ロックアウト継続時間

EXT_API_RATE_LIMIT_PER_MINUTE = 60   # APIキー単位の上限
EXT_API_RATE_LIMIT_WINDOW_SECONDS = 60.0

# 掃除対象と見なす無操作期間（この秒数を超えて更新の無いエントリは lazy cleanup で捨てる）。
# ロックアウト期間・レート制限ウィンドウのどちらよりも十分長く取る（掃除が有効な制限より先に
# エントリを消してしまわないため）。
_GC_IDLE_SECONDS = 3600.0
_GC_INTERVAL_SECONDS = 300.0   # このデータ構造への呼び出しが GC_INTERVAL 秒に1回だけ間引きを行う


# ==== ログイン失敗バックオフ ====

class _LoginState:
    __slots__ = ("fail_count", "locked_until", "last_update")

    def __init__(self) -> None:
        self.fail_count = 0
        self.locked_until = 0.0   # 0 = ロックされていない
        self.last_update = time.time()


_login_lock = threading.Lock()
_login_state: dict[str, _LoginState] = {}
_login_last_gc = 0.0


def _gc_login_locked(now: float) -> None:
    """呼び出し元が `_login_lock` を保持している前提の lazy cleanup。"""
    global _login_last_gc
    if now - _login_last_gc < _GC_INTERVAL_SECONDS:
        return
    _login_last_gc = now
    stale = [uid for uid, st in _login_state.items() if now - st.last_update > _GC_IDLE_SECONDS]
    for uid in stale:
        del _login_state[uid]


def check_login_lockout(uid: str) -> float | None:
    """ロックアウト中なら残り秒数（>0）を返す。ロックアウト中でなければ None。

    パスワード照合の**前**に呼ぶこと（仕様: 正しいパスワードでも拒否）。
    """
    now = time.time()
    with _login_lock:
        _gc_login_locked(now)
        st = _login_state.get(uid)
        if st is None or st.locked_until <= now:
            return None
        return st.locked_until - now


def record_login_failure(uid: str) -> None:
    """ログイン失敗を記録し、しきい値に達したらロックアウトを開始する。"""
    now = time.time()
    with _login_lock:
        _gc_login_locked(now)
        st = _login_state.setdefault(uid, _LoginState())
        st.fail_count += 1
        st.last_update = now
        if st.fail_count >= LOGIN_FAIL_THRESHOLD:
            st.locked_until = now + LOGIN_LOCKOUT_SECONDS


def record_login_success(uid: str) -> None:
    """ログイン成功でそのuidのカウンタ/ロックアウトをリセットする。

    ロックアウト中はリセットしない（監査台帳 MED-2 の多層防御）: 呼び出し側（`api.py` の
    `auth_login`）は照合成功直後にもう一度 `check_login_lockout` を確認してロック中なら 429 を返し、
    本関数を呼ばない設計だが、万一その二重チェックを経由せず呼ばれても、ロック中のカウンタ消去
    （＝別リクエストが成立させたロックアウトを踏み消すこと）を防ぐ。
    """
    now = time.time()
    with _login_lock:
        st = _login_state.get(uid)
        if st is not None and st.locked_until > now:
            return   # ロック中はカウンタ/ロックアウトを維持する（クリアしない）
        _login_state.pop(uid, None)


# ==== ext_api キー単位のレート制限（sliding window） ====

_ext_lock = threading.Lock()
_ext_hits: dict[int, list[float]] = {}   # key_id -> 直近ウィンドウ内のリクエスト時刻（昇順）
_ext_last_gc = 0.0


def _gc_ext_locked(now: float) -> None:
    """呼び出し元が `_ext_lock` を保持している前提の lazy cleanup。"""
    global _ext_last_gc
    if now - _ext_last_gc < _GC_INTERVAL_SECONDS:
        return
    _ext_last_gc = now
    empty_keys = []
    for key_id, hits in _ext_hits.items():
        cutoff = now - EXT_API_RATE_LIMIT_WINDOW_SECONDS
        i = 0
        while i < len(hits) and hits[i] <= cutoff:
            i += 1
        if i:
            del hits[:i]
        if not hits:
            empty_keys.append(key_id)
    for key_id in empty_keys:
        del _ext_hits[key_id]


def check_ext_api_rate_limit(key_id: int) -> float | None:
    """APIキー単位の sliding window レート制限。

    上限内ならリクエストを記録して None を返す。上限超過なら（記録はせず）ウィンドウが空くまでの
    残り秒数（>0）を返す。
    """
    now = time.time()
    cutoff = now - EXT_API_RATE_LIMIT_WINDOW_SECONDS
    with _ext_lock:
        _gc_ext_locked(now)
        hits = _ext_hits.setdefault(key_id, [])
        i = 0
        while i < len(hits) and hits[i] <= cutoff:
            i += 1
        if i:
            del hits[:i]
        if len(hits) >= EXT_API_RATE_LIMIT_PER_MINUTE:
            return hits[0] + EXT_API_RATE_LIMIT_WINDOW_SECONDS - now
        hits.append(now)
        return None


# ==== ext_api キー単位の日次クォータ（固定窓・24時間でリセット）====
# 既存の per-minute レート制限機構をそのまま流用し、新規に追加するのは日次クォータの判定のみ。
# カレンダー日（JST）境界ではなく、そのキーの直近24時間の**固定窓**（最初の呼び出しから24時間で
# 自動リセット）＝sliding window の per-minute 実装と同じ「時刻ベース・DB不要・プロセス内メモリ」
# の考え方を流用し、タイムゾーン依存を持ち込まない（本モジュールは他のどの関数も timezone を扱わない）。

EXT_API_DAILY_QUOTA_WINDOW_SECONDS = 86400.0

_ext_daily_lock = threading.Lock()
_ext_daily: dict[int, tuple[float, int]] = {}   # key_id -> (窓の開始時刻, その窓内の呼び出し数)
_ext_daily_last_gc = 0.0


def _gc_ext_daily_locked(now: float) -> None:
    """呼び出し元が `_ext_daily_lock` を保持している前提の lazy cleanup。"""
    global _ext_daily_last_gc
    if now - _ext_daily_last_gc < _GC_INTERVAL_SECONDS:
        return
    _ext_daily_last_gc = now
    stale = [kid for kid, (start, _n) in _ext_daily.items()
             if now - start >= EXT_API_DAILY_QUOTA_WINDOW_SECONDS]
    for kid in stale:
        del _ext_daily[kid]


def check_ext_api_daily_quota(key_id: int, quota: int | None) -> float | None:
    """APIキー単位の日次クォータ（固定窓・24時間でリセット）。

    `quota` が None なら常に許可（無制限・記録もしない＝既存キーは全て None のまま無制限）。
    上限内ならリクエストを記録して None を返す。上限超過なら（記録はせず）窓が明けるまでの
    残り秒数（>0）を返す（`check_ext_api_rate_limit` と同じ返値の型）。
    """
    if quota is None:
        return None
    now = time.time()
    with _ext_daily_lock:
        _gc_ext_daily_locked(now)
        start, count = _ext_daily.get(key_id, (now, 0))
        if now - start >= EXT_API_DAILY_QUOTA_WINDOW_SECONDS:
            start, count = now, 0
        if count >= quota:
            _ext_daily[key_id] = (start, count)
            return start + EXT_API_DAILY_QUOTA_WINDOW_SECONDS - now
        _ext_daily[key_id] = (start, count + 1)
        return None


def _reset_for_tests() -> None:
    """テスト間の状態漏れ防止用（本番コードパスからは呼ばない）。"""
    with _login_lock:
        _login_state.clear()
    with _ext_lock:
        _ext_hits.clear()
    with _ext_daily_lock:
        _ext_daily.clear()
