<#
.SYNOPSIS
    Start DAZIA automatically when you log in to Windows (Phase 8). No Administrator needed.

.DESCRIPTION
    Creates or updates the Task Scheduler task "DAZIA" for the current user:
      - runs .venv\Scripts\pythonw.exe -m app.main from the project folder (no console window)
      - starts 30 seconds after you log in (Ollama starts by itself from its own Startup shortcut)
      - keeps running on battery and has no time limit
      - re-checks every 5 minutes: if DAZIA crashed it is started again; if it is running nothing happens
        (Task Scheduler's own "restart on failure" does not react to a crash, so this is the restart)
    Output goes to logs\dazia.log. Settings still come from .env as usual.

.PARAMETER Remove
    Stop DAZIA if the task is running and delete the task.

    To stop DAZIA for a while without uninstalling, disable the task first (otherwise the 5-minute check
    starts it again):  Disable-ScheduledTask DAZIA; Stop-ScheduledTask DAZIA
    Turn it back on:   Enable-ScheduledTask DAZIA; Start-ScheduledTask DAZIA

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1
    powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove
#>
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'DAZIA'
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $ProjectDir '.venv\Scripts\pythonw.exe'
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed task '$TaskName'. DAZIA will no longer start at login."
    } else {
        Write-Host "No task named '$TaskName' was found. Nothing to remove."
    }
    exit 0
}

if (-not (Test-Path $Pythonw)) {
    Write-Error "Not found: $Pythonw. Create the virtual environment first (see README > Install)."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument '-m app.main' -WorkingDirectory $ProjectDir
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$atLogon.Delay = 'PT30S'
# Crash recovery: every 5 minutes from now on, forever (runs only while you are logged in).
$every5min = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
$trigger = @($atLogon, $every5min)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
# Interactive + Limited: runs only while you are logged in, as you, without admin rights (needed for toasts).
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Description 'DAZIA personal assistant (local).' `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Task '$TaskName' installed: DAZIA starts 30 seconds after you log in."
Write-Host ''
Write-Host "Start now:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop:        Disable-ScheduledTask -TaskName $TaskName; Stop-ScheduledTask -TaskName $TaskName"
Write-Host "Re-enable:   Enable-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName"
Write-Host "Log file:    $(Join-Path $ProjectDir 'logs\dazia.log')"
Write-Host 'Uninstall:   run this script again with -Remove.'
