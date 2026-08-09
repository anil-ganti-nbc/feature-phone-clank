<#
Read-only status check for the "FeaturePhoneClank HMD Soak" scheduled task:
state, next run time, last run time/result. Safe to run anytime, no
elevation required.
#>

param([string]$TaskName = "FeaturePhoneClank HMD Soak")

$ErrorActionPreference = "Stop"

try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    Write-Output "Task:            $TaskName"
    Write-Output "State:           $($task.State)"
    Write-Output "Last run time:   $($info.LastRunTime)"
    Write-Output "Last result:     $($info.LastTaskResult)  (0 = success)"
    Write-Output "Next run time:   $($info.NextRunTime)"
    Write-Output "Number of tasks: 1 task, 4 daily triggers (06:30, 12:30, 18:30, 22:30)"
    Write-Output ""
    Write-Output "Triggers:"
    foreach ($t in $task.Triggers) {
        Write-Output "  - StartBoundary=$($t.StartBoundary) Enabled=$($t.Enabled)"
    }
    exit 0
} catch {
    Write-Output "Task '$TaskName' is not registered (or query failed): $_"
    exit 1
}
