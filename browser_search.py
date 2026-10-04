"""Bing discovery extraction for the installed Edge reader, never article evidence.

The pet and each worker reuse their respective browser sessions. The optional
standalone entry point owns a temporary visible Edge; no external LLM or bypass.
"""
import asyncio
import base64
import json
import sys
from urllib.parse import parse_qs, urlsplit


def destination(value):
    """Unwrap a URL actually supplied by Bing, never manufacture an article URL."""
    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.hostname in {"www.bing.com", "bing.com"} and parsed.path == "/ck/a":
        token = parse_qs(parsed.query).get("u", [""])[0]
        if token.startswith("a1"):
            try:
                value = base64.urlsafe_b64decode(token[2:] + "=" * (-len(token[2:]) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                return ""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if parsed.hostname in {"www.bing.com", "bing.com"}:
        return ""
    return value


def results(observation):
    text = observation.get("text", "")
    if any(s in text.lower() for s in ("verify you are human", "solve the challenge", "unusual traffic")):
        raise ValueError("Search was blocked by verification. No bypass or fallback attempted.")
    rows, seen = [], set()
    for link in observation.get("links", []):
        url = destination(link.get("url", ""))
        if url and url not in seen:
            seen.add(url)
            rows.append({"title": link.get("title", "")[:500], "url": url,
                         "source": urlsplit(url).hostname})
        if len(rows) == 8:
            break
    return rows


async def observe_results(page):
    # Bing sometimes returns public result cards in the DOM while hydration/visibility
    # is incomplete in our read-only browser. These are discovery URLs, NOT article
    # evidence. Restrict extraction to ordinary result headings; don't scrape nav/ads.
    return await page.evaluate("""() => ({
        title: document.title, text: document.body.innerText,
        links: Array.from(document.querySelectorAll('#b_results .b_algo h2 a[href]'))
          .slice(0, 16).map(a => ({title:a.textContent.trim().slice(0,500),url:a.href}))
    })""")


async def main(query):
    from shared_browser import SharedBrowser
    browser = SharedBrowser()
    try:
        return await browser.command({"action": "discover", "query": query})
    finally:
        await browser.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        print(json.dumps(asyncio.run(main(sys.argv[1])), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc).split("\n")[0]}))
        raise SystemExit(1)
