<#
.SYNOPSIS
  Scheduled-task entry point on Windows (runs every minute): keeps the metrics loop
  running; GitHub update + SMART report every 15 minutes.

.DESCRIPTION
  1. Pull: fetch the latest `main` into %ProgramData%\RackAgent\repo (sparse checkout of
     dashboard/agents only, read-only deploy key).
  2. Check: every agent script that changed must parse cleanly (PowerShell parser).
  3. Install: copy changed scripts into %ProgramData%\RackAgent; the previous copy is kept
     as <name>.prev. A script that fails the check is NOT installed.
  4. Run: make sure the scheduled task "Rack Dashboard metrics" exists and is running
     (Send-Metrics.ps1 -LoopSeconds 10, a sample every 10 s; registered here the first
     time). Send-SmartReport.ps1 runs when a full run is due (every 15 min), whatever
     the update outcome. A failed fetch (GitHub unreachable, key problem) or a broken
     commit never stops reporting.
  The metrics loop exits by itself when Send-Metrics.ps1 is updated (and once a day);
  this script, every minute, starts it again. It also makes sure its own scheduled
  task repeats every minute (older installs ran every 15), so new agents roll out
  without touching the machine.

  Installed by Install-SmartAgent.ps1 -AutoUpdate. Windows PowerShell 5.1 compatible.
  Log: %ProgramData%\RackAgent\agent.log
#>
[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:ProgramData 'RackAgent'),
    [string]$Git = 'git',
    [string]$Branch = 'main',
    [switch]$NoRun,         # update only (used by the installer and tests)
    [switch]$AfterSelfUpdate   # set when an older copy of this script just installed this one
)
$ErrorActionPreference = 'Stop'
$LogPath = Join-Path $Dir 'agent.log'
$Repo = Join-Path $Dir 'repo'
$Managed = @('Send-SmartReport.ps1', 'Send-Metrics.ps1', 'Invoke-RackAgent.ps1')   # files this updater keeps current
# backups/ (repo) -> %ProgramData%\HomelabBackups, on machines that backups/tasks.json gives a
# task (or that had one). After installing, Sync-MirrorTasks.ps1 makes the \Homelab\ tasks match
# tasks.json, so a merged PR + Deploy all creates, changes or removes them: no local setup.
$BackupsDir = Join-Path $env:ProgramData 'HomelabBackups'
$BackupsManaged = @('Invoke-Mirror.ps1', 'Sync-MirrorTasks.ps1', 'MirrorCredential.ps1', 'tasks.json',
                    'Invoke-CloudBackup.ps1', 'Set-CloudBackupSecrets.ps1', 'kopia.json')
# vms/ (repo) -> %ProgramData%\HomelabVMs, on machines that vms/vms.json gives a VM. Sync-HyperV.ps1
# then creates any missing VM (never changes or deletes one): see vms/README.md.
$VmsDir = Join-Path $env:ProgramData 'HomelabVMs'
$VmsManaged = @('Sync-HyperV.ps1', 'vms.json', 'cloud-ops.user-data')
$TaskName = 'Rack Dashboard SMART agent'
$Stamp = Join-Path $Dir 'last-full-run.txt'
$FullEveryMinutes = 15
$MetricsTask = 'Rack Dashboard metrics'
$StatusFile = Join-Path $Dir 'deploy-status.json'   # shown on the dashboard's Deployments screen
$MetricsEverySeconds = 10

function Write-Log([string]$Message) {
    $line = '{0:u} [update] {1}' -f (Get-Date).ToUniversalTime(), $Message
    Write-Host $line
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Invoke-Git([string[]]$GitArgs) {
    # The repo is created by an admin during install but this runs as SYSTEM; git refuses
    # repos owned by another user unless the path is marked safe (protected config only).
    $ErrorActionPreference = 'Continue'
    $out = & $Git -c "safe.directory=$($Repo -replace '\\','/')" -C $Repo @GitArgs 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed: $($out.Trim())" }
    return $out.Trim()
}

function Test-PowerShellFile([string]$Path) {
    # .json must parse, .ps1/.psm1 must parse as PowerShell; other files (templates) are copied as-is.
    if ($Path -like '*.json') {
        try { Get-Content -Raw $Path | ConvertFrom-Json | Out-Null; return $true } catch { return $false }
    }
    if ($Path -notlike '*.ps1' -and $Path -notlike '*.psm1') { return $true }
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$errors)
    return @($errors).Count -eq 0
}

