<#
.SYNOPSIS
  Makes this machine's mirror tasks in Task Scheduler's \Homelab\ folder match
  backups/tasks.json. Run by the rack agent's updater (as SYSTEM) on every update, so a
  merged PR + "Deploy all" is all it takes to change a task.

.DESCRIPTION
  For each entry in tasks.json whose "host" is this machine (it must have "source" and
  "destination"; Invoke-Mirror.ps1 checks the destination is really this job's mirror):
    - the task \Homelab\Mirror <job> is built from the definition: runs as SYSTEM,
      starts Invoke-Mirror.ps1 -Job <job>, schedule / time limit / enabled from the file;
    - it's (re-)registered when the definition changed OR the task was changed/removed by
      hand (git is the source of truth; to disable a task, set "enabled": false).
  Tasks in \Homelab\ that this script created for this host name but that are no longer
  defined for it are removed. Tasks it didn't create (or created under another host name)
  are never touched.

  Schedule formats (local time):
    "daily":   { "at": "02:00", "every_days": 1 }
    "weekly":  { "days": ["mon", "thu"], "at": "02:00" }
    "monthly": { "days": [1, 15, "last"], "at": "03:00" }

  Share credential (jobs with "share_access", whose destination is another machine's
  share; see README "Share credential"). Fully automatic, both ends run this script:
    - on the job's host (consumer): keep an RSA key in the machine key store, publish the
      PUBLIC key via the dashboard, and collect the encrypted password once it's there;
    - on share_access.provider: create the local account (random password, no local/RDP
      logon), give it Change on the share and Modify on its folder, and post the password
      encrypted to the consumer's key. The first key seen is pinned; a different key is
      refused unless share_access.key_epoch was bumped in tasks.json.

  Returns an object: updated / unchanged / removed / skipped / pending / errors (lists of
  job names or messages). Windows PowerShell 5.1 compatible.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostName,
    [string]$Dir = (Join-Path $env:ProgramData 'HomelabBackups'),
    [string]$AgentDir = (Join-Path $env:ProgramData 'RackAgent'),
    [int]$WaitSeconds = 150,    # how long one end waits for the other during a new handoff
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$Version = 'Sync-MirrorTasks.ps1/1'
$TaskPath = '\Homelab\'
$Marker = 'homelab-managed'
$StatePath = Join-Path $Dir 'tasks-state.json'
$ProviderStatePath = Join-Path $Dir 'share-access-state.json'
. (Join-Path $PSScriptRoot 'MirrorCredential.ps1')

function ConvertTo-ClockTime([string]$at) {
    if ("$at" -notmatch '^([01]?\d|2[0-3]):([0-5]\d)$') { throw "bad time '$at' (use HH:mm)" }
    return '{0:D2}:{1}:00' -f [int]$Matches[1], $Matches[2]
}

function New-TriggerXml($schedule) {
    $kinds = @($schedule.PSObject.Properties.Name)
    if ($kinds.Count -ne 1) { throw 'schedule needs exactly one of: daily, weekly, monthly' }
    $kind = $kinds[0]; $s = $schedule.$kind
    if ($kind -notin 'daily', 'weekly', 'monthly') { throw "unknown schedule '$kind' (use daily, weekly or monthly)" }
    $start = "2026-01-01T$(ConvertTo-ClockTime $s.at)"
    switch ($kind) {
        'daily' {
            $n = if ($s.every_days) { [int]$s.every_days } else { 1 }
            if ($n -lt 1 -or $n -gt 365) { throw 'daily.every_days must be 1-365' }
            $body = "<ScheduleByDay><DaysInterval>$n</DaysInterval></ScheduleByDay>"
        }
        'weekly' {
            $names = @{ mon = 'Monday'; tue = 'Tuesday'; wed = 'Wednesday'; thu = 'Thursday'; fri = 'Friday'; sat = 'Saturday'; sun = 'Sunday' }
            $days = @(foreach ($d in @($s.days)) {
                $k = "$d".ToLower(); if ($k.Length -gt 3) { $k = $k.Substring(0, 3) }
                if (-not $names[$k]) { throw "weekly: bad day '$d'" }
                "<$($names[$k]) />"
            })
            if (-not $days) { throw 'weekly needs "days"' }
            $body = "<ScheduleByWeek><DaysOfWeek>$(($days | Select-Object -Unique) -join '')</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        }
        'monthly' {
            $days = @(foreach ($d in @($s.days)) {
                if ("$d" -eq 'last') { '<Day>Last</Day>' }
                elseif ("$d" -match '^\d+$' -and [int]$d -ge 1 -and [int]$d -le 31) { "<Day>$([int]$d)</Day>" }
                else { throw "monthly: bad day '$d' (1-31 or ""last"")" }
            })
            if (-not $days) { throw 'monthly needs "days"' }
            $months = (('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December') | ForEach-Object { "<$_ />" }) -join ''
            $body = "<ScheduleByMonth><DaysOfMonth>$($days -join '')</DaysOfMonth><Months>$months</Months></ScheduleByMonth>"
        }
        default { throw "unknown schedule '$kind' (use daily, weekly or monthly)" }
    }
    return "<CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled>$body</CalendarTrigger>"
}

function New-TaskXml([string]$Job, $Def, [string]$Fingerprint) {
    $esc = { param($t) [Security.SecurityElement]::Escape([string]$t) }
    $hours = if ($Def.time_limit_hours) { [int]$Def.time_limit_hours } else { 24 }
    if ($hours -lt 1 -or $hours -gt 720) { throw 'time_limit_hours must be 1-720' }
    $enabled = if ($Def.enabled -eq $false) { 'false' } else { 'true' }
    $runner = Join-Path $Dir 'Invoke-Mirror.ps1'
    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`" -Job $Job"
    $desc = "$($Def.description) [$Marker host:$($HostName.ToLower()) def:$Fingerprint; from backups/tasks.json, changes here are overwritten]".Trim()
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>homelab backups/tasks.json</Author><Description>$(& $esc $desc)</Description></RegistrationInfo>
  <Triggers>$(New-TriggerXml $Def.schedule)</Triggers>
  <Principals><Principal id="Author"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT$($hours)H</ExecutionTimeLimit>
    <Enabled>$enabled</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>$(& $esc $arguments)</Arguments></Exec></Actions>
</Task>
"@
}

function Get-Hash([string]$text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return (-join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($text)) | ForEach-Object { $_.ToString('x2') })).Substring(0, 12) }
    finally { $sha.Dispose() }
}

function Get-Exported([string]$name) {
    # Fingerprint of the task as Task Scheduler stores it now (catches hand edits).
    try { return Get-Hash (Export-ScheduledTask -TaskPath $TaskPath -TaskName $name) } catch { return $null }
}

$result = [ordered]@{ updated = @(); unchanged = @(); removed = @(); skipped = @(); pending = @(); errors = @() }

function Wait-For([scriptblock]$Probe, [int]$Seconds) {
    # Both machines get the same update / Deploy all within ~10 s and run within ~1 min,
    # so during a NEW handoff each end waits briefly for the other: one press completes it.
    $end = (Get-Date).AddSeconds($Seconds)
    while ($true) {
        $r = & $Probe
        if ($r -or (Get-Date) -ge $end) { return $r }
        Start-Sleep -Seconds 5
    }
}

function Test-NewHandoff([string]$StateKey, [string]$Marker) {
    # True during the first 15 min after $Marker (a key fingerprint / job) first appeared,
    # so a host that's down doesn't make every later update wait.
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $st = $pstate[$StateKey]
    if (-not $st -or $st.marker -ne $Marker) { $st = [ordered]@{ marker = $Marker; since = $now }; $pstate[$StateKey] = $st }
    return ($now - [int64]$st.since) -lt 900
}

function Protect-Folder([string]$Path) {
    # SYSTEM + Administrators only, nothing inherited.
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
    if ($env:HOMELAB_TEST_KEYDIR) { return }
    & icacls $Path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
}

function Sync-ConsumerCredential([string]$Job, $Def, $Api) {
    # This machine runs the job: publish our public key, collect the encrypted password.
    $sa = $Def.share_access
    $epoch = if ($sa.key_epoch) { [int]$sa.key_epoch } else { 1 }
    $rsa = Get-MirrorKey $Job $epoch
    $pub = $rsa.ToXmlString($false)
    $fp = Get-KeyFingerprint $pub
    Invoke-CredsApi $Api 'POST' 'key' $Job @{ id = $Job; host = $HostName; pubkey = $pub } | Out-Null
    $probe = { $x = Invoke-CredsApi $Api 'GET' 'secret' $Job; if ($x -and $x.key_fp -eq $fp) { $x } }
    $sec = & $probe
    if (-not $sec -and (Test-NewHandoff "consumer/$Job" $fp)) { $sec = Wait-For $probe $WaitSeconds }
    if (-not $sec) { return "waiting for the share credential from $($sa.provider)" }
    $dir = Join-Path $Dir 'creds'
    Protect-Folder $dir
    $file = Join-Path $dir "$Job.json"
    $new = ConvertTo-Json -Compress -InputObject ([ordered]@{ user = $sec.user; ciphertext = $sec.ciphertext; key_fp = $sec.key_fp; epoch = $epoch })
    if (-not (Test-Path $file) -or (Get-Content -Raw $file).Trim() -ne $new) {
        Unprotect-WithKey $rsa $sec.ciphertext | Out-Null     # proves it's for our key before storing it
        Set-Content -Path $file -Value $new -Encoding UTF8
        return 'received'
    }
    return $null
}

function Grant-ShareWrite([string]$Share, [string]$Account) {
    $sh = Get-SmbShare -Name $Share -ErrorAction Stop
    $have = @(Get-SmbShareAccess -Name $Share | Where-Object { "$($_.AccountName)" -like "*\$Account" -and "$($_.AccessControlType)" -eq 'Allow' -and "$($_.AccessRight)" -in 'Change', 'Full' })
    if (-not $have) { Grant-SmbShareAccess -Name $Share -AccountName $Account -AccessRight Change -Force | Out-Null }
    $modify = [Security.AccessControl.FileSystemRights]::Modify
    $ok = @((Get-Acl -LiteralPath $sh.Path).Access | Where-Object {
        "$($_.IdentityReference)" -like "*\$Account" -and "$($_.AccessControlType)" -eq 'Allow' -and ($_.FileSystemRights -band $modify) -eq $modify -and
        "$($_.InheritanceFlags)" -match 'ContainerInherit' -and "$($_.InheritanceFlags)" -match 'ObjectInherit' })
    if (-not $ok) {
        & icacls $sh.Path /grant "${Account}:(OI)(CI)M" /Q | Out-Null     # inherited by everything below
        if ($LASTEXITCODE) { throw "icacls on $($sh.Path) failed ($LASTEXITCODE)" }
    }
}

function Set-ServiceAccount([string]$Account, [string]$Password) {
    $sec = ConvertTo-SecureString $Password -AsPlainText -Force
    if (Get-LocalUser -Name $Account -ErrorAction SilentlyContinue) {
        Set-LocalUser -Name $Account -Password $sec
    } else {
        New-LocalUser -Name $Account -Password $sec -PasswordNeverExpires -UserMayNotChangePassword `
            -Description 'Homelab mirror share access (Sync-MirrorTasks)' | Out-Null   # Windows max: 48 chars
    }
    # Not in Users: no local or Remote Desktop logon; network (SMB) logon still works.
    try { Remove-LocalGroupMember -Group 'Users' -Member $Account -ErrorAction Stop } catch { }
}

function Sync-ProviderCredential([string]$Job, $Def, $Api, $PState) {
    # This machine holds the share: account + access, and the password for the consumer's key.
    $sa = $Def.share_access
    $epoch = if ($sa.key_epoch) { [int]$sa.key_epoch } else { 1 }
    $account = if ($sa.account) { "$($sa.account)" } else { 'svc-backup' }
    if ($account -notmatch '^[A-Za-z0-9._-]{1,20}$') { throw "bad account name '$account'" }
    $probe = { Invoke-CredsApi $Api 'GET' 'key' $Job }
    $key = & $probe
    if (-not $key -and (Test-NewHandoff "provider-wait/$Job" "e$epoch")) { $key = Wait-For $probe $WaitSeconds }
    if (-not $key) { return "waiting for $($Def.host) to publish its key" }
    if ((Get-KeyFingerprint $key.pubkey) -ne $key.fp) { throw 'key fingerprint mismatch from the dashboard' }
    $pin = $PState[$Job]
    if ($pin -and [int]$pin.epoch -eq $epoch -and $pin.fp -ne $key.fp) {
        throw "a different key was published for $Job (by '$($key.host)') without a key_epoch bump: refused. If $($Def.host) really got a new key, bump share_access.key_epoch in tasks.json."
    }
    Get-SmbShare -Name $sa.share -ErrorAction Stop | Out-Null    # fail early (before any account change) if the share is wrong
    $sec = Invoke-CredsApi $Api 'GET' 'secret' $Job
    $issue = -not $pin -or $pin.fp -ne $key.fp -or -not $pin.issued -or -not $sec -or $sec.key_fp -ne $key.fp -or
             -not (Get-LocalUser -Name $account -ErrorAction SilentlyContinue)
    if (-not $issue) { Grant-ShareWrite $sa.share $account; return $null }     # keeps access in place
    $PState[$Job] = [ordered]@{ fp = $key.fp; epoch = $epoch; issued = $false }
    $password = New-RandomPassword
    Set-ServiceAccount $account $password      # the account must exist before it can be granted anything
    Grant-ShareWrite $sa.share $account
    Invoke-CredsApi $Api 'POST' 'secret' $Job @{ id = $Job; host = $HostName; user = "$env:COMPUTERNAME\$account"; key_fp = $key.fp
                                               ciphertext = (Protect-ForKey $key.pubkey $password) } | Out-Null
    $PState[$Job].issued = $true
    return 'issued'
}

$defs = (Get-Content -Raw (Join-Path $Dir 'tasks.json') | ConvertFrom-Json).tasks
$state = @{}
if (Test-Path $StatePath) { try { foreach ($p in (Get-Content -Raw $StatePath | ConvertFrom-Json).PSObject.Properties) { $state[$p.Name] = $p.Value } } catch { } }

# ---- share credentials (both ends) ----
$pstate = @{}
if (Test-Path $ProviderStatePath) { try { foreach ($p in (Get-Content -Raw $ProviderStatePath | ConvertFrom-Json).PSObject.Properties) { $pstate[$p.Name] = $p.Value } } catch { } }
$api = $null
foreach ($p in $defs.PSObject.Properties) {
    $job, $def = $p.Name, $p.Value
    if (-not $def.share_access) { continue }
    $consumer = "$($def.host)".ToLower() -eq $HostName.ToLower()
    $provider = "$($def.share_access.provider)".ToLower() -eq $HostName.ToLower()
    if (-not ($consumer -or $provider) -or $WhatIf) { continue }
    try {
        if (-not $api) { $api = Get-DashboardApi $AgentDir }
        $note = if ($consumer) { Sync-ConsumerCredential $job $def $api } else { Sync-ProviderCredential $job $def $api $pstate }
        if ($note -like 'waiting*') { $result.pending += "${job}: $note" }
        elseif ($note) { $result.updated += "$job (share credential $note)" }
    } catch { $result.errors += "${job} (share credential): $($_.Exception.Message)" }
}
if ($pstate.Count -and -not $WhatIf) {
    Protect-Folder $Dir
    Set-Content -Path $ProviderStatePath -Value (ConvertTo-Json -InputObject $pstate -Depth 4) -Encoding UTF8
}

$wanted = @{}
foreach ($p in $defs.PSObject.Properties) {
    $job, $def = $p.Name, $p.Value
    if ("$($def.host)".ToLower() -ne $HostName.ToLower()) { continue }
    $name = "Mirror $job"
    $wanted[$name] = $true
    try {
        if ($job -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw "bad job name '$job'" }
        if (-not $def.source -or -not $def.destination) { throw 'needs "source" and "destination"' }
        $fp = Get-Hash ((New-TaskXml $job $def '') + $Version)
        $xml = New-TaskXml $job $def $fp
        $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
        $s = $state[$name]
        if ($existing -and $s -and $s.def -eq $fp -and $s.exported -and $s.exported -eq (Get-Exported $name)) {
            $result.unchanged += $job; continue
        }
        if ($WhatIf) { $result.updated += "$job (would register)"; continue }
        Register-ScheduledTask -TaskPath $TaskPath -TaskName $name -Xml $xml -Force | Out-Null
        $state[$name] = [ordered]@{ def = $fp; exported = (Get-Exported $name) }
        $result.updated += $job
    } catch {
        $result.errors += "${job}: $($_.Exception.Message)"
    }
}

# Remove tasks this script created FOR THIS HOST NAME that are no longer defined for it.
# (Tagged with the host name, so a wrong -HostName can never remove the real host's tasks.)
foreach ($t in @(Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue)) {
    if ($wanted[$t.TaskName] -or "$($t.Description)" -notmatch "\[$Marker host:$([regex]::Escape($HostName.ToLower())) ") { continue }
    try {
        if (-not $WhatIf) { Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $t.TaskName -Confirm:$false; $state.Remove($t.TaskName) }
        $result.removed += $t.TaskName
    } catch { $result.errors += "remove $($t.TaskName): $($_.Exception.Message)" }
}

if (-not $WhatIf) { Set-Content -Path $StatePath -Value (ConvertTo-Json -InputObject $state -Depth 4) -Encoding UTF8 }
return [pscustomobject]$result
