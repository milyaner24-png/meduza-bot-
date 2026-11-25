import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
import importlib

from pathlib import Path
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

import supra_2jz_gte

# =====================================================
# 1. ASOSIY SOZLAMALAR
# =====================================================

VERSION = "1.1.0"

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or "BU_YERGA_TOKENINGNI_YOZ"
)

ADMIN_ID = 8381887071

SECURITY_MODULE_NAME = "Xavfsizlik_markazi"

DATA_DIR = Path(os.getenv("SUPRA_DATA_DIR", ".")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)

REGISTRATION_FILE = DATA_DIR / "main_registrations.json"
SETTINGS_FILE = DATA_DIR / "main_settings.json"
ERROR_FILE = DATA_DIR / "main_errors.json"
NOTIFICATION_FILE = DATA_DIR / "main_notifications.json"

RETRY_INTERVAL = 10
HEALTH_INTERVAL = 30
MAX_ERROR_HISTORY = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("SUPRA_MAIN")

# =====================================================
# 2. ASOSIY OBYEKTLAR
# =====================================================

bot = None
dp = Dispatcher()
router = Router(name="main_router")

supra_gateway = supra_2jz_gte.supra_gateway

security_module = None
admin_module = None
users_module = None
settings_module = None

security_ready = False
supra_ready = False
admin_ready = False
business_modules_ready = False
server_running = False

gateway_connected_modules = set()
BUILTIN_MODULES = {
    "Xavfsizlik_markazi": [
        "security_status",
        "security_incidents",
        "security_permissions",
        "security_army",
        "security_restrictions",
        "admin_panel_close",
    ],
    "foydalanuvchilar": [
        "register_user",
        "get_user",
        "get_user_count",
    ],
    "sozlamalar": [
        "get_setting",
        "stage_change",
        "confirm_change",
        "reject_change",
        "freeze_setting",
        "set_channel_group",
        "set_order_limit",
        "set_daily_bonus",
        "set_premium_tariff",
        "sync_pending",
    ],
}

main_lock = asyncio.Lock()
registration_lock = asyncio.Lock()

registration_states = {}

settings = {
    "important_notifications": True,
    "error_notifications": True,
    "repeat_notifications": True,
    "muted_fingerprints": [],
}

error_history = []
notification_history = {}

health_alerted = set()

# =====================================================
# 3. VAQT VA JSON
# =====================================================

def current_time():
    return datetime.now(timezone.utc).isoformat()

def load_json(path, default):
    if not path.exists():
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)

    except Exception:
        logger.exception(
            "JSON faylini o'qishda xatolik: %s",
            path,
        )
        return default

def save_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=4,
            )

            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, path)

    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

def load_local_data():
    global registration_states
    global settings
    global error_history
    global notification_history

    registrations = load_json(
        REGISTRATION_FILE,
        {},
    )

    loaded_settings = load_json(
        SETTINGS_FILE,
        {},
    )

    errors = load_json(
        ERROR_FILE,
        [],
    )

    notifications = load_json(
        NOTIFICATION_FILE,
        {},
    )

    if isinstance(registrations, dict):
        registration_states = registrations

    if isinstance(loaded_settings, dict):
        for key in (
            "important_notifications",
            "error_notifications",
            "repeat_notifications",
        ):
            value = loaded_settings.get(key)

            if isinstance(value, bool):
                settings[key] = value

        muted = loaded_settings.get("muted_fingerprints", [])

        if isinstance(muted, list):
            settings["muted_fingerprints"] = muted

    if isinstance(errors, list):
        error_history = errors[-MAX_ERROR_HISTORY:]

    if isinstance(notifications, dict):
        notification_history = notifications

async def save_registrations():
    async with main_lock:
        save_json(
            REGISTRATION_FILE,
            registration_states,
        )

async def save_settings():
    async with main_lock:
        save_json(
            SETTINGS_FILE,
            settings,
        )

async def save_errors():
    async with main_lock:
        save_json(
            ERROR_FILE,
            error_history[-MAX_ERROR_HISTORY:],
        )

async def save_notifications():
    async with main_lock:
        save_json(
            NOTIFICATION_FILE,
            notification_history,
        )

# =====================================================
# 4. BILDIRISHNOMA TAKRORLANISHINI BOSHQARISH
# =====================================================

def notification_fingerprint(text, notification_type):
    raw = f"{notification_type}:{text}"

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()

def notification_keyboard(notification_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="TUSHUNDIM",
                    callback_data=f"ack:{notification_id}",
                ),
                InlineKeyboardButton(
                    text="TAKRORLAMASLIK",
                    callback_data=f"mute:{notification_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="XATOLIKNI KO'RISH",
                    callback_data=f"view:{notification_id}",
                )
            ],
        ]
    )

