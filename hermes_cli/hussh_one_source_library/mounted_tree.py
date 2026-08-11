# SPDX-FileCopyrightText: 2026 Hushh Labs
# SPDX-License-Identifier: Apache-2.0

"""Read-only mounted-tree adapter for explicitly approved cloud roots."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Iterable

from .contracts import CatalogEntry, ScanLimits, SourceBinding


class SourceAccessError(RuntimeError):
    pass


_TEXT_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".json", ".csv"})
_DOCUMENT_SUFFIXES = frozenset({".docx", ".xlsx", ".ipynb"})
_METADATA_SUFFIXES = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".tiff",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
})
_PLACEHOLDER_SUFFIXES = frozenset({".gdoc", ".gsheet", ".gslides", ".gdraw"})


def _entry_id(source_id: str, relative_path: str) -> str:
    digest = hashlib.sha256(f"{source_id}\x00{relative_path}".encode()).hexdigest()
    return f"src_entry_{digest[:24]}"


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
    source_kinds = frozenset({"icloud_drive", "google_drive"})

    def bind(self, *, source_id: str, source_kind: str, label: str, root: Path, created_at: str) -> SourceBinding:
        if source_kind not in self.source_kinds:
            raise SourceAccessError("Only mounted iCloud Drive and Google Drive roots are supported.")
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
        return SourceBinding(
            source_id=source_id,
            source_kind=source_kind,  # type: ignore[arg-type]
            label=normalized_label,
            root_locator=str(resolved),
            root_device=int(st.st_dev),
            root_inode=int(st.st_ino),
            created_at=created_at,
        )

    def validate(self, binding: SourceBinding) -> Path:
        root = Path(binding.root_locator)
        try:
            root_lstat = root.lstat()
            if stat.S_ISLNK(root_lstat.st_mode):
                raise SourceAccessError("The source root was replaced by a symbolic link.")
            resolved = root.resolve(strict=True)
            st = resolved.stat()
        except SourceAccessError:
            raise
        except OSError as exc:
            raise SourceAccessError("The bound source root is unavailable.") from exc
        if resolved != root or not stat.S_ISDIR(st.st_mode):
            raise SourceAccessError("The bound source root identity is no longer valid.")
        if (int(st.st_dev), int(st.st_ino)) != (
            binding.root_device,
            binding.root_inode,
        ):
            raise SourceAccessError("The bound source root was replaced; bind it again.")
        return root

    def resolve_entry(self, binding: SourceBinding, relative_path: str) -> Path:
        pure = PurePosixPath(relative_path)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise SourceAccessError("The catalog path is invalid.")
        root = self.validate(binding)
        candidate = root.joinpath(*pure.parts)
        try:
            if candidate.is_symlink():
                raise SourceAccessError("Symbolic links are not readable source entries.")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except SourceAccessError:
            raise
        except (OSError, ValueError) as exc:
            raise SourceAccessError("The source entry is unavailable or escaped its root.") from exc
        return resolved

    def read_snapshot(
        self,
        binding: SourceBinding,
        entry: CatalogEntry,
        *,
        max_bytes: int,
    ) -> bytes:
        """Read through a no-follow descriptor walk rooted at the binding."""
        pure = PurePosixPath(entry.relative_path)
        if pure.is_absolute() or not pure.parts or any(
            part in {"", ".", ".."} for part in pure.parts
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
                raise SourceAccessError("The bound source root was replaced; bind it again.")
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
                if (
                    int(after.st_size), int(after.st_mtime_ns)
                ) != (entry.size_bytes, entry.modified_ns):
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

    def enumerate(self, binding: SourceBinding, limits: ScanLimits) -> Iterable[CatalogEntry]:
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
            raise SourceAccessError("The bound source root was replaced; bind it again.")
        # Pinned, no-follow directory descriptors keep every recursive hop
        # inside the originally opened root even if a queued path is replaced.
        stack: list[tuple[int, PurePosixPath, int]] = [(root_fd, PurePosixPath(), 0)]
        emitted = 0
        try:
            while stack and emitted < limits.max_entries:
                if time.monotonic() > deadline:
                    break
                directory_fd, relative_dir, depth = stack.pop()
                try:
                    children = sorted(
                        os.scandir(directory_fd), key=lambda item: item.name.casefold()
                    )
                except OSError:
                    os.close(directory_fd)
                    continue
                pending_dirs: list[tuple[int, PurePosixPath, int]] = []
                try:
                    for child in children:
                        if emitted >= limits.max_entries or time.monotonic() > deadline:
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
                                    int(opened.st_dev), int(opened.st_ino)
                                ) != (int(st.st_dev), int(st.st_ino)):
                                    os.close(child_fd)
                                    continue
                                pending_dirs.append((child_fd, relative, depth + 1))
                            continue
                        if not stat.S_ISREG(st.st_mode):
                            continue
                        relative_text = relative.as_posix()
                        state, media_kind = _classify(Path(child.name), st)
                        yield CatalogEntry(
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
