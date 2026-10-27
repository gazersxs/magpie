#!/usr/bin/env python3
"""List, onboard and verify local Magpie ChatGPT accounts. See scripts/README.md."""

import argparse
import json
import os
from pathlib import Path
import random
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from magpie_account_tools import (
    ToolError, WebClient, chrome, load_accounts, local_url, web_key, write_report,
)


def browser_options(parser):
    parser.add_argument("--cdp-helper-path", type=Path, default=Path(os.environ.get(
        "MAGPIE_CDP_HELPER_PATH", str(Path(__file__).resolve().parents[2] / "playwright-chrome" / "src"))),
        help="Directory containing cdp_session.py (default: sibling playwright-chrome/src)")
    parser.add_argument("--cdp-port", type=int, help="Override the CDP helper's configured port")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Read visible text of existing OpenAI tabs")
    browser_options(inspect)
    inspect.add_argument("--max-chars", type=int, default=5000)
    for name in ("list", "onboard", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--config-dir", type=Path,
                             default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "magpie")
        command.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
        command.add_argument("--email", required=name == "onboard")
        command.add_argument("--output", type=Path, help="Optional metadata-only JSON report")
        if name == "onboard":
            browser_options(command)
            command.add_argument("--web-url", default="http://127.0.0.1:3430")
            command.add_argument("--web-key-file", type=Path, help="File containing only the dashboard key")
            selection = command.add_mutually_exclusive_group(required=True)
            selection.add_argument("--workspace", action="append", help="Exact workspace name or ID; repeat to select several")
            selection.add_argument("--all-business", action="store_true", help="Select all workspaces except Personal account")
            command.add_argument("--reauthorize", action="store_true", help="Renew existing accounts instead of skipping their OAuth")
            command.add_argument("--min-gap", type=int, default=60, help="Minimum seconds between authorizations (at least 60)")
            command.add_argument("--max-gap", type=int, default=90)
            command.add_argument("--timeout", type=int, default=180, help="Seconds allowed for manual sign-in and each OAuth callback")
        elif name == "verify":
            command.add_argument("--gateway-url", default="http://127.0.0.1:3425")
            command.add_argument("--model", required=True, help="Magpie provider/model, e.g. codex/<your-model>")
            command.add_argument("--gateway-key-env", default="MAGPIE_GATEWAY_KEY", help="Environment variable for the gateway key (default token: magpie-codex)")
            command.add_argument("--workspace-id", action="append", help="Filter to these workspace IDs")
            command.add_argument("--timeout", type=int, default=90)
    return root


def inspect_tabs(args):
    if args.max_chars < 0:
        raise ToolError("--max-chars must be nonnegative.")
    with chrome(args.cdp_helper_path, args.cdp_port) as session:
        for page in session.context.pages:
            url = urlsplit(page.url)
            if url.hostname in {"auth.openai.com", "chatgpt.com"}:
                print("Page:", url.hostname, url.path)
                print(page.locator("body").inner_text()[:args.max_chars])
    return 0


