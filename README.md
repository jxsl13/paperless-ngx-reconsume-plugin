# paperless-ngx-reconsume-plugin

**Full re-consume for [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) — turn "Reprocess" into a complete consume pipeline.**

paperless-ngx's built-in *Reprocess* action only redoes OCR and rebuilds the archive file. It deliberately skips everything else the original consume did: date detection, correspondent/type/tag matching, storage path assignment and workflows. If your documents were once consumed with bad OCR (e.g. `PAPERLESS_OCR_MODE=skip`), reprocessing fixes the text but leaves all the derived metadata stale — and nothing ever shows up in the *File tasks* view.

This plugin fixes that. After every successful reprocess it automatically runs:

1. **Date re-detection** from the OCR text — with a deterministic, **language-independent heuristic** that beats paperless' naive "first date wins" approach
2. **Classification / matching** — correspondent, document type, tags, storage path (same handlers the consumer uses)
3. **Search index** update
4. **Workflows** (`Document updated` by default)
5. **UI cache** invalidation
6. A **visible task entry** in the frontend *File tasks* view

All of it lives **completely outside the paperless source tree**. No core file is modified. Ever.

---

## How it works

```
"Reprocess" button (single or bulk)
        │
        ▼
documents.tasks.update_document_content_maybe_archive_file   (paperless core, untouched)
        │  celery task_postrun signal (stable Celery API)
        ▼
reconsume hook  ──►  reconsume.tasks.full_consume_steps
                       ├─ 1. date from OCR text (scored heuristic)
                       ├─ 2. set_correspondent / set_document_type /
                       │      set_tags / set_storage_path
                       ├─ 3. add_to_index
                       ├─ 4. run_workflows (updated|added|none)
                       ├─ 5. clear document caches
                       └─ 6. PaperlessTask row → visible in "File tasks"
```

**Coupling to paperless internals is a single string** — the name of the core reprocess task. If a future paperless version renames it, the hook simply never fires; nothing breaks. Every pipeline step is individually wrapped in `try/except` (fail-soft): if an internal API changes, that step logs an error and the rest continues.

## The date heuristic

paperless picks the **first** regex match in the document — which is frequently a birth date, a footer date, or a referenced old year. This plugin scores **every** date candidate instead, using only **structural, language-independent evidence** (no hardcoded keywords in any language):

| Signal                                                                                           | Score                 |
| ------------------------------------------------------------------------------------------------ | --------------------- |
| `:` directly before the date (label syntax in any script: `Datum:`, `Date:`, `日付：`)           | +20                   |
| `,` directly before (letter-head style `<City>, <date>`)                                         | +10                   |
| Position in the first ~1200 chars (top of page 1)                                                | +20 (+10 more < 400)  |
| Same calendar date repeated in the document                                                      | +10…+20               |
| Embedded in a digit/spec blob (`AM4/1151/1150/1155` → phantom dates)                             | −40                   |
| January 1 (artifact of month/year-only matches)                                                  | −25 (other day-1: −5) |
| More than 6 years older than the newest clean date in the document (old references, birth dates) | −35                   |

Candidate extraction combines paperless' own `DATE_REGEX` with a generic textual-date pattern (any unicode letters, generic day suffixes) so formats like `October 2nd, 2022` or `2. Oktober 2022` are found regardless of your OCR language. Parsing is done by [`dateparser`](https://github.com/scrapinghub/dateparser) — first with your configured paperless locale settings, then with full auto-detection (~200 languages) as fallback. Fully deterministic: same input → same output.

Ties break by score, then by earliest position. If no candidate is found, the document's date is left untouched.

---

## Installation

### Requirements

- paperless-ngx ≥ 2.x (developed & tested against 2.20.x, Celery 5.5)
- No extra Python dependencies — the plugin only uses what paperless already ships

### Option A: Docker / docker-compose

The official paperless-ngx image runs webserver, consumer, worker and scheduler in one container, so a single volume mount + two environment variables is all you need.

1. Clone this repository next to your `docker-compose.yml`:

   ```bash
   git clone https://github.com/<you>/paperless-ngx-reconsume-plugin.git
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
   git clone https://github.com/<you>/paperless-ngx-reconsume-plugin.git /opt/paperless/plugins-repo
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
reconsume: reprocess of document 123 finished, queued full consume steps
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

## Troubleshooting

| Symptom                             | Cause / fix                                                                                                                         |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Webserver won't start after install | `PYTHONPATH` not visible to the service → check the drop-in / compose env; `python3 -c "import reconsume"` with that path must work |
| Hook never fires                    | Task name changed in a newer paperless (check `REPROCESS_TASK` in `apps.py` against `documents/tasks.py`) — adjust the one string   |
| Date not updated on some document   | No parseable date candidate in the OCR text (check with `journalctl … | grep reconsume`); the plugin never guesses                  |
| A step logs an exception            | That step is skipped for that document, everything else still runs — fail-soft by design                                            |

## Update safety

- **Zero core edits** — nothing under the paperless installation is modified
- Loaded through `PAPERLESS_APPS`, paperless' official extension point
- Uses stable Celery APIs (`task_postrun`, `shared_task`) for the hook
- paperless-internal calls (`parse_date` machinery, matching handlers, signals) are each isolated; a breaking change in one degrades exactly one step and logs loudly
- Tested against paperless-ngx **2.20.x**; on major upgrades (e.g. 3.0), reprocess one test document and check the log before bulk runs

## License

[MIT](LICENSE)
