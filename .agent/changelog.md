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
