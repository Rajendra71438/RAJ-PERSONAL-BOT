#!/usr/bin/env python3
"""
Personal Telegram Assistant - ULTIMATE VERSION
- QR login in terminal (no web panel needed for login)
- Direct broadcast preserves original filenames (APK, etc.)
- Step messages preserve premium emojis, captions, formatting
- Green/Orange theme
"""

import asyncio
import sqlite3
import json
import os
import threading
import time
import secrets
import tempfile
import shutil
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, request, jsonify, session
from flask_cors import CORS
import bcrypt
from telethon import TelegramClient, events, errors
import dotenv
import logging

# QR library
try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    print("⚠️ qrcode not installed. Install: pip install qrcode[pil]")

logging.getLogger('werkzeug').disabled = True
logging.getLogger('telethon').setLevel(logging.ERROR)

def print_neon(text, color='green'):
    codes = {'green': '\033[92m', 'orange': '\033[93m', 'cyan': '\033[96m', 'red': '\033[91m'}
    reset = '\033[0m'
    bold = '\033[1m'
    print(f"{bold}{codes.get(color, codes['green'])}{text}{reset}")

dotenv.load_dotenv()

API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "").strip()  # empty = QR
SOURCE_CHAT_ID = int(os.getenv("SOURCE_CHAT_ID", 0))
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(24))
PORT = int(os.getenv("PORT", 5000))

if not all([API_ID, API_HASH, SOURCE_CHAT_ID]):
    raise ValueError("Missing API_ID, API_HASH, or SOURCE_CHAT_ID in .env")

DB_PATH = "bot.db"
app = Flask(__name__)
app.secret_key = SECRET_KEY
CORS(app)

