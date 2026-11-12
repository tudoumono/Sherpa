"""`scripts/ocr_retry.py`（make ocr-retry）: 既定は件数を出して確認・理由と資料フォルダで絞る・leased は触らない。"""
from __future__ import annotations

import importlib.util
import io
import sys
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Json

from sherpa import store
from sherpa.store import ocr_jobs
from tests._replace_row import offline_row

_SPEC = importlib.util.spec_from_file_location(
    "ocr_retry", Path(__file__).resolve().parents[2] / "scripts" / "ocr_retry.py")
RETRY = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = RETRY
_SPEC.loader.exec_module(RETRY)


class _Stdin(io.StringIO):
    def __init__(self, text: str, *, tty: bool):
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _insert(world: str, route: str, status: str, error_code: str | None = None) -> None:
    leased = status == "leased"
    with ocr_jobs._connect() as connection:
        connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, attempts, "
            "error_code, lease_owner, lease_token, lease_expires_at) "
            "VALUES (%s,'a.xlsx',%s,%s,%s,%s,%s,%s,%s,3,%s,%s,%s,CASE WHEN %s THEN now() + interval '1 hour' END)",
            (
                world, "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64, route,
                Json({"route_input_id": route, "status": "selected"}), "sha256:" + "e" * 64,
                status, error_code, "worker" if leased else None, "token" if leased else None, leased,
            ),
        )


def _statuses(world: str) -> dict[str, str]:
    with ocr_jobs._connect() as connection:
        return {
            str(row["route_input_id"]): str(row["status"]) for row in connection.execute(
                "SELECT route_input_id, status FROM ocr_jobs WHERE world=%s", (world,),
            ).fetchall()
        }


@pytest.fixture()
def world():
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")
    name = "test-ocr-retry-cli-" + uuid.uuid4().hex
    _insert(name, "oom", "failed", "oom")
    _insert(name, "timeout", "failed", "timeout")
    _insert(name, "leased", "leased")
    yield name
    ocr_jobs.purge_world(name, row=offline_row(name, kind="delete"))


def test_yes_requeues_only_selected_reason(world):
    out = io.StringIO()
    code = RETRY.main(["--reason", "oom", "--world", world, "--yes"], stdout=out)
    assert code == 0
    assert "oom: 1 件" in out.getvalue() and "積み直しました: 1 件" in out.getvalue()
    assert world not in out.getvalue()
    assert _statuses(world) == {"oom": "queued", "timeout": "failed", "leased": "leased"}


def test_without_yes_and_without_terminal_prints_counts_but_changes_nothing(world):
    out = io.StringIO()
    code = RETRY.main(["--world", world], stdin=_Stdin("yes\n", tty=False), stdout=out)
    assert code == 2
    assert "合計: 2 件" in out.getvalue()
    assert _statuses(world) == {"oom": "failed", "timeout": "failed", "leased": "leased"}


def test_terminal_answer_decides_whether_to_run(world):
    out = io.StringIO()
    assert RETRY.main(["--world", world], stdin=_Stdin("no\n", tty=True), stdout=out) == 1
    assert _statuses(world)["oom"] == "failed"
    assert RETRY.main(["--world", world], stdin=_Stdin("yes\n", tty=True), stdout=out) == 0
    assert _statuses(world) == {"oom": "queued", "timeout": "queued", "leased": "leased"}


def test_nothing_to_requeue_is_reported_without_prompt(world):
    out = io.StringIO()
    assert RETRY.main(["--reason", "engine_failure", "--world", world], stdin=_Stdin("", tty=False), stdout=out) == 0
    assert "対象はありません" in out.getvalue()


def test_blank_reason_list_is_rejected():
    assert RETRY.main(["--reason", " , "], stdout=io.StringIO()) == 2
