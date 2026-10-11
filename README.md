# Disk Status Page

A single container on debian-nas collects disk power state, caches SMART
health, stores observed transitions in SQLite, and serves a home-network
dashboard. No third-party Python dependencies, frontend build, or database
server. Monitoring is passive by default; authenticated controls can wake
disks, put unmounted spares in standby, or request extended SMART self-tests.

## Monitoring behavior

- Checks power state every 30 seconds with the supplied Compose file using `hdparm -C`.
- Resolves configured serials using `/dev/disk/by-id/ata-*` links; never
  identifies a disk by its changing `sdX` name and never runs a discovery scan.
- Reads SMART at most every 30 minutes per awake disk. Explicit `-d ata`
  prevents device-type autodetection; `-n standby,3,5` skips a disk that fell
  asleep and fails closed if power-mode checking is unsupported. Set
  `smart_type` to `sat` in a disk's configuration only if hardware tests
  establish that the controller needs it.
- Sleeping disks retain timestamped SMART readings. Unknown power state
  prevents SMART reads. Overall health, warning attributes, counters,
  error logs and self-test logs appear in the UI/API.
- Records observed transitions, not exact hardware event times. Minute
  polling can miss short cycles. First observations, unknown states and
  long collection gaps do not establish transitions. History survives
  restarts; transitions are retained for one year (latest 100 shown).
- Reads kernel I/O counters without disk queries. These are cumulative
  counters and may reset after reboot; they do not identify a responsible app.
- Stores only the latest SMART data per disk, not historical SMART trends.
- Roles in `disks.json` describe intended usage, not actual mount status.
  The ongoing migration must finish before treating the spares as unused.

Hardware compatibility and non-waking behavior through the JMicron
controller remain to be validated on debian-nas. There is no fallback to
an unrestricted SMART query. NVMe health and notifications are not included.

## Local development

```bash
python3 -m unittest discover -s tests -v
DEMO=true DATA_DIR=./data PORT=8080 python3 app.py
```

Open http://localhost:8080. Demo mode queries no disks and clearly labels
sample values. Use a separate data directory from production.

Build and preview the image without host privileges or disk access:

```bash
docker build -t disk-status-page:local .
docker run --rm -p 8088:8080 -e DEMO=true \
  --tmpfs /data disk-status-page:local
```

## Publishing

The intended repository is `blakdav/disk-status-page`. GitHub Actions tests
pushes and pull requests. Successful pushes to the repository's actual
default branch automatically build and publish:

- `ghcr.io/blakdav/disk-status-page:latest`
- `ghcr.io/blakdav/disk-status-page:sha-<full-commit-sha>`

The workflow authenticates with `GITHUB_TOKEN`, with `contents: read` and
`packages: write`. No credentials belong in this repository. Manual workflow
runs publish only when selected on the default branch. The Dockerfile links
the package to the source repository using an OCI label.

After the first publish, set the GHCR package visibility to public to match
the other homelab images. A public repository does not itself guarantee a
public package. For a private package, log in on the NAS as the same user
that runs Compose, using a GitHub personal access token (classic) with
`read:packages` and access to the package:

```bash
read -rsp 'GHCR token: ' ghcr_token; echo
printf '%s' "$ghcr_token" | docker login ghcr.io \
  -u blakdav --password-stdin
unset ghcr_token
```

