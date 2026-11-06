# Shared, tracked guard for the formal 8021/5181 launchers.
# This file only defines functions; it does not start or stop anything.

function Assert-FormalEnvironment {
    if (([string]$env:ENVIRONMENT).Trim().ToLower() -eq 'testing') {
        throw 'Refusing formal start: ENVIRONMENT=testing. Use scripts/run_l1_isolated.ps1 for L1.'
    }
    $testKeys = @([Environment]::GetEnvironmentVariables().Keys | Where-Object { $_ -like 'TEST_*' })
    if ($testKeys.Count -gt 0) {
        throw ('Refusing formal start: TEST_* environment variables present: ' + ($testKeys -join ','))
    }
    if (([string]$env:VITE_APP_ENV).Trim().ToLower() -eq 'testing') {
        throw 'Refusing formal start: VITE_APP_ENV=testing is an L1 marker.'
    }
    foreach ($name in @('VITE_API_BASE_URL', 'VITE_WS_URL')) {
        $value = [string](Get-Item "Env:$name" -ErrorAction SilentlyContinue).Value
        if ($value -match ':(5191|8031|9110)(/|$)') {
            throw "Refusing formal start: $name points to an L1 port: $value"
        }
    }
}

function Assert-PortAvailable([int]$port) {
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
        throw ("Refusing formal start: port $port is occupied: " + ($details -join ' | ') + '; no process was stopped.')
    }
}
