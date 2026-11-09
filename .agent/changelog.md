# Agent Changelog

## 2026-11-09 — Fix: Sync profanity filter with admin panel, log bad words, whole-word matching

- The admin-managed `bad_words` table (PostgreSQL, mirrored to Redis) is now the single source of truth for the profanity filter; the separate hard-coded aggressive word floor is no longer applied, so harmless words like `بگیم` are not flagged.
- `settings_store.load_badwords()` / `load_badwords_refresh()` now log the active blacklist (`BADWORDS <source> chat=... count=... words=[...]`) whenever it is loaded, fetched, refreshed, seeded, added to, or removed from. Repeated per-message cache reads are de-duplicated so logs stay readable.
- Redis snapshots are refreshed on panel/PV changes and given the same TTL on both the initial load and refresh paths.
- Strengthened whole-word matching in `find_fight_word`: the optional `بی` prefix now also requires a strict left word boundary, so a prefixed entry embedded inside a longer word (e.g. `کتابیکسکش`) can never fire. Independent words, common inflections (`کسکشی`), and obfuscation glue (`کص.ننت`, `ک‌ونی`, spacing/digits) still match.
- Updated the corresponding test comment to reflect that only the admin list drives the filter.

## 2026-11-09 — Fix: Sync profanity filter with admin panel, log bad words, whole-word matching

- The admin-managed `bad_words` table (PostgreSQL, mirrored to Redis) is now the single source of truth for the profanity filter; the separate hard-coded aggressive word floor is no longer applied, so harmless words like `بگیم` are not flagged.
- `settings_store.load_badwords()` / `load_badwords_refresh()` now log the active blacklist (`BADWORDS <source> chat=... count=... words=[...]`) whenever it is loaded, fetched, refreshed, seeded, added to, or removed from. Repeated per-message cache reads are de-duplicated so logs stay readable.
- Redis snapshots are refreshed on panel/PV changes and given the same TTL on both the initial load and refresh paths.
- Strengthened whole-word matching in `find_fight_word`: the optional `بی` prefix now also requires a strict left word boundary, so a prefixed entry embedded inside a longer word (e.g. `کتابیکسکش`) can never fire. Independent words, common inflections (`کسکشی`), and obfuscation glue (`کص.ننت`, `ک‌ونی`, spacing/digits) still match.
- Bulk addition of new forbidden words and phrases from the admin task list, handled gracefully with `get_or_create` (skips duplicates, inserts new ones). Redis cache refreshed immediately after sync so the new words take effect right away.
- Updated the corresponding test comment to reflect that only the admin list drives the filter.

## 2026-11-08 — Feature: Captcha & Auto-Approve toggles (Tier 5+) + batch deleteMessages

- Added per-chat `auto_approve_enabled` alongside `captcha_enabled` (both default `False`) to `GroupPolicy`, with an idempotent `ALTER TABLE` migration in `core/db.py`.
- Extended `settings_store` with `GroupPolicyFlags.auto_approve_enabled`, Redis hash field `auto`, `set_auto_approve()`, and `toggle_policy()`.
- Group panel (`panel_kb` / `panel_help_kb`) now shows two 2-column toggles with live status: `کپچا: ✅/❌` and `تایید خودکار: ✅/❌` (`abg:pol:captcha`, `abg:pol:auto`).
- Added text commands: `قفل کپچا روشن/خاموش` and `قفل تایید خودکار روشن/خاموش`.
- Restricted both UI toggles and text commands to Tier 6 (God) and Tier 5 (Deputy / group owner); lower tiers get an `answer_callback_query(..., show_alert=True)` denial.
- `chat_join_request` now approves only when `auto_approve_enabled` is on and runs the 5-second math captcha only when `captcha_enabled` is on.
- PV dashboard policy page now also exposes the auto-approve toggle and shows both statuses.
- Replaced the one-by-one `_do_wipe_all` loop with chunked `bot.delete_messages` batches of 100 (with one-by-one fallback per failed chunk), dramatically speeding up full-history wipes.

## 2026-11-07 — Fix: Restore broken promote/demote commands after custom title refactor

- Fixed `ValueError: too many values to unpack` in `on_role_command` caused by `_parse_role_command_details` still returning 4 values while the caller unpacked only 3.
- Updated `_parse_role_command_details` to return only `(command_text, target_role, explicit_id)` — removed the `custom_title` from the return tuple.
- Simplified `_parse_role_command` to directly delegate to `_parse_role_command_details`.
- Promotion/demotion commands now work correctly again: they accept targeting via Reply, Numeric ID, or Username (e.g., `نصب ادمین چت 123456789`) and ignore any trailing text.

