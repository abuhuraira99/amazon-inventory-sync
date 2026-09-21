# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/abuhuraira99/amazon-inventory-sync/security/advisories/new)
— the "Report a vulnerability" button on the Security tab. That opens a
disclosure only you and the maintainers can see.

Please include:

- what the vulnerability lets an attacker do
- the steps to reproduce it
- the version or commit you tested
- anything you know about how widely it applies

**What to expect:** an acknowledgement within 3 working days, an assessment
within 10, and credit in the advisory unless you would rather not be named. If
you have not heard back within a week, please chase — a missed notification is
far more likely than a decision to ignore you.

Please give us a reasonable window to ship a fix before disclosing publicly.
We will not take legal action over good-faith research reported this way.

## Supported versions

This is a small project. Fixes go to `main`; there are no maintained release
branches.

## What this software holds

Deployments of this system hold credentials to an Amazon seller account with
permission to change listing quantities. A compromise means somebody else can
change what a seller appears to have in stock. That makes the following
in-scope and worth reporting:

- any way to extract a stored credential in plaintext
- any way to reach a state-changing route without authentication
- any way to make the system send a **price** to Amazon (see below)
- any way to defeat the guardrails, the scope filter or practice mode
- any way to write to a listing outside the configured SKU prefixes
- credential leakage into logs, reports, or error messages

## Design decisions that are security decisions

Worth knowing before you report — several things that look like gaps are
deliberate.

**The dashboard binds to `127.0.0.1`.** That is most of its security. It is
meant to be reached over an SSH tunnel, Tailscale or Cloudflare Tunnel. A
deployment that exposes port 8000 to the internet has removed a control the
design depends on. If you find something that only applies once the port is
exposed, it is still worth reporting, but say so.

**There is no write API.** Every route under `app/routers/api.py` is read-only.
Every mutation is a form POST behind a session cookie and, where it matters, a
typed confirmation. An API key that could zero a catalogue would be a liability
with no compensating benefit — the only consumer is a page on the same origin.

**Credentials are encrypted with AES-GCM under a master key held only in the
environment**, never in the database. A database dump alone does not yield a
usable credential. The interface can show the last four characters of a secret
and nothing else; there is no code path that returns a stored secret to a
browser.

**Prices are structurally impossible to send.** `Decision` has no price field;
a guardrail scans every decision; `assert_quantity_only()` walks the outgoing
payload and raises on any price-related key, however nested. There is also a
`never_send_price` setting marked `locked=True` that is deliberately read by no
code — it exists so no future setting can pretend to disable the rule. A way
past all of that is a serious finding.

**The application should not be granted the Pricing role.** See
[docs/AMAZON-APP-SETUP.md](docs/AMAZON-APP-SETUP.md). Requesting only Product
Listing means that even a total compromise of this software cannot change a
price, because Amazon itself refuses. Two independent locks on the same
promise, and the outer one is not ours to break.

**No CI secrets, no automatic deploy.** A pipeline that could authenticate to a
live seller account is a pipeline that could change a live listing. The test
suite needs neither.

**There is no `print()` in `app/`.** Logging goes through a filter that redacts
credentials; a `print` would bypass it.

## Out of scope

- vulnerabilities in Amazon's Selling Partner API — report those to Amazon
- attacks that require an already-compromised host or database
- missing hardening headers on a deployment that has exposed port 8000 against
  this document's advice
- dependency advisories with no demonstrated path to exploitation here (open a
  normal issue instead — Dependabot already watches the manifests)