# ==================== DATABASE (all functions) ====================
def get_db():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                step INTEGER DEFAULT 1,
                last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                blocked BOOLEAN DEFAULT 0,
                is_agent BOOLEAN DEFAULT 0,
                joined_date DATE DEFAULT CURRENT_DATE
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step INTEGER NOT NULL,
                order_within_step INTEGER NOT NULL,
                type TEXT NOT NULL,
                source_message_id INTEGER NOT NULL,
                content TEXT,
                caption TEXT,
                UNIQUE(step, order_within_step)
            );
            CREATE TABLE IF NOT EXISTS steps (
                step_number INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS subadmins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                permissions TEXT NOT NULL,
                is_main BOOLEAN DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_broadcast (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT,
                content TEXT,
                caption TEXT,
                link_preview BOOLEAN DEFAULT 1,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        try:
            db.execute("ALTER TABLE users ADD COLUMN is_agent BOOLEAN DEFAULT 0")
        except:
            pass
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_active', '1')")
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('reset_days', '0')")
        if not db.execute("SELECT id FROM subadmins WHERE is_main=1").fetchone():
            hashed = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
            perms = json.dumps({"stats":True,"messages":True,"broadcast":True,"blocked":True,"settings":True,"main":True})
            db.execute("INSERT INTO subadmins (username, password_hash, permissions, is_main) VALUES (?,?,?,1)",
                       ("admin", hashed, perms))
    print_neon("✅ Database initialized", 'green')

def get_bot_active():
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='bot_active'").fetchone()
        return row["value"] == "1"

def set_bot_active(active):
    with get_db() as db:
        db.execute("UPDATE settings SET value=? WHERE key='bot_active'", ("1" if active else "0"))

def get_reset_days():
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key='reset_days'").fetchone()
        return int(row["value"]) if row else 0

def set_reset_days(days):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reset_days', ?)", (str(days),))

def get_user_last_active(user_id):
    with get_db() as db:
        row = db.execute("SELECT last_active FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["last_active"] if row else None

def upsert_user(user_id, first_name, username):
    with get_db() as db:
        db.execute("""INSERT INTO users (user_id, first_name, username, last_active) VALUES (?,?,?,?)
                      ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username, last_active=excluded.last_active""",
                   (user_id, first_name or "", username or "", datetime.now()))

def get_user_step(user_id):
    with get_db() as db:
        row = db.execute("SELECT step FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["step"] if row else 1

def set_user_step(user_id, step):
    with get_db() as db:
        db.execute("UPDATE users SET step=? WHERE user_id=?", (step, user_id))

def is_user_blocked(user_id):
    with get_db() as db:
        row = db.execute("SELECT blocked FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["blocked"] if row else False

def is_user_agent(user_id):
    with get_db() as db:
        row = db.execute("SELECT is_agent FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["is_agent"] if row else False

def set_user_agent(user_id, is_agent):
    with get_db() as db:
        db.execute("UPDATE users SET is_agent=? WHERE user_id=?", (1 if is_agent else 0, user_id))

def get_all_users(include_blocked=False):
    with get_db() as db:
        if include_blocked:
            return db.execute("SELECT user_id, first_name, username, step, blocked, is_agent, joined_date, last_active FROM users WHERE is_agent=0").fetchall()
        return db.execute("SELECT user_id, first_name, username, step, blocked, is_agent, joined_date, last_active FROM users WHERE blocked=0 AND is_agent=0").fetchall()

def get_agents():
    with get_db() as db:
        return db.execute("SELECT user_id, first_name, username, joined_date, last_active FROM users WHERE is_agent=1").fetchall()

def get_blocked_users():
    with get_db() as db:
        return db.execute("SELECT user_id, first_name, username FROM users WHERE blocked=1 AND is_agent=0").fetchall()

def block_user(user_id):
    with get_db() as db:
        db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))

def unblock_user(user_id):
    with get_db() as db:
        db.execute("UPDATE users SET blocked=0 WHERE user_id=?", (user_id,))

def delete_user(user_id):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE user_id=?", (user_id,))

def get_stats():
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM users WHERE is_agent=0").fetchone()[0]
        today = db.execute("SELECT COUNT(*) FROM users WHERE joined_date=? AND is_agent=0", (date.today().isoformat(),)).fetchone()[0]
        blocked = db.execute("SELECT COUNT(*) FROM users WHERE blocked=1 AND is_agent=0").fetchone()[0]
        agents = db.execute("SELECT COUNT(*) FROM users WHERE is_agent=1").fetchone()[0]
        return total, today, blocked, agents

def add_step(step):
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO steps (step_number) VALUES (?)", (step,))

def delete_step(step):
    with get_db() as db:
        db.execute("DELETE FROM steps WHERE step_number=?", (step,))
        db.execute("DELETE FROM messages WHERE step=?", (step,))

def get_all_steps():
    with get_db() as db:
        steps = db.execute("SELECT step_number FROM steps ORDER BY step_number").fetchall()
        result = []
        for s in steps:
            msgs = db.execute("SELECT * FROM messages WHERE step=? ORDER BY order_within_step", (s["step_number"],)).fetchall()
            result.append({"step": s["step_number"], "messages": [dict(m) for m in msgs]})
        return result

def add_message(step, order, msg_type, source_message_id, content="", caption=""):
    with get_db() as db:
        db.execute("UPDATE messages SET order_within_step = order_within_step + 1 WHERE step=? AND order_within_step >= ?", (step, order))
        db.execute("INSERT INTO messages (step, order_within_step, type, source_message_id, content, caption) VALUES (?,?,?,?,?,?)",
                   (step, order, msg_type, source_message_id, content, caption))

def delete_message(msg_id):
    with get_db() as db:
        row = db.execute("SELECT step, order_within_step FROM messages WHERE id=?", (msg_id,)).fetchone()
        if row:
            db.execute("DELETE FROM messages WHERE id=?", (msg_id,))
            db.execute("UPDATE messages SET order_within_step = order_within_step - 1 WHERE step=? AND order_within_step > ?", (row["step"], row["order_within_step"]))

def update_message(msg_id, msg_type, source_message_id, content="", caption=""):
    with get_db() as db:
        db.execute("UPDATE messages SET type=?, source_message_id=?, content=?, caption=? WHERE id=?", (msg_type, source_message_id, content, caption, msg_id))

def create_broadcast(bcast_type, content, caption="", link_preview=True):
    with get_db() as db:
        db.execute("INSERT INTO pending_broadcast (type, content, caption, link_preview, status) VALUES (?,?,?,?,'pending')",
                   (bcast_type, content, caption, 1 if link_preview else 0))

def get_pending_broadcast():
    with get_db() as db:
        return db.execute("SELECT * FROM pending_broadcast WHERE status='pending' ORDER BY id LIMIT 1").fetchone()

def mark_broadcast_done(bcast_id):
    with get_db() as db:
        db.execute("UPDATE pending_broadcast SET status='done' WHERE id=?", (bcast_id,))

def mark_broadcast_cancelled(bcast_id):
    with get_db() as db:
        db.execute("UPDATE pending_broadcast SET status='cancelled' WHERE id=?", (bcast_id,))

def cancel_all_pending_broadcasts():
    with get_db() as db:
        db.execute("UPDATE pending_broadcast SET status='cancelled' WHERE status='pending'")

def get_broadcast_history():
    with get_db() as db:
        return db.execute("SELECT * FROM pending_broadcast ORDER BY id DESC LIMIT 50").fetchall()

def delete_broadcast_history(only_done=False):
    with get_db() as db:
        if only_done:
            rows = db.execute("SELECT content FROM pending_broadcast WHERE status='done' AND type != 'text'").fetchall()
            for row in rows:
                path = row["content"]
                if path and os.path.exists(path):
                    try:
                        if os.path.isdir(path):
                            shutil.rmtree(path)
                        else:
                            os.remove(path)
                    except:
                        pass
            db.execute("DELETE FROM pending_broadcast WHERE status='done'")
        else:
            db.execute("DELETE FROM pending_broadcast")

def delete_broadcast_by_id(bcast_id):
    with get_db() as db:
        row = db.execute("SELECT content, type FROM pending_broadcast WHERE id=?", (bcast_id,)).fetchone()
        if row and row["type"] != "text" and row["content"] and os.path.exists(row["content"]):
            try:
                if os.path.isdir(row["content"]):
                    shutil.rmtree(row["content"])
                else:
                    os.remove(row["content"])
            except:
                pass
        db.execute("DELETE FROM pending_broadcast WHERE id=?", (bcast_id,))

def get_subadmin_by_username(username):
    with get_db() as db:
        row = db.execute("SELECT id, username, password_hash, permissions, is_main FROM subadmins WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

def get_subadmin_by_id(uid):
    with get_db() as db:
        row = db.execute("SELECT id, username, password_hash, permissions, is_main FROM subadmins WHERE id=?", (uid,)).fetchone()
        return dict(row) if row else None

def create_subadmin(username, password, permissions):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    perms_json = json.dumps(permissions)
    with get_db() as db:
        try:
            db.execute("INSERT INTO subadmins (username, password_hash, permissions, is_main) VALUES (?,?,?,0)", (username, hashed, perms_json))
            return True
        except sqlite3.IntegrityError:
            return False

def delete_subadmin(uid):
    with get_db() as db:
        db.execute("DELETE FROM subadmins WHERE id=? AND is_main=0", (uid,))

def list_subadmins():
    with get_db() as db:
        rows = db.execute("SELECT id, username, permissions, is_main FROM subadmins").fetchall()
        return [{"id": r["id"], "username": r["username"], "permissions": json.loads(r["permissions"]), "is_main": r["is_main"]} for r in rows]

def change_subadmin_password(uid, new_password):
    hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    with get_db() as db:
        db.execute("UPDATE subadmins SET password_hash=? WHERE id=?", (hashed, uid))

# ==================== TELEGRAM CLIENT ====================
client = TelegramClient('personal_session', API_ID, API_HASH)
client_loop = asyncio.new_event_loop()

cancel_broadcast_event = None
temp_dirs_to_clean = set()

def cleanup_temp_dirs():
    for d in list(temp_dirs_to_clean):
        if os.path.exists(d):
            try:
                shutil.rmtree(d)
            except:
                pass
    temp_dirs_to_clean.clear()

async def send_content(user_id, msg):
    try:
        await client.copy_message(user_id, SOURCE_CHAT_ID, msg["source_message_id"])
        return True
    except:
        try:
            await client.forward_messages(user_id, msg["source_message_id"], SOURCE_CHAT_ID)
            return True
        except:
            try:
                original = await client.get_messages(SOURCE_CHAT_ID, ids=msg["source_message_id"])
                if original:
                    if msg["type"] == "text":
                        await client.send_message(user_id, original.text)
                    elif msg["type"] == "voice":
                        await client.send_file(user_id, original.media, voice_note=True)
                    else:
                        await client.send_file(user_id, original.media, caption=msg["caption"] or original.text or "")
                    return True
            except:
                pass
        return False

async def send_sequence_to_user(user_id):
    step = get_user_step(user_id)
    with get_db() as db:
        messages = db.execute("SELECT * FROM messages WHERE step=? ORDER BY order_within_step", (step,)).fetchall()
    if not messages:
        with get_db() as db:
            nxt = db.execute("SELECT MIN(step_number) as ns FROM steps WHERE step_number > ?", (step,)).fetchone()
            if nxt and nxt["ns"]:
                set_user_step(user_id, nxt["ns"])
                await send_sequence_to_user(user_id)
        return
    for msg in messages:
        if not await send_content(user_id, msg):
            break
    set_user_step(user_id, step + 1)

async def broadcast_worker():
    global cancel_broadcast_event
    while True:
        bcast = get_pending_broadcast()
        if bcast:
            users = get_all_users(include_blocked=False)
            file_path = bcast["content"] if bcast["type"] != "text" and bcast["content"] and os.path.exists(bcast["content"]) else None
            for user in users:
                if cancel_broadcast_event and cancel_broadcast_event.is_set():
                    mark_broadcast_cancelled(bcast["id"])
                    cancel_broadcast_event.clear()
                    break
                try:
                    if bcast["type"] == "text":
                        await client.send_message(user["user_id"], bcast["content"], link_preview=bool(bcast["link_preview"]))
                    elif file_path:
                        await client.send_file(user["user_id"], file_path, caption=bcast["caption"] or "", voice_note=(bcast["type"] == "voice"))
                except:
                    pass
                await asyncio.sleep(0.05)
            if file_path and os.path.exists(file_path):
                try:
                    if os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                    else:
                        os.remove(file_path)
                    temp_dirs_to_clean.discard(file_path)
                except:
                    pass
            if not (cancel_broadcast_event and cancel_broadcast_event.is_set()):
                mark_broadcast_done(bcast["id"])
            else:
                cancel_broadcast_event.clear()
        await asyncio.sleep(2)

@client.on(events.NewMessage(incoming=True))
async def handle_incoming(event):
    if event.is_private and not event.out:
        user_id = event.sender_id
        if is_user_blocked(user_id) or is_user_agent(user_id) or not get_bot_active():
            return
        sender = await event.get_sender()
        reset_days = get_reset_days()
        last_active = get_user_last_active(user_id)
        with get_db() as db:
            existing = db.execute("SELECT user_id, step FROM users WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            current_step = existing["step"]
            with get_db() as db:
                max_step = db.execute("SELECT MAX(step_number) FROM steps").fetchone()[0]
            if reset_days == 0:
                if max_step and current_step > max_step:
                    set_user_step(user_id, 1)
            elif reset_days > 0 and last_active:
                days_since = (datetime.now() - last_active).days
                if days_since >= reset_days:
                    set_user_step(user_id, 1)
        upsert_user(user_id, sender.first_name, sender.username)
        await send_sequence_to_user(user_id)

async def qr_login_terminal():
    """Print QR code to terminal and handle 2FA"""
    print_neon("\n📱 QR Code Login", 'orange')
    print_neon("No PHONE_NUMBER in .env. Using QR login...", 'cyan')
    try:
        qr = await client.qr_login()
        if QR_AVAILABLE:
            # Generate QR image
            img = qrcode.make(qr.url)
            img.save("qr_login.png")
            print_neon("✅ QR code saved as 'qr_login.png'", 'green')
            # Print ASCII QR code
            try:
                # Create ASCII QR (smaller for terminal)
                qr_ascii = qrcode.make(qr.url, box_size=1, border=1)
                lines = []
                for y in range(qr_ascii.size[1]):
                    line = ""
                    for x in range(qr_ascii.size[0]):
                        line += "██" if qr_ascii.getpixel((x, y)) else "  "
                    lines.append(line)
                ascii_qr = "\n".join(lines)
                print(ascii_qr)
            except:
                pass
        else:
            print_neon(f"Scan this URL with Telegram: {qr.url}", 'orange')
        print_neon("Waiting for scan...", 'cyan')
        try:
            await qr.wait_for_login(timeout=None)
        except Exception as e:
            if "2FA" in str(e) or "password" in str(e).lower():
                pwd = input("Enter your 2FA password: ").strip()
                await qr.login(pwd)
            else:
                raise
        print_neon("✅ QR login successful!", 'green')
    except Exception as e:
        print_neon(f"QR login error: {e}", 'red')
        raise

async def start_telegram():
    global cancel_broadcast_event
    cancel_broadcast_event = asyncio.Event()
    # Check if we already have a valid session
    if await client.is_user_authorized():
        print_neon("✅ Using existing Telegram session", 'green')
    else:
        if PHONE_NUMBER:
            print_neon(f"Logging in with phone: {PHONE_NUMBER}", 'orange')
            await client.start(phone=PHONE_NUMBER)
        else:
            await qr_login_terminal()
    asyncio.create_task(broadcast_worker())
    await client.run_until_disconnected()

def start_client():
    asyncio.set_event_loop(client_loop)
    client_loop.run_until_complete(start_telegram())

# ==================== FLASK API (all endpoints) ====================
def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = get_subadmin_by_username(data['username'])
    if user and bcrypt.checkpw(data['password'].encode(), user['password_hash'].encode()):
        session['user_id'] = user['id']
        session['is_main'] = user['is_main']
        session['username'] = user['username']
        return jsonify({"message": "ok", "is_main": user['is_main'], "username": user['username']})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
@auth_required
def logout():
    session.clear()
    return jsonify({"message": "ok"})

@app.route('/api/current_user', methods=['GET'])
@auth_required
def current_user():
    return jsonify({"id": session['user_id'], "is_main": session.get('is_main', False), "username": session.get('username', '')})

@app.route('/api/stats', methods=['GET'])
@auth_required
def stats():
    total, today, blocked, agents = get_stats()
    return jsonify({"total": total, "today": today, "blocked": blocked, "agents": agents})

@app.route('/api/bot_active', methods=['GET'])
@auth_required
def bot_active():
    return jsonify({"active": get_bot_active()})

@app.route('/api/toggle_bot', methods=['POST'])
@auth_required
def toggle_bot():
    new_val = not get_bot_active()
    set_bot_active(new_val)
    return jsonify({"active": new_val})

@app.route('/api/users', methods=['GET'])
@auth_required
def users():
    users = get_all_users(include_blocked=True)
    return jsonify([dict(u) for u in users])

@app.route('/api/delete_user/<int:user_id>', methods=['DELETE'])
@auth_required
def delete_user_route(user_id):
    delete_user(user_id)
    return jsonify({"ok": True})

@app.route('/api/blocked', methods=['GET'])
@auth_required
def blocked_list():
    users = get_blocked_users()
    return jsonify([dict(u) for u in users])

@app.route('/api/block', methods=['POST'])
@auth_required
def block():
    data = request.json
    block_user(data['user_id'])
    return jsonify({"ok": True})

@app.route('/api/unblock', methods=['POST'])
@auth_required
def unblock():
    data = request.json
    unblock_user(data['user_id'])
    return jsonify({"ok": True})

@app.route('/api/block_by_identifier', methods=['POST'])
@auth_required
def block_by_identifier():
    identifier = request.json['identifier']
    with get_db() as db:
        if identifier.isdigit():
            row = db.execute("SELECT user_id FROM users WHERE user_id=?", (int(identifier),)).fetchone()
        else:
            row = db.execute("SELECT user_id FROM users WHERE username=?", (identifier,)).fetchone()
        if row:
            block_user(row['user_id'])
            return jsonify({"ok": True})
    return jsonify({"error": "User not found"}), 404

@app.route('/api/agents', methods=['GET'])
@auth_required
def agents():
    agents = get_agents()
    return jsonify([dict(a) for a in agents])

@app.route('/api/set_agent', methods=['POST'])
@auth_required
def set_agent():
    data = request.json
    set_user_agent(data['user_id'], True)
    return jsonify({"ok": True})

@app.route('/api/remove_agent', methods=['POST'])
@auth_required
def remove_agent():
    data = request.json
    set_user_agent(data['user_id'], False)
    return jsonify({"ok": True})

@app.route('/api/steps', methods=['GET'])
@auth_required
def get_steps():
    steps = get_all_steps()
    return jsonify(steps)

@app.route('/api/add_step', methods=['POST'])
@auth_required
def add_step_route():
    step = request.json['step']
    add_step(step)
    return jsonify({"ok": True})

@app.route('/api/delete_step/<int:step>', methods=['DELETE'])
@auth_required
def delete_step_route(step):
    delete_step(step)
    return jsonify({"ok": True})

@app.route('/api/add_message', methods=['POST'])
@auth_required
def add_message_route():
    step = int(request.form['step'])
    order = int(request.form['order'])
    msg_type = request.form['msg_type']
    caption = request.form.get('caption', '')
    if msg_type == 'text':
        text = request.form['text_content']
        async def send_text():
            msg = await client.send_message(SOURCE_CHAT_ID, text)
            return msg.id
        msg_id = asyncio.run_coroutine_threadsafe(send_text(), client_loop).result()
        add_message(step, order, msg_type, msg_id, content=text, caption=caption)
    elif msg_type == 'voice':
        file = request.files['media_file']
        async def send_voice():
            temp_path = f"/tmp/{file.filename}"
            file.save(temp_path)
            msg = await client.send_file(SOURCE_CHAT_ID, temp_path, voice_note=True)
            os.remove(temp_path)
            return msg.id
        msg_id = asyncio.run_coroutine_threadsafe(send_voice(), client_loop).result()
        add_message(step, order, msg_type, msg_id, content="", caption="")
    else:
        file = request.files['media_file']
        async def send_media():
            temp_path = f"/tmp/{file.filename}"
            file.save(temp_path)
            msg = await client.send_file(SOURCE_CHAT_ID, temp_path, caption=caption)
            os.remove(temp_path)
            return msg.id
        msg_id = asyncio.run_coroutine_threadsafe(send_media(), client_loop).result()
        add_message(step, order, msg_type, msg_id, content="", caption=caption)
    return jsonify({"ok": True})

@app.route('/api/edit_message', methods=['POST'])
@auth_required
def edit_message_route():
    msg_id = int(request.form['msg_id'])
    msg_type = request.form['msg_type']
    caption = request.form.get('caption', '')
    content = ""
    new_source_id = None
    if msg_type == 'text':
        text = request.form['text_content']
        content = text
        async def send_new():
            msg = await client.send_message(SOURCE_CHAT_ID, text)
            return msg.id
        new_source_id = asyncio.run_coroutine_threadsafe(send_new(), client_loop).result()
    elif msg_type == 'voice':
        file = request.files.get('media_file')
        if file:
            async def send_new():
                temp_path = f"/tmp/{file.filename}"
                file.save(temp_path)
                msg = await client.send_file(SOURCE_CHAT_ID, temp_path, voice_note=True)
                os.remove(temp_path)
                return msg.id
            new_source_id = asyncio.run_coroutine_threadsafe(send_new(), client_loop).result()
        else:
            with get_db() as db:
                row = db.execute("SELECT source_message_id FROM messages WHERE id=?", (msg_id,)).fetchone()
                new_source_id = row["source_message_id"]
    else:
        file = request.files.get('media_file')
        if file:
            async def send_new():
                temp_path = f"/tmp/{file.filename}"
                file.save(temp_path)
                msg = await client.send_file(SOURCE_CHAT_ID, temp_path, caption=caption)
                os.remove(temp_path)
                return msg.id
            new_source_id = asyncio.run_coroutine_threadsafe(send_new(), client_loop).result()
        else:
            with get_db() as db:
                row = db.execute("SELECT source_message_id FROM messages WHERE id=?", (msg_id,)).fetchone()
                new_source_id = row["source_message_id"]
    update_message(msg_id, msg_type, new_source_id, content=content, caption=caption)
    return jsonify({"ok": True})

@app.route('/api/delete_message/<int:msg_id>', methods=['DELETE'])
@auth_required
def delete_message_route(msg_id):
    delete_message(msg_id)
    return jsonify({"ok": True})

@app.route('/api/broadcast', methods=['POST'])
@auth_required
def broadcast():
    msg_type = request.form['msg_type']
    caption = request.form.get('caption', '')
    link_url = request.form.get('link_url', '')
    above_text = request.form.get('above_text', '')
    below_text = request.form.get('below_text', '')
    disable_preview = request.form.get('disable_link_preview', 'false').lower() == 'true'
    if msg_type == 'text':
        if link_url:
            combined = (above_text.strip() + "\n" + link_url.strip() + "\n" + below_text.strip()).strip()
        else:
            combined = request.form.get('text_content', '')
        create_broadcast('text', combined, caption, link_preview=not disable_preview)
    else:
        file = request.files['media_file']
        temp_dir = tempfile.mkdtemp(prefix="broadcast_")
        safe_filename = os.path.basename(file.filename)
        file_path = os.path.join(temp_dir, safe_filename)
        file.save(file_path)
        temp_dirs_to_clean.add(temp_dir)
        create_broadcast(msg_type, file_path, caption, link_preview=True)
    return jsonify({"ok": True})

@app.route('/api/broadcast_history', methods=['GET'])
@auth_required
def broadcast_history():
    hist = get_broadcast_history()
    return jsonify([dict(h) for h in hist])

@app.route('/api/broadcast/cancel', methods=['POST'])
@auth_required
def cancel_broadcasts():
    global cancel_broadcast_event
    cancel_all_pending_broadcasts()
    if cancel_broadcast_event:
        asyncio.run_coroutine_threadsafe(cancel_broadcast_event.set(), client_loop)
    return jsonify({"ok": True})

@app.route('/api/broadcast/clear_history', methods=['DELETE'])
@auth_required
def clear_broadcast_history():
    hist = get_broadcast_history()
    for h in hist:
        if h["type"] != "text" and h["content"] and os.path.exists(h["content"]):
            try:
                if os.path.isdir(h["content"]):
                    shutil.rmtree(h["content"])
                else:
                    os.remove(h["content"])
            except:
                pass
    delete_broadcast_history(only_done=True)
    return jsonify({"ok": True})

@app.route('/api/broadcast/<int:bcast_id>', methods=['DELETE'])
@auth_required
def delete_broadcast(bcast_id):
    delete_broadcast_by_id(bcast_id)
    return jsonify({"ok": True})

@app.route('/api/change_password', methods=['POST'])
@auth_required
def change_password():
    data = request.json
    uid = session['user_id']
    user = get_subadmin_by_id(uid)
    if bcrypt.checkpw(data['old_password'].encode(), user['password_hash'].encode()):
        change_subadmin_password(uid, data['new_password'])
        return jsonify({"message": "Password changed successfully"})
    return jsonify({"error": "Wrong old password"}), 400

@app.route('/api/subadmins', methods=['GET'])
@auth_required
def subadmins():
    if not session.get('is_main'):
        return jsonify({"error": "Not authorized"}), 403
    subs = list_subadmins()
    return jsonify(subs)

@app.route('/api/add_subadmin', methods=['POST'])
@auth_required
def add_subadmin():
    if not session.get('is_main'):
        return jsonify({"error": "Not authorized"}), 403
    data = request.json
    ok = create_subadmin(data['username'], data['password'], data['permissions'])
    return jsonify({"ok": ok})

@app.route('/api/subadmin/<int:sub_id>', methods=['DELETE'])
@auth_required
def del_subadmin(sub_id):
    if not session.get('is_main'):
        return jsonify({"error": "Not authorized"}), 403
    delete_subadmin(sub_id)
    return jsonify({"ok": True})

@app.route('/api/env', methods=['GET'])
@auth_required
def get_env():
    if not session.get('is_main'):
        return jsonify({"error": "Not authorized"}), 403
    return jsonify({"API_ID": API_ID, "API_HASH": API_HASH, "PHONE_NUMBER": PHONE_NUMBER, "SOURCE_CHAT_ID": SOURCE_CHAT_ID, "PORT": PORT})

@app.route('/api/update_env', methods=['POST'])
@auth_required
def update_env():
    if not session.get('is_main'):
        return jsonify({"error": "Not authorized"}), 403
    data = request.json
    content = f"""API_ID={data['API_ID']}
API_HASH={data['API_HASH']}
PHONE_NUMBER={data['PHONE_NUMBER']}
SOURCE_CHAT_ID={data['SOURCE_CHAT_ID']}
SECRET_KEY={SECRET_KEY}
PORT={data['PORT']}
"""
    with open('.env', 'w') as f:
        f.write(content)
    return jsonify({"message": "Environment updated. Restart required."})

@app.route('/api/reset_days', methods=['GET'])
@auth_required
def get_reset_days_api():
    return jsonify({"days": get_reset_days()})

@app.route('/api/reset_days', methods=['POST'])
@auth_required
def set_reset_days_api():
    data = request.json
    days = int(data.get('days', 0))
    set_reset_days(days)
    return jsonify({"days": days, "message": "Reset days updated."})

# ==================== HTML DASHBOARD ====================
# (Full HTML embedded – same as before, but for brevity we include it completely)
# In this final answer, the HTML is the same as the previous full version.
# I'll include a placeholder here but in the actual final code it's all there.
# For the user to copy, I will provide the complete HTML in the final answer.

# Due to length, I'm not repeating the 500+ lines of HTML here. 
# The final answer will contain the full HTML exactly as in the previous message.

# For now, I'll just use a minimal HTML to avoid truncation, but the actual final code will have the full dashboard.
# The user will receive the full code via the final message.

# ==================== HTML DASHBOARD ====================
HTML_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>BOT CONTROL · ORANGE/GREEN</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
:root {
  --bg: #0a0f0f;
  --bg2: #0f1414;
  --bg3: #141a1a;
  --card: #0f1818;
  --card2: #142020;
  --border: #1e2a2a;
  --border2: #2a3a3a;
  --green: #10b981;
  --green-bright: #34d399;
  --orange: #f97316;
  --orange-bright: #fb923c;
  --red: #ef4444;
  --yellow: #fbbf24;
  --purple: #8b5cf6;
  --text: #e2e8f0;
  --text2: #94a3b8;
  --text3: #64748b;
  --mono: 'Space Mono', monospace;
  --sans: 'DM Sans', sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; }
body { background: var(--bg); color: var(--text); font-family: var(--sans); display: flex; }

/* Sidebar */
.sidebar {
  width: 260px;
  min-width: 260px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  position: relative;
  z-index: 10;
  transition: left 0.3s ease;
}
.sidebar::after {
  content: '';
  position: absolute;
  top: 0; right: 0;
  width: 1px; height: 100%;
  background: linear-gradient(to bottom, transparent, var(--orange), transparent);
  opacity: 0.4;
}
.sidebar-brand {
  padding: 28px 24px 20px;
  border-bottom: 1px solid var(--border);
}
.brand-label {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 3px;
  color: var(--orange);
  text-transform: uppercase;
  margin-bottom: 4px;
}
.brand-title {
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
  letter-spacing: 1px;
}
.brand-title span { color: var(--green); }
.brand-sub {
  font-size: 11px;
  color: var(--text3);
  margin-top: 4px;
  font-family: var(--mono);
}
.nav-section {
  padding: 16px 12px 8px;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 2.5px;
  color: var(--text3);
  text-transform: uppercase;
}
.nav-list { list-style: none; padding: 0 8px; }
.nav-item { margin-bottom: 2px; }
.nav-link {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 10px;
  color: var(--text2);
  text-decoration: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  position: relative;
  border: 1px solid transparent;
}
.nav-link:hover { background: var(--card); color: var(--text); border-color: var(--border); }
.nav-link.active {
  background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(249,115,22,0.08));
  color: var(--green);
  border-color: rgba(16,185,129,0.3);
  box-shadow: inset 0 0 20px rgba(16,185,129,0.05);
}
.nav-link.active .nav-icon { color: var(--green); }
.nav-link .nav-icon { width: 18px; font-size: 13px; }
.nav-badge {
  margin-left: auto;
  background: var(--green);
  color: var(--bg);
  font-family: var(--mono);
  font-size: 9px;
  padding: 2px 7px;
  border-radius: 20px;
  font-weight: 700;
}
.nav-badge.red { background: var(--red); }
.nav-badge.green { background: var(--green); color: var(--bg); }
.sidebar-footer {
  margin-top: auto;
  padding: 16px 12px;
  border-top: 1px solid var(--border);
}
.user-chip {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--card);
  border-radius: 10px;
  border: 1px solid var(--border);
  margin-bottom: 8px;
}
.user-avatar {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--green), var(--orange));
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 700;
  color: var(--bg);
  flex-shrink: 0;
}
.user-info { flex: 1; min-width: 0; }
.user-name { font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.user-role { font-size: 10px; font-family: var(--mono); color: var(--orange); letter-spacing: 1px; }
.logout-btn {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  border-radius: 10px;
  color: var(--text3);
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all 0.2s;
  border: 1px solid transparent;
}
.logout-btn:hover { background: rgba(239,68,68,0.1); color: var(--red); border-color: rgba(239,68,68,0.3); }

/* Main */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}
.topbar {
  height: 64px;
  min-height: 64px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 32px;
  gap: 16px;
}
.menu-toggle {
  display: none;
  background: transparent;
  border: none;
  color: var(--text2);
  font-size: 20px;
  cursor: pointer;
  padding: 4px;
}
.page-title {
  font-family: var(--mono);
  font-size: 13px;
  color: var(--text2);
  letter-spacing: 1px;
}
.page-title span { color: var(--green); }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 12px; }
.status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 8px var(--green);
  animation: pulse-dot 2s infinite;
}
.status-dot.red { background: var(--red); box-shadow: 0 0 8px var(--red); }
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.4} }
.status-text { font-family: var(--mono); font-size: 11px; color: var(--text2); }
.toggle-btn {
  background: transparent;
  border: 1px solid var(--border2);
  color: var(--text2);
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  font-family: var(--mono);
  transition: all 0.2s;
}
.toggle-btn:hover { border-color: var(--green); color: var(--green); }
.content-area {
  flex: 1;
  overflow-y: auto;
  padding: 32px;
}
.content-area::-webkit-scrollbar { width: 4px; }
.content-area::-webkit-scrollbar-track { background: transparent; }
.content-area::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
.page-section { display: none; animation: fadein 0.3s ease; }
.page-section.active { display: block; }
@keyframes fadein { from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:translateY(0)} }

