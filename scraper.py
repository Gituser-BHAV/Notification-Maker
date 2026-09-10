import os
import re
import sqlite3
import smtplib
import html
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

# ============================================================
# New Code
# ============================================================
def parse_last_date(text):
    """
    Extract the application last date from job text.

    Supported examples:
    31/12/2026
    31-12-2026
    31.12.2026
    31 December 2026
    31 Dec 2026
    """

    if not text:
        return None

    text = re.sub(r"\s+", " ", text)

    # Only search near application/deadline wording.
    deadline_keywords = [
        "last date",
        "last date to apply",
        "application last date",
        "online application",
        "apply online till",
        "closing date",
        "application deadline",
        "registration last date",
    ]

    keyword_pattern = "|".join(
        re.escape(keyword) for keyword in deadline_keywords
    )

    # Capture a reasonable section after a deadline keyword.
    match = re.search(
        rf"(?:{keyword_pattern}).{{0,120}}",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    deadline_text = match.group(0)

    # Numeric date formats: 31/12/2026, 31-12-2026, 31.12.2026
    numeric_match = re.search(
        r"\b(0?[1-9]|[12][0-9]|3[01])\s*[/\-.]\s*"
        r"(0?[1-9]|1[0-2])\s*[/\-.]\s*(20\d{2})\b",
        deadline_text,
        flags=re.IGNORECASE,
    )

    if numeric_match:
        day, month, year = map(int, numeric_match.groups())

        try:
            return date(year, month, day)
        except ValueError:
            return None

    # Text date formats: 31 December 2026 / 31 Dec 2026
    month_names = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December|"
        "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )

    text_match = re.search(
        rf"\b(0?[1-9]|[12][0-9]|3[01])\s+"
        rf"({month_names})\s+(20\d{{2}})\b",
        deadline_text,
        flags=re.IGNORECASE,
    )

    if text_match:
        day = int(text_match.group(1))
        month_text = text_match.group(2)
        year = int(text_match.group(3))

        for fmt in ("%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(
                    f"{day} {month_text} {year}",
                    fmt,
                ).date()
            except ValueError:
                continue

    return None
# ============================================================
# Filter Function
# ============================================================
def is_expired_job(job_text):
    """
    Returns True when the job's application deadline
    is before today's date.
    """

    last_date = parse_last_date(job_text)

    # If no deadline was found, do not automatically reject it.
    # This prevents valid jobs from being discarded because
    # the website uses an unusual date format.
    if last_date is None:
        return False

    today = date.today()

    return last_date < today
# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://sarkariresult.com.cm"
LATEST_URL = f"{BASE_URL}/latest-jobs/"
DB_FILE = "jobs.db"

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD")
TO_EMAIL = os.environ.get("TO_EMAIL")
TEST_URL = os.environ.get("TEST_URL")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0 Safari/537.36"
    )
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT,
            added_at TEXT
        )
    """)

    conn.commit()
    return conn


def already_processed(conn, url):
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE url = ? LIMIT 1",
        (url,)
    ).fetchone()

    return row is not None


def save_job(conn, url, title):
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
        (url, title, added_at)
        VALUES (?, ?, ?)
        """,
        (
            url,
            title,
            datetime.utcnow().isoformat()
        )
    )

    conn.commit()


# ============================================================
# HTTP
# ============================================================

def fetch_page(url):
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response.text

    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return None


# ============================================================
# GENERAL TEXT HELPERS
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = html.unescape(text)

    text = text.replace("\xa0", " ")
    text = text.replace("\u200b", "")
    text = text.replace("\r", "\n")

    lines = []

    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            lines.append(line)

    return "\n".join(lines)


def one_line(text):
    if not text:
        return ""

    return re.sub(r"\s+", " ", text).strip()


def normalize_label(text):
    text = one_line(text).lower()

    text = text.replace(":", "")
    text = text.replace("-", " ")

    return re.sub(r"\s+", " ", text).strip()


def unique_lines(lines):
    result = []
    seen = set()

    for line in lines:
        line = one_line(line)

        if not line:
            continue

        key = line.lower()

        if key not in seen:
            seen.add(key)
            result.append(line)

    return result


# ============================================================
# MAIN ARTICLE
# ============================================================

def get_main_container(soup):
    candidates = [
        soup.select_one(".entry-content"),
        soup.select_one(".post-content"),
        soup.select_one(".td-post-content"),
        soup.select_one("article"),
        soup.select_one("main"),
    ]

    for candidate in candidates:
        if candidate and len(candidate.get_text(" ", strip=True)) > 500:
            return candidate

    return soup


