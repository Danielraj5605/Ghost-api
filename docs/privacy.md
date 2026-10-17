# Privacy policy

Effective 17 October 2026.

This policy covers this documentation website. The Ghost API software itself runs on your own
computer; what it collects and sends is described in [Security and data](security.md).

## The short version

This website has no accounts, forms, cookies, analytics, advertising or tracking. We do not
collect, sell or share personal data about visitors.

## What happens when you visit

**Hosting.** The website is hosted on [GitHub Pages](https://pages.github.com/). Like any web
server, GitHub receives your IP address and the usual request details (for example your browser
type) when your browser loads a page, and GitHub logs visitor IP addresses for security
purposes. We cannot see or use those logs. GitHub's handling of that data is described in the
[GitHub General Privacy Statement](https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement).

**Repository details.** The page header shows the project's GitHub repository with its version
and star and fork counts. To show them, your browser requests them from GitHub's public API
(`api.github.com`), so GitHub also receives your IP address for that request.

**Compatibility script.** Only in very old browsers that lack a standard feature
(`ResizeObserver`), the page loads a small compatibility script from `unpkg.com`, which then
receives your IP address. Current browsers do not make this request.

**Nothing else.** Fonts are your device's own system fonts, so no font service is contacted.
Search runs inside your browser: what you type in the search box is not sent anywhere.

## What is stored in your browser

The website stores two small items in your browser's own storage. Neither is a cookie, and
neither is sent to us or anyone else:

| Item | Where | Purpose | How long |
|---|---|---|---|
| `__palette` | local storage | remembers whether you chose light or dark mode | until you clear your browser's site data |
| `__source` | session storage | keeps the repository details from GitHub so they are not fetched on every page | until you close the tab |

You can delete them at any time by clearing this site's data in your browser settings.

## Links to other websites

Pages link to other websites (for example GitHub, PyPI and the practice sites in the
[gallery](gallery.md)). Their own privacy policies apply when you visit them.

## Your rights

Because this website does not collect personal data itself, there is nothing about you for us
to access, correct or delete. For data held by GitHub as the host, see GitHub's privacy
statement above, which explains how to exercise your rights with GitHub.

## Changes to this policy

If this policy changes, the new version is published on this page with a new effective date.
Every earlier version stays visible in the
[history of this page](https://github.com/Danielraj5605/Ghost-api/commits/main/docs/privacy.md).

## Contact

Questions about this policy: open an issue in the
[GitHub repository](https://github.com/Danielraj5605/Ghost-api/issues). For anything you would
rather not post publicly, use **Security → Report a vulnerability** in the repository, which
reaches the maintainer privately.
