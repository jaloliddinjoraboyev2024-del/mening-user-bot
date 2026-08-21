import asyncio
import getpass
import io
import json
import logging
import os
import re
import secrets
import signal
import stat
import sys
import tempfile
from datetime import datetime
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient, events, Button, functions
from telethon.errors import MessageNotModifiedError
from telethon.errors import SessionPasswordNeededError
from filelock import FileLock, Timeout as FileLockTimeout
import qrcode

import config
import db
import userbot

def _ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


_ensure_parent_dir(config.LOG_FILE)
_ensure_parent_dir(config.DB_PATH)
_ensure_parent_dir(config.QR_FILE)

# ---------- Logging: fayl + konsol, foydalanuvchiga esa faqat qisqa xabar ----------
# Log fayli chat ID va owner ID kabi metadata saqlaydi (xabar matni yoki chat nomlari
# emas). Baribir bu shaxsiy metadata, shuning uchun faylni faqat egasi o'qiy oladigan
# huquq (0600) bilan yaratamiz.
_log_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
try:
    os.chmod(config.LOG_FILE, stat.S_IRUSR | stat.S_IWUSR)
except OSError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        _log_handler,
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bot")

user_client = TelegramClient("user_session", config.API_ID, config.API_HASH)
bot_client = TelegramClient("bot_session", config.API_ID, config.API_HASH)

PAGE_SIZE = 8
CATEGORY_TITLES = {
    "groups": "👥 Guruhlar",
    "channels": "📢 Kanallar",
    "private": "💬 Shaxsiy chatlar",
    "bots": "🤖 Botlar",
}

# Faqat runtime davomida kerak bo'lgan, saqlanishi shart bo'lmagan holat:
LOCKS = {}          # owner_id -> asyncio.Lock  (bitta amal bir vaqtda faqat 1 marta ishlashi uchun)
CANCEL_FLAGS = {}   # owner_id -> bool           (uzun amalni to'xtatish uchun)


def get_lock(owner_id):
    if owner_id not in LOCKS:
        LOCKS[owner_id] = asyncio.Lock()
    return LOCKS[owner_id]


async def safe_edit(event, *args, **kwargs):
    """Edit callback message without treating an idempotent edit as a crash.

    Telegram returns MessageNotModifiedError when the requested text/buttons are
    already identical to the current message. This is a normal race condition
    (for example double-clicking a button), not an application failure.
    """
    try:
        return await event.edit(*args, **kwargs)
    except MessageNotModifiedError:
        log.debug("Callback edit skipped: message was already up to date")
        return None


def owner_only(handler):
    async def wrapper(event):
        # Faqat shaxsiy (private) chatda, va faqat OWNER_ID dan. Bot biror guruhga
        # qo'shilib qolsa ham, guruhdagi buyruq/tugma bosishlariga javob bermaydi —
        # aks holda boshqaruv interfeysi (tanlangan chatlar, tasdiqlash tugmalari)
        # guruhda ochilib, tasodifiy bosish butun akkauntga ta'sir qilishi mumkin edi.
        if event.sender_id != config.OWNER_ID or not event.is_private:
            return
        try:
            await handler(event)
        except Exception:
            log.exception("Handler xatosi (owner=%s)", event.sender_id)
            try:
                await event.respond(
                    "⚠️ Kutilmagan ichki xatolik yuz berdi. Tafsilotlar log faylida "
                    "saqlandi, ma'muriyat ko'rib chiqadi."
                )
            except Exception:
                pass
    return wrapper


# ---------------------------------------------------------------------------
# Dialoglarni olish (kesh bilan)
# ---------------------------------------------------------------------------

async def ensure_dialogs(owner_id, force=False):
    if not force:
        cached = db.get_cached_dialogs(owner_id, config.DIALOG_CACHE_TTL)
        if cached is not None:
            return cached
    dialogs = await userbot.categorize_dialogs(user_client)
    db.cache_dialogs(owner_id, dialogs)
    log.info("Dialoglar yangilandi (owner=%s): %s guruh, %s kanal, %s shaxsiy, %s bot",
              owner_id, len(dialogs["groups"]), len(dialogs["channels"]),
              len(dialogs["private"]), len(dialogs["bots"]))
    return dialogs


# ---------------------------------------------------------------------------
# Klaviaturalar
# ---------------------------------------------------------------------------

def _back(button=b"menu:main"):
    return [Button.inline("⬅️ Orqaga", button)]


def main_menu_buttons(owner_id):
    n = len(db.get_selected(owner_id))
    return [
        [Button.inline("🎯 Chatlar bilan ishlash", b"menu:categories")],
        [Button.inline(f"📌 Tanlanganlar ({n})", b"menu:selected")],
        [Button.inline("⚡ Amallar markazi", b"menu:actions")],
        [Button.inline("📊 Holat va statistika", b"menu:status")],
        [Button.inline("🔐 Xavfsizlik / Sessiya", b"menu:security")],
        [Button.inline("📖 Yordam / Qo'llanma", b"menu:help")],
        [Button.inline("🔄 Chatlar ro'yxatini yangilash", b"menu:refresh")],
    ]


def category_buttons(dialogs):
    rows = []
    for key, title in CATEGORY_TITLES.items():
        count = len(dialogs.get(key, []))
        rows.append([Button.inline(f"{title}  •  {count} ta", f"cat:{key}:0".encode())])
    rows += [
        [Button.inline("🔎 Istalgan chatni nomi bo'yicha qidirish", b"search:all")],
        [Button.inline("📌 Tanlanganlarni ko'rish", b"menu:selected")],
        [Button.inline("⬅️ Bosh menyu", b"menu:main")],
    ]
    return rows


