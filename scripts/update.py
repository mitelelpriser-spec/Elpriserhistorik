#!/usr/bin/env python3
"""
Henter day-ahead-priser (DKK/MWh, ekskl. moms og afgifter) fra Energi Data Service og skriver én fil pr. område og måned:

    data/DK1/2026-10.json   {"startUtc": "2026-09-30T22:00:00Z", "step": 15, "v": [pris, pris, ...]}

Pladsen i listen svarer til startUtc + plads * 15 minutter (UTC, så sommertid ikke volder problemer).
Timepriser (før oktober 2025) er udfyldt som fire ens kvarter. Manglende tal er null.
Kun Python 3.9+ standardbibliotek.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://api.energidataservice.dk/dataset/DayAheadPrices"
DK = ZoneInfo("Europe/Copenhagen")
UTC = timezone.utc
AREAS = ["DK1", "DK2"]
MONTHS_BACK = 14  # appen bruger 13 måneder
OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
STEP = timedelta(minutes=15)


def month_bounds(y, m):
    start = datetime(y, m, 1, tzinfo=DK).astimezone(UTC)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = datetime(ny, nm, 1, tzinfo=DK).astimezone(UTC)
    return start, end


def fetch_rows(area, start_utc, end_utc):
    """[(tid_utc, pris)] sorteret. Spørger lidt bredt (hele dage) og filtrerer bagefter på UTC."""
    d0 = (start_utc - timedelta(days=1)).date()
    d1 = (end_utc + timedelta(days=1)).date()
    q = {
        "start": f"{d0}T00:00",
        "end": f"{d1}T00:00",
        "filter": json.dumps({"PriceArea": [area]}),
        "columns": "TimeUTC,DayAheadPriceDKK",
        "sort": "TimeUTC asc",
        "limit": "50000",
    }
    url = API + "?" + urllib.parse.urlencode(q)
    last = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "elpris-data-script"})
            with urllib.request.urlopen(req, timeout=90) as r:
                recs = json.load(r)["records"]
            break
        except urllib.error.HTTPError as e:
            last = e
            wait = 10 * (attempt + 1)
            if e.code == 429:
                # "Rate limit is exceeded. Try again in X seconds." - vent så længe, de beder om
                try:
                    body = e.read().decode("utf-8", "ignore")
                    m = re.search(r"(\d+)\s*second", body)
                    ra = e.headers.get("Retry-After")
                    wait = int(m.group(1)) + 2 if m else (int(ra) + 2 if ra and ra.isdigit() else 30)
                except Exception:  # noqa: BLE001
                    wait = 30
                wait = min(max(wait, 5), 120)
            print(f"  {area}: HTTP {e.code}, venter {wait} s", flush=True)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10 * (attempt + 1))
    else:
        raise RuntimeError(f"Kunne ikke hente {area}: {last}")
    rows = []
    for rec in recs:
        p = rec.get("DayAheadPriceDKK")
        if p is None:
            continue
        t = datetime.fromisoformat(rec["TimeUTC"].replace("Z", "")).replace(tzinfo=UTC)
        if start_utc <= t < end_utc:
            rows.append((t, float(p)))
    rows.sort()
    return rows


def build_slots(rows, start, end):
    n = int((end - start) / STEP)
    arr = [None] * n
    prev_dur = 15
    for i, (t, p) in enumerate(rows):
        dur = int((rows[i + 1][0] - t).total_seconds() // 60) if i + 1 < len(rows) else prev_dur
        prev_dur = dur if dur > 0 else prev_dur
        slots = 4 if dur == 60 else 1
        base = int((t - start) / STEP)
        for k in range(slots):
            if 0 <= base + k < n:
                arr[base + k] = round(p, 2)
    return arr


def is_complete(path, n_expected):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        v = d["v"]
        if len(v) != n_expected:
            return False
        tail = v[-8:]
        return any(x is not None for x in tail)
    except Exception:  # noqa: BLE001
        return False


def months_list(today):
    y, m = today.year, today.month
    out = []
    for _ in range(MONTHS_BACK):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def main():
    today = datetime.now(DK).date()
    months = months_list(today)
    recent = set(months[-2:])  # indeværende og forrige måned hentes altid på ny
    failures = 0
    written = []
    for area in AREAS:
        os.makedirs(os.path.join(OUT, area), exist_ok=True)
        todo = []
        for (y, m) in months:
            path = os.path.join(OUT, area, f"{y}-{m:02d}.json")
            start, end = month_bounds(y, m)
            n = int((end - start) / STEP)
            if (y, m) not in recent and is_complete(path, n):
                written.append(f"{area}/{y}-{m:02d}")
            else:
                todo.append((y, m, path, start, end, n))
        if not todo:
            continue
        # Alle manglende måneder hentes i ÉN forespørgsel pr. område og deles op bagefter (skåner API'et)
        try:
            all_rows = fetch_rows(area, todo[0][3], todo[-1][4])
        except Exception as e:  # noqa: BLE001
            failures += len(todo)
            print(f"FEJL {area}: {e}", file=sys.stderr)
            continue
        for (y, m, path, start, end, n) in todo:
            rows = [r for r in all_rows if start <= r[0] < end]
            if not rows:
                print(f"{area} {y}-{m:02d}: ingen data (springes over)")
                continue
            arr = build_slots(rows, start, end)
            doc = {"area": area, "month": f"{y}-{m:02d}", "startUtc": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "step": 15, "v": arr}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            written.append(f"{area}/{y}-{m:02d}")
            print(f"{area} {y}-{m:02d}: {sum(x is not None for x in arr)}/{n} kvarter")
        time.sleep(5)
    index = {"updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "areas": AREAS, "months": [f"{y}-{m:02d}" for y, m in months]}
    # index.json ændres kun, når listen over måneder ændrer sig – så der ikke laves en ny version hver time
    idx_path = os.path.join(OUT, "index.json")
    old = None
    try:
        with open(idx_path, encoding="utf-8") as f:
            old = json.load(f)
    except Exception:  # noqa: BLE001
        pass
    if not old or old.get("months") != index["months"]:
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f, separators=(",", ":"))
    # Tariffer og afgifter (højst én gang i døgnet); fejl her må ikke vælte spotpriserne
    try:
        import tariffs
        tariffs.main()
    except Exception as e:  # noqa: BLE001
        print(f"ADVARSEL tariffer: {e}", file=sys.stderr)
    if failures:
        print(f"ADVARSEL: {failures} måned(er) kunne ikke hentes; de tages ved næste kørsel.", file=sys.stderr)
    # Delvist resultat er fint (færdige måneder springes over næste gang). Kun fejl hvis intet lykkedes.
    return 1 if failures and not written else 0


if __name__ == "__main__":
    sys.exit(main())