# ============================================================
# HEADINGS
# ============================================================

HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def is_heading(tag):
    if not isinstance(tag, Tag):
        return False

    return tag.name in HEADING_TAGS


def heading_text(tag):
    return one_line(tag.get_text(" ", strip=True))


def find_heading(container, patterns):
    """
    Finds a heading whose text matches one of the supplied patterns.
    """

    patterns = [p.lower() for p in patterns]

    for tag in container.find_all(HEADING_TAGS):
        text = heading_text(tag).lower()

        for pattern in patterns:
            if pattern in text:
                return tag

    return None


# ============================================================
# SECTION EXTRACTION
# ============================================================

def get_section_lines(container, heading, stop_keywords=None):
    """
    Extracts content after a heading until another heading is reached.

    Unlike the older implementation, this does not rely on heading
    levels. This prevents things such as Application Fee content
    accidentally swallowing Age Limit/Post Details.
    """

    if not heading:
        return []

    stop_keywords = [
        x.lower() for x in (stop_keywords or [])
    ]

    lines = []

    for element in heading.find_all_next():

        if element == heading:
            continue

        if is_heading(element):
            break

        # Stop at common section markers even if the website uses
        # unusual HTML instead of proper heading tags.
        if isinstance(element, Tag):

            text = one_line(element.get_text(" ", strip=True))

            lower = text.lower()

            if text and any(
                lower.startswith(keyword)
                for keyword in stop_keywords
            ):
                break

            if element.name in ["p", "li"]:
                if text:
                    lines.append(text)

            elif element.name == "tr":
                cells = [
                    one_line(c.get_text(" ", strip=True))
                    for c in element.find_all(["td", "th"])
                ]

                cells = [c for c in cells if c]

                if cells:
                    lines.append(" | ".join(cells))

    return unique_lines(lines)


def get_section_text(container, heading, stop_keywords=None):
    return "\n".join(
        get_section_lines(
            container,
            heading,
            stop_keywords
        )
    )


# ============================================================
# TITLE
# ============================================================

def extract_title(soup):
    candidates = [
        soup.select_one("h1"),
        soup.select_one("h2"),
    ]

    for tag in candidates:
        if tag:
            text = one_line(tag.get_text(" ", strip=True))

            if text and len(text) > 5:
                return text

    if soup.title:
        title = one_line(soup.title.get_text())

        title = re.sub(
            r"\s*[-|]\s*Sarkari Result.*$",
            "",
            title,
            flags=re.I
        )

        return title

    return "New Job Notification"


# ============================================================
# ORGANIZATION
# ============================================================

def extract_organization(container):
    text = clean_text(
        container.get_text("\n", strip=True)
    )

    patterns = [
        r"^(.*?)\s+has released",
        r"^(.*?)\s+has announced",
        r"^(.*?)\s+invites",
        r"^(.*?)\s+is inviting",
    ]

    for line in text.split("\n"):

        line = one_line(line)

        for pattern in patterns:

            match = re.search(
                pattern,
                line,
                re.I
            )

            if match:

                org = one_line(
                    match.group(1)
                )

                if 3 < len(org) < 200:
                    return org

    # Common fallback headings
    heading = find_heading(
        container,
        [
            "Organization",
            "Department",
            "Recruiting Organization"
        ]
    )

    if heading:
        lines = get_section_lines(
            container,
            heading
        )

        if lines:
            return lines[0]

    return "Not found"


# ============================================================
# TOTAL VACANCIES
# ============================================================

def extract_total_vacancies(container):
    heading = find_heading(
        container,
        [
            "Total Post",
            "Total Posts",
            "Total Vacancy",
            "Total Vacancies",
            "Number of Post"
        ]
    )

    if heading:
        lines = get_section_lines(
            container,
            heading,
            stop_keywords=[
                "education qualification",
                "educational qualification",
                "eligibility",
                "age limit",
                "application fee",
                "important date",
                "important dates",
                "post details",
            ]
        )

        for line in lines:

            if re.search(
                r"\d[\d,]*\s*(posts?|vacancies?)",
                line,
                re.I
            ):
                return line

            if re.fullmatch(
                r"[\d,]+",
                line
            ):
                return f"{line} Posts"

    # Search nearby text as fallback
    text = clean_text(
        container.get_text("\n", strip=True)
    )

    patterns = [
        r"Total\s+(?:Post|Posts|Vacancy|Vacancies)\s*[:\-]?\s*([\d,]+)",
        r"([\d,]+)\s+(?:Posts|Vacancies)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.I
        )

        if match:
            return f"{match.group(1)} Posts"

    return "Not found"