/* Login */
.login-wrap {
  position: fixed; inset: 0;
  background: var(--bg);
  display: flex; align-items: center; justify-content: center;
  z-index: 1000;
}
.login-card {
  width: 380px;
  background: var(--card);
  border: 1px solid var(--border2);
  border-radius: 20px;
  padding: 40px;
  box-shadow: 0 0 60px rgba(16,185,129,0.08), 0 0 120px rgba(0,0,0,0.5);
  position: relative;
  overflow: hidden;
}
.login-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--green), transparent);
}
.login-logo {
  text-align: center;
  margin-bottom: 32px;
}
.login-logo .big { font-family: var(--mono); font-size: 28px; font-weight: 700; color: var(--green); }
.login-logo .sub { font-size: 12px; color: var(--text3); font-family: var(--mono); letter-spacing: 2px; margin-top: 4px; }

/* Stats */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 20px 24px;
  position: relative;
  overflow: hidden;
  transition: all 0.2s;
}
.stat-card:hover { border-color: var(--border2); transform: translateY(-2px); box-shadow: 0 8px 32px rgba(0,0,0,0.3); }
.stat-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
}
.stat-card.green::before { background: linear-gradient(90deg, transparent, var(--green), transparent); }
.stat-card.orange::before { background: linear-gradient(90deg, transparent, var(--orange), transparent); }
.stat-card.red::before { background: linear-gradient(90deg, transparent, var(--red), transparent); }
.stat-card.purple::before { background: linear-gradient(90deg, transparent, var(--purple), transparent); }
.stat-label { font-family: var(--mono); font-size: 10px; letter-spacing: 2px; color: var(--text3); text-transform: uppercase; margin-bottom: 8px; }
.stat-value { font-family: var(--mono); font-size: 36px; font-weight: 700; line-height: 1; }
.stat-card.green .stat-value { color: var(--green); }
.stat-card.orange .stat-value { color: var(--orange); }
.stat-card.red .stat-value { color: var(--red); }
.stat-card.purple .stat-value { color: var(--purple); }
.stat-icon { position: absolute; right: 20px; top: 50%; transform: translateY(-50%); font-size: 32px; opacity: 0.07; }

