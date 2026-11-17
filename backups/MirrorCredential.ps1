<#
  Shared by Sync-MirrorTasks.ps1 and Invoke-Mirror.ps1 (dot-sourced). The share credential
  for a job that writes to another machine's share (see backups/README.md, "Share credential").

  - The consumer's RSA key lives in the Windows MACHINE key store (container
    HomelabMirror-<job>-e<epoch>); its private half never leaves the machine and is readable
    by SYSTEM / Administrators only.
  - Only the public key and the encrypted password travel, via the dashboard (creds.py).
  - Encryption: RSA-OAEP (SHA-1, as supported by Windows PowerShell 5.1's CSP keys).

  Test hook: if $env:HOMELAB_TEST_KEYDIR is set, keys are kept as XML files there instead
  (the machine key store only exists on Windows).
#>

function Get-MirrorKey([string]$Job, [int]$Epoch) {
    $name = "HomelabMirror-$Job-e$Epoch"
    if ($env:HOMELAB_TEST_KEYDIR) {
        $f = Join-Path $env:HOMELAB_TEST_KEYDIR "$name.xml"
        $rsa = [System.Security.Cryptography.RSA]::Create()
        if (Test-Path $f) { $rsa.FromXmlString((Get-Content -Raw $f)) }
        else { $rsa.KeySize = 3072; Set-Content -Path $f -Value $rsa.ToXmlString($true) }
        return $rsa
    }
    $csp = New-Object System.Security.Cryptography.CspParameters
    $csp.KeyContainerName = $name
    $csp.Flags = [System.Security.Cryptography.CspProviderFlags]::UseMachineKeyStore
    $rsa = New-Object System.Security.Cryptography.RSACryptoServiceProvider -ArgumentList 3072, $csp
    $rsa.PersistKeyInCsp = $true
    return $rsa
}

function Get-KeyFingerprint([string]$PublicXml) {
    # Same as dashboard creds.fingerprint(): sha256 of the trimmed XML, first 16 hex chars.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return (-join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($PublicXml.Trim())) | ForEach-Object { $_.ToString('x2') })).Substring(0, 16) }
    finally { $sha.Dispose() }
}

function Protect-ForKey([string]$PublicXml, [string]$Secret) {
    $rsa = [System.Security.Cryptography.RSA]::Create()
    $rsa.FromXmlString($PublicXml)
    return [Convert]::ToBase64String($rsa.Encrypt([Text.Encoding]::UTF8.GetBytes($Secret), [System.Security.Cryptography.RSAEncryptionPadding]::OaepSHA1))
}

function Unprotect-WithKey($Rsa, [string]$Ciphertext) {
    return [Text.Encoding]::UTF8.GetString($Rsa.Decrypt([Convert]::FromBase64String($Ciphertext), [System.Security.Cryptography.RSAEncryptionPadding]::OaepSHA1))
}

function New-RandomPassword([int]$Length = 32) {
    # Upper, lower, digit and symbol guaranteed (local password policy), rest random.
    $sets = 'ABCDEFGHJKLMNPQRSTUVWXYZ', 'abcdefghijkmnopqrstuvwxyz', '23456789', '!#%*+-=?@^_'
    $all = -join $sets
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $pick = { param($chars) $b = New-Object byte[] 4; $rng.GetBytes($b); $chars[[BitConverter]::ToUInt32($b, 0) % $chars.Length] }
    $chars = @(foreach ($s in $sets) { & $pick $s }) + @(1..($Length - $sets.Count) | ForEach-Object { & $pick $all })
    $rng.Dispose()
    return -join ($chars | Sort-Object { [guid]::NewGuid() })
}

function Get-DashboardApi([string]$AgentDir) {
    # Base URL and token from the rack agent's config (same ones the agents use).
    $cfg = Get-Content -Raw (Join-Path $AgentDir 'config.json') | ConvertFrom-Json
    $base = ($cfg.url -replace '/api/(smart|metrics)/?$', '').TrimEnd('/')
    $token = (Get-Content -Raw (Join-Path $AgentDir 'token.txt')).Trim()
    return @{ base = $base; headers = @{ Authorization = "Bearer $token" } }
}

function Invoke-CredsApi($Api, [string]$Method, [string]$Kind, [string]$Id, $Body) {
    # Returns the response object, or $null on 404.
    $uri = "$($Api.base)/api/creds/$Kind"
    try {
        if ($Method -eq 'GET') { return Invoke-RestMethod -Uri "$uri`?id=$Id" -Headers $Api.headers -UseBasicParsing -TimeoutSec 15 }
        return Invoke-RestMethod -Uri $uri -Method Post -Headers $Api.headers -ContentType 'application/json' -UseBasicParsing -TimeoutSec 15 `
            -Body ([Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Body -Compress)))
    } catch {
        $code = $null
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code -eq 404) { return $null }
        throw "dashboard $Method /api/creds/$Kind failed: $($_.Exception.Message)"
    }
}
