<#
.SYNOPSIS
  Compute metrics agent for Windows (homeshare, backup-compute). Normally runs as the
  long-lived scheduled task "Rack Dashboard metrics" with -LoopSeconds 10, posting a
  sample to the dashboard's /api/metrics every 10 seconds. Without -LoopSeconds it
  sends one sample (1-second window) and exits.

.DESCRIPTION
  Performance counters over the interval (CPU per core, NICs, disks, processes),
  plus WMI (CPU model, memory, uptime), pending-reboot flags, and nvidia-smi if present.
  Uses the same config.json and token.txt as Send-SmartReport.ps1; the SMART URL in
  config.json (/api/smart) is rewritten to /api/metrics.
  Counter names are the English ones. Only state changes are logged (start, first
  failure, recovery), so the log doesn't grow every 10 seconds. In loop mode it exits
  when this file is replaced (self-update) or after 24 h; Invoke-RackAgent.ps1 starts
  it again within a minute. Windows PowerShell 5.1 compatible.

  "Deploy now": the dashboard answers each sample with the latest deploy request. If it
  is newer than last-full-run.txt, that stamp is deleted, so Invoke-RackAgent.ps1 does
  its update + SMART on its next run (within a minute). Nothing sent by the dashboard is
  executed. deploy-status.json (written by the wrapper) is reported for the dashboard's
  Deployments screen.
#>
[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:ProgramData 'RackAgent'),
    [int]$LoopSeconds = 0,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Version = 'Send-Metrics.ps1/3'   # bump with LATEST_AGENTS in metrics.py
$LogPath = Join-Path $Dir 'agent.log'

function Write-Log([string]$Message) {
    $line = '{0:u} [metrics] {1}' -f (Get-Date).ToUniversalTime(), $Message
    Write-Host $line
    if (-not $DryRun) { Add-Content -Path $LogPath -Value $line -Encoding UTF8 }
}

function Get-NvidiaGpus {
    $exe = (Get-Command nvidia-smi -ErrorAction SilentlyContinue).Source
    if (-not $exe -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $exe = "$env:SystemRoot\System32\nvidia-smi.exe" }
    if (-not $exe) { return @() }
    $q = 'name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,utilization.encoder,utilization.decoder'
    $num = { param($x) $d = 0.0; if ([double]::TryParse($x.Trim(), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$d)) { $d } else { $null } }
    $gpus = @()
    foreach ($line in (& $exe "--query-gpu=$q" '--format=csv,noheader,nounits' 2>$null)) {
        $f = $line -split ','
        if ($f.Count -lt 8) { continue }
        $gpus += [ordered]@{
            name = $f[0].Trim(); util_pct = (& $num $f[1]); mem_used = (& $num $f[2]) * 1048576; mem_total = (& $num $f[3]) * 1048576
            temp_c = (& $num $f[4]); power_w = (& $num $f[5]); enc_pct = (& $num $f[6]); dec_pct = (& $num $f[7])
        }
    }
    return $gpus
}

