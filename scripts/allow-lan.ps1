<#
.SYNOPSIS
    Let phones on your home Wi-Fi reach DAZIA (Phase 3). Run as Administrator.

.DESCRIPTION
    1. Checks that your current network is marked "Private" (home). Stops if it is "Public"
       (cafe, airport...), because DAZIA must never be opened to strangers' networks.
    2. Creates or updates the inbound firewall rule "DAZIA (LAN)":
       TCP <Port>, Private profile only, and only from devices on your local subnet.
    3. Prints this laptop's Wi-Fi/LAN address(es) and the URL to open on your phone.

    Before using it: set a passphrase (python -m app.auth) and DAZIA_HOST=0.0.0.0 in .env.

.PARAMETER Port
    The DAZIA port (DAZIA_PORT in .env). Default 8000.

.PARAMETER Remove
    Delete the "DAZIA (LAN)" firewall rule and exit.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1
    powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1 -Port 8001
    powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1 -Remove
#>
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$RuleName = 'DAZIA (LAN)'

# Firewall changes need an elevated (Administrator) PowerShell.
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error 'Please run this script from PowerShell opened with "Run as administrator".'
    exit 1
}

if ($Remove) {
    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Remove-NetFirewallRule
        Write-Host "Removed firewall rule '$RuleName'. Phones can no longer reach DAZIA."
    } else {
        Write-Host "No firewall rule named '$RuleName' was found. Nothing to remove."
    }
    exit 0
}

# 1. The network you are connected to must be Private (home), never Public.
$profiles = @(Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne 'Disconnected' -or $_.IPv6Connectivity -ne 'Disconnected' })
if ($profiles.Count -eq 0) {
    Write-Error 'No active network connection found. Connect to your home Wi-Fi first.'
    exit 1
}
$public = @($profiles | Where-Object { $_.NetworkCategory -eq 'Public' })
if ($public.Count -gt 0) {
    foreach ($p in $public) {
        Write-Warning "Network '$($p.Name)' on '$($p.InterfaceAlias)' is marked Public."
    }
    Write-Warning 'Stopping: only open DAZIA on a trusted home network.'
    Write-Warning 'If this IS your home Wi-Fi: Settings > Network & internet > Wi-Fi > (your network) > Network profile type > Private, then run this again.'
    exit 1
}

# 2. Create or update the firewall rule: this port, Private profile, local subnet only.
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Set-NetFirewallRule -Direction Inbound -Action Allow -Profile Private -Enabled True
    $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
    $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
    Write-Host "Updated firewall rule '$RuleName' (TCP $Port, Private networks, local subnet only)."
} else {
    New-NetFirewallRule -DisplayName $RuleName -Description 'DAZIA personal assistant, home Wi-Fi only.' `
        -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
        -Profile Private -RemoteAddress LocalSubnet | Out-Null
    Write-Host "Created firewall rule '$RuleName' (TCP $Port, Private networks, local subnet only)."
}

# 3. Show the address(es) to type on the phone.
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceIndex -in $profiles.InterfaceIndex -and $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' })
if ($addresses.Count -eq 0) {
    Write-Warning 'Could not find a LAN IPv4 address. Check with: ipconfig'
} else {
    Write-Host ''
    Write-Host 'On your phone (same Wi-Fi), open:'
    foreach ($a in $addresses) {
        Write-Host "    http://$($a.IPAddress):$Port    ($($a.InterfaceAlias))"
    }
}
Write-Host ''
Write-Host 'Reminder: .env must contain DAZIA_PASSPHRASE_HASH=... and DAZIA_HOST=0.0.0.0, then restart DAZIA.'
Write-Host 'To undo: run this script again with -Remove.'
