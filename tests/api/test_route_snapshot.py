"""ルート表スナップショット（refactoring-plan フェーズ0・フェーズ1/3 の保険）。

`sherpa.api.app.routes` の「メソッド＋パス＋タグ＋endpoint 関数名」を**定義順**で固定する。
ルートの前方一致・重複登録の解決は定義順に依存するため、順序も含めて golden 化する。
掃除（フェーズ1）や分割（フェーズ3・APIRouter 化）でルート表が意図せず変わっていないかを
検知し、意図した増減（削除したレガシールート等）だけが差分に出ることを担保する。

golden の更新手順（**意図した増減のときだけ**実行し、差分を目視確認してからコミットする）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/api'); \
         import test_route_snapshot as t; t._write_golden()"

行フォーマット（TSV・4 列）:
    "<methods>\t<path>\t<tags>\t<endpoint 関数名>"
  - methods: HEAD/OPTIONS を除いたソート済みメソッド（Mount は "MOUNT"）
  - tags: APIRoute の tags をカンマ連結（フレームワーク既定ルート/Mount は空）
"""
from __future__ import annotations

import pathlib

from fastapi.routing import APIRoute
from starlette.routing import Mount

from sherpa.api import app

_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "routes.txt"


def _api_route_line(r: APIRoute) -> str:
    methods = ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))
    tags = ",".join(r.tags or [])
    return f"{methods}\t{r.path}\t{tags}\t{r.endpoint.__name__}"


def _route_lines(application) -> list[str]:
    """app.routes を定義順で TSV 1 行/ルートに整形する（golden とテストで同一ロジック）。"""
    lines: list[str] = []
    for r in application.routes:
        if isinstance(r, APIRoute):
            lines.append(_api_route_line(r))
        elif isinstance(r, Mount):
            lines.append(f"MOUNT\t{r.path}\t\t{r.name}")
        elif hasattr(r, "effective_route_contexts"):
            # FastAPI 0.139+ の遅延 `app.include_router()`（`_IncludedRouter`・E1 の ext_api.router で
            # 初導入）。app.routes には未展開のまま積まれるため、`effective_route_contexts()` で
            # 実体の APIRoute（`original_route`）を取り出す。
            for ctx in r.effective_route_contexts():
                if isinstance(ctx.original_route, APIRoute):
                    lines.append(_api_route_line(ctx.original_route))
        else:  # Starlette 既定ルート（/openapi.json・/docs・/redoc 等）
            methods = ",".join(sorted((r.methods or set()) - {"HEAD", "OPTIONS"}))
            lines.append(f"{methods}\t{getattr(r, 'path', '')}\t\t{getattr(r, 'name', '')}")
    return lines


def _write_golden() -> None:
    """現状の app.routes から golden を書き出す（更新手順は module docstring 参照）。"""
    _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN.write_text("\n".join(_route_lines(app)) + "\n", encoding="utf-8")


def test_route_table_matches_golden():
    """ルート表（メソッド＋パス＋タグ＋endpoint 名＋定義順）が golden と完全一致する。"""
    assert _GOLDEN.exists(), f"golden 未生成: {_GOLDEN}（module docstring の手順で作成）"
    expected = _GOLDEN.read_text(encoding="utf-8").splitlines()
    actual = _route_lines(app)
    assert actual == expected, (
        "ルート表が golden と不一致。意図した増減なら module docstring の手順で golden を更新すること。\n"
        f"追加された行: {[ln for ln in actual if ln not in expected]}\n"
        f"消えた行: {[ln for ln in expected if ln not in actual]}"
    )
