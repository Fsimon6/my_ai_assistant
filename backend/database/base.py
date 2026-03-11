# -*- coding: utf-8 -*-
"""
数据库基础配置
"""
from sqlalchemy import create_engine
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
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith('sqlite') else {},
    echo=settings.DEBUG,    # 调试模式下显示SQL
    pool_pre_ping=True,     # 连接池预检查
)

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
    print(' 初始化数据库表...')

Base.metadata.create_all(bind=engine)
print(' 数据库表创建完成')
