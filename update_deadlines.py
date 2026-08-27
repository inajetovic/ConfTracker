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

5.  INDICE ESTERNO COME RETE DI SICUREZZA. Oltre ai siti ufficiali viene
    letto https://quantum.technology/conf/<anno>.html (lista curata di
    conferenze quantum): da li' si ricava il link aggiornato all'edizione -
    l'unico modo affidabile di seguire una conferenza che cambia dominio - e,
    in mancanza d'altro, le deadline pubblicate nell'indice, che pero' valgono
    meno di quelle lette sul sito ufficiale. Si disattiva con --no-directory.

6.  URL MODIFICABILI SENZA TOCCARE IL CODICE. Se una conferenza cambia
    dominio (tipico di QTML, che ogni anno e' ospitata da un'universita'
    diversa), basta creare sources.local.json accanto a questo file:

        { "QTML": { "homes": ["https://qtml2028.example.edu/"] } }

Uso:
    python update_deadlines.py                 # aggiorna data.json
    python update_deadlines.py --dry-run -v    # mostra cosa farebbe
    python update_deadlines.py --only QIP TQC  # solo alcune conferenze
    python update_deadlines.py --offline       # usa solo la cache locale
    python update_deadlines.py --no-directory  # solo siti ufficiali
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
from collections import Counter
from dataclasses import dataclass, field, replace
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
    # nomi con cui la conferenza compare nell'indice quantum.technology
    aliases: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        for a in (self.name, self.full_name, *self.aliases):
            a = a.strip()
            if len(a) < 3:
                continue
            # (?<![A-Za-z]) invece di \b: "QIP2027" deve matchare "QIP"
            if re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", text, re.I):
                return True
        return False


CONFERENCES: list[Conference] = [
    Conference("QIP", "Quantum Information Processing",
               ("https://qipconference.org/",),
               aliases=("QIP", "Quantum Information Processing Conference")),
    Conference("QCrypt", "Quantum Cryptography",
               ("https://qcrypt.net/",), aliases=("QCrypt",)),
    Conference("TQC", "Theory of Quantum Computation, Communication and Cryptography",
               ("https://tqc-conference.org/",), aliases=("TQC",)),
    Conference("QTML", "Quantum Techniques in Machine Learning",
               ("https://qtml2026.nithecs.ac.za/",),
               aliases=("QTML", "Quantum Techniques in Machine Learning"),
               note="dominio diverso ogni anno"),
    Conference("QCTiP", "Quantum Computing Theory in Practice",
               ("https://qctipconf.github.io/",), aliases=("QCTiP",)),
    Conference("AQIS", "Asian Quantum Information Science Conference",
               ("https://aqis-conf.org/",), aliases=("AQIS",)),
    Conference("QCNC", "Quantum Communications, Networking, and Computing",
               ("https://www.ieee-qcnc.org/",), default_type="Technical paper",
               aliases=("QCNC",)),
    Conference("QCMC", "Quantum Communication, Measurement and Computing",
               ("https://qcmc.org/", "http://www.qcmc-conference.org/"), aliases=("QCMC",)),
    Conference("IEEE QCE / Quantum Week", "IEEE International Conference on Quantum Computing and Engineering",
               ("https://qce.quantum.ieee.org/",), default_type="Technical paper",
               aliases=("QCE", "IEEE Quantum Week", "Quantum Computing and Engineering")),
    Conference("QSim", "Quantum Simulation Conference",
               ("https://qsimconference.org/",), aliases=("QSim",)),
    Conference("QPL", "Quantum Physics and Logic",
               ("https://qplconference.org/",), aliases=("QPL", "Quantum Physics and Logic")),
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
                tuple(o.get("aliases", c.aliases)),
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
    r"proceedings|final version|copyright|"
    # aperture, non scadenze: "submission site opens 1 January 2027"
    r"\bopens?\b|\bopening\b|\bavailable from\b|submissions? open|"
    # esiti, non scadenze: "Poster decisions start; decisions are rolling"
    r"\bdecisions?\b|"
    # bandi per OSPITARE la conferenza, non per sottomettere lavori
    r"steering committee|top contenders|full proposal|bid to host|"
    r"to host the conference|expression of interest|organi[sz]ing committee proposal", re.I)

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
# usate per decidere se fidarsi del tipo di default (vedi extract())
RE_STRONG = re.compile(r"deadline|due\b|closes|closing", re.I)
RE_SUBMIT = re.compile(r"submission|submit|call for (?:papers|abstracts|contributions)|\bcfp\b", re.I)


