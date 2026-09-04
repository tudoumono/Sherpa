"""P1-c（Codex 強化計画 Phase1・生成ファイルカード）単体テスト。

CodexProvider.run() の Codex サブプロセス実行部は既存 test_codex_workspace_authoring.py と同じ
「ソース検査」方式（subprocess を起動しない）で確認する。台帳登録（record_workspace_file）自体の
挙動は既存テストで担保済みのため、ここでは P1-c で新設した配線（_created_file_rows → env["created_files"]、
DL URL の形）に絞って検証する。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


def _src():
    import inspect
    return inspect.getsource(A.CodexProvider.run) + inspect.getsource(A.CodexProvider._run_authoring)


def test_created_file_rows_captured_from_record_workspace_file():
    src = _src()
    assert "_created_file_rows: list[dict] = []" in src, "_created_file_rows の初期化が無い"
    assert "_created_file_rows.append(_row)" in src, "record_workspace_file の戻り値を溜めていない"
    i_record = src.index("_store.record_workspace_file(")
    i_append = src.index("_created_file_rows.append(_row)")
    assert i_record < i_append, "record_workspace_file の呼び出しより前に append している（戻り値未使用の疑い）"


def test_env_created_files_built_with_correct_shape_and_url():
    src = _src()
    assert 'env["created_files"] = [' in src, "env['created_files'] の構築が無い"
    assert '"name": r["rel_path"]' in src, "created_files の name が rel_path 由来でない"
    # P1-c: 新設した DL エンドポイント（/workspace/files/{file_id}/download）と一致するURLを組み立てること。
    assert '"download_url": f"/workspace/files/{r[\'id\']}/download"' in src, \
        "created_files の download_url が新設 DL エンドポイントの形と一致しない"


def test_created_files_only_set_when_rows_nonempty():
    src = _src()
    i_guard = src.index("if _created_file_rows:")
    i_build = src.index('env["created_files"] = [')
    assert i_guard < i_build, "created_files が空判定なしで無条件に書かれている"


def test_created_files_placed_after_codex_wrote_files_flag():
    """codex_wrote_files（共有ブロック連動の既存フラグ）の設定を壊さず、その後段に created_files を足す配線。"""
    src = _src()
    i_flag = src.index('env["codex_wrote_files"] =')
    i_created = src.index('env["created_files"] =')   # 代入の実箇所（先頭の型注釈コメントと区別・末尾 = 必須）
    assert i_flag < i_created


def test_agents_md_excluded_from_both_snapshots():
    """ルート直下の AGENTS.md はスナップショット後に write_agents_md() が書くため、
    before/after 両方のスキャンから除外しないと初回実行で成果物と誤認 → files/ へ move され
    （authoring から消える）→ 毎回 AGENTS_N.md が台帳に蓄積する。両側に除外があること。"""
    src = _src()
    assert src.count('p.relative_to(ws_authoring) != Path("AGENTS.md")') == 2, \
        "AGENTS.md の除外が before/after の両スキャンに入っていない"


def test_download_endpoint_matches_url_format():
    """api.py 側に P1-c の DL 先（GET /workspace/files/{file_id}/download）が実在すること
    （created_files の download_url が指す実体との整合確認）。"""
    from sherpa import api
    import inspect
    assert hasattr(api, "workspace_file_download"), "workspace_file_download エンドポイントが無い"
    src = inspect.getsource(api.workspace_file_download)
    assert "get_workspace_file" in src, "所有者確認（store.get_workspace_file）を使っていない"
    assert "FileResponse" in src
