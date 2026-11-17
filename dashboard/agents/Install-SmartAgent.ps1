<#
.SYNOPSIS
  Installs the SMART agent on a Windows machine as a scheduled task running as SYSTEM.

.EXAMPLE
  # From an elevated PowerShell, in the folder containing the .ps1 files:
  .\Install-SmartAgent.ps1 -DashboardUrl http://optiplex-management.home.arpa:8080/api/smart -Name homeshare -AutoUpdate

  You'll be asked for the agent token. On the OptiPlex:  sudo cat /etc/rack-dashboard/agent-token
  Re-running is safe: it keeps the token and deploy key unless you choose to replace them.

.PARAMETER AutoUpdate
  Keep the agent up to date from GitHub (needs Git for Windows). Creates a read-only deploy
  key for this machine, shows the public key to add under the repo's Settings > Deploy keys,
  clones only dashboard/agents, and points the scheduled task at Invoke-RackAgent.ps1, which
  pulls, checks and installs updates before every run.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DashboardUrl,
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [int]$IntervalMinutes = 15,
    [string]$Smartctl = 'C:\Program Files\smartmontools\bin\smartctl.exe',
    [switch]$AutoUpdate,
    [string]$RepoUrl = 'git@github.com:j-will178/homelab.git'
)
$ErrorActionPreference = 'Stop'

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this from an elevated (Administrator) PowerShell.'
}
if ($Name -notmatch '^[A-Za-z0-9._-]{1,64}$') { throw "Name '$Name' must match [A-Za-z0-9._-]{1,64}" }
if (-not (Test-Path $Smartctl)) {
    Write-Warning "smartctl not found at $Smartctl. Install smartmontools first (https://www.smartmontools.org, or: winget install smartmontools.smartmontools)."
    Write-Warning 'Continuing: the agent will still send Windows disk health, and the dashboard will show the missing smartctl.'
}

$dir = Join-Path $env:ProgramData 'RackAgent'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
# Lock the folder down to SYSTEM + Administrators (SIDs, so it works on any display language).
& icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null

Copy-Item -Force -Path (Join-Path $PSScriptRoot 'Send-SmartReport.ps1') -Destination $dir
if ($AutoUpdate) { foreach ($f in 'Invoke-RackAgent.ps1', 'Send-Metrics.ps1') { Copy-Item -Force -Path (Join-Path $PSScriptRoot $f) -Destination $dir } }

$config = [ordered]@{ url = $DashboardUrl; name = $Name; smartctl = $Smartctl }
ConvertTo-Json -InputObject $config | Set-Content -Path (Join-Path $dir 'config.json') -Encoding UTF8

$tokenPath = Join-Path $dir 'token.txt'
if ((Test-Path $tokenPath) -and -not (Read-Host 'A token is already saved. Replace it? (y/N)').StartsWith('y')) {
    Write-Host 'Keeping existing token.'
} else {
    $secure = Read-Host 'Agent token (from the OptiPlex: sudo cat /etc/rack-dashboard/agent-token)' -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $plain.Trim()) { throw 'Empty token.' }
    [IO.File]::WriteAllText($tokenPath, $plain.Trim())
}

# ---- auto-update from GitHub (optional) ----
if ($AutoUpdate) {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { throw 'Git for Windows is required for -AutoUpdate: https://git-scm.com/download/win (defaults are fine), then re-run.' }
    $sshDir = 'C:\Windows\System32\OpenSSH'
    if (-not (Test-Path "$sshDir\ssh.exe")) { $sshDir = Join-Path (Split-Path (Split-Path $git.Source)) 'usr\bin' }
    $key = Join-Path $dir 'deploy_key'
    if (-not (Test-Path $key)) {
        # Start-Process passes the raw command line, so the empty passphrase survives on every PowerShell version.
        Start-Process -FilePath "$sshDir\ssh-keygen.exe" -Wait -NoNewWindow `
            -ArgumentList "-q -t ed25519 -N `"`" -C rack-agent@$Name -f `"$key`""
    }
    # Windows ssh-keygen makes the key owner-only (the admin who ran this), but the task runs
    # as SYSTEM, and OpenSSH then refuses it ("bad permissions"). Reset the key to the folder's
    # ACL (SYSTEM + Administrators) and make Administrators the owner: valid for both.
    & icacls.exe $key /reset | Out-Null
    & icacls.exe $key /setowner '*S-1-5-32-544' | Out-Null
    $repo = Join-Path $dir 'repo'
    $fwd = { param($p) $p -replace '\\', '/' }
    $sshCmd = "'$(& $fwd "$sshDir\ssh.exe")' -i '$(& $fwd $key)' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='$(& $fwd (Join-Path $dir 'known_hosts'))'"
    if (-not (Test-Path (Join-Path $repo '.git'))) {
        Write-Host ''
        Write-Host 'Add this public key on GitHub: repo > Settings > Deploy keys > Add deploy key' -ForegroundColor Cyan
        Write-Host "  Title: rack-agent-$Name    Allow write access: leave UNCHECKED" -ForegroundColor Cyan
        Write-Host ''
        Get-Content "$key.pub"
        Write-Host ''
        Read-Host 'Press Enter once the key is added'
        & git -c "core.sshCommand=$sshCmd" clone --quiet --depth 1 --filter=blob:none --sparse $RepoUrl $repo
        if ($LASTEXITCODE -ne 0) { throw 'git clone failed: check the deploy key was added (read-only is fine) and that this machine can reach github.com:22.' }
    }
    # Save the key setting BEFORE sparse-checkout: with a partial clone, sparse-checkout
    # fetches the files over SSH, and without this it would use the default (no) key.
    & git -C $repo config core.sshCommand $sshCmd
    & git -C $repo sparse-checkout set dashboard/agents
    if ($LASTEXITCODE -ne 0) { throw 'git sparse-checkout failed: see the error above (deploy key / github.com:22).' }
    & (Join-Path $dir 'Invoke-RackAgent.ps1') -Dir $dir -NoRun
    Write-Host 'Auto-update is set up: each run pulls main, checks the scripts, and installs updates before reporting.'
}

$taskName = 'Rack Dashboard SMART agent'
# With auto-update the wrapper runs every minute (metrics) and throttles SMART/update to every 15.
if ($AutoUpdate -and -not $PSBoundParameters.ContainsKey('IntervalMinutes')) { $IntervalMinutes = 1 }
$script = Join-Path $dir $(if ($AutoUpdate -or (Test-Path (Join-Path $dir 'repo\.git'))) { 'Invoke-RackAgent.ps1' } else { 'Send-SmartReport.ps1' })
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
# Explicit duration: without it, some Windows versions stop repeating after one day.
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 9999)
$boot = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$runAs = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($every, $boot) -Settings $settings -Principal $runAs -Force | Out-Null

Write-Host "Installed '$taskName' (every $IntervalMinutes min, as SYSTEM). Running it once now..."
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 20
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("Last result: 0x{0:X} (0 = success)" -f $info.LastTaskResult)
if (Test-Path (Join-Path $dir 'agent.log')) { Get-Content (Join-Path $dir 'agent.log') -Tail 3 }
