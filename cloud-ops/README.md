# cloud-ops: the cloud and honeypot operations VM

A small Debian 13 VM on **backup-compute** (Hyper-V). It will hold everything that touches the cloud, so that optiplex-management never does:
- the DigitalOcean token;
- Terraform state and Packer;
- the SSH keys for the honeypot bastion;
- the log puller that connects to the honeypot and posts parsed events to the dashboard.

> **Status:** this folder makes cloud-ops a regular lab host: metrics agent, **Deploy all**, firewall, automatic security updates. The honeypot tooling (pinned Terraform/Packer, secrets prompt, weekly rebuild, log puller) arrives in the next PR.

**Why a separate VM:** the cloud account shouldn't live on the box that can push changes to every lab host, and the SSH client that connects every 30 s to a machine attackers may control should run somewhere disposable. Displaying attacker strings is the dashboard's job wherever this runs, and the dashboard already treats them as hostile.

## What it runs (managed from git)

| File | Installed as | What |
|---|---|---|
| `files/rack-metrics-agent.service` | `/etc/systemd/system/` | The same metrics agent as optiplex-management, posting to `optiplex-management.home.arpa:8080` as `cloud-ops`. It also carries **Deploy all** |
| `files/nftables.conf` | `/etc/nftables.conf` | In: SSH from the LAN only. Out: DNS, DHCP, NTP, HTTP(S), SSH (GitHub), 2222 (bastion), 8080 to the dashboard. Everything else dropped and logged |
| `files/50-cloud-ops-sshd.conf` | `/etc/ssh/sshd_config.d/` | Keys only, no root |
| `files/20auto-upgrades`, `52unattended-reboot` | `/etc/apt/apt.conf.d/` | Daily security updates; reboot at 04:45 if a kernel update needs it |

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

To rebuild it from scratch: in Hyper-V Manager, delete the VM and its folder (`C:\Hyper-V\cloud-ops`), remove its old deploy key on GitHub, then **Deploy all**.

## Day to day

Same as optiplex-management (see [`deploy/README.md`](../deploy/README.md#day-to-day)): `journalctl -u homelab-deploy -f`, `sudo systemctl start homelab-deploy`. First boot: `journalctl -u homelab-bootstrap`.
