# deploy — auto-deploy on the OptiPlex

A systemd timer on the OptiPlex checks `main` every ~2 minutes. When it changes, `homelab-deploy.sh` pulls it and deploys only the projects whose folders changed.

After pulling, the script re-runs itself from the new checkout (`--apply`), so a change to `homelab-deploy.sh` takes effect in the same deploy. (Before, each deploy ran the previous commit's script, which is how PR #20's and #21's agent units went uninstalled.) On every run, including when nothing changed, it also checks that this machine's agent units are installed and running, and fixes them if not.

**dashboard** (`rack-dashboard.service`, `/opt/rack-dashboard`):
1. Syntax-check the Python files.
2. First install only: create the venv and copy the GeoIP database from the old `/opt/noise-map`.
3. Generate `/etc/rack-dashboard/agent-token` if it's missing.
4. Snapshot `/opt/rack-dashboard`, then sync the code, keeping `.venv/` and `*.mmdb`.
5. `pip install` if `requirements.txt` changed.
6. Install the unit if it's missing or changed. A `noise-map` drop-in is carried over.
7. Stop the old `noise-map.service` if it's running.
8. Restart and health-check `http://127.0.0.1:8080/api/smart` and the unit's state.
9. Once healthy, disable `noise-map` and install the OptiPlex's own SMART agent timer.

If anything fails, it restores the snapshot. During the migration, it also brings `noise-map` back exactly as it was.

### Roles

The same script runs on more than one Linux host. `/etc/homelab/role` says what this host is; no file means `dashboard` (the OptiPlex, as before).

| Role | Host | Deploys when these change | What |
|---|---|---|---|
| `dashboard` | optiplex-management | `dashboard/` | The steps above |
| `cloud-ops` | cloud-ops (VM) | `cloud-ops/`, `honeypot/`, `dashboard/agents/metrics_agent.py` | Metrics agent, nftables, sshd config, unattended-upgrades (checked with `nft -c` and `sshd -t` first; snapshot and rollback like the dashboard); signature-verified Terraform/Packer, honeypot puller and rebuild timers. A merge touching `honeypot/packer/` starts a rebuild, `honeypot/terraform/` a `terraform apply`. See [`cloud-ops/`](../cloud-ops/) |

An unknown role fails the deploy instead of guessing. cloud-ops doesn't use the manual setup below: its first boot (see [`vms/`](../vms/)) makes the deploy key, clones and installs the timer.

A failed commit is recorded and **not retried** until `main` moves, so a broken commit can't restart services every 2 minutes. To force a retry: `sudo rm /var/lib/homelab-deploy/failed-rev`.

Pull, not push: the OptiPlex only makes outbound connections to GitHub. No webhook port, no Actions runner.

## Security

- The OptiPlex gets a **read-only deploy key** scoped to this one repo.
- The deploy runs as root, so **anyone who can push to `main` can run code on the jump host.** Protect `main`: Settings → Branches → add a rule requiring a pull request before merging.
- `/opt/homelab` is a mirror: local edits there are wiped on the next deploy.

## One-time setup (on the OptiPlex)

```bash
sudo apt install -y git rsync curl

# 1. Read-only deploy key
sudo ssh-keygen -t ed25519 -N "" -C "optiplex-deploy" -f /root/.ssh/homelab_deploy
sudo cat /root/.ssh/homelab_deploy.pub
```
Add that public key at **GitHub → homelab → Settings → Deploy keys → Add deploy key**. Leave **Allow write access unchecked**.

```bash
# 2. SSH alias so only this repo uses the key
sudo tee -a /root/.ssh/config >/dev/null <<'EOF'
Host github-homelab
    HostName github.com
    User git
    IdentityFile /root/.ssh/homelab_deploy
    IdentitiesOnly yes
EOF
sudo ssh -T github-homelab   # accept GitHub's host key; expect "successfully authenticated"

# 3. Clone
sudo git clone git@github-homelab:j-will178/homelab.git /opt/homelab

# 4. (Historical) The first deploy used to need a dry run to protect hand edits in
#    /opt/noise-map. The dashboard now installs to its own folder and leaves
#    /opt/noise-map untouched, so there's nothing to preview.
```

Local-only settings (e.g. `--home`, a different `--iface`) go in a drop-in so a future unit update from git doesn't overwrite them:
```bash
sudo systemctl edit rack-dashboard
```
See [`dashboard/README.md`](../dashboard/README.md#deployment) for the full `ExecStart` line.

```bash
# 5. Install and run once by hand
sudo cp /opt/homelab/deploy/homelab-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start homelab-deploy.service
journalctl -u homelab-deploy -n 30 --no-pager   # expect "dashboard: healthy" and "deployed <sha>"

# 6. Turn on the timer
sudo systemctl enable --now homelab-deploy.timer
systemctl list-timers homelab-deploy.timer
```

## Day to day

| Task | Command |
|---|---|
| Deploy log | `journalctl -u homelab-deploy -f` |
| Deploy right now | `sudo systemctl start homelab-deploy` |
| Currently deployed commit | `cat /var/lib/homelab-deploy/deployed-rev` |
| Pause auto-deploy | `sudo systemctl stop homelab-deploy.timer` |
| Force full redeploy | `sudo rm /var/lib/homelab-deploy/deployed-rev && sudo systemctl start homelab-deploy` |
| Retry a commit that failed | `sudo rm /var/lib/homelab-deploy/failed-rev && sudo systemctl start homelab-deploy` |

## Adding another project

In `homelab-deploy.sh`, write `deploy_<name>` and `rollback_<name>` (check every step with `|| return 1`: `set -e` does not apply inside them), then add one line to `main()`:
```bash
if touches "$changed" "<folder>/"; then run_step <name> "$changed" || failed=1; fi
```
