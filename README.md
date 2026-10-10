# ABG Guard

**Dual-bot Telegram group moderation platform with local ML content screening.**

A production-grade, multi-tenant moderation system for Telegram communities. It runs two bot identities in a single process — a **Master** bot for text, commands, RBAC and panels, and a **Worker** bot for media inspection — backed by PostgreSQL, Redis, and a dedicated ONNX microservice for real-time nudity detection.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        abgguard process                          │
│                                                                  │
│  ┌──────────────────┐          ┌──────────────────────────────┐ │
│  │   MASTER BOT      │          │       WORKER BOT              │ │
│  │  (text / commands │          │  (media / stickers / images)  │ │
│  │   FSM / panels)   │          │                              │ │
│  └────────┬─────────┘          └──────────────┬───────────────┘ │
│           │                                    │                 │
│           ▼                                    ▼                 │
│  ┌──────────────────┐          ┌──────────────────────────────┐ │
│  │  Router Pipeline  │          │   Content-Lock Pipeline       │ │
│  │  help → pv-fsm →  │          │   bot-guard → RBAC → VIP →   │ │
│  │  pv → rbac →      │          │   flags → caption → type →   │ │
│  │  modcmd → panel → │          │   ML scan                    │ │
│  │  guards → members │          │                              │ │
│  └────────┬─────────┘          └──────────────┬───────────────┘ │
│           │                                    │                 │
│           ▼                                    ▼                 │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │              Redis (hot path) + PostgreSQL (truth)           ││
│  │  RBAC sets · flood ZSETs · settings hashes · durable LIST    ││
│  └─────────────────────────────────────────────────────────────┘│
│                              │                                   │
│                              ▼                                   │
│               ┌──────────────────────────────┐                   │
│               │   Sanction Bus (Redis LIST)   │                   │
│               │   BRPOP → reason grammar →    │                   │
│               │   mute / escalate / log       │                   │
│               └──────────────────────────────┘                   │
└──────────────────────────────┬──────────────────────────────────┘
                               │ HTTP (multipart)
                               ▼
                 ┌──────────────────────────────┐
                 │     pornscan (microservice)   │
                 │   aiohttp · onnxruntime       │
                 │   MobileNetV4 · CPU-only      │
                 │   POST /api/v1/detect         │
                 └──────────────────────────────┘
```

### Why two bots?

The Master never touches media — a media flood can never starve command processing. The Worker never touches text or FSM — the hot path stays allocation-light. Disjoint `allowed_updates` scopes prevent double-handling, and a `is_bot_message` predicate makes every bot-authored message untouchable by locks, flood counters and the dataset harvester.

### pornscan microservice

`pornscan` is a standalone `aiohttp` service scoring images, video frames and GIFs with **onnxruntime** (no PyTorch, no TensorFlow, no external ML API). A 25 MB MobileNetV4 ONNX model runs entirely on the local CPU.

- `POST /api/v1/detect` — multipart upload, returns `{is_porn, score, labels}`
- 25 MB hard upload cap (fast 413)
- Pre-warmed model singleton (loaded at Application startup)
- Temp-file hygiene: every upload is deleted in a `finally` block
- Liveness (`/health/live`) vs readiness (`/health/ready`) split

The Guard client **fails open** on any timeout, non-200 or connection error — a dead detector never freezes moderation.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Telegram | aiogram 3.15 (dual Bot instances) |
| Database | PostgreSQL 16 · Tortoise ORM · asyncpg |
| Cache / bus | Redis 5 (custom key prefix `abg:`) |
| ML inference | onnxruntime 1.21 · MobileNetV4 (CPU) |
| Microservice | aiohttp 3.10 |
| Config | python-dotenv · frozen dataclass (`slots=True`) |
| JSON | orjson (hot path) |
| Linting | ruff (line-length 100, py312, aggressive rule set) |
| Testing | pytest 8.4 (pure unit, zero live infra) |

---

## Key Design Decisions

**Durable sanction bus over pub/sub.** Sanctions are pushed onto a Redis LIST (`abg:events:ban:q`), not a pub/sub channel. Unconsumed events survive a worker crash or master restart — a pub/sub channel would drop them.

**Dual-store RBAC.** PostgreSQL is the durable source of truth; Redis sets are the hot path. Writes are always Postgres-first, then Redis mirror, then cache invalidation. Private-chat resolution cannot use group-scoped sets, so it queries Postgres directly on every privileged callback.

**Anti-lockout guarantee.** A hardcoded fallback God Admin ID ensures a broken environment can never revoke Level-3 access. If `GOD_ADMIN_IDS` is missing or unparsable, the fallback is used and an error is logged.

**Never-break moderation ladder.** The bot NEVER autonomously bans. It applies timed or permanent mutes and escalates to the Level-2 owner in private for a final decision. The only real `ban_chat_member` call sites are the admin `/ban` command and the CAPTCHA timeout kick.

**Anti-bypass profanity normalization.** Arabic-to-Persian letter folding, harakat/tatweel/ZWNJ stripping, a character-level regex defeating spaced/dotted/digit smuggling, and word-boundary lookarounds with Persian inflection allowance.

**Zero-loss dataset harvesting.** Clean group messages are appended to a rotating JSONL corpus. On crossing 50 MB, the file is zipped and a fresh one is opened immediately — no loss in the gap. Delivery is automatic to God admins via Telegram.

---

## Project Structure

```
abgguard/
├── abgguard/
│   ├── app.py              # entry point, dual-bot boot
│   ├── config.py           # frozen Settings dataclass
│   ├── core/               # redis, rbac, help, moderation, harvester, metrics
│   ├── master/             # master dispatcher, routers, FSM, panels
│   ├── worker/             # worker dispatcher, media handlers
│   ├── models/             # Tortoise ORM entities
│   └── services/           # antiporn client, triggers, flood, captcha
├── microservice/
│   └── pornscan/           # ONNX nudity-detection microservice
│       ├── main.py
│       └── requirements.txt
├── tests/                  # ~200 unit tests (no live infra)
├── pyproject.toml          # ruff + pytest config
├── requirements.txt
├── run.py
├── Dockerfile
└── docker-compose.yml
```

---

## Quick Start

```bash
# 1. Clone and install
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — set MASTER_BOT_TOKEN, WORKER_BOT_TOKEN, GOD_ADMIN_ID,
# POSTGRES_*, REDIS_URL

# 3. Start pornscan (ML microservice)
cd microservice/pornscan
pip install -r requirements.txt
python main.py   # listens on :8010

# 4. Start the Guard
cd ../..
python run.py
```

### Docker Compose

```bash
docker compose up -d   # starts db, redis, pornscan, abgguard
```

Resource caps: 1 CPU / 512 MB for db, redis, abgguard; 1 CPU / 1 GB for pornscan.

---

## Testing

```bash
pytest tests/ -v
```

~200 unit tests covering the dispatcher pipeline, RBAC flows, god-assignment FSM, moderation ladder, harvester rotation, help system, panel close, polling mode and metrics. Zero live infrastructure required — all Redis storage methods are monkeypatched to no-ops.

---

## Security

- Bot tokens, DB credentials and Redis URLs are loaded exclusively from `.env` (never committed).
- All Redis key names are namespaced under `abg:`.
- Media payloads are never written to disk by the Guard (only pornscan's temp files, deleted in `finally`).
- The dataset harvester skips bot messages, staff messages and anything the moderation ladder deleted.
- RBAC is re-verified on every callback — no handler trusts FSM data alone.
- The Worker fails open on ML errors; the Master fails safe on Redis/Postgres errors (logs + DMs, never crashes).

---

## License

Private. All rights reserved.
