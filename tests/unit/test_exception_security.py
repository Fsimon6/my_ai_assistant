"""生产环境异常响应安全边界测试（父进程不导入 backend）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SENSITIVE = 'SENSITIVE_DB_PASSWORD_7f3a9c'
INTERNAL_PATH = '/internal/secret/path/database.sqlite3'

PROBE = r'''
import asyncio
import json

from fastapi import HTTPException
from backend.utils.exceptions import global_exception_handler

SENSITIVE = %(sensitive)r
INTERNAL_PATH = %(internal_path)r


class DummyRequest:
    method = 'GET'
    url = 'http://testserver/api/v1/boom?token=' + SENSITIVE
    client = None


async def main():
    try:
        raise RuntimeError(
            'database connection failed at ' + INTERNAL_PATH + ' password=' + SENSITIVE
        )
    except RuntimeError as exc:
        response = await global_exception_handler(DummyRequest(), exc)
    print('PROBE_JSON:' + json.dumps({
        'status': response.status_code,
        'body': json.loads(response.body),
    }, ensure_ascii=False))

    http_exc = HTTPException(status_code=404, detail='资源不存在')
    http_response = await global_exception_handler(DummyRequest(), http_exc)
    print('HTTP_JSON:' + json.dumps({
        'status': http_response.status_code,
        'body': json.loads(http_response.body),
    }, ensure_ascii=False))


asyncio.run(main())
''' % {'sensitive': SENSITIVE, 'internal_path': INTERNAL_PATH}


def _run_probe(tmp_path, environment, debug):
    env = os.environ.copy()
    for key in (
        'ENVIRONMENT', 'DEBUG', 'JWT_SECRET', 'DATABASE_URL',
        'VECTOR_DB_PATH', 'UPLOAD_TEMP_DIR', 'LOG_FILE', 'LOG_LEVEL',
        'LLM_PROVIDER', 'API_KEY', 'ALLOWED_HOSTS', 'BACKEND_CORS_ORIGINS',
    ):
        env.pop(key, None)
    env.update({
        'ENVIRONMENT': environment,
        'DEBUG': debug,
        'JWT_SECRET': 'unit-test-only-production-secret',
        'DATABASE_URL': f'sqlite:///{(tmp_path / "test.db").as_posix()}',
        'VECTOR_DB_PATH': str(tmp_path / 'chroma'),
        'UPLOAD_TEMP_DIR': str(tmp_path / 'uploads'),
        'LOG_FILE': str(tmp_path / 'app.log'),
        'PYTHONPATH': str(ROOT),
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    proc = subprocess.run(
        [sys.executable, '-c', PROBE],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    payloads = {}
    for line in proc.stdout.splitlines():
        if line.startswith('PROBE_JSON:'):
            payloads['error'] = json.loads(line.removeprefix('PROBE_JSON:'))
        elif line.startswith('HTTP_JSON:'):
            payloads['http'] = json.loads(line.removeprefix('HTTP_JSON:'))
    assert set(payloads) == {'error', 'http'}, proc.stdout
    return payloads


def test_production_debug_true_masks_internal_error(tmp_path):
    result = _run_probe(tmp_path, 'production', 'true')
    response = result['error']
    body = response['body']
    serialized = json.dumps(body, ensure_ascii=False)

    assert response['status'] == 500
    assert body['success'] is False
    assert body['error']['code'] == 'ERR_INTERNAL'
    assert body['error']['message'] == '服务器内部错误，请稍后重试'
    assert body['error']['type'] == 'InternalServerError'
    assert 'traceback' not in body['error']
    assert 'detail' not in body['error']
    assert 'timestamp' in body
    assert SENSITIVE not in serialized
    assert INTERNAL_PATH not in serialized
    assert 'RuntimeError' not in serialized
    assert 'Traceback' not in serialized


def test_production_debug_true_does_not_change_http_error(tmp_path):
    response = _run_probe(tmp_path, 'production', 'true')['http']
    body = response['body']

    assert response['status'] == 404
    assert body['success'] is False
    assert body['error']['code'] == 'ERR_404'
    assert body['error']['message'] == '资源不存在'
    assert body['error']['type'] == 'HTTPException'


def test_development_debug_true_keeps_diagnostic_detail(tmp_path):
    response = _run_probe(tmp_path, 'development', 'true')['error']
    body = response['body']

    assert response['status'] == 500
    assert body['error']['code'] == 'ERR_INTERNAL'
    assert body['error']['type'] == 'RuntimeError'
    assert SENSITIVE in body['error']['message']
    assert INTERNAL_PATH in body['error']['message']
    assert SENSITIVE in body['error']['traceback']
    assert INTERNAL_PATH in body['error']['detail']
