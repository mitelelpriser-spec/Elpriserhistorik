#!/usr/bin/env python3
"""
Henter tariffer og afgifter fra Energi Data Service (DatahubPricelist) – skånsomt, ét netselskab ad gangen (kører i sin egen workflow, se .github/workflows/tariffs.yml):

    data/grids.json                 liste over netselskaber (GLN, kode, navn, beskrivelse)
    data/tariffs/<GLN>.json         netselskabets C-tarif-rækker
    data/tariffs/energinet.json     Energinets transmissions- og systemtarif og elafgiften

Hver række: {"code": "CD", "from": "2025-01-01T00:00:00", "to": null, "p": [24 timepriser i DKK/kWh ekskl. moms]}.
Første gang hentes 2 år tilbage; derefter højst hver 6. dag pr. fil, kun 60 dage tilbage, flettet ind i de gemte rækker.
Altid 120 dage frem. Pause mellem opslag, og scriptet stopper pænt efter en tidsgrænse (resten tages næste kørsel).
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
EN_CODES = ["EA-001", "40000", "41000"]
OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"))
BACK_FULL = 3650        # første gang pr. selskab: langt tilbage, for DataHub søger på startdato og den gældende række kan være gammel
KEEP_DAYS = 800         # gemte rækker, der sluttede for mere end 800 dage siden, kasseres
META_BACK = 3650        # netselskabslisten bygges ud fra rækker op til 10 år tilbage
GRIDS_VERSION = 7       # hæves, når listereglerne ændres (tvinger ny liste)
BACK_INC = 60           # senere: kun 60 dage tilbage
AHEAD = 120
REFRESH_H = 6 * 24      # hver fil opdateres højst hver 6. dag
SLEEP = 70.0            # pause mellem opslag (Energi Data Service tillader ca. 1 opslag i minuttet)
BUDGET_S = 95 * 60      # stop pænt efter 95 minutter (workflowet har 120 minutter)


def http_json(url):
    last = None
    for attempt in range(6):
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


def query(filter_dict, back, columns=None, limit=100000):
    today = datetime.now(timezone.utc).date()
    q = {
        "start": f"{today - timedelta(days=back)}T00:00",
        "end": f"{today + timedelta(days=AHEAD)}T00:00",
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
    return {"code": str(rec.get("ChargeTypeCode", "")).strip(), "from": str(vf)[:19],
            "to": str(vt)[:19] if vt else None, "p": prices24(rec)}


def parse_dt(s):
    s = s.strip()[:19]
    return datetime.fromisoformat(s if len(s) > 10 else s + "T00:00:00")


def covers_now(rows, code=None):
    now = datetime.now()
    cand = [r for r in rows if (code is None or r["code"].lower() == code.lower()) and parse_dt(r["from"]) <= now]
    if not cand:
        return False
    r = max(cand, key=lambda x: parse_dt(x["from"]))
    return r["to"] is None or now < parse_dt(r["to"])


def merge(old, new):
    d = {(r["code"].lower(), r["from"]): r for r in old}
    for r in new:
        d[(r["code"].lower(), r["from"])] = r
    return sorted(d.values(), key=lambda r: (r["code"], r["from"]))


def is_c_note(note):
    """Noten (små bogstaver) peger på en C-tarif: 'Nettarif C …', 'C-Kunde Nettarif', 'C-tarif' osv."""
    return "nettarif c" in note or "c-kunde" in note or "c kunde" in note or "c-tarif" in note \
        or "0-100000kwh" in note.replace(".", "").replace(" ", "")   # Hurup Elværk: C-kunder = forbrug 0-100.000 kWh


def build_grids(recs):
    """Samme regler som appen: noten skal indeholde 'nettarif c', gældende nu; nyeste række pr. (GLN, kode)."""
    now = datetime.now()
    best = {}
    for o in recs:
        vf = o.get("ValidFrom")
        if not vf:
            continue
        note = str(o.get("Note") or "").strip().lower()
        if not is_c_note(note) or "sanity" in str(o.get("ChargeOwner") or "").lower():
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


def prune(rows):
    """Kasserer rækker, der sluttede for længe siden (de gældende rækker bliver, uanset hvor gamle de er)."""
    cut = datetime.now() - timedelta(days=KEEP_DAYS)
    return [r for r in rows if r["to"] is None or parse_dt(r["to"]) >= cut]


def diagnose(recs, grids):
    """Skriver til loggen, hvilke selskaber med takstposter der IKKE kom på listen, og hvilke noter de har."""
    have = {g["gln"] for g in grids}
    owners = {}
    for o in recs:
        gln = str(o.get("GLN_Number") or "")
        if not gln or gln in have or gln == ENERGINET_GLN:
            continue
        d = owners.setdefault(gln, {"name": str(o.get("ChargeOwner") or "").strip(), "notes": {}, "last": ""})
        if not d["name"]:
            d["name"] = str(o.get("ChargeOwner") or "").strip()
        n = str(o.get("Note") or "").strip()
        d["notes"][n] = d["notes"].get(n, 0) + 1
        if is_c_note(n.lower()):   # hvornår udløb den seneste C-række? (ingen = åben)
            vt = str(o.get("ValidTo") or "åben")[:10]
            if d["last"] == "" or vt == "åben" or (d["last"] != "åben" and vt > d["last"]):
                d["last"] = vt
    if not owners:
        print("  Alle selskaber med takstposter er med på listen.", flush=True)
        return
    print(f"  UDELADT fra listen ({len(owners)} selskaber har takstposter, men ingen 'Nettarif C'):", flush=True)
    for gln, d in sorted(owners.items(), key=lambda kv: kv[1]["name"].lower()):
        notes = ", ".join(f"{k or '(tom)'}×{v}" for k, v in list(d["notes"].items())[:6])
        tail = f"  [seneste C-række gælder til: {d['last']}]" if d["last"] else ""
        print(f"    {d['name'] or gln} ({gln}): {notes}{tail}", flush=True)


def diagnose_extra(recs, grids):
    """Skriver gældende poster for selskaber PÅ listen, som ikke er på listen (fx 'Nettarif 60 kV', 'overordnet net', 'regional').
    Nogle selskaber har flere led, der skal lægges sammen; loggen viser, hvilke koder det drejer sig om."""
    now = datetime.now()
    have = {(g["gln"], g["code"]) for g in grids}
    glns = {g["gln"] for g in grids}
    seen = {}
    for o in recs:
        gln = str(o.get("GLN_Number") or "")
        code = str(o.get("ChargeTypeCode") or "").strip()
        if gln not in glns or (gln, code) in have or not o.get("ValidFrom"):
            continue
        frm = parse_dt(str(o["ValidFrom"]))
        to = parse_dt(str(o["ValidTo"])) if o.get("ValidTo") else None
        if frm > now or (to is not None and to <= now):
            continue
        key = (gln, code)
        if key not in seen or frm > seen[key][0]:
            seen[key] = (frm, to, str(o.get("ChargeOwner") or "").strip(), str(o.get("Note") or "").strip(), str(o.get("Description") or "").strip())
    if not seen:
        return
    print(f"  ØVRIGE gældende poster hos selskaber på listen ({len(seen)}; kan være led, der skal lægges til):", flush=True)
    for (gln, code), (frm, to, name, note, desc) in sorted(seen.items(), key=lambda kv: (kv[1][2].lower(), kv[0][1])):
        print(f"    {name or gln} | {code} | note: {note[:40]} | {desc[:60]} | {str(frm)[:10]}→{str(to)[:10] if to else 'åben'}", flush=True)


EXTRA_WORDS = ("overordnet net", "overliggende net", "nettarif 60 kv", "60kv net", "nettab", "nettarif - energinet", "regional nettarif")
EXTRA_SKIP = ("indfødning", "produktion", "effektbetaling", "rådighed", "egenprod", "samplac", "kollektiv", "bruges ikke")


def build_extras(recs, grids):
    """Ekstra led hos selskaber på listen, som kan skulle lægges oven i C-tariffen (overordnet net, 60 kV, nettab ...).
    Kun poster, der er gyldige nu. Det afgøres bagefter ud fra tallene, hvilke led der faktisk skal bruges."""
    now = datetime.now()
    glns = {g["gln"] for g in grids}
    have = {(g["gln"], g["code"]) for g in grids}
    best = {}
    for o in recs:
        gln = str(o.get("GLN_Number") or "")
        code = str(o.get("ChargeTypeCode") or "").strip()
        if gln not in glns or (gln, code) in have or not o.get("ValidFrom"):
            continue
        text = (str(o.get("Note") or "") + " " + str(o.get("Description") or "")).lower()
        if not any(w in text for w in EXTRA_WORDS) or any(w in text for w in EXTRA_SKIP):
            continue
        frm = parse_dt(str(o["ValidFrom"]))
        to = parse_dt(str(o["ValidTo"])) if o.get("ValidTo") else None
        if frm > now or (to is not None and to <= now):
            continue
        key = (gln, code)
        if key not in best or frm > best[key][0]:
            best[key] = (frm, {"gln": gln, "code": code, "name": str(o.get("ChargeOwner") or "").strip(),
                               "note": str(o.get("Note") or "").strip(), "desc": str(o.get("Description") or "").strip()})
    return sorted((b[1] for b in best.values()), key=lambda e: (e["name"].lower(), e["code"]))


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def age_hours(obj):
    try:
        upd = datetime.strptime(obj["updated"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - upd).total_seconds() / 3600
    except Exception:  # noqa: BLE001
        return 1e9


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    t0 = time.time()
    tdir = os.path.join(OUT, "tariffs")
    grids_path = os.path.join(OUT, "grids.json")
    gj = read_json(grids_path)
    first_request = True

    def pause():
        nonlocal first_request
        if not first_request:
            time.sleep(SLEEP)
        first_request = False

    # 1) netselskabslisten (kun hvis den mangler eller er over 6 dage gammel)
    if gj and gj.get("v") == GRIDS_VERSION and age_hours(gj) < REFRESH_H and len(gj.get("grids", [])) >= 10:
        grids = gj["grids"]
        extras = gj.get("extras", [])
        print(f"Netselskabslisten er frisk ({len(grids)} koder).", flush=True)
    else:
        print("Henter netselskaber …", flush=True)
        pause()
        cols = "GLN_Number,ChargeOwner,ChargeTypeCode,Note,Description,ValidFrom,ValidTo"
        # Først de seneste 800 dage (virkede og gav 48 koder); derefter 10 år tilbage som tillæg, så selskaber med gamle gældende
        # rækker kommer med. Svigter eller afkortes den lange forespørgsel, har vi stadig den korte.
        meta = query({"ChargeType": ["D03"]}, 800, columns=cols)
        try:
            pause()
            meta = meta + query({"ChargeType": ["D03"]}, META_BACK, columns=cols)
        except Exception as e:  # noqa: BLE001
            print(f"  Den lange forespørgsel (10 år) mislykkedes ({e}) – bruger kun de seneste 800 dage.", flush=True)
        grids = build_grids(meta)
        print(f"  {len(meta)} takstposter læst.", flush=True)
        diagnose(meta, grids)
        diagnose_extra(meta, grids)
        extras = build_extras(meta, grids)
        print(f"  EKSTRA led der hentes ({len(extras)}):", flush=True)
        for e in extras:
            print(f"    {e['name']} | {e['code']} | {e['note'][:45]}", flush=True)
        if len(grids) < 10:
            raise RuntimeError(f"Kun {len(grids)} netselskaber fundet – dataene ser forkerte ud, springer over.")
        write_json(grids_path, {"v": GRIDS_VERSION, "updated": now_iso(), "grids": grids, "extras": extras})
        print(f"  {len(grids)} netselskaber (koder).", flush=True)

    # 2) Energinet + hvert netselskab, ét opslag ad gangen
    jobs = [(ENERGINET_GLN, EN_CODES, os.path.join(tdir, "energinet.json"), "Energinet")]
    by_gln = {}
    names = {}
    for g in grids:
        by_gln.setdefault(g["gln"], []).append(g["code"])
        names[g["gln"]] = g["name"]
    for e in extras:   # ekstra led hentes sammen med selskabets øvrige koder (samme fil)
        if e["code"] not in by_gln.get(e["gln"], []):
            by_gln.setdefault(e["gln"], []).append(e["code"])
    for gln, codes in by_gln.items():
        jobs.append((gln, codes, os.path.join(tdir, f"{gln}.json"), names[gln]))

    done = skipped = 0
    failed = []
    for gln, codes, path, name in jobs:
        ex = read_json(path)
        old_rows = ex.get("rows", []) if ex else []
        have_all = all(any(r["code"].lower() == c.lower() for r in old_rows) for c in codes)
        if ex and have_all and age_hours(ex) < REFRESH_H and all(covers_now(old_rows, c) for c in codes):
            skipped += 1
            continue
        if time.time() - t0 > BUDGET_S:
            failed.append(f"{name}: tidsgrænse nået – tages næste kørsel")
            continue
        pause()
        try:
            recs = query({"GLN_Number": [gln], "ChargeTypeCode": codes}, BACK_INC if have_all else BACK_FULL)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{name}: {e}")
            continue
        new_rows = [r for r in (to_row(x) for x in recs) if r]
        rows = prune(merge(old_rows, new_rows))
        if not rows:
            failed.append(f"{name}: ingen rækker")
            continue
        write_json(path, {"gln": gln, "updated": now_iso(), "rows": rows})
        done += 1
        print(f"  {name}: {len(rows)} rækker", flush=True)

    print(f"Hentet {done}, sprunget over {skipped}, mislykket {len(failed)} (af {len(jobs)}).")
    for f in failed:
        print("ADVARSEL:", f, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