/* Cards */
.panel {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  margin-bottom: 20px;
}
.panel-head {
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--card2);
}
.panel-head-icon { color: var(--green); font-size: 14px; }
.panel-head-title { font-family: var(--mono); font-size: 12px; letter-spacing: 1.5px; color: var(--text); text-transform: uppercase; font-weight: 700; }
.panel-head-sub { margin-left: auto; font-size: 11px; color: var(--text3); font-family: var(--mono); }
.panel-body { padding: 24px; }

/* Table */
.data-table { width: 100%; border-collapse: collapse; }
.data-table thead tr { border-bottom: 2px solid var(--border2); }
.data-table th {
  padding: 10px 16px;
  text-align: left;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 2px;
  color: var(--orange);
  text-transform: uppercase;
  background: var(--card2);
  white-space: nowrap;
}
.data-table td {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--text2);
  vertical-align: middle;
}
.data-table tbody tr:hover { background: rgba(255,255,255,0.02); }
.data-table tbody tr:last-child td { border-bottom: none; }
.data-table .mono { font-family: var(--mono); font-size: 12px; color: var(--text); }

/* Badges */
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 10px;
  font-family: var(--mono);
  font-weight: 700;
  letter-spacing: 1px;
  text-transform: uppercase;
}
.badge-green { background: rgba(16,185,129,0.12); color: var(--green); border: 1px solid rgba(16,185,129,0.25); }
.badge-red { background: rgba(239,68,68,0.12); color: var(--red); border: 1px solid rgba(239,68,68,0.25); }
.badge-orange { background: rgba(249,115,22,0.12); color: var(--orange); border: 1px solid rgba(249,115,22,0.25); }
.badge-yellow { background: rgba(251,191,36,0.12); color: var(--yellow); border: 1px solid rgba(251,191,36,0.25); }
.badge-purple { background: rgba(139,92,246,0.12); color: var(--purple); border: 1px solid rgba(139,92,246,0.25); }

/* Buttons */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.2s;
  font-family: var(--sans);
  white-space: nowrap;
}
.btn-primary { background: rgba(16,185,129,0.15); color: var(--green); border-color: rgba(16,185,129,0.4); }
.btn-primary:hover { background: rgba(16,185,129,0.25); box-shadow: 0 0 16px rgba(16,185,129,0.2); }
.btn-success { background: rgba(16,185,129,0.12); color: var(--green); border-color: rgba(16,185,129,0.3); }
.btn-success:hover { background: rgba(16,185,129,0.2); }
.btn-danger { background: rgba(239,68,68,0.12); color: var(--red); border-color: rgba(239,68,68,0.3); }
.btn-danger:hover { background: rgba(239,68,68,0.2); }
.btn-warning { background: rgba(251,191,36,0.12); color: var(--yellow); border-color: rgba(251,191,36,0.3); }
.btn-warning:hover { background: rgba(251,191,36,0.2); }
.btn-orange { background: rgba(249,115,22,0.12); color: var(--orange); border-color: rgba(249,115,22,0.3); }
.btn-orange:hover { background: rgba(249,115,22,0.2); }
.btn-ghost { background: transparent; color: var(--text3); border-color: var(--border); }
.btn-ghost:hover { background: var(--card2); color: var(--text2); }
.btn-sm { padding: 5px 12px; font-size: 11px; }
.btn-xs { padding: 3px 8px; font-size: 10px; }

/* Inputs */
.form-group { margin-bottom: 16px; }
.form-label {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 2px;
  color: var(--orange);
  text-transform: uppercase;
  margin-bottom: 8px;
}
.form-input, .form-select, .form-textarea {
  width: 100%;
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 10px;
  color: var(--text);
  font-size: 13px;
  padding: 10px 14px;
  font-family: var(--sans);
  transition: all 0.2s;
  outline: none;
}
.form-input:focus, .form-select:focus, .form-textarea:focus {
  border-color: rgba(16,185,129,0.5);
  box-shadow: 0 0 0 3px rgba(16,185,129,0.08);
}
.form-input::placeholder, .form-textarea::placeholder { color: var(--text3); }
.form-textarea { resize: vertical; min-height: 90px; }
.form-select option { background: var(--bg3); }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* Search */
.search-wrap { position: relative; margin-bottom: 16px; }
.search-wrap .search-icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--text3); font-size: 12px; }
.search-input {
  width: 100%;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--text);
  font-size: 13px;
  padding: 10px 14px 10px 36px;
  outline: none;
  transition: all 0.2s;
}
.search-input:focus { border-color: rgba(16,185,129,0.4); }

/* Step cards */
.step-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  margin-bottom: 20px;
  overflow: hidden;
}
.step-head {
  background: var(--card2);
  border-bottom: 1px solid var(--border);
  padding: 14px 20px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.step-num {
  background: linear-gradient(135deg, var(--green), var(--orange));
  color: var(--bg);
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  padding: 4px 12px;
  border-radius: 20px;
  letter-spacing: 1px;
}
.step-title { font-family: var(--mono); font-size: 13px; color: var(--text); font-weight: 700; letter-spacing: 0.5px; }
.step-actions { margin-left: auto; display: flex; gap: 8px; }

/* Message preview */
.msg-preview-text {
  font-size: 13px;
  color: var(--text);
  line-height: 1.5;
  background: var(--bg3);
  border-left: 3px solid var(--green);
  padding: 8px 12px;
  border-radius: 0 8px 8px 0;
  max-width: 400px;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg-preview-media {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text2);
  background: var(--bg3);
  border: 1px solid var(--border);
  padding: 6px 12px;
  border-radius: 8px;
}
.msg-preview-media .media-icon { font-size: 16px; }

/* Modal */
.modal-overlay {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  backdrop-filter: blur(4px);
  z-index: 500;
  align-items: center;
  justify-content: center;
}
.modal-overlay.open { display: flex; }
.modal-box {
  background: var(--card);
  border: 1px solid var(--border2);
  border-radius: 20px;
  width: 520px;
  max-width: 95vw;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 0 80px rgba(0,0,0,0.5);
  position: relative;
}
.modal-box::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--green), transparent);
}
.modal-head {
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.modal-title { font-family: var(--mono); font-size: 14px; color: var(--text); font-weight: 700; }
.modal-close {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text2);
  width: 30px; height: 30px;
  border-radius: 8px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px;
  transition: all 0.2s;
}
.modal-close:hover { background: rgba(239,68,68,0.1); color: var(--red); border-color: rgba(239,68,68,0.3); }
.modal-body { padding: 24px; }
.modal-footer { padding: 16px 24px; border-top: 1px solid var(--border); display: flex; gap: 10px; justify-content: flex-end; }

/* Permissions checkboxes */
.perm-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.perm-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 10px;
  cursor: pointer;
  transition: all 0.2s;
}
.perm-item:hover { border-color: var(--green); }
.perm-item input[type="checkbox"] { display: none; }
.perm-check {
  width: 18px; height: 18px;
  border: 2px solid var(--border2);
  border-radius: 5px;
  display: flex; align-items: center; justify-content: center;
  font-size: 9px;
  color: transparent;
  transition: all 0.2s;
  flex-shrink: 0;
}
.perm-item.checked .perm-check {
  background: var(--green);
  border-color: var(--green);
  color: var(--bg);
}
.perm-item.checked { border-color: rgba(16,185,129,0.4); background: rgba(16,185,129,0.05); }
.perm-label { font-size: 12px; font-weight: 600; color: var(--text2); }
.perm-item.checked .perm-label { color: var(--green); }

/* Broadcast history */
.bcast-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin-bottom: 8px;
  background: var(--bg3);
  flex-wrap: wrap;
}
.bcast-type { font-family: var(--mono); font-size: 10px; letter-spacing: 1px; }
.bcast-content { flex: 1; font-size: 13px; color: var(--text2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 100px; }
.bcast-actions { display: flex; gap: 6px; }

/* Section header */
.section-hdr { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
.section-hdr h2 { font-family: var(--mono); font-size: 20px; color: var(--text); font-weight: 700; }
.section-hdr p { font-size: 13px; color: var(--text3); margin-top: 4px; }

/* Toast */
.toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; pointer-events: none; }
.toast {
  background: var(--card);
  border: 1px solid var(--border2);
  border-radius: 12px;
  padding: 12px 18px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  animation: slideInToast 0.3s ease, fadeOutToast 0.4s ease 2.6s forwards;
  pointer-events: all;
  min-width: 240px;
  max-width: 360px;
}
.toast.success { border-left: 3px solid var(--green); }
.toast.error { border-left: 3px solid var(--red); }
.toast.info { border-left: 3px solid var(--orange); }
.toast-icon { font-size: 16px; }
.toast.success .toast-icon { color: var(--green); }
.toast.error .toast-icon { color: var(--red); }
.toast.info .toast-icon { color: var(--orange); }
@keyframes slideInToast { from{opacity:0;transform:translateX(20px)} to{opacity:1;transform:translateX(0)} }
@keyframes fadeOutToast { from{opacity:1} to{opacity:0;transform:translateX(20px)} }

/* Empty state */
.empty-state {
  text-align: center;
  padding: 48px 24px;
  color: var(--text3);
}
.empty-state i { font-size: 40px; margin-bottom: 12px; opacity: 0.3; }
.empty-state p { font-size: 14px; }

/* Divider */
.divider { border: none; border-top: 1px solid var(--border); margin: 20px 0; }

/* Settings grid */
.settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }

/* Env grid */
.env-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* Scrollable nav */
.nav-scroll { overflow-y: auto; flex: 1; padding-bottom: 8px; }
.nav-scroll::-webkit-scrollbar { width: 0; }

/* Mobile responsive */
@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: -280px;
    top: 0;
    z-index: 100;
    width: 280px;
    min-width: 280px;
  }
  .sidebar.open { left: 0; }
  .main { margin-left: 0; }
  .topbar { padding: 0 16px; gap: 8px; }
  .menu-toggle { display: inline-flex; }
  .content-area { padding: 16px; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .form-row, .env-grid, .settings-grid { grid-template-columns: 1fr; }
  .data-table { font-size: 11px; }
  .data-table th, .data-table td { padding: 8px 10px; }
  .stat-value { font-size: 28px; }
  .section-hdr h2 { font-size: 16px; }
  .topbar-right .toggle-btn { padding: 4px 10px; font-size: 10px; }
  .bcast-item { flex-direction: column; align-items: flex-start; }
}
</style>
</head>
<body>

<!-- Login Screen -->
<div class="login-wrap" id="loginWrap">
  <div class="login-card">
    <div class="login-logo">
      <div class="big">BOT_CTRL</div>
      <div class="sub">// GREEN/ORANGE EDITION</div>
    </div>
    <div class="form-group">
      <label class="form-label">Username</label>
      <input type="text" class="form-input" id="loginUser" placeholder="Enter username" autocomplete="username">
    </div>
    <div class="form-group">
      <label class="form-label">Password</label>
      <input type="password" class="form-input" id="loginPass" placeholder="••••••••" autocomplete="current-password" onkeydown="if(event.key==='Enter')doLogin()">
    </div>
    <button class="btn btn-primary" style="width:100%;justify-content:center;padding:12px;" onclick="doLogin()">
      <i class="fas fa-sign-in-alt"></i> Access Panel
    </button>
  </div>
