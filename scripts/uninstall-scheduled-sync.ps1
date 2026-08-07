$ErrorActionPreference = "Stop"

$taskName = "Desktop YouTube Archive - Daily MP3 Sync"
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed task: $taskName"
} else {
    Write-Output "Task is not installed: $taskName"
}
