"""Public URL checks and Edge-backed rendered-page readers; no external model."""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse


def public_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 3000:
        raise ValueError("Use a public HTTPS URL shorter than 3000 characters.")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError("Public browsing supports HTTPS only, without credentials or custom ports.")
    try:
        addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Cannot resolve the public source hostname.") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Browsing cannot access localhost, private networks or reserved IP addresses.")
    return url


def page_problem(page):
    """Obvious failures are not article evidence; not a comprehensive challenge detector."""
    status = page.get("http_status")
    if isinstance(status, int) and status >= 400:
        return f"Edge received HTTP {status}. Page not saved as article evidence; no bypass attempted."
    text = page.get("text", page.get("body", ""))
    title = str(page.get("title", "")).lower()
    markers = ("verify you are human", "verify that you are human", "verify you're human",
               "solve the challenge", "unusual traffic", "access denied", "checking your browser",
               "err_blocked_by_client", "this site can’t be reached", "this site can't be reached")
    if (any(m in title for m in markers) or title.strip() == "just a moment..."
            or (len(text) < 2000 and any(m in text.lower() for m in markers))):
        return "Page requires attention or access was blocked. Not article evidence; no bypass attempted."
    return ""


def fetch_page(url: str) -> dict:
    # The pet/CLI and each research job reuse their own Browser directly. This
    # standalone convenience path owns a temporary Edge session and always closes it.
    public_url(url)
    from workspace_tools import Browser
    browser = Browser()
    try:
        return browser.call("read_url", url=url)
    finally:
        browser.close()


def search_web(query: str) -> dict:
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise ValueError("Use a nonempty search query shorter than 300 characters.")
    from workspace_tools import Browser
    browser = Browser()
    try:
        return browser.call("discover", query=query)
    finally:
        browser.close()
