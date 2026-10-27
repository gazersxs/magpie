"""Shared local account and Chrome CDP helpers; never writes credentials."""

import base64
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import http.cookiejar
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit


class ToolError(Exception):
    """An actionable error whose message contains no credentials."""


def read_json(path, missing=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return missing
    except (OSError, ValueError):
        raise ToolError(f"Cannot read valid JSON from {path}; file left unchanged.") from None


def token_claims(auth):
    try:
        tokens = auth.get("tokens") or {}
        token = tokens.get("id_token", "")
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        if not isinstance(claims, dict):
            raise ValueError
        return claims
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ToolError("Cannot identify a Codex account from its saved credentials.") from None


def token_identity(auth):
    try:
        tokens = auth.get("tokens") or {}
        claims = token_claims(auth)
        email = claims["email"].casefold()
        workspace = tokens.get("account_id") or claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id")
        if not email or not workspace:
            raise ValueError
        return email, workspace
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        raise ToolError("Cannot identify a Codex account from its saved credentials.") from None


@dataclass
class Account:
    user: str
    email: str
    workspace_id: str
    enabled: bool
    active: bool
    refreshable: bool
    lapsed: bool

    @property
    def identity(self):
        return self.email, self.workspace_id

    def public(self):
        return asdict(self)


def load_accounts(config_dir, codex_home):
    saved = read_json(config_dir / "logins.json", [])
    live = read_json(codex_home / "auth.json", {})
    if not isinstance(saved, list) or not isinstance(live, dict):
        raise ToolError("Unexpected account file format; files left unchanged.")
    live_identity = token_identity(live) if live.get("tokens") else None
    accounts = []
    for entry in saved:
        if not isinstance(entry, dict):
            raise ToolError("Unexpected saved account entry; file left unchanged.")
        if entry.get("agent") != "codex":
            continue
        auth = entry.get("auth", {})
        email, workspace = token_identity(auth)
        active = (email, workspace) == live_identity
        user = entry.get("user")
        if not isinstance(user, str) or not user:
            raise ToolError("A saved Codex account has no name.")
        accounts.append(Account(
            user, email, workspace,
            bool((active or entry.get("on")) and not entry.get("paused")),
            active, bool((live if active else auth).get("tokens", {}).get("refresh_token")),
            bool(entry.get("lapsed")),
        ))
    if live_identity and not any(a.active for a in accounts):
        email, workspace = live_identity
        plan = token_claims(live).get("https://api.openai.com/auth", {}).get("chatgpt_plan_type", "")
        user = email + " · " + plan.capitalize() if plan in {"team", "business", "enterprise", "edu"} else email
        # Match Magpie's codexUser/codexName when this live seat was never saved.
        taken = {account.user.casefold() for account in accounts}
        if user.casefold() in taken:
            base = user + " · " + workspace[:8]
            user = base
            number = 2
            while user.casefold() in taken:
                user = f"{base} ({number})"
                number += 1
        accounts.append(Account(user, email, workspace, True, True,
                                bool(live["tokens"].get("refresh_token")), False))
    identities = [a.identity for a in accounts]
    if len(set(identities)) != len(identities):
        raise ToolError("Duplicate email/workspace identities in saved accounts; inspect them in Magpie.")
    return accounts


def local_url(value):
    url = urlsplit(value)
    if (url.scheme not in {"http", "https"} or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        raise ToolError("Use a local HTTP(S) base URL without a path, credentials, or query.")
    return value.rstrip("/")


def web_key(config_dir, key_file=None):
    if key_file:
        try:
            key = key_file.read_text().strip()
        except OSError:
            raise ToolError("Cannot read the web key file.") from None
    else:
        key = os.environ.get("MAGPIE_WEB_KEY", "")
        if not key:
            try:
                lines = (config_dir / "web.env").read_text().splitlines()
            except OSError:
                raise ToolError("Set MAGPIE_WEB_KEY or supply --web-key-file for the running dashboard.") from None
            key = next((line.split("=", 1)[1].strip().strip("\"'") for line in lines
                        if line.strip().startswith("MAGPIE_WEB_KEY=")), "")
    if not key:
        raise ToolError("The Magpie web key is empty.")
    return key


class WebClient:
    def __init__(self, base, key):
        self.base = local_url(base)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        self._read(urllib.request.Request(self.base + "/?" + urlencode({"k": key})))

    def _read(self, request):
        try:
            with self.opener.open(request, timeout=40) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise ToolError(f"Magpie dashboard returned HTTP {error.code}.") from None
        except (OSError, urllib.error.URLError):
            raise ToolError("Cannot reach the Magpie dashboard; check its address and service.") from None

    def api(self, path, data=None):
        raw = self._read(urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
        ))
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise ToolError("Magpie dashboard did not return JSON; check the web key.") from None


@contextmanager
def chrome(helper_path, port=None):
    helper = helper_path.expanduser().resolve()
    if not (helper / "cdp_session.py").is_file():
        raise ToolError("CDP helper not found; use --cdp-helper-path /path/to/playwright-chrome/src.")
    sys.path.insert(0, str(helper))
    try:
        module = importlib.import_module("cdp_session")
        with module.cdp_browser(port=port, auto_start=False) as session:
            yield session
    finally:
        sys.path.remove(str(helper))


def write_report(path, rows):
    """Replace only the requested metadata report, keeping partial progress."""
    if path is None:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump(rows, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