$script:cpus = $null
function New-MetricsBody([string]$name, [int]$Window) {
    # ---- static facts (WMI); the CPU list never changes, so it's read once ----
    $os = Get-CimInstance Win32_OperatingSystem
    if (-not $script:cpus) { $script:cpus = @(Get-CimInstance Win32_Processor) }
    $cpus = $script:cpus
    $threads = [int]($cpus | Measure-Object NumberOfLogicalProcessors -Sum).Sum
    $cores = [int]($cpus | Measure-Object NumberOfCores -Sum).Sum

    # ---- counter window (2 samples $Window s apart: rates are averaged over it) ----
    $paths = @(
        '\Processor(*)\% Processor Time',
        '\Network Interface(*)\Bytes Received/sec', '\Network Interface(*)\Bytes Sent/sec', '\Network Interface(*)\Current Bandwidth',
        '\PhysicalDisk(*)\Disk Read Bytes/sec', '\PhysicalDisk(*)\Disk Write Bytes/sec', '\PhysicalDisk(*)\% Idle Time',
        '\Memory\% Committed Bytes In Use',
        '\Process(*)\% Processor Time', '\Process(*)\Working Set - Private'
    )
    $set = Get-Counter -Counter $paths -SampleInterval ([math]::Max(1, $Window)) -MaxSamples 2 -ErrorAction SilentlyContinue
    $samples = @($set)[-1].CounterSamples

    # Group as  counterset -> instance -> counter -> value
    $c = @{}
    foreach ($s in $samples) {
        if ($s.Path -notmatch '\\\\[^\\]+\\([^\\(]+)(?:\((.*)\))?\\(.+)$') { continue }
        $setName, $ctr = $Matches[1].ToLower(), $Matches[3].ToLower()
        $inst = if ($Matches[2]) { $Matches[2] } else { '' }   # e.g. \Memory\... has no instance
        if (-not $c[$setName]) { $c[$setName] = @{} }
        if (-not $c[$setName][$inst]) { $c[$setName][$inst] = @{} }
        $c[$setName][$inst][$ctr] = $s.CookedValue
    }

    $proc = $c['processor']
    $perCore = @()
    if ($proc) {
        $perCore = @($proc.Keys | Where-Object { $_ -match '^\d+$' } | Sort-Object { [int]$_ } |
            ForEach-Object { [math]::Round([math]::Min(100.0, $proc[$_]['% processor time']), 1) })
    }

    $net = @()
    if ($c['network interface']) {
        foreach ($k in $c['network interface'].Keys) {
            $v = $c['network interface'][$k]
            $bw = [double]$v['current bandwidth']
            if ($bw -le 0 -or $k -match 'isatap|teredo|loopback|pseudo') { continue }
            $net += [ordered]@{ name = $k; rx_bps = [math]::Round($v['bytes received/sec'], 1); tx_bps = [math]::Round($v['bytes sent/sec'], 1); speed_mbps = [int]($bw / 1e6) }
        }
    }

    $disks = @()
    if ($c['physicaldisk']) {
        foreach ($k in ($c['physicaldisk'].Keys | Where-Object { $_ -ne '_total' } | Sort-Object)) {
            $v = $c['physicaldisk'][$k]
            $disks += [ordered]@{ name = $k; read_bps = [math]::Round($v['disk read bytes/sec'], 1); write_bps = [math]::Round($v['disk write bytes/sec'], 1)
                                  busy_pct = [math]::Round([math]::Max(0.0, 100.0 - $v['% idle time']), 1) }
        }
    }

    $top = @(); $topMem = @()
    $procCount = 0
    if ($c['process']) {
        $rows = foreach ($k in $c['process'].Keys) {
            if ($k -in '_total', 'idle') { continue }
            $procCount++
            [pscustomobject]@{ name = ($k -replace '#\d+$', '')
                               cpu_pct = [math]::Round($c['process'][$k]['% processor time'] / [math]::Max(1, $threads), 1)
                               mem = [double]$c['process'][$k]['working set - private'] }
        }
        $top = @($rows | Sort-Object cpu_pct -Descending | Select-Object -First 3 | ForEach-Object { [ordered]@{ name = $_.name; cpu_pct = $_.cpu_pct } })
        # Memory by program name (e.g. all svchost instances together): private working set, i.e. RAM the process alone holds
        $topMem = @($rows | Where-Object { $_.mem -gt 0 } | Group-Object name | ForEach-Object {
                [pscustomobject]@{ name = $_.Name; mem = ($_.Group | Measure-Object mem -Sum).Sum; n = $_.Count } } |
            Sort-Object mem -Descending | Select-Object -First 3 |
            ForEach-Object { [ordered]@{ name = $_.name; mem = [math]::Round($_.mem); n = $_.n } })
    }

    $pending = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or
               (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')

    $totalMem = [double]$os.TotalVisibleMemorySize * 1024
    $sample = [ordered]@{
        schema = 1; host = $name; agent = $Version; os = 'windows'
        collected_at = (Get-Date).ToUniversalTime().ToString('o')
        uptime_s = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
        pending_reboot = [bool]$pending
        deploy_status = Get-DeployStatus
        cpu = [ordered]@{
            model = ($cpus[0].Name -replace '\s+', ' ').Trim(); sockets = $cpus.Count; cores = $cores; threads = $threads
            util_pct = if ($proc -and $proc['_total']) { [math]::Round([math]::Min(100.0, $proc['_total']['% processor time']), 1) } else { $null }
            per_core_pct = $perCore
        }
        mem = [ordered]@{
            total = $totalMem; used = $totalMem - [double]$os.FreePhysicalMemory * 1024
            commit_pct = if ($c['memory']) { [math]::Round($c['memory'][''][''+'% committed bytes in use'], 1) } else { $null }
        }
        gpus = @(Get-NvidiaGpus)
        net = $net; disks = $disks
        procs = [ordered]@{ count = $procCount; top = $top; top_mem = $topMem }
    }
    $tasks = Get-HomelabTasks
    if ($null -ne $tasks) { $sample.tasks = @($tasks) }
    return ConvertTo-Json -InputObject $sample -Depth 8 -Compress
}

function Send-Body([string]$url, [string]$body) {
    $token = (Get-Content -Raw -Path (Join-Path $Dir 'token.txt')).Trim()
    return Invoke-RestMethod -Uri $url -Method Post -UseBasicParsing -TimeoutSec 15 `
        -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
}

function Invoke-DeployRequest($resp) {
    # Bring the 15-minute full run forward if the request is newer than its stamp.
    $req = if ($resp) { $resp.deploy } else { $null }
    if (-not $req) { return }
    $stamp = Join-Path $Dir 'last-full-run.txt'
    if (-not (Test-Path $stamp)) { return }        # a full run is already due
    $stampAge = ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalSeconds
    if ($stampAge -gt [double]$req.age_s) {
        Remove-Item -Force $stamp
        Write-Log 'deploy requested: update + SMART on the next run (within a minute)'
    }
}

function Get-ScheduleInfo($schedule) {
    # From a backups/tasks.json schedule: a label, and the LONGEST normal gap between runs
    # (what "overdue" is measured against; e.g. days 1 and 15 of a month -> 17 days).
    if (-not $schedule) { return $null }
    $kind = @($schedule.PSObject.Properties.Name)[0]
    $s = $schedule.$kind
    $at = "$($s.at)"
    if ($kind -eq 'daily') {
        $n = if ($s.every_days) { [int]$s.every_days } else { 1 }
        $label = if ($n -eq 1) { "daily at $at" } else { "every $n days at $at" }
        return @{ interval_s = $n * 86400; text = $label }
    }
    if ($kind -eq 'weekly') {
        $order = @('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
        $idx = @($s.days | ForEach-Object { $k = "$_".ToLower(); if ($k.Length -gt 3) { $k = $k.Substring(0, 3) }; [array]::IndexOf($order, $k) } |
                 Where-Object { $_ -ge 0 } | Sort-Object -Unique)
        if (-not $idx) { return $null }
        $gap = 0
        for ($i = 0; $i -lt $idx.Count; $i++) {
            $next = if ($i + 1 -lt $idx.Count) { $idx[$i + 1] } else { $idx[0] + 7 }
            $gap = [math]::Max($gap, $next - $idx[$i])
        }
        $names = ($idx | ForEach-Object { $order[$_].Substring(0, 1).ToUpper() + $order[$_].Substring(1) }) -join ', '
        return @{ interval_s = $gap * 86400; text = "weekly $names at $at" }
    }
    if ($kind -eq 'monthly') {
        # "last" counts as day 31; gaps assume 31-day months (the worst case).
        $days = @($s.days | ForEach-Object { if ("$_" -eq 'last') { 31 } else { [int]$_ } } | Sort-Object -Unique)
        if (-not $days) { return $null }
        $gap = 0
        for ($i = 0; $i -lt $days.Count; $i++) {
            $next = if ($i + 1 -lt $days.Count) { $days[$i + 1] } else { $days[0] + 31 }
            $gap = [math]::Max($gap, $next - $days[$i])
        }
        $list = @($s.days | ForEach-Object { "$_" })
        $names = if ($list.Count -eq 1) { "day $($list[0])" } else { "days $($list -join ', ')" }
        return @{ interval_s = $gap * 86400; text = "monthly on $names at $at" }
    }
    return $null
}

$script:tasksAt = [datetime]::MinValue
function Get-HomelabTasks {
    # Every scheduled task in \Homelab\ (state, last/next run, Task Scheduler result), plus,
    # for mirror jobs, the runner's own results (backups/Invoke-Mirror.ps1). Refreshed at
    # most once a minute; returns $null in between, so most samples don't carry it.
    if (((Get-Date) - $script:tasksAt).TotalSeconds -lt 55) { return $null }
    $script:tasksAt = Get-Date
    $now = Get-Date
    $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $ageOf = { param($dt) if ($dt -and $dt.Year -gt 2000) { [int][math]::Max(0, ($now - $dt).TotalSeconds) } else { $null } }
    $resultsDir = Join-Path (Join-Path $env:ProgramData 'HomelabBackups') 'results'
    $defs = $null
    try { $defs = (Get-Content -Raw (Join-Path (Join-Path $env:ProgramData 'HomelabBackups') 'tasks.json') | ConvertFrom-Json).tasks } catch { }
    $out = @()
    foreach ($t in @(Get-ScheduledTask -TaskPath '\Homelab\' -ErrorAction SilentlyContinue)) {
        $info = $t | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
        $job = $null
        foreach ($a in $t.Actions) { if ("$($a.Arguments)" -match '(?:Invoke-Mirror|Invoke-CloudBackup)\.ps1"?\s+-Job\s+"?([A-Za-z0-9._-]+)') { $job = $Matches[1] } }
        $next = if ($info -and $info.NextRunTime -and $info.NextRunTime.Year -gt 2000) { [int](($info.NextRunTime - $now).TotalSeconds) } else { $null }
        $entry = [ordered]@{
            name = $t.TaskName; state = "$($t.State)"; job = $job; description = $t.Description
            last_run_age_s = (& $ageOf $info.LastRunTime); next_run_in_s = $next
            last_task_result = $(if ($info) { $info.LastTaskResult }); missed_runs = $(if ($info) { $info.NumberOfMissedRuns })
        }
        if ($job -and $defs -and $defs.$job) {
            $si = Get-ScheduleInfo $defs.$job.schedule
            if ($si) { $entry.interval_s = $si.interval_s; $entry.schedule = $si.text }
        }
        if ($job -and (Test-Path (Join-Path $resultsDir "$job.json"))) {
            try {
                $r = Get-Content -Raw (Join-Path $resultsDir "$job.json") | ConvertFrom-Json
                $entry.source = $r.source; $entry.destination = $r.destination; $entry.kind = $(if ($r.kind) { "$($r.kind)" } else { 'mirror' })
                if ($r.current) { $entry.running_for_s = [int]($epoch - [int64]$r.current.start) }
                $entry.runs = @($r.runs | ForEach-Object {
                    [ordered]@{ age_s = [int]($epoch - [int64]$_.end); duration_s = [int]([int64]$_.end - [int64]$_.start); status = $_.status
                                message = $_.message; exit_code = $_.exit_code; summary = $_.summary; error_count = $_.error_count; errors = $_.errors }
                })
            } catch { $entry.results_error = $_.Exception.Message }
        }
        $out += $entry
    }
    return , $out
}

function Get-DeployStatus {
    # deploy-status.json from Invoke-RackAgent.ps1, with its times turned into ages so
    # the dashboard doesn't depend on this machine's clock.
    $f = Join-Path $Dir 'deploy-status.json'
    if (-not (Test-Path $f)) { return $null }
    try { $st = Get-Content -Raw $f | ConvertFrom-Json } catch { return $null }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $out = [ordered]@{}
    foreach ($p in $st.PSObject.Properties) {
        if ($p.Name -in 'checked_at', 'changed_at') {
            if ($null -ne $p.Value) { $out[($p.Name -replace '_at$', '_age_s')] = [math]::Max(0, $now - [int64]$p.Value) }
        } elseif ($p.Name -eq 'running_since') {
            if ($null -ne $p.Value) { $out.running_for_s = [math]::Max(0, $now - [int64]$p.Value) }
        } else { $out[$p.Name] = $p.Value }
    }
    return $out
}

function Get-Target {
    $config = Get-Content -Raw -Path (Join-Path $Dir 'config.json') | ConvertFrom-Json
    $name = if ($config.name) { $config.name } else { $env:COMPUTERNAME.ToLower() }
    return @{ name = $name; url = ($config.url -replace '/api/smart/?$', '/api/metrics') }
}

# ---- one-shot (default, and -DryRun) ----
if ($LoopSeconds -le 0 -or $DryRun) {
    try {
        $t = Get-Target
        $body = New-MetricsBody $t.name 1
        if ($DryRun) { Write-Host $body; exit 0 }
        Send-Body $t.url $body | Out-Null
        exit 0
    } catch {
        Write-Log "FAILED: $($_.Exception.Message)"
        exit 1
    }
}

# ---- loop: a sample every $LoopSeconds, averaged over the interval ----
if ($LoopSeconds -lt 2) { $LoopSeconds = 2 }
$self = $PSCommandPath
$selfStamp = (Get-Item $self).LastWriteTimeUtc
$started = Get-Date
$failing = $null
Write-Log "started: $Version, every $LoopSeconds s"
while ($true) {
    $t0 = Get-Date
    $err = $null
    try {
        $t = Get-Target
        $resp = Send-Body $t.url (New-MetricsBody $t.name ($LoopSeconds - 2))
        Invoke-DeployRequest $resp
    } catch { $err = $_.Exception.Message }
    if ($err -and $err -ne $failing) { Write-Log "FAILED: $err" }
    elseif (-not $err -and $failing) { Write-Log 'recovered' }
    $failing = $err

    if (-not (Test-Path $self) -or (Get-Item $self).LastWriteTimeUtc -ne $selfStamp) {
        Write-Log 'agent file changed on disk: exiting so the new version starts'; exit 0
    }
    if (((Get-Date) - $started).TotalHours -ge 24) { exit 0 }   # daily fresh start
    $wait = $LoopSeconds - ((Get-Date) - $t0).TotalSeconds
    if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }
}
