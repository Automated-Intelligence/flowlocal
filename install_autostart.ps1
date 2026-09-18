# Make FlowLocal always-on: start at login, and re-check every 10 minutes.
# Run once from this folder:  powershell -ExecutionPolicy Bypass -File install_autostart.ps1
# Undo:                       powershell -ExecutionPolicy Bypass -File install_autostart.ps1 -Remove
param([switch]$Remove)

$root = $PSScriptRoot
$vbs = Join-Path $root "ensure_running.vbs"
$shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\FlowLocal.lnk"
$task = "FlowLocal Watchdog"

if ($Remove) {
    Remove-Item $shortcut -ErrorAction SilentlyContinue
    schtasks /Delete /TN $task /F 2>$null
    Write-Host "Auto-start removed."
    exit
}

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($shortcut)
$sc.TargetPath = "wscript.exe"
$sc.Arguments = '"' + $vbs + '"'
$sc.WorkingDirectory = $root
$sc.IconLocation = Join-Path $root "icon.ico"
$sc.Description = "FlowLocal offline dictation (watchdog check)"
$sc.Save()

schtasks /Create /TN $task /TR ('wscript.exe "' + $vbs + '"') /SC MINUTE /MO 10 /F | Out-Null
& $vbs
Write-Host "FlowLocal will now start at login and self-heal every 10 minutes."
