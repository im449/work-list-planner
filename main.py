from pydantic import BaseModel, Field
from typing import Optional
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import FastAPI, Response, HTTPException, Depends, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fpdf import FPDF
import os
import sqlite3
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
from passlib.context import CryptContext
from jose import JWTError, jwt

# === НАСТРОЙКИ АВТОРИЗАЦИИ ===
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set. Check .env / systemd EnvironmentFile")
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

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        "SELECT id, username, password_hash, role, is_active FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if not row or not row["is_active"] or not verify_password(password, row["password_hash"]):
        return False
    return {"id": row["id"], "username": row["username"], "role": row["role"]}

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить токен",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    conn = db()
    try:
        row = conn.execute(
            "SELECT id, username, role, is_active FROM users WHERE username = ?",
            (username,)
        ).fetchone()

        if not row or not row["is_active"]:
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

async def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user

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
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
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


@app.get("/tasks")
async def get_tasks(archived: bool = False, current_user: dict = Depends(get_current_user)):
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
async def create_task(task: dict, current_user: dict = Depends(get_current_user)):
    title = task.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    due_date = task.get("dueDate", "")
    description = task.get("description", "")
    conn = db()
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
    current_user: dict = Depends(get_current_user)
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
async def delete_task(task_id: int, current_user: dict = Depends(get_current_user)):
    conn = db()
    conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, current_user["id"]))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# === АРХИВ ===
@app.post("/tasks/archive-done")
async def archive_done_tasks(current_user: dict = Depends(get_current_user)):
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
async def archive_task(task_id: int, current_user: dict = Depends(get_current_user)):
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
async def restore_task(task_id: int, current_user: dict = Depends(get_current_user)):
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
async def generate_report(request: Request, current_user: dict = Depends(get_current_user)):
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
):
    name, goal, days = _parse_savings_goal(item)
    start_date, end_date, days = _parse_savings_date_range(item, None, days)
    conn = db()
    try:
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
async def get_savings_summary(current_user: dict = Depends(get_current_user)):
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
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
    current_user: dict = Depends(get_current_user)
):
    if day < 1 or day > 3650:
        raise HTTPException(status_code=400, detail="Некорректный номер дня")
    try:
        amount = float(item.get("amount", 0))
    except (TypeError, ValueError):
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


# === ЗДОРОВЬЕ И ПРОВЕРКА ===
@app.get("/health")
async def health():
    return {"status": "ok", "service": "planner"}

@app.get("/version")
async def version():
    return {"version": "1.0.0", "build": "2026-10-09"}
