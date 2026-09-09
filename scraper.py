import os
import re
import sqlite3
import smtplib
import hashlib
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ---------------- CONFIGURATION ----------------

LATEST_JOBS_URL = "https://sarkariresult.com.cm/latest-jobs/"
DB_FILE = "jobs.db"

# These are read from GitHub Actions secrets.
EMAIL_ADDRESS = os.environ["EMAIL_ADDRESS"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
TO_EMAIL = os.environ["TO_EMAIL"]

# ------------------------------------------------


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


def get_job_links(html, base_url):
    """
    Find links from the latest-jobs page.

    The selectors are intentionally broad because the exact HTML
    structure of the website may change.
    """

    soup = BeautifulSoup(html, "html.parser")

    links = []

    # Common WordPress / job-listing selectors.
    selectors = [
        "article a[href]",
        ".entry-title a[href]",
        ".post-title a[href]",
        "h2 a[href]",
        "h3 a[href]",
        ".job-listing a[href]",
        ".latest-jobs a[href]",
        ".job-list a[href]",
    ]

    for selector in selectors:
        for a in soup.select(selector):
            href = a.get("href")
            title = clean_text(a.get_text(" ", strip=True))

            if not href or not title:
                continue

            full_url = absolute_url(base_url, href)

            if full_url:
                links.append((title, full_url))

    # Fallback: collect links that look like job posts.
    if not links:
        for a in soup.find_all("a", href=True):
            title = clean_text(a.get_text(" ", strip=True))
            href = absolute_url(base_url, a["href"])

            if not title or not href:
                continue

            if any(word in title.lower() for word in [
                "recruitment", "vacancy", "job", "result",
                "admit card", "answer key", "online form",
                "notification"
            ]):
                links.append((title, href))

    # Remove duplicates while preserving order.
    unique = []
    seen = set()

    for title, url in links:
        if url not in seen:
            seen.add(url)
            unique.append((title, url))

    return unique


def extract_job_details(html, url, fallback_title):
    """
    Extract readable text from an individual job page.

    This is the first version. It does not yet use AI or OCR.
    """

    soup = BeautifulSoup(html, "html.parser")

    # Remove things that are not useful in the email.
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()

    title_tag = soup.find("h1")

    title = clean_text(
        title_tag.get_text(" ", strip=True)
        if title_tag
        else fallback_title
    )

    # Prefer the article content.
    content = (
        soup.select_one(".entry-content")
        or soup.select_one(".post-content")
        or soup.select_one("article")
        or soup.body
    )

    text = clean_text(
        content.get_text("\n", strip=True)
        if content
        else ""
    )

    # Limit extremely large pages.
    text = text[:20000]

    return {
        "title": title,
        "url": url,
        "text": text,
    }


def extract_field(text, patterns):
    """
    Try to find a field such as last date, fee, or vacancies.
    """

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_text(match.group(1))

    return "Not found"


def extract_details(text):
    return {
        "vacancies": extract_field(text, [
            r"(?:Total\s+)?(?:Number\s+of\s+)?Vacancies?\s*[:\-]?\s*([^\n]{1,100})",
            r"Total\s+Posts?\s*[:\-]?\s*([^\n]{1,100})",
            r"Total\s+Post\s*[:\-]?\s*([^\n]{1,100})",
        ]),

        "last_date": extract_field(text, [
            r"Last\s+Date\s*[:\-]?\s*([^\n]{1,100})",
            r"Last\s+Date\s+to\s+Apply\s*[:\-]?\s*([^\n]{1,100})",
            r"Closing\s+Date\s*[:\-]?\s*([^\n]{1,100})",
        ]),

        "fee": extract_field(text, [
            r"Application\s+Fee\s*[:\-]?\s*([^\n]{1,100})",
            r"Exam\s+Fee\s*[:\-]?\s*([^\n]{1,100})",
            r"Application\s+Fees?\s*[:\-]?\s*([^\n]{1,100})",
        ]),
    }


def init_database():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT,
            first_seen TEXT,
            email_sent INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    return conn


def is_new_job(conn, url):
    row = conn.execute(
        "SELECT id FROM jobs WHERE url = ?",
        (url,)
    ).fetchone()

    return row is None


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


def build_email(job, details):
    msg = EmailMessage()

    msg["Subject"] = f"New Job: {job['title']}"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = TO_EMAIL

    body = f"""
New job notification

Job Name:
{job['title']}

Total Vacancies:
{details['vacancies']}

Application Fee:
{details['fee']}

Last Date:
{details['last_date']}

Official Notification / Job Page:
{job['url']}

Eligibility Criteria:
The full extracted notification text is included below.

--------------------------------------------------

{job['text']}
"""

    msg.set_content(body)

    return msg


def send_email(msg):
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    session = get_session()
    conn = init_database()

    print("Checking latest jobs...")

    latest_html = fetch_page(session, LATEST_JOBS_URL)
    job_links = get_job_links(latest_html, LATEST_JOBS_URL)

    print(f"Found {len(job_links)} job links.")

    for title, url in job_links:

        if not is_new_job(conn, url):
            continue

        print(f"New job found: {title}")

        try:
            job_html = fetch_page(session, url)

            job = extract_job_details(
                job_html,
                url,
                title
            )

            details = extract_details(job["text"])

            save_job(conn, job["title"], url)

            msg = build_email(job, details)

            send_email(msg)

            mark_email_sent(conn, url)

            print("Email sent.")

        except Exception as e:
            print(f"Error processing {url}: {e}")

    conn.close()


if __name__ == "__main__":
    main()
