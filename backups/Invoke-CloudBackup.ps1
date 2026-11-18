<#
.SYNOPSIS
  Runs one Kopia cloud-backup job from tasks.json (type "kopia") and records what happened,
  for the rack dashboard's Scheduled tasks screen. Started by its scheduled task in \Homelab\.

.DESCRIPTION
  Kopia encrypts everything here, before upload; the bucket only ever holds encrypted,
  randomly named pack files. The repository password never leaves this machine.

  Each run:
    1. Refuses (status "aborted") if the source is missing or EMPTY: with "keep N daily",
       N empty snapshots in a row would push every good snapshot out of retention.
    2. Connects to the S3-compatible bucket (Backblaze B2's S3 endpoint; needed for Object
       Lock). If the bucket holds no repository yet, creates one with Object Lock retention
       (COMPLIANCE, kopia.object_lock_days) and enables lock extension in maintenance.
    3. Applies the policy from tasks.json when it changed (retention, compression,
       Volume Shadow Copy for open files).
    4. Takes the snapshot; Kopia's own maintenance runs afterwards (expiring old snapshots,
       renewing locks).
  Secrets (B2 key, repository password) come from creds\<job>.json, encrypted to this
  machine's key by Set-CloudBackupSecrets.ps1. They are passed to Kopia in environment
  variables for this run only, Kopia is told not to save the password, and its connection
  file (which holds the B2 key) is deleted at the end of every run. The cache is kept.

  results\<job>.json keeps the last -KeepRuns runs; logs\ the newest -KeepRuns logs.
  Exit code: 0 ok, 1 warn, 2 failed/aborted, 3 already running. Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Job,
    [string]$Dir = (Join-Path $env:ProgramData 'HomelabBackups'),
    [int]$KeepRuns = 30
)
$ErrorActionPreference = 'Stop'
$Version = 'Invoke-CloudBackup.ps1/1'
. (Join-Path $PSScriptRoot 'MirrorCredential.ps1')
$ResultsDir, $LogsDir = (Join-Path $Dir 'results'), (Join-Path $Dir 'logs')
$ResultPath = Join-Path $ResultsDir "$Job.json"
$Work = Join-Path (Join-Path $Dir 'kopia') $Job        # cache, Kopia logs, policy fingerprint
foreach ($d in $ResultsDir, $LogsDir, $Work) { if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null } }

function Get-Epoch { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Save-Result($current, $run, [string]$destination) {
    $runs = @()
    if (Test-Path $ResultPath) { try { $runs = @((Get-Content -Raw $ResultPath | ConvertFrom-Json).runs) } catch { } }
    if ($run) { $runs = @($runs + $run) | Select-Object -Last $KeepRuns }
    $doc = [ordered]@{ job = $Job; kind = 'kopia'; source = $def.source; destination = $destination; agent = $Version; current = $current; runs = @($runs) }
    Set-Content -Path "$ResultPath.tmp" -Value (ConvertTo-Json -InputObject $doc -Depth 7 -Compress) -Encoding UTF8
    Move-Item -Force "$ResultPath.tmp" $ResultPath
}

# ---- job definition ----
$def = $null
try { $def = (Get-Content -Raw (Join-Path $Dir 'tasks.json') | ConvertFrom-Json).tasks.$Job } catch { }
if (-not $def -or $def.type -ne 'kopia' -or -not $def.source -or -not $def.kopia.bucket -or -not $def.kopia.endpoint) {
    Write-Warning "kopia job '$Job' (source, kopia.bucket, kopia.endpoint) not found in tasks.json"; exit 2
}
$k = $def.kopia
$destination = "b2://$($k.bucket)"
$lockDays = if ($k.object_lock_days) { [int]$k.object_lock_days } else { 30 }
$keepDaily = if ($k.retention.keep_daily) { [int]$k.retention.keep_daily } else { 30 }

$kopiaExe = $env:HOMELAB_TEST_KOPIA      # tests only
if (-not $kopiaExe) {
    $pin = Get-Content -Raw (Join-Path $Dir 'kopia.json') | ConvertFrom-Json
    $kopiaExe = Join-Path (Join-Path (Join-Path $Dir 'kopia\bin') $pin.version) 'kopia.exe'
}

function Invoke-Kopia([string[]]$KopiaArgs, [string]$Log) {
    # Runs kopia; stdout returned, stderr appended to $Log (UTF-8). Sets $script:kopiaExit.
    # Locally 'Continue': in PS 5.1 any stderr line from a native command is an error record,
    # which 'Stop' would turn into an exception.
    $ErrorActionPreference = 'Continue'
    $out = New-Object System.Collections.Generic.List[string]
    $err = New-Object System.Collections.Generic.List[string]
    & $kopiaExe @KopiaArgs 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $err.Add("$($_.Exception.Message)") } else { $out.Add("$_") }
    }
    $script:kopiaExit = $LASTEXITCODE
    Add-Content -Path $Log -Value (@("> kopia $($KopiaArgs[0..1] -join ' ') (exit $script:kopiaExit)") + $err) -Encoding UTF8
    return ($out -join "`n")
}

