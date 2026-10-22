# Commands and reports

[Back to the README](../README.md)

## Usage

```sh
node-mash scan ~/Dev
node-mash dedupe ~/Dev                         # Preview only
node-mash dedupe ~/Dev --apply                 # Independent copy-on-write clones
node-mash purge ~/Dev --days 180                # Preview only
node-mash purge ~/Dev --days 180 --apply        # Permanently delete stale dependencies
node-mash service ~/Dev --interval 86400        # Periodic preview
```

All writes require `--apply`. Service mode deduplicates; automatic deletion additionally requires `--purge`. JSON reports show planned and applied actions plus a `summary` containing estimated dedupe, purge, and combined savings in bytes. Combined savings account for overlap by estimating dedupe only among retained projects. These are logical byte estimates; filesystem cloning and compression can make actual reclaimed space smaller. Stop foreground service with Ctrl-C. Scope roots to directories you own, and run only one instance at a time while package managers and builds are stopped.

Standalone `__pycache__` directories are also discovered for deduplication, using the same content checks and minimum file size. Caches inside a discovered virtual environment are covered by its scan. Standalone caches are excluded from age-based purging and activity timestamps. Use `--min-bytes 0` to include small bytecode files.

## Reports

Every CLI or service cycle saves a uniquely named JSON report and progress log in `~/.local/state/node-mash/reports` by default, outside the checkout. JSON output is also printed to stdout. Choose another location with:

```sh
node-mash dedupe ~/Dev --report-dir ~/Documents/node-mash-reports
```

Reports use UTC timestamps plus a random suffix; existing reports are not overwritten. Files are created with owner-only permissions, and new report directories use owner-only access. Aborted runs save an error and completed-operation events alongside the progress log. Reports survive a normal error or interruption; a forced kill or power loss may leave only the progress journal. Logs are flushed after each event. Report-writing failure stops the run. No automatic retention cleanup is performed.

Keep reports outside dependency directories; these locations are rejected. A report directory inside a scan root is excluded from discovery and project activity timestamps. Add `--report-dir` and its absolute path to the launchd argument array to customize scheduled reports.

## Privacy

node-mash does not send telemetry or make network requests. Reports and progress logs contain absolute file paths and project activity timestamps; keep them private and review them before sharing. Local reports, logs, environment files, and cleanup history are excluded from Git by default.

## Install from source

Python 3.11+ is required. From a source checkout:

```sh
python3 -m pip install .
```

For local development, use a virtual environment and install with `python3 -m pip install -e .`.

## Verification

```sh
python3 -m unittest discover -s tests -v
```
