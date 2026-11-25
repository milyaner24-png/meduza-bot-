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

REGISTRATION_FILE = Path("main_registrations.json")
SETTINGS_FILE = Path("main_settings.json")
ERROR_FILE = Path("main_errors.json")
NOTIFICATION_FILE = Path("main_notifications.json")

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

security_ready = False
supra_ready = False
server_running = False

temporary_module_registered = False

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

        await bot.send_message(
            ADMIN_ID,
            text,
            reply_markup=notification_keyboard(
                notification_id
            ),
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
# 9. VAQTINCHALIK FOYDALANUVCHILAR KO'PRIGI
# =====================================================

class TemporaryUsersBridge:

    MODULE_NAME = "foydalanuvchilar"

    def __init__(self):
        self.file = Path("temporary_users.json")
        self.users = {}
        self.initialized = False
        self.lock = asyncio.Lock()

    async def initialize(self):
        data = load_json(
            self.file,
            {},
        )

        if not isinstance(data, dict):
            raise ValueError(
                "Vaqtinchalik foydalanuvchilar fayli noto'g'ri."
            )

        self.users = data
        self.initialized = True

        return True

    async def handle_command(
        self,
        command,
        payload,
        context,
    ):
        if command != "register_user":
            return {
                "registered": False,
                "message": "Buyruq qo'llab-quvvatlanmaydi.",
            }

        if not self.initialized:
            raise RuntimeError(
                "Vaqtinchalik modul tayyor emas."
            )

        if not isinstance(payload, dict):
            return {
                "registered": False,
                "message": "Ma'lumot formati noto'g'ri.",
            }

        telegram_id = payload.get("telegram_id")
        name = payload.get("name")
        phone = payload.get("phone")

        if not telegram_id or not name or not phone:
            return {
                "registered": False,
                "message": "Ma'lumotlar to'liq emas.",
            }

        telegram_id = str(telegram_id)

        async with self.lock:
            existing = self.users.get(telegram_id)

            if existing:
                return {
                    "registered": True,
                    "message": "Foydalanuvchi oldin ro'yxatdan o'tgan.",
                    "existing": True,
                    "storage_type": "temporary",
                }

            self.users[telegram_id] = {
                "telegram_id": telegram_id,
                "name": str(name),
                "phone": str(phone),
                "registration_time": payload.get(
                    "registration_time",
                    current_time(),
                ),
                "request_id": (
                    context.get("request_id")
                    if isinstance(context, dict)
                    else None
                ),
                "storage_type": "temporary",
            }

            save_json(
                self.file,
                self.users,
            )

        return {
            "registered": True,
            "message": "Ma'lumotlar vaqtinchalik omborga saqlandi.",
            "storage_type": "temporary",
        }

temporary_users = TemporaryUsersBridge()

# =====================================================
# 10. XAVFSIZLIK MARKAZI
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
# 13. FOYDALANUVCHILAR MODULINI SUPRA GA ULASH
# =====================================================

async def connect_temporary_users():
    global temporary_module_registered

    if not supra_ready:
        return False

    try:
        await temporary_users.initialize()

        if not temporary_module_registered:
            result = await supra_gateway.register_module(
                name="foydalanuvchilar",
                module=temporary_users,
                capabilities=["register_user"],
                requested_permissions=["register_user"],
            )

            if not isinstance(result, dict):
                raise RuntimeError(
                    "SUPRA modul ro'yxatga olishda noto'g'ri javob qaytardi."
                )

            if result.get("success") is not True:
                raise RuntimeError(
                    result.get(
                        "error",
                        "Modul ro'yxatga olinmadi.",
                    )
                )

            temporary_module_registered = True

        # Modul register_module orqali ro'yxatga olinishi
        # uning tasdiqlanganini anglatmaydi.
        approval = await supra_gateway.approve_module(
            name="foydalanuvchilar",
            approved_capabilities=["register_user"],
        )

        if (
            not isinstance(approval, dict)
            or approval.get("success") is not True
        ):
            raise RuntimeError(
                "Vaqtinchalik modul xavfsizlik markazi "
                "tomonidan tasdiqlanmadi. Modul pending holatida."
            )

        await record_event(
            "Vaqtinchalik foydalanuvchilar ko'prigi",
            (
                "foydalanuvchilar.py mavjud bo'lmagani uchun "
                "vaqtinchalik JSON ombor ishlatilmoqda."
            ),
            event_type="TEMPORARY_STORAGE",
        )

        return True

    except Exception as error:
        await log_error(
            "Foydalanuvchilar modulini ulash xatosi",
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
        await admin_panel(message)
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
        f"Foydalanuvchilar ko'prigi: "
        f"{'ULANGAN' if temporary_module_registered else 'ULANMAGAN'}\n"
        f"Versiya: {VERSION}\n"
        f"Hodisalar: {len(error_history)}"
    )

@router.message(Command("admin"))
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
    global security_ready
    global supra_ready

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

        if supra_ready and not temporary_module_registered:
            connected = await connect_temporary_users()

            if not connected:
                await asyncio.sleep(RETRY_INTERVAL)
                continue

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

# Fayl: supra_2jz_gte.py
# SUPRA 2JZ-GTE VERSION 2.1.1

import asyncio
import copy
import inspect
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

class SupraGateway:
    """
    SUPRA 2JZ-GTE VERSION 2.1.1

    Markaziy aloqa va buyruqlarni yo'naltirish tizimi.

    Ichki fayllar:
        Oldindan tanilgan tizim qismlari.
        Yangi fayl qidirish mexanizmiga kirmaydi.
        Xavfsizlik ruxsatlari baribir tekshiriladi.

    Tashqi fayllar:
        Yangi modul sifatida ro'yxatga olinadi.
        Xavfsizlik markazi tekshiruvidan o'tadi.
    """

    VERSION = "2.1.1"

    # Tizimning oldindan tanilgan ichki fayllari.
    # Bu ro'yxat yangi fayllarni qidirish uchun emas.
    BUILTIN_MODULES = {
        "admin",
        "Xavfsizlik_markazi",
        "sozlamalar",
        "foydalanuvchilar",
        "tugmalar",
    }

    # Asosiy yadro alohida ishlaydi.
    CORE_MODULES = {
        "main.py",
    }

    def __init__(
        self,
        data_file: str = "supra_gateway_data.json",
        max_concurrent_jobs: int = 10,
        request_timeout: int = 60,
    ):
        self.data_file = Path(data_file)
        self.max_concurrent_jobs = max(
            1, int(max_concurrent_jobs)
        )
        self.request_timeout = max(
            1, int(request_timeout)
        )

        self.modules = {}
        self.jobs = {}
        self.processed_requests = {}

        self.initialized = False
        self.running = False

        self._data_lock = asyncio.Lock()
        self._registry_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(
            self.max_concurrent_jobs
        )

        self.notify_admin = None
        self.report_security_event = None
        self.security_center = None
        self.verify_module_approval = None

    # -------------------------------------------------
    # 1. BOSHLANG'ICH SOZLASH
    # -------------------------------------------------

    def setup(
        self,
        security_center=None,
        notify_admin: Optional[Callable] = None,
        report_security_event: Optional[Callable] = None,
        verify_module_approval: Optional[Callable] = None,
    ):
        self.security_center = security_center
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event
        self.verify_module_approval = verify_module_approval

    async def _call_callback(self, callback, *args):
        if not callable(callback):
            return None

        result = callback(*args)

        if inspect.isawaitable(result):
            return await result

        return result

    def _make_event(
        self,
        event_type,
        description,
        title=None,
    ):
        event = {
            "source": "supra_2jz_gte.py",
            "type": str(event_type),
            "event_type": str(event_type),
            "description": str(description),
            "time": time.time(),
        }

        if title is not None:
            event["title"] = str(title)

        return event

    def _module_type(self, name):
        if name in self.BUILTIN_MODULES:
            return "builtin"

        return "external"

    async def initialize(self):
        load_error = None
        save_error = None

        async with self._data_lock:
            self.data_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if self.data_file.exists():
                try:
                    data = json.loads(
                        self.data_file.read_text(
                            encoding="utf-8"
                        )
                    )

                    if not isinstance(data, dict):
                        raise ValueError(
                            "Ma'lumotlar formati noto'g'ri."
                        )

                    jobs = data.get("jobs", {})
                    requests = data.get(
                        "processed_requests", {}
                    )
                    saved_modules = data.get("modules", {})

                    if not isinstance(jobs, dict):
                        raise ValueError(
                            "Jobs ma'lumotlari noto'g'ri."
                        )

                    if not isinstance(requests, dict):
                        raise ValueError(
                            "Request ma'lumotlari noto'g'ri."
                        )

                    if not isinstance(saved_modules, dict):
                        raise ValueError(
                            "Modules ma'lumotlari noto'g'ri."
                        )

                    self.jobs = jobs
                    self.processed_requests = requests
                    self.modules = {}

                    for name, info in saved_modules.items():
                        if not isinstance(info, dict):
                            continue

                        capabilities = info.get(
                            "capabilities", []
                        )
                        permissions = info.get(
                            "requested_permissions", []
                        )

                        if not isinstance(capabilities, list):
                            capabilities = []

                        if not isinstance(permissions, list):
                            permissions = []

                        module_type = info.get(
                            "module_type",
                            self._module_type(name),
                        )

                        self.modules[name] = {
                            "name": name,
                            "module": None,
                            "capabilities": capabilities,
                            "requested_permissions": permissions,
                            "status": "pending",
                            "module_type": module_type,
                            "registered_at": info.get(
                                "registered_at",
                                time.time(),
                            ),
                            "restored_from_storage": True,
                        }

                except Exception as error:
                    load_error = str(error)
                    self.initialized = False
                    self.running = False

            if load_error is None:
                for job in self.jobs.values():
                    if (
                        isinstance(job, dict)
                        and job.get("status") == "processing"
                    ):
                        job["status"] = "interrupted"
                        job["finished_at"] = time.time()

                for request in self.processed_requests.values():
                    if (
                        isinstance(request, dict)
                        and request.get("status") == "processing"
                    ):
                        request["status"] = "interrupted"
                        request["finished_at"] = time.time()

                try:
                    await self._save_locked()
                except Exception as error:
                    save_error = str(error)
                else:
                    self.initialized = True
                    self.running = True

        if load_error is not None:
            await self._report_error(
                "SUPRA ma'lumotlarini o'qishda xatolik",
                load_error,
            )
            return False

        if save_error is not None:
            self.initialized = False
            self.running = False

            await self._report_error(
                "SUPRA boshlang'ich saqlash xatosi",
                save_error,
            )
            return False

        return True

    async def health_check(self):
        async with self._registry_lock:
            registered_count = len(self.modules)
            builtin_count = sum(
                1
                for info in self.modules.values()
                if info.get("module_type") == "builtin"
            )
            external_count = sum(
                1
                for info in self.modules.values()
                if info.get("module_type") == "external"
            )

        async with self._data_lock:
            active_count = sum(
                1
                for job in self.jobs.values()
                if (
                    isinstance(job, dict)
                    and job.get("status") == "processing"
                )
            )
            saved_count = len(self.jobs)

        return {
            "module": "SUPRA 2JZ-GTE",
            "version": self.VERSION,
            "ready": self.initialized,
            "running": self.running,
            "registered_modules": registered_count,
            "builtin_modules": builtin_count,
            "external_modules": external_count,
            "active_jobs": active_count,
            "saved_jobs": saved_count,
        }

    # -------------------------------------------------
    # 2. MA'LUMOTLARNI SAQLASH
    # -------------------------------------------------

    def _build_storage_data(self):
        saved_modules = {}

        for name, info in self.modules.items():
            saved_modules[name] = {
                "name": name,
                "capabilities": list(
                    info.get("capabilities", [])
                ),
                "requested_permissions": list(
                    info.get("requested_permissions", [])
                ),
                "status": info.get("status", "pending"),
                "module_type": info.get(
                    "module_type",
                    self._module_type(name),
                ),
                "registered_at": info.get(
                    "registered_at",
                    time.time(),
                ),
            }

        return {
            "version": self.VERSION,
            "updated_at": time.time(),
            "modules": saved_modules,
            "jobs": self.jobs,
            "processed_requests": self.processed_requests,
        }

    async def _save_locked(self):
        data = self._build_storage_data()
        temporary_file = None

        try:
            self.data_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            content = json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            )

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.data_file.parent),
                prefix=self.data_file.name + ".",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_file = Path(file.name)
                file.write(content)
                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temporary_file,
                self.data_file,
            )

        finally:
            if (
                temporary_file is not None
                and temporary_file.exists()
            ):
                try:
                    temporary_file.unlink()
                except OSError:
                    pass

    async def save_state(self):
        async with self._registry_lock:
            async with self._data_lock:
                await self._save_locked()

        return True

    # -------------------------------------------------
    # 3. XATOLIKLAR VA XAVFSIZLIK XABARLARI
    # -------------------------------------------------

    async def _report_error(self, title, description):
        event = self._make_event(
            "SUPRA_ERROR",
            description,
            title=title,
        )

        if callable(self.report_security_event):
            try:
                await self._call_callback(
                    self.report_security_event,
                    event,
                )
                return
            except Exception:
                pass

        if callable(self.notify_admin):
            try:
                await self._call_callback(
                    self.notify_admin,
                    f"{title}\n{description}",
                )
            except Exception:
                pass

    async def report_event(self, event_type, description):
        event = self._make_event(
            event_type,
            description,
        )

        if callable(self.report_security_event):
            try:
                await self._call_callback(
                    self.report_security_event,
                    event,
                )
                return True
            except Exception as error:
                await self._report_error(
                    "Xavfsizlik markaziga xabar yuborilmadi",
                    str(error),
                )
                return False

        return False

    # -------------------------------------------------
    # 4. MODULLARNI RO'YXATGA OLISH
    # -------------------------------------------------

    async def register_module(
        self,
        name: str,
        module: Any,
        capabilities: list,
        requested_permissions: Optional[list] = None,
    ):
        if not self.initialized or not self.running:
            return {
                "success": False,
                "error": "SUPRA hali ishga tushmagan.",
            }

        if not isinstance(name, str) or not name.strip():
            return {
                "success": False,
                "error": "Fayl nomi noto'g'ri.",
            }

        name = name.strip()

        if module is None:
            return {
                "success": False,
                "error": "Modul obyekti topilmadi.",
            }

        if not isinstance(capabilities, list):
            return {
                "success": False,
                "error": "Imkoniyatlar ro'yxati noto'g'ri.",
            }

        if requested_permissions is None:
            requested_permissions = []

        if not isinstance(requested_permissions, list):
            return {
                "success": False,
                "error": "Ruxsatlar ro'yxati noto'g'ri.",
            }

        if not all(
            isinstance(item, str) and item.strip()
            for item in capabilities
        ):
            return {
                "success": False,
                "error": "Imkoniyatlar ichida noto'g'ri qiymat bor.",
            }

        if not all(
            isinstance(item, str) and item.strip()
            for item in requested_permissions
        ):
            return {
                "success": False,
                "error": "Ruxsatlar ichida noto'g'ri qiymat bor.",
            }

        capabilities = list(dict.fromkeys(capabilities))
        requested_permissions = list(
            dict.fromkeys(requested_permissions)
        )

        module_type = self._module_type(name)
        save_error = None
        result = None

        async with self._registry_lock:
            if name in self.modules:
                existing = self.modules[name]

                if (
                    existing.get("module") is None
                    and existing.get("status") == "pending"
                ):
                    old_info = {
                        key: copy.deepcopy(value)
                        for key, value in existing.items()
                        if key != "module"
                    }

                    existing["module"] = module
                    existing["capabilities"] = capabilities
                    existing["requested_permissions"] = (
                        requested_permissions
                    )
                    existing["module_type"] = module_type
                    existing["reconnected_at"] = time.time()

                    try:
                        async with self._data_lock:
                            await self._save_locked()
                    except Exception as error:
                        existing["module"] = None
                        existing.clear()
                        existing.update(old_info)
                        existing["module"] = None
                        save_error = str(error)
                    else:
                        result = {
                            "success": True,
                            "module": name,
                            "module_type": module_type,
                            "status": "pending",
                            "message": (
                                "Modul qayta ulandi. "
                                "Xavfsizlik ruxsati kutilmoqda."
                            ),
                        }
                else:
                    return {
                        "success": False,
                        "error": (
                            "Bu fayl oldin ro'yxatga olingan."
                        ),
                    }

            else:
                self.modules[name] = {
                    "name": name,
                    "module": module,
                    "capabilities": capabilities,
                    "requested_permissions": requested_permissions,
                    "status": "pending",
                    "module_type": module_type,
                    "registered_at": time.time(),
                }

                try:
                    async with self._data_lock:
                        await self._save_locked()
                except Exception as error:
                    self.modules.pop(name, None)
                    save_error = str(error)
                else:
                    result = {
                        "success": True,
                        "module": name,
                        "module_type": module_type,
                        "status": "pending",
                        "message": (
                            "Ichki modul qayd etildi. "
                            "Xavfsizlik ruxsati kutilmoqda."
                            if module_type == "builtin"
                            else
                            "Yangi modul qayd etildi. "
                            "Xavfsizlik tekshiruvi kutilmoqda."
                        ),
                    }

        if save_error is not None:
            await self._report_error(
                "Modulni ro'yxatga olishda xatolik",
                save_error,
            )
            return {
                "success": False,
                "error": "Modul ma'lumotlarini saqlab bo'lmadi.",
            }

        # Ichki fayllar yangi fayl sifatida e'lon qilinmaydi.
        if module_type == "external":
            await self.report_event(
                "NEW_MODULE_REGISTERED",
                (
                    f"Yangi modul: {name}. "
                    "Xavfsizlik tekshiruvi kutilmoqda."
                ),
            )

        return result

    # -------------------------------------------------
    # 5. MODULGA RUXSAT BERISH
    # -------------------------------------------------

    async def approve_module(
        self,
        name: str,
        approved_capabilities: list,
    ):
        if not isinstance(name, str) or not name.strip():
            return {
                "success": False,
                "error": "Modul nomi noto'g'ri.",
            }

        name = name.strip()

        if not callable(self.verify_module_approval):
            await self._report_error(
                "Modulni tasdiqlash rad etildi",
                (
                    f"Fayl: {name}. "
                    "Xavfsizlik tekshiruvi ulanmagan."
                ),
            )
            return {
                "success": False,
                "error": "Xavfsizlik tekshiruvi ulanmagan.",
            }

        if not isinstance(approved_capabilities, list):
            return {
                "success": False,
                "error": "Tasdiqlangan imkoniyatlar ro'yxati noto'g'ri.",
            }

        if not all(
            isinstance(item, str) and item.strip()
            for item in approved_capabilities
        ):
            return {
                "success": False,
                "error": "Imkoniyatlar ichida noto'g'ri qiymat bor.",
            }

        approved_capabilities = list(
            dict.fromkeys(approved_capabilities)
        )

        try:
            verified = await self._call_callback(
                self.verify_module_approval,
                name,
                approved_capabilities,
            )
        except Exception as error:
            await self._report_error(
                "Xavfsizlik tasdig'ini tekshirishda xatolik",
                str(error),
            )
            return {
                "success": False,
                "error": "Xavfsizlik tasdig'ini tekshirib bo'lmadi.",
            }

        if verified is not True:
            return {
                "success": False,
                "error": "Xavfsizlik markazi ruxsat bermadi.",
            }

        save_error = None

        async with self._registry_lock:
            module_info = self.modules.get(name)

            if not module_info:
                return {
                    "success": False,
                    "error": "Modul ro'yxatdan topilmadi.",
                }

            if module_info.get("module") is None:
                return {
                    "success": False,
                    "error": "Modul obyekti ulanmagan.",
                }

            if module_info.get("status") == "suspended":
                return {
                    "success": False,
                    "error": "Cheklangan modulni tasdiqlab bo'lmaydi.",
                }

            requested = set(
                module_info.get("capabilities", [])
            )
            approved = set(approved_capabilities)

            if not approved.issubset(requested):
                return {
                    "success": False,
                    "error": (
                        "Tasdiqlangan imkoniyatlar "
                        "so'ralgan imkoniyatlarga mos emas."
                    ),
                }

            old_status = module_info.get("status")
            old_capabilities = list(
                module_info.get("capabilities", [])
            )
            old_approved_at = module_info.get("approved_at")

            module_info["capabilities"] = list(
                approved_capabilities
            )
            module_info["status"] = "approved"
            module_info["approved_at"] = time.time()

            try:
                async with self._data_lock:
                    await self._save_locked()
            except Exception as error:
                module_info["status"] = old_status
                module_info["capabilities"] = old_capabilities

                if old_approved_at is None:
                    module_info.pop("approved_at", None)
                else:
                    module_info["approved_at"] = old_approved_at

                save_error = str(error)

        if save_error is not None:
            await self._report_error(
                "Modul ruxsatini saqlashda xatolik",
                save_error,
            )
            return {
                "success": False,
                "error": "Modul ruxsatini saqlab bo'lmadi.",
            }

        await self.report_event(
            "MODULE_APPROVED",
            f"Fayl: {name}. Modul tasdiqlandi.",
        )

        return {
            "success": True,
            "module": name,
            "module_type": module_type if False else self._module_type(name),
            "status": "approved",
            "approved_capabilities": approved_capabilities,
            "message": "Modul muvaffaqiyatli tasdiqlandi.",
        }

    # -------------------------------------------------
    # 6. MODULNI CHEKLASH VA TIKLASH
    # -------------------------------------------------

    async def suspend_module(self, name: str, reason: str):
        save_error = None

        async with self._registry_lock:
            module_info = self.modules.get(name)

            if not module_info:
                return False

            old_status = module_info.get("status")
            old_reason = module_info.get("suspension_reason")
            old_time = module_info.get("suspended_at")

            module_info["status"] = "suspended"
            module_info["suspension_reason"] = str(reason)
            module_info["suspended_at"] = time.time()

            try:
                async with self._data_lock:
                    await self._save_locked()
            except Exception as error:
                module_info["status"] = old_status

                if old_reason is None:
                    module_info.pop("suspension_reason", None)
                else:
                    module_info["suspension_reason"] = old_reason

                if old_time is None:
                    module_info.pop("suspended_at", None)
                else:
                    module_info["suspended_at"] = old_time

                save_error = str(error)

        if save_error is not None:
            await self._report_error(
                "Modulni cheklashda saqlash xatosi",
                save_error,
            )
            return False

        await self.report_event(
            "MODULE_SUSPENDED",
            f"Fayl: {name}\nSabab: {reason}",
        )
        return True

    async def restore_module(self, name: str):
        save_error = None

        async with self._registry_lock:
            module_info = self.modules.get(name)

            if not module_info:
                return False

            if module_info.get("status") != "suspended":
                return False

            module_info["status"] = "pending"
            module_info["restored_at"] = time.time()
            module_info.pop("approved_at", None)

            try:
                async with self._data_lock:
                    await self._save_locked()
            except Exception as error:
                module_info["status"] = "suspended"
                module_info.pop("restored_at", None)
                save_error = str(error)

        if save_error is not None:
            await self._report_error(
                "Modulni tiklashda saqlash xatosi",
                save_error,
            )
            return False

        await self.report_event(
            "MODULE_RESTORED",
            f"Fayl: {name}. Qayta ruxsat kutilmoqda.",
        )
        return True

    async def get_module_status(self, name: str):
        async with self._registry_lock:
            module_info = self.modules.get(name)

            if not module_info:
                return None

            return {
                key: copy.deepcopy(value)
                for key, value in module_info.items()
                if key != "module"
            }

    async def get_modules(self):
        async with self._registry_lock:
            return [
                {
                    key: copy.deepcopy(value)
                    for key, value in info.items()
                    if key != "module"
                }
                for info in self.modules.values()
            ]

    # -------------------------------------------------
    # 7. BUYRUQLARNI YO'NALTIRISH
    # -------------------------------------------------

    async def request(
        self,
        source: str,
        target: str,
        command: str,
        payload: Optional[dict] = None,
        request_id: Optional[str] = None,
    ):
        if not self.initialized or not self.running:
            return {
                "success": False,
                "error": "SUPRA hozir ishlamayapti.",
            }

        if not all(
            isinstance(value, str) and value.strip()
            for value in (source, target, command)
        ):
            return {
                "success": False,
                "error": "Buyruq ma'lumotlari noto'g'ri.",
            }

        source = source.strip()
        target = target.strip()
        command = command.strip()

        if payload is None:
            payload = {}

        if not isinstance(payload, dict):
            return {
                "success": False,
                "error": "Buyruq ma'lumotlari lug'at bo'lishi kerak.",
            }

        if request_id is None:
            request_id = str(uuid.uuid4())

        if (
            not isinstance(request_id, str)
            or not request_id.strip()
            or len(request_id) > 128
        ):
            return {
                "success": False,
                "error": "Request ID noto'g'ri.",
            }

        request_id = request_id.strip()

        async with self._data_lock:
            previous = self.processed_requests.get(request_id)

            if previous:
                previous_response = previous.get("response")

                if previous_response is not None:
                    return copy.deepcopy(previous_response)

                return {
                    "success": False,
                    "duplicate": True,
                    "request_id": request_id,
                    "message": "Bu so'rov avval qayd etilgan.",
                    "previous_status": previous.get("status"),
                }

            now = time.time()

            self.processed_requests[request_id] = {
                "source": source,
                "target": target,
                "command": command,
                "status": "processing",
                "created_at": now,
            }

            self.jobs[request_id] = {
                "id": request_id,
                "source": source,
                "target": target,
                "command": command,
                "status": "processing",
                "created_at": now,
            }

            try:
                await self._save_locked()
            except Exception as error:
                self.processed_requests.pop(request_id, None)
                self.jobs.pop(request_id, None)
                start_save_error = str(error)
            else:
                start_save_error = None

        if start_save_error is not None:
            await self._report_error(
                "Buyruqni boshlashda saqlash xatosi",
                start_save_error,
            )
            return {
                "success": False,
                "request_id": request_id,
                "error": "Buyruq ma'lumotlarini saqlab bo'lmadi.",
            }

        response = None
        status = "failed"

        try:
            async with self._semaphore:
                if not self.running:
                    raise RuntimeError("SUPRA to'xtatilmoqda.")

                async with self._registry_lock:
                    module_info = self.modules.get(target)

                    if not module_info:
                        raise RuntimeError(
                            "Qabul qiluvchi modul topilmadi."
                        )

                    if module_info.get("status") != "approved":
                        raise PermissionError(
                            "Qabul qiluvchi modul tasdiqlanmagan."
                        )

                    if module_info.get("module") is None:
                        raise RuntimeError(
                            "Qabul qiluvchi modul ulanmagan."
                        )

                    if command not in module_info.get(
                        "capabilities", []
                    ):
                        raise PermissionError(
                            "Modul bu buyruqni bajarish huquqiga ega emas."
                        )

                    source_info = self.modules.get(source)

                    if source not in self.CORE_MODULES and source != "admin":
                        if (
                            not source_info
                            or source_info.get("status") != "approved"
                            or source_info.get("module") is None
                        ):
                            raise PermissionError(
                                "So'rov yuboruvchi modul tasdiqlanmagan."
                            )

                    module = module_info["module"]
                    handler = getattr(
                        module,
                        "handle_command",
                        None,
                    )

                if not callable(handler):
                    raise RuntimeError(
                        "Qabul qiluvchi modulda handle_command mavjud emas."
                    )

                context = {
                    "request_id": request_id,
                    "source": source,
                    "target": target,
                    "command": command,
                    "created_at": time.time(),
                }

                if inspect.iscoroutinefunction(handler):
                    result = await asyncio.wait_for(
                        handler(
                            command,
                            copy.deepcopy(payload),
                            context,
                        ),
                        timeout=self.request_timeout,
                    )
                else:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            handler,
                            command,
                            copy.deepcopy(payload),
                            context,
                        ),
                        timeout=self.request_timeout,
                    )

            response = {
                "success": True,
                "request_id": request_id,
                "result": result,
            }
            status = "completed"

        except asyncio.TimeoutError:
            response = {
                "success": False,
                "request_id": request_id,
                "error": "Buyruq bajarish vaqti tugadi.",
            }

        except Exception as error:
            response = {
                "success": False,
                "request_id": request_id,
                "error": str(error),
            }

            await self._report_error(
                "SUPRA buyruqni bajarishda xatolik",
                (
                    f"ID: {request_id}\n"
                    f"Manba: {source}\n"
                    f"Qabul qiluvchi: {target}\n"
                    f"Buyruq: {command}\n"
                    f"Xato: {error}"
                ),
            )

        async with self._data_lock:
            now = time.time()

            if request_id in self.jobs:
                self.jobs[request_id]["status"] = (
                    "completed" if response["success"] else "failed"
                )
                self.jobs[request_id]["finished_at"] = now
                self.jobs[request_id]["response"] = response

            if request_id in self.processed_requests:
                self.processed_requests[request_id]["status"] = (
                    "completed" if response["success"] else "failed"
                )
                self.processed_requests[request_id]["finished_at"] = now
                self.processed_requests[request_id]["response"] = response

            try:
                await self._save_locked()
            except Exception as error:
                finish_save_error = str(error)
            else:
                finish_save_error = None

        if finish_save_error is not None:
            await self._report_error(
                "Buyruq natijasini saqlashda xatolik",
                f"ID: {request_id}\nXato: {finish_save_error}",
            )

        return response

    # -------------------------------------------------
    # 8. ADMIN UCHUN BUYRUQ SHLYUZI
    # -------------------------------------------------

    async def send_command(
        self,
        command: str,
        target: Optional[str] = None,
        payload: Optional[dict] = None,
    ):
        if not target:
            return {
                "success": False,
                "error": "Qabul qiluvchi modul ko'rsatilmagan.",
            }

        return await self.request(
            source="admin",
            target=target,
            command=command,
            payload=payload or {},
        )

    # -------------------------------------------------
    # 9. BUYURTMA VA SO'ROV HOLATLARI
    # -------------------------------------------------

    async def get_job(self, request_id: str):
        async with self._data_lock:
            job = self.jobs.get(request_id)

            if not job:
                return None

            return copy.deepcopy(job)

    async def get_jobs(self, status: Optional[str] = None):
        async with self._data_lock:
            return [
                copy.deepcopy(job)
                for job in self.jobs.values()
                if (
                    status is None
                    or job.get("status") == status
                )
            ]

    async def get_request(self, request_id: str):
        async with self._data_lock:
            request = self.processed_requests.get(request_id)

            if not request:
                return None

            return copy.deepcopy(request)

    # -------------------------------------------------
    # 10. ESKI MA'LUMOTLARNI TOZALASH
    # -------------------------------------------------

    async def cleanup(self, retention_days: int = 30):
        retention_days = max(1, int(retention_days))
        cutoff = time.time() - retention_days * 86400

        removed = 0
        save_error = None

        async with self._data_lock:
            removable_ids = [
                job_id
                for job_id, job in self.jobs.items()
                if (
                    isinstance(job, dict)
                    and job.get("status") in (
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                    )
                    and job.get("finished_at", 0) < cutoff
                )
            ]

            for job_id in removable_ids:
                self.jobs.pop(job_id, None)
                self.processed_requests.pop(job_id, None)
                removed += 1

            try:
                await self._save_locked()
            except Exception as error:
                save_error = str(error)

        if save_error is not None:
            await self._report_error(
                "SUPRA tozalash jarayonida xatolik",
                save_error,
            )
            return {
                "success": False,
                "removed": 0,
                "error": save_error,
            }

        return {
            "success": True,
            "removed": removed,
        }

    # -------------------------------------------------
    # 11. TIZIMNI TO'XTATISH
    # -------------------------------------------------

    async def shutdown(self):
        self.running = False
        save_error = None

        async with self._registry_lock:
            async with self._data_lock:
                for job in self.jobs.values():
                    if (
                        isinstance(job, dict)
                        and job.get("status") == "processing"
                    ):
                        job["status"] = "interrupted"
                        job["finished_at"] = time.time()

                for request in self.processed_requests.values():
                    if (
                        isinstance(request, dict)
                        and request.get("status") == "processing"
                    ):
                        request["status"] = "interrupted"
                        request["finished_at"] = time.time()

                try:
                    await self._save_locked()
                except Exception as error:
                    save_error = str(error)

        if save_error is not None:
            await self._report_error(
                "SUPRA to'xtashida saqlash xatosi",
                save_error,
            )
            return False

        self.initialized = False
        return True