def chat_list_buttons(dialogs, category, page, owner_id, items=None, search_mode=False):
    """Render a stable, paginated chat picker. Selection is persistent until the owner removes it."""
    source = list(items) if items is not None else list(dialogs.get(category, []))
    page = max(0, int(page))
    start = page * PAGE_SIZE
    chunk = source[start:start + PAGE_SIZE]
    selected = db.get_selected(owner_id)
    rows = []
    for item in chunk:
        cid = int(item["id"])
        name = (item.get("name") or str(cid)).strip()
        mark = "✅" if cid in selected else "⬜"
        prefix = "stoggle" if search_mode else "toggle"
        rows.append([Button.inline(f"{mark} {name[:40]}", f"{prefix}:{category}:{page}:{cid}".encode())])

    nav = []
    if page > 0:
        prefix = "scat" if search_mode else "cat"
        nav.append(Button.inline("◀️ Oldingi", f"{prefix}:{category}:{page - 1}".encode()))
    if start + PAGE_SIZE < len(source):
        prefix = "scat" if search_mode else "cat"
        nav.append(Button.inline("Keyingi ▶️", f"{prefix}:{category}:{page + 1}".encode()))
    if nav:
        rows.append(nav)

    if search_mode:
        rows.append([Button.inline("☑️ Natijalarning barchasini tanlash", f"selallsearch:{category}".encode())])
    else:
        rows.append([Button.inline("☑️ Barchasini tanlash", f"selall:{category}".encode())])
    rows.append([Button.inline("⬜ Shu kategoriyadagi tanlovni bekor qilish", f"deselall:{category}".encode())])
    rows.append([Button.inline(f"📌 Tanlanganlar ({len(selected)})", b"menu:selected")])
    rows.append([Button.inline("⬅️ Kategoriyalar", b"menu:categories")])
    return rows


def action_buttons():
    return [
        [Button.inline("📤 Navbat bilan 1 marta yuborish", b"action:send")],
        [Button.inline("🧹 Faqat o'z xabarlarimni o'chirish", b"action:clear")],
        [Button.inline("🧹🧹 Barcha xabarlarni o'chirish", b"action:clearall")],
        [Button.inline("📜 Amal tarixi", b"action:audit")],
        [Button.inline("📌 Tanlangan chatlarni boshqarish", b"menu:selected")],
        [Button.inline("⬅️ Bosh menyu", b"menu:main")],
    ]


def status_buttons():
    return [
        [Button.inline("🔄 Dialoglarni yangilash", b"menu:refresh")],
        [Button.inline("📜 Amal tarixini ko'rish", b"action:audit")],
        [Button.inline("🔐 Sessiya holati", b"menu:security")],
        [Button.inline("⬅️ Bosh menyu", b"menu:main")],
    ]


def security_buttons():
    return [
        [Button.inline("🔐 Userbot sessiyasini tekshirish", b"security:check")],
        [Button.inline("🔄 Userbotni qayta ulash", b"security:reconnect")],
        [Button.inline("🔑 QR-login haqida", b"security:qr")],
        [Button.inline("⬅️ Bosh menyu", b"menu:main")],
    ]


def help_buttons():
    return [
        [Button.inline("🎯 Chat tanlash qanday ishlaydi?", b"help:select")],
        [Button.inline("📤 Xabar yuborish qanday ishlaydi?", b"help:send")],
        [Button.inline("🧹 O'chirish qanday ishlaydi?", b"help:delete")],
        [Button.inline("🔐 QR-login qanday ishlaydi?", b"help:qr")],
        [Button.inline("🛡️ Xavfsizlik qoidalari", b"help:safety")],
        [Button.inline("⬅️ Bosh menyu", b"menu:main")],
    ]

def confirm_buttons(action, token):
    return [
        [Button.inline("✅ Ha, tasdiqlayman", f"confirm:{action}:{token}".encode())],
        [Button.inline("❌ Bekor qilish", b"menu:actions")],
    ]


def cancel_button():
    return [[Button.inline("🛑 To'xtatish", b"cancelop")]]


_CALLBACK_RE = re.compile(
    r"(?:menu:(?:main|refresh|clearsel|categories|viewsel|selected|actions|status|security|help)|action:(?:send|clear|clearall|audit)|"
    r"cancelop|cat:(?:groups|channels|private|bots):\d+|scat:(?:groups|channels|private|bots):\d+|"
    r"(?:toggle|stoggle):(groups|channels|private|bots):\d+:-?\d+|(?:selall|deselall|selallsearch|search):(all|groups|channels|private|bots)|"
    r"unselect:-?\d+:\d+|viewsel:\d+|security:(?:check|reconnect|qr)|help:(?:select|send|delete|qr|safety)|confirm:(?:clear|clearall|send_bulk):[0-9a-f]{32})$"
)


def valid_callback(data):
    """Reject stale/corrupt callback payloads before parsing pages or ids."""
    if data in {"menu:main", "menu:refresh", "menu:clearsel", "menu:categories", "menu:viewsel",
                "action:send", "action:clear", "action:clearall", "action:audit", "cancelop"}:
        return True
    return bool(_CALLBACK_RE.fullmatch(data))


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@bot_client.on(events.NewMessage(pattern="/start"))
@owner_only
async def start_handler(event):
    owner_id = event.sender_id
    db.set_awaiting(owner_id, None)
    db.clear_confirm(owner_id)
    await ensure_dialogs(owner_id)  # keshni oldindan isitib qo'yamiz (natija shart emas)
    n = len(db.get_selected(owner_id))
    await event.respond(
        f"🤖 Akkauntingizni boshqarish boti tayyor.\n"
        f"📌 Hozir tanlangan chatlar: {n}\n\n"
        f"Kerakli chatlarni tanlang, so'ng ular ustida qanday amal bajarishni tanlaysiz.",
        buttons=main_menu_buttons(owner_id),
    )


# ---------------------------------------------------------------------------
# Callback (tugma) handler
# ---------------------------------------------------------------------------

