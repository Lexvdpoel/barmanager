# Security policy

## Supported versions

Only the newest release and the `latest` image get fixes. Please update (`docker pull
ghcr.io/lexvdpoel/barmanager:latest`) before you report a problem.

## Reporting a vulnerability

Please do **not** report security problems in public issues or Discussions.

Report them privately through
[private vulnerability reporting](https://github.com/Lexvdpoel/barmanager/security/advisories/new)
(the **Security** tab of the repository, then **Report a vulnerability**). Describe what the problem is,
which version you use, how BarManager is reached (directly, or behind which reverse proxy), and the steps
that show it.

You get an answer as soon as possible. When the problem is confirmed, it is fixed in a new release, and you
are credited in the advisory unless you prefer not to be.

## Scope

In scope: BarManager itself (login and sessions, access to locations of other users, the public bar menu,
file and photo uploads, fetching images from URLs, the API) and the Docker image.

Out of scope: your reverse proxy, TLS set-up and server; and third-party services and software that
BarManager talks to (Open Food Facts, UPCitemdb, Wikipedia, TheCocktailDB, SearXNG, Brave Search, Ollama).
Report problems there to their own projects.

## Running it safely

- Always put BarManager behind HTTPS, and change the first administrator password.
- The public bar menu link can be switched off or renewed per location at any time.
- Back up the `/config` folder; it holds the database and the photos.
