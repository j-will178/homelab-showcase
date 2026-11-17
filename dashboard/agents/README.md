# SMART agents

One small agent per machine. Every 15 minutes it reads drive health with `smartctl` and POSTs it to the dashboard on the OptiPlex. Agents only make outbound connections to the OptiPlex on TCP 8080. Nothing new is opened toward the servers.

Each machine sends two things: **compute metrics every 10 seconds** (`/api/metrics`) and **SMART every 15 minutes** (`/api/smart`).

Schedulers can't go below a minute (Task Scheduler, OPNsense cron), so the metrics agent is a **long-running loop** (`--loop 10` / `-LoopSeconds 10`). Each sample is averaged over its 10 s. When an update replaces the agent file, the loop exits by itself and its supervisor starts the new version.

The loop also carries **Deploy all** (see `dashboard/README.md`): the dashboard's reply to each sample includes the latest deploy request, and the agent brings this machine's own update forward. Nothing it receives is executed: what "update now" means is fixed locally (`--deploy-stamp` / `--deploy-command` on Linux and FreeBSD, the `last-full-run.txt` stamp on Windows). On Windows the metrics agent also reports, once a minute, every scheduled task in `\Homelab\` and the mirror jobs' results (`%ProgramData%\HomelabBackups\results`), for the Scheduled tasks screen. On machines that `backups/tasks.json` gives a job, the updater also installs the backup scripts and makes the `\Homelab\` tasks match the file (see `backups/README.md`).

Each updater also writes a status file after every run (commit, branch, last check/change, result, git version), which the loop forwards for the Deployments screen: `/var/lib/homelab-deploy/status.json` (OptiPlex), `deploy-status.json` in the agent folder (Windows, OPNsense).

| Machine | Agents | Schedule | How it stays up to date |
|---|---|---|---|
| optiplex-management | `metrics_agent.py`, `smart_agent.py` | `rack-metrics-agent.service` (loop, `Restart=always`), `rack-smart-agent.timer` (15 min) | `homelab-deploy` (see `deploy/`) |
| homeshare, backup-compute | `Send-Metrics.ps1` (task *Rack Dashboard metrics*, loop), `Send-SmartReport.ps1` via `Invoke-RackAgent.ps1` | *Rack Dashboard SMART agent* every minute: starts the metrics task if it isn't running; SMART every 15 min | **Pulls from GitHub every 15 min** |
| opnsense | `metrics_agent.py` (loop under `daemon -r`), `smart_agent.py`, via `opnsense/run.sh` | GUI cron → configd action, **every minute**: starts the loop if it isn't running; SMART every 15 min | **Pulls from GitHub every 15 min** |

The every-minute wrappers are watchdogs for the metrics loop, and run the GitHub update + SMART only when 15 minutes have passed. On Windows the wrapper also switches its own scheduled task to every minute and registers the *Rack Dashboard metrics* task the first time. On OPNsense the cron job has to be changed **once by hand** (see below), because cron jobs live in OPNsense's config and a script can't safely edit them.

### Auto-update (Windows and OPNsense)

Before each run (every 15 min), the wrapper:
1. **pulls** the latest `main` with a **read-only deploy key** unique to that machine. Only `dashboard/agents` (plus a few top-level files) is checked out, so `docs/` never lands on these machines. The key can still *read* the whole repo, which has no secrets.
2. **checks** changed scripts: PowerShell parser on Windows, Python parse / `sh -n` on OPNsense.
3. **installs** them, keeping the previous copy as `.prev`. **A script that fails the check is not installed.**
4. **runs** the agent regardless. If GitHub is unreachable or a commit is broken, reporting carries on with the last good copy, and the log says why.

So an agent fix reaches every machine within ~15 min of merging. The dashboard's *agent outdated* badge disappears as each one updates. Anyone who can merge to `main` can run code as SYSTEM/root on these machines, the same trust model as the OptiPlex. That's why PRs get reviewed.

Logs: Windows `C:\ProgramData\RackAgent\agent.log` (`[update]` lines) · OPNsense `/var/log/rack-agent.log`.

Agents run `smartctl -x` (not `-a`), which includes ATA Device Statistics: the drive's own "percentage of endurance used". It's the only SSD wear figure that means the same thing across vendors.

**Updating an agent:** when an agent changes, its version goes up (`smart_agent.py/N`, `Send-SmartReport.ps1/N`) and so does `LATEST_AGENTS` in `smart.py`. The dashboard then shows an **"agent vN · update available"** badge on every host still running the old copy. The OptiPlex updates itself. For the others, re-copy the file (OPNsense) or re-run the installer (Windows).

All agents authenticate with the same token, created on the OptiPlex at first deploy:
```bash
sudo cat /etc/rack-dashboard/agent-token
```
To rotate it: write a new value to that file, `sudo systemctl restart rack-dashboard`, then update each agent.

Use these host names so the dashboard orders them like the rack: `opnsense`, `optiplex-management`, `homeshare`, `backup-compute`. The order is set by `--smart-order` in `rack-dashboard.service`.

## OptiPlex (Debian)

Only one step is manual:
```bash
sudo apt install -y smartmontools
sudo systemctl start rack-smart-agent    # send a report now instead of waiting 15 min
journalctl -u rack-smart-agent -n 5
```

## homeshare and backup-compute (Windows)

Nothing is copied by hand: the machine creates its own deploy key, pulls the agent folder from GitHub, and runs the installer from that clone. All steps in an **elevated** PowerShell.

1. **Prerequisites:** smartmontools (Windows installer from https://www.smartmontools.org/wiki/Download) and Git:
   ```powershell
   winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements
   ```
   Then **open a new** elevated PowerShell, so `git` is on PATH (`git --version`).
2. **Deploy key**, unique to this machine:
   ```powershell
   $d = "$env:ProgramData\RackAgent"; New-Item -ItemType Directory -Force -Path $d | Out-Null
   Start-Process C:\Windows\System32\OpenSSH\ssh-keygen.exe -Wait -NoNewWindow -ArgumentList "-q -t ed25519 -N `"`" -C rack-agent@$($env:COMPUTERNAME.ToLower()) -f `"$d\deploy_key`""
   Get-Content "$d\deploy_key.pub"
   ```
   Add it on GitHub: repo → **Settings → Deploy keys → Add deploy key**, title `rack-agent-<host>`, **leave "Allow write access" unchecked**.
3. **Clone the agent folder** with that key:
   ```powershell
   $ssh = "'C:/Windows/System32/OpenSSH/ssh.exe' -i 'C:/ProgramData/RackAgent/deploy_key' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='C:/ProgramData/RackAgent/known_hosts'"
   git -c core.sshCommand=$ssh clone --depth 1 --filter=blob:none --sparse git@github.com:j-will178/homelab.git "$d\repo"
   git -C "$d\repo" config core.sshCommand $ssh
   git -C "$d\repo" sparse-checkout set dashboard/agents
   ```
4. **Name the Storage Spaces pools** (the dashboard shows them on every drive; the default is "Storage pool"):
   ```powershell
   Get-StoragePool -IsPrimordial $false | Format-Table FriendlyName, UniqueId
   Set-StoragePool -UniqueId '<UniqueId>' -NewFriendlyName 'Main'
   ```
5. **Install from the clone:**
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   & "$d\repo\dashboard\agents\Install-SmartAgent.ps1" -DashboardUrl http://optiplex-management.home.arpa:8080/api/smart -Name homeshare -AutoUpdate   # or backup-compute
   ```
   Paste the agent token when asked (on optiplex-management: `sudo cat /etc/rack-dashboard/agent-token`; re-running keeps the saved one). The installer fixes the key's permissions so SYSTEM can use it, locks `C:\ProgramData\RackAgent` to SYSTEM + Administrators, registers the task **Rack Dashboard SMART agent** (every 15 min and at startup, running `Invoke-RackAgent.ps1`), runs it once and shows the result.
