from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PvGodSG(StatesGroup):
    """Level 3 prompts - everything happens in the God Admin's private chat."""

    # God Admin -> admin assignment, strictly staged (rule 3: the (user_id,
    # chat_id) pair is always explicit, never implied):
    #   1/3 owner_chat  -> target group id   (-100123456789)
    #   2/3 owner_user  -> target user id    (123456789)
    #   3/3 owner_level -> Level 2 / Level 1 (inline buttons)
    owner_chat = State()
    owner_user = State()
    owner_level = State()

    broadcast = State()    # free text -> fan-out to every managed group

    # payment gateway management (card / crypto, God Admin only):
    #   pay_title -> display title  ("بانک ملت" / "Tether TRC20")
    #   pay_value -> card number    / wallet address
    pay_title = State()
    pay_value = State()


class PvAdminSG(StatesGroup):
    """Level 2/1 prompts - configured per selected group, never in the group."""

    badword_add = State()    # comma separated blacklist words
    subadmin_add = State()   # raw Telegram user_id (Level 2 only)
    welcome_set = State()    # custom Persian welcome template
    trigger_set = State()    # new panel trigger name (2-20 chars)
