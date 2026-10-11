#!/usr/bin/env python3
"""Offerte Coop dell'Ipercoop Cesena Lungo Savio sui prodotti che compro.

Legge i volantini del negozio (API pubblica di coopalleanza3-0.it), tiene solo
le offerte dei gruppi in lista.json, ne conserva lo storico in SQLite, genera
index.html e manda una notifica macOS quando compaiono offerte nuove.
Per confrontare le offerte scarica anche il listino easycoop.com (stessa
cooperativa, stessi EAN) delle stesse categorie: i prezzi normali del negozio
non sono pubblici, quelli online sono uguali o un po' piu' alti.

    python3 coop.py            # offerte + report + notifica; listino se ha piu' di 7 giorni
    python3 coop.py listino    # forza anche il listino
    python3 coop.py report     # solo rigenera index.html
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "offerte.db"
LIST_PATH = HERE / "lista.json"
HTML_PATH = HERE / "index.html"
TEMPLATE_PATH = HERE / "template.html"

API = "https://svdgt.coopalleanza3-0.it/apim/"
EASYCOOP = "https://www.easycoop.com/graphql"
EASYCOOP_STORE = "c_901"   # Coop Alleanza 3.0 online store (Emilia/Veneto)
SLEEP = 0.3            # pause between requests, be polite
HISTORY_DAYS = 365     # how far back the report shows expired offers
LISTINO_DAYS = 7       # regular prices change rarely: refresh the reference catalogue weekly
QUALITA_DAYS = 90      # Open Food Facts data changes rarely
OFF_API = "https://world.openfoodfacts.org/api/v2/product/"
OFF_PAUSE = 1.5        # OFF throttles bursts with HTTP 429

PRICE_RE = re.compile(r"(\d+(?:\.\d{3})*,\d{2})\s*€")


def fetch(req, retries=4):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            if "errors" in data:
                raise RuntimeError(data["errors"][0]["message"])
            time.sleep(SLEEP)
            return data
        except Exception as e:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt * 3
            print(f"  errore ({e}), riprovo tra {wait}s", file=sys.stderr)
            time.sleep(wait)


UA = "Mozilla/5.0 (coop-cesena personal tracker)"


def get(url):
    return fetch(urllib.request.Request(url, headers={"User-Agent": UA}))


def gql(query):
    return fetch(urllib.request.Request(EASYCOOP, data=json.dumps({"query": query}).encode(), headers={
        "Content-Type": "application/json", "Store": EASYCOOP_STORE, "User-Agent": UA}))["data"]


def load_list():
    cfg = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    season = re.compile(cfg["stagione"][str(date.today().month)], re.I)
    groups = [{
        "nome": g["nome"],
        "cerca": re.compile(g["cerca"], re.I),
        "escludi": re.compile(g["escludi"], re.I) if g.get("escludi") else None,
        "categorie": set(g.get("categorie_volantino", cfg["categorie_volantino"])),
        "stagione": season if g.get("stagione") else None,
        "easycoop": {
            "categorie": g["easycoop"]["categorie"],
            "includi": re.compile(g["easycoop"]["includi"], re.I) if g["easycoop"].get("includi") else None,
            "escludi": re.compile(g["easycoop"]["escludi"], re.I) if g["easycoop"].get("escludi") else None,
        },
    } for g in cfg["gruppi"]]
    return cfg["negozio"], groups


def match_group(groups, promo, text):
    for g in groups:
        if (promo["categoryCode"] in g["categorie"]
                and g["cerca"].search(text)
                and not (g["escludi"] and g["escludi"].search(text))
                and not (g["stagione"] and not g["stagione"].search(text))):
            return g["nome"]
    return None


def eur(s):
    m = PRICE_RE.search(s)
    return float(m.group(1).replace(".", "").replace(",", ".")) if m else None


def box(promo, key):
    return [t for t in ((promo.get(key) or {}).get("txt") or []) if t]


def parse_price(promo):
    """Price labels look like ['~1,99 €~', '1,39 €', '15,44 € al kg'] or ['1,49 € Per tutti', '1,34 €', ...]."""
    out = {"prezzo": None, "prezzo_pieno": None, "per_tutti": None, "prezzo_unita": None, "unita": ""}
    for t in box(promo, "DXTop") + box(promo, "DXBottom"):
        v = eur(t)
        if v is None:
            continue
        if t.strip().startswith("~"):
            out["prezzo_pieno"] = v
        elif "per tutti" in t.lower():
            out["per_tutti"] = v
        elif " al " in t:
            out["prezzo_unita"] = v
            out["unita"] = t.split(" al ", 1)[1].strip()
        elif out["prezzo"] is None:
            out["prezzo"] = v
    labels = " ".join(t for k in ("SXTop", "CXTop", "SXBottom", "CXBottom") for t in box(promo, k))
    m = re.search(r"-(\d+)%", labels)
    out["sconto"] = int(m.group(1)) if m else None
    out["soci"] = 1 if re.search(r"\bsoci", labels, re.I) else 0
    return out


def iso(d):
    return datetime.strptime(d, "%d/%m/%Y").date().isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS offerte (
  volantino_id TEXT, promo_id TEXT, volantino TEXT,
  valido_da TEXT, valido_a TEXT, validita TEXT,
  gruppo TEXT, nome TEXT, formato TEXT, marca TEXT,
  prezzo REAL, prezzo_pieno REAL, per_tutti REAL, prezzo_unita REAL, unita TEXT,
  sconto INTEGER, soci INTEGER, img TEXT, ean TEXT,
  first_seen TEXT, last_seen TEXT,
  PRIMARY KEY (volantino_id, promo_id)
);
-- easycoop reference catalogue (online prices, used only for comparison)
CREATE TABLE IF NOT EXISTS listino (
  sku TEXT PRIMARY KEY, ean TEXT, nome TEXT, marca TEXT, gruppo TEXT, sottocategoria TEXT,
  prezzo REAL, prezzo_attuale REAL, quantita REAL, unita TEXT, url_key TEXT, img TEXT,
  first_seen TEXT, last_seen TEXT
);
-- one row per change of the online regular price
CREATE TABLE IF NOT EXISTS listino_prezzi (
  sku TEXT, date TEXT, prezzo REAL, PRIMARY KEY (sku, date)
);
-- Open Food Facts health data by EAN (nutriscore a-e, nova 1-4 = degree of processing)
CREATE TABLE IF NOT EXISTS qualita (
  ean TEXT PRIMARY KEY, trovato INTEGER, nutriscore TEXT, nova INTEGER, additivi INTEGER, bio INTEGER,
  aggiornato TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  date TEXT PRIMARY KEY, offerte INTEGER, nuove INTEGER, finished TEXT
);
"""


