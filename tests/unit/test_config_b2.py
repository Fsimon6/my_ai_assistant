# -*- coding: utf-8 -*-
"""B2：关键运维参数真正生效（全部经子进程验证，父进程不导入 backend）。

覆盖：
  A. `LLM_TIMEOUT`（秒）——默认 30、`.env`/环境变量优先级、非法值报错、
     并真正传给 `AsyncOpenAI(timeout=...)`（fake client，不发真实请求）；
  B. `MAX_UPLOAD_SIZE`（字节）——默认 10MiB、99MiB 上限、非法值报错、
     真实在 `save_uploaded_file` 生效（普通文档与 Excel 共用同一入口），
     且配置放大后不再被旧的 10MB 硬编码常量拒绝；
  C. HOST/PORT 约定——开发默认 8021、生产默认 8000，且 README / Vite / 启动脚本一致。

隔离约定：
  * 本文件在父 pytest 进程中**不导入任何 `backend.*`**（也不导入 Settings），
    因此不会缓存使用测试密钥构造的 Settings 单例；
  * 需要 Settings / document_service / LLM client 的用例全部在子进程中执行；
  * 子进程先清除可能继承的真实配置，再注入固定测试值
    （`JWT_SECRET=unit-test-only-secret`、必要时 `API_KEY=unit-test-only-key`）；
  * 正式 `.env` 不读取、不修改；上传只写 `tmp_path`；不触碰正式 SQLite / Chroma。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_JWT_SECRET = 'unit-test-only-secret'
TEST_API_KEY = 'unit-test-only-key'
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
#: B2 上限（99MiB，严格低于 nginx 的 100M）
MAX_UPLOAD_BYTES = 99 * 1024 * 1024

#: 子进程启动前必须清除的变量（避免继承开发者真实配置/密钥）
_CLEAR_ENV = (
    'ENVIRONMENT', 'JWT_SECRET', 'DATABASE_URL', 'LLM_MODEL', 'LLM_TIMEOUT',
    'MAX_UPLOAD_SIZE', 'HOST', 'PORT', 'API_KEY', 'LLM_PROVIDER', 'LLM_BASE_URL',
    'EMBEDDING_MODEL', 'ALLOWED_HOSTS',
)


def _run(code: str, env_extra=None):
    env = dict(os.environ)
    for key in _CLEAR_ENV:
        env.pop(key, None)
    env['JWT_SECRET'] = TEST_JWT_SECRET
    env.update(env_extra or {})
    env['PYTHONPATH'] = str(REPO_ROOT)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return subprocess.run(
        [sys.executable, '-c', code], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
    )


def _probe(code: str, env_extra=None) -> dict:
    """执行探针并解析结构化 JSON（不只看退出码）。"""
    proc = _run(code, env_extra)
    assert proc.returncode == 0, proc.stderr[-3000:]
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('PROBE_JSON:'):
            return json.loads(line[len('PROBE_JSON:'):])
    raise AssertionError(
        'probe 未输出 PROBE_JSON；stdout=%r stderr=%r'
        % (proc.stdout[-800:], proc.stderr[-800:])
    )


def _kv(stdout: str) -> dict:
    out = {}
    for line in stdout.splitlines():
        if '=' in line and not line.startswith((' ', '\t')):
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


# ==========================================================================
# 探针：Settings 默认值 / 临时 .env / 非法值
# ==========================================================================
DEFAULTS_PROBE = """
import json
from backend.config.settings import Settings
f = Settings.model_fields
print('PROBE_JSON:' + json.dumps({
    'llm_default': f['LLM_TIMEOUT'].default,
    'llm_constraints': bool(f['LLM_TIMEOUT'].metadata),
    'upload_default': f['MAX_UPLOAD_SIZE'].default,
    'upload_constraints': bool(f['MAX_UPLOAD_SIZE'].metadata),
}))
"""


def _settings_probe(path) -> str:
    return (
        "import json\n"
        "from backend.config.settings import Settings\n"
        "s = Settings(_env_file=%r)\n"
        "print('PROBE_JSON:' + json.dumps({'llm_timeout': s.LLM_TIMEOUT,"
        " 'max_upload': s.MAX_UPLOAD_SIZE}))\n" % str(path)
    )


def _invalid_probe(path) -> str:
    return (
        "import json\n"
        "from backend.config.settings import Settings\n"
        "try:\n"
        "    Settings(_env_file=%r)\n"
        "except Exception as exc:\n"
        "    print('PROBE_JSON:' + json.dumps({'error': type(exc).__name__,"
        " 'message': str(exc)[:600]}))\n"
        "else:\n"
        "    print('PROBE_JSON:' + json.dumps({'error': None}))\n" % str(path)
    )


def _make_env_file(tmp_path, name, **pairs):
    lines = ['JWT_SECRET=' + TEST_JWT_SECRET]
    lines += ['%s=%s' % (k, v) for k, v in pairs.items()]
    path = tmp_path / (name + '.env')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


ALLOWED_HOSTS_SETTINGS_PROBE = (
    "import json\n"
    "from backend.config.settings import Settings, parse_allowed_hosts\n"
    "s = Settings(_env_file=%r)\n"
    "print('PROBE_JSON:' + json.dumps({'raw': s.ALLOWED_HOSTS,"
    " 'parsed': parse_allowed_hosts(s.ALLOWED_HOSTS)}))\n"
)

PRODUCTION_ALLOWED_HOSTS_PROBE = (
    "import json\n"
    "from backend.config.settings import settings\n"
    "settings.ALLOWED_HOSTS = 'api.example.com, localhost,, '\n"
    "from backend.config.production import ProductionConfig\n"
    "print('PROBE_JSON:' + json.dumps({"
    "'hosts': ProductionConfig.ALLOWED_HOSTS}))\n"
)

PRODUCTION_ALLOWED_HOSTS_EMPTY_PROBE = (
    "import json\n"
    "from backend.config.settings import settings\n"
    "settings.ALLOWED_HOSTS = ''\n"
    "from backend.config.production import ProductionConfig\n"
    "try:\n"
    "    ProductionConfig.validate_config()\n"
    "except ValueError as exc:\n"
    "    print('PROBE_JSON:' + json.dumps({'error': 'ValueError',"
    " 'message': str(exc)}))\n"
    "else:\n"
    "    print('PROBE_JSON:' + json.dumps({'error': None}))\n"
)

HOST_PORT_SETTINGS_PROBE = (
    "import json\n"
    "from backend.config.settings import Settings\n"
    "s = Settings(_env_file=%r)\n"
    "print('PROBE_JSON:' + json.dumps({'host': s.HOST, 'port': s.PORT}))\n"
)

HOST_PORT_INVALID_PROBE = (
    "import json\n"
    "from pydantic import ValidationError\n"
    "from backend.config.settings import Settings\n"
    "try:\n"
    "    Settings(_env_file=%r)\n"
    "except ValidationError as exc:\n"
    "    print('PROBE_JSON:' + json.dumps({'error': 'ValidationError',"
    " 'message': str(exc)[:600]}))\n"
    "else:\n"
    "    print('PROBE_JSON:' + json.dumps({'error': None}))\n"
)

DEV_SERVER_CONFIG_PROBE = (
    "import json\n"
    "import backend.config as c\n"
    "print('PROBE_JSON:' + json.dumps({'host': c.Config.HOST,"
    " 'port': c.Config.PORT}))\n"
)

PRODUCTION_SERVER_CONFIG_PROBE = (
    "import json\n"
    "from backend.config.production import ProductionConfig as C\n"
    "print('PROBE_JSON:' + json.dumps({'host': C.HOST, 'port': C.PORT}))\n"
)

MAIN_SERVER_CONFIG_PROBE = (
    "import json\n"
    "import backend.main as m\n"
    "print('PROBE_JSON:' + json.dumps({'host': m.Config.HOST,"
    " 'port': m.Config.PORT}))\n"
)


# ==========================================================================
# ALLOWED_HOSTS：统一 Settings 来源与生产配置消费
# ==========================================================================
def test_allowed_hosts_reads_tmp_env_and_strips_empty_entries(tmp_path):
    path = _make_env_file(
        tmp_path,
        'allowed-hosts',
        ALLOWED_HOSTS='api.example.com, localhost,,',
    )

    data = _probe(ALLOWED_HOSTS_SETTINGS_PROBE % str(path))

    assert data['raw'] == 'api.example.com, localhost,,'
    assert data['parsed'] == ['api.example.com', 'localhost']


def test_allowed_hosts_process_env_overrides_tmp_env(tmp_path):
    path = _make_env_file(
        tmp_path,
        'allowed-hosts-priority',
        ALLOWED_HOSTS='file.example.com',
    )

    data = _probe(
        ALLOWED_HOSTS_SETTINGS_PROBE % str(path),
        {'ALLOWED_HOSTS': 'env.example.com, env2.example.com'},
    )

    assert data['raw'] == 'env.example.com, env2.example.com'
    assert data['parsed'] == ['env.example.com', 'env2.example.com']


def test_production_config_uses_settings_allowed_hosts():
    data = _probe(PRODUCTION_ALLOWED_HOSTS_PROBE)

    assert data['hosts'] == ['api.example.com', 'localhost']


def test_production_config_still_rejects_empty_allowed_hosts():
    data = _probe(PRODUCTION_ALLOWED_HOSTS_EMPTY_PROBE)

    assert data['error'] == 'ValueError'
    assert 'ALLOWED_HOSTS' in data['message']


# ==========================================================================
# HOST / PORT：统一 Settings、环境优先级与异常校验
# ==========================================================================
def test_host_port_reads_tmp_env(tmp_path):
    path = _make_env_file(
        tmp_path,
        'server-bind',
        HOST='127.0.0.1',
        PORT='9001',
    )

    data = _probe(HOST_PORT_SETTINGS_PROBE % str(path))

    assert data == {'host': '127.0.0.1', 'port': 9001}


def test_host_port_process_env_overrides_tmp_env(tmp_path):
    path = _make_env_file(
        tmp_path,
        'server-bind-priority',
        HOST='file.example.com',
        PORT='9001',
    )

    data = _probe(
        HOST_PORT_SETTINGS_PROBE % str(path),
        {'HOST': 'env.example.com', 'PORT': '9002'},
    )

    assert data == {'host': 'env.example.com', 'port': 9002}


@pytest.mark.parametrize('bad_port', ['', 'abc', '0', '70000'])
def test_invalid_port_is_rejected(tmp_path, bad_port):
    path = _make_env_file(
        tmp_path,
        'server-bind-invalid',
        PORT=bad_port,
    )

    data = _probe(HOST_PORT_INVALID_PROBE % str(path))

    assert data['error'] == 'ValidationError'
    assert 'PORT' in data['message']


def test_development_config_uses_settings_host_port():
    data = _probe(
        DEV_SERVER_CONFIG_PROBE,
        {'ENVIRONMENT': 'development', 'HOST': '127.0.0.1', 'PORT': '9001'},
    )

    assert data == {'host': '127.0.0.1', 'port': 9001}


def test_production_config_uses_settings_host_port():
    data = _probe(
        PRODUCTION_SERVER_CONFIG_PROBE,
        {'ENVIRONMENT': 'production', 'HOST': '127.0.0.1', 'PORT': '9002'},
    )

    assert data == {'host': '127.0.0.1', 'port': 9002}


def test_main_uses_resolved_host_port_config():
    data = _probe(
        MAIN_SERVER_CONFIG_PROBE,
        {'ENVIRONMENT': 'development', 'HOST': '127.0.0.1', 'PORT': '9003'},
    )

    assert data == {'host': '127.0.0.1', 'port': 9003}


# ==========================================================================
# 探针：上传限制（真实调用 save_uploaded_file，只写 tmp 目录）
# ==========================================================================
def _upload_probe(filename, content_type, size, upload_dir) -> str:
    return (
        "import asyncio, io, json, os\n"
        "from fastapi import HTTPException\n"
        "from backend.services import document_service\n"
        "\n"
        "class _U:\n"
        "    def __init__(self, filename, content_type, data):\n"
        "        self.filename = filename\n"
        "        self.content_type = content_type\n"
        "        self._buf = io.BytesIO(data)\n"
        "\n"
        "    async def read(self, size=-1):\n"
        "        return self._buf.read(size)\n"
        "\n"
        "async def main():\n"
        "    result = {'status': 'accepted'}\n"
        "    try:\n"
        "        saved = await document_service.save_uploaded_file(\n"
        "            _U(%r, %r, b'x' * %d))\n"
        "    except HTTPException as exc:\n"
        "        result = {'status': 'rejected', 'code': exc.status_code,"
        " 'detail': str(exc.detail)}\n"
        "    else:\n"
        "        result.update({'size': os.path.getsize(saved),"
        " 'exists': os.path.exists(saved)})\n"
        "    upload_dir = %r\n"
        "    result['remaining'] = (sorted(os.listdir(upload_dir))\n"
        "                           if os.path.isdir(upload_dir) else [])\n"
        "    print('PROBE_JSON:' + json.dumps(result))\n"
        "\n"
        "asyncio.run(main())\n"
        % (filename, content_type, size, str(upload_dir))
    )


# ==========================================================================
# 探针：LLM transport（fake AsyncOpenAI，捕获真实构造参数）
# ==========================================================================
LLM_TRANSPORT_PROBE = """
import asyncio, json
from types import SimpleNamespace

