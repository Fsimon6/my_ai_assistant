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