def connect():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    return db


def aggiorna():
    today = date.today().isoformat()
    store, groups = load_list()
    db = connect()
    first_run = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    known = {(v, p) for v, p in db.execute("SELECT volantino_id, promo_id FROM offerte")}

    print(f"Volantini Ipercoop Cesena Lungo Savio ({today})...")
    rows = []
    for lf in get(f"{API}leaflets/{store}")["leaflets"]:
        promos = get(f"{API}{lf['id']}/{store}/promos?")["promos"]
        n = 0
        for p in promos:
            text = " ".join(x for x in (p["desc_promo"], p["desc_promo2"], p["brand"]) if x)
            gruppo = match_group(groups, p, text)
            if not gruppo:
                continue
            n += 1
            price = parse_price(p)
            sold_by = re.search(r"\bal (kg|litro)\b", p["desc_promo2"] or "")
            if sold_by and price["prezzo_unita"] is None:   # price is already per kg/litre
                price["prezzo_unita"], price["unita"] = price["prezzo"], sold_by.group(1)
            rows.append({
                "volantino_id": lf["id"], "promo_id": p["id"], "volantino": lf["titolo"],
                "valido_da": iso(lf["valido_da"]), "valido_a": iso(lf["valido_a"]),
                "validita": p.get("promo_pretty_name_validita") or lf["pretty_name_validita"],
                "gruppo": gruppo, "nome": p["desc_promo"].strip(), "formato": (p["desc_promo2"] or "").strip(),
                "marca": p["brand"] or "", "img": p.get("img") or "", "ean": ",".join(p.get("prodotti") or []),
                **price,
            })
        print(f"  {lf['titolo']:<28} {lf['valido_da']}-{lf['valido_a']}  {len(promos):4d} offerte, {n:3d} per te")

    new = [r for r in rows if (r["volantino_id"], r["promo_id"]) not in known]
    with db:
        for r in rows:
            db.execute("""
                INSERT INTO offerte VALUES
                  (:volantino_id,:promo_id,:volantino,:valido_da,:valido_a,:validita,:gruppo,:nome,:formato,:marca,
                   :prezzo,:prezzo_pieno,:per_tutti,:prezzo_unita,:unita,:sconto,:soci,:img,:ean,:today,:today)
                ON CONFLICT(volantino_id, promo_id) DO UPDATE SET
                  gruppo=excluded.gruppo, prezzo=excluded.prezzo, prezzo_pieno=excluded.prezzo_pieno,
                  per_tutti=excluded.per_tutti, prezzo_unita=excluded.prezzo_unita, unita=excluded.unita,
                  sconto=excluded.sconto, soci=excluded.soci, validita=excluded.validita,
                  valido_a=excluded.valido_a, last_seen=excluded.last_seen
            """, {**r, "today": today})
        db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?)",
                   (today, len(rows), len(new), datetime.now().isoformat(timespec="seconds")))
    print(f"Fatto: {len(rows)} offerte per te, {len(new)} nuove.")
    if new and not first_run:
        notifica(new)


