"""Local fixtures and HTTP servers only; never reads a real browser or HOME."""

import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magpie_account_tools import Account, ToolError, WebClient, load_accounts, write_report
import magpie_accounts as cli


def auth(email, workspace, plan=None):
    body = {"email": email}
    if plan:
        body["https://api.openai.com/auth"] = {"chatgpt_plan_type": plan}
    claims = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return {"auth_mode": "chatgpt", "tokens": {
        "id_token": f"header.{claims}.signature", "account_id": workspace,
        "access_token": "fixture-access", "refresh_token": "fixture-refresh",
    }}


def account(user, email, workspace, **extra):
    return {"agent": "codex", "user": user, "auth": auth(email, workspace), **extra}


@contextmanager
def server(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "magpie"
        self.codex = self.root / "codex"
        self.config.mkdir()
        self.codex.mkdir()

    def write(self, saved, live=None):
        (self.config / "logins.json").write_text(json.dumps(saved))
        if live is not None:
            (self.codex / "auth.json").write_text(json.dumps(live))

    def test_shared_workspace_keeps_each_emails_seat_and_active_account(self):
        self.write([
            account("one · Team", "one@example.com", "shared"),
            account("two · Team", "two@example.com", "shared", on=True),
            account("one · Team · other", "one@example.com", "other", on=True),
        ], auth("one@example.com", "shared"))
        rows = load_accounts(self.config, self.codex)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].active)
        self.assertTrue(rows[0].enabled)  # On is absent on an active account.
        self.assertFalse(rows[1].active)  # Shared workspace ID, different email.
        self.assertEqual(len({row.identity for row in rows}), 3)

    def test_paused_active_account_is_disabled(self):
        self.write([account("one", "one@example.com", "shared", paused=True)], auth("one@example.com", "shared"))
        self.assertFalse(load_accounts(self.config, self.codex)[0].enabled)

    def test_live_account_without_saved_login_is_listed(self):
        self.write([], auth("one@example.com", "shared"))
        rows = load_accounts(self.config, self.codex)
        self.assertEqual(rows[0].identity, ("one@example.com", "shared"))
        self.assertTrue(rows[0].enabled)

    def test_unsaved_live_business_seat_has_magpies_name(self):
        self.write([account("one@example.com · Team", "one@example.com", "older", on=True)],
                   auth("one@example.com", "new-workspace", plan="team"))
        rows = load_accounts(self.config, self.codex)
        self.assertEqual(rows[-1].user, "one@example.com · Team · new-work")
        self.assertTrue(rows[-1].active)

    def test_malformed_credentials_are_unknown_not_empty(self):
        path = self.config / "logins.json"
        path.write_text('{"private": "fixture-secret",')
        with self.assertRaises(ToolError) as result:
            load_accounts(self.config, self.codex)
        self.assertNotIn("fixture-secret", str(result.exception))
        self.assertEqual(path.read_text(), '{"private": "fixture-secret",')

    def test_report_only_contains_public_metadata(self):
        self.write([account("one", "one@example.com", "shared", on=True)])
        path = self.root / "report.json"
        write_report(path, [row.public() for row in load_accounts(self.config, self.codex)])
        text = path.read_text()
        self.assertNotIn("fixture-access", text)
        self.assertNotIn("fixture-refresh", text)
        self.assertEqual(json.loads(text)[0]["email"], "one@example.com")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_selection_is_exact_and_duplicate_names_need_ids(self):
        choices = [("Business", "id-1"), ("Business", "id-2"), ("Personal account", "id-3")]
        with self.assertRaises(ToolError):
            cli.selected_workspaces(choices, ["Business"])
        self.assertEqual(cli.selected_workspaces(choices, ["id-2"]), [("Business", "id-2")])
        self.assertEqual(cli.selected_workspaces(choices, None), choices[:2])

    def test_failed_verification_sets_nonzero_exit_and_keeps_report(self):
        self.write([account("one", "one@example.com", "shared", on=True)])
        path = self.root / "report.json"
        with patch.object(cli, "verify_account", return_value={"ok": False}), patch("sys.stdout", new=io.StringIO()):
            code = cli.main(["verify", "--config-dir", str(self.config), "--codex-home", str(self.codex),
                             "--model", "codex/example", "--output", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(path.read_text()), [{"ok": False}])

    def onboarding_args(self, *extra):
        return cli.parser().parse_args([
            "onboard", "--email", "one@example.com", "--all-business",
            "--config-dir", str(self.config), "--codex-home", str(self.codex),
            *extra,
        ])

    def test_existing_accounts_are_enabled_without_new_credentials(self):
        saved = Account("one · Team", "one@example.com", "shared", False, False, True, False)
        client = unittest.mock.MagicMock()
        client.api.return_value = {"id": "pending", "url": "https://auth.openai.com/authorize", "state": "waiting"}
        page = unittest.mock.MagicMock()
        session = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
        with patch.object(cli, "load_accounts", return_value=[]), \
                patch.object(cli, "web_key", return_value="fixture-key"), \
                patch.object(cli, "WebClient", return_value=client), \
                patch.object(cli, "chrome") as browser, \
                patch.object(cli, "consent_page", return_value=[("Business", "shared")]), \
                patch.object(cli, "find_account", return_value=saved), \
                patch("sys.stdout", new=io.StringIO()):
            browser.return_value.__enter__.return_value = session
            self.assertEqual(cli.onboarding(self.onboarding_args()), 0)
        client.api.assert_any_call("/api/login/on", {"Agent": "codex", "User": saved.user})
        client.api.assert_any_call("/api/signin/pending/cancel", {})
        page.get_by_role.assert_not_called()  # Never submits workspace consent.

    def test_later_failure_keeps_report_cancels_signin_and_observes_gap(self):
        first = Account("one · Team", "one@example.com", "first", True, False, True, False)
        choices = [("First", "first"), ("Second", "second")]
        report = self.root / "partial.json"
        client = unittest.mock.MagicMock()
        starts = []

        def api(path, data=None):
            if path == "/api/signin":
                starts.append(len(starts) + 1)
                return {"id": str(starts[-1]), "url": "https://auth.openai.com/authorize", "state": "waiting"}
            if path == "/api/signin/1":
                return {"state": "done"}
            if path == "/api/signin/2":
                return {"state": "failed"}
            return None

        client.api.side_effect = api
        page = unittest.mock.MagicMock()
        page.url = "https://auth.openai.com/consent"
        session = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
        clock = SimpleNamespace(now=0)

        def sleep(seconds):
            clock.now += seconds

        with patch.object(cli, "load_accounts", return_value=[]), \
                patch.object(cli, "web_key", return_value="fixture-key"), \
                patch.object(cli, "WebClient", return_value=client), \
                patch.object(cli, "chrome") as browser, \
                patch.object(cli, "consent_page", return_value=choices), \
                patch.object(cli, "find_account", side_effect=[None, first, first, None]), \
                patch.object(cli.random, "SystemRandom") as rng, \
                patch.object(cli.time, "monotonic", side_effect=lambda: clock.now), \
                patch.object(cli.time, "sleep", side_effect=sleep), \
                patch("sys.stdout", new=io.StringIO()):
            rng.return_value.randint.return_value = 73
            browser.return_value.__enter__.return_value = session
            with self.assertRaisesRegex(ToolError, "Authorization did not complete"):
                cli.onboarding(self.onboarding_args("--output", str(report)))
        self.assertEqual(json.loads(report.read_text())[0]["workspace_id"], "first")
        client.api.assert_any_call("/api/signin/2/cancel", {})
        rng.return_value.randint.assert_called_once_with(60, 90)
        self.assertEqual(clock.now, 73)

    def test_wrong_email_and_signin_rate_limit_stop_before_consent(self):
        for text, message in [("Too many requests", "rate limited"),
                              ("different@example.com\nSelect a workspace", "requested email")]:
            with self.subTest(text=text):
                page = unittest.mock.MagicMock()
                page.url = "https://auth.openai.com/consent"
                page.locator("body").inner_text.return_value = text
                page.locator('input[name="workspace_id"]').count.return_value = 1
                with self.assertRaisesRegex(ToolError, message):
                    cli.consent_page(page, "one@example.com", 5)
                page.get_by_role.assert_not_called()


class HttpTests(unittest.TestCase):
    def test_dashboard_uses_cookie_and_accepts_empty_cancel_response(self):
        requests = []

        class Handler(QuietHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Set-Cookie", "magpie_web_test=session; Path=/; HttpOnly")
                self.end_headers()

            def do_POST(self):
                requests.append((self.path, self.headers.get("Cookie")))
                self.send_response(204)
                self.end_headers()

        with server(Handler) as base:
            client = WebClient(base, "fixture-web-key")
            self.assertIsNone(client.api("/api/signin/id/cancel", {}))
        self.assertEqual(requests, [("/api/signin/id/cancel", "magpie_web_test=session")])

    def test_pinned_request_keeps_unicode_account_name(self):
        received = []

        class Handler(QuietHandler):
            def do_POST(self):
                received.append((self.headers["X-Magpie-Account"].encode("latin-1").decode("utf-8"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                for event in [{"type": "response.output_text.delta", "delta": "MAGPIE_OK"},
                              {"type": "response.completed", "response": {"status": "completed"}}]:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")

        account = Account("one@example.com · Team · id-1", "one@example.com", "shared", True, False, True, False)
        with server(Handler) as base:
            args = SimpleNamespace(gateway_url=base, model="codex/example", gateway_key_env="FIXTURE_UNUSED_KEY", timeout=5)
            result = cli.verify_account(account, args)
        self.assertTrue(result["ok"])
        self.assertEqual(received[0][0], account.user)
        self.assertFalse(received[0][1]["store"])

    def test_http_error_never_prints_upstream_secret_body(self):
        class Handler(QuietHandler):
            def do_POST(self):
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'{"error":"fixture-private-token"}')

        account = Account("one", "one@example.com", "shared", True, False, True, False)
        with server(Handler) as base:
            result = cli.verify_account(account, SimpleNamespace(
                gateway_url=base, model="codex/example", gateway_key_env="FIXTURE_UNUSED_KEY", timeout=5))
        self.assertFalse(result["ok"])
        self.assertIn("429", result["error"])
        self.assertNotIn("fixture-private-token", json.dumps(result))

    def test_incomplete_stream_never_counts_as_success(self):
        self.assertFalse(cli.response_ok([
            b'data: {"type":"response.output_text.delta","delta":"MAGPIE_OK"}\n',
            b'data: {"type":"response.incomplete"}\n',
        ]))


if __name__ == "__main__":
    unittest.main()
