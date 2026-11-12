"""起点語ヒントの照会失敗は、種類を問わずグラフ不調として空で続行する（直読へ進む）。"""
import pytest

from sherpa import chat_service as CS


class _Session:
    def __init__(self, exc):
        self._exc = exc

    def run(self, *a, **k):
        raise self._exc


def _auth_error():
    from neo4j.exceptions import AuthError
    return AuthError._hydrate_neo4j(code="Neo.ClientError.Security.Unauthorized", message="unauthorized")


@pytest.mark.parametrize("make", [_auth_error, lambda: PermissionError("denied"), lambda: RuntimeError("x")])
def test_known_terms_graph_failure_of_any_kind_degrades(make):
    out = CS._known_terms(_Session(make()), "w1")
    assert list(out) == []
    assert out.failed is True
