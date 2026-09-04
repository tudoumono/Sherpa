from __future__ import annotations

import hashlib
from pathlib import Path

from ui_automation.runner.filesystem_safety import assert_no_mount_targets

from .database import usage_event_checkpoint, usage_events_after


def hash_tree(root: Path) -> dict[str, str]:
    assert root.is_dir(), f"real World fixture directory is missing: {root}"
    assert_no_mount_targets(root)
    hashes = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), f"real World fixture must not contain symlinks: {path}"
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert hashes, f"real World fixture is empty: {root}"
    return hashes


def ensure_real_world(api, config, evidence) -> str:
    config.require_isolated()
    before = api.get_json("/worlds", save_as="state/worlds-before.json").get("worlds") or []
    matching = [world for world in before if Path(world.get("root_path") or "").resolve() == config.world_path]
    assert len(matching) <= 1, f"duplicate World bindings for {config.world_path}"
    created = False
    if matching:
        world = matching[0]
    else:
        assert not before, f"isolated UI stack already contains a different World; refusing to alter an unexpected binding: {before}"
        usage_checkpoints = {kind: usage_event_checkpoint(config.database_url, kind) for kind in ("embed", "vlm")}
        response = api.post_json(
            "/worlds",
            {"path": str(config.world_path), "label": "UI Automation Evidence World"},
            save_as="state/world-create.json",
        )
        world = response.get("world") or {}
        created = True
    world_id = str(world.get("world_id") or "")
    assert world_id, f"World registration returned no world_id: {world}"
    if created:
        for kind, checkpoint in usage_checkpoints.items():
            usage_world = world_id if kind == "embed" else None
            for usage in usage_events_after(
                config.database_url,
                kind,
                checkpoint,
                world=usage_world,
            ):
                evidence.record_usage_event(
                    usage,
                    turn_id=f"{kind}:{usage['id']}",
                    operation=f"world-{kind}",
                )
    evidence.write_json("state/files.sha256", hash_tree(config.world_path))
    if created:

        def cleanup() -> None:
            api.delete_json(f"/worlds/{world_id}", save_as="state/world-delete.json")

        evidence.add_cleanup(f"delete World {world_id}", cleanup)
    return world_id
