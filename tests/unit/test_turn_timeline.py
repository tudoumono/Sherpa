"""scripts/turn_timeline.py の単体テスト（合成の会話記録で attempt の区切り・道具の時間・往復回数を確かめる）。"""
import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_conversation_trace as tct  # noqa: E402

pytestmark = pytest.mark.unit

_SPEC = importlib.util.spec_from_file_location(
    "turn_timeline", Path(__file__).resolve().parents[2] / "scripts" / "turn_timeline.py")
tl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tl)


def test_attempts_split_by_task_started(tmp_path: Path):
    tct._sessions(tmp_path)
    res = tl.analyze(tl.read_threads(tmp_path / "u1" / "workspace" / ".codex-sessions" / "7"))
    assert len(res["attempts"]) == 3
    first = res["attempts"][0]
    assert first["tool_calls"] == 5
    assert first["tools"]["ripgrep_search"] == 1 and first["tools"]["exec"] == 2
    assert first["rounds"] == 2 and first["input_tokens_resent"] == 300
    assert first["mcp_ms"] == pytest.approx(3000.0)
    assert res["children"]["threads"] == 1 and res["children"]["tool_calls"] == 1
    text = tl.render(res)
    assert "attempts=3" in text and "架空" not in text  # 本文・検索語は出さない


def test_trace_time_breakdown_uses_the_same_counting(tmp_path: Path):
    tct._sessions(tmp_path)
    conv_dir = tmp_path / "u1" / "workspace" / ".codex-sessions" / "7"
    total = tl.analyze(tl.read_threads(conv_dir))["total"]
    threads, _ = tct.CT.read_threads(conv_dir)
    got = tct.CT._ev.timing(threads, 0.0, 1e12)
    assert got == {"tools_ms": round(total["mcp_ms"]), "shell_ms": round(total["exec_ms"]),
                   "model_wait_ms": round(total["model_wait_ms"])}
    assert tct.CT._ev.timing(threads, 0.0, 1.0) is None
