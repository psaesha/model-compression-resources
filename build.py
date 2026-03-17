"""
build.py
--------
Reads papers.txt, fetches metadata, regenerates README.md and papers.bib.

Two fetch paths:
  arXiv URLs   → arXiv Atom API       (no key, stdlib only)
  everything else → DOI content negotiation (doi.org → publisher BibTeX)

For non-arXiv URLs, you need a DOI — either:
  - paste the doi.org URL directly:         https://doi.org/10.18653/v1/2024.findings-emnlp.901
  - paste the ACL Anthology URL directly:   https://aclanthology.org/2024.findings-emnlp.901
  - paste any publisher URL that embeds a DOI

Usage:
    python build.py
"""

import re, os, sys, time, json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

SOURCE_FILE = "papers.txt"
README_FILE = "README.md"
BIB_FILE    = "papers.bib"
CACHE_FILE  = ".cache.txt"
MANUAL_FILE = "manual.bib"
REPOS_FILE  = "repos.txt"
REPOS_CACHE = ".repos_cache.txt"

ARXIV_API = "http://export.arxiv.org/api/query?id_list={}"
NS = {
    "atom"  : "http://www.w3.org/2005/Atom",
    "arxiv" : "http://arxiv.org/schemas/atom",
}

# ── Cache ──────────────────────────────────────────────────────────────────

def load_cache():
    cache = {}
    if os.path.exists(CACHE_FILE):
        for line in open(CACHE_FILE):
            if "\t" in line.strip():
                url, data = line.strip().split("\t", 1)
                cache[url] = data
    return cache

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        for url, data in cache.items():
            f.write(f"{url}\t{data}\n")

# ── arXiv ──────────────────────────────────────────────────────────────────

