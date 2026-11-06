# Shared, testable formal start control flow.
# Production passes the real launchers and health probe; tests pass isolated fakes.

function Invoke-FormalStart {
    param(
        [Parameter(Mandatory = $true)][int]$BackendPort,
        [Parameter(Mandatory = $true)][int]$FrontendPort,
        [Parameter(Mandatory = $true)][scriptblock]$BackendLauncher,
        [Parameter(Mandatory = $true)][scriptblock]$FrontendLauncher,
        [Parameter(Mandatory = $true)][scriptblock]$HealthCheck,
        [int]$WaitMilliseconds = 5000
    )

    $started = @()
    try {
        $backendProcess = & $BackendLauncher
        if ($null -eq $backendProcess) { throw 'backend launcher returned no process' }
        # Stage 1: add a raw pending record before reading any identity field.
        $backendRecord = [pscustomobject]@{
            Process = $backendProcess; Id = $null; Name = $null
            StartTime = $null; CommandLine = $null; Identity = 'PENDING'
        }
        $started += $backendRecord
        # Stage 2: enrich identity; failures keep the pending record.
        Update-OwnedRecord $backendRecord

        $frontendProcess = & $FrontendLauncher
        if ($null -eq $frontendProcess) { throw 'frontend launcher returned no process' }
        $frontendRecord = [pscustomobject]@{
            Process = $frontendProcess; Id = $null; Name = $null
            StartTime = $null; CommandLine = $null; Identity = 'PENDING'
        }
        $started += $frontendRecord
        Update-OwnedRecord $frontendRecord

        $backendAlive = $null -ne (Get-Process -Id $backendProcess.Id -ErrorAction SilentlyContinue)
        $frontendAlive = $null -ne (Get-Process -Id $frontendProcess.Id -ErrorAction SilentlyContinue)
        $backendHealthy = [bool](& $HealthCheck "http://127.0.0.1:$BackendPort/health")
        $frontendHealthy = [bool](& $HealthCheck "http://127.0.0.1:$FrontendPort/login")

        if (-not $backendAlive -or -not $backendHealthy) {
            throw "Formal backend failed to become healthy at 127.0.0.1:$BackendPort"
        }
        if (-not $frontendAlive -or -not $frontendHealthy) {
            throw "Formal frontend failed to become healthy at 127.0.0.1:$FrontendPort"
        }

        return [pscustomobject]@{
            ExitCode = 0
            Started  = $started
            Error    = $null
            Cleanup  = @()
        }
    } catch {
        $originalFailure = $_
        $cleanupResults = @()
        foreach ($record in $started) {
            try {
                $status = Stop-Owned $record $WaitMilliseconds
            } catch {
                $status = 'CLEANUP_FAILED'
                Write-Output "CLEANUP_FAILED pid=$($record.Id): $($_.Exception.Message)"
            }
            $cleanupResults += [pscustomobject]@{ Id = $record.Id; Status = $status }
        }
        return [pscustomobject]@{
            ExitCode = 1
            Started  = $started
            Error    = $originalFailure.Exception.Message
            Cleanup  = $cleanupResults
        }
    }
}
