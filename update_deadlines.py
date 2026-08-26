#!/usr/bin/env python3
"""
update_deadlines.py - aggiornatore delle deadline CFP per il Quantum CFP Tracker.

Principi di progetto (pensati per durare negli anni, senza toccare il codice):

1.  ZERO DATE HARDCODED. Nessuna data, nessun anno e nessuna edizione sono
    scritti nel sorgente: l'anno corrente viene da datetime.date.today() e le
    edizioni candidate sono sempre {anno, anno+1, anno+2}. A gennaio 2028 lo
    script cerchera' da solo le pagine 2028/2029/2030.

2.  UN SOLO PARSER GENERICO, NON UNO PER CONFERENZA. Le pagine CFP cambiano
    HTML in continuazione, ma dicono tutte la stessa cosa: una riga con una
    data e accanto una parola tipo "deadline" / "submission" / "due".
    Il parser lavora su righe logiche (righe di tabella, <li>, paragrafi) e
    riconosce ~8 formati di data. Per conferenza si configurano solo gli URL.

3.  MAI SOVRASCRIVERE UN DATO BUONO CON UNO PEGGIORE. Ogni record ha una
    confidenza (official > inferred > tbd). Un record esistente viene
    rimpiazzato solo da uno di confidenza uguale o maggiore.

4.  FALLIRE PIANO, MAI DI COLPO. Se un sito e' giu', cambia dominio o non
    contiene date, il vecchio valore resta nel data.json e viene marcato
    "da verificare". Nessuna eccezione fa saltare l'intero run.

5.  URL MODIFICABILI SENZA TOCCARE IL CODICE. Se una conferenza cambia
    dominio (tipico di QTML, che ogni anno e' ospitata da un'universita'
    diversa), basta creare sources.local.json accanto a questo file:

        { "QTML": { "homes": ["https://qtml2028.example.edu/"] } }

Uso:
    python update_deadlines.py                 # aggiorna data.json
    python update_deadlines.py --dry-run -v    # mostra cosa farebbe
    python update_deadlines.py --only QIP TQC  # solo alcune conferenze
    python update_deadlines.py --offline       # usa solo la cache locale
    python update_deadlines.py --selftest      # test del parser, niente rete
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Servono 'requests' e 'beautifulsoup4':  pip install requests beautifulsoup4")

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data.json"
CACHE = ROOT / ".cache"
OVERRIDES = ROOT / "sources.local.json"
USER_AGENT = "quantum-cfp-tracker/2.0 (personal deadline dashboard)"

TODAY = datetime.date.today()


# --------------------------------------------------------------------------
# 1. Configurazione delle conferenze: solo URL, niente date.
#    {year} viene sostituito con ognuno degli anni candidati.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Conference:
    name: str
    full_name: str
    homes: tuple[str, ...]
    paths: tuple[str, ...] = ("", "{year}/", "cfp/", "call-for-papers/", "submissions/")
    default_type: str = "Submission"
    note: str = ""


CONFERENCES: list[Conference] = [
    Conference("QIP", "Quantum Information Processing",
               ("https://qipconference.org/",)),
    Conference("QCrypt", "Quantum Cryptography",
               ("https://qcrypt.net/",)),
    Conference("TQC", "Theory of Quantum Computation, Communication and Cryptography",
               ("https://tqc-conference.org/",)),
    Conference("QTML", "Quantum Techniques in Machine Learning",
               ("https://qtml2026.nithecs.ac.za/",),
               note="dominio diverso ogni anno: usare sources.local.json"),
    Conference("QCTiP", "Quantum Computing Theory in Practice",
               ("https://qctipconf.github.io/",)),
    Conference("AQIS", "Asian Quantum Information Science Conference",
               ("https://aqis-conf.org/",)),
    Conference("QCNC", "Quantum Communications, Networking, and Computing",
               ("https://www.ieee-qcnc.org/",), default_type="Technical paper"),
    Conference("QCMC", "Quantum Communication, Measurement and Computing",
               ("https://qcmc.org/",)),
    Conference("IEEE QCE / Quantum Week", "IEEE International Conference on Quantum Computing and Engineering",
               ("https://qce.quantum.ieee.org/",), default_type="Technical paper"),
    Conference("QSim", "Quantum Simulation Conference",
               ("https://qsimconference.org/",)),
    Conference("QPL", "Quantum Physics and Logic",
               ("https://qplconference.org/",)),
]


def load_overrides(confs: list[Conference]) -> list[Conference]:
    """sources.local.json puo' rimpiazzare homes/paths senza toccare il codice."""
    if not OVERRIDES.exists():
        return confs
    try:
        cfg = json.loads(OVERRIDES.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"! sources.local.json illeggibile ({exc}), ignorato", file=sys.stderr)
        return confs
    out = []
    for c in confs:
        o = cfg.get(c.name)
        if isinstance(o, dict):
            c = Conference(
                c.name, c.full_name,
                tuple(o.get("homes", c.homes)),
                tuple(o.get("paths", c.paths)),
                o.get("default_type", c.default_type),
                o.get("note", c.note),
            )
        out.append(c)
    return out


