# --- Automatyczne ustawienie zmiennych środowiskowych ---
import os

os.environ["ADZUNA_APP_ID"] = "62844f44"
os.environ["ADZUNA_APP_KEY"] = "818aa1df70e76f3730095c470c83b368"

# --- Wersja 2 programu ---
import sys
import webbrowser
import datetime as dt
import threading
import tempfile
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import re
import requests
import tkinter as tk
from tkinter import ttk, messagebox

# --- Konfiguracja / stałe ---
STALE_DAYS_THRESHOLD = 60
MAX_BATCH = 10
USER_AGENT = "SzukamPracy/1.2 (+local)"
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")


@dataclass
class JobOffer:
    title: str
    company: str
    location: str
    url: str
    source: str
    created_at: dt.datetime
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None
    description: str = ""
    is_stale: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def salary_score(self) -> float:
        if self.salary_max:
            return float(self.salary_max)
        if self.salary_min:
            return float(self.salary_min)
        return 0.0

    def freshness_score(self) -> float:
        age_days = (dt.datetime.now(dt.timezone.utc) - self.created_at).days
        return 1.0 / (1 + max(age_days, 0))

    def total_score(self) -> float:
        return 0.7 * self.salary_score() + 0.3 * self.freshness_score()

# --- Pomocnicze ---
def parse_iso(iso_str: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return dt.datetime.now(dt.timezone.utc)

def mark_stale(created_at: dt.datetime, threshold_days: int = STALE_DAYS_THRESHOLD) -> bool:
    return (dt.datetime.now(dt.timezone.utc) - created_at).days >= threshold_days

def normalize_city(city: str) -> str:
    return city.strip()

# --- Filtry doświadczenia ---
SENIOR_RE = re.compile(r"\b(senior|sr\.?|lead|principal|expert)\b", re.I)
EXP_PATTERNS = [
    re.compile(r"\b([0-9]{1,2})\s*(?:years?|yrs?|lat|lata)\b", re.I),           # "4 years", "5 lat"
    re.compile(r"\b(at\s*least|min(?:imum)?\.?)\s*([0-9]{1,2})\s*(?:years?|yrs?|lat|lata)\b", re.I),
    re.compile(r"\b([0-9]{1,2})\s*(?:\+|\s*or\s*more)\s*(?:years?|yrs?|lat|lata)\b", re.I),  # "3+ years"
    re.compile(r">\s*([0-9]{1,2})\s*(?:years?|yrs?|lat|lata)\b", re.I),                      # "> 3 years"
]

def requires_more_than(title: str, desc: str, max_years: int) -> bool:
    text = f"{title}\n{desc}".lower()
    if SENIOR_RE.search(text):
        return True
    for rx in EXP_PATTERNS:
        for m in rx.finditer(text):
            # znajdź liczbę w pasującym wzorcu
            years = None
            # grupy mogą być na 1. lub 2. pozycji w zależności od wzorca
            for g in m.groups():
                if g and g.strip().isdigit():
                    years = int(g)
                    break
            if years is None:
                continue
            # „3+” lub „>3” traktujemy jako >3
            if "or more" in m.group(0) or "+" in m.group(0) or ">" in m.group(0):
                if years >= max_years + 1:
                    return True
                if years == max_years and ("+" in m.group(0) or "or more" in m.group(0)):
                    return True
            if years > max_years:
                return True
    return False

# --- Dostawcy ---
def fetch_adzuna(job_title: str, work_type: str, city: Optional[str], max_years: int) -> List[JobOffer]:
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return []
    base = "https://api.adzuna.com/v1/api/jobs/pl/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": 50,
        "what": job_title,
        "content-type": "application/json",
        "sort_by": "date",
    }
    what_extra = []
    wt = work_type.lower()
    if wt == "remote":
        what_extra += ["remote", "zdaln*"]
    elif wt in ("hybrydowa", "stacjonarna"):
        if wt == "hybrydowa":
            what_extra += ["hybryd*"]
        else:
            what_extra += ["stacjonarn*", "on-site"]
        if city:
            params["where"] = normalize_city(city)
    if what_extra:
        params["what_and"] = " ".join(what_extra)

    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(base, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Adzuna] error: {e}", file=sys.stderr)
        return []

    out: List[JobOffer] = []
    for it in data.get("results", []):
        created = parse_iso(it.get("created", ""))
        desc = (it.get("description") or "")[:4000]
        offer = JobOffer(
            title=it.get("title") or "Brak tytułu",
            company=(it.get("company") or {}).get("display_name") or "N/D",
            location=(it.get("location") or {}).get("display_name") or "Polska",
            url=it.get("redirect_url") or it.get("adref") or "",
            source="Adzuna",
            created_at=created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc),
            salary_min=it.get("salary_min"),
            salary_max=it.get("salary_max"),
            currency=it.get("salary_currency"),
            description=desc,
            meta={"category": (it.get("category") or {}).get("label")},
        )
        offer.is_stale = mark_stale(offer.created_at)
        if not requires_more_than(offer.title, offer.description, max_years):
            out.append(offer)
    return out

