# Local account tools

These standalone Python tools reuse Magpie's existing dashboard and gateway
APIs. They add no Go dependencies and change no upstream application files.
Keep them as a separate commit when rebasing this checkout onto upstream.

## Requirements

- Python 3.10 or newer. Listing and verification use only the standard library.
- `magpie web --no-open` running locally for onboarding, with a known web key.
  A terminal-only gateway serves verification but has no dashboard OAuth API.
- For inspection and onboarding, a running Chrome CDP session and the
  `playwright-chrome` checkout used for the original setup. Its Python
  environment provides Playwright and PyYAML; its `src/cdp_session.py` handles
  the configured CDP port and WSL bridge. These tools connect with
  `auto_start=False`, so Chrome and the bridge must already be running.

From this repo, use the existing environment:

```sh
../playwright-chrome/.venv/bin/python scripts/magpie_accounts.py --help
```

The browser commands default to `../playwright-chrome/src`, resolved relative
to this repository, and accept `--cdp-helper-path /path/to/src` and
`--cdp-port 9222`. `MAGPIE_CDP_HELPER_PATH` can set the helper directory.
No browser dependency is imported for `--help`, `list`, or `verify`.

## Inspect existing OpenAI tabs

```sh
../playwright-chrome/.venv/bin/python scripts/magpie_accounts.py inspect --max-chars 3000
```

The original entrypoint remains available with the same options:

```sh
../playwright-chrome/.venv/bin/python scripts/magpie_c_account_inspect.py
```

Inspection prints each matching tab's hostname, path, and visible text. It
does not navigate or extract tokens. Visible text can include private chats,
so review the output before sharing it.

## List saved accounts

```sh
python3 scripts/magpie_accounts.py list --email person@example.com
```

Omit `--email` to list every local Codex account. Output includes the saved
account name, email, workspace ID, enabled/active status, and whether renewable
credentials are present. It never includes tokens. Listing makes no network
requests and writes no account files.

The default state paths are `$XDG_CONFIG_HOME/magpie` (or `~/.config/magpie`)
and `$CODEX_HOME` (or `~/.codex`). Use `--config-dir` and `--codex-home` for a
portable installation, a Windows-mounted profile, or another location. The
paths must identify the same files that the running Magpie instance uses.

Identity is the email **and** workspace ID together. Different emails can
share a workspace; one email can have several workspaces. The active account
is enabled unless paused, even when its saved `on` field is absent.

## Add business workspaces

```sh
../playwright-chrome/.venv/bin/python scripts/magpie_accounts.py onboard \
  --email person@example.com --all-business \
  --min-gap 60 --max-gap 90 \
  --output /tmp/magpie-onboarding.json
```

Or select exact names or workspace IDs explicitly:

```sh
../playwright-chrome/.venv/bin/python scripts/magpie_accounts.py onboard \
  --email person@example.com \
  --workspace 'Business workspace A' --workspace 'Business workspace B'
```

The tool starts Magpie's OAuth flow in a new Chrome tab and checks that the
consent page identifies the requested email. Passwords, passkeys, verification
codes, or unexpected login screens are completed by you in Chrome. The default
wait is 180 seconds; change it with `--timeout`.

`--all-business` excludes the English `Personal account` option. Explicit
selection uses an exact name or workspace ID. Duplicate names require IDs.
The automation expects the English OpenAI sign-in UI used during the original
setup; a changed UI or another language can require manual intervention.

Existing renewable accounts are enabled without authorizing them again. Use
`--reauthorize` to renew them deliberately. Missing, lapsed, or nonrenewable
accounts go through OAuth, and their saved email/workspace identity is checked
before they are enabled. Magpie alone owns and refreshes their credentials.

The tool waits a random 60–90 seconds between authorizations, with configurable
bounds of at least 60 seconds. It stops on sign-in rate limits instead of
retrying. Completed accounts stay enabled, the optional report is updated
after each account, and pending sign-ins are canceled on failure or Ctrl-C.
Rerun after completing any manual login or allowing a rate limit to clear.
Reruns discover the workspace list through OAuth, then skip saved accounts;
there is no requirement to keep a previous report to resume.

Dashboard authentication reads `MAGPIE_WEB_KEY`, or the `MAGPIE_WEB_KEY=…` line
in `<config-dir>/web.env` from the original local service setup. Alternatively,
`--web-key-file /path/to/key` reads a file containing only the running
dashboard's key. Set that same key when starting `magpie web`; these tools do
not create a key or restart the service. `--web-url` defaults to
`http://127.0.0.1:3430`. Reports contain metadata only, and atomic replacements
use private file permissions on Unix. Prefer reports outside the checkout,
or under the ignored `scripts/reports/` folder.

## Verify each account through the gateway

```sh
python3 scripts/magpie_accounts.py verify \
  --email person@example.com --model codex/YOUR_MODEL \
  --output /tmp/magpie-verification.json
```

Choose a model that these accounts actually serve (`magpie models codex` lists
the available names). The tool sends one tiny request per matching account,
pinned by its exact saved name with `X-Magpie-Account`. A successful test proves
that account answered, with no fallback to another account. Verification uses
quota and Magpie records it in Usage. Tests run sequentially. A completed
stream returning `MAGPIE_OK` passes; a disabled account, refused request,
incomplete stream, or incorrect reply fails. The command exits nonzero if any
account fails or no accounts match.

`--workspace-id ID` narrows the selection and can be repeated. Combine it with
`--email` to select a particular seat of a shared workspace. `--gateway-url`
defaults to `http://127.0.0.1:3425`, and `--timeout` defaults to 90 seconds per
request. The local gateway token defaults to `magpie-codex`; `MAGPIE_GATEWAY_KEY`
or the variable named by `--gateway-key-env` supplies a managed gateway key.
The supplied key must be allowed to use the selected accounts and model.

The tools support local dashboard and gateway URLs only. They do not change
Codex wiring, provider models, routing modes, services, or upstream Go code.
Routing and fallback remain controlled by Magpie's existing settings.

## Verify the tools

```sh
python3 -B -m unittest discover -s scripts/tests -v
```

The tests use temporary account files and local HTTP fixtures. They never
connect to Chrome, read your real account store, or call a model provider.
They cover shared workspace seats, active/paused accounts, malformed state,
exact workspace selection, dashboard cookies and empty cancellation replies,
Unicode account pinning, failed verification, and credential-free reports.

Relevant upstream references: [providers and accounts](../docs/subsystems/providers-accounts.md),
[gateway routing](../docs/subsystems/gateway-routing.md), and
[GUI/browser API](../docs/subsystems/gui-shell.md). Their contracts are unchanged.
