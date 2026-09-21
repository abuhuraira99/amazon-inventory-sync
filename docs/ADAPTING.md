# Adapting this to your own setup

The system was built against one supplier feed and one marketplace, but the
parts most likely to differ were made configurable rather than hard-coded.

Roughly:

| What you are changing | Effort |
|---|---|
| Column names, delimiter, SKU prefix | settings, no restart |
| A different archive layout or filename convention | small code change |
| SFTP instead of FTPS | configuration, already supported |
| A different marketplace | settings plus one config value |
| A different region | config value, and re-check the roles |
| A non-file feed (an API, a database) | new module behind the same seam |

Read [ARCHITECTURE.md](ARCHITECTURE.md) first if you have not — several of
these have sharp edges that make more sense with the design in view.

---

## A different feed format

### Delimiter and columns

Both are settings, editable on the Settings page, effective on the next run.

```
feed_delimiter   "|"      the character between columns
column_map       {...}    which column holds what
```

`column_map` maps the system's internal field names to the names in *your*
file's header row:

```json
{
  "barcode": "EAN",
  "brand":   "Manufacturer",
  "title":   "ProductName",
  "price":   "NetPrice",
  "stock":   "QtyAvailable",
  "format":  "ProductGroup"
}
```

Only `barcode` and `stock` genuinely matter to the sync. The rest are carried
for the reports and for answering questions later.

A note on why the coercion helpers accept `None`: the column indices they are
fed come from this setting, so a mapping that names a column your file does not
have would otherwise raise **mid-parse**, hundreds of thousands of rows in.
That is the worst possible moment to discover a configuration error, so the
parser degrades to a rejected row instead.

If a required column is missing entirely, the header guardrail halts the run
before anything is decided. Turn `guardrail_require_known_header` off only if
you genuinely have a feed whose header varies between deliveries — you are
giving up a cheap check against "somebody put the wrong file in the folder".

### Barcode conventions

If your Amazon SKUs pad barcodes to a width other than 13, change
`CANONICAL_WIDTH` in `app/core/barcode.py`. That is the one constant.

If your SKUs do **not** embed the barcode at all — a supplier part number, an
internal code — then barcode matching is the wrong strategy for you and you
want `sku_overrides` (explicit mappings) as the primary source instead. The
matcher already treats manual overrides as the highest trust tier; you would be
making that the normal case rather than the exception.

Before changing anything, run the coverage tool against your own two files:

```bash
python scripts/stage0_coverage.py --feed YOUR_FEED.zip --listings report.txt
```

It will tell you what fraction matches, what naive concatenation would have
matched, and give you a sample of what failed. That is a much faster way to
understand your own data than reading code.

### Archive layout and filenames

`app/vendor/parser.py` expects a ZIP containing a single text member.
`app/vendor/filename.py` expects names shaped like:

```
<KIND>_FEED_<account>_<YYYYMMDD>[_<sequence>].zip
```

and extracts three things: whether the feed is **full or delta**, the date, and
a sequence number for ordering same-day deltas.