# ============================================================
# ELIGIBILITY
# ============================================================

def extract_eligibility(container):
    heading = find_heading(
        container,
        [
            "Eligibility Criteria",
            "Eligibility",
            "Education Qualification",
            "Educational Qualification",
            "Educational Qualification Details",
            "Qualification"
        ]
    )

    lines = []

    if heading:

        lines = get_section_lines(
            container,
            heading,
            stop_keywords=[
                "age limit",
                "application fee",
                "important date",
                "important dates",
                "post details",
                "selection process",
                "how to apply",
                "some useful",
                "you may also check",
                "useful important links",
                "important links",
                "faq",
            ]
        )

    # Tables are particularly important for SSC-style pages.
    if heading:

        table = heading.find_next("table")

        if table:

            for row in table.find_all("tr"):

                cells = [
                    one_line(
                        cell.get_text(
                            " ",
                            strip=True
                        )
                    )
                    for cell in row.find_all(
                        ["td", "th"]
                    )
                ]

                cells = [
                    c for c in cells
                    if c
                ]

                if cells:
                    lines.append(
                        " | ".join(cells)
                    )

    lines = unique_lines(lines)

    # Remove obvious non-eligibility contamination.
    filtered = []

    bad_patterns = [
        r"^application fee",
        r"^age limit",
        r"^minimum age",
        r"^maximum age",
        r"^post details",
        r"^important dates?",
        r"^selection process",
        r"^how to apply",
    ]

    for line in lines:

        if any(
            re.search(
                pattern,
                line,
                re.I
            )
            for pattern in bad_patterns
        ):
            continue

        filtered.append(line)

    if filtered:
        return filtered

    return ["Not found"]


# ============================================================
# APPLICATION FEE
# ============================================================

def extract_application_fee(container):
    heading = find_heading(
        container,
        [
            "Application Fee",
            "Application Fees",
            "Exam Fee"
        ]
    )

    if not heading:
        return "Not found"

    lines = get_section_lines(
        container,
        heading,
        stop_keywords=[
            "age limit",
            "minimum age",
            "maximum age",
            "age relaxation",
            "post details",
            "education qualification",
            "educational qualification",
            "eligibility",
            "important date",
            "important dates",
            "selection process",
            "how to apply",
            "some useful",
            "useful important links",
        ]
    )

    valid = []

    for line in lines:

        lower = line.lower()

        # Explicit no-fee statements
        if (
            "no application fee" in lower
            or "no fee" in lower
            or "application fee is nil" in lower
            or "fee is nil" in lower
        ):
            valid.append(line)
            continue

        # Fee lines normally contain ₹, Rs, INR, or fee-related wording.
        if (
            "₹" in line
            or "rs." in lower
            or "rs " in lower
            or "inr" in lower
            or "fee" in lower
        ):
            # Avoid accidentally accepting age/post information.
            if not re.search(
                r"\bage\b|\bpost details\b|\bminimum age\b|\bmaximum age\b",
                lower
            ):
                valid.append(line)

    valid = unique_lines(valid)

    if valid:
        return "\n".join(valid)

    return "Not found"


# ============================================================
# DATES
# ============================================================

DATE_LABELS = {
    "start": [
        "application start date",
        "online application start date",
        "online application start",
        "application start",
        "form start date",
        "start date",
    ],

    "last": [
        "last date for apply online",
        "last date to apply online",
        "application last date",
        "last date to apply",
        "last date",
        "closing date",
    ],

    "fee": [
        "fee payment last date",
        "last date for fee payment",
        "fee payment date",
        "last date of fee payment",
    ],

    "correction": [
        "correction date",
        "correction window",
        "online correction date",
        "online correction",
        "application correction",
    ],

    "exam": [
        "exam date",
        "examination date",
        "written exam date",
    ],

    "admit": [
        "admit card date",
        "admit card",
    ],

    "result": [
        "result date",
        "result",
    ],
}

