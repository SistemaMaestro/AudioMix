# install-service.ps1
# Registers AudioMix as a Windows Task Scheduler task so it starts
# automatically at system boot (no login required) and at user logon.
#
# Usage (run as Administrator):
#   cd "C:\Projetos GIT\AudioMix"
#   .\install\install-service.ps1
#
# Options:
#   -AudioMixDir  "C:\path\to\AudioMix"   (default: parent of this script)
#   -Python       "python"                  (default: python)
#   -Unregister                             (remove tasks instead of creating)

param(
    [string]$AudioMixDir = (Split-Path -Parent $PSScriptRoot),
    [string]$Python = "python",
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$TASK_BOOT  = "AudioMix (boot)"
$TASK_LOGON = "AudioMix (logon)"

if ($Unregister) {
    foreach ($name in @($TASK_BOOT, $TASK_LOGON)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed: $name" -ForegroundColor Yellow
        } else {
            Write-Host "Not found: $name" -ForegroundColor Gray
        }
    }
    Write-Host "AudioMix tasks unregistered." -ForegroundColor Cyan
    exit 0
}

# Verify we can find AudioMix.py
$entry = Join-Path $AudioMixDir "AudioMix.py"
if (-not (Test-Path $entry)) {
    Write-Error "AudioMix.py not found at: $entry`nPass -AudioMixDir to specify the correct path."
}

# Resolve python exe
$pythonExe = (Get-Command $Python -ErrorAction SilentlyContinue)?.Source
if (-not $pythonExe) {
    Write-Error "Python not found. Install Python or pass -Python 'C:\...\python.exe'"
}

Write-Host "AudioMix dir : $AudioMixDir"
Write-Host "Python exe   : $pythonExe"

# Build action
$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "`"$entry`"" `
    -WorkingDirectory $AudioMixDir

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

# ── Task 1: at system startup, run as SYSTEM (no login required) ──────────
$triggerBoot = New-ScheduledTaskTrigger -AtStartup
$principalBoot = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

if (Get-ScheduledTask -TaskName $TASK_BOOT -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TASK_BOOT -Confirm:$false
}
Register-ScheduledTask `
    -TaskName $TASK_BOOT `
    -Action $action `
    -Trigger $triggerBoot `
    -Principal $principalBoot `
    -Settings $settings `
    -Description "AudioMix StudioLive gateway — starts at system boot" `
    | Out-Null
Write-Host "Created : $TASK_BOOT  (SYSTEM, AtStartup)" -ForegroundColor Green

# ── Task 2: at any user logon, run as that user (catches boot-fail cases) ─
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn
$principalLogon = New-ScheduledTaskPrincipal `
    -GroupId "BUILTIN\Users" `
    -RunLevel Highest

if (Get-ScheduledTask -TaskName $TASK_LOGON -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TASK_LOGON -Confirm:$false
}
Register-ScheduledTask `
    -TaskName $TASK_LOGON `
    -Action $action `
    -Trigger $triggerLogon `
    -Principal $principalLogon `
    -Settings $settings `
    -Description "AudioMix StudioLive gateway — starts at user logon (failsafe)" `
    | Out-Null
Write-Host "Created : $TASK_LOGON  (Users, AtLogOn)" -ForegroundColor Green

Write-Host ""
Write-Host "AudioMix will now start automatically." -ForegroundColor Cyan
Write-Host "Admin UI: http://localhost:47900/admin"
Write-Host "API docs: http://localhost:47900/api/docs"
Write-Host ""
Write-Host "To remove: .\install\install-service.ps1 -Unregister"