# --------------------------------------------------------------------------
# 2. Riconoscimento date (indipendente dal formato e dalla lingua della pagina)
# --------------------------------------------------------------------------

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"

RE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
RE_DMY = re.compile(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})\.?,?\s+(\d{{4}})\b", re.I)
RE_MDY = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b", re.I)
RE_NUM = re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")
RE_DM = re.compile(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MON})\b\.?", re.I)   # senza anno
RE_MD = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}}){_ORD}\b", re.I)             # senza anno


@dataclass
class Found:
    date: datetime.date
    explicit_year: bool
    start: int


def _mk(y: int, m: int, d: int) -> Optional[datetime.date]:
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, edition: int) -> list[Found]:
    """Tutte le date plausibili in una riga, in ordine di apparizione.

    edition serve solo a ricostruire l'anno quando la pagina non lo scrive
    (tabelle tipo "5 Oct | Talk submission deadline"): la deadline di
    un'edizione N cade sempre nell'anno N o N-1.
    """
    found: list[Found] = []
    taken: list[tuple[int, int]] = []

    for m in RE_ISO.finditer(text):
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        if d:
            found.append(Found(d, True, m.start()))
            taken.append(m.span())
    for m in RE_DMY.finditer(text):
        d = _mk(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
        if d:
            found.append(Found(d, True, m.start()))
            taken.append(m.span())
    for m in RE_MDY.finditer(text):
        d = _mk(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        if d:
            found.append(Found(d, True, m.start()))
            taken.append(m.span())
    for m in RE_NUM.finditer(text):
        a, b = int(m[1]), int(m[2])
        # i siti di conferenze sono in maggioranza europei -> giorno/mese,
        # tranne quando e' impossibile (13/09 -> 13 e' il giorno; 09/13 -> mese/giorno)
        day, mon = (a, b) if a > 12 or b <= 12 else (b, a)
        d = _mk(int(m[3]), mon, day)
        if d:
            found.append(Found(d, True, m.start()))
            taken.append(m.span())

    def overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < e and s < span[1] for s, e in taken)

    for rx, day_first in ((RE_DM, True), (RE_MD, False)):
        for m in rx.finditer(text):
            if overlaps(m.span()):
                continue
            day = int(m[1]) if day_first else int(m[2])
            mon = MONTHS[(m[2] if day_first else m[1]).lower()]
            # anno ricostruito: quello che cade piu' vicino a oggi fra N-1 e N
            cands = [c for c in (_mk(edition - 1, mon, day), _mk(edition, mon, day)) if c]
            if not cands:
                continue
            best = min(cands, key=lambda d: abs((d - TODAY).days))
            found.append(Found(best, False, m.start()))
            taken.append(m.span())

    found.sort(key=lambda f: f.start)
    return found


def plausible(d: datetime.date, edition: int) -> bool:
    """Scarta date archeologiche o assurde (es. il copyright 2019 in fondo)."""
    if not (edition - 1 <= d.year <= edition):
        return False
    return (TODAY - datetime.timedelta(days=400)) <= d <= (TODAY + datetime.timedelta(days=900))


# --------------------------------------------------------------------------
# 3. Riconoscimento del contesto: e' davvero una deadline di sottomissione?
# --------------------------------------------------------------------------

KW_DEADLINE = re.compile(
    r"deadline|submission|submit|due\b|closes|closing|cfp|call for (?:papers|abstracts|contributions)",
    re.I)

# righe che contengono date ma NON sono deadline di sottomissione
KW_BLOCK = re.compile(
    r"notification|acceptance|accepted|camera[- ]ready|rebuttal|author response|"
    r"early[- ]?bird|registration (?:opens|fee|closes|deadline for attend)|payment|"
    r"visa|hotel|accommodation|travel (?:grant|support) notif|banquet|"
    r"conference dates|workshop dates|tutorial day|programme online|program online|"
    r"proceedings|final version|copyright", re.I)

# tipo di deadline: prima regola che matcha vince
TYPE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"talk\s*registration", re.I), "Talk registration"),
    (re.compile(r"poster", re.I), "Poster submission"),
    (re.compile(r"talk\s*(submission|proposal)", re.I), "Talk submission"),
    (re.compile(r"(technical|research|full|long|short)\s*paper", re.I), "Technical paper"),
    (re.compile(r"\bpaper", re.I), "Technical paper"),
    (re.compile(r"abstract", re.I), "Abstract submission"),
    (re.compile(r"workshop|tutorial|special session", re.I), "Workshop/Tutorial proposal"),
]

