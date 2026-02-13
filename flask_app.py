# flask_app.py
from flask import Flask, render_template_string, send_from_directory, abort, jsonify, request, Response
import os
import io
import csv
import json
import re

app = Flask(__name__)
BASE_DIR = os.environ.get('STUDY_BASE_DIR') or ('/home/clep/mysite' if os.path.isdir('/home/clep/mysite') else os.path.dirname(os.path.abspath(__file__)))

# -----------------------------
# Helpers
# -----------------------------
def resolve_subject_dir(subject: str):
    raw = (subject or "").strip()
    candidates = [
        raw,
        raw.lower(),
        raw.replace(" ", "_"),
        raw.lower().replace(" ", "_"),
    ]
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        p = os.path.join(BASE_DIR, cand)
        if os.path.isdir(p):
            return cand, p
    return raw, os.path.join(BASE_DIR, raw)


def _datatable_dirs(subject_dir: str):
    candidates = [
        os.path.join(subject_dir, "tables"),
        os.path.join(subject_dir, "datatables"),
        os.path.join(subject_dir, "data_tables"),
        os.path.join(subject_dir, "data"),
    ]
    return [d for d in candidates if os.path.isdir(d)]


def _pretty_sheet_name(filename: str):
    base = os.path.splitext(os.path.basename(filename))[0]
    base = base.replace("_", " ").strip()
    base = re.sub(r"\s*-\s*Table\s*\d+\s*$", "", base, flags=re.I).strip()
    base = re.sub(r"\s*\(\s*Table\s*\d+\s*\)\s*$", "", base, flags=re.I).strip()
    return base or os.path.basename(filename)


# -------- Combined DataTables (single CSV with repeated headers) --------
# If you place ONE CSV named like datatable(s).csv under <subject>/tables/ (or similar),
# and inside that file each section starts with a "header row" (module title in col1, column names after),
# the app will expose each section as its own table in the left pane.

_COMBINED_DT_CACHE = {}  # path -> {"mtime": float, "tables": [...], "by_id": {...}}

def _combined_datatable_candidates(subject_dir: str):
    # Flat layout: keep everything inside /home/clep/mysite/<subject>/ (no subfolders)
    preferred_names = (
        "datatable.csv",
        "datatables.csv",
        "data_table.csv",
        "data_tables.csv",
        "tables.csv",
    )

    out = []
    for name in preferred_names:
        p = os.path.join(subject_dir, name)
        if os.path.isfile(p):
            out.append(p)
    return out

def _is_section_header_row(cells, prev_title=None):
    """Heuristic: detect section header rows inside a combined datatable.csv.

    Expected pattern per section:
      <SECTION TITLE>, <col1>, <col2>, ...

    Data rows usually look like:
      <SECTION TITLE>, <value1>, <value2>, ... (often includes long sentences, quotes, periods)

    We classify a row as a header only if the cells after the title look like *column labels*:
    mostly short, no sentence punctuation/quotes, few words.
    """
    if not cells or len(cells) < 3:
        return False

    title = (cells[0] or "").strip()
    header_cells = [(c or "").strip() for c in cells[1:]]

    # Only consider a header at a section boundary (start of file or when the title changes)
    if prev_title is not None and title == prev_title:
        return False


    def looks_like_label(s: str) -> bool:
        if not s:
            return False
        # too long => probably data/definition
        if len(s) > 45:
            return False
        # sentences/quotes usually mean data
        if '"' in s or "'" in s:
            return False
        if "." in s or "?" in s or "!" in s or "," in s:
            return False
        # extremely wordy => likely data
        if len(s.split()) > 7:
            return False
        return True

    # Header rows should have *only* label-like cells after the title
    # (data rows almost always contain at least one long sentence / quote / punctuation cell).
    nonempty = [s for s in header_cells if s]
    if len(nonempty) < 2:
        return False

    headerish = all(looks_like_label(s) for s in nonempty)

    return headerish


def parse_combined_datatable_csv(full_path: str):
    """
    Parse a single CSV that contains multiple 'tables' concatenated together.
    Each table starts with a header row shaped like:
        <table title>, <col1>, <col2>, ...
    Followed by data rows shaped like:
        <same title>, <val1>, <val2>, ...
    Until the next header row.
    """
    with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        raw_rows = list(reader)

    tables = []
    current = None
    idx = 0
    prev_title = None

    for raw in raw_rows:
        cells = [(c.strip() if isinstance(c, str) else ("" if c is None else str(c))) for c in raw]

        # drop trailing empties (so "...,," doesn't create fake columns)
        while cells and cells[-1] == "":
            cells.pop()

        # skip blank lines
        if not cells or all(c == "" for c in cells):
            continue

        if _is_section_header_row(cells, prev_title=prev_title):
            idx += 1
            title = (cells[0] or "").strip() or f"Table {idx}"
            prev_title = (cells[0] or "").strip() or prev_title
            columns = [(c or "").strip() for c in cells[1:] if (c or "").strip()]

            # If there are duplicate column names, make them unique
            seen = {}
            uniq_cols = []
            for c in columns:
                if c in seen:
                    seen[c] += 1
                    uniq_cols.append(f"{c} ({seen[c]})")
                else:
                    seen[c] = 1
                    uniq_cols.append(c)

            current = {
                "id": f"combined__{idx:02d}",
                "name": title,
                "columns": uniq_cols,
                "rows": [],
            }
            tables.append(current)
            continue

        # data row
        row_title = (cells[0] or "").strip()
        if row_title:
            prev_title = row_title

        if not current:
            # ignore junk before first header
            continue

        vals = cells[1:]  # first cell repeats title/module
        if len(vals) < len(current["columns"]):
            vals = vals + [""] * (len(current["columns"]) - len(vals))
        else:
            vals = vals[:len(current["columns"])]

        row_dict = {current["columns"][i]: (vals[i].strip() if isinstance(vals[i], str) else vals[i]) for i in range(len(current["columns"]))}
        # drop rows that are completely empty
        any_text = any((v or "").strip() for v in row_dict.values() if isinstance(v, str))
        any_nontext = any(v for v in row_dict.values() if not isinstance(v, str))
        if any_text or any_nontext:
            current["rows"].append(row_dict)

    return tables

def get_combined_datatables(subject_dir: str):
    path = next((p for p in _combined_datatable_candidates(subject_dir)), None)
    if not path:
        return None

    mtime = os.path.getmtime(path)
    cached = _COMBINED_DT_CACHE.get(path)
    if cached and cached.get("mtime") == mtime:
        return cached

    tables = parse_combined_datatable_csv(path)
    pack = {
        "path": path,
        "mtime": mtime,
        "tables": tables,
        "by_id": {t["id"]: t for t in tables},
    }
    _COMBINED_DT_CACHE[path] = pack
    return pack

def write_table_to_csv_string(table: dict):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(table.get("columns") or [])
    for r in (table.get("rows") or []):
        writer.writerow([(r.get(c, "") if isinstance(r, dict) else "") for c in (table.get("columns") or [])])
    return buf.getvalue()



def list_datatables(subject_dir: str):
    # If a combined datatable CSV exists, expose each section as its own table "sheet"
    combined = get_combined_datatables(subject_dir)
    if combined and combined.get("tables"):
        out = []
        used = {}
        for t in combined["tables"]:
            name = t.get("name") or t.get("id")
            # de-dupe display names
            if name in used:
                used[name] += 1
                name = f"{name} ({used[name]})"
            else:
                used[name] = 1
            out.append({"id": t["id"], "name": name, "full": combined["path"]})
        return out

    # Flat layout fallback: show each CSV/JSON file in the subject root that looks like a table
    results = []
    for fname in sorted(os.listdir(subject_dir)):
        lo = fname.lower()
        if fname.startswith("."):
            continue
        if not (lo.endswith(".csv") or lo.endswith(".json")):
            continue
        # Avoid listing quiz/flashcards/resources as "tables"
        if fname.lower() in ("quiz.csv", "flashcards.csv", "resources.json", "mindmap.md", "markmap.md"):
            continue
        if not any(k in lo for k in ("table", "datatable", "sheet")):
            continue
        full = os.path.join(subject_dir, fname)
        if os.path.isfile(full):
            results.append((fname, _pretty_sheet_name(fname), full))

    used = {}
    out = []
    for fid, name, full in results:
        n = name
        if n in used:
            used[n] += 1
            n = f"{n} ({used[name]})"
        else:
            used[n] = 1
        out.append({"id": fid, "name": n, "full": full})
    return out

    # Default behavior: each CSV/JSON file is its own table
    results = []

    dirs = _datatable_dirs(subject_dir)
    if dirs:
        for d in dirs:
            for fname in sorted(os.listdir(d)):
                lo = fname.lower()
                if fname.startswith("."):
                    continue
                if not (lo.endswith(".csv") or lo.endswith(".json")):
                    continue
                full = os.path.join(d, fname)
                if os.path.isfile(full):
                    results.append((fname, _pretty_sheet_name(fname), full))
    else:
        for fname in sorted(os.listdir(subject_dir)):
            lo = fname.lower()
            if fname.startswith("."):
                continue
            if not (lo.endswith(".csv") or lo.endswith(".json")):
                continue
            if not any(k in lo for k in ("table", "datatable", "sheet")):
                continue
            full = os.path.join(subject_dir, fname)
            if os.path.isfile(full):
                results.append((fname, _pretty_sheet_name(fname), full))

    used = {}
    out = []
    for fid, name, full in results:
        n = name
        if n in used:
            used[n] += 1
            n = f"{n} ({used[name]})"
        else:
            used[n] = 1
        out.append({"id": fid, "name": n, "full": full})
    return out



