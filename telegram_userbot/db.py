"""
Botning barcha holati (tanlangan chatlar, kutilayotgan amal, tasdiqlash tokenlari,
dialoglar keshi) shu modul orqali SQLite faylida saqlanadi.

Nega global Python lug'ati emas, SQLite?
- Bot qayta ishga tushsa ham (server restart, deploy, crash) tanlov va holat yo'qolmaydi.
- Kelajakda bir nechta egasi (OWNER_ID) qo'llab-quvvatlansa, har biri owner_id bo'yicha
  ajratilgan holda ishlaydi — holatlar aralashmaydi.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS selections (
    owner_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    PRIMARY KEY (owner_id, chat_id)
);

CREATE TABLE IF NOT EXISTS owner_state (
    owner_id INTEGER PRIMARY KEY,
    awaiting TEXT,
    awaiting_targets TEXT,
    confirm_token TEXT,
    confirm_action TEXT,
    confirm_expires REAL,
    confirm_targets TEXT
);

CREATE TABLE IF NOT EXISTS dialog_cache (
    owner_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS search_cache (
    owner_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (owner_id, category)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


@contextmanager
def _conn():
    # timeout: shu qadar soniya SQLITE_BUSY holatida avtomatik qayta uriniladi,
    # shundan keyingina "database is locked" xatosi tashlanadi.
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA busy_timeout=10000;")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        # Older releases keyed search_cache only by owner_id, so categories
        # overwrote each other. Migrate it once while preserving the cache.
        search_columns = c.execute("PRAGMA table_info(search_cache)").fetchall()
        if search_columns and not any(row[5] and row[1] == "category" for row in search_columns):
            c.execute("ALTER TABLE search_cache RENAME TO search_cache_legacy")
            c.execute("CREATE TABLE search_cache (owner_id INTEGER NOT NULL, category TEXT NOT NULL, payload TEXT NOT NULL, fetched_at REAL NOT NULL, PRIMARY KEY (owner_id, category))")
            c.execute("INSERT INTO search_cache SELECT owner_id, category, payload, fetched_at FROM search_cache_legacy")
            c.execute("DROP TABLE search_cache_legacy")
        c.executescript(_SCHEMA)
        # Eski bazalarda ustun yo'q bo'lishi mumkin — bor-yo'qligini tekshirib qo'shamiz.
        cols = {row[1] for row in c.execute("PRAGMA table_info(owner_state)").fetchall()}
        if "confirm_targets" not in cols:
            c.execute("ALTER TABLE owner_state ADD COLUMN confirm_targets TEXT")
        if "awaiting_targets" not in cols:
            c.execute("ALTER TABLE owner_state ADD COLUMN awaiting_targets TEXT")
    # Bazada tanlangan chatlar, tasdiqlash tokenlari kabi shaxsiy metadata bor —
    # faylni faqat egasi o'qiy oladigan qilib qo'yamiz (Windows'da bu no-op bo'lishi
    # mumkin, lekin Linux/macOS serverda muhim).
    try:
        os.chmod(config.DB_PATH, 0o600)
    except OSError:
        pass


# ---------- Tanlangan chatlar ----------

def get_selected(owner_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT chat_id, chat_name FROM selections WHERE owner_id=?", (owner_id,)
        ).fetchall()
    return {cid: name for cid, name in rows}


def toggle_selected(owner_id, chat_id, chat_name):
    with _conn() as c:
        exists = c.execute(
            "SELECT 1 FROM selections WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
        ).fetchone()
        if exists:
            c.execute(
                "DELETE FROM selections WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
            )
            return False
        else:
            c.execute(
                "INSERT INTO selections (owner_id, chat_id, chat_name) VALUES (?, ?, ?)",
                (owner_id, chat_id, chat_name),
            )
            return True


def add_selected_bulk(owner_id, chat_id_name_pairs):
    with _conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO selections (owner_id, chat_id, chat_name) VALUES (?, ?, ?)",
            [(owner_id, cid, name) for cid, name in chat_id_name_pairs],
        )


def remove_selected(owner_id, chat_id):
    with _conn() as c:
        c.execute(
            "DELETE FROM selections WHERE owner_id=? AND chat_id=?", (owner_id, chat_id)
        )


def clear_selected(owner_id, chat_ids=None):
    with _conn() as c:
        if chat_ids is None:
            c.execute("DELETE FROM selections WHERE owner_id=?", (owner_id,))
        elif chat_ids:
            placeholders = ",".join("?" for _ in chat_ids)
            c.execute(f"DELETE FROM selections WHERE owner_id=? AND chat_id IN ({placeholders})",
                      (owner_id, *chat_ids))


# ---------- Owner holati (awaiting input, tasdiqlash) ----------

def _ensure_row(c, owner_id):
    c.execute(
        "INSERT OR IGNORE INTO owner_state (owner_id, awaiting) VALUES (?, NULL)",
        (owner_id,),
    )


def set_awaiting(owner_id, value, targets=None):
    """
    targets: agar "awaiting" holati keyinroq bir guruh chatga nisbatan amal
    bajarishga olib kelsa (masalan, xabar matni kutilyapti), nishon chatlar
    ro'yxati SHU YERDA muzlatiladi. Shu bilan foydalanuvchi matn yozayotган
    paytda boshqa oynada tanlovni o'zgartirib qo'ysa ham, xabar aynan
    boshida ko'rsatilgan chatlarga boradi.
    """
    with _conn() as c:
        _ensure_row(c, owner_id)
        c.execute(
            "UPDATE owner_state SET awaiting=?, awaiting_targets=? WHERE owner_id=?",
            (value, json.dumps(targets) if targets is not None else None, owner_id),
        )


def get_awaiting(owner_id):
    with _conn() as c:
        row = c.execute(
            "SELECT awaiting FROM owner_state WHERE owner_id=?", (owner_id,)
        ).fetchone()
    return row[0] if row else None


def get_awaiting_targets(owner_id):
    with _conn() as c:
        row = c.execute(
            "SELECT awaiting_targets FROM owner_state WHERE owner_id=?", (owner_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def set_confirm(owner_id, token, action, ttl_seconds, targets=None):
    """
    targets: tasdiqlash so'ralgan paytdagi chat_id ro'yxatining SNAPSHOT'i (list of int).
    Bu ro'yxat tasdiqlash tokeni bilan birga saqlanadi va amal bajarilganda
    ANA SHU ro'yxat ishlatiladi — foydalanuvchi tasdiqlash oynasi ochiq turgan
    paytda eski tugmalar orqali tanlovni o'zgartirsa ham, amal boshqa chatlarga
    tegmaydi.
    """
    with _conn() as c:
        _ensure_row(c, owner_id)
        c.execute(
            "UPDATE owner_state SET confirm_token=?, confirm_action=?, confirm_expires=?, "
            "confirm_targets=? WHERE owner_id=?",
            (token, action, time.time() + ttl_seconds,
             json.dumps(targets) if targets is not None else None, owner_id),
        )


def check_and_consume_confirm(owner_id, token, action):
    """
    Tasdiqlash tokenini TEKSHIRADI va DARHOL bekor qiladi (bir marta ishlatiladigan token).
    Shu tufayli tugmani ikki marta bosish yoki eski tasdiqni qayta yuborish
    amalni ikki marta bajarmaydi.
    Returns (ok: bool, targets: list|None) — ok=True bo'lsa targets shu tasdiqlash
    so'ralgan paytdagi chat_id ro'yxati (snapshot bo'lmasa None).
    """
    with _conn() as c:
        row = c.execute(
            "SELECT confirm_token, confirm_action, confirm_expires, confirm_targets "
            "FROM owner_state WHERE owner_id=?",
            (owner_id,),
        ).fetchone()
        if not row:
            return False, None
        db_token, db_action, expires, targets_json = row
        # Har holatda tokenni tozalab qo'yamiz — qayta ishlatib bo'lmaydi
        c.execute(
            "UPDATE owner_state SET confirm_token=NULL, confirm_action=NULL, "
            "confirm_expires=NULL, confirm_targets=NULL WHERE owner_id=?",
            (owner_id,),
        )
        if not db_token or db_token != token or db_action != action:
            return False, None
        if expires is None or time.time() > expires:
            return False, None
        targets = json.loads(targets_json) if targets_json else None
        return True, targets


def clear_confirm(owner_id):
    with _conn() as c:
        _ensure_row(c, owner_id)
        c.execute(
            "UPDATE owner_state SET confirm_token=NULL, confirm_action=NULL, "
            "confirm_expires=NULL, confirm_targets=NULL WHERE owner_id=?",
            (owner_id,),
        )


# ---------- Dialoglar keshi ----------

def cache_dialogs(owner_id, dialogs_dict):
    with _conn() as c:
        c.execute(
            "INSERT INTO dialog_cache (owner_id, payload, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET payload=excluded.payload, "
            "fetched_at=excluded.fetched_at",
            (owner_id, json.dumps(dialogs_dict), time.time()),
        )


def get_cached_dialogs(owner_id, max_age_seconds):
    with _conn() as c:
        row = c.execute(
            "SELECT payload, fetched_at FROM dialog_cache WHERE owner_id=?", (owner_id,)
        ).fetchone()
    if not row:
        return None
    payload, fetched_at = row
    if time.time() - fetched_at > max_age_seconds:
        return None
    return json.loads(payload)


# ---------- Qidiruv natijalari keshi (pagination/toggle uchun) ----------

def set_search_results(owner_id, category, matches):
    with _conn() as c:
        c.execute(
            "INSERT INTO search_cache (owner_id, category, payload, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id, category) DO UPDATE SET "
            "payload=excluded.payload, fetched_at=excluded.fetched_at",
            (owner_id, category, json.dumps(matches), time.time()),
        )


def get_search_results(owner_id, category, max_age_seconds=1800):
    with _conn() as c:
        row = c.execute(
            "SELECT payload, fetched_at FROM search_cache WHERE owner_id=? AND category=?", (owner_id, category)
        ).fetchone()
    if not row:
        return None
    payload, fetched_at = row
    if time.time() - fetched_at > max_age_seconds:
        return None
    return json.loads(payload)


def add_audit_log(owner_id, action, details):
    with _conn() as c:
        c.execute("INSERT INTO audit_log (owner_id, action, details, created_at) VALUES (?, ?, ?, ?)",
                  # Audit must never turn a completed action into a crash just
                  # because a third-party object found its way into diagnostics.
                  (owner_id, action, json.dumps(details, default=str), time.time()))


def get_audit_log(owner_id, limit=20):
    with _conn() as c:
        return c.execute("SELECT action, details, created_at FROM audit_log WHERE owner_id=? "
                         "ORDER BY id DESC LIMIT ?", (owner_id, limit)).fetchall()
