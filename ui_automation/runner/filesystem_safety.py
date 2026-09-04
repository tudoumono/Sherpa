"""Fail-closed guards for recursive filesystem operations."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from pathlib import Path


class MountBoundaryViolation(RuntimeError):
    pass


class HardlinkBoundaryViolation(RuntimeError):
    pass


_MOUNTINFO = Path("/proc/self/mountinfo")
_MOUNTINFO_ESCAPE = re.compile(r"\\([0-7]{3})")


def _decode_mountinfo_path(value: str) -> str:
    return _MOUNTINFO_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def mount_targets_at_or_below(root: Path) -> tuple[Path, ...]:
    """Return mount targets at/below ``root`` using this process' mount namespace.

    Bind mounts can share a device id with their parent, so ``st_dev`` is not a
    sufficient recursive-deletion boundary.  Linux mountinfo records the exact
    target paths, including same-device bind mounts.
    """

    lexical = Path(os.path.abspath(root))
    for component in reversed(lexical.parents):
        if component == Path(component.anchor):
            continue
        try:
            if component.is_symlink():
                raise MountBoundaryViolation("mount-boundary path must not contain symlink components")
        except OSError as exc:
            raise MountBoundaryViolation("mount-boundary path component is unavailable") from exc
    if lexical.is_symlink():
        raise MountBoundaryViolation("mount-boundary root must not be a symlink")
    try:
        metadata = lexical.lstat()
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise MountBoundaryViolation("mount-boundary root is unavailable") from exc
    if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
        raise MountBoundaryViolation("mount-boundary root is not a regular file or directory")
    try:
        lines = _MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MountBoundaryViolation("Linux mountinfo is unavailable or unreadable") from exc
    if not lines:
        raise MountBoundaryViolation("Linux mountinfo is empty")

    targets: set[Path] = set()
    for line in lines:
        prefix, separator, suffix = line.partition(" - ")
        prefix_fields = prefix.split()
        if not separator or len(prefix_fields) < 6 or len(suffix.split()) < 3:
            raise MountBoundaryViolation("Linux mountinfo contains a malformed record")
        decoded = _decode_mountinfo_path(prefix_fields[4])
        mount_target = Path(decoded)
        if not mount_target.is_absolute():
            raise MountBoundaryViolation("Linux mountinfo contains a non-absolute target")
        try:
            mount_target.relative_to(resolved)
        except ValueError:
            continue
        targets.add(mount_target)
    return tuple(sorted(targets))


def assert_no_mount_targets(root: Path) -> None:
    """Refuse recursive reads, permission changes, or deletion across a mount."""

    targets = mount_targets_at_or_below(root)
    if targets:
        raise MountBoundaryViolation(f"recursive operation refused because {len(targets)} mount target(s) exist at or below the owned path")


def ensure_directory_no_follow(path: Path, *, mode: int = 0o700, require_owner_uid: int | None = None) -> None:
    """Create/traverse an absolute directory without following any component symlink."""

    lexical = Path(os.path.abspath(path))
    descriptor = os.open(lexical.anchor, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for index, component in enumerate(lexical.parts[1:], 1):
            flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=mode, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise MountBoundaryViolation("private directory path contains a non-directory component")
            if index == len(lexical.parts) - 1 and require_owner_uid is not None and metadata.st_uid != require_owner_uid:
                os.close(child)
                raise PermissionError("private directory has an unexpected owner")
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)
    assert_no_mount_targets(lexical)


def assert_no_unsafe_hardlinks(root: Path) -> None:
    """Reject regular files whose inode has another directory entry anywhere.

    A hardlink can escape an otherwise private tree.  Recursive writes, chmod,
    or chown must therefore require every regular file to have exactly one link.
    """

    assert_no_mount_targets(root)
    candidates = [root] if not root.is_dir() else [root, *root.rglob("*")]
    for path in candidates:
        metadata = path.lstat()
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise HardlinkBoundaryViolation("recursive operation refused because a multiply-linked regular file exists")


def chmod_tree_no_follow(
    root: Path,
    *,
    directory_mode: int,
    file_mode: int,
    allow_symlinks: bool,
    require_owner_uid: int | None = None,
) -> None:
    """Recursively chmod opened inodes without following path replacements."""

    assert_no_mount_targets(root)
    lexical = Path(os.path.abspath(root))
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(lexical.parent, parent_flags)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        root_before = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        root_descriptor = os.open(lexical.name, flags, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)

    def validate_owner(metadata: os.stat_result) -> None:
        if require_owner_uid is not None and metadata.st_uid != require_owner_uid:
            raise PermissionError("recursive permission target has an unexpected owner")

    def visit(directory_descriptor: int) -> None:
        with os.scandir(directory_descriptor) as iterator:
            entries = list(iterator)
        for entry in entries:
            before = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                if allow_symlinks:
                    continue
                raise MountBoundaryViolation("recursive permission target contains a symlink")
            if not stat.S_ISDIR(before.st_mode) and not stat.S_ISREG(before.st_mode):
                raise MountBoundaryViolation("recursive permission target contains a special file")
            child_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            if stat.S_ISDIR(before.st_mode):
                child_flags |= os.O_DIRECTORY
            else:
                child_flags |= getattr(os, "O_NONBLOCK", 0)
            child_descriptor = os.open(entry.name, child_flags, dir_fd=directory_descriptor)
            try:
                opened = os.fstat(child_descriptor)
                if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
                    before.st_dev,
                    before.st_ino,
                    stat.S_IFMT(before.st_mode),
                ):
                    raise MountBoundaryViolation("recursive permission target changed during traversal")
                validate_owner(opened)
                if stat.S_ISDIR(opened.st_mode):
                    visit(child_descriptor)
                    os.fchmod(child_descriptor, directory_mode)
                elif stat.S_ISREG(opened.st_mode):
                    if opened.st_nlink != 1:
                        raise HardlinkBoundaryViolation("recursive permission target contains a multiply-linked file")
                    os.fchmod(child_descriptor, file_mode)
                else:
                    raise MountBoundaryViolation("recursive permission target contains a special file")
            finally:
                os.close(child_descriptor)

    try:
        root_metadata = os.fstat(root_descriptor)
        if (root_metadata.st_dev, root_metadata.st_ino, stat.S_IFMT(root_metadata.st_mode)) != (
            root_before.st_dev,
            root_before.st_ino,
            stat.S_IFMT(root_before.st_mode),
        ):
            raise MountBoundaryViolation("recursive permission root changed while it was opened")
        validate_owner(root_metadata)
        # The path-based mount check and the opened descriptor must still name
        # the same root before any inode is changed.
        assert_no_mount_targets(lexical)
        current = lexical.lstat()
        if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
            stat.S_IFMT(root_metadata.st_mode),
        ):
            raise MountBoundaryViolation("recursive permission root changed before traversal")
        visit(root_descriptor)
        os.fchmod(root_descriptor, directory_mode)
    finally:
        os.close(root_descriptor)
    assert_no_mount_targets(root)


def chmod_path_no_follow(path: Path, mode: int, *, require_owner_uid: int | None = None) -> None:
    """chmod one opened inode after path, type, owner, and link checks."""

    assert_no_mount_targets(path)
    lexical = Path(os.path.abspath(path))
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(lexical.parent, parent_flags)
    descriptor = -1
    try:
        before = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
            raise MountBoundaryViolation("permission target is not a regular file or directory")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if stat.S_ISDIR(before.st_mode):
            flags |= os.O_DIRECTORY
        else:
            flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(lexical.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != (
            before.st_dev,
            before.st_ino,
            stat.S_IFMT(before.st_mode),
        ):
            raise MountBoundaryViolation("permission target changed while it was opened")
        if require_owner_uid is not None and opened.st_uid != require_owner_uid:
            raise PermissionError("permission target has an unexpected owner")
        if stat.S_ISREG(opened.st_mode) and opened.st_nlink != 1:
            raise HardlinkBoundaryViolation("permission target is a multiply-linked regular file")
        assert_no_mount_targets(lexical)
        current = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
        ):
            raise MountBoundaryViolation("permission target changed before chmod")
        os.fchmod(descriptor, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def unlink_runtime_control_socket_no_follow(runtime: Path, *, require_owner_uid: int) -> bool:
    """Remove only ``control/runner.sock`` after pinned inode/type checks."""

    assert_no_mount_targets(runtime)
    lexical = Path(os.path.abspath(runtime))
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(lexical.parent, parent_flags)
    runtime_descriptor = -1
    control_descriptor = -1
    quarantine_name = f".runner.sock.delete-{os.getpid()}-{secrets.token_hex(8)}"
    renamed = False
    try:
        runtime_before = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        runtime_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        runtime_descriptor = os.open(lexical.name, runtime_flags, dir_fd=parent_descriptor)
        runtime_opened = os.fstat(runtime_descriptor)
        if (
            not stat.S_ISDIR(runtime_opened.st_mode)
            or runtime_opened.st_uid != require_owner_uid
            or (runtime_opened.st_dev, runtime_opened.st_ino) != (runtime_before.st_dev, runtime_before.st_ino)
        ):
            raise MountBoundaryViolation("runtime control-socket root changed or has an unexpected owner")
        assert_no_mount_targets(lexical)
        runtime_current = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (runtime_current.st_dev, runtime_current.st_ino) != (runtime_opened.st_dev, runtime_opened.st_ino):
            raise MountBoundaryViolation("runtime control-socket root changed before traversal")
        try:
            control_before = os.stat("control", dir_fd=runtime_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(control_before.st_mode):
            raise MountBoundaryViolation("runtime control path is not a directory")
        control_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        control_descriptor = os.open("control", control_flags, dir_fd=runtime_descriptor)
        control_opened = os.fstat(control_descriptor)
        if (
            control_opened.st_uid != require_owner_uid
            or stat.S_IMODE(control_opened.st_mode) != 0o700
            or (control_opened.st_dev, control_opened.st_ino) != (control_before.st_dev, control_before.st_ino)
        ):
            raise MountBoundaryViolation("runtime control directory changed or failed ownership/mode validation")
        try:
            socket_before = os.stat("runner.sock", dir_fd=control_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISSOCK(socket_before.st_mode) or socket_before.st_uid != require_owner_uid or socket_before.st_nlink != 1:
            raise MountBoundaryViolation("runtime control socket failed inode/type/owner/link validation")
        os.rename("runner.sock", quarantine_name, src_dir_fd=control_descriptor, dst_dir_fd=control_descriptor)
        renamed = True
        quarantined = os.stat(quarantine_name, dir_fd=control_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(quarantined.st_mode)
            or quarantined.st_uid != require_owner_uid
            or quarantined.st_nlink != 1
            or (quarantined.st_dev, quarantined.st_ino) != (socket_before.st_dev, socket_before.st_ino)
        ):
            try:
                os.stat("runner.sock", dir_fd=control_descriptor, follow_symlinks=False)
                original_name_is_free = False
            except FileNotFoundError:
                original_name_is_free = True
            if original_name_is_free:
                os.rename(quarantine_name, "runner.sock", src_dir_fd=control_descriptor, dst_dir_fd=control_descriptor)
                renamed = False
            raise MountBoundaryViolation("runtime control socket changed during quarantine")
        os.unlink(quarantine_name, dir_fd=control_descriptor)
        renamed = False
        return True
    finally:
        if control_descriptor >= 0:
            os.close(control_descriptor)
        if runtime_descriptor >= 0:
            os.close(runtime_descriptor)
        os.close(parent_descriptor)
        if renamed:
            raise MountBoundaryViolation("runtime control-socket quarantine remains after refusal")


def rmtree_no_follow(root: Path) -> None:
    """Remove one owned directory through a pinned parent descriptor."""

    if not shutil.rmtree.avoids_symlink_attacks:
        raise MountBoundaryViolation("platform rmtree does not provide symlink-attack resistance")
    assert_no_mount_targets(root)
    lexical = Path(os.path.abspath(root))
    if lexical.is_symlink() or not lexical.is_dir():
        raise MountBoundaryViolation("recursive deletion root must be a non-symlink directory")
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_descriptor = os.open(lexical.parent, parent_flags)
    root_descriptor = -1
    quarantine_name = f".{lexical.name}.delete-{os.getpid()}-{secrets.token_hex(8)}"
    renamed = False
    try:
        before = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise MountBoundaryViolation("recursive deletion target changed before removal")
        root_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        root_descriptor = os.open(lexical.name, root_flags, dir_fd=parent_descriptor)
        opened = os.fstat(root_descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise MountBoundaryViolation("recursive deletion root changed while it was opened")
        assert_no_mount_targets(lexical)
        current = os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise MountBoundaryViolation("recursive deletion root changed before quarantine")
        os.rename(lexical.name, quarantine_name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
        renamed = True
        quarantined = os.stat(quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (quarantined.st_dev, quarantined.st_ino) != (opened.st_dev, opened.st_ino):
            # Do not delete an inode that was not the opened, validated root.
            try:
                os.stat(lexical.name, dir_fd=parent_descriptor, follow_symlinks=False)
                original_name_is_free = False
            except FileNotFoundError:
                original_name_is_free = True
            if original_name_is_free:
                os.rename(quarantine_name, lexical.name, src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor)
                renamed = False
            raise MountBoundaryViolation("recursive deletion root changed during quarantine")
        shutil.rmtree(quarantine_name, dir_fd=parent_descriptor)
        renamed = False
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)
    if renamed:
        raise MountBoundaryViolation("recursive deletion quarantine remains after refusal")
    if lexical.exists() or lexical.is_symlink():
        raise MountBoundaryViolation("recursive deletion target remains after removal")
