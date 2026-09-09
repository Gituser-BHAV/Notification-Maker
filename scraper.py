import os
import re
import sqlite3
import smtplib
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# =================================================
# CONFIGURATION
# =================================================

LATEST_JOBS_URL = "https://sarkariresult.com.cm/latest-jobs/"
DB_FILE = "jobs.db"

EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ["TO_EMAIL"]

SITE_DOMAIN = "https://sarkariresult.com.cm/"


# =================================================
# HTTP
# =================================================

def get_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })

    return session


def fetch_page(session, url):
    print(f"Fetching: {url}")

    response = session.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    return response.text


# =================================================
# TEXT / URL HELPERS
# =================================================

def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def absolute_url(base_url, href):
    if not href:
        return None

    return urljoin(base_url, href)


# =================================================
# DATABASE
# =================================================

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


# =================================================
# FIND JOB POSTS FROM LATEST JOBS PAGE
# =================================================

def get_job_links(html, base_url):

    soup = BeautifulSoup(html, "html.parser")

    links = []
    seen = set()

    # ---------------------------------------------
    # Find "All Latest Jobs"
    # ---------------------------------------------

    heading = None

    for tag in soup.find_all(["h2", "h3"]):

        text = clean_text(
            tag.get_text(" ", strip=True)
        )

        if text.lower() == "all latest jobs":
            heading = tag
            break

    if not heading:

        print("WARNING: Could not find 'All Latest Jobs' heading.")

        return []


    # ---------------------------------------------
    # Find links after the heading
    # ---------------------------------------------

    for element in heading.find_all_next("a", href=True):

        title = clean_text(
            element.get_text(" ", strip=True)
        )

        href = element.get("href")

        if not title or not href:
            continue

        url = absolute_url(
            base_url,
            href
        )

        if not url:
            continue

        # Only SarkariResult pages
        if not url.startswith(SITE_DOMAIN):
            continue

        # Ignore site/navigation links
        ignored_parts = [
            "/contact",
            "/privacy",
            "/disclaimer",
            "/author/",
            "/category/",
            "/feed/",
        ]

        if any(
            part in url.lower()
            for part in ignored_parts
        ):
            continue

        # Ignore pagination links
        if "/page/" in url.lower():
            continue

        # Ignore the latest-jobs page itself
        if url.rstrip("/") == base_url.rstrip("/"):
            continue

        # Ignore duplicate URLs
        if url in seen:
            continue

        # Job titles on this page are generally substantial.
        if len(title) < 10:
            continue

        seen.add(url)

        links.append(
            (
                title,
                url
            )
        )

    return links


# =================================================
# EXTRACT LINK FROM JOB PAGE
# =================================================

def find_labeled_link(soup, labels):

    """
    Find a link associated with a heading such as:

        Apply Online
        Check Official Notification

    The website generally places the actual link
    immediately after these headings.
    """

    for heading in soup.find_all(
        ["h3", "h4", "h5", "h6"]
    ):

        heading_text = clean_text(
            heading.get_text(" ", strip=True)
        ).lower()

        matched = False

        for label in labels:

            if label.lower() in heading_text:
                matched = True
                break

        if not matched:
            continue

        # Search following elements for the first link.
        for element in heading.find_all_next("a", href=True):

            href = element.get("href")

            if href:

                return absolute_url(
                    str(soup),
                    href
                )

    return None


def extract_useful_links(soup, base_url):

    apply_link = None
    notification_link = None

    # ---------------------------------------------
    # Look for headings
    # ---------------------------------------------

    for heading in soup.find_all(
        ["h3", "h4", "h5", "h6"]
    ):

        heading_text = clean_text(
            heading.get_text(" ", strip=True)
        ).lower()

        # -----------------------------------------
        # APPLY ONLINE
        # -----------------------------------------

        if (
            "apply online" in heading_text
            or "apply online link" in heading_text
        ):

            for element in heading.find_all_next(
                "a",
                href=True
            ):

                href = element.get("href")

                if href:

                    apply_link = urljoin(
                        base_url,
                        href
                    )

                    break


        # -----------------------------------------
        # OFFICIAL NOTIFICATION
        # -----------------------------------------

        if (
            "official notification" in heading_text
            or "check official notification"
            in heading_text
        ):

            for element in heading.find_all_next(
                "a",
                href=True
            ):

                href = element.get("href")

                if href:

                    notification_link = urljoin(
                        base_url,
                        href
                    )

                    break


    # ---------------------------------------------
    # SECOND METHOD:
    # Search all links by visible text
    # ---------------------------------------------

    if not apply_link:

        for a in soup.find_all(
            "a",
            href=True
        ):

            text = clean_text(
                a.get_text(" ", strip=True)
            ).lower()

            if (
                text == "click here"
                or "apply online" in text
            ):

                href = a.get("href")

                if href:

                    apply_link = urljoin(
                        base_url,
                        href
                    )

                    break


    if not notification_link:

        for a in soup.find_all(
            "a",
            href=True
        ):

            text = clean_text(
                a.get_text(" ", strip=True)
            ).lower()

            if (
                "official notification"
                in text
            ):

                href = a.get("href")

                if href:

                    notification_link = urljoin(
                        base_url,
                        href
                    )

                    break


    return {
        "apply_link": apply_link,
        "notification_link": notification_link
    }