function Write-DeployStatus([string]$Result, [string]$Message, [switch]$Changed) {
    try {
        $old = $null
        if (Test-Path $StatusFile) { try { $old = Get-Content -Raw $StatusFile | ConvertFrom-Json } catch { } }
        $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $commit = $null; $subject = $null; $committed = $null; $gitVer = $null
        if (Test-Path (Join-Path $Repo '.git')) {
            try {
                $commit = Invoke-Git @('rev-parse', 'HEAD')
                $ct, $subject = (Invoke-Git @('log', '-1', '--format=%ct%x09%s', 'HEAD')) -split "`t", 2
                $committed = [int64]$ct
            } catch { }
        }
        try { $gitVer = ((& $Git --version) -split ' ')[2] } catch { }
        $st = [ordered]@{
            checked_at = $now; changed_at = if ($Changed) { $now } elseif ($old) { $old.changed_at } else { $null }
            every_s = $FullEveryMinutes * 60; result = $Result; message = $Message; branch = $Branch
            commit = $commit; subject = $subject; committed_at = $committed; git = $gitVer
        }
        Set-Content -Path $StatusFile -Value (ConvertTo-Json -InputObject $st -Compress) -Encoding UTF8
    } catch { Write-Log "could not write deploy status: $($_.Exception.Message)" }
}

