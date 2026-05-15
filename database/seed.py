"""
Seed the Neo v2 database with Phase 1 colleges and their YouTube channels.

Phase 1 covers 5 colleges with verified YouTube channels.
Channel IDs sourced from Neo v1 school_IDs.csv (confidence 9/10).

Usage (from project root):
    uv run python database/seed.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from database.models import School, Channel

# ---------------------------------------------------------------------------
# Phase 1 seed data
# ---------------------------------------------------------------------------

PHASE1_COLLEGES = [
    {
        "school": {
            "name": "Houston City College",
            "slug": "houston_city_college",
            "website": "https://www.hccs.edu",
            "state": "TX",
            "source_type": "youtube",
        },
        "channel": {
            "youtube_channel_id": "UCwHm1fTMp6kfNN9owU62LYQ",
            "channel_name": "Houston City College",
        },
    },
    {
        "school": {
            "name": "Lone Star College",
            "slug": "lone_star_college",
            "website": "https://www.lonestar.edu",
            "state": "TX",
            "source_type": "youtube",
        },
        "channel": {
            "youtube_channel_id": "UCNQ1Xm47IL2MPuf1cF6IzVQ",
            "channel_name": "Lone Star College",
        },
    },
    {
        "school": {
            "name": "El Paso Community College",
            "slug": "el_paso_community_college",
            "website": "https://www.epcc.edu",
            "state": "TX",
            "source_type": "youtube",
        },
        "channel": {
            "youtube_channel_id": "UCeDnsaeakptM1bxNx1uyJ6A",
            "channel_name": "El Paso Community College",
        },
    },
    {
        "school": {
            "name": "Central Texas College",
            "slug": "central_texas_college",
            "website": "https://www.ctcd.edu",
            "state": "TX",
            "source_type": "youtube",
        },
        "channel": {
            "youtube_channel_id": "UCoP5c9kvln3ADDRCvVn7etw",
            "channel_name": "Central Texas College",
        },
    },
    {
        "school": {
            "name": "Mt. San Antonio College",
            "slug": "mt_san_antonio_college",
            "website": "https://www.mtsac.edu",
            "state": "CA",
            "source_type": "youtube",
        },
        "channel": {
            "youtube_channel_id": "UCcDRNdBOOte8-3MHZAU_TGQ",
            "channel_name": "Mt. San Antonio College",
        },
    },
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("[ERROR] DATABASE_URL not set. Copy .env.example to .env and fill in your password.")
        sys.exit(1)

    engine = create_engine(database_url, echo=False)
    Session = sessionmaker(bind=engine)

    # Quick connection check
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[OK] Database connection successful.")
    except Exception as exc:
        print(f"[ERROR] Cannot connect to database: {exc}")
        sys.exit(1)

    print("\nNeo v2 — Phase 1 seed")
    print("=" * 40)

    with Session() as session:
        inserted_schools = 0
        inserted_channels = 0

        for entry in PHASE1_COLLEGES:
            school_data = entry["school"]
            channel_data = entry["channel"]

            # ── Upsert school ──────────────────────────────────────────────
            school = session.query(School).filter_by(slug=school_data["slug"]).first()
            if school:
                print(f"  [SKIP] School already exists: {school.name}")
            else:
                school = School(**school_data)
                session.add(school)
                session.flush()   # get school_id before inserting channel
                inserted_schools += 1
                print(f"  [ADD]  School: {school.name} (school_id={school.school_id})")

            # ── Upsert channel ─────────────────────────────────────────────
            channel = (
                session.query(Channel)
                .filter_by(youtube_channel_id=channel_data["youtube_channel_id"])
                .first()
            )
            if channel:
                print(f"         Channel already exists: {channel.channel_name}")
            else:
                channel = Channel(school_id=school.school_id, **channel_data)
                session.add(channel)
                inserted_channels += 1
                print(f"         Channel: {channel_data['channel_name']}")

        session.commit()

    print(f"\n[DONE] Inserted {inserted_schools} school(s) and {inserted_channels} channel(s).")
    print("\nVerify with:")
    print("  SELECT school_id, name, slug FROM schools;")
    print("  SELECT c.channel_name, c.youtube_channel_id, s.name FROM channels c JOIN schools s ON s.school_id = c.school_id;")


if __name__ == "__main__":
    main()