def extract_arxiv_id(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
    return m.group(1) if m else None

def fetch_arxiv(arxiv_id):
    with urllib.request.urlopen(ARXIV_API.format(arxiv_id), timeout=10) as resp:
        xml = resp.read()
    root  = ET.fromstring(xml)
    entry = root.find("atom:entry", NS)
    if entry is None:
        return None
    title       = entry.findtext("atom:title",    "", NS).strip().replace("\n", " ")
    year        = entry.findtext("atom:published", "", NS)[:4]
    journal_ref = entry.findtext("arxiv:journal_ref", "", NS)
    authors     = [a.findtext("atom:name", "", NS)
                   for a in entry.findall("atom:author", NS)]
    return {
        "title"    : title,
        "authors"  : authors,
        "year"     : year,
        "venue"    : journal_ref or "arXiv",
        "arxiv_id" : arxiv_id,
        "doi"      : None,
        "_bibtex"  : None,   # arXiv entries are built programmatically
    }

# ── DOI extraction from various URL formats ────────────────────────────────

def extract_doi(url):
    # Direct: https://doi.org/10.xxxx/...
    m = re.search(r"doi\.org/(10\.\d{4,}/.+)", url)
    if m:
        return m.group(1).rstrip("/")
    # ACL Anthology: reconstruct DOI from paper ID
    m = re.search(r"aclanthology\.org/([\w.+-]+?)/?$", url)
    if m:
        return f"10.18653/v1/{m.group(1)}"
    # Bare DOI pasted directly
    m = re.match(r"(10\.\d{4,}/.+)", url)
    if m:
        return m.group(1).rstrip("/")
    return None

# ── DOI content negotiation → BibTeX ──────────────────────────────────────

def fetch_doi_bibtex(doi):
    """Ask doi.org for BibTeX via content negotiation. Works for most publishers."""
    req = urllib.request.Request(
        f"https://doi.org/{doi}",
        headers={
            "Accept"    : "application/x-bibtex",
            "User-Agent": "paper-repo/1.0",
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8")

def parse_bibtex(bib, doi):
    """Extract structured fields from a raw BibTeX string."""
    def field(name):
        # matches  name = {value}  or  name = "value"  (possibly multiline)
        m = re.search(name + r"\s*=\s*[{\"](.+?)[}\"](?:\s*[,}])",
                      bib, re.DOTALL | re.IGNORECASE)
        if not m:
            return ""
        return re.sub(r"\s+", " ", m.group(1)).strip().strip("{}")

    title   = re.sub(r"[{}]", "", field("title"))
    year    = field("year")
    venue   = field("booktitle") or field("journal") or field("publisher") or ""
    raw_authors = field("author")
    authors = [a.strip() for a in re.split(r"\s+and\s+", raw_authors)] if raw_authors else []

    return {
        "title"    : title,
        "authors"  : authors,
        "year"     : year,
        "venue"    : venue,
        "arxiv_id" : None,
        "doi"      : doi,
        "_bibtex"  : bib,   # store the raw BibTeX so we don't have to rebuild it
    }

# ── Dispatch ───────────────────────────────────────────────────────────────

def fetch(url, cache):
    if url in cache:
        return json.loads(cache[url])

    arxiv_id = extract_arxiv_id(url)
    doi      = extract_doi(url)
    meta     = None

    if arxiv_id:
        meta = fetch_arxiv(arxiv_id)
    elif doi:
        try:
            bib  = fetch_doi_bibtex(doi)
            meta = parse_bibtex(bib, doi)
        except Exception as e:
            print(f"\n  ⚠️  DOI fetch failed for {doi}: {e}")
    else:
        print(f"\n  ⚠️  No arXiv ID or DOI found in: {url}")

    if meta:
        cache[url] = json.dumps(meta)
    return meta

# ── Formatting ─────────────────────────────────────────────────────────────

def authors_short(meta, n=3):
    names = meta.get("authors", [])
    return ", ".join(names[:n]) + (" et al." if len(names) > n else "")

def bib_key(meta):
    first_author = (meta.get("authors") or ["anon"])[0]
    # Handle both "Last, First" (BibTeX) and "First Last" (arXiv API) formats
    last = first_author.split(",")[0].split()[-1].lower()
    year = meta.get("year", "")
    word = re.sub(r"[^a-z]", "", meta.get("title", "paper").split()[0].lower())
    return f"{last}{year}{word}"

def make_bib_entry(meta):
    """Use raw BibTeX if we have it (DOI path), otherwise build it (arXiv path)."""
    key = bib_key(meta)
    if meta.get("_bibtex"):
        # Rewrite the key in the raw BibTeX to our standard format
        bib = re.sub(r"(@\w+\{)[^,]+,", rf"\g<1>{key},", meta["_bibtex"], count=1)
        return bib, key

    # Build from scratch (arXiv)
    authors = " and ".join(meta.get("authors", []))
    lines   = [
        f"@article{{{key},",
        f"  title         = {{{meta.get('title', '')}}},",
        f"  author        = {{{authors}}},",
        f"  year          = {{{meta.get('year', '')}}},",
        f"  journal       = {{{meta.get('venue', 'arXiv')}}},",
        f"  eprint        = {{{meta['arxiv_id']}}},",
        f"  archivePrefix = {{arXiv}},",
        f"}}",
    ]
    return "\n".join(lines), key



# ── Repositories ───────────────────────────────────────────────────────────

def load_repos_cache():
    cache = {}
    if os.path.exists(REPOS_CACHE):
        for line in open(REPOS_CACHE):
            if "\t" in line.strip():
                url, data = line.strip().split("\t", 1)
                cache[url] = data
    return cache

def save_repos_cache(cache):
    with open(REPOS_CACHE, "w") as f:
        for url, data in cache.items():
            f.write(f"{url}\t{data}\n")

def fetch_github(url):
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", url)
    if not m:
        return None
    slug = m.group(1).rstrip("/")
    req  = urllib.request.Request(
        f"https://api.github.com/repos/{slug}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "paper-repo/1.0"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
    return {
        "name"       : d.get("full_name", slug),
        "description": d.get("description") or "",
        "stars"      : d.get("stargazers_count", 0),
        "url"        : url,
        "kind"       : "github",
    }

def fetch_huggingface(url):
    # Handles both models and datasets:
    # huggingface.co/{org}/{model}  or  huggingface.co/datasets/{org}/{dataset}
    m = re.search(r"huggingface\.co/(?:datasets/)?(([\w.-]+)/([\w.-]+))", url)
    if not m:
        return None
    slug    = m.group(1)
    is_ds   = "/datasets/" in url
    api_url = f"https://huggingface.co/api/{'datasets' if is_ds else 'models'}/{slug}"
    req     = urllib.request.Request(api_url, headers={"User-Agent": "paper-repo/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
    likes = d.get("likes", 0)
    downloads = d.get("downloads", 0)
    return {
        "name"       : slug,
        "description": d.get("description") or (d.get("cardData") or {}).get("description") or "",
        "stars"      : likes,
        "downloads"  : downloads,
        "url"        : url,
        "kind"       : "huggingface",
    }

def fetch_repo(url, cache):
    if url in cache:
        return json.loads(cache[url])
    try:
        if "github.com" in url:
            meta = fetch_github(url)
        elif "huggingface.co" in url:
            meta = fetch_huggingface(url)
        else:
            print(f"\n  ⚠️  Unrecognised repo URL: {url}")
            return None
    except Exception as e:
        print(f"\n  ⚠️  Failed to fetch {url}: {e}")
        return None
    if meta:
        cache[url] = json.dumps(meta)
    return meta

def parse_repos_file():
    """Parse repos.txt, same format as papers.txt."""
    if not os.path.exists(REPOS_FILE):
        return []
    sections, current_header, current_urls = [], None, []
    for raw in open(REPOS_FILE):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if current_urls:
                sections.append((current_header, current_urls))
                current_urls = []
            current_header = line.lstrip("#").strip() or None
        else:
            current_urls.append(line)
    if current_urls:
        sections.append((current_header, current_urls))
    return sections

def render_repo_line(meta):
    icon  = "🤗" if meta["kind"] == "huggingface" else "⭐"
    stars = meta.get("stars", 0)
    desc  = f" — {meta['description']}" if meta.get("description") else ""
    return f"- [{meta['name']}]({meta['url']}){desc}"

# ── Manual BibTeX ──────────────────────────────────────────────────────────

def read_manual_bib():
    """Parse manual.bib and return list of (raw_entry, meta) tuples."""
    if not os.path.exists(MANUAL_FILE):
        return []

    entries = []
    # Split on @ boundaries, keeping the @ delimiter
    raw = open(MANUAL_FILE).read()
    blocks = re.split(r"(?=@\w+\s*\{)", raw)

    for block in blocks:
        block = block.strip()
        if not block or block.startswith("%"):
            continue

        def field(name):
            m = re.search(name + r"\s*=\s*[{\"](.*?)[}\"](\s*[,}])",
                          block, re.DOTALL | re.IGNORECASE)
            if not m:
                return ""
            return re.sub(r"\s+", " ", m.group(1)).strip().strip("{}")

        title   = re.sub(r"[{}]", "", field("title"))
        year    = field("year")
        venue   = field("booktitle") or field("journal") or field("publisher") or ""
        doi     = field("doi")
        raw_authors = field("author")
        # BibTeX authors are "Last, First and Last, First"
        authors = [a.strip() for a in re.split(r"\s+and\s+", raw_authors)] if raw_authors else []

        if not title:
            continue   # skip comment blocks or malformed entries

        meta = {
            "title"    : title,
            "authors"  : authors,
            "year"     : year,
            "venue"    : venue,
            "arxiv_id" : None,
            "doi"      : doi,
            "_bibtex"  : block,
        }
        entries.append(meta)

    return entries

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(SOURCE_FILE):
        sys.exit(f"❌  {SOURCE_FILE} not found.")

    # Parse papers.txt
    sections, current_header, current_urls = [], None, []
    for raw in open(SOURCE_FILE):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if current_urls:
                sections.append((current_header, current_urls))
                current_urls = []
            current_header = line.lstrip("#").strip() or None
        else:
            current_urls.append(line)
    if current_urls:
        sections.append((current_header, current_urls))

    # Deduplicate
    seen, dupes = set(), []
    for _, urls in sections:
        unique = []
        for url in urls:
            if url in seen:
                dupes.append(url)
            else:
                seen.add(url)
                unique.append(url)
        urls[:] = unique
    if dupes:
        print(f"⚠️  Skipping {len(dupes)} duplicate(s): {', '.join(dupes)}\n")

    all_urls = [u for _, urls in sections for u in urls]
    print(f"📄  {len(all_urls)} papers in {SOURCE_FILE}\n")

    cache, results = load_cache(), {}
    for url in all_urls:
        cached = url in cache
        print(f"  {'(cache)' if cached else '→      '} {url}", end="  ", flush=True)
        meta = fetch(url, cache)
        if meta:
            print(f"✓  {meta.get('title','')[:55]}")
            results[url] = meta
        else:
            print()
        if not cached:
            time.sleep(0.5)

    save_cache(cache)

    # ── README ─────────────────────────────────────────────────────────
    # ── Repos ──────────────────────────────────────────────────────────
    repo_sections = parse_repos_file()
    all_repo_urls = [u for _, urls in repo_sections for u in urls]
    repos_cache   = load_repos_cache()
    repo_results  = {}

    if all_repo_urls:
        print(f"\n🗂️   Fetching {len(all_repo_urls)} repositories...")
    for url in all_repo_urls:
        cached = url in repos_cache
        print(f"  {'(cache)' if cached else '→      '} {url}", end="  ", flush=True)
        meta = fetch_repo(url, repos_cache)
        if meta:
            print(f"✓  {meta['name']}")
            repo_results[url] = meta
        else:
            print()
        if not cached:
            time.sleep(0.3)
    save_repos_cache(repos_cache)

    manual_entries = read_manual_bib()
    if manual_entries:
        print(f"\n📎  {len(manual_entries)} manual entr{'y' if len(manual_entries)==1 else 'ies'} from {MANUAL_FILE}")

    # Merge papers and repos into a shared category order
    # Collect all category names in order of first appearance
    category_order = []
    seen_cats = set()
    for header, _ in sections:
        cat = header or ""
        if cat not in seen_cats:
            category_order.append(cat)
            seen_cats.add(cat)
    for header, _ in repo_sections:
        cat = header or ""
        if cat not in seen_cats:
            category_order.append(cat)
            seen_cats.add(cat)

    # Index papers and repos by category
    papers_by_cat = {}
    for header, urls in sections:
        papers_by_cat.setdefault(header or "", []).extend(urls)

    repos_by_cat = {}
    for header, urls in repo_sections:
        repos_by_cat.setdefault(header or "", []).extend(urls)

    readme = [
        "# Model Compression Papers and Repositories",
        "",
        f"> Generated from `{SOURCE_FILE}` and `{REPOS_FILE}` · "
        f"{len(results)} papers · {len(repo_results)} repos · {datetime.today().strftime('%Y-%m-%d')}",
        f"> Add URLs to `{SOURCE_FILE}` or `{REPOS_FILE}` and commit — Action regenerates this automatically.",
        "",
    ]
    n = 1
    for cat in category_order:
        if cat:
            readme += [f"## {cat}", ""]

        paper_urls = papers_by_cat.get(cat, [])
        if paper_urls:
            readme += ["### Papers", ""]
            for url in paper_urls:
                meta = results.get(url)
                if not meta:
                    readme.append(f"{n}. {url}")
                else:
                    _, key = make_bib_entry(meta)
                    readme.append(
                        f"{n}. [{meta['title']}]({url}) — "
                        f"{authors_short(meta)} ({meta.get('year', '')}) "
                        # f"`\\cite{{{key}}}`"
                    )
                n += 1
            readme.append("")

        repo_urls = repos_by_cat.get(cat, [])
        if repo_urls:
            readme += ["### Repositories", ""]
            for url in repo_urls:
                meta = repo_results.get(url)
                if meta:
                    readme.append(render_repo_line(meta))
                else:
                    readme.append(f"- {url}")
            readme.append("")

    if manual_entries:
        readme += ["## Other", "", "### Papers", ""]
        for meta in manual_entries:
            _, key = make_bib_entry(meta)
            readme.append(
                f"{n}. {meta['title']} — "
                f"{authors_short(meta)} ({meta.get('year', '')}) "
                # f"`\\cite{{{key}}}`"
            )
            n += 1
        readme.append("")

    # ── BibTeX ─────────────────────────────────────────────────────────
    bib_lines = [
        f"% papers.bib — generated from {SOURCE_FILE} on {datetime.today().strftime('%Y-%m-%d')}",
        f"% Do not edit. Edit {SOURCE_FILE} and re-run build.py.",
        "",
    ]
    for url in all_urls:
        meta = results.get(url)
        if meta:
            entry, _ = make_bib_entry(meta)
            bib_lines += [entry, ""]

    for meta in manual_entries:
        entry, _ = make_bib_entry(meta)
        bib_lines += [entry, ""]

    with open(README_FILE, "w") as f:
        f.write("\n".join(readme))
    with open(BIB_FILE, "w") as f:
        f.write("\n".join(bib_lines))

    print(f"\n✅  {README_FILE} and {BIB_FILE} updated.")

if __name__ == "__main__":
    main()