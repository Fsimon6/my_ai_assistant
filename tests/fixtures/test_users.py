"""
用户测试数据 Fixtures
"""
import pytest
from datetime import datetime, timedelta
import jwt
from backend.config.testing import TestingConfig


@pytest.fixture
def test_user():
    """测试用户数据"""
    return {
        "email": "test@example.com",
        "password": "TestPassword123!",
        "username": "testuser",
        "full_name": "Test User"
    }


@pytest.fixture
def test_user_2():
    """第二个测试用户"""
    return {
        "email": "test2@example.com",
        "password": "Password456!",
        "username": "testuser2",
        "full_name": "Test User 2"
    }


@pytest.fixture
def admin_user():
    """管理员用户"""
    return {
        "email": "admin@example.com",
        "password": "AdminPassword789!",
        "username": "admin",
        "full_name": "Administrator",
        "is_admin": True
    }


@pytest.fixture
def test_user_in_db(db_session, test_user):
    """已经在数据库中的测试用户"""
    from backend.models.user import User
    from backend.utils.security import get_password_hash

    user = User(
        email=test_user["email"],
        username=test_user["username"],
        full_name=test_user["full_name"],
        hashed_password=get_password_hash(test_user["password"]),
        is_active=True,
        created_at=datetime.utcnow()
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def test_users_in_db(db_session, test_user, test_user_2):
    """多个测试用户"""
    from backend.models.user import User
    from backend.utils.security import get_password_hash

    users = []
    for user_data in [test_user, test_user_2]:
        user = User(
            email=user_data["email"],
            username=test_user["username"],
            full_name=test_user["full_name"],
            hashed_password=get_password_hash(test_user["password"]),
            is_active=True,
            created_at=datetime.utcnow()
        )
        db_session.add(user)
        users.append(user)

    db_session.commit()
    for user in users:
        db_session.refresh(user)

    return users


@pytest.fixture
def auth_headers(test_user_in_db):
    """认证头（包含JWT令牌）"""
    from backend.utils.auth import create_access_token

    access_token = create_access_token(
        data={"sub": test_user_in_db.email},
        expires_delta=timedelta(minutes=30)
    )

    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def expired_token():
    """过期的JWT令牌"""
    payload = {
        "sub": "test@example.com",
        "exp": datetime.utcnow() - timedelta(hours=1)
    }
    token = jwt.encode(
        payload,
        TestingConfig.SECRET_KEY,
        algorithm="HS256"
    )
    return token


@pytest.fixture
def invalid_token():
    """无效的JWT令牌"""
    return "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0QGV4YW1wbGUuY29tIn0.invalid"


@pytest.fixture
def user_login_data(test_user):
    """用户登录数据"""
    return {
        "email": test_user["email"],
        "password": test_user["password"],
    }


@pytest.fixture
def user_update_data():
    """用户更新数据"""
    return {
        "full_name": "Updated Name",
        "bio": "This is my bio",
        "avatar": "https://example.com/avatar.jpg",
    }


@pytest.fixture
def password_change_data():
    """密码修改数据"""
    return {
        "old_password": "TestPassword123!",
        "new_password": "NewPassword456!",
        "confirm_password": "NewPassword456!"
    }