def find_label_value(lines, labels):
    # Check longer labels first so:
    # "Last Date for Fee Payment"
    # does not get incorrectly matched as:
    # "Last Date"

    labels = sorted(
        labels,
        key=len,
        reverse=True
    )

    for i, line in enumerate(lines):

        normalized = normalize_label(line)

        for label in labels:

            label_norm = normalize_label(label)

            if normalized.startswith(label_norm):

                value = line[
                    len(label_norm):
                ].strip(" :-")

                if value:
                    # Remove extra wording such as:
                    # "for Apply Online : District Wise"
                    value = re.sub(
                        r"^for\s+apply\s+online\s*[:\-]?\s*",
                        "",
                        value,
                        flags=re.I
                    )

                    return value

                if i + 1 < len(lines):
                    return lines[i + 1]

    return None
def extract_dates(container):
    result = {
        "start": "Not found",
        "last": "Not found",
        "fee": "Not found",
        "correction": "Not found",
        "exam": "Not found",
        "admit": "Not found",
        "result": "Not found",
    }

    # First inspect Important Dates section.
    heading = find_heading(
        container,
        [
            "Important Dates",
            "Important Date",
            "Important Dates / Schedule",
        ]
    )

    if heading:
        lines = get_section_lines(
            container,
            heading,
            stop_keywords=[
                "application fee",
                "age limit",
                "eligibility",
                "education qualification",
                "how to apply",
                "selection process",
                "some useful",
                "useful important links",
            ]
        )
    else:
        lines = clean_text(
            container.get_text("\n", strip=True)
        ).split("\n")

    lines = unique_lines(lines)

    for key, labels in DATE_LABELS.items():

        value = find_label_value(
            lines,
            labels
        )

        if value:
            result[key] = value

    # --------------------------------------------------------
    # Regex fallback for common date formats
    # --------------------------------------------------------

    all_text = "\n".join(lines)

    date_pattern = (
        r"\d{1,2}"
        r"(?:st|nd|rd|th)?"
        r"\s+"
        r"[A-Za-z]+"
        r"\s+"
        r"\d{4}"
    )

    for line in lines:

        dates = re.findall(
            date_pattern,
            line
        )

        if not dates:
            continue

        lower = line.lower()

        if result["start"] == "Not found":
            if "start" in lower:
                result["start"] = dates[0]

        if result["last"] == "Not found":
            if "last date" in lower:
                result["last"] = dates[-1]

    # --------------------------------------------------------
    # District-wise fallback
    # --------------------------------------------------------

    full_page = clean_text(
        container.get_text("\n", strip=True)
    )

    district_wise = bool(
        re.search(
            r"district[\s\-]?wise",
            full_page,
            re.I
        )
    )

    if district_wise:

        if result["start"] == "Not found":
            if re.search(
                r"application.*district[\s\-]?wise",
                full_page,
                re.I
            ):
                result["start"] = "District Wise"

        if result["last"] == "Not found":
            if re.search(
                r"last date.*district[\s\-]?wise",
                full_page,
                re.I
            ):
                result["last"] = "District Wise"

        # Some pages simply say that dates are district-wise.
        if (
            result["start"] == "Not found"
            and result["last"] == "Not found"
            and re.search(
                r"start.*district[\s\-]?wise",
                full_page,
                re.I
            )
        ):
            result["start"] = "District Wise"
            result["last"] = "District Wise"

    return result


# ============================================================
# USEFUL LINKS
# ============================================================

def classify_link(label, href):
    label = one_line(label).lower()
    href = href.lower()

    if (
        "online correction" in label
        or "correction" in label
    ):
        return "correction"

    if (
        "official notification" in label
        or "download notification" in label
        or "notification" in label
    ):
        return "notification"

    if (
        "official website" in label
        or label.strip() == "official site"
        or label.strip() == "website"
    ):
        return "website"

    if (
        "apply online" in label
        or "registration" in label
        or "login" in label
        or "apply now" in label
    ):
        return "apply"

    # URL-based fallback
    if "registration" in href:
        return "apply"

    return None