## 2026-11-07 — Refactor: Remove all custom title assignment logic from promotion commands

- Completely removed automated custom title assignment (`set_chat_administrator_custom_title`) from promotion/demotion commands (`نصب معاون`, `نصب ارشد چت`, `نصب ادمین چت`, etc.).
- Deleted `_clean_custom_title()`, `_reply_custom_title()`, and the `custom_title_text` parameter from `_do_promote()`.
- Promotion commands now only handle ID/Username/Reply targeting — any text after the command or ID is ignored.
- Custom titles are now 100% manual (set via Telegram UI only).

## 2026-11-07 — Security: Restrict custom title assignment to Senior tier and above

- Custom title assignment (`set_chat_administrator_custom_title`) is now restricted to actors with hierarchy level >= 4 (Senior Chat / Senior Call / Deputy / God).
- Lower tiers (Chat Admin, Call Admin, Sub-Admin - Tier 3 and below) can no longer set custom titles via promotion commands.
- If a Tier 3 or lower admin uses a promotion command with a custom title, the title request is ignored and only the standard default role title is applied.

## 2026-11-07 — Feature: Auto-delete all Telegram service messages and system notification logs

- Added a service message handler that instantly deletes all Telegram service messages as soon as they appear in the group.
- Covers: `new_chat_members`, `left_chat_member`, `pinned_message`, `video_chat_scheduled/started/ended/participants_invited`, `message_auto_delete_timer_changed`, `proximity_alert_triggered`, `forum_topic_created/edited/closed/reopened`, `general_forum_topic_hidden/unhidden`, `invoice`, `passport_data`, `voice_chat_scheduled/started/ended/participants_invited`.
- Each service message is deleted via `await message.delete()` inside a `try/except` block to gracefully handle missing permissions.

## 2026-11-07 — Fix: 5-minute captcha timeout, safe number-only input, and 30s auto-deleting welcome

### Join Request Approval
- `chat_join_request` now approves every join request immediately without any profile screening (no photo or username checks).

### 5-Second Delay & 5-Minute Captcha Timeout
- After approval, the bot waits 5 seconds, then sends the Smart Math Captcha challenge in the group.
- Redis state TTL for the unverified captcha state increased from 60 to 300 seconds (`captcha_timeout` in `config.py`).

### Safe Captcha Interaction (No Bans)
- Unverified users are **NOT** restricted from sending text numbers — they can type their numeric answer freely.
- Correct number: verify them, add to safe list, delete prompt/answer messages, send welcome greeting.
- Non-number input: instantly delete their message, send a temporary auto-deleting warning asking them to solve the math captcha — **never ban**.

### Auto-Cleaning Welcome Message
- On successful captcha, send a welcome message mentioning/tagging the user (e.g., *"خوش آمدی [نام](tg://user?id=...)!"*).
- Welcome message auto-deletes after 30 seconds.
- All bot warnings, captcha prompts, and alerts auto-delete — zero bot footprint.

## 2026-11-07 — Admin Audit & Combined Title Sync (`بررسی ادمین`)

- Added the `بررسی ادمین` / `/audit_admins` command for Level 2+ staff.
- The audit fetches every group administrator and infers Chat/Call Admin roles from both the organizational hierarchy and Telegram permissions.
- Administrators holding both Chat and Call Admin roles are synced to the combined `ادمین چت و کال` title.
- Custom/manual titles and Redis-marked protected titles are never overwritten; only empty or bot-managed titles are updated.
- Added the combined `ADMIN_CHAT_CALL` role so promotions merge Chat+Call admins and demotions fall back to the remaining single role.
- The audit reply reports checked, updated, preserved, and failed counts.

## 2026-11-07 — Custom Titles From Promotion Commands & Replies

- Promotion commands now accept an inline custom title after the command, e.g. `نصب ارشد چت مدیر کل`.
- When no inline title is provided, the replied-to message text or caption is used as the title source.
- Custom titles are stripped of emojis, whitespace-normalized, and truncated to Telegram's 16-character limit.
- If the resulting title is empty, the role's default title is used.
- Explicit numeric target IDs remain supported; when replying, numeric text after the command is treated as a title.

## 2026-11-07 — Join Request Screening, Math CAPTCHA & Auto-Cleaning Welcome

