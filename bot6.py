#!/usr/bin/env python3
"""
Personal Telegram Assistant - ULTIMATE VERSION
- QR login in terminal (no web panel needed for login)
- Direct broadcast preserves original filenames (APK, etc.)
- Step messages preserve premium emojis, captions, formatting
- Green/Orange theme
- Persistent session (never ask again)
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

# ==================== DATABASE ====================
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
    """Print QR code to terminal and handle 2FA using correct Telethon API"""
    print_neon("\n📱 QR Code Login", 'orange')
    print_neon("No PHONE_NUMBER in .env. Starting QR login...", 'cyan')
    try:
        qr = await client.qr_login()
        if QR_AVAILABLE:
            img = qrcode.make(qr.url)
            img.save("qr_login.png")
            print_neon("✅ QR code saved as 'qr_login.png'", 'green')
            # Print ASCII QR
            qr_ascii = qrcode.make(qr.url, box_size=1, border=1)
            lines = []
            for y in range(qr_ascii.size[1]):
                line = ""
                for x in range(qr_ascii.size[0]):
                    line += "██" if qr_ascii.getpixel((x, y)) else "  "
                lines.append(line)
            ascii_qr = "\n".join(lines)
            print(ascii_qr)
        else:
            print_neon(f"Scan this URL: {qr.url}", 'orange')
        print_neon("Waiting for scan...", 'cyan')
        try:
            # Correct method for Telethon QR login
            await qr.wait()
        except errors.rpcerrorlist.SessionPasswordNeededError:
            pwd = input("Enter your 2FA password: ").strip()
            await client.sign_in(password=pwd)
        except Exception as e:
            print_neon(f"QR login failed, falling back to phone: {e}", 'red')
            phone = input("Enter your phone number (with country code): ").strip()
            await client.send_code_request(phone)
            code = input("Enter the code you received: ").strip()
            await client.sign_in(phone, code)
        print_neon("✅ QR login successful!", 'green')
    except Exception as e:
        print_neon(f"QR login error: {e}", 'red')
        raise

async def start_telegram():
    global cancel_broadcast_event
    cancel_broadcast_event = asyncio.Event()
    await client.connect()
    if not client.is_connected():
        print_neon("Failed to connect to Telegram", 'red')
        return
    if await client.is_user_authorized():
        print_neon("✅ Using existing Telegram session", 'green')
    else:
        if PHONE_NUMBER:
            print_neon(f"Logging in with phone: {PHONE_NUMBER}", 'orange')
            await client.start(phone=PHONE_NUMBER)
        else:
            await qr_login_terminal()
    print_neon("🤖 BOT IS RUNNING SUCCESSFULLY!", 'green')
    asyncio.create_task(broadcast_worker())
    await client.run_until_disconnected()

def start_client():
    asyncio.set_event_loop(client_loop)
    client_loop.run_until_complete(start_telegram())

# ==================== FLASK API ====================
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
HTML_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>BOT CONTROL · ORANGE/GREEN</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
:root {
  --bg: #0a0c0c;
  --bg2: #0f1212;
  --bg3: #151919;
  --card: #0c1010;
  --card2: #111616;
  --border: #1a2020;
  --border2: #253030;
  --green: #10b981;
  --green-dark: #0d9668;
  --orange: #f97316;
  --orange-dark: #ea580c;
  --red: #ef4444;
  --blue: #3b82f6;
  --text: #e5e7eb;
  --text2: #9ca3af;
  --text3: #6b7280;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'Inter', sans-serif;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: var(--sans); height: 100vh; overflow: hidden; display: flex; }

/* Sidebar */
.sidebar {
  width: 280px;
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  height: 100vh;
  position: relative;
  z-index: 10;
  transition: left 0.2s ease;
}
.sidebar::after {
  content: '';
  position: absolute;
  top: 0; right: 0;
  width: 2px;
  height: 100%;
  background: linear-gradient(to bottom, transparent, var(--orange), var(--green), transparent);
  opacity: 0.3;
}
.sidebar-header {
  padding: 28px 24px;
  border-bottom: 1px solid var(--border);
}
.logo { font-family: var(--mono); font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
.logo span { color: var(--green); }
.logo small { font-size: 10px; color: var(--orange); letter-spacing: 2px; display: block; margin-top: 4px; }
.nav {
  flex: 1;
  padding: 24px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  border-radius: 12px;
  color: var(--text2);
  cursor: pointer;
  transition: all 0.2s;
  font-size: 14px;
  font-weight: 500;
}
.nav-item i { width: 20px; font-size: 16px; }
.nav-item:hover { background: rgba(16,185,129,0.08); color: var(--text); }
.nav-item.active {
  background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(249,115,22,0.08));
  color: var(--green);
  border-left: 2px solid var(--green);
}
.nav-badge {
  margin-left: auto;
  background: var(--green);
  color: var(--bg);
  padding: 2px 8px;
  border-radius: 20px;
  font-size: 10px;
  font-weight: 700;
}
.nav-badge.red { background: var(--red); }
.sidebar-footer {
  padding: 20px;
  border-top: 1px solid var(--border);
}
.user {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px;
  background: var(--card);
  border-radius: 14px;
  margin-bottom: 12px;
}
.user-avatar {
  width: 40px;
  height: 40px;
  background: linear-gradient(135deg, var(--green), var(--orange));
  border-radius: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 18px;
}
.user-info { flex: 1; }
.user-name { font-weight: 600; font-size: 14px; }
.user-role { font-size: 11px; color: var(--orange); font-family: var(--mono); }
.logout {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  color: var(--text3);
  cursor: pointer;
  transition: 0.2s;
}
.logout:hover { background: rgba(239,68,68,0.1); color: var(--red); }

/* Main */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.topbar {
  height: 70px;
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
}
.page-title { font-family: var(--mono); font-size: 14px; color: var(--text2); letter-spacing: 1px; }
.page-title span { color: var(--green); }
.topbar-right { display: flex; align-items: center; gap: 20px; }
.status { display: flex; align-items: center; gap: 8px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }
.status-dot.red { background: var(--red); box-shadow: none; animation: none; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }
.toggle-bot { background: transparent; border: 1px solid var(--border2); color: var(--text2); padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: 12px; transition: 0.2s; }
.toggle-bot:hover { border-color: var(--green); color: var(--green); }
.content {
  flex: 1;
  overflow-y: auto;
  padding: 28px 32px;
}
.content::-webkit-scrollbar { width: 4px; }
.content::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }

/* Cards & Stats */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  margin-bottom: 32px;
}
.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 20px;
  transition: 0.2s;
}
.stat-card:hover { border-color: var(--border2); transform: translateY(-2px); }
.stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 2px; color: var(--text3); margin-bottom: 8px; }
.stat-value { font-size: 32px; font-weight: 700; font-family: var(--mono); }
.stat-card.green .stat-value { color: var(--green); }
.stat-card.red .stat-value { color: var(--red); }
.stat-card.orange .stat-value { color: var(--orange); }

.panel {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 20px;
  margin-bottom: 24px;
  overflow: hidden;
}
.panel-header {
  padding: 16px 24px;
  background: var(--card2);
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.panel-title { font-weight: 600; font-size: 14px; letter-spacing: 0.5px; }
.panel-body { padding: 20px 24px; }
.table-wrapper { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 12px 16px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 1px; color: var(--orange); border-bottom: 1px solid var(--border); }
td { padding: 12px 16px; font-size: 13px; color: var(--text2); border-bottom: 1px solid var(--border); }
tr:last-child td { border-bottom: none; }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 10px;
  border-radius: 30px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
}
.badge-green { background: rgba(16,185,129,0.12); color: var(--green); }
.badge-red { background: rgba(239,68,68,0.12); color: var(--red); }
.badge-orange { background: rgba(249,115,22,0.12); color: var(--orange); }
.btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 14px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid transparent;
  transition: 0.2s;
}
.btn-primary { background: rgba(16,185,129,0.12); color: var(--green); border-color: rgba(16,185,129,0.3); }
.btn-primary:hover { background: rgba(16,185,129,0.2); }
.btn-danger { background: rgba(239,68,68,0.12); color: var(--red); border-color: rgba(239,68,68,0.3); }
.btn-warning { background: rgba(249,115,22,0.12); color: var(--orange); border-color: rgba(249,115,22,0.3); }
.btn-sm { padding: 4px 10px; font-size: 11px; }
input, select, textarea {
  background: var(--bg3);
  border: 1px solid var(--border2);
  border-radius: 10px;
  padding: 10px 14px;
  color: var(--text);
  font-size: 13px;
  width: 100%;
  outline: none;
  transition: 0.2s;
}
input:focus, select:focus, textarea:focus { border-color: var(--green); }
.form-group { margin-bottom: 16px; }
.form-label { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--orange); margin-bottom: 6px; display: block; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }

/* Login */
.login-overlay {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.login-card {
  width: 400px;
  background: var(--card);
  border: 1px solid var(--border2);
  border-radius: 28px;
  padding: 40px;
  text-align: center;
}
.login-logo { margin-bottom: 32px; }
.login-logo h1 { font-family: var(--mono); font-size: 28px; }
.login-logo p { color: var(--text3); font-size: 12px; margin-top: 4px; }

/* Modal */
.modal {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.8);
  backdrop-filter: blur(4px);
  z-index: 500;
  align-items: center;
  justify-content: center;
}
.modal.open { display: flex; }
.modal-content {
  background: var(--card);
  border: 1px solid var(--border2);
  border-radius: 24px;
  width: 500px;
  max-width: 90vw;
  max-height: 85vh;
  overflow-y: auto;
}
.modal-header {
  padding: 20px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.modal-body { padding: 24px; }
.modal-footer { padding: 16px 24px; border-top: 1px solid var(--border); display: flex; justify-content: flex-end; gap: 12px; }
.close { background: transparent; border: none; color: var(--text3); font-size: 20px; cursor: pointer; }

/* Toast */
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.toast {
  background: var(--card);
  border-left: 3px solid var(--green);
  padding: 12px 20px;
  border-radius: 12px;
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  animation: slideIn 0.2s ease, fadeOut 0.3s ease 2.7s forwards;
  box-shadow: 0 8px 20px rgba(0,0,0,0.3);
}
.toast.error { border-left-color: var(--red); }
@keyframes slideIn { from { opacity:0; transform: translateX(20px); } to { opacity:1; transform: translateX(0); } }
@keyframes fadeOut { to { opacity:0; transform: translateX(20px); } }

@media (max-width: 768px) {
  .sidebar { position: fixed; left: -280px; z-index: 20; }
  .sidebar.open { left: 0; }
  .stats-grid { grid-template-columns: 1fr 1fr; }
  .content { padding: 20px; }
  .row { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<div id="loginScreen" class="login-overlay">
  <div class="login-card">
    <div class="login-logo"><h1>BOT<span style="color:var(--orange)">_</span>CTRL</h1><p>green/orange edition</p></div>
    <div class="form-group"><input type="text" id="loginUser" placeholder="Username" autocomplete="username"></div>
    <div class="form-group"><input type="password" id="loginPass" placeholder="Password" autocomplete="current-password" onkeypress="if(event.key==='Enter')doLogin()"></div>
    <button class="btn btn-primary" style="width:100%; justify-content:center;" onclick="doLogin()"><i class="fas fa-key"></i> Access Panel</button>
  </div>
</div>

<div id="app" style="display:none; display:flex; width:100%;">
  <div class="sidebar" id="sidebar">
    <div class="sidebar-header"><div class="logo">BOT<span>_</span>PANEL<br><small>// v2.0</small></div></div>
    <div class="nav">
      <div class="nav-item active" data-page="dashboard"><i class="fas fa-chart-pie"></i> Dashboard</div>
      <div class="nav-item" data-page="messages"><i class="fas fa-layer-group"></i> Message Steps</div>
      <div class="nav-item" data-page="users"><i class="fas fa-users"></i> Users <span class="nav-badge" id="navUsers">0</span></div>
      <div class="nav-item" data-page="agents"><i class="fas fa-user-tie"></i> Agents <span class="nav-badge" id="navAgents">0</span></div>
      <div class="nav-item" data-page="broadcast"><i class="fas fa-satellite-dish"></i> Broadcast</div>
      <div class="nav-item" data-page="blocked"><i class="fas fa-shield-alt"></i> Blocked <span class="nav-badge red" id="navBlocked">0</span></div>
      <div class="nav-item" data-page="settings"><i class="fas fa-sliders-h"></i> Settings</div>
      <div class="nav-item" id="mainControlBtn" data-page="maincontrol" style="display:none;"><i class="fas fa-terminal"></i> Main Control</div>
    </div>
    <div class="sidebar-footer">
      <div class="user"><div class="user-avatar" id="userAvatar">A</div><div class="user-info"><div class="user-name" id="userName">Admin</div><div class="user-role" id="userRole">MAIN_ADMIN</div></div></div>
      <div class="logout" onclick="doLogout()"><i class="fas fa-sign-out-alt"></i> Logout</div>
    </div>
  </div>
  <div class="main">
    <div class="topbar"><div class="page-title">BOT_CTRL / <span id="currentPage">Dashboard</span></div><div class="topbar-right"><div class="status"><div class="status-dot" id="statusDot"></div><span id="statusText">BOT ACTIVE</span></div><button class="toggle-bot" onclick="toggleBot()"><i class="fas fa-power-off"></i> Toggle</button></div></div>
    <div class="content" id="content">
      <!-- pages injected dynamically -->
    </div>
  </div>
</div>

<div id="toastContainer" class="toast-container"></div>

<!-- Modals (simplified, but functional) -->
<div id="addStepModal" class="modal"><div class="modal-content"><div class="modal-header"><span>New Step</span><button class="close" onclick="closeModal('addStepModal')">&times;</button></div><div class="modal-body"><input type="number" id="newStepNum" placeholder="Step number"></div><div class="modal-footer"><button class="btn btn-primary" onclick="addStep()">Create</button></div></div></div>
<div id="addMsgModal" class="modal"><div class="modal-content"><div class="modal-header"><span>Add Message</span><button class="close" onclick="closeModal('addMsgModal')">&times;</button></div><div class="modal-body"><input type="hidden" id="addMsgStep"><input type="number" id="addMsgOrder" placeholder="Order"><select id="addMsgType"><option value="text">Text</option><option value="photo">Photo</option><option value="video">Video</option><option value="document">Document</option><option value="voice">Voice</option></select><div id="addTextArea"><textarea id="addMsgText" placeholder="Message text"></textarea></div><div id="addFileArea" style="display:none"><input type="file" id="addMsgFile"></div><input type="text" id="addMsgCaption" placeholder="Caption"></div><div class="modal-footer"><button class="btn btn-primary" onclick="addMessage()">Add</button></div></div></div>
<div id="editMsgModal" class="modal"><div class="modal-content"><div class="modal-header"><span>Edit Message</span><button class="close" onclick="closeModal('editMsgModal')">&times;</button></div><div class="modal-body"><input type="hidden" id="editMsgId"><select id="editMsgType"><option value="text">Text</option><option value="photo">Photo</option><option value="video">Video</option><option value="document">Document</option><option value="voice">Voice</option></select><div id="editTextArea"><textarea id="editMsgText"></textarea></div><div id="editFileArea" style="display:none"><input type="file" id="editMsgFile"><small>Leave empty to keep current media</small></div><input type="text" id="editMsgCaption" placeholder="Caption"></div><div class="modal-footer"><button class="btn btn-primary" onclick="saveEditMessage()">Save</button></div></div></div>
<div id="addSubModal" class="modal"><div class="modal-content"><div class="modal-header"><span>Add Subadmin</span><button class="close" onclick="closeModal('addSubModal')">&times;</button></div><div class="modal-body"><input type="text" id="subUsername" placeholder="Username"><input type="password" id="subPassword" placeholder="Password"><div class="form-group"><label class="form-label">Permissions</label><div style="display:grid; grid-template-columns:1fr 1fr; gap:8px"><label><input type="checkbox" value="stats"> Stats</label><label><input type="checkbox" value="messages"> Messages</label><label><input type="checkbox" value="broadcast"> Broadcast</label><label><input type="checkbox" value="blocked"> Block Users</label><label><input type="checkbox" value="settings"> Settings</label><label><input type="checkbox" value="main"> Main Control</label></div></div></div><div class="modal-footer"><button class="btn btn-primary" onclick="addSubadmin()">Add</button></div></div></div>

<script>
let currentUser = null;
function toast(msg,type='success'){const t=document.createElement('div');t.className=`toast ${type}`;t.innerHTML=`<i class="fas fa-${type==='success'?'check-circle':'exclamation-circle'}"></i> ${msg}`;document.getElementById('toastContainer').appendChild(t);setTimeout(()=>t.remove(),3000);}
async function api(method,url,body=null){const opts={method,headers:{'Content-Type':'application/json'}};if(body)opts.body=JSON.stringify(body);const res=await fetch(url,opts);if(!res.ok){const d=await res.json().catch(()=>({error:res.statusText}));throw new Error(d.error||res.statusText);}return res.json();}
async function apiForm(url,fd){const res=await fetch(url,{method:'POST',body:fd});if(!res.ok)throw new Error(await res.text());return res.json();}
function openModal(id){document.getElementById(id).classList.add('open');}
function closeModal(id){document.getElementById(id).classList.remove('open');}
document.querySelectorAll('.modal').forEach(m=>{m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open');});});

async function doLogin(){const u=document.getElementById('loginUser').value.trim(),p=document.getElementById('loginPass').value.trim();if(!u||!p)return toast('Enter credentials','error');try{const r=await api('POST','/api/login',{username:u,password:p});currentUser=r;document.getElementById('loginScreen').style.display='none';document.getElementById('app').style.display='flex';document.getElementById('userName').innerText=r.username||u;document.getElementById('userRole').innerText=r.is_main?'MAIN_ADMIN':'SUBADMIN';document.getElementById('userAvatar').innerText=(r.username||u)[0].toUpperCase();if(r.is_main)document.getElementById('mainControlBtn').style.display='flex';loadDashboard();loadSteps();loadUsers();loadAgents();loadBlocked();loadBroadcastHistory();loadResetDays();loadSubadmins();loadEnv();toast('Welcome back');}catch(e){toast('Invalid credentials','error');}}
async function doLogout(){await api('POST','/api/logout');location.reload();}
document.querySelectorAll('.nav-item[data-page]').forEach(item=>{item.addEventListener('click',()=>{const page=item.getAttribute('data-page');document.querySelectorAll('.nav-item').forEach(i=>i.classList.remove('active'));item.classList.add('active');document.getElementById('currentPage').innerText=page.charAt(0).toUpperCase()+page.slice(1);document.getElementById('content').innerHTML='<div style="text-align:center;padding:40px;">Loading...</div>';if(page==='dashboard')loadDashboard();else if(page==='messages')loadSteps();else if(page==='users')loadUsers();else if(page==='agents')loadAgents();else if(page==='broadcast')loadBroadcastHistory();else if(page==='blocked')loadBlocked();else if(page==='settings')loadResetDays();else if(page==='maincontrol'){loadSubadmins();loadEnv();}});});

async function loadDashboard(){try{const s=await api('GET','/api/stats');document.getElementById('content').innerHTML=`<div class="stats-grid"><div class="stat-card green"><div class="stat-label">Total Users</div><div class="stat-value">${s.total}</div></div><div class="stat-card green"><div class="stat-label">Joined Today</div><div class="stat-value">${s.today}</div></div><div class="stat-card red"><div class="stat-label">Blocked</div><div class="stat-value">${s.blocked}</div></div><div class="stat-card orange"><div class="stat-label">Agents</div><div class="stat-value">${s.agents}</div></div></div><div class="panel"><div class="panel-header"><span class="panel-title">System Status</span></div><div class="panel-body"><span class="badge badge-green" id="botActiveBadge"><i class="fas fa-circle"></i> ACTIVE</span></div></div>`;const a=await api('GET','/api/bot_active');const badge=document.getElementById('botActiveBadge');if(a.active)badge.innerHTML='<i class="fas fa-circle"></i> ACTIVE';else badge.innerHTML='<i class="fas fa-circle"></i> PAUSED';const dot=document.getElementById('statusDot'),txt=document.getElementById('statusText');if(a.active){dot.className='status-dot';txt.innerText='BOT ACTIVE';}else{dot.className='status-dot red';txt.innerText='BOT PAUSED';}document.getElementById('navUsers').innerText=s.total;document.getElementById('navBlocked').innerText=s.blocked;document.getElementById('navAgents').innerText=s.agents;}catch(e){}}
async function toggleBot(){await api('POST','/api/toggle_bot');loadDashboard();toast('Bot toggled');}
async function loadSteps(){const steps=await api('GET','/api/steps');let html='<div style="margin-bottom:20px;display:flex;justify-content:flex-end;"><button class="btn btn-primary" onclick="openModal(\'addStepModal\')"><i class="fas fa-plus"></i> New Step</button></div>';if(!steps.length)html+='<div class="panel"><div class="panel-body" style="text-align:center;">No steps yet.</div></div>';else{for(const s of steps){html+=`<div class="panel"><div class="panel-header"><span class="panel-title">Step ${s.step} (${s.messages.length} msgs)</span><div><button class="btn btn-danger btn-sm" onclick="deleteStep(${s.step})"><i class="fas fa-trash"></i> Delete Step</button><button class="btn btn-primary btn-sm" onclick="openAddMsg(${s.step})"><i class="fas fa-plus"></i> Add Message</button></div></div><div class="table-wrapper"><table><thead><tr><th>#</th><th>Type</th><th>Preview</th><th>Actions</th></tr></thead><tbody>`;for(const m of s.messages){let preview=m.type==='text'?(m.content||'').substring(0,60):`${m.type} media`;html+=`<tr><td>${m.order_within_step}</td><td><span class="badge badge-orange">${m.type}</span></td><td>${preview}</td><td><button class="btn btn-warning btn-sm" onclick="openEditMsg(${m.id},'${m.type}','${(m.content||'').replace(/'/g,"\\'")}','${(m.caption||'').replace(/'/g,"\\'")}')">Edit</button><button class="btn btn-danger btn-sm" onclick="deleteMsg(${m.id})">Del</button></td></tr>`;}html+=`</tbody></table></div></div>`;}}document.getElementById('content').innerHTML=html;}
async function addStep(){const num=document.getElementById('newStepNum').value;if(!num)return toast('Enter step number','error');await api('POST','/api/add_step',{step:parseInt(num)});closeModal('addStepModal');loadSteps();toast('Step created');}
async function deleteStep(step){if(!confirm(`Delete step ${step}?`))return;await api('DELETE',`/api/delete_step/${step}`);loadSteps();}
function openAddMsg(step){document.getElementById('addMsgStep').value=step;document.getElementById('addMsgOrder').value='';document.getElementById('addMsgText').value='';document.getElementById('addMsgCaption').value='';document.getElementById('addMsgType').value='text';toggleAddMsgFields();openModal('addMsgModal');}
function toggleAddMsgFields(){const t=document.getElementById('addMsgType').value;document.getElementById('addTextArea').style.display=t==='text'?'block':'none';document.getElementById('addFileArea').style.display=t!=='text'?'block':'none';}
async function addMessage(){const step=document.getElementById('addMsgStep').value,order=document.getElementById('addMsgOrder').value,type=document.getElementById('addMsgType').value;if(!order)return toast('Order required','error');const fd=new FormData();fd.append('step',step);fd.append('order',order);fd.append('msg_type',type);if(type==='text')fd.append('text_content',document.getElementById('addMsgText').value);else{const file=document.getElementById('addMsgFile').files[0];if(!file)return toast('Select a file','error');fd.append('media_file',file);}fd.append('caption',document.getElementById('addMsgCaption').value);await apiForm('/api/add_message',fd);closeModal('addMsgModal');loadSteps();toast('Message added');}
function openEditMsg(id,type,content,caption){document.getElementById('editMsgId').value=id;document.getElementById('editMsgType').value=type;document.getElementById('editMsgText').value=content;document.getElementById('editMsgCaption').value=caption;toggleEditMsgFields();openModal('editMsgModal');}
function toggleEditMsgFields(){const t=document.getElementById('editMsgType').value;document.getElementById('editTextArea').style.display=t==='text'?'block':'none';document.getElementById('editFileArea').style.display=t!=='text'?'block':'none';}
async function saveEditMessage(){const fd=new FormData();fd.append('msg_id',document.getElementById('editMsgId').value);fd.append('msg_type',document.getElementById('editMsgType').value);if(document.getElementById('editMsgType').value==='text')fd.append('text_content',document.getElementById('editMsgText').value);else{const file=document.getElementById('editMsgFile').files[0];if(file)fd.append('media_file',file);}fd.append('caption',document.getElementById('editMsgCaption').value);await apiForm('/api/edit_message',fd);closeModal('editMsgModal');loadSteps();toast('Message updated');}
async function deleteMsg(id){if(!confirm('Delete this message?'))return;await api('DELETE',`/api/delete_message/${id}`);loadSteps();}
async function loadUsers(){const users=await api('GET','/api/users');let html=`<div class="panel"><div class="panel-header"><span class="panel-title">User List (${users.length})</span></div><div class="panel-body"><input type="text" id="userSearch" placeholder="Search by ID, name or username..." style="margin-bottom:16px;"></div><div class="table-wrapper"><table><thead><tr><th>ID</th><th>Name</th><th>Username</th><th>Step</th><th>Status</th><th>Actions</th></tr></thead><tbody id="usersTableBody"></tbody></table></div></div>`;document.getElementById('content').innerHTML=html;const tbody=document.getElementById('usersTableBody');tbody.innerHTML='';users.forEach(u=>{const tr=document.createElement('tr');tr.setAttribute('data-search',`${u.user_id} ${u.first_name||''} ${u.username||''}`.toLowerCase());tr.innerHTML=`<td class="mono">${u.user_id}</td><td>${u.first_name||'—'}</td><td>${u.username?'@'+u.username:'—'}</td><td><span class="badge badge-orange">Step ${u.step}</span></td><td>${u.blocked?'<span class="badge badge-red">Blocked</span>':'<span class="badge badge-green">Active</span>'}</td><td>${u.blocked?`<button class="btn btn-success btn-sm" onclick="unblockUser(${u.user_id})">Unblock</button>`:`<button class="btn btn-warning btn-sm" onclick="blockUser(${u.user_id})">Block</button>`}<button class="btn btn-primary btn-sm" onclick="tagAsAgent(${u.user_id})">Agent</button><button class="btn btn-danger btn-sm" onclick="deleteUser(${u.user_id})">Del</button></td>`;tbody.appendChild(tr);});document.getElementById('userSearch').addEventListener('keyup',function(){const q=this.value.toLowerCase();document.querySelectorAll('#usersTableBody tr').forEach(r=>{r.style.display=r.getAttribute('data-search').includes(q)?'':'none';});});}
async function blockUser(uid){await api('POST','/api/block',{user_id:uid});loadUsers();loadBlocked();loadDashboard();toast('User blocked');}
async function unblockUser(uid){await api('POST','/api/unblock',{user_id:uid});loadUsers();loadBlocked();loadDashboard();toast('User unblocked');}
async function deleteUser(uid){if(!confirm(`Delete user ${uid}?`))return;await api('DELETE',`/api/delete_user/${uid}`);loadUsers();loadDashboard();loadBlocked();}
async function tagAsAgent(uid){await api('POST','/api/set_agent',{user_id:uid});loadUsers();loadAgents();loadDashboard();}
async function loadAgents(){const agents=await api('GET','/api/agents');let html=`<div class="panel"><div class="panel-header"><span class="panel-title">Agents (${agents.length})</span></div><div class="table-wrapper"><table><thead><tr><th>ID</th><th>Name</th><th>Username</th><th>Actions</th></tr></thead><tbody>`;agents.forEach(a=>{html+=`<tr><td>${a.user_id}</td><td>${a.first_name||'—'}</td><td>${a.username?'@'+a.username:'—'}</td><td><button class="btn btn-warning btn-sm" onclick="removeAgent(${a.user_id})">Remove</button><button class="btn btn-danger btn-sm" onclick="deleteUser(${a.user_id})">Del</button></td></tr>`;});html+=`</tbody></table></div></div>`;document.getElementById('content').innerHTML=html;}
async function removeAgent(uid){await api('POST','/api/remove_agent',{user_id:uid});loadAgents();loadUsers();loadDashboard();}
async function loadBlocked(){const users=await api('GET','/api/blocked');let html=`<div class="panel"><div class="panel-header"><span class="panel-title">Blocked Users</span></div><div class="panel-body"><div style="display:flex; gap:12px; margin-bottom:20px;"><input type="text" id="blockIdentifier" placeholder="User ID or @username"><button class="btn btn-danger" onclick="blockByIdentifier()"><i class="fas fa-ban"></i> Block</button></div></div><div class="table-wrapper"><table><thead><tr><th>ID</th><th>Name</th><th>Username</th><th>Actions</th></tr></thead><tbody>`;users.forEach(u=>{html+=`<tr><td>${u.user_id}</td><td>${u.first_name||'—'}</td><td>${u.username?'@'+u.username:'—'}</td><td><button class="btn btn-success btn-sm" onclick="unblockUser(${u.user_id})">Unblock</button></td></tr>`;});html+=`</tbody></table></div></div>`;document.getElementById('content').innerHTML=html;}
async function blockByIdentifier(){const id=document.getElementById('blockIdentifier').value.trim();if(!id)return toast('Enter ID or username','error');try{await api('POST','/api/block_by_identifier',{identifier:id});loadBlocked();loadUsers();loadDashboard();toast('User blocked');}catch(e){toast('User not found','error');}}
async function loadBroadcastHistory(){const hist=await api('GET','/api/broadcast_history');let html=`<div style="margin-bottom:20px;"><div class="panel"><div class="panel-header"><span class="panel-title">New Broadcast</span></div><div class="panel-body"><div class="form-group"><label class="form-label">Type</label><select id="bcastType" onchange="toggleBcastMedia()"><option value="text">Text</option><option value="photo">Photo</option><option value="video">Video</option><option value="document">Document</option><option value="voice">Voice</option></select></div><div id="bcastTextDiv"><textarea id="bcastText" placeholder="Message text..."></textarea></div><div id="bcastFileDiv" style="display:none"><input type="file" id="bcastFile"></div><input type="text" id="bcastCaption" placeholder="Caption (optional)"><button class="btn btn-primary" style="margin-top:16px;" onclick="sendBroadcast()"><i class="fas fa-paper-plane"></i> Send Broadcast</button><button class="btn btn-warning" style="margin-top:16px; margin-left:8px;" onclick="cancelBroadcasts()"><i class="fas fa-stop"></i> Cancel Pending</button><button class="btn btn-danger" style="margin-top:16px; margin-left:8px;" onclick="clearBroadcastHistory()"><i class="fas fa-trash"></i> Clear History</button></div></div></div><div class="panel"><div class="panel-header"><span class="panel-title">Broadcast History</span></div><div class="panel-body" id="bcastHistoryList"></div></div>`;document.getElementById('content').innerHTML=html;const listDiv=document.getElementById('bcastHistoryList');if(hist.length===0)listDiv.innerHTML='<div style="text-align:center;color:var(--text3);padding:20px;">No broadcasts yet.</div>';else{listDiv.innerHTML='';hist.forEach(b=>{const div=document.createElement('div');div.className='bcast-item';div.style.display='flex';div.style.justifyContent='space-between';div.style.alignItems='center';div.style.padding='12px';div.style.borderBottom='1px solid var(--border)';div.innerHTML=`<div><span class="badge badge-orange">${b.type}</span> <span>${b.type==='text'?b.content.substring(0,50):'Media file'}</span> <span class="badge ${b.status==='done'?'badge-green':b.status==='cancelled'?'badge-red':'badge-orange'}">${b.status}</span></div><button class="btn btn-danger btn-sm" onclick="deleteBroadcastItem(${b.id})"><i class="fas fa-trash"></i></button>`;listDiv.appendChild(div);});}}
function toggleBcastMedia(){const t=document.getElementById('bcastType').value;document.getElementById('bcastTextDiv').style.display=t==='text'?'block':'none';document.getElementById('bcastFileDiv').style.display=t!=='text'?'block':'none';}
async function sendBroadcast(){const type=document.getElementById('bcastType').value;const fd=new FormData();fd.append('msg_type',type);if(type==='text'){const text=document.getElementById('bcastText').value;if(!text)return toast('Enter message','error');fd.append('text_content',text);}else{const file=document.getElementById('bcastFile').files[0];if(!file)return toast('Select file','error');fd.append('media_file',file);}fd.append('caption',document.getElementById('bcastCaption').value);await apiForm('/api/broadcast',fd);toast('Broadcast queued');loadBroadcastHistory();}
async function cancelBroadcasts(){await api('POST','/api/broadcast/cancel');toast('Cancelled');loadBroadcastHistory();}
async function clearBroadcastHistory(){if(!confirm('Delete all broadcast history?'))return;await api('DELETE','/api/broadcast/clear_history');loadBroadcastHistory();toast('History cleared');}
async function deleteBroadcastItem(id){await api('DELETE',`/api/broadcast/${id}`);loadBroadcastHistory();}
async function loadResetDays(){const r=await api('GET','/api/reset_days');let html=`<div class="panel"><div class="panel-header"><span class="panel-title">Change Password</span></div><div class="panel-body"><input type="password" id="oldPwd" placeholder="Current password"><input type="password" id="newPwd" placeholder="New password"><button class="btn btn-primary" onclick="changePassword()">Update</button></div></div><div class="panel"><div class="panel-header"><span class="panel-title">Auto-Reset Sequence</span></div><div class="panel-body"><select id="resetMode"><option value="0">Restart on completion</option><option value="-1">Never restart</option><option value="custom">After inactivity (days)</option></select><div id="resetDaysDiv" style="display:none; margin-top:12px;"><input type="number" id="resetDays" placeholder="Days"></div><button class="btn btn-primary" onclick="saveResetDays()">Save</button></div></div>`;document.getElementById('content').innerHTML=html;const sel=document.getElementById('resetMode');if(r.days===0)sel.value='0';else if(r.days===-1)sel.value='-1';else{sel.value='custom';document.getElementById('resetDays').value=r.days;document.getElementById('resetDaysDiv').style.display='block';}sel.addEventListener('change',function(){document.getElementById('resetDaysDiv').style.display=this.value==='custom'?'block':'none';});}
async function saveResetDays(){const mode=document.getElementById('resetMode').value;let days;if(mode==='0')days=0;else if(mode==='-1')days=-1;else days=parseInt(document.getElementById('resetDays').value);await api('POST','/api/reset_days',{days});toast('Saved');}
async function changePassword(){const old=document.getElementById('oldPwd').value,nw=document.getElementById('newPwd').value;if(!old||!nw)return toast('Fill both fields','error');await api('POST','/api/change_password',{old_password:old,new_password:nw});toast('Password changed');}
async function loadEnv(){if(!currentUser?.is_main)return;const env=await api('GET','/api/env');document.getElementById('envApiId')?document.getElementById('envApiId').value=env.API_ID:null;document.getElementById('envApiHash')?document.getElementById('envApiHash').value=env.API_HASH:null;document.getElementById('envPhone')?document.getElementById('envPhone').value=env.PHONE_NUMBER:null;document.getElementById('envSourceChat')?document.getElementById('envSourceChat').value=env.SOURCE_CHAT_ID:null;document.getElementById('envPort')?document.getElementById('envPort').value=env.PORT:null;}
async function saveEnv(){const data={API_ID:document.getElementById('envApiId').value,API_HASH:document.getElementById('envApiHash').value,PHONE_NUMBER:document.getElementById('envPhone').value,SOURCE_CHAT_ID:document.getElementById('envSourceChat').value,PORT:document.getElementById('envPort').value};await api('POST','/api/update_env',data);toast('Saved. Restart required.');}
async function loadSubadmins(){if(!currentUser?.is_main)return;const subs=await api('GET','/api/subadmins');let html=`<div class="panel"><div class="panel-header"><span class="panel-title">Subadmins</span><button class="btn btn-primary" onclick="openModal('addSubModal')">Add</button></div><div class="table-wrapper"><table><thead><tr><th>Username</th><th>Role</th><th>Permissions</th><th>Actions</th></tr></thead><tbody>`;subs.forEach(s=>{const perms=Object.entries(s.permissions).filter(([k,v])=>v).map(([k])=>`<span class="badge badge-orange">${k}</span>`).join('');html+=`<tr><td>${s.username}</td><td>${s.is_main?'MAIN':'SUB'}</td><td>${perms}</td><td>${!s.is_main?`<button class="btn btn-danger btn-sm" onclick="deleteSubadmin(${s.id})">Del</button>`:'—'}</td></tr>`;});html+=`</tbody></table></div></div><div class="panel"><div class="panel-header"><span class="panel-title">Environment Variables</span></div><div class="panel-body"><div class="row"><input id="envApiId" placeholder="API_ID"><input id="envApiHash" placeholder="API_HASH"><input id="envPhone" placeholder="PHONE_NUMBER"><input id="envSourceChat" placeholder="SOURCE_CHAT_ID"><input id="envPort" placeholder="PORT"></div><button class="btn btn-warning" onclick="saveEnv()">Save (restart required)</button></div></div>`;document.getElementById('content').innerHTML=html;loadEnv();}
async function addSubadmin(){const u=document.getElementById('subUsername').value.trim(),p=document.getElementById('subPassword').value.trim();if(!u||!p)return toast('Fill all fields','error');const perms={};document.querySelectorAll('#addSubModal input[type=checkbox]').forEach(cb=>{perms[cb.value]=cb.checked;});const r=await api('POST','/api/add_subadmin',{username:u,password:p,permissions:perms});if(r.ok){closeModal('addSubModal');loadSubadmins();toast('Subadmin added');}else toast('Username taken','error');}
async function deleteSubadmin(id){await api('DELETE',`/api/subadmin/${id}`);loadSubadmins();toast('Deleted');}
// Additional event listeners for the main control page exist but fine.
</script>
</body>
</html>'''

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_root(path):
    return HTML_PAGE

if __name__ == '__main__':
    cleanup_temp_dirs()
    print_neon("=" * 60, 'green')
    print_neon("  BOT CONTROL PANEL – ULTIMATE", 'green')
    print_neon("  - Direct broadcast preserves original filenames", 'green')
    print_neon("  - QR login in terminal (fixed)", 'green')
    print_neon("  - Persistent session", 'green')
    print_neon("=" * 60, 'green')
    init_db()
    t = threading.Thread(target=start_client, daemon=True)
    t.start()
    print_neon("⏳  Starting Telegram client...", 'orange')
    time.sleep(3)
    print_neon("=" * 60, 'green')
    print_neon(f"  🌐  Web panel:  http://0.0.0.0:{PORT}", 'green')
    print_neon(f"  🔐  Login:      admin / admin123", 'orange')
    if not PHONE_NUMBER:
        print_neon("  📱  QR code printed above – scan it now!", 'orange')
    print_neon("=" * 60, 'green')
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
