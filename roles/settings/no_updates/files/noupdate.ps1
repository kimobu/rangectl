$ErrorActionPreference = 'Continue'

function Set-Dword {
  param(
    [string]$Path,
    [string]$Name,
    [int]$Value
  )
  if (-not (Test-Path $Path)) {
    New-Item -Path $Path -Force | Out-Null
  }
  New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}

$windowsUpdatePolicy = 'HKLM:\Software\Policies\Microsoft\Windows\WindowsUpdate'
$automaticUpdatePolicy = Join-Path $windowsUpdatePolicy 'AU'
Set-Dword -Path $automaticUpdatePolicy -Name NoAutoUpdate -Value 1
Set-Dword -Path $automaticUpdatePolicy -Name AUOptions -Value 1
Set-Dword -Path $automaticUpdatePolicy -Name ScheduledInstallDay -Value 0
Set-Dword -Path $automaticUpdatePolicy -Name ScheduledInstallTime -Value 3
Set-Dword -Path $windowsUpdatePolicy -Name DisableOSUpgrade -Value 1
Set-Dword -Path $windowsUpdatePolicy -Name DisableWindowsUpdateAccess -Value 1
Set-Dword -Path $windowsUpdatePolicy -Name DoNotConnectToWindowsUpdateInternetLocations -Value 1
Set-Dword -Path 'HKLM:\Software\Policies\Microsoft\Windows\DeliveryOptimization' -Name DODownloadMode -Value 0
Set-Dword -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update' -Name AUOptions -Value 1
Set-Dword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' -Name PreventDeviceEncryption -Value 1
Set-Dword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard' -Name EnableVirtualizationBasedSecurity -Value 0
Set-Dword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard' -Name RequirePlatformSecurityFeatures -Value 0
Set-Dword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -Name Enabled -Value 0
Set-Dword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -Name LsaCfgFlags -Value 0
Set-Dword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name EnableVirtualizationBasedSecurity -Value 0
Set-Dword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name LsaCfgFlags -Value 0
Set-Dword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name RequirePlatformSecurityFeatures -Value 0

try {
  bcdedit.exe /set '{current}' hypervisorlaunchtype off | Out-Null
  bcdedit.exe /set '{current}' vsmlaunchtype off | Out-Null
  bcdedit.exe /set '{current}' loadoptions DISABLE-LSA-ISO | Out-Null
} catch {
  Write-Output "rangectl VBS bcdedit warning: $($_.Exception.Message)"
}

if (Get-Command manage-bde.exe -ErrorAction SilentlyContinue) {
  $bitlockerStatus = manage-bde.exe -status C: 2>$null | Out-String
  if ($LASTEXITCODE -eq 0 -and $bitlockerStatus -match 'Conversion Status:\s+(?!Fully Decrypted)') {
    manage-bde.exe -off C: | Out-Null
  }
}

$services = @(
  'wuauserv',
  'UsoSvc',
  'WaaSMedicSvc',
  'BITS',
  'DoSvc',
  'InstallService'
)
foreach ($serviceName in $services) {
  $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
  if ($service -and $service.Status -ne 'Stopped') {
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
  }
  sc.exe config $serviceName start= disabled | Out-Null
  $serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
  if (Test-Path $serviceKey) {
    Set-ItemProperty -Path $serviceKey -Name Start -Value 4 -ErrorAction SilentlyContinue
  }
}

$taskRoots = @(
  '\Microsoft\Windows\WindowsUpdate\',
  '\Microsoft\Windows\UpdateOrchestrator\',
  '\Microsoft\Windows\WaaSMedic\',
  '\Microsoft\Windows\InstallService\',
  '\Microsoft\Windows\USO_UxBroker\'
)
foreach ($taskRoot in $taskRoots) {
  Get-ScheduledTask -TaskPath $taskRoot -ErrorAction SilentlyContinue |
    Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null
}

Write-Output 'rangectl Windows automatic updates disabled'
