"""認証の純ヘルパ（パスワードハッシュ・セッショントークン）。

- パスワードは pbkdf2_hmac(sha256)。
- セッションは不透明トークンを cookie に入れ、DB には token の SHA-256 hash だけを保存する。
設計: docs/design/users.md「ログインとセッション」
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ALG = "pbkdf2_sha256"
_ITER = 200_000
DEFAULT_ADMIN_PASSWORD = "Sherpa2026!"

# 存在しない uid のログイン試行でも pbkdf2 を実行してタイミングを揃えるための固定ダミーハッシュ
_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$200000$"
    "9c1a2e6f3b8d4507a1c2e3f4b5a6d7c8$"
    "6c0be4bc565271d11424ba8c7939888df807d511c0da23a383139c41d68dfd17"
)


def hash_password(pw: str) -> str:
    """`pbkdf2_sha256$iters$salt_hex$hash_hex` 形式。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _ITER)
    return f"{_ALG}${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    """保存 hash と照合（定数時間比較・不正形式は False）。"""
    if not stored or not stored.startswith(_ALG + "$"):
        return False
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


def new_token() -> str:
    """cookie に入れる不透明トークン（十分長いランダム）。DB には hash のみ保存。"""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def initial_admin_password() -> str:
    """初期 admin のブートストラップパスワード。環境変数が無ければローカル配布用の既定値。"""
    return os.environ.get("SHERPA_ADMIN_PASSWORD") or DEFAULT_ADMIN_PASSWORD


def auth_enabled() -> bool:
    """認証が有効か（既定で有効。`SHERPA_AUTH_DISABLED=1` の明示時だけ無効）。"""
    return not auth_disabled()


def auth_disabled() -> bool:
    """開発・テスト互換モード。`SHERPA_AUTH_DISABLED=1` の明示時だけ合成 admin を返す。

    本番プロファイル（`SHERPA_ENV` が prod/production）では常に False（毎リクエスト呼ばれるためログは出さない）。
    """
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    if env in ("prod", "production"):
        return False
    return os.environ.get("SHERPA_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
