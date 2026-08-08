# Local SearXNG operations

This directory contains the rebuild assets for the project's trusted, local
web-discovery service. The live deployment is SearXNG in Docker Engine inside
the `Ubuntu-24.04` WSL distribution; it is not Docker Desktop.

## Architecture

`python agent.py research` on Windows calls
`http://127.0.0.1:8888/search?format=json`. Windows forwards that loopback-only
port to container port `8080`:

- container: `canvas-searxng`
- image: `docker.io/searxng/searxng:latest`
- restart policy: `unless-stopped`
- settings: `/opt/canvas-obsidian-search/config` -> `/etc/searxng`
- SearXNG data: `/opt/canvas-obsidian-search/data` -> `/var/cache/searxng`
- normalized application cache:
  `C:\Users\kyrit\AppData\Local\canvas-obsidian-sync\research-cache`

The files here have narrow roles:

- `wsl.conf` enables systemd and selects the local WSL user.
- `docker.sources` is the Docker Engine apt source for Ubuntu Noble/amd64.
- `settings.yml.template` enables safe search and both HTML and JSON output.
- `configure-secret.sh` replaces the settings placeholder exactly once without
  printing the generated secret.
- `configure-project-env.ps1` updates only the four research keys in an
  existing `.env` file.
- `start.ps1` launches a hidden, recorded WSL keeper and waits for Docker,
  `canvas-searxng`, and the loopback HTTP endpoint.
- `status.ps1` checks the keeper identity and HTTP health without waking WSL.
- `stop.ps1` stops only `canvas-searxng`, then its validated keeper process.
- `verify.ps1` drives the real project adapter and asserts live, zero-cost,
  zero-model, HTTPS-only results.

The generated `settings.yml`, its secret, `.env`, and both caches stay outside
version control. The checked-in WSL and apt files are specific to this machine's
current user, Ubuntu release, and architecture; review them before reuse
elsewhere.

To wire an existing deployment into this checkout, run from the repository in
PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\local-search\configure-project-env.ps1 -EnvPath .\.env
```

## Lifecycle

WSL systemd services do not keep a distribution awake on their own. Use the
repository scripts from Windows PowerShell; a one-shot `wsl ... docker start`
will let Ubuntu idle off again after the command exits.

Start or ensure the service is ready:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\start.ps1
```

The starter is idempotent. It records the keeper PID and process start time in
`%LOCALAPPDATA%\canvas-obsidian-sync\local-search-keeper.json`, so later commands
can reject stale or reused PIDs. Check health without waking a stopped distro:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\status.ps1
```

Stop or restart only this service and its keeper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\stop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\start.ps1
```

For diagnosis after `start.ps1` fails, inspect only the named container:

```powershell
wsl.exe -d Ubuntu-24.04 -- docker ps --all --filter "name=^canvas-searxng$"
wsl.exe -d Ubuntu-24.04 -- docker port canvas-searxng
wsl.exe -d Ubuntu-24.04 -- docker logs --tail 100 canvas-searxng
```

`wsl.exe --terminate Ubuntu-24.04` stops the whole distribution, including any
unrelated work in it, and is not a normal local-search lifecycle command.

## Update or recreate

Pulling `latest` does not replace the running container. Enter the distribution,
pull the image, then recreate the container with the existing bind-mounted
configuration and data:

```powershell
wsl.exe -d Ubuntu-24.04
```

```bash
docker pull docker.io/searxng/searxng:latest
docker stop canvas-searxng
docker rm canvas-searxng
docker run --detach \
  --name canvas-searxng \
  --restart unless-stopped \
  --publish 127.0.0.1:8888:8080/tcp \
  --volume /opt/canvas-obsidian-search/config:/etc/searxng \
  --volume /opt/canvas-obsidian-search/data:/var/cache/searxng \
  docker.io/searxng/searxng:latest
exit
```

Removing the container does not remove either bind-mounted directory. Run
`start.ps1`, `status.ps1`, and `verify.ps1` after every update.

## Removal

