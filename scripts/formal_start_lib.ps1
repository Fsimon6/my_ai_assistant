# Minimal, testable ownership/cleanup helpers for formal_start.ps1.
# Functions never call exit and never stop a process without identity checks.

function New-PendingOwnedRecord([System.Diagnostics.Process]$process) {
    # Stage 1: retain the raw Process object before reading any identity field.
    return [pscustomobject]@{
        Process     = $process
        Id          = $null
        Name        = $null
        StartTime   = $null
        CommandLine = $null
        Identity    = 'PENDING'
    }
}

function Update-OwnedRecord([object]$record) {
    # Stage 2: enrich identity. These reads may fail; the pending record stays.
    if ($null -eq $record) { return }
    try { $record.Id = $record.Process.Id } catch { }
    try { $record.Name = $record.Process.ProcessName } catch { }
    try { $record.StartTime = $record.Process.StartTime } catch {
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
            if ($null -ne $proc) { $record.StartTime = $proc.CreationDate }
        } catch { }
    }
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
        if ($null -ne $proc) { $record.CommandLine = $proc.CommandLine }
    } catch { }
    if ($null -ne $record.Id -and -not [string]::IsNullOrWhiteSpace([string]$record.Name) -and
        $null -ne $record.StartTime -and -not [string]::IsNullOrWhiteSpace([string]$record.CommandLine)) {
        $record.Identity = 'COMPLETE'
    }
}

function Register-OwnedProcess([System.Diagnostics.Process]$process, [object[]]$records) {
    $record = New-PendingOwnedRecord $process
    # Caller must add the returned object to its owned list immediately.
    Update-OwnedRecord $record
    return $record
}

function Stop-Owned([object]$record, [int]$WaitMilliseconds = 5000) {
    if ($null -eq $record) {
        return 'SKIPPED_NULL_RECORD'
    }
    try {
        if ($null -eq $record.Id -or $null -eq $record.Process) {
            Write-Output 'SKIPPED_IDENTITY_UNVERIFIABLE pid=<pending>'
            return 'SKIPPED_IDENTITY_UNVERIFIABLE'
        }
        $p = Get-Process -Id $record.Id -ErrorAction SilentlyContinue
        if ($null -eq $p) {
            Write-Output "ALREADY_EXITED pid=$($record.Id)"
            return 'ALREADY_EXITED'
        }
        if ([string]::IsNullOrWhiteSpace([string]$record.Name) -or
            $null -eq $record.StartTime -or
            [string]::IsNullOrWhiteSpace([string]$record.CommandLine)) {
            Write-Output "SKIPPED_IDENTITY_UNVERIFIABLE pid=$($record.Id)"
            return 'SKIPPED_IDENTITY_UNVERIFIABLE'
        }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($record.Id)" -ErrorAction SilentlyContinue
        if ($null -eq $proc) {
            Write-Output "SKIPPED_IDENTITY_UNVERIFIABLE pid=$($record.Id)"
            return 'SKIPPED_IDENTITY_UNVERIFIABLE'
        }
        $sameName = $p.ProcessName -eq $record.Name
        $sameStart = $p.StartTime -eq $record.StartTime
        $sameCommand = $proc.CommandLine -eq $record.CommandLine
        if (-not ($sameName -and $sameStart -and $sameCommand)) {
            Write-Output "SKIPPED_IDENTITY_MISMATCH pid=$($record.Id)"
            return 'SKIPPED_IDENTITY_MISMATCH'
        }

        Stop-Process -Id $record.Id -Force -ErrorAction Stop
        try {
            if ($p.HasExited) {
                Write-Output "CLEANED pid=$($record.Id) name=$($record.Name)"
                return 'CLEANED'
            }
        } catch { }
        try {
            $exited = $p.WaitForExit($WaitMilliseconds)
        } catch {
            Write-Output "CLEANUP_UNCONFIRMED pid=$($record.Id): $($_.Exception.Message)"
            return 'CLEANUP_UNCONFIRMED'
        }
        if ($exited -and $p.HasExited) {
            Write-Output "CLEANED pid=$($record.Id) name=$($record.Name)"
            return 'CLEANED'
        }
        if (-not $exited) {
            Write-Output "CLEANUP_TIMEOUT pid=$($record.Id) wait_ms=$WaitMilliseconds"
            return 'CLEANUP_TIMEOUT'
        }
        Write-Output "CLEANUP_UNCONFIRMED pid=$($record.Id)"
        return 'CLEANUP_UNCONFIRMED'
    } catch {
        Write-Output "CLEANUP_FAILED pid=$($record.Id): $($_.Exception.Message)"
        return 'CLEANUP_FAILED'
    }
}

function Stop-AllOwned([object[]]$records, [int]$WaitMilliseconds = 5000) {
    $results = @()
    foreach ($record in $records) {
        try {
            $status = Stop-Owned $record $WaitMilliseconds
        } catch {
            $status = 'CLEANUP_FAILED'
            Write-Output "CLEANUP_FAILED pid=$($record.Id): $($_.Exception.Message)"
        }
        $results += [pscustomobject]@{ Id = $record.Id; Status = $status }
    }
    return $results
}
