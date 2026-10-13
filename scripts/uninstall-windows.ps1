# Removes the Wispr logon task and command shim. Leaves .venv and .env alone.
#
#   powershell -ExecutionPolicy Bypass -File scripts\uninstall-windows.ps1
$ErrorActionPreference = 'Stop'

$TaskName = 'Wispr Dictation'
$BinDir = Join-Path $env:USERPROFILE '.local\bin'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'"
}
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $BinDir 'wispr.cmd')
Write-Host 'Wispr uninstalled.'
