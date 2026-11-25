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

_data_dir = Path(os.getenv("SUPRA_DATA_DIR", ".")).expanduser()
supra_gateway = SupraGateway(
    data_file=str(_data_dir / "supra_gateway_data.json")
)

