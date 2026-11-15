# Starts the Claude bridge as a hidden background process (survives the terminal).
# Logs: logs\bridge.log (app), logs\bridge.out.log / bridge.err.log (stdout/stderr). PID: logs\bridge.pid
param([int]$Port = 41220)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'logs'
New-Item -ItemType Directory -Force $logs | Out-Null

$py = $env:PYTHON
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py) { throw 'Python not found: set $env:PYTHON' }

# local API key the bridge expects (n8n stores the same value as a Header Auth credential)
$keyFile = Join-Path $PSScriptRoot '.key'
if (-not (Test-Path $keyFile)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    'sk-local-' + ([BitConverter]::ToString($bytes) -replace '-', '').ToLower() | Set-Content -NoNewline -Encoding ascii $keyFile
}
$env:BRIDGE_KEY_FILE = $keyFile
$env:BRIDGE_PORT = "$Port"

$p = Start-Process -FilePath $py -ArgumentList @('-I', '-X', 'utf8', (Join-Path $PSScriptRoot 'claude_bridge.py')) `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs 'bridge.out.log') -RedirectStandardError (Join-Path $logs 'bridge.err.log')
Start-Sleep -Seconds 2
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    Write-Output "bridge running, pid $($p.Id), health: $($h.status)"
} catch {
    Write-Output "bridge started (pid $($p.Id)) but /health did not answer yet - see logs\bridge.err.log"
}