</div>

<!-- App Shell -->
<div id="appShell" style="display:none; display:flex; width:100%; display:none;">
  <div class="sidebar" id="sidebar">
    <div class="sidebar-brand">
      <div class="brand-label">// SYSTEM</div>
      <div class="brand-title">BOT<span>_</span>PANEL</div>
      <div class="brand-sub" id="serverUrl">Loading...</div>
    </div>
    <div class="nav-scroll">
      <div class="nav-section">Navigation</div>
      <ul class="nav-list">
        <li class="nav-item"><a class="nav-link active" data-page="dashboard"><span class="nav-icon"><i class="fas fa-chart-pie"></i></span> Dashboard</a></li>
        <li class="nav-item"><a class="nav-link" data-page="messages"><span class="nav-icon"><i class="fas fa-layer-group"></i></span> Message Steps</a></li>
        <li class="nav-item"><a class="nav-link" data-page="users"><span class="nav-icon"><i class="fas fa-users"></i></span> Users <span class="nav-badge" id="badge-users">0</span></a></li>
        <li class="nav-item"><a class="nav-link" data-page="agents"><span class="nav-icon"><i class="fas fa-user-tie"></i></span> Agents <span class="nav-badge green" id="badge-agents">0</span></a></li>
        <li class="nav-item"><a class="nav-link" data-page="broadcast"><span class="nav-icon"><i class="fas fa-satellite-dish"></i></span> Broadcast</a></li>
        <li class="nav-item"><a class="nav-link" data-page="blocked"><span class="nav-icon"><i class="fas fa-shield-alt"></i></span> Blocked <span class="nav-badge red" id="badge-blocked">0</span></a></li>
        <li class="nav-item"><a class="nav-link" data-page="settings"><span class="nav-icon"><i class="fas fa-sliders-h"></i></span> Settings</a></li>
        <li class="nav-item" id="mainControlNav" style="display:none;"><a class="nav-link" data-page="maincontrol"><span class="nav-icon"><i class="fas fa-terminal"></i></span> Main Control</a></li>
      </ul>
    </div>
    <div class="sidebar-footer">
      <div class="user-chip">
        <div class="user-avatar" id="avatarInitial">A</div>
        <div class="user-info">
          <div class="user-name" id="sidebarUsername">Admin</div>
          <div class="user-role" id="sidebarRole">MAIN_ADMIN</div>
        </div>
      </div>
      <div class="logout-btn" onclick="doLogout()"><i class="fas fa-power-off"></i> Logout</div>
    </div>
  </div>

  <div class="main">
    <div class="topbar">
      <button class="menu-toggle" id="menuToggle"><i class="fas fa-bars"></i></button>
      <span class="page-title">BOT_CTRL / <span id="topbarSection">Dashboard</span></span>
      <div class="topbar-right">
        <div class="status-dot" id="botStatusDot"></div>
        <span class="status-text" id="botStatusText">BOT ACTIVE</span>
        <button class="toggle-btn" onclick="toggleBot()"><i class="fas fa-power-off"></i> Toggle</button>
      </div>
    </div>

    <div class="content-area">
      <!-- Dashboard -->
      <div id="dashboard-section" class="page-section active">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Dashboard</h2>
            <p>System overview and quick stats</p>
          </div>
        </div>
        <div class="stats-grid">
          <div class="stat-card green">
            <div class="stat-label">Total Users</div>
            <div class="stat-value" id="statTotal">—</div>
            <div class="stat-icon"><i class="fas fa-users"></i></div>
          </div>
          <div class="stat-card green">
            <div class="stat-label">Joined Today</div>
            <div class="stat-value" id="statToday">—</div>
            <div class="stat-icon"><i class="fas fa-user-plus"></i></div>
          </div>
          <div class="stat-card red">
            <div class="stat-label">Blocked</div>
            <div class="stat-value" id="statBlocked">—</div>
            <div class="stat-icon"><i class="fas fa-ban"></i></div>
          </div>
          <div class="stat-card orange">
            <div class="stat-label">Agents</div>
            <div class="stat-value" id="statAgents">—</div>
            <div class="stat-icon"><i class="fas fa-user-tie"></i></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-robot"></i></span>
            <span class="panel-head-title">Bot System Status</span>
            <span class="panel-head-sub" id="dashboardTime"></span>
          </div>
          <div class="panel-body" style="display:flex; gap:24px; align-items:center; flex-wrap:wrap;">
            <div>
              <div class="form-label" style="margin-bottom:6px;">Current Status</div>
              <span class="badge badge-green" id="botActiveBadge"><i class="fas fa-circle"></i> ACTIVE</span>
            </div>
            <div>
              <div class="form-label" style="margin-bottom:6px;">Reset Mode</div>
              <span class="badge badge-orange" id="resetModeBadge">Loading...</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Messages -->
      <div id="messages-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Message Steps</h2>
            <p>Configure auto-reply sequences</p>
          </div>
          <button class="btn btn-success" onclick="openModal('addStepModal')"><i class="fas fa-plus"></i> New Step</button>
        </div>
        <div id="stepsContainer">
          <div class="empty-state"><i class="fas fa-layer-group"></i><p>No steps yet. Create your first step above.</p></div>
        </div>
      </div>

      <!-- Users -->
      <div id="users-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Users</h2>
            <p>Manage all bot users</p>
          </div>
        </div>
        <div class="search-wrap">
          <i class="fas fa-search search-icon"></i>
          <input type="text" class="search-input" id="userSearch" placeholder="Search by ID, name or username...">
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-users"></i></span>
            <span class="panel-head-title">User List</span>
            <span class="panel-head-sub" id="userCount">0 users</span>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table" id="usersTable">
              <thead><tr>
                <th>User ID</th>
                <th>Name</th>
                <th>Username</th>
                <th>Step</th>
                <th>Joined</th>
                <th>Status</th>
                <th>Actions</th>
              </tr></thead>
              <tbody id="usersBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Agents -->
      <div id="agents-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Agents</h2>
            <p>Users marked as agents — they will not receive bot messages</p>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-user-tie"></i></span>
            <span class="panel-head-title">Agent List</span>
            <span class="panel-head-sub" id="agentCount">0 agents</span>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table">
              <thead><tr>
                <th>User ID</th>
                <th>Name</th>
                <th>Username</th>
                <th>Joined</th>
                <th>Last Active</th>
                <th>Actions</th>
              </tr></thead>
              <tbody id="agentsBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Broadcast -->
      <div id="broadcast-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Broadcast</h2>
            <p>Send messages to all active users (instant, no source upload, original filenames preserved)</p>
          </div>
          <div>
            <button class="btn btn-danger btn-sm" onclick="cancelBroadcasts()"><i class="fas fa-stop-circle"></i> Cancel Pending</button>
            <button class="btn btn-warning btn-sm" onclick="clearBroadcastHistory()"><i class="fas fa-trash-alt"></i> Clear History</button>
          </div>
        </div>
        <div class="panel" style="margin-bottom:20px;">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-satellite-dish"></i></span>
            <span class="panel-head-title">New Broadcast</span>
          </div>
          <div class="panel-body">
            <div class="form-group">
              <label class="form-label">Message Type</label>
              <select class="form-select" id="bcastType" onchange="toggleBcastMedia()">
                <option value="text">📝 Text</option>
                <option value="photo">🖼️ Photo</option>
                <option value="video">🎥 Video</option>
                <option value="document">📎 Document</option>
                <option value="voice">🎤 Voice</option>
              </select>
            </div>
            <!-- Text Broadcast Fields -->
            <div id="bcastTextField">
              <div class="form-group">
                <label class="form-label">Text Mode</label>
                <select class="form-select" id="bcastLinkMode" onchange="toggleBcastLinkFields()">
                  <option value="simple">Simple Text</option>
                  <option value="link">Text with Link</option>
                </select>
              </div>
              <div id="bcastSimpleText">
                <div class="form-group">
                  <label class="form-label">Message</label>
                  <textarea class="form-textarea" id="bcastText" placeholder="Type your broadcast message..."></textarea>
                </div>
              </div>
              <div id="bcastLinkText" style="display:none;">
                <div class="form-group">
                  <label class="form-label">Above Link Text</label>
                  <textarea class="form-textarea" id="bcastAboveText" placeholder="Text before the link..."></textarea>
                </div>
                <div class="form-group">
                  <label class="form-label">Link URL</label>
                  <input type="text" class="form-input" id="bcastLinkUrl" placeholder="https://...">
                </div>
                <div class="form-group">
                  <label class="form-label">Below Link Text</label>
                  <textarea class="form-textarea" id="bcastBelowText" placeholder="Text after the link..."></textarea>
                </div>
                <div class="form-group">
                  <label class="form-label">
                    <input type="checkbox" id="bcastDisablePreview"> Disable link preview
                  </label>
                </div>
              </div>
            </div>
            <!-- Media Broadcast Fields -->
            <div class="form-group" id="bcastMediaField" style="display:none;">
              <label class="form-label">Media File</label>
              <input type="file" class="form-input" id="bcastFile">
            </div>
            <div class="form-group" id="bcastCaptionWrap">
              <label class="form-label">Caption (optional)</label>
              <input type="text" class="form-input" id="bcastCaption" placeholder="Optional caption...">
            </div>
            <button class="btn btn-orange" onclick="sendBroadcast()"><i class="fas fa-paper-plane"></i> Send Broadcast (No Delay, Original Filename)</button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-history"></i></span>
            <span class="panel-head-title">Broadcast History</span>
          </div>
          <div class="panel-body" id="bcastHistory">
            <div class="empty-state"><i class="fas fa-history"></i><p>No broadcasts yet.</p></div>
          </div>
        </div>
      </div>

      <!-- Blocked -->
      <div id="blocked-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left">
            <h2>Blocked Users</h2>
            <p>Users blocked from receiving messages</p>
          </div>
        </div>
        <div class="panel" style="margin-bottom:20px;">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-plus-circle"></i></span>
            <span class="panel-head-title">Block User</span>
          </div>
          <div class="panel-body" style="display:flex; gap:12px; align-items:flex-end;">
            <div style="flex:1;">
              <label class="form-label">User ID or Username</label>
              <input type="text" class="form-input" id="blockIdentifier" placeholder="Enter user ID or @username">
            </div>
            <button class="btn btn-danger" onclick="blockByIdentifier()"><i class="fas fa-ban"></i> Block</button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-shield-alt"></i></span>
            <span class="panel-head-title">Blocked List</span>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table">
              <thead><tr><th>User ID</th><th>Name</th><th>Username</th><th>Actions</th></tr></thead>
              <tbody id="blockedBody"></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Settings -->
      <div id="settings-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left"><h2>Settings</h2><p>Bot configuration options</p></div>
        </div>
        <div class="settings-grid">
          <div class="panel">
            <div class="panel-head"><span class="panel-head-icon"><i class="fas fa-lock"></i></span><span class="panel-head-title">Change Password</span></div>
            <div class="panel-body">
              <div class="form-group"><label class="form-label">Current Password</label><input type="password" class="form-input" id="oldPwd" placeholder="Current password"></div>
              <div class="form-group"><label class="form-label">New Password</label><input type="password" class="form-input" id="newPwd" placeholder="New password"></div>
              <button class="btn btn-primary" onclick="changePassword()"><i class="fas fa-save"></i> Update Password</button>
            </div>
          </div>
          <div class="panel">
            <div class="panel-head"><span class="panel-head-icon"><i class="fas fa-redo"></i></span><span class="panel-head-title">Auto-Reset Sequence</span></div>
            <div class="panel-body">
              <div class="form-group">
                <label class="form-label">Reset Mode</label>
                <select class="form-select" id="resetModeSelect">
                  <option value="0">♻️ Restart on new message (after completion)</option>
                  <option value="-1">🚫 Never restart (manual removal only)</option>
                  <option value="custom">⏱️ Restart after inactivity (days)</option>
                </select>
              </div>
              <div class="form-group" id="resetDaysWrap" style="display:none;">
                <label class="form-label">Inactive Days Threshold</label>
                <input type="number" class="form-input" id="resetDaysInput" placeholder="e.g., 7" min="1">
                <div style="font-size:11px; color:var(--text3); margin-top:6px; font-family:var(--mono);">If user is inactive for this many days, they restart from Step 1.</div>
              </div>
              <button class="btn btn-primary" onclick="saveResetDays()"><i class="fas fa-save"></i> Save Settings</button>
            </div>
          </div>
        </div>
      </div>

      <!-- Main Control -->
      <div id="maincontrol-section" class="page-section">
        <div class="section-hdr">
          <div class="section-hdr-left"><h2>Main Control</h2><p>Environment and subadmin management</p></div>
        </div>
        <div class="panel" style="margin-bottom:20px;">
          <div class="panel-head"><span class="panel-head-icon"><i class="fas fa-terminal"></i></span><span class="panel-head-title">Environment Variables</span></div>
          <div class="panel-body">
            <div class="env-grid">
              <div class="form-group"><label class="form-label">API_ID</label><input type="text" class="form-input" id="envApiId"></div>
              <div class="form-group"><label class="form-label">API_HASH</label><input type="text" class="form-input" id="envApiHash"></div>
              <div class="form-group"><label class="form-label">PHONE_NUMBER</label><input type="text" class="form-input" id="envPhone"></div>
              <div class="form-group"><label class="form-label">SOURCE_CHAT_ID</label><input type="text" class="form-input" id="envSourceChat"></div>
              <div class="form-group"><label class="form-label">PORT</label><input type="text" class="form-input" id="envPort"></div>
            </div>
            <button class="btn btn-warning" onclick="saveEnv()"><i class="fas fa-save"></i> Save — Restart Required</button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head">
            <span class="panel-head-icon"><i class="fas fa-user-shield"></i></span>
            <span class="panel-head-title">Subadmins</span>
            <button class="btn btn-success btn-sm" style="margin-left:auto;" onclick="openModal('addSubModal')"><i class="fas fa-user-plus"></i> Add</button>
          </div>
          <div style="overflow-x:auto;">
            <table class="data-table">
              <thead><tr><th>Username</th><th>Role</th><th>Permissions</th><th>Actions</th></tr></thead>
              <tbody id="subadminsBody"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Modals -->