function Get-Tail([string]$Log, [int]$Lines = 4) {
    if (-not (Test-Path $Log)) { return '' }
    return ((Get-Content $Log | Where-Object { $_ -notmatch '^> kopia ' } | Select-Object -Last $Lines) -join ' ').Trim()
}

function Invoke-Job {
    $start = Get-Epoch
    $fail = { param($status, $msg, $log) [ordered]@{ start = $start; end = (Get-Epoch); exit_code = $script:kopiaExit; status = $status
                                                     message = $msg; summary = $null; error_count = 0; errors = @(); log = $log } }
    # ---- guards ----
    if (-not (Test-Path -LiteralPath $def.source -PathType Container)) { return & $fail 'aborted' "source $($def.source) is missing" $null }
    if (-not (Get-ChildItem -LiteralPath $def.source -Force | Select-Object -First 1)) {
        return & $fail 'aborted' "source $($def.source) is empty: refusing to snapshot (empty snapshots would push good ones out of retention)" $null
    }
    if (-not (Test-Path $kopiaExe)) { return & $fail 'aborted' "Kopia isn't installed yet (see kopia.json; the updater installs it once its SHA-256 is pinned)" $null }
    $credFile = Join-Path (Join-Path $Dir 'creds') "$Job.json"
    if (-not (Test-Path $credFile)) { return & $fail 'aborted' "secrets not set: run Set-CloudBackupSecrets.ps1 -Job $Job once on this machine" $null }

    # ---- secrets, for this process only ----
    $c = Get-Content -Raw $credFile | ConvertFrom-Json
    $rsa = Get-MirrorKey $Job ([int]$c.epoch)
    $env:AWS_ACCESS_KEY_ID = Unprotect-WithKey $rsa $c.key_id
    $env:AWS_SECRET_ACCESS_KEY = Unprotect-WithKey $rsa $c.app_key
    $env:KOPIA_PASSWORD = Unprotect-WithKey $rsa $c.password
    $env:KOPIA_CONFIG_PATH = Join-Path $Work 'repository.config'
    $env:KOPIA_LOG_DIR = Join-Path $Work 'kopia-logs'
    $env:KOPIA_CHECK_FOR_UPDATES = 'false'

    $log = Join-Path $LogsDir ('{0}-{1:yyyyMMdd-HHmmss}.log' -f $Job, (Get-Date))
    Save-Result ([ordered]@{ start = $start; log = $log }) $null $destination

    # ---- connect, or create the repository on first use ----
    $storage = @('s3', "--bucket=$($k.bucket)", "--endpoint=$($k.endpoint)")
    if ($k.prefix) { $storage += "--prefix=$($k.prefix)" }
    $common = @("--cache-directory=$(Join-Path $Work 'cache')", '--override-hostname=homeshare', '--override-username=system', '--no-persist-credentials')
    Invoke-Kopia (@('repository', 'connect') + $storage + $common) $log | Out-Null
    if ($script:kopiaExit -ne 0) {
        if ((Get-Content -Raw $log) -notmatch 'not initialized|repository not found|does not exist') {
            return & $fail 'failed' "couldn't connect to $destination`: $(Get-Tail $log)" $log
        }
        Invoke-Kopia (@('repository', 'create') + $storage + $common +
            @('--retention-mode=COMPLIANCE', "--retention-period=$($lockDays * 24)h")) $log | Out-Null
        if ($script:kopiaExit -ne 0) { return & $fail 'failed' "couldn't create the repository in $destination`: $(Get-Tail $log)" $log }
        # Renew Object Lock retention during full maintenance (must run more often than the lock period).
        Invoke-Kopia @('maintenance', 'set', '--extend-object-locks=true', '--full-interval=24h') $log | Out-Null
        if ($script:kopiaExit -ne 0) { return & $fail 'failed' "created the repository but couldn't enable lock extension: $(Get-Tail $log)" $log }
    }

    # ---- policy from tasks.json (only when it changed) ----
    $policy = @('policy', 'set', '--global', '--keep-latest=1', '--keep-hourly=0', "--keep-daily=$keepDaily", '--keep-weekly=0',
                '--keep-monthly=0', '--keep-annual=0', '--compression=zstd-fastest', '--enable-volume-shadow-copy=when-available')
    $neverCompress = @('.mp4', '.mkv', '.mov', '.avi', '.m4v', '.jpg', '.jpeg', '.png', '.heic', '.gif', '.webp', '.mp3', '.flac', '.aac',
                       '.zip', '.7z', '.rar', '.gz', '.iso')
    $policy += @($neverCompress | ForEach-Object { "--add-never-compress=$_" })
    $fpFile = Join-Path $Work 'policy.fp'
    $fp = Get-KeyFingerprint ($policy -join ' ')
    if (-not (Test-Path $fpFile) -or (Get-Content -Raw $fpFile).Trim() -ne $fp) {
        Invoke-Kopia $policy $log | Out-Null
        if ($script:kopiaExit -ne 0) { return & $fail 'failed' "couldn't apply the backup policy: $(Get-Tail $log)" $log }
        Set-Content -Path $fpFile -Value $fp
    }

    # ---- snapshot ----
    $logLinesBefore = @(Get-Content $log).Count
    $json = Invoke-Kopia @('snapshot', 'create', $def.source, '--json', '--no-progress') $log
    $snapExit = $script:kopiaExit
    $snap = $null
    try { $snap = $json | ConvertFrom-Json } catch { }
    $st = if ($snap) { $snap.stats } else { $null }
    $errorCount = if ($st) { [int]$st.errorCount + [int]$st.ignoredErrorCount } else { 0 }
    $errLines = @(if (Test-Path $log) { Get-Content $log | Select-Object -Skip $logLinesBefore | Where-Object { $_ -match '(?i)\berror\b' } | Select-Object -First 5 })
    $status = if ($snapExit -ne 0 -or -not $snap) { 'failed' } elseif ($errorCount -gt 0) { 'warn' } else { 'ok' }
    $message = if ($status -eq 'failed') { "snapshot failed: $(Get-Tail $log)" }
               else { '{0} changed or new file(s) uploaded{1}' -f [int]$st.nonCachedFiles, $(if ($errorCount) { ", $errorCount file(s) could not be read" } else { '' }) }
    return [ordered]@{
        start = $start; end = (Get-Epoch); exit_code = $snapExit; status = $status; message = $message
        summary = if ($st) { [ordered]@{
            files = [ordered]@{ total = [int64]$st.fileCount; copied = [int64]$st.nonCachedFiles; skipped = [int64]$st.cachedFiles
                                mismatch = 0; failed = $errorCount; extras = 0 }
            bytes = [ordered]@{ total = [int64]$st.totalSize; copied = $null; skipped = $null; mismatch = 0; failed = $null; extras = 0 } } } else { $null }
        error_count = $errorCount; errors = $errLines; log = $log
        snapshot_id = if ($snap) { "$($snap.id)" } else { $null }
    }
}

# ---- one run per job at a time ----
$lockPath = Join-Path $ResultsDir "$Job.lock"
try { $lock = [System.IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Write-Warning "job '$Job' is already running"; exit 3 }

$run = $null
try { $run = Invoke-Job }
catch { $run = [ordered]@{ start = (Get-Epoch); end = (Get-Epoch); exit_code = $null; status = 'failed'; message = "runner error: $($_.Exception.Message)"
                           summary = $null; error_count = 0; errors = @(); log = $null } }
finally {
    # Nothing secret outlives the run: Kopia's connection file holds the B2 key.
    foreach ($f in 'repository.config', 'repository.config.kopia-password') { Remove-Item -Force (Join-Path $Work $f) -ErrorAction SilentlyContinue }
    foreach ($v in 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'KOPIA_PASSWORD') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    if ($run) {
        Save-Result $null $run $destination
        Get-ChildItem -LiteralPath $LogsDir -Filter "$Job-*.log" | Sort-Object Name -Descending | Select-Object -Skip $KeepRuns |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
    $lock.Dispose()
    Remove-Item -Force $lockPath -ErrorAction SilentlyContinue
}
switch ($run.status) { 'ok' { exit 0 } 'warn' { exit 1 } default { exit 2 } }
