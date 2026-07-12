# paperless-ngx-reconsume-plugin

**Full re-consume for [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) — turn "Reprocess" into a complete consume pipeline.**

paperless-ngx's built-in *Reprocess* action only redoes OCR and rebuilds the archive file. It deliberately skips everything else the original consume did: date detection, correspondent/type/tag matching, storage path assignment and workflows. If your documents were once consumed with bad OCR (e.g. `PAPERLESS_OCR_MODE=skip`), reprocessing fixes the text but leaves all the derived metadata stale — and nothing ever shows up in the *File tasks* view.

This plugin fixes that. After every successful reprocess it automatically runs:

1. **Date re-detection** from the OCR text **and the file name** — with a deterministic, **language-independent heuristic** that beats paperless' naive "first date wins" approach
2. **Classification / matching** — correspondent, document type, tags, storage path (same handlers the consumer uses)
3. **Search index** update
4. **Workflows** (`Document updated` by default)
5. **UI cache** invalidation
6. A **visible task entry** in the frontend *File tasks* view — walking the full *queued → started → finished* lifecycle (the minutes-long OCR shows as *started*, failures surface as *failed* instead of vanishing), with the same **open-document button** as native consume tasks and a compact **field diff** as its result
7. **Live WebSocket progress events** on paperless' own `status_updates` channel, so the frontend refreshes during bulk runs instead of looking frozen

All of it lives **completely outside the paperless source tree**. No core file is modified. Ever.

---

## How it works

```
"Reprocess" button (single or bulk)
        │  celery before_task_publish  →  PaperlessTask row: PENDING ("queued" tab)
        ▼
documents.tasks.update_document_content_maybe_archive_file   (paperless core, untouched)
        │  celery task_prerun          →  row: STARTED (visible for the whole OCR)
        │  celery task_postrun         →  on failure: row FAILURE + clear message
        ▼
reconsume hook  ── runs INLINE (~1 s, same worker slot) ──►  full_consume_steps
                       ├─ 1. date from OCR text + filename (scored heuristic)
                       ├─ 2. set_correspondent / set_document_type /
                       │      set_tags / set_storage_path
                       ├─ 3. add_to_index
                       ├─ 4. run_workflows (updated|added|none)
                       ├─ 5. clear document caches
                       └─ 6. row: SUCCESS + field diff + open-document button;
                              WebSocket push at every transition
```

**Coupling to paperless internals is a single string** — the name of the core reprocess task. If a future paperless version renames it, the hook simply never fires; nothing breaks. Every pipeline step is individually wrapped in `try/except` (fail-soft): if an internal API changes, that step logs an error and the rest continues.

