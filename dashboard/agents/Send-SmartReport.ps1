<#
.SYNOPSIS
  SMART agent for Windows (homeshare, backup-compute). Pushes drive health to the rack dashboard.

.DESCRIPTION
  Collects two things and POSTs them to the dashboard:
    1. Raw `smartctl -j -x` output for every device smartctl can see.
    2. Windows' own view of every physical disk (Get-PhysicalDisk + Get-StorageReliabilityCounter),
       including which Storage Spaces pool it belongs to. This covers pool members that smartctl
       can't open, and labels every drive with its pool.
  The dashboard matches the two by serial number.

  Runs as SYSTEM from a scheduled task (see Install-SmartAgent.ps1). Windows PowerShell 5.1 compatible.
  Config:  %ProgramData%\RackAgent\config.json   {"url": "...", "name": "homeshare", "smartctl": "..."}
  Token:   %ProgramData%\RackAgent\token.txt     (readable by SYSTEM and Administrators only)
  Log:     %ProgramData%\RackAgent\agent.log
#>
[CmdletBinding()]
param(
    [string]$Dir = (Join-Path $env:ProgramData 'RackAgent'),
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Version = 'Send-SmartReport.ps1/2'   # bump with LATEST_AGENTS in smart.py
$LogPath = Join-Path $Dir 'agent.log'

function Write-Log([string]$Message) {
    $line = '{0:u} {1}' -f (Get-Date).ToUniversalTime(), $Message
    Write-Host $line
    if (-not $DryRun) {
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
        $lines = @(Get-Content -Path $LogPath -Encoding UTF8)
        if ($lines.Count -gt 1000) { $lines[-500..-1] | Set-Content -Path $LogPath -Encoding UTF8 }
    }
}

function Invoke-SmartctlJson([string]$Exe, [string[]]$Arguments) {
    # Returns @{ Text = <raw json string>; Exit = <int> }. Raw text is forwarded as-is, so nothing is
    # lost to PowerShell's JSON round-tripping.
    # 'Continue' here: under 'Stop', Windows PowerShell 5.1 turns native stderr output into a terminating error.
    $ErrorActionPreference = 'Continue'
    $out = & $Exe -j @Arguments 2>$null | Out-String
    return @{ Text = $out.Trim(); Exit = $LASTEXITCODE }
}

try {
    $config = Get-Content -Raw -Path (Join-Path $Dir 'config.json') | ConvertFrom-Json
    $name = if ($config.name) { $config.name } else { $env:COMPUTERNAME.ToLower() }
    $smartctl = if ($config.smartctl) { $config.smartctl } else { 'C:\Program Files\smartmontools\bin\smartctl.exe' }
    $errors = New-Object System.Collections.Generic.List[string]
    $driveJson = New-Object System.Collections.Generic.List[string]

    # ---- 1. smartctl ----
    if (Test-Path $smartctl) {
        $scan = Invoke-SmartctlJson $smartctl @('--scan-open')
        $devices = @()
        try { $devices = @(($scan.Text | ConvertFrom-Json).devices) } catch { $errors.Add('smartctl --scan-open returned unparseable output') }
        foreach ($d in $devices) {
            if (-not $d.name) { continue }
            if ($d.open_error) { $errors.Add("$($d.name): $($d.open_error)"); continue }
            # -x (not -a): includes ATA Device Statistics, the authoritative SSD wear value
            $smArgs = if ($d.type) { @('-x', '-d', $d.type, $d.name) } else { @('-x', $d.name) }
            $r = Invoke-SmartctlJson $smartctl $smArgs
            try {
                $null = $r.Text | ConvertFrom-Json      # validate only
                $driveJson.Add($r.Text)
            } catch {
                $stub = @{ device = @{ name = $d.name; type = $d.type }; agent_error = 'unparseable smartctl output'; smartctl = @{ exit_status = $r.Exit } }
                $driveJson.Add((ConvertTo-Json -InputObject $stub -Compress -Depth 5))
            }
        }
    } else {
        $errors.Add("smartctl not found at $smartctl (install smartmontools)")
    }

    # ---- 2. Windows physical disks + Storage Spaces pool membership ----
    $poolOf = @{}
    try {
        foreach ($pool in @(Get-StoragePool -IsPrimordial $false -ErrorAction Stop)) {
            foreach ($pd in @($pool | Get-PhysicalDisk)) { if ($pd.UniqueId) { $poolOf[$pd.UniqueId] = $pool.FriendlyName } }
        }
    } catch { $errors.Add("Get-StoragePool failed: $($_.Exception.Message)") }

    $winDisks = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($pd in @(Get-PhysicalDisk -ErrorAction Stop)) {
            $rc = $null
            try { $rc = $pd | Get-StorageReliabilityCounter -ErrorAction Stop } catch { }
            $winDisks.Add([ordered]@{
                device_id                = $pd.DeviceId
                friendly_name            = $pd.FriendlyName
                serial                   = ("$($pd.SerialNumber)").Trim()
                firmware                 = $pd.FirmwareVersion
                media_type               = "$($pd.MediaType)"
                bus_type                 = "$($pd.BusType)"
                size                     = [int64]$pd.Size
                health_status            = "$($pd.HealthStatus)"
                operational_status       = ($pd.OperationalStatus | ForEach-Object { "$_" }) -join ', '
                pool                     = if ($pd.UniqueId) { $poolOf[$pd.UniqueId] } else { $null }
                reliability_available    = [bool]$rc
                temperature              = if ($rc) { $rc.Temperature } else { $null }
                wear                     = if ($rc) { $rc.Wear } else { $null }
                power_on_hours           = if ($rc) { $rc.PowerOnHours } else { $null }
                read_errors_uncorrected  = if ($rc) { $rc.ReadErrorsUncorrected } else { $null }
                write_errors_uncorrected = if ($rc) { $rc.WriteErrorsUncorrected } else { $null }
            })
        }
    } catch { $errors.Add("Get-PhysicalDisk failed: $($_.Exception.Message)") }

    # ---- 3. Build the report: header via ConvertTo-Json, smartctl output spliced in raw ----
    $head = [ordered]@{
        schema        = 1
        host          = $name
        agent         = $Version
        os            = 'windows'
        collected_at  = (Get-Date).ToUniversalTime().ToString('o')
        windows_disks = $winDisks.ToArray()
        errors        = $errors.ToArray()
    }
    $headJson = ConvertTo-Json -InputObject $head -Compress -Depth 6
    $body = $headJson.Substring(0, $headJson.Length - 1) + ',"drives":[' + ($driveJson -join ',') + ']}'

    if ($DryRun) {
        Write-Host "host=$name smartctl drives=$($driveJson.Count) windows disks=$($winDisks.Count)"
        $winDisks | ForEach-Object { Write-Host (" {0,-32} {1,-20} pool={2} health={3}" -f $_.friendly_name, $_.serial, $_.pool, $_.health_status) }
        $errors | ForEach-Object { Write-Host "error: $_" }
        exit 0
    }

    $token = (Get-Content -Raw -Path (Join-Path $Dir 'token.txt')).Trim()
    $resp = Invoke-RestMethod -Uri $config.url -Method Post -UseBasicParsing -TimeoutSec 30 `
        -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/json' `
        -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
    Write-Log "sent $($driveJson.Count) smartctl drive(s) + $($winDisks.Count) Windows disk(s) as '$name' ($($errors.Count) error(s))"
    exit 0
} catch {
    Write-Log "FAILED: $($_.Exception.Message)"
    exit 1
}
