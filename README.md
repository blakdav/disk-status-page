# Disk Status Page

A single container on debian-nas collects disk power state, caches SMART
health, stores observed transitions in SQLite, and serves a home-network
dashboard. No third-party Python dependencies, frontend build, or database
server. The app never requests a spin-down or initiates a SMART self-test.

## Monitoring behavior

- Checks power state every 60 seconds using `hdparm -C`.
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
has no login and should remain on the trusted home network.

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

Seven unit tests pass, covering sleeping/unknown states, standby races,
unsupported SMART power checks, health warning bits, persisted transitions,
observation gaps, and HTTP responses tested without sockets. Python compiles,
YAML parses, and `docker compose config --quiet` passes. This development
environment denies Docker daemon access and listening sockets, so the image
build, live HTTP/browser preview, and NAS hardware checks are still pending.
No image has been published and no server deployment has been performed.