**Normal consumption benefits too:** by default the plugin also replaces the stock first-match date detection during regular consumption of *new* documents (runtime patch of the consumer's `parse_date`, no file modified). Explicit dates supplied by the user bypass the detector entirely, so nothing you set manually is ever overridden. On any error the original detector answers. Disable with `RECONSUME_UPGRADE_CONSUME_DATE=false`.

## The date heuristic

paperless picks the **first** regex match in the document — which is frequently a birth date, a footer date, or a referenced old year. This plugin scores **every** date candidate instead, using only **structural, language-independent evidence** (no hardcoded keywords in any language):

| Signal                                                                                              | Score                |
| ---------------------------------------------------------------------------------------------------- | -------------------- |
| `:` directly before the date (label syntax in any script: `Datum:`, `Date:`, `日付：`)              | +20                  |
| `,` directly before (letter-head style `<City>, <date>`)                                            | +10                  |
| `,` plus one short word (≤4 letters) before — `<City>, den <date>`, `<City>, le <date>`             | +10                  |
| Position in the first ~1200 chars (top of page 1)                                                   | +20 (+10 more < 400) |
| **Paragraph isolation** — the date sits in its own blank-line-delimited block                       | +35                  |
| **Line isolation** — (nearly) alone on its text line inside a busy block, when no label already matched | +20              |
| Deeply embedded in running prose (paragraph ≥300 extra chars)                                       | −10                  |
| Distinct repetition **clusters** of the same date (nearby occurrences collapse into one)            | +10…+20              |
| One-off date **preceding** the first occurrence of a dominant repeated date (year consistent with any strong filename date) | +40 |
| Confirmed by a **strong filename date** (same day, or same year+month for a month-precision name)   | +35                  |
| Digit/spec blob (`AM4/1151/1150/1155`), identifier-glued (`ISA-25.11.2017`), barcode-fenced (`*13.05.26*`), or **parenthesized** (`(Fassung Jan. 2015)`) | −40, excluded from repetition & anchor |
| Full date whose day-of-month is **1** (`zum 01.10.2016` — effective/cut-off day prior)              | −15                  |
| Partial date — month/year only, no explicit day (`11.2016`, `Oktober 2016`)                         | −10                  |
| Bare year in date-shaped disguise (word+year whose "word" validated as not a month)                 | −25 more             |
| More than 6 years older than the newest clean date in the document (old references)                 | −35                  |
| More than 15 years older — birth-date territory                                                     | −60 more             |

Partial dates resolve to the **last day of that month** (`Oktober 2016` → `2016-10-31`), calendar-aware including February and leap years (`Februar 2024` → `2024-02-29`). Whether a day is present is detected structurally (digit-group counting), not by language.

**Year-only fallback:** if no month/day candidate parses anywhere in the document or filename, standalone **past** years resolve to **Dec 31 of that year** — a yearly tax statement mentioning only `2019` gets `2019-12-31`. Multiple years compete through the same structural scoring (frequency, position). The current year is excluded, since its Dec 31 lies in the future.

**Filename dates:** a structured filename (`2019.03.04_Shop.pdf`, `BAföG_2018_04-1.pdf`, `Abrechnung_08_2019.pdf`) is extracted deterministically as YMD — a leading 4-digit year fixes the field order without any locale guessing. A text-content date confirmed by a strong filename date gets a large bonus; scanner-timestamp-shaped filenames (`20250725_140140_006519`) are recognized and treated as weak, last-resort evidence only.

**Repetition is cluster-aware, not a raw count:** occurrences of the same date within roughly 200 characters of each other collapse into one "cluster" — three adjacent line items restating the same date count as one piece of evidence, not three. This also feeds a **"precedes the dominant repeat" bonus**: a non-repeated date that appears earlier in the document than the first occurrence of a clearly dominant, repeated date gets a bonus — defending a letter's own one-off dateline against a deadline or effective-date phrase restated several times in the body (`"the changes take effect on March 15" ×5` should not outscore the letter's own header date, mentioned once).

**Bare years in date-shaped disguise** (a word+year match whose "word" validates as NOT a real month — a coverage-period tag like `"für 2023"` used as a page watermark) are recognized and scored as weaker evidence than genuine month/day precision, so they only win when nothing better exists in the document (unlike a real Dec-31 fallback case, e.g. `"Lohnsteuerbescheinigung für 2016"` with no other date at all).

**Very old dates are crushed, not just discounted:** a date more than ~15 years older than the newest clean candidate in the document (birth-date territory) gets a decisive penalty — strong enough that even a weak, last-resort candidate elsewhere in the document will still beat a birth date, in a structured form where every field (including "Geburtsdatum") is individually well-formatted and isolated.

**Barcode/reference-block and parenthesized dates are recognized as noise**, not just digit-glued ones: a date fenced by `*`/`#`/`|` characters (`*13.05.26*` inside a mail-barcode line) or immediately enclosed in parentheses (`(Fassung Jan. 2015)` form-version tags, law citations, period details) is excluded from competing at all — parentheses are the universal typographic marker for secondary information, never a document's own dateline.

