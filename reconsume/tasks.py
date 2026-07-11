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


def _open_task_row(task_id, document):
    """Create a PaperlessTask row so the run shows up in the frontend
    "Dateiaufgaben" view (which lists exactly this table). Fail-soft."""
    try:
        from celery import states
        from django.utils import timezone
        from documents.models import PaperlessTask

        return PaperlessTask.objects.create(
            task_id=task_id or str(uuid.uuid4()),
            task_name=PaperlessTask.TaskName.CONSUME_FILE,
            type=PaperlessTask.TaskType.AUTO,
            status=states.STARTED,
            date_started=timezone.now(),
            task_file_name=f"Reconsume: {document.title or document.pk}",
            owner=document.owner,
        )
    except Exception:
        logger.debug("reconsume: could not create PaperlessTask row", exc_info=True)
        return None


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
def full_consume_steps(self, document_id):
    from documents.classifier import load_classifier
    from documents.models import Document
    from documents.signals import handlers

    set_created = _flag("RECONSUME_SET_CREATED", True)
    replace = _flag("RECONSUME_REPLACE", False)
    add_inbox = _flag("RECONSUME_ADD_INBOX_TAGS", False)
    workflows = os.getenv("RECONSUME_RUN_WORKFLOWS", "updated").strip().lower()

    document = Document.objects.get(pk=document_id)
    logging_group = uuid.uuid4()
    summary = []
    task_row = _open_task_row(getattr(self.request, "id", None), document)

    # -- 1. re-detect the created date from OCR content -----------------
    if set_created:
        try:
            detected = detect_date(document)
            if detected is not None and detected != document.created:
                old = document.created
                document.created = detected
                # save() (not .update()) so filename handling stays
                # consistent, same as an edit through the API
                document.save(update_fields=["created"])
                summary.append(f"created {old} -> {detected}")
        except Exception:
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
            summary.append(name)
        except Exception:
            logger.exception(
                "reconsume: %s failed for document %s", fn, document_id
            )

    # -- 3. optional: inbox tags (exactly like a fresh consume) ---------
    if add_inbox:
        try:
            handlers.add_inbox_tags(
                sender=None, document=document, logging_group=logging_group
            )
            summary.append("inbox_tags")
        except Exception:
            logger.exception(
                "reconsume: add_inbox_tags failed for document %s", document_id
            )

    # -- 4. search index -------------------------------------------------
    try:
        handlers.add_to_index(sender=None, document=document)
        summary.append("index")
    except Exception:
        logger.exception(
            "reconsume: index update failed for document %s", document_id
        )

    # -- 5. workflows ------------------------------------------------------
    try:
        if workflows == "added":
            handlers.run_workflows_added(
                sender=None, document=document, logging_group=logging_group
            )
            summary.append("workflows(added)")
        elif workflows == "updated":
            handlers.run_workflows_updated(
                sender=None, document=document, logging_group=logging_group
            )
            summary.append("workflows(updated)")
    except Exception:
        logger.exception(
            "reconsume: workflows failed for document %s", document_id
        )

    # -- 6. clear per-document UI caches ---------------------------------
    try:
        from documents.caching import clear_document_caches

        clear_document_caches(document.pk)
        summary.append("caches")
    except Exception:
        logger.debug("reconsume: cache clearing unavailable", exc_info=True)

    result = f"reconsume document {document_id}: " + ", ".join(summary)
    logger.info(result)
    _close_task_row(task_row, ok=True, result=result)
    return result
