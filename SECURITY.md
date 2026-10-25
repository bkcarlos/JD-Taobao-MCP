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

HTTP mode binds only to `127.0.0.1` and requires an independent random `MCP_HTTP_TOKEN` of at least 32 visible ASCII characters. Every HTTP path and method requires the exact `Authorization: Bearer <token>` header, including requests carrying a session ID. Missing, duplicate or invalid authorization headers are rejected. Tokens in query strings are not accepted. Access logs are disabled.

All authenticated clients share the same browser session. This is not a multi-user authorization scheme or an OAuth authorization server. Do not share the token between unrelated users. For remote use, provide a trusted TLS reverse proxy or encrypted tunnel, preserve authentication headers, and restrict network access. Never expose the port through an unauthenticated forwarding service. Rotate the token by updating the server and client configuration and restarting the server.

## Verification

Run `python -m unittest discover -s tests -v`. Set `MCP_BROWSER_TESTS=1` after installing Playwright Chromium to include browser regressions. Tests use fake credentials, synthetic pages and loopback HTTP servers; no account login or real shopping traffic is needed. The suite checks sensitive-page rejection, output minimization, redirect-chain blocking, disabled mutation tools and HTTP authorization. It does not audit third-party dependency source code or prove compatibility with live shopping sites.
