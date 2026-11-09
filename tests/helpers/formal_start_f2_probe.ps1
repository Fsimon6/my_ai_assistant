param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('backend-f2a', 'frontend-f2a', 'null-error', 'null-null', 'missing-process', 'normal', 'parser-mixed', 'journal-orphan', 'error-record', 'property-throws', 'timeout-hold')]
    [string]$Scenario,
    [Parameter(Mandatory = $true)]
    [string]$ResultPath,
    [string]$JournalPath = '',
    [string]$ReadyPath = ''
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $repo 'scripts/formal_start_lib.ps1')
. (Join-Path $repo 'scripts/formal_start_core.ps1')

$script:fakeProcesses = @()
$script:fakeIdentities = @()
$script:fallbackResults = @()
$script:journalError = $null
$script:testMarker = 'f2a-' + [guid]::NewGuid().ToString('N')

function Write-Journal {
    if ([string]::IsNullOrWhiteSpace($JournalPath)) { return }
    try {
        [ordered]@{
            marker = $script:testMarker
            processes = $script:fakeIdentities
        } | ConvertTo-Json -Depth 6 | Set-Content -Path $JournalPath -Encoding UTF8
    } catch {
        $script:journalError = $_.Exception.Message
    }
}

function New-FakeProcess([switch]$JournalOnly) {
    $command = "Start-Sleep -Seconds 60; # $($script:testMarker)"
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-Command', $command
    ) -WindowStyle Hidden -PassThru

    $identity = [pscustomobject]@{
        id           = $p.Id
        name         = $p.ProcessName
        start_time   = $p.StartTime.ToUniversalTime().ToString('o')
        command_line = $null
        marker       = $script:testMarker
    }
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
        if ($null -ne $cim) { $identity.command_line = $cim.CommandLine }
    } catch { }

    if (-not $JournalOnly) { $script:fakeProcesses += $p }
    $script:fakeIdentities += $identity
    Write-Journal
    return $p
}

function Wait-Gone([int]$id, [int]$timeoutMs = 5000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $id -ErrorAction SilentlyContinue)) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $null -eq (Get-Process -Id $id -ErrorAction SilentlyContinue)
}

function Get-CleanupSummary($cleanup) {
    $items = @()
    $knownStatuses = @(
        'CLEANED', 'ALREADY_EXITED', 'SKIPPED_IDENTITY_MISMATCH',
        'SKIPPED_IDENTITY_UNVERIFIABLE', 'CLEANUP_FAILED',
        'CLEANUP_TIMEOUT', 'CLEANUP_UNCONFIRMED'
    )
    foreach ($item in @($cleanup)) {
        if ($null -eq $item) { continue }
        if ($null -eq $item.PSObject.Properties['Status']) { continue }
        $raw = @($item.Status | ForEach-Object { [string]$_ })
        if ($raw.Count -eq 0) {
            $status = 'UNKNOWN'
        } else {
            # Stop-Owned emits a diagnostic line and then returns the status.
            # Only the final returned value is authoritative; never prefix-match.
            $last = $raw[-1]
            $status = if ($knownStatuses -contains $last) { $last } else { 'UNKNOWN' }
        }
        $items += [pscustomobject]@{
            id          = $item.Id
            status      = $status
            raw_status  = $raw
            diagnostics = @($raw | Select-Object -SkipLast 1)
        }
    }
    return $items
}

function Stop-TestProcess($p) {
    $identity = $script:fakeIdentities | Where-Object { $_.id -eq $p.Id } | Select-Object -First 1
    if ($null -eq $identity -or [string]::IsNullOrWhiteSpace([string]$identity.command_line)) {
        return [pscustomobject]@{ id = $p.Id; status = 'SKIPPED_IDENTITY_UNVERIFIABLE' }
    }
    $reg = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
    if ($null -eq $reg -or $null -eq $cim) {
        return [pscustomobject]@{ id = $p.Id; status = 'ALREADY_EXITED' }
    }
    $sameName = $reg.ProcessName -eq $identity.name
    $sameStart = $reg.StartTime.ToUniversalTime().ToString('o') -eq $identity.start_time
    $sameCommand = $cim.CommandLine -eq $identity.command_line
    if (-not ($sameName -and $sameStart -and $sameCommand)) {
        return [pscustomobject]@{ id = $p.Id; status = 'SKIPPED_IDENTITY_MISMATCH' }
    }
    try {
        Stop-Process -Id $p.Id -Force -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ id = $p.Id; status = 'CLEANUP_FAILED' }
    }
    $gone = Wait-Gone $p.Id
    return [pscustomobject]@{ id = $p.Id; status = $(if ($gone) { 'CLEANED' } else { 'FAILED' }) }
}

$result = [ordered]@{
    scenario = $Scenario
    probe_exit = 0
    product_exit = $null
    started_count = 0
    started_ids = @()
    cleanup = @()
    started_process_state = @()
    error = $null
    production_cleanup_ok = $false
    fallback_used = $false
    fallback = @()
    fake_identities = @()
    journal_path = $JournalPath
    journal_error = $null
    processes_alive_after = @()
    probe_error = $null
}