The full/delta distinction is not cosmetic. Absence from a full feed means
"the supplier dropped this product"; absence from a delta means "unchanged".
Getting that backwards is the single most dangerous change you can make here —
see [SAFETY-MODEL.md](SAFETY-MODEL.md#why-a-missing-feed-cannot-zero-a-catalogue).

If your supplier only ever sends full feeds, make the parser report every file
as full. If only deltas, **the dropped-product logic can never run**, so set
`missing_from_full_feeds` handling aside entirely; products will need removing
by another route.

Keep the date coming from the **filename** rather than from a server timestamp
if you possibly can. A modification time drifts with upload duration, retries
and the server's own clock; the filename is the supplier's own statement about
which day the data belongs to.

### Timezones

**Read this one carefully. It is the sharpest edge in the system.**

The date in a feed filename is in the **supplier's** calendar — not yours, not
your server's, and not UTC.

Set the `timezone` setting to **the supplier's timezone**. If you do not, a
feed published late in the supplier's day is already "tomorrow" in your zone,
so `process_only_today` judges it to be yesterday's file and **skips it — every
day, silently, with a run that looks completely healthy.**

There is no error anywhere. Every component reports success. The only symptom
is data that is quietly a day out of date.

`today_in()` and `is_from_today()` in `app/vendor/filename.py` take the zone as
a parameter, and there is exactly one place that decides what "today" means.
Keep it that way. A mistyped zone is refused on save rather than falling back
silently, because a fallback here recreates the skipped-feed bug from a single
missing letter.

If you write tests that build date-named fixture files, derive the date from
the `timezone` setting, not from UTC. Otherwise they pass all day and fail for
a few hours every night.

### SFTP instead of FTPS

Already supported. Set `VENDOR_FTP_MODE=sftp` and use port 22. `paramiko` is
already a dependency for exactly this, imported lazily so it costs nothing when
unused.

Plain `ftp` is refused unless you set `ALLOW_PLAINTEXT_FTP=true`, because it
puts the password on the wire in clear text. Explicit TLS is normally available
on the same port, so the downgrade usually buys nothing.

If FTPS fails with `CERTIFICATE_VERIFY_FAILED` against a server you are sure is
fine, read the two-trust-stores note in
[ARCHITECTURE.md](ARCHITECTURE.md#the-two-trust-stores-trap) before doing
anything else — and do not reach for disabling verification.

### A feed that is not a file at all

If your supplier offers an API rather than files, the seam is
`app/vendor/ftp_client.py`'s `connect()` context manager and the `RemoteFile`
shape it yields. The pipeline consumes an iterable of parsed rows and does not
otherwise care where they came from.

Keep the two-stage split: fetch everything first, release the connection, then
parse. It matters for the same reason with an HTTP session as with FTP.

---

## A different marketplace or region

### Same region, different marketplace

Change `MARKETPLACE_ID` in `.env`. The common values are published by Amazon:

| Marketplace | ID |
|---|---|
| US | `ATVPDKIKX0DER` |
| Canada | `A2EUQ1WTGCTBG2` |
| Mexico | `A1AM78C64UM0Y8` |
| UK | `A1F83G8C2ARO7P` |
| Germany | `A1PA6795UKMFR9` |

Check Amazon's current documentation rather than trusting a table in a README —
these do change.

### A different region

Change `sp_api_endpoint` in `app/config.py`:

| Region | Endpoint |
|---|---|
| North America | `sellingpartnerapi-na.amazon.com` |
| Europe | `sellingpartnerapi-eu.amazon.com` |
| Far East | `sellingpartnerapi-fe.amazon.com` |

The LWA token endpoint is global and does not change.

Your refresh token is tied to the region you authorised in, so moving region
means re-authorising. Re-read
[AMAZON-APP-SETUP.md](AMAZON-APP-SETUP.md) when you do — in particular, confirm
you are still requesting Product Listing and **not** Pricing.

### Selling in several marketplaces

Not supported as one installation, and the honest answer is that bolting it on
is a bad idea. Quantity is per-marketplace, the catalogue report is
per-marketplace, and the guardrail thresholds that are sensible for a large
marketplace are wrong for a small one.

Run separate installations with separate databases. It is less elegant and it
fails independently, which is the property you actually want.

---

## Things you should not adapt away

Stated plainly because each looks like a reasonable simplification.

**Do not remove the read-back verification** to save API calls. Amazon reports
success for writes it discards; without the read-back you have no idea what
actually happened.

**Do not move the `checkpoint()` before `send_batch`** to batch writes more
efficiently, and do not wrap a run in a single transaction. A crash during
sending then rolls back the rows undo needs.

**Do not add a price field**, anywhere, for any reason.

**Do not make the empty scope list mean "everything"** to match the usual
convention for empty filters. It means "nothing" deliberately, so a fresh
install cannot write to an account nobody has configured it for.

**Do not run production on SQLite.** The run lock is a PostgreSQL advisory
lock and is a no-op there, so two runs can overlap and send the same changes
twice. `config.py` refuses to start for this reason; removing the refusal does
not make it safe.

---

## Testing your adaptation

The order that will save you the most time:

1. **Coverage tool first**, offline, no credentials. If matching is poor, stop
   and fix that before anything else — everything downstream depends on it.
2. **Practice mode** (`sync_mode = dry_run`) for as long as it takes to stop
   being surprised. It runs every stage and writes nothing.
3. **Compare the proposals against your own judgement.** Take a sample from the
   reports and check them by hand. This is the test that actually matters, and
   it is the one most often skipped.
4. **Pilot** with a handful of whitelisted SKUs in `needs_approval` mode.
5. **Press Undo on purpose**, once, before you need it. See
   [OPERATIONS.md](OPERATIONS.md).
6. Widen the scope.

Add regression tests for your own feed's quirks — and prove them the way the
rest of the suite is proven: watch the test fail against the unfixed code
before you fix it. See [CONTRIBUTING.md](../CONTRIBUTING.md).