<div class="modal-overlay" id="addStepModal">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title"><i class="fas fa-plus me-2"></i>New Step</span><button class="modal-close" onclick="closeModal('addStepModal')"><i class="fas fa-times"></i></button></div>
    <div class="modal-body">
      <div class="form-group"><label class="form-label">Step Number</label><input type="number" class="form-input" id="newStepNum" placeholder="e.g., 1, 2, 3..." min="1"></div>
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal('addStepModal')">Cancel</button><button class="btn btn-success" onclick="addStep()"><i class="fas fa-check"></i> Create Step</button></div>
  </div>
</div>

<div class="modal-overlay" id="addMsgModal">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title"><i class="fas fa-plus me-2"></i>Add Message — Step <span id="addMsgStepLabel">?</span></span><button class="modal-close" onclick="closeModal('addMsgModal')"><i class="fas fa-times"></i></button></div>
    <div class="modal-body">
      <input type="hidden" id="addMsgStepId">
      <div class="form-row">
        <div class="form-group"><label class="form-label">Order</label><input type="number" class="form-input" id="addMsgOrder" placeholder="1, 2, 3..." min="1"></div>
        <div class="form-group"><label class="form-label">Type</label><select class="form-select" id="addMsgType" onchange="toggleAddMsgFields()"><option value="text">📝 Text</option><option value="photo">🖼️ Photo</option><option value="video">🎥 Video</option><option value="document">📎 Document</option><option value="voice">🎤 Voice</option></select></div>
      </div>
      <div class="form-group" id="addTextField"><label class="form-label">Text Content</label><textarea class="form-textarea" id="addMsgText" placeholder="Message text..."></textarea></div>
      <div class="form-group" id="addMediaField" style="display:none;"><label class="form-label">Media File</label><input type="file" class="form-input" id="addMsgFile"></div>
      <div class="form-group" id="addCaptionWrap"><label class="form-label">Caption (optional)</label><input type="text" class="form-input" id="addMsgCaption" placeholder="Optional caption..."></div>
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal('addMsgModal')">Cancel</button><button class="btn btn-primary" onclick="addMessage()"><i class="fas fa-check"></i> Add Message</button></div>
  </div>
</div>

<div class="modal-overlay" id="editMsgModal">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title"><i class="fas fa-edit me-2"></i>Edit Message</span><button class="modal-close" onclick="closeModal('editMsgModal')"><i class="fas fa-times"></i></button></div>
    <div class="modal-body">
      <input type="hidden" id="editMsgId">
      <div class="form-group"><label class="form-label">Type</label><select class="form-select" id="editMsgType" onchange="toggleEditMsgFields()"><option value="text">📝 Text</option><option value="photo">🖼️ Photo</option><option value="video">🎥 Video</option><option value="document">📎 Document</option><option value="voice">🎤 Voice</option></select></div>
      <div class="form-group" id="editTextField"><label class="form-label">Text Content</label><textarea class="form-textarea" id="editMsgText"></textarea></div>
      <div class="form-group" id="editMediaField" style="display:none;"><label class="form-label">Media File</label><input type="file" class="form-input" id="editMsgFile"><div style="font-size:11px; color:var(--text3); margin-top:4px; font-family:var(--mono);">Leave empty to keep existing media</div></div>
      <div class="form-group" id="editCaptionWrap"><label class="form-label">Caption</label><input type="text" class="form-input" id="editMsgCaption" placeholder="Caption..."></div>
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal('editMsgModal')">Cancel</button><button class="btn btn-primary" onclick="saveEditMessage()"><i class="fas fa-save"></i> Save Changes</button></div>
  </div>
</div>

<div class="modal-overlay" id="addSubModal">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title"><i class="fas fa-user-plus me-2"></i>New Subadmin</span><button class="modal-close" onclick="closeModal('addSubModal')"><i class="fas fa-times"></i></button></div>
    <div class="modal-body">
      <div class="form-row">
        <div class="form-group"><label class="form-label">Username</label><input type="text" class="form-input" id="subUsername" placeholder="username"></div>
        <div class="form-group"><label class="form-label">Password</label><input type="password" class="form-input" id="subPassword" placeholder="password"></div>
      </div>
      <div class="form-group"><label class="form-label">Permissions</label>
        <div class="perm-grid">
          <label class="perm-item" id="perm-stats"><input type="checkbox" value="stats"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-chart-bar" style="margin-right:6px;"></i>Statistics</span></label>
          <label class="perm-item" id="perm-messages"><input type="checkbox" value="messages"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-envelope" style="margin-right:6px;"></i>Messages</span></label>
          <label class="perm-item" id="perm-broadcast"><input type="checkbox" value="broadcast"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-satellite-dish" style="margin-right:6px;"></i>Broadcast</span></label>
          <label class="perm-item" id="perm-blocked"><input type="checkbox" value="blocked"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-ban" style="margin-right:6px;"></i>Block Users</span></label>
          <label class="perm-item" id="perm-settings"><input type="checkbox" value="settings"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-cog" style="margin-right:6px;"></i>Settings</span></label>
          <label class="perm-item" id="perm-main"><input type="checkbox" value="main"><div class="perm-check"><i class="fas fa-check"></i></div><span class="perm-label"><i class="fas fa-crown" style="margin-right:6px;"></i>Main Control</span></label>
        </div>
      </div>
    </div>
    <div class="modal-footer"><button class="btn btn-ghost" onclick="closeModal('addSubModal')">Cancel</button><button class="btn btn-success" onclick="addSubadmin()"><i class="fas fa-user-plus"></i> Add Subadmin</button></div>
  </div>
</div>

<div class="toast-container" id="toastContainer"></div>

<script>
let currentUser = null;

function toast(msg, type='info') {
  const icons = { success: 'check-circle', error: 'exclamation-circle', info: 'info-circle' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<i class="fas fa-${icons[type]} toast-icon"></i><span>${msg}</span>`;
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
document.querySelectorAll('.modal-overlay').forEach(m => {
  m.addEventListener('click', e => { if(e.target === m) m.classList.remove('open'); });
});

async function api(method, url, body=null) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if(body) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if(!res.ok) { const d = await res.json().catch(()=>({error:res.statusText})); throw new Error(d.error||res.statusText); }
  return res.json();
}
async function apiForm(url, formData) {
  const res = await fetch(url, { method: 'POST', body: formData });
  if(!res.ok) throw new Error(res.statusText);
  return res.json();
}

async function doLogin() {
  const username = document.getElementById('loginUser').value.trim();
  const password = document.getElementById('loginPass').value.trim();
  if(!username || !password) { toast('Enter username and password', 'error'); return; }
  try {
    const res = await api('POST', '/api/login', {username, password});
    currentUser = res;
    document.getElementById('loginWrap').style.display = 'none';
    document.getElementById('appShell').style.display = 'flex';
    document.getElementById('sidebarUsername').textContent = res.username || username;
    document.getElementById('sidebarRole').textContent = res.is_main ? 'MAIN_ADMIN' : 'SUBADMIN';
    document.getElementById('avatarInitial').textContent = (res.username || username)[0].toUpperCase();
    document.getElementById('serverUrl').textContent = window.location.host;
    if(res.is_main) document.getElementById('mainControlNav').style.display = 'block';
    initAll();
    toast('Welcome back, ' + (res.username || username), 'success');
  } catch(e) { toast('Invalid credentials', 'error'); }
}

async function doLogout() {
  await api('POST', '/api/logout').catch(()=>{});
  location.reload();
}

document.getElementById('menuToggle').addEventListener('click', () => {
  document.getElementById('sidebar').classList.toggle('open');
});
document.querySelectorAll('.nav-link[data-page]').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    const page = link.getAttribute('data-page');
    switchPage(page);
    if(window.innerWidth <= 768) document.getElementById('sidebar').classList.remove('open');
  });
});

function switchPage(page) {
  document.querySelectorAll('.page-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-link[data-page]').forEach(l => l.classList.remove('active'));
  const sec = document.getElementById(page + '-section');
  if(sec) sec.classList.add('active');
  const lnk = document.querySelector(`.nav-link[data-page="${page}"]`);
  if(lnk) lnk.classList.add('active');
  const titles = { dashboard:'Dashboard', messages:'Message Steps', users:'Users', agents:'Agents', broadcast:'Broadcast', blocked:'Blocked', settings:'Settings', maincontrol:'Main Control' };
  document.getElementById('topbarSection').textContent = titles[page] || page;
  if(page==='messages') loadSteps();
  if(page==='users') loadUsers();
  if(page==='agents') loadAgents();
  if(page==='blocked') loadBlocked();
  if(page==='broadcast') loadBroadcastHistory();
  if(page==='settings') loadResetDays();
  if(page==='maincontrol') { loadSubadmins(); loadEnv(); }
}

async function initAll() {
  loadDashboard();
  loadSteps();
  loadUsers();
  loadAgents();
  updateTime();
  setInterval(updateTime, 30000);
}

function updateTime() {
  const el = document.getElementById('dashboardTime');
  if(el) el.textContent = new Date().toLocaleTimeString();
}

async function loadDashboard() {
  try {
    const d = await api('GET', '/api/stats');
    document.getElementById('statTotal').textContent = d.total;
    document.getElementById('statToday').textContent = d.today;
    document.getElementById('statBlocked').textContent = d.blocked;
    document.getElementById('statAgents').textContent = d.agents;
    document.getElementById('badge-users').textContent = d.total;
    document.getElementById('badge-blocked').textContent = d.blocked;
    document.getElementById('badge-agents').textContent = d.agents;
  } catch(e){}
  try {
    const a = await api('GET', '/api/bot_active');
    const dot = document.getElementById('botStatusDot');
    const txt = document.getElementById('botStatusText');
    const badge = document.getElementById('botActiveBadge');
    if(a.active) {
      dot.className = 'status-dot'; txt.textContent = 'BOT ACTIVE';
      badge.className = 'badge badge-green'; badge.innerHTML = '<i class="fas fa-circle"></i> ACTIVE';
    } else {
      dot.className = 'status-dot red'; txt.textContent = 'BOT PAUSED';
      badge.className = 'badge badge-red'; badge.innerHTML = '<i class="fas fa-circle"></i> PAUSED';
    }
  } catch(e){}
  try {
    const r = await api('GET', '/api/reset_days');
    const badge = document.getElementById('resetModeBadge');
    if(r.days === 0) badge.innerHTML = '♻️ Restart on completion';
    else if(r.days === -1) badge.innerHTML = '🚫 Never restart';
    else badge.innerHTML = `⏱️ Reset after ${r.days} inactive days`;
  } catch(e){}
}

async function toggleBot() {
  try {
    await api('POST', '/api/toggle_bot');
    loadDashboard();
    toast('Bot status toggled', 'info');
  } catch(e) { toast('Failed to toggle bot', 'error'); }
}

const typeIcons = { text:'📝', photo:'🖼️', video:'🎥', document:'📎', voice:'🎤', audio:'🔊' };
const typeBadges = { text:'badge-green', photo:'badge-purple', video:'badge-orange', document:'badge-yellow', voice:'badge-green', audio:'badge-green' };
const _msgStore = {};

async function loadSteps() {
  try {
    const steps = await api('GET', '/api/steps');
    const c = document.getElementById('stepsContainer');
    if(!steps.length) {
      c.innerHTML = '<div class="empty-state"><i class="fas fa-layer-group"></i><p>No steps yet. Create your first step.</p></div>';
      return;
    }
    let html = '';
    for(const s of steps) {
      for(const m of s.messages) { _msgStore[m.id] = m; }
      html += `<div class="step-card">
        <div class="step-head">
          <span class="step-num">STEP ${s.step}</span>
          <span class="step-title">${s.messages.length} message${s.messages.length!==1?'s':''}</span>
          <div class="step-actions">
            <button class="btn btn-primary btn-sm" onclick="openAddMsg(${s.step})"><i class="fas fa-plus"></i> Add Message</button>
            <button class="btn btn-danger btn-sm" onclick="deleteStep(${s.step})"><i class="fas fa-trash"></i></button>
          </div>
        </div>`;
      if(!s.messages.length) {
        html += `<div style="padding:24px; text-align:center; color:var(--text3); font-size:13px;">No messages in this step.</div>`;
      } else {
        html += `<div style="overflow-x:auto;"><table class="data-table"><thead><tr><th>#</th><th>Type</th><th>Preview</th><th>Actions</th></tr></thead><tbody>`;
        for(const m of s.messages) {
          let preview = '';
          if(m.type === 'text') {
            const text = (m.content || '').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            preview = `<div class="msg-preview-text">${text || '<em style="color:var(--text3)">Empty</em>'}</div>`;
          } else {
            const cap = m.caption ? ` · <span style="color:var(--text3)">${m.caption.substring(0,40)}</span>` : '';
            preview = `<div class="msg-preview-media"><span class="media-icon">${typeIcons[m.type]||'📎'}</span> ${m.type.charAt(0).toUpperCase()+m.type.slice(1)}${cap}</div>`;
          }
          html += `<tr>
            <td class="mono">${m.order_within_step}</td>
            <td><span class="badge ${typeBadges[m.type]||'badge-green'}">${typeIcons[m.type]||'📎'} ${m.type}</span></td>
            <td>${preview}</td>
            <td style="white-space:nowrap;">
              <button class="btn btn-warning btn-xs edit-msg-btn" data-msgid="${m.id}"><i class="fas fa-edit"></i> Edit</button>
              <button class="btn btn-danger btn-xs" onclick="deleteMsg(${m.id})"><i class="fas fa-trash"></i></button>
            </td>
           </tr>`;
        }
        html += `</tbody></table></div>`;
      }
      html += `</div>`;
    }
    c.innerHTML = html;
    document.querySelectorAll('.edit-msg-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const msgId = parseInt(btn.getAttribute('data-msgid'));
        const m = _msgStore[msgId];
        if(m) openEditMsg(m.id, m.type, m.content || '', m.caption || '');
      });
    });
  } catch(e) { console.error(e); }
}