See [GitHub registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

## NAS deployment — after migration and hardware validation

Docker must first be installed using the homelab NAS playbook. Do not run
that full playbook until the disk migration is confirmed complete: its
existing storage tasks change pool membership and unmount spare disks.
This project does not install host packages or change host services.

Keep `docker-compose.yml` at `/opt/docker/disk-status-page/` on debian-nas.
Its SQLite bind mount is `/mnt/vault/apps/disk-status-page` on NVMe. Confirm
`/mnt/vault` is mounted before creating the directory or starting Compose.
Provision those directories through Ansible, following homelab policy.
The directory layout and compose placement are deployment prerequisites,
not actions this project has applied to the NAS.

On debian-nas, once those prerequisites and the image publication are done:

```bash
cd /opt/docker/disk-status-page
docker pull ghcr.io/blakdav/disk-status-page:latest
docker compose up -d
docker compose ps
docker compose logs --tail=50
```

To deploy a subsequent published update:

```bash
cd /opt/docker/disk-status-page
docker compose pull
docker compose up -d
```

The initial Compose file uses `privileged: true` and a read-only `/dev` bind
mount for controller compatibility and stable serial links. Privileged mode
still grants broad host access; the read-only bind is not a security boundary
against raw disk commands. Reduce permissions after testing actual hardware.
The app runs as root for disk queries; PUID/PGID are not supported.

Direct URL: http://172.23.24.16:8088. Configure Nginx Proxy Manager on the
Docker VM to forward the chosen hostname to `172.23.24.16:8088`, enabling
HTTPS there. No Docker network shared across hosts is needed. The dashboard
is readable without login. Actions require the generated NAS token. Keep
it on the trusted home network and use HTTPS through NPM for controls.

Optional disk configuration override:

```yaml
    environment:
      DISKS_FILE: /config/disks.json
    volumes:
      - ./disks.json:/config/disks.json:ro
```

Merge these entries into the existing service rather than replacing its
other environment or volume settings. Recreate the container after changing
the disk configuration.

`/api/status` exposes current values and recent history. `/healthz` reports
collector freshness, independently of whether individual disks are healthy
or accessible. An unhealthy container needs investigation; Docker health
checks alone do not automatically restart it.

## Host setup commands

The related homelab playbook changes remain local until explicitly committed
and pushed. Once published and the migration is confirmed complete, run on
the Ansible control node as root:

```bash
cd /root/homelab && git pull && cd ansible
ansible-playbook nas.yml
```

The playbook installs Docker, hdparm and smartmontools. It does not disable
smartd; host background monitoring remains separate from this container.

## Current validation

Thirty-six unit tests pass, covering sleeping/unknown states, standby races,
unsupported SMART power checks, health warning bits, persisted transitions,
observation gaps, and HTTP responses tested without sockets. Python compiles,
YAML parses, and `docker compose config --quiet` passes. This development
environment denies Docker daemon access and listening sockets, so the image
build, live HTTP/browser preview, and NAS hardware checks are still pending.
The initial version has been published and validated on the NAS. These new
controls have not yet been published or tested on physical disks.


## Disk controls and manual refresh

The header's **Refresh disks** button requests a new collector pass. It
checks all disk states and refreshes health data only on awake disks. It
never wakes sleeping disks. The normal polling interval remains independent
of the browser's polling for already-collected results.

Each disk has **Spin up**, **Spin down**, **Refresh health**, and
**Extended self-test** controls. A health refresh retrieves counters and
logs; it does not run a self-test. Sleeping disks require explicit wake
confirmation for health reads or tests. Extended tests have an additional
confirmation and run in the drive for hours, without blocking the server.
Their status and logs appear in the dashboard. While a test is observed
running, the collector retrieves health/test status every collection cycle.
It does not prevent the drive's own standby timer or another host process
from interrupting a test. The app never forcibly replaces a running test.

Only configured disks can be targeted. `allow_spindown: true` enables
spin-down eligibility; it is set only for the five intended spares. The
server also checks the host's mounts, swap and kernel holders, and refuses
spin-down if it cannot verify those checks. Mounted pool/parity disks are
ineligible. A running or unreadable self-test status blocks spin-down on an
awake disk. These checks cannot prevent another host process from mounting
or using a disk immediately afterward; coordinate administrative disk work.

Compose now requires read-only binds of `/proc/1/mountinfo` and `/proc/swaps`
into `/host`. Existing deployments must update their Compose file, not
just pull the new image. These files expose the NAS's state rather than the
container's mount namespace. Missing host information disables spin-down
by failing the check; other actions and monitoring can continue.

Actions and regular collection run sequentially in one worker, avoiding
collector/action races. Only one action is accepted at a time. The UI shows
queued/running/completed/failed status and automatically fetches observed
results after every action. It never assumes a command changed the disk's
state. The latest action status is transient and resets on restart; disk
history and health cache remain persistent.

On first production startup, a random action token is saved at
`/data/action-token` with mode 0600. Retrieve it locally on the NAS:

```bash
docker exec disk-status-page cat /data/action-token
```

Open the dashboard through your NPM HTTPS URL, choose **Unlock controls**,
and enter the token. It is kept only in the page's memory, discarded on
reload, and never embedded in repository files or returned by the public
API. Use **Lock controls** when done. All action requests, including manual
refresh, require it. Requests use POST with JSON, a custom token header and
same-origin checks; cross-origin requests are not permitted. NPM must
preserve the original Host header (its usual proxy configuration does).
Do not share the token or put it into URL parameters.

## Updating this version

After committing/pushing and confirming the GitHub Actions publication,
update `docker-compose.yml` on the NAS to the version in this repository
(including both `/host` binds), keeping any intentional local settings.
The supplied Compose interval is now 30 seconds.

Then, on debian-nas:

```bash
cd /opt/docker/disk-status-page
docker compose config --quiet
docker pull ghcr.io/blakdav/disk-status-page:latest
docker compose up -d
docker compose ps
docker compose logs --tail=50
```

No Ansible changes are required for this update. Existing history remains
in the same NVMe directory. The GitHub Actions workflow still publishes
`latest` and `sha-<full-commit-sha>` using `GITHUB_TOKEN` with
`contents: read` and `packages: write`. Private GHCR authentication, if
applicable, is described above.

## Homepage native panels and acknowledged warnings

`GET /api/homepage` returns cached summary counts and per-disk descriptions.
Homepage requests never query hardware, wake disks or initiate actions. This
read-only endpoint needs no action token; do not put the control token in
Homepage configuration.

Merge [examples/homepage-services.yaml](examples/homepage-services.yaml)
into Homepage's `services.yaml` on debian-docker. It provides a native
summary card plus an optional dynamic disk list, without an iframe or a
custom Homepage image. Keep your existing groups and replace the card's
`href` with your NPM HTTPS address. The widget's API URL can remain the
internal NAS address `http://172.23.24.16:8088/api/homepage`.

The example uses Homepage's documented Custom API mappings and dynamic-list
mode. The detailed list requires a Homepage version that supports that
mode; the summary uses ordinary numeric fields. Homepage must be able to
reach the NAS on port 8088. See the
[Custom API documentation](https://gethomepage.dev/widgets/services/customapi/).
The example has not yet been applied to your live Homepage configuration.

Summary fields include `total`, `awake`, `sleeping`, `unknown`, `attention`,
`known_issues`, `health_unknown`, `tests_running`, and `stale`. Counts refer
to disks, not numbers of warnings. Stale state is never counted as awake or
sleeping. A disk that has never provided health data is marked health
unknown rather than failed. The detailed panel makes that distinction
visible. Stale/unreachable state and collector errors still need attention.
Cached temperature and test status describe the last health read, not a
fresh measurement. No-data configured disks appear unknown until collected.

To acknowledge an existing cumulative SMART warning:

1. Unlock controls in Disk Status Page.
2. Review the warning's raw value and choose **Acknowledge current value**.
3. Confirm. The warning becomes **Known issue · unchanged**, visible in
   details and the known-issues count but excluded from the attention count.

Acknowledgements are saved in the existing SQLite database, keyed by disk
serial and attribute ID. A higher raw count needs attention again and shows
the increase over the acknowledged baseline. An **Undo acknowledgement**
button restores ordinary warning treatment. The action only updates the
app's database; it never clears or modifies the drive's SMART records.
The browser sends the displayed raw value, and the server rejects an
acknowledgement if that value changed before the action was processed.

Acknowledgement is allowed for cumulative counters 5 (reallocated sectors),
187 (reported uncorrectable), 188 (command timeout), and 199 (CRC errors).
It does not suppress current pending/unreadable-sector warnings, overall
SMART failure, manufacturer threshold failure flags, failed self-tests or
error-log warnings. A known issue does not establish that a disk is healthy.
Older cached warnings remain visible after upgrade; unclassified legacy
warnings stay active until replaced by the next successful health read.

This update adds a table automatically without changing existing history.
No Compose or Ansible changes are required relative to the disk-controls
version. After committing/pushing and waiting for Actions to publish, run
on debian-nas:

```bash
cd /opt/docker/disk-status-page
docker pull ghcr.io/blakdav/disk-status-page:latest
docker compose up -d
```

Then add the Homepage example to the existing Homepage configuration.
The GHCR workflow continues to publish `latest` and a commit-specific
`sha-<full-commit-sha>` tag. If the package is private, authenticate on the
NAS as described under Publishing before pulling.

Disk cards and Homepage rows retain inventory order from `disks.json`,
including during partial collection and individual disk actions. Database
updates do not determine their display order.


## Last extended self-test dates

Each disk card shows the last recorded extended-test start, the last time
an ended test was observed, and its result. Times use Pacific time and
include the year. Tracking is saved in the existing SQLite volume, survives
restarts, and is also available under each disk's `extended_test` field in
both `/api/status` and `/api/homepage`.

Successful app requests record their exact request timestamp. Tests found
running in an explicitly extended SMART log entry receive a **First observed
running** timestamp instead. Generic self-test execution status alone cannot
establish whether an external test is short or extended, so the app does
not infer an extended start from it.

A new result is recorded when a fresh SMART read discovers it. This is an
observation time, not the exact moment the drive finished. Failed and aborted
tests remain distinct from successful ones. Old results discovered on first
use are labeled **Historical result; calendar date unknown**, with the
reported power-on hours; no calendar date is reconstructed from those hours.
Cached readings while a disk sleeps do not advance these dates. Short tests
do not replace the last extended result. A pending request retains the prior
result until a distinguishable new extended result is observed.

Tests started before this feature cannot acquire exact start dates
retroactively. Two otherwise identical log entries at the same power-on hour
may be indistinguishable; the app conservatively keeps the result pending
instead of claiming completion. The app retains the latest tracking record
per disk, rather than a complete calendar history of every test.

This version automatically adds a tracking table without modifying existing
disk history or acknowledgements. After publication, update on debian-nas:

```bash
cd /opt/docker/disk-status-page
docker pull ghcr.io/blakdav/disk-status-page:latest
docker compose up -d
```

No Compose or Ansible changes are needed. The existing GHCR workflow still
publishes `latest` and a full-commit-specific tag. New tracking code has unit
test coverage; live NAS and browser verification remain pending deployment.


Homepage disk descriptions include colored Unicode markers for power and
health separately: green awake/healthy, blue sleeping, purple testing, red
attention, yellow known issues, and neutral unknown/stale. Cached testing
status on a sleeping or stale disk is explicitly labeled last-read testing
with a neutral marker. Text remains present so color is never the only
signal. These markers work with the existing `description` mapping; no custom
Homepage image or CSS is required. Appearance depends on the browser font.
Custom API does not support dynamic block highlighting; this does not change
Homepage's text/background colors. Optional summary labels in the example
use the same markers, serving as a static legend rather than a health alert.

Homepage requests the API on load and every 30 seconds while its page is
active, as configured by `refreshInterval: 30000`. The API reads the app's
cache. Disk collection has its own 30-second Compose interval, so a state
change can take roughly a minute to appear across both polling cycles.
SMART health normally refreshes every 30 minutes on awake disks, and test
status is refreshed each collection cycle once a running test is detected.
Browser background throttling and command duration can delay updates.