async def notify_admin(
    text,
    notification_id=None,
    notification_type="important",
    reply_markup=None,
    **send_kwargs,
):
    if not bot:
        return False

    if notification_type == "error":
        if not settings["error_notifications"]:
            return False

    elif not settings["important_notifications"]:
        return False

    fingerprint = notification_fingerprint(
        text,
        notification_type,
    )

    muted = settings.get(
        "muted_fingerprints",
        [],
    )

    if (
        not settings["repeat_notifications"]
        and fingerprint in muted
    ):
        return False

    if fingerprint in muted:
        return False

    if notification_id is None:
        notification_id = str(uuid.uuid4())

    notification_history[notification_id] = {
        "text": str(text),
        "type": notification_type,
        "fingerprint": fingerprint,
        "acknowledged": False,
        "muted": False,
        "time": current_time(),
    }

    try:
        await save_notifications()

        markup = (
            reply_markup
            if reply_markup is not None
            else notification_keyboard(notification_id)
        )

        await bot.send_message(
            ADMIN_ID,
            text,
            reply_markup=markup,
            **send_kwargs,
        )

        return True

    except Exception:
        logger.exception(
            "Admin bildirishnomasi yuborilmadi."
        )
        return False

# =====================================================
# 5. XATOLIKLAR VA HODISALAR JURNALI
# =====================================================

async def record_event(
    title,
    description,
    event_type="MAIN_EVENT",
    notification_type="important",
    forward_to_security=True,
):
    event = {
        "id": str(uuid.uuid4()),
        "source": "main.py",
        "type": event_type,
        "title": str(title),
        "description": str(description),
        "time": current_time(),
        "timestamp": time.time(),
    }

    error_history.append(event)

    if len(error_history) > MAX_ERROR_HISTORY:
        del error_history[:-MAX_ERROR_HISTORY]

    try:
        await save_errors()
    except Exception:
        logger.exception(
            "Hodisani jurnalga saqlab bo'lmadi."
        )

    if event_type.endswith("ERROR"):
        logger.error(
            "%s | %s",
            title,
            description,
        )
    else:
        logger.info(
            "%s | %s",
            title,
            description,
        )

    if (
        forward_to_security
        and security_ready
        and security_module
    ):
        reporter = getattr(
            security_module,
            "report_incident",
            None,
        )

        if callable(reporter):
            try:
                result = reporter(event)

                if asyncio.iscoroutine(result):
                    await result

            except Exception:
                logger.exception(
                    "Xavfsizlik markaziga hodisa yuborilmadi."
                )

    await notify_admin(
        f"{title}\n\n{description}",
        notification_id=event["id"],
        notification_type=notification_type,
    )

    return event

async def log_error(
    title,
    description,
    event_type="MAIN_ERROR",
    forward_to_security=True,
):
    return await record_event(
        title=title,
        description=description,
        event_type=event_type,
        notification_type="error",
        forward_to_security=forward_to_security,
    )

# SUPRA yoki Xavfsizlik markazidan keladigan
# bitta argumentli hodisalarni qabul qilish adapteri.

async def report_security_event(*args, **kwargs):
    if not args:
        return None

    event = args[0]

    if isinstance(event, dict):
        return await log_error(
            event.get("title", "Tashqi xavfsizlik hodisasi"),
            event.get("description", str(event)),
            event.get("type", "SECURITY_ERROR"),
            forward_to_security=False,
        )

    title = str(event)

    description = (
        str(args[1])
        if len(args) > 1
        else str(kwargs.get("description", ""))
    )

    return await log_error(
        title,
        description,
        forward_to_security=False,
    )

# =====================================================
# 6. BILDIRISHNOMA TUGMALARI
# =====================================================