# -----------------------------------------------------
# SUPRA OBYEKTINI YARATISH
# -----------------------------------------------------

supra_gateway = SupraGateway()

import asyncio
import copy
import inspect
import json
import os
import uuid

from pathlib import Path
from datetime import datetime, timezone

from aiogram import Router
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

class ButtonsManager:
    """
    SUPRA ichki yordamchi tugmalar boshqaruv qismi.

    VERSION: 1.3.0

    Vazifalari:
        - Foydalanuvchi tugmalarini boshqarish.
        - Admin menyulari uchun InlineKeyboard yaratish.
        - Menyularni belgilangan tartibda ko'rsatish.
        - Tugma bosilishini qayta ishlash.
        - Buyruqlarni SUPRA Gateway orqali yuborish.
        - Tugmalar statistikasini saqlash.
        - Tugma sozlamalarini boshqarish.
        - Modul tasdiqlash klaviaturasini yaratish.

    Bu qism mustaqil tashqi modul emas.
    Gateway'da alohida modul sifatida ro'yxatdan o'tmaydi.
    """

    MODULE_NAME = "tugmalar"
    VERSION = "1.3.0"

    FROZEN_MESSAGE = (
        "Kechirasiz, texnik sababga ko‘ra bu bo‘lim ishlamaydi."
    )

    MAIN_BUTTONS = {
        "BUYURTMA": "BUYURTMA",
        "PROFIL": "PROFIL",
        "PREMIUM": "PREMIUM",
    }

    PREMIUM_BUTTONS = {
        "ODDIY": "ODDIY",
        "MAXSUS": "MAXSUS",
        "SUPER": "SUPER",
    }

    ALLOWED_ROUTES = {
        "BUYURTMA": (
            "Xavfsizlik_markazi",
            "start_order",
        ),
        "PROFIL": (
            "foydalanuvchilar",
            "get_own_profile",
        ),
        "PREMIUM": (
            None,
            None,
        ),
        "PREMIUM_ODDIY": (
            "sozlamalar",
            "get_premium_tariff",
        ),
        "PREMIUM_MAXSUS": (
            "sozlamalar",
            "get_premium_tariff",
        ),
        "PREMIUM_SUPER": (
            "sozlamalar",
            "get_premium_tariff",
        ),
    }

    def __init__(
        self,
        bot,
        supra_gateway,
        is_source_authorized=None,
        notify_admin=None,
        report_security_event=None,
        data_file="tugmalar_data.json",
    ):
        self.bot = bot
        self.supra_gateway = supra_gateway
        self.is_source_authorized = is_source_authorized
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event

        self.data_file = Path(data_file)
        self.router = Router(name=self.MODULE_NAME)

        self.buttons = {}
        self.statistics = {}

        self.lock = asyncio.Lock()
        self.initialized = False

        self._register_handlers()

    # =================================================
    # VAQT
    # =================================================

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _date_key():
        return ButtonsManager._now().date().isoformat()

    @staticmethod
    def _week_key():
        now = ButtonsManager._now()
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"

    @staticmethod
    def _month_key():
        return ButtonsManager._now().strftime("%Y-%m")

    # =================================================
    # CALLBACK YORDAMCHILARI
    # =================================================

    @staticmethod
    async def _resolve(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _notify_admin(self, message):
        if not callable(self.notify_admin):
            return False

        try:
            await self._resolve(
                self.notify_admin(str(message))
            )
            return True
        except Exception:
            return False

    async def _security_event(self, title, details):
        if not callable(self.report_security_event):
            await self._notify_admin(
                f"SUPRA TUGMALAR XABARI\n{title}\n{details}"
            )
            return False

        event = {
            "type": "BUTTONS_ERROR",
            "event_type": "BUTTONS_ERROR",
            "module": self.MODULE_NAME,
            "title": str(title),
            "description": str(details),
            "timestamp": self._now().isoformat(),
        }

        try:
            await self._resolve(
                self.report_security_event(event)
            )
            return True
        except Exception:
            await self._notify_admin(
                f"SUPRA TUGMALAR XABARI\n{title}\n{details}"
            )
            return False

    # =================================================
    # ADMIN KLAVIATURALARI
    # =================================================

    @staticmethod
    def build_admin_main_menu(main_sections, extra_actions):
        """
        Admin asosiy menyusini yaratadi.
        Callback ma'lumotlarini Admin moduli qayta ishlaydi.
        """
        keyboard = []

        for key, title in main_sections.items():
            keyboard.append([
                InlineKeyboardButton(
                    text=str(title),
                    callback_data=f"admin:section:{key}",
                )
            ])

        for action, title in extra_actions:
            keyboard.append([
                InlineKeyboardButton(
                    text=str(title),
                    callback_data=f"admin:extra:{action}",
                )
            ])

        return InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )

    @staticmethod
    def build_admin_submenu(items):
        """
        Admin ichki bo'lim klaviaturasini yaratadi.
        """
        keyboard = []

        for action, title in items:
            keyboard.append([
                InlineKeyboardButton(
                    text=str(title),
                    callback_data=f"admin:action:{action}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                text="⬅️ ORQAGA",
                callback_data="admin:home",
            )
        ])

        return InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )

    @staticmethod
    def build_admin_extra_menu(extra_actions):
        """
        Admin qo'shimcha amallar klaviaturasini yaratadi.
        """
        keyboard = []

        for action, title in extra_actions:
            keyboard.append([
                InlineKeyboardButton(
                    text=str(title),
                    callback_data=f"admin:extra:{action}",
                )
            ])

        keyboard.append([
            InlineKeyboardButton(
                text="⬅️ ASOSIY MENYU",
                callback_data="admin:home",
            )
        ])

        return InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )

    @staticmethod
    def build_module_review_menu(token):
        """
        Modulni tasdiqlash yoki rad etish klaviaturasi.
        """
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 64
        ):
            raise ValueError("Modul tokeni noto'g'ri.")

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ TASDIQLASH",
                        callback_data=(
                            f"security:module:approve:{token}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ RAD ETISH",
                        callback_data=(
                            f"security:module:deny:{token}"
                        ),
                    )
                ],
            ]
        )

    # =================================================
    # BOSHLANG'ICH TUGMALAR
    # =================================================

    def _default_buttons(self):
        return {
            "BUYURTMA": {
                "label": "BUYURTMA",
                "visible": True,
                "frozen": False,
                "target": "Xavfsizlik_markazi",
                "command": "start_order",
            },
            "PROFIL": {
                "label": "PROFIL",
                "visible": True,
                "frozen": False,
                "target": "foydalanuvchilar",
                "command": "get_own_profile",
            },
            "PREMIUM": {
                "label": "PREMIUM",
                "visible": True,
                "frozen": False,
                "target": None,
                "command": None,
            },
            "PREMIUM_ODDIY": {
                "label": "ODDIY",
                "visible": True,
                "frozen": False,
                "target": "sozlamalar",
                "command": "get_premium_tariff",
            },
            "PREMIUM_MAXSUS": {
                "label": "MAXSUS",
                "visible": True,
                "frozen": False,
                "target": "sozlamalar",
                "command": "get_premium_tariff",
            },
            "PREMIUM_SUPER": {
                "label": "SUPER",
                "visible": True,
                "frozen": False,
                "target": "sozlamalar",
                "command": "get_premium_tariff",
            },
        }

    # =================================================
    # TUGMA SOZLAMALARINI TEKSHIRISH
    # =================================================

    def _validate_button_route(self, key, target, command):
        if key not in self.ALLOWED_ROUTES:
            return False

        allowed_target, allowed_command = (
            self.ALLOWED_ROUTES[key]
        )

        return (
            target == allowed_target
            and command == allowed_command
        )

    def _validate_button_data(self, key, item):
        if not isinstance(item, dict):
            return False

        if not isinstance(item.get("label"), str):
            return False

        if not item["label"].strip():
            return False

        if len(item["label"]) > 50:
            return False

        if not isinstance(item.get("visible"), bool):
            return False

        if not isinstance(item.get("frozen"), bool):
            return False

        return self._validate_button_route(
            key,
            item.get("target"),
            item.get("command"),
        )

    # =================================================
    # MA'LUMOTLARNI SAQLASH
    # =================================================

    async def _save_locked(self):
        data = {
            "version": self.VERSION,
            "buttons": self.buttons,
            "statistics": self.statistics,
            "updated_at": self._now().isoformat(),
        }

        temporary_file = self.data_file.with_suffix(
            self.data_file.suffix + ".tmp"
        )

        try:
            self.data_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with open(
                temporary_file,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=4,
                )
                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temporary_file,
                self.data_file,
            )

            return True

        except Exception as error:
            try:
                if temporary_file.exists():
                    temporary_file.unlink()
            except OSError:
                pass

            raise RuntimeError(
                f"Tugmalar faylini saqlash xatosi: {error}"
            ) from error

    async def _save(self):
        async with self.lock:
            return await self._save_locked()

    # =================================================
    # YUKLASH
    # =================================================

    async def initialize(self):
        if self.initialized:
            return True

        try:
            defaults = self._default_buttons()

            if self.data_file.exists():
                with open(
                    self.data_file,
                    "r",
                    encoding="utf-8",
                ) as file:
                    data = json.load(file)

                if not isinstance(data, dict):
                    raise ValueError(
                        "Tugmalar fayli formati noto'g'ri."
                    )

                buttons = data.get("buttons")
                statistics = data.get("statistics")

                if not isinstance(buttons, dict):
                    raise ValueError(
                        "Tugmalar ro'yxati noto'g'ri."
                    )

                if not isinstance(statistics, dict):
                    raise ValueError(
                        "Statistika formati noto'g'ri."
                    )

                buttons = copy.deepcopy(buttons)
                changed = False

                for key, default in defaults.items():
                    if key not in buttons:
                        buttons[key] = copy.deepcopy(default)
                        changed = True
                        continue

                    if not self._validate_button_data(
                        key,
                        buttons[key],
                    ):
                        raise ValueError(
                            f"Tugma sozlamasi noto'g'ri: {key}"
                        )

                self.buttons = buttons
                self.statistics = statistics

                if changed:
                    await self._save()

            else:
                self.buttons = defaults
                self.statistics = {}
                await self._save()

            self.initialized = True
            return True

        except Exception as error:
            self.initialized = False

            await self._security_event(
                "Tugmalar faylini yuklashda xato",
                str(error),
            )

            await self._notify_admin(
                "DIQQAT! Tugmalar ma'lumotlarini yuklashda xato.\n"
                "Mavjud fayl avtomatik o'chirilmadi yoki almashtirilmadi."
            )

            return False

    # =================================================
    # TUGMA HOLATI
    # =================================================

    def _is_visible(self, key):
        button = self.buttons.get(key)
        return bool(button and button.get("visible") is True)

    def _is_frozen(self, key):
        button = self.buttons.get(key)
        return bool(button and button.get("frozen") is True)

    def _get_label(self, key):
        button = self.buttons.get(key, {})
        return button.get("label", key)

    @staticmethod
    def _normalize(text):
        return text.strip().casefold()

    def _find_button_by_text(self, text):
        normalized = self._normalize(text)

        for key in self.MAIN_BUTTONS:
            if (
                self._is_visible(key)
                and self._normalize(
                    self._get_label(key)
                ) == normalized
            ):
                return key

        for level in self.PREMIUM_BUTTONS:
            key = f"PREMIUM_{level}"

            if (
                self._is_visible(key)
                and self._normalize(
                    self._get_label(key)
                ) == normalized
            ):
                return key

        return None

    def _is_menu_message(self, message):
        if not message or not message.text:
            return False

        text = message.text.strip()

        if self._normalize(text) == self._normalize("ORTGA"):
            return True

        return self._find_button_by_text(text) is not None

    # =================================================
    # ASOSIY MENYU
    # =================================================

    def get_main_menu(self):
        if not self.initialized:
            return ReplyKeyboardRemove()

        rows = []
        row = []

        for key in self.MAIN_BUTTONS:
            if not self._is_visible(key):
                continue

            row.append(
                KeyboardButton(
                    text=self._get_label(key)
                )
            )

            if len(row) == 2:
                rows.append(row)
                row = []

        if row:
            rows.append(row)

        if not rows:
            return ReplyKeyboardRemove()

        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            is_persistent=True,
        )

    # =================================================
    # PREMIUM MENYUSI
    # =================================================

    def get_premium_menu(self):
        if not self.initialized:
            return ReplyKeyboardRemove()

        rows = []
        row = []

        for level in self.PREMIUM_BUTTONS:
            key = f"PREMIUM_{level}"

            if not self._is_visible(key):
                continue

            row.append(
                KeyboardButton(
                    text=self._get_label(key)
                )
            )

            if len(row) == 2:
                rows.append(row)
                row = []

        if row:
            rows.append(row)

        rows.append([
            KeyboardButton(text="ORTGA")
        ])

        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            is_persistent=True,
        )

    # =================================================
    # BOSHQARUV MANBASI RUXSATI
    # =================================================

    async def _check_source(self, source):
        if (
            not isinstance(source, str)
            or not source.strip()
        ):
            return False

        if not callable(self.is_source_authorized):
            return False

        try:
            result = await self._resolve(
                self.is_source_authorized(source)
            )

            return result is True

        except Exception:
            return False

    # =================================================
    # TUGMA SOZLAMALARINI O'ZGARTIRISH
    # =================================================

    async def update_button(
        self,
        source,
        button_key,
        visible=None,
        frozen=None,
        label=None,
        target=None,
        command=None,
    ):
        if not await self._check_source(source):
            await self._security_event(
                "Ruxsatsiz tugma sozlamasini o'zgartirish",
                f"Manba: {source}; tugma: {button_key}",
            )
            return False

        if button_key not in self.buttons:
            return False

        async with self.lock:
            old = copy.deepcopy(self.buttons[button_key])
            candidate = copy.deepcopy(old)

            try:
                if visible is not None:
                    if not isinstance(visible, bool):
                        return False
                    candidate["visible"] = visible

                if frozen is not None:
                    if not isinstance(frozen, bool):
                        return False
                    candidate["frozen"] = frozen

                if label is not None:
                    if (
                        not isinstance(label, str)
                        or not label.strip()
                        or len(label.strip()) > 50
                    ):
                        return False

                    candidate["label"] = label.strip()

                if target is not None:
                    if (
                        not isinstance(target, str)
                        or not target.strip()
                    ):
                        return False

                    candidate["target"] = target.strip()

                if command is not None:
                    if (
                        not isinstance(command, str)
                        or not command.strip()
                    ):
                        return False

                    candidate["command"] = command.strip()

                if not self._validate_button_data(
                    button_key,
                    candidate,
                ):
                    return False

                self.buttons[button_key] = candidate

                try:
                    await self._save_locked()
                except Exception:
                    self.buttons[button_key] = old
                    raise

                return True

            except Exception as error:
                self.buttons[button_key] = old

                await self._security_event(
                    "Tugma sozlamasini saqlashda xato",
                    str(error),
                )

                return False

    # =================================================
    # STATISTIKA
    # =================================================

    async def _record_click(
        self,
        button_key,
        telegram_id,
    ):
        async with self.lock:
            old_statistics = copy.deepcopy(
                self.statistics
            )

            try:
                button_stats = self.statistics.setdefault(
                    button_key,
                    {
                        "total": 0,
                        "users": [],
                        "daily": {},
                        "weekly": {},
                        "monthly": {},
                    },
                )

                if not isinstance(button_stats, dict):
                    raise ValueError(
                        "Tugma statistikasi noto'g'ri."
                    )

                button_stats["total"] = (
                    int(button_stats.get("total", 0)) + 1
                )

                users = button_stats.setdefault(
                    "users",
                    [],
                )

                if not isinstance(users, list):
                    raise ValueError(
                        "Foydalanuvchilar statistikasi noto'g'ri."
                    )

                user_id = str(telegram_id)

                if user_id not in users:
                    users.append(user_id)

                periods = (
                    ("daily", self._date_key()),
                    ("weekly", self._week_key()),
                    ("monthly", self._month_key()),
                )

                for period, key in periods:
                    values = button_stats.setdefault(
                        period,
                        {},
                    )

                    if not isinstance(values, dict):
                        raise ValueError(
                            f"{period} statistikasi noto'g'ri."
                        )

                    record = values.setdefault(
                        key,
                        {
                            "clicks": 0,
                            "users": [],
                        },
                    )

                    if not isinstance(record, dict):
                        raise ValueError(
                            "Davriy statistika yozuvi noto'g'ri."
                        )

                    record["clicks"] = (
                        int(record.get("clicks", 0)) + 1
                    )

                    period_users = record.setdefault(
                        "users",
                        [],
                    )

                    if not isinstance(period_users, list):
                        raise ValueError(
                            "Davriy foydalanuvchilar statistikasi noto'g'ri."
                        )

                    if user_id not in period_users:
                        period_users.append(user_id)

                await self._save_locked()
                return True

            except Exception:
                self.statistics = old_statistics
                raise

    async def get_statistics(
        self,
        source,
        button_key=None,
    ):
        if not await self._check_source(source):
            return None

        async with self.lock:
            if button_key is not None:
                if button_key not in self.statistics:
                    return None

                return copy.deepcopy(
                    self.statistics[button_key]
                )

            return copy.deepcopy(self.statistics)

    # =================================================
    # SUPRA GATEWAY ORQALI BUYRUQ YUBORISH
    # =================================================

    async def _dispatch(
        self,
        telegram_id,
        button_key,
    ):
        button = self.buttons.get(button_key)

        if not button:
            return False, "Tugma topilmadi."

        target = button.get("target")
        command = button.get("command")

        if not target or not command:
            return (
                False,
                "Bu tugma uchun xizmat hali ulanmagan.",
            )

        if self.supra_gateway is None:
            return (
                False,
                "SUPRA aloqa tizimi mavjud emas.",
            )

        if not self._validate_button_route(
            button_key,
            target,
            command,
        ):
            await self._security_event(
                "Noto'g'ri tugma yo'nalishi",
                (
                    f"Tugma: {button_key}\n"
                    f"Manzil: {target}\n"
                    f"Buyruq: {command}"
                ),
            )

            return (
                False,
                "Bu tugma uchun belgilangan yo'nalish noto'g'ri.",
            )

        request_id = str(uuid.uuid4())

        payload = {
            "telegram_id": telegram_id,
            "button": button_key,
            "request_id": request_id,
        }

        try:
            result = await self.supra_gateway.request(
                source=self.MODULE_NAME,
                target=target,
                command=command,
                payload=payload,
                request_id=request_id,
            )

            if not isinstance(result, dict):
                return (
                    False,
                    "SUPRA javobi noto'g'ri.",
                )

            if result.get("success") is not True:
                return (
                    False,
                    result.get(
                        "error",
                        "Buyruq bajarilmadi.",
                    ),
                )

            return True, result.get("result")

        except Exception as error:
            await self._security_event(
                "Tugma buyrug'ini yuborishda xato",
                str(error),
            )

            return (
                False,
                "Tizim bilan aloqa vaqtincha mavjud emas.",
            )

    # =================================================
    # TUGMA BOSILISHINI QAYTA ISHLASH
    # =================================================

    async def _handle_button(
        self,
        message: Message,
        button_key,
    ):
        if not message.from_user:
            return

        telegram_id = message.from_user.id
        button = self.buttons.get(button_key)

        if not button or not button.get("visible"):
            return

        if button.get("frozen"):
            await message.answer(
                self.FROZEN_MESSAGE
            )
            return

        try:
            await self._record_click(
                button_key,
                telegram_id,
            )

        except Exception as error:
            await self._security_event(
                "Tugma statistikasini saqlashda xato",
                str(error),
            )

            await message.answer(
                "Texnik xatolik yuz berdi. Keyinroq urinib ko'ring."
            )
            return

        if button_key == "PREMIUM":
            await message.answer(
                "Premium darajasini tanlang:",
                reply_markup=self.get_premium_menu(),
            )
            return

        success, response = await self._dispatch(
            telegram_id,
            button_key,
        )

        if not success:
            await message.answer(str(response))
            return

        if isinstance(response, dict):
            text = response.get("message")

            if text:
                await message.answer(str(text))
                return

        if button_key.startswith("PREMIUM_"):
            await message.answer(
                "Tarif ma'lumotlari so'raldi."
            )
        else:
            await message.answer(
                "So'rov tegishli modulga yetkazildi."
            )

    # =================================================
    # HANDLERLAR
    # =================================================

    def _register_handlers(self):

        @self.router.message(
            lambda message: self._is_menu_message(message)
        )
        async def menu_handler(message: Message):
            if not message.from_user or not message.text:
                return

            text = message.text.strip()

            if self._normalize(text) == self._normalize("ORTGA"):
                await message.answer(
                    "Asosiy menyu:",
                    reply_markup=self.get_main_menu(),
                )
                return

            button_key = self._find_button_by_text(text)

            if button_key:
                await self._handle_button(
                    message,
                    button_key,
                )

    # =================================================
    # MENYUNI KO'RSATISH
    # =================================================

    async def show_main_menu(self, telegram_id):
        if not self.initialized:
            return False

        try:
            await self.bot.send_message(
                chat_id=telegram_id,
                text="Asosiy menyu:",
                reply_markup=self.get_main_menu(),
            )

            return True

        except Exception as error:
            await self._security_event(
                "Asosiy menyuni ko'rsatishda xato",
                str(error),
            )
            return False

    # =================================================
    # MODUL HOLATI
    # =================================================

    async def health_check(self):
        return {
            "module": self.MODULE_NAME,
            "version": self.VERSION,
            "initialized": self.initialized,
            "buttons_count": len(self.buttons),
            "statistics_count": len(self.statistics),
            "gateway_connected": (
                self.supra_gateway is not None
            ),
            "authorization_configured": callable(
                self.is_source_authorized
            ),
            "admin_keyboard_support": True,
            "status": (
                "healthy"
                if self.initialized
                else "not_ready"
            ),
        }

