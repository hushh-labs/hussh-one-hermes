# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Contained mounted-tree adapter for explicitly approved cloud roots."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
import uuid
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable

from .contracts import CatalogEntry, ScanLimits, SourceBinding


class SourceAccessError(RuntimeError):
    pass


_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".json", ".csv"})
_DOCUMENT_SUFFIXES = frozenset({".docx", ".xlsx", ".ipynb"})
_METADATA_SUFFIXES = frozenset({
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".tiff",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
})
_PLACEHOLDER_SUFFIXES = frozenset({".gdoc", ".gsheet", ".gslides", ".gdraw"})


def _entry_id(_source_id: str, _relative_path: str) -> str:
    """Create a non-derivable candidate id for a newly observed file.

    The index preserves it using a same-path or stable inode match on later
    reconciliations. No filename-derived identifier crosses the SQLite boundary.
    """
    return f"src_entry_{uuid.uuid4().hex}"


def _revision(st: os.stat_result) -> str:
    value = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _classify(path: Path, st: os.stat_result) -> tuple[str, str]:
    suffix = path.suffix.lower()
    flags = int(getattr(st, "st_flags", 0) or 0)
    offline_flag = int(getattr(stat, "UF_OFFLINE", 0) or 0)
    is_icloud_placeholder = path.name.startswith(".") and path.name.endswith(".icloud")
    if is_icloud_placeholder or suffix in _PLACEHOLDER_SUFFIXES or flags & offline_flag:
        return "not_materialized", "cloud_placeholder"
    if suffix in _TEXT_SUFFIXES:
        return "available", "text"
    if suffix in _DOCUMENT_SUFFIXES:
        return "available", "document"
    if suffix in _METADATA_SUFFIXES:
        return "metadata_only", "metadata"
    return "metadata_only", "unsupported"


