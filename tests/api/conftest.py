# -*- coding: utf-8 -*-
"""Stage 5 — 后端 API contract / integration regression 的**隔离底座**。

隔离原则（绝不污染开发环境）：
  * **临时 SQLite**：API 依赖（``get_db``）与 conversation / character service 共用同一引擎
    （后者在模块级 `from backend.database.base import SessionLocal` 绑定，必须逐个 patch）；
  * **临时 Chroma 目录**：每个用例一个，用完即弃（并重置 ``_vector_store_manager`` 单例）；
  * **临时 Excel data root**：representation / 原文件都落在 ``tmp_path``；
  * **不进入 app lifespan**：lifespan 会 ``init_db()`` 并创建 ``./data/chroma_db``、
    ``./data/uploads``（真实开发目录），故这里既 patch ``init_db``，也不以
    ``with TestClient(...)`` 方式启动应用；
  * **假 provider**：只伪造 provider 的返回值（chat JSON + embedding 向量），
    router → service → authority → 执行 → 历史落库 / DB / Chroma **全部真实**。

本文件只做 API 层回归（HTTP 完整链路），不重复测 NL parser 内部细节（那在 tests/unit）。
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.main import app
from backend.database.base import Base, get_db
import backend.database.base as db_base
import backend.services.character_service as char_mod
import backend.services.conversation_service as conv_mod
import backend.services.llm_service as llm_mod
import backend.services.rag_service as rag_mod
import backend.services.vector_service as vec_mod
from backend.excel import store as excel_store
from backend.services.llm_service import LLMConfig, OpenAILikeLLM

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
GOLDEN_SMALL = FIXTURES / 'excel' / 'golden_small.xlsx'
GOLDEN_MEDIUM = FIXTURES / 'excel' / 'golden_medium.xlsx'
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

#: 假向量维度（只需维度一致；不关心语义）
VECTOR_DIM = 8


class RecordingEmbeddingsAPI:
    """伪"远端 embedding 端点"：只记录每次请求的 input（并可注入失败）。"""

    def __init__(self, owner: 'FakeProviderLLM') -> None:
        self._owner = owner

    async def create(self, model=None, input=None):
        batch = list(input or [])
        self._owner.embedding_requests.append(batch)              # 记录**尝试**
        if self._owner.embedding_error is not None:
            raise self._owner.embedding_error
        if self._owner.fail_on_embedding_call == len(self._owner.embedding_requests):
            raise RuntimeError('remote embedding exploded at call %d'
                               % self._owner.fail_on_embedding_call)
        return SimpleNamespace(data=[SimpleNamespace(embedding=_fake_vector(t))
                                     for t in batch])


class FakeProviderLLM(OpenAILikeLLM):
    """真实 ``OpenAILikeLLM`` 的**子类**：只覆盖 chat（模型返回值），embedding 走真实实现。

    这样 API 层验证的 embedding 链路与生产**完全一致**（含 ``generate_embeddings`` 的
    ≤20 分片），只把"远端客户端"换成 ``RecordingEmbeddingsAPI``（不外发任何请求）。
    """

    def __init__(self) -> None:
        super().__init__(LLMConfig(
            provider='openai', api_key='test-key-not-used',
            base_url='http://127.0.0.1:1/v1', model='test-model',
            embedding_model='test-embedding-model'))
        self.embedding_requests: List[List[str]] = []
        self.turn_payload: Optional[Dict[str, Any]] = None
        self.aggregate_payload: Optional[Dict[str, Any]] = None
        self.analysis_payload: Optional[Dict[str, Any]] = None
        self.chat_kinds: List[str] = []
        self.embedding_error: Optional[Exception] = None
        #: 让**第 N 次**远端 embedding 调用失败（用于验证「后续批失败 -> 不部分成功」）
        self.fail_on_embedding_call: Optional[int] = None
        self.client = SimpleNamespace(embeddings=RecordingEmbeddingsAPI(self))

    # ---- chat（被 llm_parse_turn / llm_parse_aggregate / llm_parse_analysis 调用）----
    async def chat_completion(self, messages, stream=False, temperature=None):
        system = messages[0]['content']
        if system.startswith('你是一个「统计参数抽取器」'):
            kind, payload = 'aggregate', self.aggregate_payload
        elif system.startswith('你是一个「两阶段分析计划生成器」'):
            kind, payload = 'analysis', self.analysis_payload
        else:
            kind, payload = 'turn', self.turn_payload
        self.chat_kinds.append(kind)
        if payload is None:
            raise RuntimeError('FakeProviderLLM 未提供 %s 响应' % kind)
        yield json.dumps(payload, ensure_ascii=False)


def _fake_vector(text: str) -> List[float]:
    digest = hashlib.sha256(str(text).encode('utf-8')).digest()
    return [(digest[i] + 1) / 256.0 for i in range(VECTOR_DIM)]


def unwrap(payload: Any) -> Any:
    """统一响应包装：``api_response.success`` 返回 ``{'code','data',...}``。"""
    if isinstance(payload, dict) and 'data' in payload and 'code' in payload:
        return payload['data']
    return payload


class ApiEnv:
    """一次用例的隔离环境（客户端 + 假 provider + 临时目录）。"""

    def __init__(self, client: TestClient, fake: FakeProviderLLM, tmp_path: Path):
        self.client = client
        self.fake = fake
        self.tmp_path = tmp_path
        self._seq = 0

    # ------------------------------------------------------------------ auth
    def register(self, username: str) -> Dict[str, Any]:
        resp = self.client.post('/api/v1/auth/register', json={
            'username': username, 'email': f'{username}@example.com',
            'password': 'test-password-123', 'full_name': username,
        })
        assert resp.status_code == 200, resp.text
        return unwrap(resp.json())

    def login(self, username: str) -> Dict[str, str]:
        resp = self.client.post('/api/v1/auth/login', json={
            'username': username, 'password': 'test-password-123'})
        assert resp.status_code == 200, resp.text
        return {'Authorization': 'Bearer %s' % unwrap(resp.json())['access_token']}

    def user(self, username: str) -> Dict[str, str]:
        self.register(username)
        return self.login(username)

    def user_with_id(self, username: str):
        """返回 ``(headers, user_id)``（user_id 用于直接核对 service 层历史隔离）。"""
        data = self.register(username)
        return self.login(username), int(data['id'])

    # ---------------------------------------------------------------- upload
    def upload_file(self, headers, path: Path, filename: Optional[str] = None):
        with open(path, 'rb') as fh:
            return self.client.post(
                '/api/v1/rag/upload', headers=headers,
                files={'file': (filename or path.name, fh, XLSX_MIME)})

    def upload_bytes(self, headers, filename: str, content: bytes,
                     content_type: str = 'text/plain'):
        return self.client.post(
            '/api/v1/rag/upload', headers=headers,
            files={'file': (filename, content, content_type)})

    def upload_excel(self, headers, path: Path, filename=None) -> Dict[str, Any]:
        resp = self.upload_file(headers, path, filename)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['success'] is True, body
        return body

    # ------------------------------------------------------------- character
    def create_character(self, headers, name: Optional[str] = None) -> str:
        self._seq += 1
        resp = self.client.post('/api/v1/characters/', headers=headers, json={
            'name': name or 'API回归角色%d' % self._seq,
            'system_prompt': '只用于 API regression（不调用真实模型）。',
        })
        assert resp.status_code == 200, resp.text
        return str(unwrap(resp.json())['id'])

    # ------------------------------------------------------------ nl-query
    def nl_query(self, headers, message: str, *, character_id=None, session_id=None):
        body: Dict[str, Any] = {'message': message}
        if character_id is not None:
            body['character_id'] = character_id
        if session_id is not None:
            body['session_id'] = session_id
        return self.client.post('/api/v1/rag/excel/nl-query', headers=headers, json=body)

    def nl_ok(self, headers, message: str, **kw) -> Dict[str, Any]:
        resp = self.nl_query(headers, message, **kw)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body['success'] is True, body
        assert body['status'] == 'ok', body.get('message')
        return body


@pytest.fixture()
def api(tmp_path, monkeypatch) -> ApiEnv:
    """隔离的 API 环境（见模块 docstring 的隔离原则）。"""
    # 1) 临时 SQLite：API 依赖 + service 层共用同一引擎
    engine = create_engine('sqlite:///%s' % (tmp_path / 'api_regression.db'),
                           connect_args={'check_same_thread': False})
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    from backend import models  # noqa: F401  触发模型注册（否则 create_all 不建表）
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(db_base, 'SessionLocal', SessionLocal)
    monkeypatch.setattr(conv_mod, 'SessionLocal', SessionLocal)
    monkeypatch.setattr(char_mod, 'SessionLocal', SessionLocal)

    def _override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setitem(app.dependency_overrides, get_db, _override_get_db)
    # lifespan 会调用 init_db()（真实引擎）-> 本轮不启动 lifespan，同时兜底 patch
    monkeypatch.setattr(db_base, 'init_db', lambda: None)

    # 2) 临时 Excel data root
    monkeypatch.setattr(excel_store, 'EXCEL_DATA_ROOT', tmp_path / 'excel_data')

    # 3) 假 provider + 临时 Chroma（重置单例，避免复用开发环境的库/向量）
    fake = FakeProviderLLM()
    monkeypatch.setattr(llm_mod, 'get_llm', lambda: fake)
    monkeypatch.setattr(vec_mod, 'get_llm', lambda: fake)
    monkeypatch.setattr(rag_mod, 'get_llm', lambda: fake)
    manager = vec_mod.VectorStoreManager(persist_directory=str(tmp_path / 'chroma'))
    monkeypatch.setattr(vec_mod, '_vector_store_manager', manager)
    monkeypatch.setattr(rag_mod, '_rag_service', None)

    # 注意：**不用** `with TestClient(app)`（避免触发 lifespan 触碰真实目录/DB）
    return ApiEnv(TestClient(app), fake, tmp_path)
