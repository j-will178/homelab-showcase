<#
.SYNOPSIS
  One-time: stores a cloud-backup job's secrets on this machine (B2 key ID, B2 application
  key, Kopia repository password), encrypted to this machine's key. Run again only to
  rotate them.

.DESCRIPTION
  Asks for the three values (hidden input for the key and password), encrypts each with the
  job's RSA key in the Windows machine key store (MirrorCredential.ps1; private half never
  leaves the machine), and writes creds\<job>.json in a folder only SYSTEM and Administrators
  can read. Invoke-CloudBackup.ps1 decrypts them at run time.

  The repository password is the one secret that can't be replaced: without it the cloud
  copy is permanently unreadable. Keep it in your password manager before running this.

  Run once in an elevated PowerShell (the updater installs this script):
    & "$env:ProgramData\HomelabBackups\Set-CloudBackupSecrets.ps1" -Job homeshare-to-b2
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]{1,64}$')][string]$Job,
    [string]$Dir = (Join-Path $env:ProgramData 'HomelabBackups')
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'MirrorCredential.ps1')

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this from an elevated (Administrator) PowerShell.' }
$def = (Get-Content -Raw (Join-Path $PSScriptRoot 'tasks.json') | ConvertFrom-Json).tasks.$Job
if (-not $def -or $def.type -ne 'kopia') { throw "No kopia job '$Job' in backups/tasks.json." }

function Read-Secret([string]$Prompt) {
    $s = Read-Host -AsSecureString $Prompt
    return [Net.NetworkCredential]::new('', $s).Password
}

Write-Host "Secrets for '$Job' (bucket $($def.kopia.bucket)). Input is hidden except the key ID." -ForegroundColor Cyan
$keyId = (Read-Host 'B2 application key ID').Trim()
$appKey = (Read-Secret 'B2 application key').Trim()
$password = Read-Secret 'Kopia repository password (from your password manager)'
$again = Read-Secret 'Repository password again'
if (-not $keyId -or -not $appKey) { throw 'Key ID and application key are required.' }
if ($password -ne $again) { throw "The two passwords don't match. Nothing was stored." }
if ($password.Length -lt 20) { throw 'Use a repository password of at least 20 characters (generate one in your password manager). Nothing was stored.' }

$epoch = 1
$rsa = Get-MirrorKey $Job $epoch
$pub = $rsa.ToXmlString($false)
$doc = [ordered]@{
    key_id = Protect-ForKey $pub $keyId; app_key = Protect-ForKey $pub $appKey; password = Protect-ForKey $pub $password
    epoch = $epoch; set_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
# Prove the round trip before saving.
if ((Unprotect-WithKey $rsa $doc.password) -ne $password) { throw 'Encryption round-trip failed. Nothing was stored.' }

$credDir = Join-Path $Dir 'creds'
if (-not (Test-Path $credDir)) { New-Item -ItemType Directory -Force -Path $credDir | Out-Null }
& icacls $credDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
Set-Content -Path (Join-Path $credDir "$Job.json") -Value (ConvertTo-Json -InputObject $doc -Compress) -Encoding UTF8
$password = $again = $appKey = $null

Write-Host "Stored, encrypted to this machine's key, in $credDir (SYSTEM and Administrators only)." -ForegroundColor Green
Write-Host "The next run creates the repository in the bucket if it's empty, or connects to it. To run now:"
Write-Host "  Start-ScheduledTask -TaskPath '\Homelab\' -TaskName 'Cloud backup $Job'"
Write-Host "Make sure the repository password is saved in your password manager: it cannot be recovered." -ForegroundColor Yellow