async function addStep() {
  const num = document.getElementById('newStepNum').value;
  if(!num) { toast('Enter a step number', 'error'); return; }
  try {
    await api('POST', '/api/add_step', {step: parseInt(num)});
    closeModal('addStepModal');
    document.getElementById('newStepNum').value = '';
    loadSteps();
    toast(`Step ${num} created`, 'success');
  } catch(e) { toast('Failed to add step', 'error'); }
}

async function deleteStep(step) {
  if(!confirm(`Delete Step ${step} and all its messages?`)) return;
  await api('DELETE', `/api/delete_step/${step}`);
  loadSteps();
  toast(`Step ${step} deleted`, 'success');
}

function openAddMsg(step) {
  document.getElementById('addMsgStepId').value = step;
  document.getElementById('addMsgStepLabel').textContent = step;
  document.getElementById('addMsgText').value = '';
  document.getElementById('addMsgCaption').value = '';
  document.getElementById('addMsgType').value = 'text';
  toggleAddMsgFields();
  openModal('addMsgModal');
}

function toggleAddMsgFields() {
  const type = document.getElementById('addMsgType').value;
  document.getElementById('addTextField').style.display = type==='text' ? 'block' : 'none';
  document.getElementById('addMediaField').style.display = type!=='text' ? 'block' : 'none';
  document.getElementById('addCaptionWrap').style.display = type==='voice' ? 'none' : 'block';
}

async function addMessage() {
  const step = document.getElementById('addMsgStepId').value;
  const order = document.getElementById('addMsgOrder').value;
  const type = document.getElementById('addMsgType').value;
  if(!order) { toast('Enter order number', 'error'); return; }
  const fd = new FormData();
  fd.append('step', step);
  fd.append('order', order);
  fd.append('msg_type', type);
  if(type === 'text') {
    fd.append('text_content', document.getElementById('addMsgText').value);
  } else {
    const file = document.getElementById('addMsgFile').files[0];
    if(!file) { toast('Select a file', 'error'); return; }
    fd.append('media_file', file);
  }
  fd.append('caption', document.getElementById('addMsgCaption').value);
  try {
    await apiForm('/api/add_message', fd);
    closeModal('addMsgModal');
    loadSteps();
    toast('Message added', 'success');
  } catch(e) { toast('Failed to add message', 'error'); }
}

function openEditMsg(id, type, content, caption) {
  document.getElementById('editMsgId').value = id;
  document.getElementById('editMsgType').value = type;
  document.getElementById('editMsgText').value = content;
  document.getElementById('editMsgCaption').value = caption;
  document.getElementById('editMsgFile').value = '';
  toggleEditMsgFields();
  openModal('editMsgModal');
}

function toggleEditMsgFields() {
  const type = document.getElementById('editMsgType').value;
  document.getElementById('editTextField').style.display = type==='text' ? 'block' : 'none';
  document.getElementById('editMediaField').style.display = type!=='text' ? 'block' : 'none';
  document.getElementById('editCaptionWrap').style.display = type==='voice' ? 'none' : 'block';
}

async function saveEditMessage() {
  const fd = new FormData();
  fd.append('msg_id', document.getElementById('editMsgId').value);
  const type = document.getElementById('editMsgType').value;
  fd.append('msg_type', type);
  if(type==='text') fd.append('text_content', document.getElementById('editMsgText').value);
  else {
    const file = document.getElementById('editMsgFile').files[0];
    if(file) fd.append('media_file', file);
  }
  fd.append('caption', document.getElementById('editMsgCaption').value);
  try {
    await apiForm('/api/edit_message', fd);
    closeModal('editMsgModal');
    loadSteps();
    toast('Message updated', 'success');
  } catch(e) { toast('Failed to update message', 'error'); }
}

async function deleteMsg(id) {
  if(!confirm('Delete this message?')) return;
  await api('DELETE', `/api/delete_message/${id}`);
  loadSteps();
  toast('Message deleted', 'success');
}

async function loadUsers() {
  try {
    const users = await api('GET', '/api/users');
    document.getElementById('userCount').textContent = `${users.length} users`;
    let html = '';
    users.forEach(u => {
      const blocked = u.blocked ? `<span class="badge badge-red"><i class="fas fa-ban"></i> Blocked</span>` : `<span class="badge badge-green"><i class="fas fa-circle"></i> Active</span>`;
      const joined = u.joined_date ? u.joined_date.split('T')[0] : '—';
      html += `<tr data-search="${u.user_id} ${u.first_name||''} ${u.username||''}" >
        <td class="mono">${u.user_id}</td>
        <td>${u.first_name||'—'}</td>
        <td>${u.username ? '<span style="color:var(--orange)">@'+u.username+'</span>' : '—'}</td>
        <td><span class="badge badge-orange">Step ${u.step}</span></td>
        <td class="mono" style="font-size:11px;">${joined}</td>
        <td>${blocked}</td>
        <td style="white-space:nowrap;">
          ${u.blocked
            ? `<button class="btn btn-success btn-xs" onclick="unblockUser(${u.user_id})"><i class="fas fa-unlock"></i> Unblock</button>`
            : `<button class="btn btn-warning btn-xs" onclick="blockUser(${u.user_id})"><i class="fas fa-ban"></i> Block</button>`}
          <button class="btn btn-purple btn-xs" onclick="tagAsAgent(${u.user_id})"><i class="fas fa-user-tie"></i> Agent</button>
          <button class="btn btn-danger btn-xs" onclick="deleteUser(${u.user_id})"><i class="fas fa-trash"></i></button>
        </td>
      </tr>`;
    });
    document.getElementById('usersBody').innerHTML = html || '<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:32px;">No users yet.</td></tr>';
  } catch(e) { console.error(e); }
}

document.getElementById('userSearch').addEventListener('keyup', function() {
  const f = this.value.toLowerCase();
  document.querySelectorAll('#usersBody tr').forEach(row => {
    row.style.display = (row.dataset.search||'').toLowerCase().includes(f) ? '' : 'none';
  });
});

async function blockUser(uid) { await api('POST','/api/block',{user_id:uid}); loadUsers(); loadBlocked(); loadDashboard(); toast('User blocked','success'); }
async function unblockUser(uid) { await api('POST','/api/unblock',{user_id:uid}); loadUsers(); loadBlocked(); loadDashboard(); toast('User unblocked','success'); }
async function deleteUser(uid) {
  if(!confirm(`Delete user ${uid}? They will start from step 1 if they message again.`)) return;
  await api('DELETE', `/api/delete_user/${uid}`);
  loadUsers(); loadDashboard(); loadBlocked();
  toast('User deleted','success');
}
async function tagAsAgent(uid) {
  if(!confirm(`Tag user ${uid} as Agent? They will stop receiving bot messages.`)) return;
  await api('POST','/api/set_agent',{user_id:uid});
  loadUsers(); loadAgents(); loadDashboard();
  toast('User tagged as agent','success');
}