def extract_useful_links(container, job_url):
    result = {
        "apply": "Not found",
        "notification": "Not found",
        "correction": "Not found",
        "website": "Not found",
    }

    heading = find_heading(
        container,
        [
            "SOME USEFUL IMPORTANT LINKS",
            "Useful Important Links",
            "Important Links",
            "Useful Links",
        ]
    )

    search_root = heading if heading else container

    # --------------------------------------------------------
    # TABLE-FIRST APPROACH
    # --------------------------------------------------------

    tables = []

    if heading:
        for table in heading.find_all_next("table"):
            tables.append(table)

            # Usually useful-links table is small.
            if len(tables) >= 3:
                break
    else:
        tables = container.find_all("table")

    for table in tables:

        for row in table.find_all("tr"):

            anchors = row.find_all("a", href=True)

            if not anchors:
                continue

            row_text = one_line(
                row.get_text(
                    " ",
                    strip=True
                )
            )

            for anchor in anchors:

                href = urljoin(
                    job_url,
                    anchor.get("href")
                )

                anchor_text = one_line(
                    anchor.get_text(
                        " ",
                        strip=True
                    )
                )

                label = (
                    f"{row_text} "
                    f"{anchor_text}"
                )

                kind = classify_link(
                    label,
                    href
                )

                if kind:
                    # Correction must never replace Apply Online.
                    if kind == "correction":
                        result["correction"] = href

                    elif result[kind] == "Not found":
                        result[kind] = href

    # --------------------------------------------------------
    # ANCHOR FALLBACK
    # --------------------------------------------------------

    for anchor in search_root.find_all(
        "a",
        href=True
    ):

        href = urljoin(
            job_url,
            anchor.get("href")
        )

        text = one_line(
            anchor.get_text(
                " ",
                strip=True
            )
        )

        # Include nearby parent text.
        parent_text = ""

        if anchor.parent:
            parent_text = one_line(
                anchor.parent.get_text(
                    " ",
                    strip=True
                )
            )

        label = (
            f"{text} {parent_text}"
        )

        kind = classify_link(
            label,
            href
        )

        if kind:

            if kind == "correction":
                result["correction"] = href

            elif result[kind] == "Not found":
                result[kind] = href

    return result


# ============================================================
# DISTRICT-WISE WEBSITE FALLBACK
# ============================================================

def extract_official_website_from_text(container):
    text = clean_text(
        container.get_text("\n", strip=True)
    )

    # Common "official website: URL" pattern
    match = re.search(
        r"official website\s*[:\-]?\s*(https?://[^\s]+)",
        text,
        re.I
    )

    if match:
        return match.group(1).rstrip(").,;")

    return None


# ============================================================
# JOB PAGE EXTRACTION
# ============================================================

def extract_job_page(url, html_content):
    soup = BeautifulSoup(
        html_content,
        "html.parser"
    )

    container = get_main_container(
        soup
    )

    title = extract_title(
        soup
    )

    organization = extract_organization(
        container
    )

    vacancies = extract_total_vacancies(
        container
    )

    eligibility = extract_eligibility(
        container
    )

    fee = extract_application_fee(
        container
    )

    dates = extract_dates(
        container
    )

    links = extract_useful_links(
        container,
        url
    )

    # Official website fallback
    if links["website"] == "Not found":

        website = extract_official_website_from_text(
            container
        )

        if website:
            links["website"] = website

    # --------------------------------------------------------
    # UP ANGanwadi-style fallback
    # --------------------------------------------------------

    page_text = clean_text(
        container.get_text(
            "\n",
            strip=True
        )
    )

    # If the page clearly states no application fee,
    # prefer that over a contaminated section.
    if re.search(
        r"there is no application fee",
        page_text,
        re.I
    ):
        fee = "No application fee"

    # If the page says application/last date is district-wise,
    # ensure the email doesn't say Not found.
    if re.search(
        r"application.*district[\s\-]?wise",
        page_text,
        re.I
    ):
        if dates["start"] == "Not found":
            dates["start"] = "District Wise"

    if re.search(
        r"last date.*district[\s\-]?wise",
        page_text,
        re.I
    ):
        if dates["last"] == "Not found":
            dates["last"] = "District Wise"

    return {
        "title": title,
        "organization": organization,
        "vacancies": vacancies,
        "eligibility": eligibility,
        "fee": fee,
        "start_date": dates["start"],
        "last_date": dates["last"],
        "fee_date": dates["fee"],
        "correction_date": dates["correction"],
        "exam_date": dates["exam"],
        "admit_card": dates["admit"],
        "result_date": dates["result"],
        "apply": links["apply"],
        "notification": links["notification"],
        "correction": links["correction"],
        "website": links["website"],
        "job_page": url,
    }


# ============================================================
# LATEST JOB LINKS
# ============================================================

