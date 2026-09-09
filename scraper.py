import os
import re
import sqlite3
import smtplib
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIGURATION
# ============================================================

LATEST_JOBS_URL = "https://sarkariresult.com.cm/latest-jobs/"
DB_FILE = "jobs.db"

EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ["TO_EMAIL"]

SITE_DOMAIN = "https://sarkariresult.com.cm/"


# ============================================================
# HTTP SESSION
# ============================================================

def get_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Connection": "keep-alive",
    })

    return session


def fetch_page(session, url):
    print(f"Fetching: {url}")

    response = session.get(
        url,
        timeout=30,
        allow_redirects=True
    )

    response.raise_for_status()

    return response.text


# ============================================================
# TEXT / URL HELPERS
# ============================================================

def clean_text(text):
    if not text:
        return ""

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def clean_multiline_text(text):
    if not text:
        return ""

    lines = []

    for line in text.splitlines():
        line = clean_text(line)

        if line:
            lines.append(line)

    return "\n".join(lines)


def absolute_url(base_url, href):
    if not href:
        return None

    return urljoin(base_url, href.strip())


def is_site_url(url):
    if not url:
        return False

    parsed = urlparse(url)

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.endswith("sarkariresult.com.cm")
    )


def normalize_url(url):
    if not url:
        return None

    return url.split("#")[0].strip()


# ============================================================
# DATABASE
# ============================================================

