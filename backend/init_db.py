# -*- coding: utf-8 -*-
"""
数据库初始化脚本
"""
import sys
import os
import secrets
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database.base import init_db, SessionLocal
from backend.models.user import User
from backend.utils.security import get_password_hash


def _initial_password(env_name: str) -> tuple:
    """取得初始口令：优先读环境变量，未配置则**随机生成**（避免硬编码弱口令入库）。

    返回 ``(password, from_env)``；随机生成的口令只打印一次，请立即记录并修改。
    """
    password = os.getenv(env_name)
    if password:
        return password, True
    return secrets.token_urlsafe(12), False


def init_admin_user():
    """初始化管理员用户"""
    is_production = os.getenv('ENVIRONMENT', 'development').lower() == 'production'
    db = SessionLocal()
    try:
        # 检查是否已有管理员
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            # 创建管理员用户（口令来自 INIT_ADMIN_PASSWORD，未设置则随机生成）
            admin_password, admin_from_env = _initial_password('INIT_ADMIN_PASSWORD')
            admin_email = os.getenv('INIT_ADMIN_EMAIL', 'admin@example.com')
            admin = User(
                username="admin",
                email=admin_email,
                hashed_password=get_password_hash(admin_password),
                full_name="系统管理员",
                is_superuser=True,
            )
            db.add(admin)
            db.commit()
            print('?? 管理员用户创建成功')
            print('     用户名：admin')
            print('     邮箱：%s' % admin_email)
            if admin_from_env:
                print('     密码：来自环境变量 INIT_ADMIN_PASSWORD')
            else:
                print('     密码（随机生成，仅显示这一次，请立即修改）：%s' % admin_password)
        else:
            print('?? 管理员用户已存在')

        # 创建测试用户：仅限非生产环境（生产环境不允许存在默认口令账号）
        if is_production:
            print('?? 生产环境跳过测试用户创建')
        else:
            test_user = db.query(User).filter(User.username == "test").first()
            if not test_user:
                test_password, test_from_env = _initial_password('INIT_TEST_PASSWORD')
                test_user = User(
                    username="test",
                    email="test@example.com",
                    hashed_password=get_password_hash(test_password),
                    full_name="测试用户",
                )
                db.add(test_user)
                db.commit()
                print('?? 测试用户创建成功')
                print('     用户名：test')
                if test_from_env:
                    print('     密码：来自环境变量 INIT_TEST_PASSWORD')
                else:
                    print('     密码（随机生成，仅显示这一次）：%s' % test_password)
    except Exception as e:
        print(f'?? 初始化用户失败：{e}')
        db.rollback()
    finally:
        db.close()

if __name__ == '__main__':
    print(' 开始初始化数据库')
    init_db()   # 创建表
    init_admin_user()   # 初始化用户
    print(' 数据库初始化完成！')