function Set-DeployPhase([string]$Phase) {
    # While updating: "running since" + current step, shown on the Deployments screen
    # (forwarded by the metrics loop every 10 s). Write-DeployStatus replaces the whole
    # file at the end, which clears both. Never fatal.
    try {
        $st = [ordered]@{}
        if (Test-Path $StatusFile) { foreach ($p in (Get-Content -Raw $StatusFile | ConvertFrom-Json).PSObject.Properties) { $st[$p.Name] = $p.Value } }
        if (-not $script:runSince) { $script:runSince = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
        $st.running_since = $script:runSince
        $st.phase = $Phase
        Set-Content -Path $StatusFile -Value (ConvertTo-Json -InputObject $st -Compress) -Encoding UTF8
    } catch { }
}

function Update-Agent {
    if (-not (Test-Path (Join-Path $Repo '.git'))) { Write-Log 'no repo checkout: auto-update not set up, skipping'; Write-DeployStatus 'failed' 'auto-update not set up'; return }
    $before = Invoke-Git @('rev-parse', 'HEAD')
    Set-DeployPhase 'pulling from GitHub'
    Invoke-Git @('fetch', '--depth', '1', 'origin', $Branch) | Out-Null
    $after = Invoke-Git @('rev-parse', 'FETCH_HEAD')
    if ($after -ne $before) {
        Invoke-Git @('reset', '--hard', '--quiet', 'FETCH_HEAD') | Out-Null
        Write-Log "repo $($before.Substring(0,7)) -> $($after.Substring(0,7))"
    }
    # The checkout started as dashboard/agents only; also bring in backups/ (small).
    if (-not (Test-Path (Join-Path $Repo 'backups'))) {
        try { Invoke-Git @('sparse-checkout', 'add', 'backups') | Out-Null; Write-Log 'checkout now includes backups/' }
        catch { Write-Log "could not add backups/ to the checkout: $($_.Exception.Message)" }
    }
    if (-not (Test-Path (Join-Path $Repo 'vms'))) {
        try { Invoke-Git @('sparse-checkout', 'add', 'vms') | Out-Null; Write-Log 'checkout now includes vms/' }
        catch { Write-Log "could not add vms/ to the checkout: $($_.Exception.Message)" }
    }
    $files = @($Managed | ForEach-Object { @{ name = $_; src = (Join-Path (Join-Path (Join-Path $Repo 'dashboard') 'agents') $_); dst = (Join-Path $Dir $_) } })
    $hostName = $null
    try { $hostName = (Get-Content -Raw (Join-Path $Dir 'config.json') | ConvertFrom-Json).name } catch { }
    if (-not $hostName) { $hostName = $env:COMPUTERNAME.ToLower() }
    $hasTasks = Test-Path (Join-Path $BackupsDir 'tasks.json')    # had tasks before: keep syncing (also removes them)
    if (-not $hasTasks) {
        try {
            $defs = (Get-Content -Raw (Join-Path (Join-Path $Repo 'backups') 'tasks.json') | ConvertFrom-Json).tasks
            $hasTasks = [bool]@($defs.PSObject.Properties | Where-Object {
                "$($_.Value.host)".ToLower() -eq $hostName.ToLower() -or "$($_.Value.share_access.provider)".ToLower() -eq $hostName.ToLower() }).Count
        } catch { }
    }
    if ($hasTasks) {
        if (-not (Test-Path $BackupsDir)) { New-Item -ItemType Directory -Force -Path $BackupsDir | Out-Null }
        $files += @($BackupsManaged | ForEach-Object { @{ name = "backups/$_"; src = (Join-Path (Join-Path $Repo 'backups') $_); dst = (Join-Path $BackupsDir $_) } })
    }
    $hasVms = Test-Path (Join-Path $VmsDir 'vms.json')
    if (-not $hasVms) {
        try {
            $vdefs = (Get-Content -Raw (Join-Path (Join-Path $Repo 'vms') 'vms.json') | ConvertFrom-Json).vms
            $hasVms = [bool]@($vdefs.PSObject.Properties | Where-Object { "$($_.Value.host)".ToLower() -eq $hostName.ToLower() }).Count
        } catch { }
    }
    if ($hasVms) {
        if (-not (Test-Path $VmsDir)) { New-Item -ItemType Directory -Force -Path $VmsDir | Out-Null }
        $files += @($VmsManaged | ForEach-Object { @{ name = "vms/$_"; src = (Join-Path (Join-Path $Repo 'vms') $_); dst = (Join-Path $VmsDir $_) } })
    }
    $rejected = @(); $installed = $false
    foreach ($f in $files) {
        $name, $src, $dst = $f.name, $f.src, $f.dst
        if (-not (Test-Path $src)) { continue }
        if ((Test-Path $dst) -and ((Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash)) { continue }
        if (-not (Test-PowerShellFile $src)) {
            Write-Log "$name at $($after.Substring(0,7)) has parse errors: NOT installed, keeping current copy"
            $rejected += $name
            continue
        }
        if (Test-Path $dst) { Copy-Item -Force $dst "$dst.prev" }
        Copy-Item -Force $src $dst
        Write-Log "installed $name from $($after.Substring(0,7))"
        $installed = $true
        if ($name -eq 'Invoke-RackAgent.ps1') { $script:selfUpdated = $true }
    }
    # Scheduled tasks from backups/tasks.json (every update, so hand edits are put back too).
    if ($hasTasks) { Set-DeployPhase 'applying scheduled tasks' }
    $taskNote, $taskError, $pendingNote = '', $null, ''
    if ($hasTasks -and (Test-Path (Join-Path $BackupsDir 'Sync-MirrorTasks.ps1'))) {
        try {
            $r = & (Join-Path $BackupsDir 'Sync-MirrorTasks.ps1') -Dir $BackupsDir -HostName $hostName -AgentDir $Dir
            foreach ($j in @($r.updated)) { Write-Log "task Mirror $j registered from tasks.json" }
            foreach ($j in @($r.removed)) { Write-Log "task $j removed (no longer in tasks.json)" }
            foreach ($j in @($r.skipped)) { Write-Log "task skipped: $j" }
            foreach ($e in @($r.errors)) { Write-Log "task error: $e" }
            foreach ($w in @($r.pending)) { Write-Log "task pending: $w" }
            $parts = @()
            if (@($r.updated).Count) { $parts += "$(@($r.updated).Count) updated" }
            if (@($r.removed).Count) { $parts += "$(@($r.removed).Count) removed" }
            $dot = " $([char]0x00B7) "   # keep this file ASCII: Windows PowerShell 5.1 reads it as ANSI
            if ($parts) { $taskNote = "${dot}tasks: $($parts -join ', ')" }
            if (@($r.pending).Count) { $pendingNote = "$dot$(@($r.pending) -join '; ')" }
            if (@($r.errors).Count) { $taskError = "tasks.json: $(@($r.errors) -join '; ')" }
        } catch { $taskError = "task sync failed: $($_.Exception.Message)" }
    }
    # VMs from vms/vms.json: creates missing ones only (a first build downloads the image, minutes).
    if ($hasVms -and (Test-Path (Join-Path $VmsDir 'Sync-HyperV.ps1'))) {
        Set-DeployPhase 'applying VMs'
        $dot = " $([char]0x00B7) "
        try {
            $r = & (Join-Path $VmsDir 'Sync-HyperV.ps1') -HostName $hostName -Dir $VmsDir -AgentDir $Dir
            foreach ($j in @($r.updated)) { Write-Log $j }
            foreach ($w in @($r.pending)) { Write-Log "VM pending: $w" }
            foreach ($e in @($r.errors)) { Write-Log "VM error: $e" }
            if (@($r.updated).Count) { $taskNote += "${dot}VMs: $(@($r.updated) -join '; ')" }
            if (@($r.pending).Count) { $pendingNote += "$dot$(@($r.pending) -join '; ')" }
            if (@($r.errors).Count) { $vmErr = "vms.json: $(@($r.errors) -join '; ')"; $taskError = if ($taskError) { "$taskError; $vmErr" } else { $vmErr } }
        } catch { $vmErr = "VM sync failed: $($_.Exception.Message)"; $taskError = if ($taskError) { "$taskError; $vmErr" } else { $vmErr } }
    }

    if ($rejected.Count) { Write-DeployStatus 'rejected' "not installed (failed checks): $($rejected -join ', ')" }
    elseif ($taskError) { Write-DeployStatus 'failed' $taskError -Changed:($installed -or $after -ne $before) }
    elseif ($installed -or $after -ne $before -or $taskNote) { Write-DeployStatus 'ok' "updated to $($after.Substring(0,7))$taskNote$pendingNote" -Changed }
    else { Write-DeployStatus 'ok' "up to date$pendingNote" }
}

function Set-TaskEveryMinute {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    $changed = $false
    foreach ($tr in $task.Triggers) {
        if ($tr.Repetition -and $tr.Repetition.Interval -and $tr.Repetition.Interval -ne 'PT1M') {
            $tr.Repetition.Interval = 'PT1M'; $changed = $true
        }
    }
    if ($changed) {
        Set-ScheduledTask -TaskName $TaskName -Trigger $task.Triggers | Out-Null
        Write-Log 'scheduled task now repeats every minute'
    }
}

function Start-MetricsLoop {
    $metrics = Join-Path $Dir 'Send-Metrics.ps1'
    if (-not (Test-Path $metrics)) { return }
    $task = Get-ScheduledTask -TaskName $MetricsTask -ErrorAction SilentlyContinue
    if (-not $task) {
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$metrics`" -Dir `"$Dir`" -LoopSeconds $MetricsEverySeconds")
        $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        $runAs = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $MetricsTask -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $settings -Principal $runAs `
            -Description "Rack dashboard: compute metrics every $MetricsEverySeconds s. Kept running by '$TaskName'." | Out-Null
        Write-Log "registered scheduled task '$MetricsTask' (metrics every $MetricsEverySeconds s)"
        $task = Get-ScheduledTask -TaskName $MetricsTask
    }
    if ($task.State -ne 'Running') { Start-ScheduledTask -TaskName $MetricsTask }
}

# A full run (update + SMART) is due every 15 minutes; metrics run every minute.
$script:selfUpdated = $false
$due = $NoRun -or $AfterSelfUpdate -or -not (Test-Path $Stamp) -or ((Get-Date) - (Get-Item $Stamp).LastWriteTime).TotalMinutes -ge ($FullEveryMinutes - 0.5)
if ($due) {
    try { Update-Agent } catch {
        Write-Log "update FAILED (running the current copy): $($_.Exception.Message)"
        Write-DeployStatus 'failed' ($_.Exception.Message -replace '\s+', ' ')
    }
    try { Set-TaskEveryMinute } catch { Write-Log "could not retime scheduled task: $($_.Exception.Message)" }
}

# This run's code is the OLD version if the update just replaced this script: run the new
# one now (once), so a single update / Deploy all applies everything the new commit adds.
if ($script:selfUpdated -and -not $AfterSelfUpdate) {
    Write-Log 'updater replaced itself: re-running as the new version'
    $again = @{ Dir = $Dir; Git = $Git; Branch = $Branch; AfterSelfUpdate = $true }
    if ($NoRun) { $again.NoRun = $true }
    & (Join-Path $Dir 'Invoke-RackAgent.ps1') @again
    exit $LASTEXITCODE
}

if (-not $NoRun) {
    $rc = 0
    try { Start-MetricsLoop } catch { Write-Log "could not start metrics loop: $($_.Exception.Message)"; $rc = 1 }
    if ($due) {
        & (Join-Path $Dir 'Send-SmartReport.ps1') -Dir $Dir
        if ($LASTEXITCODE) { $rc = $LASTEXITCODE }
        Set-Content -Path $Stamp -Value (Get-Date).ToUniversalTime().ToString('o')
    }
    exit $rc
}
