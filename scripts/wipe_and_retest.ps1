# Neo v2 — Wipe extracted data and run one HCC meeting through the v2.5 extractor
# Run from: C:\Users\darkr\Downloads\NEO\Neo_v2
# Usage:    .\scripts\wipe_and_retest.ps1

Set-Location $PSScriptRoot\..

Write-Host "=== Neo v2: Wipe + Retest (Extractor v2.5) ===" -ForegroundColor Cyan

# 1. Reset meetings and clear tables
Write-Host "`n[1] Wiping extracted data..." -ForegroundColor Yellow
uv run python - << 'PYEOF'
import sys
sys.path.insert(0, '.')
from sqlalchemy import create_engine, text
import config

engine = create_engine(config.DATABASE_URL)
with engine.connect() as conn:
    r = conn.execute(text("UPDATE meetings SET status='processed' WHERE status='extracted'"))
    print(f"  Reset {r.rowcount} meeting(s) to 'processed'")
    for tbl in ('votes', 'financial_items', 'personnel_actions'):
        r2 = conn.execute(text(f"DELETE FROM {tbl}"))
        print(f"  Cleared {r2.rowcount} rows from {tbl}")
    conn.commit()
print("  Wipe complete.")
PYEOF

# 2. Run extractor on 1 HCC meeting
Write-Host "`n[2] Running extractor v2.5 on 1 HCC meeting..." -ForegroundColor Yellow
uv run python pipeline/extractor.py --school houston_city_college --limit 1

# 3. Show results
Write-Host "`n[3] Results:" -ForegroundColor Yellow
$query = @"
SELECT 'votes' AS tbl, topic_category AS label,
       COUNT(*) AS cnt,
       ROUND(AVG(confidence)::numeric,2) AS avg_conf,
       SUM(needs_review::int) AS flagged
FROM votes GROUP BY topic_category
UNION ALL
SELECT 'financial', action_type, COUNT(*), ROUND(AVG(confidence)::numeric,2), SUM(needs_review::int)
FROM financial_items GROUP BY action_type
UNION ALL
SELECT 'personnel', action_type, COUNT(*), ROUND(AVG(confidence)::numeric,2), SUM(needs_review::int)
FROM personnel_actions GROUP BY action_type
ORDER BY 1, 3 DESC;
"@

psql -U postgres -d neo_v2 -c $query

# 4. Show discard log if any
Write-Host "`n[4] Discard log:" -ForegroundColor Yellow
$discardDir = "data\extraction_discards"
if (Test-Path $discardDir) {
    $files = Get-ChildItem $discardDir -Filter "*.jsonl"
    if ($files.Count -gt 0) {
        foreach ($f in $files) {
            $lines = (Get-Content $f.FullName | Measure-Object -Line).Lines
            Write-Host "  $($f.Name): $lines discards"
        }
        Write-Host "`nSample discards from most recent file:"
        Get-Content ($files | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName |
            Select-Object -First 10 |
            ForEach-Object { $_ | python -c "import sys,json; d=json.load(sys.stdin); print(f'  [{d[\"table\"]}] {d[\"reason\"]}')" }
    } else {
        Write-Host "  No discard logs found."
    }
} else {
    Write-Host "  No discard directory yet."
}

Write-Host "`n=== Done ===" -ForegroundColor Cyan
