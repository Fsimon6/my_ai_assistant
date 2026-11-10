"""F2-A regression tests for the formal start/cleanup control flow.

The PowerShell probe calls the real `Invoke-FormalStart` and `Stop-Owned`
implementations with isolated fake child processes. It never uses the formal
8021/5181/9100 ports and never touches production data.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests" / "helpers" / "formal_start_f2_probe.ps1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "win32" or shutil.which("powershell.exe") is None,
        reason="F2-A probe requires Windows PowerShell and Windows processes",
    ),
]

_TIMEOUT_COORDINATOR = r"""
import json
import subprocess
import sys

probe, probe_result, journal, ready, coordinator_result, timeout_s = sys.argv[1:7]
command = [
    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
    "-File", probe, "-Scenario", "timeout-hold",
    "-ResultPath", probe_result, "-JournalPath", journal,
    "-ReadyPath", ready,
]
try:
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=float(timeout_s),
        check=False,
    )
    outcome = {"timed_out": False, "returncode": proc.returncode}
except subprocess.TimeoutExpired:
    outcome = {"timed_out": True, "exception": "TimeoutExpired"}
with open(coordinator_result, "w", encoding="utf-8") as handle:
    json.dump(outcome, handle)
"""


_CLEANUP_SCRIPT = r"""
$journalPath = '__JOURNAL_PATH__'
try {
    $data = Get-Content -LiteralPath $journalPath -Raw | ConvertFrom-Json
} catch {
    exit 2
}
$results = @()
foreach ($p in @($data.processes)) {
    $id = [int]$p.id
    $reg = Get-Process -Id $id -ErrorAction SilentlyContinue
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
    if ($null -eq $reg -or $null -eq $cim) {
        $results += [pscustomobject]@{ id = $id; status = 'ALREADY_EXITED' }
        continue
    }
    $sameName = $reg.ProcessName -eq $p.name
    $sameStart = $reg.StartTime.ToUniversalTime().ToString('o') -eq $p.start_time
    $sameCommand = $cim.CommandLine -eq $p.command_line
    if (-not ($sameName -and $sameStart -and $sameCommand)) {
        $results += [pscustomobject]@{ id = $id; status = 'SKIPPED_IDENTITY_MISMATCH' }
        continue
    }
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    $gone = $null -eq (Get-Process -Id $id -ErrorAction SilentlyContinue)
    $results += [pscustomobject]@{ id = $id; status = $(if ($gone) { 'CLEANED' } else { 'FAILED' }) }
}
$results | ConvertTo-Json -Compress
"""


def _safe_cleanup_from_journal(journal_path: Path) -> None:
    """Best-effort cleanup of test-owned PIDs with full identity checks."""
    if not journal_path.is_file():
        return
    try:
        data = json.loads(journal_path.read_text(encoding="utf-8-sig"))
        entries = data.get("processes") or []
        if not entries:
            return
    except (OSError, ValueError, TypeError):
        return

    escaped_path = str(journal_path).replace("'", "''")
    script = _CLEANUP_SCRIPT.replace("__JOURNAL_PATH__", escaped_path)
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _process_alive(pid: int) -> bool:
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    return proc.returncode == 0


def _run_probe(tmp_path: Path, scenario: str) -> dict:
    result_path = tmp_path / f"{scenario}-result.json"
    journal_path = tmp_path / f"{scenario}-journal.json"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(PROBE),
        "-Scenario",
        scenario,
        "-ResultPath",
        str(result_path),
        "-JournalPath",
        str(journal_path),
    ]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    finally:
        _safe_cleanup_from_journal(journal_path)

    assert result_path.is_file(), (
        f"probe did not write {result_path}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    assert proc.returncode == 0, (
        f"probe exit={proc.returncode}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    return json.loads(result_path.read_text(encoding="utf-8-sig"))


def _cleanup_statuses(result: dict) -> list[str]:
    return [item["status"] for item in result["cleanup"]]


def _fake_ids(result: dict) -> list[int]:
    return [int(item["id"]) for item in result["fake_identities"]]


def _assert_identity_fields(result: dict) -> None:
    for identity in result["fake_identities"]:
        assert isinstance(identity["id"], int)
        assert identity["name"] == "powershell"
        assert isinstance(identity["start_time"], str) and identity["start_time"]
        assert isinstance(identity["command_line"], str) and identity["command_line"]
        assert identity["marker"] == result["fake_identities"][0]["marker"]
        assert identity["marker"] in identity["command_line"]


def test_backend_f2a_registers_process_before_error(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "backend-f2a")

    assert result["product_exit"] == 1
    assert result["started_count"] == 1
    assert _cleanup_statuses(result) == ["CLEANED"]
    assert result["production_cleanup_ok"] is True
    assert result["fallback_used"] is False
    assert result["processes_alive_after"] == []
    assert result["journal_error"] is None
    assert "backend post-start error" in result["error"]

    fake_ids = _fake_ids(result)
    assert len(fake_ids) == 1
    assert result["started_ids"] == fake_ids
    assert [item["id"] for item in result["cleanup"]] == fake_ids
    _assert_identity_fields(result)


def test_frontend_f2a_cleans_both_registered_processes(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "frontend-f2a")

    assert result["product_exit"] == 1
    assert result["started_count"] == 2
    assert _cleanup_statuses(result) == ["CLEANED", "CLEANED"]
    assert result["production_cleanup_ok"] is True
    assert result["fallback_used"] is False
    assert result["processes_alive_after"] == []
    assert result["journal_error"] is None
    assert "frontend post-start error" in result["error"]

    fake_ids = _fake_ids(result)
    assert len(fake_ids) == 2
    assert sorted(result["started_ids"]) == sorted(fake_ids)
    assert sorted(item["id"] for item in result["cleanup"]) == sorted(fake_ids)
    _assert_identity_fields(result)


def test_null_process_with_error_does_not_claim_cleanup(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "null-error")

    assert result["product_exit"] == 1
    assert result["started_count"] == 0
    assert result["cleanup"] == []
    assert result["production_cleanup_ok"] is False
    assert result["fallback_used"] is False
    assert "null process error" in result["error"]
    assert result["fake_identities"] == []


def test_null_process_and_null_error_is_diagnosable(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "null-null")

    assert result["product_exit"] == 1
    assert result["started_count"] == 0
    assert result["cleanup"] == []
    assert result["fallback_used"] is False
    assert "no process" in result["error"]
    assert result["fake_identities"] == []


def test_missing_process_property_is_reported(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "missing-process")

    assert result["product_exit"] == 1
    assert result["started_count"] == 0
    assert result["cleanup"] == []
    assert result["fallback_used"] is False
    assert "unsupported result object" in result["error"]
    assert result["fake_identities"] == []


def test_normal_path_returns_zero_and_uses_only_test_fallback(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "normal")

    assert result["product_exit"] == 0
    assert result["started_count"] == 2
    assert result["cleanup"] == []
    assert result["production_cleanup_ok"] is False
    assert result["fallback_used"] is True
    assert result["processes_alive_after"] == []
    assert result["journal_error"] is None
    assert [item["status"] for item in result["fallback"]] == ["CLEANED", "CLEANED"]

    fake_ids = _fake_ids(result)
    assert len(fake_ids) == 2
    assert sorted(result["started_ids"]) == sorted(fake_ids)
    assert sorted(item["id"] for item in result["fallback"]) == sorted(fake_ids)
    _assert_identity_fields(result)


def test_status_parser_rejects_mixed_cleaned_and_failed_output(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "parser-mixed")

    assert _cleanup_statuses(result) == ["CLEANUP_FAILED"]
    assert result["cleanup"][0]["raw_status"] == ["CLEANED pid=999", "CLEANUP_FAILED"]
    assert result["production_cleanup_ok"] is False
    assert result["fake_identities"] == []


def test_journal_fallback_cleans_only_identified_orphan(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "journal-orphan")

    assert result["journal_error"] is None
    assert len(result["fake_identities"]) == 1
    pid = int(result["fake_identities"][0]["id"])
    assert not _process_alive(pid)
    # The probe deliberately leaves this test process for the pytest fallback.
    assert result["fallback_used"] is False


def test_error_record_is_converted_to_diagnostic_failure(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "error-record")

    assert result["product_exit"] == 1
    assert result["started_count"] == 1
    assert _cleanup_statuses(result) == ["CLEANED"]
    assert result["production_cleanup_ok"] is True
    assert result["fallback_used"] is False
    assert result["processes_alive_after"] == []
    assert "error record failure" in result["error"]

    fake_ids = _fake_ids(result)
    assert result["started_ids"] == fake_ids
    assert [item["id"] for item in result["cleanup"]] == fake_ids
    _assert_identity_fields(result)


def test_process_property_exception_is_reported_and_fallback_cleans(
    tmp_path: Path,
) -> None:
    result = _run_probe(tmp_path, "property-throws")

    assert result["product_exit"] == 1
    assert result["started_count"] == 0
    assert result["cleanup"] == []
    assert result["production_cleanup_ok"] is False
    assert result["fallback_used"] is True
    assert result["processes_alive_after"] == []
    assert "property read failure" in result["error"]

    fake_ids = _fake_ids(result)
    assert len(fake_ids) == 1
    assert result["started_ids"] == []
    assert [item["id"] for item in result["fallback"]] == fake_ids
    _assert_identity_fields(result)


def test_cleanup_process_identity_and_exit(tmp_path: Path) -> None:
    result = _run_probe(tmp_path, "backend-f2a")

    assert result["started_count"] == 1
    assert len(result["started_process_state"]) == 1
    state = result["started_process_state"][0]
    identity = result["fake_identities"][0]

    assert state["id"] == identity["id"]
    assert state["name"] == identity["name"]
    assert state["command_line"] == identity["command_line"]
    assert state["start_time"]
    assert state["has_exited"] is True
    assert state["state_error"] is None
    assert state["id"] in result["started_ids"]
    assert [item["id"] for item in result["cleanup"]] == [state["id"]]


def test_real_subprocess_timeout_cleans_journaled_fake(tmp_path: Path) -> None:
    probe_result = tmp_path / "timeout-probe-result.json"
    journal = tmp_path / "timeout-journal.json"
    ready = tmp_path / "timeout-ready.txt"
    coordinator_result = tmp_path / "timeout-coordinator.json"
    pid = None

    try:
        coordinator = subprocess.run(
            [
                sys.executable,
                "-c",
                _TIMEOUT_COORDINATOR,
                str(PROBE),
                str(probe_result),
                str(journal),
                str(ready),
                str(coordinator_result),
                "8",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        assert coordinator.returncode == 0, (
            f"coordinator exit={coordinator.returncode}; "
            f"stdout={coordinator.stdout!r}; stderr={coordinator.stderr!r}"
        )
        outcome = json.loads(coordinator_result.read_text(encoding="utf-8"))
        assert outcome["timed_out"] is True
        assert ready.is_file(), "probe did not reach the ready barrier before timeout"
        assert journal.is_file(), "probe did not persist the fake journal before timeout"

        data = json.loads(journal.read_text(encoding="utf-8-sig"))
        entries = data.get("processes") or []
        assert len(entries) == 1
        pid = int(entries[0]["id"])
        assert _process_alive(pid), "fake must still be alive after probe timeout"
    finally:
        _safe_cleanup_from_journal(journal)

    assert pid is not None
    assert not _process_alive(pid)


# ---------------------------------------------------------------------------
# F2-B: process created by a launcher before the launcher returns/throws.
# These tests use real, test-owned PowerShell children and identity-check all
# cleanup. They do not modify or exercise the formal 8021/5181 services.
# ---------------------------------------------------------------------------

_F2B_PROBE = r"""
$ErrorActionPreference = 'Stop'
$repo = '__REPO__'
. (Join-Path $repo 'scripts/formal_start_lib.ps1')
. (Join-Path $repo 'scripts/formal_start_core.ps1')
$side = '__SIDE__'
$resultPath = '__RESULT__'
$Scenario = '__SCENARIO__'
$script:identities = New-Object System.Collections.ArrayList
$script:testMarker = 'f2b-' + [guid]::NewGuid().ToString('N')

