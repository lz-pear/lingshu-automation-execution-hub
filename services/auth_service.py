"""密码、会话令牌与固定管理员初始化。"""
import base64
import hashlib
import hmac
import secrets
from datetime import datetime

from sqlalchemy import delete, func, select, update

from config import ADMIN_DISPLAY_NAME, ADMIN_PASSWORD, ADMIN_USERNAME
from database import async_session
from models import AuthSession, User, UserScriptPin, Script


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 260_000


def normalize_username(username: str) -> str:
    return (username or "").strip()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations_text)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def create_session_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(48)
    return token, hash_session_token(token)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def invalidate_user_sessions(session, user: User):
    user.auth_version = (user.auth_version or 1) + 1
    await session.execute(delete(AuthSession).where(AuthSession.user_id == user.id))


async def sync_fixed_admin():
    """保证只有一个管理员，并让配置成为固定管理员凭据来源。"""
    username = normalize_username(ADMIN_USERNAME) or "admin"
    password = ADMIN_PASSWORD
    if not password:
        raise RuntimeError("必须设置 PLATFORM_ADMIN_PASSWORD，禁止使用默认管理员密码。")

    async with async_session() as session:
        admin_result = await session.execute(
            select(User).where(User.role == "admin").order_by(User.id.asc())
        )
        admins = admin_result.scalars().all()
        admin = admins[0] if admins else None

        if admin is None:
            existing_result = await session.execute(
                select(User).where(func.lower(User.username) == username.lower())
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                raise RuntimeError(f"固定管理员用户名已被普通用户占用: {username}")
            admin = User(
                username=username,
                display_name=ADMIN_DISPLAY_NAME,
                password_hash=hash_password(password),
                role="admin",
                auth_version=1,
            )
            session.add(admin)
            await session.flush()
        else:
            conflict_result = await session.execute(
                select(User).where(
                    func.lower(User.username) == username.lower(),
                    User.id != admin.id,
                )
            )
            if conflict_result.scalar_one_or_none():
                raise RuntimeError(f"固定管理员用户名已被占用: {username}")
            admin.username = username
            admin.display_name = ADMIN_DISPLAY_NAME
            if not verify_password(password, admin.password_hash):
                admin.password_hash = hash_password(password)
                await invalidate_user_sessions(session, admin)

        for duplicate_admin in admins[1:]:
            duplicate_admin.role = "user"
            await invalidate_user_sessions(session, duplicate_admin)

        pinned_result = await session.execute(
            select(Script.id, Script.pinned_at).where(Script.pinned_at.is_not(None))
        )
        existing_pin_result = await session.execute(
            select(UserScriptPin.script_id).where(UserScriptPin.user_id == admin.id)
        )
        existing_script_ids = set(existing_pin_result.scalars().all())
        legacy_pins = pinned_result.all()
        for script_id, pinned_at in legacy_pins:
            if script_id not in existing_script_ids:
                session.add(
                    UserScriptPin(
                        user_id=admin.id,
                        script_id=script_id,
                        pinned_at=pinned_at or datetime.now(),
                    )
                )
        if legacy_pins:
            await session.execute(
                update(Script)
                .where(Script.pinned_at.is_not(None))
                .values(pinned_at=None)
            )

        await session.commit()