# =====================================================
# MODULNI ISHGA TUSHIRISH
# =====================================================

buttons_manager = None

async def setup(
    bot,
    dp,
    supra_gateway,
    is_source_authorized=None,
    notify_admin=None,
    report_security_event=None,
    data_file="tugmalar_data.json",
):
    global buttons_manager

    if buttons_manager is not None:
        if buttons_manager.initialized:
            return buttons_manager

    manager = ButtonsManager(
        bot=bot,
        supra_gateway=supra_gateway,
        is_source_authorized=is_source_authorized,
        notify_admin=notify_admin,
        report_security_event=report_security_event,
        data_file=data_file,
    )

    initialized = await manager.initialize()

    if not initialized:
        raise RuntimeError(
            "Tugmalar modulini ishga tushirib bo'lmadi."
        )

    dp.include_router(manager.router)

    buttons_manager = manager
    return buttons_manager

async def initialize():
    if buttons_manager is None:
        return False

    return await buttons_manager.initialize()

async def health_check():
    if buttons_manager is None:
        return {
            "module": "tugmalar",
            "version": ButtonsManager.VERSION,
            "status": "not_started",
        }

    return await buttons_manager.health_check()

# =====================================================
# ADMIN VA BOSHQA ICHKI MODULLAR UCHUN INTERFEYS
# =====================================================

