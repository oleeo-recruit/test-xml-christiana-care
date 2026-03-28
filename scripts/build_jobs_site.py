from __future__ import annotations

import html
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


FEED_URL = os.getenv("FEED_URL", "https://careers-cms.christianacare.org/google/feed.php")
BASE_URL = os.getenv("BASE_URL", "https://oleeo-recruit.github.io/test-xml-christiana-care").rstrip("/")
SITE_TITLE = os.getenv("SITE_TITLE", "ChristianaCare Jobs")
OUT_DIR = Path(os.getenv("OUT_DIR", "site"))
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "build-debug"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))
MAX_JOBS = int(os.getenv("MAX_JOBS", "0"))  # 0 = no limit

FIELD_ALIASES = {
    "id": ["id", "jobid", "job_id", "reqid", "requisitionid", "referencenumber", "reference", "identifier"],
    "title": ["title", "jobtitle", "job_title", "positiontitle", "position", "name"],
    "description": ["description", "summary", "body", "content", "jobdescription", "job_description"],
    "apply_url": ["url", "applyurl", "apply_url", "link", "joburl", "job_url", "applylink"],
    "location": ["location", "citystate", "joblocation"],
    "city": ["city", "jobcity"],
    "state": ["state", "region", "province"],
    "country": ["country"],
    "department": ["department", "category", "team", "jobcategory", "job_category"],
    "employment_type": ["employmenttype", "employment_type", "type", "jobtype", "job_type", "schedule"],
    "updated": ["updated", "lastupdated", "last_updated", "pubdate", "date", "posteddate", "postingdate"],
}


def localname(tag: str) -> str:
    if "}" in tag:
        tag = tag.split("}", 1)[1]
    if ":" in tag:
        tag = tag.split(":", 1)[1]
    return tag.strip().lower()