@router.callback_query(F.data.startswith("ack:"))
async def acknowledge_notification(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    notification_id = callback.data.split(":", 1)[1]

    item = notification_history.get(
        notification_id
    )

    if not item:
        await callback.answer("Xabar topilmadi.")
        return

    item["acknowledged"] = True

    await save_notifications()

    await callback.answer(
        "Xabar tushunildi."
    )

@router.callback_query(F.data.startswith("mute:"))
async def mute_notification(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    notification_id = callback.data.split(":", 1)[1]

    item = notification_history.get(
        notification_id
    )

    if not item:
        await callback.answer("Xabar topilmadi.")
        return

    item["muted"] = True

    fingerprint = item.get("fingerprint")

    if fingerprint:
        muted = settings.setdefault(
            "muted_fingerprints",
            [],
        )

        if fingerprint not in muted:
            muted.append(fingerprint)

    await save_settings()
    await save_notifications()

    await callback.answer(
        "Ushbu turdagi takroriy xabarlar to'xtatildi."
    )

@router.callback_query(F.data.startswith("view:"))
async def view_notification(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    notification_id = callback.data.split(":", 1)[1]

    item = notification_history.get(
        notification_id
    )

    if not item:
        await callback.answer("Xabar topilmadi.")
        return

    await callback.message.answer(
        "XABAR TAFSILOTLARI\n\n"
        f"Turi: {item['type']}\n"
        f"Vaqti: {item['time']}\n\n"
        f"{item['text']}"
    )

    await callback.answer()

# =====================================================
# 7. BILDIRISHNOMA SOZLAMALARI
# =====================================================

def notification_settings_keyboard():
    important = (
        "YOQILGAN"
        if settings["important_notifications"]
        else "O'CHIRILGAN"
    )

    errors = (
        "YOQILGAN"
        if settings["error_notifications"]
        else "O'CHIRILGAN"
    )

    repeats = (
        "YOQILGAN"
        if settings["repeat_notifications"]
        else "O'CHIRILGAN"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Muhim eslatmalar: {important}",
                    callback_data="setting:important",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Xatolik xabarlari: {errors}",
                    callback_data="setting:errors",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"Takroriy xabarlar: {repeats}",
                    callback_data="setting:repeats",
                )
            ],
        ]
    )

@router.callback_query(F.data.startswith("setting:"))
async def change_notification_setting(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    setting_name = callback.data.split(":", 1)[1]

    mapping = {
        "important": "important_notifications",
        "errors": "error_notifications",
        "repeats": "repeat_notifications",
    }

    key = mapping.get(setting_name)

    if not key:
        await callback.answer(
            "Noto'g'ri sozlama."
        )
        return

    settings[key] = not settings[key]

    await save_settings()

    await callback.message.edit_reply_markup(
        reply_markup=notification_settings_keyboard()
    )

    await callback.answer(
        "Sozlama saqlandi."
    )

@router.message(Command("bildirishnomalar"))
async def notification_settings(
    message: Message,
):
    if not message.from_user:
        return

    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "BILDIRISHNOMALARNI BOSHQARISH",
        reply_markup=notification_settings_keyboard(),
    )

# =====================================================
# 8. XATOLIKLAR TARIXI
# =====================================================

@router.message(Command("xatoliklar"))
async def show_errors(message: Message):
    if not message.from_user:
        return

    if message.from_user.id != ADMIN_ID:
        return

    if not error_history:
        await message.answer(
            "Xatoliklar jurnali bo'sh."
        )
        return

    lines = ["OXIRGI HODISALAR:"]

    for event in error_history[-10:]:
        lines.append(
            f"\nID: {event.get('id')}\n"
            f"Vaqt: {event.get('time')}\n"
            f"Turi: {event.get('type')}\n"
            f"Sarlavha: {event.get('title')}\n"
            f"Tafsilot: {event.get('description')}"
        )

    text = "\n".join(lines)

    for start in range(0, len(text), 4000):
        await message.answer(
            text[start:start + 4000]
        )

# =====================================================
# 9. XAVFSIZLIK MARKAZI
# =====================================================

async def prepare_security_center():
    global security_module
    global security_ready

    try:
        security_module = importlib.import_module(
            SECURITY_MODULE_NAME
        )

        setup_result = security_module.setup(
            bot=bot,
            dp=dp,
            notify_admin=notify_admin,
            report_security_event=report_security_event,
        )

        if asyncio.iscoroutine(setup_result):
            await setup_result

        initialize_result = security_module.initialize()

        if asyncio.iscoroutine(initialize_result):
            initialize_result = await initialize_result

        if initialize_result is not True:
            raise RuntimeError(
                "Xavfsizlik markazi ishga tushmadi."
            )

        health = security_module.health_check()

        if asyncio.iscoroutine(health):
            health = await health

        ready = (
            health is True
            or (
                isinstance(health, dict)
                and (
                    health.get("ready") is True
                    or health.get("status") == "ACTIVE"
                )
            )
        )

        if not ready:
            raise RuntimeError(
                "Xavfsizlik markazi tayyorligini tasdiqlamadi."
            )

        security_ready = True

        logger.info(
            "Xavfsizlik markazi tayyor."
        )

        await record_event(
            "Xavfsizlik markazi ishga tushdi",
            "Xavfsizlik markazi tayyorlikni tasdiqladi.",
            event_type="SECURITY_STARTED",
        )

        return True

    except Exception as error:
        security_ready = False

        logger.exception(
            "Xavfsizlik markazini ishga tushirish xatosi."
        )

        await log_error(
            "Xavfsizlik markazini ishga tushirish xatosi",
            str(error),
        )

        return False