@bot_client.on(events.CallbackQuery())
@owner_only
async def callback_handler(event):
    owner_id = event.sender_id
    try:
        data = event.data.decode("utf-8")
    except UnicodeDecodeError:
        await event.answer("Yaroqsiz tugma.", alert=True)
        return
    if not valid_callback(data):
        await event.answer("Bu tugma eskirgan yoki yaroqsiz.", alert=True)
        return

    if data == "menu:main":
        n = len(db.get_selected(owner_id))
        await safe_edit(event, f"📌 Tanlangan chatlar: {n}\n\nBosh menyu:", buttons=main_menu_buttons(owner_id))
        return

    if data == "menu:refresh":
        await event.answer("Yangilanmoqda...")
        dialogs = await ensure_dialogs(owner_id, force=True)
        await safe_edit(event, 
            f"✅ Ro'yxat yangilandi.\n📌 Tanlangan: {len(db.get_selected(owner_id))}\n\nBosh menyu:",
            buttons=main_menu_buttons(owner_id),
        )
        return

    if data == "menu:clearsel":
        db.clear_selected(owner_id)
        await event.answer("Tanlov tozalandi")
        await safe_edit(event, "📌 Tanlangan chatlar: 0\n\nBosh menyu:", buttons=main_menu_buttons(owner_id))
        return

    if data == "menu:selected":
        await _reshow_viewsel(event, owner_id, 0)
        return

    if data == "menu:actions":
        if not db.get_selected(owner_id):
            await safe_edit(event, "📌 Hozircha chat tanlanmagan. Avval chatlarni tanlang.",
                            buttons=[[Button.inline("🎯 Chatlarni tanlash", b"menu:categories")],
                                     [Button.inline("⬅️ Bosh menyu", b"menu:main")]])
            return
        await safe_edit(event,
            f"⚡ <b>Amallar markazi</b>\n\n📌 Tanlangan: {len(db.get_selected(owner_id))} ta\n\n"
            "Bitta vazifani tanlang. Keyingi bosqichlar alohida tasdiqlash oynalari orqali ochiladi.",
            buttons=action_buttons(), parse_mode="html")
        return

    if data == "menu:status":
        selected = db.get_selected(owner_id)
        cached = db.get_cached_dialogs(owner_id, config.DIALOG_CACHE_TTL)
        if cached:
            total = sum(len(v) for v in cached.values())
            breakdown = (f"👥 Guruhlar: {len(cached['groups'])}\n📢 Kanallar: {len(cached['channels'])}\n"
                         f"💬 Shaxsiy: {len(cached['private'])}\n🤖 Botlar: {len(cached['bots'])}")
        else:
            total, breakdown = 0, "Ro'yxat hali yuklanmagan."
        lock_state = "🔴 Band" if get_lock(owner_id).locked() else "🟢 Bo'sh"
        await safe_edit(event,
            f"📊 <b>Holat va statistika</b>\n\n📚 Jami dialoglar: {total}\n📌 Tanlangan: {len(selected)}\n⚙️ Amal holati: {lock_state}\n\n{breakdown}",
            buttons=status_buttons(), parse_mode="html")
        return

    if data == "menu:security":
        authorized = await user_client.is_user_authorized() if user_client.is_connected() else False
        state = "🟢 Ulangan va avtorizatsiyalangan" if authorized else "🟡 Ulanish/autorizatsiya kerak"
        await safe_edit(event, f"🔐 <b>Xavfsizlik / Sessiya</b>\n\nUserbot: {state}\n\n"
                        "QR-login faqat kerak bo'lganda ishga tushadi. Sessiya fayli va 2FA paroli bot orqali ko'rsatilmaydi.",
                        buttons=security_buttons(), parse_mode="html")
        return

    if data == "menu:help":
        await safe_edit(event, "📖 <b>Yordam markazi</b>\n\nKerakli bo'limni tanlang:", buttons=help_buttons(), parse_mode="html")
        return

    if data.startswith("security:"):
        action = data.split(":", 1)[1]
        if action == "check":
            authorized = await user_client.is_user_authorized() if user_client.is_connected() else False
            await safe_edit(event, f"🔐 Sessiya tekshiruvi\n\n{'🟢 Faol' if authorized else '🔴 Faol emas'}", buttons=security_buttons())
            return
        if action == "reconnect":
            if get_lock(owner_id).locked():
                await event.answer("Hozir boshqa amal bajarilyapti.", alert=True)
                return
            await safe_edit(event, "🔄 Userbot sessiyasi qayta ulanmoqda...", buttons=_back(b"menu:security"))
            try:
                if user_client.is_connected():
                    await user_client.disconnect()
                await _ensure_user_authorized()
                await safe_edit(event, "✅ Userbot muvaffaqiyatli qayta ulandi.", buttons=security_buttons())
            except Exception:
                log.exception("Userbot reconnect xatosi")
                await safe_edit(event, "❌ Qayta ulanish amalga oshmadi. Logni tekshiring.", buttons=security_buttons())
            return
        if action == "qr":
            await safe_edit(event, "🔑 <b>QR-login</b>\n\nYangi sessiya avtorizatsiya qilinmagan bo'lsa, dastur avtomatik QR yaratadi va ownerga yuboradi. QR muddati tugasa yangisi yaratiladi. 2FA yoqilgan bo'lsa parol faqat terminalda so'raladi.", buttons=security_buttons(), parse_mode="html")
            return

    if data.startswith("help:"):
        topic = data.split(":", 1)[1]
        texts = {
            "select": "🎯 <b>Chat tanlash</b>\n\nKategoriyani oching → chatni bosing → ☑️ belgisi tanlanganini bildiradi. Kerak bo'lsa sahifalar orqali davom eting.",
            "send": "📤 <b>Navbat bilan 1 marta yuborish</b>\n\nTanlangan chatlarga ketma-ket yuboriladi. Har bir chat alohida natija beradi, xatolik keyingi chatga ta'sir qilmaydi. Jarayonni 🛑 To'xtatish tugmasi bilan bekor qilish mumkin.",
            "delete": "🧹 <b>O'chirish</b>\n\nIkki alohida amal bor: faqat o'zingiz yozgan xabarlarni o'chirish yoki huquq mavjud bo'lsa barcha xabarlarni o'chirish. Qaytarib bo'lmaydi va tasdiqlash talab qilinadi.",
            "qr": "🔐 <b>QR-login</b>\n\nYangi user session uchun QR yuboriladi. QR eskirsa avtomatik yangilanadi. 2FA paroli terminalda yashirin kiritiladi.",
            "safety": "🛡️ <b>Xavfsizlik</b>\n\nFaqat owner boshqaradi. Destruktiv amallar tasdiqlanadi. Bitta amal bir vaqtda ishlaydi. Media vaqtinchalik faylga yuklanadi va tugagach o'chiriladi.",
        }
        await safe_edit(event, texts[topic], buttons=help_buttons(), parse_mode="html")
        return

    if data == "menu:categories":
        dialogs = await ensure_dialogs(owner_id)
        await safe_edit(event, "Qaysi turdagi chatlardan tanlaysiz?", buttons=category_buttons(dialogs))
        return

    if data == "menu:viewsel" or data.startswith("viewsel:"):
        page = int(data.split(":")[1]) if data.startswith("viewsel:") else 0
        await _reshow_viewsel(event, owner_id, page)
        return

    if data.startswith("unselect:"):
        _, cid, page = data.split(":")
        db.remove_selected(owner_id, int(cid))
        await _reshow_viewsel(event, owner_id, int(page))
        return

    if data == "menu:actions":
        if not db.get_selected(owner_id):
            await event.answer("Avval kamida bitta chat tanlang!", alert=True)
            return
        await safe_edit(event, 
            f"✅ Tanlangan chatlar: {len(db.get_selected(owner_id))} ta\n\nNima qilamiz?",
            buttons=action_buttons(),
        )
        return

    if data.startswith("cat:"):
        _, category, page = data.split(":")
        dialogs = await ensure_dialogs(owner_id)
        await safe_edit(event, 
            f"{CATEGORY_TITLES[category]} — chatni bosib tanlang/bekor qiling:",
            buttons=chat_list_buttons(dialogs, category, int(page), owner_id),
        )
        return

    if data.startswith("toggle:"):
        _, category, page, chat_id = data.split(":")
        chat_id = int(chat_id)
        dialogs = await ensure_dialogs(owner_id)
        name = next((d["name"] for d in dialogs[category] if d["id"] == chat_id), str(chat_id))
        db.toggle_selected(owner_id, chat_id, name)
        await safe_edit(event, 
            f"{CATEGORY_TITLES[category]} — chatni bosib tanlang/bekor qiling:",
            buttons=chat_list_buttons(dialogs, category, int(page), owner_id),
        )
        return

    if data.startswith("selall:"):
        category = data.split(":")[1]
        dialogs = await ensure_dialogs(owner_id)
        db.add_selected_bulk(owner_id, [(d["id"], d["name"]) for d in dialogs[category]])
        await event.answer(f"{CATEGORY_TITLES[category]} — barchasi tanlandi ✅")
        await safe_edit(event, 
            f"{CATEGORY_TITLES[category]} — chatni bosib tanlang/bekor qiling:",
            buttons=chat_list_buttons(dialogs, category, 0, owner_id),
        )
        return

    if data.startswith("deselall:"):
        category = data.split(":")[1]
        dialogs = await ensure_dialogs(owner_id)
        ids = {d["id"] for d in dialogs[category]}
        for cid in list(db.get_selected(owner_id).keys()):
            if cid in ids:
                db.remove_selected(owner_id, cid)
        await event.answer(f"{CATEGORY_TITLES[category]} — tanlov bekor qilindi")
        await safe_edit(event, 
            f"{CATEGORY_TITLES[category]} — chatni bosib tanlang/bekor qiling:",
            buttons=chat_list_buttons(dialogs, category, 0, owner_id),
        )
        return

    if data.startswith("selallsearch:"):
        category = data.split(":")[1]
        matches = db.get_search_results(owner_id, category)
        if not matches:
            await event.answer("Qidiruv natijalari eskirgan, qaytadan qidiring.", alert=True)
            return
        db.add_selected_bulk(owner_id, [(d["id"], d["name"]) for d in matches])
        await event.answer("Qidiruv natijalarining barchasi tanlandi ✅")
        dialogs = await ensure_dialogs(owner_id)
        await safe_edit(event, 
            f"🔎 {len(matches)} ta natija:",
            buttons=chat_list_buttons(dialogs, category, 0, owner_id, items=matches, search_mode=True),
        )
        return

    if data.startswith("scat:"):
        _, category, page = data.split(":")
        matches = db.get_search_results(owner_id, category)
        if not matches:
            await event.answer("Qidiruv natijalari eskirgan, qaytadan qidiring.", alert=True)
            return
        dialogs = await ensure_dialogs(owner_id)
        await safe_edit(event, 
            f"🔎 {len(matches)} ta natija:",
            buttons=chat_list_buttons(dialogs, category, int(page), owner_id, items=matches, search_mode=True),
        )
        return

    if data.startswith("stoggle:"):
        _, category, page, chat_id = data.split(":")
        chat_id = int(chat_id)
        matches = db.get_search_results(owner_id, category)
        if not matches:
            await event.answer("Qidiruv natijalari eskirgan, qaytadan qidiring.", alert=True)
            return
        name = next((d["name"] for d in matches if d["id"] == chat_id), str(chat_id))
        db.toggle_selected(owner_id, chat_id, name)
        dialogs = await ensure_dialogs(owner_id)
        await safe_edit(event, 
            f"🔎 {len(matches)} ta natija:",
            buttons=chat_list_buttons(dialogs, category, int(page), owner_id, items=matches, search_mode=True),
        )
        return

    if data == "search:all":
        await safe_edit(event, "🔎 Qidirish uchun kategoriya tanlang:", buttons=[
            [Button.inline("👥 Guruhlar", b"search:groups"), Button.inline("📢 Kanallar", b"search:channels")],
            [Button.inline("💬 Shaxsiy", b"search:private"), Button.inline("🤖 Botlar", b"search:bots")],
            [Button.inline("⬅️ Orqaga", b"menu:categories")],
        ])
        return

    if data.startswith("search:"):
        category = data.split(":")[1]
        db.set_awaiting(owner_id, f"search:{category}")
        await safe_edit(event, 
            f"🔎 {CATEGORY_TITLES[category]} ichidan qidirmoqchi bo'lgan nom qismini yozing.\n"
            "(Bekor qilish uchun pastdagi 'Bosh menyu' tugmasini bosing)"
        )
        return

    if data == "action:send":
        selected = db.get_selected(owner_id)
        if len(selected) > config.MAX_BROADCAST_TARGETS:
            await safe_edit(event, 
                f"⚠️ Siz {len(selected)} ta chat tanlagansiz, bu bitta ishga tushirish uchun "
                f"belgilangan maksimal ({config.MAX_BROADCAST_TARGETS}) dan ko'p. "
                f"Spam-himoya sifatida tanlovni kamaytiring.",
                buttons=[[Button.inline("⬅️ Orqaga", b"menu:actions")]],
            )
            return
        if len(selected) > config.BROADCAST_CONFIRM_THRESHOLD:
            names = "\n".join(f"• {n}" for n in list(selected.values())[:15])
            more = f"\n… va yana {len(selected) - 15} ta" if len(selected) > 15 else ""
            token = secrets.token_hex(16)
            # Nishon chatlar shu yerda "muzlatiladi" — tasdiqlash oynasi ochiq turgan
            # paytda tanlov o'zgarsa ham, aynan shu ro'yxat ishlatiladi.
            db.set_confirm(owner_id, token, "send_bulk", config.CONFIRM_TOKEN_TTL,
                            targets=list(selected.items()))
            await safe_edit(event, 
                f"⚠️ {len(selected)} ta chatga yubormoqchisiz — bu chegaradan ({config.BROADCAST_CONFIRM_THRESHOLD}) ko'p.\n\n"
                f"Ro'yxat:\n{names}{more}\n\nDavom etasizmi?",
                buttons=confirm_buttons("send_bulk", token),
            )
            return
        db.set_awaiting(owner_id, "send_content", targets=list(selected.items()))
        await safe_edit(event, 
            f"✍️ {len(selected)} ta tanlangan chatga yuboriladigan xabarni yozing yoki "
            f"rasm/fayl/video ni caption bilan yuboring.\n\n(Bekor qilish uchun pastdagi 'Bosh menyu' tugmasini bosing)"
        )
        return

    if data == "action:audit":
        rows = db.get_audit_log(owner_id, 10)
        if not rows:
            await safe_edit(event, "📜 Hali saqlangan amal tarixi yo'q.", buttons=action_buttons())
            return
        lines = []
        for action, details_json, created_at in rows:
            details = json.loads(details_json)
            if action == "broadcast":
                summary = f"yuborildi {details.get('success', 0)}, bekor: {details.get('cancelled', False)}"
            else:
                summary = f"o'chirildi {details.get('messages_deleted', 0)}, bekor: {details.get('cancelled', False)}"
            lines.append(f"• {action}: {summary}")
        await safe_edit(event, "📜 So'nggi 10 amal:\n" + "\n".join(lines), buttons=action_buttons())
        return

    if data == "action:clear":
        selected = db.get_selected(owner_id)
        if len(selected) > config.MAX_DELETE_TARGETS:
            await event.answer(f"O'chirish bir martada {config.MAX_DELETE_TARGETS} ta chat bilan cheklangan.", alert=True)
            return
        token = secrets.token_hex(16)
        db.set_confirm(owner_id, token, "clear", config.CONFIRM_TOKEN_TTL,
                        targets=list(selected.items()))
        await safe_edit(event, 
            f"🧹 {len(selected)} ta tanlangan chatda FAQAT o'zingiz yozgan xabarlar o'chiriladi. "
            f"Bu amalni QAYTARIB BO'LMAYDI. Tasdiqlaysizmi?",
            buttons=confirm_buttons("clear", token),
        )
        return

    if data == "action:clearall":
        await safe_edit(event, "🔎 Har bir chatda huquqingiz tekshirilmoqda, biroz kuting...")
        selected = db.get_selected(owner_id)
        if len(selected) > config.MAX_DELETE_TARGETS:
            await safe_edit(event, f"⚠️ O'chirish bir martada {config.MAX_DELETE_TARGETS} ta chat bilan cheklangan.",
                             buttons=[[Button.inline("⬅️ Orqaga", b"menu:actions")]])
            return
        eligible, blocked = [], []
        for cid, name in selected.items():
            ok, reason = await userbot.check_delete_all_permission(user_client, cid)
            (eligible if ok else blocked).append((cid, name, reason))
        if not eligible:
            await safe_edit(event, 
                "❌ Tanlangan chatlarning hech birida 'barchasini o'chirish' huquqi yo'q.\n\n"
                + "\n".join(f"• {n}: {r}" for _, n, r in blocked[:15]),
                buttons=[[Button.inline("⬅️ Orqaga", b"menu:actions")]],
            )
            return
        # Har bir eligible chatda taxminiy xabar sonini olib, foydalanuvchiga
        # nima ko'lamda o'chirilishini ko'rsatamiz (aniq raqam emas, taxminiy).
        estimates = {}
        for cid, name, _ in eligible[:15]:
            estimates[cid] = await userbot.estimate_message_count(user_client, cid)

        token = secrets.token_hex(16)
        # DIQQAT: selection (db.get_selected) endi bu yerda O'ZGARTIRILMAYDI — foydalanuvchi
        # tanlagan chatlar tanlash menyusida xuddi shunday qolaveradi. Faqat huquqi
        # tasdiqlangan `eligible` ro'yxati tasdiqlash tokeni bilan bog'lab saqlanadi
        # va aynan shu ro'yxat bo'yicha o'chirish amalga oshiriladi.
        db.set_confirm(owner_id, token, "clearall", config.CONFIRM_TOKEN_TTL,
                        targets=[[cid, name] for cid, name, _ in eligible])
        text = f"✅ Huquq bor: {len(eligible)} ta chat.\n"
        est_lines = [f"• {n}: ~{estimates[cid]} xabar" for cid, n, _ in eligible[:15]
                     if estimates.get(cid) is not None]
        if est_lines:
            text += "\n📊 Taxminiy hajm:\n" + "\n".join(est_lines)
            if len(eligible) > 15:
                text += f"\n… va yana {len(eligible) - 15} ta chat"
        if blocked:
            text += f"\n⚠️ Huquq yo'q (o'tkazib yuboriladi): {len(blocked)} ta:\n"
            text += "\n".join(f"• {n}: {r}" for _, n, r in blocked[:10])
        text += "\n\nBARCHA xabarlar o'chiriladi, QAYTARIB BO'LMAYDI. Tasdiqlaysizmi?"
        await safe_edit(event, text, buttons=confirm_buttons("clearall", token))
        return

    if data == "cancelop":
        CANCEL_FLAGS[owner_id] = True
        await event.answer("To'xtatilmoqda...")
        return

    if data.startswith("confirm:"):
        _, action, token = data.split(":")
        ok, targets = db.check_and_consume_confirm(owner_id, token, action)
        if not ok:
            await event.answer(
                "Bu tasdiqlash eskirgan yoki allaqachon bajarilgan — qaytadan boshlang.",
                alert=True,
            )
            await safe_edit(event, "Bosh menyu:", buttons=main_menu_buttons(owner_id))
            return
        # targets — aynan tasdiqlash so'ralgan paytdagi chat ro'yxati (list of [cid, name]).
        # Hozirgi tanlov (db.get_selected) bilan farq qilishi mumkin, shuning uchun
        # KEYINGI amallarning barchasi shu snapshot'dan foydalanadi.
        chat_pairs = [(cid, name) for cid, name in (targets or [])]

        lock = get_lock(owner_id)
        if lock.locked():
            await event.answer("Boshqa amal hali bajarilyapti, kuting.", alert=True)
            return

        async with lock:
            CANCEL_FLAGS[owner_id] = False
            if action == "clear":
                await _run_delete(event, owner_id, chat_pairs, all_messages=False)
            elif action == "clearall":
                # Permission may have changed while the confirmation was open.
                allowed = []
                denied = []
                for cid, name in chat_pairs:
                    permitted, reason = await userbot.check_delete_all_permission(user_client, cid)
                    (allowed if permitted else denied).append((cid, name, reason))
                if not allowed:
                    await safe_edit(event, "❌ Tasdiqlashdan keyin huquqlar o'zgargan: o'chirish boshlanmadi.",
                                     buttons=main_menu_buttons(owner_id))
                    return
                await _run_delete(event, owner_id, [(cid, name) for cid, name, _ in allowed], all_messages=True,
                                  skipped=denied)
            elif action == "send_bulk":
                db.set_awaiting(owner_id, "send_content", targets=chat_pairs)
                await safe_edit(event, 
                    f"✍️ {len(chat_pairs)} ta chatga yuboriladigan xabarni yozing "
                    f"yoki media yuboring.\n\n(Bekor qilish uchun pastdagi Bosh menyu tugmasini bosing)"
                )
        return


