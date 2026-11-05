param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^ROOT\\(?:DISPLAY|MTTVDD)\\[A-Za-z0-9&_.-]+$')]
    [string] $InstanceId
)

$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Enable-PnpDevice must run through the normal Windows UAC approval flow.'
}

$device = Get-PnpDevice -InstanceId $InstanceId -ErrorAction Stop
if ($device.Class -ne 'Display' -or
    $device.Manufacturer -ne 'MikeTheTech' -or
    $device.FriendlyName -ne 'Virtual Display Driver') {
    throw 'The exact MikeTheTech display device identity did not match. No action was performed.'
}

if ($device.Status -eq 'OK') {
    exit 0
}

Enable-PnpDevice -InstanceId $InstanceId -Confirm:$false -ErrorAction Stop

$deadline = [DateTime]::UtcNow.AddSeconds(25)
do {
    Start-Sleep -Milliseconds 250
    $device = Get-PnpDevice -InstanceId $InstanceId -ErrorAction Stop
    if ($device.Class -ne 'Display' -or
        $device.Manufacturer -ne 'MikeTheTech' -or
        $device.FriendlyName -ne 'Virtual Display Driver') {
        throw 'Device identity changed while verifying. No further action was performed.'
    }
    if ($device.Status -eq 'OK') {
        exit 0
    }
} while ([DateTime]::UtcNow -lt $deadline)

exit 22
