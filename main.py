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

    # =================================================
    # SOZLAMALARNI TO'LIQ TEKSHIRISH
    # =================================================

    @classmethod
    def validate_all(cls, settings):
        errors = []

        if not isinstance(settings, dict):
            return ["Sozlamalar formati noto'g'ri."]

        defaults = cls._default_settings()

        for section in defaults:
            if not isinstance(settings.get(section), dict):
                errors.append(
                    f"{section} bo'limi mavjud emas yoki noto'g'ri."
                )

        if errors:
            return errors

        warnings = settings["users"].get(
            "premium_warning_days"
        )

        if (
            not isinstance(warnings, list)
            or any(
                not isinstance(day, int)
                or isinstance(day, bool)
                or day < 0
                for day in warnings
            )
            or len(set(warnings)) != len(warnings)
        ):
            errors.append(
                "Premium ogohlantirish kunlari noto'g'ri."
            )

        tariffs = settings["premium"].get("tariffs")

        if not isinstance(tariffs, dict):
            errors.append("Premium tariflari noto'g'ri.")
        else:
            for level in cls.PREMIUM_LEVELS:
                tariff = tariffs.get(level)

                if not isinstance(tariff, dict):
                    errors.append(
                        f"{level} tarifi mavjud emas."
                    )
                    continue

                price = tariff.get("price")
                duration = tariff.get("duration_days")

                if (
                    not isinstance(price, (int, float))
                    or isinstance(price, bool)
                    or price < 0
                ):
                    errors.append(
                        f"{level} tarifining narxi noto'g'ri."
                    )

                if (
                    not isinstance(duration, int)
                    or isinstance(duration, bool)
                    or duration <= 0
                ):
                    errors.append(
                        f"{level} tarifining muddati noto'g'ri."
                    )

        limits = settings["orders"].get("daily_limits")

        if not isinstance(limits, dict):
            errors.append("Buyurtma limitlari noto'g'ri.")
        else:
            for key in (
                "ODDIY",
                "PREMIUM_ODDIY",
                "MAXSUS",
                "SUPER",
            ):
                value = limits.get(key)

                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or value < 0
                ):
                    errors.append(
                        f"{key} buyurtma limiti noto'g'ri."
                    )

        bonus = settings["bonus"].get("daily_amount")

        if (
            not isinstance(bonus, (int, float))
            or isinstance(bonus, bool)
            or bonus < 0
        ):
            errors.append("Kunlik BONUS miqdori noto'g'ri.")

        channels = settings["channels"]

        for group in (
            "registration_mandatory",
            "daily_bonus_mandatory",
            "bonus_channels",
        ):
            if not cls._validate_channels(
                channels.get(group)
            ):
                errors.append(
                    f"{group} kanallar ro'yxati noto'g'ri."
                )

        buttons = settings["buttons"]

        if not isinstance(buttons, dict):
            errors.append("Tugmalar sozlamalari noto'g'ri.")
        else:
            for key, value in buttons.items():
                if not isinstance(value, dict):
                    errors.append(
                        f"{key} tugmasi sozlamasi noto'g'ri."
                    )
                    continue

                if not isinstance(value.get("visible"), bool):
                    errors.append(
                        f"{key} tugmasining visible qiymati noto'g'ri."
                    )

                if not isinstance(value.get("frozen"), bool):
                    errors.append(
                        f"{key} tugmasining frozen qiymati noto'g'ri."
                    )

        duration = settings["security"].get(
            "admin_panel_duration_minutes"
        )

        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not 1 <= duration <= 1440
        ):
            errors.append(
                "Admin panel muddati 1 dan 1440 daqiqagacha bo'lishi kerak."
            )

        return errors

    # =================================================
    # ADMIN O'ZGARISH TAKLIFI
    # =================================================

    async def stage_change(
        self,
        admin_id,
        setting_path,
        new_value,
    ):
        if not await self._check_admin(admin_id):
            await self._security_event(
                "Ruxsatsiz sozlama o'zgarishi",
                f"Admin ID: {admin_id}; sozlama: {setting_path}",
            )
            return {
                "ok": False,
                "error": "Admin ruxsati mavjud emas.",
            }

        if not self.initialized:
            return {
                "ok": False,
                "error": "Sozlamalar moduli tayyor emas.",
            }

        async with self.lock:
            if setting_path in self.frozen_settings:
                return {
                    "ok": False,
                    "error": "Bu sozlama muzlatilgan.",
                }

            try:
                old_value = self._get_nested(
                    self.settings,
                    setting_path,
                )

                candidate = copy.deepcopy(self.settings)

                self._set_nested(
                    candidate,
                    setting_path,
                    new_value,
                )

            except (KeyError, TypeError):
                return {
                    "ok": False,
                    "error": "Bunday sozlama mavjud emas.",
                }

            errors = self.validate_all(candidate)

            if errors:
                return {
                    "ok": False,
                    "error": errors,
                }

            change_id = str(uuid.uuid4())

            self.pending_changes[change_id] = {
                "change_id": change_id,
                "admin_id": admin_id,
                "setting_path": setting_path,
                "old_value": old_value,
                "new_value": copy.deepcopy(new_value),
                "status": "waiting_confirmation",
                "created_at": self._now(),
            }

            try:
                await self._save_locked()
            except Exception as error:
                self.pending_changes.pop(change_id, None)

                await self._security_event(
                    "O'zgarish taklifini saqlashda xato",
                    str(error),
                )

                return {
                    "ok": False,
                    "error": "Taklifni saqlashda xato.",
                }

        await self._notify_admin(
            "SOZLAMA O'ZGARISHI TAKLIF QILINDI\n\n"
            f"ID: {change_id}\n"
            f"Sozlama: {setting_path}\n"
            f"Eski qiymat: {old_value}\n"
            f"Yangi qiymat: {new_value}\n\n"
            "Tasdiqlash yoki rad etish mumkin."
        )

        return {
            "ok": True,
            "change_id": change_id,
            "status": "waiting_confirmation",
        }

    # =================================================
    # O'ZGARISHNI TASDIQLASH
    # =================================================

    async def confirm_change(self, admin_id, change_id):
        if not await self._check_admin(admin_id):
            return {
                "ok": False,
                "error": "Admin ruxsati mavjud emas.",
            }

        if not self.initialized:
            return {
                "ok": False,
                "error": "Sozlamalar moduli tayyor emas.",
            }

        async with self.lock:
            change = self.pending_changes.get(change_id)

            if not isinstance(change, dict):
                return {
                    "ok": False,
                    "error": "O'zgarish topilmadi.",
                }

            if change.get("status") != "waiting_confirmation":
                return {
                    "ok": False,
                    "error": "Bu o'zgarish avval ko'rib chiqilgan.",
                }

            if change.get("admin_id") != admin_id:
                return {
                    "ok": False,
                    "error": "O'zgarishni faqat uni taklif qilgan Admin tasdiqlashi mumkin.",
                }

            path = change.get("setting_path")

            if path in self.frozen_settings:
                return {
                    "ok": False,
                    "error": "Bu sozlama muzlatilgan.",
                }

            try:
                current = self._get_nested(
                    self.settings,
                    path,
                )
            except KeyError:
                return {
                    "ok": False,
                    "error": "Sozlama topilmadi.",
                }

            if current != change.get("old_value"):
                return {
                    "ok": False,
                    "error": "Sozlama o'zgargan. Yangi taklif yarating.",
                }

            candidate = copy.deepcopy(self.settings)

            self._set_nested(
                candidate,
                path,
                change.get("new_value"),
            )

            errors = self.validate_all(candidate)

            if errors:
                return {
                    "ok": False,
                    "error": errors,
                }

            old_settings = self.settings
            old_version = self.version
            old_change = copy.deepcopy(change)

            new_version = old_version + 1

            # Avval mahalliy tasdiqlangan holat saqlanadi.
            # Tarqatish esa alohida navbat orqali bajariladi.
            self.settings = candidate
            self.version = new_version

            change["status"] = "approved"
            change["finished_at"] = self._now()

            distribution_id = str(uuid.uuid4())

            self.pending_distribution[distribution_id] = {
                "distribution_id": distribution_id,
                "setting_path": path,
                "value": copy.deepcopy(change["new_value"]),
                "version": new_version,
                "targets": {
                    target: "waiting"
                    for target in self.DISTRIBUTION_TARGETS
                },
                "created_at": self._now(),
            }

            self.history.append({
                "admin_id": admin_id,
                "setting_path": path,
                "old_value": change["old_value"],
                "new_value": change["new_value"],
                "version": new_version,
                "time": self._now(),
                "status": "approved",
            })

            self.history = self.history[-self.MAX_HISTORY:]

            try:
                await self._save_locked()
            except Exception as error:
                self.settings = old_settings
                self.version = old_version
                self.pending_distribution.pop(
                    distribution_id,
                    None,
                )
                self.pending_changes[change_id] = old_change
                self.history.pop()

                await self._security_event(
                    "Tasdiqlangan sozlamani saqlashda xato",
                    str(error),
                )

                return {
                    "ok": False,
                    "error": "Yangi sozlamani saqlash amalga oshmadi.",
                }

        await self._notify_admin(
            "SOZLAMA TASDIQLANDI.\n"
            f"Sozlama: {path}\n"
            f"Yangi versiya: {new_version}\n"
            "Boshqa modullarga tarqatish navbatga qo'yildi."
        )

        return {
            "ok": True,
            "version": new_version,
            "setting_path": path,
            "distribution_id": distribution_id,
            "distribution_status": "waiting",
        }

    # =================================================
    # SOZLAMANI MODULLARGA TARQATISH
    # =================================================

    async def sync_pending(self, admin_id):
        if not await self._check_admin(admin_id):
            return {
                "ok": False,
                "error": "Admin ruxsati mavjud emas.",
            }

        if self.supra_gateway is None:
            return {
                "ok": False,
                "error": "SUPRA Gateway mavjud emas.",
            }

        async with self.lock:
            jobs = copy.deepcopy(
                self.pending_distribution
            )

        results = []

        for distribution_id, job in jobs.items():
            for target, status in job["targets"].items():
                if status == "delivered":
                    continue

                request_id = str(uuid.uuid4())

                try:
                    response = await self.supra_gateway.request(
                        source=self.MODULE_NAME,
                        target=target,
                        command="settings_updated",
                        payload={
                            "distribution_id": distribution_id,
                            "setting_path": job["setting_path"],
                            "value": copy.deepcopy(job["value"]),
                            "version": job["version"],
                        },
                        request_id=request_id,
                    )

                    delivered = (
                        isinstance(response, dict)
                        and response.get("ok") is True
                    )

                    if delivered:
                        result_data = response.get("response")

                        if (
                            isinstance(result_data, dict)
                            and result_data.get("ok") is False
                        ):
                            delivered = False

                except Exception:
                    delivered = False

                async with self.lock:
                    current = self.pending_distribution.get(
                        distribution_id
                    )

                    if current:
                        current["targets"][target] = (
                            "delivered" if delivered else "waiting"
                        )

                        try:
                            await self._save_locked()
                        except Exception as error:
                            await self._security_event(
                                "Tarqatish holatini saqlashda xato",
                                str(error),
                            )

                results.append({
                    "distribution_id": distribution_id,
                    "target": target,
                    "delivered": delivered,
                })

        return {
            "ok": True,
            "results": results,
        }

    # =================================================
    # O'ZGARISHNI RAD ETISH
    # =================================================

    async def reject_change(self, admin_id, change_id):
        if not await self._check_admin(admin_id):
            return False

        async with self.lock:
            change = self.pending_changes.get(change_id)

            if not isinstance(change, dict):
                return False

            if change.get("status") != "waiting_confirmation":
                return False

            if change.get("admin_id") != admin_id:
                return False

            old_change = copy.deepcopy(change)

            change["status"] = "rejected"
            change["finished_at"] = self._now()

            self.history.append({
                "admin_id": admin_id,
                "setting_path": change["setting_path"],
                "old_value": change["old_value"],
                "new_value": change["new_value"],
                "time": self._now(),
                "status": "rejected",
            })

            self.history = self.history[-self.MAX_HISTORY:]

            try:
                await self._save_locked()
            except Exception:
                self.pending_changes[change_id] = old_change
                self.history.pop()
                return False

        await self._notify_admin(
            f"Sozlama o'zgarishi rad etildi: {change_id}"
        )

        return True

    # =================================================
    # SOZLAMANI MUZLATISH
    # =================================================

    async def freeze_setting(
        self,
        admin_id,
        setting_path,
        frozen=True,
    ):
        if not await self._check_admin(admin_id):
            return False

        if not isinstance(frozen, bool):
            return False

        try:
            self._get_nested(
                self.settings,
                setting_path,
            )
        except KeyError:
            return False

        async with self.lock:
            old_frozen = set(self.frozen_settings)

            if frozen:
                self.frozen_settings.add(setting_path)
            else:
                self.frozen_settings.discard(setting_path)

            try:
                await self._save_locked()
            except Exception:
                self.frozen_settings = old_frozen
                return False

        await self._notify_admin(
            f"Sozlama {setting_path}: "
            + ("MUZLATILDI" if frozen else "FAOLLASHTIRILDI")
        )

        return True

    # =================================================
    # SOZLAMALARNI OLISH
    # =================================================

    async def get_setting(self, setting_path):
        if not self.initialized:
            return None

        async with self.lock:
            try:
                return self._get_nested(
                    self.settings,
                    setting_path,
                )
            except KeyError:
                return None

    async def get_all_settings(self, admin_id):
        if not await self._check_admin(admin_id):
            return None

        async with self.lock:
            return copy.deepcopy(self.settings)

    async def get_history(self, admin_id, limit=50):
        if not await self._check_admin(admin_id):
            return None

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
        ):
            return None

        async with self.lock:
            return copy.deepcopy(
                self.history[-min(limit, self.MAX_HISTORY):]
            )

    async def get_pending_changes(self, admin_id):
        if not await self._check_admin(admin_id):
            return None

        async with self.lock:
            return copy.deepcopy(self.pending_changes)

    async def get_pending_distribution(self, admin_id):
        if not await self._check_admin(admin_id):
            return None

        async with self.lock:
            return copy.deepcopy(self.pending_distribution)

    # =================================================
    # KANAL GURUHLARI
    # =================================================

    async def set_channel_group(
        self,
        admin_id,
        group_name,
        channels,
    ):
        allowed = (
            "registration_mandatory",
            "daily_bonus_mandatory",
            "bonus_channels",
        )

        if group_name not in allowed:
            return {
                "ok": False,
                "error": "Kanal guruhi nomi noto'g'ri.",
            }

        if not self._validate_channels(channels):
            return {
                "ok": False,
                "error": "Kanallar ro'yxati noto'g'ri yoki 10 tadan ko'p.",
            }

        return await self.stage_change(
            admin_id,
            f"channels.{group_name}",
            channels,
        )

    # =================================================
    # BUYURTMA LIMITLARI
    # =================================================

    async def set_order_limit(
        self,
        admin_id,
        category,
        limit,
    ):
        allowed = (
            "ODDIY",
            "PREMIUM_ODDIY",
            "MAXSUS",
            "SUPER",
        )

        if category not in allowed:
            return {
                "ok": False,
                "error": "Toifa noto'g'ri.",
            }

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 0
        ):
            return {
                "ok": False,
                "error": "Buyurtma limiti noto'g'ri.",
            }

        return await self.stage_change(
            admin_id,
            f"orders.daily_limits.{category}",
            limit,
        )

    # =================================================
    # KUNLIK BONUS
    # =================================================

    async def set_daily_bonus(self, admin_id, amount):
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or amount < 0
        ):
            return {
                "ok": False,
                "error": "BONUS miqdori noto'g'ri.",
            }

        return await self.stage_change(
            admin_id,
            "bonus.daily_amount",
            amount,
        )

    # =================================================
    # PREMIUM TARIFINI BIRGALIKDA O'ZGARTIRISH
    # =================================================

    async def set_premium_tariff(
        self,
        admin_id,
        level,
        price,
        duration_days,
    ):
        if level not in self.PREMIUM_LEVELS:
            return {
                "ok": False,
                "error": "Premium darajasi noto'g'ri.",
            }

        if (
            not isinstance(price, (int, float))
            or isinstance(price, bool)
            or price < 0
        ):
            return {
                "ok": False,
                "error": "Tarif narxi noto'g'ri.",
            }

        if (
            not isinstance(duration_days, int)
            or isinstance(duration_days, bool)
            or duration_days <= 0
        ):
            return {
                "ok": False,
                "error": "Tarif muddati noto'g'ri.",
            }

        return await self.stage_change(
            admin_id,
            f"premium.tariffs.{level}",
            {
                "price": price,
                "duration_days": duration_days,
            },
        )

    # =================================================
    # SUPRA BUYRUQLARINI QABUL QILISH
    # =================================================

    async def handle_command(
        self,
        command,
        payload,
        context=None,
    ):
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": "Buyruq ma'lumotlari noto'g'ri.",
            }

        if command == "get_setting":
            path = payload.get("setting_path")

            if not isinstance(path, str):
                return {
                    "ok": False,
                    "error": "Sozlama nomi noto'g'ri.",
                }

            value = await self.get_setting(path)

            if value is None:
                return {
                    "ok": False,
                    "error": "Sozlama topilmadi.",
                }

            return {
                "ok": True,
                "value": value,
                "version": self.version,
            }

        if command == "stage_change":
            return await self.stage_change(
                admin_id=payload.get("admin_id"),
                setting_path=payload.get("setting_path"),
                new_value=payload.get("new_value"),
            )

        if command == "confirm_change":
            return await self.confirm_change(
                admin_id=payload.get("admin_id"),
                change_id=payload.get("change_id"),
            )

        if command == "reject_change":
            result = await self.reject_change(
                admin_id=payload.get("admin_id"),
                change_id=payload.get("change_id"),
            )
            return {"ok": result}

        if command == "freeze_setting":
            result = await self.freeze_setting(
                admin_id=payload.get("admin_id"),
                setting_path=payload.get("setting_path"),
                frozen=payload.get("frozen", True),
            )
            return {"ok": result}

        if command == "set_channel_group":
            return await self.set_channel_group(
                admin_id=payload.get("admin_id"),
                group_name=payload.get("group_name"),
                channels=payload.get("channels"),
            )

        if command == "set_order_limit":
            return await self.set_order_limit(
                admin_id=payload.get("admin_id"),
                category=payload.get("category"),
                limit=payload.get("limit"),
            )

        if command == "set_daily_bonus":
            return await self.set_daily_bonus(
                admin_id=payload.get("admin_id"),
                amount=payload.get("amount"),
            )

        if command == "set_premium_tariff":
            return await self.set_premium_tariff(
                admin_id=payload.get("admin_id"),
                level=payload.get("level"),
                price=payload.get("price"),
                duration_days=payload.get("duration_days"),
            )

        if command == "sync_pending":
            return await self.sync_pending(
                admin_id=payload.get("admin_id"),
            )

        return {
            "ok": False,
            "error": "Noma'lum sozlama buyrug'i.",
        }

    # =================================================
    # MODUL HOLATI
    # =================================================

    async def health_check(self):
        async with self.lock:
            pending_count = sum(
                1
                for item in self.pending_changes.values()
                if isinstance(item, dict)
                and item.get("status") == "waiting_confirmation"
            )

            distribution_count = len(
                self.pending_distribution
            )

            return {
                "module": self.MODULE_NAME,
                "version": self.VERSION,
                "initialized": self.initialized,
                "settings_version": self.version,
                "pending_changes": pending_count,
                "pending_distribution": distribution_count,
                "history_records": len(self.history),
                "frozen_settings": len(self.frozen_settings),
                "gateway_connected": self.supra_gateway is not None,
                "admin_verification_configured": callable(
                    self.is_admin_verified
                ),
                "status": (
                    "healthy"
                    if self.initialized
                    else "not_ready"
                ),
            }

