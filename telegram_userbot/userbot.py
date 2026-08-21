"""
Telethon orqali shaxsiy akkauntga bog'liq amallar.
Har bir funksiya bitta chatdagi xatoni butun ishni to'xtatmasdan qaytaradi —
chaqiruvchi kod har bir chat uchun natijani alohida ko'radi.
"""

import asyncio
import logging

from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UserBannedInChannelError,
    UserIsBlockedError,
    PeerIdInvalidError,
    RPCError,
)
from telethon.tl.types import Chat, Channel, User

log = logging.getLogger("userbot")

# Telegramning bitta xabar uchun matn limiti. Undan katta matn API xatosi bilan
# rad etiladi, shuning uchun yuborishdan oldin tekshirib/bo'lib yuborish kerak.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

# Har bir chatda FloodWait tufayli qancha kutish mumkinligi (soniya). Telegram
# ba'zan juda uzoq (soatlab) FloodWait berishi mumkin — shunday holatda cheksiz
# kutib turmasdan, shu chatni "xato" deb belgilab, keyingisiga o'tamiz.
MAX_FLOODWAIT_SLEEP = 300
DELETE_BATCH_SIZE = 100


async def interruptible_sleep(seconds, cancel_check=None):
    """Sleep in short intervals so the Stop button takes effect promptly."""
    remaining = max(0.0, float(seconds))
    while remaining:
        if cancel_check and cancel_check():
            raise asyncio.CancelledError
        interval = min(0.5, remaining)
        await asyncio.sleep(interval)
        remaining -= interval


async def categorize_dialogs(client):
    """Barcha suhbatlarni guruh, kanal, shaxsiy chat va bot turlariga ajratadi.
    Har bir element {"id", "name"} shaklida — JSON keshga solish uchun oddiy tuzilma.
    "Saved Messages" (o'zingiz bilan suhbat) chiqarib tashlanadi — ommaviy yuborish/
    o'chirish ro'yxatida bo'lishi shart emas va tasodifan tanlanib qolmasligi kerak."""
    groups, channels, private, bots = [], [], [], []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, User) and getattr(entity, "is_self", False):
            continue
        item = {"id": dialog.id, "name": dialog.name or "Noma'lum"}
        if isinstance(entity, User):
            (bots if entity.bot else private).append(item)
        elif isinstance(entity, Chat):
            groups.append(item)
        elif isinstance(entity, Channel):
            (groups if entity.megagroup else channels).append(item)
    return {"groups": groups, "channels": channels, "private": private, "bots": bots}


async def check_delete_all_permission(client, chat_id):
    """
    'Barchasini o'chirish' dan oldin haqiqatan ham huquq bor-yo'qligini tekshiradi.
    Guruh/kanal a'zosi bo'lsangiz-u, admin/delete-huquqingiz bo'lmasa, oldindan
    ogohlantiradi — jarayonni behuda boshlamaydi.
    Returns (ok: bool, reason: str)
    """
    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:
        return False, f"chat topilmadi: {e}"

    if isinstance(entity, User):
        # Shaxsiy chatda "hammasini o'chirish" tushunchasi yo'q — faqat o'z xabarlaringiz
        return False, "shaxsiy chatda faqat o'z xabarlaringizni o'chira olasiz"

    try:
        perms = await client.get_permissions(entity, "me")
    except Exception as e:
        return False, f"huquqlarni tekshirib bo'lmadi: {e}"

    if perms.is_creator:
        return True, "egasisiz"
    if perms.is_admin and getattr(perms, "delete_messages", False):
        return True, "admin, o'chirish huquqi bor"
    if perms.is_admin:
        return False, "admin, lekin 'delete messages' huquqi berilmagan"
    return False, "admin emassiz — faqat o'z xabarlaringiz o'chadi"


