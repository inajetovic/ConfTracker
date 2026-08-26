"""
Scheduled deadline updater.

Recommended production architecture:
- Keep one parser per conference because CFP pages have different HTML structures.
- Each parser returns normalized records:
  name, edition, type, deadline, status, source, note
- Never overwrite an official date with a guessed date.
- If a source changes and no date can be extracted, keep the old value and mark it for review.
"""
from pathlib import Path
import json, re, datetime, requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data.json"
TODAY = datetime.date.today()

# Example adapter for QIP: its 2027 key dates are in a normal HTML table.
def parse_qip():
    url = "https://qipconference.org/2027/"
    soup = BeautifulSoup(requests.get(url, timeout=30).text, "html.parser")
    text = soup.get_text(" ", strip=True)
    found = []
    patterns = [
        ("Talk registration", r"28 Sep 2026\s+Talk registration deadline"),
        ("Talk submission", r"5 Oct 2026\s+Talk submission deadline"),
        ("Poster submission", r"19 Oct 2026\s+Poster submission deadline"),
    ]
    for kind, pat in patterns:
        if re.search(pat, text):
            date = {"Talk registration":"2026-09-28","Talk submission":"2026-10-05","Poster submission":"2026-10-19"}[kind]
            found.append({
                "name":"QIP","edition":"2027",
                "fullName":"Quantum Information Processing",
                "type":kind,"deadline":date,"status":"official",
                "source":url,"note":"23:59 AoE"
            })
    return found

def main():
    old = json.loads(DATA.read_text())
    # In production, add parse_* adapters for QCrypt, TQC, QTML, QCTiP,
    # AQIS, QCNC, QCMC, IEEE QCE, QSim and QPL.
    fresh = parse_qip()
    if fresh:
        old = [x for x in old if not (x["name"]=="QIP" and x["edition"]=="2027")]
        old.extend(fresh)
    DATA.write_text(json.dumps(old, indent=2) + "\n")
    print(f"Updated {DATA}")

if __name__ == "__main__":
    main()
