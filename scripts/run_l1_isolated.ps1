# L1 isolated launcher. Formal ports 5181/8021 are deliberately not used.
param(
    [switch]$PreflightOnly,
    [switch]$TestOnly,
    [switch]$SmokeTest,
    [switch]$FailAfterStart,
    [int]$FrontendPort = 5191,
    [int]$BackendPort = 8031,
    [int]$FakePort = 9110
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$tempRoot = Join-Path $env:TEMP ('my_ai_assistant_L1_' + [guid]::NewGuid().ToString('N'))
$python = Join-Path $root '.venv\Scripts\python.exe'
$started = @()

function Assert-PortFree([int]$port) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $details = foreach ($listener in $listeners) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
            $identity = if ($null -ne $proc) {
                "name=$($proc.Name); path=$($proc.ExecutablePath); cmd=$($proc.CommandLine)"
            } else {
                'identity=UNKNOWN'
            }
            "$($listener.LocalAddress):$($listener.LocalPort) pid=$($listener.OwningProcess); $identity"
        }
        throw ("L1 port $port is already occupied: " + ($details -join ' | ') + ". Refusing to start; no process was stopped.")
    }
}

function New-OwnedRecord([System.Diagnostics.Process]$process) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)" -ErrorAction SilentlyContinue
    return [pscustomobject]@{
        Id          = $process.Id
        Name        = $process.ProcessName
        StartTime   = $process.StartTime
        CommandLine = if ($null -ne $proc) { $proc.CommandLine } else { $null }
    }
}

