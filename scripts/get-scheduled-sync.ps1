$ErrorActionPreference = "Stop"

$taskName = "Desktop YouTube Archive - Daily MP3 Sync"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Error "Scheduled task is not installed: $taskName"
    exit 3
}

$info = Get-ScheduledTaskInfo -TaskName $taskName
$trigger = $task.Triggers | Select-Object -First 1
$start = [datetime]$trigger.StartBoundary
$nextRun = $null
if ($info.NextRunTime -and $info.NextRunTime.Year -gt 1900) {
    $nextRun = $info.NextRunTime.ToString("yyyy-MM-dd HH:mm")
}

[pscustomobject]@{
    time = $start.ToString("HH:mm")
    state = [string]$task.State
    next_run_time = $nextRun
} | ConvertTo-Json -Compress
