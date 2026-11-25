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

_data_dir = Path(os.getenv("SUPRA_DATA_DIR", Path(__file__).resolve().parent)).expanduser()
DATA_FILE = _data_dir / "xavfsizlik_markazi_data.json"

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

            # Admin tasdiqlash tokenlari RAM'da saqlanadi. Server qayta
            # ishga tushganda PENDING modullar uchun yangi so'rov yuboriladi.
            for module in self.state.get("modules", {}).values():
                if isinstance(module, dict) and module.get("status") == "PENDING":
                    module["notification_sent"] = False

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

    def record_admin_activity(self, action, details=None, admin_id=None):
        """Admin faoliyatini xavfsizlik tarixiga yozadigan ommaviy API."""
        self._require_admin(admin_id)
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Admin amali nomi noto'g'ri.")

        item = {
            "type": action.strip(),
            "action": action.strip(),
            "details": copy.deepcopy(details),
            "admin_id": int(admin_id),
            "time": self._now(),
        }
        with self.lock:
            self._add_admin_history(item)
            self._save_state()
        return True

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
# 25. SUPRA GATEWAY BUYRUQ ADAPTERI
# ============================================================

async def handle_command(command, payload, context=None):
    if not isinstance(payload, dict):
        return {"success": False, "error": "Buyruq ma'lumotlari noto'g'ri."}

    if command == "security_status":
        return {"success": True, "status": security_center.get_status()}
    if command == "security_incidents":
        return {"success": True, "incidents": security_center.get_incidents()}
    if command == "security_permissions":
        with security_center.lock:
            modules = copy.deepcopy(security_center.state.get("modules", {}))
        return {"success": True, "modules": modules}
    if command == "security_restrictions":
        user_id = payload.get("user_id")
        if user_id is None:
            return {"success": False, "error": "user_id talab qilinadi."}
        return {"success": True, "restrictions": security_center.get_user_restrictions(user_id)}
    if command == "security_army":
        required = ("event_type", "file_name", "description")
        if not all(payload.get(key) for key in required):
            return {"success": False, "error": "event_type, file_name va description talab qilinadi."}
        return {"success": await security_center.army_report(*(payload[key] for key in required))}
    if command == "admin_panel_close":
        return {"success": True, "message": "Admin panelini yopish qayd etildi."}

    return {"success": False, "error": "Noma'lum xavfsizlik buyrug'i."}

# ============================================================
# 26. MODUL TASHQI FUNKSIYALARI
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
