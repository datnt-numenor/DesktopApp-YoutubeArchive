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
$lastRun = $null
if ($info.LastRunTime -and $info.LastRunTime.Year -gt 1900) {
    $lastRun = $info.LastRunTime.ToString("yyyy-MM-dd HH:mm:ss")
}
$lastResult = [uint32]($info.LastTaskResult -band 0xffffffffL)

[pscustomobject]@{
    time = $start.ToString("HH:mm")
    state = [string]$task.State
    next_run_time = $nextRun
    last_run_time = $lastRun
    last_task_result = [long]$lastResult
    last_task_result_hex = ("0x{0:X8}" -f $lastResult)
    wake_to_run = [bool]$task.Settings.WakeToRun
    disallow_start_if_on_batteries = [bool]$task.Settings.DisallowStartIfOnBatteries
    start_when_available = [bool]$task.Settings.StartWhenAvailable
} | ConvertTo-Json -Compress