def notifica(new):
    def label(r):
        price = f" {r['prezzo']:.2f} €".replace(".", ",") if r["prezzo"] else ""
        return f"{r['nome'].title()}{price}"
    body = "; ".join(label(r) for r in new[:3]) + (f" e altre {len(new) - 3}" if len(new) > 3 else "")
    title = f"{len(new)} nuove offerte Coop per te"
    # text goes in as argv so no AppleScript quoting is needed
    subprocess.run(["osascript", "-e", "on run argv",
                    "-e", 'display notification (item 1 of argv) with title (item 2 of argv) sound name "default"',
                    "-e", "end run", body, title], check=False)


EASYCOOP_FIELDS = """
  sku name url_key small_image { url }
  price_range { minimum_price { regular_price { value } final_price { value } } }
  custom_attributesV2(filters: {is_visible_on_front: true}) {
    items { code
      ... on AttributeValue { value }
      ... on AttributeSelectedOptions { selected_options { label } }
    }
  }
"""


def easycoop_categories(ids):
    ids_gql = ", ".join(f'"{i}"' for i in ids)
    items, page = [], 1
    while True:
        d = gql(f"{{ products(filter: {{category_id: {{in: [{ids_gql}]}}}}, pageSize: 100, currentPage: {page}, "
                f"sort: {{price: ASC}}) {{ total_count items {{ {EASYCOOP_FIELDS} }} }} }}")["products"]
        items += d["items"]
        if len(items) >= d["total_count"] or not d["items"]:
            return items
        page += 1


def fix_quantity(name, qty, unit):
    """Variable-weight packs ("250 G Ca") come with 1 kg as quantity but a per-pack price;
    multi-piece packs ("10 Pz") come with 1 piece."""
    pcs = re.search(r"\b(\d+)\s*(pz|pezzi|pcs)\b", name, re.I)
    if qty == 1.0 and unit.upper() == "PZ" and pcs and int(pcs.group(1)) > 1:
        return float(pcs.group(1))
    m = (re.search(r"\b(\d+(?:[.,]\d+)?)\s*(kg|g)\b", name, re.I)          # "250 G Ca"
         or re.search(r"\b(kg|g)\s*(\d+(?:[.,]\d+)?)\b", name, re.I))       # fruit style "G 600 Ca"
    if qty == 1.0 and unit.upper() == "KG" and m and not re.search(r"\d\s*[x×]\s*\d", name, re.I):
        num, um = (m.group(1), m.group(2)) if m.group(1)[0].isdigit() else (m.group(2), m.group(1))
        v = float(num.replace(",", "."))
        v = v / 1000 if um.lower() == "g" else v
        if v and abs(v - 1) > 0.01:
            return v
    return qty


def listino_stale():
    last = connect().execute("SELECT MAX(last_seen) FROM listino").fetchone()[0]
    return not last or date.fromisoformat(last) <= date.today() - timedelta(days=LISTINO_DAYS)


