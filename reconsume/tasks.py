"""
Follow-up task that re-runs the post-consume pipeline for an existing
document — exactly the part the core reprocess task deliberately skips.

Every step is fail-soft: if a paperless-internal API changes in a future
release, the step logs an error and the rest continues.

Config (env vars, all optional):
  RECONSUME_SET_CREATED      default true      re-detect created date from OCR text
  RECONSUME_DATE_STRATEGY    default heuristic  heuristic = scored candidates
                                                (see dating.py) | first = paperless'
                                                original first-match behaviour
  RECONSUME_REPLACE          default false     overwrite existing correspondent/
                                               type/tags (false = fill empty only)
  RECONSUME_ADD_INBOX_TAGS   default false     re-add inbox tags like a fresh consume
  RECONSUME_RUN_WORKFLOWS    default updated   one of: added | updated | none
"""

import datetime
import logging
import os
import uuid

from celery import shared_task

logger = logging.getLogger("paperless.reconsume")


def _flag(name, default):
    return os.getenv(name, "true" if default else "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def detect_date(document):
    """Return the re-detected created date (datetime.date) or None."""
    strategy = os.getenv("RECONSUME_DATE_STRATEGY", "heuristic").strip().lower()

    if strategy == "first":
        from documents.parsers import parse_date

        d = parse_date(document.original_filename or "", document.content or "")
        if isinstance(d, datetime.datetime):
            d = d.date()
        return d

    # default: scored heuristic
    from reconsume.dating import best_date, paperless_parse_one

    return best_date(
        document.original_filename or "",
        document.content or "",
        paperless_parse_one(),
    )


def create_pending_task_row(task_id, document_id):
    """
    Create the PaperlessTask row when the core REPROCESS task is queued
    (before_task_publish), so one row tracks the whole chain in the
    frontend "File tasks" view: queued (PENDING) while waiting, started
    (STARTED) during the — potentially minutes-long — OCR, finished when
    the follow-up completes. Fail-soft.
    """
    try:
        from celery import states
        from documents.models import Document, PaperlessTask

        document = Document.objects.get(pk=document_id)
        row, _created = PaperlessTask.objects.get_or_create(
            task_id=task_id,
            defaults={
                "task_name": PaperlessTask.TaskName.CONSUME_FILE,
                "type": PaperlessTask.TaskType.AUTO,
                "status": states.PENDING,
                "task_file_name": f"Reconsume: {document.title or document.pk}",
                "owner": document.owner,
            },
        )
        return row
    except Exception:
        logger.debug("reconsume: could not create PaperlessTask row", exc_info=True)
        return None


def mark_task_row_started(task_id):
    """Reprocess execution begins: PENDING -> STARTED. Fail-soft."""
    try:
        from celery import states
        from django.utils import timezone
        from documents.models import PaperlessTask

        PaperlessTask.objects.filter(task_id=task_id, status=states.PENDING).update(
            status=states.STARTED,
            date_started=timezone.now(),
        )
    except Exception:
        logger.debug("reconsume: could not mark row started", exc_info=True)


def fail_task_row(task_id, message):
    """Core reprocess failed: finalize the row as FAILURE. Fail-soft."""
    try:
        from celery import states
        from django.utils import timezone
        from documents.models import PaperlessTask

        PaperlessTask.objects.filter(task_id=task_id).update(
            status=states.FAILURE,
            date_done=timezone.now(),
            result=message,
        )
    except Exception:
        logger.debug("reconsume: could not fail row", exc_info=True)


def _open_task_row(task_row_id, fallback_task_id, document):
    """
    Take over the chain row created at reprocess-publish time (it is
    already STARTED while the OCR ran). If it does not exist (follow-up
    dispatched without the publish hook, e.g. manually), create one on the
    fly. Fail-soft.
    """
    try:
        from celery import states
        from django.utils import timezone
        from documents.models import PaperlessTask

        row = None
        if task_row_id:
            row = PaperlessTask.objects.filter(task_id=task_row_id).first()
        if row is None:
            row = PaperlessTask.objects.create(
                task_id=task_row_id or fallback_task_id or str(uuid.uuid4()),
                task_name=PaperlessTask.TaskName.CONSUME_FILE,
                type=PaperlessTask.TaskType.AUTO,
                status=states.STARTED,
                date_started=timezone.now(),
                task_file_name=f"Reconsume: {document.title or document.pk}",
                owner=document.owner,
            )
        elif row.status != states.STARTED:
            row.status = states.STARTED
            if row.date_started is None:
                row.date_started = timezone.now()
            row.save(update_fields=["status", "date_started"])
        return row
    except Exception:
        logger.debug("reconsume: could not open PaperlessTask row", exc_info=True)
        return None


def _fmt_val(v):
    """Compact value formatting for the diff notation."""
    if v in (None, ""):
        return "∅"
    s = str(v)
    if len(s) > 40:
        s = s[:39] + "…"
    return f'"{s}"' if " " in s else s


def _snapshot(document):
    """Capture the diff-relevant fields of a document."""
    return {
        "created": document.created,
        "corr": document.correspondent.name if document.correspondent else None,
        "type": document.document_type.name if document.document_type else None,
        "path": document.storage_path.name if document.storage_path else None,
        "tags": set(document.tags.values_list("name", flat=True)),
    }


def _diff_result(document_id, before, after, extras):
    """
    Build the one-line result diff. Notation:
      field old→new   changed
      field =value    detected / kept, unchanged (∅ = empty)
      tags +a -b      tags added/removed (= if unchanged)
      [idx wf:… cache]  housekeeping steps that ran; !step = step failed
    """
    parts = []
    for key, label in (
        ("created", "created"),
        ("corr", "corr"),
        ("type", "type"),
        ("path", "path"),
    ):
        b, a = before[key], after[key]
        if str(b) != str(a):
            parts.append(f"{label} {_fmt_val(b)}→{_fmt_val(a)}")
        else:
            parts.append(f"{label} ={_fmt_val(a)}")
    added = sorted(after["tags"] - before["tags"])
    removed = sorted(before["tags"] - after["tags"])
    tag_bits = [f"+{_fmt_val(t)}" for t in added] + [f"-{_fmt_val(t)}" for t in removed]
    parts.append("tags " + (" ".join(tag_bits) if tag_bits else "="))
    return (
        f"doc {document_id}: "
        + " | ".join(parts)
        + " ["
        + " ".join(extras)
        + "]"
    )


def _close_task_row(row, ok, result):
    if row is None:
        return
    try:
        from celery import states
        from django.utils import timezone

        row.status = states.SUCCESS if ok else states.FAILURE
        row.date_done = timezone.now()
        row.result = result
        row.save(update_fields=["status", "date_done", "result"])
    except Exception:
        logger.debug("reconsume: could not update PaperlessTask row", exc_info=True)


@shared_task(bind=True)
def full_consume_steps(self, document_id, task_row_id=None):
    from documents.classifier import load_classifier
    from documents.models import Document
    from documents.signals import handlers

    set_created = _flag("RECONSUME_SET_CREATED", True)
    replace = _flag("RECONSUME_REPLACE", False)
    add_inbox = _flag("RECONSUME_ADD_INBOX_TAGS", False)
    workflows = os.getenv("RECONSUME_RUN_WORKFLOWS", "updated").strip().lower()

    document = Document.objects.get(pk=document_id)
    logging_group = uuid.uuid4()
    before = _snapshot(document)
    extras = []
    task_row = _open_task_row(
        task_row_id, getattr(self.request, "id", None), document
    )

    # -- 1. re-detect the created date from OCR content -----------------
    if set_created:
        try:
            detected = detect_date(document)
            if detected is not None and detected != document.created:
                document.created = detected
                # save() (not .update()) so filename handling stays
                # consistent, same as an edit through the API
                document.save(update_fields=["created"])
        except Exception:
            extras.append("!created")
            logger.exception(
                "reconsume: date re-detection failed for document %s",
                document_id,
            )

    # -- 2. classification / matching (same handlers consume runs) ------
    classifier = None
    try:
        classifier = load_classifier()
    except Exception:
        logger.exception("reconsume: could not load classifier")

    for name, fn in (
        ("correspondent", "set_correspondent"),
        ("document_type", "set_document_type"),
        ("tags", "set_tags"),
        ("storage_path", "set_storage_path"),
    ):
        try:
            getattr(handlers, fn)(
                sender=None,
                document=document,
                logging_group=logging_group,
                classifier=classifier,
                replace=replace,
            )
        except Exception:
            extras.append(f"!{name}")
            logger.exception(
                "reconsume: %s failed for document %s", fn, document_id
            )

    # -- 3. optional: inbox tags (exactly like a fresh consume) ---------
    if add_inbox:
        try:
            handlers.add_inbox_tags(
                sender=None, document=document, logging_group=logging_group
            )
        except Exception:
            extras.append("!inbox")
            logger.exception(
                "reconsume: add_inbox_tags failed for document %s", document_id
            )

    # -- 4. search index -------------------------------------------------
    try:
        handlers.add_to_index(sender=None, document=document)
        extras.append("idx")
    except Exception:
        extras.append("!idx")
        logger.exception(
            "reconsume: index update failed for document %s", document_id
        )

    # -- 5. workflows ------------------------------------------------------
    try:
        if workflows == "added":
            handlers.run_workflows_added(
                sender=None, document=document, logging_group=logging_group
            )
            extras.append("wf:added")
        elif workflows == "updated":
            handlers.run_workflows_updated(
                sender=None, document=document, logging_group=logging_group
            )
            extras.append("wf:updated")
    except Exception:
        extras.append("!wf")
        logger.exception(
            "reconsume: workflows failed for document %s", document_id
        )

    # -- 6. clear per-document UI caches ---------------------------------
    try:
        from documents.caching import clear_document_caches

        clear_document_caches(document.pk)
        extras.append("cache")
    except Exception:
        logger.debug("reconsume: cache clearing unavailable", exc_info=True)

    # -- result: compact diff of everything detected/changed -------------
    after = _snapshot(Document.objects.get(pk=document_id))
    result = _diff_result(document_id, before, after, extras)
    logger.info("reconsume %s", result)
    _close_task_row(task_row, ok=True, result=result)
    return result
