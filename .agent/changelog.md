# Agent Changelog

## 2026-11-03 — Profanity Filter & ModLog Workflow Improvements

### Profanity Filter (Scunthorpe Problem Fix)
- Replaced blind substring matching in `find_badword_root()` with a compiled Regex using `(?<!\w)` / `(?!\w)` word boundaries, so innocent words like `عکس` (picture) and `تخمه` (seed) no longer trigger `کس` / `تخم`.
- Removed everyday/ambiguous words from `BAD_WORDS_ROOTS`: `کس`, `کص`, `قس`, `تخم`, `ممه`, `بکن`, `کردن`, `کردم`, `دادن`, `میدم`, `دادم`, `حامله`, `ساک`, `کست`, `کصت`, `کسم`, `کصم`, `کسه`.
- Added `BAD_PHRASES` set (16 exact severe phrases like `کس مادرت`, `کیرم تو کس`, `تخم سگ`) merged into `PROFANITY_REGEX` with `\W*` obfuscation handling.
- Added `CONTEXT_PATTERNS` (4 proximity regex patterns) so ambiguous words only fire when near target words (e.g., `کس` within 15 chars of `مادر`).
- `find_badword_root()` now checks `PROFANITY_REGEX` first, then `COMPILED_CONTEXT` proximity patterns.

### ModLog Inline Buttons & Raw Text
- `render_report()` now includes `💬 متن اصلی پیام: {message_text}` showing the verbatim user message.
- `deliver()` attaches inline action buttons to content-violation cards:
  - `🔊 لغو سکوت (Unmute)` → `modact:unmute:<chat_id>:<user_id>`
  - `⛔️ اخراج (Ban)` → `modact:ban:<chat_id>:<user_id>`
  - `🗑 نادیده گرفتن` → `modact:dismiss`
- Added `modact_callback` handler in `pv_panel.py` to process `modact:*` callbacks (unmute, ban, dismiss).
- `message_text` now flows through `publish_ban_event()` → `handle_ban_event()` → `modlog.notify()`.

### Auto-Clean Public Warnings
- Public warning messages in groups are auto-deleted after 30 seconds via `delete_after()` fire-and-forget task.

### API & Call Manager Adjustments
- Removed MTProto Call Manager from Guard; group calls handled by playerbot project.
- Wipe command configured to use Local Telegram Bot API on `http://127.0.0.1:8081` for >48h deletions and bulk purges.

## 2026-11-03 — Dataset Collector & Escalating Strike System

### 50MB JSONL LLM Dataset Collector (`services/harvester.py`)
- `DatasetHarvester` silently records natural group conversations as JSONL pairs (`{"context", "reply", "chat", "user", "msg", "ts}`).
- Automatic rotation at exactly 50 MB (`DATASET_MAX_BYTES`): ZIP-compresses into `./dataset`, removes the source file, opens a new file — all inside an asyncio lock with zero message loss.
- Zero-disk retention: every archive is shipped to the God Admin's private chat as a document; wiped from local disk only after Telegram confirms upload. Failed uploads keep the file and notify the God Admin.
- God Admin manual trigger: `rotate_now()` backs the PV button `[🟣 تخلیه دستی و فشرده‌سازی]` for on-demand ZIP rotation and delivery.

### 24-Hour Redis Escalating Strike System (`core/redis.py`, `master/handlers/guards.py`)
- New `k_strike(chat_id, user_id)` key (`abg:strikes:{chat_id}:{user_id}`) with 86400s (24h) TTL.
- On every profanity/NSFW violation: message is always deleted immediately, then the strike counter is incremented.
- Escalating mute durations: Strikes 1-4 → delete only (no mute). Strike 5 → 5 min, Strike 6 → 10 min, Strike 7 → 30 min, Strike 8 → 1 hour, Strike 9+ → 24 hours.
- Auto-deleting public warning (30s) shows strike count: `"⚠️ کاربر {mention}، پیام شما به دلیل محتوای نامناسب پاک شد. (اخطار {strike_count}/4)"` for strikes < 5, or `"🔇 کاربر {mention} به دلیل تکرار تخلف ({strike_count} بار) به مدت {duration_text} سکوت شد."` for strikes >= 5.
- PV ModLog card includes `⚠️ تعداد تخلف امروز: {strike_count}` and `⚖️ اقدام ربات: {action_taken}`.

## 2026-11-03 — Hierarchical RBAC, Uniform Titles & Dynamic Mute

