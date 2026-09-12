# Registers "FareWatcher" to run every 5 minutes, SILENTLY, via Task Scheduler.
# Silent = launched with pythonw.exe (no console window ever appears).
# Run this ONCE:  .\register_task.ps1   (elevate if it reports an access error)
# Re-running updates the existing task.

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$watcher   = Join-Path $scriptDir "fare_watcher.py"

# Prefer pythonw.exe (windowless). Fall back to python.exe if not found.
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
$exe = if (Test-Path $pythonw) { $pythonw } else { $python }

$action = New-ScheduledTaskAction -Execute $exe -Argument "`"$watcher`"" -WorkingDirectory $scriptDir

# Every 5 minutes, effectively forever (10 years), starting now.
# (TimeSpan.MaxValue is rejected by Task Scheduler as out-of-range, so use a
#  large but valid duration.)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Hidden + start when available + run on battery (critical for laptops: the
# default power conditions leave the task stuck "Queued" on battery power).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -RunOnlyIfNetworkAvailable -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

# Run as the current user, only when logged on (no stored password needed).
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "FareWatcher" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Polls free flight-deal feeds every 5 min and pushes mistake-fare alerts to Telegram. Runs silently." `
    -Force | Out-Null

Write-Host "Registered 'FareWatcher' (every 5 min, silent via $([System.IO.Path]::GetFileName($exe)))."
Write-Host "Manage it in Task Scheduler, or run:  Get-ScheduledTask FareWatcher | Get-ScheduledTaskInfo"
