"""Owned Edge over stdio: navigation/reading tools, no personal profile or TCP port.

Public page POST/OPTIONS and WSS are allowed. Website JS is not network-write-free.
Owner-approved 2026-10-03 helpers: scroll, follow a link, back/forward, tabs and typing a
query into a site's own search box. No login/password/other form, arbitrary click or script tool.
Private URLs/downloads/permissions stay blocked.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote_plus, urldefrag, urlsplit

import web_tools
from window_title import owned_titles


# Numbered links shared by browser_view and click_link, so "link 3" means the same thing in both.
LINKS_JS = """() => {
  const root = document.querySelector('main, article, [role=main]') || document.body;
  const bing = document.querySelectorAll('#b_results .b_algo h2 a');
  const candidates = bing.length ? bing : (root || document).querySelectorAll('a[href]');
  document.querySelectorAll('[data-dot-link]').forEach(a => a.removeAttribute('data-dot-link'));
  const out = [];
  for (const a of candidates) {
    if (out.length >= 40) break;
    if (!a.getClientRects().length || !a.href.startsWith('https://')) continue;
    const title = a.innerText.trim().replace(/\\s+/g, ' ').slice(0, 160);
    if (!title) continue;
    a.setAttribute('data-dot-link', String(out.length + 1));
    out.push({n: out.length + 1, title, url: a.href});
  }
  return out;
}"""

# Find a visible link anywhere on the page by its text (exact match preferred).
LINK_BY_TEXT_JS = """(wanted) => {
  wanted = wanted.trim().toLowerCase();
  document.querySelectorAll('[data-dot-link]').forEach(a => a.removeAttribute('data-dot-link'));
  const links = Array.from(document.querySelectorAll('a[href]')).filter(a =>
    a.getClientRects().length && a.href.startsWith('https://') && a.innerText.trim());
  const text = a => a.innerText.trim().replace(/\\s+/g, ' ').toLowerCase();
  const pick = links.find(a => text(a) === wanted) || links.find(a => text(a).includes(wanted));
  if (!pick) return null;
  pick.setAttribute('data-dot-link', 'pick');
  return {title: pick.innerText.trim().replace(/\\s+/g, ' ').slice(0, 160), url: pick.href};
}"""

# A site's own search box only. Skips anything in a form with a password field (sign-in).
SEARCH_BOX_JS = """() => {
  document.querySelectorAll('[data-dot-search]').forEach(e => e.removeAttribute('data-dot-search'));
  const selectors = ['input[type=search]', '[role=searchbox]', 'input[name=q]', 'input[name=query]',
    'input[name=search]', 'input[name=search_query]', 'textarea[name=q]', 'input[aria-label*="search" i]',
    'input[placeholder*="search" i]', 'input[title*="search" i]', 'textarea[aria-label*="search" i]'];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (!el.getClientRects().length || el.disabled || el.readOnly) continue;
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (el.tagName === 'INPUT' && !['', 'search', 'text'].includes(type)) continue;
      const form = el.closest('form');
      if (form && form.querySelector('input[type=password]')) continue;
      el.setAttribute('data-dot-search', '1');
      return {found: true, label: (el.getAttribute('aria-label') || el.placeholder || el.name || 'search').slice(0, 80)};
    }
  }
  return {found: false};
}"""

# Some sites hide their search box behind a "Search" button (never a form submit button).
SEARCH_TOGGLE_JS = """() => {
  document.querySelectorAll('[data-dot-search-toggle]').forEach(e => e.removeAttribute('data-dot-search-toggle'));
  for (const el of document.querySelectorAll('button, [role=button], input[type=button]')) {
    // Outside any form: a "Search" button inside a form would submit that form instead.
    if (!el.getClientRects().length || el.disabled || el.closest('form')) continue;
    const label = [el.getAttribute('aria-label'), el.title, el.value, el.innerText].join(' ').toLowerCase();
    if (!label.includes('search')) continue;
    el.setAttribute('data-dot-search-toggle', '1');
    return true;
  }
  return false;
}"""

# Readable text currently inside the viewport, after a scroll.
VISIBLE_TEXT_JS = """() => {
  const seen = new Set(), parts = [];
  // Main content only, so sticky sidebars/menus are not reported as "what's in view".
  const root = document.querySelector('main, article, [role=main]') || document.body;
  for (const el of root.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,figcaption,td,th,pre,dt,dd')) {
    const r = el.getBoundingClientRect();
    if (r.bottom <= 0 || r.top >= innerHeight || !r.height) continue;
    if (el.parentElement && el.parentElement.closest('p,li,blockquote,td,th,pre,dd')) continue;
    if (el.closest('nav,aside,header,footer,menu,[role=navigation],[role=complementary],[role=menu]')) continue;
    let pinned = false;
    for (let up = el; up && up !== root.parentElement; up = up.parentElement) {
      const position = getComputedStyle(up).position;
      if (position === 'fixed' || position === 'sticky') { pinned = true; break; }
    }
    if (pinned) continue;
    const t = el.innerText.trim().replace(/\\s+/g, ' ');
    if (!t || seen.has(t)) continue;
    seen.add(t); parts.push(t);
  }
  const doc = document.scrollingElement || document.documentElement;
  const max = Math.max(1, doc.scrollHeight - innerHeight);
  return {visible_text: parts.join('\\n').slice(0, 4000), scroll_percent: Math.round(100 * Math.min(1, scrollY / max)),
          at_top: scrollY <= 2, at_bottom: scrollY >= max - 2};
}"""

INTERACTIONS = {"scroll", "click_link", "back", "forward", "tabs", "switch_tab", "site_search"}


class SharedBrowser:
    def __init__(self, headless=False, profile=None, foreground=True):
        self.headless, self.foreground = headless, foreground
        self.profile = Path(profile).resolve() if profile else None
        self.playwright = self.browser = self.context = self.current = None
        self.guards, self.responses = {}, {}
        self.browser_pid = None
        self.blocked_redirects = []

    async def start(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        options = {"accept_downloads": False, "service_workers": "block"}
        if not self.headless:
            # Without this, Playwright pins pages to a 1280x720 box inside a larger visible
            # window: black margins, the bottom cut off and pop-ups half off-screen.
            options["no_viewport"] = True
        # A persistent Dot-only profile, or a fresh temporary context for a research job.
        # Never attach to an existing browser, reuse its profile or silently install Edge.
        if self.profile:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile), channel="msedge", headless=self.headless, **options)
            self.browser = self.context.browser
        else:
            self.browser = await self.playwright.chromium.launch(channel="msedge", headless=self.headless)
            self.context = await self.browser.new_context(**options)
        root = await self.browser.new_browser_cdp_session()
        info = await root.send("SystemInfo.getProcessInfo")
        self.browser_pid = next(int(p["id"]) for p in info["processInfo"] if p["type"] == "browser")
        await root.detach()
        await self.context.grant_permissions([])
        await self.context.route("**/*", self.route)
        await self.context.route_web_socket("**/*", self.websocket)
        self.context.on("page", self.new_page)
        for page in self.context.pages:
            self.new_page(page)
        if not self.context.pages:
            await self.context.new_page()
        await self.request_guard(self.current.main_frame)

    def new_page(self, page):
        if self.current is None or self.current.is_closed():
            self.current = page
        page.on("download", lambda download: asyncio.create_task(download.cancel()))
        page.on("response", lambda response: self.navigation_response(page, response))
        page.on("close", lambda: self.responses.pop(page, None))
        # Embedded frames (sign-in dialogs, videos, consent pop-ups) need their own guard, started
        # as soon as the frame appears so it is usually ready before the frame's first request.
        page.on("frameattached", lambda frame: asyncio.create_task(self.prepare_frame(frame)))
        asyncio.create_task(self.prepare_page(page))

    def navigation_response(self, page, response):
        if response.request.is_navigation_request() and response.frame == page.main_frame:
            self.responses[page] = (urldefrag(response.url)[0], response.status)

    async def websocket(self, ws):
        try:
            if not ws.url.startswith("wss://"):
                raise ValueError("Secure public WebSockets only")
            await asyncio.to_thread(web_tools.public_url, "https://" + ws.url[6:])
            ws.connect_to_server()
        except Exception:
            await ws.close()

    async def prepare_page(self, page):
        try:
            await self.request_guard(page.main_frame)
        except Exception:
            pass  # Requests stay blocked if preparation fails or the tab closes.

    async def prepare_frame(self, frame):
        try:
            await self.request_guard(frame)
        except Exception:
            pass  # That frame's requests stay blocked.

    async def request_guard(self, frame):
        # Playwright's public route hook skips later HTTP redirect hops. A native
        # Chromium request-stage guard also checks those before they reach the network.
        async def attach():
            if frame.parent_frame is None:
                cdp = await self.context.new_cdp_session(frame.page)
            else:
                try:
                    # Cross-site frames run as their own Chromium target: guard that target.
                    cdp = await self.context.new_cdp_session(frame)
                except Exception:
                    # Same-process frame: the page's guard already sees its requests.
                    await self.request_guard(frame.page.main_frame)
                    return None
            async def paused(event):
                request_id = event["requestId"]
                try:
                    request = event["request"]
                    if request["method"] not in {"GET", "HEAD", "POST", "OPTIONS"}:
                        raise ValueError("Unsupported page request method")
                    await asyncio.to_thread(web_tools.public_url, request["url"])
                    await cdp.send("Fetch.continueRequest", {"requestId": request_id})
                except Exception:
                    self.blocked_redirects.append(event["request"]["url"])
                    del self.blocked_redirects[:-20]
                    try:
                        await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
                    except Exception:
                        pass
            cdp.on("Fetch.requestPaused", paused)
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
            return cdp
        if frame not in self.guards:
            self.guards[frame] = asyncio.create_task(attach())
        await self.guards[frame]

    async def route(self, route):
        request = route.request
        try:
            if request.method not in {"GET", "HEAD", "POST", "OPTIONS"}:
                raise ValueError("Unsupported page request method")
            if request.url.startswith("data:") and not request.is_navigation_request():
                await route.continue_()
                return
            await asyncio.to_thread(web_tools.public_url, request.url)
            frame = request.frame
            guard = self.guards.get(frame)
            if frame.parent_frame is not None and (guard is None or not guard.done()):
                # Embedded frame: wait briefly for its guard instead of refusing everything
                # inside it. If attaching stalls (Chromium can pause a new target), fail closed.
                if guard is None:
                    asyncio.create_task(self.prepare_frame(frame))
                    await asyncio.sleep(0)
                    guard = self.guards.get(frame)
                try:
                    await asyncio.wait_for(asyncio.shield(guard), 3)
                except Exception:
                    await route.abort("blockedbyclient")
                    return
            if guard is None or not guard.done():
                # Installing interception while a new target's first navigation is
                # paused can deadlock Chromium initialization. Fail closed instead.
                # A newly opened popup may need a reload; ordinary prepared tabs work.
                await route.abort("blockedbyclient")
                asyncio.create_task(self.prepare_page(frame.page))
                return
            await guard
            await route.continue_()
        except Exception:
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass  # The owned context may already be closing.

    def page(self):
        pages = [p for p in self.context.pages if not p.is_closed()]
        if not pages:
            raise ValueError("The shared browser window was closed. Ask Dot to reopen it.")
        if self.current is None or self.current.is_closed():
            self.current = pages[-1]
        return self.current

    async def selected_page(self):
        pages = [p for p in self.context.pages if not p.is_closed()]
        if len(pages) <= 1:
            return self.page()
        # Playwright forces hasFocus()/visibilityState for EVERY tab. Do not trust
        # those emulated values. Match only our own Chromium's visible window title.
        # Duplicate titles or extra windows are ambiguous: refuse rather than guess.
        for _ in range(6):
            titles = owned_titles(self.browser_pid)
            matches = []
            if len(titles) == 1:
                for page in pages:
                    try:
                        # Browser error pages (e.g. a blocked navigation) may be matched by title so
                        # one blocked tab does not freeze selection; status() still refuses to read them.
                        if page.url != "about:blank" and not page.url.startswith("chrome-error://"):
                            await asyncio.to_thread(web_tools.public_url, page.url)
                    except (ValueError, OSError):
                        continue
                    title = await page.title()
                    if title and titles[0].startswith(title):
                        suffix = titles[0][len(title):]
                        # Edge adds a tab-count phrase before its profile/browser suffix.
                        # Evaluate every candidate; duplicate/prefix ambiguity still refuses.
                        if not suffix or suffix.startswith(" - ") or re.match(r"^ and \d+ more pages? - ", suffix):
                            matches.append(page)
            if len(matches) == 1:
                self.current = matches[0]
                return self.current
            await asyncio.sleep(.05)
        raise ValueError("Cannot identify the selected shared tab unambiguously. Keep one shared window and close duplicate-title tabs (or keep just one tab).")

    async def status(self):
        if not self.browser or not self.browser.is_connected():
            return {"open": False}
        try:
            page = await self.selected_page()
            if page.url != "about:blank":
                try:
                    await asyncio.to_thread(web_tools.public_url, page.url)
                except (ValueError, OSError):
                    return {"open": True, "blocked": True, "note": "Current tab is outside the public-HTTPS scope; its title/text are not observed."}
            return {"open": True, "url": page.url, "title": await page.title(),
                    "browser": "Microsoft Edge", "profile": "saved Dot-only profile" if self.profile else "temporary research context",
                    "tabs": len(self.context.pages), "selection": "selected tab in the owned window (ambiguous matches refused)",
                    "content_read": False, "vision": False}
        except Exception as exc:
            return {"open": bool(self.context.pages), "content_read": False, "selection_unknown": True, "note": str(exc)}

    async def command(self, command):
        action = command.get("action")
        if action == "status":
            return await self.status()
        if action == "close":
            await self.close()
            return {"closed": True}
        if action in INTERACTIONS:
            if not self.browser or not self.browser.is_connected() or not self.context.pages:
                return {"open": False, "note": "No shared browser is open. Ask Dot to open it first."}
            return await self.interact(action, command)
        if action not in {"open", "search", "view", "discover", "read_url"}:
            raise ValueError("Only navigation/search/reading, scroll, link/back/forward/tab and site-search-box helpers exist. No login/forms/arbitrary click/script tool.")
        query, url = command.get("query"), command.get("url", "")
        if action in {"search", "discover"}:
            if not isinstance(query, str) or not query.strip() or len(query) > 300:
                raise ValueError("Use a public search query of 1-300 characters.")
            url = "https://www.bing.com/search?q=" + quote_plus(query)
        if action == "read_url" or url:
            await asyncio.to_thread(web_tools.public_url, url)
        if not self.browser or not self.browser.is_connected():
            if action == "view":
                return {"open": False, "note": "No shared browser is open. Ordinary windows are not observed."}
            await self.close()
            try:
                await self.start()
            except Exception:
                await self.close()
                raise
        if not self.context.pages:
            if action == "view":
                return {"open": False, "note": "Shared window closed. Ask to reopen it."}
            self.current = await self.context.new_page()
        page = await self.selected_page()
        if action == "open" and command.get("new_tab") and url:
            page = await self.context.new_page()
            self.current = page
        if action in {"open", "search", "discover", "read_url"}:
            await self.request_guard(page.main_frame)
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            if self.foreground:
                await page.bring_to_front()
            if action in {"open", "search"}:
                return await self.status()
        if action == "discover":
            search_url = page.url  # Use the actual final URL, including legitimate Bing redirects.
            from browser_search import observe_results, results
            from playwright.async_api import TimeoutError as BrowserTimeout
            try:
                await page.wait_for_selector("#b_results .b_algo h2 a", state="attached", timeout=5000)
            except BrowserTimeout:
                pass
            observation = await observe_results(page)
            response = self.responses.get(page)
            observation["http_status"] = response[1] if response and response[0] == urldefrag(page.url)[0] else None
            problem = web_tools.page_problem(observation)
            if problem:
                raise ValueError(problem)
            if await self.selected_page() is not page or page.url != search_url:
                raise ValueError("The Edge search tab changed. Ask for a fresh search.")
            return {"engine": "Bing Web in Microsoft Edge", "query": query, "results": results(observation),
                    "limit": "Discovery links only, not read articles or exhaustive coverage. Read sources before making claims; verify their dates."}
        observation = await self.observe(page)
        if action == "read_url" and observation.get("blocked"):
            raise ValueError(observation["note"])
        return observation

    async def observe(self, page):
        if page.url == "about:blank":
            return {**await self.status(), "text": "", "note": "The shared tab is blank. No page contents to read."}
        expected_url = page.url
        await asyncio.to_thread(web_tools.public_url, expected_url)
        from playwright.async_api import TimeoutError as BrowserTimeout
        try:
            # DOMContentLoaded alone can precede a site's first API-rendered article.
            # Bounded wait, not networkidle (many ordinary pages keep connections open).
            await page.wait_for_function("""() => {
                const root = document.querySelector('main, article, [role=main]') || document.body;
                return root && root.innerText.trim().length >= 200;
            }""", timeout=5000)
        except BrowserTimeout:
            pass
        observation = await page.evaluate("""() => {
          const root = document.querySelector('main, article, [role=main]') || document.body;
          const text = root ? root.innerText : '';
          const date = document.querySelector('meta[property="article:published_time"],meta[itemprop="datePublished"]');
          return {url:location.href,title:document.title,text:text.slice(0,50000),total_chars:text.length,
                  published_raw:date?date.content:''};
        }""")
        observation["links"] = await page.evaluate(LINKS_JS)
        await asyncio.to_thread(web_tools.public_url, observation["url"])
        if (await self.selected_page() is not page or page.url != observation["url"]
                or urldefrag(observation["url"])[0] != urldefrag(expected_url)[0]):
            raise ValueError("The shared tab changed during this observation. Ask for a fresh view.")
        response = self.responses.get(page)
        observation["http_status"] = response[1] if response and response[0] == urldefrag(page.url)[0] else None
        problem = web_tools.page_problem(observation)
        return {**await self.status(), **observation, "content_read": True,
                "blocked": bool(problem), "note": problem,
                "links_note": "Numbered links can be opened with browser_click n.",
                "limit": "Edge rendered text, not screenshot/vision. Images/charts/canvas, other apps and ordinary browser profiles are not observed."}

    async def settle(self, page):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass  # Single-page sites may not navigate; report the state we actually see.

    async def interact(self, action, command):
        pages = [p for p in self.context.pages if not p.is_closed()]
        if action == "tabs":
            try:
                selected = await self.selected_page()
            except ValueError:
                selected = None
            tabs = []
            for number, tab in enumerate(pages, 1):
                try:
                    if tab.url != "about:blank":
                        await asyncio.to_thread(web_tools.public_url, tab.url)
                    tabs.append({"n": number, "title": await tab.title(), "url": tab.url, "selected": tab is selected})
                except (ValueError, OSError):
                    tabs.append({"n": number, "title": "(outside public scope; not observed)", "selected": tab is selected})
            return {"open": True, "tabs": tabs}
        if action == "switch_tab":
            number = command.get("n")
            if type(number) is not int or not 1 <= number <= len(pages):
                raise ValueError(f"Choose a tab number from 1 to {len(pages)}; use browser_tabs to list them.")
            page = pages[number - 1]
            await page.bring_to_front()
            self.current = page
            await asyncio.sleep(.3)
            return {**await self.status(), "switched_to": number}
        page = await self.selected_page()
        leaving_error = action in {"back", "forward"} and page.url.startswith("chrome-error://")
        if page.url != "about:blank" and not leaving_error:
            await asyncio.to_thread(web_tools.public_url, page.url)
        if action == "scroll":
            direction = command.get("direction", "down")
            amounts = {"down": .85, "up": -.85, "bottom": 1000, "top": -1000}
            if direction not in amounts:
                raise ValueError("Scroll direction must be down, up, top or bottom.")
            size = await page.evaluate("() => [innerWidth, innerHeight, scrollY]")
            await page.mouse.move(size[0] / 2, size[1] / 2)
            # A real wheel scroll moves whichever panel is under the pointer, like a person would.
            await page.mouse.wheel(0, amounts[direction] * size[1])
            await asyncio.sleep(.7)
            if direction in {"down", "up"} and await page.evaluate("() => scrollY") == size[2]:
                # The wheel can land in an embedded widget or be ignored in a background window.
                await page.evaluate("dy => window.scrollBy({top: dy, behavior: 'smooth'})", amounts[direction] * size[1])
                await asyncio.sleep(.6)
            if direction in {"top", "bottom"}:
                await page.evaluate("d => window.scrollTo(0, d === 'top' ? 0 : document.documentElement.scrollHeight)", direction)
                await asyncio.sleep(.3)
            return {**await self.status(), "scrolled": direction, **await page.evaluate(VISIBLE_TEXT_JS),
                    "note": "Text now in view (headings/paragraphs/list items), not a screenshot."}
        if action in {"back", "forward"}:
            before = page.url
            await self.request_guard(page.main_frame)
            go = page.go_back if action == "back" else page.go_forward
            await go(wait_until="domcontentloaded", timeout=25000)
            moved = page.url != before
            return {**await self.status(), "moved": moved,
                    **({} if moved else {"note": f"No {'earlier' if action == 'back' else 'later'} page in this tab."})}
        if action == "click_link":
            number, text = command.get("n"), command.get("text")
            if type(number) is int:
                links = await page.evaluate(LINKS_JS)
                if not 1 <= number <= len(links):
                    raise ValueError(f"Link {number} does not exist; this page lists {len(links)}. Use browser_view to see them.")
                link, marker = links[number - 1], str(number)
            elif isinstance(text, str) and 0 < len(text.strip()) <= 160:
                link, marker = await page.evaluate(LINK_BY_TEXT_JS, text), "pick"
                if not link:
                    raise ValueError("No visible link with that text. Use browser_view to see the numbered links.")
            else:
                raise ValueError("Give a link number n (from browser_view) or the link's visible text.")
            await asyncio.to_thread(web_tools.public_url, link["url"])
            # Show the owner which link is being followed, then open its address in this tab.
            # Navigating to the href (not a synthetic click) avoids site click handlers and popups.
            await page.evaluate("""m => { const a = document.querySelector(`[data-dot-link="${m}"]`); if (!a) return;
                a.scrollIntoView({block: 'center', behavior: 'smooth'});
                a.style.outline = '3px solid #ff8a65'; a.style.outlineOffset = '2px'; }""", marker)
            await asyncio.sleep(.8)
            target = page
            if command.get("new_tab"):
                target = await self.context.new_page()
                self.current = target
            await self.request_guard(target.main_frame)
            await target.goto(link["url"], wait_until="domcontentloaded", timeout=25000)
            if urlsplit(target.url).path.startswith("/ck/a"):
                # Bing result links pass through a script redirect; wait for the real page.
                try:
                    await target.wait_for_url(lambda u: not urlsplit(u).path.startswith("/ck/a"), timeout=10000)
                    await self.settle(target)
                except Exception:
                    pass
            if self.foreground:
                await target.bring_to_front()
            await asyncio.sleep(.3)
            return {**await self.status(), "url": target.url, "followed": {"title": link["title"], "url": link["url"]},
                    "note": "Opened the link. Use browser_view to read the new page."}
        if action == "site_search":
            text = command.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 200 or "\n" in text:
                raise ValueError("Type a public search query of 1-200 characters on one line.")
            box = await page.evaluate(SEARCH_BOX_JS)
            if not box["found"] and await page.evaluate(SEARCH_TOGGLE_JS):
                await page.locator('[data-dot-search-toggle="1"]').first.click(timeout=5000)
                await asyncio.sleep(.6)
                box = await page.evaluate(SEARCH_BOX_JS)
            if not box["found"]:
                raise ValueError("No usable search box on this page (sign-in forms and other fields are never typed into). If the site has a 'Search' link, follow it with browser_click and try again.")
            field = page.locator('[data-dot-search="1"]').first
            await field.scroll_into_view_if_needed(timeout=5000)
            await field.click(timeout=5000)
            await asyncio.sleep(.4)
            # Many sites swap the box for their own widget on click: type into whatever now has
            # focus, but only if it is still a plain text/search field outside any sign-in form.
            focused = await page.evaluate("""() => { const el = document.activeElement;
                if (!el || !['INPUT', 'TEXTAREA'].includes(el.tagName)) return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (el.tagName === 'INPUT' && !['', 'search', 'text'].includes(type)) return false;
                const form = el.closest('form');
                return !(form && form.querySelector('input[type=password]')); }""")
            if not focused:
                raise ValueError("The search box did not take focus as a plain search field; nothing typed.")
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(text.strip(), delay=35)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1.2)
            await self.settle(page)
            return {**await self.status(), "typed": text.strip(), "search_box": box["label"],
                    "note": "Typed into this site's own search box and pressed Enter. Use browser_view to read the results."}
        raise ValueError("Unknown browser helper.")

    async def close(self):
        try:
            if self.context:
                await self.context.close()  # Flush only the dedicated profile's cookies/storage.
            if self.browser and self.browser.is_connected():
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()
            self.playwright = self.browser = self.context = self.current = None
            self.guards.clear()
            self.responses.clear()


async def serve(headless=False, profile=None, foreground=True):
    session = SharedBrowser(headless, profile, foreground)
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request = {}
            try:
                if len(line) > 20000:
                    raise ValueError("Browser command too large.")
                request = json.loads(line)
                result = await session.command(request)
                response = {"id": request["id"], "result": result}
            except Exception as exc:
                response = {"id": request.get("id"), "error": str(exc).splitlines()[0][:1200]}
            print(json.dumps(response, ensure_ascii=False), flush=True)
            if request.get("action") == "close":
                break
    finally:
        await session.close()


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Isolated fixture checks only")
    parser.add_argument("--profile", type=Path, help="App-supplied Dot-only profile, never the normal Edge profile")
    parser.add_argument("--background", action="store_true", help="Do not bring research pages to the front")
    args = parser.parse_args()
    asyncio.run(serve(args.headless, args.profile, not args.background))
