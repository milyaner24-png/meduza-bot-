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
        data_file=None,
    ):
        self.bot = bot
        self.supra_gateway = supra_gateway
        self.is_source_authorized = is_source_authorized
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event

        if data_file is None:
            data_dir = Path(os.getenv("SUPRA_DATA_DIR", Path(__file__).resolve().parent)).expanduser()
            data_file = data_dir / "tugmalar_data.json"
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
    data_file=None,
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