### Hierarchical RBAC (`core/hierarchy.py`, `core/redis.py`)
- New `k_role(chat_id, user_id)` key (`abg:role:{chat_id}:{user_id}`) stored in Redis.
- Role hierarchy: GOD (6) > DEPUTY (5) > SENIOR_CHAT/SENIOR_CALL (4) > ADMIN_CHAT/ADMIN_CALL (3) > USER (0).
- Appointment rules: GOD appoints anyone; DEPUTY appoints Seniors/Admins; SENIOR_CHAT appoints Chat Admins; SENIOR_CALL appoints Call Admins.
- `can_appoint(actor_role, target_role)` enforces hierarchy; `get_role()`/`set_role()`/`remove_role()` manage Redis storage.

### Uniform Custom Titles (no emojis, <= 16 chars)
- Deputy: `"معاون مالک"`; Senior Chat: `"ارشد چت"`; Senior Call: `"ارشد کال"`; Chat Admin: `"ادمین چت"`; Call Admin: `"ادمین کال"`.
- `set_chat_administrator_custom_title()` called immediately after `promote_chat_member()`.

### Promotion/Demotion Commands
- `نصب معاون` / `عزل معاون`; `نصب ارشد چت` / `عزل ارشد چت`; `نصب ارشد کال` / `عزل ارشد کال`; `نصب ادمین چت` / `عزل ادمین چت`; `نصب ادمین کال` / `عزل ادمین کال`.
- Executed by replying to target user; checks hierarchical authority before acting.
- `promote_chat_member()` grants only role-appropriate permissions; custom title set immediately after.

### Moderation Execution Validation
- Ban (`بن` / `اخراج`): allowed ONLY for God, Deputy, Senior Chat, Senior Call.
- Mute (`سکوت`): allowed ONLY for God, Deputy, Senior Chat, Chat Admin.

### Dynamic Mute Command (`سکوت`)
- Bare `سکوت` -> 30 minutes (1800s).
- `سکوت X` (e.g., `سکوت 1`, `سکوت 5`) -> X hours (X * 3600s).
- Uses `bot.restrict_chat_member(chat_id, user_id, until_date=time.time() + duration)`.

## 2026-11-04 — Wipe All Debug & Execution Timer

### Local API Connection Verified
- `core/botapi.py` `make_session()` returns `AiohttpSession(api=TelegramAPIServer.from_base("http://127.0.0.1:8081"))` — all Bot API calls route through the local server, bypassing the 48-hour cloud restriction.
- `master/bot.py` `create_master()` passes `session=make_session()` to `Bot()`.

### Wipe All Rewrite (`master/handlers/modcmd.py`)
- `WIPE_ALL_DEPTH` increased from 200 to 10000 messages.
- `WIPE_ALL_SLEEP` reduced from 0.3s to 0.05s between chunks.
- Loop walks backward from `current_id` down to `max(1, current_id - 10000)` in chunks of 99.
- `start_time = time.time()` timer started before the loop; `elapsed = round(time.time() - start_time, 2)` calculated after.
- `deleted_count` counter tracks successfully deleted messages.
- `log.debug()` statements for each chunk processed; `TelegramBadRequest` and `TelegramForbiddenError` explicitly logged with chat_id and message_id.
- Fallback to one-by-one `bot.delete_message()` when `delete_messages()` chunk fails.
- Success message: `"✅ تاریخچه گروه با موفقیت پاک‌سازی شد.\n🗑 تعداد: {deleted_count} پیام\n⏱ زمان عملیات: {elapsed} ثانیه"` auto-deletes after 15 seconds.

## 2026-11-04 — Bug Fix: Accurate Deletion Counting & Creator Immunity

### Wipe All Counter Fix (`master/handlers/modcmd.py`)
- `_do_wipe_all` now iterates one-by-one instead of trusting `delete_messages()` return value (Telegram can return `True` even when admin messages cannot be deleted).
- `deleted_count` is only incremented after a successful `await bot.delete_message()` — not before.
- `skipped_count` tracks messages that could not be deleted (admin immunity, not found, etc.) and is reported in the final notice.
- `TelegramBadRequest` and `TelegramForbiddenError` are explicitly logged per message_id.

### Moderation Deletion Validation (`services/moderation.py`)
- `delete_and_mute()` and `delete_and_ban()` now check the return value of `safe_delete()`.
- If deletion fails (e.g., creator/admin immunity), the public notice is suppressed to avoid falsely claiming the message was removed.
- The sanction (mute/ban) is still applied, but a warning is logged instead of sending a misleading public message.
