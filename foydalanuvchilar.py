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

_data_dir = Path(os.getenv("SUPRA_DATA_DIR", Path(__file__).resolve().parent)).expanduser()
DATA_FILE = _data_dir / "foydalanuvchilar.txt"

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