def text_from_element(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = "".join(el.itertext())
    raw = html.unescape(raw)
    raw = raw.replace("]]>", " ")
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = re.sub(r"\s+", " ", raw)
    return raw.strip()


def slugify(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "job"


def fetch_xml(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; OleeoJobsKB/1.0; +https://oleeo-recruit.github.io/)"
        },
    )
    with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        data = resp.read()
    return data.decode(charset, errors="replace")


def detect_records(root: ET.Element) -> tuple[list[ET.Element], str]:
    all_elements = list(root.iter())
    by_tag: dict[str, list[ET.Element]] = {}
    for el in all_elements:
        tag = localname(el.tag)
        by_tag.setdefault(tag, []).append(el)

    priority = ["job", "item", "opening", "position", "posting", "listing", "record", "opportunity"]
    for tag in priority:
        els = by_tag.get(tag, [])
        rich = [el for el in els if len(list(el)) >= 2]
        if len(rich) >= 1:
            return rich, tag

    best_tag = None
    best_score = (-1, -1)
    best_records: list[ET.Element] = []
    for tag, els in by_tag.items():
        if len(els) < 2:
            continue
        richness = sum(len({localname(c.tag) for c in list(el)}) for el in els) / len(els)
        if richness < 2:
            continue
        score = (int(richness), len(els))
        if score > best_score:
            best_score = score
            best_tag = tag
            best_records = els

    if best_records:
        return best_records, best_tag or "unknown"

    direct_children = list(root)
    if len(direct_children) >= 2:
        return direct_children, localname(direct_children[0].tag)
    if len(direct_children) == 1 and len(list(direct_children[0])) >= 2:
        children = list(direct_children[0])
        return children, localname(children[0].tag)

    raise RuntimeError("Could not detect repeating job records in the XML feed.")


def find_first_text(record: ET.Element, aliases: list[str]) -> str:
    alias_set = {a.lower() for a in aliases}

    for child in list(record):
        if localname(child.tag) in alias_set:
            txt = text_from_element(child)
            if txt:
                return txt

    for el in record.iter():
        if el is record:
            continue
        if localname(el.tag) in alias_set:
            txt = text_from_element(el)
            if txt:
                return txt

    return ""


def normalize_location(record: ET.Element) -> str:
    location = find_first_text(record, FIELD_ALIASES["location"])
    if location:
        return location
    parts = [
        find_first_text(record, FIELD_ALIASES["city"]),
        find_first_text(record, FIELD_ALIASES["state"]),
        find_first_text(record, FIELD_ALIASES["country"]),
    ]
    parts = [p for p in parts if p]
    return ", ".join(dict.fromkeys(parts))


def normalize_apply_url(record: ET.Element) -> str:
    raw = find_first_text(record, FIELD_ALIASES["apply_url"])
    if not raw:
        return ""

    url = urljoin(FEED_URL, raw).strip()

    # Convert Workday apply links to the job detail page
    url = re.sub(r"/apply/?$", "/", url)

    return url


def record_to_job(record: ET.Element, index: int) -> dict:
    title = find_first_text(record, FIELD_ALIASES["title"]) or f"Job {index}"
    job_id = find_first_text(record, FIELD_ALIASES["id"])
    description = find_first_text(record, FIELD_ALIASES["description"])
    if not description:
        description = text_from_element(record)
        if description.startswith(title):
            description = description[len(title):].strip(" -:\n\t")
    job = {
        "id": job_id or f"row-{index}",
        "title": title,
        "location": normalize_location(record),
        "department": find_first_text(record, FIELD_ALIASES["department"]),
        "employment_type": find_first_text(record, FIELD_ALIASES["employment_type"]),
        "updated": find_first_text(record, FIELD_ALIASES["updated"]),
        "apply_url": normalize_apply_url(record),
        "description": description,
    }
    job["slug"] = slugify(f"{job['id']}-{job['title']}")
    return job


def html_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def render_job_page(job: dict) -> str:
    meta_bits = []
    if job["location"]:
        meta_bits.append(f"<p><strong>Location:</strong> {html_escape(job['location'])}</p>")
    if job["department"]:
        meta_bits.append(f"<p><strong>Department:</strong> {html_escape(job['department'])}</p>")
    if job["employment_type"]:
        meta_bits.append(f"<p><strong>Employment type:</strong> {html_escape(job['employment_type'])}</p>")
    if job["updated"]:
        meta_bits.append(f"<p><strong>Updated:</strong> {html_escape(job['updated'])}</p>")
    if job["apply_url"]:
        meta_bits.append(
            f'<p><strong>Apply:</strong> <a href="{html_escape(job["apply_url"])}">{html_escape(job["apply_url"])}</a></p>'
        )

    description_paragraphs = []
    for para in re.split(r"\n{2,}", job["description"]):
        para = re.sub(r"\s+", " ", para).strip()
        if para:
            description_paragraphs.append(f"<p>{html_escape(para)}</p>")

    if not description_paragraphs:
        description_paragraphs = ["<p>No description was available in the feed for this job.</p>"]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="index,follow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(job['title'])} | {html_escape(SITE_TITLE)}</title>
</head>
<body>
  <main>
    <p><a href="../index.html">Back to all jobs</a></p>
    <article>
      <h1>{html_escape(job['title'])}</h1>
      {''.join(meta_bits)}
      <section>
        <h2>Description</h2>
        {''.join(description_paragraphs)}
      </section>
    </article>
  </main>
</body>
</html>
"""


def render_index(jobs: list[dict], fetched_at: str) -> str:
    items = []

    for job in jobs:
        location_html = f"<p><strong>Location:</strong> {html_escape(job['location'])}</p>" if job["location"] else ""
        department_html = f"<p><strong>Department:</strong> {html_escape(job['department'])}</p>" if job["department"] else ""
        employment_type_html = (
            f"<p><strong>Employment type:</strong> {html_escape(job['employment_type'])}</p>"
            if job["employment_type"]
            else ""
        )
        apply_html = (
            f'<p><strong>Apply:</strong> <a href="{html_escape(job["apply_url"])}">{html_escape(job["apply_url"])}</a></p>'
            if job["apply_url"]
            else ""
        )

        description_paragraphs = []
        for para in re.split(r"\n{2,}", job["description"]):
            para = re.sub(r"\s+", " ", para).strip()
            if para:
                description_paragraphs.append(f"<p>{html_escape(para)}</p>")

        if not description_paragraphs:
            description_paragraphs = ["<p>No description was available in the feed for this job.</p>"]

        items.append(f"""
        <article>
          <h2><a href="jobs/{html_escape(job['slug'])}.html">{html_escape(job['title'])}</a></h2>
          {location_html}
          {department_html}
          {employment_type_html}
          {apply_html}
          {''.join(description_paragraphs)}
        </article>
        """)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="robots" content="index,follow">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(SITE_TITLE)}</title>
</head>
<body>
  <main>
    <h1>{html_escape(SITE_TITLE)}</h1>
    <p>This page is generated automatically from the ChristianaCare XML feed.</p>
    <p><strong>Last refreshed:</strong> {html_escape(fetched_at)}</p>
    <p><strong>Total jobs:</strong> {len(jobs)}</p>
    {''.join(items)}
  </main>
</body>
</html>
"""


def render_sitemap(jobs: list[dict]) -> str:
    urls = [f"{BASE_URL}/", f"{BASE_URL}/index.html", f"{BASE_URL}/sitemap.xml"]
    urls.extend(f"{BASE_URL}/jobs/{job['slug']}.html" for job in jobs)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    ]
    today = datetime.now(timezone.utc).date().isoformat()
    for url in urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{html_escape(url)}</loc>")
        lines.append(f"    <lastmod>{today}</lastmod>")
        lines.append("  </url>")
    lines.append("</urlset>")
    return "\n".join(lines)


def main() -> int:
    reset_dir(OUT_DIR)
    reset_dir(DEBUG_DIR)

    xml_text = fetch_xml(FEED_URL)
    write_text(DEBUG_DIR / "feed-sample.xml", xml_text[:20000])

    root = ET.fromstring(xml_text.encode("utf-8", errors="ignore"))
    records, record_tag = detect_records(root)
    if MAX_JOBS > 0:
        records = records[:MAX_JOBS]

    jobs = [record_to_job(record, i) for i, record in enumerate(records, start=1)]

    deduped = []
    seen = set()
    for job in jobs:
        slug = job["slug"]
        n = 2
        original = slug
        while slug in seen:
            slug = f"{original}-{n}"
            n += 1
        job["slug"] = slug
        seen.add(slug)
        deduped.append(job)
    jobs = deduped

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    write_text(OUT_DIR / "index.html", render_index(jobs, fetched_at))
    write_text(OUT_DIR / "sitemap.xml", render_sitemap(jobs))
    write_text(OUT_DIR / "robots.txt", "User-agent: *\nAllow: /\n")

    for job in jobs:
        write_text(OUT_DIR / "jobs" / f"{job['slug']}.html", render_job_page(job))

    summary = {
        "feed_url": FEED_URL,
        "base_url": BASE_URL,
        "record_tag_detected": record_tag,
        "jobs_built": len(jobs),
        "first_job": jobs[0] if jobs else None,
        "generated_at": fetched_at,
    }
    write_text(DEBUG_DIR / "summary.json", json.dumps(summary, indent=2, ensure_ascii=False))

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
