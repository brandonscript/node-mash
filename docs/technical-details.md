# Technical details

[Back to the README](../README.md)

## Project activity

A project is the parent of a discovered `node_modules` or a Python virtual environment named `.venv`, `venv`, or `env` containing `pyvenv.cfg`. Discovery is recursive and does not follow directory symlinks or enter dependency roots. Python environments are handled as a whole; system Python installations and arbitrary `site-packages` are not purge targets.

Activity is the latest file modification time anywhere under the project, excluding dependencies, Git/Hg/SVN internals, Python/tool caches, symlinks, `.DS_Store`, `Thumbs.db`, and `desktop.ini`. Directory timestamps are ignored. No meaningful files means no purge. A read/stat failure aborts the command. Activity is rechecked before deletion; source files and lockfiles remain intact. A recent generated file conservatively keeps a project active. Read-only use does not update modification time, and Git checkout can refresh it: this measures modifications, not actual usage.

Purging permanently removes the dependency directory; reinstall with your package manager. Preview before applying. Do not purge hand-maintained or irreplaceable environment contents.

## Dedupe behavior

Files must have matching SHA-256 content, size, and permission mode; package version names are not trusted. Files below 4 KiB are skipped by default (`--min-bytes 0` includes them). Existing symlinks are skipped. Replacement uses an atomic rename, with contents rechecked before replacement.

`--method clone` (default) uses macOS `clonefile` to share data blocks while keeping independent files. In-place writes, replacements, and deletions affect only that project. Cloning must be supported on the same filesystem; unsupported operations are reported as skipped, with no fallback to links or full copies. Temporary clones are verified before an atomic rename. Target metadata is restored with macOS `copyfile` (stat, ACLs, and extended attributes), with ownership and modification time checked. Existing hardlinked files already sharing an inode are skipped; this is not an unlink/migration tool.

Scheduled previews use clone mode by default. Its file-size estimates remain potential savings, not measured reclaimed space: files might already share APFS blocks. Repeated runs can recreate clones even when no additional blocks can be saved.

`--method hardlink` (explicit opt-in) shares file inodes on the same filesystem; cross-filesystem candidates are reported as skipped. `--method symlink` copies one verified object into `~/.local/share/node-mash/objects`, then points duplicate files there. Keep this store permanently available; deleting it breaks links. Store garbage collection and unlink/materialize restoration are not yet implemented. The first original remains independent in symlink mode, so two copies are needed until further duplicates share the object.

The two opt-in link methods share writes: modifying one linked file can modify others. Use only for reinstallable dependencies that your tools treat as immutable. Native addons and tools that inspect real paths may behave differently with symlinks. Link modes do not preserve or compare ACLs, extended attributes, ownership, or timestamps across linked files. No mode provides a transaction spanning the entire run. Reports describe logical bytes shared, not measured physical disk savings.

## Verify after each operation

A narrow macOS exception permits addition, removal, or value changes of `com.apple.provenance` only: macOS may attach or replace this attribute during clone creation. Differences are logged as a `metadata_note`. This field is excluded only from target metadata equality; source metadata must remain unchanged. Quarantine, all other extended attributes, and ACLs remain strict.

Every applied clone is checked **after** the final atomic rename, before moving to another file. Checks cover source and target SHA-256 contents, independent regular-file identity, permissions, ownership, modification time, file flags, ACLs, and extended-attribute names and values. Inode, change time, creation time, and access time are not preservation checks: replacing a file necessarily changes some identity/time metadata.

Files or directories that disappear during dedupe scanning or before clone replacement are logged and skipped. This does not make simultaneous package installs, builds, or purging safe. Post-replacement disappearance always aborts.

Any verification mismatch or other read error aborts the entire run with a nonzero exit status; foreground service mode exits without retrying. There is no automatic rollback of an already replaced file. A failure can therefore leave the current file affected; earlier verified operations remain applied. Do not run package managers or builds concurrently. Each completed file emits a verification event immediately to stderr (the scheduled job's error log), preserving progress even when the final summary is not produced. The daily launchd job remains scheduled for subsequent days; a failed run is not a permanent disable switch.

Opt-in hardlink and symlink modes verify contents and link topology after replacement; they retain their documented shared-metadata behavior. Purges check that the deleted directory is absent before moving to another. Purge verification does not prove deleted dependencies were reinstallable or validate the rest of a project.

## Platform support

Copy-on-write clone mode requires macOS and a compatible filesystem such as APFS. Unsupported cloning is skipped without falling back to shared links. Scan, purge, and opt-in link modes use Python's filesystem APIs; clone metadata integration tests run only on macOS.
