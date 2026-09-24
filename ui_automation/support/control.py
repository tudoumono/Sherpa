from __future__ import annotations

import json
import socket


def _control_request(config, request: dict) -> dict:
    endpoint = config.require_control_socket()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(max(config.timeout_ms / 1000, 30))
        client.connect(str(endpoint))
        client.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    assert raw, "runner restart control returned an empty response"
    return json.loads(raw.decode("utf-8"))


def restart_application(config, evidence) -> dict:
    request = {
        "action": "restart_app",
        "run_id": config.run_id,
        "profile": config.profile,
    }
    response = _control_request(config, request)
    evidence.write_json("state/application-restart-control.json", response)
    assert response.get("ok") is True and response.get("action") == "restart_app", response
    assert int(response.get("app_start_count") or 0) >= 2, response
    return response


def restart_application_with_profile_env(config, evidence, transition_id: str) -> dict:
    assert transition_id, "runner environment transition id is required"
    request = {
        "action": "restart_app_with_profile_env",
        "run_id": config.run_id,
        "profile": config.profile,
        "transition_id": transition_id,
    }
    response = _control_request(config, request)
    evidence.write_json("state/profile-environment-restart-control.json", response)
    assert response.get("ok") is True, response
    assert response.get("action") == "restart_app_with_profile_env", response
    assert response.get("transition_id") == transition_id, response
    assert isinstance(response.get("changed_keys"), list) and response["changed_keys"], response
    assert int(response.get("app_start_count") or 0) >= 2, response
    return response


def stop_isolated_neo4j(config, evidence) -> dict:
    request = {
        "action": "stop_neo4j",
        "run_id": config.run_id,
        "profile": config.profile,
    }
    response = _control_request(config, request)
    evidence.write_json("state/neo4j-stop-control.json", response)
    assert response.get("ok") is True and response.get("action") == "stop_neo4j", response
    assert response.get("service") == "neo4j" and response.get("available") is False, response
    return response


def start_isolated_neo4j(config, evidence) -> dict:
    request = {
        "action": "start_neo4j",
        "run_id": config.run_id,
        "profile": config.profile,
    }
    response = _control_request(config, request)
    evidence.write_json("state/neo4j-start-control.json", response)
    assert response.get("ok") is True and response.get("action") == "start_neo4j", response
    assert response.get("service") == "neo4j" and response.get("available") is True, response
    return response