def classify(text: str, default: str) -> tuple[str, bool]:
    """(tipo, e_generico). generico = nessuna regola ha matchato, si e' usato il default."""
    for rx, label in TYPE_RULES:
        if rx.search(text):
            return label, False
    return default, True


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
    generic: bool = False   # tipo non riconosciuto: e' finito nel catch-all di default
    line: str = ""          # riga sorgente, per capire da dove viene una data sbagliata
    strong: bool = False    # la riga contiene una vera parola-scadenza (deadline/due/closes)

    def key(self) -> tuple[str, str, str]:
        return (self.name.lower(), str(self.edition), self.type.lower())

    def to_json(self) -> dict:
        return {
            "name": self.name, "edition": self.edition, "fullName": self.full_name,
            "type": self.type, "deadline": self.deadline, "status": self.status,
            "source": self.source, "note": self.note,
            "lastChecked": TODAY.isoformat(),
        }


CONFIDENCE = {"official": 3, "inferred": 2, "directory": 2, "past": 1, "tbd": 1}


def confidence(status: str) -> int:
    return CONFIDENCE.get(str(status).split(" ")[0].lower(), 0)


def extract(conf: Conference, url: str, html: str, edition: int) -> list[Record]:
    lines = html_to_lines(html)
    has_date = [bool(find_dates(l, edition)) for l in lines]
    best: dict[str, Record] = {}

    for i, line in enumerate(lines):
        dates = [f for f in find_dates(line, edition) if plausible(f.date, edition)]
        if not dates:
            continue

        ctx = line
        # Se la data e' sola nella sua riga, l'etichetta puo' stare nella riga
        # accanto. MA solo se quella riga non ha gia' una data propria: due
        # deadline consecutive non devono scambiarsi le etichette.
        if not KW_DEADLINE.search(ctx):
            for j in (i - 1, i + 1):
                if not (0 <= j < len(lines)) or has_date[j]:
                    continue
                if KW_DEADLINE.search(lines[j]) and len(lines[j]) < 160:
                    ctx = f"{ctx} | {lines[j]}"
                    break

        if not KW_DEADLINE.search(ctx) or KW_BLOCK.search(ctx):
            continue

        f = dates[0]
        typ, fallback = classify(ctx, conf.default_type)
        # Il catch-all viene considerato affidabile solo se la riga dice
        # chiaramente sia "deadline" sia "submission": cosi' "Submission
        # deadline extended to X" resta, mentre "registration deadline" o
        # "the conference takes place on X, submissions welcome" no.
        generic = fallback and not (RE_STRONG.search(ctx) and RE_SUBMIT.search(ctx))
        note = "AoE" if re.search(r"\baoe\b|anywhere on earth", ctx, re.I) else ""
        if not f.explicit_year:
            note = (note + " · anno dedotto dalla pagina").strip(" ·")
        rec = Record(conf.name, str(edition), conf.full_name, typ,
                     f.date.isoformat(),
                     "official" if f.explicit_year else "inferred",
                     url, note, bool(RE_EXTENDED.search(ctx)), generic, ctx[:200],
                     strong=bool(RE_STRONG.search(ctx)))

        prev = best.get(typ)
        if prev is None or _better(rec, prev):
            best[typ] = rec

    recs = list(best.values())
    # Il tipo di default e' un catch-all: se la pagina ha prodotto anche tipi
    # riconosciuti (talk/poster/paper/abstract), il catch-all e' quasi sempre
    # rumore (date della conferenza, registrazione, righe generiche) -> via.
    if any(not r.generic for r in recs):
        recs = [r for r in recs if not r.generic]
    return recs


def _better(new: Record, old: Record) -> bool:
    """Ordine di preferenza fra due candidati dello stesso tipo.

    1. la riga che dice esplicitamente "deadline/due/closes" batte quella che
       parla di sottomissioni di sfuggita ("decisions are made within 2 weeks
       of submission" non e' una scadenza);
    2. una proroga batte la data originale;
    3. un anno esplicito batte uno dedotto.
    """
    return ((new.strong, new.extended, confidence(new.status))
            > (old.strong, old.extended, confidence(old.status)))