def init_database():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            email_sent INTEGER DEFAULT 0
        )
    """)

    conn.commit()

    return conn


def job_exists(conn, url):
    result = conn.execute(
        """
        SELECT 1
        FROM jobs
        WHERE url = ?
        LIMIT 1
        """,
        (url,)
    ).fetchone()

    return result is not None


def save_job(conn, title, url):
    conn.execute(
        """
        INSERT OR IGNORE INTO jobs
        (url, title, first_seen, email_sent)
        VALUES (?, ?, ?, 0)
        """,
        (
            url,
            title,
            datetime.utcnow().isoformat()
        )
    )

    conn.commit()


def mark_email_sent(conn, url):
    conn.execute(
        """
        UPDATE jobs
        SET email_sent = 1
        WHERE url = ?
        """,
        (url,)
    )

    conn.commit()


# ============================================================
# LATEST JOBS PAGE
# ============================================================

def get_job_links(html, base_url):
    """
    Finds job links under the "All Latest Jobs" section.

    The website contains many unrelated links, so we start
    specifically from the All Latest Jobs heading.
    """

    soup = BeautifulSoup(html, "html.parser")

    links = []
    seen = set()

    heading = None

    for tag in soup.find_all(["h2", "h3", "h4"]):
        text = clean_text(
            tag.get_text(" ", strip=True)
        )

        if text.lower() == "all latest jobs":
            heading = tag
            break

    if not heading:
        print(
            "WARNING: Could not find 'All Latest Jobs' heading."
        )

        return []

    ignored_parts = [
        "/contact",
        "/privacy",
        "/disclaimer",
        "/author/",
        "/category/",
        "/feed/",
        "/page/",
        "/tag/",
        "/search/",
    ]

    for element in heading.find_all_next("a", href=True):

        title = clean_text(
            element.get_text(" ", strip=True)
        )

        href = element.get("href")

        if not title or not href:
            continue

        url = absolute_url(base_url, href)

        if not url:
            continue

        url = normalize_url(url)

        if not is_site_url(url):
            continue

        if any(
            part in url.lower()
            for part in ignored_parts
        ):
            continue

        if url.rstrip("/") == base_url.rstrip("/"):
            continue

        if url in seen:
            continue

        # Avoid navigation links such as Home, Latest Jobs, etc.
        if len(title) < 10:
            continue

        # Avoid obvious non-job navigation text.
        ignored_titles = {
            "home",
            "latest jobs",
            "admit card",
            "result",
            "admission",
            "syllabus",
            "answer key",
            "more",
            "contact us",
            "privacy policy",
            "disclaimer",
        }

        if title.lower() in ignored_titles:
            continue

        seen.add(url)

        links.append(
            (
                title,
                url
            )
        )

    return links


# ============================================================
# DOM SECTION HELPERS
# ============================================================

def heading_matches(text, keywords):
    text = clean_text(text).lower()

    return any(
        keyword.lower() in text
        for keyword in keywords
    )


def get_heading_level(tag):
    if not tag:
        return 0

    match = re.match(
        r"h([1-6])",
        tag.name or "",
        re.IGNORECASE
    )

    if match:
        return int(match.group(1))

    return 0


def find_section_heading(soup, keywords):
    """
    Finds headings such as:

    Important Dates
    Application Fee
    Total Post
    Eligibility Criteria
    SOME USEFUL IMPORTANT LINKS
    """

    for tag in soup.find_all(
        ["h2", "h3", "h4", "h5", "h6"]
    ):
        text = clean_text(
            tag.get_text(" ", strip=True)
        )

        if heading_matches(text, keywords):
            return tag

    return None


def collect_section_nodes(soup, heading):
    """
    Collect elements after a heading until another heading
    of the same or higher level is encountered.
    """

    if not heading:
        return []

    nodes = []

    level = get_heading_level(heading)

    for element in heading.find_all_next():

        if element is heading:
            continue

        if element.name in [
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6"
        ]:

            next_level = get_heading_level(element)

            if next_level <= level:
                break

        nodes.append(element)

    return nodes


def section_text(soup, keywords):
    heading = find_section_heading(
        soup,
        keywords
    )

    if not heading:
        return ""

    nodes = collect_section_nodes(
        soup,
        heading
    )

    pieces = []

    for node in nodes:

        if node.name in [
            "script",
            "style",
            "noscript",
            "iframe"
        ]:
            continue

        text = clean_text(
            node.get_text(" ", strip=True)
        )

        if text:
            pieces.append(text)

    # Remove duplicates while preserving order.
    unique = []

    for piece in pieces:
        if piece not in unique:
            unique.append(piece)

    return "\n".join(unique)


# ============================================================
# FIELD EXTRACTION
# ============================================================

def extract_label_value(text, labels):
    """
    Extracts the value immediately following a known label.

    Example:

    Online Apply Last Date : 07 October 2026

    returns:

    07 October 2026
    """

    if not text:
        return "Not found"

    for label in labels:

        pattern = (
            re.escape(label)
            + r"\s*[:\-]?\s*"
            r"(.+?)(?="
            r"\s+(?:Online Apply|Last Date|"
            r"Correction|Exam Date|Admit Card|"
            r"Result|Application Fee|Total Post|"
            r"Payment Mode)"
            r"|$)"
        )

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            value = clean_text(
                match.group(1)
            )

            if value:
                return value

    return "Not found"


def extract_dates(soup):
    """
    Extracts dates from the Important Dates section.
    """

    text = section_text(
        soup,
        [
            "Important Dates"
        ]
    )

    if not text:
        # Fallback: inspect the whole page.
        text = clean_multiline_text(
            soup.get_text("\n", strip=True)
        )

    start_date = extract_label_value(
        text,
        [
            "Online Apply Start Date",
            "Apply Online Start Date",
            "Application Start Date",
            "Start Date"
        ]
    )

    last_date = extract_label_value(
        text,
        [
            "Online Apply Last Date",
            "Apply Online Last Date",
            "Last Date for Apply Online",
            "Last Date For Apply Online",
            "Application Last Date",
            "Last Date"
        ]
    )

    fee_payment_date = extract_label_value(
        text,
        [
            "Last Date For Fee Payment",
            "Last Date for Fee Payment",
            "Last Date Pay Exam Fee",
            "Last Date For Payment"
        ]
    )

    correction_date = extract_label_value(
        text,
        [
            "Correction Date",
            "Last Date Correction",
            "Correction Last Date"
        ]
    )

    exam_date = extract_label_value(
        text,
        [
            "Exam Date"
        ]
    )

    admit_card = extract_label_value(
        text,
        [
            "Admit Card"
        ]
    )

    result_date = extract_label_value(
        text,
        [
            "Result Declared Date",
            "Result Date"
        ]
    )

    return {
        "start_date": start_date,
        "last_date": last_date,
        "fee_payment_date": fee_payment_date,
        "correction_date": correction_date,
        "exam_date": exam_date,
        "admit_card": admit_card,
        "result_date": result_date
    }


# ============================================================
# APPLICATION FEE
# ============================================================

def extract_application_fee(soup):
    """
    Extracts the complete Application Fee section.

    This avoids the previous problem where regex stopped at
    words such as "For".
    """

    heading = find_section_heading(
        soup,
        [
            "Application Fee"
        ]
    )

    if not heading:
        return "Not found"

    nodes = collect_section_nodes(
        soup,
        heading
    )

    lines = []

    for node in nodes:

        if node.name in [
            "script",
            "style",
            "noscript",
            "iframe"
        ]:
            continue

        # Prefer individual list/table rows.
        if node.name in [
            "li",
            "tr",
            "p"
        ]:

            text = clean_text(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if not text:
                continue

            # Don't accidentally include unrelated sections.
            if text.lower().startswith(
                (
                    "total post",
                    "age limit",
                    "eligibility",
                    "how to fill",
                    "mode of selection"
                )
            ):
                continue

            if text not in lines:
                lines.append(text)

    # If the DOM did not give useful lines,
    # use section text as fallback.
    if not lines:

        text = section_text(
            soup,
            ["Application Fee"]
        )

        if text:
            return clean_text(text)

        return "Not found"

    # Remove obvious payment-mode text from the fee field
    # only if it is clearly separate.
    filtered = []

    for line in lines:

        if line.lower().startswith(
            "payment mode"
        ):
            continue

        if line.lower() in [
            "online",
            "offline"
        ]:
            continue

        filtered.append(line)

    if not filtered:
        return "Not found"

    return "\n".join(filtered)


# ============================================================
# TOTAL VACANCIES
# ============================================================

def extract_total_vacancies(soup):
    """
    Extracts the value immediately associated with
    the "Total Post" section.
    """

    heading = find_section_heading(
        soup,
        [
            "Total Post",
            "Total Posts"
        ]
    )

    if not heading:
        return "Not found"

    # First inspect the immediate following elements.
    for element in heading.find_all_next():

        if element.name in [
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6"
        ]:
            break

        text = clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        # Typical values:
        # 2536 Posts
        # 1700 Posts
        # 2482 Posts
        #
        # Stop at the first sensible post count.
        match = re.search(
            r"\b([\d,]+)\s*(?:Posts?|Vacancies?)\b",
            text,
            re.IGNORECASE
        )

        if match:
            return (
                match.group(1)
                + " Posts"
            )

    # Fallback: inspect text immediately after heading.
    parent = heading.parent

    if parent:

        text = clean_text(
            parent.get_text(
                " ",
                strip=True
            )
        )

        match = re.search(
            r"\b([\d,]+)\s*(?:Posts?|Vacancies?)\b",
            text,
            re.IGNORECASE
        )

        if match:
            return (
                match.group(1)
                + " Posts"
            )

    return "Not found"


# ============================================================
# ORGANIZATION
# ============================================================

def extract_organization(soup):
    """
    Extracts organization from the introductory paragraph.

    Example:

    Staff Selection Commission (SSC) has released a Notification...
    """

    # First inspect paragraphs.
    for paragraph in soup.find_all("p"):

        text = clean_text(
            paragraph.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        if "has released" in text.lower():

            match = re.search(
                r"^(.+?)\s+has\s+released\b",
                text,
                re.IGNORECASE
            )

            if match:

                organization = clean_text(
                    match.group(1)
                )

                # Remove common unwanted prefixes.
                organization = re.sub(
                    r"^(Post Date.*?|SarkariResult\.com\.cm)\s+",
                    "",
                    organization,
                    flags=re.IGNORECASE
                )

                if len(organization) > 2:
                    return organization

    # Fallback: use first relevant sentence from body text.
    body_text = clean_multiline_text(
        soup.get_text("\n", strip=True)
    )

    match = re.search(
        r"([A-Z][A-Za-z0-9 &(),./\-]{2,100})"
        r"\s+has\s+released\b",
        body_text,
        re.IGNORECASE
    )

    if match:
        return clean_text(
            match.group(1)
        )

    return "Not found"


# ============================================================
# ELIGIBILITY
# ============================================================

def extract_eligibility(soup):
    """
    Extracts eligibility primarily from tables.

    This is much more reliable than regex against the complete
    flattened webpage text.
    """

    heading = find_section_heading(
        soup,
        [
            "Eligibility Criteria",
            "Eligibility"
        ]
    )

    # --------------------------------------------------------
    # Strategy 1: Find table after eligibility heading
    # --------------------------------------------------------

    if heading:

        nodes = collect_section_nodes(
            soup,
            heading
        )

        for node in nodes:

            if node.name != "table":
                continue

            rows = node.find_all("tr")

            if not rows:
                continue

            result = []

            for row in rows:

                cells = row.find_all(
                    ["th", "td"]
                )

                values = [
                    clean_text(
                        cell.get_text(
                            " ",
                            strip=True
                        )
                    )
                    for cell in cells
                ]

                values = [
                    value
                    for value in values
                    if value
                ]

                if not values:
                    continue

                result.append(values)

            if result:

                # Format two-column eligibility table.
                formatted = []

                for row in result:

                    if len(row) >= 2:

                        post_name = row[0]
                        criteria = " ".join(
                            row[1:]
                        )

                        # Skip header.
                        if (
                            post_name.lower()
                            in [
                                "post name",
                                "post",
                                "name"
                            ]
                            and
                            "eligibility"
                            in criteria.lower()
                        ):
                            continue

                        formatted.append(
                            f"{post_name}: {criteria}"
                        )

                    else:

                        value = row[0]

                        if value.lower() not in [
                            "post name",
                            "eligibility criteria"
                        ]:
                            formatted.append(value)

                if formatted:
                    return "\n".join(formatted)

    # --------------------------------------------------------
    # Strategy 2: Look for any table containing eligibility
    # --------------------------------------------------------

    for table in soup.find_all("table"):

        table_text = clean_text(
            table.get_text(
                " ",
                strip=True
            )
        )

        if "eligibility criteria" not in table_text.lower():
            continue

        rows = table.find_all("tr")

        formatted = []

        for row in rows:

            cells = row.find_all(
                ["th", "td"]
            )

            values = [
                clean_text(
                    cell.get_text(
                        " ",
                        strip=True
                    )
                )
                for cell in cells
            ]

            values = [
                value
                for value in values
                if value
            ]

            if len(values) >= 2:

                if (
                    values[0].lower()
                    == "post name"
                ):
                    continue

                formatted.append(
                    f"{values[0]}: "
                    f"{' '.join(values[1:])}"
                )

        if formatted:
            return "\n".join(formatted)

    # --------------------------------------------------------
    # Strategy 3: Text fallback
    # --------------------------------------------------------

    if heading:

        text = section_text(
            soup,
            [
                "Eligibility Criteria",
                "Eligibility"
            ]
        )

        if text:

            # Remove common unrelated recommendation.
            text = re.split(
                r"You May Also Check\s*:",
                text,
                flags=re.IGNORECASE
            )[0]

            # Remove How To Fill if it was included.
            text = re.split(
                r"How To Fill",
                text,
                flags=re.IGNORECASE
            )[0]

            text = clean_text(text)

            if text:
                return text

    return "Not found"


# ============================================================
# USEFUL LINKS
# ============================================================

def extract_useful_links(soup, base_url):
    """
    Extracts links from the "SOME USEFUL IMPORTANT LINKS"
    section.

    This prevents "Online Correction Link" from being
    incorrectly selected as "Apply Online".
    """

    result = {
        "apply_link": None,
        "notification_link": None,
        "official_website": None,
        "correction_link": None
    }

    heading = find_section_heading(
        soup,
        [
            "SOME USEFUL IMPORTANT LINKS",
            "USEFUL IMPORTANT LINKS",
            "IMPORTANT LINKS"
        ]
    )

    if not heading:
        return result

    nodes = collect_section_nodes(
        soup,
        heading
    )

    current_label = ""

    for node in nodes:

        if node.name in [
            "script",
            "style",
            "noscript",
            "iframe"
        ]:
            continue

        # ----------------------------------------------------
        # Detect labels
        # ----------------------------------------------------

        if node.name in [
            "h3",
            "h4",
            "h5",
            "h6",
            "strong",
            "b",
            "p",
            "div",
            "td"
        ]:

            text = clean_text(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            lower = text.lower()

            if "online correction" in lower:
                current_label = "correction"

            elif (
                "apply online" in lower
                or "online form" in lower
                or "registration" in lower
            ):
                current_label = "apply"

            elif (
                "official notification" in lower
                or "download notification" in lower
                or "notification" == lower
            ):
                current_label = "notification"

            elif (
                "official website" in lower
                or "ssc official website" in lower
            ):
                current_label = "website"

        # ----------------------------------------------------
        # Detect links
        # ----------------------------------------------------

        for anchor in node.find_all(
            "a",
            href=True
        ):

            href = anchor.get("href")

            if not href:
                continue

            url = absolute_url(
                base_url,
                href
            )

            if not url:
                continue

            anchor_text = clean_text(
                anchor.get_text(
                    " ",
                    strip=True
                )
            ).lower()

            # Determine link type using both the current label
            # and anchor text.
            if current_label == "correction":

                if not result["correction_link"]:
                    result["correction_link"] = url

            elif current_label == "apply":

                if not result["apply_link"]:
                    result["apply_link"] = url

            elif current_label == "notification":

                if not result["notification_link"]:
                    result["notification_link"] = url

            elif current_label == "website":

                if not result["official_website"]:
                    result["official_website"] = url

            # Additional fallback based on anchor itself.
            elif (
                "apply online" in anchor_text
                or "registration" in anchor_text
            ):

                if (
                    "correction"
                    not in anchor_text
                    and not result["apply_link"]
                ):
                    result["apply_link"] = url

            elif (
                "official notification"
                in anchor_text
                or "download notification"
                in anchor_text
            ):

                if not result["notification_link"]:
                    result["notification_link"] = url

    # --------------------------------------------------------
    # Final fallback: inspect all links inside section
    # --------------------------------------------------------

    anchors = []

    for node in nodes:

        for anchor in node.find_all(
            "a",
            href=True
        ):

            url = absolute_url(
                base_url,
                anchor.get("href")
            )

            text = clean_text(
                anchor.get_text(
                    " ",
                    strip=True
                )
            ).lower()

            if url:
                anchors.append(
                    (
                        text,
                        url
                    )
                )

    for text, url in anchors:

        if (
            not result["apply_link"]
            and (
                "apply online" in text
                or "registration" in text
                or "login" in text
            )
            and "correction" not in text
        ):
            result["apply_link"] = url

        if (
            not result["notification_link"]
            and (
                "official notification"
                in text
                or "download official"
                in text
            )
        ):
            result["notification_link"] = url

        if (
            not result["correction_link"]
            and "correction" in text
        ):
            result["correction_link"] = url

        if (
            not result["official_website"]
            and "official website" in text
        ):
            result["official_website"] = url

    return result


# ============================================================
# JOB TITLE
# ============================================================

def extract_job_title(soup, fallback_title):
    """
    Extracts the actual H1 job title.
    """

    h1 = soup.find("h1")

    if h1:

        title = clean_text(
            h1.get_text(
                " ",
                strip=True
            )
        )

        if title:
            return title

    title_tag = soup.find("title")

    if title_tag:

        title = clean_text(
            title_tag.get_text(
                " ",
                strip=True
            )
        )

        # Remove common website suffix.
        title = re.sub(
            r"\s*[-|]\s*Sarkari Result.*$",
            "",
            title,
            flags=re.IGNORECASE
        )

        if title:
            return title

    return clean_text(fallback_title)


# ============================================================
# JOB PAGE EXTRACTION
# ============================================================

def extract_job_page(html, url, fallback_title):
    """
    Parses an individual job page while preserving the DOM
    structure for accurate extraction.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    title = extract_job_title(
        soup,
        fallback_title
    )

    organization = extract_organization(
        soup
    )

    dates = extract_dates(
        soup
    )

    fee = extract_application_fee(
        soup
    )

    vacancies = extract_total_vacancies(
        soup
    )

    eligibility = extract_eligibility(
        soup
    )

    useful_links = extract_useful_links(
        soup,
        url
    )

    return {
        "title": title,
        "url": url,

        "organization": organization,

        "vacancies": vacancies,

        "eligibility": eligibility,

        "fee": fee,

        "start_date": dates["start_date"],
        "last_date": dates["last_date"],
        "fee_payment_date": dates["fee_payment_date"],
        "correction_date": dates["correction_date"],
        "exam_date": dates["exam_date"],
        "admit_card": dates["admit_card"],
        "result_date": dates["result_date"],

        "apply_link": useful_links["apply_link"],
        "notification_link": useful_links["notification_link"],
        "correction_link": useful_links["correction_link"],
        "official_website": useful_links["official_website"],
    }