def listino():
    """Online catalogue of the same groups, the yardstick for judging an offer."""
    today = date.today().isoformat()
    _, groups = load_list()
    print("Listino di riferimento easycoop.com...")
    picked = {}
    for g in groups:
        ec, n = g["easycoop"], 0
        for it in easycoop_categories(ec["categorie"]):
            name = it["name"].strip()
            if (it["sku"] in picked
                    or (ec["includi"] and not ec["includi"].search(name))
                    or (ec["escludi"] and ec["escludi"].search(name))
                    or (g["stagione"] and not g["stagione"].search(name))):
                continue
            attrs = {}
            for a in it["custom_attributesV2"]["items"]:
                attrs[a["code"]] = (", ".join(o["label"] for o in a["selected_options"] or [])
                                    if "selected_options" in a else a.get("value"))
            mp = it["price_range"]["minimum_price"]
            try:
                qty = float(attrs.get("misura_prezzo_chiaro") or 0) or None
            except ValueError:
                qty = None
            qty = fix_quantity(name, qty, attrs.get("tipo_misura") or "")
            picked[it["sku"]] = {
                "sku": it["sku"], "ean": attrs.get("ean") or "", "nome": name, "marca": attrs.get("manufacturer") or "",
                "gruppo": g["nome"], "sottocategoria": attrs.get("category_id_third_level") or "",
                "prezzo": mp["regular_price"]["value"], "prezzo_attuale": mp["final_price"]["value"],
                "quantita": qty, "unita": attrs.get("tipo_misura") or "", "url_key": it["url_key"],
                "img": (it.get("small_image") or {}).get("url") or "",
            }
            n += 1
        print(f"  {g['nome']:<32} {n:4d} prodotti")

    db = connect()
    last = dict(db.execute("SELECT sku, prezzo FROM listino_prezzi p WHERE date = "
                           "(SELECT MAX(date) FROM listino_prezzi WHERE sku = p.sku)"))
    with db:
        for r in picked.values():
            db.execute("""
                INSERT INTO listino VALUES
                  (:sku,:ean,:nome,:marca,:gruppo,:sottocategoria,:prezzo,:prezzo_attuale,:quantita,:unita,
                   :url_key,:img,:today,:today)
                ON CONFLICT(sku) DO UPDATE SET
                  ean=excluded.ean, nome=excluded.nome, marca=excluded.marca, gruppo=excluded.gruppo,
                  sottocategoria=excluded.sottocategoria, prezzo=excluded.prezzo,
                  prezzo_attuale=excluded.prezzo_attuale, quantita=excluded.quantita, unita=excluded.unita,
                  url_key=excluded.url_key, img=excluded.img, last_seen=excluded.last_seen
            """, {**r, "today": today})
            if last.get(r["sku"]) != r["prezzo"]:
                db.execute("INSERT OR REPLACE INTO listino_prezzi VALUES (?,?,?)", (r["sku"], today, r["prezzo"]))
    print(f"Fatto: {len(picked)} prodotti nel listino.")


def qualita():
    """Nutri-Score / NOVA from Open Food Facts for EANs not yet known (or older than QUALITA_DAYS)."""
    db = connect()
    today = date.today()
    fresh = {e for (e,) in db.execute("SELECT ean FROM qualita WHERE aggiornato > ?",
                                      ((today - timedelta(days=QUALITA_DAYS)).isoformat(),))}
    cfg = json.loads(LIST_PATH.read_text(encoding="utf-8"))
    food = [g["nome"] for g in cfg["gruppi"] if g.get("alimentare", True)]
    marks = ",".join("?" * len(food))
    eans = {e for (e,) in db.execute(
        f"SELECT ean FROM listino WHERE ean <> '' AND gruppo IN ({marks}) "
        "AND last_seen = (SELECT MAX(last_seen) FROM listino)", food)}
    for (e,) in db.execute(f"SELECT ean FROM offerte WHERE valido_a >= ? AND ean <> '' AND gruppo IN ({marks})",
                           (today.isoformat(), *food)):
        eans.update(e.split(","))
    todo = sorted(eans - fresh)
    # EANs starting with 2 are in-store codes (weighed products): not in Open Food Facts
    instore = [e for e in todo if e.startswith("2")]
    db.executemany("INSERT OR REPLACE INTO qualita VALUES (?,0,NULL,NULL,NULL,0,?)",
                   [(e, today.isoformat()) for e in instore])
    db.commit()
    todo = [e for e in todo if not e.startswith("2")]
    if not todo:
        return
    print(f"Qualità Open Food Facts: {len(todo)} prodotti da controllare (~{len(todo) * OFF_PAUSE / 60:.0f} min)...")
    found = 0
    for i, ean in enumerate(todo, 1):
        req = urllib.request.Request(f"{OFF_API}{ean}?fields=nutriscore_grade,nova_group,additives_n,labels_tags",
                                     headers={"User-Agent": "coop-cesena/1.0 (personal price tracker)"})
        p = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    p = json.load(r).get("product") or {}
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    p = None
                    break
                time.sleep(60 if e.code in (429, 503) else 10)
            except Exception:
                time.sleep(10)
        else:
            print(f"  Open Food Facts non risponde, riprendo al prossimo aggiornamento ({i - 1}/{len(todo)} fatti)")
            return
        ns = (p or {}).get("nutriscore_grade")
        db.execute("INSERT OR REPLACE INTO qualita VALUES (?,?,?,?,?,?,?)", (
            ean, 1 if p is not None else 0, ns if ns in ("a", "b", "c", "d", "e") else None,
            (p or {}).get("nova_group"), (p or {}).get("additives_n"),
            1 if "en:organic" in ((p or {}).get("labels_tags") or []) else 0, today.isoformat()))
        found += p is not None
        if i % 25 == 0:
            db.commit()
            print(f"  {i}/{len(todo)}")
        time.sleep(OFF_PAUSE)
    db.commit()
    print(f"Fatto: {found}/{len(todo)} trovati su Open Food Facts.")


