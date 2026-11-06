# Tracked formal launcher: 127.0.0.1:8021 + 127.0.0.1:5181.
# Fails closed on testing/TEST_* and L1 Vite variables; never kills a listener.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'formal_start_guard.ps1')
. (Join-Path $PSScriptRoot 'formal_start_lib.ps1')
. (Join-Path $PSScriptRoot 'formal_start_core.ps1')

$BackendPort = 8021
$FrontendPort = 5181

function Wait-Healthy([string]$url, [int]$attempts = 30) {
    for ($i = 1; $i -le $attempts; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { return $true }
        } catch {
            # Not ready yet.
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

try {
    Assert-FormalEnvironment
    Assert-PortAvailable $BackendPort
    Assert-PortAvailable $FrontendPort

    $env:VITE_APP_ENV = 'development'
    $env:VITE_API_BASE_URL = "http://127.0.0.1:$BackendPort"
    $env:VITE_WS_URL = "ws://127.0.0.1:$BackendPort/ws"
    $env:PYTHONPATH = $root
    $env:PYTHONIOENCODING = 'utf-8'

    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { throw "Python not found: $python" }
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $vite = Join-Path $root 'frontend\node_modules\vite\bin\vite.js'
    if (-not (Test-Path $vite)) { throw "Vite not found: $vite" }
    $logs = Join-Path $root 'logs'
    New-Item -ItemType Directory -Path $logs -ErrorAction Stop | Out-Null

    $result = Invoke-FormalStart `
        -BackendPort $BackendPort `
        -FrontendPort $FrontendPort `
        -BackendLauncher {
            Start-Process -FilePath $python -ArgumentList @(
                '-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$BackendPort"
            ) -WorkingDirectory $root -RedirectStandardOutput (Join-Path $logs 'formal_backend.log') -RedirectStandardError (Join-Path $logs 'formal_backend_err.log') -PassThru
        } `
        -FrontendLauncher {
            Start-Process -FilePath $node -ArgumentList @(
                $vite, '--host', '127.0.0.1', '--port', "$FrontendPort", '--strictPort'
            ) -WorkingDirectory (Join-Path $root 'frontend') -RedirectStandardOutput (Join-Path $logs 'formal_frontend.log') -RedirectStandardError (Join-Path $logs 'formal_frontend_err.log') -PassThru
        } `
        -HealthCheck {
            param($url)
            Wait-Healthy $url
        }

    if ($result.ExitCode -ne 0) {
        Write-Output ("FORMAL_START_FAILED: " + $result.Error)
        foreach ($item in $result.Cleanup) {
            Write-Output ("CLEANUP_RESULT pid=$($item.Id) status=$($item.Status)")
        }
        exit 1
    }

    $backendRecord = $result.Started[0]
    $frontendRecord = $result.Started[1]
    Write-Output "formal backend  : http://127.0.0.1:$BackendPort (pid=$($backendRecord.Id))"
    Write-Output "formal frontend : http://127.0.0.1:$FrontendPort (pid=$($frontendRecord.Id))"
    Write-Output 'formal start verified healthy; no external listener was stopped.'
    exit 0
} catch {
    Write-Output ("FORMAL_START_FAILED: " + $_.Exception.Message)
    exit 1
}
