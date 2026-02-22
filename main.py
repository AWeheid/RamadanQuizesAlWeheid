from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import json
import uuid
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
import os
import bcrypt
from dotenv import load_dotenv
from pywebpush import webpush, WebPushException

load_dotenv()

app = FastAPI(title="Ramadan Quiz API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ramadan_quiz"
)
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "ramadan2026admin")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@bazl.app")

# Resolve the public directory relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")


# --- DB Setup ---
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            phone TEXT UNIQUE,
            password_hash TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            day INTEGER NOT NULL,
            question_type TEXT NOT NULL DEFAULT 'multiple_choice',
            question_text TEXT NOT NULL,
            options TEXT NOT NULL DEFAULT '[]',
            correct_answer TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            order_num INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS answers (
            id SERIAL PRIMARY KEY,
            participant_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            time_taken INTEGER DEFAULT 30,
            answered_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(participant_id, question_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS quiz_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Default settings
    c.execute(
        "INSERT INTO quiz_settings (key, value) VALUES ('quiz_open_time', '21:00') ON CONFLICT (key) DO NOTHING"
    )
    c.execute(
        "INSERT INTO quiz_settings (key, value) VALUES ('current_day', '1') ON CONFLICT (key) DO NOTHING"
    )
    c.execute(
        "INSERT INTO quiz_settings (key, value) VALUES ('quiz_close_time', '22:45') ON CONFLICT (key) DO NOTHING"
    )
    # Add password_hash column if it doesn't exist (migration for existing DBs)
    c.execute("""
        DO $$ BEGIN
            ALTER TABLE participants ADD COLUMN password_hash TEXT;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
    """)
    # Add points column for speed-based scoring
    c.execute("""
        DO $$ BEGIN
            ALTER TABLE answers ADD COLUMN points INTEGER DEFAULT 0;
        EXCEPTION WHEN duplicate_column THEN NULL;
        END $$;
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            participant_id INTEGER NOT NULL REFERENCES participants(id),
            created_at TIMESTAMP DEFAULT NOW(),
            expires_at TIMESTAMP NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            participant_id INTEGER NOT NULL REFERENCES participants(id),
            subscription_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(participant_id, subscription_json)
        )
    """)
    conn.commit()
    conn.close()


init_db()


# --- Models ---
class RegisterModel(BaseModel):
    name: str
    phone: str
    password: str


class LoginModel(BaseModel):
    phone: str
    password: str


class QuestionModel(BaseModel):
    day: int
    question_type: str = "multiple_choice"
    question_text: str
    options: list[str] = []
    correct_answer: str
    category: str = "general"
    order_num: int = 1


class SettingsModel(BaseModel):
    quiz_open_time: Optional[str] = None
    quiz_close_time: Optional[str] = None
    current_day: Optional[int] = None


# --- Helper ---
def create_session(participant_id: int) -> str:
    session_id = uuid.uuid4().hex
    conn = get_db()
    cur = conn.cursor()
    expires = datetime.now() + timedelta(days=SESSION_DAYS)
    cur.execute(
        "INSERT INTO sessions (id, participant_id, expires_at) VALUES (%s, %s, %s)",
        (session_id, participant_id, expires),
    )
    conn.commit()
    conn.close()
    return session_id


def verify_player(request: Request) -> dict:
    session_id = request.cookies.get("session_id", "")
    if not session_id:
        raise HTTPException(status_code=401, detail="غير مصرّح")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT s.participant_id, s.expires_at, p.name "
        "FROM sessions s JOIN participants p ON s.participant_id = p.id "
        "WHERE s.id = %s",
        (session_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="جلسة غير صالحة")
    if row["expires_at"] < datetime.now():
        # Clean up expired session
        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
        conn2.commit()
        conn2.close()
        raise HTTPException(
            status_code=401, detail="انتهت صلاحية الجلسة، سجّل دخولك مجدداً"
        )
    return {"participant_id": row["participant_id"], "name": row["name"]}


def verify_admin(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    if token != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="Unauthorized")


def get_setting(key: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM quiz_settings WHERE key=%s", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else None


# --- Public Routes ---


@app.get("/api/status")
def get_status():
    """Get current quiz status and countdown info"""
    current_day = int(get_setting("current_day") or 1)
    quiz_time = get_setting("quiz_open_time") or "21:00"

    close_time_str = get_setting("quiz_close_time") or "22:45"

    now = datetime.now()
    hour, minute = map(int, quiz_time.split(":"))
    close_h, close_m = map(int, close_time_str.split(":"))
    quiz_open = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    quiz_close = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

    is_open = quiz_open <= now <= quiz_close

    return {
        "current_day": current_day,
        "quiz_open_time": quiz_time,
        "quiz_close_time": close_time_str,
        "is_open": is_open,
        "server_time": now.isoformat(),
        "total_days": 30,
    }


@app.post("/api/register")
def register(data: RegisterModel):
    conn = get_db()
    cur = conn.cursor()
    try:
        if not data.phone or not data.phone.strip():
            raise HTTPException(status_code=400, detail="رقم الجوال مطلوب للتسجيل")

        phone = data.phone.strip()
        cur.execute("SELECT id FROM participants WHERE phone=%s", (phone,))
        existing = cur.fetchone()
        if existing:
            conn.close()
            raise HTTPException(
                status_code=400, detail="رقم الجوال مسجّل مسبقاً، استخدم تسجيل الدخول"
            )

        pw_hash = bcrypt.hashpw(data.password.encode("utf-8"), bcrypt.gensalt()).decode(
            "utf-8"
        )
        cur.execute(
            "INSERT INTO participants (name, phone, password_hash) VALUES (%s, %s, %s) RETURNING id",
            (data.name.strip(), phone, pw_hash),
        )
        participant_id = cur.fetchone()["id"]
        conn.commit()
        conn.close()
        session_id = create_session(participant_id)
        resp = JSONResponse(
            {"participant_id": participant_id, "name": data.name.strip()}
        )
        resp.set_cookie(
            key="session_id",
            value=session_id,
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/login")
def login(data: LoginModel):
    if not data.phone or not data.phone.strip():
        raise HTTPException(status_code=400, detail="رقم الجوال مطلوب")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, password_hash FROM participants WHERE phone=%s",
        (data.phone.strip(),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="رقم الجوال غير مسجّل، سجّل أولاً")
    if not row["password_hash"] or not bcrypt.checkpw(
        data.password.encode("utf-8"), row["password_hash"].encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="كلمة المرور غير صحيحة")
    session_id = create_session(row["id"])
    resp = JSONResponse({"participant_id": row["id"], "name": row["name"]})
    resp.set_cookie(
        key="session_id",
        value=session_id,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/api/questions/{day}")
def get_questions(day: int):
    """Get questions for a specific day (without correct answers)"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, question_type, question_text, options, category, order_num "
        "FROM questions WHERE day=%s ORDER BY order_num",
        (day,),
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        q = dict(r)
        q["options"] = json.loads(q["options"])
        result.append(q)
    return result


class AnswerPayload(BaseModel):
    question_id: int
    selected_answer: str
    time_taken: int = 30


@app.post("/api/answer")
def submit_answer(data: AnswerPayload, request: Request):
    player = verify_player(request)
    pid = player["participant_id"]
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT correct_answer, question_type FROM questions WHERE id=%s",
            (data.question_id,),
        )
        q = cur.fetchone()
        if not q:
            conn.close()
            raise HTTPException(status_code=404, detail="السؤال غير موجود")

        correct = q["correct_answer"]
        qtype = q["question_type"]

        if qtype == "fill_blank":
            is_correct = (
                1
                if data.selected_answer.strip().lower() == correct.strip().lower()
                else 0
            )
        else:
            is_correct = 1 if data.selected_answer == correct else 0

        # Speed-based scoring: correct = max(10, 100 - time_taken*3), wrong = 0
        points = max(10, 100 - data.time_taken * 3) if is_correct else 0

        cur.execute(
            "INSERT INTO answers (participant_id, question_id, selected_answer, is_correct, time_taken, points) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (participant_id, question_id) DO NOTHING",
            (
                pid,
                data.question_id,
                data.selected_answer,
                is_correct,
                data.time_taken,
                points,
            ),
        )
        conn.commit()
        conn.close()
        return {"status": "ok"}
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/me")
def get_me(request: Request):
    """Get current player info from session cookie"""
    player = verify_player(request)
    return {"participant_id": player["participant_id"], "name": player["name"]}


@app.post("/api/logout")
def logout(request: Request):
    """Clear session cookie and delete session from DB"""
    session_id = request.cookies.get("session_id", "")
    if session_id:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
        conn.commit()
        conn.close()
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("session_id", path="/")
    return resp


@app.get("/api/check-day/{day}")
def check_day(day: int, request: Request):
    """Check if current player already answered questions for a given day"""
    player = verify_player(request)
    pid = player["participant_id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) as cnt FROM answers a "
        "JOIN questions q ON a.question_id = q.id "
        "WHERE a.participant_id=%s AND q.day=%s",
        (pid, day),
    )
    row = cur.fetchone()
    conn.close()
    return {"answered": row["cnt"] > 0, "count": row["cnt"]}


@app.get("/api/my-history")
def my_history(request: Request):
    """Get player's quiz history - which days they answered and how many questions"""
    player = verify_player(request)
    pid = player["participant_id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT q.day,
               COUNT(a.id) as answered_count,
               MIN(a.answered_at) as answered_at
        FROM answers a
        JOIN questions q ON a.question_id = q.id
        WHERE a.participant_id = %s
        GROUP BY q.day
        ORDER BY q.day
    """,
        (pid,),
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("answered_at") and hasattr(d["answered_at"], "isoformat"):
            d["answered_at"] = d["answered_at"].isoformat()
        result.append(d)
    return result


@app.get("/api/my-answers/{day}")
def my_answers(day: int, request: Request):
    """Get player's answers for a specific day with correct answers revealed"""
    player = verify_player(request)
    pid = player["participant_id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT q.id, q.question_text, q.question_type, q.options, q.category, q.order_num,
               q.correct_answer, a.selected_answer, a.is_correct, a.time_taken, a.points
        FROM answers a
        JOIN questions q ON a.question_id = q.id
        WHERE a.participant_id = %s AND q.day = %s
        ORDER BY q.order_num
    """,
        (pid, day),
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"])
        result.append(d)
    return result


@app.get("/api/leaderboard")
def public_leaderboard():
    """Public leaderboard - anonymous (no names), top 10 by points"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COALESCE(SUM(a.points), 0) as points,
            COUNT(a.id) as total_answered
        FROM participants p
        LEFT JOIN answers a ON p.id = a.participant_id
        GROUP BY p.id
        HAVING COUNT(a.id) > 0
        ORDER BY points DESC, total_answered DESC
        LIMIT 10
    """)
    rows = cur.fetchall()
    conn.close()
    result = []
    for i, r in enumerate(rows):
        result.append(
            {
                "rank": i + 1,
                "points": r["points"],
                "total_answered": r["total_answered"],
            }
        )
    return result


# --- Admin Routes ---


@app.get("/api/admin/leaderboard")
def get_leaderboard(request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.id, p.name, p.phone, p.created_at,
               COUNT(a.id) as total_answered,
               COALESCE(SUM(a.points), 0) as points,
               COUNT(DISTINCT CASE WHEN a.is_correct=1 THEN DATE(a.answered_at) END) as days_won
        FROM participants p
        LEFT JOIN answers a ON p.id = a.participant_id
        GROUP BY p.id, p.name, p.phone, p.created_at
        ORDER BY points DESC, total_answered DESC
    """)
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        # Convert datetime to string for JSON serialization
        if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result


@app.get("/api/admin/stats")
def get_stats(request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM participants")
    total_participants = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM answers")
    total_answers = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(is_correct), 0) as c FROM answers")
    correct_answers = cur.fetchone()["c"]
    cur.execute(
        "SELECT COUNT(DISTINCT participant_id) as c FROM answers WHERE DATE(answered_at)=CURRENT_DATE"
    )
    today_participants = cur.fetchone()["c"]
    conn.close()
    return {
        "total_participants": total_participants,
        "total_answers": total_answers,
        "correct_answers": correct_answers,
        "today_participants": today_participants,
        "accuracy_rate": round(
            (correct_answers / total_answers * 100) if total_answers else 0, 1
        ),
    }


@app.post("/api/admin/questions")
def add_question(data: QuestionModel, request: Request):
    verify_admin(request)

    # Validate based on question type
    if data.question_type == "multiple_choice":
        if len(data.options) < 2:
            raise HTTPException(status_code=400, detail="يجب إضافة خيارين على الأقل")
        if len(data.options) > 6:
            raise HTTPException(status_code=400, detail="الحد الأقصى 6 خيارات")
    elif data.question_type == "true_false":
        data.options = ["صح", "خطأ"]
    elif data.question_type == "fill_blank":
        data.options = []
    else:
        raise HTTPException(status_code=400, detail="نوع سؤال غير معروف")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO questions (day, question_type, question_text, options, correct_answer, category, order_num) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            data.day,
            data.question_type,
            data.question_text,
            json.dumps(data.options),
            data.correct_answer,
            data.category,
            data.order_num,
        ),
    )
    qid = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return {"id": qid, "message": "تم إضافة السؤال"}


@app.get("/api/admin/questions/{day}")
def admin_get_questions(day: int, request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM questions WHERE day=%s ORDER BY order_num", (day,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        q = dict(r)
        q["options"] = json.loads(q["options"])
        result.append(q)
    return result


@app.put("/api/admin/questions/{question_id}")
def update_question(question_id: int, data: QuestionModel, request: Request):
    verify_admin(request)

    if data.question_type == "multiple_choice":
        if len(data.options) < 2:
            raise HTTPException(status_code=400, detail="يجب إضافة خيارين على الأقل")
        if len(data.options) > 6:
            raise HTTPException(status_code=400, detail="الحد الأقصى 6 خيارات")
    elif data.question_type == "true_false":
        data.options = ["صح", "خطأ"]
    elif data.question_type == "fill_blank":
        data.options = []
    else:
        raise HTTPException(status_code=400, detail="نوع سؤال غير معروف")

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE questions SET day=%s, question_type=%s, question_text=%s, options=%s, "
        "correct_answer=%s, category=%s, order_num=%s WHERE id=%s",
        (
            data.day,
            data.question_type,
            data.question_text,
            json.dumps(data.options),
            data.correct_answer,
            data.category,
            data.order_num,
            question_id,
        ),
    )
    conn.commit()
    conn.close()
    return {"message": "تم تعديل السؤال"}


@app.delete("/api/admin/questions/{question_id}")
def delete_question(question_id: int, request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM questions WHERE id=%s", (question_id,))
    conn.commit()
    conn.close()
    return {"message": "تم الحذف"}


@app.put("/api/admin/settings")
def update_settings(data: SettingsModel, request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    if data.quiz_open_time:
        cur.execute(
            "INSERT INTO quiz_settings (key, value) VALUES ('quiz_open_time', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (data.quiz_open_time,),
        )
    if data.quiz_close_time:
        cur.execute(
            "INSERT INTO quiz_settings (key, value) VALUES ('quiz_close_time', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (data.quiz_close_time,),
        )
    if data.current_day is not None:
        cur.execute(
            "INSERT INTO quiz_settings (key, value) VALUES ('current_day', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(data.current_day),),
        )
    conn.commit()
    conn.close()
    return {"message": "تم تحديث الإعدادات"}


@app.get("/api/admin/participants")
def get_participants(request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM participants ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        if d.get("created_at") and hasattr(d["created_at"], "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result


@app.get("/api/admin/export")
def export_data(request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT p.name, p.phone, p.created_at,
               q.day, q.question_text, q.question_type, q.category,
               a.selected_answer, q.correct_answer, a.is_correct, a.time_taken, a.answered_at
        FROM answers a
        JOIN participants p ON a.participant_id = p.id
        JOIN questions q ON a.question_id = q.id
        ORDER BY p.name, q.day, q.order_num
    """)
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
        result.append(d)
    return result


# --- Push Notifications ---
@app.get("/api/vapid-public-key")
def get_vapid_public_key():
    return {"public_key": VAPID_PUBLIC_KEY}


class PushSubscriptionModel(BaseModel):
    subscription: dict


@app.post("/api/push/subscribe")
def push_subscribe(data: PushSubscriptionModel, request: Request):
    player = verify_player(request)
    sub_json = json.dumps(data.subscription)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO push_subscriptions (participant_id, subscription_json) "
        "VALUES (%s, %s) ON CONFLICT (participant_id, subscription_json) DO NOTHING",
        (player["participant_id"], sub_json),
    )
    conn.commit()
    conn.close()
    return {"message": "تم تفعيل الإشعارات"}


class PushMessageModel(BaseModel):
    title: str
    body: str


@app.post("/api/admin/push/send")
def admin_send_push(data: PushMessageModel, request: Request):
    verify_admin(request)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, subscription_json FROM push_subscriptions")
    subs = cur.fetchall()
    sent = 0
    failed_ids = []
    for sub in subs:
        try:
            webpush(
                subscription_info=json.loads(sub["subscription_json"]),
                data=json.dumps(
                    {"title": data.title, "body": data.body}, ensure_ascii=False
                ),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
            sent += 1
        except WebPushException as e:
            if e.response and e.response.status_code in (404, 410):
                failed_ids.append(sub["id"])
            else:
                failed_ids.append(sub["id"])
    # Clean up invalid subscriptions
    if failed_ids:
        cur.execute("DELETE FROM push_subscriptions WHERE id = ANY(%s)", (failed_ids,))
    conn.commit()
    conn.close()
    return {"sent": sent, "removed": len(failed_ids)}


# --- Static file serving ---
# PWA assets
@app.get("/manifest.json")
def serve_manifest():
    return FileResponse(
        os.path.join(PUBLIC_DIR, "manifest.json"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def serve_sw():
    return FileResponse(
        os.path.join(PUBLIC_DIR, "sw.js"), media_type="application/javascript"
    )


# Serve icons directory
app.mount(
    "/icons", StaticFiles(directory=os.path.join(PUBLIC_DIR, "icons")), name="icons"
)


# Serve competion.html at /competion
@app.get("/competion")
def serve_competion():
    return FileResponse(os.path.join(PUBLIC_DIR, "competion.html"))


# Serve admin.html at /admin
@app.get("/admin")
def serve_admin():
    return FileResponse(os.path.join(PUBLIC_DIR, "admin.html"))


# Serve index.html at root with injected session data
@app.get("/")
def serve_index(request: Request):
    session_id = request.cookies.get("session_id", "")
    session_data = "null"
    if session_id:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(
                "SELECT s.participant_id, s.expires_at, p.name "
                "FROM sessions s JOIN participants p ON s.participant_id = p.id "
                "WHERE s.id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row and row["expires_at"] >= datetime.now():
                session_data = json.dumps(
                    {"participant_id": row["participant_id"], "name": row["name"]}
                )
        except Exception:
            pass
    html_path = os.path.join(PUBLIC_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    html = html.replace(
        "<script>",
        f"<script>window.__SESSION__={session_data};",
        1,
    )
    return HTMLResponse(html)
