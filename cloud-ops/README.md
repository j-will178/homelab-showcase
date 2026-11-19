# cloud-ops: the cloud and honeypot operations VM

A small Debian 13 VM on **backup-compute** (Hyper-V). It holds everything that touches the cloud, so that optiplex-management never does:
- the DigitalOcean token;
- Terraform state and Packer;
- the SSH keys for the honeypot bastion;
- the log puller that connects to the honeypot and posts its events to the dashboard.

> **Status:** a regular lab host (metrics agent, **Deploy all**, firewall, automatic security updates) with the honeypot tooling installed. Nothing exists on DigitalOcean until you run the one-time `hp-secrets` (below).

**Why a separate VM:** the cloud account shouldn't live on the box that can push changes to every lab host, and the SSH client that connects every 30 s to a machine attackers may control should run somewhere disposable. Displaying attacker strings is the dashboard's job wherever this runs, and the dashboard already treats them as hostile.

## What it runs (managed from git)

| File | Installed as | What |
|---|---|---|
| `files/rack-metrics-agent.service` | `/etc/systemd/system/` | The same metrics agent as optiplex-management, posting to `optiplex-management.home.arpa:8080` as `cloud-ops`. It also carries **Deploy all** |
| `files/nftables.conf` | `/etc/nftables.conf` | In: SSH from the LAN only. Out: DNS, DHCP, NTP, HTTP(S), SSH (GitHub), 2222 (bastion), 8080 to the dashboard. Everything else dropped and logged |
| `files/50-cloud-ops-sshd.conf` | `/etc/ssh/sshd_config.d/` | Keys only, no root |
| `files/20auto-upgrades`, `52unattended-reboot` | `/etc/apt/apt.conf.d/` | Daily security updates; reboot at 04:45 if a kernel update needs it |

### Honeypot operations (`honeypot/`, `tools.env`, `files/hc-install`)

| What | Runs as | When |
|---|---|---|
| **Terraform and Packer** (`/usr/local/bin`), versions pinned in `tools.env` | installed by the deploy | when `tools.env` changes |
| `hp-rebuild` (`hp-rebuild.service`): new Packer image + `terraform apply`. Only the honeypot is replaced (new one up before the old one goes); old snapshots deleted, the last two kept | root | weekly, Sun ~05:30 (`hp-rebuild.timer`), and after a merge that changes `honeypot/packer/` |
| `hp-rebuild --refresh` (`hp-refresh.service`): `terraform apply` with the current image. Follows a changed home IP into the cloud firewalls; applies a changed `exposure` | root | daily ~06:15 (`hp-refresh.timer`), and after a merge that changes `honeypot/terraform/` |
| `hp-pull` (`hp-pull.service`): reads Cowrie's log through the bastion every 30 s and posts it to the dashboard (`/api/honeypot/ingest`) with this side's status | unprivileged system user `hp-pull`, sandboxed; gets only the lab key and agent token | always |
| `hp-secrets`: the one-time step | you, with sudo | once |

**Verified downloads:** `hc-install` fetches HashiCorp's public key and refuses it unless its fingerprint matches `HASHICORP_FINGERPRINT` (from [hashicorp.com/security](https://www.hashicorp.com/security)). It then checks the signature on `SHA256SUMS` and the zip's hash, and installs only if all three pass. Fetching the key each time picks up expiry extensions; a rotated key stops the install until the new fingerprint is checked and merged. To upgrade, change the version in `tools.env`.

Terraform verifies the DigitalOcean provider itself (registry signatures; `.terraform.lock.hcl` kept in the work directory after the first run). The Packer plugin is pinned to an exact version and checked against its GitHub release checksums; Packer has no signature check for third-party plugins.