function Wait-Url([string]$url, [int]$attempts = 30) {
    for ($i = 1; $i -le $attempts; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { return $true }
        } catch {
            # Not ready yet.
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-Owned([object]$record) {
    if ($null -eq $record) { return }
    $p = Get-Process -Id $record.Id -ErrorAction SilentlyContinue
    if ($null -eq $p) {
        Write-Output "L1 child already exited pid=$($record.Id)"
        return
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
    $sameStart = $p.StartTime -eq $record.StartTime
    $sameName = $p.ProcessName -eq $record.Name
    $sameCommand = ($null -ne $record.CommandLine -and $null -ne $proc -and $proc.CommandLine -eq $record.CommandLine)
    if ($sameStart -and $sameName -and $sameCommand) {
        Stop-Process -Id $record.Id -Force -ErrorAction SilentlyContinue
        Write-Output "stopped L1 child pid=$($record.Id) name=$($record.Name)"
    } else {
        Write-Output "SKIPPED cleanup pid=$($record.Id): identity mismatch (PID reuse or command changed)"
    }
}

Assert-PortFree $FrontendPort
Assert-PortFree $BackendPort
Assert-PortFree $FakePort

if ($PreflightOnly) {
    Write-Output "L1_PREFLIGHT_OK frontend=$FrontendPort backend=$BackendPort fake=$FakePort"
    exit 0
}

if (-not (Test-Path $python)) { throw "Python not found: $python" }
if (Test-Path $tempRoot) { throw "L1 temp root collision: $tempRoot" }
New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
foreach ($dir in @('logs', 'uploads', 'chroma', 'excel', 'vite-cache')) {
    New-Item -ItemType Directory -Path (Join-Path $tempRoot $dir) -ErrorAction Stop | Out-Null
}

$env:ENVIRONMENT = 'testing'
$env:TEST_ISOLATION_ROOT = $tempRoot
$env:TEST_DATABASE_URL = 'sqlite:///' + (Join-Path $tempRoot 'manual.db').Replace('\', '/')
$env:DATABASE_URL = $env:TEST_DATABASE_URL
$env:TEST_VECTOR_DB_PATH = Join-Path $tempRoot 'chroma'
$env:VECTOR_DB_PATH = $env:TEST_VECTOR_DB_PATH
$env:TEST_UPLOAD_FOLDER = Join-Path $tempRoot 'uploads'
$env:UPLOAD_TEMP_DIR = $env:TEST_UPLOAD_FOLDER
$env:TEST_EXCEL_DATA_ROOT = Join-Path $tempRoot 'excel'
$env:EXCEL_DATA_ROOT = $env:TEST_EXCEL_DATA_ROOT
$env:TEST_LOG_FILE = Join-Path $tempRoot 'logs\app.log'
$env:LOG_FILE = $env:TEST_LOG_FILE
$env:JWT_SECRET = [guid]::NewGuid().ToString('N')
$env:HOST = '127.0.0.1'
$env:PORT = "$BackendPort"
$env:BACKEND_CORS_ORIGINS = "http://127.0.0.1:$FrontendPort"
$env:LLM_PROVIDER = 'openai'
$env:LLM_BASE_URL = "http://127.0.0.1:$FakePort/v1"
$env:LLM_MODEL = 'l1-fake-model'
$env:API_KEY = 'l1-fake-local-key'
$env:VITE_APP_ENV = 'testing'
$env:VITE_API_BASE_URL = "http://127.0.0.1:$FrontendPort"
$env:VITE_WS_URL = "ws://127.0.0.1:$BackendPort/ws"
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$env:no_proxy = '127.0.0.1,localhost,::1'
$env:L1_VITE_CACHE_DIR = Join-Path $tempRoot 'vite-cache'
$env:L1_FAKE_LOG = Join-Path $tempRoot 'fake_requests.jsonl'
$env:PYTHONDONTWRITEBYTECODE = '1'

try {
    $fake = Start-Process -FilePath $python -ArgumentList @((Join-Path $PSScriptRoot 'l1_fake_provider.py')) -WorkingDirectory $root -RedirectStandardOutput (Join-Path $tempRoot 'fake.stdout.log') -RedirectStandardError (Join-Path $tempRoot 'fake.stderr.log') -WindowStyle Hidden -PassThru
    $started += (New-OwnedRecord $fake)
    if (-not (Wait-Url "http://127.0.0.1:$FakePort/v1/models" 30)) {
        throw "L1 Fake Provider did not become ready on 127.0.0.1:$FakePort"
    }

    $backend = Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$BackendPort") -WorkingDirectory $root -RedirectStandardOutput (Join-Path $tempRoot 'backend.stdout.log') -RedirectStandardError (Join-Path $tempRoot 'backend.stderr.log') -WindowStyle Hidden -PassThru
    $started += (New-OwnedRecord $backend)
    if (-not (Wait-Url "http://127.0.0.1:$BackendPort/health" 30)) {
        throw "L1 backend did not become healthy on 127.0.0.1:$BackendPort"
    }

    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $vite = Start-Process -FilePath $node -ArgumentList @((Join-Path $root 'frontend\node_modules\vite\bin\vite.js'), '--config', (Join-Path $root 'frontend\vite.config.l1.ts'), '--host', '127.0.0.1', '--port', "$FrontendPort") -WorkingDirectory (Join-Path $root 'frontend') -RedirectStandardOutput (Join-Path $tempRoot 'vite.stdout.log') -RedirectStandardError (Join-Path $tempRoot 'vite.stderr.log') -WindowStyle Hidden -PassThru
    $started += (New-OwnedRecord $vite)
    if (-not (Wait-Url "http://127.0.0.1:$FrontendPort/login" 30)) {
        throw "L1 frontend did not become ready on 127.0.0.1:$FrontendPort"
    }

    $health = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$BackendPort/health" -TimeoutSec 10
    if ($health.StatusCode -ne 200) { throw "L1 backend health failed: $($health.StatusCode)" }
    $page = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$FrontendPort/login" -TimeoutSec 10
    if ($page.StatusCode -ne 200) { throw "L1 frontend page failed: $($page.StatusCode)" }

    $module = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$FrontendPort/src/utils/requests.ts" -TimeoutSec 10
    if ($module.Content -match '8021' -or $module.Content -notmatch '127\.0\.0\.1:5191') {
        throw 'L1 frontend module still uses the formal 8021 API base'
    }

    $proxyStatus = 0
    try {
        Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$FrontendPort/api/v1/auth/me" -TimeoutSec 10 | Out-Null
    } catch {
        $proxyStatus = [int]$_.Exception.Response.StatusCode
    }
    if ($proxyStatus -ne 401) { throw "L1 API proxy did not return expected 401: $proxyStatus" }
    Start-Sleep -Milliseconds 400
    $backendLog = Get-Content (Join-Path $tempRoot 'backend.stderr.log') -Raw -ErrorAction SilentlyContinue
    if ($backendLog -notmatch 'GET /api/v1/auth/me') {
        throw 'L1 API proxy request did not reach the L1 backend'
    }

    Write-Output "L1_READY frontend=http://127.0.0.1:$FrontendPort backend=http://127.0.0.1:$BackendPort fake=http://127.0.0.1:$FakePort root=$tempRoot"
    if ($SmokeTest) {
        & $python (Join-Path $PSScriptRoot 'l1_smoke.py') "http://127.0.0.1:$FrontendPort"
        if ($LASTEXITCODE -ne 0) { throw "L1 smoke test failed with exit code $LASTEXITCODE" }
        $fakeLog = Get-Content (Join-Path $tempRoot 'fake_requests.jsonl') -Raw -ErrorAction SilentlyContinue
        if ($fakeLog -notmatch 'L1 smoke streaming') {
            throw 'Fake Provider did not receive the L1 smoke request'
        }
        Write-Output 'L1_SMOKE_OK'
    }
    if ($FailAfterStart) {
        throw 'L1 intentional failure after startup (cleanup test)'
    }
    if ($TestOnly) {
        Write-Output 'L1_TEST_ONLY_OK'
        return
    }
    Write-Output 'Press Ctrl+C to stop L1-owned processes.'
    while ($true) { Start-Sleep -Seconds 2 }
}
finally {
    foreach ($record in $started) { Stop-Owned $record }
}
