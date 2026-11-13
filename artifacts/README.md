# Local artifacts

This repository does not track installers, archives, disk images, or course
payloads. Download each required file from its publisher and stage it at the
path expected by the consuming role. Git ignores the paths below.

## Sysmon

The `sysmon/install` role requires the 64-bit Windows Sysmon executable:

```text
roles/sysmon/install/files/Sysmon/Sysmon64.exe
```

Microsoft publishes Sysmon on the
[Sysmon documentation page](https://learn.microsoft.com/sysinternals/downloads/sysmon).
Its official ZIP download is:

```text
https://download.sysinternals.com/files/Sysmon.zip
```

From the repository root, download the current archive and extract only the
required executable:

```bash
mkdir -p roles/sysmon/install/files/Sysmon
curl --fail --location \
  https://download.sysinternals.com/files/Sysmon.zip \
  --output /tmp/rangectl-Sysmon.zip
unzip -j /tmp/rangectl-Sysmon.zip Sysmon64.exe \
  -d roles/sysmon/install/files/Sysmon
```

The role fails with the required path and URL when an endpoint needs Sysmon and
the controller has no staged executable.

## Windows Management Framework 5.1

The `powershell_upgrade` role uses WMF 5.1 on Windows Server 2012 R2 hosts whose
PowerShell major version is below 5. Download WMF 5.1 from the
[Microsoft Download Center](https://www.microsoft.com/en-us/download/details.aspx?id=54616)
or locate KB3191564 in the
[Microsoft Update Catalog](https://www.catalog.update.microsoft.com/Search.aspx?q=KB3191564).

Select this file:

```text
Win8.1AndW2K12R2-KB3191564-x64.msu
```

Save it at:

```text
playbooks/files/Win8.1AndW2K12R2-KB3191564-x64.msu
```

The Download Center identifies the file as the 19 MB x64 package for Windows
8.1 and Windows Server 2012 R2. Check the downloaded file's Microsoft signature
before using it.

## Optional offline NuGet provider

Older Windows images may need the NuGet PackageManagement provider before they
can install PowerShell modules. In a connected lab, the `powershell_upgrade`
role installs the provider from the PowerShell Gallery. For a disconnected lab,
stage a trusted copy at:

```text
playbooks/files/Microsoft.PackageManagement.NuGetProvider-2.8.5.208.dll
```

## Course and Security Onion artifacts

Content packs and roles may also expect these local files:

- `artifacts/office/files.zip`
- `artifacts/office/chromedriver-win64.zip`
- `artifacts/securityonion/so-elastic-agent_darwin_amd64`
- `artifacts/securityonion/so-elastic-agent_darwin_arm64`
- `artifacts/securityonion/so-elastic-agent_linux_amd64`
- `artifacts/securityonion/so-elastic-agent_windows_amd64.exe`

The `seconion/artifacts` role can download Security Onion Elastic Agent
installers from the package endpoint configured by the range. If the endpoint
requires web authentication, create `~/.seconion.env` on the controller:

```bash
SECONION_PACKAGE_USERNAME='...'
SECONION_PACKAGE_PASSWORD='...'
```

The role uses these credentials for the browser login flow. It refreshes the
selected cached artifacts by default and rejects an HTML login page in place of
an installer.
