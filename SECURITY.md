# Security boundaries

This is a local, single-user product, JD cart, order-history and followed-product reader. Complete login and verification directly in the visible browser. Never give account passwords, SMS codes, payment credentials or identity documents to an MCP client.

## Data exposed to MCP

- Only explicitly allowed public product, search and home pages can be inspected by generic tools. The fixed official login URLs can be opened with `open_login`; generic `open_url` accepts public pages only. Login pages can be used manually, but their contents cannot be exported. Generic tools reject account, order, address and payment routes.
- Link enumeration exports only references and allowed URLs, not input values, labels, anchor text or page text. Product extractors return selected fields, not raw page bodies or raw JSON-LD. Whole-page parameter and review fallbacks are removed.
- Cookie presence is checked locally as a login hint. Neither cookie names nor values are returned.
- Input, form submission, screenshot and history tools are removed. Link navigation does not invoke page click handlers. Setting the old `ALLOW_STATE_CHANGING_ACTIONS=true` fails at startup.
- Public links discard unknown query parameters and fragments. Extracted image URLs must belong to the platform resource allowlist and omit query parameters and fragments.

### Private JD cart

`get_cart` is a separate, authenticated account-data capability. It copies the existing
JD login cookies into an ephemeral browser context, opens only `https://cart.jd.com/cart_index`,
and closes the context after the read. Generic navigation, snapshots and product extraction
still reject the cart. Only the observed POST query `pcCart_jc_getCurrentCart` at
`https://api.m.jd.com/api` and static assets from `360buyimg.com` are allowed in this context.
All redirects, other APIs, iframe documents, WebSockets, service workers and downloads
are blocked. No tool clicks, changes quantities/selection, claims coupons, deletes items
or proceeds to checkout. Platform query behavior is still controlled by JD.

The response includes only merchandise fields, nested bundle components, displayed gift
items, structured availability and the selected-items estimate. Bundle stock uses its
virtual SKU rather than component stock; unrecognized stock signals remain unknown.
Component standalone reference prices are not
presented as allocated bundle prices. Main cart entries, component rows, gift rows and
gift quantities have separate counts. Missing nested details or inconsistent counts
produce a partial result. Coverage reports the desktop source and bounded provider counts;
account-wide completeness remains unverified even when this response is fully parsed.
User-reported missing main products cannot be accounted for using gifts or bundle components. Account
identifiers, addresses, cookie values, internal item identifiers, provider errors and the
raw response are discarded. Cart contents are private data delivered to the authorized
MCP client/model; they are not public product results. The server does not persist them,
but clients may store tool results in their chat history. All clients holding the MCP
token can access this single user's cart; keep the token private. No real cart fixtures
belong in the repository. Missing prices remain unknown, and partial results are labeled.

### Private JD order history

`get_orders` is another authenticated account-data capability. It uses an ephemeral
context, copies only cookies applicable to the fixed JD order and session-sync hosts,
and reads the most recent three months, up to five pages and 100 orders. The initial
order document may redirect between `order.jd.com/center/list.action`,
`order.jd.hk/center/list.action`, `passport.jd.com/new/login.aspx` and
`sso.jd.hk/sso/sync/redirect`. Each GET redirect is inspected, capped at eight hops,
and its return URL must target a fixed order document. Credentials are never entered;
login or verification that cannot be completed by the existing session fails closed.
Synchronization URLs and newly issued cookies stay in the temporary context.

Rendering permits only the fixed paginated order documents, static assets from
`360buyimg.com`, the observed `gias.jd.com/js/pc-tk.js` script, and the POST query
`pcorder_center_list` at `/api` on `api.m.jd.com` or `color.jd.hk`. Query bodies must
specify the recent-three-months period, all statuses and a bounded page number.
Other APIs, redirects, iframe documents, WebSockets and downloads are blocked.
No cancellations, deletions, payments, invoice requests, receipt confirmations or
other order mutations are enabled.

Only merchandise, quantities, gift markers, order time, status and the displayed
amount and label are returned. Order IDs are used only for in-memory deduplication;
recipients, addresses, phone numbers, invoice data, payment accounts and raw responses
are discarded. Unknown amounts remain unknown and are never substituted with another
payment field. Canceled orders, unpaid orders and gift suborders remain distinguishable
by their provider metadata. Failed pages, changing totals, duplicates and result limits
produce partial results. These private results share the cart capability's single-user
token and client-history limitations; never commit real order fixtures.

### Private JD followed products

`get_followed_products` reads the official desktop cart's unfiltered "刚收藏" list.
It copies cookies applicable to the cart and API hosts into an ephemeral context,
loads the fixed cart document and reuses JD's own signing SDK. The only API allowed
is the GET `pc_follow_product_new` query on `https://api.m.jd.com/api`, with fixed
source, page size 20 and page numbers 1–6. At most five data pages and one empty-page
probe are fetched; at most 100 products are returned. Filters, duplicate parameters,
mutating APIs, redirects, iframe documents, WebSockets, service workers and downloads
are blocked. No follow/unfollow, cart changes or coupon collection is performed.

Only product names, canonical product links, displayed prices and their labels are
returned. Signing values, cookies and raw responses remain internal. Missing prices
remain unknown. Duplicate or malformed entries, failed pages and limits produce partial
results; only an empty response page confirms the end of this desktop list. It does
not establish completeness across the mobile app or include followed stores. Results
share the other private capabilities' token and client-history limitations. All tests
use synthetic products and account sentinels; never commit real followed lists.

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

Live pointer events use the same authenticated human session and Origin checks as other
portal actions. One gesture may hold the mouse button between requests, for at most
15 seconds. Moves/releases must match that gesture; cancellation, login cleanup and a
disconnect timeout release the button. Coordinates are bounded to the browser viewport.
The human sees refreshed frames while dragging; no trajectory or solution is generated
by the server, and the frames and input are not persisted or exposed to MCP.

While that human session is active, main-frame GET navigation may also visit the exact
HTTPS endpoint `sso.jd.com/setCookie`, used by JD's login script for its SSO cookie handoff.
Every subsequent redirect is checked again. This exception does not permit other SSO
paths, iframe navigation, POST replay, MCP navigation, or reading the callback page.
The same scoped navigation rule permits the observed verification documents at
`cfe.m.jd.com/privatedomain/risk_handler/03101900/` and `aq.jd.com/certified/index`.
Only the authenticated human portal can view or interact with those documents.
Password autofill remains restricted to the
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
