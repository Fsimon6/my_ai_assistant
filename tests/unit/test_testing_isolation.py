"""TestingConfig 运行时隔离验证（父进程不导入 backend）。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_CLEAR_ENV = (
    'ENVIRONMENT', 'JWT_SECRET', 'API_KEY', 'DATABASE_URL',
    'VECTOR_DB_PATH', 'UPLOAD_TEMP_DIR', 'LOG_FILE', 'EXCEL_DATA_ROOT',
    'LLM_PROVIDER', 'LLM_MODEL', 'LLM_BASE_URL', 'TEST_ISOLATION_ROOT',
    'TEST_DATABASE_URL', 'TEST_VECTOR_DB_PATH', 'TEST_UPLOAD_FOLDER',
    'TEST_LOG_FILE', 'TEST_EXCEL_DATA_ROOT', 'TEST_LLM_PROVIDER',
    'TEST_LLM_MODEL', 'TEST_LLM_BASE_URL',
)

PROBE = r'''
import asyncio
import io
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

import backend.config as c
from backend.config.settings import settings
from backend.database.base import engine
from backend.main import app
from backend.services.document_service import save_uploaded_file
from backend.services.llm_service import LLMFactory
from backend.services.vector_service import get_vector_store_manager
from backend.utils.security import create_access_token, decode_access_token

root = Path(os.environ['TEST_ISOLATION_ROOT']).resolve()
expected_db = root / 'test.db'
expected_chroma = root / 'chroma'
expected_uploads = root / 'uploads'
expected_log = root / 'logs' / 'app.log'
expected_excel = root / 'excel'

assert c.Config.__name__ == 'TestingConfig'
assert c.Config.JWT_SECRET == settings.JWT_SECRET
assert c.Config.DATABASE_URL == settings.DATABASE_URL
assert c.Config.VECTOR_DB_PATH == settings.VECTOR_DB_PATH
assert c.Config.UPLOAD_FOLDER == settings.UPLOAD_TEMP_DIR
assert c.Config.LLM_PROVIDER == settings.LLM_PROVIDER

c.Config.validate_config()

with TestClient(app) as client:
    health = client.get('/health')

token = create_access_token({'sub': 'isolation-probe'})
payload = decode_access_token(token)
assert payload['sub'] == 'isolation-probe'

with engine.connect() as conn:
    assert conn.execute(text('SELECT 1')).scalar() == 1
engine_db = Path(engine.url.database).resolve()

manager = get_vector_store_manager()
chroma_path = Path(manager.persist_directory).resolve()

llm = LLMFactory.from_env()
assert llm.config.provider == settings.LLM_PROVIDER == 'local'
assert llm.config.model == settings.LLM_MODEL


class Upload:
    filename = 'probe.txt'
    content_type = 'text/plain'

    class _File:
        def __init__(self):
            self._buf = io.BytesIO(b'isolation probe')

        def read(self, size=-1):
            return self._buf.read(size)

    def __init__(self):
        self.file = self._File()


upload_path = Path(asyncio.run(save_uploaded_file(Upload()))).resolve()
upload_exists = upload_path.is_file()
upload_path.unlink()

print('PROBE_JSON:' + json.dumps({
    'config': c.Config.__name__,
    'health_status': health.status_code,
    'health_body': health.json(),
    'db_engine': str(engine_db),
    'db_expected': str(expected_db.resolve()),
    'chroma': str(chroma_path),
    'chroma_expected': str(expected_chroma.resolve()),
    'chroma_exists': chroma_path.is_dir(),
    'upload': str(upload_path.parent),
    'upload_expected': str(expected_uploads.resolve()),
    'upload_exists': upload_exists,
    'log_exists': expected_log.is_file(),
    'log_expected': str(expected_log.resolve()),
    'excel_expected': str(expected_excel.resolve()),
    'jwt_sub': payload['sub'],
    'llm_provider': llm.config.provider,
    'llm_model': llm.config.model,
}, ensure_ascii=False))
'''

VALIDATE_PROBE = r'''
import json

try:
    import backend.config as c
    c.Config.validate_config()
except Exception as exc:
    print('VALIDATION_ERROR:' + str(exc))
else:
    print('VALIDATION_OK')
'''

STARTUP_PROBE = "import backend.main\nprint('IMPORTED')\n"

SETTINGS_PROBE = r'''
import json
from backend.config.settings import settings

print('SETTINGS_JSON:' + json.dumps({
    'database': settings.DATABASE_URL,
    'vector': settings.VECTOR_DB_PATH,
    'upload': settings.UPLOAD_TEMP_DIR,
    'log': settings.LOG_FILE,
    'excel': settings.EXCEL_DATA_ROOT,
    'provider': settings.LLM_PROVIDER,
    'model': settings.LLM_MODEL,
}, ensure_ascii=False))
'''

LOG_PROBE = r'''
import json
import logging
from backend.config.settings import settings
from backend.utils.logger import setup_logging

setup_logging()
print('LOG_JSON:' + json.dumps({
    'log': settings.LOG_FILE,
    'handlers': [type(h).__name__ for h in logging.getLogger().handlers],
}, ensure_ascii=False))
'''


def _paths(root: Path) -> dict:
    return {
        'TEST_ISOLATION_ROOT': str(root),
        'TEST_DATABASE_URL': 'sqlite:///' + (root / 'test.db').as_posix(),
        'TEST_VECTOR_DB_PATH': str(root / 'chroma'),
        'TEST_UPLOAD_FOLDER': str(root / 'uploads'),
        'TEST_LOG_FILE': str(root / 'logs' / 'app.log'),
        'TEST_EXCEL_DATA_ROOT': str(root / 'excel'),
        'TEST_LLM_PROVIDER': 'local',
        'TEST_LLM_MODEL': 'unit-test-model',
        'TEST_LLM_BASE_URL': 'http://127.0.0.1:1/v1',
    }


def _env(root: Path, overrides=None) -> dict:
    env = os.environ.copy()
    for key in _CLEAR_ENV:
        env.pop(key, None)
    env.update({
        'ENVIRONMENT': 'testing',
        'JWT_SECRET': 'unit-test-only-isolation-secret',
        'API_KEY': 'unit-test-only-key',
        # 故意把标准字段指向正式路径：TEST_* 必须覆盖它们，否则校验失败。
        'DATABASE_URL': 'sqlite:///' + (ROOT / 'my_ai_assistant.db').as_posix(),
        'VECTOR_DB_PATH': str(ROOT / 'data' / 'chroma_db'),
        'UPLOAD_TEMP_DIR': str(ROOT / 'data' / 'uploads'),
        'LOG_FILE': str(ROOT / 'logs' / 'app.log'),
        'EXCEL_DATA_ROOT': str(ROOT / 'data' / 'excel'),
        'LLM_PROVIDER': 'openai',
        'ANONYMIZED_TELEMETRY': 'False',
        'PYTHONPATH': str(ROOT),
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    env.update(_paths(root))
    env.update(overrides or {})
    return env


def _run_raw(code: str, env: dict):
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=180,
    )


def _run(code: str, root: Path, overrides=None):
    return _run_raw(code, _env(root, overrides))


def _standard_testing_env(iso_root: Path) -> dict:
    """testing 但完全使用标准 Settings 字段，不设置任何 TEST_* 路径别名。"""
    env = _env(iso_root)
    for key in (
        'TEST_DATABASE_URL', 'TEST_VECTOR_DB_PATH', 'TEST_UPLOAD_FOLDER',
        'TEST_LOG_FILE', 'TEST_EXCEL_DATA_ROOT', 'TEST_LLM_PROVIDER',
        'TEST_LLM_MODEL', 'TEST_LLM_BASE_URL',
    ):
        env.pop(key, None)
    env.update({
        'DATABASE_URL': 'sqlite:///' + (iso_root / 'test.db').as_posix(),
        'VECTOR_DB_PATH': str(iso_root / 'chroma'),
        'UPLOAD_TEMP_DIR': str(iso_root / 'uploads'),
        'LOG_FILE': str(iso_root / 'logs' / 'app.log'),
        'EXCEL_DATA_ROOT': str(iso_root / 'excel'),
        'LLM_PROVIDER': 'local',
        'LLM_MODEL': 'unit-test-model',
        'LLM_BASE_URL': 'http://127.0.0.1:1/v1',
    })
    env['TEST_ISOLATION_ROOT'] = str(iso_root)
    return env


def _json_line(proc, prefix: str):
    for line in proc.stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix):])
    raise AssertionError('未找到 %s；stdout=%r stderr=%r' % (
        prefix, proc.stdout[-1000:], proc.stderr[-2000:]))


def test_testing_runtime_uses_isolated_settings(tmp_path):
    root = (tmp_path / 'runtime').resolve()
    proc = _run(PROBE, root)
    assert proc.returncode == 0, proc.stderr[-4000:]
    data = _json_line(proc, 'PROBE_JSON:')

    assert data['config'] == 'TestingConfig'
    assert data['health_status'] == 200
    assert data['health_body']['status'] == 'healthy'
    assert data['jwt_sub'] == 'isolation-probe'

    assert Path(data['db_engine']) == Path(data['db_expected'])
    assert Path(data['chroma']) == Path(data['chroma_expected'])
    assert Path(data['upload']) == Path(data['upload_expected'])
    assert Path(data['log_expected']).is_file()

    assert data['chroma_exists'] is True
    assert data['upload_exists'] is True
    assert data['llm_provider'] == 'local'
    assert data['llm_model'] == 'unit-test-model'


@pytest.mark.parametrize('key,label', [
    ('TEST_DATABASE_URL', 'DATABASE_URL'),
    ('TEST_VECTOR_DB_PATH', 'VECTOR_DB_PATH'),
    ('TEST_UPLOAD_FOLDER', 'UPLOAD_TEMP_DIR'),
    ('TEST_LOG_FILE', 'LOG_FILE'),
    ('TEST_EXCEL_DATA_ROOT', 'EXCEL_DATA_ROOT'),
])
def test_testing_rejects_formal_paths(tmp_path, key, label):
    root = (tmp_path / 'reject').resolve()
    formal = {
        'TEST_DATABASE_URL': 'sqlite:///' + (ROOT / 'my_ai_assistant.db').as_posix(),
        'TEST_VECTOR_DB_PATH': str(ROOT / 'data' / 'chroma_db'),
        'TEST_UPLOAD_FOLDER': str(ROOT / 'data' / 'uploads'),
        'TEST_LOG_FILE': str(ROOT / 'logs' / 'app.log'),
        'TEST_EXCEL_DATA_ROOT': str(ROOT / 'data' / 'excel'),
    }
    proc = _run(VALIDATE_PROBE, root, {key: formal[key]})
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert 'VALIDATION_ERROR:' in proc.stdout
    assert label in proc.stdout
    assert '隔离根目录' in proc.stdout


def test_invalid_testing_database_has_no_project_side_effect(tmp_path):
    root = (tmp_path / 'db_guard').resolve()
    parent = ROOT / 'side_effect_db' / tmp_path.name
    bad_db = parent / 'bad.db'
    proc = _run(
        'import backend.main\nprint("IMPORTED")',
        root,
        {'TEST_DATABASE_URL': 'sqlite:///' + bad_db.as_posix()},
    )

    assert proc.returncode != 0, proc.stdout
    assert 'DATABASE_URL' in proc.stderr
    assert not parent.exists()
    assert not bad_db.exists()


def test_invalid_testing_log_has_no_project_side_effect(tmp_path):
    root = (tmp_path / 'log_guard').resolve()
    parent = ROOT / 'side_effect_logs' / tmp_path.name
    bad_log = parent / 'app.log'
    proc = _run(
        'import backend.main\nprint("IMPORTED")',
        root,
        {'TEST_LOG_FILE': str(bad_log)},
    )

    assert proc.returncode != 0, proc.stdout
    assert 'LOG_FILE' in proc.stderr
    assert not parent.exists()
    assert not bad_log.exists()


@pytest.mark.parametrize('key,value', [
    ('TEST_VECTOR_DB_PATH', '~/chroma'),
    ('TEST_UPLOAD_FOLDER', '~/uploads'),
    ('TEST_LOG_FILE', '~/logs/app.log'),
    ('TEST_EXCEL_DATA_ROOT', '~/excel'),
    ('TEST_DATABASE_URL', 'sqlite:///~/test.db'),
])
def test_testing_rejects_home_shorthand(tmp_path, key, value):
    root = (tmp_path / 'home_guard').resolve()
    proc = _run(VALIDATE_PROBE, root, {key: value})

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert 'VALIDATION_ERROR:' in proc.stdout
    assert '~' in proc.stdout
    assert not (ROOT / '~').exists()


@pytest.mark.parametrize('value,set_value', [
    (None, False),
    ('', True),
    ('   ', True),
])
def test_blank_test_log_is_disabled(tmp_path, value, set_value):
    root = (tmp_path / 'blank_log').resolve()
    env = _env(root)
    env.pop('LOG_FILE', None)
    env.pop('TEST_LOG_FILE', None)
    if set_value:
        env['TEST_LOG_FILE'] = value

    proc = _run_raw(LOG_PROBE, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = _json_line(proc, 'LOG_JSON:')
    assert data['log'] == ''
    assert 'RotatingFileHandler' not in data['handlers']


@pytest.mark.parametrize('environment', ['development', 'production'])
def test_non_testing_ignores_test_overrides(tmp_path, environment):
    iso_root = (tmp_path / 'iso').resolve()
    standard = (tmp_path / 'standard').resolve()
    override_root = ROOT / 'test_override' / tmp_path.name
    std_db = 'sqlite:///' + (standard / 'test.db').as_posix()
    env = _env(iso_root, {
        'ENVIRONMENT': environment,
        'DATABASE_URL': std_db,
        'VECTOR_DB_PATH': str(standard / 'chroma'),
        'UPLOAD_TEMP_DIR': str(standard / 'uploads'),
        'LOG_FILE': str(standard / 'logs' / 'app.log'),
        'EXCEL_DATA_ROOT': str(standard / 'excel'),
        'LLM_PROVIDER': 'openai',
        'LLM_MODEL': 'standard-model',
        'API_KEY': 'unit-test-only-key',
        'TEST_DATABASE_URL': 'sqlite:///' + (override_root / 'test.db').as_posix(),
        'TEST_VECTOR_DB_PATH': str(override_root / 'chroma'),
        'TEST_UPLOAD_FOLDER': str(override_root / 'uploads'),
        'TEST_LOG_FILE': str(override_root / 'logs' / 'app.log'),
        'TEST_EXCEL_DATA_ROOT': str(override_root / 'excel'),
        'TEST_LLM_PROVIDER': 'local',
        'TEST_LLM_MODEL': 'override-model',
    })

    proc = _run_raw(SETTINGS_PROBE, env)
    assert proc.returncode == 0, proc.stderr[-3000:]
    data = _json_line(proc, 'SETTINGS_JSON:')

    assert data['database'] == std_db
    assert data['vector'] == str(standard / 'chroma')
    assert data['upload'] == str(standard / 'uploads')
    assert data['log'] == str(standard / 'logs' / 'app.log')
    assert data['excel'] == str(standard / 'excel')
    assert data['provider'] == 'openai'
    assert data['model'] == 'standard-model'
    assert not override_root.exists()


@pytest.mark.parametrize('key,label,rel', [
    ('DATABASE_URL', 'DATABASE_URL', 'test.db'),
    ('VECTOR_DB_PATH', 'VECTOR_DB_PATH', 'chroma'),
    ('UPLOAD_TEMP_DIR', 'UPLOAD_TEMP_DIR', 'uploads'),
    ('LOG_FILE', 'LOG_FILE', 'logs/app.log'),
    ('EXCEL_DATA_ROOT', 'EXCEL_DATA_ROOT', 'excel'),
])
def test_testing_standard_project_paths_are_rejected(tmp_path, key, label, rel):
    iso_root = (tmp_path / 'std_iso').resolve()
    target_dir = ROOT / 'standard_side_effect' / tmp_path.name
    env = _standard_testing_env(iso_root)
    if key == 'DATABASE_URL':
        env[key] = 'sqlite:///' + (target_dir / 'test.db').as_posix()
    else:
        env[key] = str(target_dir / rel)

    proc = _run_raw(STARTUP_PROBE, env)
    assert proc.returncode != 0, proc.stdout
    assert label in proc.stderr
    assert not target_dir.exists()


@pytest.mark.parametrize('inside_project', [True, False])
def test_testing_isolation_root_inside_project_fails(tmp_path, inside_project):
    iso_root = (tmp_path / 'valid_iso').resolve()
    if inside_project:
        bad_root = ROOT
    else:
        bad_root = ROOT / 'isolation_subdir_guard' / tmp_path.name
    target_dir = ROOT / 'isolation_root_side_effect' / tmp_path.name
    env = _standard_testing_env(iso_root)
    env['TEST_ISOLATION_ROOT'] = str(bad_root)
    env['DATABASE_URL'] = 'sqlite:///' + (target_dir / 'test.db').as_posix()

    proc = _run_raw(STARTUP_PROBE, env)
    assert proc.returncode != 0, proc.stdout
    assert '隔离根目录' in proc.stderr
    assert not target_dir.exists()
    if not inside_project:
        assert not bad_root.exists()


@pytest.mark.parametrize('key,label,rel', [
    ('TEST_DATABASE_URL', 'DATABASE_URL', 'test.db'),
    ('TEST_VECTOR_DB_PATH', 'VECTOR_DB_PATH', 'chroma'),
    ('TEST_UPLOAD_FOLDER', 'UPLOAD_TEMP_DIR', 'uploads'),
    ('TEST_LOG_FILE', 'LOG_FILE', 'logs/app.log'),
    ('TEST_EXCEL_DATA_ROOT', 'EXCEL_DATA_ROOT', 'excel'),
])
def test_testing_rejects_parent_traversal(tmp_path, key, label, rel):
    iso_root = (tmp_path / 'escape_iso').resolve()
    escape_dir = (tmp_path / 'escape_out').resolve()
    env = _env(iso_root)
    if key == 'TEST_DATABASE_URL':
        env[key] = 'sqlite:///' + (iso_root / '..' / 'escape_out' / 'test.db').as_posix()
    else:
        env[key] = str(iso_root / '..' / 'escape_out' / rel)

    proc = _run_raw(STARTUP_PROBE, env)
    assert proc.returncode != 0, proc.stdout
    assert label in proc.stderr
    assert not escape_dir.exists()
