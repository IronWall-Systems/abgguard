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