# =====================================================
# MODULNI ISHGA TUSHIRISH
# =====================================================

settings_manager = None

async def setup(
    supra_gateway=None,
    is_admin_verified=None,
    notify_admin=None,
    report_security_event=None,
    data_file="sozlamalar_data.json",
):
    global settings_manager

    if settings_manager is not None:
        if settings_manager.initialized:
            return settings_manager

    manager = SettingsManager(
        supra_gateway=supra_gateway,
        is_admin_verified=is_admin_verified,
        notify_admin=notify_admin,
        report_security_event=report_security_event,
        data_file=data_file,
    )

    initialized = await manager.initialize()

    if not initialized:
        raise RuntimeError(
            "Sozlamalar modulini ishga tushirib bo'lmadi."
        )

    settings_manager = manager
    return settings_manager

async def initialize():
    if settings_manager is None:
        return False

    return await settings_manager.initialize()

async def health_check():
    if settings_manager is None:
        return {
            "module": "sozlamalar",
            "version": SettingsManager.VERSION,
            "status": "not_started",
        }

    return await settings_manager.health_check()

import asyncio
import logging
import os
import re
import uuid

from pathlib import Path
from datetime import datetime, timezone

# =====================================================
# 1. ASOSIY SOZLAMALAR
# =====================================================

VERSION = "1.0.0"

MODULE_NAME = "foydalanuvchilar"

DATA_FILE = Path("foydalanuvchilar.txt")

MAX_USERS = 1000000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(MODULE_NAME)

# =====================================================
# 2. ASOSIY HOLAT
# =====================================================

users = {}

initialized = False
running = False

user_lock = asyncio.Lock()

notify_admin_callback = None
report_security_callback = None

# =====================================================
# 3. VAQT
# =====================================================

def current_time():
    return datetime.now(
        timezone.utc
    ).isoformat()

# =====================================================
# 4. MA'LUMOTLARNI TOZALASH
# =====================================================

def clean_text(value):
    if value is None:
        return ""

    return " ".join(
        str(value).replace("\n", " ").replace("\r", " ").split()
    )

def validate_user_data(
    telegram_id,
    name,
    phone,
):
    if not telegram_id or not str(telegram_id).isdigit():
        raise ValueError(
            "Telegram ID noto'g'ri."
        )

    name = clean_text(name)

    if not name or len(name) > 150:
        raise ValueError(
            "Foydalanuvchi ismi noto'g'ri."
        )

    phone = clean_text(phone)

    phone_digits = re.sub(
        r"[\s()+\-]",
        "",
        phone,
    )

    if not phone_digits.isdigit():
        raise ValueError(
            "Telefon raqami noto'g'ri."
        )

    if not 7 <= len(phone_digits) <= 15:
        raise ValueError(
            "Telefon raqami uzunligi noto'g'ri."
        )

    return {
        "telegram_id": str(telegram_id),
        "name": name,
        "phone": phone,
    }

# =====================================================
# 5. FAYLDAN MA'LUMOTLARNI O'QISH
# =====================================================