async def estimate_message_count(client, chat_id):
    """Chatdagi taxminiy umumiy xabar sonini qaytaradi (tasdiqlash oynasida ko'rsatish uchun)."""
    try:
        result = await client.get_messages(chat_id, limit=1)
        return result.total if hasattr(result, "total") else None
    except Exception:
        return None


async def _delete_loop(client, chat_id, message_iter, cancel_check=None):
    """delete_my_messages va delete_all_messages uchun umumiy ichki tsikl.
    Returns (count, error_or_None). error faqat "hech bo'lmasa bitta muammo bo'ldi"
    degan umumiy signal — foydalanuvchiga qaysi turdagi xato ekani ko'rsatiladi,
    lekin butun jarayon davom etaveradi (bitta xabar o'chmasa ham)."""
    count = 0
    rpc_error_count = 0
    last_rpc_error = None
    batch = []

    async def flush_batch():
        nonlocal count, rpc_error_count, last_rpc_error, batch
        while batch:
            if cancel_check and cancel_check():
                raise asyncio.CancelledError
            try:
                await client.delete_messages(chat_id, batch)
                count += len(batch)
                batch = []
                return
            except FloodWaitError as e:
                if e.seconds > MAX_FLOODWAIT_SLEEP:
                    raise RuntimeError(f"FloodWait {e.seconds}s juda uzun, chat o'tkazib yuborildi")
                log.warning("FloodWait %s soniya (chat=%s)", e.seconds, chat_id)
                await interruptible_sleep(e.seconds, cancel_check)
            except RPCError as e:
                rpc_error_count += len(batch)
                last_rpc_error = type(e).__name__
                batch = []
                return

    async for msg in message_iter:
        if cancel_check and cancel_check():
            note = "foydalanuvchi to'xtatdi"
            if rpc_error_count:
                note += f" ({rpc_error_count} ta xabar o'chirilmadi: {last_rpc_error})"
            return count, note
        batch.append(msg.id)
        if len(batch) < DELETE_BATCH_SIZE:
            continue
        try:
            await flush_batch()
        except asyncio.CancelledError:
            return count, "foydalanuvchi to'xtatdi"
        except RuntimeError as e:
            return count, str(e)
    if batch and not (cancel_check and cancel_check()):
        try:
            await flush_batch()
        except asyncio.CancelledError:
            return count, "foydalanuvchi to'xtatdi"
        except RuntimeError as e:
            return count, str(e)
    error = None
    if rpc_error_count:
        error = f"{rpc_error_count} ta xabar o'chmadi (masalan: {last_rpc_error})"
    return count, error


async def delete_my_messages(client, chat_id, cancel_check=None):
    """Berilgan chatda faqat egasining xabarlarini o'chiradi.

    Telegram/kanal tarixida ``from_user`` filtri ayrim holatlarda kutilgancha
    natija bermasligi mumkin. Shuning uchun avval tezkor server filtri ishlatiladi;
    natija bo'lmasa, yaqindagi tarix bo'yicha sender ID bilan tekshiriladigan fallback
    ishlaydi. Bu yangi yozilgan xabarlarni keyingi ishga tushirishda ham topishga
    yordam beradi.
    """
    try:
        me = await client.get_me()
        # Fast path.
        fast = client.iter_messages(chat_id, from_user=me.id)
        count, err = await _delete_loop(client, chat_id, fast, cancel_check)
        if count > 0 or err is not None or (cancel_check and cancel_check()):
            return count, err

        # Fallback: Telegram filter empty bo'lsa, tarixni ko'rib sender ID bilan
        # ajratamiz. Bu cheksiz tarix emas; kerakli yangi xabarlar uchun katta,
        # lekin boshqariladigan limit ishlatiladi.
        async def mine():
            async for msg in client.iter_messages(chat_id, limit=1000):
                if cancel_check and cancel_check():
                    break
                sender_id = getattr(msg, "sender_id", None)
                if sender_id == me.id:
                    yield msg

        return await _delete_loop(client, chat_id, mine(), cancel_check)
    except asyncio.CancelledError:
        return 0, "foydalanuvchi to'xtatdi"
    except Exception as e:
        log.exception("delete_my_messages xato: chat=%s", chat_id)
        return 0, str(e)