def fetch_remotive(job_title: str, max_years: int) -> List[JobOffer]:
    url = "https://remotive.com/api/remote-jobs"
    headers = {"User-Agent": USER_AGENT}
    params = {"search": job_title}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[Remotive] error: {e}", file=sys.stderr)
        return []

    out: List[JobOffer] = []
    for it in data.get("jobs", []):
        created = parse_iso(it.get("publication_date", ""))
        salary_str = (it.get("salary") or "").replace(" ", "")
        salary_min = salary_max = None
        m = re.search(r"(\d[\d.,]*)\D+(\d[\d.,]*)", salary_str)
        if m:
            try:
                salary_min = float(m.group(1).replace(",", "."))
                salary_max = float(m.group(2).replace(",", "."))
            except Exception:
                pass
        mcur = re.search(r"(USD|EUR|PLN|\$|€|zł)", salary_str, re.I)
        currency = None
        if mcur:
            cur = mcur.group(1).upper()
            currency = {"$": "USD", "€": "EUR", "ZŁ": "PLN"}.get(cur, cur)

        desc = it.get("description") or ""
        offer = JobOffer(
            title=it.get("title") or "Brak tytułu",
            company=it.get("company_name") or "N/D",
            location=it.get("candidate_required_location") or "Remote",
            url=it.get("url") or "",
            source="Remotive",
            created_at=created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc),
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            description=desc[:4000],
            meta={"job_type": it.get("job_type")},
        )
        offer.is_stale = mark_stale(offer.created_at)
        if not requires_more_than(offer.title, offer.description, max_years):
            out.append(offer)
    return out

def dedupe_by_url(offers: List[JobOffer]) -> List[JobOffer]:
    seen, out = set(), []
    for o in offers:
        if not o.url or o.url in seen:
            continue
        seen.add(o.url)
        out.append(o)
    return out

def rank_offers(offers: List[JobOffer]) -> List[JobOffer]:
    return sorted(offers, key=lambda o: o.total_score(), reverse=True)

