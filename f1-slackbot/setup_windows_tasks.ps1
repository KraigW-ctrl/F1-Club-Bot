# Creates two Windows scheduled tasks for the F1 Club Bot.
# Run from the f1-slackbot folder in PowerShell:  .\setup_windows_tasks.ps1
# (If scripts are blocked:  Set-ExecutionPolicy -Scope Process Bypass)

$ErrorActionPreference = "Stop"
$here   = $PSScriptRoot
$python = Join-Path $here ".venv\Scripts\pythonw.exe"   # pythonw = no console window popping up
$bot    = Join-Path $here "bot.py"
$log    = Join-Path $here "bot.log"

if (-not (Test-Path $python)) { throw "Can't find $python - create the venv first (see README)." }

# Save the Slack settings as user environment variables (read by the tasks).
if (-not [Environment]::GetEnvironmentVariable("SLACK_BOT_TOKEN", "User")) {
    $secure = Read-Host "Paste the Slack bot token" -AsSecureString
    $token  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    [Environment]::SetEnvironmentVariable("SLACK_BOT_TOKEN", $token, "User")
}
[Environment]::SetEnvironmentVariable("SLACK_CHANNEL_ID", "C09CMJ8ASBW", "User")

# StartWhenAvailable = if the PC was off/asleep at the scheduled time, run as soon as it's back.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries `
            -AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# 1) Race week: daily at 9am. The bot only posts in a race week, once.
$raceAction  = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $here `
               -Argument "/c `"`"$python`" `"$bot`" race-week >> `"$log`" 2>&1`""
$raceTrigger = New-ScheduledTaskTrigger -Daily -At 9:00am
Register-ScheduledTask -TaskName "F1 Club Bot - Race Week" -Action $raceAction `
    -Trigger $raceTrigger -Settings $settings -Force | Out-Null

# 2) Breaking news: every 15 minutes.
$newsAction  = New-ScheduledTaskAction -Execute "cmd.exe" -WorkingDirectory $here `
               -Argument "/c `"`"$python`" `"$bot`" breaking >> `"$log`" 2>&1`""
$newsTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
               -RepetitionInterval (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "F1 Club Bot - Breaking News" -Action $newsAction `
    -Trigger $newsTrigger -Settings $settings -Force | Out-Null

Write-Host "Done. Two tasks created in Task Scheduler (look for 'F1 Club Bot')."
Write-Host "Output is logged to $log"
Write-Host "To remove later: Unregister-ScheduledTask -TaskName 'F1 Club Bot*' -Confirm:`$false"
