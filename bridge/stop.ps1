# Stops the Claude bridge started by start.ps1
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root 'logs\bridge.pid'
if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile)
    try { Stop-Process -Id $id -Force -ErrorAction Stop; Write-Output "stopped pid $id" } catch { Write-Output "pid $id not running" }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
} else {
    Write-Output 'no pid file - bridge not running?'
}