def load_users():
    global users

    if not DATA_FILE.exists():
        users = {}
        return True

    loaded = {}

    try:
        content = DATA_FILE.read_text(
            encoding="utf-8"
        )

        blocks = re.split(
            r"(?m)^\s*(\d+)\)\s*ID:\s*",
            content,
        )

        for index in range(1, len(blocks), 2):
            telegram_id = blocks[index].strip()

            block = blocks[index + 1]

            name_match = re.search(
                r"(?m)^Nomi:\s*(.*)$",
                block,
            )

            phone_match = re.search(
                r"(?m)^Telefon:\s*(.*)$",
                block,
            )

            time_match = re.search(
                r"(?m)^Ro'yxatdan o'tgan vaqt:\s*(.*)$",
                block,
            )

            if not name_match or not phone_match:
                logger.warning(
                    "Noto'liq foydalanuvchi yozuvi: %s",
                    telegram_id,
                )
                continue

            loaded[telegram_id] = {
                "telegram_id": telegram_id,
                "name": clean_text(name_match.group(1)),
                "phone": clean_text(phone_match.group(1)),
                "registration_time": (
                    clean_text(time_match.group(1))
                    if time_match
                    else "Noma'lum"
                ),
            }

        users = loaded

        logger.info(
            "%s ta foydalanuvchi yuklandi.",
            len(users),
        )

        return True

    except Exception:
        logger.exception(
            "Foydalanuvchilar faylini o'qishda xatolik."
        )
        raise

# =====================================================
# 6. MA'LUMOTLARNI FAYLGA SAQLASH
# =====================================================

def save_users():
    temporary = DATA_FILE.with_suffix(
        DATA_FILE.suffix + ".tmp"
    )

    DATA_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        lines = []

        for index, telegram_id in enumerate(
            sorted(users, key=lambda item: int(item)),
            start=1,
        ):
            user = users[telegram_id]

            lines.extend([
                f"{index}) ID: {telegram_id}",
                f"Nomi: {user['name']}",
                f"Telefon: {user['phone']}",
                (
                    "Ro'yxatdan o'tgan vaqt: "
                    f"{user['registration_time']}"
                ),
                "",
            ])

        content = "\n".join(lines)

        with open(
            temporary,
            "w",
            encoding="utf-8",
        ) as file:

            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary,
            DATA_FILE,
        )

        # Imkon bo'lsa, fayl ruxsatlarini cheklash.
        try:
            os.chmod(DATA_FILE, 0o600)
        except OSError:
            logger.warning(
                "Fayl ruxsatlarini cheklash amalga oshmadi."
            )

    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass

# =====================================================
# 7. MODULNI ISHGA TUSHIRISH
# =====================================================

async def setup(
    bot=None,
    dp=None,
    notify_admin=None,
    report_security_event=None,
):
    global notify_admin_callback
    global report_security_callback

    notify_admin_callback = notify_admin
    report_security_callback = report_security_event

    return True

async def initialize():
    global initialized
    global running

    async with user_lock:
        load_users()

        initialized = True
        running = True

    logger.info(
        "FOYDALANUVCHILAR MODULI ISHGA TUSHDI. Versiya: %s",
        VERSION,
    )

    return True

# =====================================================
# 8. MODUL HOLATINI TEKSHIRISH
# =====================================================

async def health_check():
    return {
        "module": MODULE_NAME,
        "version": VERSION,
        "ready": initialized,
        "running": running,
        "status": "ACTIVE" if running else "STOPPED",
        "users_count": len(users),
    }

# =====================================================
# 9. YANGI FOYDALANUVCHINI RO'YXATDAN O'TKAZISH
# =====================================================

async def register_user(payload, context=None):
    if not initialized or not running:
        raise RuntimeError(
            "Foydalanuvchilar moduli tayyor emas."
        )

    if not isinstance(payload, dict):
        return {
            "registered": False,
            "message": "Ma'lumot formati noto'g'ri.",
        }

    try:
        data = validate_user_data(
            payload.get("telegram_id"),
            payload.get("name"),
            payload.get("phone"),
        )

    except ValueError as error:
        return {
            "registered": False,
            "message": str(error),
        }

    telegram_id = data["telegram_id"]

    async with user_lock:

        existing = users.get(telegram_id)

        if existing:
            return {
                "registered": True,
                "existing": True,
                "message": "Foydalanuvchi oldin ro'yxatdan o'tgan.",
            }

        if len(users) >= MAX_USERS:
            return {
                "registered": False,
                "message": "Foydalanuvchilar ombori limiti to'ldi.",
            }

        user = {
            "telegram_id": telegram_id,
            "name": data["name"],
            "phone": data["phone"],
            "registration_time": clean_text(
                payload.get("registration_time")
            ) or current_time(),
            "request_id": (
                context.get("request_id")
                if isinstance(context, dict)
                else None
            ),
        }

        users[telegram_id] = user

        try:
            save_users()

        except Exception:
            users.pop(telegram_id, None)

            logger.exception(
                "Foydalanuvchini saqlashda xatolik."
            )

            raise

    logger.info(
        "Yangi foydalanuvchi ro'yxatdan o'tdi. ID: %s",
        telegram_id,
    )

    return {
        "registered": True,
        "existing": False,
        "message": "Foydalanuvchi muvaffaqiyatli saqlandi.",
    }

# =====================================================
# 10. SUPRA BUYRUQLARINI QABUL QILISH
# =====================================================

async def handle_command(
    command,
    payload,
    context,
):
    if not initialized or not running:
        raise RuntimeError(
            "Foydalanuvchilar moduli ishlamayapti."
        )

    if command == "register_user":
        return await register_user(
            payload,
            context,
        )

    if command == "get_user":
        telegram_id = str(
            payload.get("telegram_id", "")
        )

        async with user_lock:
            user = users.get(telegram_id)

            if not user:
                return {
                    "found": False,
                    "message": "Foydalanuvchi topilmadi.",
                }

            return {
                "found": True,
                "user": dict(user),
            }

    if command == "get_user_count":
        return {
            "count": len(users),
        }

    return {
        "success": False,
        "message": "Noma'lum buyruq.",
    }

# =====================================================
# 11. FOYDALANUVCHINI QIDIRISH
# =====================================================

async def get_user(telegram_id):
    if not initialized:
        return None

    async with user_lock:
        user = users.get(
            str(telegram_id)
        )

        return dict(user) if user else None

# =====================================================
# 12. FOYDALANUVCHILAR SONI
# =====================================================

async def get_user_count():
    async with user_lock:
        return len(users)

# =====================================================
# 13. MODULNI TO'XTATISH
# =====================================================

async def shutdown():
    global running
    global initialized

    async with user_lock:
        if initialized:
            save_users()

        running = False
        initialized = False

    logger.info(
        "FOYDALANUVCHILAR MODULI TO'XTATILDI."
    )

    return True

# -*- coding: utf-8 -*-
"""
SUPRA — ADMIN MODULE
VERSION: 2.2.0

Vazifalari:
- Admin panelini boshqarish
- Admin huquqini tekshirish
- Asosiy va ichki menyularni boshqarish
- Gateway orqali buyruqlar yuborish
- Modul so'rovlarini tasdiqlash/rad etish
- Admin faoliyatini Xavfsizlik markaziga qayd etish

Klaviaturalar tugmalar.py modulida yaratiladi.
main.py o'zgartirilmaydi.
"""

import asyncio
import inspect
import logging
import uuid

from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery

# ============================================================
# 1. MODUL SOZLAMALARI
# ============================================================

MODULE_NAME = "admin"
VERSION = "2.2.0"

ADMIN_ID = 8381887071
DEFAULT_ADMIN_COMMAND = "admin"

SECURITY_MODULE_NAME = "Xavfsizlik_markazi"

router = Router(name="admin_router")

logger = logging.getLogger(__name__)

# ============================================================
# 2. GLOBAL HOLATLAR
# ============================================================

bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None

notify_admin = None
report_security_event = None
is_admin_verified = None

supra_gateway = None
security_center = None

initialized = False
router_registered = False
admin_command = DEFAULT_ADMIN_COMMAND

panel_open = False

buttons_setup_task = None
buttons_manager = None

CALLBACK_CACHE_LIMIT = 500
processed_callbacks = set()
processed_callbacks_order = deque(maxlen=CALLBACK_CACHE_LIMIT)

module_requests = {}
pending_module_requests = {}

# ============================================================
# 3. BUYRUQLAR VA MODUL YO'NALISHLARI
# ============================================================

ACTION_TARGETS = {
    "security_status": SECURITY_MODULE_NAME,
    "security_incidents": SECURITY_MODULE_NAME,
    "security_permissions": SECURITY_MODULE_NAME,
    "security_army": SECURITY_MODULE_NAME,
    "security_restrictions": SECURITY_MODULE_NAME,

    "users_all": "foydalanuvchilar",
    "users_search": "foydalanuvchilar",
    "users_premium": "foydalanuvchilar",
    "users_restrictions": "foydalanuvchilar",
    "users_permissions": "foydalanuvchilar",
}

# ============================================================
# 4. ASOSIY MENYULAR
# ============================================================

MAIN_SECTIONS = {
    "security": "Xavfsizlik",
    "settings": "Sozlamalar",
    "users": "Foydalanuvchilar",
    "orders": "Buyurtmalar",
    "bonus": "Bonuslar",
    "reports": "Hisobotlar",
    "files": "Fayllar",
    "logs": "Jurnallar",
}

SUBMENUS = {
    "security": {
        "security_status": "Xavfsizlik holati",
        "security_incidents": "Xavfsizlik hodisalari",
        "security_permissions": "Ruxsatlar",
        "security_army": "Himoya tizimi",
        "security_restrictions": "Cheklovlar",
    },

    "settings": {
        "settings_users": "Foydalanuvchilar sozlamalari",
        "settings_premium": "Premium sozlamalari",
        "settings_orders": "Buyurtma sozlamalari",
        "settings_bonus": "Bonus sozlamalari",
        "settings_channels": "Kanallar",
        "settings_buttons": "Tugmalar",
        "settings_security": "Xavfsizlik sozlamalari",
        "settings_history": "Sozlamalar tarixi",
    },

    "users": {
        "users_all": "Barcha foydalanuvchilar",
        "users_search": "Foydalanuvchini qidirish",
        "users_premium": "Premium foydalanuvchilar",
        "users_restrictions": "Foydalanuvchi cheklovlari",
        "users_permissions": "Foydalanuvchi ruxsatlari",
    },

    "orders": {
        "orders_all": "Barcha buyurtmalar",
        "orders_queue": "Navbatdagi buyurtmalar",
        "orders_active": "Faol buyurtmalar",
        "orders_completed": "Yakunlangan buyurtmalar",
        "orders_cancelled": "Bekor qilingan buyurtmalar",
    },

    "bonus": {
        "bonus_users": "Foydalanuvchi bonuslari",
        "bonus_channels": "Kanal bonuslari",
        "bonus_history": "Bonuslar tarixi",
        "bonus_statistics": "Bonus statistikasi",
    },

    "reports": {
        "reports_users": "Foydalanuvchilar hisoboti",
        "reports_orders": "Buyurtmalar hisoboti",
        "reports_payments": "To'lovlar hisoboti",
        "reports_bonus": "Bonuslar hisoboti",
        "reports_system": "Tizim hisoboti",
    },

    "files": {
        "files_status": "Fayllar holati",
        "files_connections": "Ulanishlar",
        "files_permissions": "Fayl ruxsatlari",
        "files_backup": "Zaxira nusxa",
        "files_restore": "Tiklash",
    },

    "logs": {
        "logs_system": "Tizim jurnali",
        "logs_admin": "Admin jurnali",
        "logs_security": "Xavfsizlik jurnali",
        "logs_errors": "Xatoliklar jurnali",
    },
}

EXTRA_ACTIONS = {
    "broadcast": "Xabar yuborish",
    "contact": "Aloqa",
    "feedback": "Fikr-mulohazalar",
    "payments": "To'lovlar",
    "inquiries": "Murojaatlar",
    "statistics": "Statistika",
    "panel_duration": "Panel davomiyligi",
    "close_panel": "Panelni yopish",
}

# ============================================================
# 5. YORDAMCHI FUNKSIYALAR
# ============================================================

