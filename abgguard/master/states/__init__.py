from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PanelSG(StatesGroup):
    """Management states entered straight from the in-group inline panel."""

    bad_word = State()      # 🤬 مدیریت کلمات رکیک
    sub_admin_add = State()  # 👥 افزودن ساب‌ادمین (reply or user id)
    sub_admin_remove = State()
    trigger_name = State()   # 🏷 نام فراخوانی ربات (per-group alias, 2-20 chars)
