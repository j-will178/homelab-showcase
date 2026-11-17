<#
.SYNOPSIS
  Runs one robocopy mirror job from tasks.json and records what happened, for the rack
  dashboard's Scheduled tasks screen. Started by its scheduled task in \Homelab\.

.DESCRIPTION
  Source and destination come from tasks.json (installed from git by the rack agent's
  updater). Because a commit can change them, the destination must be PROVEN to be this
  job's mirror before /MIR may delete anything in it:
    - it holds a .homelab-mirror marker naming this job (written on the first run, and
      excluded from the mirror so it's never deleted); or
    - it doesn't exist yet / is empty (a new mirror: the marker is written); or
    - it has no marker but already looks like a mirror of the source (at least 90% of
      its top-level names also exist in the source): adopted once and marked.
  Anything else (a folder with other content, or another job's marker) is refused.

  Before robocopy runs, also refuses (status "aborted") if:
    - the source folder is missing or empty: /MIR from an empty source would empty
      the destination;
    - source and destination are the same folder or one is inside the other;
    - the destination's parent can't be reached;
    - this job is already running.

  Robocopy: /MIR /R:2 /W:5 /FP /NP /NDL /BYTES, logged to logs\<job>-<time>.log.
  Its exit code is a set of flags (1 copied, 2 extras removed, 4 mismatches,
  8 copy failures, 16 fatal); 0-3 = ok, 4-7 = warn, 8+ = failed.

  results\<job>.json keeps the last -KeepRuns runs (summary, not file lists); only the
  newest -KeepRuns logs are kept. Nothing grows without bound.

  Exit code for Task Scheduler: 0 ok, 1 warn, 2 failed/aborted, 3 skipped (already running).
  Windows PowerShell 5.1 compatible.

.EXAMPLE
  Invoke-Mirror.ps1 -Job homeshare-to-backup
  Invoke-Mirror.ps1 -Job homeshare-to-archive -WhatIf     # robocopy /L: list only, records nothing
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Job,
    [string]$Dir = (Join-Path $env:ProgramData 'HomelabBackups'),
    [int]$KeepRuns = 30,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$Version = 'Invoke-Mirror.ps1/1'
$ResultsDir = Join-Path $Dir 'results'
$LogsDir = Join-Path $Dir 'logs'
$ResultPath = Join-Path $ResultsDir "$Job.json"
foreach ($d in $ResultsDir, $LogsDir) { if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null } }

function Get-Epoch { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Read-Result {
    if (Test-Path $ResultPath) {
        try { return (Get-Content -Raw $ResultPath | ConvertFrom-Json) } catch { }
    }
    return $null
}

function Save-Result($current, $run, [string]$source, [string]$destination) {
    $old = Read-Result
    $runs = @()
    if ($old -and $old.runs) { $runs = @($old.runs) }
    if ($run) { $runs = @($runs + $run) | Select-Object -Last $KeepRuns }
    $doc = [ordered]@{ job = $Job; source = $source; destination = $destination; agent = $Version; current = $current; runs = @($runs) }
    $tmp = "$ResultPath.tmp"
    Set-Content -Path $tmp -Value (ConvertTo-Json -InputObject $doc -Depth 6 -Compress) -Encoding UTF8
    Move-Item -Force $tmp $ResultPath
}

function Get-FullPath([string]$p) { [System.IO.Path]::GetFullPath($p).TrimEnd('\', '/') }

# Robocopy's closing summary (with /BYTES, all plain numbers):
#                Total    Copied   Skipped  Mismatch    FAILED    Extras
#     Dirs :       120         2       118         0         0         1
function Read-RobocopySummary([string[]]$lines) {
    $sum = [ordered]@{}
    foreach ($line in $lines) {
        if ($line -match '^\s*(Dirs|Files|Bytes)\s*:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$') {
            $sum[$Matches[1].ToLower()] = [ordered]@{ total = [int64]$Matches[2]; copied = [int64]$Matches[3]; skipped = [int64]$Matches[4]
                                                     mismatch = [int64]$Matches[5]; failed = [int64]$Matches[6]; extras = [int64]$Matches[7] }
        }
    }
    return $sum
}

function Get-ExitMeaning([int]$code) {
    if ($code -ge 16) { return 'failed', 'robocopy fatal error (nothing was copied)' }
    $parts = @()
    if ($code -band 1) { $parts += 'files copied' }
    if ($code -band 2) { $parts += 'extra files removed' }
    if ($code -band 4) { $parts += 'mismatched files/folders' }
    if ($code -band 8) { $parts += 'some files could not be copied' }
    if (-not $parts) { $parts = @('no changes') }
    $status = if ($code -ge 8) { 'failed' } elseif ($code -ge 4) { 'warn' } else { 'ok' }
    return $status, ($parts -join ', ')
}

# ---- job definition ----
$defsPath = Join-Path $Dir 'tasks.json'
$def = $null
try { $def = (Get-Content -Raw $defsPath | ConvertFrom-Json).tasks.$Job } catch { }
if (-not $def -or -not $def.source -or -not $def.destination) {
    Write-Warning "job '$Job' (with source and destination) not found in $defsPath"
    exit 2
}
$MarkerName = '.homelab-mirror'
# Windows' own folders at the root of a drive: never mirrored, never deleted, not "content".
$SystemDirs = @('$RECYCLE.BIN', 'System Volume Information')
. (Join-Path $PSScriptRoot 'MirrorCredential.ps1')
$script:mapped = $null

function Connect-Share {
    # Jobs with share_access write to another machine's share as its service account.
    # The password is stored only encrypted to this machine's key (Sync-MirrorTasks.ps1).
    # Returns $null when connected, otherwise the reason it can't be.
    $sa = $def.share_access
    if (-not $sa) { return $null }
    if ($destination -notmatch '^(\\\\[^\\]+\\[^\\]+)') { return 'share_access needs a \\server\share destination' }
    $root = $Matches[1]
    $file = Join-Path (Join-Path $Dir 'creds') "$Job.json"
    if (-not (Test-Path $file)) { return "share credential not received yet from $($sa.provider) (it arrives with the next update / Deploy all)" }
    try {
        $c = Get-Content -Raw $file | ConvertFrom-Json
        $password = Unprotect-WithKey (Get-MirrorKey $Job ([int]$c.epoch)) $c.ciphertext
        Remove-SmbMapping -RemotePath $root -Force -ErrorAction SilentlyContinue
        New-SmbMapping -RemotePath $root -UserName $c.user -Password $password -Persistent $false -ErrorAction Stop | Out-Null
        $script:mapped = $root
    } catch { return "couldn't connect to ${root} as $($c.user): $($_.Exception.Message)" }
    return $null
}

function Test-Destination([string]$src, [string]$dst) {
    # $null if /MIR into $dst is allowed (writing the marker when it's new or adopted),
    # otherwise the reason to refuse.
    $marker = Join-Path $dst $MarkerName
    if (Test-Path -LiteralPath $marker) {
        $owner = (Get-Content -Raw -LiteralPath $marker).Trim()
        if ($owner -eq $Job) { return $null }
        return "destination $dst belongs to job '$owner', not '$Job'"
    }
    $skip = @($SystemDirs | ForEach-Object { $_.ToLower() })
    $entries = @()
    if (Test-Path -LiteralPath $dst) { $entries = @(Get-ChildItem -LiteralPath $dst -Force -Name | Where-Object { $_.ToLower() -notin $skip }) }
    if ($entries.Count -gt 0) {
        $inSource = @{}
        foreach ($n in (Get-ChildItem -LiteralPath $src -Force -Name)) { $inSource[$n.ToLower()] = $true }
        $other = @($entries | Where-Object { -not $inSource[$_.ToLower()] })
        $matching = $entries.Count - $other.Count
        $counts = "$matching of $($entries.Count) top-level names match the source"
        if ($def.adopt_existing -eq $true) {
            $script:adopted = "adopted existing destination because tasks.json sets adopt_existing ($counts); remove that setting now"
        } elseif ($matching / $entries.Count -lt 0.9) {
            $shown = (@($other | Select-Object -First 5) | ForEach-Object { "'$_'" }) -join ', '
            if ($other.Count -gt 5) { $shown += ", ... ($($other.Count - 5) more)" }
            return "destination $dst has other content ($counts; not in the source: $shown) and no $MarkerName marker: refusing to mirror into it. If it IS this job's mirror, set ""adopt_existing"": true on the job in tasks.json once."
        } else {
            $script:adopted = "adopted existing mirror ($counts)"
        }
    }
    if (-not $WhatIf) {
        if (-not (Test-Path -LiteralPath $dst)) { New-Item -ItemType Directory -Force -Path $dst | Out-Null }
        Set-Content -LiteralPath $marker -Value $Job -Encoding ASCII
        (Get-Item -LiteralPath $marker -Force).Attributes = 'Hidden'
    }
    return $null
}
$source, $destination = [string]$def.source, [string]$def.destination

# ---- one run per job at a time ----
$lockPath = Join-Path $ResultsDir "$Job.lock"
try { $lock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Write-Warning "job '$Job' is already running"; exit 3 }

function Invoke-Job {
    # ---- guards ----
    $abort = Connect-Share
    if ($abort) { }
    elseif (-not (Test-Path -LiteralPath $source -PathType Container)) { $abort = "source $source is missing" }
    elseif (-not (Get-ChildItem -LiteralPath $source -Force | Select-Object -First 1)) { $abort = "source $source is empty: refusing to mirror (it would empty the destination)" }
    else {
        $s, $d = (Get-FullPath $source), (Get-FullPath $destination)
        if ($s -eq $d -or $d.StartsWith("$s\") -or $s.StartsWith("$d\") -or $d.StartsWith("$s/") -or $s.StartsWith("$d/")) {
            $abort = 'source and destination overlap'
        } else {
            # The folder that must exist: the share itself for \\server\share, else the parent.
            $parent = if ($d -match '^\\\\[^\\]+\\[^\\]+$') { $d } else { Split-Path -Parent $d }
            if ($parent -and -not (Test-Path -LiteralPath $parent)) { $abort = "destination $parent can't be reached" }
            else { $abort = Test-Destination $source $destination }
        }
    }
    if ($abort) {
        Write-Warning "aborted: $abort"
        return [ordered]@{ start = $start; end = (Get-Epoch); exit_code = $null; status = 'aborted'; message = $abort; summary = $null; error_count = 0; errors = @(); log = $null }
    }

    # ---- run ----
    $log = Join-Path $LogsDir ('{0}-{1:yyyyMMdd-HHmmss}.log' -f $Job, (Get-Date))
    $rcArgs = @($source, $destination, '/MIR', '/XF', $MarkerName, '/XD') + $SystemDirs + @('/R:2', '/W:5', '/FP', '/NP', '/NDL', '/BYTES', "/UNILOG:$log")
    if ($WhatIf) { $rcArgs += '/L' }
    else { Save-Result ([ordered]@{ start = $start; log = $log }) $null $source $destination }

    & robocopy @rcArgs | Out-Null
    $code = $LASTEXITCODE

    $lines = @()
    if (Test-Path -LiteralPath $log) { $lines = @(Get-Content -LiteralPath $log -Encoding Unicode) }
    $errLines = @($lines | Where-Object { $_ -match '\sERROR\s+\d+\s' })
    $status, $meaning = Get-ExitMeaning $code
    if ($script:adopted) { $meaning = "$meaning; $($script:adopted)" }
    return [ordered]@{
        start = $start; end = (Get-Epoch); exit_code = $code; status = $status; message = $meaning
        summary = (Read-RobocopySummary $lines); error_count = $errLines.Count
        errors = @($errLines | Select-Object -First 5 | ForEach-Object { ($_ -replace '^\s*\S+\s+\S+\s+', '').Trim() })
        log = $log
    }
}

$script:adopted = $null
$start = Get-Epoch
$run = $null
try {
    $run = Invoke-Job
    if ($WhatIf) { Write-Host "WhatIf (robocopy /L): $($run.message), exit $($run.exit_code). Log: $($run.log)" }
}
finally {
    if ($run -and -not $WhatIf) {
        Save-Result $null $run $source $destination
        # keep only the newest $KeepRuns logs for this job
        Get-ChildItem -LiteralPath $LogsDir -Filter "$Job-*.log" | Sort-Object Name -Descending |
            Select-Object -Skip $KeepRuns | Remove-Item -Force -ErrorAction SilentlyContinue
    } elseif (-not $run -and -not $WhatIf) {
        # crashed before producing a result: record that instead of leaving "running"
        Save-Result $null ([ordered]@{ start = $start; end = (Get-Epoch); exit_code = $null; status = 'failed'; message = 'runner error (see Task Scheduler history)'; summary = $null; error_count = 0; errors = @(); log = $null }) $source $destination
    }
    if ($script:mapped) { Remove-SmbMapping -RemotePath $script:mapped -Force -ErrorAction SilentlyContinue }
    $lock.Dispose()
    Remove-Item -Force $lockPath -ErrorAction SilentlyContinue
}

if ($WhatIf) { exit 0 }
switch ($run.status) { 'ok' { exit 0 } 'warn' { exit 1 } default { exit 2 } }