def detect_edition(html: str, name: str, url: str, hint: Optional[int] = None) -> Optional[int]:
    """Anno dell'edizione: dall'URL, poi dal titolo della pagina, poi dall'indice.

    Negli URL con piu' anni vince l'ULTIMO: qcrypt.net/2026/2027/ e' l'edizione
    2027 ospitata sotto il sito 2026, non l'edizione 2026.
    """
    years = re.findall(r"/(20\d{2})(?=[/_-]|$)", url)
    if years:
        return int(years[-1])
    short = re.escape(name.split(" ")[0])
    m = re.search(rf"{short}\s*'?\s*(20\d{{2}})", html, re.I)
    if m:
        return int(m[1])
    return hint


# --------------------------------------------------------------------------
# 6. Rete (con cache su disco, cosi' i re-run sono veloci e gentili)
# --------------------------------------------------------------------------

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


def scrape(conf: Conference, fetcher: Fetcher, verbose=False, keep_past=False,
           extra_urls: tuple[str, ...] = ()) -> tuple[list[Record], Optional[int], bool]:
    """(record pubblicabili, edizione, solo_date_passate).

    extra_urls arriva dall'indice quantum.technology e viene provato per primo:
    e' l'unico modo per seguire una conferenza che ha cambiato dominio.
    """
    years = [TODAY.year, TODAY.year + 1, TODAY.year + 2]
    by_edition: dict[int, list[Record]] = {}
    seen_editions: set[int] = set()
    urls = list(extra_urls) + [u for u in candidate_urls(conf, years) if u not in extra_urls]
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
        return [], (max(seen_editions) if seen_editions else None), False
    return select_edition(by_edition, keep_past)


def select_edition(by_edition: dict[int, list[Record]], keep_past: bool = False
                   ) -> tuple[list[Record], Optional[int], bool]:
    """Sceglie l'edizione da pubblicare.

    Regola: pubblica solo un'edizione con almeno una deadline futura. Se sul
    sito ci sono solo date passate vuol dire che stiamo leggendo l'archivio
    dell'edizione appena conclusa, NON la call attiva: in quel caso non si
    scrive niente e la conferenza resta 'tbd' in attesa della prossima call.
    (con --keep-past i record vengono comunque salvati, ma marcati 'past')
    """
    now = TODAY.isoformat()
    future = [e for e, rs in by_edition.items()
              if any(r.deadline and r.deadline >= now for r in rs)]
    if future:
        edition = max(future)
        return _drop_generic(by_edition[edition]), edition, False

    edition = max(by_edition)
    if not keep_past:
        return [], edition, True
    stale = [replace(r, status="past") for r in _drop_generic(by_edition[edition])]
    return stale, edition, True


def _drop_generic(recs: list[Record]) -> list[Record]:
    """Stesso filtro di extract(), riapplicato dopo aver unito piu' pagine."""
    return [r for r in recs if not r.generic] if any(not r.generic for r in recs) else recs


# --------------------------------------------------------------------------
# 6bis. Indice esterno: quantum.technology/conf/<anno>.html
#
# E' una lista curata di conferenze quantum con, per ognuna: il link al sito
# ufficiale dell'edizione e le deadline dentro l'attributo title del link.
# Serve a due cose:
#   a) scoprire il sito quando una conferenza cambia dominio (QTML, QCMC...);
#   b) avere una deadline di riserva se il sito ufficiale non e' parsabile.
# Le date da qui valgono meno di quelle prese dal sito ufficiale.
# --------------------------------------------------------------------------

DIRECTORY_BASE = "https://quantum.technology/conf/"
DIRECTORY_PAGES = ("{year}.html", "index.html")