def get_job_links():
    html_content = fetch_page(
        LATEST_URL
    )

    if not html_content:
        return []

    soup = BeautifulSoup(
        html_content,
        "html.parser"
    )

    links = []

    # Find the "All Latest Jobs" area.
    heading = find_heading(
        soup,
        [
            "All Latest Jobs",
            "Latest Jobs",
        ]
    )

    if heading:

        for element in heading.find_all_next():

            if is_heading(element):
                if element != heading:
                    break

            if not isinstance(element, Tag):
                continue

            if element.name != "a":
                continue

            href = element.get("href")

            if not href:
                continue

            href = urljoin(
                BASE_URL,
                href
            )

            parsed = urlparse(href)

            if parsed.netloc not in [
                "",
                urlparse(BASE_URL).netloc
            ]:
                continue

            if href.rstrip("/") == LATEST_URL.rstrip("/"):
                continue

            if "/latest-jobs" in href.lower():
                continue

            if href not in links:
                links.append(href)

    # --------------------------------------------------------
    # Fallback if heading structure changes
    # --------------------------------------------------------

    if not links:

        for anchor in soup.find_all(
            "a",
            href=True
        ):

            href = urljoin(
                BASE_URL,
                anchor["href"]
            )

            parsed = urlparse(
                href
            )

            if parsed.netloc != urlparse(BASE_URL).netloc:
                continue

            if href.rstrip("/") == LATEST_URL.rstrip("/"):
                continue

            if "/latest-jobs" in href.lower():
                continue

            if href not in links:
                links.append(href)

    print(
        f"Found {len(links)} job links."
    )

    return links


# ============================================================
# EMAIL FORMATTING
# ============================================================

def html_escape(value):
    return html.escape(
        str(value)
    )


def format_eligibility(items):
    if not items:
        return "<li>Not found</li>"

    result = []

    for item in items:

        # Convert table-style entries into readable text.
        item = item.replace(
            " | ",
            " — "
        )

        result.append(
            f"<li>{html_escape(item)}</li>"
        )

    return "\n".join(result)


def format_multiline(value):
    if not value:
        return "Not found"

    lines = str(value).split("\n")

    return "<br>".join(
        html_escape(
            line
        )
        for line in lines
        if line.strip()
    )


def build_email(job):
    title = html_escape(
        job["title"]
    )

    organization = html_escape(
        job["organization"]
    )

    vacancies = html_escape(
        job["vacancies"]
    )

    fee = format_multiline(
        job["fee"]
    )

    start_date = html_escape(
        job["start_date"]
    )

    last_date = html_escape(
        job["last_date"]
    )

    fee_date = html_escape(
        job["fee_date"]
    )

    correction_date = html_escape(
        job["correction_date"]
    )

    exam_date = html_escape(
        job["exam_date"]
    )

    admit_card = html_escape(
        job["admit_card"]
    )

    result_date = html_escape(
        job["result_date"]
    )

    apply = job["apply"]
    notification = job["notification"]
    correction = job["correction"]
    website = job["website"]
    job_page = job["job_page"]

    def link_or_text(
        url,
        label
    ):
        if (
            url
            and url != "Not found"
            and url.startswith("http")
        ):
            return (
                f'<a href="{html_escape(url)}" '
                f'target="_blank">{html_escape(label)}</a>'
            )

        return html_escape(
            url or "Not found"
        )

    return f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">

<style>
body {{
    margin: 0;
    padding: 0;
    background: #f4f6f8;
    font-family: Arial, Helvetica, sans-serif;
    color: #222;
}}

.container {{
    max-width: 680px;
    margin: 30px auto;
    background: #ffffff;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 3px 14px rgba(0,0,0,0.08);
}}

.header {{
    padding: 24px;
    background: #111827;
    color: white;
}}

.header h1 {{
    margin: 0;
    font-size: 22px;
}}

.header p {{
    margin: 7px 0 0;
    opacity: 0.8;
    font-size: 13px;
}}

.content {{
    padding: 24px;
}}

.section {{
    margin-bottom: 22px;
}}

.section-title {{
    font-size: 15px;
    font-weight: bold;
    margin-bottom: 8px;
    color: #111827;
}}

.value {{
    font-size: 14px;
    line-height: 1.6;
}}

ul {{
    margin-top: 6px;
    padding-left: 20px;
}}

li {{
    margin-bottom: 7px;
    line-height: 1.5;
}}

table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}}

td {{
    padding: 9px 0;
    border-bottom: 1px solid #eeeeee;
    vertical-align: top;
}}

td:first-child {{
    width: 42%;
    font-weight: bold;
}}