async def delete_all_messages(client, chat_id, cancel_check=None):
    """
    Chatdagi BARCHA xabarlarni o'chiradi. Chaqiruvchi kod bundan oldin
    check_delete_all_permission() ni chaqirishi kerak — bu funksiya faqat bajaradi.
    Returns (count, error_or_None).
    """
    try:
        return await _delete_loop(client, chat_id, client.iter_messages(chat_id), cancel_check)
    except asyncio.CancelledError:
        return 0, "foydalanuvchi to'xtatdi"
    except Exception as e:
        log.exception("delete_all_messages xato: chat=%s", chat_id)
        return 0, str(e)


_RECOVERABLE_SEND_ERRORS = (
    ChatWriteForbiddenError,
    ChannelPrivateError,
    UserBannedInChannelError,
    UserIsBlockedError,
    PeerIdInvalidError,
)


async def send_to_chat(client, chat_id, text=None, file=None, max_retries=1, cancel_check=None):
    """
    Bitta chatga xabar (matn va/yoki media) yuboradi.
    FloodWaitError bo'lsa, ko'rsatilgan vaqtcha kutib, bir marta qayta urinadi.
    Returns (ok: bool, detail: str) — detail muvaffaqiyatsizlik sababi yoki "ok".
    """
    if not file and not text:
        return False, "bo'sh xabar yuborilmaydi"
    attempt = 0
    while True:
        try:
            if file:
                if hasattr(file, "seek"):
                    file.seek(0)
                await client.send_file(chat_id, file, caption=text or "")
            else:
                await client.send_message(chat_id, text)
            return True, "ok"
        except FloodWaitError as e:
            attempt += 1
            if e.seconds > MAX_FLOODWAIT_SLEEP:
                return False, f"FloodWait {e.seconds}s juda uzun, o'tkazib yuborildi"
            if attempt > max_retries:
                return False, f"FloodWait: {e.seconds}s kutish kerak edi, urinishlar tugadi"
            log.warning("FloodWait %s soniya, kutyapmiz (chat=%s)", e.seconds, chat_id)
            await interruptible_sleep(e.seconds, cancel_check)
        except asyncio.CancelledError:
            return False, "bekor qilindi"
        except _RECOVERABLE_SEND_ERRORS as e:
            return False, f"yuborib bo'lmaydi: {type(e).__name__}"
        except Exception as e:
            log.exception("send_to_chat kutilmagan xato: chat=%s", chat_id)
            return False, f"kutilmagan xato: {e}"


async def broadcast(client, chat_ids, text=None, file=None, base_delay=3, cancel_check=None,
                     progress_cb=None):
    """
    Bir nechta chatga ketma-ket yuboradi, har biri uchun natijani alohida qayd etadi.
    Adaptiv kechikish: FloodWait uchraganda keyingi yuborishlar oldidan kechikish oshiriladi.
    Returns list of dict: {"chat_id", "ok", "detail"}
    """
    results = []
    delay = base_delay
    for i, chat_id in enumerate(chat_ids):
        if cancel_check and cancel_check():
            break
        ok, detail = await send_to_chat(client, chat_id, text=text, file=file,
                                        cancel_check=cancel_check)
        if detail == "bekor qilindi":
            break
        results.append({"chat_id": chat_id, "ok": ok, "detail": detail})
        if "FloodWait" in detail:
            delay = min(delay * 2, 60)  # keyingi safar sekinroq yuboramiz
        if progress_cb:
            await progress_cb(i + 1, len(chat_ids))
        if i < len(chat_ids) - 1:
            try:
                await interruptible_sleep(delay, cancel_check)
            except asyncio.CancelledError:
                break
    return results