async def call_maybe_async(function, *args, **kwargs):
    """Oddiy yoki async funksiyani chaqiradi."""
    if function is None:
        return None

    result = function(*args, **kwargs)

    if inspect.isawaitable(result):
        return await result

    return result

def utc_now() -> str:
    """UTC vaqtini ISO formatida qaytaradi."""
    return datetime.now(timezone.utc).isoformat()

def valid_admin_id(user_id: Any) -> bool:
    """Telegram ID qiymatini tekshiradi."""
    try:
        return int(user_id) == ADMIN_ID
    except (TypeError, ValueError):
        return False

async def admin_verified(user_id: Any) -> bool:
    """
    Admin ID va Xavfsizlik markazining tasdig'ini tekshiradi.
    Faqat ADMIN_ID bo'lishning o'zi yetarli emas.
    """
    if not valid_admin_id(user_id):
        return False

    if is_admin_verified is None:
        return False

    try:
        result = await call_maybe_async(
            is_admin_verified,
            int(user_id)
        )
        return bool(result)
    except Exception:
        logger.exception("Admin huquqini tekshirishda xatolik")
        return False

async def send_admin_message(text: str, **kwargs):
    """Admin ID ga xabar yuboradi."""
    if notify_admin is not None:
        try:
            return await call_maybe_async(
                notify_admin,
                text,
                **kwargs
            )
        except TypeError:
            return await call_maybe_async(notify_admin, text)

    if bot is not None:
        return await bot.send_message(ADMIN_ID, text, **kwargs)

    return None

async def security_report(event: str, details: Any = None):
    """Xavfsizlik hodisasini qayd etish."""
    if report_security_event is None:
        return False

    try:
        await call_maybe_async(
            report_security_event,
            event,
            details
        )
        return True
    except TypeError:
        try:
            await call_maybe_async(
                report_security_event,
                {
                    "module": MODULE_NAME,
                    "event": event,
                    "details": details,
                    "time": utc_now(),
                }
            )
            return True
        except Exception:
            logger.exception("Xavfsizlik hodisasini yuborib bo'lmadi")
    except Exception:
        logger.exception("Xavfsizlik hodisasini yuborib bo'lmadi")

    return False

async def record_admin_activity(action: str, details: Any = None):
    """
    Admin faoliyatini Xavfsizlik markazining OMMAVIY API
    funksiyasi orqali qayd etadi.

    Xavfsizlik markazining ichki state yoki _save_state()
    metodiga bevosita murojaat qilinmaydi.
    """
    if security_center is None:
        return False

    writer = getattr(
        security_center,
        "record_admin_activity",
        None
    )

    if not callable(writer):
        logger.warning(
            "Xavfsizlik markazida record_admin_activity() "
            "ommaviy funksiyasi mavjud emas"
        )
        return False

    try:
        result = await call_maybe_async(
            writer,
            action=action,
            details=details,
            admin_id=ADMIN_ID,
        )
        return result is not False
    except Exception:
        logger.exception("Admin faoliyatini qayd etishda xatolik")
        return False

def remember_callback(callback_id: str) -> bool:
    """Callback takroran bajarilishini cheklaydi."""
    if not callback_id:
        return False

    if callback_id in processed_callbacks:
        return False

    if len(processed_callbacks_order) >= CALLBACK_CACHE_LIMIT:
        old_id = processed_callbacks_order.popleft()
        processed_callbacks.discard(old_id)

    processed_callbacks.add(callback_id)
    processed_callbacks_order.append(callback_id)

    return True

# ============================================================
# 6. TUGMALAR MODULI BILAN INTEGRATSIYA
# ============================================================

def _buttons_module():
    """Tugmalar modulini import qiladi."""
    import tugmalar
    return tugmalar

def main_menu_keyboard():
    """Admin asosiy klaviaturasini tugmalar.py dan oladi."""
    return _buttons_module().get_admin_main_menu(
        MAIN_SECTIONS,
        EXTRA_ACTIONS
    )

def submenu_keyboard(section: str):
    """Ichki menyu klaviaturasini tugmalar.py dan oladi."""
    items = SUBMENUS.get(section)

    if items is None:
        raise ValueError(f"Noma'lum Admin bo'limi: {section}")

    return _buttons_module().get_admin_submenu(items)

def extra_menu_keyboard():
    """Qo'shimcha amallar klaviaturasi."""
    return _buttons_module().get_admin_extra_menu(
        EXTRA_ACTIONS
    )

def module_review_keyboard(token: str):
    """Modulni ko'rib chiqish klaviaturasi."""
    return _buttons_module().get_module_review_keyboard(token)

async def _authorize_buttons_source(source: str) -> bool:
    """
    Tugmalar yordamchi moduli Gateway bilan ishlaganda
    Admin orqali ruxsat olishini tekshiradi.
    """
    if source != "admin":
        return False

    return await admin_verified(ADMIN_ID)

async def _initialize_buttons_helper():
    """Tugmalar yordamchi modulini ishga tushiradi."""
    global buttons_manager

    module = _buttons_module()

    buttons_manager = await module.setup(
        bot=bot,
        dp=dp,
        supra_gateway=supra_gateway,
        is_source_authorized=_authorize_buttons_source,
        notify_admin=notify_admin,
        report_security_event=report_security_event,
    )

    return buttons_manager

def _schedule_buttons_setup():
    """Tugmalar modulini joriy event loop ichida ishga tushiradi."""
    global buttons_setup_task

    if buttons_setup_task is not None:
        if not buttons_setup_task.done():
            return buttons_setup_task

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        buttons_setup_task = None
        return None

    buttons_setup_task = loop.create_task(
        _initialize_buttons_helper()
    )

    return buttons_setup_task

# ============================================================
# 7. GATEWAY ORQALI BUYRUQ YUBORISH
# ============================================================

def get_target_for_action(
    action: str,
    data: Optional[dict] = None,
    target: Optional[str] = None
):
    """
    Buyruq uchun ruxsat etilgan modulni aniqlaydi.

    Foydalanuvchi yuborgan target qiymati ACTION_TARGETS
    xaritasini chetlab o'ta olmaydi.
    """
    if action == "admin_panel_close":
        expected_target = SECURITY_MODULE_NAME
    else:
        expected_target = ACTION_TARGETS.get(action)

    if expected_target is None:
        return None

    if target is not None and target != expected_target:
        return None

    if isinstance(data, dict):
        supplied_target = data.get("target")

        if (
            supplied_target is not None
            and supplied_target != expected_target
        ):
            return None

    return expected_target

async def send_to_supra(
    action: str,
    data: Optional[dict] = None,
    target: Optional[str] = None
):
    """Gateway orqali boshqa modulga buyruq yuboradi."""
    if supra_gateway is None:
        return {
            "ok": False,
            "error": "GATEWAY_NOT_AVAILABLE",
        }

    resolved_target = get_target_for_action(
        action,
        data=data,
        target=target,
    )

    if resolved_target is None:
        await security_report(
            "ADMIN_TARGET_REJECTED",
            {
                "action": action,
                "target": target,
            }
        )

        return {
            "ok": False,
            "error": "TARGET_NOT_CONFIGURED",
        }

    payload = data.copy() if isinstance(data, dict) else {}

    # Target foydalanuvchi ma'lumotidan olinmaydi.
    payload.pop("target", None)

    try:
        result = await call_maybe_async(
            supra_gateway.send_command,
            action,
            resolved_target,
            payload,
        )

        await record_admin_activity(
            action,
            {
                "target": resolved_target,
                "result": str(result)[:500],
            }
        )

        return result

    except Exception as exc:
        logger.exception("Gateway buyruq yuborishda xatolik")

        await security_report(
            "ADMIN_GATEWAY_ERROR",
            {
                "action": action,
                "target": resolved_target,
                "error": str(exc),
            }
        )

        return {
            "ok": False,
            "error": "GATEWAY_REQUEST_FAILED",
            "details": str(exc),
        }

# ============================================================
# 8. MODUL SO'ROVLARINI BOSHQARISH
# ============================================================

async def send_module_approval_request(
    module_name: str,
    permissions: Optional[list] = None,
    description: str = ""
):
    """Yangi modul uchun Admin tasdiqlash so'rovini yuboradi."""
    if not module_name:
        return None

    token = uuid.uuid4().hex

    request_data = {
        "token": token,
        "module_name": module_name,
        "permissions": list(permissions or []),
        "description": description,
        "created_at": utc_now(),
        "status": "PENDING",
    }

    module_requests[token] = request_data
    pending_module_requests[token] = request_data

    try:
        keyboard = module_review_keyboard(token)

        message_text = (
            "Yangi modul tasdiqlash uchun yuborildi.\n\n"
            f"Modul: {module_name}\n"
            f"Ruxsatlar: {', '.join(request_data['permissions']) or 'Ko‘rsatilmagan'}\n"
            f"Tavsif: {description or 'Mavjud emas'}\n"
            f"Token: {token}"
        )

        await send_admin_message(
            message_text,
            reply_markup=keyboard
        )

        await security_report(
            "MODULE_APPROVAL_REQUEST_RECEIVED",
            request_data
        )

        return token

    except Exception:
        logger.exception("Modul tasdiqlash so'rovini yuborib bo'lmadi")
        module_requests.pop(token, None)
        pending_module_requests.pop(token, None)
        return None

async def activate_gateway_module(
    module_name: str,
    permissions: Optional[list] = None
):
    """Tasdiqlangan modulni Gateway orqali faollashtiradi."""
    if supra_gateway is None:
        return False

    approve = getattr(
        supra_gateway,
        "approve_module",
        None
    )

    if not callable(approve):
        return False

    try:
        result = await call_maybe_async(
            approve,
            name=module_name,
            approved_capabilities=list(permissions or []),
        )
        return result is not False
    except Exception:
        logger.exception("Gateway modulni faollashtira olmadi")
        return False

async def _security_module_already_approved(
    module_name: str,
    permissions: list
) -> bool:
    """Xavfsizlik markazida modul avval tasdiqlanganini tekshiradi."""
    if security_center is None:
        return False

    verifier = getattr(
        security_center,
        "verify_module_approval",
        None
    )

    if not callable(verifier):
        return False

    try:
        return bool(
            await call_maybe_async(
                verifier,
                module_name,
                permissions
            )
        )
    except TypeError:
        try:
            return bool(
                await call_maybe_async(
                    verifier,
                    module_name=module_name,
                    capabilities=permissions,
                )
            )
        except Exception:
            logger.exception("Modul tasdig'ini tekshirishda xatolik")
            return False
    except Exception:
        logger.exception("Modul tasdig'ini tekshirishda xatolik")
        return False

async def _review_module_in_security_center(
    module_name: str,
    approved: bool,
    permissions: list
):
    """Xavfsizlik markazida modul so'rovini ko'rib chiqadi."""
    if security_center is None:
        return False

    reviewer = getattr(
        security_center,
        "review_module",
        None
    )

    if not callable(reviewer):
        return False

    # Turli interfeys nomlari bilan ishlaydigan moslashuv.
    try:
        return await call_maybe_async(
            reviewer,
            module_name=module_name,
            approved=approved,
            permissions=permissions,
            admin_id=ADMIN_ID,
        )
    except TypeError:
        pass

    try:
        return await call_maybe_async(
            reviewer,
            module_name,
            approved,
            permissions,
        )
    except TypeError:
        pass

    try:
        return await call_maybe_async(
            reviewer,
            module_name,
            approved,
        )
    except Exception:
        logger.exception("Modul so'rovini ko'rib chiqib bo'lmadi")
        return False

