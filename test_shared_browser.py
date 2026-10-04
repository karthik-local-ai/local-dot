"""Real installed Edge with fulfilled fixture pages: no public network/model calls."""
import asyncio
import importlib.util
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from shared_browser import SharedBrowser
from workspace_tools import Browser

HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def fake_dns(host, *args, **kwargs):
    address = "10.20.30.1" if host == "private.example" else host if host in {"127.0.0.1", "10.20.30.1"} else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


@unittest.skipUnless(HAS_PLAYWRIGHT, "Use existing Windows Python for native Chromium checks")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dns = patch("web_tools.socket.getaddrinfo", side_effect=fake_dns)
        self.dns.start()
        headed = {"test_focus_switch_tracks_owner_tab", "test_tabs_list_switch_and_new_tab"}
        self.session = SharedBrowser(headless=self._testMethodName not in headed)
        await self.session.start()
        self.blocked = []
        self.seen = []
        async def fixture(real):
            outer = self
            class Route:
                request = real.request
                async def abort(self, *args):
                    outer.blocked.append(real.request.url)
                    await real.abort(*args)
                async def continue_(self):
                    outer.seen.append(real.request.url)
                    path = urlsplit(real.request.url).path
                    if path in {"/redirect-private", "/redirect-domain"}:
                        target = "https://127.0.0.1/secret" if path == "/redirect-private" else "https://private.example/secret"
                        await real.fulfill(status=302, headers={"location": target})
                        return
                    if path == "/api":
                        await real.fulfill(content_type="application/json", body='{"text":"' + 'Rendered API article. ' * 20 + '"}')
                        return
                    if path == "/hydrate":
                        await real.fulfill(content_type="text/html", body="""<title>Dynamic fixture</title><main>Loading</main>
                            <script>setTimeout(()=>fetch('/api',{method:'POST',body:'query'}).then(r=>r.json())
                            .then(r=>document.querySelector('main').innerText=r.text),250)</script>""")
                        return
                    if path == "/denied":
                        await real.fulfill(status=403, content_type="text/html", body='<main>' + 'Long access failure. ' * 200 + '</main>')
                        return
                    if path == "/search":
                        normalize = "<script>history.replaceState({},'',location.href+'&normalized=1')</script>" if 'normalize' in real.request.url else ''
                        await real.fulfill(content_type="text/html", body='<title>Search fixture</title><ol id="b_results"><li class="b_algo"><h2><a href="https://example.org/hydrate">Dynamic article</a></h2></li></ol>' + normalize)
                        return
                    if path == "/long":
                        sections = "".join(f'<h2>Section {i}</h2><p>{"Filler words. " * 60}</p>' for i in range(1, 30))
                        await real.fulfill(content_type="text/html", body=f'<title>Long fixture</title><main><div style="position:sticky;top:0"><ul><li>STICKY-MENU</li></ul></div><h1>TOP-MARKER</h1>{sections}<p>BOTTOM-MARKER</p></main>')
                        return
                    if path == "/sitesearch":
                        await real.fulfill(content_type="text/html", body='<title>Site with search</title><header><form action="/results" method="get"><input type="search" name="q" aria-label="Search this site"></form></header><main>' + 'Site home. ' * 40 + '</main>')
                        return
                    if path == "/framed":
                        await real.fulfill(content_type="text/html", body='<title>Framed</title><main>' + 'Host page. ' * 30 + '<iframe id="good" src="https://widgets.example/inner"></iframe><iframe id="bad" src="https://widgets.example/redirect-private"></iframe></main>')
                        return
                    if path == "/inner":
                        await real.fulfill(content_type="text/html", body='<p>EMBEDDED-OK</p>')
                        return
                    if path == "/hiddensearch":
                        await real.fulfill(content_type="text/html", body='<title>Hidden search</title><header><button type="button" aria-label="Open search" onclick="document.querySelector(\'#box\').style.display=\'block\'">S</button><form action="/wrong"><input type="button" value="Search"></form><form id="box" style="display:none" action="/results"><input type="text" name="query" placeholder="Search papers"></form></header><main>' + 'Home. ' * 40 + '</main>')
                        return
                    if path == "/results":
                        from urllib.parse import parse_qs
                        params = parse_qs(urlsplit(real.request.url).query)
                        query = (params.get("q") or params.get("query") or [""])[0]
                        await real.fulfill(content_type="text/html", body=f'<title>Results</title><main><p>RESULTS-FOR: {query}</p><p>{"Result text. " * 30}</p></main>')
                        return
                    if path == "/login":
                        await real.fulfill(content_type="text/html", body='<title>Sign in</title><main><form action="/session" method="post"><input name="q" aria-label="Search or email"><input type="password" name="pw"></form>' + 'Sign in page. ' * 30 + '</main>')
                        return
                    if path == "/privatelink":
                        await real.fulfill(content_type="text/html", body='<title>Private link fixture</title><main><a href="https://private.example/secret">Internal dashboard</a><p>' + 'Public text. ' * 30 + '</p></main>')
                        return
                    if path == "/download":
                        await real.fulfill(status=200, headers={"content-type": "application/octet-stream",
                            "content-disposition": "attachment; filename=fixture.txt"}, body="Do not save this fixture")
                        return
                    marker = "COPPER-GATE-47" if path == "/a" else "VIOLET-RIVER-82"
                    html = f'<html><head><title>Fixture {path}</title></head><body><main><h1>Shared page</h1><p>Current marker: {marker}.</p><p>{"Fixture text only. " * 20}</p><a href="https://example.org/b">Open B</a></main></body></html>'
                    await real.fulfill(status=200, content_type="text/html", body=html)
            # Exercise the production request guard, replacing only its network continuation.
            await self.session.route(Route())
        await self.session.context.unroute("**/*")
        await self.session.context.route("**/*", fixture)

    async def asyncTearDown(self):
        await self.session.close()
        self.dns.stop()

    async def test_shared_browser_can_open_read_and_search(self):
        opened = await self.session.command({"action": "open", "url": "https://example.org/a"})
        self.assertTrue(opened["open"])
        viewed = await self.session.command({"action": "view"})
        self.assertIn("COPPER-GATE-47", viewed["text"])
        self.assertEqual(viewed["url"], "https://example.org/a")
        self.assertFalse(viewed["vision"])
        self.assertEqual(viewed["links"][0]["url"], "https://example.org/b")
        search = await self.session.command({"action": "search", "query": "local AI & speech"})
        self.assertIn("bing.com/search?q=local+AI+%26+speech", search["url"])

    async def test_owner_navigation_is_observed_not_previous_tool_url(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        page = self.session.page()
        # Simulate owner navigation through the real browser, not Dot's open command.
        await page.goto("https://example.org/b", wait_until="domcontentloaded")
        viewed = await self.session.command({"action": "view"})
        self.assertEqual(viewed["url"], "https://example.org/b")
        self.assertIn("VIOLET-RIVER-82", viewed["text"])
        self.assertNotIn("COPPER-GATE-47", viewed["text"])

    async def test_focus_switch_tracks_owner_tab(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        a = self.session.page()
        b = await self.session.context.new_page()
        await self.session.request_guard(b.main_frame)
        await b.goto("https://example.org/b")
        await b.bring_to_front()
        await self.session.status()
        self.assertIs(self.session.current, b)
        self.assertIn("VIOLET-RIVER-82", (await self.session.command({"action": "view"}))["text"])
        await a.bring_to_front()
        await self.session.status()
        self.assertIs(self.session.current, a)
        self.assertIn("COPPER-GATE-47", (await self.session.command({"action": "view"}))["text"])

    async def test_private_navigation_and_redirects_are_blocked(self):
        for url in ("https://127.0.0.1/secret", "https://10.20.30.1/", "file:///C:/secret.txt", "http://example.org/", "https://user:pass@example.org/"):
            with self.assertRaises(ValueError):
                await self.session.command({"action": "open", "url": url})
        with self.assertRaises(Exception):
            await self.session.command({"action": "open", "url": "https://example.org/redirect-private"})
        self.assertIn("https://127.0.0.1/secret", self.blocked + self.session.blocked_redirects)
        self.assertNotIn("https://127.0.0.1/secret", self.seen)

    async def test_public_page_post_allowed_but_private_post_and_agent_actions_blocked(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        posted = await self.session.page().evaluate("fetch('/submit',{method:'POST',body:'fixture'}).then(()=>true).catch(()=>false)")
        self.assertTrue(posted)
        self.assertIn("https://example.org/submit", self.seen)
        private = await self.session.page().evaluate("fetch('https://private.example/secret',{method:'POST',body:'fixture'}).then(()=>true).catch(()=>false)")
        self.assertFalse(private)
        self.assertNotIn("https://private.example/secret", self.seen)
        for action in ("evaluate", "click", "fill", "submit", "login"):
            with self.assertRaises(ValueError):
                await self.session.command({"action": action, "script": "alert('not allowed')"})

    async def test_scroll_reports_text_now_in_view(self):
        await self.session.command({"action": "open", "url": "https://example.org/long"})
        down = await self.session.command({"action": "scroll", "direction": "down"})
        self.assertGreater(down["scroll_percent"], 0)
        self.assertNotIn("TOP-MARKER", down["visible_text"])
        self.assertIn("Section", down["visible_text"])
        self.assertNotIn("STICKY-MENU", down["visible_text"])
        bottom = await self.session.command({"action": "scroll", "direction": "bottom"})
        self.assertTrue(bottom["at_bottom"])
        self.assertIn("BOTTOM-MARKER", bottom["visible_text"])
        top = await self.session.command({"action": "scroll", "direction": "top"})
        self.assertTrue(top["at_top"])
        self.assertIn("TOP-MARKER", top["visible_text"])
        with self.assertRaises(ValueError):
            await self.session.command({"action": "scroll", "direction": "sideways"})
        # A wheel swallowed by an embedded widget still scrolls the page.
        with patch.object(type(self.session.page().mouse), "wheel", AsyncMock()):
            swallowed = await self.session.command({"action": "scroll", "direction": "down"})
        self.assertGreater(swallowed["scroll_percent"], 0)

    async def test_click_link_by_number_and_text_then_back_forward(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        viewed = await self.session.command({"action": "view"})
        self.assertEqual(viewed["links"][0], {"n": 1, "title": "Open B", "url": "https://example.org/b"})
        clicked = await self.session.command({"action": "click_link", "n": 1})
        self.assertEqual(clicked["url"], "https://example.org/b")
        self.assertIn("VIOLET-RIVER-82", (await self.session.command({"action": "view"}))["text"])
        back = await self.session.command({"action": "back"})
        self.assertTrue(back["moved"])
        self.assertEqual(back["url"], "https://example.org/a")
        forward = await self.session.command({"action": "forward"})
        self.assertEqual(forward["url"], "https://example.org/b")
        self.assertFalse((await self.session.command({"action": "forward"}))["moved"])
        await self.session.command({"action": "back"})
        by_text = await self.session.command({"action": "click_link", "text": "open b"})
        self.assertEqual(by_text["url"], "https://example.org/b")
        for bad in ({"n": 99}, {"text": "no such link"}, {}):
            with self.assertRaises(ValueError):
                await self.session.command({"action": "click_link", **bad})

    async def test_search_result_number_follows_bing_result(self):
        await self.session.command({"action": "discover", "query": "fixture public query"})
        clicked = await self.session.command({"action": "click_link", "n": 1})
        self.assertEqual(clicked["url"], "https://example.org/hydrate")

    async def test_click_refuses_private_link(self):
        await self.session.command({"action": "open", "url": "https://example.org/privatelink"})
        with self.assertRaises(ValueError):
            await self.session.command({"action": "click_link", "text": "Internal dashboard"})
        self.assertEqual(self.session.page().url, "https://example.org/privatelink")
        self.assertNotIn("https://private.example/secret", self.seen)

    async def test_site_search_types_into_own_box_but_never_sign_in_forms(self):
        await self.session.command({"action": "open", "url": "https://example.org/sitesearch"})
        typed = await self.session.command({"action": "site_search", "text": "unicorn art"})
        self.assertEqual(typed["search_box"], "Search this site")
        self.assertIn("/results?q=unicorn+art", typed["url"])
        self.assertIn("RESULTS-FOR: unicorn art", (await self.session.command({"action": "view"}))["text"])
        await self.session.command({"action": "open", "url": "https://example.org/hiddensearch"})
        hidden = await self.session.command({"action": "site_search", "text": "speech synthesis"})
        self.assertIn("/results?query=speech+synthesis", hidden["url"])
        await self.session.command({"action": "open", "url": "https://example.org/login"})
        with self.assertRaisesRegex(ValueError, "No usable search box"):
            await self.session.command({"action": "site_search", "text": "someone@example.org"})
        self.assertNotIn("https://example.org/session", self.seen)
        for bad in ("", "x" * 201, "two\nlines", None):
            with self.assertRaises(ValueError):
                await self.session.command({"action": "site_search", "text": bad})

    async def test_embedded_frames_load_but_private_redirects_stay_blocked(self):
        await self.session.command({"action": "open", "url": "https://example.org/framed"})
        page = self.session.page()
        async def frame_text():
            for frame in page.frames:
                if frame.url == "https://widgets.example/inner":
                    try:
                        return await frame.evaluate("document.body.innerText")
                    except Exception:
                        return ""
            return ""
        for _ in range(40):
            if "EMBEDDED-OK" in await frame_text():
                break
            await asyncio.sleep(.1)
        self.assertIn("EMBEDDED-OK", await frame_text())
        await asyncio.sleep(.5)
        self.assertIn("https://127.0.0.1/secret", self.blocked + self.session.blocked_redirects)
        self.assertNotIn("https://127.0.0.1/secret", self.seen)

    async def test_helpers_do_not_launch_a_closed_browser(self):
        await self.session.close()
        for action in ("scroll", "click_link", "back", "tabs", "site_search"):
            self.assertFalse((await self.session.command({"action": action, "n": 1, "text": "x"}))["open"])

    async def test_tabs_list_switch_and_new_tab(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        opened = await self.session.command({"action": "open", "url": "https://example.org/b", "new_tab": True})
        self.assertEqual(opened["tabs"], 2)
        tabs = (await self.session.command({"action": "tabs"}))["tabs"]
        self.assertEqual([t["url"] for t in tabs], ["https://example.org/a", "https://example.org/b"])
        self.assertTrue(tabs[1]["selected"])
        switched = await self.session.command({"action": "switch_tab", "n": 1})
        self.assertEqual(switched["url"], "https://example.org/a")
        self.assertIn("COPPER-GATE-47", (await self.session.command({"action": "view"}))["text"])
        with self.assertRaises(ValueError):
            await self.session.command({"action": "switch_tab", "n": 5})
        # A blocked navigation in one tab must not freeze tab selection.
        with self.assertRaises(Exception):
            await self.session.command({"action": "open", "url": "https://example.org/redirect-private"})
        self.assertTrue((await self.session.command({"action": "status"}))["blocked"])
        back = await self.session.command({"action": "back"})
        self.assertEqual(back["url"], "https://example.org/a")

    async def test_search_and_rendered_api_article_share_the_same_edge(self):
        pid = self.session.browser_pid
        found = await self.session.command({"action": "discover", "query": "fixture public query"})
        self.assertEqual(found["results"][0]["url"], "https://example.org/hydrate")
        read = await self.session.command({"action": "read_url", "url": found["results"][0]["url"]})
        self.assertIn("Rendered API article.", read["text"])
        self.assertEqual(read["http_status"], 200)
        self.assertEqual(self.session.browser_pid, pid)
        self.assertEqual(read["browser"], "Microsoft Edge")

    async def test_search_uses_actual_final_url_not_requested_spelling(self):
        found = await self.session.command({"action":"discover", "query":"normalize fixture"})
        self.assertEqual(len(found["results"]), 1)
        self.assertIn("normalized=1", self.session.page().url)

    async def test_owner_navigation_during_render_wait_refuses_wrong_page(self):
        await self.session.command({"action":"open", "url":"https://example.org/hydrate"})
        async def navigate():
            await asyncio.sleep(.05)
            await self.session.page().goto("https://example.org/b", wait_until="domcontentloaded")
        task = asyncio.create_task(navigate())
        try:
            with self.assertRaisesRegex(ValueError, "tab changed"):
                await self.session.command({"action":"view"})
        finally:
            await task

    async def test_long_http_failure_remains_visible_but_is_not_read_success(self):
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            await self.session.command({"action": "read_url", "url": "https://example.org/denied"})
        viewed = await self.session.command({"action": "view"})
        self.assertTrue(viewed["blocked"])
        self.assertEqual(viewed["http_status"], 403)
        self.assertTrue(viewed["open"])

    async def test_rendered_metadata_does_not_use_modification_date_as_publication(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        await self.session.page().evaluate("document.head.insertAdjacentHTML('beforeend', '<meta property=\"article:modified_time\" content=\"2026-10-03\">')")
        self.assertEqual((await self.session.command({"action":"view"}))["published_raw"], "")
        await self.session.page().evaluate("document.head.insertAdjacentHTML('beforeend', '<meta property=\"article:published_time\" content=\"2026-09-30\">')")
        self.assertEqual((await self.session.command({"action":"view"}))["published_raw"], "2026-09-30")

    async def test_websocket_policy_allows_only_secure_public_destinations(self):
        for url, allowed in (("wss://example.org/socket", True), ("wss://private.example/socket", False),
                             ("wss://127.0.0.1/socket", False), ("ws://example.org/socket", False)):
            socket_route = Mock(url=url, close=AsyncMock())
            await self.session.websocket(socket_route)
            if allowed:
                socket_route.connect_to_server.assert_called_once()
                socket_route.close.assert_not_awaited()
            else:
                socket_route.close.assert_awaited_once()
                socket_route.connect_to_server.assert_not_called()

    async def test_dedicated_profile_retains_cookie_and_local_storage(self):
        await self.session.close()
        with tempfile.TemporaryDirectory(prefix="dot-edge-fixture-") as profile:
            for attempt in range(2):
                self.session = SharedBrowser(headless=True, profile=profile)
                await self.session.start()
                try:
                    await self.session.page().route("https://example.org/**", lambda route: route.fulfill(
                        content_type="text/html", body='<title>Storage fixture</title><main>' + 'Public fixture. ' * 30 + '</main>'))
                    await self.session.command({"action": "open", "url": "https://example.org/storage"})
                    if attempt == 0:
                        await self.session.context.add_cookies([{"name":"dot_fixture", "value":"kept", "url":"https://example.org/", "expires":time.time()+3600}])
                        await self.session.page().evaluate("localStorage.setItem('dot_fixture','kept')")
                    else:
                        cookies = await self.session.context.cookies("https://example.org/")
                        self.assertTrue(any(c["name"] == "dot_fixture" and c["value"] == "kept" for c in cookies))
                        self.assertEqual(await self.session.page().evaluate("localStorage.getItem('dot_fixture')"), "kept")
                finally:
                    await self.session.close()

    async def test_temporary_research_context_does_not_inherit_shared_cookies(self):
        await self.session.context.add_cookies([{"name":"private_fixture", "value":"never_copy", "url":"https://example.org/"}])
        worker = SharedBrowser(headless=True, foreground=False)
        try:
            await worker.start()
            self.assertEqual(await worker.context.cookies("https://example.org/"), [])
            self.assertIsNone(worker.profile)
            self.assertNotEqual(worker.browser_pid, self.session.browser_pid)
        finally:
            await worker.close()

    async def test_private_dns_redirect_is_blocked_before_continuation(self):
        with self.assertRaises(Exception):
            await self.session.command({"action": "open", "url": "https://example.org/redirect-domain"})
        self.assertIn("https://private.example/secret", self.session.blocked_redirects)
        self.assertNotIn("https://private.example/secret", self.seen)

    async def test_duplicate_titles_refused_instead_of_guessing_tab(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        a = self.session.page()
        b = await self.session.context.new_page()
        await self.session.request_guard(b.main_frame)
        await b.goto("https://example.org/b")
        await b.evaluate("document.title = 'Fixture /a'")
        with patch("shared_browser.owned_titles", return_value=["Fixture /a and 1 more page - Profile 1 - Microsoft Edge"]):
            self.assertTrue((await self.session.status())["selection_unknown"])
            with self.assertRaisesRegex(ValueError, "unambiguously"):
                await self.session.command({"action": "view"})

    async def test_file_download_is_denied(self):
        page = self.session.page()
        async with page.expect_download() as pending:
            with self.assertRaises(Exception):
                await page.goto("https://example.org/download")
        download = await pending.value
        self.assertTrue(await download.failure())
        with self.assertRaises(Exception):
            await download.path()

    async def test_permissions_are_denied(self):
        await self.session.command({"action": "open", "url": "https://example.org/a"})
        states = await self.session.page().evaluate("Promise.all(['geolocation','notifications','camera','microphone'].map(name=>navigator.permissions.query({name}).then(p=>p.state)))")
        self.assertEqual(states, ["denied"] * 4)

    async def test_closed_window_not_falsely_reported_as_visible(self):
        await self.session.page().close()
        self.assertFalse((await self.session.command({"action": "status"}))["open"])
        self.assertFalse((await self.session.command({"action": "view"}))["open"])
        self.assertTrue((await self.session.command({"action": "open", "url": ""}))["open"])


class BrowserBridgeTests(unittest.TestCase):
    def test_owned_stdio_bridge_launch_close_and_denials(self):
        browser = Browser(headless=True)
        self.assertFalse(browser.status()["open"])
        self.assertIsNone(browser.process)
        try:
            result = browser.call("open", url="")
            self.assertTrue(result["open"])
            self.assertEqual(result["url"], "about:blank")
            process = browser.process
            self.assertIn("blank", browser.call("view")["note"])
            with self.assertRaises(ValueError):
                browser.call("open", url="https://127.0.0.1/")
            with self.assertRaises(ValueError):
                browser.call("evaluate", script="not exposed")
        finally:
            browser.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(browser.status()["open"])


if __name__ == "__main__":
    unittest.main()