class MountedTreeAdapter:
    source_kinds = frozenset({"icloud_drive", "google_drive", "local_drive"})

    def bind(
        self,
        *,
        source_id: str,
        source_kind: str,
        label: str,
        root: Path,
        created_at: str,
        access_mode: str = "observe",
    ) -> SourceBinding:
        if source_kind not in self.source_kinds:
            raise SourceAccessError(
                "Only mounted iCloud Drive and Google Drive roots are supported."
            )
        if not root.is_absolute():
            raise SourceAccessError("A source binding requires an absolute root path.")
        try:
            root_lstat = root.lstat()
            if stat.S_ISLNK(root_lstat.st_mode):
                raise SourceAccessError("A source root cannot be a symbolic link.")
            resolved = root.resolve(strict=True)
            st = resolved.stat()
        except SourceAccessError:
            raise
        except OSError as exc:
            raise SourceAccessError("The selected source root is unavailable.") from exc
        if not stat.S_ISDIR(st.st_mode):
            raise SourceAccessError("The selected source root is not a directory.")
        normalized_label = label.strip()
        if not normalized_label or len(normalized_label) > 120:
            raise SourceAccessError("A concise source label is required.")
        if access_mode not in {"observe", "manage"}:
            raise SourceAccessError("Source access must be observe or manage.")
        return SourceBinding(
            source_id=source_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            label=normalized_label,
            root_locator=str(resolved),
            root_device=int(st.st_dev),
            root_inode=int(st.st_ino),
            created_at=created_at,
            read_only=access_mode == "observe",
            access_mode=access_mode,  # type: ignore[arg-type]
        )

    def validate(self, binding: SourceBinding) -> Path:
        root = Path(binding.root_locator)
        try:
            root_lstat = root.lstat()
            if stat.S_ISLNK(root_lstat.st_mode):
                raise SourceAccessError(
                    "The source root was replaced by a symbolic link."
                )
            resolved = root.resolve(strict=True)
            st = resolved.stat()
        except SourceAccessError:
            raise
        except OSError as exc:
            raise SourceAccessError("The bound source root is unavailable.") from exc
        if resolved != root or not stat.S_ISDIR(st.st_mode):
            raise SourceAccessError(
                "The bound source root identity is no longer valid."
            )
        if (int(st.st_dev), int(st.st_ino)) != (
            binding.root_device,
            binding.root_inode,
        ):
            raise SourceAccessError(
                "The bound source root was replaced; bind it again."
            )
        return root

    def resolve_entry(self, binding: SourceBinding, relative_path: str) -> Path:
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise SourceAccessError("The catalog path is invalid.")
        root = self.validate(binding)
        candidate = root.joinpath(*pure.parts)
        try:
            if candidate.is_symlink():
                raise SourceAccessError(
                    "Symbolic links are not readable source entries."
                )
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except SourceAccessError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceAccessError(
                "The source entry is unavailable or escaped its root."
            ) from exc
        return resolved

    @staticmethod
    def _relative_parts(relative_path: str) -> tuple[str, ...]:
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise SourceAccessError("The source path is invalid.")
        return pure.parts

    def resolve_destination(
        self,
        binding: SourceBinding,
        relative_path: str,
        *,
        create_parents: bool = False,
    ) -> Path:
        """Resolve a not-yet-existing destination without leaving the bound root."""
        parts = self._relative_parts(relative_path)
        root = self.validate(binding)
        parent = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
        if create_parents:
            current = root
            for part in parts[:-1]:
                candidate = current / part
                if candidate.exists() and candidate.is_symlink():
                    raise SourceAccessError(
                        "A destination parent cannot be a symbolic link."
                    )
                candidate.mkdir(exist_ok=True)
                resolved = candidate.resolve(strict=True)
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise SourceAccessError(
                        "The destination escaped its source root."
                    ) from exc
                current = resolved
            parent = current
        try:
            resolved_parent = parent.resolve(strict=True)
            resolved_parent.relative_to(root)
        except (OSError, ValueError) as exc:
            raise SourceAccessError(
                "The destination parent is unavailable or unsafe."
            ) from exc
        destination = resolved_parent / parts[-1]
        if destination.exists() and destination.is_symlink():
            raise SourceAccessError("A destination cannot replace a symbolic link.")
        return destination

    def create_file(
        self, binding: SourceBinding, *, relative_path: str, content: bytes
    ) -> None:
        destination = self.resolve_destination(
            binding, relative_path, create_parents=True
        )
        if destination.exists():
            raise SourceAccessError("The destination already exists.")
        temporary = destination.with_name(
            f".{destination.name}.hussh-tmp-{uuid.uuid4().hex}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise SourceAccessError(
                    "The destination was created concurrently."
                ) from exc
            temporary.unlink()
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()

    def atomic_overwrite(
        self,
        binding: SourceBinding,
        entry: CatalogEntry,
        *,
        content: bytes,
    ) -> None:
        source = self.resolve_entry(binding, entry.relative_path)
        current = source.stat(follow_symlinks=False)
        if _revision(current) != entry.content_revision:
            raise SourceAccessError(
                "The source changed after review; reconcile it again."
            )
        temporary = source.with_name(f".{source.name}.hussh-tmp-{uuid.uuid4().hex}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            latest = source.stat(follow_symlinks=False)
            if _revision(latest) != entry.content_revision:
                raise SourceAccessError(
                    "The source changed while the overwrite was prepared."
                )
            os.replace(temporary, source)
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()

    def move_entry(
        self,
        source_binding: SourceBinding,
        entry: CatalogEntry,
        *,
        destination_binding: SourceBinding,
        destination_relative_path: str,
    ) -> None:
        if source_binding.source_id != destination_binding.source_id:
            raise SourceAccessError(
                "Cross-root moves are not supported by the mounted adapter."
            )
        source = self.resolve_entry(source_binding, entry.relative_path)
        current = source.stat(follow_symlinks=False)
        if _revision(current) != entry.content_revision:
            raise SourceAccessError(
                "The source changed after review; reconcile it again."
            )
        destination = self.resolve_destination(
            destination_binding, destination_relative_path, create_parents=True
        )
        if destination.exists():
            raise SourceAccessError("The move destination already exists.")
        # A normal rename replaces a destination created in the check/use
        # window. Link-then-unlink gives regular-file moves a fail-closed,
        # no-clobber publication step on the same mounted filesystem.
        try:
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise SourceAccessError(
                "The move destination was created concurrently."
            ) from exc
        except OSError as exc:
            raise SourceAccessError(
                "The file could not be linked at its destination."
            ) from exc
        try:
            linked = destination.stat(follow_symlinks=False)
            latest = source.stat(follow_symlinks=False)
            if (
                _revision(linked) != entry.content_revision
                or _revision(latest) != entry.content_revision
            ):
                raise SourceAccessError(
                    "The source changed while the move was prepared."
                )
            source.unlink()
        except Exception:
            try:
                destination.unlink()
            except OSError:
                pass
            raise

    def copy_entry(
        self,
        source_binding: SourceBinding,
        entry: CatalogEntry,
        *,
        destination_binding: SourceBinding,
        destination_relative_path: str,
        max_bytes: int,
    ) -> None:
        if entry.size_bytes > max_bytes:
            raise SourceAccessError("The source exceeds the bounded share-copy limit.")
        destination = self.resolve_destination(
            destination_binding, destination_relative_path, create_parents=True
        )
        if destination.exists():
            raise SourceAccessError("The share destination already exists.")
        snapshot = self.read_snapshot(source_binding, entry, max_bytes=max_bytes)
        temporary = destination.with_name(
            f".{destination.name}.hussh-tmp-{uuid.uuid4().hex}"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as writer:
                writer.write(snapshot)
                writer.flush()
                os.fsync(writer.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as exc:
                raise SourceAccessError(
                    "The destination was created concurrently."
                ) from exc
            temporary.unlink()
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()

    def trash_entry(self, binding: SourceBinding, entry: CatalogEntry) -> None:
        source = self.resolve_entry(binding, entry.relative_path)
        current = source.stat(follow_symlinks=False)
        if _revision(current) != entry.content_revision:
            raise SourceAccessError(
                "The source changed after review; reconcile it again."
            )
        trash = Path("/usr/bin/trash")
        if not trash.is_file():
            raise SourceAccessError(
                "The operating-system Trash service is unavailable."
            )
        completed = subprocess.run(
            [str(trash), "--stopOnError", str(source)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise SourceAccessError("The file could not be moved to Trash.")

    def read_snapshot(
        self,
        binding: SourceBinding,
        entry: CatalogEntry,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read through a no-follow descriptor walk rooted at the binding."""
        pure = PurePosixPath(entry.relative_path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise SourceAccessError("The catalog path is invalid.")
        root = self.validate(binding)
        directory_flags = (
            os.O_RDONLY
            | int(getattr(os, "O_DIRECTORY", 0))
            | int(getattr(os, "O_NOFOLLOW", 0))
        )
        file_flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        directory_fd = os.open(root, directory_flags)
        try:
            opened_root = os.fstat(directory_fd)
            if (int(opened_root.st_dev), int(opened_root.st_ino)) != (
                binding.root_device,
                binding.root_inode,
            ):
                raise SourceAccessError(
                    "The bound source root was replaced; bind it again."
                )
            for part in pure.parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(pure.parts[-1], file_flags, dir_fd=directory_fd)
            try:
                before = os.fstat(file_fd)
                expected = (
                    entry.device,
                    entry.inode,
                    entry.size_bytes,
                    entry.modified_ns,
                )
                observed = (
                    int(before.st_dev),
                    int(before.st_ino),
                    int(before.st_size),
                    int(before.st_mtime_ns),
                )
                if observed != expected:
                    raise SourceAccessError(
                        "The source changed after scanning; scan it again."
                    )
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    chunk = os.read(file_fd, min(remaining, 1024 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                after = os.fstat(file_fd)
                if (int(after.st_size), int(after.st_mtime_ns)) != (
                    entry.size_bytes,
                    entry.modified_ns,
                ):
                    raise SourceAccessError(
                        "The source changed while being read; scan it again."
                    )
                data = b"".join(chunks)
                if len(data) > max_bytes:
                    raise SourceAccessError(
                        "The source exceeds the bounded-read size limit."
                    )
                return data
            finally:
                os.close(file_fd)
        except OSError as exc:
            raise SourceAccessError(
                "The source entry is unavailable or was replaced by a symbolic link."
            ) from exc
        finally:
            os.close(directory_fd)

    def enumerate_snapshot(
        self, binding: SourceBinding, limits: ScanLimits
    ) -> tuple[list[CatalogEntry], bool, str | None]:
        limits.validate()
        root = self.validate(binding)
        deadline = time.monotonic() + limits.max_seconds
        directory_flags = (
            os.O_RDONLY
            | int(getattr(os, "O_DIRECTORY", 0))
            | int(getattr(os, "O_NOFOLLOW", 0))
        )
        try:
            root_fd = os.open(root, directory_flags)
            opened_root = os.fstat(root_fd)
        except OSError as exc:
            raise SourceAccessError("The bound source root is unavailable.") from exc
        if (int(opened_root.st_dev), int(opened_root.st_ino)) != (
            binding.root_device,
            binding.root_inode,
        ):
            os.close(root_fd)
            raise SourceAccessError(
                "The bound source root was replaced; bind it again."
            )
        # Pinned, no-follow directory descriptors keep every recursive hop
        # inside the originally opened root even if a queued path is replaced.
        stack: list[tuple[int, PurePosixPath, int]] = [(root_fd, PurePosixPath(), 0)]
        emitted = 0
        entries: list[CatalogEntry] = []
        stop_reason: str | None = None
        try:
            while stack and emitted < limits.max_entries:
                if time.monotonic() > deadline:
                    stop_reason = "time_limit"
                    break
                directory_fd, relative_dir, depth = stack.pop()
                try:
                    children = sorted(
                        os.scandir(directory_fd), key=lambda item: item.name.casefold()
                    )
                except OSError:
                    stop_reason = stop_reason or "unavailable_subtree"
                    os.close(directory_fd)
                    continue
                pending_dirs: list[tuple[int, PurePosixPath, int]] = []
                try:
                    for child in children:
                        if emitted >= limits.max_entries:
                            stop_reason = stop_reason or "entry_limit"
                            break
                        if time.monotonic() > deadline:
                            stop_reason = stop_reason or "time_limit"
                            break
                        relative = relative_dir / child.name
                        try:
                            st = child.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if stat.S_ISLNK(st.st_mode):
                            continue
                        if stat.S_ISDIR(st.st_mode):
                            if depth < limits.max_depth:
                                try:
                                    child_fd = os.open(
                                        child.name,
                                        directory_flags,
                                        dir_fd=directory_fd,
                                    )
                                except OSError:
                                    continue
                                opened = os.fstat(child_fd)
                                if not stat.S_ISDIR(opened.st_mode) or (
                                    int(opened.st_dev),
                                    int(opened.st_ino),
                                ) != (int(st.st_dev), int(st.st_ino)):
                                    os.close(child_fd)
                                    continue
                                pending_dirs.append((child_fd, relative, depth + 1))
                            else:
                                # A bounded-depth scan is not a complete mirror;
                                # unseen descendants must never be tombstoned.
                                stop_reason = stop_reason or "depth_limit"
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        relative_text = relative.as_posix()
                        state, media_kind = _classify(Path(child.name), st)
                        entries.append(
                            CatalogEntry(
                                entry_id=_entry_id(binding.source_id, relative_text),
                                source_id=binding.source_id,
                                relative_path=relative_text,
                                display_name=child.name,
                                suffix=Path(child.name).suffix.lower(),
                                size_bytes=int(st.st_size),
                                modified_ns=int(st.st_mtime_ns),
                                device=int(st.st_dev),
                                inode=int(st.st_ino),
                                state=state,  # type: ignore[arg-type]
                                media_kind=media_kind,
                                content_revision=_revision(st),
                            )
                        )
                        emitted += 1
                finally:
                    os.close(directory_fd)
                stack.extend(reversed(pending_dirs))
        finally:
            for directory_fd, _relative_dir, _depth in stack:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
        if emitted >= limits.max_entries:
            stop_reason = stop_reason or "entry_limit"
        complete = not stack and stop_reason is None
        return entries, complete, stop_reason

    def enumerate(
        self, binding: SourceBinding, limits: ScanLimits
    ) -> Iterable[CatalogEntry]:
        entries, _complete, _stop_reason = self.enumerate_snapshot(binding, limits)
        return iter(entries)
