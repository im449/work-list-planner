from pydantic import BaseModel, Field
from typing import Optional, Literal
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI, Response, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fpdf import FPDF
import os
import math
import sqlite3
import asyncio
import contextvars
import time
import hmac
import logging
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, date
from passlib.context import CryptContext
from jose import JWTError, jwt
import base64
import hashlib
import io
import json
import secrets
import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken

# === НАСТРОЙКИ АВТОРИЗАЦИИ ===
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set. Check .env / systemd EnvironmentFile")

TOTP_ENCRYPTION_KEY = os.environ.get("TOTP_ENCRYPTION_KEY", "")
if not TOTP_ENCRYPTION_KEY:
    raise RuntimeError("TOTP_ENCRYPTION_KEY is not set. Check .env / systemd EnvironmentFile")

try:
    totp_fernet = Fernet(TOTP_ENCRYPTION_KEY.encode())
except (ValueError, TypeError) as exc:
    raise RuntimeError("TOTP_ENCRYPTION_KEY has an invalid Fernet format") from exc

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

app = FastAPI()

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://work-list.ru"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "planner.db"))
_DEMO_DB_BASE = os.path.dirname(os.path.abspath(DB_PATH if DB_PATH != ":memory:" else __file__))
DEMO_DB_DIR = os.environ.get("DEMO_DB_DIR", os.path.join(_DEMO_DB_BASE, ".demo_sessions"))
DEMO_IDLE_TIMEOUT_SECONDS = 120 * 60
DEMO_MAX_LIFETIME_SECONDS = 24 * 60 * 60
DEMO_MAX_TASKS = 200
DEMO_MAX_BUDGET_ITEMS = 500
DEMO_MAX_CATEGORIES = 50
DEMO_MAX_SAVINGS_GOALS = 20
_DEMO_DB_PATH = contextvars.ContextVar("demo_db_path", default=None)
_demo_cleanup_task = None
logger = logging.getLogger(__name__)