# ============================================================
# EMAIL
# ============================================================

def format_value(value):
    if not value:
        return "Not found"

    return value.strip()


def create_email(job):
    """
    Creates a clean notification email.

    The previous FULL DETAILS dump has intentionally been
    removed because the extracted page text was noisy and
    duplicated information.
    """

    subject = (
        f"New Job Alert: {job['title']}"
    )

    body = f"""
New job notification

Job Name:
{format_value(job['title'])}

Organization:
{format_value(job['organization'])}

Total Vacancies:
{format_value(job['vacancies'])}

Eligibility Criteria:
{format_value(job['eligibility'])}

Application Fee:
{format_value(job['fee'])}

Application Start Date:
{format_value(job['start_date'])}

Last Date:
{format_value(job['last_date'])}

Last Date For Fee Payment:
{format_value(job['fee_payment_date'])}

Correction Date:
{format_value(job['correction_date'])}

Exam Date:
{format_value(job['exam_date'])}

Admit Card:
{format_value(job['admit_card'])}

Result Date:
{format_value(job['result_date'])}

Apply Online:
{format_value(job['apply_link'])}

Official Notification:
{format_value(job['notification_link'])}

Online Correction:
{format_value(job['correction_link'])}

Official Website:
{format_value(job['official_website'])}

Official Notification / Job Page:
{format_value(job['url'])}

--------------------------------------------------
This notification was automatically generated by your
Sarkari Result job monitoring system.
"""

    message = EmailMessage()

    message["Subject"] = subject
    message["From"] = EMAIL_ADDRESS
    message["To"] = TO_EMAIL

    message.set_content(
        body.strip()
    )

    return message


