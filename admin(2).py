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
    """Admin ID ga xabar yuboradi; muhim markupni yo'qotmaydi."""
    if notify_admin is not None:
        try:
            result = await call_maybe_async(
                notify_admin,
                text,
                **kwargs
            )
        except TypeError:
            result = await call_maybe_async(notify_admin, text)

        if result is not False:
            return result

    # Muhim Admin xabari umumiy bildirishnoma sozlamasi sabab
    # yuborilmasa, klaviaturani saqlagan holda to'g'ridan-to'g'ri yuboriladi.
    if bot is not None:
        return await bot.send_message(ADMIN_ID, text, **kwargs)

    return False

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
    description: str = "",
    functions: Optional[list] = None,
    requested_permissions: Optional[list] = None,
):
    """Yangi modul uchun Admin tasdiqlash so'rovini yuboradi."""
    if not module_name:
        return None

    token = uuid.uuid4().hex

    combined_permissions = list(dict.fromkeys(
        list(functions or [])
        + list(requested_permissions or [])
        + list(permissions or [])
    ))

    request_data = {
        "token": token,
        "module_name": module_name,
        "permissions": combined_permissions,
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

        sent = await send_admin_message(
            message_text,
            reply_markup=keyboard
        )
        if sent is False or sent is None:
            raise RuntimeError("Admin tasdiqlash xabari yuborilmadi.")

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
        if isinstance(result, dict):
            return result.get("success") is True
        return result is True
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
            reviewed_by=ADMIN_ID,
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

        if result is False or (
            isinstance(result, dict) and result.get("success") is not True
        ):
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

        if review_result is False or (
            isinstance(review_result, dict)
            and review_result.get("success") is not True
        ):
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

