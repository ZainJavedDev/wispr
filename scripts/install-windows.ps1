# Installs Wispr for the current Windows user and runs it at logon.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#
# A real Windows service runs in session 0, which cannot reach the user's
# microphone, clipboard, or keyboard, so the daemon runs as a per-user
# scheduled task instead (the counterpart of the Linux systemd user service).
$ErrorActionPreference = 'Stop'

$TaskName = 'Wispr Dictation'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPath = Join-Path $ProjectRoot '.venv'
$Scripts = Join-Path $VenvPath 'Scripts'
$BinDir = Join-Path $env:USERPROFILE '.local\bin'
$EnvFile = Join-Path $ProjectRoot '.env'

function Find-Python {
    $probe = 'import sys; assert sys.version_info >= (3, 11); print(sys.executable)'
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # Prefer 3.12: every dependency ships Windows wheels for it.
        $candidates += '-3.12', '-3.13', '-3.11', '-3' | ForEach-Object { , @('py', $_) }
    }
    $candidates += , @('python')
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        $arguments = @($candidate | Select-Object -Skip 1) + @('-c', $probe)
        $path = & $candidate[0] $arguments | Select-Object -Last 1
        if ($LASTEXITCODE -eq 0 -and $path) { return $path }
    }
    throw 'Python 3.11+ is required (install it from python.org).'
}

$python = Find-Python
Write-Host "Creating virtual environment in $VenvPath with $python"
& $python -m venv $VenvPath
if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
& "$Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Scripts\python.exe" -m pip install --quiet --editable $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }

# Command-line shim, mirroring ~/.local/bin/wispr on Linux.
New-Item -ItemType Directory -Force $BinDir | Out-Null
Set-Content -Encoding ascii (Join-Path $BinDir 'wispr.cmd') "@`"$Scripts\wispr.exe`" %*"
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($userPath -split ';') -notcontains $BinDir) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$BinDir"), 'User')
    Write-Host "Added $BinDir to your user PATH (open a new terminal to use it)"
}

$action = New-ScheduledTaskAction -Execute "$Scripts\wisprw.exe" -Argument 'daemon' `
    -WorkingDirectory $ProjectRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# Task Scheduler only restarts tasks that fail to launch, not ones that exit
# with an error. This repeating trigger is the watchdog: with IgnoreNew it is a
# no-op while the daemon runs and relaunches it within 5 minutes if it died.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
# Defaults would stop the task on battery, after 3 days, and run it at
# below-normal priority; none of that suits an always-on dictation daemon.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -Priority 4
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon, $watchdog `
    -Principal $principal -Settings $settings -Force `
    -Description 'Wispr Gemini voice dictation (Win+Alt+Space)' | Out-Null
Write-Host "Registered scheduled task '$TaskName' (starts at logon)"

$hasKey = (Test-Path $EnvFile) -and
    (Select-String -Path $EnvFile -Pattern '^\s*GEMINI_API_KEY\s*=\s*\S' -Quiet)
if (-not $hasKey) {
    if (-not (Test-Path $EnvFile)) {
        Set-Content -Encoding ascii $EnvFile "GEMINI_API_KEY=`n"
    }
    Write-Warning "Add your key to $EnvFile, then run: Start-ScheduledTask -TaskName '$TaskName'"
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Write-Host "Wispr is running. Press Win+Alt+Space to dictate."
