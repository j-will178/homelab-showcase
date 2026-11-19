# vms: Hyper-V VMs defined in git

VMs on the Windows hosts, created by code. `vms.json` says which VM runs where; the agent on that host (`Invoke-RackAgent.ps1`, as SYSTEM) runs `Sync-HyperV.ps1` on every update, so a merged PR + **Deploy all** builds a VM. There is no setup on the host beyond enabling Hyper-V and making one switch.

| File | What |
|---|---|
| `vms.json` | Pinned base images (file name + SHA-512) and the VMs: host, CPU, memory, disk, switch, MAC, role, admin user and SSH keys |
| `Sync-HyperV.ps1` | Builds any VM that's missing on this host (installed to `%ProgramData%\HomelabVMs`) |
| `<role>.user-data` | First-boot config (cloud-init) for VMs of that role, e.g. `cloud-ops.user-data` |

## What a build does

1. Downloads the Debian cloud image named in `vms.json` (once, cached in `%ProgramData%\HomelabVMs\images`) and checks its SHA-512 against the hash in git. No match: no VM.
2. Turns the raw image into a VHDX (fixed-VHD footer, then `Convert-VHD`) and grows it to `disk_gb`.
3. Writes a small seed disk (FAT, label `CIDATA`) with the rendered `user-data`: hostname, admin user and SSH keys, the role, the agent token and GitHub's host key. The last two come from the host's own agent folder, so nothing is typed in and nothing secret is in git.
4. Creates the VM: Gen 2, Secure Boot (UEFI CA template, which Debian's shim is signed with), static memory, fixed MAC, starts with the host. Then starts it.

On first boot the VM installs its packages, makes its own read-only deploy key and posts the **public** key to the dashboard. The Deployments screen shows a banner with the key and a Copy button: add it on GitHub (**homelab → Settings → Deploy keys**, write access unticked). Within a minute the VM clones the repo, runs `homelab-deploy` for its role, and shows up on the Compute and Deployments screens like any other host.

## What it never does

- **Change or delete a VM.** CPU, memory or disk changes in `vms.json` show as *pending* ("differs from vms.json"). To apply one, delete the VM (and its folder) by hand, then **Deploy all** rebuilds it from git.
- **Touch a VM it didn't make.** Its VMs carry a `[homelab-managed …]` marker in their Notes; a VM with the same name but no marker is reported and left alone.
- **Overwrite a disk.** If `<name>.vhdx` exists without a VM, it stops and says so.
- **Store VMs in a share.** A path inside an SMB share is refused (`HomeShareBackup` is a mirror destination: anything not on homeshare gets deleted).

The VM folder and the seed disk are readable by SYSTEM and Administrators only, because the seed holds the agent token.

## One-time setup on the host

In an elevated PowerShell on backup-compute, outside the 02:00 mirror window (needs a reboot):

```powershell
Install-WindowsFeature -Name Hyper-V -IncludeManagementTools -Restart
```

After the reboot, the switch. The host's network drops for a few seconds while the switch takes over the adapter:

```powershell
Get-NetAdapter                                     # find the LAN adapter's name
New-VMSwitch -Name LAN -NetAdapterName '<LAN adapter>' -AllowManagementOS $true
```

Until both exist, the Deployments screen shows the sync as *pending* and nothing is built.

## Adding a VM

1. Add it to `vms.json`: a unique `mac` (locally administered: `02:…`), a `role` with a matching `<role>.user-data`, and a `path` on a local drive.
2. Add a DHCP reservation for the MAC in OPNsense, so `<name>.home.arpa` stays stable.
3. Update `docs/topology.md` and the host names in `CLAUDE.md`.

## Updating the base image

Debian publishes new cloud images regularly. To move to a newer one, copy the line for `debian-13-genericcloud-amd64-<version>.raw` from `SHA512SUMS` on [cloud.debian.org/images/cloud/trixie](https://cloud.debian.org/images/cloud/trixie/) into `vms.json`. Only new builds use it; running VMs update themselves with unattended-upgrades.
