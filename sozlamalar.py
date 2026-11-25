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
        data_file=None,
    ):
        self.supra_gateway = supra_gateway
        self.is_admin_verified = is_admin_verified
        self.notify_admin = notify_admin
        self.report_security_event = report_security_event

        if data_file is None:
            data_dir = Path(os.getenv("SUPRA_DATA_DIR", Path(__file__).resolve().parent)).expanduser()
            data_file = data_dir / "sozlamalar_data.json"
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
    data_file=None,
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



async def handle_command(command, payload, context=None):
    """SUPRA Gateway uchun modul darajasidagi buyruq adapteri."""
    if settings_manager is None or not settings_manager.initialized:
        return {"ok": False, "error": "Sozlamalar moduli tayyor emas."}
    return await settings_manager.handle_command(command, payload, context)

async def shutdown():
    if settings_manager is None:
        return True
    return True
