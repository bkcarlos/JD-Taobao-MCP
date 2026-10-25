# Security boundaries

This is a local, single-user product reader. Complete login and verification directly in the visible browser. Never give account passwords, SMS codes, payment credentials or identity documents to an MCP client.

## Data exposed to MCP

- Only explicitly allowed public product, search and home pages can be inspected. The fixed official login URLs can be opened with `open_login`; generic `open_url` accepts public pages only. Login pages can be used manually, but their contents cannot be exported. Account, order, address and payment routes are not readable.
- Link enumeration exports only references and allowed URLs, not input values, labels, anchor text or page text. Product extractors return selected fields, not raw page bodies or raw JSON-LD. Whole-page parameter and review fallbacks are removed.
- Cookie presence is checked locally as a login hint. Neither cookie names nor values are returned.
- Input, form submission, screenshot and history tools are removed. Link navigation does not invoke page click handlers. Setting the old `ALLOW_STATE_CHANGING_ACTIONS=true` fails at startup.
- Public links discard unknown query parameters and fragments. Extracted image URLs must belong to the platform resource allowlist and omit query parameters and fragments.

## Network controls and limitations

Browser navigation uses explicit HTTPS host/path rules; resources use a separate platform-domain allowlist. Unknown destinations are rejected before fetching. HTTP redirects are fetched without automatic following. Allowed navigation redirects become fresh GET navigations so Chromium cannot bypass the next check by following an unintercepted redirect chain. Subresource redirects and 307/308 redirects requiring request-body replay are blocked. WebSockets, Service Workers and downloads are disabled.

These controls cover intercepted page requests, not all traffic made by the browser process or dependencies. They are not a firewall. Platform scripts can still make requests and change state on allowed platform domains; the tools do not enforce read-only behavior inside the shopping sites themselves. DNS, browser vulnerabilities, compromised platform content and dependency supply-chain compromise remain outside this boundary. A configured proxy is also a trusted component.

Strict rules can break login challenges, embedded frames, CDN redirects or content that requires clicking tabs. Review individual required destinations before extending an allowlist. Do not replace the policy with a wildcard. No real shopping-account login compatibility is claimed by the offline test suite.

Product text, metadata and reviews remain untrusted external content. MCP clients must treat them as data, not instructions, and must not automatically follow embedded commands or URLs. Structured extraction is not a guarantee that a platform page contains no personalized information.

## Local login state

Use a dedicated `BROWSER_PROFILE_DIR`, never a normal daily browser profile. Login state remains in this directory. Do not upload, share, back up to an untrusted service or commit it. The application restricts its profile directory to mode 0700 on POSIX and rejects a directly symlinked profile directory; this does not promise encryption at rest or Windows ACL isolation. Protect the parent directory and the OS account as well. Closing the browser does not delete saved login state.

The application has no account-upload endpoint or configured telemetry server. This is a source-level observation, not a guarantee about future releases, installed dependencies or the shopping platforms themselves.

## Transports

Prefer stdio on the same machine. A client allowed to launch the server can use its shopping browser session; operating-system isolation remains necessary.

HTTP mode defaults to `127.0.0.1` and requires an independent random `MCP_HTTP_TOKEN` of at least 32 visible ASCII characters. Every HTTP path and method requires the exact `Authorization: Bearer <token>` header, including requests carrying a session ID. Missing, duplicate or invalid authorization headers are rejected. Tokens in query strings are not accepted. Access logs are disabled.

For server-to-server connections on a private Docker network, `MCP_HTTP_EXTRA_HOSTS` adds comma-separated exact Host values (for example `shopping-mcp:8000`). Wildcards, URLs and invalid ports are rejected. This does not expand the Origin allowlist or disable authentication and DNS rebinding protection.

`MCP_HTTP_HOST` can explicitly change the listening address for a container. The supplied Compose file binds inside the container to `0.0.0.0` but publishes only on the host loopback address. Host/Origin validation remains enabled. The container runs as a non-root user with all capabilities dropped, a read-only root filesystem and a private persistent data volume. Chromium sandboxing remains enabled; the bundled Playwright seccomp profile permits its user namespaces without using privileged mode.