import openai

class _Completions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='ok'))])

class _FakeAsyncOpenAI:
    last = {}

    def __init__(self, **kwargs):
        _FakeAsyncOpenAI.last = dict(kwargs)
        self.completions = _Completions()
        self.chat = SimpleNamespace(completions=self.completions)

openai.AsyncOpenAI = _FakeAsyncOpenAI

from backend.config.settings import settings
from backend.services.llm_service import LLMConfig, OpenAILikeLLM

llm = OpenAILikeLLM(LLMConfig(provider='openai', api_key='k',
                              base_url='http://fake.local/v1', model='m'))

async def _collect():
    return [chunk async for chunk in llm.chat_completion(
        [{'role': 'user', 'content': 'hi'}])]

chunks = asyncio.run(_collect())
print('PROBE_JSON:' + json.dumps({
    'settings_timeout': settings.LLM_TIMEOUT,
    'request_timeout': llm.request_timeout,
    'client_timeout': _FakeAsyncOpenAI.last.get('timeout'),
    'call_has_timeout': 'timeout' in llm.client.chat.completions.calls[0],
    'chunks': chunks,
}))
"""

LLMCONFIG_OVERRIDE_PROBE = """
import json
from backend.config.settings import settings
from backend.services.llm_service import LLMConfig, resolve_request_timeout
print('PROBE_JSON:' + json.dumps({
    'resolved': resolve_request_timeout(
        LLMConfig(provider='openai', api_key='k', timeout=99)),
    'settings_timeout': settings.LLM_TIMEOUT,
}))
"""


# ==========================================================================
# A. LLM_TIMEOUT
# ==========================================================================
def test_llm_timeout_default_is_30_seconds():
    data = _probe(DEFAULTS_PROBE)
    assert data['llm_default'] == 30
    assert data['llm_constraints'] is True      # ge/le 约束存在


def test_llm_timeout_from_env_file(tmp_path):
    env_file = _make_env_file(tmp_path, 'llm', LLM_TIMEOUT='7')
    data = _probe(_settings_probe(env_file))
    assert data['llm_timeout'] == 7


def test_llm_timeout_os_env_overrides_env_file(tmp_path):
    env_file = _make_env_file(tmp_path, 'llm', LLM_TIMEOUT='7')
    data = _probe(_settings_probe(env_file), {'LLM_TIMEOUT': '9'})
    assert data['llm_timeout'] == 9          # 进程环境变量 > .env


@pytest.mark.parametrize('bad', ['0', '-1', '601', 'abc', '30s', '1.5'])
def test_llm_timeout_invalid_values_fail_loudly(tmp_path, bad):
    env_file = _make_env_file(tmp_path, 'bad_llm', LLM_TIMEOUT=bad)
    data = _probe(_invalid_probe(env_file))
    assert data['error'] and data['error'].endswith('ValidationError'), data
    assert 'LLM_TIMEOUT' in data['message'], data


def test_llm_timeout_reaches_transport():
    """不只是"字段存在"：真实构造出的 client 收到该值，且请求不覆盖它。"""
    data = _probe(LLM_TRANSPORT_PROBE, {'LLM_TIMEOUT': '7'})
    assert data['settings_timeout'] == 7
    assert data['request_timeout'] == 7.0
    assert data['client_timeout'] == 7.0
    assert data['chunks'] == ['ok']
    assert data['call_has_timeout'] is False     # client 级生效


def test_llmconfig_timeout_no_longer_overrides_settings():
    data = _probe(LLMCONFIG_OVERRIDE_PROBE, {'LLM_TIMEOUT': '11'})
    assert data['settings_timeout'] == 11
    assert data['resolved'] == 11.0


# ==========================================================================
# B. MAX_UPLOAD_SIZE
# ==========================================================================
def test_upload_limit_default_is_10mb():
    data = _probe(DEFAULTS_PROBE)
    assert data['upload_default'] == 10 * 1024 * 1024
    assert data['upload_constraints'] is True


def test_upload_limit_from_env_file(tmp_path):
    env_file = _make_env_file(tmp_path, 'upload', MAX_UPLOAD_SIZE='2048')
    data = _probe(_settings_probe(env_file))
    assert data['max_upload'] == 2048


@pytest.mark.parametrize('bad', [
    '0', '-1', str(MAX_UPLOAD_BYTES + 1), '1073741825', '10MB', 'abc',
])
def test_upload_limit_invalid_values_fail_loudly(tmp_path, bad):
    env_file = _make_env_file(tmp_path, 'bad_upload', MAX_UPLOAD_SIZE=bad)
    data = _probe(_invalid_probe(env_file))
    assert data['error'] and data['error'].endswith('ValidationError'), data
    assert 'MAX_UPLOAD_SIZE' in data['message'], data


def test_upload_limit_accepts_max_boundary_99mib(tmp_path):
    """新的合法上限 = 99MiB（103809024），且严格低于 nginx 的 100M。"""
    env_file = _make_env_file(tmp_path, 'max_upload',
                              MAX_UPLOAD_SIZE=str(MAX_UPLOAD_BYTES))
    data = _probe(_settings_probe(env_file))
    assert data['max_upload'] == MAX_UPLOAD_BYTES
    assert MAX_UPLOAD_BYTES < 100 * 1024 * 1024


def test_upload_over_limit_is_rejected_and_cleaned(tmp_path):
    upload_dir = tmp_path / 'uploads'
    data = _probe(
        _upload_probe('note.txt', 'text/plain', 1025, upload_dir),
        {'MAX_UPLOAD_SIZE': '1024', 'UPLOAD_TEMP_DIR': str(upload_dir)},
    )
    assert data['status'] == 'rejected'
    assert data['code'] == 413
    assert '文件过大' in data['detail']
    assert data['remaining'] == []            # 半成品必须清理


def test_upload_within_limit_is_accepted(tmp_path):
    upload_dir = tmp_path / 'uploads'
    data = _probe(
        _upload_probe('note.txt', 'text/plain', 1024, upload_dir),
        {'MAX_UPLOAD_SIZE': '1024', 'UPLOAD_TEMP_DIR': str(upload_dir)},
    )
    assert data['status'] == 'accepted'
    assert data['exists'] is True
    assert data['size'] == 1024


def test_excel_upload_uses_same_limit(tmp_path):
    """Excel 与普通文档共用同一大小校验入口（只看字节数，不进解析器）。"""
    upload_dir = tmp_path / 'uploads'
    data = _probe(
        _upload_probe('book.xlsx', XLSX_MIME, 1025, upload_dir),
        {'MAX_UPLOAD_SIZE': '1024', 'UPLOAD_TEMP_DIR': str(upload_dir)},
    )
    assert data['status'] == 'rejected'
    assert data['code'] == 413
    assert '文件过大' in data['detail']


def test_configured_limit_above_legacy_10mb_is_honored(tmp_path):
    """配置放宽到 11MB 后，10MB+1 字节的文件不得再被旧硬编码常量拒绝。"""
    upload_dir = tmp_path / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)
    size = 10 * 1024 * 1024 + 1
    data = _probe(
        _upload_probe('big.txt', 'text/plain', size, upload_dir),
        {'MAX_UPLOAD_SIZE': str(11 * 1024 * 1024),
         'UPLOAD_TEMP_DIR': str(upload_dir)},
    )
    assert data['status'] == 'accepted'
    assert data['size'] == size


# ==========================================================================
# C. HOST / PORT 约定（开发 8021；容器/prod 保持 8000；前端 5181）
# ==========================================================================
def test_dev_default_port_is_8021():
    code = ("import backend.config as c\n"
            "print('ENV=' + c.env)\n"
            "print('HOST=%s' % c.Config.HOST)\n"
            "print('PORT=%s' % c.Config.PORT)\n")
    proc = _run(code)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert 'ENV=development' in proc.stdout
    assert 'HOST=0.0.0.0' in proc.stdout
    assert 'PORT=8021' in proc.stdout


def test_production_default_port_stays_8000():
    code = ("from backend.config.production import ProductionConfig as C\n"
            "print('HOST=%s' % C.HOST)\n"
            "print('PORT=%s' % C.PORT)\n")
    proc = _run(code, {'ENVIRONMENT': 'production'})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert 'HOST=0.0.0.0' in proc.stdout
    assert 'PORT=8000' in proc.stdout


def test_frontend_and_readme_use_same_dev_ports():
    vite = (REPO_ROOT / 'frontend' / 'vite.config.ts').read_text('utf-8')
    assert re.search(r'port:\s*5181', vite), vite[:200]
    assert 'http://127.0.0.1:8021' in vite

    # 该文件历史上以 GBK 保存；只断言其中的 ASCII 端口配置
    env_dev = (REPO_ROOT / 'frontend' / '.env.development').read_text(
        'utf-8', errors='ignore')
    assert 'VITE_API_BASE_URL=http://127.0.0.1:8021' in env_dev

    readme = (REPO_ROOT / 'README.md').read_text('utf-8')
    assert '8021' in readme and '5181' in readme


def test_start_scripts_use_dev_ports():
    # 仓库内跟踪的脚本：默认端口保持 8021 / 5181，且启动参数来自 Settings。
    text = (REPO_ROOT / 'start.sh').read_text('utf-8', errors='ignore')
    assert '8021' in text and '5181' in text
    assert 'settings.HOST' in text and 'settings.PORT' in text
    assert '--host "$BACKEND_HOST"' in text
    assert '--port "$BACKEND_PORT"' in text
    assert '--host 127.0.0.1 --port 8021' not in text

    bat = (REPO_ROOT / 'start_backend.bat').read_text('utf-8', errors='ignore')
    assert '8021' in bat
    assert '8000' not in bat
    assert 'settings.HOST' in bat and 'settings.PORT' in bat
    assert '--host "%BACKEND_HOST%"' in bat
    assert '--port "%BACKEND_PORT%"' in bat
    assert '--host 127.0.0.1 --port 8021' not in bat

    # 本地便利脚本（.gitignore: /start_*.ps1，干净检出中不存在）→ 存在时才校验
    for name in ('start_servers.ps1', 'start_copy.ps1'):
        path = REPO_ROOT / name
        if not path.exists():
            continue
        local = path.read_text('utf-8', errors='ignore')
        assert '8021' in local and '5181' in local, name
