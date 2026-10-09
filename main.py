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
        "SELECT id, username, password_hash, role FROM users WHERE username = ?", (username,)
    ).fetchone()
    conn.close()
    if not row or not verify_password(password, row["password_hash"]):
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
    row = conn.execute("SELECT id, username, role FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        raise credentials_exception
    return {"id": row["id"], "username": row["username"], "role": row["role"]}

async def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user

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
@app.get("/admin/users")
async def admin_list_users(admin: dict = Depends(require_admin)):
    conn = db()
    rows = conn.execute("""
        SELECT u.id, u.username, u.role, u.created_at,
               (SELECT COUNT(*) FROM tasks t WHERE t.user_id = u.id) AS task_count
        FROM users u ORDER BY u.id
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

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
@app.get("/api/savings")
async def get_savings(current_user: dict = Depends(get_current_user)):
    conn = db()
    try:
        rows = conn.execute(
            "SELECT day, amount, paid FROM savings WHERE user_id = ? ORDER BY day",
            (current_user["id"],)
        ).fetchall()

        saved = {
            row["day"]: {
                "day": row["day"],
                "amount": row["amount"],
                "paid": bool(row["paid"])
            }
            for row in rows
        }

        return [
            saved.get(day, {"day": day, "amount": 0, "paid": False})
            for day in range(1, 366)
        ]
    finally:
        conn.close()


@app.put("/api/savings/{day}")
async def update_saving(
    day: int,
    item: dict,
    current_user: dict = Depends(get_current_user)
):
    if day < 1 or day > 365:
        raise HTTPException(status_code=400, detail="День должен быть от 1 до 365")

    try:
        amount = float(item.get("amount", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректная сумма")

    if amount < 0:
        raise HTTPException(status_code=400, detail="Сумма не может быть отрицательной")

    paid = 1 if item.get("paid", False) else 0

    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO savings (user_id, day, amount, paid)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, day)
            DO UPDATE SET amount = excluded.amount, paid = excluded.paid
            """,
            (current_user["id"], day, amount, paid)
        )
        conn.commit()
        return {"day": day, "amount": amount, "paid": bool(paid)}
    finally:
        conn.close()


# === ЗДОРОВЬЕ И ПРОВЕРКА ===
@app.get("/health")
async def health():
    return {"status": "ok", "service": "planner"}

@app.get("/version")
async def version():
    return {"version": "1.0.0", "build": "2026-10-09"}
