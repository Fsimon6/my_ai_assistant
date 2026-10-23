#!/usr/bin/env python
"""Run selected pytest files in a disposable copy of the current workspace.

This launcher intentionally uses only the Python standard library. It never
imports ``backend``: the application is imported by the pytest subprocess from
the temporary copy, after the testing environment has been fully prepared.

Usage:
    python scripts/run_tests_isolated.py
    python scripts/run_tests_isolated.py tests/unit/test_config_b2.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EXPECTED_SOURCE = Path(r"C:\Users\FSimon\Desktop\my_ai_assistant-副本").resolve()

DEFAULT_TESTS = (
    "tests/unit/test_testing_isolation.py",
    "tests/unit/test_exception_security.py",
    "tests/unit/test_config_b2.py",
    "tests/unit/test_config_environment.py",
    "tests/unit/test_llm_timeout.py",
)

# Directories that must never be copied into the disposable test project.
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    "htmlcov",
    ".coverage",
    "test-reports",
}

# Root-only runtime/business directories.
ROOT_IGNORED_DIR_NAMES = {
    "data",
    "logs",
    "cache",
    "uploads",
    "chroma",
    "chroma_db",
}

# Environment variables that could carry real configuration or credentials.
CLEAR_EXACT = {
    "ENVIRONMENT",
    "JWT_SECRET",
    "SECRET_KEY",
    "API_KEY",
    "API_KEY_SALT",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "ALLOW_REGISTRATION",
    "DATABASE_URL",
    "VECTOR_DB_PATH",
    "UPLOAD_TEMP_DIR",
    "EXCEL_DATA_ROOT",
    "LOG_FILE",
    "LOG_LEVEL",
    "LOG_MAX_BYTES",
    "LOG_BACKUP_COUNT",
    "DEBUG",
    "HOST",
    "PORT",
    "MAX_UPLOAD_SIZE",
    "EXCEL_MAX_SHEET_ROWS",
    "EXCEL_MAX_SHEET_CELLS",
    "EXCEL_MAX_TOTAL_CELLS",
    "EMBEDDING_MODEL",
    "BACKEND_CORS_ORIGINS",
    "INIT_ADMIN_PASSWORD",
    "INIT_ADMIN_EMAIL",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
}

PROVIDER_KEYS = {
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
    "ZHIPU_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "MINIMAX_API_KEY",
    "SILICONFLOW_API_KEY",
    "QWEN_API_KEY",
    "QIANFAN_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACEHUB_API_TOKEN",
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
}


def _fail(message: str, code: int = 2) -> int:
    print(f"[isolated-tests] ERROR: {message}", file=sys.stderr)
    return code


def _ignore_copy(dir_path: str, names: list[str]) -> set[str]:
    """Return names to skip while copying the current workspace."""
    base = Path(dir_path)
    ignored: set[str] = set()

    for name in names:
        if name in IGNORED_DIR_NAMES:
            ignored.add(name)
            continue

        # Root business/runtime data only.
        if base == EXPECTED_SOURCE and name in ROOT_IGNORED_DIR_NAMES:
            ignored.add(name)
            continue

        if name.lower().endswith((".db", ".sqlite", ".sqlite3")):
            ignored.add(name)
            continue

        if name == ".coverage" or name.startswith(".coverage."):
            ignored.add(name)
            continue

        if name == "deploy-docker.sh":
            ignored.add(name)
            continue

        # Do not copy environment files that may contain real secrets.
        # Keep the public template and the frontend development file required
        # by test_config_b2.
        if name == ".env" or name.startswith(".env."):
            if name == ".env.example":
                continue
            if base.name == "frontend" and name == ".env.development":
                continue
            ignored.add(name)
            continue

        # Real business spreadsheets live at the project root. Synthetic
        # fixtures below tests/fixtures are deliberately preserved.
        if base == EXPECTED_SOURCE and name.lower().endswith(".xlsx"):
            ignored.add(name)

    return ignored


def _sqlite_uri(path: Path) -> str:
    return "sqlite:///" + path.resolve().as_posix()


def _make_isolation_env(workspace: Path, isolation: Path) -> dict[str, str]:
    """Build a clean environment for the pytest child process.

    Values are test-only. Sensitive caller configuration is removed rather
    than passed through; no caller-provided provider key is retained.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in CLEAR_EXACT
        and key not in PROVIDER_KEYS
        and not key.startswith("TEST_")
        and not key.startswith("LLM_")
        and not key.startswith("LANGSMITH_")
        and not key.startswith("LANGCHAIN_")
    }

    # Keep the child's temporary files inside the launcher-owned scratch tree.
    tmp_dir = isolation / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env.update({
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
    })

    paths = {
        "DATABASE_URL": _sqlite_uri(isolation / "test.db"),
        "TEST_DATABASE_URL": _sqlite_uri(isolation / "test.db"),
        "VECTOR_DB_PATH": str(isolation / "chroma"),
        "TEST_VECTOR_DB_PATH": str(isolation / "chroma"),
        "UPLOAD_TEMP_DIR": str(isolation / "uploads"),
        "TEST_UPLOAD_FOLDER": str(isolation / "uploads"),
        "LOG_FILE": str(isolation / "logs" / "app.log"),
        "TEST_LOG_FILE": str(isolation / "logs" / "app.log"),
        "EXCEL_DATA_ROOT": str(isolation / "excel"),
        "TEST_EXCEL_DATA_ROOT": str(isolation / "excel"),
        "TEST_ISOLATION_ROOT": str(isolation),
    }
    env.update(paths)
    env.update({
        "ENVIRONMENT": "testing",
        "JWT_SECRET": "unit-test-only-isolated-launcher-secret",
        "API_KEY": "unit-test-only-key",
        "API_KEY_SALT": "unit-test-only-salt",
        "LLM_PROVIDER": "local",
        "LLM_MODEL": "unit-test-model",
        "LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "EMBEDDING_MODEL": "unit-test-embedding-model",
        "TEST_LLM_PROVIDER": "local",
        "TEST_LLM_MODEL": "unit-test-model",
        "TEST_LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "DEBUG": "false",
        "LOG_LEVEL": "INFO",
        "PYTHONPATH": str(workspace),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    return env


def _snapshot_artifacts(root: Path) -> dict[str, tuple[bool, int | None]]:
    names = (
        "data",
        "logs",
        "cache",
        "uploads",
        "chroma",
        "chroma_db",
        "my_ai_assistant.db",
        ".pytest_cache",
        ".coverage",
        "htmlcov",
        "test-reports",
    )
    snapshot: dict[str, tuple[bool, int | None]] = {}
    for name in names:
        path = root / name
        if path.exists():
            try:
                snapshot[name] = (True, path.stat().st_mtime_ns)
            except OSError:
                snapshot[name] = (True, None)
        else:
            snapshot[name] = (False, None)
    return snapshot


def _select_tests(raw_args: list[str]) -> list[str]:
    if not raw_args:
        return list(DEFAULT_TESTS)

    tests_root = (EXPECTED_SOURCE / "tests").resolve()
    selected: list[str] = []
    for raw in raw_args:
        candidate = Path(raw)
        if candidate.is_absolute():
            raise ValueError(f"测试路径必须是相对 tests/ 的路径：{raw}")
        resolved = (EXPECTED_SOURCE / candidate).resolve()
        if not resolved.is_relative_to(tests_root):
            raise ValueError(f"测试路径必须位于 tests/ 下：{raw}")
        if not resolved.exists():
            raise ValueError(f"测试路径不存在：{raw}")
        selected.append(candidate.as_posix())
    return selected


def _verify_copy(workspace: Path, isolation: Path, tests: list[str]) -> None:
    if (workspace / ".env").exists():
        raise RuntimeError("临时副本包含正式 .env")
    for name in ROOT_IGNORED_DIR_NAMES:
        if (workspace / name).exists():
            raise RuntimeError(f"临时副本包含业务/运行时目录：{name}")
    if (workspace / "deploy-docker.sh").exists():
        raise RuntimeError("临时副本包含 deploy-docker.sh")
    if list(workspace.glob("*.xlsx")):
        raise RuntimeError("临时副本根目录包含真实业务 Excel")

    if isolation.resolve() == workspace.resolve() or isolation.resolve().is_relative_to(workspace.resolve()):
        raise RuntimeError("isolation 必须位于临时副本之外")
    if isolation.resolve().is_relative_to(EXPECTED_SOURCE):
        raise RuntimeError("isolation 不得位于正式工作区")

    for test in tests:
        if not (workspace / test).is_file():
            raise RuntimeError(f"临时副本缺少测试文件：{test}")

    # Prove untracked tests and synthetic fixtures made it into the copy.
    required = (
        "tests/unit/test_testing_isolation.py",
        "tests/unit/test_exception_security.py",
        "tests/unit/test_config_environment.py",
        "tests/fixtures/excel/golden_small.xlsx",
        ".env.example",
        "frontend/.env.development",
        "start.sh",
    )
    for rel in required:
        if not (workspace / rel).exists():
            raise RuntimeError(f"临时副本缺少必要文件：{rel}")

    print("[isolated-tests] copy_checks             = passed")
    print("[isolated-tests] copy_formal_env         = false")
    print("[isolated-tests] copy_business_dirs       = false")
    print("[isolated-tests] copy_root_business_xlsx  = false")
    print("[isolated-tests] copy_deploy_docker       = false")
    print("[isolated-tests] isolation_outside_copy   = true")
    print("[isolated-tests] required_tests_present   = true")
    print("[isolated-tests] synthetic_fixtures_present = true")


def _cleanup(scratch: Path) -> bool:
    """Remove only the launcher-created scratch root."""
    try:
        shutil.rmtree(scratch)
        return not scratch.exists()
    except OSError as exc:
        print(f"[isolated-tests] WARNING: 临时目录清理失败：{exc}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run selected pytest files in an isolated temporary copy."
    )
    parser.add_argument(
        "tests",
        nargs="*",
        help="测试路径（相对 tests/；默认运行首轮安全测试集）",
    )
    args = parser.parse_args(argv)

    try:
        tests = _select_tests(args.tests)
    except ValueError as exc:
        return _fail(str(exc))

    if Path(__file__).resolve().parents[1] != EXPECTED_SOURCE:
        return _fail(f"启动器不在指定工作区运行：{Path(__file__).resolve()}")
    if not EXPECTED_SOURCE.is_dir():
        return _fail(f"工作区不存在：{EXPECTED_SOURCE}")

    scratch = Path(tempfile.mkdtemp(prefix="my_ai_assistant_pytest_"))
    workspace = scratch / "workspace"
    isolation = scratch / "isolation"
    source_artifacts_before = _snapshot_artifacts(EXPECTED_SOURCE)
    pytest_code: int | None = None
    started = time.time()

    try:
        isolation.mkdir(parents=True, exist_ok=False)
        shutil.copytree(EXPECTED_SOURCE, workspace, ignore=_ignore_copy)
        _verify_copy(workspace, isolation, tests)

        env = _make_isolation_env(workspace, isolation)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "-p",
            "no:cacheprovider",
            "--no-cov",
            "-q",
        ]

        print(f"[isolated-tests] source      = {EXPECTED_SOURCE}")
        print(f"[isolated-tests] workspace   = {workspace}")
        print(f"[isolated-tests] isolation   = {isolation}")
        print(f"[isolated-tests] cwd         = {workspace}")
        print("[isolated-tests] environment = testing")
        print(f"[isolated-tests] command     = {' '.join(cmd)}")

        result = subprocess.run(cmd, cwd=str(workspace), env=env, check=False)
        pytest_code = result.returncode
        print(f"[isolated-tests] pytest_exit = {pytest_code}")
        print(f"[isolated-tests] elapsed     = {time.time() - started:.2f}s")

        source_artifacts_after = _snapshot_artifacts(EXPECTED_SOURCE)
        if source_artifacts_after != source_artifacts_before:
            print(
                "[isolated-tests] WARNING: 原工作区运行时产物状态发生变化",
                file=sys.stderr,
            )
            if pytest_code == 0:
                pytest_code = 4
        else:
            print("[isolated-tests] source_artifacts_unchanged = true")

        return pytest_code

    except Exception as exc:  # noqa: BLE001 - launcher reports setup failures
        print(f"[isolated-tests] ERROR: {exc}", file=sys.stderr)
        return 3
    finally:
        cleaned = _cleanup(scratch)
        print(f"[isolated-tests] cleanup_ok = {cleaned}")


if __name__ == "__main__":
    raise SystemExit(main())