# --- Otwieranie i zamykanie okna przeglądarki dla partii 10 ---
class BatchBrowser:
    """
    Próbuje otwierać każdą partię 10 linków w ODDZIELNYM oknie Chrome/Edge
    z tymczasowym profilem – wtedy możemy je zamknąć.
    Fallback: webbrowser (bez zamykania).
    """
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.tmpdir: Optional[str] = None
        self.browser_cmd = self._find_browser()

    def _find_browser(self) -> Optional[List[str]]:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            # Linux/macOS (na wypadek)
            "google-chrome", "chromium", "msedge", "open", "xdg-open",
        ]
        for c in candidates:
            if os.path.isfile(c) or shutil.which(c):
                return [c]
        return None

    def close_previous(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        if self.tmpdir and os.path.isdir(self.tmpdir):
            try:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
            except Exception:
                pass
        self.proc, self.tmpdir = None, None

    def open_urls(self, urls: List[str]) -> bool:
        # zamknij poprzednie okno (jeśli było)
        self.close_previous()
        if not urls:
            return True
        if self.browser_cmd:
            # uruchom osobne okno z tymczasowym profilem
            self.tmpdir = tempfile.mkdtemp(prefix="szukam_pracy_profile_")
            args = self.browser_cmd + [
                f"--user-data-dir={self.tmpdir}",
                "--no-first-run", "--no-default-browser-check",
                "--new-window",
            ] + urls
            try:
                self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except Exception:
                pass
        # fallback – bez zamykania poprzednich
        for u in urls:
            webbrowser.open_new_tab(u)
        return False

# --- GUI ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Szukam pracy")
        self.geometry("1080x720")
        self.resizable(True, True)

        self.all_ranked: List[JobOffer] = []
        self.next_index: int = 0
        self.opened_urls: set[str] = set()
        self.batch_browser = BatchBrowser()

        # Form
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="x")

        ttk.Label(frm, text="Job title:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.job_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.job_var, width=40).grid(row=0, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(frm, text="Rodzaj pracy:").grid(row=0, column=2, sticky="w", padx=12, pady=4)
        self.type_var = tk.StringVar(value="Remote")
        self.cmb = ttk.Combobox(frm, textvariable=self.type_var,
                                values=["Remote", "Hybrydowa", "Stacjonarna"],
                                state="readonly", width=14)
        self.cmb.grid(row=0, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(frm, text="Miasto:").grid(row=0, column=4, sticky="w", padx=12, pady=4)
        self.city_var = tk.StringVar()
        self.city_entry = ttk.Entry(frm, textvariable=self.city_var, width=24, state="disabled")
        self.city_entry.grid(row=0, column=5, sticky="w", padx=4, pady=4)

        def on_type_change(_evt=None):
            t = self.type_var.get().lower()
            self.city_entry.configure(state="disabled" if t == "remote" else "normal")
        self.cmb.bind("<<ComboboxSelected>>", on_type_change)

        self.btn_search = ttk.Button(frm, text="Szukaj", command=self.on_search_click)
        self.btn_search.grid(row=0, column=6, sticky="w", padx=12, pady=4)

        # Suwak lat doświadczenia
        ttk.Label(frm, text="Max lat doświadczenia:").grid(row=1, column=0, sticky="w", padx=4, pady=(8,4))
        self.exp_var = tk.IntVar(value=3)
        self.exp_scale = ttk.Scale(frm, from_=0, to=10, orient="horizontal",
                                   command=lambda v: self.exp_label.configure(text=f"{int(float(v))}"),
                                   length=240)
        self.exp_scale.set(3)
        self.exp_scale.grid(row=1, column=1, sticky="w", padx=4, pady=(8,4))
        self.exp_label = ttk.Label(frm, text="3")
        self.exp_label.grid(row=1, column=2, sticky="w", padx=6, pady=(8,4))

        # Przycisk partii 10
        self.btn_more = ttk.Button(frm, text="Znajdź 10 nowych ofert", command=self.on_open_next, state="disabled")
        self.btn_more.grid(row=1, column=6, sticky="w", padx=12, pady=(8,4))

        # Progress
        pfrm = ttk.Frame(self, padding=(12, 0))
        pfrm.pack(fill="x")
        ttk.Label(pfrm, text="Postęp:").pack(side="left")
        self.progress = ttk.Progressbar(pfrm, mode="determinate", maximum=100)
        self.progress.pack(fill="x", expand=True, padx=8, pady=6)

        # Wyniki
        treefrm = ttk.Frame(self, padding=12)
        treefrm.pack(fill="both", expand=True)

        columns = ("title", "company", "location", "salary", "source", "created", "flag")
        self.tree = ttk.Treeview(treefrm, columns=columns, show="headings", height=22)
        for c, text, width in [
            ("title", "Tytuł", 360),
            ("company", "Firma", 170),
            ("location", "Lokalizacja", 160),
            ("salary", "Widełki", 120),
            ("source", "Źródło", 90),
            ("created", "Data", 110),
            ("flag", "Uwaga", 130),
        ]:
            self.tree.heading(c, text=text)
            self.tree.column(c, width=width, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        vs = ttk.Scrollbar(treefrm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.tag_configure("stale", foreground="red")

        # Status
        self.status_var = tk.StringVar(value="Gotowy.")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(12, 0)).pack(fill="x")

    def set_progress(self, pct: int, msg: str = ""):
        self.progress["value"] = pct
        if msg:
            self.status_var.set(msg)
        self.update_idletasks()

    def on_search_click(self):
        job = self.job_var.get().strip()
        if not job:
            messagebox.showwarning("Uwaga", "Podaj 'Job title'.")
            return
        wt = self.type_var.get()
        city = self.city_var.get().strip() if wt.lower() != "remote" else None
        self.max_years = int(float(self.exp_scale.get()))

        # wyczyść GUI
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.set_progress(0, "Szukam ofert...")
        self.all_ranked, self.next_index, self.opened_urls = [], 0, set()
        self.btn_more.configure(state="disabled")

        threading.Thread(target=self._do_search, args=(job, wt, city, self.max_years), daemon=True).start()

    def _do_search(self, job: str, work_type: str, city: Optional[str], max_years: int):
        offers: List[JobOffer] = []
        if work_type.lower() == "remote":
            self.set_progress(10, "Pobieram (Remotive)...")
            offers += fetch_remotive(job, max_years)
            self.set_progress(35, f"Z Remotive: {len(offers)}")
        self.set_progress(40, "Pobieram (Adzuna)...")
        offers += fetch_adzuna(job, work_type, city, max_years)
        self.set_progress(70, f"Po zebraniu i filtrach: {len(offers)}")

        offers = dedupe_by_url(offers)
        self.all_ranked = rank_offers(offers)
        self.next_index, self.opened_urls = 0, set()

        # wypełnij tabelę
        self.set_progress(85, "Przygotowuję listę...")
        for o in self.all_ranked:
            salary_txt = "N/D"
            if o.salary_min or o.salary_max:
                if o.salary_min and o.salary_max:
                    salary_txt = f"{int(o.salary_min)}–{int(o.salary_max)} {o.currency or ''}".strip()
                elif o.salary_max:
                    salary_txt = f"do {int(o.salary_max)} {o.currency or ''}".strip()
                else:
                    salary_txt = f"od {int(o.salary_min)} {o.currency or ''}".strip()
            flag = "WISI OD MIESIĘCY" if o.is_stale else ""
            tag = ("stale",) if o.is_stale else ()
            self.tree.insert("", "end",
                             values=(o.title, o.company, o.location, salary_txt, o.source,
                                     o.created_at.date().isoformat(), flag),
                             tags=tag)

        # automatycznie otwórz pierwszą 10 i włącz przycisk
        opened = self._open_next_batch(replace_previous=True)
        self.set_progress(100, f"Otwarto {opened}. Łącznie dostępnych: {len(self.all_ranked)}")
        self.btn_more.configure(state="normal" if self.next_index < len(self.all_ranked) else "disabled")

    # --- obsługa partii 10 ---
    def _open_next_batch(self, replace_previous: bool = False) -> int:
        urls = []
        count = 0
        start_idx = self.next_index
        while self.next_index < len(self.all_ranked) and count < MAX_BATCH:
            o = self.all_ranked[self.next_index]
            self.next_index += 1
            if not o.url or o.url in self.opened_urls:
                continue
            urls.append(o.url)
            self.opened_urls.add(o.url)
            count += 1

        if not urls:
            return 0

        # Otwórz w osobnym oknie (zamyka poprzednie, jeśli replace_previous=True)
        if replace_previous:
            self.batch_browser.close_previous()
        ok = self.batch_browser.open_urls(urls)
        if not ok:
            # fallback: nie mogliśmy kontrolować okna – tylko informacja
            self.status_var.set("Uwaga: przeglądarka nieobsługiwana – nie mogę zamykać poprzednich kart.")
        return len(urls)

    def on_open_next(self):
        if not self.all_ranked:
            return
        self.set_progress(10, "Znajduję 10 nowych i zamieniam poprzednie...")
        opened = self._open_next_batch(replace_previous=True)
        self.set_progress(100, f"Otwarto nowy zestaw: {opened}. Pozostało: {max(0, len(self.all_ranked)-self.next_index)}")
        if self.next_index >= len(self.all_ranked):
            self.btn_more.configure(state="disabled")

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
