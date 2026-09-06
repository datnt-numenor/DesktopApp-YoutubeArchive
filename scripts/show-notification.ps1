param(
    [Parameter(Mandatory = $true)]
    [string]$Title,

    [Parameter(Mandatory = $true)]
    [string]$Message,

    [ValidateSet("Info", "Warning", "Error")]
    [string]$Level = "Info",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ($DryRun) {
    [pscustomobject]@{
        title = $Title
        message = $Message
        level = $Level
    } | ConvertTo-Json -Compress
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$notification = New-Object System.Windows.Forms.NotifyIcon
try {
    $notification.Icon = [System.Drawing.SystemIcons]::Information
    $notification.Visible = $true
    $notification.BalloonTipTitle = $Title
    $notification.BalloonTipText = $Message
    $notification.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::$Level
    $notification.ShowBalloonTip(5000)
    Start-Sleep -Seconds 6
}
finally {
    $notification.Visible = $false
    $notification.Dispose()
}
