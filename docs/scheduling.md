# Scheduling

[Back to the README](../README.md)

## Homebrew services

Homebrew can register a daily 04:00 job for your logged-in user:

```sh
mkdir -p ~/.config/node-mash
cp "$(brew --prefix node-mash)/share/node-mash/service.json" ~/.config/node-mash/service.json
brew services start node-mash
```

Edit the copied configuration before enabling cleanup. Installation never creates or overwrites your private configuration. Starting the service registers its schedule; it does not immediately run cleanup. Run without `sudo` so paths and permissions belong to your user.

```json
{
  "roots": ["~/Dev"],
  "days": 180,
  "report_dir": "~/.local/state/node-mash/reports",
  "apply": false,
  "purge": false
}
```

`apply: false` previews all changes. Set `apply: true` to enable deduplication; service purging additionally needs `purge: true`. `method` defaults to `clone`; `store` and `min_bytes` are also configurable. Unknown keys and invalid values abort before cleanup. Configuration is read fresh each run, so changing roots or cleanup settings does not require restarting the job. Keep your configuration private: paths and timestamps appear in reports and logs.

To preview the same configuration now:

```sh
node-mash service --once --config ~/.config/node-mash/service.json --dry-run
```

Explicit CLI options override configuration. CLI roots replace configured roots. `--dry-run` forces preview mode even when the configuration enables cleanup. A missing configuration or absent roots is an error; the service never silently chooses a scan directory.

```sh
brew services info node-mash
brew services stop node-mash
```

The formula uses `node-mash service --once` on a calendar schedule. Reports and per-run logs go to your configured `report_dir`; Homebrew captures stdout/stderr in `$(brew --prefix)/var/log/node-mash.log` and `node-mash.error.log`. The 04:00 time follows the Mac's timezone. A run missed during sleep normally runs on wake; the user must be logged in. A failed run exits and the next scheduled day remains enabled.

If migrating from a manually installed LaunchAgent, unload that job before starting Homebrew's service so both cannot run together. For the example label:

```sh
launchctl bootout "gui/$(id -u)/com.node-mash"
```

Do not use that command unless the old job exists. Homebrew manages a separate service label; use `brew services info node-mash` to inspect it. The formula does not automatically enable cleanup, register a service, or modify a pre-existing LaunchAgent.

## Foreground service

Without Homebrew, run a preview cycle every 24 hours from the time you start it:

```sh
node-mash service ~/Dev --interval 86400
```

Stop with Ctrl-C. Add `--apply` to enable deduplication; automatic purging additionally requires `--purge`. `--once` runs one cycle. You can also use `--config` with source installations.

## Service installation on macOS

[examples/com.node-mash.plist](../examples/com.node-mash.plist) runs a single preview daily at 04:00 local system time with a 180-day stale threshold. Replace all `/ABSOLUTE/PATH/TO/...` placeholders with your Python executable, saved `node_mash.py`, project root, and an existing log directory. Launchd does not expand `~` or shell variables in these fields. Calendar scheduling follows the Mac's timezone; a run missed during sleep normally runs on wake. This is a per-user LaunchAgent, so it requires a logged-in session.

```sh
mkdir -p ~/Library/LaunchAgents
cp examples/com.node-mash.plist ~/Library/LaunchAgents/com.node-mash.plist
# Edit the installed plist and replace every placeholder before loading it.
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.node-mash.plist
```

Add `--apply` to its argument array to enable dedupe writes; automatic deletion additionally requires `--purge`. After editing a loaded job, unload with `launchctl bootout "gui/$(id -u)/com.node-mash"` and bootstrap again. Keep the Python and script paths available.