RE_EXTENDED = re.compile(r"extend", re.I)


def classify(text: str, default: str) -> str:
    for rx, label in TYPE_RULES:
        if rx.search(text):
            return label
    return default


# --------------------------------------------------------------------------
# 4. HTML -> righe logiche (una riga di tabella resta una riga sola)
# --------------------------------------------------------------------------

def clean(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    return re.sub(r"\s+", " ", s).strip()


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "head"]):
        tag.decompose()

    lines: list[str] = []

    # le tabelle sono il formato piu' comune per le "key dates":
    # data in una cella, etichetta in quella accanto -> vanno tenute insieme
    for tr in soup.find_all("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        row = " | ".join(c for c in cells if c)
        if row:
            lines.append(row)
        tr.decompose()

    # liste di definizione: <dt>data</dt><dd>etichetta</dd>
    for dl in soup.find_all("dl"):
        pair: list[str] = []
        for child in dl.find_all(["dt", "dd"]):
            pair.append(clean(child.get_text(" ", strip=True)))
            if child.name == "dd":
                lines.append(" | ".join(p for p in pair if p))
                pair = []
        dl.decompose()

    lines.extend(clean(x) for x in soup.get_text("\n").splitlines())
    return [x for x in lines if x]


# --------------------------------------------------------------------------
# 5. Estrazione dei record da una pagina
# --------------------------------------------------------------------------

@dataclass
class Record:
    name: str
    edition: str
    full_name: str
    type: str
    deadline: Optional[str]
    status: str          # official | inferred | tbd
    source: str
    note: str = ""
    extended: bool = False

    def key(self) -> tuple[str, str, str]:
        return (self.name.lower(), str(self.edition), self.type.lower())

    def to_json(self) -> dict:
        return {
            "name": self.name, "edition": self.edition, "fullName": self.full_name,
            "type": self.type, "deadline": self.deadline, "status": self.status,
            "source": self.source, "note": self.note,
            "lastChecked": TODAY.isoformat(),
        }


CONFIDENCE = {"official": 3, "inferred": 2, "past": 1, "tbd": 1}


def confidence(status: str) -> int:
    return CONFIDENCE.get(str(status).split(" ")[0].lower(), 0)


def extract(conf: Conference, url: str, html: str, edition: int) -> list[Record]:
    lines = html_to_lines(html)
    best: dict[str, Record] = {}

    for i, line in enumerate(lines):
        dates = [f for f in find_dates(line, edition) if plausible(f.date, edition)]
        if not dates:
            continue

        ctx = line
        # se la data e' sola nella sua riga, guarda la riga prima e dopo
        if not KW_DEADLINE.search(ctx):
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and KW_DEADLINE.search(lines[j]) and len(lines[j]) < 160:
                    ctx = f"{ctx} | {lines[j]}"
                    break

        if not KW_DEADLINE.search(ctx) or KW_BLOCK.search(ctx):
            continue

        f = dates[0]
        typ = classify(ctx, conf.default_type)
        note = "AoE" if re.search(r"\baoe\b|anywhere on earth", ctx, re.I) else ""
        if not f.explicit_year:
            note = (note + " · anno dedotto dalla pagina").strip(" ·")
        rec = Record(conf.name, str(edition), conf.full_name, typ,
                     f.date.isoformat(),
                     "official" if f.explicit_year else "inferred",
                     url, note, bool(RE_EXTENDED.search(ctx)))

        prev = best.get(typ)
        if prev is None or _better(rec, prev):
            best[typ] = rec

    return list(best.values())


def _better(new: Record, old: Record) -> bool:
    """Una deadline prorogata batte l'originale; un anno esplicito batte uno dedotto."""
    return (new.extended, confidence(new.status)) > (old.extended, confidence(old.status))


def detect_edition(html: str, name: str, url: str) -> Optional[int]:
    """Anno dell'edizione: prima dall'URL, poi dal titolo/testo della pagina."""
    m = re.search(r"/(20\d{2})(?:[/_-]|$)", url)
    if m:
        return int(m[1])
    short = re.escape(name.split(" ")[0])
    m = re.search(rf"{short}\s*'?\s*(20\d{{2}})", html, re.I)
    if m:
        return int(m[1])
    return None



class Fetcher:
    def __init__(self, offline=False, ttl_hours=12, timeout=30, verbose=False, use_cache=True):
        self.offline, self.ttl, self.timeout = offline, ttl_hours * 3600, timeout
        self.verbose, self.use_cache = verbose, use_cache
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        if use_cache:
            CACHE.mkdir(exist_ok=True)

    def _path(self, url: str) -> Path:
        return CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    def get(self, url: str) -> Optional[str]:
        p = self._path(url)
        if self.use_cache and p.exists() and (time.time() - p.stat().st_mtime) < self.ttl:
            return p.read_text(encoding="utf-8", errors="ignore")
        if self.offline:
            return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else None
        for attempt in range(2):
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                if r.status_code == 200 and r.text:
                    if self.use_cache:
                        p.write_text(r.text, encoding="utf-8")
                    if self.verbose:
                        print(f"    GET {url} -> 200 ({len(r.text)//1024} kB)")
                    return r.text
                if self.verbose:
                    print(f"    GET {url} -> {r.status_code}")
                return None
            except requests.RequestException as exc:
                if attempt:
                    if self.verbose:
                        print(f"    GET {url} -> {type(exc).__name__}")
                    return None
                time.sleep(1.5)
        return None


def candidate_urls(conf: Conference, years: list[int]) -> list[str]:
    urls: list[str] = []
    for home in conf.homes:
        base = home if home.endswith("/") else home + "/"
        for path in conf.paths:
            if "{year}" in path:
                for y in years:
                    urls.append(base + path.format(year=y))
            else:
                urls.append(base + path)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def discover_links(html: str, base: str, years: list[int]) -> list[str]:
    """Trova sulla home i link alle edizioni future (es. 'QIP 2029')."""
    out = []
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href, label = a["href"], clean(a.get_text(" ", strip=True))
        if not any(str(y) in href or str(y) in label for y in years):
            continue
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = re.sub(r"^(https?://[^/]+).*$", r"\1", base) + href
        else:
            url = base.rstrip("/") + "/" + href
        if url.startswith("http") and url not in out:
            out.append(url)
    return out[:8]


def scrape(conf: Conference, fetcher: Fetcher, verbose=False) -> tuple[list[Record], Optional[int]]:
    """Restituisce i record dell'edizione piu' avanzata trovata, + quell'edizione."""
    years = [TODAY.year, TODAY.year + 1, TODAY.year + 2]
    by_edition: dict[int, list[Record]] = {}
    seen_editions: set[int] = set()
    urls = candidate_urls(conf, years)
    extra_done = False

    idx = 0
    while idx < len(urls):
        url = urls[idx]
        idx += 1
        html = fetcher.get(url)
        if not html:
            continue
        edition = detect_edition(html, conf.name, url) or (TODAY.year + 1)
        seen_editions.add(edition)
        recs = extract(conf, url, html, edition)
        if recs:
            by_edition.setdefault(edition, [])
            for r in recs:
                existing = {x.type: x for x in by_edition[edition]}
                if r.type not in existing or _better(r, existing[r.type]):
                    by_edition[edition] = [x for x in by_edition[edition] if x.type != r.type] + [r]
        if not extra_done:
            extra_done = True
            urls.extend(u for u in discover_links(html, conf.homes[0], years) if u not in urls)

    if not by_edition:
        return [], (max(seen_editions) if seen_editions else None)

    # preferisci l'edizione piu' recente che ha almeno una deadline futura
    future = [e for e, rs in by_edition.items()
              if any(r.deadline and r.deadline >= TODAY.isoformat() for r in rs)]
    edition = max(future) if future else max(by_edition)
    return by_edition[edition], edition


# --------------------------------------------------------------------------
# 7. Merge conservativo con data.json
# --------------------------------------------------------------------------

def merge(old_items: list[dict], fresh: list[Record], touched: set[str], verbose=False) -> tuple[list[dict], list[str]]:
    log: list[str] = []
    index: dict[tuple, dict] = {}
    order: list[tuple] = []
    for it in old_items:
        k = (str(it.get("name", "")).lower(), str(it.get("edition", "")), str(it.get("type", "")).lower())
        if k not in index:
            order.append(k)
        index[k] = it

    fresh_keys = {r.key() for r in fresh}

    for r in fresh:
        k = r.key()
        cur = index.get(k)
        if cur is None:
            index[k] = r.to_json()
            order.append(k)
            log.append(f"+ {r.name} {r.edition} {r.type}: {r.deadline}")
        elif confidence(r.status) >= confidence(cur.get("status", "")):
            if cur.get("deadline") != r.deadline or confidence(cur.get("status", "")) < confidence(r.status):
                log.append(f"~ {r.name} {r.edition} {r.type}: {cur.get('deadline')} -> {r.deadline}")
            merged = r.to_json()
            # "noteManual" e' tuo: aggiungilo a mano a un record e non verra' mai perso
            manual = cur.get("noteManual")
            if manual:
                merged["noteManual"] = manual
            merged["note"] = " · ".join(x for x in (r.note, manual) if x) or cur.get("note", "")
            index[k] = merged
        else:
            # regola 3: non degradare un dato ufficiale
            cur["lastChecked"] = TODAY.isoformat()
            log.append(f"= {r.name} {r.edition} {r.type}: mantenuto {cur.get('deadline')} (nuovo dato meno affidabile)")

    # record vecchi di una conferenza che abbiamo interrogato ma non riconfermato
    for k, it in index.items():
        if k in fresh_keys or it.get("name", "") not in touched:
            continue
        if it.get("deadline") and it["deadline"] >= TODAY.isoformat() and confidence(it.get("status", "")) >= 2:
            if not str(it.get("status", "")).endswith("(da verificare)"):
                it["status"] = f"{str(it.get('status','official')).split(' ')[0]} (da verificare)"
                log.append(f"? {it['name']} {it['edition']} {it['type']}: non piu' trovata sul sito, marcata da verificare")

    return [index[k] for k in order], log


def ensure_placeholders(items: list[dict], confs: list[Conference], editions: dict[str, Optional[int]]) -> list[dict]:
    """Ogni conferenza deve restare visibile nella dashboard anche senza date."""
    have_future = {
        it.get("name") for it in items
        if (it.get("deadline") or "") >= TODAY.isoformat()
    }
    have_tbd = {(it.get("name"), str(it.get("edition"))) for it in items if not it.get("deadline")}
    for c in confs:
        if c.name in have_future:
            continue
        ed = str(editions.get(c.name) or (TODAY.year + 1))
        if (c.name, ed) in have_tbd:
            continue
        items.append({
            "name": c.name, "edition": ed, "fullName": c.full_name,
            "type": c.default_type, "deadline": None, "status": "tbd",
            "source": c.homes[0],
            "note": c.note or "deadline non ancora pubblicata",
            "lastChecked": TODAY.isoformat(),
        })
    return items


def prune(items: list[dict]) -> list[dict]:
    """Evita che il file cresca all'infinito: via le cose molto vecchie."""
    limit = (TODAY - datetime.timedelta(days=365)).isoformat()
    out = []
    for it in items:
        d = it.get("deadline")
        if d and d < limit:
            continue
        if not d and str(it.get("edition", "9999")).isdigit() and int(it["edition"]) < TODAY.year:
            continue
        out.append(it)
    return out


def sort_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: (it.get("deadline") or "9999-12-31",
                                         str(it.get("name", "")), str(it.get("type", ""))))