6. **Check:** `Get-Content "$env:ProgramData\RackAgent\agent.log" -Tail 4`. A `sent N drive(s)` line with **no** `update FAILED` above it means the pull works as SYSTEM. Other useful commands: `& "$d\Send-SmartReport.ps1" -DryRun` (collect without sending), `& "$d\Invoke-RackAgent.ps1" -NoRun` (pull now), `Start-ScheduledTask -TaskName 'Rack Dashboard SMART agent'` (pull and report now).

The Windows agent sends two things:
- **`smartctl` data** for every drive it can open: full SMART detail.
- **Windows' own disk view** (`Get-PhysicalDisk` + `Get-StorageReliabilityCounter`), including each disk's **Storage Spaces pool**. Windows can hide pool members from `smartctl`. When that happens, those disks still show up from Windows' counters, marked *via Windows*. Disks seen both ways are merged by serial number and labeled with their pool.

Log: `C:\ProgramData\RackAgent\agent.log`

Send a report now instead of waiting: `Start-ScheduledTask -TaskName 'Rack Dashboard SMART agent'`

**Confirmed on homeshare (2026-11-16):** `smartctl` reads every drive directly, including the Storage Spaces members and the SAS drives behind the HBA (IT mode). The *via Windows* fallback isn't needed there, but it stays in place in case a future disk can't be opened.

## OPNsense

1. **System → Firmware → Plugins**: install **os-smart** (provides `smartctl`). Under **System → Settings → Administration → Secure Shell**, temporarily allow root login (LAN only).
2. From optiplex-management, copy the token and the one-time installer over:
   ```bash
   ssh root@opnsense.home.arpa 'mkdir -p /usr/local/rack-agent && chmod 700 /usr/local/rack-agent'
   sudo cat /etc/rack-dashboard/agent-token | ssh root@opnsense.home.arpa 'umask 077; cat > /usr/local/rack-agent/token'
   scp /opt/rack-dashboard/agents/opnsense/install-opnsense.sh root@opnsense.home.arpa:/tmp/
   ```
3. On OPNsense (shell), run it. It installs `git` if needed, creates the deploy key and prints it (add it on GitHub as `rack-agent-opnsense`, **read-only**), clones the agent folder, installs the configd action pointing at `run.sh`, and tests a push:
   ```sh
   sh /tmp/install-opnsense.sh http://optiplex-management.home.arpa:8080/api/smart
   ```
   Expect `sent 1 drive(s) as 'opnsense': HTTP 200` at the end.
4. **System → Settings → Cron**: edit the job *Push SMART report to rack dashboard* and set **Minutes** to `*` (every minute). `run.sh` keeps the metrics loop running and sends SMART every 15 min. Then turn root/password SSH back off.
5. *(Optional, for the CPU temperature on the compute screen)* **System → Settings → Miscellaneous → Thermal Sensors**: set *Hardware* to **Intel Core** (loads `coretemp`).

That's the last time anything gets copied to OPNsense by hand.

## Report format

`POST /api/smart` with `Authorization: Bearer <token>` and a JSON body:
```json
{"schema": 1, "host": "homeshare", "agent": "...", "os": "windows", "collected_at": "...",
 "drives": [ <raw smartctl -j -a output>, ... ],
 "windows_disks": [ ... ],   "errors": [ "notes shown on the dashboard" ]}
```
The collector stores the latest report per host and evaluates health on read (`smart.py`).