# =====================================================
# 11. MODUL RUXSATINI TEKSHIRISH
# =====================================================

async def verify_module_approval(
    name,
    capabilities,
):
    if not security_ready or not security_module:
        return False

    verifier = getattr(
        security_module,
        "verify_module_approval",
        None,
    )

    if not callable(verifier):
        logger.error(
            "Xavfsizlik markazida verify_module_approval "
            "interfeysi topilmadi."
        )
        return False

    try:
        result = verifier(
            name,
            capabilities,
        )

        if asyncio.iscoroutine(result):
            result = await result

        return result is True

    except Exception as error:
        await log_error(
            "Modul tasdig'ini tekshirish xatosi",
            str(error),
        )

        return False

# =====================================================
# 12. SUPRA ISHGA TUSHIRISH
# =====================================================

async def prepare_supra():
    global supra_ready

    if not security_ready:
        return False

    try:
        supra_gateway.setup(
            security_center=security_module,
            notify_admin=notify_admin,
            report_security_event=report_security_event,
            verify_module_approval=verify_module_approval,
        )

        result = await supra_gateway.initialize()

        if result is not True:
            raise RuntimeError(
                "SUPRA ishga tushmadi."
            )

        health = await supra_gateway.health_check()

        if (
            not isinstance(health, dict)
            or health.get("ready") is not True
            or health.get("running") is not True
        ):
            raise RuntimeError(
                "SUPRA tayyorligini tasdiqlamadi."
            )

        supra_ready = True

        logger.info(
            "SUPRA 2JZ-GTE tayyor."
        )

        await record_event(
            "SUPRA ishga tushdi",
            "SUPRA server yadrosi bilan aloqa o'rnatdi.",
            event_type="SUPRA_STARTED",
        )

        return True

    except Exception as error:
        supra_ready = False

        logger.exception(
            "SUPRA ishga tushirish xatosi."
        )

        await log_error(
            "SUPRA ishga tushirish xatosi",
            str(error),
        )

        return False

# =====================================================
# 14. RO'YXATDAN O'TISH TUGMALARI
# =====================================================

def registration_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="RO'YXATDAN O'TISH"
                )
            ]
        ],
        resize_keyboard=True,
    )

def contact_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="TELEFON RAQAMIMNI YUBORISH",
                    request_contact=True,
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

# =====================================================
# 15. RO'YXATDAN O'TISH
# =====================================================

async def begin_registration(message: Message):
    if not message.from_user:
        return

    # ADMIN UCHUN MAXSUS KIRISH:
    # Admin oddiy foydalanuvchi kabi ro'yxatdan o'tmaydi.
    if message.from_user.id == ADMIN_ID:
        await admin_panel(message)
        return

    if not security_ready or not supra_ready:
        await message.answer(
            "Ro'yxatdan o'tish tizimi hozircha tayyor emas. "
            "Keyinroq urinib ko'ring."
        )
        return

    telegram_id = message.from_user.id

    name_parts = [
        message.from_user.first_name or "",
        message.from_user.last_name or "",
    ]

    name = " ".join(
        part.strip()
        for part in name_parts
        if part.strip()
    ).strip()

    if not name:
        await message.answer(
            "Telegram profilingizda ism mavjud emas."
        )
        return

    key = str(telegram_id)

    existing = registration_states.get(key)

    if existing and existing.get("status") == "completed":
        await message.answer(
            "Siz oldin ro'yxatdan o'tgansiz."
        )
        return

    registration_states[key] = {
        "telegram_id": telegram_id,
        "name": name,
        "phone": None,
        "status": "waiting_contact",
        "created_at": current_time(),
        "request_id": str(uuid.uuid4()),
    }

    await save_registrations()

    await message.answer(
        f"Assalomu alaykum, {name}!\n\n"
        "Telegram ID avtomatik olindi.\n"
        "Endi telefon raqamingizni yuboring.",
        reply_markup=contact_keyboard(),
    )

@router.message(CommandStart())
async def start_handler(message: Message):
    # ADMIN /start BOSGANDA TO'G'RIDAN-TO'G'RI PANEL OCHILADI.
    if message.from_user and message.from_user.id == ADMIN_ID:
        if admin_ready and admin_module is not None:
            await admin_module.open_admin_panel(
                message.from_user.id,
                message,
            )
        else:
            await message.answer(
                "Admin paneli hali ishga tushmoqda. Bir ozdan keyin qayta urinib ko'ring."
            )
        return

    await message.answer(
        "SUPRA 2JZ-GTE tizimiga xush kelibsiz.\n\n"
        "Ro'yxatdan o'tish uchun tugmani bosing.",
        reply_markup=registration_keyboard(),
    )

