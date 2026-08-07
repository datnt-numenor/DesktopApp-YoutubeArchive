param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')]
    [string]$Time
)

$ErrorActionPreference = "Stop"
$taskName = "Desktop YouTube Archive - Daily MP3 Sync"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Scheduled task is not installed: $taskName"
}

$trigger = New-ScheduledTaskTrigger -Daily -At $Time
Set-ScheduledTask -TaskName $taskName -Trigger $trigger | Out-Null
Write-Output "Updated task time to $Time"