def get_admin_main_menu(main_sections, extra_actions):
    if buttons_manager is None:
        raise RuntimeError(
            "Tugmalar boshqaruv qismi hali ishga tushmagan."
        )

    return buttons_manager.build_admin_main_menu(
        main_sections,
        extra_actions,
    )

def get_admin_submenu(items):
    if buttons_manager is None:
        raise RuntimeError(
            "Tugmalar boshqaruv qismi hali ishga tushmagan."
        )

    return buttons_manager.build_admin_submenu(items)

def get_admin_extra_menu(extra_actions):
    if buttons_manager is None:
        raise RuntimeError(
            "Tugmalar boshqaruv qismi hali ishga tushmagan."
        )

    return buttons_manager.build_admin_extra_menu(
        extra_actions
    )

def get_module_review_keyboard(token):
    if buttons_manager is None:
        raise RuntimeError(
            "Tugmalar boshqaruv qismi hali ishga tushmagan."
        )

    return buttons_manager.build_module_review_menu(token)

import asyncio
import copy
import inspect
import json
import os
import uuid

from pathlib import Path
from datetime import datetime, timezone

class SettingsManager:
    """
    SUPRA markaziy sozlamalar boshqaruvchisi.
    VERSION: 1.1.0
    """

    MODULE_NAME = "sozlamalar"
    VERSION = "1.1.0"

    MAX_CHANNELS = 10
    MAX_HISTORY = 500

    DISTRIBUTION_TARGETS = (
        "tugmalar",
        "foydalanuvchilar",
        "Xavfsizlik_markazi",
    )

    PREMIUM_LEVELS = ("ODDIY", "MAXSUS", "SUPER")

    def __init__(
        self,
        supra_gateway=None,
        is_admin_verified=None,
        notify_admin=None,
        report_security_event=None,
        data_file="sozlamalar_data.json",
    ):
        self.supra_gateway = supra_gateway
        self.is_admin_verified = is_admin_verified
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event

        self.data_file = Path(data_file)

        self.settings = {}
        self.frozen_settings = set()
        self.pending_changes = {}
        self.pending_distribution = {}
        self.history = []

        self.version = 1
        self.initialized = False

        self.lock = asyncio.Lock()

    # =================================================
    # YORDAMCHI FUNKSIYALAR
    # =================================================

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    async def _resolve(value):
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _default_settings():
        return {
            "users": {
                "premium_warning_days": [3, 1],
            },
            "premium": {
                "tariffs": {
                    "ODDIY": {
                        "price": 0,
                        "duration_days": 30,
                    },
                    "MAXSUS": {
                        "price": 0,
                        "duration_days": 30,
                    },
                    "SUPER": {
                        "price": 0,
                        "duration_days": 30,
                    },
                },
            },
            "orders": {
                "daily_limits": {
                    "ODDIY": 2,
                    "PREMIUM_ODDIY": 5,
                    "MAXSUS": 10,
                    "SUPER": 20,
                },
            },
            "bonus": {
                "daily_amount": 0,
            },
            "channels": {
                "registration_mandatory": [],
                "daily_bonus_mandatory": [],
                "bonus_channels": [],
            },
            "buttons": {
                "BUYURTMA": {
                    "visible": True,
                    "frozen": False,
                },
                "PROFIL": {
                    "visible": True,
                    "frozen": False,
                },
                "PREMIUM": {
                    "visible": True,
                    "frozen": False,
                },
                "PREMIUM_ODDIY": {
                    "visible": True,
                    "frozen": False,
                },
                "PREMIUM_MAXSUS": {
                    "visible": True,
                    "frozen": False,
                },
                "PREMIUM_SUPER": {
                    "visible": True,
                    "frozen": False,
                },
            },
            "security": {
                "admin_panel_duration_minutes": 60,
            },
        }

    @classmethod
    def _merge_defaults(cls, saved, defaults):
        """
        Eski JSON faylida yangi bo'limlar yetishmasa,
        mavjud qiymatlarni saqlagan holda to'ldiradi.
        """
        result = copy.deepcopy(defaults)

        for key, value in saved.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(value, dict)
            ):
                result[key] = cls._merge_defaults(
                    value,
                    result[key],
                )
            else:
                result[key] = copy.deepcopy(value)

        return result

    # =================================================
    # XABARNOMALAR
    # =================================================

    async def _notify_admin(self, message):
        if not callable(self.notify_admin):
            return False

        try:
            await self._resolve(
                self.notify_admin(str(message))
            )
            return True
        except Exception:
            return False

    async def _security_event(self, title, details):
        if not callable(self.report_security_event):
            await self._notify_admin(
                f"SUPRA XAVFSIZLIK XABARI\n{title}\n{details}"
            )
            return False

        event = {
            "type": "SETTINGS_ERROR",
            "event_type": "SETTINGS_ERROR",
            "module": self.MODULE_NAME,
            "title": str(title),
            "description": str(details),
            "timestamp": self._now(),
        }

        try:
            await self._resolve(
                self.report_security_event(event)
            )
            return True
        except Exception:
            await self._notify_admin(
                f"SUPRA XAVFSIZLIK XABARI\n{title}\n{details}"
            )
            return False

    # =================================================
    # ADMIN TEKSHIRUVI
    # =================================================

    async def _check_admin(self, admin_id):
        if (
            not isinstance(admin_id, int)
            or isinstance(admin_id, bool)
            or not callable(self.is_admin_verified)
        ):
            return False

        try:
            result = await self._resolve(
                self.is_admin_verified(admin_id)
            )
            return result is True
        except Exception:
            return False

    # =================================================
    # FAYLGA SAQLASH
    # =================================================

    async def _save_locked(self):
        data = {
            "version": self.version,
            "settings": self.settings,
            "frozen_settings": sorted(self.frozen_settings),
            "pending_changes": self.pending_changes,
            "pending_distribution": self.pending_distribution,
            "history": self.history[-self.MAX_HISTORY:],
            "updated_at": self._now(),
        }

        temporary_file = self.data_file.with_suffix(
            self.data_file.suffix + ".tmp"
        )

        try:
            self.data_file.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with open(
                temporary_file,
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=4,
                )
                file.flush()
                os.fsync(file.fileno())

            os.replace(
                temporary_file,
                self.data_file,
            )

            return True

        except Exception as error:
            try:
                if temporary_file.exists():
                    temporary_file.unlink()
            except OSError:
                pass

            raise RuntimeError(
                f"Sozlamalarni saqlashda xato: {error}"
            ) from error

    async def _save(self):
        async with self.lock:
            return await self._save_locked()

    # =================================================
    # YUKLASH
    # =================================================

    async def initialize(self):
        if self.initialized:
            return True

        async with self.lock:
            try:
                defaults = self._default_settings()

                if not self.data_file.exists():
                    self.settings = defaults
                    self.frozen_settings = set()
                    self.pending_changes = {}
                    self.pending_distribution = {}
                    self.history = []
                    self.version = 1

                    await self._save_locked()

                else:
                    with open(
                        self.data_file,
                        "r",
                        encoding="utf-8",
                    ) as file:
                        data = json.load(file)

                    if not isinstance(data, dict):
                        raise ValueError(
                            "Sozlamalar fayli formati noto'g'ri."
                        )

                    saved_settings = data.get("settings")

                    if not isinstance(saved_settings, dict):
                        raise ValueError(
                            "Sozlamalar bo'limi noto'g'ri."
                        )

                    frozen = data.get("frozen_settings", [])
                    pending = data.get("pending_changes", {})
                    distribution = data.get(
                        "pending_distribution",
                        {},
                    )
                    history = data.get("history", [])

                    if (
                        not isinstance(frozen, list)
                        or not all(
                            isinstance(item, str)
                            for item in frozen
                        )
                    ):
                        raise ValueError(
                            "Muzlatilgan sozlamalar formati noto'g'ri."
                        )

                    if not isinstance(pending, dict):
                        raise ValueError(
                            "Kutilayotgan o'zgarishlar formati noto'g'ri."
                        )

                    if not isinstance(distribution, dict):
                        raise ValueError(
                            "Tarqatish navbati formati noto'g'ri."
                        )

                    if not isinstance(history, list):
                        raise ValueError(
                            "Tarix formati noto'g'ri."
                        )

                    self.settings = self._merge_defaults(
                        saved_settings,
                        defaults,
                    )

                    self.frozen_settings = set(frozen)
                    self.pending_changes = pending
                    self.pending_distribution = distribution
                    self.history = history[-self.MAX_HISTORY:]

                    self.version = data.get("version", 1)

                    if (
                        not isinstance(self.version, int)
                        or isinstance(self.version, bool)
                        or self.version < 1
                    ):
                        raise ValueError(
                            "Sozlamalar versiyasi noto'g'ri."
                        )

                    errors = self.validate_all(self.settings)

                    if errors:
                        raise ValueError(
                            "Saqlangan sozlamalar tekshiruvdan o'tmadi: "
                            + "; ".join(errors)
                        )

                self.initialized = True
                return True

            except Exception as error:
                self.initialized = False

                await self._security_event(
                    "Sozlamalar faylini yuklashda xato",
                    str(error),
                )

                await self._notify_admin(
                    "DIQQAT! Sozlamalar faylini yuklashda xato.\n"
                    "Fayl avtomatik o'chirilmadi yoki almashtirilmadi."
                )

                return False

    # =================================================
    # ICHKI QIYMATNI OLISH VA O'ZGARTIRISH
    # =================================================

    @staticmethod
    def _get_nested(data, path):
        if not isinstance(path, str) or not path:
            raise KeyError(path)

        current = data

        for part in path.split("."):
            if not isinstance(current, dict):
                raise KeyError(path)

            if part not in current:
                raise KeyError(path)

            current = current[part]

        return copy.deepcopy(current)

    @staticmethod
    def _set_nested(data, path, value):
        if not isinstance(path, str) or not path:
            raise KeyError(path)

        parts = path.split(".")
        current = data

        for part in parts[:-1]:
            if part not in current:
                raise KeyError(path)

            if not isinstance(current[part], dict):
                raise KeyError(path)

            current = current[part]

        if parts[-1] not in current:
            raise KeyError(path)

        current[parts[-1]] = copy.deepcopy(value)

    # =================================================
    # KANALLARNI TEKSHIRISH
    # =================================================

    @staticmethod
    def _validate_channels(channels):
        if not isinstance(channels, list):
            return False

        if len(channels) > SettingsManager.MAX_CHANNELS:
            return False

        seen = set()

        for channel in channels:
            if not isinstance(channel, (str, int)):
                return False

            if isinstance(channel, bool):
                return False

            value = str(channel).strip()

            if not value or len(value) > 200:
                return False

            if value in seen:
                return False

            seen.add(value)

        return True

    # =========== 