async def handle_module_review(
    token: str,
    approved: bool,
    admin_id: Any
):
    """
    Modul tasdiqlash/rad etish jarayoni.

    Xavfsizlik markazida avval tasdiqlangan, ammo Gateway
    faollashtirishida xato yuz bergan modul qayta ko'rib
    chiqilmaydi — Gateway faollashtirish qayta uriniladi.
    """
    if not await admin_verified(admin_id):
        await security_report(
            "UNAUTHORIZED_MODULE_REVIEW",
            {
                "admin_id": admin_id,
                "token": token,
            }
        )
        return False

    request_data = pending_module_requests.get(token)

    if request_data is None:
        return False

    module_name = request_data["module_name"]
    permissions = request_data.get("permissions", [])

    if not approved:
        result = await _review_module_in_security_center(
            module_name,
            False,
            permissions,
        )

        if result is False:
            return False

        request_data["status"] = "REJECTED"
        pending_module_requests.pop(token, None)

        await record_admin_activity(
            "MODULE_REJECTED",
            {
                "module": module_name,
                "token": token,
            }
        )

        return True

    already_approved = await _security_module_already_approved(
        module_name,
        permissions,
    )

    if not already_approved:
        review_result = await _review_module_in_security_center(
            module_name,
            True,
            permissions,
        )

        if review_result is False:
            return False

    gateway_result = await activate_gateway_module(
        module_name,
        permissions,
    )

    if not gateway_result:
        # So'rov tokeni saqlanadi. Keyingi urinishda
        # Xavfsizlik markazida tasdiq qayta bajarilmaydi.
        request_data["status"] = "APPROVED_GATEWAY_PENDING"

        await security_report(
            "MODULE_GATEWAY_ACTIVATION_FAILED",
            {
                "module": module_name,
                "token": token,
            }
        )

        return False

    request_data["status"] = "ACTIVE"
    pending_module_requests.pop(token, None)

    await record_admin_activity(
        "MODULE_APPROVED_AND_ACTIVATED",
        {
            "module": module_name,
            "permissions": permissions,
            "token": token,
        }
    )

    return True

# ============================================================
# 9. ADMIN PANELINI OCHISH VA YOPISH
# ============================================================

async def open_admin_panel(
    user_id: Any,
    message: Optional[Message] = None
):
    """Admin panelini ochadi."""
    global panel_open

    if not await admin_verified(user_id):
        await security_report(
            "UNAUTHORIZED_ADMIN_PANEL_ACCESS",
            {"user_id": user_id}
        )
        return False

    panel_open = True

    if message is not None:
        await message.answer(
            "Admin paneliga xush kelibsiz.",
            reply_markup=main_menu_keyboard()
        )

    await record_admin_activity(
        "ADMIN_PANEL_OPENED",
        {"admin_id": ADMIN_ID}
    )

    return True

async def close_admin_panel(user_id: Any):
    """Admin panelini yopadi."""
    global panel_open

    if not await admin_verified(user_id):
        return False

    panel_open = False

    result = await send_to_supra(
        "admin_panel_close",
        {
            "admin_id": ADMIN_ID,
            "time": utc_now(),
        },
        target=SECURITY_MODULE_NAME,
    )

    await record_admin_activity(
        "ADMIN_PANEL_CLOSED",
        {"admin_id": ADMIN_ID}
    )

    return result

async def _admin_callback_allowed(
    callback: CallbackQuery
) -> bool:
    """Panel callbacklari uchun yagona ruxsat tekshiruvi."""
    if callback.from_user is None:
        return False

    if not await admin_verified(callback.from_user.id):
        await callback.answer(
            "Ruxsat berilmagan.",
            show_alert=True
        )
        return False

    if not panel_open:
        await callback.answer(
            "Admin paneli yopilgan. /admin buyrug'i bilan oching.",
            show_alert=True
        )
        return False

    return True

# ============================================================
# 10. ADMIN BUYRUQ HANDLERI
# ============================================================

async def admin_command_handler(message: Message):
    """ /admin buyrug'i handleri."""
    if message.from_user is None:
        return

    await open_admin_panel(
        message.from_user.id,
        message=message
    )

# ============================================================
# 11. CALLBACK HANDLERLAR
# ============================================================

async def admin_home_handler(callback: CallbackQuery):
    """Asosiy menyuga qaytish."""
    if not await _admin_callback_allowed(callback):
        return

    await callback.message.edit_text(
        "Admin paneli",
        reply_markup=main_menu_keyboard()
    )

    await callback.answer()

async def admin_section_handler(callback: CallbackQuery):
    """Asosiy bo'limni ochish."""
    if not await _admin_callback_allowed(callback):
        return

    section = callback.data.split(":", 2)[-1]

    if section not in SUBMENUS:
        await callback.answer(
            "Bo'lim topilmadi.",
            show_alert=True
        )
        return

    await callback.message.edit_text(
        MAIN_SECTIONS.get(section, section),
        reply_markup=submenu_keyboard(section)
    )

    await callback.answer()

async def admin_action_handler(callback: CallbackQuery):
    """Ichki menyudagi amalni bajarish."""
    if not await _admin_callback_allowed(callback):
        return

    if not remember_callback(callback.id):
        await callback.answer(
            "Bu amal avval bajarilgan.",
            show_alert=True
        )
        return

    action = callback.data.split(":", 2)[-1]

    result = await send_to_supra(action)

    if isinstance(result, dict) and result.get("ok") is False:
        await callback.answer(
            "Buyruq bajarilmadi.",
            show_alert=True
        )
    else:
        await callback.answer("Buyruq yuborildi.")

    try:
        await callback.message.answer(
            f"Amal: {action}\nNatija: {str(result)[:3000]}"
        )
    except Exception:
        logger.exception("Admin javobini yuborishda xatolik")

async def admin_extra_handler(callback: CallbackQuery):
    """Qo'shimcha amallar handleri."""
    global panel_open

    if callback.from_user is None:
        return

    if not await admin_verified(callback.from_user.id):
        await callback.answer(
            "Ruxsat berilmagan.",
            show_alert=True
        )
        return

    action = callback.data.split(":", 2)[-1]

    if action == "close_panel":
        if not remember_callback(callback.id):
            await callback.answer(
                "Bu amal avval bajarilgan.",
                show_alert=True
            )
            return

        await close_admin_panel(callback.from_user.id)

        try:
            await callback.message.edit_text(
                "Admin paneli yopildi."
            )
        except Exception:
            pass

        await callback.answer("Panel yopildi.")
        return

    if not panel_open:
        await callback.answer(
            "Admin paneli yopilgan.",
            show_alert=True
        )
        return

    if not remember_callback(callback.id):
        await callback.answer(
            "Bu amal avval bajarilgan.",
            show_alert=True
        )
        return

    result = await send_to_supra(action)

    await callback.answer("Amal qabul qilindi.")

    try:
        await callback.message.answer(
            f"Qo'shimcha amal: {action}\n"
            f"Natija: {str(result)[:3000]}"
        )
    except Exception:
        logger.exception("Qo'shimcha amal javobida xatolik")

# ============================================================
# 12. MODUL TASDIQLASH CALLBACK HANDLERI
# ============================================================

async def module_review_callback_handler(
    callback: CallbackQuery
):
    """
    Tugmalar modulidan kelgan modul ko'rib chiqish callbacki.

    Kutiladigan format:
    admin:module:approve:TOKEN
    admin:module:reject:TOKEN
    """
    if callback.from_user is None:
        return

    if not await admin_verified(callback.from_user.id):
        await callback.answer(
            "Ruxsat berilmagan.",
            show_alert=True
        )
        return

    parts = callback.data.split(":")

    if len(parts) < 4:
        await callback.answer(
            "Callback formati noto'g'ri.",
            show_alert=True
        )
        return

    decision = parts[2].lower()
    token = ":".join(parts[3:])

    if decision not in ("approve", "reject"):
        await callback.answer(
            "Noma'lum qaror.",
            show_alert=True
        )
        return

    approved = decision == "approve"

    result = await handle_module_review(
        token=token,
        approved=approved,
        admin_id=callback.from_user.id,
    )

    if result:
        answer = (
            "Modul tasdiqlandi."
            if approved
            else "Modul rad etildi."
        )

        await callback.answer(answer)

        try:
            await callback.message.edit_text(answer)
        except Exception:
            pass
    else:
        await callback.answer(
            "Jarayon yakunlanmadi. So'rov saqlangan bo'lishi mumkin.",
            show_alert=True
        )

# ============================================================
# 13. ADMIN BUYRUQ NOMINI BOSHQARISH
# ============================================================

def set_admin_command(command: str) -> bool:
    """Admin buyrug'i nomini o'zgartiradi."""
    global admin_command

    if not isinstance(command, str):
        return False

    command = command.strip().lstrip("/")

    if (
        not command
        or not command.replace("_", "").isalnum()
    ):
        return False

    admin_command = command
    return True

def get_admin_command() -> str:
    return admin_command

def get_panel_status() -> bool:
    return panel_open

# ============================================================
# 14. SETUP
# ============================================================

def setup(
    bot_instance: Bot,
    dp_instance: Dispatcher,
    notify_admin_function,
    report_security_event_function=None,
    is_admin_verified_function=None,
    supra_gateway_instance=None,
    security_center_instance=None,
):
    """
    Admin modulini ulaydi.

    Setup sync saqlangan: Xavfsizlik markazining mavjud
    setup() chaqiruviga mos keladi.
    """
    global bot, dp
    global notify_admin, report_security_event
    global is_admin_verified, supra_gateway
    global security_center, router_registered

    bot = bot_instance
    dp = dp_instance

    notify_admin = notify_admin_function
    report_security_event = report_security_event_function
    is_admin_verified = is_admin_verified_function

    supra_gateway = supra_gateway_instance
    security_center = security_center_instance

    if not router_registered:
        dp.include_router(router)
        router_registered = True

    _schedule_buttons_setup()

    return True

# ============================================================
# 15. INITIALIZE
# ============================================================

async def initialize():
    """Admin modulini to'liq ishga tayyorlaydi."""
    global initialized, buttons_setup_task, buttons_manager

    if bot is None or dp is None:
        raise RuntimeError(
            "Admin setup() chaqirilmagan: bot yoki dp mavjud emas."
        )

    if security_center is None:
        raise RuntimeError(
            "Xavfsizlik markazi Admin moduliga ulanmagan."
        )

    if supra_gateway is None:
        raise RuntimeError(
            "SUPRA Gateway Admin moduliga ulanmagan."
        )

    if is_admin_verified is None:
        raise RuntimeError(
            "Admin tekshiruv funksiyasi ulanmagan."
        )

    if buttons_setup_task is None:
        buttons_setup_task = asyncio.create_task(
            _initialize_buttons_helper()
        )

    try:
        buttons_manager = await buttons_setup_task
    except Exception as exc:
        logger.exception("Tugmalar modulini ishga tushirib bo'lmadi")
        raise RuntimeError(
            "tugmalar.py yordamchi moduli ishga tushmadi."
        ) from exc

    if buttons_manager is None:
        raise RuntimeError(
            "Tugmalar moduli manager obyektini qaytarmadi."
        )

    initialized = True

    await record_admin_activity(
        "ADMIN_MODULE_INITIALIZED",
        {
            "version": VERSION,
            "time": utc_now(),
        }
    )

    logger.info(
        "Admin moduli VERSION %s ishga tushdi",
        VERSION
    )

    return True

# ============================================================
# 16. ROUTER HANDLERLARINI RO'YXATGA OLISH
# ============================================================

router.message.register(
    admin_command_handler,
    F.text.regexp(r"^/admin(?:@\w+)?$")
)

router.callback_query.register(
    admin_home_handler,
    F.data == "admin:home"
)

router.callback_query.register(
    admin_section_handler,
    F.data.startswith("admin:section:")
)

router.callback_query.register(
    admin_action_handler,
    F.data.startswith("admin:action:")
)

