import os
from dotenv import load_dotenv
load_dotenv(".env")
from sqlalchemy import create_engine, text
e = create_engine(os.environ["DATABASE_URL"])
with e.connect() as c:
    rows = c.execute(text("""
        SELECT s.slug, m.status, COUNT(*) AS n
        FROM meetings m JOIN schools s ON s.school_id=m.school_id
        WHERE s.slug IN ('austin_community_college','alamo_colleges','dallas_college')
        GROUP BY s.slug, m.status ORDER BY s.slug, m.status
    """)).fetchall()
    for r in rows:
        print(f"{r[0]:30s} {r[1]:25s} {r[2]}")
    print("---")
    v = c.execute(text("""
        SELECT (SELECT COUNT(*) FROM votes) AS v,
               (SELECT COUNT(*) FROM financial_items) AS f,
               (SELECT COUNT(*) FROM personnel_actions) AS p,
               (SELECT COUNT(*) FROM initiatives) AS i,
               (SELECT COUNT(*) FROM pattern_signals) AS ps
    """)).fetchone()
    print(f"votes={v[0]} financial={v[1]} personnel={v[2]} initiatives={v[3]} pattern_signals={v[4]}")