def key(nome, formato):
    return re.sub(r"\W+", " ", f"{nome} {formato}".lower()).strip()


def report():
    db = connect()
    last_run = db.execute("SELECT MAX(date) FROM runs").fetchone()[0]
    if not last_run:
        sys.exit("Nessun dato: lancia prima `python3 coop.py`.")
    since = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    cols = ["volantino_id", "volantino", "valido_da", "valido_a", "validita", "gruppo", "nome", "formato",
            "marca", "prezzo", "prezzo_pieno", "per_tutti", "prezzo_unita", "unita", "sconto", "soci",
            "img", "ean", "first_seen", "promo_id"]
    rows = [dict(zip(cols, r)) for r in db.execute(
        f"SELECT {', '.join(cols)} FROM offerte WHERE valido_a >= ? ORDER BY valido_da", (since,))]

    # past occurrences of the same product, to tell whether today's offer is a good one
    seen = {}
    for r in rows:
        seen.setdefault(key(r["nome"], r["formato"]), []).append([r["valido_da"], r["prezzo"]])
    for r in rows:
        r["storia"] = [h for h in seen[key(r["nome"], r["formato"])] if h[0] != r["valido_da"]]

    for r in rows:
        r["ean"] = r["ean"].split(",") if r["ean"] else []

    last_listino = db.execute("SELECT MAX(last_seen) FROM listino").fetchone()[0]
    lcols = ["sku", "ean", "nome", "marca", "gruppo", "sottocategoria", "prezzo", "prezzo_attuale",
             "quantita", "unita", "url_key", "img"]
    listino_rows = [dict(zip(lcols, r)) for r in db.execute(
        f"SELECT {', '.join(lcols)} FROM listino WHERE last_seen = ?", (last_listino,))]
    # regular price over time, for the per-product chart
    storico = {}
    for sku, d, prezzo in db.execute("SELECT sku, date, prezzo FROM listino_prezzi ORDER BY date"):
        storico.setdefault(sku, []).append([d, prezzo])
    for x in listino_rows:
        x["storico"] = storico.get(x["sku"], [])

    q = {e: [ns, nova, add, bio] for e, ns, nova, add, bio in db.execute(
        "SELECT ean, nutriscore, nova, additivi, bio FROM qualita WHERE trovato = 1")}
    for x in listino_rows:
        x["q"] = q.get(x["ean"])
    for r in rows:
        r["q"] = next((q[e] for e in r["ean"] if e in q), None)

    groups = [g["nome"] for g in json.loads(LIST_PATH.read_text(encoding="utf-8"))["gruppi"]]
    first = db.execute("SELECT MIN(date) FROM runs").fetchone()[0]
    finished = db.execute("SELECT finished FROM runs WHERE date = ?", (last_run,)).fetchone()[0]
    meta = {"updated": last_run, "updated_at": (finished or "")[11:16], "today": date.today().isoformat(),
            "since": first, "groups": groups, "listino": last_listino}
    payload = json.dumps({"meta": meta, "rows": rows, "listino": listino_rows},
                         ensure_ascii=False, separators=(",", ":"))
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"Report: {HTML_PATH} ({len(rows)} offerte, {len(listino_rows)} prodotti nel listino)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tutto"
    if cmd not in ("tutto", "listino", "report"):
        sys.exit(__doc__)
    if cmd != "report":
        aggiorna()
    report()   # fresh offers on the page right away; the catalogue below can take minutes
    if cmd == "listino" or listino_stale():
        listino()
        report()
    if cmd != "report":
        qualita()
        report()
