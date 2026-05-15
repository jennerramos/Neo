# Neo v2 — Phase 0 Setup

## Prerequisites
- PostgreSQL installed and running on Windows
- `uv` installed (`pip install uv` or via winget)
- Python 3.11+

---

## Step 1 — Create the database

Open **psql** or **pgAdmin** and run:

```sql
CREATE DATABASE neo_v2;
```

---

## Step 2 — Configure .env

Copy `.env.example` to `.env` and set your password:

```
DATABASE_URL=postgresql://postgres:yourpassword@localhost:5432/neo_v2
```

---

## Step 3 — Install dependencies with uv

```powershell
cd "C:\Users\darkr\Downloads\NEO\Neo v2"
uv sync
```

---

## Step 4 — Run Alembic migration (creates all tables)

```powershell
uv run alembic upgrade head
```

Expected output:
```
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema — schools, channels, videos, transcripts, chunks
```

---

## Step 5 — Seed Phase 1 colleges

```powershell
uv run python scripts/seed_db.py
```

Expected output:
```
Neo v2 — Phase 1 seed
========================================
[OK] Database connection successful.
  [ADD]  School: Houston City College (id=1)
         Channel: Houston City College
  [ADD]  School: Lone Star College (id=2)
         Channel: Lone Star College
  [ADD]  School: El Paso Community College (id=3)
         Channel: El Paso Community College
  [ADD]  School: Central Texas College (id=4)
         Channel: Central Texas College
  [ADD]  School: Mt. San Antonio College (id=5)
         Channel: Mt. San Antonio College

[DONE] Inserted 5 school(s) and 5 channel(s).
```

---

## Verify in psql

```sql
SELECT s.name, c.youtube_channel_id, c.youtube_channel_name
FROM schools s
JOIN channels c ON c.school_id = s.id
ORDER BY s.id;
```

---

## Project structure

```
Neo v2/
├── pyproject.toml          ← uv project manifest
├── .env                    ← your local DB config (gitignored)
├── .env.example            ← template
├── alembic.ini             ← Alembic config
├── alembic/
│   ├── env.py              ← reads .env, imports all models
│   ├── script.py.mako      ← migration file template
│   └── versions/
│       └── 0001_initial_schema.py  ← creates all 5 tables
├── neo/
│   ├── db/
│   │   ├── base.py         ← SQLAlchemy Base
│   │   ├── session.py      ← engine + get_session() context manager
│   │   └── models/
│   │       ├── school.py   ← schools table
│   │       ├── channel.py  ← channels table
│   │       ├── video.py    ← videos table + status enums
│   │       ├── transcript.py ← transcripts table
│   │       └── chunk.py    ← chunks table (pgvector-ready)
│   └── seed/
│       └── phase1.py       ← 5 Phase 1 college definitions
└── scripts/
    └── seed_db.py          ← idempotent seed script
```

---

## Next phases

- **Phase 1** — YouTube video discovery & collection (port from v1)
- **Phase 2** — Caption download + Whisper ASR (port from v1, write results to `videos` + `transcripts` tables)
- **Phase 3** — Transcript cleaning + chunking (write to `chunks` table)
- **Phase 4** — Embeddings via pgvector (add `embedding` column to `chunks`)
- **Phase 5** — RAG interface + trustee dashboard
