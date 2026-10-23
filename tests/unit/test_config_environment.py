# -*- coding: utf-8 -*-
"""B1：ENVIRONMENT 单一来源（Settings）与配置类选择一致性回归。

隔离约定（保证在**没有项目 `.env`** 的干净检出/CI 环境同样可运行，并且
父 pytest 进程不缓存测试配置单例）：
* 测试文件在导入阶段不 import 任何 `backend.*` 模块；
* 所有需要 `backend.*` 的验证（含 `normalize_env` 纯函数）全部在**子进程**中执行，
  子进程使用固定的非真实测试密钥 `unit-test-only-secret`，且**清除**从当前进程
  继承的 `JWT_SECRET / ENVIRONMENT / DATABASE_URL / LLM_MODEL`；
* 不读取、不修改项目真实 `.env`（仅在其存在时做一次前后哈希对比）；
* 不启动服务、不访问网络；`init_db` 用例只使用 `tmp_path` 下的临时 SQLite。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 固定的非真实测试密钥（仅用于子进程构造 Settings / 启动 ProductionConfig）
TEST_JWT_SECRET = 'unit-test-only-secret'
#: testing 配置预校验需要完整的隔离路径；这些路径不需要提前创建。
TESTING_ROOT = Path(tempfile.gettempdir()) / 'b1_testing_paths'
TESTING_PATHS = {
    'TEST_ISOLATION_ROOT': str(TESTING_ROOT),
    'TEST_DATABASE_URL': 'sqlite:///' + (TESTING_ROOT / 'test.db').as_posix(),
    'TEST_VECTOR_DB_PATH': str(TESTING_ROOT / 'chroma'),
    'TEST_UPLOAD_FOLDER': str(TESTING_ROOT / 'uploads'),
    'TEST_LOG_FILE': str(TESTING_ROOT / 'logs' / 'app.log'),
    'TEST_EXCEL_DATA_ROOT': str(TESTING_ROOT / 'excel'),
}

EXPECTED_CONFIG_CLASS = {
    'development': 'DevelopmentConfig',
    'production': 'ProductionConfig',
    'testing': 'TestingConfig',
}

# 子进程探针：生效环境 / 配置类名 / 导出的 env，三者必须同源
PROBE = (
    "import backend.config as c\n"
    "print('ENV=' + c.get_env())\n"
    "print('CLS=' + c.Config.__name__)\n"
    "print('EXPORTED=' + c.env)\n"
)

LOG_PROBE = (
    "import logging\n"
    "logging.basicConfig(level=logging.INFO, format='%(message)s')\n"
    "import backend.main as m\n"
    "m.log_startup_configuration()\n"
)

SETTINGS_DEFAULT_PROBE = (
    "import json\n"
    "from backend.config.settings import Settings\n"
    "print('PROBE_JSON:' + json.dumps({"
    "'default': Settings.model_fields['ENVIRONMENT'].default}))\n"
)

# 临时 .env：用程序内唯一的 LLM_MODEL 标记证明“文件内容确实被读取”
TMP_ENV_PROBE = (
    "from backend.config.settings import Settings\n"
    "import backend.config as c\n"
    "s = Settings(_env_file={path!r})\n"
    "print('MODEL=' + s.LLM_MODEL)\n"
    "print('RAW=' + s.ENVIRONMENT)\n"
    "print('NORMALIZED=' + c.normalize_env(s.ENVIRONMENT))\n"
)

TMP_ENV_PRECEDENCE_PROBE = (
    "from backend.config.settings import Settings\n"
    "s = Settings(_env_file={path!r})\n"
    "print('RAW=' + s.ENVIRONMENT)\n"
    "print('MODEL=' + s.LLM_MODEL)\n"
)

# init_db：在临时 SQLite 上执行真实的建表 + 建号流程，输出生效环境/配置类/用户集合
INIT_DB_PROBE = (
    "import backend.config as c\n"
    "print('EFFECTIVE=' + c.env)\n"
    "print('CLASS=' + c.Config.__name__)\n"
    "from backend.database.base import init_db, SessionLocal\n"
    "init_db()\n"
    "from backend.init_db import init_admin_user\n"
    "init_admin_user()\n"
    "from backend.models.user import User\n"
    "db = SessionLocal()\n"
    "print('USERS=' + ','.join(sorted(u.username for u in db.query(User).all())))\n"
    "db.close()\n"
)

# normalize_env：一次子进程算完所有取值（纯函数，无副作用）
NORMALIZE_INPUTS = [
    'development', 'production', 'testing', 'PRODUCTION', '  Testing  ',
    ' Production ', '', None, '   ', 'staging', 'prod', 'dev', 'productionn',
]


def _normalize_values_probe(inputs) -> str:
    return (
        "import json\n"
        "from backend.config import DEFAULT_ENV, ENV_DEVELOPMENT, normalize_env\n"
        "inputs = %r\n"
        "pairs = [{'raw': r, 'value': normalize_env(r)} for r in inputs]\n"
        "print('PROBE_JSON:' + json.dumps({'pairs': pairs,"
        " 'default_env': DEFAULT_ENV, 'dev': ENV_DEVELOPMENT}))\n" % (inputs,)
    )


# 警告：只输入 staging / 空 / None，便于精确断言
NORMALIZE_WARNING_PROBE = (
    "import json, logging\n"
    "from backend.config import normalize_env\n"
    "class _H(logging.Handler):\n"
    "    def __init__(self):\n"
    "        super().__init__(); self.msgs = []\n"
    "    def emit(self, record):\n"
    "        self.msgs.append(record.getMessage())\n"
    "h = _H(); logging.getLogger('backend.config').addHandler(h)\n"
    "values = [normalize_env('staging'), normalize_env(''), normalize_env(None)]\n"
    "print('PROBE_JSON:' + json.dumps({'values': values, 'warnings': h.msgs}))\n"
)

EXPORT_PROBE = (
    "import json\n"
    "import backend.config as c\n"
    "print('PROBE_JSON:' + json.dumps({"
    "'env': c.env, 'cls': c.Config.__name__, 'get_env': c.get_env()}))\n"
)

SNAPSHOT_PROBE = (
    "import json, os\n"
    "import backend.config as c\n"
    "first = c.env\n"
    "os.environ['ENVIRONMENT'] = ('testing' if first != 'testing' else 'production')\n"
    "print('PROBE_JSON:' + json.dumps({"
    "'snapshot': first, 'after_env_change': c.env, 'get_env': c.get_env()}))\n"
)


def _run(code: str, env_overrides: Optional[dict] = None):
    """在受控子进程中执行探针。

    - 清除可能从开发者 shell / pytest 进程继承的真实配置；
    - 注入固定的测试 JWT 密钥（不输出）；
    - `ENVIRONMENT` 只由 `env_overrides` 指定，未指定时等同于“未设置”。
    """
    env = dict(os.environ)
    for key in (
        'ENVIRONMENT', 'JWT_SECRET', 'DATABASE_URL', 'LLM_MODEL',
        'VECTOR_DB_PATH', 'UPLOAD_TEMP_DIR', 'LOG_FILE', 'EXCEL_DATA_ROOT',
        'LLM_PROVIDER', 'API_KEY', 'TEST_ISOLATION_ROOT',
        'TEST_DATABASE_URL', 'TEST_VECTOR_DB_PATH', 'TEST_UPLOAD_FOLDER',
        'TEST_LOG_FILE', 'TEST_EXCEL_DATA_ROOT', 'TEST_LLM_PROVIDER',
        'TEST_LLM_MODEL', 'TEST_LLM_BASE_URL',
    ):
        env.pop(key, None)
    env['JWT_SECRET'] = TEST_JWT_SECRET
    env.update(env_overrides or {})
    # testing 现在会在 backend.config 初始化阶段 fail-fast；配置类选择
    # 用例也必须提供完整合法隔离路径，否则导入即被拒绝。
    if str(env.get('ENVIRONMENT', '')).strip().lower() == 'testing':
        for key, value in TESTING_PATHS.items():
            env.setdefault(key, value)
    env['PYTHONPATH'] = str(REPO_ROOT)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return subprocess.run(
        [sys.executable, '-c', code], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
    )


def _probe(code: str, env_overrides: Optional[dict] = None) -> dict:
    proc = _run(code, env_overrides)
    assert proc.returncode == 0, proc.stderr[-3000:]
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith('PROBE_JSON:'):
            return json.loads(line[len('PROBE_JSON:'):])
    raise AssertionError(
        'probe 未输出 PROBE_JSON；stdout=%r stderr=%r'
        % (proc.stdout[-800:], proc.stderr[-800:])
    )


def _kv(stdout: str) -> dict:
    """解析 `KEY=VALUE` 行；忽略其它日志行（不用于打印可能含口令的输出）。"""
    out = {}
    for line in stdout.splitlines():
        if '=' in line and not line.startswith((' ', '\t')):
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope='module')
def normalize_values():
    return _probe(_normalize_values_probe(NORMALIZE_INPUTS))


@pytest.fixture(scope='module')
def normalize_warnings():
    return _probe(NORMALIZE_WARNING_PROBE)


def _normalized(data: dict, raw) -> str:
    for pair in data['pairs']:
        if pair['raw'] == raw:
            return pair['value']
    raise AssertionError('未找到输入 %r 的结果' % (raw,))


# ==========================================================================
# 1) 规约函数：支持的取值 / 大小写 / 空白
# ==========================================================================
@pytest.mark.parametrize('raw,expected', [
    ('development', 'development'),
    ('production', 'production'),
    ('testing', 'testing'),
    ('PRODUCTION', 'production'),
    ('  Testing  ', 'testing'),
    (' Production ', 'production'),
])
def test_normalize_env_accepts_supported_values(normalize_values, raw, expected):
    assert _normalized(normalize_values, raw) == expected


# ==========================================================================
# 2) 空值 / 非法值沿用既有约定（回退 development）
# ==========================================================================
@pytest.mark.parametrize('raw', ['', None, '   ', 'staging', 'prod', 'dev', 'productionn'])
def test_normalize_env_falls_back_for_empty_or_invalid(normalize_values, raw):
    assert _normalized(normalize_values, raw) == 'development'


def test_normalize_env_warns_only_for_non_empty_invalid(normalize_warnings):
    """只统计“本次非法值”产生的 ENVIRONMENT 警告，不受初始化兜底警告影响。"""
    assert normalize_warnings['values'] == ['development', 'development', 'development']

    messages = normalize_warnings['warnings']
    env_warnings = [m for m in messages if m.startswith('ENVIRONMENT=')]

    assert len(env_warnings) == 1, env_warnings          # 只有 staging 触发
    assert 'staging' in env_warnings[0]
    # 空值 / 缺省值不产生“非法值”告警
    assert not any("ENVIRONMENT=''" in m or 'ENVIRONMENT=None' in m for m in messages)


# ==========================================================================
# 3) 缺省值仍符合原有约定；模块内三处口径一致
# ==========================================================================
def test_default_environment_unchanged(normalize_values):
    assert normalize_values['dev'] == 'development'
    assert normalize_values['default_env'] == 'development'
    assert _normalized(normalize_values, None) == 'development'

    data = _probe(SETTINGS_DEFAULT_PROBE)
    assert data['default'] == 'development'


def test_module_exports_are_consistent():
    data = _probe(EXPORT_PROBE)
    assert data['env'] in EXPECTED_CONFIG_CLASS
    assert data['cls'] == EXPECTED_CONFIG_CLASS[data['env']]
    assert data['get_env'] == data['env']


# ==========================================================================
# 4) 进程环境变量：选择正确的配置类，且三处口径一致
# ==========================================================================
@pytest.mark.parametrize('value', ['development', 'production', 'testing'])
def test_env_var_selects_matching_config_class(value):
    proc = _run(PROBE, {'ENVIRONMENT': value})
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = _kv(proc.stdout)

    assert data['ENV'] == value, data
    assert data['EXPORTED'] == value, data
    assert data['CLS'] == EXPECTED_CONFIG_CLASS[value], data


def test_env_value_is_case_insensitive_end_to_end():
    proc = _run(PROBE, {'ENVIRONMENT': '  PRODUCTION  '})
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = _kv(proc.stdout)
    assert data['ENV'] == 'production'
    assert data['CLS'] == 'ProductionConfig'


# ==========================================================================
# 5) 非法值：回退默认环境，且"生效环境 / 配置类 / 启动日志"三者不打架
# ==========================================================================
def test_invalid_env_value_falls_back_consistently():
    proc = _run(PROBE, {'ENVIRONMENT': 'staging'})
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = _kv(proc.stdout)

    assert data['ENV'] == 'development'
    assert data['EXPORTED'] == 'development'
    assert data['CLS'] == 'DevelopmentConfig'
    assert '不受支持' in proc.stderr, proc.stderr[-1000:]


# ==========================================================================
# 6) `.env` 的影响（不依赖真实 .env：临时文件 + 子进程）
# ==========================================================================
def test_env_file_value_drives_resolution(tmp_path):
    real_env = REPO_ROOT / '.env'
    before = hashlib.sha256(real_env.read_bytes()).hexdigest() if real_env.exists() else None

    tmp_env = tmp_path / '.env'
    tmp_env.write_text(
        'JWT_SECRET=%s\nENVIRONMENT=production\nLLM_MODEL=b1-tmp-env-model\n' % TEST_JWT_SECRET,
        encoding='utf-8',
    )

    proc = _run(TMP_ENV_PROBE.format(path=str(tmp_env)))
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = _kv(proc.stdout)

    assert data['MODEL'] == 'b1-tmp-env-model'          # 证明临时 .env 被真正读取
    assert data['RAW'] == 'production'
    assert data['NORMALIZED'] == 'production'
    assert EXPECTED_CONFIG_CLASS[data['NORMALIZED']] == 'ProductionConfig'

    if before is not None:                              # 仅当真实 .env 存在时附加检查
        assert hashlib.sha256(real_env.read_bytes()).hexdigest() == before


def test_os_env_overrides_env_file(tmp_path):
    tmp_env = tmp_path / '.env'
    tmp_env.write_text(
        'JWT_SECRET=%s\nENVIRONMENT=production\nLLM_MODEL=b1-tmp-env-model\n' % TEST_JWT_SECRET,
        encoding='utf-8',
    )

    proc = _run(TMP_ENV_PRECEDENCE_PROBE.format(path=str(tmp_env)), {'ENVIRONMENT': 'testing'})
    assert proc.returncode == 0, proc.stderr[-2000:]
    data = _kv(proc.stdout)

    assert data['RAW'] == 'testing'                      # 进程环境变量优先
    assert data['MODEL'] == 'b1-tmp-env-model'           # .env 其余项仍被读取


def test_runtime_env_change_does_not_hot_reload():
    """已实例化的 Settings 是启动快照：运行期改环境变量不会改变已选定的环境。"""
    data = _probe(SNAPSHOT_PROBE)
    assert data['after_env_change'] == data['snapshot']
    assert data['get_env'] == data['snapshot']


# ==========================================================================
# 7) 启动日志与实际配置类一致（子进程，避免污染本进程日志/环境）
# ==========================================================================
def test_startup_log_matches_actual_config_class():
    proc = _run(LOG_PROBE, {'ENVIRONMENT': 'production'})
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = proc.stdout + proc.stderr
    assert 'environment        = production (config class = ProductionConfig)' in out, out[-2000:]


def test_startup_log_shows_fallback_for_invalid_value():
    proc = _run(LOG_PROBE, {'ENVIRONMENT': 'staging'})
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = proc.stdout + proc.stderr
    assert 'environment        = development (config class = DevelopmentConfig)' in out, out[-2000:]
    assert 'environment        = staging' not in out, out[-2000:]


# ==========================================================================
# 8) init_db 与配置类使用同一套有效环境值（临时 SQLite，隔离真实数据）
# ==========================================================================
def _run_init_db(tmp_path, environment: str):
    db_file = tmp_path / 'initdb_probe.db'
    return _run(INIT_DB_PROBE, {
        'ENVIRONMENT': environment,
        'DATABASE_URL': 'sqlite:///' + db_file.as_posix(),
    })


def test_init_db_treats_padded_production_as_production(tmp_path):
    """`ENVIRONMENT=" production "`：实际配置类是 Production，且不得创建 test 账号。"""
    proc = _run_init_db(tmp_path, ' production ')
    assert proc.returncode == 0, proc.stderr[-2500:]
    data = _kv(proc.stdout)

    assert data.get('EFFECTIVE') == 'production', data
    assert data.get('CLASS') == 'ProductionConfig', data

    users = {u for u in (data.get('USERS') or '').split(',') if u}
    assert 'admin' in users
    assert 'test' not in users, '生产环境不得创建默认 test 账号'


def test_init_db_creates_test_user_outside_production(tmp_path):
    """对照组：非生产环境仍应创建 test 账号（证明上面的断言不是空转）。"""
    proc = _run_init_db(tmp_path, 'development')
    assert proc.returncode == 0, proc.stderr[-2500:]
    data = _kv(proc.stdout)

    assert data.get('EFFECTIVE') == 'development', data
    assert data.get('CLASS') == 'DevelopmentConfig', data

    users = {u for u in (data.get('USERS') or '').split(',') if u}
    assert {'admin', 'test'} <= users, users
