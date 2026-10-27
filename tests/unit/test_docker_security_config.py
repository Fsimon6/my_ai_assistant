# -*- coding: utf-8 -*-
"""Docker 构建上下文与秘密文件隔离的静态回归测试。"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_root_dockerfile_does_not_copy_dotenv():
    content = _read("Dockerfile")

    assert not re.search(
        r"^\s*(?:COPY|ADD)\s+[^\n]*\.env(?:\s|$)",
        content,
        flags=re.IGNORECASE | re.MULTILINE,
    )


def test_root_dockerignore_covers_nested_secrets_business_files_and_local_state():
    content = _read(".dockerignore")

    assert "**/.env" in content
    assert "**/.env.*" in content
    assert "**/*.pem" in content
    assert "**/*.key" in content
    assert "**/*.p12" in content
    assert "**/*.xlsx" in content
    assert "**/*.xls" in content
    assert "**/*.xlsm" in content
    assert "**/*.err" in content
    assert "**/*.duckdb" in content
    assert "**/*.sqlite3" in content


def test_frontend_dockerignore_protects_dev_context_and_keeps_tracked_env():
    path = REPO_ROOT / "frontend" / ".dockerignore"
    assert path.exists()
    content = path.read_text(encoding="utf-8")

    assert ".env" in content
    assert "**/.env" in content
    assert "**/.env.*" in content
    assert "!.env.development" in content
    assert "!.env.production" in content
    assert "node_modules/" in content
    assert "dist/" in content
    assert "**/*.pem" in content
    assert "**/*.key" in content


def test_dev_backend_build_context_matches_dockerfile_copy_paths():
    content = _read("docker-compose-dev.yml")

    assert "context: ." in content
    assert "dockerfile: backend/Dockerfile" in content
    assert "context: ./backend" not in content
    assert "./backend:/app/backend" in content
    assert "python -m backend.init_db" in content
    backend_section = content.split("frontend-dev:", 1)[0]
    frontend_section = content.split("frontend-dev:", 1)[1]
    assert "env_file:" in backend_section
    assert "- .env" in backend_section
    assert "env_file:" not in frontend_section


def test_dev_compose_host_ports_and_proxy_contract():
    compose = _read("docker-compose-dev.yml")
    vite = _read("frontend/vite.config.ts")
    dockerfile = _read("frontend/Dockerfile.dev")
    env_dev = _read("frontend/.env.development")

    assert '"8021:8000"' in compose
    assert '"5181:5181"' in compose
    assert "DEBUG=true" in compose
    assert "$${DEBUG:-false}" in compose
    assert "python -m backend.init_db &&" in compose
    assert "--reload" in compose
    assert "DEV_PROXY_TARGET=http://backend-dev:8000" in compose
    assert "--port 5181" in compose
    assert "EXPOSE 5181" in dockerfile
    assert "process.env.DEV_PROXY_TARGET" in vite
    assert "http://127.0.0.1:8021" in vite
    assert "VITE_API_BASE_URL=http://127.0.0.1:8021" in env_dev


def test_production_compose_keeps_existing_build_contract():
    content = _read("docker-compose.yml")

    assert "dockerfile: backend/Dockerfile" in content
    assert "dockerfile: frontend/Dockerfile" in content
    assert "context: ." in content
    assert "8000" in content


def test_deploy_script_uses_placeholders_and_required_config_check():
    path = REPO_ROOT / "deploy-docker.sh"
    if not path.exists():
        pytest.skip("deploy-docker.sh 被隔离启动器排除，需用独立静态检查验证")

    content = path.read_text(encoding="utf-8")

    assert "JWT_SECRET=change-me-to-a-random-secret-string" in content
    assert "QIANFAN_API_KEY=replace-with-provider-key" in content
    assert "QIANFAN_SECRET_KEY=replace-with-provider-secret" in content
    assert "JWT_SECRET 尚未配置" in content
    assert "if [ -f .env ]" in content
    assert "cp .env.example .env" in content
    assert not re.search(
        r"(?:SECRET_KEY|QIANFAN_API_KEY|QIANFAN_SECRET_KEY)=['\"][A-Za-z0-9+/=_-]{12,}['\"]",
        content,
    )


def test_example_env_files_only_contain_placeholders():
    assert "JWT_SECRET=change-me-to-a-random-secret-string" in _read(".env.example")
    assert "API_KEY=sk-your-api-key-here" in _read(".env.example")
    assert "JWT_SECRET=change-me-to-a-random-secret-string" in _read(
        "backend/.env.example"
    )
    assert "API_KEY=sk-your-api-key-here" in _read("backend/.env.example")
