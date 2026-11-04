"""回答の時間の調べ（`scripts/slow_turn_report.py`）が、資料の名前やパスを出さずに道具の呼び出しを分けられるか。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


R = _load("slow_turn_report")
CT = _load("conversation_trace")


def test_exec_code_is_split_into_tools_and_shell_programs_without_paths():
    code = ('const [a, b] = await Promise.all([tools.mcp__sherpa__ripgrep_search({query:"受付"}), '
            'tools.mcp__sherpa__es_search({query:"仕様"})]);\n'
            'const r = await tools.exec_command({cmd:"sed -n \'1,40p\' /data/kb/w/設計/詳細設計書.md","workdir":"/x"});\n'
            "const s = await tools.exec_command({cmd:'cat /data/kb/w/顧客一覧.csv'});")
    parts = R.exec_parts(CT, code)
    assert parts == ["ripgrep_search", "es_search", "シェル sed", "シェル cat"]
    assert R._call_key(parts) == "es_search・ripgrep_search・シェル cat…"
    assert "設計" not in "".join(parts) and "/data" not in "".join(parts)


def test_unknown_programs_and_odd_names_are_not_shown():
    assert R.exec_parts(CT, 'await tools.exec_command({cmd:"./顧客集計.sh --all"})') == ["シェル その他"]
    assert R._tool_name("mcp__sherpa__帳票") == "その他"
    assert R._code("ConnectionRefusedError") == "ConnectionRefusedError"
    assert R._code("/mnt/share/設計 が見つかりません") == "その他"


def test_busy_time_does_not_double_count_overlapping_calls():
    calls = [SimpleNamespace(ts=0.0, duration_ms=10_000), SimpleNamespace(ts=5.0, duration_ms=10_000),
             SimpleNamespace(ts=30.0, duration_ms=2_000), SimpleNamespace(ts=40.0, duration_ms=None)]
    assert R._busy_seconds(calls) == pytest.approx(17.0)
