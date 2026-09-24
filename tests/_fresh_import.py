"""env 駆動の import-time 固定定数を、実プロセスを起こして検証する共通ヘルパー
（`tests/unit/test_agentic_search.py` 等が参照）。

`sherpa.agentic_search.MAX_HITS` のような「import 時に一度だけ `os.environ` を読んで確定する定数」は、
同一プロセス内の `monkeypatch.setenv()` では「起動時に env がどうだったか」を再現できない
（import 済みのモジュールは reload しない限り値を保持したまま・reload は他テストが握る参照と
食い違いうる副作用があるため使わない）。本モジュールは `sys.executable` で完全に独立したプロセスを
起動し、指定 env の下で行ったインポート／処理の結果を JSON 経由で受け取る。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _build_env(env: dict | None) -> dict:
    full_env = dict(os.environ)
    for k, v in (env or {}).items():
        if v is None:
            full_env.pop(k, None)
        else:
            full_env[k] = v
    return full_env


def run_script(script: str, env: dict | None = None, timeout: float = 30.0) -> str:
    """`script`（Python コード文字列）を新規プロセスで実行し、標準出力（末尾改行除去）を返す。

    `cwd` はリポジトリルート（`sys.path[0]` が空文字＝cwd になる `-c` 実行の挙動を利用し、
    追加の PYTHONPATH 指定なしで `sherpa` パッケージを import できる）。非ゼロ終了は assert で失敗させる。
    """
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=_build_env(env), capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=timeout,
    )
    assert result.returncode == 0, f"fresh import 失敗:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}"
    return result.stdout.strip()


def fresh_import_fails(module_name: str, env: dict | None = None, timeout: float = 30.0) -> str:
    """`module_name` を新規プロセスで import し、**失敗する**ことを確認して stderr を返す。

    `run_script` の逆（成功前提）——import 時に env を検証して明示エラーを送出する定数
    （例: `sherpa.agentic_search._TOOLS_AVAILABILITY_TTL`）が、不正値では実際に起動を落とす
    ことを確認するために使う。成功してしまった場合はテストを失敗させる。
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module_name}"],
        env=_build_env(env), capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=timeout,
    )
    assert result.returncode != 0, f"import が成功してしまった（失敗を期待）: {module_name} env={env}"
    return result.stderr


def fresh_import_attr(module_name: str, attr: str, env: dict | None = None, timeout: float = 30.0):
    """`module_name` を新規プロセスで import し、その直後の `attr`（モジュール属性）の値を返す。

    `env`: 上書きする env の dict。値が `None` ならそのキーを未設定（delenv 相当）にする。
    戻り値は JSON でシリアライズ可能な値（int/float/str/bool/None 等）を想定する。
    """
    script = f"import json\nimport {module_name} as m\nprint(json.dumps(m.{attr}))"
    out = run_script(script, env=env, timeout=timeout)
    return json.loads(out.splitlines()[-1])


def fresh_import_param_default(module_name: str, func_name: str, param: str,
                               env: dict | None = None, timeout: float = 30.0):
    """`module_name` を新規プロセスで import し、`m.{func_name}` の関数シグネチャにおける
    引数 `param` のデフォルト値を返す（`inspect.signature` 越し）。

    引数のデフォルト値は関数定義時（＝モジュール import 時）に評価・固定されるため、
    「起動時の env が既定値と異なる場合でも実際に反映されるか」を確認するのに使う——同一プロセス内で
    既定値と同じ env のまま `inspect.signature(fn).parameters[param].default` を見るだけでは、
    「定数を正しく参照している」場合と「たまたま同じ値をリテラルで書いている（退行）」場合を
    区別できない。
    """
    script = (
        f"import inspect, json\nimport {module_name} as m\n"
        f"print(json.dumps(inspect.signature(m.{func_name}).parameters['{param}'].default))"
    )
    out = run_script(script, env=env, timeout=timeout)
    return json.loads(out.splitlines()[-1])
