# Running in the background and in Docker

Autostart per user on Linux, macOS and Windows, and an optional hardened container.

[← Back to the README](../README.md)

---

## Run in the background

Each script installs a per-user autostart (no admin/root), bound to localhost, and has a `--remove` / `-Remove` option. The scripts live in the repository and run Trioditis from its folder, so they need the clone install ([README, step 2](../README.md#quick-start)), not `pipx`.

| OS | Command | Logs |
|---|---|---|
| Linux | `./deploy/linux/install-service.sh` | `journalctl --user -u trioditis -f` |
| macOS | `./deploy/macos/install-service.sh` | `logs/server.log` |
| Windows | `powershell -ExecutionPolicy Bypass -File deploy\windows\install-task.ps1` | `logs\server.log` |

## Docker (optional)

Running natively is simpler and keeps Ollama on localhost. If you want the container:

```bash
cp config.example.yaml config.yaml   # set the ollama base_url to http://host.docker.internal:11434/v1
cp .env.example .env                 # TRIODITIS_API_KEY is required inside a container
docker compose up -d --build
```

- **Data:** `metrics.db`, `budget.json`, `prices.json` and the rest are in `logs/trioditis/` (`TRIODITIS_HOME=/app/logs/trioditis` in the image). A volume from an image before 0.9.1 has them in `logs/hlidskjalv/`: the first start moves them and leaves a relative link at each old path, which also works from the host.
- **macOS / Windows (Docker Desktop):** `host.docker.internal` reaches Ollama on the host; nothing else to change.
- **Linux:** the container can only reach Ollama if Ollama also listens on the Docker bridge. Use `OLLAMA_HOST=172.17.0.1:11434`, **not** `0.0.0.0`, which would expose your models to the whole network.

The container runs as a non-root user on a read-only filesystem with all capabilities dropped, and is published on `127.0.0.1` only.

## Upgrading from 0.9.0 or earlier

Up to 0.9.0, Trioditis kept its files in `~/.hlidskjalv`, a folder it shared with the dashboard now called Lunete. From 0.9.1 it has its own folder, `~/.trioditis` (`TRIODITIS_HOME` moves it). Until the files move, Trioditis keeps reading the old folder, so the upgrade never starts from an empty one.

1. Upgrade: `pipx upgrade trioditis`, or in a clone `git pull` and the `pip install --require-hashes` line from the README.
2. See what moves: `trioditis migrate --dry-run`. Only Trioditis's own files move: `metrics.db`, `budget.json`, `prices.json` (and its `.bak` copies), `hardware.json`, `accounts.json`, `plan-salt`, the refit files and `trials/`. Lunete's files stay where they are.
3. Restart the service (Linux: `systemctl --user restart trioditis`): `serve` makes the move when it starts. To move by hand, stop Trioditis first, then run `trioditis migrate`. On Linux it refuses while another process still has `metrics.db` open for writing; on macOS it cannot tell, so stopping first matters there.
4. Each old path is now a link to the new file, so Lunete and anything else that reads the old paths keep working. Edit `budget.json` and `prices.json` in `~/.trioditis` from now on: an editor that saves by replacing the file would cut the link at the old path, and `trioditis check` would report both copies.

- **`HLIDSKJALV_HOME` set:** it is still read, with a one-line warning. Rename it to `TRIODITIS_HOME` and keep the folder. To move to a new folder instead, set `TRIODITIS_HOME` to it while `HLIDSKJALV_HOME` is still set, and `migrate` (or the next `serve`) moves the files between the two.
- **Docker:** nothing to do; the first start moves `logs/hlidskjalv/` into `logs/trioditis/`.
- **Windows:** nothing moves yet, because links there need extra rights. Trioditis keeps reading `%USERPROFILE%\.hlidskjalv`, and `trioditis check` says so.