a {{
    color: #2563eb;
    text-decoration: none;
}}

.footer {{
    padding: 18px 24px;
    background: #f9fafb;
    font-size: 12px;
    color: #6b7280;
}}
</style>

</head>

<body>

<div class="container">

    <div class="header">
        <h1>New Job Notification</h1>
        <p>{title}</p>
    </div>

    <div class="content">

        <div class="section">
            <div class="section-title">
                Job Name
            </div>

            <div class="value">
                {title}
            </div>
        </div>


        <div class="section">
            <div class="section-title">
                Organization
            </div>

            <div class="value">
                {organization}
            </div>
        </div>


        <div class="section">
            <div class="section-title">
                Total Vacancies
            </div>

            <div class="value">
                {vacancies}
            </div>
        </div>


        <div class="section">
            <div class="section-title">
                Eligibility Criteria
            </div>

            <ul>
                {format_eligibility(job["eligibility"])}
            </ul>
        </div>


        <div class="section">
            <div class="section-title">
                Application Fee
            </div>

            <div class="value">
                {fee}
            </div>
        </div>


        <div class="section">
            <div class="section-title">
                Important Dates
            </div>

            <table>

                <tr>
                    <td>Application Start</td>
                    <td>{start_date}</td>
                </tr>

                <tr>
                    <td>Last Date</td>
                    <td>{last_date}</td>
                </tr>

                <tr>
                    <td>Fee Payment</td>
                    <td>{fee_date}</td>
                </tr>

                <tr>
                    <td>Correction</td>
                    <td>{correction_date}</td>
                </tr>

                <tr>
                    <td>Exam Date</td>
                    <td>{exam_date}</td>
                </tr>

                <tr>
                    <td>Admit Card</td>
                    <td>{admit_card}</td>
                </tr>

                <tr>
                    <td>Result</td>
                    <td>{result_date}</td>
                </tr>

            </table>
        </div>


        <div class="section">
            <div class="section-title">
                Useful Links
            </div>

            <table>

                <tr>
                    <td>Apply Online</td>
                    <td>
                        {link_or_text(
                            apply,
                            "Apply Online"
                        )}
                    </td>
                </tr>

                <tr>
                    <td>Official Notification</td>
                    <td>
                        {link_or_text(
                            notification,
                            "Official Notification"
                        )}
                    </td>
                </tr>

                <tr>
                    <td>Online Correction</td>
                    <td>
                        {link_or_text(
                            correction,
                            "Online Correction"
                        )}
                    </td>
                </tr>

                <tr>
                    <td>Official Website</td>
                    <td>
                        {link_or_text(
                            website,
                            "Official Website"
                        )}
                    </td>
                </tr>

                <tr>
                    <td>Job Page</td>
                    <td>
                        {link_or_text(
                            job_page,
                            "View Job Page"
                        )}
                    </td>
                </tr>

            </table>
        </div>

    </div>


    <div class="footer">
        Automated notification from your Government Job Monitor.
    </div>

</div>