router.callback_query.register(
    admin_extra_handler,
    F.data.startswith("admin:extra:")
)

router.callback_query.register(
    module_review_callback_handler,
    F.data.startswith("admin:module:")
)

# ============================================================
# 17. HEALTH CHECK
# ============================================================

async def health_check():
    """Modul va integratsiyalar holatini qaytaradi."""
    buttons_ready = buttons_manager is not None

    if buttons_setup_task is not None:
        if buttons_setup_task.done():
            try:
                buttons_ready = (
                    buttons_setup_task.exception() is None
                    and buttons_manager is not None
                )
            except asyncio.CancelledError:
                buttons_ready = False

    return {
        "module": MODULE_NAME,
        "version": VERSION,
        "initialized": initialized,
        "router_registered": router_registered,
        "admin_id": ADMIN_ID,
        "panel_open": panel_open,
        "gateway_connected": supra_gateway is not None,
        "security_center_connected": security_center is not None,
        "admin_verification_connected": (
            is_admin_verified is not None
        ),
        "buttons_helper_ready": buttons_ready,
        "pending_module_requests": len(
            pending_module_requests
        ),
        "status": (
            "HEALTHY"
            if (
                initialized
                and router_registered
                and buttons_ready
            )
            else "NOT_READY"
        ),
    }

# ============================================================
# 18. TASHQI BOSHQARUV FUNKSIYALARI
# ============================================================

async def open_panel_for_admin(user_id: Any):
    """Tashqi moduldan Admin panelini ochish."""
    return await open_admin_panel(user_id)

async def send_admin_command(
    action: str,
    data: Optional[dict] = None,
    target: Optional[str] = None
):
    """Tashqi moduldan Admin nomidan buyruq yuborish."""
    if not initialized:
        return {
            "ok": False,
            "error": "ADMIN_NOT_INITIALIZED",
        }

    return await send_to_supra(
        action=action,
        data=data,
        target=target,
    )

def get_main_sections():
    return MAIN_SECTIONS.copy()

def get_submenus():
    return {
        key: value.copy()
        for key, value in SUBMENUS.items()
    }

def get_extra_actions():
    return EXTRA_ACTIONS.copy()

def get_status():
    """Sinxron holat ma'lumotlari."""
    return {
        "module": MODULE_NAME,
        "version": VERSION,
        "initialized": initialized,
        "router_registered": router_registered,
        "panel_open": panel_open,
        "admin_id": ADMIN_ID,
        "gateway_connected": supra_gateway is not None,
        "security_center_connected": security_center is not None,
        "buttons_manager_ready": buttons_manager is not None,
        "pending_requests": len(pending_module_requests),
    }

# ============================================================
# 19. MODUL MA'LUMOTI
# ============================================================

def get_module_info():
    return {
        "name": MODULE_NAME,
        "version": VERSION,
        "description": "SUPRA Admin boshqaruv moduli",
        "admin_id": ADMIN_ID,
        "main_sections": list(MAIN_SECTIONS.keys()),
        "extra_actions": list(EXTRA_ACTIONS.keys()),
        "helper_module": "tugmalar",
        "security_module": SECURITY_MODULE_NAME,
    }

import asyncio
import copy
import importlib
import inspect
import json
import logging
import os
import tempfile
import threading
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ============================================================
# 1. ASOSIY SOZLAMALAR
# ============================================================

MODULE_NAME = "Xavfsizlik_markazi"
VERSION = "2.1.0"

ADMIN_ID = 8381887071

DATA_FILE = Path(__file__).with_name(
    "xavfsizlik_markazi_data.json"
)

MAX_CHANNELS_PER_GROUP = 10
MAX_HISTORY_ITEMS = 5000

CHANNEL_GROUPS = (
    "registration",
    "daily_bonus",
    "bonus",
)

DEFAULT_DAILY_ORDER_LIMIT = 2

logger = logging.getLogger("SUPRA.SecurityCenter")

# ============================================================
# 2. BOSHLANG'ICH MA'LUMOTLAR
# ============================================================

def create_default_state():
    return {
        "version": 2,
        "modules": {},
        "incidents": [],
        "permissions": {},
        "channel_groups": {
            "registration": [],
            "daily_bonus": [],
            "bonus": [],
        },
        "users": {},
        "army_events": [],
        "admin_history": [],
        "system_status": "INITIALIZING",
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
    }

# ============================================================
# 3. XAVFSIZLIK MARKAZI
# ============================================================