# =================================================
# EXTRACT JOB PAGE
# =================================================

def extract_job_page(
    html,
    url,
    fallback_title
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # ---------------------------------------------
    # Extract title
    # ---------------------------------------------

    title_element = soup.find("h1")

    if title_element:

        title = clean_text(
            title_element.get_text(
                " ",
                strip=True
            )
        )

    else:

        title = fallback_title


    # ---------------------------------------------
    # Extract useful links BEFORE removing tags
    # ---------------------------------------------

    useful_links = extract_useful_links(
        soup,
        url
    )


    # ---------------------------------------------
    # Remove unnecessary elements
    # ---------------------------------------------

    for tag in soup([
        "script",
        "style",
        "noscript",
        "iframe",
        "nav",
        "footer"
    ]):

        tag.decompose()


    # ---------------------------------------------
    # Find main article content
    # ---------------------------------------------

    content = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.body
    )


    if content:

        text = content.get_text(
            "\n",
            strip=True
        )

    else:

        text = ""


    # ---------------------------------------------
    # Clean text
    # ---------------------------------------------

    lines = []

    for line in text.splitlines():

        line = clean_text(line)

        if line:

            lines.append(line)


    text = "\n".join(lines)


    return {
        "title": title,
        "url": url,
        "text": text[:30000],
        "apply_link": useful_links["apply_link"],
        "notification_link": useful_links["notification_link"]
    }


# =================================================
# INFORMATION EXTRACTION
# =================================================

def find_field(text, patterns):

    for pattern in patterns:

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


def extract_information(text):

    # ---------------------------------------------
    # Vacancies
    # ---------------------------------------------

    vacancies = find_field(
        text,
        [
            r"Total\s+(?:Number\s+of\s+)?Vacancies?\s*[:\-]?\s*([^\n]+)",

            r"Total\s+Posts?\s*[:\-]?\s*([^\n]+)",

            r"Total\s+Post\s*[:\-]?\s*([^\n]+)",

            r"No\.?\s+of\s+Posts?\s*[:\-]?\s*([^\n]+)",
        ]
    )


    # ---------------------------------------------
    # Application Fee
    # ---------------------------------------------

    fee = find_field(
        text,
        [
            r"Application\s+Fee\s*[:\-]?\s*([^\n]+)",

            r"Application\s+Fees?\s*[:\-]?\s*([^\n]+)",

            r"Exam\s+Fee\s*[:\-]?\s*([^\n]+)",
        ]
    )


    # ---------------------------------------------
    # Last Date
    # ---------------------------------------------

    last_date = find_field(
        text,
        [
            r"Online\s+Apply\s+Last\s+Date\s*[:\-]?\s*([^\n]+)",

            r"Apply\s+Online\s+Last\s+Date\s*[:\-]?\s*([^\n]+)",

            r"Last\s+Date\s+(?:to\s+Apply)?\s*[:\-]?\s*([^\n]+)",

            r"Closing\s+Date\s*[:\-]?\s*([^\n]+)",
        ]
    )


    # ---------------------------------------------
    # Start Date
    # ---------------------------------------------

    start_date = find_field(
        text,
        [
            r"Online\s+Apply\s+Start\s+Date\s*[:\-]?\s*([^\n]+)",

            r"Apply\s+Online\s+Start\s+Date\s*[:\-]?\s*([^\n]+)",

            r"Application\s+Start\s+Date\s*[:\-]?\s*([^\n]+)",

            r"Start\s+Date\s*[:\-]?\s*([^\n]+)",
        ]
    )


    # ---------------------------------------------
    # Organization
    # ---------------------------------------------

    organization = "Not found"

    organization_patterns = [
        r"^([A-Z][A-Za-z0-9 &(),.\-]+)\s*,?\s*has released",
        r"^([A-Z][A-Za-z0-9 &(),.\-]+)\s+has released",
    ]

    for pattern in organization_patterns:

        match = re.search(
            pattern,
            text,
            re.MULTILINE
        )

        if match:

            organization = clean_text(
                match.group(1)
            )

            break


    # ---------------------------------------------
    # Eligibility
    # ---------------------------------------------

    eligibility = "Not found"

    eligibility_patterns = [

        r"Post Name\s*\|\s*Eligibility Criteria\s*\n(.+?)(?=\n\n|How To Fill)",

        r"Eligibility Criteria\s*[:\-]?\s*(.+?)(?=\n\n|How To Fill)",

        r"Educational Qualification\s*[:\-]?\s*(.+?)(?=\n\n|Age Limit)",
    ]

    for pattern in eligibility_patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE | re.DOTALL
        )

        if match:

            eligibility = clean_text(
                match.group(1)
            )

            break


    return {
        "organization": organization,
        "vacancies": vacancies,
        "fee": fee,
        "start_date": start_date,
        "last_date": last_date,
        "eligibility": eligibility
    }