# Nell'indice le deadline sono righe "Etichetta: valore"
DIR_TYPES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"talk\s*abstract|talk", re.I), "Talk submission"),
    (re.compile(r"poster", re.I), "Poster submission"),
    (re.compile(r"paper", re.I), "Technical paper"),
    (re.compile(r"abstract", re.I), "Abstract submission"),
    (re.compile(r"submission|submit", re.I), "Submission"),
]
# etichette che NON sono deadline di sottomissione
DIR_SKIP = re.compile(r"registration|tickets|early ?bird|financial|travel|application|visa", re.I)
RE_LI_START = re.compile(r"^\s*(TBA|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.I)


@dataclass
class DirEntry:
    label: str          # testo completo della voce (contiene l'acronimo)
    url: str            # sito ufficiale dell'edizione
    deadlines: list[tuple[str, str]]   # [(etichetta, testo della data)]
    year: int


def parse_directory(html: str, fallback_year: int) -> list[DirEntry]:
    soup = BeautifulSoup(html, "html.parser")
    m = re.search(r"(20\d{2})\s*Conf", soup.get_text(" ", strip=True))
    year = int(m[1]) if m else fallback_year

    entries: list[DirEntry] = []
    for a in soup.find_all("a", href=True):
        li = a.find_parent("li")
        if li is None:
            continue
        label = clean(li.get_text(" ", strip=True))
        # le voci di conferenza iniziano con il mese ("Aug 23-27: ...") o "TBA";
        # cosi' si scartano i link di menu/navigazione
        if not RE_LI_START.match(label):
            continue
        url = a["href"].strip()
        if not url.startswith(("http://", "https://")):
            continue

        deadlines = []
        for part in re.split(r"\n|#13;|\r", a.get("title", "")):
            part = clean(part)
            if ":" not in part:
                continue
            lab, _, val = part.partition(":")
            deadlines.append((clean(lab), clean(val)))
        entries.append(DirEntry(label, url, deadlines, year))
    return entries


def fetch_directory(fetcher: Fetcher, years: list[int], verbose=False) -> list[DirEntry]:
    entries: list[DirEntry] = []
    seen_urls: set[str] = set()
    for year in years:
        for tmpl in DIRECTORY_PAGES:
            url = DIRECTORY_BASE + tmpl.format(year=year)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            html = fetcher.get(url)
            if not html:
                continue
            try:
                found = parse_directory(html, year)
            except Exception as exc:
                print(f"! indice {url} non parsabile: {type(exc).__name__}", file=sys.stderr)
                continue
            if verbose:
                print(f"    indice {url}: {len(found)} voci")
            entries.extend(found)
    return entries


def directory_for(conf: Conference, entries: list[DirEntry]) -> list[DirEntry]:
    """Voci dell'indice che riguardano questa conferenza, edizione piu' recente prima."""
    mine = [e for e in entries if conf.matches(e.label)]
    return sorted(mine, key=lambda e: e.year, reverse=True)


def directory_records(conf: Conference, entries: list[DirEntry]) -> tuple[list[str], list[Record]]:
    """(url da provare, record di riserva ricavati dall'indice)."""
    urls: list[str] = []
    recs: list[Record] = []
    for e in directory_for(conf, entries):
        if e.url not in urls:
            urls.append(e.url)
        for lab, val in e.deadlines:
            if DIR_SKIP.search(lab) or not val or re.fullmatch(r"TBA(\s+20\d{2})?", val, re.I):
                continue
            dates = [f for f in find_dates(val, e.year) if plausible(f.date, e.year)]
            if not dates:
                continue
            typ = next((t for rx, t in DIR_TYPES if rx.search(lab)), conf.default_type)
            recs.append(Record(
                conf.name, str(e.year), conf.full_name, typ,
                dates[0].date.isoformat(), "directory", e.url,
                "via quantum.technology/conf"))
    # una sola data per tipo, quella dell'edizione piu' recente (gia' ordinata)
    best: dict[str, Record] = {}
    for r in recs:
        best.setdefault(r.type, r)
    return urls, list(best.values())


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
    """Evita che il file cresca all'infinito e toglie i record che non dicono nulla.

    Un record senza data non e' informativo in due casi: se e' marcato "past"
    (scadenza passata di cui non si conosce la data: l'interfaccia lo mostrerebbe
    come "not published yet", cioe' il contrario del vero) o se e' il segnaposto
    di un'edizione gia' superata da una piu' recente.
    Un record annotato a mano (noteManual) non viene mai eliminato.
    """
    limit = (TODAY - datetime.timedelta(days=365)).isoformat()

    def edition_of(it: dict) -> int:
        e = str(it.get("edition", ""))
        return int(e) if e.isdigit() else 0

    newest: dict[str, int] = {}
    for it in items:
        n = str(it.get("name", ""))
        newest[n] = max(newest.get(n, 0), edition_of(it))

    out = []
    for it in items:
        if it.get("noteManual"):
            out.append(it)
            continue
        d = it.get("deadline")
        if d and d < limit:
            continue
        if not d:
            ed = edition_of(it)
            if ed and ed < TODAY.year:
                continue
            if str(it.get("status", "")).lower().startswith("past"):
                continue
            if ed and ed <= TODAY.year and newest.get(str(it.get("name", "")), 0) > ed:
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

    # --- regressioni viste sul run reale ---

    # 1. due deadline consecutive non devono scambiarsi l'etichetta:
    #    la riga con la sola data non puo' rubare la label a una riga gia' datata
    html_pair = """<ul>
      <li>Talk submission deadline: 5 Oct 2026</li>
      <li>19 Oct 2026</li>
      <li>Poster submission deadline</li></ul>"""
    r2 = {r.type: r.deadline for r in extract(conf, "https://x/2027/", html_pair, 2027)}
    check(r2.get("Talk submission") == "2026-10-05", "talk submission alterata")
    check(r2.get("Poster submission") == "2026-10-19",
          f"poster agganciato male: {r2.get('Poster submission')}")

    # 2. il catch-all "Submission" sparisce se la pagina ha tipi riconosciuti
    html_noise = """<ul><li>Talk submission deadline: 5 Oct 2026</li>
      <li>The conference takes place on 26 February 2027, submissions welcome</li></ul>"""
    types = {r.type for r in extract(conf, "https://x/", html_noise, 2027)}
    check(types == {"Talk submission"}, f"catch-all non filtrato: {types}")
    # ...ma resta se e' l'unico tipo presente
    only_generic = extract(conf, "https://x/", "<p>Submission deadline: 3 March 2027</p>", 2027)
    check([r.type for r in only_generic] == ["Submission"], "catch-all rimosso a torto")

    # 3. un'edizione con sole date passate non va pubblicata come attiva
    past = Record("X", "2026", "x", "Submission", "2026-03-13", "official", "s")
    fut = Record("X", "2027", "x", "Submission", "2027-03-13", "official", "s")
    recs_sel, ed, only_past = select_edition({2026: [past]})
    check(recs_sel == [] and ed == 2026 and only_past, "archivio pubblicato come call attiva")
    recs_sel, ed, only_past = select_edition({2026: [past]}, keep_past=True)
    check(recs_sel[0].status == "past", "--keep-past non marca i record")
    recs_sel, ed, only_past = select_edition({2026: [past], 2027: [fut]})
    check(ed == 2027 and not only_past, "edizione futura non preferita")

    # 4. casi reali visti su qipconference.org e qcrypt.net (agosto 2026)
    qip_real = """<table>
    <tr><td>28 Sep 2026</td><td>Talk registration deadline</td></tr>
    <tr><td>5 Oct 2026</td><td>Talk submission deadline</td></tr>
    <tr><td>5 Oct 2026</td><td>Poster decisions start; decisions are rolling and made within 2 weeks of submission</td></tr>
    <tr><td>30 Nov 2026</td><td>Decision notification for talks</td></tr>
    <tr><td>4 Dec 2026</td><td>Poster submission deadline</td></tr>
    <tr><td>20 Dec 2026</td><td>Early Bird Registration Closes</td></tr>
    <tr><td>20 Jan 2027</td><td>Registration Closes</td></tr>
    <tr><td>20 - 26 Feb 2027</td><td>QIP 2027</td></tr></table><p>20-26 February 2027</p>"""
    got = {r.type: r.deadline for r in extract(conf, "https://qipconference.org/2027/", qip_real, 2027)}
    check(got == {"Talk registration": "2026-09-28", "Talk submission": "2026-10-05",
                  "Poster submission": "2026-12-04"}, f"tabella QIP reale: {got}")

    # pagina "Organize QCrypt 2027": e' un bando per OSPITARE, non una CFP
    host_page = """<h1>QCrypt 2027</h1>
    <p>QCrypt 2027 will be hosted in Vienna, Austria, August 23-27, 2027.</p>
    <p>The steering committee will request the top contenders, by January 1, 2027,
    to submit a full proposal, to be submitted by January 31, 2027.</p>"""
    qc0 = Conference("QCrypt", "Quantum Cryptography", ("https://qcrypt.net/",))
    check(extract(qc0, "https://qcrypt.net/2026/2027/", host_page, 2027) == [],
          "bando di hosting scambiato per CFP")

    # 5. record senza data e senza informazione vanno via
    stale = [
        {"name": "AQIS", "edition": "2026", "deadline": None, "status": "past",
         "note": "submission deadline has passed"},                      # niente data + past
        {"name": "AQIS", "edition": "2027", "deadline": None, "status": "tbd"},
        {"name": "QPL", "edition": "2026", "deadline": None, "status": "tbd"},   # superato dal 2027
        {"name": "QPL", "edition": "2027", "deadline": None, "status": "tbd"},
        {"name": "TQC", "edition": "2026", "deadline": None, "status": "tbd",
         "noteManual": "annotato a mano"},                                # da preservare
        {"name": "QIP", "edition": "2027", "deadline": "2026-12-04", "status": "official"},
    ]
    kept = {(x["name"], x["edition"]) for x in prune(stale)}
    check(("AQIS", "2026") not in kept, "record 'past' senza data non rimosso")
    check(("AQIS", "2027") in kept, "segnaposto dell'edizione corrente rimosso a torto")
    check(("QPL", "2026") not in kept, "segnaposto superato non rimosso")
    check(("TQC", "2026") in kept, "record annotato a mano eliminato")
    check(("QIP", "2027") in kept, "deadline valida eliminata")

    # 6. indice quantum.technology: scoperta URL + deadline di riserva
    dir_html = """<h1>2027 Conferences</h1><ul>
      <li><a href="https://quantum.technology/index.html">Home</a></li>
      <li><b>Aug 23-27:</b> <a href="https://qcrypt.net/2026/2027/"
         title="Abstract: 3 March 2027&#13;Early bird registration: TBA 2026">QCrypt 2027</a>, Vienna.</li>
      <li><b>TBA:</b> <a href="http://www.qcmc-conference.org"
         title="Abstract: TBA 2026&#13;Registration: TBA 2026">17th International Conference on
         Quantum Communication, Measurement and Computing</a> (QCMC 2027), TBA.</li></ul>"""
    entries = parse_directory(dir_html, 2027)
    check(len(entries) == 2, f"link di menu non filtrati: {len(entries)} voci")

    qcmc = Conference("QCMC", "Quantum Communication, Measurement and Computing",
                      ("https://qcmc.org/",), aliases=("QCMC",))
    urls, recs_dir = directory_records(qcmc, entries)
    check(urls == ["http://www.qcmc-conference.org"], f"URL non scoperto: {urls}")
    check(recs_dir == [], "TBA trasformato in data")

    qc = Conference("QCrypt", "Quantum Cryptography", ("https://qcrypt.net/",), aliases=("QCrypt",))
    urls, recs_dir = directory_records(qc, entries)
    check([(r.type, r.deadline, r.status) for r in recs_dir]
          == [("Abstract submission", "2027-03-03", "directory")], f"indice: {recs_dir}")
    check(confidence("directory") < confidence("official"), "l'indice non deve battere il sito")

    # URL con due anni: vince l'ultimo (edizione 2027 ospitata sotto il sito 2026)
    check(detect_edition("", "QCrypt", "https://qcrypt.net/2026/2027/") == 2027, "edizione errata")

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


def explain(url: str, timeout: int = 30) -> int:
    """Diagnostica: mostra come viene letta UNA pagina, riga per riga.

    Serve quando una data esce sbagliata e non si capisce da dove arrivi.
    """
    fetcher = Fetcher(timeout=timeout, verbose=True)
    html = fetcher.get(url)
    if not html:
        print("pagina non raggiungibile")
        return 1
    edition = detect_edition(html, "", url) or (TODAY.year + 1)
    print(f"edizione dedotta: {edition}\n")
    lines = html_to_lines(html)
    has_date = [bool(find_dates(l, edition)) for l in lines]
    for i, line in enumerate(lines):
        dates = find_dates(line, edition)
        if not dates:
            continue
        ok = [f for f in dates if plausible(f.date, edition)]
        short = line[:150]
        if not ok:
            print(f"[scartata: data implausibile] {short}")
            continue
        ctx = line
        if not KW_DEADLINE.search(ctx):
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and not has_date[j] and KW_DEADLINE.search(lines[j]) \
                        and len(lines[j]) < 160:
                    ctx = f"{ctx} | {lines[j]}"
                    break
        if not KW_DEADLINE.search(ctx):
            print(f"[scartata: nessuna parola-deadline] {short}")
        elif KW_BLOCK.search(ctx):
            hit = KW_BLOCK.search(ctx)[0]
            print(f"[scartata: '{hit}'] {short}")
        else:
            typ, fallback = classify(ctx, "Submission")
            gen = fallback and not (RE_STRONG.search(ctx) and RE_SUBMIT.search(ctx))
            flag = " (catch-all, scartato se la pagina ha tipi specifici)" if gen else ""
            print(f"[TENUTA {ok[0].date} -> {typ}{flag}] {short}")
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
    ap.add_argument("--explain", metavar="URL",
                    help="stampa riga per riga come viene analizzata una singola pagina")
    ap.add_argument("--no-directory", action="store_true",
                    help="non usare l'indice quantum.technology/conf")
    ap.add_argument("--keep-past", action="store_true",
                    help="salva anche le edizioni con sole date passate, marcate 'past'")
    ap.add_argument("--dry-run", action="store_true", help="non scrive data.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if args.explain:
        return explain(args.explain, args.timeout)

    confs = load_overrides(CONFERENCES)
    if args.only:
        wanted = {w.lower() for w in args.only}
        confs = [c for c in confs if any(w in c.name.lower() for w in wanted)]
        if not confs:
            return print("Nessuna conferenza corrisponde a --only") or 2

    items = json.loads(args.data.read_text(encoding="utf-8")) if args.data.exists() else []
    fetcher = Fetcher(args.offline, args.cache_ttl, args.timeout, args.verbose, not args.no_cache)

    years = [TODAY.year, TODAY.year + 1, TODAY.year + 2]
    dir_entries: list[DirEntry] = []
    if not args.no_directory:
        print("-> indice quantum.technology/conf")
        try:
            dir_entries = fetch_directory(fetcher, years, args.verbose)
            print(f"   {len(dir_entries)} conferenze indicizzate")
        except Exception as exc:
            print(f"   indice non raggiungibile ({type(exc).__name__}), si prosegue "
                  "con i soli siti ufficiali")

    all_fresh: list[Record] = []
    editions: dict[str, Optional[int]] = {}
    touched: set[str] = set()
    summary: list[str] = []

    for c in confs:
        print(f"-> {c.name}")
        dir_urls, dir_recs = directory_records(c, dir_entries)
        if dir_urls and args.verbose:
            print(f"    indice -> {', '.join(dir_urls[:3])}")
        try:
            recs, edition, only_past = scrape(c, fetcher, args.verbose, args.keep_past,
                                              tuple(dir_urls))
        except Exception as exc:  # regola 4: nessuna conferenza puo' far saltare il run
            summary.append(f"   {c.name}: ERRORE {type(exc).__name__}: {exc} (dati precedenti mantenuti)")
            continue
        touched.add(c.name)
        # se leggiamo l'archivio di un'edizione chiusa, il placeholder deve
        # puntare alla prossima edizione, non a quella appena finita
        editions[c.name] = (edition + 1 if only_past and edition and edition <= TODAY.year
                            else edition)
        site_types = {r.type for r in recs}
        site_dates = {r.deadline for r in recs}
        # dall'indice si prende solo quello che il sito non ha gia' dato,
        # e solo deadline ancora aperte (stessa data = stesso evento, non doppione)
        fallback = [r for r in dir_recs
                    if r.type not in site_types and r.deadline not in site_dates
                    and (args.keep_past or (r.deadline or "") >= TODAY.isoformat())]
        # l'indice va inserito PRIMA del sito: a parita' di confidenza vince l'ultimo
        all_fresh.extend(fallback)
        all_fresh.extend(recs)

        # provenienza: con -v si vede da quale riga arriva ogni data
        if args.verbose:
            for r in recs + fallback:
                print(f"      {r.type} = {r.deadline}  <-  {r.line or '[indice]'}")
                print(f"          fonte: {r.source}")

        # due tipi con la stessa data sono quasi sempre un errore di aggancio
        dupes = [d for d, n in Counter(r.deadline for r in recs).items() if n > 1]
        for d in dupes:
            tipi = ", ".join(r.type for r in recs if r.deadline == d)
            summary.append(f"   ! {c.name}: {d} assegnata a piu' tipi ({tipi})"
                           " - verifica con -v, potrebbe essere un aggancio sbagliato")

        shown = ", ".join(f"{r.type}={r.deadline}" for r in recs)
        extra = ", ".join(f"{r.type}={r.deadline} [indice]" for r in fallback)
        both = ", ".join(x for x in (shown, extra) if x)
        if recs:
            summary.append(f"   {c.name} {edition}: {both}")
        elif fallback:
            summary.append(f"   {c.name}: sito non parsabile, dall'indice -> {extra}")
        elif only_past:
            summary.append(f"   {c.name}: sul sito solo date passate (edizione {edition}, call chiusa)"
                           " -> in attesa della prossima edizione")
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