# ============================================================
# EMAIL SENDING
# ============================================================

def send_email(message):
    print("Connecting to Gmail...")

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as server:

        server.login(
            EMAIL_ADDRESS,
            EMAIL_APP_PASSWORD
        )

        server.send_message(
            message
        )

    print("Email sent successfully.")


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 70)
    print("SARKARI RESULT JOB MONITOR")
    print("=" * 70)

    session = get_session()

    conn = init_database()

    try:

        print("\nChecking:")
        print(LATEST_JOBS_URL)

        latest_html = fetch_page(
            session,
            LATEST_JOBS_URL
        )

        jobs = get_job_links(
            latest_html,
            LATEST_JOBS_URL
        )

        print(
            f"\nFound {len(jobs)} possible job posts."
        )

        print("\nDetected jobs:")

        for title, url in jobs[:20]:

            print(f"  - {title}")
            print(f"    {url}")

        new_jobs = 0

        for index, (title, url) in enumerate(
            jobs,
            start=1
        ):

            print("\n" + "-" * 70)

            print(
                f"[{index}/{len(jobs)}] {title}"
            )

            print(url)

            # ------------------------------------------------
            # Existing job
            # ------------------------------------------------

            if job_exists(
                conn,
                url
            ):

                print(
                    "Already processed."
                )

                continue

            print(
                "NEW JOB!"
            )

            # ------------------------------------------------
            # Fetch job page
            # ------------------------------------------------

            try:

                job_html = fetch_page(
                    session,
                    url
                )

                job = extract_job_page(
                    job_html,
                    url,
                    title
                )

                # ------------------------------------------------
                # Display extracted information in GitHub log
                # ------------------------------------------------

                print(
                    f"Job: {job['title']}"
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
                    f"Eligibility: "
                    f"{job['eligibility']}"
                )

                print(
                    f"Fee: "
                    f"{job['fee']}"
                )

                print(
                    f"Start date: "
                    f"{job['start_date']}"
                )

                print(
                    f"Last date: "
                    f"{job['last_date']}"
                )

                print(
                    f"Fee payment date: "
                    f"{job['fee_payment_date']}"
                )

                print(
                    f"Correction date: "
                    f"{job['correction_date']}"
                )

                print(
                    f"Exam date: "
                    f"{job['exam_date']}"
                )

                print(
                    f"Apply link: "
                    f"{job['apply_link']}"
                )

                print(
                    f"Notification: "
                    f"{job['notification_link']}"
                )

                print(
                    f"Official website: "
                    f"{job['official_website']}"
                )

                # ------------------------------------------------
                # Create and send email
                # ------------------------------------------------

                email = create_email(
                    job
                )

                send_email(
                    email
                )

                # ------------------------------------------------
                # Save ONLY after successful email
                # ------------------------------------------------

                save_job(
                    conn,
                    job["title"],
                    url
                )

                mark_email_sent(
                    conn,
                    url
                )

                new_jobs += 1

                print(
                    "Job saved successfully."
                )

            except Exception as error:

                print(
                    f"ERROR processing job: "
                    f"{error}"
                )

                # Continue processing other jobs.
                continue

    finally:

        conn.close()

    print("\n" + "=" * 70)

    print(
        f"New jobs emailed: {new_jobs}"
    )

    print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