VIEWSEL_PAGE_SIZE = 30


async def _reshow_viewsel(event, owner_id, page=0):
    selected = list(db.get_selected(owner_id).items())
    if not selected:
        await safe_edit(event, "👁 Tanlanganlar: 0", buttons=[[Button.inline("⬅️ Orqaga", b"menu:main")]])
        return
    start = page * VIEWSEL_PAGE_SIZE
    chunk = selected[start:start + VIEWSEL_PAGE_SIZE]
    rows = []
    for cid, name in chunk:
        label = (name or str(cid))[:28]
        rows.append([Button.inline(f"❌ {label}", f"unselect:{cid}:{page}".encode())])
    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ Oldingi", f"viewsel:{page - 1}".encode()))
    if start + VIEWSEL_PAGE_SIZE < len(selected):
        nav.append(Button.inline("Keyingi ▶️", f"viewsel:{page + 1}".encode()))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("⬅️ Orqaga", b"menu:main")])
    await safe_edit(event, f"👁 Tanlanganlar ({len(selected)}):", buttons=rows)


async def _run_delete(event, owner_id, chat_ids, all_messages, skipped=None):
    total = len(chat_ids)
    await safe_edit(event, f"⏳ 0/{total} chat qayta ishlandi...", buttons=cancel_button())

    def cancel_check():
        return CANCEL_FLAGS.get(owner_id, False)

    deleted_total = 0
    per_chat_errors = [f"• {name}: huquq qayta tekshiruvdan o'tmadi ({reason})"
                       for _, name, reason in (skipped or [])]
    for i, (cid, name) in enumerate(chat_ids, start=1):
        if cancel_check():
            break
        if all_messages:
            count, err = await userbot.delete_all_messages(user_client, cid, cancel_check=cancel_check)
        else:
            count, err = await userbot.delete_my_messages(user_client, cid, cancel_check=cancel_check)
        deleted_total += count
        if err:
            per_chat_errors.append(f"• {name}: {err}")
        if i % max(1, total // 10) == 0 or i == total:
            try:
                await safe_edit(event, f"⏳ {i}/{total} chat qayta ishlandi... ({deleted_total} xabar o'chirildi)",
                                  buttons=cancel_button())
            except Exception:
                pass

    log.info("Delete tugadi (owner=%s, all=%s): %s ta xabar, %s ta xato",
              owner_id, all_messages, deleted_total, len(per_chat_errors))
    # Tanlangan chatlar o'chirishdan keyin ham saqlanadi. Foydalanuvchi ularni
    # alohida "Tanlanganlar" menyusidan olib tashlaydi.
    text = f"✅ Tayyor. Jami {deleted_total} ta xabar o'chirildi ({len(chat_ids)} ta chatda)."
    if cancel_check():
        text = f"🛑 To'xtatildi. Shu vaqtgacha {deleted_total} ta xabar o'chirildi."
    if per_chat_errors:
        text += "\n\n⚠️ Xatoliklar:\n" + "\n".join(per_chat_errors[:10])
        if len(per_chat_errors) > 10:
            text += f"\n… va yana {len(per_chat_errors) - 10} ta"
    db.add_audit_log(owner_id, "clearall" if all_messages else "clear", {
        "targets": [cid for cid, _ in chat_ids], "messages_deleted": deleted_total,
        "cancelled": cancel_check(), "errors": per_chat_errors,
    })
    await safe_edit(event, text, buttons=main_menu_buttons(owner_id))


# ---------------------------------------------------------------------------
# Oddiy matn / media xabarlar (faqat "kutilayotgan input" holatlarida ishlaydi)
# ---------------------------------------------------------------------------

@bot_client.on(events.NewMessage())
@owner_only
async def message_handler(event):
    owner_id = event.sender_id
    if event.raw_text.startswith("/"):
        return

    awaiting = db.get_awaiting(owner_id)
    if not awaiting:
        return  # kutilmagan oddiy xabar — e'tiborsiz qoldiramiz

    if awaiting.startswith("search:"):
        category = awaiting.split(":")[1]
        query = event.raw_text.strip().lower()
        dialogs = await ensure_dialogs(owner_id)
        matches = [d for d in dialogs[category] if query in d["name"].lower()]
        db.set_awaiting(owner_id, None)
        if not matches:
            await event.respond("Hech narsa topilmadi.", buttons=[[Button.inline("⬅️ Orqaga", f"cat:{category}:0".encode())]])
            return
        db.set_search_results(owner_id, category, matches)
        await event.respond(
            f"🔎 '{event.raw_text}' bo'yicha {len(matches)} ta topildi:",
            buttons=chat_list_buttons(dialogs, category, 0, owner_id, items=matches, search_mode=True),
        )
        return

    if awaiting == "send_content":
        lock = get_lock(owner_id)
        if lock.locked():
            await event.respond("Boshqa amal hali bajarilyapti, biroz kuting.")
            return
        targets = db.get_awaiting_targets(owner_id) or list(db.get_selected(owner_id).items())
        chat_ids = [cid for cid, _ in targets]

        if not chat_ids:
            await event.respond("⚠️ Nishon chatlar topilmadi (tanlov bekor qilingan bo'lishi mumkin). "
                                 "/start orqali qaytadan boshlang.")
            return

        text = event.raw_text or None
        file = None
        temp_path = None
        if event.message.media:
            size = getattr(event.message.file, "size", None)
            max_bytes = config.MAX_MEDIA_SIZE_MB * 1024 * 1024
            if size is not None and size > max_bytes:
                await event.respond(
                    f"⚠️ Fayl juda katta ({size / 1024 / 1024:.1f} MB). "
                    f"Limit: {config.MAX_MEDIA_SIZE_MB} MB (.env dagi MAX_MEDIA_SIZE_MB). "
                    f"Katta fayl bot xotirasini to'ldirib, jarayonni yiqitishi mumkin."
                )
                return
            text = event.message.text or None
            if text and len(text) > userbot.TELEGRAM_CAPTION_LIMIT:
                await event.respond(f"⚠️ Media captioni {userbot.TELEGRAM_CAPTION_LIMIT} belgidan oshmasligi kerak.")
                return
            # Download in bounded chunks to a private temporary file. This also
            # enforces the limit when Telegram did not supply a file size.
            fd, temp_path = tempfile.mkstemp(prefix="telegram_userbot_", suffix=".upload")
            os.close(fd)
            written = 0
            try:
                with open(temp_path, "wb") as stream:
                    async for chunk in bot_client.iter_download(event.message.media, request_size=64 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("media_limit")
                        stream.write(chunk)
                if os.name == "posix":
                    os.chmod(temp_path, stat.S_IRUSR | stat.S_IWUSR)
                file = temp_path
            except ValueError:
                os.unlink(temp_path)
                await event.respond(f"⚠️ Fayl {config.MAX_MEDIA_SIZE_MB} MB limitidan katta. Kichikroq fayl yuboring.")
                return
            except Exception:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                log.exception("Media yuklab olinmadi")
                await event.respond("⚠️ Media yuklab olinmadi. Qayta urinib ko'ring.")
                return
        elif text and len(text) > userbot.TELEGRAM_TEXT_LIMIT:
            await event.respond(
                f"⚠️ Xabar {len(text)} belgi — Telegram limiti "
                f"({userbot.TELEGRAM_TEXT_LIMIT}) dan katta. Qisqartiring va qayta yuboring."
            )
            db.set_awaiting(owner_id, "send_content", targets=targets)
            return
        elif not text:
            await event.respond("⚠️ Matn yoki media yuboring.")
            return

        db.set_awaiting(owner_id, None)

        status_msg = await event.respond(f"📤 0/{len(chat_ids)} yuborildi...", buttons=cancel_button())

        def cancel_check():
            return CANCEL_FLAGS.get(owner_id, False)

        async def progress(done, total):
            try:
                await status_msg.edit(f"📤 {done}/{total} yuborildi...", buttons=cancel_button())
            except Exception:
                pass

        try:
            async with lock:
                CANCEL_FLAGS[owner_id] = False
                results = await userbot.broadcast(
                    user_client, chat_ids, text=text, file=file,
                    base_delay=config.BROADCAST_BASE_DELAY,
                    cancel_check=cancel_check, progress_cb=progress,
                )
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

        ok_count = sum(1 for r in results if r["ok"])
        failed = [r for r in results if not r["ok"]]
        log.info("Broadcast tugadi (owner=%s): %s/%s muvaffaqiyatli", owner_id, ok_count, len(results))
        cancelled = cancel_check()
        report = ("🛑 Jarayon bekor qilindi. " if cancelled else "✅ Jarayon yakunlandi. ") + f"Yuborildi: {ok_count}/{len(chat_ids)}"
        if failed:
            report += "\n\n⚠️ Yuborilmaganlar:\n" + "\n".join(
                f"• {r['chat_id']}: {r['detail']}" for r in failed[:10]
            )
            if len(failed) > 10:
                report += f"\n… va yana {len(failed) - 10} ta"
        db.add_audit_log(owner_id, "broadcast", {"targets": chat_ids, "success": ok_count,
                                                   "failed": failed, "cancelled": cancelled})
        await status_msg.edit(report, buttons=main_menu_buttons(owner_id))
        return


# ---------------------------------------------------------------------------
# Ishga tushirish va to'xtatish
# ---------------------------------------------------------------------------

_LOCK_FILE = config.DB_PATH + ".lock"
_INSTANCE_LOCK = None


def _release_single_instance_lock():
    """Release the OS-level lock. The lock file itself may remain; that is safe."""
    global _INSTANCE_LOCK
    if _INSTANCE_LOCK is None:
        return
    try:
        _INSTANCE_LOCK.release()
    except Exception:
        log.exception("Instance lock release xatosi")
    finally:
        _INSTANCE_LOCK = None


def _acquire_single_instance_lock():
    """
    PID-faylni qo'lda tekshirib/o'chirish o'rniga haqiqiy OS-level file lock ishlatiladi.
    Process crash qilsa ham lock avtomatik bo'shaydi; eski lock faylini qo'lda o'chirish
    talab qilinmaydi. Windows va POSIX uchun filelock platformaga mos mexanizmni tanlaydi.
    """
    global _INSTANCE_LOCK
    lock = FileLock(_LOCK_FILE, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout:
        print("XATOLIK: bot allaqachon ishlamoqda.", file=sys.stderr)
        raise SystemExit(1)
    except OSError as exc:
        print(f"XATOLIK: bot lock faylini egallab bo'lmadi: {exc}", file=sys.stderr)
        raise SystemExit(1)
    _INSTANCE_LOCK = lock


def _qr_remaining_seconds(qr_login):
    expires = qr_login.expires
    now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
    return max(5.0, (expires - now).total_seconds())


async def _show_qr_to_owner(qr_login):
    """QR URL'ni vaqtinchalik PNG qilib faqat OWNER_ID ga yuboradi."""
    path = config.QR_FILE
    img = qrcode.make(qr_login.url)
    img.save(path)
    try:
        if os.name == "posix":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    caption = (
        "🔐 Telegram QR-login\n\n"
        "1) Telefoningizda Telegram → Settings → Devices → Link Desktop Device.\n"
        "2) Shu QR kodni skaner qiling.\n"
        "3) Login tasdiqlangach, bu QR avtomatik bekor qilinadi.\n\n"
        "⚠️ QR kodni boshqa odamga yubormang."
    )
    try:
        await bot_client.send_file(config.OWNER_ID, path, caption=caption)
        log.info("QR login kodi ownerga yuborildi.")
    except Exception:
        # Bot bilan chat hali ochilmagan bo'lishi mumkin. QR tokenni logga yozmaymiz.
        log.exception("QR kodni ownerga yuborib bo'lmadi")
        print("QR-login uchun vaqtinchalik QR fayl:", os.path.abspath(path))
        print("QR URL maxfiy: uni boshqa odamga bermang.")


async def _ensure_user_authorized():
    """
    Mavjud session ishlatiladi. Yangi session bo'lsa SMS/kod so'ramasdan QR-login
    boshlanadi. QR muddati tugasa yangi QR avtomatik yaratiladi. 2FA yoqilgan bo'lsa,
    parol terminalda getpass orqali olinadi va saqlanmaydi.
    """
    await user_client.connect()
    if await user_client.is_user_authorized():
        return

    log.info("User session avtorizatsiya qilinmagan; QR-login boshlanmoqda.")
    while not await user_client.is_user_authorized():
        qr_login = await user_client.qr_login()
        await _show_qr_to_owner(qr_login)
        try:
            await asyncio.wait_for(qr_login.wait(), timeout=_qr_remaining_seconds(qr_login) + 5)
        except asyncio.TimeoutError:
            log.info("QR muddati tugadi; yangi QR yaratiladi.")
            continue
        except SessionPasswordNeededError:
            print("Telegram 2FA paroli kerak. Parol ekranda ko'rsatilmaydi.")
            password = getpass.getpass("Telegram 2FA paroli: ")
            if not password:
                raise RuntimeError("2FA paroli bo'sh bo'lishi mumkin emas.")
            await user_client.sign_in(password=password)
        finally:
            try:
                if os.path.exists(config.QR_FILE):
                    os.remove(config.QR_FILE)
            except OSError:
                log.warning("QR vaqtinchalik faylini o'chirib bo'lmadi: %s", config.QR_FILE)

    log.info("Userbot QR-login orqali muvaffaqiyatli ulandi.")


async def _presence_keepalive(stop_event):
    """Maintain an active user session while the process is running.

    This does not bypass Telegram privacy/rate limits; it only refreshes the
    connected account status while the application itself remains online.
    """
    while not stop_event.is_set():
        try:
            if user_client.is_connected() and await user_client.is_user_authorized():
                await user_client(functions.account.UpdateStatusRequest(offline=False))
        except Exception:
            log.exception("Presence keepalive xatosi")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=45)
        except asyncio.TimeoutError:
            pass


async def main():
    _acquire_single_instance_lock()
    db.init_db()

    # Control botni avval ishga tushiramiz: birinchi login paytida QR ownerga
    # shu bot orqali yuborilishi mumkin.
    try:
        await bot_client.start(bot_token=config.BOT_TOKEN)
        log.info("Boshqaruv boti ishga tushdi.")
        await _ensure_user_authorized()
        log.info("Userbot (shaxsiy akkaunt) ulandi.")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _graceful_stop(*_):
            log.info("To'xtatish signali qabul qilindi, yakunlanmoqda...")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _graceful_stop)
            except (NotImplementedError, RuntimeError):
                pass

        async def wait_and_disconnect():
            await stop_event.wait()
            await bot_client.disconnect()
            await user_client.disconnect()

        await asyncio.gather(
            user_client.run_until_disconnected(),
            bot_client.run_until_disconnected(),
            wait_and_disconnect(),
            _presence_keepalive(stop_event),
        )
    finally:
        for client in (bot_client, user_client):
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                log.exception("Client disconnect xatosi")
        try:
            if os.path.exists(config.QR_FILE):
                os.remove(config.QR_FILE)
        except OSError:
            log.warning("QR vaqtinchalik faylini yakunda o'chirib bo'lmadi.")
        _release_single_instance_lock()
    log.info("Bot to'xtatildi.")


if __name__ == "__main__":
    asyncio.run(main())