- Added a `chat_join_request` screener: rejects bots/invalid profiles and users without a profile photo, logs missing usernames, and approves valid requests.
- Approved join requests immediately enter the math CAPTCHA flow when the group CAPTCHA policy is enabled.
- Replaced the inline-button CAPTCHA with a one-answer math challenge using `*`, positive `-`, and whole-number `/` questions (numbers 1-10).
- Pending CAPTCHA state is stored in Redis with a 60-second timeout; unverified users are restricted from sending messages.
- Any unverified message other than the exact numeric answer is deleted immediately, followed by a 12-second auto-deleting warning.
- Correct answers delete both the prompt and the answer, lift the restriction, add the user to the Redis verified/safe list, and send a welcome greeting.
- Welcome greetings now mention the user and auto-delete after 30 seconds; CAPTCHA prompts, warnings, and join-flood alerts also auto-delete.
- Added a dedicated early `captcha_router` so unverified filtering runs before moderation/guards and cannot be bypassed by media or commands.
- `ruff check` and join-request/CAPTCHA smoke tests pass.

## 2026-11-07 — Promotion Command Repair & Hierarchy UI

### Promotion Commands
- Root cause: `bot.promote_chat_member()` was called with the removed `can_manage_voice_chats` keyword, raising an uncaught `TypeError` that produced no group feedback. Removed the unsupported argument.
- Group owners and God admins are now mapped into the organizational hierarchy for authorization, so the first promotion no longer silently returns because no stored hierarchy role exists yet.
- Promotion commands now accept both replies and explicit numeric Telegram IDs, including Persian/Arabic digits, Arabic/Persian character variants, extra whitespace, and a leading slash.
- Added generic aliases: `نصب ادمین` / `عزل ادمین` map to Chat Admin / User.
- Missing targets, insufficient rank, Telegram API errors, and internal persistence failures now return visible feedback; unexpected exceptions are logged.
- Replaced the generic sub-admin panel with the read-only organizational-tier instructions in group and PV panels; public help now hides the God tier.

## 2026-11-05 — Help Placement & Subscription Navigation Fix

- Removed the full install/management tutorial from subscription status, trial, expiry, and payment cards.
- The group-panel `❓ راهنما` button now opens the comprehensive management guide (6-tier hierarchy, mute syntax, auto-filters, wipe rules) instead of the brief lock legend.
- Added a Back button to the group subscription card (`abg:back`), PV subscription list (`pv:root`), and PV per-group subscription card (`pv:substat`).
- Added Back to the group help screen while keeping Close as the final row.
- Subscription keyboards now always keep Close at the bottom; the PV root handler is private-chat only so a stray group callback cannot render the dashboard in a group.
- `ruff check .` and `python -m compileall` pass.

## 2026-11-05 — Profanity Strike Fix & In-App Help Overhaul

### Profanity Strikes 1-4 Are Delete-Only
- Root cause: the guard ladder already avoided muting on strikes 1-4, but it published a bare reason to the event bus. `handle_ban_event()` interpreted any bare reason as a permanent mute, so the first offense still restricted the member.
- Added the explicit `deleteonly:<reason>` event intent and handled it in `handle_ban_event()` as delete + log only, with no restriction and no owner escalation.
- Extracted the shared `escalate_content_violation()` ladder into `services/moderation.py`; text and media-caption profanity now use the same path.
- Strikes 1-4: delete + 30-second public warning only. Strike 5: 5 minutes; 6: 10 minutes; 7: 30 minutes; 8: 1 hour; 9+: 24 hours.
- Worker media-caption profanity no longer calls `delete_and_mute()` directly.
- Updated `tests/test_fight_words.py` and `tests/test_bus_events.py` for delete-only behavior and the fifth-strike mute.

### Help Text & Close Button
- Rewrote the interactive help sections and `/help` guide around the 6-tier organizational hierarchy, promotion/demotion syntax, mute semantics, wipe confirmation, and the two authorization layers.
- `ui.public_help_text()` now matches `USER_MANUAL_FA.md` instead of the outdated command catalog.
- Updated the PV tutorial and public start snippets for correct mute/wipe/hierarchy wording.
- Private `/help` now opens the role-aware interactive menu.
- Added `[❌ بستن پنل | Close]` (`pv:close`) to public help/start/mygroups keyboards and to subscription/onboarding/payment keyboards.
- Updated `USER_MANUAL_FA.md` to document automatic hierarchy ↔ operational RBAC sync.
- `ruff check .` and `python -m compileall` pass.

