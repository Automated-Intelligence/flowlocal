# Start the FlowLocal supervisor if it isn't running. Idempotent: the
# supervisor also holds a single-instance lock, so double-starts are harmless.
$root = $PSScriptRoot
$running = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
    Where-Object { $_.CommandLine -like '*supervisor.py*' }
if (-not $running) {
    Start-Process (Join-Path $root "venv\Scripts\pythonw.exe") `
        -ArgumentList ('"' + (Join-Path $root "supervisor.py") + '"') `
        -WorkingDirectory $root
}