$parserOnly = $false
try {
    switch ($Scenario) {
        'backend-f2a' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                $p = New-FakeProcess
                [pscustomobject]@{ Process = $p; Error = 'backend post-start error' }
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'frontend-f2a' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                $p = New-FakeProcess
                [pscustomobject]@{ Process = $p; Error = $null }
            } -FrontendLauncher {
                $p = New-FakeProcess
                [pscustomobject]@{ Process = $p; Error = 'frontend post-start error' }
            } -HealthCheck { param($url) $false })
        }
        'null-error' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                [pscustomobject]@{ Process = $null; Error = 'null process error' }
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'null-null' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                [pscustomobject]@{ Process = $null; Error = $null }
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'missing-process' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                [pscustomobject]@{ Error = 'missing process property' }
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'normal' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                $p = New-FakeProcess
                [pscustomobject]@{ Process = $p; Error = $null }
            } -FrontendLauncher {
                $p = New-FakeProcess
                [pscustomobject]@{ Process = $p; Error = $null }
            } -HealthCheck { param($url) $true })
        }
        'parser-mixed' {
            $parserOnly = $true
            $result.product_exit = 1
            $result.error = 'parser mixed status input'
            $synthetic = @([pscustomobject]@{
                Id = 999
                Status = @('CLEANED pid=999', 'CLEANUP_FAILED')
            })
            $result.cleanup = @(Get-CleanupSummary $synthetic)
        }
        'journal-orphan' {
            $parserOnly = $true
            $orphan = New-FakeProcess -JournalOnly
            $result.product_exit = 0
            $result.error = 'journal-only orphan for pytest fallback'
            $result.started_ids = @($orphan.Id)
        }
        'error-record' {
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                $p = New-FakeProcess
                $err = [System.Management.Automation.ErrorRecord]::new(
                    [System.Exception]::new('error record failure'),
                    'F2A',
                    [System.Management.Automation.ErrorCategory]::NotSpecified,
                    $null
                )
                [pscustomobject]@{ Process = $p; Error = $err }
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'property-throws' {
            $p = New-FakeProcess
            $obj = [pscustomobject]@{ Error = 'property read failure' }
            $obj | Add-Member -MemberType ScriptProperty -Name Process -Value {
                throw 'simulated process property read failure'
            }
            $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 -BackendLauncher {
                $obj
            } -FrontendLauncher { throw 'not reached' } -HealthCheck { param($url) $false })
        }
        'timeout-hold' {
            $parserOnly = $true
            $p = New-FakeProcess
            $result.product_exit = 0
            $result.error = 'timeout hold scenario'
            if (-not [string]::IsNullOrWhiteSpace($ReadyPath)) {
                Set-Content -Path $ReadyPath -Value $script:testMarker -Encoding UTF8
            }
            Start-Sleep -Seconds 120
        }
    }

    if (-not $parserOnly) {
        $product = $invocation | Where-Object {
            $null -ne $_.PSObject.Properties['ExitCode']
        } | Select-Object -Last 1
        if ($null -eq $product) { throw 'formal start result object not found' }

        $result.product_exit = [int]$product.ExitCode
        $result.started_count = @($product.Started).Count
        $result.started_ids = @($product.Started | ForEach-Object { $_.Id })
        $result.cleanup = @(Get-CleanupSummary $product.Cleanup)
        $result.error = $product.Error
        $result.production_cleanup_ok = ($result.cleanup.Count -gt 0 -and
            @($result.cleanup | Where-Object { $_.status -ne 'CLEANED' }).Count -eq 0)

        $result.started_process_state = @(
            foreach ($record in @($product.Started)) {
                $state = [ordered]@{
                    id = $record.Id
                    name = $record.Name
                    start_time = $record.StartTime
                    command_line = $record.CommandLine
                    has_exited = $null
                    state_error = $null
                }
                try {
                    $record.Process.Refresh()
                    $state.has_exited = [bool]$record.Process.HasExited
                } catch {
                    $state.state_error = $_.Exception.Message
                }
                [pscustomobject]$state
            }
        )
    }
}
catch {
    $result.probe_error = $_.Exception.Message
    $result.probe_exit = 2
}
finally {
    # Test-owned fallback only; identity is checked by Stop-TestProcess.
    foreach ($p in $script:fakeProcesses) {
        $alive = $null -ne (Get-Process -Id $p.Id -ErrorAction SilentlyContinue)
        if ($alive) {
            $result.fallback_used = $true
            $fallback = Stop-TestProcess $p
            $script:fallbackResults += $fallback
        }
    }
    $result.fallback = $script:fallbackResults
    $result.fake_identities = $script:fakeIdentities
    $result.journal_error = $script:journalError
    $result.processes_alive_after = @(
        $script:fakeProcesses | Where-Object {
            $null -ne (Get-Process -Id $_.Id -ErrorAction SilentlyContinue)
        } | ForEach-Object { $_.Id }
    )
    $result | ConvertTo-Json -Depth 8 | Set-Content -Path $ResultPath -Encoding UTF8
}

exit ([int]$result.probe_exit)
