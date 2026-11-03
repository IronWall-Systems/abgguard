from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from tortoise import fields, models

# Rule 6 names this literal: "پنل" is the default per-group panel trigger.
DEFAULT_TRIGGER_NAME = "پنل"


class BaseModel(models.Model):
    id = fields.BigIntField(pk=True)
    created_at = fields.DatetimeField(default=datetime.utcnow, index=True)

    class Meta:
        abstract = True


class GodAdmin(BaseModel):
    """Level 3 - global owner of the whole system."""

    user_id = fields.BigIntField(unique=True, index=True)
    username = fields.CharField(max_length=64, null=True)
    note = fields.CharField(max_length=255, null=True)

    class Meta:
        table = "rbac_god_admin"

    def __str__(self) -> str:  # pragma: no cover
        return f"GodAdmin({self.user_id})"


class Group(BaseModel):
    chat_id = fields.BigIntField(unique=True, index=True)
    title = fields.CharField(max_length=255, null=True)
    username = fields.CharField(max_length=64, null=True)
    # per-group panel alias (2-20 chars at the API layer); default = "پنل"
    trigger_name = fields.CharField(max_length=32, default=DEFAULT_TRIGGER_NAME)
    is_locked_until = fields.DatetimeField(null=True)
    slow_mode_until = fields.DatetimeField(null=True)

    class Meta:
        table = "groups"


class RoleBinding(BaseModel):
    """Level 2 (ADMIN) and Level 1 (SUBADMIN) membership, scoped to one chat."""

    ADMIN = 2
    SUBADMIN = 1

    chat_id = fields.BigIntField(index=True)
    user_id = fields.BigIntField(index=True)
    role = fields.SmallIntField()
    granted_by = fields.BigIntField(null=True)

    class Meta:
        table = "rbac_role_binding"
        unique_together = (("chat_id", "user_id"),)


class GroupSettings(BaseModel):
    chat_id = fields.BigIntField(unique=True, index=True)
    porn_lock = fields.BooleanField(default=True)
    photo_lock = fields.BooleanField(default=False)
    video_lock = fields.BooleanField(default=False)
    emoji_lock = fields.BooleanField(default=False)
    gif_lock = fields.BooleanField(default=False)
    link_lock = fields.BooleanField(default=False)
    forward_lock = fields.BooleanField(default=False)

    class Meta:
        table = "group_settings"


class GroupPolicy(BaseModel):
    """
    PV-configurable per-group policy that is *not* a boolean lock:
    the anti-bot CAPTCHA switch and the custom welcome template.

    Kept in its own table (instead of new columns on `group_settings`) so that
    `generate_schemas(safe=True)` can create it on an already-deployed
    database without an ALTER TABLE migration.
    """

    chat_id = fields.BigIntField(unique=True, index=True)
    captcha_enabled = fields.BooleanField(default=False)
    welcome_text = fields.CharField(max_length=500, null=True)

    class Meta:
        table = "group_policy"


class BadWord(BaseModel):
    chat_id = fields.BigIntField(index=True)
    word = fields.CharField(max_length=64, index=True)
    added_by = fields.BigIntField(null=True)

    class Meta:
        table = "bad_words"
        unique_together = (("chat_id", "word"),)


class BanLog(BaseModel):
    chat_id = fields.BigIntField(index=True)
    user_id = fields.BigIntField(index=True)
    reason = fields.CharField(max_length=64)
    message_id = fields.BigIntField(null=True)
    score = fields.FloatField(null=True)

    class Meta:
        table = "ban_log"


class GroupSubscription(BaseModel):
    """
    SaaS subscription state for one group (`chat_id`).

    `MANUAL_WHITELIST` rows carry `expires_at = None` and are never billed.
    `notified_days` is an int bitmask (1 = 1-day warning sent, 2 = 2-day,
    4 = 3-day) reset on every renewal so each threshold fires once per cycle.
    """

    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    MANUAL_WHITELIST = "MANUAL_WHITELIST"
    EXPIRED = "EXPIRED"
    UNPAID = "UNPAID"

    notified_1, notified_2, notified_3 = 1, 2, 4
    notified_bits: ClassVar[dict[str, int]] = {"3": 4, "2": 2, "1": 1}

    chat_id = fields.BigIntField(unique=True, index=True)
    title = fields.CharField(max_length=255, null=True)
    status = fields.CharField(max_length=24, default=UNPAID, index=True)
    expires_at = fields.DatetimeField(null=True)
    trial_used = fields.BooleanField(default=False)
    notified_days = fields.IntField(default=0)

    class Meta:
        table = "group_subscription"

    def __str__(self) -> str:  # pragma: no cover
        return f"GroupSubscription({self.chat_id}, {self.status})"


class PaymentMethod(BaseModel):
    """God-managed payment gateways shown on the SaaS payment screen.

    `type` is "card" (bank card number) or "crypto" (USDT wallet address);
    users get one `CopyTextButton` per active row.
    """

    CARD = "card"
    CRYPTO = "crypto"

    type = fields.CharField(max_length=8, default=CARD)
    title = fields.CharField(max_length=64)
    value = fields.CharField(max_length=128)
    is_active = fields.BooleanField(default=True)

    class Meta:
        table = "payment_method"

    def __str__(self) -> str:  # pragma: no cover
        return f"PaymentMethod({self.type}, {self.title})"


ALL_MODELS = [
    GodAdmin,
    Group,
    RoleBinding,
    GroupSettings,
    GroupPolicy,
    BadWord,
    BanLog,
    GroupSubscription,
    PaymentMethod,
]
