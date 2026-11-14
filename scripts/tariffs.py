#!/usr/bin/env python3
"""
Henter tariffer og afgifter fra Energi Data Service (DatahubPricelist) og skriver dem som små filer:

    data/grids.json                 liste over netselskaber (GLN, kode, navn, beskrivelse)
    data/tariffs/<GLN>.json         netselskabets C-tarif-rækker (alle koder for selskabet)
    data/tariffs/energinet.json     Energinets transmissions- og systemtarif og elafgiften

Hver række: {"code": "CD", "from": "2025-01-01T00:00:00", "to": null, "p": [24 timepriser i DKK/kWh ekskl. moms]}.
Data hentes højst en gang i døgnet (tariffer ændrer sig sjældent) og med 120 dage frem, så kommende ændringer kan ses i god tid.
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
from datetime import datetime, timedelta, timezone

API = "https://api.energidataservice.dk/dataset/DatahubPricelist"
ENERGINET_GLN = "5790000432752"
OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
MAX_AGE_H = 20          # tariffer hentes højst hver 20. time
MAX_SINGLE = 80         # højst så mange enkeltopslag pr. kørsel (for selskaber der ikke er med i samlet opslag)
SLEEP = 3.0             # pause mellem opslag


def http_json(url):
    last = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "elpris-data-script"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            wait = 10 * (attempt + 1)
            if e.code == 429:
                try:
                    body = e.read().decode("utf-8", "ignore")
                    m = re.search(r"(\d+)\s*second", body)
                    ra = e.headers.get("Retry-After")
                    wait = int(m.group(1)) + 2 if m else (int(ra) + 2 if ra and ra.isdigit() else 30)
                except Exception:  # noqa: BLE001
                    wait = 30
                wait = min(max(wait, 5), 120)
            print(f"  HTTP {e.code}, venter {wait} s", flush=True)
            time.sleep(wait)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Kunne ikke hente: {last}")


def query(filter_dict, columns=None, limit=100000):
    today = datetime.now(timezone.utc).date()
    q = {
        "start": f"{today - timedelta(days=800)}T00:00",
        "end": f"{today + timedelta(days=120)}T00:00",
        "filter": json.dumps(filter_dict),
        "timezone": "dk",
        "limit": str(limit),
    }
    if columns:
        q["columns"] = columns
    return http_json(API + "?" + urllib.parse.urlencode(q))["records"]


def prices24(rec):
    out = []
    p1 = rec.get("Price1")
    for h in range(1, 25):
        v = rec.get(f"Price{h}")
        if v is None:
            v = p1 if p1 is not None else 0.0   # flad takst: Price1 gælder alle timer
        out.append(round(float(v), 6))
    return out


def to_row(rec):
    vf = rec.get("ValidFrom")
    if not vf:
        return None
    vt = rec.get("ValidTo")
    return {
        "code": str(rec.get("ChargeTypeCode", "")).strip(),
        "from": str(vf)[:19],
        "to": str(vt)[:19] if vt else None,
        "p": prices24(rec),
    }


def parse_dt(s):
    s = s.strip()[:19]
    return datetime.fromisoformat(s if len(s) > 10 else s + "T00:00:00")


def covers_now(rows):
    now = datetime.now()
    cand = [r for r in rows if parse_dt(r["from"]) <= now]
    if not cand:
        return False
    r = max(cand, key=lambda x: parse_dt(x["from"]))
    return r["to"] is None or now < parse_dt(r["to"])


def build_grids(recs):
    """Samme regler som appen: noten skal indeholde 'nettarif c', gældende nu; nyeste række pr. (GLN, kode)."""
    now = datetime.now()
    best = {}
    for o in recs:
        vf = o.get("ValidFrom")
        if not vf:
            continue
        note = str(o.get("Note") or "").strip().lower()
        if "nettarif c" not in note:
            continue
        gln = str(o.get("GLN_Number") or "")
        code = str(o.get("ChargeTypeCode") or "").strip()
        if not gln or not code:
            continue
        frm = parse_dt(str(vf))
        if frm > now:
            continue
        to = parse_dt(str(o["ValidTo"])) if o.get("ValidTo") else None
        key = (gln, code)
        old = best.get(key)
        if old is None or frm > old["from"]:
            best[key] = {"from": frm, "to": to, "gln": gln, "code": code,
                         "name": (str(o.get("ChargeOwner") or "").strip() or gln),
                         "desc": str(o.get("Description") or "").strip()}
    out = [b for b in best.values() if b["to"] is None or b["to"] > now]
    out.sort(key=lambda b: b["name"].lower())
    return [{"gln": b["gln"], "code": b["code"], "name": b["name"], "desc": b["desc"]} for b in out]


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def fresh_enough():
    try:
        with open(os.path.join(OUT, "grids.json"), encoding="utf-8") as f:
            d = json.load(f)
        upd = datetime.strptime(d["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        ok_files = os.path.exists(os.path.join(OUT, "tariffs", "energinet.json"))
        return ok_files and datetime.now(timezone.utc) - upd < timedelta(hours=MAX_AGE_H)
    except Exception:  # noqa: BLE001
        return False


def main():
    if fresh_enough():
        print("Tariffer er friske nok (springes over).")
        return 0
    print("Henter netselskaber …", flush=True)
    meta = query({"ChargeType": ["D03"]}, columns="GLN_Number,ChargeOwner,ChargeTypeCode,Note,Description,ValidFrom,ValidTo")
    grids = build_grids(meta)
    if len(grids) < 10:
        raise RuntimeError(f"Kun {len(grids)} netselskaber fundet – dataene ser forkerte ud, springer over.")
    print(f"  {len(grids)} netselskaber (koder).", flush=True)

    time.sleep(SLEEP)
    print("Henter nettariffer (samlet) …", flush=True)
    bulk = query({"ChargeType": ["D03"], "Note": ["Nettarif C"]})
    by_key = {}
    for rec in bulk:
        row = to_row(rec)
        if row:
            by_key.setdefault((str(rec.get("GLN_Number") or ""), row["code"]), []).append(row)

    time.sleep(SLEEP)
    print("Henter Energinets tariffer og elafgift …", flush=True)
    en = query({"GLN_Number": [ENERGINET_GLN], "ChargeTypeCode": ["EA-001", "40000", "41000"]})
    en_rows = [r for r in (to_row(x) for x in en) if r]
    codes = {r["code"] for r in en_rows}
    if not {"EA-001", "40000", "41000"} <= codes:
        raise RuntimeError(f"Energinets satser mangler (fandt {sorted(codes)}) – springer over.")
    covered = {c for c in codes if covers_now([r for r in en_rows if r["code"] == c])}
    print(f"  Energinet: {len(en_rows)} rækker, gældende nu: {sorted(covered)}", flush=True)

    per_gln = {}
    single = 0
    failed = []
    for g in grids:
        key = (g["gln"], g["code"])
        rows = by_key.get(key, [])
        if not covers_now(rows) and single < MAX_SINGLE:
            single += 1
            time.sleep(SLEEP)
            try:
                recs = query({"GLN_Number": [g["gln"]], "ChargeTypeCode": [g["code"]]})
                rows = [r for r in (to_row(x) for x in recs) if r]
            except Exception as e:  # noqa: BLE001
                failed.append(f"{g['name']} ({g['code']}): {e}")
                continue
        if rows:
            per_gln.setdefault(g["gln"], []).extend(rows)
    if single:
        print(f"  {single} selskaber hentet enkeltvis.", flush=True)

    for gln, rows in per_gln.items():
        rows.sort(key=lambda r: (r["code"], r["from"]))
        write_json(os.path.join(OUT, "tariffs", f"{gln}.json"), {"gln": gln, "rows": rows})
    write_json(os.path.join(OUT, "tariffs", "energinet.json"), {"gln": ENERGINET_GLN, "rows": sorted(en_rows, key=lambda r: (r["code"], r["from"]))})
    write_json(os.path.join(OUT, "grids.json"), {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grids": grids,
    })
    print(f"Skrev {len(per_gln)} netselskabsfiler + energinet.json + grids.json.")
    for f in failed:
        print("ADVARSEL:", f, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