@router.message(F.text == "RO'YXATDAN O'TISH")
async def registration_button(message: Message):
    await begin_registration(message)

# =====================================================
# 16. TELEFON RAQAMINI QABUL QILISH
# =====================================================

@router.message(F.contact)
async def receive_contact(message: Message):
    if not message.from_user:
        return

    # ADMIN UCHUN TELEFON KONTAKTI TALAB QILINMAYDI.
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Siz Admin sifatida tizimga kirgansiz. "
            "Ro'yxatdan o'tishingiz shart emas."
        )
        return

    telegram_id = message.from_user.id
    key = str(telegram_id)

    state = registration_states.get(key)

    if (
        not state
        or state.get("status") not in (
            "waiting_contact",
            "waiting_retry",
        )
    ):
        await message.answer(
            "Avval ro'yxatdan o'tish tugmasini bosing."
        )
        return

    contact = message.contact

    if contact.user_id != telegram_id:
        await message.answer(
            "Faqat o'zingizning Telegram kontaktingizni yuboring."
        )
        return

    phone = contact.phone_number

    if not phone:
        await message.answer(
            "Telefon raqami olinmadi. Qayta urinib ko'ring."
        )
        return

    state["phone"] = phone
    state["status"] = "sending"

    await save_registrations()

    await message.answer(
        "Ma'lumotlar tekshirilmoqda va SUPRA orqali yuborilmoqda...",
        reply_markup=ReplyKeyboardRemove(),
    )

    success, response = await send_registration(
        telegram_id,
        state,
    )

    if success:
        state["status"] = "completed"
        state["completed_at"] = current_time()

        await save_registrations()

        await message.answer(
            "Ro'yxatdan o'tish muvaffaqiyatli yakunlandi!\n\n"
            f"Ism: {state['name']}\n"
            f"Telegram ID: {telegram_id}\n"
            f"Telefon: {phone}",
            reply_markup=registration_keyboard(),
        )

    else:
        state["status"] = "waiting_retry"
        state["last_error"] = response

        await save_registrations()

        await message.answer(
            "Ro'yxatdan o'tish hozircha yakunlanmadi.\n\n"
            f"Sabab: {response}\n\n"
            "Qayta urinish uchun /retry buyrug'ini yuboring.",
            reply_markup=registration_keyboard(),
        )

# =====================================================
# 17. SUPRA ORQALI RO'YXATDAN O'TKAZISH
# =====================================================

async def send_registration(
    telegram_id,
    state,
):
    if not security_ready or not supra_ready:
        return False, "Xavfsizlik markazi yoki SUPRA tayyor emas."

    payload = {
        "telegram_id": telegram_id,
        "name": state["name"],
        "phone": state["phone"],
        "registration_time": state["created_at"],
    }

    request_id = state["request_id"]

    try:
        result = await supra_gateway.request(
            source="main.py",
            target="foydalanuvchilar",
            command="register_user",
            payload=payload,
            request_id=request_id,
        )

        if not isinstance(result, dict):
            return False, "SUPRA noto'g'ri javob qaytardi."

        if result.get("success") is not True:
            return False, result.get(
                "error",
                "SUPRA so'rovni bajarmadi.",
            )

        response = result.get("result")

        if not isinstance(response, dict):
            return False, "Foydalanuvchilar moduli javobi noto'g'ri."

        if response.get("registered") is not True:
            return False, response.get(
                "message",
                "Ro'yxatdan o'tish tasdiqlanmadi.",
            )

        return True, response.get(
            "message",
            "Tasdiqlandi.",
        )

    except Exception as error:
        await log_error(
            "Ro'yxatdan o'tkazish xatosi",
            str(error),
        )

        return False, "Tizim bilan aloqa vaqtincha uzildi."

@router.message(Command("retry"))
async def retry_registration(message: Message):
    if not message.from_user:
        return

    # ADMIN QAYTA RO'YXATDAN O'TISHGA MUHTOJ EMAS.
    if message.from_user.id == ADMIN_ID:
        await admin_panel(message)
        return

    key = str(message.from_user.id)

    state = registration_states.get(key)

    if not state or state.get("status") != "waiting_retry":
        await message.answer(
            "Qayta urinish uchun saqlangan jarayon topilmadi."
        )
        return

    if not supra_ready:
        await message.answer(
            "SUPRA hozir tayyor emas."
        )
        return

    state["status"] = "sending"

    await save_registrations()

    success, response = await send_registration(
        message.from_user.id,
        state,
    )

    if success:
        state["status"] = "completed"
        state["completed_at"] = current_time()

        await save_registrations()

        await message.answer(
            "Ro'yxatdan o'tish muvaffaqiyatli yakunlandi!",
            reply_markup=registration_keyboard(),
        )

    else:
        state["status"] = "waiting_retry"
        state["last_error"] = response

        await save_registrations()

        await message.answer(
            f"Qayta urinish amalga oshmadi.\nSabab: {response}"
        )

