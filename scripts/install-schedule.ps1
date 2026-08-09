<#
Registers the "FeaturePhoneClank HMD Soak" Windows Scheduled Task: four
daily triggers (06:30, 12:30, 18:30, 22:30 local time) running
run-silent.vbs -> run-production.cmd -> `feature-phone-clank run` (run
lock always on).

Invoked by install-schedule.cmd, not meant to be run standalone (though
it's harmless to). Uses Register-ScheduledTask with Execute/Argument as
separate fields, never a single combined "program args" string - this
project's folder name has a space in it ("feature phone clank"), and
schtasks.exe's /tr interface (a single combined string) is known to
mis-split or corrupt a spaced path in that position (confirmed the hard
way on OEM Radar; see that project's install-hourly-task.ps1 for the full
writeup). A real .ps1 with named parameters sidesteps it entirely.
#>

param(
    [string]$VbsPath = (Join-Path $PSScriptRoot "run-silent.vbs"),
    [string]$TaskName = "FeaturePhoneClank HMD Soak"
)

$ErrorActionPreference = "Stop"

try {
    if (-not (Test-Path -LiteralPath $VbsPath)) {
        Write-Error "run-silent.vbs not found at: $VbsPath"
        exit 1
    }

    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`""

    $times = @("06:30", "12:30", "18:30", "22:30")
    $triggers = $times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }

    # Interactive logon, Limited run level, current user: no stored
    # password, no elevation - runs only while this user is logged in.
    # Matches the posture of this machine's other scheduled Clank tasks.
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited

    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -DontStopOnIdleEnd `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null

    Write-Output "SUCCESS: registered scheduled task '$TaskName' (06:30, 12:30, 18:30, 22:30 daily)"
    exit 0
} catch {
    Write-Error "FAILED to register scheduled task: $_"
    exit 1
}