All authenticated clients share the same browser session. This is not a multi-user authorization scheme or an OAuth authorization server. Do not share the token between unrelated users. For remote use, provide a trusted TLS reverse proxy or encrypted tunnel, preserve authentication headers, and restrict network access. Never expose the port through an unauthenticated forwarding service. Rotate the token by updating the server and client configuration and restarting the server.

## Verification

Run `python -m unittest discover -s tests -v`. Set `MCP_BROWSER_TESTS=1` after installing Playwright Chromium to include browser regressions. Tests use fake credentials, synthetic pages and loopback HTTP servers; no account login or real shopping traffic is needed. The suite checks sensitive-page rejection, output minimization, redirect-chain blocking, disabled mutation tools and HTTP authorization. It does not audit third-party dependency source code or prove compatibility with live shopping sites.


## Optional human login portal

`LOGIN_PUBLIC_URL` enables a separate `/login` portal. Keep it on a private network or an
SSH loopback tunnel, with HTTPS for non-loopback origins. Proxy only the login routes;
MCP routes still require the existing bearer token. Portal Host values must exactly match
the configured URL, and POST requests require the exact Origin and a custom action header.

`begin_login` returns a single-use, 10-minute capability in a URL fragment. Treat that link
as sensitive: it grants a short-lived human login session, not arbitrary browser control or
MCP tools. The human browser removes the fragment and exchanges it for a short-lived
HttpOnly, SameSite=Strict cookie (Secure with HTTPS). The portal has no external scripts,
frames or analytics and uses no-store responses and a restrictive CSP. It renders only the
fixed QR image or, on explicit human request, the fixed official JD login document.
It cannot screenshot arbitrary account/product pages. Password submission fills only the
fixed username/password fields and login button in that document, with no caller-supplied
selectors, URLs or scripts. No password, code or screenshot is exposed as an MCP tool or
returned in MCP results. Bodies are size-bounded, errors are fixed strings, access logs are
disabled, and credentials are not written to disk. In-memory strings are transient but
Python does not guarantee secure memory erasure. The private server remains a trusted
credential processor; use HTTPS outside loopback and do not share login links.

Only the authenticated human session can submit a password, view the official document,
or forward a bounded click/drag gesture and verification text. Every action checks the
same fixed official document and short-lived deadline. Codes can only fill a focused
non-password verification input. A password form may be submitted at most three times
per portal session. Human pointer input is replayed, never generated by an automated
CAPTCHA solver; existing MFA and verification requirements remain in force. The official
page, including transient password/code fields, is cleared on success, cancellation or
expiry. Cookie persistence remains separate from password input.

While that human session is active, main-frame GET navigation may also visit the exact
HTTPS endpoint `sso.jd.com/setCookie`, used by JD's login script for its SSO cookie handoff.
Every subsequent redirect is checked again. This exception does not permit other SSO
paths, iframe navigation, POST replay, MCP navigation, or reading the callback page.
The same scoped navigation rule permits the observed verification document at
`cfe.m.jd.com/privatedomain/risk_handler/03101900/`. Only the authenticated human portal
can view or interact with that document. Password autofill remains restricted to the
original login form; verification pages, their parameters and contents are never exported
to MCP. This supports manual verification and does not bypass a platform rejection.

For blocked or failed main-frame navigation during human login, the private profile keeps
one `login-navigation.json` record (mode 0600), replaced on the next failure. It contains the
timestamp, failure type, scheme, hostname and bounded path, never URL userinfo, query,
fragment, request bodies or cookies. Treat endpoint metadata as private: this file is
ignored by Git and is not returned through MCP or the portal. MCP status continues to
redact unrecognized hostnames. An administrator can inspect this record to review a
specific destination rather than widening navigation rules based on guesses.

Cookie snapshots remain inside the private persistent profile (0700 directory, atomic
0600 file). They are not returned to MCP or the portal and are ignored by Git. Only
unexpired first-party cookies are restored, without overwriting newer Chromium state.
A cookie-presence hint is not proof of a valid server-side session. Profile and volume
backups contain credentials and must be protected like account access. A hard crash can
lose changes since the last login checkpoint or clean close.
