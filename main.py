from fastapi import FastAPI, Response, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fpdf import FPDF
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
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
            user_id INTEGER
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
    # Миграция существующей схемы
    user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)")]
    if "role" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
    task_cols = [r["name"] for r in c.execute("PRAGMA table_info(tasks)")]
    if "user_id" not in task_cols:
        c.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")
        # Первый зарегистрированный пользователь становится админом
        # и забирает все существующие задачи себе
        c.execute("UPDATE users SET role='admin' WHERE id = (SELECT MIN(id) FROM users)")
        c.execute("UPDATE tasks SET user_id = (SELECT MIN(id) FROM users) WHERE user_id IS NULL")
    if "archived" not in task_cols:
        c.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER DEFAULT 0")
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
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
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
async def register_user(form_data: OAuth2PasswordRequestForm = Depends()):
    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (form_data.username,)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Пользователь уже существует")
    hashed = get_password_hash(form_data.password)
    conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", (form_data.username, hashed))
    conn.commit()
    conn.close()
    return {"status": "ok", "username": form_data.username}

# === ОСНОВНЫЕ ЭНДПОИНТЫ ===
@app.get("/")
async def root():
    file_path = os.path.join(os.path.dirname(__file__), "index.html")
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
async def update_task(task_id: int, task: dict, current_user: dict = Depends(get_current_user)):
    conn = db()
    row = conn.execute(
        "SELECT status, description FROM tasks WHERE id = ? AND user_id = ?",
        (task_id, current_user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    new_status = task.get("status") or row["status"]
    new_desc = task.get("description") if task.get("description") is not None else row["description"]
    conn.execute(
        "UPDATE tasks SET status = ?, description = ? WHERE id = ? AND user_id = ?",
        (new_status, new_desc, task_id, current_user["id"])
    )
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
    """Убрать в архив все выполненные задачи пользователя"""
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

# === PDF ОТЧЁТ ===
class PDFWithCyrillic(FPDF):
    def __init__(self):
        super().__init__()
        base_path = "/usr/share/fonts/truetype/dejavu"
        self.add_font("DejaVu", "", os.path.join(base_path, "DejaVuSans.ttf"), uni=True)
        self.add_font("DejaVu", "B", os.path.join(base_path, "DejaVuSans-Bold.ttf"), uni=True)

def _get_status(t):
    return t["status"] if isinstance(t, dict) else t.status

def _get_title(t):
    return t["title"] if isinstance(t, dict) else t.title

def generate_report_pdf(tasks):
    pdf = PDFWithCyrillic()
    pdf.add_page()
    pdf.set_font("DejaVu", size=14)
    pdf.cell(0, 10, txt="Work Report", ln=True, align="C")
    pdf.set_font("DejaVu", size=10)
    period = datetime.now().strftime("%d.%m.%Y")
    pdf.cell(0, 8, txt=f"Period: {period}", ln=True, align="C")
    pdf.ln(10)
    col_w = pdf.w / 3 - 10
    pdf.set_fill_color(240, 240, 240)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("DejaVu", "B", size=10)
    pdf.cell(col_w, 8, "TODO", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "IN PROGRESS", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "DONE", border=1, align="C", fill=True, ln=1)
    todo = [t for t in tasks if _get_status(t) == "todo"]
    progress = [t for t in tasks if _get_status(t) == "progress"]
    done = [t for t in tasks if _get_status(t) == "done"]
    max_rows = max(len(todo), len(progress), len(done))
    pdf.set_font("DejaVu", size=9)
    for i in range(max_rows):
        cells = [
            _get_title(todo[i]) if i < len(todo) else "",
            _get_title(progress[i]) if i < len(progress) else "",
            _get_title(done[i]) if i < len(done) else "",
        ]
        for text in cells:
            display = (text[:40] + "...") if text and len(text) > 40 else (text or "")
            pdf.cell(col_w, 6, txt=display, border=1, ln=0, align="L")
        pdf.ln()
    return pdf.output(dest="S")

@app.get("/report")
async def get_report(current_user: dict = Depends(get_current_user)):
    conn = db()
    rows = conn.execute(
        "SELECT id, title, due_date, status, description FROM tasks WHERE user_id = ? AND archived = 0 ORDER BY id",
        (current_user["id"],),
    ).fetchall()
    conn.close()
    tasks = [{
        "id": r["id"],
        "title": r["title"],
        "dueDate": r["due_date"] or "",
        "status": r["status"],
        "description": r["description"] or ""
    } for r in rows]
    try:
        pdf_bytes = generate_report_pdf(tasks).encode("latin-1")
    except Exception as e:
        raise HTTPException(status_code=500, detail="PDF generation failed: " + str(e))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=weekly_report.pdf"}
    )