**Where things live:**
- `/etc/honeypot/` (root, 0700): `do_token`, `admin.pub` (your `hpadmin` key), `lab_ed25519` (cloud-ops' key: `jump` on the bastion, `collector` on the honeypot).
- `/var/lib/honeypot/` (root): `keys/` (host keys injected into the droplets, so they're pinned in advance), `terraform/` (work dir + **state**), `packer/`, `status.json` and `endpoints.json` (read by the puller and shown on the dashboard).
- Nothing secret is passed on a command line: Terraform's inputs go in a 0600 `run.auto.tfvars.json`; the token reaches curl on stdin.
- The home IP for the cloud firewalls is looked up at each run (two services must agree) and never written to git.

`deploy/homelab-deploy.sh` reads the host's role from `/etc/homelab/role`:
- **`cloud-ops`:** it deploys this folder (checking the agent's syntax, the firewall rules and the sshd config first, with rollback on failure).
- **No role file** (optiplex-management): the dashboard role, exactly as before.

## How it's built

Entirely from git: [`vms/`](../vms/) defines the VM (`vms.json`) and its first boot (`vms/cloud-ops.user-data`), and backup-compute's agent builds it on **Deploy all**. See [`vms/README.md`](../vms/README.md) for what the build does and doesn't do.

On first boot the VM writes `/etc/homelab/role` (`cloud-ops`) and the agent token, installs git and friends, then waits for GitHub access: it makes its own read-only deploy key and posts the public key to the dashboard every minute until it can clone. After that, `homelab-deploy` takes over as on optiplex-management.

## One-time setup

1. **backup-compute: Hyper-V and the `LAN` switch** ([`vms/README.md`](../vms/README.md#one-time-setup-on-the-host)). Needs a reboot; do it outside the 02:00 mirror window.
2. **`vms.json`: two values**, in a PR:
   - `images.debian-13-genericcloud`: `file` and `sha512` from Debian's `SHA512SUMS` (the `debian-13-genericcloud-amd64-<version>.raw` line);
   - `vms.cloud-ops.admin_ssh_keys`: your SSH **public** key, to log in as `labadmin` (`ssh -l labadmin cloud-ops.home.arpa`).

   Until both are set, the build waits and the Deployments screen says why.
3. **OPNsense:** a DHCP reservation for MAC `02:4C:41:42:00:01` (`cloud-ops`), so `cloud-ops.home.arpa` stays stable.
4. **Merge, then Deploy all.** The first build downloads the image (a few minutes). Then the Deployments screen shows *cloud-ops is waiting for its deploy key*: copy the key and add it on GitHub (**homelab → Settings → Deploy keys**, title `cloud-ops`, write access unticked).

Within a minute, **cloud-ops** appears on the Compute and Deployments screens, and from then on it updates with **Deploy all** like every other host.

### The honeypot (once)

Needs the DigitalOcean API token (custom scopes, see [`honeypot/README.md`](../honeypot/README.md#first-time-setup-once)) and the line from your `hpadmin.pub`:

```bash
ssh -t cloud-ops sudo hp-secrets
```

It checks the token against the API, saves both, and asks before the first build (DigitalOcean starts billing then, about $10/month). The build takes 10–15 minutes: `ssh cloud-ops journalctl -fu hp-rebuild`. It starts **lab-only**: Cowrie is reachable from the home IP only. Then work through the checklist in [`honeypot/README.md`](../honeypot/README.md#going-live-test-first-then-open-up).

To rebuild it from scratch: in Hyper-V Manager, delete the VM and its folder (`C:\Hyper-V\cloud-ops`), remove its old deploy key on GitHub, then **Deploy all**.

## Day to day

Same as optiplex-management (see [`deploy/README.md`](../deploy/README.md#day-to-day)): `journalctl -u homelab-deploy -f`, `sudo systemctl start homelab-deploy`. First boot: `journalctl -u homelab-bootstrap`.

Honeypot (the Honeypot screen's status chips show most of this):

```bash
journalctl -fu hp-pull                 # the puller
journalctl -u hp-rebuild -u hp-refresh # rebuilds and applies
sudo systemctl start hp-rebuild        # rebuild now
sudo hp-pull ssh hp-honeypot status    # the log reader on the honeypot, through the bastion, as the puller does it
sudo cat /var/lib/honeypot/status.json
```

To take everything down on DigitalOcean: `sudo systemctl disable --now hp-rebuild.timer hp-refresh.timer hp-pull`, then `cd /var/lib/honeypot/terraform && sudo -E terraform destroy` with `DIGITALOCEAN_TOKEN` set from `/etc/honeypot/do_token`.