def write_data(path: Path, items: list[dict]) -> None:
    if path.exists():
        (path.with_suffix(".json.bak")).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# 8. Selftest: verifica il parser senza toccare la rete
# --------------------------------------------------------------------------

def selftest() -> int:
    global TODAY
    real_today = TODAY
    TODAY = datetime.date(2026, 8, 26)  # data fissa per rendere il test riproducibile
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    html = """
    <table><tr><th>28 Sep 2026</th><td>Talk registration deadline (23:59 AoE)</td></tr>
    <tr><td>5 October 2026</td><td>Talk submission deadline</td></tr>
    <tr><td>19 Oct</td><td>Poster submission deadline</td></tr>
    <tr><td>2026-11-30</td><td>Notification of acceptance</td></tr>
    <tr><td>January 12, 2027</td><td>Conference dates</td></tr></table>
    <p>Submission deadline extended to 12/10/2026.</p>
    <footer>&copy; 2019 QIP</footer>"""
    conf = Conference("QIP", "Quantum Information Processing", ("https://x/",))
    recs = {r.type: r for r in extract(conf, "https://x/2027/", html, 2027)}

    check(recs["Talk registration"].deadline == "2026-09-28", "talk registration errata")
    check("AoE" in recs["Talk registration"].note, "nota AoE persa")
    check(recs["Poster submission"].deadline == "2026-10-19", "anno non dedotto per il poster")
    check(recs["Poster submission"].status == "inferred", "anno dedotto deve valere 'inferred'")
    check("Notification" not in str(recs), "riga di notifica non filtrata")
    check(all(r.deadline != "2027-01-12" for r in recs.values()), "date della conferenza non filtrate")
    check(recs["Submission"].deadline == "2026-10-12" and recs["Submission"].extended,
          "proroga non riconosciuta")

    # formati numerici e ambiguita' giorno/mese
    check(find_dates("deadline 03/09/2026", 2027)[0].date == datetime.date(2026, 9, 3), "d/m/Y errata")
    check(find_dates("deadline 09/13/2026", 2027)[0].date == datetime.date(2026, 9, 13), "m/d/Y errata")
    # date implausibili scartate
    check(not plausible(datetime.date(2019, 5, 1), 2027), "data vecchia non scartata")

    # merge: non degradare un dato ufficiale
    old = [{"name": "QIP", "edition": "2027", "fullName": "x", "type": "Talk submission",
            "deadline": "2026-10-05", "status": "official", "source": "s", "note": ""}]
    weak = Record("QIP", "2027", "x", "Talk submission", "2026-10-09", "inferred", "s")
    merged, _ = merge(old, [weak], {"QIP"})
    check(merged[0]["deadline"] == "2026-10-05", "dato ufficiale sovrascritto da uno dedotto")

    strong = Record("QIP", "2027", "x", "Talk submission", "2026-10-09", "official", "s")
    merged, _ = merge(old, [strong], {"QIP"})
    check(merged[0]["deadline"] == "2026-10-09", "proroga ufficiale non applicata")

    merged, _ = merge(old, [], {"QIP"})
    check("da verificare" in merged[0]["status"], "record sparito non marcato")

    TODAY = real_today
    if failures:
        print("SELFTEST FALLITO:")
        for f in failures:
            print("  -", f)
        return 1
    print("selftest ok")
    return 0


