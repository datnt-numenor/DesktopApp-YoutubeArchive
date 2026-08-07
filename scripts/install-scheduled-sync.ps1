param(
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$Time = "12:00"
)

$ErrorActionPreference = "Stop"

$taskName = "Desktop YouTube Archive - Daily MP3 Sync"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$runner = Join-Path $PSScriptRoot "scheduled_sync.py"

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "Python environment not found: $pythonw"
}
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Scheduled sync runner not found: $runner"
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"{0}"' -f $runner) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Sync enabled YouTube Archive playlists and download new MP3 files every day at 12:00." `
    -Force | Out-Null

Write-Output "Installed task: $taskName"
Write-Output "Schedule: daily at $Time, starts only on AC power, runs when the user is logged on."