def db_for_path(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def db():
    return db_for_path(_DEMO_DB_PATH.get() or DB_PATH)

# === ИНИЦИАЛИЗАЦИЯ И МИГРАЦИЯ БАЗЫ ===
def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_date TEXT,
            status TEXT DEFAULT 'todo',
            description TEXT DEFAULT '',
            user_id INTEGER,
            archived INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            role TEXT DEFAULT 'user'
        )
    """)

    user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
    if "role" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")

    if "is_active" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")

    if "last_seen" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")

    if "token_version" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")

    # Поля двухфакторной авторизации.
    if "totp_secret" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")

    if "totp_pending_secret" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN totp_pending_secret TEXT")

    if "totp_enabled" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0"
        )

    if "recovery_codes_hashes" not in user_cols:
        c.execute(
            "ALTER TABLE users ADD COLUMN recovery_codes_hashes TEXT NOT NULL DEFAULT '[]'"
        )

    c.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    c.execute("""
        INSERT OR IGNORE INTO app_settings (key, value)
        VALUES ('registrations_enabled', '1')
    """)

    task_cols = [r["name"] for r in c.execute("PRAGMA table_info(tasks)")]
    if "user_id" not in task_cols:
        c.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")
        c.execute("UPDATE users SET role='admin' WHERE id = (SELECT MIN(id) FROM users)")
        c.execute("UPDATE tasks SET user_id = (SELECT MIN(id) FROM users) WHERE user_id IS NULL")

    if "archived" not in task_cols:
        c.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER DEFAULT 0")


    c.execute("""
        CREATE TABLE IF NOT EXISTS savings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, day)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS savings_settings (
            user_id INTEGER PRIMARY KEY,
            goal REAL NOT NULL DEFAULT 100000,
            days INTEGER NOT NULL DEFAULT 365
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS savings_goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            goal REAL NOT NULL DEFAULT 100000,
            days INTEGER NOT NULL DEFAULT 365,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_savings_goals_user ON savings_goals(user_id, archived, id)")

    # Calendar metadata added additively; existing amounts/day indexes are untouched.
    goal_cols = [r["name"] for r in c.execute("PRAGMA table_info(savings_goals)")]
    if "start_date" not in goal_cols:
        c.execute("ALTER TABLE savings_goals ADD COLUMN start_date TEXT")
    goal_cols = [r["name"] for r in c.execute("PRAGMA table_info(savings_goals)")]
    if "end_date" not in goal_cols:
        c.execute("ALTER TABLE savings_goals ADD COLUMN end_date TEXT")
    goal_cols = [r["name"] for r in c.execute("PRAGMA table_info(savings_goals)")]
    if "created_date_known" not in goal_cols:
        # Existing records cannot prove the original user-facing creation date.
        c.execute("ALTER TABLE savings_goals ADD COLUMN created_date_known INTEGER NOT NULL DEFAULT 0")
    goal_cols = [r["name"] for r in c.execute("PRAGMA table_info(savings_goals)")]
    if "calendar_configured" not in goal_cols:
        # Legacy goals get one opportunity to set a truthful calendar anchor.
        c.execute("ALTER TABLE savings_goals ADD COLUMN calendar_configured INTEGER NOT NULL DEFAULT 0")

    # For legacy goals use a sensible calendar period, but label their historical
    # creation date as unknown instead of presenting the migration date as truth.
    c.execute("""
        UPDATE savings_goals
        SET start_date = COALESCE(start_date, date('now')),
            end_date = COALESCE(
                end_date,
                date(COALESCE(start_date, date('now')), printf('+%d days', days - 1))
            )
        WHERE start_date IS NULL OR end_date IS NULL
    """)
    c.execute("""
        UPDATE savings_goals SET created_date_known = 1
        WHERE id NOT IN (SELECT MIN(id) FROM savings_goals GROUP BY user_id)
    """)

    # New rows created by helper/automatic replacement code also receive dates.
    # This trigger runs only on future inserts; it does not relabel old goals.
    c.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_savings_goals_calendar_defaults
        AFTER INSERT ON savings_goals
        FOR EACH ROW
        WHEN NEW.start_date IS NULL OR NEW.end_date IS NULL
        BEGIN
            UPDATE savings_goals
            SET start_date = COALESCE(NEW.start_date, date('now')),
                end_date = COALESCE(
                    NEW.end_date,
                    date(COALESCE(NEW.start_date, date('now')), printf('+%d days', NEW.days - 1))
                ),
                created_date_known = 1,
                calendar_configured = 1
            WHERE id = NEW.id;
        END
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS savings_goal_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            day INTEGER NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            UNIQUE(goal_id, day),
            FOREIGN KEY(goal_id) REFERENCES savings_goals(id)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_savings_goal_entries_goal ON savings_goal_entries(goal_id, day)")

    # === ГОДОВОЙ БЮДЖЕТ ===
    c.execute("""
        CREATE TABLE IF NOT EXISTS budget_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0),
            item_type TEXT NOT NULL CHECK (item_type IN ('income', 'expense')),
            category TEXT NOT NULL DEFAULT 'Без категории',
            due_date TEXT NOT NULL,
            mandatory INTEGER NOT NULL DEFAULT 0 CHECK (mandatory IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'paid')),
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS budget_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_type TEXT NOT NULL
                CHECK (item_type IN ('income', 'expense')),
            name TEXT COLLATE NOCASE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, item_type, name)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_budget_items_user_date
        ON budget_items(user_id, due_date, id)
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS demo_sessions (
            session_id TEXT PRIMARY KEY,
            db_path TEXT NOT NULL UNIQUE,
            ip_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_activity INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_demo_sessions_activity ON demo_sessions(last_activity, expires_at)")
    conn.commit()
    conn.close()

init_db()

# === ФУНКЦИИ АВТОРИЗАЦИИ ===
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def authenticate_user(username: str, password: str):
    conn = db()
    row = conn.execute(
        "SELECT id, username, password_hash, role, is_active, token_version FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return False
    return {
        "id": row["id"], "username": row["username"],
        "role": row["role"], "token_version": row["token_version"],
    }

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def encrypt_totp_secret(secret: str) -> str:
    """Шифрует TOTP-секрет перед сохранением в БД."""
    return totp_fernet.encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted_secret: str) -> str:
    """Расшифровывает сохранённый TOTP-секрет."""
    return totp_fernet.decrypt(encrypted_secret.encode()).decode()


def verify_totp_code(secret: str, code: str) -> bool:
    """Проверяет шестизначный TOTP-код с небольшим допуском по времени."""
    if not code or not code.isdigit() or len(code) != 6:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def make_totp_qr_data_uri(username: str, secret: str) -> str:
    """Создаёт QR-код для приложения-аутентификатора."""
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=username,
        issuer_name="Work List Planner",
    )
    image = qrcode.make(uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def hash_recovery_code(code: str) -> str:
    """Хеширует случайный резервный код перед сохранением."""
    normalized = code.replace("-", "").replace(" ", "").upper()
    return hashlib.sha256(normalized.encode()).hexdigest()


def generate_recovery_codes(count: int = 8) -> list[str]:
    """Создаёт одноразовые резервные коды высокой энтропии."""
    return [
        f"{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
        for _ in range(count)
    ]

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить токен",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("kind") == "demo":
            raise credentials_exception
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    conn = db()
    try:
        row = conn.execute(
            "SELECT id, username, role, is_active, token_version FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if not row or not row["is_active"]:
            raise credentials_exception

        if payload.get("ver", 0) != row["token_version"]:
            raise credentials_exception

        # Не чаще одного обновления за пять минут для каждого пользователя.
        conn.execute("""
            UPDATE users
            SET last_seen = CURRENT_TIMESTAMP
            WHERE id = ?
              AND (
                  last_seen IS NULL
                  OR datetime(last_seen) < datetime('now', '-5 minutes')
              )
        """, (row["id"],))
        conn.commit()

        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"]
        }
    finally:
        conn.close()

def _safe_demo_db_path(path):
    if not path:
        return None
    root = os.path.realpath(DEMO_DB_DIR)
    candidate = os.path.realpath(path)
    try:
        if os.path.commonpath([root, candidate]) != root or candidate == root:
            return None
    except ValueError:
        return None
    return candidate


def _remove_demo_db(path):
    safe_path = _safe_demo_db_path(path)
    if not safe_path:
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(safe_path + suffix)
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("Не удалось удалить временную базу демосессии")


def _delete_demo_session(session_id):
    conn = db_for_path(DB_PATH)
    path = None
    try:
        row = conn.execute(
            "SELECT db_path FROM demo_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row:
            path = row["db_path"]
            conn.execute("DELETE FROM demo_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
    finally:
        conn.close()
    if path:
        _remove_demo_db(path)


def _cleanup_expired_demo_sessions():
    now = int(time.time())
    conn = db_for_path(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT session_id, db_path FROM demo_sessions WHERE last_activity <= ? OR expires_at <= ?",
            (now - DEMO_IDLE_TIMEOUT_SECONDS, now),
        ).fetchall()
        if rows:
            conn.executemany("DELETE FROM demo_sessions WHERE session_id = ?", [(row["session_id"],) for row in rows])
            conn.commit()
    finally:
        conn.close()
    for row in rows:
        _remove_demo_db(row["db_path"])


def _enforce_demo_record_limit(conn, table, user_id, limit, needed=1):
    allowed_tables = {"tasks", "budget_items", "budget_categories", "savings_goals"}
    if table not in allowed_tables:
        raise RuntimeError("Недопустимая таблица для ограничения демоданных")
    count = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?", (user_id,)).fetchone()["n"]
    if count + needed > limit:
        raise HTTPException(status_code=429, detail=f"В деморежиме достигнут лимит записей ({limit})")


@app.post("/api/demo/sessions")
@limiter.limit("5/10 minutes")
async def create_demo_session(request: Request):
    _cleanup_expired_demo_sessions()
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hmac.new(SECRET_KEY.encode("utf-8"), client_ip.encode("utf-8"), hashlib.sha256).hexdigest()
    now = int(time.time())
    session_id = secrets.token_urlsafe(32)
    os.makedirs(DEMO_DB_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(DEMO_DB_DIR, 0o700)
    except OSError:
        logger.warning("Не удалось установить права каталога демосессий")
    db_path = os.path.join(DEMO_DB_DIR, secrets.token_hex(32) + ".sqlite3")

    conn = db_for_path(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM demo_sessions WHERE ip_hash = ? AND last_activity > ? AND expires_at > ?",
            (ip_hash, now - DEMO_IDLE_TIMEOUT_SECONDS, now),
        ).fetchone()["n"]
        if active >= 3:
            conn.rollback()
            raise HTTPException(status_code=429, detail="Слишком много активных демосессий с этого адреса. Попробуйте позже.")
        conn.execute(
            "INSERT INTO demo_sessions (session_id, db_path, ip_hash, created_at, last_activity, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, db_path, ip_hash, now, now, now + DEMO_MAX_LIFETIME_SECONDS),
        )
        conn.commit()
    finally:
        conn.close()

    context_token = _DEMO_DB_PATH.set(db_path)
    try:
        init_db()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            logger.warning("Не удалось установить права файла демобазы")
    except Exception:
        _delete_demo_session(session_id)
        raise
    finally:
        _DEMO_DB_PATH.reset(context_token)

    access_token = create_access_token(
        data={"sub": session_id, "sid": session_id, "kind": "demo"},
        expires_delta=timedelta(seconds=DEMO_MAX_LIFETIME_SECONDS),
    )
    return {"access_token": access_token, "token_type": "bearer", "idle_timeout_seconds": DEMO_IDLE_TIMEOUT_SECONDS, "expires_in": DEMO_MAX_LIFETIME_SECONDS}


async def get_app_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(status_code=401, detail="Недействительная или завершённая сессия", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_exception
    if payload.get("kind") != "demo":
        yield await get_current_user(token)
        return
    session_id = payload.get("sid")
    if not isinstance(session_id, str) or not session_id or payload.get("sub") != session_id:
        raise credentials_exception
    now = int(time.time())
    conn = db_for_path(DB_PATH)
    try:
        row = conn.execute("SELECT db_path, last_activity, expires_at FROM demo_sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            raise credentials_exception
        if now - row["last_activity"] >= DEMO_IDLE_TIMEOUT_SECONDS or now >= row["expires_at"]:
            expired_path = row["db_path"]
            conn.execute("DELETE FROM demo_sessions WHERE session_id = ?", (session_id,))
            conn.commit()
        else:
            expired_path = None
            db_path = _safe_demo_db_path(row["db_path"])
            if not db_path or not os.path.isfile(db_path):
                conn.execute("DELETE FROM demo_sessions WHERE session_id = ?", (session_id,))
                conn.commit()
                raise credentials_exception
            conn.execute("UPDATE demo_sessions SET last_activity = ? WHERE session_id = ?", (now, session_id))
            conn.commit()
    finally:
        conn.close()
    if expired_path:
        _remove_demo_db(expired_path)
        raise credentials_exception
    context_token = _DEMO_DB_PATH.set(db_path)
    try:
        yield {"id": 1, "username": "Демо", "role": "demo", "is_demo": True, "demo_session_id": session_id}
    finally:
        _DEMO_DB_PATH.reset(context_token)


@app.delete("/api/demo/sessions/current")
async def end_demo_session(current_user: dict = Depends(get_app_user)):
    if not current_user.get("is_demo"):
        raise HTTPException(status_code=403, detail="Этот маршрут доступен только демосессии")
    _delete_demo_session(current_user["demo_session_id"])
    return {"status": "ended"}


async def _demo_cleanup_loop():
    while True:
        await asyncio.sleep(300)
        try:
            _cleanup_expired_demo_sessions()
        except Exception:
            logger.exception("Ошибка фоновой очистки демосессий")


@app.on_event("startup")
async def start_demo_cleanup():
    global _demo_cleanup_task
    if _demo_cleanup_task is None or _demo_cleanup_task.done():
        _demo_cleanup_task = asyncio.create_task(_demo_cleanup_loop())


@app.on_event("shutdown")
async def stop_demo_cleanup():
    global _demo_cleanup_task
    if _demo_cleanup_task is not None:
        _demo_cleanup_task.cancel()
        try:
            await _demo_cleanup_task
        except asyncio.CancelledError:
            pass
        _demo_cleanup_task = None


async def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user

class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


class TotpSetupRequest(BaseModel):
    current_password: str = Field(min_length=1)


class TotpCodeRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@app.post("/users/me/2fa/setup")
async def setup_totp(
    data: TotpSetupRequest,
    current_user: dict = Depends(get_current_user),
):
    conn = db()
    try:
        row = conn.execute(
            "SELECT password_hash, totp_enabled FROM users WHERE id = ?",
            (current_user["id"],),
        ).fetchone()

        if not row or not verify_password(data.current_password, row["password_hash"]):
            raise HTTPException(status_code=400, detail="Неверный текущий пароль")

        if row["totp_enabled"]:
            raise HTTPException(status_code=409, detail="Двухфакторная авторизация уже включена")

        secret = pyotp.random_base32()
        encrypted = encrypt_totp_secret(secret)

        conn.execute(
            "UPDATE users SET totp_pending_secret = ? WHERE id = ?",
            (encrypted, current_user["id"]),
        )
        conn.commit()

        return {
            "secret": secret,
            "qr_code": make_totp_qr_data_uri(current_user["username"], secret),
            "message": "Добавьте аккаунт в приложение-аутентификатор и подтвердите кодом",
        }
    finally:
        conn.close()


@app.post("/users/me/2fa/confirm")
async def confirm_totp(
    data: TotpCodeRequest,
    current_user: dict = Depends(get_current_user),
):
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT totp_pending_secret, totp_enabled FROM users WHERE id = ?",
            (current_user["id"],),
        ).fetchone()

        if not row or row["totp_enabled"] or not row["totp_pending_secret"]:
            raise HTTPException(status_code=400, detail="Сначала начните настройку 2FA")

        try:
            secret = decrypt_totp_secret(row["totp_pending_secret"])
        except (InvalidToken, ValueError):
            raise HTTPException(status_code=500, detail="Не удалось прочитать секрет 2FA")

        if not verify_totp_code(secret, data.code):
            raise HTTPException(status_code=400, detail="Неверный код подтверждения")

        recovery_codes = generate_recovery_codes()
        recovery_hashes = json.dumps(
            [hash_recovery_code(code) for code in recovery_codes]
        )

        conn.execute(
            """
            UPDATE users
            SET totp_secret = ?,
                totp_pending_secret = NULL,
                totp_enabled = 1,
                recovery_codes_hashes = ?,
                token_version = token_version + 1
            WHERE id = ?
            """,
            (
                row["totp_pending_secret"],
                recovery_hashes,
                current_user["id"],
            ),
        )
        conn.commit()

        return {
            "enabled": True,
            "recovery_codes": recovery_codes,
            "message": "Сохраните резервные коды в безопасном месте. Повторно они не показываются.",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.post("/users/me/password")
async def change_password(
    data: PasswordChange,
    current_user: dict = Depends(get_current_user),
):
    if len(data.new_password) < 12:
        raise HTTPException(
            status_code=400,
            detail="Новый пароль должен содержать не менее 12 символов",
        )

    if data.current_password == data.new_password:
        raise HTTPException(
            status_code=400,
            detail="Новый пароль должен отличаться от текущего",
        )

    conn = db()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (current_user["id"],),
        ).fetchone()

        if not row or not verify_password(
            data.current_password, row["password_hash"]
        ):
            raise HTTPException(
                status_code=400,
                detail="Текущий пароль указан неверно",
            )

        conn.execute(
            """UPDATE users
               SET password_hash = ?, token_version = token_version + 1
               WHERE id = ?""",
            (get_password_hash(data.new_password), current_user["id"]),
        )
        conn.commit()
        return {"status": "ok", "message": "Пароль успешно изменён"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.get("/users/me")
async def read_current_user(current_user: dict = Depends(get_current_user)):
    return current_user


# === ЭНДПОИНТЫ АВТОРИЗАЦИИ ===
@app.post("/token")
@limiter.limit("10/minute")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )

    form = await request.form()
    totp_code = (form.get("totp_code") or "").strip()
    recovery_code = (form.get("recovery_code") or "").strip()

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT totp_enabled, totp_secret, recovery_codes_hashes "
            "FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()

        if row and row["totp_enabled"]:
            if bool(totp_code) == bool(recovery_code):
                raise HTTPException(
                    status_code=401,
                    detail="Требуется TOTP-код или резервный код",
                )

            if totp_code:
                try:
                    secret = decrypt_totp_secret(row["totp_secret"])
                except (InvalidToken, AttributeError, TypeError, ValueError):
                    secret = ""

                if not secret or not verify_totp_code(secret, totp_code):
                    raise HTTPException(
                        status_code=401,
                        detail="Неверный код 2FA",
                    )
            else:
                try:
                    hashes = json.loads(row["recovery_codes_hashes"] or "[]")
                except (TypeError, ValueError):
                    hashes = []

                code_hash = hash_recovery_code(recovery_code)
                if code_hash not in hashes:
                    raise HTTPException(
                        status_code=401,
                        detail="Неверный или уже использованный резервный код",
                    )

                hashes.remove(code_hash)
                conn.execute(
                    "UPDATE users SET recovery_codes_hashes = ? WHERE id = ?",
                    (json.dumps(hashes), user["id"]),
                )

        conn.commit()
    finally:
        conn.close()

    access_token = create_access_token(
        data={"sub": user["username"], "ver": user["token_version"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/register")
@limiter.limit("5/minute")
async def register_user(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    conn = db()
    setting = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'registrations_enabled'"
    ).fetchone()
    if not setting or setting["value"] != "1":
        conn.close()
        raise HTTPException(status_code=403, detail="Регистрация временно отключена")

    if conn.execute("SELECT 1 FROM users WHERE username = ?", (form_data.username,)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    hashed = get_password_hash(form_data.password)
    conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (form_data.username, hashed))
    conn.commit()
    conn.close()
    return {"status": "ok", "username": form_data.username}

# === МОДЕЛЬ ДЛЯ ОБНОВЛЕНИЯ ЗАДАЧИ ===
class TaskUpdate(BaseModel):
    title: Optional[str] = None
    due_date: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    archived: Optional[bool] = None

# === ОСНОВНЫЕ ЭНДПОИНТЫ ===
@app.get("/")
async def root():
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(file_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return Response(content=html_content, media_type="text/html")

@app.get("/savings.html")
async def savings_page():
    file_path = os.path.join(os.path.dirname(__file__), "savings.html")
    with open(file_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return Response(content=html_content, media_type="text/html")


@app.get("/budget.html")
async def budget_page():
    file_path = os.path.join(os.path.dirname(__file__), "budget.html")
    with open(file_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    return Response(content=html_content, media_type="text/html")


@app.get("/tasks")
async def get_tasks(archived: bool = False, current_user: dict = Depends(get_app_user)):
    conn = db()
    rows = conn.execute(
        "SELECT id, title, due_date, status, description FROM tasks WHERE user_id = ? AND archived = ? ORDER BY id DESC",
        (current_user["id"], 1 if archived else 0),
    ).fetchall()
    conn.close()
    return [{
        "id": r["id"],
        "title": r["title"],
        "dueDate": r["due_date"] or "",
        "status": r["status"],
        "description": r["description"] or ""
    } for r in rows]

@app.post("/tasks")
async def create_task(task: dict, current_user: dict = Depends(get_app_user)):
    title = task.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    due_date = task.get("dueDate", "")
    description = task.get("description", "")
    conn = db()
    if current_user.get("is_demo"):
        try:
            _enforce_demo_record_limit(conn, "tasks", current_user["id"], DEMO_MAX_TASKS)
        except Exception:
            conn.close()
            raise
    cur = conn.execute(
        "INSERT INTO tasks (title, due_date, status, description, user_id) VALUES (?, ?, ?, ?, ?)",
        (title, due_date, "todo", description, current_user["id"])
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return {"id": task_id, "status": "ok"}

@app.put("/tasks/{task_id}")
async def update_task(
    task_id: int,
    data: TaskUpdate,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    row = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, current_user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")

    updates = []
    values = []

    if data.title is not None:
        updates.append("title = ?")
        values.append(data.title)
    if data.due_date is not None:
        updates.append("due_date = ?")
        values.append(data.due_date)
    if data.description is not None:
        updates.append("description = ?")
        values.append(data.description)
    if data.status is not None:
        if data.status not in ["todo", "progress", "done"]:
            raise HTTPException(status_code=400, detail="Invalid status")
        updates.append("status = ?")
        values.append(data.status)
    if data.archived is not None:
        updates.append("archived = ?")
        values.append(1 if data.archived else 0)

    if not updates:
        conn.close()
        return {"status": "ok", "message": "No fields updated"}

    values.append(task_id)
    query = f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?"
    conn.execute(query, values)
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/tasks/{task_id}")
async def delete_task(task_id: int, current_user: dict = Depends(get_app_user)):
    conn = db()
    conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, current_user["id"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# === АРХИВ ===
@app.post("/tasks/archive-done")
async def archive_done_tasks(current_user: dict = Depends(get_app_user)):
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET archived = 1 WHERE user_id = ? AND status = 'done' AND archived = 0",
        (current_user["id"],),
    )
    conn.commit()
    count = cur.rowcount
    conn.close()
    return {"status": "ok", "archived": count}

@app.post("/tasks/{task_id}/archive")
async def archive_task(task_id: int, current_user: dict = Depends(get_app_user)):
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET archived = 1 WHERE id = ? AND user_id = ?",
        (task_id, current_user["id"])
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "ok"}

@app.post("/tasks/{task_id}/restore")
async def restore_task(task_id: int, current_user: dict = Depends(get_app_user)):
    conn = db()
    cur = conn.execute(
        "UPDATE tasks SET archived = 0 WHERE id = ? AND user_id = ?",
        (task_id, current_user["id"])
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "ok"}

# === АДМИНКА ===
@app.get("/admin/settings/registrations")
async def get_registration_setting(admin: dict = Depends(require_admin)):
    conn = db()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'registrations_enabled'"
        ).fetchone()
        return {"enabled": bool(row and row["value"] == "1")}
    finally:
        conn.close()


@app.post("/admin/settings/registrations")
async def set_registration_setting(data: dict, admin: dict = Depends(require_admin)):
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="'enabled' должен быть true или false")

    conn = db()
    try:
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = 'registrations_enabled'",
            ("1" if enabled else "0",)
        )
        conn.commit()
    finally:
        conn.close()

    return {"enabled": enabled}


@app.get("/admin/users")
async def admin_list_users(admin: dict = Depends(require_admin)):
    conn = db()
    rows = conn.execute("""
        SELECT u.id, u.username, u.role, u.created_at, u.is_active,
               (SELECT COUNT(*) FROM tasks t WHERE t.user_id = u.id) AS task_count
        FROM users u ORDER BY u.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/admin/activity")
async def admin_user_activity(admin: dict = Depends(require_admin)):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT id, username, created_at, role, is_active, last_seen
            FROM users
            ORDER BY datetime(created_at) DESC, id DESC
        """).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.post("/admin/users/{user_id}/role")
async def admin_set_role(user_id: int, data: dict, admin: dict = Depends(require_admin)):
    role = data.get("role")
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'user'")
    if user_id == admin["id"] and role != "admin":
        raise HTTPException(status_code=400, detail="Нельзя снять роль админа с самого себя")
    conn = db()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/admin/users/{user_id}/active")
async def admin_set_active(user_id: int, data: dict, admin: dict = Depends(require_admin)):
    active = data.get("is_active")
    if not isinstance(active, bool):
        raise HTTPException(status_code=400, detail="is_active должен быть true или false")

    if user_id == admin["id"] and not active:
        raise HTTPException(status_code=400, detail="Нельзя заблокировать самого себя")

    conn = db()
    try:
        cur = conn.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if active else 0, user_id)
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        conn.commit()
    finally:
        conn.close()

    return {"status": "ok", "user_id": user_id, "is_active": active}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    conn = db()
    conn.execute("DELETE FROM tasks WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/admin")
async def admin_page():
    file_path = os.path.join(os.path.dirname(__file__), "admin.html")
    with open(file_path, "r", encoding="utf-8") as f:
        return Response(content=f.read(), media_type="text/html")

# === PDF ОТЧЁТ С КИРИЛЛИЦЕЙ ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_PATH = os.path.join(BASE_DIR, "DejaVuSans.ttf")

class PDFWithCyrillic(FPDF):
    def __init__(self):
        super().__init__(orientation="L", unit="mm", format="A4")
        if os.path.exists(FONT_PATH):
            self.add_font("DejaVuSans", "", FONT_PATH, uni=True)
            self.add_font("DejaVuSans", "B", FONT_PATH, uni=True)
            self.add_font("DejaVuSans", "I", FONT_PATH, uni=True)
            self.set_font("DejaVuSans", size=12)
        else:
            self.set_font("Arial", size=12)

    def header(self):
        font = "DejaVuSans" if os.path.exists(FONT_PATH) else "Arial"
        self.set_font(font, "B", 14)
        self.cell(0, 10, "Отчёт по задачам", ln=True, align="C")

    def footer(self):
        self.set_y(-15)
        font = "DejaVuSans" if os.path.exists(FONT_PATH) else "Arial"
        self.set_font(font, "I", 8)
        self.cell(0, 10, f"Страница {self.page_no()}", align="C")


@app.post("/tasks/report")
async def generate_report(request: Request, current_user: dict = Depends(get_app_user)):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT id, title, due_date, status, description "
            "FROM tasks WHERE user_id = ? AND archived = 0 ORDER BY id DESC",
            (current_user["id"],)
        ).fetchall()
    finally:
        conn.close()

    pdf = PDFWithCyrillic()
    font = "DejaVuSans" if os.path.exists(FONT_PATH) else "Arial"
    pdf.set_margins(10, 10, 10)
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    col_widths = [12, 72, 28, 30, 135]
    headers = ["ID", "Название", "Дата", "Статус", "Описание"]
    line_h = 5
    bottom_limit = 190

    def draw_table_header():
        pdf.set_font(font, "B", 8)
        pdf.set_x(10)
        for width, label in zip(col_widths, headers):
            pdf.cell(width, 8, label, border=1, align="C")
        pdf.ln(8)

    def clean(value):
        return str(value or "").replace("\r", " ").replace("\n", " ")

    def draw_row(row):
        values = [
            str(row["id"]),
            clean(row["title"]),
            clean(row["due_date"]),
            clean(row["status"]),
            clean(row["description"]),
        ]

        pdf.set_font(font, "", 8)
        line_counts = []
        for width, value in zip(col_widths, values):
            lines = pdf.multi_cell(width, line_h, value or " ", split_only=True)
            line_counts.append(max(1, len(lines)))

        row_height = max(line_counts) * line_h

        if pdf.get_y() + row_height > bottom_limit:
            pdf.add_page()
            draw_table_header()

        x = 10
        y = pdf.get_y()
        for width, value in zip(col_widths, values):
            pdf.set_xy(x, y)
            pdf.multi_cell(width, line_h, value or " ", border=1)
            x += width

        pdf.set_xy(10, y + row_height)

    draw_table_header()

    if not rows:
        pdf.set_font(font, "", 9)
        pdf.cell(sum(col_widths), 10, "Нет задач для отображения", border=1)
        pdf.ln(10)
    else:
        for row in rows:
            draw_row(row)

    pdf_data = pdf.output(dest="S")
    if isinstance(pdf_data, str):
        pdf_data = pdf_data.encode("latin-1")

    return Response(
        content=pdf_data,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=tasks_report.pdf"}
    )





# === НАКОПЛЕНИЯ ===
def _ensure_savings_goal(conn, user_id: int):
    """Return the user's first active goal, migrating their old single goal on first use."""
    row = conn.execute(
        "SELECT * FROM savings_goals "
        "WHERE user_id = ? AND archived = 0 ORDER BY id LIMIT 1",
        (user_id,)
    ).fetchone()
    if row:
        return row

    # Preserve settings and daily contributions stored in the legacy single-goal tables.
    legacy_settings = conn.execute(
        "SELECT goal, days FROM savings_settings WHERE user_id = ?", (user_id,)
    ).fetchone()
    goal = float(legacy_settings["goal"]) if legacy_settings else 100000.0
    days = int(legacy_settings["days"]) if legacy_settings else 365
    start_date = datetime.utcnow().date()
    end_date = start_date + timedelta(days=days - 1)
    cursor = conn.execute(
        "INSERT INTO savings_goals (user_id, name, goal, days, start_date, end_date, created_date_known, calendar_configured) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (user_id, "Моя копилка", goal, days, start_date.isoformat(), end_date.isoformat())
    )
    goal_id = cursor.lastrowid
    legacy_entries = conn.execute(
        "SELECT day, amount, paid FROM savings WHERE user_id = ? ORDER BY day", (user_id,)
    ).fetchall()
    if legacy_entries:
        conn.executemany(
            "INSERT OR IGNORE INTO savings_goal_entries (goal_id, day, amount, paid) "
            "VALUES (?, ?, ?, ?)",
            [(goal_id, entry["day"], entry["amount"], entry["paid"]) for entry in legacy_entries]
        )
    conn.commit()
    return conn.execute(
        "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?",
        (goal_id, user_id)
    ).fetchone()


def _get_savings_goal(conn, user_id: int, goal_id: int | None = None):
    if goal_id is None:
        return _ensure_savings_goal(conn, user_id)
    row = conn.execute(
        "SELECT * FROM savings_goals "
        "WHERE id = ? AND user_id = ? AND archived = 0",
        (goal_id, user_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Копилка не найдена")
    return row


def _parse_savings_goal(item: dict, current=None):
    try:
        default_goal = current["goal"] if current else 100000
        default_days = current["days"] if current else 365
        goal = float(item.get("goal", default_goal))
        days = int(item.get("days", default_days))
        name = str(item.get("name", current["name"] if current else "Моя копилка")).strip()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Укажите корректное название, цель и срок")
    if not name or len(name) > 80:
        raise HTTPException(status_code=400, detail="Название копилки должно содержать от 1 до 80 символов")
    if not 0 < goal <= 1000000000000:
        raise HTTPException(status_code=400, detail="Цель должна быть больше нуля")
    if not 1 <= days <= 3650:
        raise HTTPException(status_code=400, detail="Срок должен быть от 1 до 3650 дней")
    return name, goal, days


def _parse_savings_date_range(item: dict, current=None, requested_days: int = 365):
    """Return (start_iso, end_iso, inclusive_days) with strict ISO calendar dates."""
    default_start = (current["start_date"] if current is not None else None) or datetime.utcnow().date().isoformat()
    raw_start = item.get("start_date") or default_start
    raw_end = item.get("end_date")
    try:
        start = datetime.strptime(str(raw_start), "%Y-%m-%d").date()
        if raw_end:
            end = datetime.strptime(str(raw_end), "%Y-%m-%d").date()
        else:
            end = start + timedelta(days=int(requested_days) - 1)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="Укажите корректные даты начала и окончания")
    duration = (end - start).days + 1
    if duration < 1:
        raise HTTPException(status_code=400, detail="Дата окончания не может быть раньше даты начала")
    if duration > 3650:
        raise HTTPException(status_code=400, detail="Период копилки не может превышать 3650 календарных дней")
    return start.isoformat(), end.isoformat(), duration


def _validate_savings_calendar_change(conn, goal_id: int, current, start_date: str, days: int):
    saved_count = conn.execute(
        "SELECT COUNT(*) AS n FROM savings_goal_entries WHERE goal_id = ?",
        (goal_id,)
    ).fetchone()["n"]
    if saved_count and bool(current["calendar_configured"]) and start_date != current["start_date"]:
        raise HTTPException(
            status_code=400,
            detail="Нельзя менять дату начала после сохранения записей: даты взносов сместились бы. Измените дату окончания."
        )
    last_entry = conn.execute(
        "SELECT MAX(day) AS last_day FROM savings_goal_entries WHERE goal_id = ?",
        (goal_id,)
    ).fetchone()["last_day"]
    if last_entry is not None and int(last_entry) > days:
        raise HTTPException(
            status_code=400,
            detail=f"В выбранном диапазоне только {days} дней, но данные есть до дня {last_entry}. Выберите более позднюю дату окончания."
        )


def _goal_payload(row):
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    def value(key, default=None):
        return row[key] if key in keys else default

    return {
        "id": int(row["id"]),
        "name": row["name"],
        "goal": float(row["goal"]),
        "days": int(row["days"]),
        "start_date": value("start_date"),
        "end_date": value("end_date"),
        "created_at": value("created_at"),
        "created_date_known": bool(value("created_date_known", 0)),
        "calendar_configured": bool(value("calendar_configured", 0)),
    }


@app.get("/api/savings/goals")
async def get_savings_goals(
    archived: bool = False,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        # Active-goal requests retain the original lazy migration behavior.
        # Archive queries must not create a new goal as a side effect.
        if not archived:
            _ensure_savings_goal(conn, current_user["id"])
        rows = conn.execute(
            "SELECT id, name, goal, days, start_date, end_date, created_at, created_date_known FROM savings_goals "
            "WHERE user_id = ? AND archived = ? ORDER BY id",
            (current_user["id"], 1 if archived else 0)
        ).fetchall()
        return [_goal_payload(row) for row in rows]
    finally:
        conn.close()


@app.post("/api/savings/goals")
async def create_savings_goal(
    item: dict,
    current_user: dict = Depends(get_app_user)
):
    name, goal, days = _parse_savings_goal(item)
    start_date, end_date, days = _parse_savings_date_range(item, None, days)
    conn = db()
    try:
        if current_user.get("is_demo"):
            _enforce_demo_record_limit(conn, "savings_goals", current_user["id"], DEMO_MAX_SAVINGS_GOALS)
        cursor = conn.execute(
            "INSERT INTO savings_goals (user_id, name, goal, days, start_date, end_date, created_date_known, calendar_configured) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 1)",
            (current_user["id"], name, goal, days, start_date, end_date)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?",
            (cursor.lastrowid, current_user["id"])
        ).fetchone()
        return _goal_payload(row)
    finally:
        conn.close()


@app.put("/api/savings/goals/{goal_id}")
async def rename_savings_goal(
    goal_id: int,
    item: dict,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        current = _get_savings_goal(conn, current_user["id"], goal_id)
        name, goal, days = _parse_savings_goal(item, current)
        start_date, end_date, days = _parse_savings_date_range(item, current, days)
        _validate_savings_calendar_change(conn, goal_id, current, start_date, days)
        conn.execute(
            "UPDATE savings_goals SET name = ?, goal = ?, days = ?, start_date = ?, end_date = ?, calendar_configured = 1 "
            "WHERE id = ? AND user_id = ?",
            (name, goal, days, start_date, end_date, goal_id, current_user["id"])
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        ).fetchone()
        return _goal_payload(updated)
    finally:
        conn.close()


@app.post("/api/savings/goals/{goal_id}/archive")
async def archive_savings_goal(
    goal_id: int,
    current_user: dict = Depends(get_app_user)
):
    """Archive a goal. If it is the last active goal, create a fresh empty replacement."""
    conn = db()
    try:
        goal = conn.execute(
            "SELECT id, name, goal, days, start_date, end_date, created_at, created_date_known FROM savings_goals "
            "WHERE id = ? AND user_id = ? AND archived = 0",
            (goal_id, current_user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="Активная копилка не найдена")

        active_count = conn.execute(
            "SELECT COUNT(*) AS n FROM savings_goals WHERE user_id = ? AND archived = 0",
            (current_user["id"],)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE savings_goals SET archived = 1 WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        )
        replacement_id = None
        if active_count <= 1:
            cursor = conn.execute(
                "INSERT INTO savings_goals (user_id, name, goal, days) VALUES (?, ?, ?, ?)",
                (current_user["id"], "Новая копилка", float(goal["goal"]), int(goal["days"]))
            )
            replacement_id = int(cursor.lastrowid)
        conn.commit()
        return {"archived": True, "replacement_id": replacement_id}
    finally:
        conn.close()


@app.post("/api/savings/goals/{goal_id}/restore")
async def restore_savings_goal(
    goal_id: int,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        goal = conn.execute(
            "SELECT id, name, goal, days, start_date, end_date, created_at, created_date_known FROM savings_goals "
            "WHERE id = ? AND user_id = ? AND archived = 1",
            (goal_id, current_user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="Архивная копилка не найдена")
        conn.execute(
            "UPDATE savings_goals SET archived = 0 WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        )
        conn.commit()
        restored = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        ).fetchone()
        return _goal_payload(restored)
    finally:
        conn.close()


@app.delete("/api/savings/goals/{goal_id}")
async def delete_savings_goal(
    goal_id: int,
    current_user: dict = Depends(get_app_user)
):
    """Permanently delete a goal and its entries; never leave the user without an active goal."""
    conn = db()
    try:
        goal = conn.execute(
            "SELECT * FROM savings_goals WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        ).fetchone()
        if not goal:
            raise HTTPException(status_code=404, detail="Копилка не найдена")
        if not goal["archived"]:
            active_count = conn.execute(
                "SELECT COUNT(*) AS n FROM savings_goals WHERE user_id = ? AND archived = 0",
                (current_user["id"],)
            ).fetchone()["n"]
            if active_count <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Нельзя удалить последнюю активную копилку. Создайте другую или отправьте эту в архив."
                )
        conn.execute(
            "DELETE FROM savings_goal_entries WHERE goal_id = ?",
            (goal_id,)
        )
        conn.execute(
            "DELETE FROM savings_goals WHERE id = ? AND user_id = ?",
            (goal_id, current_user["id"])
        )
        conn.commit()
        return {"deleted": True, "id": goal_id}
    finally:
        conn.close()


@app.get("/api/savings/summary")
async def get_savings_summary(current_user: dict = Depends(get_app_user)):
    conn = db()
    try:
        rows = conn.execute("""
            SELECT g.id, g.name, g.goal,
                   COALESCE(SUM(CASE WHEN e.paid = 1 THEN e.amount ELSE 0 END), 0) AS saved
            FROM savings_goals g
            LEFT JOIN savings_goal_entries e ON e.goal_id = g.id
            WHERE g.user_id = ? AND g.archived = 0
            GROUP BY g.id, g.name, g.goal
            ORDER BY g.id
        """, (current_user["id"],)).fetchall()
        goals = []
        total_goal = 0.0
        total_saved = 0.0
        total_remaining = 0.0
        completed_toward_goals = 0.0
        for row in rows:
            goal_amount = float(row["goal"] or 0)
            saved = round(float(row["saved"] or 0), 2)
            remaining = round(max(0.0, goal_amount - saved), 2)
            progress = min(100.0, saved / goal_amount * 100.0) if goal_amount > 0 else 0.0
            total_goal += goal_amount
            total_saved += saved
            total_remaining += remaining
            completed_toward_goals += min(saved, goal_amount)
            goals.append({
                "id": int(row["id"]), "name": row["name"],
                "goal": round(goal_amount, 2), "saved": saved,
                "remaining": remaining, "progress_percent": round(progress, 1)
            })
        return {
            "active_count": len(goals),
            "total_goal": round(total_goal, 2),
            "total_saved": round(total_saved, 2),
            "total_remaining": round(total_remaining, 2),
            "progress_percent": round(min(100.0, completed_toward_goals / total_goal * 100.0), 1) if total_goal > 0 else 0.0,
            "goals": goals,
        }
    finally:
        conn.close()


@app.get("/api/savings/settings")
async def get_savings_settings(
    goal_id: int | None = None,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        row = _get_savings_goal(conn, current_user["id"], goal_id)
        payload = _goal_payload(row)
        payload["has_entries"] = bool(conn.execute(
            "SELECT COUNT(*) AS n FROM savings_goal_entries WHERE goal_id = ?",
            (row["id"],)
        ).fetchone()["n"])
        return payload
    finally:
        conn.close()


@app.put("/api/savings/settings")
async def update_savings_settings(
    item: dict,
    goal_id: int | None = None,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        current = _get_savings_goal(conn, current_user["id"], goal_id)
        name, goal, days = _parse_savings_goal(item, current)
        start_date, end_date, days = _parse_savings_date_range(item, current, days)
        _validate_savings_calendar_change(conn, int(current["id"]), current, start_date, days)
        conn.execute(
            "UPDATE savings_goals SET name = ?, goal = ?, days = ?, start_date = ?, end_date = ?, calendar_configured = 1 "
            "WHERE id = ? AND user_id = ?",
            (name, goal, days, start_date, end_date, current["id"], current_user["id"])
        )
        conn.commit()
        return {"id": int(current["id"]), "name": name, "goal": goal, "days": days}
    finally:
        conn.close()


@app.get("/api/savings")
async def get_savings(
    goal_id: int | None = None,
    current_user: dict = Depends(get_app_user)
):
    conn = db()
    try:
        goal = _get_savings_goal(conn, current_user["id"], goal_id)
        rows = conn.execute(
            "SELECT day, amount, paid FROM savings_goal_entries WHERE goal_id = ? ORDER BY day",
            (goal["id"],)
        ).fetchall()
        saved = {
            row["day"]: {
                "day": row["day"],
                "amount": row["amount"],
                "paid": bool(row["paid"])
            }
            for row in rows
        }
        days = int(goal["days"])
        return [
            saved.get(day, {"day": day, "amount": 0, "paid": False})
            for day in range(1, days + 1)
        ]
    finally:
        conn.close()


@app.put("/api/savings/{day}")
async def update_saving(
    day: int,
    item: dict,
    goal_id: int | None = None,
    current_user: dict = Depends(get_app_user)
):
    if day < 1 or day > 3650:
        raise HTTPException(status_code=400, detail="Некорректный номер дня")
    try:
        amount = float(item.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    if not math.isfinite(amount):
        raise HTTPException(status_code=400, detail="Некорректная сумма")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Сумма не может быть отрицательной")
    paid = 1 if item.get("paid", False) else 0

    conn = db()
    try:
        goal = _get_savings_goal(conn, current_user["id"], goal_id)
        if day > int(goal["days"]):
            raise HTTPException(
                status_code=400,
                detail=f"День должен быть от 1 до {int(goal['days'])}"
            )
        conn.execute(
            "INSERT INTO savings_goal_entries (goal_id, day, amount, paid) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(goal_id, day) DO UPDATE SET amount = excluded.amount, paid = excluded.paid",
            (goal["id"], day, amount, paid)
        )
        conn.commit()
        return {"goal_id": int(goal["id"]), "day": day, "amount": amount, "paid": bool(paid)}
    finally:
        conn.close()



# === ГОДОВОЙ БЮДЖЕТ: МОДЕЛИ И API ===

class BudgetItemCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    amount_kopecks: int = Field(..., gt=0, le=100_000_000_000)
    item_type: Literal["income", "expense"]
    category: str = Field(default="Без категории", min_length=1, max_length=100)
    due_date: date
    mandatory: bool = False
    status: Literal["planned", "paid"] = "planned"
    notes: str = Field(default="", max_length=2000)
    repeat_monthly: bool = False


class BudgetItemUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    amount_kopecks: Optional[int] = Field(
        default=None, gt=0, le=100_000_000_000
    )
    item_type: Optional[Literal["income", "expense"]] = None
    category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    due_date: Optional[date] = None
    mandatory: Optional[bool] = None
    status: Optional[Literal["planned", "paid"]] = None
    notes: Optional[str] = Field(default=None, max_length=2000)


def _budget_item_dict(row):
    result = dict(row)
    result["mandatory"] = bool(result["mandatory"])
    return result


@app.get("/api/budget/items")
async def list_budget_items(
    year: int | None = None,
    current_user: dict = Depends(get_app_user),
):
    if year is None:
        year = date.today().year
    if year < 1900 or year > 2200:
        raise HTTPException(status_code=400, detail="Некорректный год")

    start = f"{year:04d}-01-01"
    end = f"{year + 1:04d}-01-01"

    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT id, user_id, title, amount_kopecks, item_type,
                   category, due_date, mandatory, status, notes,
                   created_at, updated_at
            FROM budget_items
            WHERE user_id = ? AND due_date >= ? AND due_date < ?
            ORDER BY due_date, id
            """,
            (current_user["id"], start, end),
        ).fetchall()
        return [_budget_item_dict(row) for row in rows]
    finally:
        conn.close()


@app.post("/api/budget/items")
async def create_budget_item(
    item: BudgetItemCreate,
    current_user: dict = Depends(get_app_user),
):
    title = item.title.strip()
    category = item.category.strip()
    if not title:
        raise HTTPException(status_code=422, detail="Название не может быть пустым")
    if not category:
        raise HTTPException(status_code=422, detail="Категория не может быть пустой")

    conn = db()
    try:
        months = range(1, 13) if item.repeat_monthly else [item.due_date.month]
        if current_user.get("is_demo"):
            _enforce_demo_record_limit(conn, "budget_items", current_user["id"], DEMO_MAX_BUDGET_ITEMS, needed=len(months))
        created_ids = []

        for month in months:
            if item.repeat_monthly:
                last_day = __import__("calendar").monthrange(
                    item.due_date.year, month
                )[1]
                day = min(item.due_date.day, last_day)
                operation_date = date(item.due_date.year, month, day)
                operation_status = "planned"
            else:
                operation_date = item.due_date
                operation_status = item.status

            cursor = conn.execute(
                """
                INSERT INTO budget_items
                    (user_id, title, amount_kopecks, item_type, category,
                     due_date, mandatory, status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_user["id"],
                    title,
                    item.amount_kopecks,
                    item.item_type,
                    category,
                    operation_date.isoformat(),
                    int(item.mandatory),
                    operation_status,
                    item.notes,
                ),
            )
            created_ids.append(cursor.lastrowid)

        conn.commit()

        placeholders = ",".join("?" for _ in created_ids)
        rows = conn.execute(
            f"""
            SELECT id, user_id, title, amount_kopecks, item_type,
                   category, due_date, mandatory, status, notes,
                   created_at, updated_at
            FROM budget_items
            WHERE user_id = ? AND id IN ({placeholders})
            ORDER BY due_date, id
            """,
            [current_user["id"], *created_ids],
        ).fetchall()

        created_items = [_budget_item_dict(row) for row in rows]
        if item.repeat_monthly:
            return {
                "repeat_monthly": True,
                "created_count": len(created_items),
                "items": created_items,
            }
        return created_items[0]
    finally:
        conn.close()


@app.put("/api/budget/items/{item_id}")
async def update_budget_item(
    item_id: int,
    item: BudgetItemUpdate,
    current_user: dict = Depends(get_app_user),
):
    changes = item.dict(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Нет данных для изменения")

    if any(value is None for value in changes.values()):
        raise HTTPException(
            status_code=422,
            detail="Поля операции не могут иметь значение null",
        )

    if "title" in changes:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise HTTPException(
                status_code=422,
                detail="Название не может быть пустым",
            )
    if "category" in changes:
        changes["category"] = changes["category"].strip()
        if not changes["category"]:
            raise HTTPException(
                status_code=422,
                detail="Категория не может быть пустой",
            )
    if "due_date" in changes and changes["due_date"] is not None:
        changes["due_date"] = changes["due_date"].isoformat()
    if "mandatory" in changes and changes["mandatory"] is not None:
        changes["mandatory"] = int(changes["mandatory"])

    allowed_fields = {
        "title", "amount_kopecks", "item_type", "category",
        "due_date", "mandatory", "status", "notes",
    }
    if not set(changes).issubset(allowed_fields):
        raise HTTPException(status_code=400, detail="Недопустимые поля")

    conn = db()
    try:
        existing = conn.execute(
            "SELECT id FROM budget_items WHERE id = ? AND user_id = ?",
            (item_id, current_user["id"]),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Операция не найдена")

        assignments = ", ".join(f"{field} = ?" for field in changes)
        values = list(changes.values())
        values.extend([datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), item_id, current_user["id"]])

        conn.execute(
            f"UPDATE budget_items SET {assignments}, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            values,
        )
        conn.commit()

        row = conn.execute(
            """
            SELECT id, user_id, title, amount_kopecks, item_type,
                   category, due_date, mandatory, status, notes,
                   created_at, updated_at
            FROM budget_items
            WHERE id = ? AND user_id = ?
            """,
            (item_id, current_user["id"]),
        ).fetchone()
        return _budget_item_dict(row)
    finally:
        conn.close()


@app.delete("/api/budget/items/{item_id}")
async def delete_budget_item(
    item_id: int,
    current_user: dict = Depends(get_app_user),
):
    conn = db()
    try:
        cursor = conn.execute(
            "DELETE FROM budget_items WHERE id = ? AND user_id = ?",
            (item_id, current_user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Операция не найдена")
        conn.commit()
        return {"status": "ok", "id": item_id}
    finally:
        conn.close()




class BudgetCategoryCreate(BaseModel):
    item_type: Literal["income", "expense"]
    name: str = Field(..., min_length=1, max_length=100)


BUDGET_DEFAULT_CATEGORIES = {
    "expense": [
        "Жильё и коммунальные услуги",
        "Продукты и товары для дома",
        "Транспорт",
        "Здоровье",
        "Связь и интернет",
        "Кредиты и долги",
        "Образование",
        "Кафе и развлечения",
        "Одежда и личные покупки",
        "Семья и питомцы",
        "Налоги и страховки",
        "Ремонт и обслуживание дома",
        "Путешествия и отпуск",
        "Подарки и благотворительность",
        "Другое",
    ],
    "income": [
        "Зарплата и премии",
        "Подработка и фриланс",
        "Бизнес",
        "Доход от инвестиций",
        "Аренда недвижимости",
        "Возвраты и компенсации",
        "Подарки и прочие поступления",
        "Другой доход",
    ],
}


@app.get("/api/budget/categories")
async def list_budget_categories(
    current_user: dict = Depends(get_app_user),
):
    conn = db()
    try:
        user_id = current_user["id"]
        for item_type, names in BUDGET_DEFAULT_CATEGORIES.items():
            for name in names:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO budget_categories
                        (user_id, item_type, name)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, item_type, name),
                )
        conn.commit()

        result = {"income": [], "expense": []}
        rows = conn.execute(
            """
            SELECT item_type, name
            FROM budget_categories
            WHERE user_id = ?
            ORDER BY id
            """,
            (user_id,),
        ).fetchall()
        for row in rows:
            result[row["item_type"]].append(row["name"])
        return result
    finally:
        conn.close()


@app.post("/api/budget/categories", status_code=201)
async def create_budget_category(
    category: BudgetCategoryCreate,
    current_user: dict = Depends(get_app_user),
):
    name = category.name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail="Название категории не может быть пустым",
        )

    conn = db()
    try:
        if current_user.get("is_demo"):
            _enforce_demo_record_limit(conn, "budget_categories", current_user["id"], DEMO_MAX_CATEGORIES)
        try:
            conn.execute(
                """
                INSERT INTO budget_categories (user_id, item_type, name)
                VALUES (?, ?, ?)
                """,
                (current_user["id"], category.item_type, name),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(
                status_code=409,
                detail="Такая категория уже существует",
            )
        return {"item_type": category.item_type, "name": name}
    finally:
        conn.close()

class BudgetCategoryRename(BaseModel):
    item_type: Literal["income", "expense"]
    old_name: str = Field(..., min_length=1, max_length=100)
    new_name: str = Field(..., min_length=1, max_length=100)


@app.patch("/api/budget/categories")
async def rename_budget_category(
    category: BudgetCategoryRename,
    current_user: dict = Depends(get_app_user),
):
    old_name = category.old_name.strip()
    new_name = category.new_name.strip()

    if not old_name or not new_name:
        raise HTTPException(
            status_code=422,
            detail="Название категории не может быть пустым",
        )

    if old_name.casefold() == new_name.casefold():
        raise HTTPException(
            status_code=400,
            detail="Новое название совпадает со старым",
        )

    conn = db()
    try:
        user_id = current_user["id"]
        existing = conn.execute(
            """
            SELECT id FROM budget_categories
            WHERE user_id = ? AND item_type = ? AND name = ?
            """,
            (user_id, category.item_type, old_name),
        ).fetchone()

        if not existing:
            raise HTTPException(
                status_code=404,
                detail="Категория не найдена",
            )

        duplicate = conn.execute(
            """
            SELECT id FROM budget_categories
            WHERE user_id = ? AND item_type = ? AND name = ?
            """,
            (user_id, category.item_type, new_name),
        ).fetchone()

        if duplicate:
            raise HTTPException(
                status_code=409,
                detail="Категория с таким названием уже существует",
            )

        conn.execute(
            """
            UPDATE budget_categories
            SET name = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_name, existing["id"], user_id),
        )
        conn.execute(
            """
            UPDATE budget_items
            SET category = ?, updated_at = datetime('now')
            WHERE user_id = ? AND item_type = ? AND category = ?
            """,
            (new_name, user_id, category.item_type, old_name),
        )
        conn.commit()

        return {
            "status": "ok",
            "item_type": category.item_type,
            "old_name": old_name,
            "new_name": new_name,
        }
    finally:
        conn.close()


@app.delete("/api/budget/categories")
async def delete_budget_category(
    item_type: Literal["income", "expense"],
    name: str,
    current_user: dict = Depends(get_app_user),
):
    name = name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail="Название категории не может быть пустым",
        )

    conn = db()
    try:
        user_id = current_user["id"]
        existing = conn.execute(
            """
            SELECT id FROM budget_categories
            WHERE user_id = ? AND item_type = ? AND name = ?
            """,
            (user_id, item_type, name),
        ).fetchone()

        if not existing:
            raise HTTPException(
                status_code=404,
                detail="Категория не найдена",
            )

        used = conn.execute(
            """
            SELECT 1 FROM budget_items
            WHERE user_id = ? AND item_type = ? AND category = ?
            LIMIT 1
            """,
            (user_id, item_type, name),
        ).fetchone()

        if used:
            raise HTTPException(
                status_code=409,
                detail="Категория используется в платежах. Сначала переименуйте её или перенесите платежи.",
            )

        conn.execute(
            "DELETE FROM budget_categories WHERE id = ? AND user_id = ?",
            (existing["id"], user_id),
        )
        conn.commit()

        return {"status": "ok", "name": name}
    finally:
        conn.close()
@app.get("/api/budget/summary")
async def budget_summary(
    year: int | None = None,
    current_user: dict = Depends(get_app_user),
):
    if year is None:
        year = date.today().year
    if year < 1900 or year > 2200:
        raise HTTPException(status_code=400, detail="Некорректный год")

    start = f"{year:04d}-01-01"
    end = f"{year + 1:04d}-01-01"

    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT
                CAST(strftime('%m', due_date) AS INTEGER) AS month,
                COALESCE(SUM(
                    CASE WHEN item_type = 'income'
                    THEN amount_kopecks ELSE 0 END
                ), 0) AS income_kopecks,
                COALESCE(SUM(
                    CASE WHEN item_type = 'expense'
                    THEN amount_kopecks ELSE 0 END
                ), 0) AS expense_kopecks,
                COALESCE(SUM(
                    CASE WHEN item_type = 'expense' AND status = 'paid'
                    THEN amount_kopecks ELSE 0 END
                ), 0) AS paid_expense_kopecks,
                COALESCE(SUM(
                    CASE WHEN item_type = 'expense' AND mandatory = 1
                    THEN amount_kopecks ELSE 0 END
                ), 0) AS mandatory_expense_kopecks
            FROM budget_items
            WHERE user_id = ? AND due_date >= ? AND due_date < ?
            GROUP BY strftime('%m', due_date)
            """,
            (current_user["id"], start, end),
        ).fetchall()

        by_month = {row["month"]: dict(row) for row in rows}
        months = []

        for month in range(1, 13):
            row = by_month.get(month, {})
            income = int(row.get("income_kopecks", 0))
            expense = int(row.get("expense_kopecks", 0))

            months.append({
                "month": month,
                "income_kopecks": income,
                "expense_kopecks": expense,
                "balance_kopecks": income - expense,
                "paid_expense_kopecks": int(
                    row.get("paid_expense_kopecks", 0)
                ),
                "mandatory_expense_kopecks": int(
                    row.get("mandatory_expense_kopecks", 0)
                ),
            })

        return {"year": year, "months": months}
    finally:
        conn.close()


# === ЗДОРОВЬЕ И ПРОВЕРКА ===
@app.get("/health")
async def health():
    return {"status": "ok", "service": "planner"}

@app.get("/version")
async def version():
    return {"version": "2.0.0", "build": "2026-10-13"}