## 2026-11-04 — UI/UX, ModLog, RBAC Sync & Safety Optimization

### Lock Panels
- Reordered shared lock toggles to Antiporn, Photo, Video, Link, Forward, Emoji, Gif.
- Replaced `✅`/`❌` indicators with explicit `🟢 فعال` / `🔴 غیرفعال` labels in both group and PV panels.
- In-group and PV lock grids now render in two columns via shared `TOGGLES`, `state_label()`, and `lock_label()`.
- PV keyboard no longer duplicates the lock-toggle list.

### Destructive Wipe Confirmation
- `/wipeall` and «پاک‌سازی همه» now show a confirmation card with «تایید ✅» / «انصراف ❌» before deleting anything.
- The confirmation prompt auto-deletes after 30 seconds.
- Added `abg:wipe:yes` / `abg:wipe:no` callback handling in the group panel.

### Moderation Command Semantics
- All moderation commands now require a reply to the target message; raw numeric user IDs are no longer accepted as targets.
- Missing-target response changed to «لطفاً روی پیام کاربر موردنظر ریپلای کنید.»
- Bare mute defaults to 30 minutes (`DEFAULT_MUTE=1800`).
- `mute X` now means exactly X hours; non-integer or non-positive values are rejected.
- Manual `/warn` keeps a 10-minute first-strike mute via `WARN_MUTE`.

### Hierarchy ↔ Operational RBAC Sync
- Promotion/demotion now synchronizes Telegram permissions, organizational hierarchy, and operational RBAC.
- Added `rbac.sync_operational_role()` and `rbac.clear_operational_role()`.
- Mapping: Deputy/Senior → Level 2 (Admin); Chat/Call Admin → Level 1 (Subadmin).
- Promotion API calls run before Redis/RBAC writes to avoid partial state.
- Self-promotion/demotion is rejected.
- `GOD` can only be appointed by `GOD`; demotion compares against the target's current rank and blocks equal/higher ranks.

### ModLog Cards
- Content-violation card compacted into a linear layout with offender link, optional username, violation/strike, action, and optional message text.
- Removed duplicate action/ID lines.
- ModLog action buttons now append the outcome and «✅ انجام شد توسط @Admin» and remove the keyboard.
- Dismiss now also stamps the card instead of only removing the keyboard.

### NSFW Cleanup
- `delete_and_ban()` now auto-deletes its public NSFW warning after 30 seconds, matching other public moderation notices.

### Docs & Tests
- Updated Persian help text for reply-only targets, mute semantics, wipe confirmation, and new lock states.
- Updated `USER_MANUAL_FA.md` for the implemented behavior.
- Updated `tests/test_modcmd.py` and `tests/test_modlog.py` for the new parser and card format.
- `ruff check .` and `python -m compileall` pass.

## 2026-11-04 — Moderation Pipeline Repair (Profanity + NSFW)

### Profanity Filter Root Cause
- `master/handlers/modcmd.py` ended with a catch-all `@router.message()` before
  `guards.router` was mounted. It matched every group text message and returned
  `None`, so aiogram stopped propagation and the text guards never ran.
- Replaced the catch-all with `RoleCommandTrigger`, which matches only the
  hierarchical promotion/demotion commands (`نصب معاون`, `عزل معاون`, ...).
  Ordinary text now continues to `master.guards.guard_text`.
- `services/textguards.py` now ignores spaces inside blacklist phrases when
  building `PROFANITY_REGEX`, so `کص ننت` also matches `کص.ننت` and `کص  ننت`.

### NSFW Download Fix (Local Bot API)
- Local Bot API `getFile` returns absolute container paths such as
  `/var/lib/telegram-bot-api/<token>/photos/file_1.jpg`. Building an HTTP
  `/file/bot...` URL from that path returned `404`, so every media scan failed
  open and no porn was detected.
- `core/botapi.py` now creates the session with `is_local=True` and
  `SimpleFilesPathWrapper`, mapping the container path to the host bind mount
  before reading the file directly. No HTTP file endpoint is used in local mode.
- Added `TELEGRAM_BOT_API_SERVER_DIR` and `TELEGRAM_BOT_API_DATA_DIR` settings
  (defaults: `/var/lib/telegram-bot-api` and
  `/opt/dailydev-telegram-bot/telegram-bot-api-data`) and documented them in
  `.env.example`.
- `services/media_guard.py` now logs the exception type, chat/message/kind and
  traceback when a download fails instead of a one-line warning.

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
