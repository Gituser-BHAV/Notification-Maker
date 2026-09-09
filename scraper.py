import os
import re
import sqlite3
import smtplib
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


LATEST_JOBS_URL = "https://sarkariresult.com.cm/latest-jobs/"
DB_FILE = "jobs.db"

EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ["TO_EMAIL"]


def get_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        )
    })

    return session


def fetch_page(session, url):
    response = session.get(url, timeout=30)
    response.raise_for_status()

    return response.text


def clean_text(text):
    return re.sub(r"\s+", " ", text).strip()


def absolute_url(base_url, href):
    if not href:
        return None

    return urljoin(base_url, href)


# -------------------------------------------------
# DATABASE
# -------------------------------------------------

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
        "SELECT 1 FROM jobs WHERE url = ? LIMIT 1",
        (url,)
    ).fetchone()

    return result is not None


def save_job(conn, title, url):
    conn.execute("""
        INSERT OR IGNORE INTO jobs
        (url, title, first_seen, email_sent)
        VALUES (?, ?, ?, 0)
    """, (
        url,
        title,
        datetime.utcnow().isoformat()
    ))

    conn.commit()


def mark_email_sent(conn, url):
    conn.execute(
        "UPDATE jobs SET email_sent = 1 WHERE url = ?",
        (url,)
    )

    conn.commit()


# -------------------------------------------------
# FIND JOBS
# -------------------------------------------------

def get_job_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")

    links = []

    # These are the preferred selectors.
    selectors = [
        "article h2 a[href]",
        "article h3 a[href]",
        "article .entry-title a[href]",
        "h2.entry-title a[href]",
        "h3.entry-title a[href]",
        ".entry-title a[href]",
        ".post-title a[href]",
    ]

    for selector in selectors:
        for a in soup.select(selector):

            title = clean_text(
                a.get_text(" ", strip=True)
            )

            href = a.get("href")

            if not title or not href:
                continue

            url = absolute_url(base_url, href)

            if url:
                links.append((title, url))

    # Remove duplicates.
    unique = []
    seen = set()

    for title, url in links:

        if url in seen:
            continue

        seen.add(url)
        unique.append((title, url))

    return unique


# -------------------------------------------------
# JOB PAGE
# -------------------------------------------------

def extract_job_page(html, url, fallback_title):
    soup = BeautifulSoup(html, "html.parser")

    # Remove unnecessary elements.
    for tag in soup([
        "script",
        "style",
        "noscript",
        "iframe",
        "nav",
        "footer"
    ]):
        tag.decompose()

    title_element = soup.find("h1")

    if title_element:
        title = clean_text(
            title_element.get_text(" ", strip=True)
        )
    else:
        title = fallback_title

    # Prefer article content.
    content = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.body
    )

    if content:
        text = content.get_text("\n", strip=True)
    else:
        text = ""

    # Clean excessive blank lines.
    lines = []

    for line in text.splitlines():
        line = clean_text(line)

        if line:
            lines.append(line)

    text = "\n".join(lines)

    return {
        "title": title,
        "url": url,
        "text": text[:30000]
    }


# -------------------------------------------------
# INFORMATION EXTRACTION
# -------------------------------------------------

def find_field(text, patterns):
    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            value = clean_text(match.group(1))

            if value:
                return value

    return "Not found"


def extract_information(text):

    vacancies = find_field(text, [
        r"Total\s+(?:Number\s+of\s+)?Vacancies?\s*[:\-]?\s*([^\n]+)",
        r"Total\s+Posts?\s*[:\-]?\s*([^\n]+)",
        r"Total\s+Post\s*[:\-]?\s*([^\n]+)",
        r"No\.?\s+of\s+Posts?\s*[:\-]?\s*([^\n]+)",
    ])

    fee = find_field(text, [
        r"Application\s+Fee\s*[:\-]?\s*([^\n]+)",
        r"Application\s+Fees?\s*[:\-]?\s*([^\n]+)",
        r"Exam\s+Fee\s*[:\-]?\s*([^\n]+)",
    ])

    last_date = find_field(text, [
        r"Last\s+Date\s+(?:to\s+Apply)?\s*[:\-]?\s*([^\n]+)",
        r"Closing\s+Date\s*[:\-]?\s*([^\n]+)",
        r"Apply\s+Online\s+Last\s+Date\s*[:\-]?\s*([^\n]+)",
    ])

    start_date = find_field(text, [
        r"Application\s+Start\s+Date\s*[:\-]?\s*([^\n]+)",
        r"Start\s+Date\s*[:\-]?\s*([^\n]+)",
        r"Online\s+Form\s+Start\s+Date\s*[:\-]?\s*([^\n]+)",
    ])

    return {
        "vacancies": vacancies,
        "fee": fee,
        "last_date": last_date,
        "start_date": start_date,
    }


# -------------------------------------------------
# EMAIL
# -------------------------------------------------

def create_email(job, information):

    subject = f"New Job Alert: {job['title']}"

    body = f"""
NEW JOB NOTIFICATION
====================

Job:
{job['title']}

Total Vacancies:
{information['vacancies']}

Application Fee:
{information['fee']}

Application Start Date:
{information['start_date']}

Last Date:
{information['last_date']}


JOB / NOTIFICATION PAGE
=======================

{job['url']}


ELIGIBILITY / NOTIFICATION DETAILS
===================================

{job['text']}


-------------------------------------------------
This notification was automatically generated.
"""

    message = EmailMessage()

    message["Subject"] = subject
    message["From"] = EMAIL_ADDRESS
    message["To"] = TO_EMAIL

    message.set_content(body)

    return message


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

        server.send_message(message)

    print("Email sent successfully.")


# -------------------------------------------------
# MAIN
# -------------------------------------------------

def main():

    print("=" * 60)
    print("SARKARI RESULT JOB MONITOR")
    print("=" * 60)

    session = get_session()

    conn = init_database()

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

    print(f"\nFound {len(jobs)} possible job posts.")

    if len(jobs) == 0:
        print("\nDEBUG: No jobs found.")
        print("Page title:", BeautifulSoup(latest_html, "html.parser").title)
        print("\nFirst 5000 characters of HTML:")
        print(latest_html[:5000])
    new_jobs = 0

    for index, (title, url) in enumerate(jobs, start=1):

        print(
            f"\n[{index}/{len(jobs)}] {title}"
        )

        if job_exists(conn, url):

            print("Already processed.")

            continue

        print("NEW JOB!")

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

            information = extract_information(
                job["text"]
            )

            print(
                f"Vacancies: {information['vacancies']}"
            )

            print(
                f"Last date: {information['last_date']}"
            )

            # Save BEFORE sending.
            # This prevents the same URL from being
            # processed repeatedly.
            save_job(
                conn,
                job["title"],
                url
            )

            email = create_email(
                job,
                information
            )

            send_email(email)

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

    print("\n" + "=" * 60)
    print(f"New jobs emailed: {new_jobs}")
    print("=" * 60)


if __name__ == "__main__":
    main()
