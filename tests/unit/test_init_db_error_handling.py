# -*- coding: utf-8 -*-
"""数据库 CLI 初始化失败必须阻止后端启动的回归测试。"""
import pytest

from backend import init_db as init_module


class _ExistingQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return object()


class _ExistingSession:
    def __init__(self):
        self.closed = False

    def query(self, *args, **kwargs):
        return _ExistingQuery()

    def close(self):
        self.closed = True


class _BrokenSession:
    def __init__(self):
        self.rolled_back = False
        self.closed = False

    def query(self, *args, **kwargs):
        raise RuntimeError('synthetic database failure')

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_init_admin_user_accepts_existing_users(monkeypatch):
    session = _ExistingSession()
    monkeypatch.setattr(init_module, 'SessionLocal', lambda: session)
    monkeypatch.setattr(init_module, 'get_env', lambda: 'development')

    init_module.init_admin_user()

    assert session.closed is True


def test_init_admin_user_rolls_back_and_raises_on_failure(monkeypatch):
    session = _BrokenSession()
    monkeypatch.setattr(init_module, 'SessionLocal', lambda: session)

    with pytest.raises(RuntimeError, match='用户初始化失败'):
        init_module.init_admin_user()

    assert session.rolled_back is True
    assert session.closed is True


def test_main_propagates_create_all_failure(monkeypatch):
    calls = []

    def fail_create_all():
        calls.append('init_db')
        raise RuntimeError('synthetic create_all failure')

    monkeypatch.setattr(init_module, 'init_db', fail_create_all)
    monkeypatch.setattr(
        init_module,
        'init_admin_user',
        lambda: calls.append('init_admin_user'),
    )

    with pytest.raises(RuntimeError, match='create_all'):
        init_module.main()

    assert calls == ['init_db']


def test_main_propagates_user_initialization_failure(monkeypatch):
    calls = []

    monkeypatch.setattr(
        init_module,
        'init_db',
        lambda: calls.append('init_db'),
    )

    def fail_admin():
        calls.append('init_admin_user')
        raise RuntimeError('synthetic user failure')

    monkeypatch.setattr(init_module, 'init_admin_user', fail_admin)

    with pytest.raises(RuntimeError, match='user failure'):
        init_module.main()

    assert calls == ['init_db', 'init_admin_user']