@router.message(Command("cancel"))
async def cancel_registration(message: Message):
    if not message.from_user:
        return

    # ADMIN UCHUN RO'YXATDAN O'TISHNI BEKOR QILISH KERAK EMAS.
    if message.from_user.id == ADMIN_ID:
        await admin_panel(message)
        return

    key = str(message.from_user.id)

    state = registration_states.get(key)

    if not state:
        await message.answer(
            "Faol jarayon topilmadi."
        )
        return

    if state.get("status") == "sending":
        await message.answer(
            "Ma'lumotlar yuborilmoqda. Bekor qilib bo'lmaydi."
        )
        return

    registration_states.pop(key, None)

    await save_registrations()

    await message.answer(
        "Ro'yxatdan o'tish bekor qilindi.",
        reply_markup=registration_keyboard(),
    )

# =====================================================
# 18. ADMIN BUYRUQLARI
# =====================================================

@router.message(Command("holat"))
async def system_status(message: Message):
    if not message.from_user:
        return

    if message.from_user.id != ADMIN_ID:
        return

    await message.answer(
        "SUPRA TIZIM HOLATI\n\n"
        f"MAIN: {'ISHLAYAPTI' if server_running else 'TOXTAGAN'}\n"
        f"Xavfsizlik markazi: {'TAYYOR' if security_ready else 'TAYYOR EMAS'}\n"
        f"SUPRA 2JZ-GTE: {'TAYYOR' if supra_ready else 'TAYYOR EMAS'}\n"
        f"Admin moduli: {'TAYYOR' if admin_ready else 'TAYYOR EMAS'}\n"
        f"Foydalanuvchilar moduli: {'TAYYOR' if users_module else 'TAYYOR EMAS'}\n"
        f"Sozlamalar moduli: {'TAYYOR' if settings_module else 'TAYYOR EMAS'}\n"
        f"Versiya: {VERSION}\n"
        f"Hodisalar: {len(error_history)}"
    )

@router.message(Command("mainadmin"))
async def admin_panel(message: Message):
    if not message.from_user:
        return

    if message.from_user.id != ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="BILDIRISHNOMALAR",
                    callback_data="admin_notifications",
                )
            ],
            [
                InlineKeyboardButton(
                    text="XATOLIKLAR",
                    callback_data="admin_errors",
                )
            ],
            [
                InlineKeyboardButton(
                    text="TIZIM HOLATI",
                    callback_data="admin_status",
                )
            ],
        ]
    )

    await message.answer(
        "SUPRA ADMIN BOSHQARUVI",
        reply_markup=keyboard,
    )