Candidate extraction combines paperless' own `DATE_REGEX` with a generic textual-date pattern (any unicode letters, generic day suffixes) so formats like `October 2nd, 2022` or `2. Oktober 2022` are found regardless of your OCR language. Parsing is done by [`dateparser`](https://github.com/scrapinghub/dateparser) — first with your configured paperless locale settings, then with full auto-detection (~200 languages) as fallback. Fully deterministic: same input → same output.

Ties break by score, then filename candidates beat text candidates, then earliest position. If no candidate is found, the document's date is left untouched.

---

## Installation

### Requirements

- paperless-ngx ≥ 2.x (developed & tested against 2.20.x, Celery 5.5)
- No extra Python dependencies — the plugin only uses what paperless already ships

### Option A: Docker / docker-compose

The official paperless-ngx image runs webserver, consumer, worker and scheduler in one container, so a single volume mount + two environment variables is all you need.

1. Clone this repository next to your `docker-compose.yml`:

   ```bash
   git clone https://github.com/jxsl13/paperless-ngx-reconsume-plugin.git
   ```

2. Add the mount and the environment variables to the `webserver` service:

   ```yaml
   services:
     webserver:
       image: ghcr.io/paperless-ngx/paperless-ngx:latest
       # ... your existing config ...
       volumes:
         # ... your existing volumes ...
         - ./paperless-ngx-reconsume-plugin/reconsume:/usr/src/paperless/plugins/reconsume:ro
       environment:
         # ... your existing environment ...
         PAPERLESS_APPS: "reconsume"
         PYTHONPATH: "/usr/src/paperless/plugins"
   ```

   > `PAPERLESS_APPS` is an [official paperless setting](https://docs.paperless-ngx.com/configuration/) that appends Django apps to `INSTALLED_APPS`. `PYTHONPATH` makes the mounted directory importable. The `:ro` mount keeps the plugin read-only inside the container.

3. Recreate the container:

   ```bash
   docker compose up -d
   ```

4. Verify (see [Verifying](#verifying-the-installation) below).

**Updating paperless:** just pull the new image as usual. The plugin is a read-only mount — it survives every update. **Removing:** delete the volume line and the two environment variables, `docker compose up -d`.

### Option B: Bare metal / LXC (systemd)

For installs where paperless runs via systemd units (e.g. in a Proxmox LXC container, `/opt/paperless` layout):

1. Clone the plugin **outside** the paperless source tree:

   ```bash
   git clone https://github.com/jxsl13/paperless-ngx-reconsume-plugin.git /opt/paperless/plugins-repo
   ln -s /opt/paperless/plugins-repo/reconsume /opt/paperless/plugins/reconsume
   # or simply copy: mkdir -p /opt/paperless/plugins && cp -r /opt/paperless/plugins-repo/reconsume /opt/paperless/plugins/
   ```

2. Register the app in your `paperless.conf`:

   ```ini
   PAPERLESS_APPS=reconsume
   ```

3. Give every paperless service the import path via systemd drop-ins (survives paperless updates, no unit file is edited):

   ```bash
   for s in webserver consumer scheduler task-queue; do
     mkdir -p /etc/systemd/system/paperless-$s.service.d
     printf '[Service]\nEnvironment=PYTHONPATH=/opt/paperless/plugins\n' \
       > /etc/systemd/system/paperless-$s.service.d/reconsume.conf
   done
   systemctl daemon-reload
   systemctl restart paperless-webserver paperless-consumer paperless-scheduler paperless-task-queue
   ```

   (Adjust the service names if yours differ.) The included [`install-lxc.sh`](install-lxc.sh) automates these steps.

**Removing:** delete the `PAPERLESS_APPS` line, remove the drop-ins, `systemctl daemon-reload`, restart. No trace remains.

---

## Configuration

All optional, set as environment variables (docker) or in `paperless.conf` (bare metal):

| Variable                   | Default     | Meaning                                                                                                                                |
| -------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `RECONSUME_SET_CREATED`    | `true`      | Re-detect the document date from OCR text                                                                                              |
| `RECONSUME_DATE_STRATEGY`  | `heuristic` | `heuristic` = scored selection (see above) · `first` = paperless' original first-match behaviour                                       |
| `RECONSUME_REPLACE`        | `false`     | `true` = matching may overwrite existing correspondent/type/tags · `false` = only fill empty fields (like consume on a fresh document) |
| `RECONSUME_ADD_INBOX_TAGS` | `false`     | Re-add inbox tags, exactly like a fresh consume                                                                                        |
| `RECONSUME_RUN_WORKFLOWS`  | `updated`   | Which workflow trigger to fire: `added` · `updated` · `none`                                                                           |
| `RECONSUME_UPGRADE_CONSUME_DATE` | `true` | Also use the scored date heuristic during **normal consumption** of new documents (replaces the stock first-match `parse_date` at runtime, fail-soft). User-supplied dates always win — the consumer only calls the detector when no explicit date was provided. |
| `RECONSUME_RELIABLE_REPROCESS` | `true` | Make reprocess survive worker kills: `acks_late` + `reject_on_worker_lost` on the reprocess chain, prefetch 1. Interrupted tasks are redelivered automatically — immediately on clean shutdown, after the redis visibility timeout (default 1 h) on a hard kill. |

The post-OCR pipeline always runs **inline**, directly after each reprocess in the same worker slot (~1 s). Queued dispatch would starve behind every still-pending OCR during bulk runs, leaving all documents without date/matching until the whole batch drained.

### Reliability & memory for big queues

paperless' celery defaults **ack tasks before execution** — a task killed mid-OCR (worker restart, OOM, crash) is silently lost, and up to `4 × workers` prefetched queue entries die with it. With `RECONSUME_RELIABLE_REPROCESS` (default on) the plugin flips the reprocess chain to late acknowledgement, so interrupted work **resumes automatically**. Both tasks are idempotent; a redelivered run just redoes the same OCR.

For memory, mass-reprocessing with `force` is the worst case (every page rasterized). Recommended paperless settings for bounded memory:

```ini
PAPERLESS_TASK_WORKERS=4              # parallel OCRs — size to RAM/1.2GB, not to cores
PAPERLESS_CONVERT_MEMORY_LIMIT=256    # ImageMagick spills to disk beyond this (MiB)
PAPERLESS_OCR_MAX_IMAGE_PIXELS=89478485   # skip decompression-bomb images
```

Plus a systemd guard so a runaway worker can never take down redis/postgres/webserver in the same container (drop-in for the task-queue unit):

```ini
[Service]
MemoryHigh=4G     # kernel throttles the worker here
MemoryMax=5G      # hard cap — worker is killed, task redelivers, rest survives
```

With `acks_late` active, even a `MemoryMax` kill is self-healing: the task redelivers and, with prefetch 1 and fewer parallel OCRs, usually succeeds on the second attempt.

## Verifying the installation

After a restart, the worker should list the plugin task:

```bash
# docker
docker compose logs webserver | grep reconsume
# systemd
journalctl -u paperless-task-queue | grep reconsume
```

You should see `reconsume.tasks.full_consume_steps` in the celery task list at boot. Then select any document → **Reprocess**. Within seconds the log shows:

```
reconsume: reprocess of document 123 finished, running full consume steps
reconsume doc 123: created 2025-11-08→2022-10-02 | corr ∅→"Gumroad, Inc." | type =Invoice | path =∅ | tags +tax | [idx wf:updated cache]
```

The run also appears as `Reconsume: <title>` in the frontend **File tasks** view, with the same diff as its result.

### Result diff notation

Each run reports one compact line describing everything it detected and changed:

| Notation                 | Meaning                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `field old→new`          | value changed (e.g. `created 2025-11-08→2022-10-02`)               |
| `field =value`           | detected / kept — unchanged                                        |
| `∅`                      | empty / not set                                                    |
| `tags +a -b`             | tags added / removed (`tags =` — no change)                        |
| `[idx wf:updated cache]` | housekeeping steps that ran: search index, workflows, cache clear  |
| `!step`                  | that step failed (fail-soft; details in the log)                   |

Fields: `created` (document date), `corr` (correspondent), `type` (document type), `path` (storage path), `tags`. Values containing spaces are quoted; long values are truncated with `…`.

## OCR-mode pitfalls (read this if reprocessing "does nothing")

The plugin can only work with the text that paperless' reprocess produces. Whether reprocess actually generates *new* text is governed by the OCR mode:

| Mode | Behaviour on reprocess |
|---|---|
| `skip` | pages that already have a text layer are skipped — reprocess is a **no-op** for previously OCR'd documents |
| `redo` | replaces existing *OCR* text layers, but does **not** touch pages it considers "born-digital" text — including PDFs with **corrupt embedded text layers** (broken font encodings that extract as garbage like `r§€N7€CEßDECjj7€`) |
| `force` | rasterizes every page and OCRs from scratch — the only mode that fixes garbage text layers |

### Pitfall 1: the frontend configuration silently overrides your config file

paperless has **two** configuration sources, and the database wins:

```
frontend "Configuration" page  (stored in the DB)   ← takes precedence
PAPERLESS_OCR_MODE             (paperless.conf / env)
```

If an OCR mode was ever saved on the frontend **Configuration** page, changing `PAPERLESS_OCR_MODE` in `paperless.conf` or docker environment **has no effect** — the DB value silently wins, with no warning anywhere.

So to re-OCR documents with broken text layers, set the mode where it actually counts: **frontend → Configuration → OCR → Mode → `force`** (admin permissions required). Alternatively clear the frontend field (set it to empty/default) so your config file applies again — you can verify the effective value with:

```bash
# inside the paperless environment
python3 manage.py shell -c "from paperless.config import OcrConfig; print(OcrConfig().mode)"
```

### Pitfall 2: don't leave `force` on permanently

`force` rasterizes born-digital PDFs too, degrading their perfect vector text to OCR quality. Recommended procedure:

1. Set mode to `force` (frontend Configuration page, see pitfall 1)
2. Reprocess the affected documents (garbage-content documents typically show `created =…| corr =∅ | type =∅ | tags =` in the reconsume result — nothing detectable in garbage)
3. Switch back to `redo` afterwards

## Troubleshooting

| Symptom                             | Cause / fix                                                                                                                         |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Webserver won't start after install | `PYTHONPATH` not visible to the service → check the drop-in / compose env; `python3 -c "import reconsume"` with that path must work |
| Hook never fires                    | Task name changed in a newer paperless (check `REPROCESS_TASK` in `apps.py` against `documents/tasks.py`) — adjust the one string   |
| Date not updated on some document   | No parseable date candidate in the OCR text (check with `journalctl … | grep reconsume`); the plugin never guesses                  |
| Reprocess changes nothing, content stays garbage | Corrupt embedded text layer + OCR mode effectively `skip`/`redo` — set `force` on the **frontend Configuration page** (it overrides the config file!), reprocess, then revert. See [OCR-mode pitfalls](#ocr-mode-pitfalls-read-this-if-reprocessing-does-nothing) |
| A step logs an exception            | That step is skipped for that document, everything else still runs — fail-soft by design                                            |

## Update safety

- **Zero core edits** — nothing under the paperless installation is modified
- Loaded through `PAPERLESS_APPS`, paperless' official extension point
- Uses stable Celery APIs (`task_postrun`, `shared_task`) for the hook
- paperless-internal calls (`parse_date` machinery, matching handlers, signals) are each isolated; a breaking change in one degrades exactly one step and logs loudly
- Tested against paperless-ngx **2.20.x**; on major upgrades (e.g. 3.0), reprocess one test document and check the log before bulk runs

## License

[MIT](LICENSE)