# =================================================
# EMAIL
# =================================================

def create_email(
    job,
    information
):

    subject = (
        f"New Job Alert: "
        f"{job['title']}"
    )


    body = f"""
NEW GOVERNMENT JOB NOTIFICATION
================================

JOB
---
{job['title']}


ORGANIZATION
------------
{information['organization']}


TOTAL VACANCIES
---------------
{information['vacancies']}


ELIGIBILITY
-----------
{information['eligibility']}


APPLICATION FEE
---------------
{information['fee']}


APPLICATION START DATE
----------------------
{information['start_date']}


LAST DATE
---------
{information['last_date']}


APPLY ONLINE
------------
{job['apply_link'] or 'Not found'}


OFFICIAL NOTIFICATION
---------------------
{job['notification_link'] or 'Not found'}


SARKARI RESULT JOB PAGE
-----------------------
{job['url']}


FULL DETAILS
------------

{job['text']}


-----------------------------------------------
This notification was automatically generated.
"""


    message = EmailMessage()

    message["Subject"] = subject

    message["From"] = EMAIL_ADDRESS

    message["To"] = TO_EMAIL

    message.set_content(body)

    return message


# =================================================
# SEND EMAIL
# =================================================

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


# =================================================
# MAIN
# =================================================

def main():

    print("=" * 60)
    print("SARKARI RESULT JOB MONITOR")
    print("=" * 60)


    session = get_session()

    conn = init_database()


    # ---------------------------------------------
    # Get latest jobs page
    # ---------------------------------------------

    print("\nChecking:")
    print(LATEST_JOBS_URL)

    latest_html = fetch_page(
        session,
        LATEST_JOBS_URL
    )


    # ---------------------------------------------
    # Find job posts
    # ---------------------------------------------

    jobs = get_job_links(
        latest_html,
        LATEST_JOBS_URL
    )


    print(
        f"\nFound {len(jobs)} possible job posts."
    )


    # Show first 10 jobs for debugging
    print("\nDetected jobs:")

    for title, url in jobs[:10]:

        print(
            f"  - {title}"
        )

        print(
            f"    {url}"
        )


    # ---------------------------------------------
    # Process jobs
    # ---------------------------------------------

    new_jobs = 0


    for index, (title, url) in enumerate(
        jobs,
        start=1
    ):

        print(
            f"\n[{index}/{len(jobs)}] {title}"
        )


        # Already processed
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


        try:

            # -------------------------------------
            # Fetch job page
            # -------------------------------------

            job_html = fetch_page(
                session,
                url
            )


            # -------------------------------------
            # Extract job
            # -------------------------------------

            job = extract_job_page(
                job_html,
                url,
                title
            )


            # -------------------------------------
            # Extract information
            # -------------------------------------

            information = extract_information(
                job["text"]
            )


            print(
                f"Organization: "
                f"{information['organization']}"
            )

            print(
                f"Vacancies: "
                f"{information['vacancies']}"
            )

            print(
                f"Start date: "
                f"{information['start_date']}"
            )

            print(
                f"Last date: "
                f"{information['last_date']}"
            )

            print(
                f"Apply link: "
                f"{job['apply_link']}"
            )

            print(
                f"Notification: "
                f"{job['notification_link']}"
            )


            # -------------------------------------
            # Save job
            # -------------------------------------

            save_job(
                conn,
                job["title"],
                url
            )


            # -------------------------------------
            # Create email
            # -------------------------------------

            email = create_email(
                job,
                information
            )


            # -------------------------------------
            # Send email
            # -------------------------------------

            send_email(
                email
            )


            # -------------------------------------
            # Mark successful email
            # -------------------------------------

            mark_email_sent(
                conn,
                url
            )


            new_jobs += 1


        except Exception as error:

            print(
                f"ERROR: {error}"
            )


    conn.close()


    print(
        "\n" + "=" * 60
    )

    print(
        f"New jobs emailed: {new_jobs}"
    )

    print(
        "=" * 60
    )


# =================================================
# START
# =================================================

if __name__ == "__main__":

    main()