def load_table_file(full_path: str):
    lo = full_path.lower()
    if lo.endswith(".csv"):
        with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for r in reader:
                rows.append({k: (v.strip() if isinstance(v, str) else v) for k, v in r.items()})
            columns = list(reader.fieldnames or [])
            return columns, rows

    if lo.endswith(".json"):
        with open(full_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "rows" in data and "columns" in data:
            cols = data.get("columns") or []
            rows = data.get("rows") or []
            if isinstance(cols, list) and isinstance(rows, list):
                return cols, rows
        if isinstance(data, list):
            rows = [x for x in data if isinstance(x, dict)]
            cols = []
            for r in rows:
                for k in r.keys():
                    if k not in cols:
                        cols.append(k)
            return cols, rows

    return [], []


# -------- Flashcards --------
def _flashcards_paths(subject_dir: str):
    return [
        os.path.join(subject_dir, "flashcards", "flashcards.csv"),
        os.path.join(subject_dir, "flashcards.csv"),
    ]


def load_flashcards(subject_dir: str):
    """
    Expected columns (case-insensitive):
      front, back, Module, CLEP Trap
    """
    path = next((p for p in _flashcards_paths(subject_dir) if os.path.exists(p)), None)
    if not path:
        return []

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        lower_map = {c.strip().lower(): c for c in fieldnames}

        def get_col(row, name, default=""):
            key = lower_map.get(name.lower())
            if not key:
                return default
            v = row.get(key, default)
            return v.strip() if isinstance(v, str) else (v if v is not None else default)

        cards = []
        for r in reader:
            front = get_col(r, "front", "")
            back = get_col(r, "back", "")
            module = get_col(r, "Module", "")
            clep_trap = get_col(r, "CLEP Trap", "")

            if not front and not back and not clep_trap:
                continue

            if not module:
                module = "Uncategorized"

            cards.append({
                "front": front,
                "back": back,
                "module": module,
                "clep_trap": clep_trap
            })
        return cards


def flashcard_modules(cards):
    counts = {}
    for c in cards:
        m = c.get("module") or "Uncategorized"
        counts[m] = counts.get(m, 0) + 1

    def keyfn(m):
        mm = m.strip()
        nums = re.findall(r"\d+", mm)
        return (0, int(nums[0])) if nums else (1, mm.lower())

    modules = sorted(counts.keys(), key=keyfn)
    return [{"name": m, "count": counts[m]} for m in modules]


# -------- Quiz --------
def _quiz_paths(subject_dir: str):
    return [
        os.path.join(subject_dir, "quiz", "quiz.csv"),
        os.path.join(subject_dir, "quiz.csv"),
    ]


def load_quiz(subject_dir: str):
    """
    Supports flexible quiz CSVs.
    Typical columns:
      Module (optional), Question, Option A..E (or more), Answer, Explanation (optional), CLEP Trap (optional)
    Your uploaded quiz.csv: Question, Option A..E, Answer
    """
    path = next((p for p in _quiz_paths(subject_dir) if os.path.exists(p)), None)
    if not path:
        return [], path

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        lower_map = {c.strip().lower(): c for c in fieldnames}

        def col(name):
            return lower_map.get(name.lower())

        module_col = col("module")
        q_col = col("question")
        ans_col = col("answer")
        exp_col = col("explanation")
        trap_col = col("clep trap")

        # option columns: anything starting with "option " (case-insensitive)
        option_cols = []
        for c in fieldnames:
            if c and c.strip().lower().startswith("option"):
                option_cols.append(c)

        # sort options by letter if possible (Option A, Option B, ...)
        def opt_key(cname):
            m = re.search(r"option\s*([a-z])", cname.strip(), flags=re.I)
            if m:
                return (0, m.group(1).lower())
            return (1, cname.lower())

        option_cols = sorted(option_cols, key=opt_key)

        items = []
        for r in reader:
            question = (r.get(q_col, "") if q_col else "").strip()
            if not question:
                continue

            module = (r.get(module_col, "").strip() if module_col else "")
            if not module:
                module = "All Questions"

            answer = (r.get(ans_col, "") if ans_col else "")
            answer = (answer or "").strip().upper()[:1]  # A/B/C...

            explanation = (r.get(exp_col, "") if exp_col else "")
            explanation = (explanation or "").strip()

            clep_trap = (r.get(trap_col, "") if trap_col else "")
            clep_trap = (clep_trap or "").strip()

            options = []
            for oc in option_cols:
                val = (r.get(oc) or "").strip()
                if not val:
                    continue
                # label extraction
                m = re.search(r"option\s*([a-z])", oc.strip(), flags=re.I)
                label = (m.group(1).upper() if m else oc.strip())
                options.append({"label": label, "text": val})

            items.append({
                "module": module,
                "question": question,
                "options": options,
                "answer": answer,
                "explanation": explanation,
                "clep_trap": clep_trap,
            })

        return items, path


def quiz_modules(items):
    counts = {}
    for it in items:
        m = it.get("module") or "All Questions"
        counts[m] = counts.get(m, 0) + 1

    def keyfn(m):
        mm = m.strip()
        if mm.lower() == "all questions":
            return (0, 0)
        nums = re.findall(r"\d+", mm)
        return (1, int(nums[0])) if nums else (2, mm.lower())

    modules = sorted(counts.keys(), key=keyfn)
    return [{"name": m, "count": counts[m]} for m in modules]


# -------- Resources --------
def _resources_paths(subject_dir: str):
    return [
        os.path.join(subject_dir, "resources", "resources.json"),
        os.path.join(subject_dir, "resources.json"),
    ]


def load_resources(subject_dir: str):
    path = next((p for p in _resources_paths(subject_dir) if os.path.exists(p)), None)
    if not path:
        return [], path

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # expected: list[{section, items:[{title,url|file,tag}]}]
    if not isinstance(data, list):
        return [], path

    normalized = []
    for block in data:
        if not isinstance(block, dict):
            continue
        section = (block.get("section") or "").strip()
        items = block.get("items") or []
        if not section:
            section = "Resources"
        if not isinstance(items, list):
            items = []
        cleaned = []
        for it in items:
            if not isinstance(it, dict):
                continue
            title = (it.get("title") or "").strip()
            url = (it.get("url") or "").strip()
            file_ = (it.get("file") or "").strip()
            tag = (it.get("tag") or "").strip()
            if not title:
                continue
            if not url and not file_:
                continue
            cleaned.append({"title": title, "url": url, "file": file_, "tag": tag})
        normalized.append({"section": section, "items": cleaned})

    return normalized, path


def resource_sections(resources):
    counts = {}
    for b in resources:
        s = b.get("section") or "Resources"
        counts[s] = counts.get(s, 0) + len(b.get("items") or [])
    return [{"name": s, "count": counts[s]} for s in sorted(counts.keys(), key=lambda x: x.lower())]


# -----------------------------
# Templates
# -----------------------------
LIBRARY_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Study Library</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root{
      --bg:#0b1220;
      --card:#0f1a2e;
      --card2:#0c1628;
      --text:#e6edf7;
      --muted:#9fb0c7;
      --accent:#63b3ed;
      --border:rgba(255,255,255,0.10);
      --shadow: 0 14px 40px rgba(0,0,0,0.35);
    }
    body{
      margin:0;
      font-family: Inter, Segoe UI, system-ui, -apple-system, sans-serif;
      background: radial-gradient(1200px 600px at 20% -10%, rgba(99,179,237,0.25), transparent 60%),
                  radial-gradient(900px 500px at 90% 10%, rgba(88,101,242,0.18), transparent 55%),
                  var(--bg);
      color: var(--text);
      min-height:100vh;
      padding: 40px 18px 60px;
      box-sizing:border-box;
    }
    .wrap{max-width:1100px;margin:0 auto;}
    .top{
      display:flex;
      gap:14px;
      align-items:flex-end;
      justify-content:space-between;
      flex-wrap:wrap;
      margin-bottom:18px;
    }
    .title{
      font-size: 34px;
      font-weight: 900;
      letter-spacing: -0.02em;
      margin:0;
    }
    .subtitle{
      margin:6px 0 0;
      color: var(--muted);
      font-weight: 600;
    }
    .search{
      min-width: 260px;
      max-width: 420px;
      width: 100%;
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.06);
      color: var(--text);
      outline: none;
      box-shadow: 0 8px 22px rgba(0,0,0,0.20);
    }
    .search::placeholder{color: rgba(230,237,247,0.55);}
    .grid{
      display:grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 14px;
      margin-top: 16px;
    }
    .card{
      display:block;
      text-decoration:none;
      color: var(--text);
      background: linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.03));
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 16px 16px 14px;
      box-shadow: var(--shadow);
      transition: transform .12s ease, border-color .12s ease, background .12s ease;
      position: relative;
      overflow:hidden;
    }
    .card::before{
      content:"";
      position:absolute;
      inset:-2px -2px auto -2px;
      height: 4px;
      background: linear-gradient(90deg, var(--accent), rgba(99,179,237,0.2), transparent);
      opacity:0.9;
    }
    .card:hover{
      transform: translateY(-2px);
      border-color: rgba(99,179,237,0.35);
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
    }
    .card-title{
      font-weight: 900;
      font-size: 16px;
      line-height: 1.25;
      margin: 2px 0 10px;
    }
    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding: 7px 10px;
      border-radius: 999px;
      background: rgba(99,179,237,0.14);
      border: 1px solid rgba(99,179,237,0.22);
      color: rgba(230,237,247,0.95);
      font-weight: 800;
      font-size: 12px;
    }
    .meta{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-top: 10px;
    }
    .empty{
      margin-top: 18px;
      padding: 18px;
      border: 1px dashed var(--border);
      border-radius: 18px;
      color: var(--muted);
      font-weight: 700;
      background: rgba(255,255,255,0.03);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
<h1 class="title">🚀 CLEP Command Center</h1>
      <p class="subtitle">Accelerated Exam Preparation & Mastery</p>
      </div>
      <input id="q" class="search" placeholder="Search subjects…" autocomplete="off">
    </div>

    {% if books|length == 0 %}
      <div class="empty">No subjects found under the base folder.</div>
    {% else %}
      <div id="grid" class="grid">
        {% for book in books %}
          <a class="card" href="/study/{{ book }}" data-name="{{ book|lower }}">
            <div class="pill">📚 Subject</div>
            <div class="card-title">{{ book|replace('_',' ')|title }}</div>
            <div class="meta">Open study hub</div>
          </a>
        {% endfor %}
      </div>
    {% endif %}
  </div>

  <script>
    const q = document.getElementById('q');
    const cards = Array.from(document.querySelectorAll('.card'));
    function filter(){
      const v = (q.value || '').trim().toLowerCase();
      cards.forEach(c => c.style.display = c.dataset.name.includes(v) ? '' : 'none');
    }
    q && q.addEventListener('input', filter);
  </script>
</body>
</html>
"""

STUDY_HTML = r"""
<!doctype html>
<html>
<head>
  <title>{{ display_subject }}</title>

  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">

  <!-- Markmap deps (LOCAL) -->
  <script src="{{ url_for('static', filename='vendor/markmap/d3.min.js') }}"></script>
  <script src="{{ url_for('static', filename='vendor/markmap/markmap-lib.iife.js') }}"></script>
  <script src="{{ url_for('static', filename='vendor/markmap/markmap-view.js') }}"></script>

  <style>
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: 'Inter', sans-serif;
      background: #f0f2f5;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    /* --- TOP NAV --- */
    .top-nav {
      height: 60px;
      background: #fff;
      border-bottom: 1px solid #ddd;
      padding: 0 18px;
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      gap: 12px;
      flex-shrink: 0;
      z-index: 1000;
    }
    .nav-left, .nav-right { display: flex; gap: 10px; align-items: center; }
    .nav-right { justify-content: flex-end; }
    .nav-center {
      font-weight: 900;
      color: #2d3748;
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }
    .tool-btn {
      padding: 8px 12px;
      border-radius: 8px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      border: 1px solid #e2e8f0;
      background: #f7fafc;
      color: #4a5568;
      user-select: none;
    }
    .tool-btn:hover { background: #edf2f7; }
    .tool-btn.active { background: #2d3748; border-color: #2d3748; color: #fff; }

    /* --- MAIN LAYOUT --- */
    .main-row { flex: 1; display: flex; min-height: 0; }

    /* Sidebar */
    #sidebar {
      width: 320px;
      position: relative;
      z-index: 5;
      margin: 20px;
      background: rgba(44, 62, 80, 0.98);
      color: white;
      border-radius: 18px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
    }

    /* Hide sidebar for tool full-screen modes (slides/mindmap only) */
    body.tool-mode #sidebar { display: none; }

    .home-btn {
      display: block;
      padding: 14px 18px;
      color: #cbd5e0;
      text-decoration: none;
      font-weight: 700;
      font-size: 13px;
      background: rgba(0,0,0,0.12);
    }
    .home-btn:hover { background: rgba(0,0,0,0.18); }

    .header {
      padding: 16px 18px;
      font-weight: 900;
      font-size: 14px;
      border-bottom: 1px solid rgba(255,255,255,0.1);
    }

    #toc-notes, #toc-tables, #toc-flashcards, #toc-quiz, #toc-resources {
      flex: 1;
      overflow: auto;
      padding: 12px;
      display: none;
    }

    /* default: notes toc visible */
    #toc-notes { display: block; }

    /* Mode-based TOC switching */
    body.datatable-mode #toc-notes { display: none; }
    body.datatable-mode #toc-tables { display: block; }
    body.datatable-mode #toc-flashcards,
    body.datatable-mode #toc-quiz,
    body.datatable-mode #toc-resources { display: none; }

    body.flashcards-mode #toc-notes,
    body.flashcards-mode #toc-tables,
    body.flashcards-mode #toc-quiz,
    body.flashcards-mode #toc-resources { display: none; }
    body.flashcards-mode #toc-flashcards { display: block; }

    body.quiz-mode #toc-notes,
    body.quiz-mode #toc-tables,
    body.quiz-mode #toc-flashcards,
    body.quiz-mode #toc-resources { display: none; }
    body.quiz-mode #toc-quiz { display: block; }

    body.resources-mode #toc-notes,
    body.resources-mode #toc-tables,
    body.resources-mode #toc-flashcards,
    body.resources-mode #toc-quiz { display: none; }
    body.resources-mode #toc-resources { display: block; }

    .toc-item {
      display: block;
      user-select: none;
      -webkit-user-select: none;
      cursor: pointer;
      padding: 12px 15px;
      margin-bottom: 10px;
      border-radius: 12px;
      background: rgba(255,255,255,0.06);
      color: #cbd5e0;
      text-decoration: none;
      font-size: 0.9rem;
      transition: 0.2s;
    }
    .toc-item:hover { background: rgba(255,255,255,0.12); }
    .toc-item.active { background: #63b3ed !important; color: white !important; font-weight: 900; }

    /* Nested TOC levels (notes) */
    .toc-h1 { font-weight: 900; color: #fff; }
    .toc-module { font-weight: 900; color: #fff; background: rgba(0,0,0,0.10); margin-top: 12px; }
    .toc-h2 { padding-left: 35px; font-size: 0.86rem; opacity: 0.95; }
    .toc-h3 { padding-left: 55px; font-size: 0.82rem; opacity: 0.9; }


    .pill-btn{
      padding: 8px 10px;
      border-radius: 999px;
      border: 1px solid #e2e8f0;
      background: #ffffff;
      color: #0f172a;
      font-weight: 800;
      cursor: pointer;
    }
    .pill-btn:hover{ background:#f8fafc; }
/* Collapse notes by default */
    .toc-h2, .toc-h3 { display: none; }

    /* Next/Prev controls (notes only) - moved to main content */
    #content-nav{
      display: flex;
      gap: 10px;
      padding: 12px 14px;
      margin: 18px 18px 0;
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 14px;
      box-shadow: 0 8px 18px rgba(0,0,0,0.05);
    }
    /* Only show in notes */
    body.datatable-mode #content-nav,
    body.flashcards-mode #content-nav,
    body.quiz-mode #content-nav,
    body.resources-mode #content-nav,
    body.tool-mode #content-nav { display: none; }
.nav-btn{
      flex: 1;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #e2e8f0;
      background: #f1f5f9;
      color: #0f172a;
      font-weight: 800;
      cursor: pointer;
    }
    .nav-btn:hover{ background:#e8eef6; }
    .nav-btn:disabled{
      opacity: 0.45;
      cursor: not-allowed;
    }
    .nav-btn:hover { background: rgba(255,255,255,0.14); }
    .nav-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* Content column */
    #content { flex: 1; overflow: hidden; min-height: 0; padding: 0; }

    /* Notes card */
    #display-area {
      max-width: 900px;
      margin: 20px auto;
      padding: 40px;
      background: white;
      border-radius: 15px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.05);
      min-height: 60vh;
      line-height: 1.7;
      overflow: auto;
      height: calc(100% - 40px);
    }

    /* Tool pages full-width */
    body.tool-mode #display-area {
      max-width: 1100px;
      padding: 0;
      background: transparent;
      box-shadow: none;
      height: calc(100% - 40px);
    }
    body.datatable-mode #display-area,
    body.flashcards-mode #display-area,
    body.quiz-mode #display-area,
    body.resources-mode #display-area {
      max-width: 1100px;
      padding: 0;
      background: transparent;
      box-shadow: none;
      height: calc(100% - 40px);
    }

    .panel {
      background: #fff;
      border-radius: 14px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.05);
      overflow: hidden;
      height: calc(100% - 40px);
      margin: 20px auto;
      max-width: 1100px;
      display: flex;
      flex-direction: column;
    }
    .panel-header {
      padding: 12px 16px;
      border-bottom: 1px solid #e2e8f0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }
    .panel-title { font-weight: 900; }
    .panel-link { font-size: 13px; font-weight: 800; color: #2b6cb0; text-decoration: none; }
    .panel-link:hover { text-decoration: underline; }
    .panel-body { flex: 1; min-height: 0; background: #f8fafc; position: relative; }

    /* Slide iframe */
    #slide-iframe { width: 100%; height: 100%; border: 0; background: #fff; }

    /* Mindmap */
    #mindmap { width: 100%; height: 100%; display: block; background: #fff; }

    /* Preserve bold/italic from guide.html */
    .force-bold { font-weight: 900 !important; }
    .force-italic { font-style: italic !important; }

    /* Data table */
    .dt-controls {
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid #e2e8f0;
      background: #fff;
    }
    .dt-controls input {
      flex: 1;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #e2e8f0;
      outline: none;
      font-weight: 600;
    }
    .dt-controls select {
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #e2e8f0;
      outline: none;
      font-weight: 700;
      background: #fff;
    }
    .dt-meta { font-size: 12px; font-weight: 800; color: #4a5568; white-space: nowrap; }
    .dt-table-wrap { flex: 1; min-height: 0; overflow: auto; background: #fff; }
    table.dt { width: 100%; border-collapse: collapse; font-size: 14px; }
    table.dt th, table.dt td { padding: 12px 12px; border-bottom: 1px solid #edf2f7; vertical-align: top; }
    table.dt th {
      position: sticky; top: 0; z-index: 5;
      background: #f7fafc; font-weight: 900; cursor: pointer; user-select: none; white-space: nowrap;
    }
    table.dt tr:hover td { background: #f8fafc; }
    .dt-pager {
      display: flex; gap: 10px; align-items: center; justify-content: flex-end;
      padding: 12px 14px; border-top: 1px solid #e2e8f0; background: #fff;
    }
    .dt-pager button {
      padding: 8px 12px; border-radius: 10px; border: 1px solid #e2e8f0; background: #f7fafc;
      cursor: pointer; font-weight: 900;
    }
    .dt-pager button:disabled { opacity: 0.5; cursor: not-allowed; }
    .dt-sort { font-size: 12px; opacity: 0.8; margin-left: 6px; }

    /* Flashcards */
    .fc-toolbar, .qz-toolbar, .rs-toolbar {
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 12px 14px;
      border-bottom: 1px solid #e2e8f0;
      background: #fff;
      flex-wrap: wrap;
    }
    .fc-pill, .qz-pill, .rs-pill {
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid #e2e8f0;
      background: #f7fafc;
      font-weight: 900;
      font-size: 12px;
      color: #2d3748;
    }
    .fc-btn, .qz-btn {
      padding: 8px 12px;
      border-radius: 10px;
      border: 1px solid #e2e8f0;
      background: #f7fafc;
      cursor: pointer;
      font-weight: 900;
    }
    .fc-btn:hover, .qz-btn:hover { background: #edf2f7; }
    .fc-btn:disabled, .qz-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .fc-card-wrap {
      flex: 1;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: #f8fafc;
    }
    .fc-card {
      width: min(860px, 100%);
      background: #fff;
      border-radius: 18px;
      padding: 26px;
      cursor: pointer;
      user-select: none;
      text-align: center;

      border: 2px solid #e2e8f0;
      box-shadow:
        0 18px 35px rgba(0,0,0,0.12),
        0 2px 0 rgba(255,255,255,0.7) inset;

      min-height: 420px;
      position: relative;
    }
    .fc-card::before {
      content: "";
      position: absolute;
      top: -14px;
      left: 50%;
      transform: translateX(-50%);
      width: 140px;
      height: 22px;
      border-radius: 999px;
      background: #edf2f7;
      border: 2px solid #e2e8f0;
      box-shadow: 0 8px 16px rgba(0,0,0,0.12);
    }

    .fc-inner {
      height: 100%;
      min-height: 360px;
      display: flex;
      flex-direction: column;
    }
    .fc-main {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .fc-trap-block { margin-top: auto; }

    .fc-front {
      font-weight: 900 !important;
      font-size: 22px;
      color: #1a202c;
      line-height: 1.35;
    }
    .fc-back {
      font-size: 16px;
      color: #2d3748;
      line-height: 1.7;
    }
    .fc-divider { height: 1px; background: #edf2f7; margin: 16px 0; }
    .fc-trap-title {
      font-weight: 900 !important;
      color: #c53030 !important;
      margin-bottom: 6px;
      text-align: center;
    }
    .fc-trap {
      background: #fff5f5;
      border: 1px solid #fed7d7;
      padding: 12px;
      border-radius: 12px;
      color: #742a2a;
      font-weight: 700;
      text-align: center;
      max-width: 780px;
      margin: 0 auto;
    }
    .fc-hint, .qz-hint, .rs-hint {
      font-size: 12px;
      font-weight: 800;
      color: #4a5568;
      padding: 0 14px 12px;
      background: #fff;
    }

    /* Quiz */
    .qz-wrap {
      flex: 1;
      min-height: 0;
      padding: 18px;
      background: #f8fafc;
      overflow: auto;
    }
    .qz-card {
      max-width: 900px;
      margin: 0 auto;
      background: #fff;
      border: 1px solid #edf2f7;
      border-radius: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.06);
      padding: 18px;
    }
    .qz-q {
      font-weight: 900;
      font-size: 18px;
      color: #1a202c;
      line-height: 1.4;
      margin-bottom: 14px;
    }
    .qz-opt {
      display: flex;
      gap: 10px;
      align-items: flex-start;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid #edf2f7;
      background: #fff;
      cursor: pointer;
      margin-bottom: 10px;
      transition: 0.15s;
    }
    .qz-opt:hover { background: #f7fafc; }
    .qz-opt input { margin-top: 4px; }
    .qz-opt.correct { border-color: #9ae6b4; background: #f0fff4; }
    .qz-opt.wrong { border-color: #feb2b2; background: #fff5f5; }
    .qz-feedback {
      margin-top: 14px;
      border-top: 1px solid #edf2f7;
      padding-top: 14px;
    }
    .qz-result {
      font-weight: 900;
      margin-bottom: 8px;
    }
    .qz-explain {
      color: #2d3748;
      line-height: 1.7;
      margin-bottom: 10px;
    }

    /* Resources */
    .rs-wrap {
      flex: 1;
      min-height: 0;
      padding: 18px;
      background: #f8fafc;
      overflow: auto;
    }
    .rs-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 12px;
      max-width: 1100px;
      margin: 0 auto;
    }
    .rs-card {
      background: #fff;
      border: 1px solid #edf2f7;
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 18px rgba(0,0,0,0.05);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .rs-title { font-weight: 900; color: #1a202c; line-height: 1.35; }
    .rs-tag { align-self: flex-start; }
    .rs-link {
      text-decoration: none;
      font-weight: 900;
      color: #2b6cb0;
    }
    .rs-link:hover { text-decoration: underline; }

    /* Lightbox */
    #lightbox {
      display: none;
      position: fixed;
      z-index: 99999;
      top: 0; left: 0;
      width: 100%; height: 100%;
      background: rgba(0,0,0,0.85);
      justify-content: center;
      align-items: center;
    }
    #lightbox img {
      max-width: 90%;
      max-height: 90%;
      border-radius: 10px;
      box-shadow: 0 0 20px rgba(0,0,0,0.4);
    }
  </style>
</head>

<body>
<div class="top-nav">
  <div class="nav-left">
    <div class="tool-btn active" id="btn-notes" onclick="selectTool('notes')">📖 Study Guide</div>
    <div class="tool-btn" id="btn-slidedeck" onclick="selectTool('slidedeck')">🖼️ Slide Deck</div>
    <div class="tool-btn" id="btn-mindmap" onclick="selectTool('mindmap')">🧠 Mindmap</div>
    <div class="tool-btn" id="btn-flashcards" onclick="selectTool('flashcards')">📇 Flashcards</div>
  </div>

  <div class="nav-center">📚 {{ display_subject }}</div>

  <div class="nav-right">
    <div class="tool-btn" id="btn-quiz" onclick="selectTool('quiz')">✍️ Quiz</div>
    <div class="tool-btn" id="btn-datatable" onclick="selectTool('datatable')">📊 Data Table</div>
    <div class="tool-btn" id="btn-resources" onclick="selectTool('resources')">🔗 Resources</div>
  </div>
</div>


  <div class="main-row">
    <div id="sidebar">
      <a class="home-btn" href="/">← Back to Library</a>
      <div class="header" id="sidebar-title">Chapters</div>

      <div id="toc-notes"></div>
      <div id="toc-tables"></div>
      <div id="toc-flashcards"></div>
      <div id="toc-quiz"></div>
      <div id="toc-resources"></div>

    </div>

    <div id="content">
      <div id="content-nav">
        <button id="prev-btn" class="nav-btn" onclick="goRelative(-1)">◀ Previous</button>
        <button id="next-btn" class="nav-btn" onclick="goRelative(1)">Next ▶</button>
      </div>
      <div id="display-area">Loading...</div>
    </div>
  </div>

  <iframe id="loader" src="/doc/{{ subject_slug }}" style="display:none;"></iframe>

  <div id="lightbox" onclick="this.style.display='none'">
    <img id="lightbox-img">
  </div>

  <script>
    const loader = document.getElementById('loader');
    const tocNotes = document.getElementById('toc-notes');
    const tocTables = document.getElementById('toc-tables');
    const tocFlash = document.getElementById('toc-flashcards');
    const tocQuiz = document.getElementById('toc-quiz');
    const tocRes = document.getElementById('toc-resources');
    const displayArea = document.getElementById('display-area');
    const sidebarTitle = document.getElementById('sidebar-title');

    let allItems = [];
    let currentIdx = -1;

    // DataTables state
    let tablesList = [];
    let activeTableId = null;

    // Flashcards state
    let modulesList = [];
    let activeModule = null;
    let cards = [];
    let fcIndex = 0;
    let isFlipped = false;

    // Quiz state
    let quizModules = [];
    let activeQuizModule = null;
    let quizItems = [];
    let qIndex = 0;
    let qAnswered = false;
    let qSelected = null;

    // Resources state
    let resSections = [];
    let activeResSection = null;
    let resData = [];

    function setActiveBtn(type) {
      const ids = ['notes','slidedeck','mindmap','flashcards','quiz','datatable','resources'];
      ids.forEach(t => {
        const el = document.getElementById('btn-' + t);
        if (el) el.classList.toggle('active', t === type);
      });
    }

    function setPageMode(mode) {
      document.body.classList.toggle('tool-mode', mode === 'tool');        // slides + mindmap
      document.body.classList.toggle('datatable-mode', mode === 'datatable');
      document.body.classList.toggle('flashcards-mode', mode === 'flashcards');
      document.body.classList.toggle('quiz-mode', mode === 'quiz');
      document.body.classList.toggle('resources-mode', mode === 'resources');

      if (mode === 'notes') sidebarTitle.textContent = 'Chapters';
      if (mode === 'datatable') sidebarTitle.textContent = 'Sheets';
      if (mode === 'flashcards') sidebarTitle.textContent = 'Modules';
      if (mode === 'quiz') sidebarTitle.textContent = 'Modules';
      if (mode === 'resources') sidebarTitle.textContent = 'Sections';
      if (mode === 'tool') sidebarTitle.textContent = 'Chapters';
    }

    // -----------------------------
    // NOTES
    // -----------------------------
    function isModuleHeading(txt) {
      return /^module\s*\d+/i.test((txt || '').trim());
    }
    function isTopLevel(linkEl) {
      return linkEl.classList.contains('toc-h1') || linkEl.classList.contains('toc-module');
    }
    function hideAllNested() {
      tocNotes.querySelectorAll('.toc-h2, .toc-h3').forEach(x => x.style.display = 'none');
    }
    function findPrevTopLevel(startEl) {
      let cur = startEl.previousElementSibling;
      while (cur && !isTopLevel(cur)) cur = cur.previousElementSibling;
      return cur;
    }
    function findPrevH2(startEl) {
      let cur = startEl.previousElementSibling;
      while (cur && !cur.classList.contains('toc-h2')) cur = cur.previousElementSibling;
      return cur;
    }
    function expandModule(topLink) {
      hideAllNested();
      let next = topLink.nextElementSibling;
      while (next && !isTopLevel(next)) {
        if (next.classList.contains('toc-h2')) next.style.display = 'block';
        next = next.nextElementSibling;
      }
    }
    function expandSubmodule(h2Link) {
      const top = findPrevTopLevel(h2Link);
      if (top) expandModule(top);

      if (top) {
        let t = top.nextElementSibling;
        while (t && !isTopLevel(t)) {
          if (t.classList.contains('toc-h3')) t.style.display = 'none';
          t = t.nextElementSibling;
        }
      }

      let next = h2Link.nextElementSibling;
      while (next && !next.classList.contains('toc-h2') && !isTopLevel(next)) {
        if (next.classList.contains('toc-h3')) next.style.display = 'block';
        next = next.nextElementSibling;
      }
    }
    function ensureExpandedForIndex(idx) {
      if (idx < 0 || idx >= allItems.length) return;
      const link = allItems[idx].link;

      if (isTopLevel(link)) { expandModule(link); return; }
      if (link.classList.contains('toc-h2')) { expandSubmodule(link); return; }
      if (link.classList.contains('toc-h3')) {
        const h2 = findPrevH2(link);
        if (h2) expandSubmodule(h2);
        else {
          const top = findPrevTopLevel(link);
          if (top) expandModule(top);
        }
      }
    }

    function updateNavButtons() {
      const prev = document.getElementById('prev-btn');
      const next = document.getElementById('next-btn');
      if (!prev || !next) return;
      prev.disabled = (currentIdx <= 0);
      next.disabled = (currentIdx >= allItems.length - 1);
    }

    function goRelative(delta) {
      const idx = currentIdx + delta;
      if (idx < 0 || idx >= allItems.length) return;
      selectTool('notes');
      loadSection(idx);
      ensureExpandedForIndex(idx);
    }

    loader.onload = function() {
      const doc = loader.contentDocument || loader.contentWindow.document;
      const headings = Array.from(doc.querySelectorAll('h1, h2, h3'));

      allItems = [];
      tocNotes.innerHTML = "";

      headings.forEach((heading) => {
        const link = document.createElement('a');
        link.textContent = heading.innerText;
        link.href = "javascript:void(0)";
        link.className = "toc-item";

        if (heading.tagName === "H1") link.classList.add("toc-h1");
        else if (heading.tagName === "H2" && isModuleHeading(heading.innerText)) link.classList.add("toc-module");
        else if (heading.tagName === "H2") link.classList.add("toc-h2");
        else link.classList.add("toc-h3");

        tocNotes.appendChild(link);

        const item = { heading, link };
        allItems.push(item);

        link.addEventListener('pointerup', (e) => {
          if (e) { e.preventDefault(); e.stopPropagation(); }
          const idx = allItems.indexOf(item);
          if (isTopLevel(link)) expandModule(link);
          else if (link.classList.contains('toc-h2')) expandSubmodule(link);
          else if (link.classList.contains('toc-h3')) ensureExpandedForIndex(idx);

          selectTool('notes');
          loadSection(idx);
          return false;
        }, { passive: false });
      });

      hideAllNested();
      const firstTop = tocNotes.querySelector('.toc-h1, .toc-module');
      if (firstTop) expandModule(firstTop);

      if (allItems.length) {
        loadSection(0);
        ensureExpandedForIndex(0);
      }
      updateNavButtons();
    };

    function processNode(node) {
      const clone = node.cloneNode(true);
      const sourceElements = node.querySelectorAll ? [node, ...node.querySelectorAll('*')] : [node];
      const cloneElements  = clone.querySelectorAll ? [clone, ...clone.querySelectorAll('*')] : [clone];

      sourceElements.forEach((el, i) => {
        if (!el.style) return;
        const style = window.getComputedStyle(el);
        const target = cloneElements[i];
        if (parseInt(style.fontWeight) >= 600 || style.fontWeight === 'bold') target.classList.add('force-bold');
        if (style.fontStyle === 'italic') target.classList.add('force-italic');
      });

      return clone;
    }

    function loadSection(index) {
      if (index < 0 || index >= allItems.length) return;
      currentIdx = index;

      allItems.forEach(i => i.link.classList.remove('active'));
      const current = allItems[index];
      current.link.classList.add('active');

      displayArea.innerHTML = '';
      const section = document.createElement('div');
      const contentSource = current.heading;

      section.appendChild(processNode(contentSource));
      let next = contentSource.nextElementSibling;
      while (next && !['H1', 'H2', 'H3'].includes(next.tagName)) {
        section.appendChild(processNode(next));
        next = next.nextElementSibling;
      }

      const allImages = section.querySelectorAll('img');
      allImages.forEach(img => {
        const fileName = (img.getAttribute('src') || '').split('/').pop();
        const fullPath = '/study/{{ subject_slug }}/images/' + fileName;
        img.src = fullPath;
        img.style.cursor = "zoom-in";
        img.onclick = () => {
          document.getElementById('lightbox-img').src = fullPath;
          document.getElementById('lightbox').style.display = 'flex';
        };
      });

      displayArea.appendChild(section);
      updateNavButtons();
    }

    // -----------------------------
    // Slide Deck / Mindmap
    // -----------------------------
    async function renderSlideDeck() {
      setPageMode('tool');
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Slide Deck</div>
            <div style="display:flex;gap:10px;align-items:center;"><button class="pill-btn" id="slides-fullscreen-btn" type="button">Fullscreen</button><a class="panel-link" href="/slides_pdf/{{ subject_slug }}" target="_blank" rel="noopener">Open PDF in new tab</a></div>
          </div>
          <div class="panel-body">
            <iframe id="slide-iframe" src="/slides_pdf/{{ subject_slug }}" allowfullscreen="true" webkitallowfullscreen="true" mozallowfullscreen="true"></iframe>
          </div>
        </div>
      `;

      const fsBtn = document.getElementById('slides-fullscreen-btn');
      const frame = document.getElementById('slide-iframe');
      if (fsBtn && frame) {
        fsBtn.onclick = () => {
          // Fullscreen the iframe itself. Most browsers allow this on user gesture.
          const el = frame;
          if (el.requestFullscreen) el.requestFullscreen();
          else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
          else if (el.msRequestFullscreen) el.msRequestFullscreen();
        };
      }
    }

    async function renderMindmap() {
      setPageMode('tool');
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Mindmap</div>
            <a class="panel-link" href="/mindmap_md/{{ subject_slug }}" target="_blank" rel="noopener">Open MD in new tab</a>
          </div>
          <div class="panel-body">
            <svg id="mindmap"></svg>
          </div>
        </div>
      `;

      let md = "";
      try {
        const res = await fetch('/mindmap_md/{{ subject_slug }}', { cache: 'no-store' });
        if (!res.ok) throw new Error('not found');
        md = await res.text();
      } catch (e) {
        displayArea.innerHTML = `
          <div class="panel">
            <div class="panel-header"><div class="panel-title">Mindmap</div></div>
            <div class="panel-body" style="padding:18px;background:#fff;">
              <div style="font-weight:900;margin-bottom:10px;">Mindmap not found</div>
              <div style="color:#4a5568;">Expected file: {{ subject_slug }}/mindmap.md (or markmap.md)</div>
            </div>
          </div>
        `;
        return;
      }

      if (!window.markmap || !window.markmap.Transformer || !window.markmap.Markmap) return;

      const { Transformer, Markmap } = window.markmap;
      const transformer = new Transformer();
      const { root } = transformer.transform(md);

      const mm = Markmap.create('#mindmap', {
        autoFit: true,
        zoom: true,
        pan: true,
        toggleRecursively: true,
        initialExpandLevel: 2
      }, root);

      mm.fit();

      mm.g.selectAll('g.markmap-node')
        .style('cursor', 'pointer')
        .on('click', (event, d) => {
          event.stopPropagation();
          mm.toggleNode(d, true);
        });

      setTimeout(() => mm.fit(), 50);
      setTimeout(() => mm.fit(), 250);
    }

    // -----------------------------
    // Data Tables (multiple)
    // -----------------------------
    function clearTableSidebarActive() {
      tocTables.querySelectorAll('.toc-item').forEach(a => a.classList.remove('active'));
    }

    function makeTableLink(t) {
      const a = document.createElement('a');
      a.href = "#";
      a.className = "toc-item";
      a.textContent = t.name;
      a.onclick = () => {
        selectTool('datatable');
        loadDataTable(t.id);
        return false;
      };
      return a;
    }

    async function loadTableList() {
      try {
        const res = await fetch('/datatable_list/{{ subject_slug }}', { cache: 'no-store' });
        if (!res.ok) throw new Error('list failed');
        const data = await res.json();
        tablesList = Array.isArray(data.tables) ? data.tables : [];
      } catch (e) {
        tablesList = [];
      }

      tocTables.innerHTML = "";

      if (!tablesList.length) {
        const msg = document.createElement('div');
        msg.className = "toc-item";
        msg.style.cursor = "default";
        msg.textContent = "No tables found";
        tocTables.appendChild(msg);
        return;
      }

      tablesList.forEach(t => tocTables.appendChild(makeTableLink(t)));
    }

    function safeCellText(v) {
      if (v === null || v === undefined) return "";
      return String(v);
    }

    function escapeHtml(str) {
      return String(str)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    function buildDataTableUI(sheetName) {
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Data Table — <span id="dt-sheet-name">${escapeHtml(sheetName || "")}</span></div>
            <a class="panel-link" id="dt-open-raw" href="#" target="_blank" rel="noopener">Open raw</a>
          </div>

          <div class="dt-controls">
            <input id="dt-search" placeholder="Search in table..." />
            <select id="dt-pagesize">
              <option value="10">10</option>
              <option value="25" selected>25</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
            <div class="dt-meta" id="dt-meta">0 rows</div>
          </div>

          <div class="dt-table-wrap">
            <table class="dt" id="dt-table"></table>
          </div>

          <div class="dt-pager">
            <button id="dt-prev">◀</button>
            <div class="dt-meta" id="dt-page">1 / 1</div>
            <button id="dt-next">▶</button>
          </div>
        </div>
      `;
    }

    async function loadDataTable(tableId) {
      setPageMode('datatable');
      activeTableId = tableId;

      clearTableSidebarActive();
      const t = tablesList.find(x => x.id === tableId);
      const sheetName = t ? t.name : tableId;

      // mark active link
      Array.from(tocTables.querySelectorAll('.toc-item')).forEach(a => {
        if ((a.textContent || '').trim() === sheetName.trim()) a.classList.add('active');
      });

      buildDataTableUI(sheetName);
      document.getElementById('dt-open-raw').href = `/datatable_raw/{{ subject_slug }}/${encodeURIComponent(tableId)}`;

      let payload = null;
      try {
        const res = await fetch(`/datatable_data/{{ subject_slug }}/${encodeURIComponent(tableId)}`, { cache: 'no-store' });
        if (!res.ok) throw new Error('data failed');
        payload = await res.json();
      } catch (e) {
        document.getElementById('dt-meta').textContent = "Failed to load";
        return;
      }

      const columns = Array.isArray(payload.columns) ? payload.columns : [];
      const rows = Array.isArray(payload.rows) ? payload.rows : [];

      let filtered = rows.slice();
      let sortCol = null;
      let sortDir = 'asc';
      let page = 1;
      let pageSize = 25;

      const elSearch = document.getElementById('dt-search');
      const elSize = document.getElementById('dt-pagesize');
      const elMeta = document.getElementById('dt-meta');
      const elTable = document.getElementById('dt-table');
      const elPrev = document.getElementById('dt-prev');
      const elNext = document.getElementById('dt-next');
      const elPage = document.getElementById('dt-page');

      function applyFilter() {
        const q = (elSearch.value || '').trim().toLowerCase();
        if (!q) filtered = rows.slice();
        else {
          filtered = rows.filter(r => {
            for (const c of columns) {
              const v = safeCellText(r[c]).toLowerCase();
              if (v.includes(q)) return true;
            }
            return false;
          });
        }
        page = 1;
        applySort();
      }

      function applySort() {
        if (!sortCol) { render(); return; }
        filtered.sort((a, b) => {
          const av = safeCellText(a[sortCol]).toLowerCase();
          const bv = safeCellText(b[sortCol]).toLowerCase();

          const an = Number(av), bn = Number(bv);
          const aNum = !isNaN(an) && av !== '';
          const bNum = !isNaN(bn) && bv !== '';
          let cmp = 0;

          if (aNum && bNum) cmp = an - bn;
          else cmp = av.localeCompare(bv);

          return sortDir === 'asc' ? cmp : -cmp;
        });
        render();
      }

      function render() {
        const total = filtered.length;
        const totalPages = Math.max(1, Math.ceil(total / pageSize));
        if (page > totalPages) page = totalPages;

        const start = (page - 1) * pageSize;
        const end = start + pageSize;
        const pageRows = filtered.slice(start, end);

        elMeta.textContent = `${total} rows`;
        elPage.textContent = `${page} / ${totalPages}`;
        elPrev.disabled = page <= 1;
        elNext.disabled = page >= totalPages;

        let html = "<thead><tr>";
        for (const c of columns) {
          const arrow = (sortCol === c) ? (sortDir === 'asc' ? '▲' : '▼') : '';
          html += `<th data-col="${encodeURIComponent(c)}">${escapeHtml(c)}<span class="dt-sort">${arrow}</span></th>`;
        }
        html += "</tr></thead>";

        html += "<tbody>";
        for (const r of pageRows) {
          html += "<tr>";
          for (const c of columns) {
            html += `<td>${escapeHtml(safeCellText(r[c]))}</td>`;
          }
          html += "</tr>";
        }
        html += "</tbody>";

        elTable.innerHTML = html;

        elTable.querySelectorAll("th[data-col]").forEach(th => {
          th.onclick = () => {
            const col = decodeURIComponent(th.getAttribute("data-col"));
            if (sortCol === col) sortDir = (sortDir === 'asc' ? 'desc' : 'asc');
            else { sortCol = col; sortDir = 'asc'; }
            applySort();
          };
        });
      }

      elSearch.oninput = () => applyFilter();
      elSize.onchange = () => { pageSize = Number(elSize.value) || 25; page = 1; render(); };
      elPrev.onclick = () => { if (page > 1) { page -= 1; render(); } };
      elNext.onclick = () => { page += 1; render(); };

      render();
    }

    async function renderDataTables() {
      setPageMode('datatable');
      await loadTableList();

      if (tablesList.length) {
        const first = tablesList[0].id;
        await loadDataTable(activeTableId || first);
      } else {
        displayArea.innerHTML = `
          <div class="panel">
            <div class="panel-header"><div class="panel-title">Data Table</div></div>
            <div class="panel-body" style="padding:18px;background:#fff;color:#4a5568;">
              No tables found.<br/><br/>
              Put CSV/JSON tables in <b>{{ subject_slug }}/tables/</b> (recommended).
            </div>
          </div>
        `;
      }
    }

    // -----------------------------
    // Flashcards
    // -----------------------------
    function clearFlashActive() {
      tocFlash.querySelectorAll('.toc-item').forEach(a => a.classList.remove('active'));
    }

    function buildFlashcardsUI() {
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Flashcards</div>
            <a class="panel-link" href="/flashcards_raw/{{ subject_slug }}" target="_blank" rel="noopener">Open CSV</a>
          </div>

          <div class="fc-toolbar">
            <div class="fc-pill" id="fc-module-pill">Module: -</div>
            <div class="fc-pill" id="fc-progress">0 / 0</div>
            <button class="fc-btn" id="fc-prev">◀ Prev</button>
            <button class="fc-btn" id="fc-flip">Flip</button>
            <button class="fc-btn" id="fc-next">Next ▶</button>
          </div>
          <div class="fc-hint">Click card or press <b>Space</b> to flip. Use <b>←</b>/<b>→</b> for prev/next.</div>

          <div class="fc-card-wrap">
            <div class="fc-card" id="fc-card"></div>
          </div>
        </div>
      `;
    }

    function renderCard() {
      const pill = document.getElementById('fc-module-pill');
      const prog = document.getElementById('fc-progress');
      const cardEl = document.getElementById('fc-card');
      const btnPrev = document.getElementById('fc-prev');
      const btnNext = document.getElementById('fc-next');

      const total = cards.length;
      if (!total) {
        pill.textContent = `Module: ${activeModule || '-'}`;
        prog.textContent = `0 / 0`;
        cardEl.innerHTML = `<div class="fc-front">No flashcards found.</div>`;
        btnPrev.disabled = true;
        btnNext.disabled = true;
        return;
      }

      if (fcIndex < 0) fcIndex = 0;
      if (fcIndex >= total) fcIndex = total - 1;

      const c = cards[fcIndex];
      pill.textContent = `Module: ${activeModule || 'All'}`;
      prog.textContent = `${fcIndex + 1} / ${total}`;

      btnPrev.disabled = fcIndex <= 0;
      btnNext.disabled = fcIndex >= total - 1;

      if (!isFlipped) {
        cardEl.innerHTML = `
          <div class="fc-inner">
            <div class="fc-main">
              <div class="fc-front">${escapeHtml(c.front || '')}</div>
            </div>
          </div>
        `;
        return;
      }

      const back = (c.back || '').trim();
      const trap = (c.clep_trap || '').trim();

      let html = `
        <div class="fc-inner">
          <div class="fc-main">
            <div class="fc-back">${escapeHtml(back)}</div>
          </div>
      `;

      if (trap) {
        html += `
          <div class="fc-trap-block">
            <div class="fc-divider"></div>
            <div class="fc-trap-title">CLEP Trap</div>
            <div class="fc-trap">${escapeHtml(trap)}</div>
          </div>
        `;
      }

      html += `</div>`;
      cardEl.innerHTML = html;
    }

    async function loadFlashModules() {
      try {
        const res = await fetch('/flashcards_modules/{{ subject_slug }}', { cache: 'no-store' });
        if (!res.ok) throw new Error('modules failed');
        const data = await res.json();
        modulesList = Array.isArray(data.modules) ? data.modules : [];
      } catch (e) {
        modulesList = [];
      }

      tocFlash.innerHTML = "";

      const allLink = document.createElement('a');
      allLink.href = "#";
      allLink.className = "toc-item";
      allLink.textContent = "All Modules";
      allLink.onclick = () => { loadFlashcardsForModule(null); return false; };
      tocFlash.appendChild(allLink);

      if (!modulesList.length) return;

      modulesList.forEach(m => {
        const a = document.createElement('a');
        a.href = "#";
        a.className = "toc-item";
        a.textContent = `${m.name} (${m.count})`;
        a.onclick = () => { loadFlashcardsForModule(m.name); return false; };
        tocFlash.appendChild(a);
      });
    }

    function markActiveModule(modName) {
      clearFlashActive();
      const links = Array.from(tocFlash.querySelectorAll('.toc-item'));
      links.forEach(a => {
        const txt = (a.textContent || '').trim();
        if (!modName && txt === "All Modules") a.classList.add('active');
        else if (modName && txt.startsWith(modName + " (")) a.classList.add('active');
      });
    }

    async function loadFlashcardsForModule(modName) {
      setPageMode('flashcards');
      if (!document.getElementById('fc-card')) buildFlashcardsUI();

      activeModule = modName || null;
      markActiveModule(activeModule);

      let url = '/flashcards_data/{{ subject_slug }}';
      if (activeModule) url += '?module=' + encodeURIComponent(activeModule);

      try {
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) throw new Error('cards failed');
        const data = await res.json();
        cards = Array.isArray(data.cards) ? data.cards : [];
      } catch (e) {
        cards = [];
      }

      fcIndex = 0;
      isFlipped = false;
      renderCard();

      document.getElementById('fc-prev').onclick = () => { if (fcIndex > 0) { fcIndex--; isFlipped = false; renderCard(); } };
      document.getElementById('fc-next').onclick = () => { if (fcIndex < cards.length - 1) { fcIndex++; isFlipped = false; renderCard(); } };
      document.getElementById('fc-flip').onclick = () => { isFlipped = !isFlipped; renderCard(); };
      document.getElementById('fc-card').onclick = () => { isFlipped = !isFlipped; renderCard(); };
    }

    function attachFlashKeys() {
      document.addEventListener('keydown', (e) => {
        if (!document.body.classList.contains('flashcards-mode')) return;

        if (e.code === 'Space') {
          e.preventDefault();
          isFlipped = !isFlipped;
          renderCard();
        } else if (e.code === 'ArrowRight') {
          e.preventDefault();
          if (fcIndex < cards.length - 1) { fcIndex++; isFlipped = false; renderCard(); }
        } else if (e.code === 'ArrowLeft') {
          e.preventDefault();
          if (fcIndex > 0) { fcIndex--; isFlipped = false; renderCard(); }
        }
      });
    }

    async function renderFlashcards() {
      setPageMode('flashcards');
      buildFlashcardsUI();
      await loadFlashModules();
      attachFlashKeys();

      const firstMod = (modulesList.length ? modulesList[0].name : null);
      await loadFlashcardsForModule(firstMod);
    }

    // -----------------------------
    // Quiz
    // -----------------------------
    function clearQuizActive() {
      tocQuiz.querySelectorAll('.toc-item').forEach(a => a.classList.remove('active'));
    }

    function buildQuizUI() {
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Quiz</div>
            <a class="panel-link" href="/quiz_raw/{{ subject_slug }}" target="_blank" rel="noopener">Open CSV</a>
          </div>

          <div class="qz-toolbar">
            <div class="qz-pill" id="qz-module-pill">Module: -</div>
            <div class="qz-pill" id="qz-progress">0 / 0</div>
            <button class="qz-btn" id="qz-prev">◀ Prev</button>
            <button class="qz-btn" id="qz-submit">Submit</button>
            <button class="qz-btn" id="qz-next">Next ▶</button>
          </div>
          <div class="qz-hint">Pick an option, then <b>Submit</b>. Use Prev/Next to navigate.</div>

          <div class="qz-wrap" id="qz-wrap"></div>
        </div>
      `;
    }

    async function loadQuizModules() {
      try {
        const res = await fetch('/quiz_modules/{{ subject_slug }}', { cache: 'no-store' });
        if (!res.ok) throw new Error('modules failed');
        const data = await res.json();
        quizModules = Array.isArray(data.modules) ? data.modules : [];
      } catch (e) {
        quizModules = [];
      }

      tocQuiz.innerHTML = "";

      // always show "All Questions"
      const allLink = document.createElement('a');
      allLink.href = "#";
      allLink.className = "toc-item";
      allLink.textContent = "All Questions";
      allLink.onclick = () => { loadQuizForModule(null); return false; };
      tocQuiz.appendChild(allLink);

      if (!quizModules.length) return;

      // If module list already includes All Questions, skip duplicates
      quizModules.forEach(m => {
        if ((m.name || "").toLowerCase() === "all questions") return;
        const a = document.createElement('a');
        a.href = "#";
        a.className = "toc-item";
        a.textContent = `${m.name} (${m.count})`;
        a.onclick = () => { loadQuizForModule(m.name); return false; };
        tocQuiz.appendChild(a);
      });
    }

    function markActiveQuizModule(modName) {
      clearQuizActive();
      const links = Array.from(tocQuiz.querySelectorAll('.toc-item'));
      links.forEach(a => {
        const txt = (a.textContent || '').trim();
        if (!modName && txt === "All Questions") a.classList.add('active');
        else if (modName && txt.startsWith(modName + " (")) a.classList.add('active');
      });
    }

    function renderQuizQuestion() {
      const pill = document.getElementById('qz-module-pill');
      const prog = document.getElementById('qz-progress');
      const wrap = document.getElementById('qz-wrap');
      const btnPrev = document.getElementById('qz-prev');
      const btnNext = document.getElementById('qz-next');
      const btnSubmit = document.getElementById('qz-submit');

      const total = quizItems.length;
      if (!total) {
        pill.textContent = `Module: ${activeQuizModule || 'All Questions'}`;
        prog.textContent = `0 / 0`;
        wrap.innerHTML = `<div class="qz-card"><div class="qz-q">No quiz questions found.</div></div>`;
        btnPrev.disabled = true;
        btnNext.disabled = true;
        btnSubmit.disabled = true;
        return;
      }

      if (qIndex < 0) qIndex = 0;
      if (qIndex >= total) qIndex = total - 1;

      const it = quizItems[qIndex];
      pill.textContent = `Module: ${activeQuizModule || 'All Questions'}`;
      prog.textContent = `${qIndex + 1} / ${total}`;

      btnPrev.disabled = qIndex <= 0;
      btnNext.disabled = qIndex >= total - 1;
      btnSubmit.disabled = false;

      qAnswered = false;
      qSelected = null;

      const optsHtml = (it.options || []).map(o => `
        <label class="qz-opt" data-label="${escapeHtml(o.label)}">
          <input type="radio" name="qz" value="${escapeHtml(o.label)}" />
          <div><b>${escapeHtml(o.label)}.</b> ${escapeHtml(o.text)}</div>
        </label>
      `).join("");

      wrap.innerHTML = `
        <div class="qz-card">
          <div class="qz-q">${escapeHtml(it.question || "")}</div>
          <div id="qz-opts">${optsHtml}</div>
          <div class="qz-feedback" id="qz-feedback" style="display:none;"></div>
        </div>
      `;

      // click behavior: clicking the whole option selects radio
      wrap.querySelectorAll('.qz-opt').forEach(el => {
        el.onclick = () => {
          const radio = el.querySelector('input[type="radio"]');
          if (radio) radio.checked = true;
        };
      });

      btnSubmit.onclick = () => submitQuizAnswer();
      btnPrev.onclick = () => { if (qIndex > 0) { qIndex--; renderQuizQuestion(); } };
      btnNext.onclick = () => { if (qIndex < quizItems.length - 1) { qIndex++; renderQuizQuestion(); } };
    }

    function submitQuizAnswer() {
      if (qAnswered) return;

      const it = quizItems[qIndex];
      const correct = (it.answer || "").toUpperCase().trim();

      const radios = Array.from(document.querySelectorAll('input[name="qz"]'));
      const picked = (radios.find(r => r.checked) || {}).value || "";
      qSelected = (picked || "").toUpperCase().trim();

      if (!qSelected) return;

      qAnswered = true;

      const feedback = document.getElementById('qz-feedback');
      feedback.style.display = "block";

      // mark correct/wrong options
      document.querySelectorAll('.qz-opt').forEach(el => {
        const label = (el.getAttribute('data-label') || "").toUpperCase().trim();
        if (label === correct) el.classList.add('correct');
        if (label === qSelected && qSelected !== correct) el.classList.add('wrong');
      });

      const isRight = (qSelected === correct);
      const resultText = isRight ? "✅ Correct" : `❌ Incorrect (Correct: ${escapeHtml(correct)})`;

      let html = `<div class="qz-result">${resultText}</div>`;

      const explanation = (it.explanation || "").trim();
      if (explanation) {
        html += `<div class="qz-explain">${escapeHtml(explanation)}</div>`;
      }

      const trap = (it.clep_trap || "").trim();
      if (trap) {
        html += `
          <div class="fc-divider"></div>
          <div class="fc-trap-title">CLEP Trap</div>
          <div class="fc-trap">${escapeHtml(trap)}</div>
        `;
      }

      feedback.innerHTML = html;
    }

    async function loadQuizForModule(modName) {
      setPageMode('quiz');
      if (!document.getElementById('qz-wrap')) buildQuizUI();

      activeQuizModule = modName || null;
      markActiveQuizModule(activeQuizModule);

      let url = '/quiz_data/{{ subject_slug }}';
      if (activeQuizModule) url += '?module=' + encodeURIComponent(activeQuizModule);

      try {
        const res = await fetch(url, { cache: 'no-store' });
        if (!res.ok) throw new Error('quiz failed');
        const data = await res.json();
        quizItems = Array.isArray(data.items) ? data.items : [];
      } catch (e) {
        quizItems = [];
      }

      qIndex = 0;
      renderQuizQuestion();
    }

    async function renderQuiz() {
      setPageMode('quiz');
      buildQuizUI();
      await loadQuizModules();

      // default to first module other than All Questions, if present
      const first = (quizModules.find(m => (m.name || "").toLowerCase() !== "all questions") || {}).name || null;
      await loadQuizForModule(first);
    }

    // -----------------------------
    // Resources
    // -----------------------------
    function clearResActive() {
      tocRes.querySelectorAll('.toc-item').forEach(a => a.classList.remove('active'));
    }

    function buildResourcesUI() {
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">Resources</div>
            <a class="panel-link" href="/resources_raw/{{ subject_slug }}" target="_blank" rel="noopener">Open JSON</a>
          </div>

          <div class="rs-toolbar">
            <div class="rs-pill" id="rs-section-pill">Section: -</div>
            <div class="rs-pill" id="rs-count-pill">0 items</div>
          </div>
          <div class="rs-hint">Links open in a new tab. Local files open from your subject’s <b>resources/files/</b>.</div>

          <div class="rs-wrap" id="rs-wrap"></div>
        </div>
      `;
    }

    async function loadResourceSections() {
      try {
        const res = await fetch('/resources_sections/{{ subject_slug }}', { cache: 'no-store' });
        if (!res.ok) throw new Error('sections failed');
        const data = await res.json();
        resSections = Array.isArray(data.sections) ? data.sections : [];
      } catch (e) {
        resSections = [];
      }

      tocRes.innerHTML = "";
      if (!resSections.length) {
        const msg = document.createElement('div');
        msg.className = "toc-item";
        msg.style.cursor = "default";
        msg.textContent = "No resources found";
        tocRes.appendChild(msg);
        return;
      }

      resSections.forEach(s => {
        const a = document.createElement('a');
        a.href = "#";
        a.className = "toc-item";
        a.textContent = `${s.name} (${s.count})`;
        a.onclick = () => { loadResourcesForSection(s.name); return false; };
        tocRes.appendChild(a);
      });
    }

    function markActiveResSection(section) {
      clearResActive();
      Array.from(tocRes.querySelectorAll('.toc-item')).forEach(a => {
        if (a.textContent.startsWith(section + " (")) a.classList.add('active');
      });
    }

    async function loadResourcesForSection(section) {
      setPageMode('resources');
      if (!document.getElementById('rs-wrap')) buildResourcesUI();

      activeResSection = section;
      markActiveResSection(section);

      let items = [];
      try {
        const res = await fetch('/resources_data/{{ subject_slug }}?section=' + encodeURIComponent(section), { cache: 'no-store' });
        if (!res.ok) throw new Error('data failed');
        const data = await res.json();
        items = Array.isArray(data.items) ? data.items : [];
      } catch (e) {
        items = [];
      }

      const pill = document.getElementById('rs-section-pill');
      const count = document.getElementById('rs-count-pill');
      pill.textContent = `Section: ${section}`;
      count.textContent = `${items.length} items`;

      const wrap = document.getElementById('rs-wrap');
      if (!items.length) {
        wrap.innerHTML = `<div class="qz-card"><div class="qz-q">No items in this section.</div></div>`;
        return;
      }

      const cardsHtml = items.map(it => {
        const title = escapeHtml(it.title || "");
        const tag = escapeHtml(it.tag || "");
        const hasUrl = !!(it.url || "");
        const hasFile = !!(it.file || "");

        let link = "";
        if (hasUrl) {
          link = `<a class="rs-link" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">Open link</a>`;
        } else if (hasFile) {
          link = `<a class="rs-link" href="/resources_file/{{ subject_slug }}/${encodeURIComponent(it.file)}" target="_blank" rel="noopener">Open file</a>`;
        }

        return `
          <div class="rs-card">
            <div class="rs-title">${title}</div>
            ${tag ? `<div class="rs-pill rs-tag">${tag}</div>` : ``}
            ${link}
          </div>
        `;
      }).join("");

      wrap.innerHTML = `<div class="rs-grid">${cardsHtml}</div>`;
    }

    async function renderResources() {
      setPageMode('resources');
      buildResourcesUI();
      await loadResourceSections();

      if (!resSections.length) {
        document.getElementById('rs-wrap').innerHTML =
          `<div class="qz-card"><div class="qz-q">No resources.json found.</div></div>`;
        return;
      }

      const first = resSections[0].name;
      await loadResourcesForSection(first);
    }

    // -----------------------------
    // Tool switching
    // -----------------------------
    function selectTool(tool) {
      setActiveBtn(tool);

      if (tool === 'notes') {
        setPageMode('notes');
        if (currentIdx >= 0) {
          ensureExpandedForIndex(currentIdx);
          loadSection(currentIdx);
        } else if (allItems.length) {
          ensureExpandedForIndex(0);
          loadSection(0);
        }
        return;
      }

      if (tool === 'slidedeck') return renderSlideDeck();
      if (tool === 'mindmap') return renderMindmap();
      if (tool === 'datatable') return renderDataTables();
      if (tool === 'flashcards') return renderFlashcards();
      if (tool === 'quiz') return renderQuiz();
      if (tool === 'resources') return renderResources();

      setPageMode('tool');
      displayArea.innerHTML = `
        <div class="panel">
          <div class="panel-header"><div class="panel-title">${tool[0].toUpperCase() + tool.slice(1)}</div></div>
          <div class="panel-body" style="padding:18px;background:#fff;color:#4a5568;">
            Coming soon.
          </div>
        </div>
      `;
    }
  </script>
</body>
</html>
"""

# -----------------------------
# Routes
# -----------------------------
@app.route("/")
def home():
    books = []
    for name in sorted(os.listdir(BASE_DIR)):
        p = os.path.join(BASE_DIR, name)
        if os.path.isdir(p) and not name.startswith(".") and name not in ("__pycache__", "static"):
            if os.path.exists(os.path.join(p, "guide.html")):
                books.append(name)
    return render_template_string(LIBRARY_HTML, books=books)


@app.route("/study/<subject>")
def study(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    display_subject = subject_slug.replace("_", " ").title()
    return render_template_string(
        STUDY_HTML,
        subject_slug=subject_slug,
        display_subject=display_subject,
    )


@app.route("/doc/<subject>")
def serve_doc(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.exists(os.path.join(subject_dir, "guide.html")):
        abort(404)
    return send_from_directory(subject_dir, "guide.html")


@app.route("/study/<subject>/images/<path:filename>")
def serve_images(subject, filename):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    images_dir = os.path.join(subject_dir, "images")
    if not os.path.isdir(images_dir):
        abort(404)
    return send_from_directory(images_dir, filename)


@app.route("/slides_pdf/<subject>")
def serve_slides_pdf(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    path = os.path.join(subject_dir, "slides.pdf")
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(subject_dir, "slides.pdf")


@app.route("/mindmap_md/<subject>")
def serve_mindmap_md(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    for fname in ("mindmap.md", "markmap.md"):
        path = os.path.join(subject_dir, fname)
        if os.path.exists(path):
            return send_from_directory(subject_dir, fname)
    abort(404)


# ---------- DataTables ----------
@app.route("/datatable_list/<subject>")
def datatable_list(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    tables = list_datatables(subject_dir)
    return jsonify({"tables": [{"id": t["id"], "name": t["name"]} for t in tables]})


@app.route("/datatable_raw/<subject>/<table_id>")
def datatable_raw(subject, table_id):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    if os.path.basename(table_id) != table_id:
        abort(400)

    # Combined CSV "virtual tables"
    if table_id.startswith("combined__"):
        combined = get_combined_datatables(subject_dir)
        if not combined:
            abort(404)
        table = combined.get("by_id", {}).get(table_id)
        if not table:
            abort(404)

        csv_text = write_table_to_csv_string(table)
        safe_name = re.sub(r"[^a-zA-Z0-9._ -]+", "_", (table.get("name") or table_id)).strip() or table_id
        headers = {"Content-Disposition": f'inline; filename="{safe_name}.csv"'}
        return Response(csv_text, mimetype="text/csv", headers=headers)

    # Default: raw file
    tables = list_datatables(subject_dir)
    hit = next((t for t in tables if t["id"] == table_id), None)
    if not hit:
        abort(404)
    folder = os.path.dirname(hit["full"])
    return send_from_directory(folder, table_id)


@app.route("/datatable_data/<subject>/<table_id>")
def datatable_data(subject, table_id):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    if os.path.basename(table_id) != table_id:
        abort(400)

    # Combined CSV "virtual tables"
    if table_id.startswith("combined__"):
        combined = get_combined_datatables(subject_dir)
        if not combined:
            abort(404)
        table = combined.get("by_id", {}).get(table_id)
        if not table:
            abort(404)
        return jsonify({"columns": table.get("columns") or [], "rows": table.get("rows") or []})

    # Default: CSV/JSON file
    tables = list_datatables(subject_dir)
    hit = next((t for t in tables if t["id"] == table_id), None)
    if not hit:
        abort(404)
    cols, rows = load_table_file(hit["full"])
    return jsonify({"columns": cols, "rows": rows})


# ---------- Flashcards ----------
@app.route("/flashcards_modules/<subject>")
def flashcards_modules(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    cards = load_flashcards(subject_dir)
    mods = flashcard_modules(cards)
    return jsonify({"modules": mods, "total": len(cards)})


@app.route("/flashcards_data/<subject>")
def flashcards_data(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)

    module = request.args.get("module")
    all_cards = load_flashcards(subject_dir)
    if module:
        filtered = [c for c in all_cards if (c.get("module") or "") == module]
    else:
        filtered = all_cards

    return jsonify({"cards": filtered})


@app.route("/flashcards_raw/<subject>")
def flashcards_raw(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    path = next((p for p in _flashcards_paths(subject_dir) if os.path.exists(p)), None)
    if not path:
        abort(404)
    folder = os.path.dirname(path)
    fname = os.path.basename(path)
    return send_from_directory(folder, fname)


# ---------- Quiz ----------
@app.route("/quiz_modules/<subject>")
def quiz_modules_route(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    items, _ = load_quiz(subject_dir)
    mods = quiz_modules(items)
    return jsonify({"modules": mods, "total": len(items)})


@app.route("/quiz_data/<subject>")
def quiz_data(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)

    module = request.args.get("module")
    items, _ = load_quiz(subject_dir)
    if module:
        filtered = [q for q in items if (q.get("module") or "") == module]
    else:
        filtered = items

    return jsonify({"items": filtered})


@app.route("/quiz_raw/<subject>")
def quiz_raw(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    _, path = load_quiz(subject_dir)
    if not path:
        abort(404)
    folder = os.path.dirname(path)
    fname = os.path.basename(path)
    return send_from_directory(folder, fname)


# ---------- Resources ----------
@app.route("/resources_sections/<subject>")
def resources_sections(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    resources, _ = load_resources(subject_dir)
    secs = resource_sections(resources)
    return jsonify({"sections": secs})


@app.route("/resources_data/<subject>")
def resources_data(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)

    section = (request.args.get("section") or "").strip()
    resources, _ = load_resources(subject_dir)

    items = []
    for b in resources:
        if (b.get("section") or "") == section:
            items = b.get("items") or []
            break

    return jsonify({"items": items})


@app.route("/resources_file/<subject>/<path:filename>")
def resources_file(subject, filename):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)

    # prevent escaping
    clean = os.path.normpath(filename).replace("\\", "/")
    if clean.startswith("../") or clean.startswith(".."):
        abort(400)

    files_dir = os.path.join(subject_dir, "resources", "files")
    if not os.path.isdir(files_dir):
        abort(404)

    full = os.path.join(files_dir, clean)
    if not os.path.exists(full):
        abort(404)

    # send_from_directory needs directory + relative path
    return send_from_directory(files_dir, clean)


@app.route("/resources_raw/<subject>")
def resources_raw(subject):
    subject_slug, subject_dir = resolve_subject_dir(subject)
    if not os.path.isdir(subject_dir):
        abort(404)
    _, path = load_resources(subject_dir)
    if not path:
        abort(404)
    folder = os.path.dirname(path)
    fname = os.path.basename(path)
    return send_from_directory(folder, fname)


if __name__ == "__main__":
    app.run(debug=True, port=8000)
