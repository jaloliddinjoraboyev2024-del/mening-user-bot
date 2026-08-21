import os
import sys
from dotenv import load_dotenv

load_dotenv()


def _get_int(name, default=None, required=False, minimum=None, maximum=None):
    val = os.getenv(name)
    if val is None or val == "":
        if required:
            print(f"XATOLIK: .env faylida {name} to'ldirilmagan.", file=sys.stderr)
            sys.exit(1)
        return default
    try:
        result = int(val)
        if minimum is not None and result < minimum:
            raise ValueError(f"kamida {minimum}")
        if maximum is not None and result > maximum:
            raise ValueError(f"ko'pi bilan {maximum}")
        return result
    except ValueError:
        print(f"XATOLIK: {name} butun son bo'lishi kerak, olindi: {val!r}", file=sys.stderr)
        sys.exit(1)


def _get_str(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and not val:
        print(f"XATOLIK: .env faylida {name} to'ldirilmagan.", file=sys.stderr)
        sys.exit(1)
    return val


# my.telegram.org saytidan olinadigan API ma'lumotlari
API_ID = _get_int("API_ID", required=True)
API_HASH = _get_str("API_HASH", required=True)

# Sizning shaxsiy akkauntingiz raqami (masalan +998901234567)
PHONE = _get_str("PHONE", default="")

# @BotFather orqali yaratilgan boshqaruv boti tokeni
BOT_TOKEN = _get_str("BOT_TOKEN", required=True)

# Faqat shu Telegram user_id buyruq bera oladi
OWNER_ID = _get_int("OWNER_ID", required=True)

# --- Xavfsizlik va ishlash sozlamalari ---

# Dialoglar ro'yxati necha soniya keshda turadi (default: 5 daqiqa)
DIALOG_CACHE_TTL = _get_int("DIALOG_CACHE_TTL", default=300, minimum=1, maximum=86400)

# Xabar yuborishlar orasidagi minimal kechikish (soniya)
BROADCAST_BASE_DELAY = _get_int("BROADCAST_BASE_DELAY", default=3, minimum=1, maximum=300)

# Shundan ko'p chatga yuborishdan oldin qo'shimcha tasdiqlash so'raladi
BROADCAST_CONFIRM_THRESHOLD = _get_int("BROADCAST_CONFIRM_THRESHOLD", default=10, minimum=1, maximum=10000)

# Bitta ishga tushirishda maksimal necha ta chatga yuborish mumkin (spam-himoya)
MAX_BROADCAST_TARGETS = _get_int("MAX_BROADCAST_TARGETS", default=100, minimum=1, maximum=10000)

# Tasdiqlash tokeni necha soniyadan keyin muddati o'tadi
CONFIRM_TOKEN_TTL = _get_int("CONFIRM_TOKEN_TTL", default=120, minimum=10, maximum=3600)

# SQLite fayli va log fayli manzili
DB_PATH = _get_str("DB_PATH", default="bot_state.db")
LOG_FILE = _get_str("LOG_FILE", default="bot.log")

# Bitta yuboriladigan media fayl uchun maksimal hajm (MB). Fayl avval xotiraga
# to'liq yuklanadi (bot va userbot sessiyalari alohida bo'lgani uchun), shuning
# uchun bu chegara serverning RAM sig'imiga qarab tanlanishi kerak.
MAX_MEDIA_SIZE_MB = _get_int("MAX_MEDIA_SIZE_MB", default=50, minimum=1, maximum=2000)

# Destructive actions are capped independently from broadcasts.
MAX_DELETE_TARGETS = _get_int("MAX_DELETE_TARGETS", default=100, minimum=1, maximum=10000)

# QR login uchun vaqtinchalik QR fayli saqlanadigan joy.
QR_FILE = _get_str("QR_FILE", default="telegram_login_qr.png")
QR_REFRESH_SECONDS = _get_int("QR_REFRESH_SECONDS", default=120, minimum=30, maximum=300)
