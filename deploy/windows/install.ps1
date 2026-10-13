# Install (or remove with -Remove) a Scheduled Task that runs `ninshubur run --daemon` at logon.
# Usage: powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 [-Remove]
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$TaskName = "Ninshubur"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$HomeDir = if ($env:NINSHUBUR_HOME) { $env:NINSHUBUR_HOME } else { Join-Path $env:USERPROFILE ".ninshubur" }

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed task $TaskName"
    exit 0
}
if (-not (Test-Path $Py)) { throw "no venv at $Root\.venv; create it and pip install -e . first" }
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$Action = New-ScheduledTaskAction -Execute $Py -Argument "-m ninshubur run --daemon" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -RunLevel Limited -Force | Out-Null
[Environment]::SetEnvironmentVariable("NINSHUBUR_HOME", $HomeDir, "User")
Start-ScheduledTask -TaskName $TaskName
Write-Host "installed task $TaskName (data in $HomeDir)"