class SecurityCenter:

    def __init__(self):

        self.state = create_default_state()

        self.bot = None
        self.dp = None

        self.notify_admin = None
        self.report_security_event = None

        self.admin_module = None
        self.supra_gateway = None

        self.initialized = False
        self.admin_connected = False

        self.lock = threading.RLock()

        self.pending_notifications = set()
        self._notification_lock = threading.Lock()

    # ========================================================
    # 4. YORDAMCHI FUNKSIYALAR
    # ========================================================

    @staticmethod
    def _now():
        return datetime.now(
            timezone.utc
        ).isoformat()

    @staticmethod
    def _normalize_permissions(permissions):

        if permissions is None:
            return []

        if not isinstance(permissions, (list, tuple, set)):
            raise TypeError(
                "Ruxsatlar ro'yxat ko'rinishida bo'lishi kerak."
            )

        result = []

        for permission in permissions:

            if not isinstance(permission, str):
                raise TypeError(
                    "Ruxsat nomlari matn bo'lishi kerak."
                )

            permission = permission.strip()

            if permission and permission not in result:
                result.append(permission)

        return result

    @staticmethod
    def _trim_history(items):

        if isinstance(items, list) and len(items) > MAX_HISTORY_ITEMS:
            del items[:-MAX_HISTORY_ITEMS]

    def _add_admin_history(self, item):

        history = self.state.setdefault(
            "admin_history",
            [],
        )

        history.append(item)
        self._trim_history(history)

    # ========================================================
    # 5. MAIN.PY BILAN ALOQA
    # ========================================================

    def setup(
        self,
        bot,
        dp,
        notify_admin,
        report_security_event=None,
    ):

        if bot is None or dp is None:
            raise ValueError(
                "Bot va Dispatcher talab qilinadi."
            )

        if not callable(notify_admin):
            raise TypeError(
                "notify_admin funksiyasi noto'g'ri."
            )

        self.bot = bot
        self.dp = dp
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event

        # SUPRA singleton bilan aloqa.
        try:

            supra_module = importlib.import_module(
                "supra_2jz_gte"
            )

            self.supra_gateway = getattr(
                supra_module,
                "supra_gateway",
                None,
            )

        except Exception:

            logger.exception(
                "SUPRA gateway bilan aloqa o'rnatilmadi."
            )

        # Admin modulini ulash.
        # main.py o'zgartirilmaydi.
        try:

            self.admin_module = importlib.import_module(
                "admin"
            )

            admin_setup = getattr(
                self.admin_module,
                "setup",
                None,
            )

            if callable(admin_setup):

                result = admin_setup(
                    bot_instance=self.bot,
                    dp_instance=self.dp,
                    notify_admin_function=self.notify_admin,
                    report_security_event_function=(
                        self.report_security_event
                    ),
                    is_admin_verified_function=(
                        self.is_admin_verified
                    ),
                    supra_gateway_instance=(
                        self.supra_gateway
                    ),
                    security_center_instance=self,
                )

                if inspect.isawaitable(result):

                    try:
                        loop = asyncio.get_running_loop()

                        loop.create_task(
                            self._finish_admin_setup(result)
                        )

                    except RuntimeError:
                        logger.error(
                            "Admin setup uchun asyncio loop topilmadi."
                        )

                else:
                    self.admin_connected = result is not False

            else:
                logger.warning(
                    "admin.py ichida setup funksiyasi topilmadi."
                )

        except Exception:

            logger.exception(
                "Admin modulini avtomatik ulashda xato."
            )

        logger.info(
            "Xavfsizlik markazi main.py bilan bog'landi."
        )

        return True

    async def _finish_admin_setup(self, awaitable):

        try:

            result = await awaitable
            self.admin_connected = result is not False

        except Exception:

            self.admin_connected = False

            logger.exception(
                "Admin modulini ishga tushirishda xato."
            )

    # ========================================================
    # 6. ADMIN SHAXSINI TEKSHIRISH
    # ========================================================

    def is_admin_verified(self, user_id):

        try:

            if isinstance(user_id, bool):
                return False

            return int(user_id) == ADMIN_ID

        except (TypeError, ValueError):
            return False

    def _require_admin(self, user_id):

        if not self.is_admin_verified(user_id):

            logger.warning(
                "Ruxsatsiz Admin amali. Telegram ID: %s",
                user_id,
            )

            raise PermissionError(
                "Bu amal faqat haqiqiy Admin uchun ruxsat etilgan."
            )

        return True

    # ========================================================
    # 7. MA'LUMOTLARNI SAQLASH
    # ========================================================

    def _load_state(self):

        if not DATA_FILE.exists():

            self.state = create_default_state()
            self._save_state()

            return

        try:

            with open(
                DATA_FILE,
                "r",
                encoding="utf-8",
            ) as file:

                loaded = json.load(file)

            if not isinstance(loaded, dict):
                raise ValueError(
                    "Xavfsizlik ma'lumotlari noto'g'ri formatda."
                )

            defaults = create_default_state()

            for key, value in defaults.items():

                if key not in loaded:
                    loaded[key] = copy.deepcopy(value)

            expected_types = {
                "modules": dict,
                "incidents": list,
                "permissions": dict,
                "channel_groups": dict,
                "users": dict,
                "army_events": list,
                "admin_history": list,
            }

            for key, expected_type in expected_types.items():

                if not isinstance(loaded.get(key), expected_type):
                    raise ValueError(
                        f"{key} ma'lumotlari noto'g'ri formatda."
                    )

            # Kanal guruhlarining eski ma'lumotlarini saqlash.
            for group_name in CHANNEL_GROUPS:

                if group_name not in loaded["channel_groups"]:
                    loaded["channel_groups"][group_name] = []

                if not isinstance(
                    loaded["channel_groups"][group_name],
                    list,
                ):
                    raise ValueError(
                        f"{group_name} kanal guruhi noto'g'ri."
                    )

            # Modul yozuvlarini tekshirish.
            for module_name, module in loaded["modules"].items():

                if not isinstance(module, dict):
                    raise ValueError(
                        f"{module_name} modul ma'lumoti noto'g'ri."
                    )

                module.setdefault("name", module_name)
                module.setdefault("functions", [])
                module.setdefault("requested_permissions", [])
                module.setdefault("approved_permissions", [])
                module.setdefault("status", "PENDING")
                module.setdefault("notification_sent", False)

            loaded["version"] = 2

            self.state = loaded

            # Eski fayldagi maydonlarni yangi shaklga saqlash.
            self._save_state()

        except Exception as error:

            logger.critical(
                "Xavfsizlik ma'lumotlarini o'qib bo'lmadi: %s",
                error,
            )

            raise RuntimeError(
                "Xavfsizlik ma'lumotlari buzilgan. "
                "Avtomatik qayta yozish amalga oshirilmadi."
            ) from error

    def _save_state(self):

        DATA_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = None

        try:

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=DATA_FILE.parent,
                delete=False,
                suffix=".tmp",
            ) as file:

                json.dump(
                    self.state,
                    file,
                    ensure_ascii=False,
                    indent=4,
                )

                file.flush()
                os.fsync(file.fileno())

                temporary_path = file.name

            os.replace(
                temporary_path,
                DATA_FILE,
            )

        except Exception:

            if (
                temporary_path
                and os.path.exists(temporary_path)
            ):

                try:
                    os.remove(temporary_path)
                except OSError:
                    pass

            raise

    # ========================================================
    # 8. ISHGA TUSHIRISH
    # ========================================================

    def initialize(self):

        with self.lock:

            self._load_state()

            self.state["system_status"] = "ACTIVE"

            self._save_state()

            self.initialized = True

        logger.info(
            "Xavfsizlik markazi ishga tushdi."
        )

        return True

    def health_check(self):

        with self.lock:

            return {
                "ready": self.initialized,
                "status": self.state.get(
                    "system_status",
                    "UNKNOWN",
                ),
                "module": MODULE_NAME,
                "version": VERSION,
                "admin_id": ADMIN_ID,
                "admin_connected": self.admin_connected,
            }

    # ========================================================
    # 9. ADMIN XABARLARI
    # ========================================================

    async def _notify(self, message):

        if self.notify_admin is None:
            return False

        try:

            result = self.notify_admin(message)

            if inspect.isawaitable(result):
                result = await result

            return result is not False

        except Exception:

            logger.exception(
                "Adminga xabar yuborilmadi."
            )

            return False

    async def _report_to_main(self, event):

        """
        Main.py ga hodisa dict shaklida yuboriladi.

        Eslatma:
        report_incident() ichida bu funksiya ataylab
        avtomatik chaqirilmaydi. Chunki main.py hodisani
        Security Center ga qayta yuborishi mumkin.
        Bu esa cheksiz takrorlanish xavfini tug'diradi.
        """

        if not callable(self.report_security_event):
            return False

        try:

            result = self.report_security_event(event)

            if inspect.isawaitable(result):
                result = await result

            return result is not False

        except Exception:

            logger.exception(
                "main.py ga xavfsizlik hodisasi yuborilmadi."
            )

            return False

    # ========================================================
    # 10. HODISALARNI QAYD ETISH
    # ========================================================

    async def report_incident(self, event):

        if not self.initialized:
            raise RuntimeError(
                "Xavfsizlik markazi hali ishga tushmagan."
            )

        if not isinstance(event, dict):
            raise TypeError(
                "Hodisa ma'lumoti dictionary bo'lishi kerak."
            )

        incident = {
            "id": str(uuid.uuid4()),
            "time": self._now(),
            "event": copy.deepcopy(event),
            "status": "RECORDED",
            "admin_notified": False,
        }

        with self.lock:

            self.state["incidents"].append(incident)

            self._trim_history(
                self.state["incidents"]
            )

            self._save_state()

        source = event.get(
            "source",
            "Noma'lum",
        )

        event_type = event.get(
            "type",
            event.get("event_type", "Noma'lum"),
        )

        description = event.get(
            "description",
            "Tavsif berilmagan",
        )

        title = event.get(
            "title",
            "XAVFSIZLIK OGOHLANTIRISHI",
        )

        message = (
            f"{title}\n\n"
            f"Hodisa ID: {incident['id']}\n"
            f"Manba: {source}\n"
            f"Turi: {event_type}\n"
            f"Tavsif: {description}\n"
            f"Vaqt: {incident['time']}\n"
            "Holat: QAYD ETILDI"
        )

        delivered = await self._notify(message)

        with self.lock:

            incident["admin_notified"] = delivered

            incident["status"] = (
                "NOTIFIED"
                if delivered
                else "NOTIFICATION_FAILED"
            )

            self._save_state()

        # Bu yerda main.py ga qayta hodisa yuborilmaydi.
        # Aks holda main -> security -> main takrorlanishi mumkin.

        return True

    def get_incidents(self, limit=50):

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError(
                "Limit musbat butun son bo'lishi kerak."
            )

        with self.lock:

            return copy.deepcopy(
                self.state["incidents"][-limit:]
            )

    # ========================================================
    # 11. MODULNI KUTILAYOTGAN HOLATGA O'TKAZISH
    # ========================================================

    def _ensure_pending_module(
        self,
        module_name,
        functions=None,
        requested_permissions=None,
    ):

        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError(
                "Modul nomi ko'rsatilishi kerak."
            )

        module_name = module_name.strip()

        permissions = self._normalize_permissions(
            requested_permissions
        )

        functions = self._normalize_permissions(
            functions
        )

        with self.lock:

            module = self.state["modules"].get(
                module_name
            )

            if module is not None:

                # Rad etilgan modul avtomatik qayta ochilmaydi.
                if module.get("status") == "DENIED":
                    return module, False

                # Tasdiqlangan modulning ruxsatlari avtomatik
                # kengaytirilmaydi.
                if module.get("status") == "APPROVED":

                    existing_functions = set(
                        module.get("functions", [])
                    )

                    existing_permissions = set(
                        module.get("requested_permissions", [])
                    )

                    if (
                        set(functions).issubset(existing_functions)
                        and set(permissions).issubset(
                            existing_permissions
                        )
                    ):
                        return module, False

                    return module, False

                module["functions"] = functions
                module["requested_permissions"] = permissions

                self._save_state()

                return module, False

            record = {
                "name": module_name,
                "functions": functions,
                "requested_permissions": permissions,
                "approved_permissions": [],
                "status": "PENDING",
                "registered_at": self._now(),
                "reviewed_by": None,
                "reviewed_at": None,
                "notification_sent": False,
            }

            self.state["modules"][module_name] = record

            self._save_state()

            return record, True

    # ========================================================
    # 12. ADMIN TASDIQLASH SO'ROVINI YUBORISH
    # ========================================================

    async def _send_module_review_request(self, module_name):

        if self.admin_module is None:
            logger.error(
                "Admin moduli mavjud emas."
            )
            return False

        send_function = getattr(
            self.admin_module,
            "send_module_approval_request",
            None,
        )

        if not callable(send_function):
            logger.error(
                "admin.py ichida send_module_approval_request topilmadi."
            )
            return False

        with self.lock:

            module = self.state["modules"].get(
                module_name
            )

            if module is None:
                return False

            if module.get("status") != "PENDING":
                return False

            functions = list(
                module.get("functions", [])
            )

            permissions = list(
                module.get("requested_permissions", [])
            )

        try:

            result = send_function(
                module_name=module_name,
                functions=functions,
                requested_permissions=permissions,
            )

            if inspect.isawaitable(result):
                result = await result

            with self.lock:

                module = self.state["modules"].get(
                    module_name
                )

                if module is not None:

                    module["notification_sent"] = (
                        result is not False
                    )

                    module["notification_time"] = self._now()

                    self._save_state()

            return result is not False

        except Exception:

            logger.exception(
                "Modulni Admin tasdig'iga yuborishda xato."
            )

            return False

    def _schedule_module_review(self, module_name):

        with self._notification_lock:

            if module_name in self.pending_notifications:
                return False

            self.pending_notifications.add(
                module_name
            )

        try:

            loop = asyncio.get_running_loop()

        except RuntimeError:

            with self._notification_lock:
                self.pending_notifications.discard(
                    module_name
                )

            logger.warning(
                "Modul tasdiqlash xabari uchun asyncio loop mavjud emas."
            )

            return False

        async def runner():

            try:

                await self._send_module_review_request(
                    module_name
                )

            finally:

                with self._notification_lock:
                    self.pending_notifications.discard(
                        module_name
                    )

        loop.create_task(runner())

        return True

    # ========================================================
    # 13. YANGI MODULLARNI RO'YXATDAN O'TKAZISH
    # ========================================================

    def register_module(
        self,
        module_name,
        functions=None,
        requested_permissions=None,
    ):

        if not self.initialized:
            raise RuntimeError(
                "Xavfsizlik markazi ishga tushmagan."
            )

        module, created = self._ensure_pending_module(
            module_name,
            functions,
            requested_permissions,
        )

        status = module.get("status")

        if status == "APPROVED":

            return {
                "success": True,
                "status": "APPROVED",
                "module": module_name,
            }

        if status == "DENIED":

            return {
                "success": False,
                "status": "DENIED",
                "module": module_name,
                "message": (
                    "Modul avval Admin tomonidan rad etilgan."
                ),
            }

        if created or not module.get("notification_sent"):

            self._schedule_module_review(
                module_name
            )

        return {
            "success": True,
            "status": "PENDING",
            "module": module_name,
            "message": (
                "Admin tasdig'i kutilmoqda. "
                "Tasdiqlanmaguncha modul faol bo'lmaydi."
            ),
        }

    # ========================================================
    # 14. SUPRA UCHUN TASDIQLASH TEKSHIRUVI
    # ========================================================

    def verify_module_approval(
        self,
        module_name,
        capabilities=None,
    ):

        if not self.initialized:
            return False

        if not isinstance(module_name, str) or not module_name.strip():
            return False

        module_name = module_name.strip()

        try:

            requested = self._normalize_permissions(
                capabilities
            )

        except (TypeError, ValueError):
            return False

        with self.lock:

            module = self.state["modules"].get(
                module_name
            )

            if module is None:

                module, created = self._ensure_pending_module(
                    module_name,
                    functions=requested,
                    requested_permissions=requested,
                )

            status = module.get("status")

            approved_permissions = self.state[
                "permissions"
            ].get(module_name, [])

            requested_permissions = module.get(
                "requested_permissions",
                [],
            )

            module_functions = module.get(
                "functions",
                [],
            )

        if status != "APPROVED":

            if status == "PENDING":
                self._schedule_module_review(
                    module_name
                )

            return False

        if not isinstance(approved_permissions, list):
            return False

        approved_set = set(approved_permissions)

        if "*" in approved_set:
            return True

        # Gateway capabilities ham Admin tasdig'ida bo'lishi kerak.
        if not set(requested).issubset(approved_set):
            return False

        # Modul so'ragan ruxsatlar ham tasdiqlangan bo'lishi kerak.
        if not set(requested_permissions).issubset(approved_set):
            return False

        # Modul funksiyalari ham tekshiriladi.
        if not set(module_functions).issubset(approved_set):
            return False

        return True

    # ========================================================
    # 15. MODULNI TASDIQLASH YOKI RAD ETISH
    # ========================================================

    def review_module(
        self,
        module_name,
        approved,
        permissions=None,
        reviewed_by=None,
    ):

        self._require_admin(reviewed_by)

        if not isinstance(approved, bool):
            raise TypeError(
                "Tasdiqlash qiymati True yoki False bo'lishi kerak."
            )

        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError(
                "Modul nomi noto'g'ri."
            )

        module_name = module_name.strip()

        if approved:
            approved_permissions = self._normalize_permissions(
                permissions
            )
        else:
            approved_permissions = []

        with self.lock:

            module = self.state["modules"].get(
                module_name
            )

            if module is None:
                raise ValueError(
                    "Modul ro'yxatdan topilmadi."
                )

            if module.get("status") != "PENDING":

                return {
                    "success": False,
                    "status": module.get("status"),
                    "message": "Modul avval ko'rib chiqilgan.",
                }

            # Admin faqat modul so'ragan imkoniyatlarni tasdiqlashi mumkin.
            allowed_to_approve = set(
                module.get("functions", [])
            ) | set(
                module.get("requested_permissions", [])
            )

            if approved and "*" not in approved_permissions:

                if not set(approved_permissions).issubset(
                    allowed_to_approve
                ):
                    raise PermissionError(
                        "Admin modul so'ramagan ruxsatni bera olmaydi."
                    )

            old_module = copy.deepcopy(module)
            old_permissions = copy.deepcopy(
                self.state["permissions"].get(module_name)
            )

            if approved:

                module["status"] = "APPROVED"

                module["approved_permissions"] = (
                    approved_permissions
                )

                self.state["permissions"][
                    module_name
                ] = approved_permissions

            else:

                module["status"] = "DENIED"

                module["approved_permissions"] = []

                self.state["permissions"].pop(
                    module_name,
                    None,
                )

            module["reviewed_by"] = int(reviewed_by)
            module["reviewed_at"] = self._now()

            self._add_admin_history({
                "type": "MODULE_REVIEW",
                "module": module_name,
                "approved": approved,
                "permissions": approved_permissions,
                "reviewed_by": int(reviewed_by),
                "time": self._now(),
            })

            try:
                self._save_state()

            except Exception:

                self.state["modules"][module_name] = old_module

                if old_permissions is None:
                    self.state["permissions"].pop(
                        module_name,
                        None,
                    )
                else:
                    self.state["permissions"][
                        module_name
                    ] = old_permissions

                raise

            final_status = module["status"]

        logger.info(
            "Modul ko'rib chiqildi: %s, holat: %s",
            module_name,
            final_status,
        )

        return {
            "success": True,
            "status": final_status,
            "module": module_name,
            "permissions": approved_permissions,
        }

    # ========================================================
    # 16. ADMIN FAOLIYATI TARIXI
    # ========================================================

    def get_admin_history(self, limit=50):

        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
        ):
            raise ValueError(
                "Limit musbat butun son bo'lishi kerak."
            )

        with self.lock:

            return copy.deepcopy(
                self.state["admin_history"][-limit:]
            )

    # ========================================================
    # 17. AMALLARGA RUXSATNI TEKSHIRISH
    # ========================================================

    def authorize_action(
        self,
        module_name,
        action_name,
    ):

        with self.lock:

            module = self.state["modules"].get(
                module_name
            )

            if module is None:
                return False

            if module.get("status") != "APPROVED":
                return False

            allowed_permissions = self.state[
                "permissions"
            ].get(module_name, [])

            return (
                "*" in allowed_permissions
                or action_name in allowed_permissions
            )

    # ========================================================
    # 18. KANALLAR GURUHLARINI BOSHQARISH
    # ========================================================

    def set_channel_group(
        self,
        group_name,
        channels,
    ):

        if group_name not in CHANNEL_GROUPS:
            raise ValueError(
                "Bunday kanal guruhi mavjud emas."
            )

        if not isinstance(channels, list):
            raise TypeError(
                "Kanallar ro'yxat ko'rinishida berilishi kerak."
            )

        if len(channels) > MAX_CHANNELS_PER_GROUP:
            raise ValueError(
                "Bir guruhda ko'pi bilan 10 ta kanal bo'lishi mumkin."
            )

        normalized = []

        for channel in channels:

            if isinstance(channel, bool):
                raise ValueError(
                    "Kanal ID yoki username bo'lishi kerak."
                )

            if not isinstance(channel, (str, int)):
                raise ValueError(
                    "Kanal ID yoki username bo'lishi kerak."
                )

            channel = str(channel).strip()

            if not channel:
                raise ValueError(
                    "Bo'sh kanal qiymati qabul qilinmaydi."
                )

            if channel not in normalized:
                normalized.append(channel)

        with self.lock:

            old_value = copy.deepcopy(
                self.state["channel_groups"].get(group_name, [])
            )

            self.state["channel_groups"][
                group_name
            ] = normalized

            try:
                self._save_state()
            except Exception:
                self.state["channel_groups"][group_name] = old_value
                raise

        return True

    def get_channel_group(self, group_name):

        if group_name not in CHANNEL_GROUPS:
            raise ValueError(
                "Noto'g'ri kanal guruhi."
            )

        with self.lock:

            return copy.deepcopy(
                self.state["channel_groups"][group_name]
            )

    # ========================================================
    # 19. FOYDALANUVCHI QOIDABUZARLIKLARI
    # ========================================================

    def _get_user_record(self, user_id):

        user_id = str(user_id)

        if user_id not in self.state["users"]:

            self.state["users"][user_id] = {
                "violations": 0,
                "blocked": False,
                "permanent_block": False,
                "spam_restricted_until": 0,
                "daily_order_limit_override": None,
                "bonus_balance_override": None,
                "history": [],
            }

        return self.state["users"][user_id]

    async def record_violation(
        self,
        user_id,
        reason,
    ):

        with self.lock:

            user = self._get_user_record(user_id)

            if user["permanent_block"]:

                return {
                    "success": False,
                    "status": "PERMANENT_BLOCK",
                }

            user["violations"] += 1

            violation_number = user["violations"]
            event_time = self._now()

            if violation_number == 1:

                user["spam_restricted_until"] = (
                    time.time() + 86400
                )

                user["daily_order_limit_override"] = 0
                user["bonus_balance_override"] = 0

                action = "FIRST_VIOLATION_24H_RESTRICTION"

                user_message = (
                    "Siz qoidalarni buzdingiz. "
                    "Limitlaringiz va bonuslaringiz 0 ga tushdi. "
                    "24 soatdan keyin sizga limit beriladi. "
                    "Iltimos, buni boshqa takrorlamang!"
                )

            elif violation_number == 2:

                user["blocked"] = True

                action = "SECOND_VIOLATION_BLOCK"

                user_message = (
                    "Hisobingiz bloklandi. "
                    "Admin bilan bog'laning."
                )

            else:

                user["blocked"] = True
                user["permanent_block"] = True

                action = "PERMANENT_BLOCK"

                user_message = (
                    "Hisobingiz doimiy bloklandi. "
                    "Admin bilan bog'laning."
                )

            history_item = {
                "violation": violation_number,
                "reason": str(reason),
                "time": event_time,
                "action": action,
            }

            user["history"].append(history_item)

            try:
                self._save_state()
            except Exception:
                user["violations"] -= 1
                user["history"].pop()
                raise

        await self._notify(
            "FOYDALANUVCHI QOIDABUZARLIGI\n\n"
            f"Telegram ID: {user_id}\n"
            f"Vaqt: {event_time}\n"
            f"Sabab: {reason}\n"
            f"Qoidabuzarlik: {violation_number}\n"
            f"Chora: {action}"
        )

        return {
            "success": True,
            "violation": violation_number,
            "action": action,
            "user_message": user_message,
        }

    # ========================================================
    # 20. FOYDALANUVCHI CHEKLOVLARINI TEKSHIRISH
    # ========================================================

    def get_user_restrictions(self, user_id):

        with self.lock:

            user = self._get_user_record(user_id)
            now = time.time()

            if (
                user["spam_restricted_until"] > 0
                and now >= user["spam_restricted_until"]
            ):

                old_until = user["spam_restricted_until"]
                old_limit = user["daily_order_limit_override"]

                user["spam_restricted_until"] = 0
                user["daily_order_limit_override"] = None

                # BONUS 0 holatida qoladi.
                try:
                    self._save_state()
                except Exception:
                    user["spam_restricted_until"] = old_until
                    user["daily_order_limit_override"] = old_limit
                    raise

            return {
                "blocked": user["blocked"],
                "permanent_block": user["permanent_block"],
                "spam_restricted": (
                    user["spam_restricted_until"] > now
                ),
                "spam_restricted_until": (
                    user["spam_restricted_until"]
                ),
                "daily_order_limit_override": (
                    user["daily_order_limit_override"]
                ),
                "bonus_balance_override": (
                    user["bonus_balance_override"]
                ),
                "violations": user["violations"],
            }

    # ========================================================
    # 21. ADMIN FOYDALANUVCHINI BLOKDAN CHIQARISHI
    # ========================================================

    def admin_unblock_user(
        self,
        user_id,
        reviewed_by=None,
    ):

        self._require_admin(reviewed_by)

        with self.lock:

            user = self._get_user_record(user_id)

            if user["permanent_block"]:

                return {
                    "success": False,
                    "reason": "PERMANENT_BLOCK",
                }

            old_blocked = user["blocked"]

            user["blocked"] = False

            user["history"].append({
                "action": "ADMIN_UNBLOCK",
                "reviewed_by": int(reviewed_by),
                "time": self._now(),
            })

            try:
                self._save_state()
            except Exception:
                user["blocked"] = old_blocked
                user["history"].pop()
                raise

            self._add_admin_history({
                "type": "USER_UNBLOCK",
                "user_id": str(user_id),
                "reviewed_by": int(reviewed_by),
                "time": self._now(),
            })

            self._save_state()

        return {
            "success": True,
            "status": "UNBLOCKED",
        }

    # ========================================================
    # 22. ARMIYA TIZIMI
    # ========================================================

    async def army_report(
        self,
        event_type,
        file_name,
        description,
    ):

        event = {
            "id": str(uuid.uuid4()),
            "event_type": str(event_type),
            "file_name": str(file_name),
            "description": str(description),
            "time": self._now(),
        }

        with self.lock:

            self.state["army_events"].append(
                event
            )

            self._trim_history(
                self.state["army_events"]
            )

            self._save_state()

        await self.report_incident({
            "source": "ARMY",
            "event_type": event_type,
            "file": file_name,
            "description": description,
        })

        return True

    # ========================================================
    # 23. TIZIM HOLATI
    # ========================================================

    def get_status(self):

        with self.lock:

            modules = self.state["modules"]

            return {
                "module": MODULE_NAME,
                "version": VERSION,
                "initialized": self.initialized,
                "system_status": self.state.get(
                    "system_status"
                ),
                "admin_id": ADMIN_ID,
                "admin_connected": self.admin_connected,
                "registered_modules": len(modules),
                "approved_modules": sum(
                    1
                    for module in modules.values()
                    if module.get("status") == "APPROVED"
                ),
                "pending_modules": sum(
                    1
                    for module in modules.values()
                    if module.get("status") == "PENDING"
                ),
                "denied_modules": sum(
                    1
                    for module in modules.values()
                    if module.get("status") == "DENIED"
                ),
                "incidents": len(
                    self.state["incidents"]
                ),
                "army_events": len(
                    self.state["army_events"]
                ),
                "users_with_security_records": len(
                    self.state["users"]
                ),
            }

