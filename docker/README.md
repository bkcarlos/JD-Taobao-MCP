# Container runtime

The Compose service publishes its authenticated MCP endpoint on host loopback only. It runs as UID/GID 10001, drops Linux capabilities, uses a read-only root filesystem and keeps login state in a private named volume. It does not need the Docker socket, host networking, privileged mode or `SYS_ADMIN`.

Chromium runs with its sandbox enabled. `seccomp.json` comes from [Playwright's Docker profile](https://github.com/microsoft/playwright/blob/91a765d1681cb3eeb0d0fb89fefec3cf0d21bd9b/utils/docker/seccomp_profile.json), licensed under [Apache 2.0](LICENSE.playwright). It keeps a deny-by-default syscall policy while permitting `clone`, `setns` and `unshare` for browser sandbox namespaces. This copy additionally allows the `chroot` syscall: Chromium uses it inside its own user namespace, while Linux capability checks still prevent the non-root parent from using it outside that sandbox. No container capabilities are added. The Python base image is pinned by digest and Playwright is pinned to the browser installer version.

Some hosts also restrict user namespaces with AppArmor. Check an authenticated `browser_start` call after deployment, not only the HTTP health check. If the host denies sandbox setup, configure an application-specific policy; do not disable the Chromium sandbox or grant privileged mode to make the check pass.

The default container is headless. Provisioning a graphical login session is a separate operation. Do not expose remote debugging ports or copy a daily browser profile into the volume. Never run two browser instances against the same profile directory.

Migration from a host installation must stop the old browser before copying its private profile into `/data/browser-profile` and setting ownership to 10001:10001. Keep the original profile and service configuration until the new service passes authenticated tool calls. A rollback must stop the container before starting the original service; do not let both bind the same host port.
