# backups: mirror and cloud backup jobs

The backup jobs on `homeshare`, run by Task Scheduler and reported on the rack dashboard's **Scheduled tasks** screen. **Everything comes from this folder:** scripts, schedules and paths. Merge a PR, press **Deploy all**, and homeshare creates, changes or removes its tasks. Nothing is set up by hand.

| Job | What | When | Status |
|---|---|---|---|
| `homeshare-to-archive` | **Main** pool → **Archive** pool (SAS, same machine) | monthly | managed here (runs as SYSTEM) |
| `homeshare-to-backup` | **Main** pool → `\\backup-compute.home.arpa\HomeShareBackup` (**Backup** pool on backup-compute) | daily | managed here (runs as SYSTEM; share credential handled automatically, see below) |
| `homeshare-to-b2` | **Main** pool → Backblaze B2, **encrypted Kopia snapshots** (30 daily, Object Lock) | daily | managed here; needs the one-time secrets step (see [Cloud backup](#cloud-backup-kopia--backblaze-b2)) |

The first two are **mirrors** (`/MIR`): files deleted on homeshare are deleted from the copy at the next run. That's intentional (the copies shouldn't grow without limit). Versioned history is the cloud backup's job.

## Files

| File | What it does |
|---|---|
| `tasks.json` | **The jobs**: host, source, destination, schedule, time limit, enabled. Change by PR. |
| `Invoke-Mirror.ps1` | Runs one job, records the result |
| `Sync-MirrorTasks.ps1` | Makes the tasks in `\Homelab\` match `tasks.json`, and exchanges share credentials (run by the updater on every update, on both ends) |
| `MirrorCredential.ps1` | Key, encryption and dashboard helpers for the share credential (and the cloud secrets) |
| `Invoke-CloudBackup.ps1` | Runs one Kopia job (`"type": "kopia"`), records the result in the same format |
| `Set-CloudBackupSecrets.ps1` | **One-time**, on homeshare: stores the B2 key and repository password, encrypted |
| `kopia.json` | Kopia version, download URL and **SHA-256**; the updater installs only a download that matches |

On homeshare (all installed and kept current by the rack agent's updater):
```
%ProgramData%\HomelabBackups\
  Invoke-Mirror.ps1, Sync-MirrorTasks.ps1, MirrorCredential.ps1, tasks.json
  tasks-state.json      what Sync last registered (to spot hand edits)
  creds\<job>.json      share credential, ENCRYPTED to this machine's key (SYSTEM/Admins only)
  results\<job>.json    last 30 runs (summary only), read by the dashboard agent
  logs\<job>-<time>.log robocopy logs, newest 30 per job
```

### Why a wrong path in a PR can't wipe a folder

`/MIR` deletes whatever in the destination isn't in the source, so the runner only mirrors into a destination it can prove is this job's:
- it holds a hidden **`.homelab-mirror`** file naming the job (written on the first run; robocopy is told to leave it alone); or
- it doesn't exist yet, or is empty (a new mirror: the marker is written); or
- it has no marker but already looks like this mirror: at least 90% of its top-level names also exist in the source. It's adopted once and marked; that's how the existing `A:\HomeShare` is picked up. Windows' own `$RECYCLE.BIN` and `System Volume Information` (at a drive root) don't count, and are excluded from the mirror so they're never touched.
- or the job has `"adopt_existing": true` in `tasks.json`. That's a deliberate one-time override for a destination that is this job's mirror but has drifted (the abort message names the folders that didn't match). It writes the marker; remove the setting afterwards. It never overrides another job's marker.

Anything else, like a folder with other content or another job's marker, is **aborted** and shown on the dashboard. It also refuses an empty or missing source, overlapping paths, an unreachable destination, or a second copy of a running job.

## Changing a task (schedule, time limit, on/off)

Edit `tasks.json` in a PR, merge, press **Deploy all**. The homeshare updater (running as SYSTEM, so no password is involved) installs the new file and creates, re-registers or removes tasks to match. Adding a job is the same: add an entry with `"host"`, `"source"`, `"destination"` and a schedule. The Deployments screen shows e.g. *updated to abc1234 · tasks: 1 updated*, or the error if the file is wrong.

```json
"homeshare-to-archive": {
  "host": "homeshare",
  "description": "Monthly mirror of the Main pool to the SAS archive",
  "source": "D:\\HomeShare",
  "destination": "A:\\HomeShare",
  "schedule": { "monthly": { "days": [1], "at": "03:00" } },
  "time_limit_hours": 24,
  "enabled": true
}
```
Schedules use local time: `"daily": { "at": "02:00", "every_days": 1 }` · `"weekly": { "days": ["mon", "thu"], "at": "02:00" }` · `"monthly": { "days": [1, 15, "last"], "at": "03:00" }`.

**Git is the source of truth.** A task changed or disabled by hand in Task Scheduler is put back at the next update. To pause a job, set `"enabled": false`. A job removed from `tasks.json` has its task deleted. Tasks in `\Homelab\` that Sync didn't create are never touched. Each managed task is tagged with its host name, so a wrong host name can't remove another host's tasks.

## What a run does

1. **Checks first** (above), and records *aborted* instead of running if anything is off.
2. `robocopy <source> <destination> /MIR /XF .homelab-mirror /XD $RECYCLE.BIN "System Volume Information" /R:2 /W:5 /FP /NP /NDL /BYTES /UNILOG:<log>`
   - `/R:2 /W:5`: a locked file is retried twice, 5 s apart, then reported as failed. The default is 1,000,000 retries 30 s apart, which can hang a run for days.
   - No `/V`: verbose mode lists every unchanged file, which would make each log huge.
3. **Reads robocopy's summary** (files and bytes copied, extras removed, failed, totals) and its exit code. The exit code is a set of flags: 1 = copied, 2 = extras removed, 4 = mismatches, 8 = some copies failed, 16 = fatal. 0–3 is *ok*, 4–7 *warn*, 8+ *failed*. The first 5 `ERROR` lines are kept.
4. Exits 0 ok / 1 warn / 2 failed or aborted / 3 already running, so Task Scheduler's *Last Run Result* means something.

The dashboard agent reports every task in **`\Homelab\`** once a minute. A task is **overdue** if it hasn't run within its schedule's longest normal gap + 10% + 1 h, and **failed** if the last run failed or was aborted.

## Rollout

Merge, press **Deploy all** once: every host pulls the commit and applies all of it in that run.
- An updater that replaces itself immediately re-runs as the new version.
- During a new credential handoff, homeshare and backup-compute each wait up to 2½ minutes for the other's step.

The one exception is a host whose *installed* updater predates that re-run. Its first update after the change applies the rest on the next press, or the next 15-minute run.

Check a job without changing anything (robocopy `/L`, records nothing), if you want to:
```powershell
& "$env:ProgramData\HomelabBackups\Invoke-Mirror.ps1" -Job homeshare-to-archive -WhatIf
```

## Share credential (jobs that write to another machine)

SYSTEM can't log on to another machine's share outside a domain, so `homeshare-to-backup` writes to `HomeShareBackup` as a local account on backup-compute, `svc-backup`. Its task still runs as SYSTEM and is managed from `tasks.json` like any other. The credential is set up and handed over **automatically**; nobody types or sees the password.

```json
"share_access": { "provider": "backup-compute", "share": "HomeShareBackup", "account": "svc-backup", "key_epoch": 1 }
```

On each update / Deploy all, `Sync-MirrorTasks.ps1` does its part on both machines:

1. **homeshare** (the job's host) keeps an RSA-3072 key in the Windows **machine key store**. The private half never leaves the machine and only SYSTEM/Administrators can use it. It publishes the **public** key through the dashboard (`/api/creds`, agent token required).
2. **backup-compute** (the provider):
   - creates `svc-backup` with a random 32-character password, not in *Users*, so no local or Remote Desktop logon (SMB still works);
   - grants it **Change** on the share and **Modify** on the share's folder;
   - encrypts the password with homeshare's public key (RSA-OAEP) and posts only the **ciphertext**.
3. **homeshare** collects the ciphertext and stores it encrypted in `creds\<job>.json`. At run time the runner decrypts it, maps the share as `svc-backup` for the run, and unmaps it afterwards.

The dashboard only ever holds a public key and ciphertext. Both machines get the same update / Deploy all, and for the first 15 minutes of a new handoff each waits up to 2½ minutes for the other's step, so one press completes it. If the other machine is down, the Deployments screen shows *waiting for the share credential*, and a run would be *aborted* with that reason, until it's back.

**Key pinning.** backup-compute pins the first key it sees for the job. If a different key is published later (for example by another machine that has the agent token, trying to get the password), it **refuses** and shows an error on the Deployments screen. If homeshare legitimately gets a new key (reinstall), bump `key_epoch` in `tasks.json`: both ends then move to a new key and a new password.

**Rotation.** A new key, a lost dashboard state, or a missing account makes backup-compute set a new password and re-issue it. Nothing else changes it.

## Cloud backup (Kopia + Backblaze B2)

`homeshare-to-b2` takes a daily [Kopia](https://kopia.io) snapshot of `D:\HomeShare` into a B2 bucket. **For now its source is only `D:\HomeShare\School`**, to test the setup and a restore before the full upload; widening it is a one-line change to `source` in `tasks.json`.

- **Private:** Kopia encrypts and deduplicates on homeshare before anything is uploaded. The bucket holds only encrypted, randomly named pack files; Backblaze can't see file names or contents.
- **Can't be deleted or overwritten for 30 days**, even with the B2 key: the repository is created with Object Lock in *compliance* mode, and Kopia's daily full maintenance renews the locks on everything still in use.
- **Retention:** 30 daily snapshots (plus the latest). Older ones expire through Kopia maintenance.
- **Safety check:** a run aborts if `D:\HomeShare` is missing or empty, so a lost volume can't push the good snapshots out of retention.
- Open files are read through Volume Shadow Copy; already-compressed media isn't recompressed.
- The first upload (~3.4 TB) takes longer than one run's time limit; it resumes from Kopia's checkpoints on the next run.

### Secrets

| Secret | Where | If lost |
|---|---|---|
| B2 application key (bucket-scoped) | homeshare `creds\homeshare-to-b2.json`, encrypted to the machine key | Make a new key in B2, re-run the secrets script |
| Kopia **repository password** | the same file **and your password manager** | **Unrecoverable**: the cloud copy can never be decrypted. Keep it in the password manager (and on paper) |

Nothing secret is in git. Each run decrypts them into its own process only, tells Kopia not to save the password, and deletes Kopia's connection file afterwards.

### Turning it on (once)

1. In B2: create a **private** bucket with **Object Lock enabled** (it can only be enabled at creation), and an application key restricted to that bucket.
2. Generate the repository password in the password manager.
3. PR: set `kopia.bucket`, `kopia.endpoint` (`s3.<region>.backblazeb2.com`, shown on the bucket), the `sha256` in `kopia.json`, and `"enabled": true`. Merge, **Deploy all**.
4. On homeshare, elevated: `& "$env:ProgramData\HomelabBackups\Set-CloudBackupSecrets.ps1" -Job homeshare-to-b2`
5. The next run creates the repository. After the first upload, **test a restore** of a few files.

To restore: `kopia repository connect s3 --bucket=... --endpoint=...` (with the key and password), then `kopia snapshot list` and `kopia restore <id> <target>`, or mount a snapshot with `kopia mount`.