Remove the service while retaining its settings and data:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\stop.ps1
wsl.exe -d Ubuntu-24.04 -- docker rm --force canvas-searxng
```

Then remove `RESEARCH_PROVIDER`, `SEARXNG_BASE_URL`, `RESEARCH_CACHE_PATH`, and
`RESEARCH_COST_PER_REQUEST_USD` from `.env` to disable live research. The image
can be removed separately with:

```powershell
wsl.exe -d Ubuntu-24.04 -- docker image rm docker.io/searxng/searxng:latest
```

For a complete, irreversible purge, first verify the exact paths, then delete
`/opt/canvas-obsidian-search/config`, `/opt/canvas-obsidian-search/data`, and the
Windows normalized-result cache. Do not purge the config if rollback or
recreation may be needed.

## Privacy and cost contract

- The published port is bound to `127.0.0.1`, not all interfaces, and the
  settings declare `public_instance: false`.
- The research command is read-only: it does not invoke a model, fetch result
  pages, or write the vault. It validates and bounds SearXNG's result metadata.
- A local SearXNG instance is not an offline search engine. The explicit query
  is sent to configured upstream engines, which can observe it. Use compact
  public topic terms, never assignment text, credentials, or personal data.
- The application cache contains normalized public titles, HTTPS URLs, and
  snippets, not raw queries, headers, credentials, or provider payloads. Treat
  it as sensitive because result text can echo query terms. SearXNG also keeps
  runtime data in its WSL bind mount.
- Research uses zero model attempts and zero input/output tokens. With this
  self-hosted service, `.env` declares the direct request cost as `$0`; a live
  cache miss records one provider request, while a valid application-cache hit
  records zero. Local compute, bandwidth, and power are outside that telemetry.

## JSON HTTP 403

The CLI always requests `format=json`. If the HTML page works but the JSON URL
or CLI returns HTTP 403, JSON is usually absent from SearXNG's effective
`search.formats` list.

Inspect the mounted settings without displaying the server secret:

```powershell
wsl.exe -d Ubuntu-24.04 -- docker exec canvas-searxng awk '/^search:/{show=1} /^server:/{show=0} show{print}' /etc/searxng/settings.yml
```

It must include:

```yaml
search:
  safe_search: 1
  formats:
    - html
    - json
```

Edit `/opt/canvas-obsidian-search/config/settings.yml` in WSL if necessary,
preserving its generated `server.secret_key`; do not overwrite the live file
with the unresolved template. Restart the container, retry the JSON URL, and
then inspect the mount and logs if it still fails:

```powershell
wsl.exe -d Ubuntu-24.04 -- docker restart canvas-searxng
wsl.exe -d Ubuntu-24.04 -- docker inspect canvas-searxng --format "{{json .Mounts}}"
wsl.exe -d Ubuntu-24.04 -- docker logs --tail 100 canvas-searxng
Get-Content .env | Select-String '^RESEARCH_PROVIDER=|^SEARXNG_BASE_URL='
```

If Docker itself is unavailable, check `systemctl is-active docker` inside
`Ubuntu-24.04`; `wsl.conf` requires systemd, and a change to it takes effect only
after terminating and restarting the distribution.

## Verification

From the repository:

```powershell
# Deterministic replay: no network, cache, provider request, or model tokens.
python agent.py research "binary tree traversal" --response-file tests/fixtures/research_results.json --no-cache --pretty

# Start and verify the real project adapter. The verifier requires one live
# provider request, configured cost $0, no model attempt/tokens, and 1-3 HTTPS hits.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\start.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\verify.ps1

# Cache acceptance: first call is live; the identical second call is source=cache
# with provider_requests=0 and estimated_cost_usd=0.0.
python agent.py research "binary tree traversal cache canary" --refresh --pretty
python agent.py research "binary tree traversal cache canary" --pretty

python -m unittest tests.test_research -v
```

The accepted reversible lifecycle test is:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\stop.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\status.ps1  # expected unhealthy/exit 1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\start.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\local-search\verify.ps1
```

On July 26, 2026, this sequence passed end to end. The final canary returned
three normalized HTTPS results with one live provider request, configured cost
`$0`, no model attempt, and zero input/output tokens. An identical repeat was a
cache hit with zero provider requests. The bind remained
`127.0.0.1:8888->8080/tcp`.
