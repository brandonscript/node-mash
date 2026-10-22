# node-mash

Free up disk space taken up by duplicate dependencies and projects you haven't touched in a while.

node-mash looks through your project folders recursively for `node_modules`, Python virtual environments, and `__pycache__`. Identical files can share disk space using APFS clones, so updating a package in one project won't change another.

## Install

Homebrew is the recommended way to install node-mash on macOS:

```sh
brew install brandonscript/tap/node-mash
```

Installation doesn't run cleanup or start a background service.

## Get started

See how much you could save:

```sh
node-mash dedupe ~/Dev
```

Everything is a preview until you add `--apply`. When you're ready:

```sh
node-mash dedupe ~/Dev --apply
```

Files are checked after each replacement. If verification fails, node-mash stops before moving to the next file. Run it while package installs and builds are stopped.

## Clean up old projects

Still have dependencies sitting in that project from six months ago? Preview removing them:

```sh
node-mash purge ~/Dev --days 180
```

Add `--apply` to delete those dependency folders. Your source files and lockfiles stay put, so you can reinstall dependencies when you come back. Deletion is permanent; only purge dependencies you can reinstall.

Project activity comes from file modification dates, ignoring dependencies, caches, and things like `.DS_Store`. Reading a project doesn't count as modifying it.

## Reports

Reports include estimated space savings and are saved to `~/.local/state/node-mash/reports`. Choose another folder with `--report-dir`:

```sh
node-mash dedupe ~/Dev --report-dir ~/Documents/node-mash-reports
```

Savings are estimates; files may already share APFS storage. Reports contain local file paths, so check them before sharing.

## Run on a schedule

Use Homebrew to run a preview every day at 4 a.m.:

```sh
mkdir -p ~/.config/node-mash
cp "$(brew --prefix node-mash)/share/node-mash/service.json" ~/.config/node-mash/service.json
brew services start node-mash
```

Edit `~/.config/node-mash/service.json` to choose your project folders and report location. Set `"apply": true` to enable deduplication, and `"purge": true` to also remove stale dependencies. Stop with `brew services stop node-mash`.

See the [scheduling guide](docs/scheduling.md) for configuration and running without Homebrew.

## More details

- [Commands, reports, and source installation](docs/usage.md)
- [Run on a schedule](docs/scheduling.md)
- [How deduplication and verification work](docs/technical-details.md)

## License

[MIT](LICENSE).
