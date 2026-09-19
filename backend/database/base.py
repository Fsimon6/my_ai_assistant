# -*- coding: utf-8 -*-
"""
数据库基础配置
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.ext.declarative import declarative_base
from typing import Generator
import os

from backend.config import Config

settings = Config()

# 数据库URL - Windows路径处理
if settings.DATABASE_URL.startswith('sqlite:///'):
    # 确保SQLite文件路径正确
    db_path = settings.DATABASE_URL.replace('sqlite:///', '')
    if not os.path.isabs(db_path):
        # 相对路径转为绝对路径
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        db_path = os.path.join(project_root, db_path)

    # 创建目录

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    # 更新数据库URL
    DATABASE_URL = f'sqlite:///{db_path}'
else:
    DATABASE_URL = settings.DATABASE_URL

print(f'数据库路径：{DATABASE_URL}')

# 创建SQLAlchemy引擎
# SQLite 并发加固：设置合理 busy timeout（避免 Windows 下并发写 database is locked），
# 并启用 WAL 日志模式（读写不互斥，显著降低锁冲突）。仅影响 SQLite，不改变 schema/查询语义。
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith('sqlite') else {}
if DATABASE_URL.startswith('sqlite'):
    connect_args["timeout"] = 30  # 等待锁释放最长 30s

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    echo=settings.DEBUG,    # 调试模式下显示SQL
    pool_pre_ping=True,     # 连接池预检查
)

if DATABASE_URL.startswith('sqlite'):
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_con, con_record):
        """每个新连接启用 WAL 与 NORMAL 同步，降低并发写锁概率。"""
        cur = dbapi_con.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
        finally:
            cur.close()

# 创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 声明基类
Base = declarative_base()

def get_db() -> Generator[Session, None, None]:
    """
    获取数据库会话依赖
    Usage：
        def some_endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """初始化数据库，创建所有表"""
    # 必须在 create_all 之前把所有模型导入到 Base.metadata，
    # 否则 metadata 为空、create_all 不会创建任何表（no such table）
    from backend import models  # noqa: F401  触发 backend.models.__init__ 中模型注册
    print(' 初始化数据库表...')

    Base.metadata.create_all(bind=engine)
    print(' 数据库表创建完成')