function Write-F2BSide {
    [ordered]@{ identities = @($script:identities) } |
        ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $side -Encoding UTF8
}

function New-F2BFake([string]$name) {
    $marker = 'f2b-' + [guid]::NewGuid().ToString('N')
    $command = "Start-Sleep -Seconds 120; # $marker"
    $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-Command', $command
    ) -WindowStyle Hidden -PassThru

    $identity = [ordered]@{
        scenario     = $name
        pid          = [int]$p.Id
        name         = [string]$p.ProcessName
        start_time   = $p.StartTime.ToUniversalTime().ToString('o')
        command_line = $null
        marker       = $marker
    }
    [void]$script:identities.Add($identity)
    Write-F2BSide

    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue
        if ($null -ne $cim) { $identity.command_line = [string]$cim.CommandLine }
    } catch { }
    Write-F2BSide
    return $p
}

function Write-F2BResult($product, $errorText) {
    $started = @()
    $cleanup = @()
    if ($null -ne $product) {
        $started = @($product.Started)
        $cleanup = @($product.Cleanup)
    }
    $knownStatuses = @(
        'CLEANED', 'ALREADY_EXITED', 'SKIPPED_IDENTITY_MISMATCH',
        'SKIPPED_IDENTITY_UNVERIFIABLE', 'CLEANUP_FAILED',
        'CLEANUP_TIMEOUT', 'CLEANUP_UNCONFIRMED'
    )
    [ordered]@{
        scenario             = $Scenario
        product_exit         = $(if ($null -ne $product) { [int]$product.ExitCode } else { $null })
        started_ids          = @($started | ForEach-Object { [int]$_.Id })
        cleanup              = @($cleanup | ForEach-Object {
            $raw = @($_.Status | ForEach-Object { [string]$_ })
            $last = if ($raw.Count -gt 0) { $raw[-1] } else { 'UNKNOWN' }
            $status = if ($knownStatuses -contains $last) { $last } else { 'UNKNOWN' }
            [ordered]@{ id = [int]$_.Id; status = $status }
        })
        error                = $(if ($null -ne $product) { [string]$product.Error } else { [string]$errorText })
        identities           = @($script:identities)
        fake_alive_after_core = @(
            $script:identities |
                Where-Object { $null -ne (Get-Process -Id $_.pid -ErrorAction SilentlyContinue) } |
                ForEach-Object { [int]$_.pid }
        )
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding UTF8
}

try {
    if ($Scenario -eq 'backend') {
        $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 `
            -BackendLauncher {
                $null = New-F2BFake 'backend'
                throw 'f2b-backend-created-before-return'
            } `
            -FrontendLauncher { throw 'f2b-frontend-not-reached' } `
            -HealthCheck { param($url) $false })
    }
    else {
        $invocation = @(Invoke-FormalStart -BackendPort 18021 -FrontendPort 15181 `
            -BackendLauncher {
                $p = New-F2BFake 'backend'
                [pscustomobject]@{ Process = $p; Error = $null }
            } `
            -FrontendLauncher {
                $null = New-F2BFake 'frontend'
                throw 'f2b-frontend-created-before-return'
            } `
            -HealthCheck { param($url) $false })
    }

    $product = $invocation | Where-Object {
        $null -ne $_.PSObject.Properties['ExitCode']
    } | Select-Object -Last 1
    Write-F2BResult $product $null
}
catch {
    Write-F2BResult $null $_.Exception.Message
}
"""

_F2B_IDENTITY = r"""
$ErrorActionPreference = 'Stop'
$identityPath = '__IDENTITY__'
$kill = __KILL__
try {
    $i = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
    $processId = [int]$i.pid
    $reg = Get-Process -Id $processId -ErrorAction SilentlyContinue
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue

    if ($null -eq $reg -or $null -eq $cim) {
        [ordered]@{ status = 'ALREADY_EXITED'; pid = $processId } |
            ConvertTo-Json -Compress
        exit 0
    }

    try {
        $sameName = $reg.ProcessName -eq [string]$i.name
        $sameStart = $reg.StartTime.ToUniversalTime().ToString('o') -eq [string]$i.start_time
        $sameCommand = $cim.CommandLine -eq [string]$i.command_line
    }
    catch {
        [ordered]@{ status = 'NOT_VERIFIED'; pid = $processId; error = $_.Exception.Message } |
            ConvertTo-Json -Compress
        exit 0
    }

    if (-not ($sameName -and $sameStart -and $sameCommand)) {
        [ordered]@{ status = 'IDENTITY_MISMATCH'; pid = $processId } |
            ConvertTo-Json -Compress
        exit 0
    }

    if (-not $kill) {
        [ordered]@{ status = 'PRESENT_MATCH'; pid = $processId } |
            ConvertTo-Json -Compress
        exit 0
    }

    try {
        Stop-Process -Id $processId -Force -ErrorAction Stop
        for ($n = 0; $n -lt 50; $n++) {
            if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
                [ordered]@{ status = 'CLEANED'; pid = $processId } |
                    ConvertTo-Json -Compress
                exit 0
            }
            Start-Sleep -Milliseconds 100
        }
        [ordered]@{ status = 'CLEANUP_FAILED'; pid = $processId } |
            ConvertTo-Json -Compress
    }
    catch {
        [ordered]@{ status = 'CLEANUP_FAILED'; pid = $processId; error = $_.Exception.Message } |
            ConvertTo-Json -Compress
    }
}
catch {
    [ordered]@{ status = 'NOT_VERIFIED'; error = $_.Exception.Message } |
        ConvertTo-Json -Compress
}
"""


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _run_f2b_case(tmp_path: Path, scenario: str) -> tuple[dict, list[dict]]:
    side = tmp_path / f"f2b-{scenario}-identities.json"
    result_path = tmp_path / f"f2b-{scenario}-result.json"
    script = (
        _F2B_PROBE.replace("__REPO__", _ps_quote(str(ROOT)))
        .replace("__SIDE__", _ps_quote(str(side)))
        .replace("__RESULT__", _ps_quote(str(result_path)))
        .replace("__SCENARIO__", scenario)
    )
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert result_path.is_file(), (
        f"F2-B probe did not write {result_path}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    result = json.loads(result_path.read_text(encoding="utf-8-sig"))
    identities = json.loads(side.read_text(encoding="utf-8-sig"))["identities"]
    return result, identities


def _f2b_identity_status(
    tmp_path: Path, identity: dict, *, kill: bool
) -> dict:
    identity_path = tmp_path / f"f2b-identity-{identity['pid']}.json"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    script = (
        _F2B_IDENTITY.replace("__IDENTITY__", _ps_quote(str(identity_path)))
        .replace("__KILL__", "$true" if kill else "$false")
    )
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, (
        f"identity helper exit={proc.returncode}; "
        f"stdout={proc.stdout!r}; stderr={proc.stderr!r}"
    )
    return json.loads(proc.stdout)


def _cleanup_f2b(tmp_path: Path, identities: list[dict]) -> list[dict]:
    results = []
    for identity in identities:
        results.append(_f2b_identity_status(tmp_path, identity, kill=True))
    return results


def test_f2b_backend_created_then_launcher_throws(tmp_path: Path) -> None:
    cleanup: list[dict] = []
    try:
        result, identities = _run_f2b_case(tmp_path, "backend")
        assert result["scenario"] == "backend"
        assert result["product_exit"] == 1
        assert result["started_ids"] == []
        assert result["cleanup"] == []
        assert "f2b-backend-created-before-return" in result["error"]
        assert len(identities) == 1

        backend = identities[0]
        assert backend["scenario"] == "backend"
        assert backend["command_line"]
        assert int(backend["pid"]) in result["fake_alive_after_core"]

        status = _f2b_identity_status(tmp_path, backend, kill=False)
        assert status["status"] == "PRESENT_MATCH", status
    finally:
        cleanup = _cleanup_f2b(tmp_path, locals().get("identities", []))

    assert cleanup, "test-owned backend fake was not recorded"
    assert cleanup[0]["status"] in {"CLEANED", "ALREADY_EXITED"}, cleanup


def test_f2b_frontend_created_then_launcher_throws(tmp_path: Path) -> None:
    cleanup: list[dict] = []
    try:
        result, identities = _run_f2b_case(tmp_path, "frontend")
        assert result["scenario"] == "frontend"
        assert result["product_exit"] == 1
        assert "f2b-frontend-created-before-return" in result["error"]
        assert len(identities) == 2

        backend = next(item for item in identities if item["scenario"] == "backend")
        frontend = next(item for item in identities if item["scenario"] == "frontend")

        assert result["started_ids"] == [int(backend["pid"])]
        assert result["cleanup"] == [
            {"id": int(backend["pid"]), "status": "CLEANED"}
        ]
        assert int(frontend["pid"]) in result["fake_alive_after_core"]
        assert int(frontend["pid"]) not in result["started_ids"]

        backend_status = _f2b_identity_status(tmp_path, backend, kill=False)
        frontend_status = _f2b_identity_status(tmp_path, frontend, kill=False)
        assert backend_status["status"] == "ALREADY_EXITED", backend_status
        assert frontend_status["status"] == "PRESENT_MATCH", frontend_status
    finally:
        cleanup = _cleanup_f2b(tmp_path, locals().get("identities", []))

    assert len(cleanup) == 2, "test-owned fakes were not recorded"
    assert all(item["status"] in {"CLEANED", "ALREADY_EXITED"} for item in cleanup), cleanup