@router.callback_query(F.data.startswith("admin_"))
async def admin_callbacks(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer(
            "Ruxsat yo'q.",
            show_alert=True,
        )
        return

    action = callback.data

    if action == "admin_notifications":
        await callback.message.answer(
            "Bildirishnomalar sozlamasi:",
            reply_markup=notification_settings_keyboard(),
        )

    elif action == "admin_errors":
        await callback.message.answer(
            f"Jurnalda {len(error_history)} ta hodisa mavjud.\n"
            "Batafsil ko'rish: /xatoliklar"
        )

    elif action == "admin_status":
        await callback.message.answer(
            f"Xavfsizlik markazi: {security_ready}\n"
            f"SUPRA: {supra_ready}\n"
            f"MAIN: {server_running}"
        )

    await callback.answer()

# =====================================================
# 13. ADMIN MODULINI ISHGA TUSHIRISH
# =====================================================

async def prepare_admin():
    global admin_module, admin_ready

    try:
        admin_module = importlib.import_module("admin")
        result = await admin_module.initialize()
        if result is not True:
            raise RuntimeError("Admin moduli initialize() muvaffaqiyatsiz.")

        health = await admin_module.health_check()
        if not isinstance(health, dict) or health.get("status") != "HEALTHY":
            raise RuntimeError(f"Admin health-check tayyor emas: {health}")

        admin_ready = True
        logger.info("Admin moduli va tugmalar yordamchisi tayyor.")
        return True
    except Exception as error:
        admin_ready = False
        logger.exception("Admin modulini ishga tushirish xatosi.")
        await log_error("Admin modulini ishga tushirish xatosi", str(error))
        return False

# =====================================================
# 14. ASOSIY MODULLARNI ISHGA TUSHIRISH VA GATEWAYGA ULASH
# =====================================================

async def prepare_business_modules():
    global users_module, settings_module, business_modules_ready

    if not admin_ready or not supra_ready or not security_ready:
        return False

    try:
        users_module = importlib.import_module("foydalanuvchilar")
        await users_module.setup(
            bot=bot,
            dp=dp,
            notify_admin=notify_admin,
            report_security_event=report_security_event,
        )
        if await users_module.initialize() is not True:
            raise RuntimeError("foydalanuvchilar moduli ishga tushmadi.")

        settings_module = importlib.import_module("sozlamalar")
        await settings_module.setup(
            supra_gateway=supra_gateway,
            is_admin_verified=admin_module.admin_verified,
            notify_admin=notify_admin,
            report_security_event=report_security_event,
            data_file=str(DATA_DIR / "sozlamalar_data.json"),
        )
        settings_health = await settings_module.health_check()
        if not isinstance(settings_health, dict) or settings_health.get("status") != "healthy":
            raise RuntimeError(f"Sozlamalar moduli tayyor emas: {settings_health}")

        users_health = await users_module.health_check()
        if not isinstance(users_health, dict) or users_health.get("ready") is not True:
            raise RuntimeError(f"Foydalanuvchilar moduli tayyor emas: {users_health}")

        business_modules_ready = True
        logger.info("Foydalanuvchilar va sozlamalar modullari tayyor.")
        return True
    except Exception as error:
        business_modules_ready = False
        logger.exception("Asosiy modullarni tayyorlashda xatolik.")
        await log_error("Asosiy modullarni tayyorlashda xatolik", str(error))
        return False

async def connect_builtin_modules():
    """Ichki modullarni Gateway va Xavfsizlik markaziga ulaydi.

    Modulning Gateway'da ro'yxatdan o'tishi uning ruxsat olganini
    anglatmaydi. Faollashish faqat Admin tasdig'idan keyin bajariladi.
    """
    if not business_modules_ready or not supra_ready or not security_ready:
        return False

    module_objects = {
        "Xavfsizlik_markazi": security_module,
        "foydalanuvchilar": users_module,
        "sozlamalar": settings_module,
    }

    all_ok = True
    for name, capabilities in BUILTIN_MODULES.items():
        module_object = module_objects.get(name)
        if module_object is None:
            all_ok = False
            continue

        try:
            if name not in gateway_connected_modules:
                gateway_result = await supra_gateway.register_module(
                    name=name,
                    module=module_object,
                    capabilities=list(capabilities),
                    requested_permissions=list(capabilities),
                )
                if isinstance(gateway_result, dict) and gateway_result.get("success") is True:
                    gateway_connected_modules.add(name)
                elif isinstance(gateway_result, dict) and "oldin ro'yxatga olingan" in str(gateway_result.get("error", "")).lower():
                    # Joriy jarayonda allaqachon ulangan bo'lsa, takroriy ulash shart emas.
                    gateway_connected_modules.add(name)
                else:
                    raise RuntimeError(f"Gateway ulash xatosi ({name}): {gateway_result}")

            security_result = security_module.register_module(
                module_name=name,
                functions=list(capabilities),
                requested_permissions=list(capabilities),
            )
            if not isinstance(security_result, dict) or security_result.get("success") is not True:
                raise RuntimeError(f"Xavfsizlik ro'yxati xatosi ({name}): {security_result}")

            approval_status = security_result.get("status")
            gateway_status = await supra_gateway.get_module_status(name)
            if approval_status == "APPROVED" and isinstance(gateway_status, dict) and gateway_status.get("status") != "approved":
                activation = await supra_gateway.approve_module(
                    name=name,
                    approved_capabilities=list(capabilities),
                )
                if not isinstance(activation, dict) or activation.get("success") is not True:
                    raise RuntimeError(f"Tasdiqlangan modulni Gateway faollashtira olmadi ({name}): {activation}")

            logger.info("Ichki modul ulandi: %s | xavfsizlik holati: %s", name, approval_status)
        except Exception as error:
            all_ok = False
            logger.exception("Ichki modul ulanishida xatolik: %s", name)
            await log_error("Ichki modul ulanishida xatolik", f"{name}: {error}")

    return all_ok

# =====================================================
# 19. SOG'LIQ MONITORINGI
# =====================================================

async def health_monitor():
    while server_running:
        await asyncio.sleep(HEALTH_INTERVAL)

        # XAVFSIZLIK MARKAZI

        if security_ready and security_module:
            try:
                result = security_module.health_check()

                if asyncio.iscoroutine(result):
                    result = await result

                healthy = (
                    result is True
                    or (
                        isinstance(result, dict)
                        and (
                            result.get("ready") is True
                            or result.get("status") == "ACTIVE"
                        )
                    )
                )

                if not healthy:
                    if "security" not in health_alerted:
                        health_alerted.add("security")

                        await log_error(
                            "Xavfsizlik markazi holati o'zgardi",
                            "Xavfsizlik markazi tayyorligini tasdiqlamadi.",
                        )
                else:
                    health_alerted.discard("security")

            except Exception as error:
                if "security" not in health_alerted:
                    health_alerted.add("security")

                    await log_error(
                        "Xavfsizlik markazi monitoring xatosi",
                        str(error),
                    )

        # SUPRA

        if supra_ready:
            try:
                result = await supra_gateway.health_check()

                healthy = (
                    isinstance(result, dict)
                    and result.get("ready") is True
                    and result.get("running") is True
                )

                if not healthy:
                    if "supra" not in health_alerted:
                        health_alerted.add("supra")

                        await log_error(
                            "SUPRA holati o'zgardi",
                            "SUPRA tayyorlik holatini yo'qotdi.",
                        )
                else:
                    health_alerted.discard("supra")

            except Exception as error:
                if "supra" not in health_alerted:
                    health_alerted.add("supra")

                    await log_error(
                        "SUPRA monitoring xatosi",
                        str(error),
                    )

# =====================================================
# 20. ISHGA TUSHIRISH NAZORATI
# =====================================================

async def startup_supervisor():
    while server_running:
        if not security_ready:
            await prepare_security_center()
            if not security_ready:
                await asyncio.sleep(RETRY_INTERVAL)
                continue

        if not supra_ready:
            await prepare_supra()
            if not supra_ready:
                await asyncio.sleep(RETRY_INTERVAL)
                continue

        if not admin_ready:
            await prepare_admin()
            if not admin_ready:
                await asyncio.sleep(RETRY_INTERVAL)
                continue

        if not business_modules_ready:
            await prepare_business_modules()
            if not business_modules_ready:
                await asyncio.sleep(RETRY_INTERVAL)
                continue

        # Pending ruxsat so'rovlari qayta yuborilishi mumkin;
        # Gateway tasdiqlangan modullarni qayta faollashtiradi.
        await connect_builtin_modules()
        await asyncio.sleep(RETRY_INTERVAL)

# =====================================================
# 21. ASOSIY SERVER YADROSI
# =====================================================

async def main():
    global bot
    global server_running

    if (
        not BOT_TOKEN
        or BOT_TOKEN == "BU_YERGA_TOKENINGNI_YOZ"
    ):
        raise RuntimeError(
            "BOT_TOKEN topilmadi. Server muhitiga haqiqiy token kiriting."
        )

    load_local_data()

    bot = Bot(token=BOT_TOKEN)

    dp.include_router(router)

    server_running = True

    supervisor_task = asyncio.create_task(
        startup_supervisor()
    )

    monitor_task = asyncio.create_task(
        health_monitor()
    )

    logger.info(
        "MAIN %s ishga tushdi.",
        VERSION,
    )

    print(
        "SUPRA SERVER YADROSI ISHGA TUSHDI."
    )

    print(
        "Telegram Bot API bilan polling aloqasi boshlanmoqda..."
    )

    try:
        # ==========================================
        # ASOSIY SERVER YADROSI
        # TELEGRAMDAN YANGI XABARLARNI QABUL QILISH
        # ==========================================

        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )

    except asyncio.CancelledError:
        logger.warning(
            "Server jarayoni bekor qilindi."
        )
        raise

    except Exception as error:
        logger.exception(
            "Server yadrosida jiddiy xatolik."
        )

        await log_error(
            "SERVER YADROSI XATOLIGI",
            str(error),
        )

        raise

    finally:
        server_running = False

        supervisor_task.cancel()
        monitor_task.cancel()

        await asyncio.gather(
            supervisor_task,
            monitor_task,
            return_exceptions=True,
        )

        try:
            if users_module is not None:
                await users_module.shutdown()
        except Exception:
            logger.exception("Foydalanuvchilar modulini to'xtatishda xatolik.")

        try:
            if settings_module is not None:
                manager = getattr(settings_module, "settings_manager", None)
                if manager is not None and hasattr(manager, "shutdown"):
                    await manager.shutdown()
        except Exception:
            logger.exception("Sozlamalar modulini to'xtatishda xatolik.")

        try:
            await supra_gateway.shutdown()

        except Exception:
            logger.exception(
                "SUPRA to'xtatishda xatolik."
            )

        if bot:
            await bot.session.close()

        logger.info(
            "MAIN serveri to'xtatildi."
        )

        print(
            "SUPRA SERVER YADROSI TO'XTADI."
        )

# =====================================================
# 22. SERVERNI ISHGA TUSHIRISH
# =====================================================

if __name__ == "__main__":
    asyncio.run(main())
