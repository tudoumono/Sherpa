"""P1-c（Codex 強化計画 Phase1・生成ファイルカード）単体テスト。

作成ファイルカードの env への載り方・個人由来の印・成果物から除外するファイルは
`test_codex_turn_run.py`（偽 codex で `run()` を実際に動かす）で確かめる。ここでは DL 先エンドポイントの実在を確かめる。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


def test_download_endpoint_matches_url_format():
    """api.py 側に P1-c の DL 先（GET /workspace/files/{file_id}/download）が実在すること
    （created_files の download_url が指す実体との整合確認）。"""
    from sherpa import api
    import inspect
    assert hasattr(api, "workspace_file_download"), "workspace_file_download エンドポイントが無い"
    src = inspect.getsource(api.workspace_file_download)
    assert "get_workspace_file" in src, "所有者確認（store.get_workspace_file）を使っていない"
    assert "FileResponse" in src
