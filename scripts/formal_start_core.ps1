# Shared, testable formal start control flow.
# Production passes the real launchers and health probe; tests pass isolated fakes.

function Get-LauncherResult([object]$value) {
    $process = $null
    $launchError = $null
    if ($null -eq $value) {
        return [pscustomobject]@{ Process = $null; Error = 'launcher returned no result' }
    }
    if ($value -is [System.Diagnostics.Process]) {
        $process = $value
    } else {
        $names = @($value.PSObject.Properties.Name)
        if ($names -contains 'Process') {
            $process = $value.Process
            if ($names -contains 'Error' -and -not [string]::IsNullOrWhiteSpace([string]$value.Error)) {
                $launchError = [string]$value.Error
            }
        } else {
            throw 'launcher returned an unsupported result object'
        }
    }
    return [pscustomobject]@{ Process = $process; Error = $launchError }
}

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
        $backendLaunch = Get-LauncherResult (& $BackendLauncher)
        if ($null -ne $backendLaunch.Process) {
            # Stage 1: register the Process before handling a launcher error.
            $backendRecord = [pscustomobject]@{
                Process = $backendLaunch.Process; Id = $null; Name = $null
                StartTime = $null; CommandLine = $null; Identity = 'PENDING'
            }
            $started += $backendRecord
            # Stage 2: enrich identity; failures keep the pending record.
            Update-OwnedRecord $backendRecord
        }
        if ($null -ne $backendLaunch.Error) { throw $backendLaunch.Error }
        if ($null -eq $backendLaunch.Process) { throw 'backend launcher returned no process' }

        $frontendLaunch = Get-LauncherResult (& $FrontendLauncher)
        if ($null -ne $frontendLaunch.Process) {
            $frontendRecord = [pscustomobject]@{
                Process = $frontendLaunch.Process; Id = $null; Name = $null
                StartTime = $null; CommandLine = $null; Identity = 'PENDING'
            }
            $started += $frontendRecord
            Update-OwnedRecord $frontendRecord
        }
        if ($null -ne $frontendLaunch.Error) { throw $frontendLaunch.Error }
        if ($null -eq $frontendLaunch.Process) { throw 'frontend launcher returned no process' }

        $backendAlive = $null -ne (Get-Process -Id $backendLaunch.Process.Id -ErrorAction SilentlyContinue)
        $frontendAlive = $null -ne (Get-Process -Id $frontendLaunch.Process.Id -ErrorAction SilentlyContinue)
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
