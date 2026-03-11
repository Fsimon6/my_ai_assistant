# -*- coding: utf-8 -*-
"""
数据库初始化脚本
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.database.base import init_db, SessionLocal
from backend.models.user import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def init_admin_user():
    """初始化管理员用户"""
    db = SessionLocal()
    try:
        # 检查是否已有管理员
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            # 创建管理员用户
            admin = User(
                username="admin",
                email="admin@example.com",
                hashed_password=pwd_context.hash("admin123"),
                full_name="系统管理员",
                is_superuser=True,
            )
            db.add(admin)
            db.commit()
            print('?? 管理员用户创建成功')
            print('     用户名：admin')
            print('     密码：admin123')
            print('     邮箱：admin#example.com')
        else:
            print('?? 管理员用户已存在')

        # 创建测试用户
        test_user = db.query(User).filter(User.username == "test").first()
        if not test_user:
            test_user = User(
                username="test",
                email="test@example.com",
                hashed_password=pwd_context.hash("test123"),
                full_name="测试用户",
            )
            db.add(test_user)
            db.commit()
            print('?? 测试用户创建成功')
            print('     用户名：test')
            print('     密码：test123')
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

