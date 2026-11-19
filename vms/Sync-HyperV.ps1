<#
.SYNOPSIS
  Creates the Hyper-V VMs that vms/vms.json gives this host. Run by the Windows agent
  (Invoke-RackAgent.ps1, as SYSTEM) on every update, so a merged PR + Deploy all builds a VM.

.DESCRIPTION
  For each VM in vms.json with host == this machine (and enabled):
    - already exists and was made by this script: report it, change nothing (a VM's CPU,
      memory or disk can't safely change while it runs; to rebuild, delete it and Deploy all);
    - exists but NOT made by this script (no marker in its Notes): never touched, reported;
    - missing: build it:
        1. download the Debian cloud image named in vms.json (once; cached), and check its
           SHA-512 against the hash pinned in git: no match, no VM;
        2. turn the raw image into a VHDX (fixed-VHD footer + Convert-VHD), grow it to disk_gb;
        3. make a small seed disk (FAT, label CIDATA) with the first-boot config from
           <role>.user-data: hostname, admin SSH keys, role, the agent token and GitHub's host
           key (both from this host's own agent, so nothing is typed in);
        4. create the VM (Gen 2, Secure Boot with the UEFI CA template, static memory, fixed MAC,
           autostart) and start it. The VM then joins the lab by itself (see the user-data).
  Never deletes a VM or its disks. Refuses a VM path inside an SMB share (e.g. the backup
  mirror's destination, which deletes anything that isn't on the source).
  Needs Hyper-V and the virtual switch, both one-time manual steps: until they exist this
  reports "pending" and does nothing.

  Returns @{ updated; unchanged; pending; errors } like Sync-MirrorTasks.ps1.
  Windows PowerShell 5.1; keep this file ASCII.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$HostName,
    [string]$Dir = (Join-Path $env:ProgramData 'HomelabVMs'),
    [string]$AgentDir = (Join-Path $env:ProgramData 'RackAgent'),
    [string]$Repo = 'j-will178/homelab',
    [string]$Dashboard = 'http://optiplex-management.home.arpa:8080'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is very slow with a progress bar
$result = @{ updated = @(); unchanged = @(); pending = @(); errors = @() }

# ---------- pure helpers (tested on any OS) ----------

function Get-DefFingerprint($Def) {
    $json = $Def | ConvertTo-Json -Depth 5 -Compress
    $sha = [Security.Cryptography.SHA256]::Create()
    return (($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($json)) | Select-Object -First 6 | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-ImageUrl($Image) {
    # debian-13-genericcloud-amd64-20261110-2291.raw lives in <base_url>20261110-2291/
    if ("$($Image.file)" -notmatch '^debian-\d+-genericcloud-amd64-(\d{8}-\d+)\.raw$') { throw "image file '$($Image.file)' is not a debian-NN-genericcloud-amd64-<version>.raw name" }
    return "$($Image.base_url.TrimEnd('/'))/$($Matches[1])/$($Image.file)"
}

function Add-FixedVhdFooter([string]$Path) {
    # A raw disk image plus this 512-byte footer is a valid fixed VHD (Microsoft VHD spec),
    # which Convert-VHD can then turn into a VHDX.
    $size = (Get-Item $Path).Length
    if ($size % 512) { throw "raw image size $size is not a multiple of 512" }
    $total = [math]::Min([int64]($size / 512), [int64]65535 * 16 * 255)
    if ($total -ge [int64]65535 * 16 * 63) { $spt = 255; $heads = 16; $cth = [int64]($total / $spt) }
    else {
        $spt = 17; $cth = [int64][math]::Floor($total / $spt)
        $heads = [int64][math]::Floor(($cth + 1023) / 1024); if ($heads -lt 4) { $heads = 4 }
        if ($cth -ge $heads * 1024 -or $heads -gt 16) { $spt = 31; $heads = 16; $cth = [int64][math]::Floor($total / $spt) }
        if ($cth -ge $heads * 1024) { $spt = 63; $heads = 16; $cth = [int64][math]::Floor($total / $spt) }
    }
    $cyl = [int64][math]::Floor($cth / $heads)
    $f = New-Object byte[] 512
    function Put([int]$Off, [byte[]]$Bytes) { [Array]::Copy($Bytes, 0, $f, $Off, $Bytes.Length) }
    function BE([int64]$Value, [int]$Len) { $b = New-Object byte[] $Len; for ($i = $Len - 1; $i -ge 0; $i--) { $b[$i] = [byte]($Value -band 0xFF); $Value = $Value -shr 8 }; return ,$b }
    Put 0 ([Text.Encoding]::ASCII.GetBytes('conectix'))
    Put 8 (BE 2 4)                                        # features: reserved bit
    Put 12 (BE 0x00010000 4)                              # format version 1.0
    Put 16 ([byte[]](0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF))   # data offset: none (fixed disk)
    $secs = [int64]([DateTime]::UtcNow - [DateTime]::new(2000, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)).TotalSeconds
    Put 24 (BE $secs 4)
    Put 28 ([Text.Encoding]::ASCII.GetBytes('win '))
    Put 32 (BE 0x000A0000 4)
    Put 36 ([Text.Encoding]::ASCII.GetBytes('Wi2k'))
    Put 40 (BE $size 8); Put 48 (BE $size 8)              # original and current size
    Put 56 (BE $cyl 2); $f[58] = [byte]$heads; $f[59] = [byte]$spt
    Put 60 (BE 2 4)                                       # disk type: fixed
    Put 68 ([Guid]::NewGuid().ToByteArray())
    $sum = [uint32]0; foreach ($b in $f) { $sum = [uint32](($sum + $b) -band 0xFFFFFFFF) }
    Put 64 (BE ([int64](-bnot $sum -band 0xFFFFFFFF)) 4)
    $fs = [IO.File]::Open($Path, 'Append', 'Write')
    try { $fs.Write($f, 0, 512) } finally { $fs.Dispose() }
}

function Format-UserData([string]$Template, [hashtable]$Values) {
    # @@NAME@@-style placeholders. Values are validated by the caller; anything that could break
    # out of YAML (newlines in a scalar) is refused here as well.
    $out = $Template
    foreach ($k in $Values.Keys) {
        $v = [string]$Values[$k]
        if ($k -notin 'ADMIN_KEYS', 'GITHUB_KNOWN_HOSTS' -and $v -match '[\r\n]') { throw "value for $k contains a line break" }
        $out = $out.Replace("@@$k@@", $v)
    }
    if ($out -match '@@[A-Z_]+@@') { throw "user-data template has an unfilled placeholder: $($Matches[0])" }
    return ($out -replace "`r`n", "`n")
}

function Get-VmInputs($Name, $Def) {
    # Everything that goes into the seed, checked. Throws with a message meant for the dashboard.
    if ($Name -notmatch '^[a-z][a-z0-9-]{1,30}$') { throw "VM name '$Name' must be lower-case letters, digits and dashes" }
    $keys = @($Def.admin_ssh_keys | Where-Object { "$_".Trim() })
    if (-not $keys.Count) { throw "no admin_ssh_keys for $Name in vms.json (your public key, so you can log in)" }
    foreach ($k in $keys) { if ("$k" -notmatch '^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]{60,800}( [ -~]{0,100})?$') { throw "admin_ssh_keys: '$("$k".Substring(0, [math]::Min(30, "$k".Length)))...' is not one SSH public key line" } }
    if ("$($Def.admin_user)" -notmatch '^[a-z][a-z0-9_-]{1,30}$') { throw "admin_user '$($Def.admin_user)' is not a valid user name" }
    if ("$($Def.role)" -notmatch '^[a-z][a-z0-9-]{1,30}$') { throw "role '$($Def.role)' is not valid" }
    $tokenFile = Join-Path $AgentDir 'token.txt'
    if (-not (Test-Path $tokenFile)) { throw "this host's agent token ($tokenFile) is missing" }
    $token = (Get-Content -Raw $tokenFile).Trim()
    if ($token -notmatch '^[A-Za-z0-9._~+/=-]{16,200}$') { throw 'the agent token has unexpected characters' }
    $kh = Join-Path $AgentDir 'known_hosts'
    $gh = @(if (Test-Path $kh) { Get-Content $kh | Where-Object { $_ -match '^github\.com[ ,]' -and $_ -match ' (ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ' } })
    if (-not $gh.Count) { throw "no GitHub host key in $kh (this host's agent pulls from GitHub; it should be there)" }
    return @{
        NAME = $Name; ROLE = $Def.role; ADMIN_USER = $Def.admin_user; AGENT_TOKEN = $token
        REPO = $Repo; DASHBOARD = $Dashboard
        ADMIN_KEYS = (($keys | ForEach-Object { "      - $("$_".Trim())" }) -join "`n")
        GITHUB_KNOWN_HOSTS = (($gh | ForEach-Object { "      $($_ -replace '^github\.com,[^ ]+', 'github.com')" }) -join "`n")
    }
}

# ---------- Hyper-V side ----------

function Protect-Folder([string]$Path) {
    # SYSTEM + Administrators only: the seed disks carry the agent token.
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Force -Path $Path | Out-Null }
    & icacls $Path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
}

function Test-InsideShare([string]$Path) {
    $norm = { param($p) ([IO.Path]::GetFullPath($p) -replace '/', '\').TrimEnd('\') + '\' }
    $full = & $norm $Path
    foreach ($s in @(Get-SmbShare -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Name -notmatch '\$$' })) {
        $sp = & $norm $s.Path
        if ($full.StartsWith($sp, [StringComparison]::OrdinalIgnoreCase)) { return $s.Name }
    }
    return $null
}

function Get-BaseImage($Image) {
    # Cached raw image, verified against vms.json's SHA-512 every time it's used.
    $cache = Join-Path $Dir 'images'
    if (-not (Test-Path $cache)) { New-Item -ItemType Directory -Force -Path $cache | Out-Null }
    $raw = Join-Path $cache $Image.file
    $want = "$($Image.sha512)".ToUpper()
    if ((Test-Path $raw) -and (Get-FileHash -Algorithm SHA512 $raw).Hash -eq $want) { return $raw }
    $url = Get-ImageUrl $Image
    $tmp = "$raw.download"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 3600
    $got = (Get-FileHash -Algorithm SHA512 $tmp).Hash
    if ($got -ne $want) { Remove-Item -Force $tmp; throw "downloaded image SHA-512 $($got.Substring(0,16))... does not match vms.json: not used" }
    Move-Item -Force $tmp $raw
    Get-ChildItem $cache -File | Where-Object { $_.Name -ne $Image.file } | Remove-Item -Force -ErrorAction SilentlyContinue   # old versions
    return $raw
}

function New-SeedDisk([string]$Path, [string]$UserData, [string]$MetaData) {
    # NoCloud datasource: a FAT volume labelled CIDATA holding user-data and meta-data.
    if (Test-Path $Path) { Remove-Item -Force $Path }
    New-VHD -Path $Path -SizeBytes 64MB -Dynamic | Out-Null
    $disk = Mount-VHD -Path $Path -Passthru | Get-Disk
    try {
        Initialize-Disk -Number $disk.Number -PartitionStyle MBR
        $part = New-Partition -DiskNumber $disk.Number -UseMaximumSize -AssignDriveLetter
        Format-Volume -Partition $part -FileSystem FAT32 -NewFileSystemLabel CIDATA -Confirm:$false -Force | Out-Null
        $letter = (Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber).DriveLetter
        if (-not $letter) { throw 'the seed disk got no drive letter' }
        $utf8 = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText("${letter}:\user-data", $UserData, $utf8)
        [IO.File]::WriteAllText("${letter}:\meta-data", $MetaData, $utf8)
    } finally {
        Dismount-VHD -Path $Path
    }
}

function New-HomelabVm([string]$Name, $Def, $Image, [string]$Marker) {
    $inputs = Get-VmInputs $Name $Def                         # all checks before anything is created
    $template = Join-Path $Dir "$($Def.role).user-data"
    if (-not (Test-Path $template)) { throw "no first-boot template $($Def.role).user-data" }
    $userData = Format-UserData (Get-Content -Raw $template) $inputs
    if ($Def.mac -notmatch '^([0-9A-Fa-f]{2}[:-]?){5}[0-9A-Fa-f]{2}$') { throw "mac '$($Def.mac)' is not a MAC address" }
    $mac = ($Def.mac -replace '[:-]', '').ToUpper()
    $root = Join-Path $Def.path $Name
    $share = Test-InsideShare $root
    if ($share) { throw "VM path $root is inside the share '$share': refusing (a mirror or a user could delete it)" }

    Protect-Folder $Dir
    if (-not (Test-Path $root)) { New-Item -ItemType Directory -Force -Path $root | Out-Null }
    Protect-Folder $root
    $disk = Join-Path $root "$Name.vhdx"
    $seed = Join-Path $root "$Name-seed.vhdx"
    if (Test-Path $disk) { throw "$disk already exists but there is no VM ${Name}: move it away (it is never overwritten)" }

    $raw = Get-BaseImage $Image
    $vhd = Join-Path $root "$Name-build.vhd"
    try {
        Copy-Item -Force $raw $vhd
        Add-FixedVhdFooter $vhd
        Convert-VHD -Path $vhd -DestinationPath $disk -VHDType Dynamic
    } finally { Remove-Item -Force $vhd -ErrorAction SilentlyContinue }
    Resize-VHD -Path $disk -SizeBytes ([int64]$Def.disk_gb * 1GB)
    New-SeedDisk $seed $userData "instance-id: $Name-$(Get-DefFingerprint $Def)`nlocal-hostname: $Name`n"

    $vm = New-VM -Name $Name -Generation 2 -MemoryStartupBytes ([int64]$Def.memory_mb * 1MB) -VHDPath $disk -SwitchName $Def.switch -Path $root
    Set-VM -VM $vm -ProcessorCount ([int]$Def.cpu) -StaticMemory -AutomaticStartAction Start -AutomaticStopAction ShutDown -Notes $Marker
    if ($Def.secure_boot -eq $false) { Set-VMFirmware -VM $vm -EnableSecureBoot Off }
    else { Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority }
    Add-VMHardDiskDrive -VM $vm -Path $seed
    Get-VMNetworkAdapter -VM $vm | Set-VMNetworkAdapter -StaticMacAddress $mac
    Set-VMFirmware -VM $vm -FirstBootDevice (Get-VMHardDiskDrive -VM $vm | Where-Object { $_.Path -eq $disk })
    Start-VM -VM $vm
}

# ---------- main ----------

$doc = Get-Content -Raw (Join-Path $Dir 'vms.json') | ConvertFrom-Json
$mine = @($doc.vms.PSObject.Properties | Where-Object { "$($_.Value.host)".ToLower() -eq $HostName.ToLower() -and $_.Value.enabled -ne $false })
if (-not $mine.Count) { return $result }

if (-not (Get-Command New-VM -ErrorAction SilentlyContinue)) {
    $result.pending += "Hyper-V is not enabled on $HostName (one-time: Install-WindowsFeature Hyper-V -IncludeManagementTools -Restart)"
    return $result
}

foreach ($p in $mine) {
    $name, $def = $p.Name, $p.Value
    try {
        $marker = "[homelab-managed host:$($HostName.ToLower()) def:$(Get-DefFingerprint $def)]"
        $vm = Get-VM -Name $name -ErrorAction SilentlyContinue
        if ($vm) {
            if ("$($vm.Notes)" -notmatch '\[homelab-managed host:') { $result.errors += "a VM named $name exists but wasn't created from vms.json: not touching it"; continue }
            if (-not "$($vm.Notes)".Contains($marker)) { $result.pending += "$name differs from vms.json (delete the VM to rebuild it from the new definition)" }
            else { $result.unchanged += $name }
            continue
        }
        $image = $doc.images.($def.image)
        if (-not $image) { throw "image '$($def.image)' is not in vms.json" }
        if ("$($image.sha512)" -notmatch '^[0-9a-fA-F]{128}$' -or -not $image.file) {
            $result.pending += "$name waits for the image in vms.json (file name and SHA-512 from Debian's SHA512SUMS)"; continue
        }
        if (-not (Get-VMSwitch -Name $def.switch -ErrorAction SilentlyContinue)) {
            $result.pending += "$name waits for the virtual switch '$($def.switch)' (one-time, see vms/README.md)"; continue
        }
        New-HomelabVm $name $def $image $marker
        $result.updated += "VM $name created and started (it posts its deploy key to the Deployments screen)"
    } catch {
        $result.errors += "${name}: $($_.Exception.Message)"
    }
}
return $result