</body>
</html>
"""


# ============================================================
# EMAIL SENDING
# ============================================================

def send_email(job):
    if not EMAIL_ADDRESS:
        raise RuntimeError(
            "EMAIL_ADDRESS secret is missing."
        )

    if not EMAIL_APP_PASSWORD:
        raise RuntimeError(
            "EMAIL_APP_PASSWORD secret is missing."
        )

    if not TO_EMAIL:
        raise RuntimeError(
            "TO_EMAIL secret is missing."
        )

    message = MIMEMultipart(
        "alternative"
    )

    message["From"] = EMAIL_ADDRESS
    message["To"] = TO_EMAIL

    message["Subject"] = (
        f"New Job: {job['title']}"
    )

    body = build_email(
        job
    )

    message.attach(
        MIMEText(
            body,
            "html",
            "utf-8"
        )
    )

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as server:

        server.login(
            EMAIL_ADDRESS,
            EMAIL_APP_PASSWORD
        )

        server.sendmail(
            EMAIL_ADDRESS,
            TO_EMAIL,
            message.as_string()
        )


# ============================================================
# MAIN
# ============================================================
def test_single_url(url):
    print("=" * 60)
    print("TEST MODE")
    print("=" * 60)
    print(f"Testing URL:\n{url}")
    print()

    page = fetch_page(url)

    if not page:
        print("Could not fetch the page.")
        return

    try:
        job = extract_job_page(
            url,
            page
        )

        # ----------------------------------------------------
        # Console output
        # ----------------------------------------------------

        print(f"Job Name:\n{job['title']}")
        print()

        print(f"Organization:\n{job['organization']}")
        print()

        print(f"Total Vacancies:\n{job['vacancies']}")
        print()

        print("Eligibility Criteria:")

        for item in job["eligibility"]:
            print(f"  • {item}")

        print()

        print(f"Application Fee:\n{job['fee']}")
        print()

        print(
            f"Application Start Date:\n"
            f"{job['start_date']}"
        )
        print()

        print(
            f"Last Date:\n"
            f"{job['last_date']}"
        )
        print()

        print(
            f"Fee Payment Date:\n"
            f"{job['fee_date']}"
        )
        print()

        print(
            f"Correction Date:\n"
            f"{job['correction_date']}"
        )
        print()

        print(
            f"Exam Date:\n"
            f"{job['exam_date']}"
        )
        print()

        print(
            f"Admit Card:\n"
            f"{job['admit_card']}"
        )
        print()

        print(
            f"Result:\n"
            f"{job['result_date']}"
        )
        print()

        print(
            f"Apply Online:\n"
            f"{job['apply']}"
        )
        print()

        print(
            f"Official Notification:\n"
            f"{job['notification']}"
        )
        print()

        print(
            f"Online Correction:\n"
            f"{job['correction']}"
        )
        print()

        print(
            f"Official Website:\n"
            f"{job['website']}"
        )
        print()

        print(
            f"Job Page:\n"
            f"{job['job_page']}"
        )
        print()

        # ----------------------------------------------------
        # REAL EMAIL TEST
        # ----------------------------------------------------

        print("=" * 60)
        print("Sending test email...")
        print("=" * 60)

        send_email(job)

        print()
        print("TEST EMAIL SENT SUCCESSFULLY.")
        print()
        print("Database was NOT modified.")
        print("=" * 60)

    except Exception as e:

        print(
            f"Test failed: {e}"
        )

def main():

     # --------------------------------------------------------
    # TEST MODE
    # --------------------------------------------------------

    if TEST_URL:
        test_single_url(TEST_URL)
        return

    # --------------------------------------------------------
    # NORMAL MODE
    # --------------------------------------------------------

    print("=" * 60)
    print("Government Job Monitor")
    print("=" * 60)

    conn = init_db()

    links = get_job_links()

    if not links:
        print(
            "No job links found."
        )
        conn.close()
        return

    new_jobs = 0

    for index, url in enumerate(
        links,
        start=1
    ):

        print()
        print(
            f"[{index}/{len(links)}] {url}"
        )

        if already_processed(
            conn,
            url
        ):
            print(
                "Already processed."
            )
            continue

        print(
            "New job detected."
        )

        page = fetch_page(
            url
        )

        if not page:
            print(
                "Could not fetch job page. "
                "Will retry next run."
            )
            continue

        try:

            job = extract_job_page(
                url,
                page
            )

            print(
                f"Title: {job['title']}"
            )

            print(
                f"Organization: "
                f"{job['organization']}"
            )

            print(
                f"Vacancies: "
                f"{job['vacancies']}"
            )

            print(
                f"Start: "
                f"{job['start_date']}"
            )

            print(
                f"Last : "
                f"{job['last_date']}"
            )
            # ----------------------------------------------------
            # DEADLINE FILTER TEST
            # ----------------------------------------------------

            if is_expired_job(job["last_date"]):

                print("=" * 60)
                print("EXPIRED JOB")
                print("=" * 60)
                print(
                    f"Last date: {job['last_date']}"
                )    
                print(
                    "Email will NOT be sent."
                )
                print("=" * 60)

                return

            print("=" * 60)
            print("JOB IS STILL OPEN")
            print("=" * 60)

            # ------------------------------------------------
            # Send email FIRST.
            # Save to DB only after successful email.
            # ------------------------------------------------

            print(
                "Sending email..."
            )

            send_email(
                job
            )

            print(
                "Email sent successfully."
            )

            save_job(
                conn,
                url,
                job["title"]
            )

            print(
                "Job saved to database."
            )

            new_jobs += 1

        except Exception as e:

            print(
                f"Error processing job: {e}"
            )

            print(
                "Job was NOT saved. "
                "It will be retried next run."
            )

    conn.close()

    print()
    print("=" * 60)
    print(
        f"Finished. New jobs emailed: {new_jobs}"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