def selected_workspaces(choices, requested):
    """Names are exact; duplicate names require selection by workspace ID."""
    business = [choice for choice in choices if choice[0].casefold() != "personal account"]
    if requested is None:
        return business
    selected = []
    for name in requested:
        matches = [choice for choice in business if name in choice]
        if len(matches) != 1:
            raise ToolError(f"Workspace {name!r} is missing or ambiguous; use its exact name or ID.")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def consent_page(page, email, timeout):
    """Wait for the user's own sign-in; never enters passwords or OTP codes."""
    deadline = time.monotonic() + timeout
    announced = False
    while time.monotonic() < deadline:
        host = urlsplit(page.url).hostname
        if host != "auth.openai.com":
            time.sleep(1)
            continue
        text = page.locator("body").inner_text()
        if "too many requests" in text.casefold():
            raise ToolError("OpenAI sign-in is rate limited. Stop and try later; saved accounts are preserved.")
        if page.locator('input[name="workspace_id"]').count():
            emails = {line.strip().casefold() for line in text.splitlines()
                      if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", line.strip())}
            if emails != {email.casefold()}:
                raise ToolError("The consent page does not identify the requested email; stopped before authorization.")
            choices = []
            labels = page.locator("label")
            for index in range(labels.count()):
                label = labels.nth(index)
                radio = label.locator('input[name="workspace_id"]')
                if radio.count():
                    # Personal accounts can include an avatar's initials above the label.
                    name = label.inner_text().strip().splitlines()[-1].strip()
                    choices.append((name, radio.input_value()))
            if not choices:
                raise ToolError("The workspace selector has changed; inspect the browser before retrying.")
            return choices
        email_field = page.get_by_role("textbox", name="Email address", exact=True)
        if email_field.count():
            email_field.fill(email)
            page.get_by_role("button", name="Continue", exact=True).click()
        elif urlsplit(page.url).path == "/choose-an-account":
            choice = page.get_by_text(email, exact=True)
            if choice.count():
                choice.click()
            elif page.get_by_text("Use another account", exact=True).count():
                page.get_by_text("Use another account", exact=True).click()
        elif not announced:
            print("Complete the sign-in or verification in the new Chrome tab; waiting…", flush=True)
            announced = True
        time.sleep(1)
    raise ToolError("Sign-in timed out. Complete the login in Chrome and rerun the command.")


def find_account(args, workspace_id):
    return next((account for account in load_accounts(args.config_dir, args.codex_home)
                 if account.identity == (args.email.casefold(), workspace_id)), None)


def onboarding(args):
    if args.min_gap < 60 or args.max_gap < args.min_gap:
        raise ToolError("Use 60 <= --min-gap <= --max-gap.")
    if args.timeout <= 0:
        raise ToolError("--timeout must be positive.")
    # Validate local state before any OAuth or account mutation.
    load_accounts(args.config_dir, args.codex_home)
    client = WebClient(args.web_url, web_key(args.config_dir, args.web_key_file))
    results = []
    selected = None
    completed = False
    pending = None
    with chrome(args.cdp_helper_path, args.cdp_port) as session:
        try:
            while selected is None or selected:
                if completed:
                    gap = random.SystemRandom().randint(args.min_gap, args.max_gap)
                    print(f"Waiting {gap} seconds before the next authorization…", flush=True)
                    until = time.monotonic() + gap
                    while time.monotonic() < until:
                        time.sleep(min(20, max(0, until - time.monotonic())))
                status = client.api("/api/signin", {"Agent": "codex"})
                pending = status["id"]
                if urlsplit(status["url"]).hostname != "auth.openai.com":
                    raise ToolError("Magpie returned an unexpected authorization host.")
                page = session.context.new_page()
                page.goto(status["url"], wait_until="domcontentloaded")
                choices = consent_page(page, args.email, args.timeout)
                if selected is None:
                    selected = selected_workspaces(choices, args.workspace)
                    if not selected:
                        raise ToolError("No business workspaces were found.")
                # Already-saved accounts need only enabling; do not rotate their tokens.
                while selected:
                    name, workspace_id = selected[0]
                    existing = find_account(args, workspace_id)
                    if args.reauthorize or not existing or not existing.refreshable or existing.lapsed:
                        break
                    client.api("/api/login/on", {"Agent": "codex", "User": existing.user})
                    results.append({"workspace": name, **existing.public(), "enabled": True, "status": "existing"})
                    write_report(args.output, results)
                    print(f"Enabled existing account: {name} — {existing.user}", flush=True)
                    selected.pop(0)
                if not selected:
                    client.api(f"/api/signin/{pending}/cancel", {})
                    pending = None
                    page.close()
                    break
                name, workspace_id = selected[0]
                if (name, workspace_id) not in choices:
                    raise ToolError("The available workspaces changed; rerun to inspect them.")
                print(f"Authorizing: {name}", flush=True)
                radio = page.locator(f'input[name="workspace_id"][value="{workspace_id}"]')
                page.locator("label").filter(has=radio).click()
                page.get_by_role("button", name="Continue", exact=True).click()
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    status = client.api(f"/api/signin/{pending}")
                    if status["state"] != "waiting":
                        break
                    time.sleep(2)
                if status["state"] != "done":
                    raise ToolError("Authorization did not complete. Check Chrome and rerun; earlier accounts are preserved.")
                pending = None
                account = find_account(args, workspace_id)
                if account is None:
                    raise ToolError("The saved email/workspace does not match the selection; inspect Magpie before retrying.")
                client.api("/api/login/on", {"Agent": "codex", "User": account.user})
                account = find_account(args, workspace_id)
                results.append({"workspace": name, **account.public(), "status": "authorized"})
                write_report(args.output, results)
                print(f"Enabled: {name} — {account.user}", flush=True)
                selected.pop(0)
                completed = True
                if urlsplit(page.url).hostname in {"localhost", "127.0.0.1", "::1"}:
                    page.close()
        finally:
            if pending:
                try:
                    client.api(f"/api/signin/{pending}/cancel", {})
                except ToolError:
                    print("Could not cancel the pending sign-in; cancel it in Magpie.", file=sys.stderr)
    print(f"Enabled {len(results)} business workspace(s).", flush=True)
    return 0


def response_ok(lines):
    """A completed stream with the expected text proves the pinned account answered."""
    text, completed = [], False
    for line in lines:
        if not line.startswith(b"data: ") or line[6:].strip() == b"[DONE]":
            continue
        try:
            event = json.loads(line[6:])
        except ValueError:
            raise ToolError("The gateway returned malformed streaming data.") from None
        if event.get("type") in {"error", "response.failed", "response.incomplete"}:
            return False
        if event.get("type") == "response.output_text.delta":
            text.append(event.get("delta", ""))
        if event.get("type") == "response.completed":
            completed = event.get("response", {}).get("status", "completed") == "completed"
    return completed and "".join(text).strip() == "MAGPIE_OK"


def verify_account(account, args):
    result = account.public()
    result["ok"] = False
    if not account.enabled:
        result["error"] = "Account is disabled or paused."
        return result
    request = urllib.request.Request(
        local_url(args.gateway_url) + "/v1/responses",
        data=json.dumps({
            "model": args.model, "stream": True, "store": False,
            "instructions": "Be concise.",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly MAGPIE_OK."}]}],
        }).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ.get(args.gateway_key_env, "magpie-codex"),
                 "X-Magpie-Account": account.user.encode("utf-8")},
    )
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=args.timeout) as response:
            result["ok"] = response_ok(response)
        if not result["ok"]:
            result["error"] = "The stream did not complete with MAGPIE_OK."
    except urllib.error.HTTPError as error:
        result["error"] = f"Gateway HTTP {error.code}; inspect Magpie's routing view."
    except (OSError, urllib.error.URLError):
        result["error"] = "Cannot reach the gateway or the request timed out."
    except ToolError as error:
        result["error"] = str(error)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return inspect_tabs(args)
        if args.command == "onboard":
            return onboarding(args)
        accounts = load_accounts(args.config_dir, args.codex_home)
        if args.email:
            accounts = [account for account in accounts if account.email == args.email.casefold()]
        if args.command == "list":
            rows = [account.public() for account in accounts]
        else:
            if args.timeout <= 0 or not args.model.startswith("codex/"):
                raise ToolError("Use a positive timeout and --model codex/<model> to test ChatGPT accounts.")
            if args.workspace_id:
                accounts = [account for account in accounts if account.workspace_id in args.workspace_id]
                if set(args.workspace_id) != {account.workspace_id for account in accounts}:
                    raise ToolError("Some requested workspace IDs were not found for this email.")
            if not accounts:
                raise ToolError("No matching accounts found; no requests were sent.")
            rows = []
            for account in accounts:
                row = verify_account(account, args)
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                write_report(args.output, rows)
            print(f"Verified {sum(row['ok'] for row in rows)}/{len(rows)} account(s).")
            return 0 if all(row["ok"] for row in rows) else 1
        write_report(args.output, rows)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    except ToolError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; completed accounts and reports are preserved.", file=sys.stderr)
        return 130
    except Exception as error:
        # Library exceptions can contain OAuth callback URLs or credential material.
        print(f"Error ({type(error).__name__}); check the local paths, CDP connection, and browser page.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