async function loadAgents() {
  try {
    const agents = await api('GET', '/api/agents');
    document.getElementById('agentCount').textContent = `${agents.length} agents`;
    document.getElementById('badge-agents').textContent = agents.length;
    let html = '';
    agents.forEach(a => {
      const joined = a.joined_date ? a.joined_date.split('T')[0] : '—';
      const lastActive = a.last_active ? (a.last_active+'').split('T')[0] : '—';
      html += `<tr>
        <td class="mono">${a.user_id}</td>
        <td>${a.first_name||'—'}</td>
        <td>${a.username ? '<span style="color:var(--orange)">@'+a.username+'</span>' : '—'}</td>
        <td class="mono" style="font-size:11px;">${joined}</td>
        <td class="mono" style="font-size:11px;">${lastActive}</td>
        <td>
          <button class="btn btn-success btn-xs" onclick="removeAgent(${a.user_id})"><i class="fas fa-user-minus"></i> Remove Agent</button>
          <button class="btn btn-danger btn-xs" onclick="deleteUser(${a.user_id})"><i class="fas fa-trash"></i></button>
        </td>
      </tr>`;
    });
    document.getElementById('agentsBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:32px;">No agents yet.</td></tr>';
  } catch(e) { console.error(e); }
}

async function removeAgent(uid) {
  await api('POST','/api/remove_agent',{user_id:uid});
  loadAgents(); loadUsers(); loadDashboard();
  toast('Agent removed — treated as user again','success');
}

async function loadBlocked() {
  try {
    const users = await api('GET', '/api/blocked');
    let html = '';
    users.forEach(u => {
      html += `<tr>
        <td class="mono">${u.user_id}</td>
        <td>${u.first_name||'—'}</td>
        <td>${u.username ? '@'+u.username : '—'}</td>
        <td><button class="btn btn-success btn-xs" onclick="unblockUser(${u.user_id})"><i class="fas fa-unlock"></i> Unblock</button></td>
      </tr>`;
    });
    document.getElementById('blockedBody').innerHTML = html || '<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:32px;">No blocked users.</td></tr>';
    document.getElementById('badge-blocked').textContent = users.length;
  } catch(e) {}
}

async function blockByIdentifier() {
  const id = document.getElementById('blockIdentifier').value.trim();
  if(!id) { toast('Enter a user ID or username','error'); return; }
  try {
    await api('POST','/api/block_by_identifier',{identifier:id});
    document.getElementById('blockIdentifier').value = '';
    loadBlocked(); loadUsers(); loadDashboard();
    toast('User blocked','success');
  } catch(e) { toast('User not found','error'); }
}

function toggleBcastMedia() {
  const type = document.getElementById('bcastType').value;
  document.getElementById('bcastTextField').style.display = type==='text' ? 'block' : 'none';
  document.getElementById('bcastMediaField').style.display = type!=='text' ? 'block' : 'none';
  document.getElementById('bcastCaptionWrap').style.display = type==='voice' ? 'none' : 'block';
}

function toggleBcastLinkFields() {
  const mode = document.getElementById('bcastLinkMode').value;
  document.getElementById('bcastSimpleText').style.display = mode==='simple' ? 'block' : 'none';
  document.getElementById('bcastLinkText').style.display = mode==='link' ? 'block' : 'none';
}

async function sendBroadcast() {
  const type = document.getElementById('bcastType').value;
  const fd = new FormData();
  fd.append('msg_type', type);
  if(type==='text') {
    const mode = document.getElementById('bcastLinkMode').value;
    if(mode==='link') {
      const above = document.getElementById('bcastAboveText').value;
      const url = document.getElementById('bcastLinkUrl').value;
      const below = document.getElementById('bcastBelowText').value;
      if(!url) { toast('Enter a link URL','error'); return; }
      fd.append('above_text', above);
      fd.append('link_url', url);
      fd.append('below_text', below);
      fd.append('disable_link_preview', document.getElementById('bcastDisablePreview').checked);
    } else {
      const text = document.getElementById('bcastText').value;
      if(!text) { toast('Enter a message','error'); return; }
      fd.append('text_content', text);
    }
  } else {
    const file = document.getElementById('bcastFile').files[0];
    if(!file) { toast('Select a file','error'); return; }
    fd.append('media_file', file);
  }
  fd.append('caption', document.getElementById('bcastCaption').value);
  try {
    await apiForm('/api/broadcast', fd);
    document.getElementById('bcastText').value = '';
    document.getElementById('bcastAboveText').value = '';
    document.getElementById('bcastLinkUrl').value = '';
    document.getElementById('bcastBelowText').value = '';
    document.getElementById('bcastCaption').value = '';
    loadBroadcastHistory();
    toast('Broadcast queued and sending directly with original filename...','success');
  } catch(e) { toast('Failed to send broadcast','error'); }
}

async function cancelBroadcasts() {
  if(!confirm('Cancel all pending broadcasts? This will stop the current broadcast if still running.')) return;
  try {
    await api('POST', '/api/broadcast/cancel');
    toast('Pending broadcasts cancelled','info');
    loadBroadcastHistory();
  } catch(e) { toast('Failed to cancel','error'); }
}

async function clearBroadcastHistory() {
  if(!confirm('Delete all broadcast history? This will NOT delete messages from users.')) return;
  try {
    await api('DELETE', '/api/broadcast/clear_history');
    loadBroadcastHistory();
    toast('Broadcast history cleared','success');
  } catch(e) { toast('Failed to clear history','error'); }
}

async function deleteBroadcastItem(bcastId) {
  if(!confirm('Delete this broadcast record? This does NOT unsend messages.')) return;
  try {
    await api('DELETE', `/api/broadcast/${bcastId}`);
    loadBroadcastHistory();
    toast('Record deleted','success');
  } catch(e) { toast('Error deleting','error'); }
}

async function loadBroadcastHistory() {
  try {
    const hist = await api('GET', '/api/broadcast_history');
    const c = document.getElementById('bcastHistory');
    if(!hist.length) { c.innerHTML = '<div class="empty-state"><i class="fas fa-history"></i><p>No broadcasts yet.</p></div>'; return; }
    let html = '';
    hist.forEach(b => {
      const statusCls = b.status==='done' ? 'badge-green' : (b.status==='cancelled' ? 'badge-red' : 'badge-orange');
      const icon = typeIcons[b.type] || '📎';
      const content = b.type === 'text' ? (b.content ? b.content.substring(0,60) + (b.content.length>60?'...':'') : 'Empty') : 'Media file';
      html += `<div class="bcast-item">
        <span class="badge badge-orange bcast-type">${icon} ${b.type}</span>
        <span class="bcast-content">${content}</span>
        <span class="badge ${statusCls}">${b.status.toUpperCase()}</span>
        <span class="bcast-actions">
          <button class="btn btn-ghost btn-xs" onclick="deleteBroadcastItem(${b.id})"><i class="fas fa-trash"></i></button>
        </span>
      </div>`;
    });
    c.innerHTML = html;
  } catch(e) {}
}

async function loadResetDays() {
  try {
    const r = await api('GET', '/api/reset_days');
    const sel = document.getElementById('resetModeSelect');
    const inp = document.getElementById('resetDaysInput');
    if(r.days === 0) { sel.value = '0'; document.getElementById('resetDaysWrap').style.display='none'; }
    else if(r.days === -1) { sel.value = '-1'; document.getElementById('resetDaysWrap').style.display='none'; }
    else { sel.value = 'custom'; inp.value = r.days; document.getElementById('resetDaysWrap').style.display='block'; }
  } catch(e) {}
}

document.getElementById('resetModeSelect').addEventListener('change', function() {
  document.getElementById('resetDaysWrap').style.display = this.value === 'custom' ? 'block' : 'none';
});

async function saveResetDays() {
  const sel = document.getElementById('resetModeSelect').value;
  let days;
  if(sel === '0') days = 0;
  else if(sel === '-1') days = -1;
  else {
    days = parseInt(document.getElementById('resetDaysInput').value);
    if(!days || days < 1) { toast('Enter valid days', 'error'); return; }
  }
  try {
    await api('POST', '/api/reset_days', {days});
    loadDashboard();
    toast('Reset settings saved','success');
  } catch(e) { toast('Failed to save','error'); }
}

async function changePassword() {
  const old = document.getElementById('oldPwd').value;
  const nw = document.getElementById('newPwd').value;
  if(!old||!nw) { toast('Fill both fields','error'); return; }
  try {
    const r = await api('POST','/api/change_password',{old_password:old,new_password:nw});
    document.getElementById('oldPwd').value=''; document.getElementById('newPwd').value='';
    toast(r.message,'success');
  } catch(e) { toast('Wrong old password','error'); }
}

async function loadEnv() {
  if(!currentUser?.is_main) return;
  try {
    const env = await api('GET','/api/env');
    document.getElementById('envApiId').value = env.API_ID||'';
    document.getElementById('envApiHash').value = env.API_HASH||'';
    document.getElementById('envPhone').value = env.PHONE_NUMBER||'';
    document.getElementById('envSourceChat').value = env.SOURCE_CHAT_ID||'';
    document.getElementById('envPort').value = env.PORT||'';
  } catch(e) {}
}

async function saveEnv() {
  const data = {
    API_ID: document.getElementById('envApiId').value,
    API_HASH: document.getElementById('envApiHash').value,
    PHONE_NUMBER: document.getElementById('envPhone').value,
    SOURCE_CHAT_ID: document.getElementById('envSourceChat').value,
    PORT: document.getElementById('envPort').value,
  };
  try {
    const r = await api('POST','/api/update_env',data);
    toast(r.message,'info');
  } catch(e) { toast('Failed to save env','error'); }
}

async function loadSubadmins() {
  if(!currentUser?.is_main) return;
  try {
    const subs = await api('GET','/api/subadmins');
    const permLabels = {stats:'Stats',messages:'Messages',broadcast:'Broadcast',blocked:'Blocked',settings:'Settings',main:'Main Control'};
    let html = '';
    subs.forEach(s => {
      const perms = Object.entries(s.permissions).filter(([k,v])=>v).map(([k])=>`<span class="badge badge-orange" style="margin:2px;">${permLabels[k]||k}</span>`).join('');
      const isMain = s.is_main ? '<span class="badge badge-purple">MAIN</span>' : '<span class="badge badge-green">SUBADMIN</span>';
      html += `<tr>
        <td class="mono">${s.username}</td>
        <td>${isMain}</td>
        <td>${perms}</td>
        <td>${!s.is_main ? `<button class="btn btn-danger btn-xs" onclick="deleteSubadmin(${s.id})"><i class="fas fa-trash"></i> Remove</button>` : '<span style="color:var(--text3);font-size:11px;">—</span>'}</td>
      </tr>`;
    });
    document.getElementById('subadminsBody').innerHTML = html || '<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:24px;">No subadmins.</td></tr>';
  } catch(e) {}
}

document.querySelectorAll('.perm-item').forEach(item => {
  item.addEventListener('click', () => {
    const cb = item.querySelector('input[type=checkbox]');
    cb.checked = !cb.checked;
    item.classList.toggle('checked', cb.checked);
  });
});

async function addSubadmin() {
  const username = document.getElementById('subUsername').value.trim();
  const password = document.getElementById('subPassword').value.trim();
  if(!username||!password) { toast('Fill username and password','error'); return; }
  const permissions = {};
  document.querySelectorAll('#addSubModal .perm-item input[type=checkbox]').forEach(cb => {
    permissions[cb.value] = cb.checked;
  });
  try {
    const r = await api('POST','/api/add_subadmin',{username,password,permissions});
    if(r.ok) {
      closeModal('addSubModal');
      document.getElementById('subUsername').value='';
      document.getElementById('subPassword').value='';
      document.querySelectorAll('#addSubModal .perm-item').forEach(i=>{i.classList.remove('checked');i.querySelector('input').checked=false;});
      loadSubadmins();
      toast('Subadmin added','success');
    } else { toast('Username already taken','error'); }
  } catch(e) { toast('Failed to add subadmin','error'); }
}

async function deleteSubadmin(id) {
  if(!confirm('Delete this subadmin?')) return;
  await api('DELETE',`/api/subadmin/${id}`);
  loadSubadmins();
  toast('Subadmin removed','success');
}
</script>
</body>
</html>'''  # Placeholder – will be replaced with full HTML in final output.

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_root(path):
    return HTML_PAGE

if __name__ == '__main__':
    cleanup_temp_dirs()
    print_neon("=" * 60, 'green')
    print_neon("  BOT CONTROL PANEL – ULTIMATE", 'green')
    print_neon("  - Direct broadcast preserves original filenames", 'green')
    print_neon("  - QR login in terminal (no web needed)", 'green')
    print_neon("  - Persistent session", 'green')
    print_neon("=" * 60, 'green')
    init_db()
    t = threading.Thread(target=start_client, daemon=True)
    t.start()
    print_neon("⏳  Starting Telegram client...", 'orange')
    time.sleep(2)
    print_neon("=" * 60, 'green')
    print_neon(f"  🌐  Web panel:  http://0.0.0.0:{PORT}", 'green')
    print_neon(f"  🔐  Login:      admin / admin123", 'orange')
    if not PHONE_NUMBER:
        print_neon("  📱  QR code printed above – scan it now!", 'orange')
    print_neon("=" * 60, 'green')
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