# ============================================================
# 24. YAGONA XAVFSIZLIK MARKAZI
# ============================================================

security_center = SecurityCenter()

# ============================================================
# 25. MODUL TASHQI FUNKSIYALARI
# ============================================================

def setup(
    bot,
    dp,
    notify_admin,
    report_security_event=None,
):

    return security_center.setup(
        bot=bot,
        dp=dp,
        notify_admin=notify_admin,
        report_security_event=report_security_event,
    )

def initialize():
    return security_center.initialize()

def health_check():
    return security_center.health_check()

async def report_incident(event):
    return await security_center.report_incident(event)

def register_module(
    module_name,
    functions=None,
    requested_permissions=None,
):

    return security_center.register_module(
        module_name,
        functions,
        requested_permissions,
    )

def verify_module_approval(
    module_name,
    capabilities=None,
):

    return security_center.verify_module_approval(
        module_name,
        capabilities,
    )

def review_module(
    module_name,
    approved,
    permissions=None,
    reviewed_by=None,
):

    return security_center.review_module(
        module_name,
        approved,
        permissions,
        reviewed_by,
    )

def authorize_action(
    module_name,
    action_name,
):

    return security_center.authorize_action(
        module_name,
        action_name,
    )

def set_channel_group(
    group_name,
    channels,
):

    return security_center.set_channel_group(
        group_name,
        channels,
    )

def get_channel_group(group_name):
    return security_center.get_channel_group(group_name)

async def record_violation(
    user_id,
    reason,
):

    return await security_center.record_violation(
        user_id,
        reason,
    )

def get_user_restrictions(user_id):
    return security_center.get_user_restrictions(user_id)

def admin_unblock_user(
    user_id,
    reviewed_by=None,
):

    return security_center.admin_unblock_user(
        user_id,
        reviewed_by,
    )

async def army_report(
    event_type,
    file_name,
    description,
):

    return await security_center.army_report(
        event_type,
        file_name,
        description,
    )

def get_status():
    return security_center.get_status()

def get_incidents(limit=50):
    return security_center.get_incidents(limit)

def get_admin_history(limit=50):
    return security_center.get_admin_history(limit)
