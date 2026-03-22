import base64
import email.utils
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


logger = logging.getLogger(__name__)

# Hardcoded file locations
CLIENT_SECRETS_FILE = "credentials/client_secret.json"  # Download from Google Cloud
TOKEN_FILE = "credentials/token.json"  # Generated after OAuth flow

# Gmail API scopes needed
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class RawEmail:
    message_id: str
    sender: str
    subject: str
    date: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    links: Optional[list[str]] = None

    def __post_init__(self):
        if self.links is None:
            self.links = []


def _run_oauth_flow():
    """Run OAuth flow to get credentials from user authorization."""
    logger.info("Running OAuth flow to authorize Gmail access...")

    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise FileNotFoundError(
            f"Client secrets file not found at {CLIENT_SECRETS_FILE}. "
            "Please download it from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    # Save the credentials for future runs
    _save_credentials(creds)
    logger.info(f"Credentials saved to {TOKEN_FILE}")

    return creds


def _save_credentials(creds) -> None:
    """Save credentials to token file."""
    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
    }

    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)


def _load_credentials():
    """Load OAuth2 credentials from token file or run OAuth flow."""
    creds = None

    # Check if we have a saved token
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

            # Refresh if expired
            if creds and creds.expired and creds.refresh_token:
                logger.info("Refreshing expired token...")
                creds.refresh(Request())
                _save_credentials(creds)

        except Exception as e:
            logger.warning(f"Failed to load existing token: {e}")
            creds = None

    # If no valid credentials, run OAuth flow
    if not creds or not creds.valid:
        creds = _run_oauth_flow()

    return creds


def _build_query(hours: int) -> str:
    """Build Gmail search query for emails newer than specified hours."""
    return f"newer_than:{hours}h"


def _parse_date_to_iso(date_str: str) -> str:
    """Convert RFC 2822 date string to ISO format."""
    try:
        # Use email.utils.parsedate_to_datetime which handles various RFC 2822 formats
        # including timezone names like "(UTC)" at the end
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception as e:
        logger.warning(f"Failed to parse date '{date_str}': {e}")
        return date_str  # Return original if parsing fails


def _decode_base64url(data: str) -> str:
    """Decode base64url encoded string."""
    # Add padding if necessary
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding

    decoded = base64.urlsafe_b64decode(data)
    return decoded.decode("utf-8", errors="replace")


def _extract_body_parts(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract text and HTML body parts from message payload."""
    body_text = None
    body_html = None

    def extract_from_part(part: dict):
        nonlocal body_text, body_html

        mime_type = part.get("mimeType", "")

        # Check if this part has body data
        if "body" in part and "data" in part["body"]:
            data = part["body"]["data"]
            decoded = _decode_base64url(data)

            if mime_type == "text/plain" and not body_text:
                body_text = decoded
            elif mime_type == "text/html" and not body_html:
                body_html = decoded

        # Recursively check sub-parts
        if "parts" in part:
            for sub_part in part["parts"]:
                extract_from_part(sub_part)

    extract_from_part(payload)
    return body_text, body_html


def _extract_links_from_html(html: str) -> list[str]:
    """Extract all href links from HTML content."""
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    links = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href and href not in links:
            links.append(href)

    return links


def _fetch_message_with_retry(service, message_id: str, max_retries: int = 5) -> dict:
    """Fetch a single message with exponential backoff retry."""
    retry_count = 0
    base_delay = 1

    while retry_count < max_retries:
        try:
            return (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 429:  # Rate limited
                retry_count += 1
                if retry_count >= max_retries:
                    raise

                delay = base_delay * (2 ** (retry_count - 1))
                logger.warning(
                    f"Rate limited. Retrying in {delay}s... "
                    f"(attempt {retry_count}/{max_retries})"
                )
                time.sleep(delay)
            else:
                raise
        except Exception:
            raise

    raise Exception(f"Failed to fetch message {message_id} after {max_retries} retries")


def fetch_emails(hours: int = 24) -> list[RawEmail]:
    """
    Fetch raw job-alert emails from Gmail for the specified time range.

    Args:
        hours: Number of hours to look back for emails

    Returns:
        List of RawEmail objects containing email data

    Raises:
        FileNotFoundError: If token file is missing
        Exception: If any email has empty body (text and HTML both missing)
    """
    logger.info(f"Fetching emails from past {hours} hours...")

    # Load credentials
    creds = _load_credentials()
    service = build("gmail", "v1", credentials=creds)

    # Build query
    query = _build_query(hours)
    logger.info(f"Using query: {query}")

    # Get all message IDs with pagination
    message_ids = []
    page_token = None
    page_count = 0

    while True:
        page_count += 1
        logger.info(f"Fetching page {page_count} of message list...")

        try:
            results = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=100, pageToken=page_token)
                .execute()
            )

            messages = results.get("messages", [])
            message_ids.extend([msg["id"] for msg in messages])

            page_token = results.get("nextPageToken")
            if not page_token:
                break

        except HttpError as e:
            logger.error(f"Failed to list messages: {e}")
            raise

    total_messages = len(message_ids)
    logger.info(f"Found {total_messages} messages to process")

    # Fetch full content for each message
    raw_emails = []

    for idx, message_id in enumerate(message_ids, 1):
        logger.info(f"Processing message {idx}/{total_messages}: {message_id}")

        try:
            # Fetch message with retry
            msg = _fetch_message_with_retry(service, message_id)

            # Extract headers
            payload = msg.get("payload", {})
            headers = {
                h["name"].lower(): h["value"] for h in payload.get("headers", [])
            }

            sender = headers.get("from", "")
            subject = headers.get("subject", "")
            date_str = headers.get("date", "")
            date_iso = _parse_date_to_iso(date_str) if date_str else ""

            # Extract body parts
            body_text, body_html = _extract_body_parts(payload)

            # Fail if both bodies are empty
            if not body_text and not body_html:
                raise Exception(
                    f"Email {message_id} has empty body (no text or HTML). "
                    "Please investigate and fix."
                )

            # Extract links from HTML
            links = _extract_links_from_html(body_html or "")

            # Create RawEmail
            raw_email = RawEmail(
                message_id=message_id,
                sender=sender,
                subject=subject,
                date=date_iso,
                body_text=body_text,
                body_html=body_html,
                links=links,
            )

            raw_emails.append(raw_email)

        except Exception as e:
            logger.error(f"Failed to process message {message_id}: {e}")
            raise

    logger.info(
        f"Successfully fetched {len(raw_emails)} emails with "
        f"{sum(len(e.links) for e in raw_emails)} total links extracted"
    )

    return raw_emails
