# Google Drive & macOS CloudStorage FileProvider Pitfalls

This reference documents verified technical behaviors and workarounds when operating the Hussh One PKM bridge and Source Library Steward over macOS CloudStorage (`/Users/.../Library/CloudStorage/GoogleDrive-*`) mounts.

## 1. Hard Links (`os.link`) on Virtual Cloud Mounts
- **Symptom:** `os.link(source, destination)` raises `OSError` ("The file could not be linked at its destination").
- **Cause:** Virtual FileProvider / CloudStorage filesystems (Google Drive, OneDrive, iCloud Drive) do not support POSIX hard links.
- **Fix:** Fall back to atomic `shutil.move()` or `os.rename()` when `os.link()` fails with `OSError` on mounted cloud roots.

## 2. Symlinked Mount Paths (`/Users/.../CloudStorage` vs `/Volumes/...`)
- **Symptom:** `relative_to(root)` raises `ValueError` ("destination escaped its source root").
- **Cause:** macOS resolves `/Users/.../Library/CloudStorage/GoogleDrive-*` through symlinks to `/Volumes/GoogleDrive-*`. Comparing a strictly resolved candidate path (`parent.resolve(strict=True)`) against an un-resolved `root` fails due to path prefix mismatches.
- **Fix:** Resolve both `root` (`root.resolve(strict=True)`) and candidates before evaluating `relative_to()`, checking both resolved and raw root paths.

## 3. Google Drive Shortcuts (`.shortcut-targets-by-id`)
- **Symptom:** Shortcuts and shared folders fail entry resolution or scanning with `SourceAccessError`.
- **Cause:** Google Drive FileProvider renders shared folder shortcuts as symlinks pointing to `.shortcut-targets-by-id/...` located under `root.parent` (`GoogleDrive-account@domain`).
- **Fix:** When checking `relative_to()` on resolved entries, verify `resolved.relative_to(root.parent)` for shortcut entries that resolve to target paths inside the account root. Use `follow_symlinks=True` when inspecting directory entries to properly traverse shortcut directories.

## 4. Keychain OSStatus `-34018` Missing Entitlement in CLI Subprocesses
- **Symptom:** `SecItemAdd` or `SecItemUpdate` with `kSecUseDataProtectionKeychain` raises `KeychainError` with `OSStatus -34018`.
- **Cause:** Standalone Python binaries running in non-interactive CLI contexts lack signed macOS Data Protection Keychain entitlements.
- **Fix:** In `hermes_cli/hussh_one_pkm/keychain.py`, catch `OSStatus -34018` and fall back to standard generic password Keychain storage (`self.set("up_" + account, secret)` / `self.get("up_" + account)`).

## 5. Multi-Account Source Library Scanning & Scaling
- **Symptom:** Sequential scanning across multiple mounted cloud drives (e.g., Work Google Drive with Shared Drives, Personal Google Drives, OneDrive, iCloud Drive) times out or blocks.
- **Cause:** Traversing deep virtual FileProvider trees across multiple cloud daemons sequentially can encounter IPC latency or massive directory trees.
- **Fix:** 
  1. Always enforce explicit per-source time and depth bounds via `ScanLimits(max_entries=..., max_depth=..., max_seconds=...)` (e.g., `max_seconds=5.0`, `max_depth=4`, `max_entries=500`).
  2. Scan sources individually rather than in a single unbounded blocking loop.
  3. Query indexed entries across all bound sources simultaneously via `SourceLibraryService.search(query=..., limit=...)`, which searches the sealed local SQLite index across all active `source_id` bindings in a single fast pass.

## 6. Dataless Placeholder Files & `Resource deadlock avoided` (Extracting File IDs via `xattr`)
- **Symptom:** Reading un-downloaded online-only `.gdoc` or `.gsheet` files directly with `cat` or `open()` throws `OSError: [Errno 11] Resource deadlock avoided`.
- **Cause:** macOS FileProvider marks cloud-only placeholders with dataless extent attributes. Opening them without materialization blocks the calling process on the FileProvider daemon.
- **Fix:** Extract metadata non-destructively without triggering a download or deadlock using extended attributes:
  ```bash
  xattr -p "com.google.drivefs.item-id" "/path/to/file.gdoc"
  ```
  The returned item ID resolves directly to the Google Drive web document: `https://docs.google.com/document/d/<ITEM_ID>/edit` or `https://docs.google.com/spreadsheets/d/<ITEM_ID>/edit`.