# --------------------------------------------------------------------------
# 9. main
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Aggiorna data.json del Quantum CFP Tracker")
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--only", nargs="+", metavar="NAME", help="aggiorna solo queste conferenze")
    ap.add_argument("--offline", action="store_true", help="usa solo la cache")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--cache-ttl", type=int, default=12, help="ore di validita' della cache")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="non scrive data.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    confs = load_overrides(CONFERENCES)
    if args.only:
        wanted = {w.lower() for w in args.only}
        confs = [c for c in confs if any(w in c.name.lower() for w in wanted)]
        if not confs:
            return print("Nessuna conferenza corrisponde a --only") or 2

    items = json.loads(args.data.read_text(encoding="utf-8")) if args.data.exists() else []
    fetcher = Fetcher(args.offline, args.cache_ttl, args.timeout, args.verbose, not args.no_cache)

    all_fresh: list[Record] = []
    editions: dict[str, Optional[int]] = {}
    touched: set[str] = set()
    summary: list[str] = []

    for c in confs:
        print(f"-> {c.name}")
        try:
            recs, edition = scrape(c, fetcher, args.verbose)
        except Exception as exc:  # regola 4: nessuna conferenza puo' far saltare il run
            summary.append(f"   {c.name}: ERRORE {type(exc).__name__}: {exc} (dati precedenti mantenuti)")
            continue
        touched.add(c.name)
        editions[c.name] = edition
        all_fresh.extend(recs)
        if recs:
            summary.append(f"   {c.name} {edition}: " + ", ".join(f"{r.type}={r.deadline}" for r in recs))
        else:
            summary.append(f"   {c.name}: nessuna data trovata"
                           + (f" (edizione {edition})" if edition else " (pagina irraggiungibile)"))

    items, log = merge(items, all_fresh, touched, args.verbose)
    items = ensure_placeholders(items, confs, editions)
    items = sort_items(prune(items))

    print("\n--- riepilogo ---")
    for s in summary:
        print(s)
    if log:
        print("\n--- modifiche ---")
        for l in log:
            print("  " + l)
    else:
        print("\nnessuna modifica")

    if args.dry_run:
        print(f"\n[dry-run] {args.data} non e' stato scritto ({len(items)} record risultanti)")
        return 0

    write_data(args.data, items)
    print(f"\nScritto {args.data} ({len(items)} record; backup in {args.data.with_suffix('.json.bak').name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())