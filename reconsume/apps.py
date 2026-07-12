"""
reconsume — post-OCR hook for paperless-ngx.

Listens on celery's task_postrun signal. Whenever the core reprocess task
(documents.tasks.update_document_content_maybe_archive_file) finishes
successfully, it queues a follow-up task that re-runs the full post-consume
pipeline (date parsing, matching, index, workflows) for that document.

Lives entirely outside the paperless source tree; loaded via PAPERLESS_APPS.
If paperless ever renames the reprocess task, this hook simply never fires —
nothing in core can break because of this app.
"""

import logging
import os

from django.apps import AppConfig

logger = logging.getLogger("paperless.reconsume")

# The only coupling to paperless internals: the task name, as a string.
REPROCESS_TASK = "documents.tasks.update_document_content_maybe_archive_file"


class ReconsumeConfig(AppConfig):
    name = "reconsume"
    verbose_name = "Reconsume (full consume steps after re-OCR)"

    def ready(self):
        # Import at startup so the celery worker registers the task.
        from reconsume import tasks as _tasks  # noqa: F401

        from celery import states
        from celery.signals import before_task_publish, task_postrun, task_prerun

        # ONE PaperlessTask row tracks the whole chain (reprocess + follow-up):
        #   before_task_publish -> PENDING  ("queued" tab, while waiting)
        #   task_prerun         -> STARTED  ("started" tab, during the OCR,
        #                                    which can take minutes with force)
        #   follow-up task      -> SUCCESS with the field diff / FAILURE

        @before_task_publish.connect(weak=False)
        def _on_reprocess_queued(sender=None, headers=None, body=None, **_):
            try:
                if sender != REPROCESS_TASK:
                    return
                task_id = (headers or {}).get("id")
                doc_id = None
                try:
                    args, task_kwargs = body[0], body[1]
                    doc_id = (task_kwargs or {}).get("document_id")
                    if doc_id is None and args:
                        doc_id = args[0]
                except Exception:
                    doc_id = None
                if task_id and doc_id is not None:
                    from reconsume.tasks import create_pending_task_row

                    create_pending_task_row(task_id, doc_id)
            except Exception:
                logger.exception("reconsume: publish hook failed (ignored)")

        @task_prerun.connect(weak=False)
        def _on_reprocess_started(sender=None, task_id=None, **_):
            try:
                if sender is None or getattr(sender, "name", None) != REPROCESS_TASK:
                    return
                from reconsume.tasks import mark_task_row_started

                mark_task_row_started(task_id)
            except Exception:
                logger.exception("reconsume: prerun hook failed (ignored)")

        @task_postrun.connect(weak=False)
        def _after_reprocess(
            sender=None, task_id=None, state=None, args=None, kwargs=None, **_
        ):
            try:
                if sender is None or getattr(sender, "name", None) != REPROCESS_TASK:
                    return
                doc_id = None
                if kwargs:
                    doc_id = kwargs.get("document_id")
                if doc_id is None and args:
                    doc_id = args[0]
                from reconsume.tasks import fail_task_row, full_consume_steps

                if state != states.SUCCESS:
                    fail_task_row(task_id, f"reprocess failed (state {state})")
                    return
                if doc_id is None:
                    return
                # Hand the chain row over to the follow-up task, which
                # finalizes it with the field diff.
                full_consume_steps.delay(document_id=doc_id, task_row_id=task_id)
                logger.info(
                    "reconsume: reprocess of document %s finished, "
                    "running full consume steps",
                    doc_id,
                )
            except Exception:
                # Never let the hook break the worker.
                logger.exception("reconsume: hook failed (ignored)")

        # --- upgrade date detection for NORMAL consumption ----------------
        # The consumer calls parse_date() only when the user supplied no
        # explicit date (overrides win upstream), so replacing it at runtime
        # upgrades exactly the automatic detection — nothing else. Fail-soft:
        # on any error the original parse_date answers. Kill switch:
        # RECONSUME_UPGRADE_CONSUME_DATE=false.
        if os.getenv("RECONSUME_UPGRADE_CONSUME_DATE", "true").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            try:
                import datetime as _dt

                from documents import consumer as _consumer_mod

                _original_parse_date = _consumer_mod.parse_date

                def _heuristic_parse_date(filename, text):
                    try:
                        from reconsume.dating import best_date, paperless_parse_one

                        d = best_date(
                            filename or "", text or "", paperless_parse_one()
                        )
                        if d is not None:
                            # aware datetime at 12:00 UTC — DateField-safe in
                            # every timezone (no midnight day-shift)
                            return _dt.datetime.combine(
                                d, _dt.time(12, 0), tzinfo=_dt.timezone.utc
                            )
                    except Exception:
                        logger.exception(
                            "reconsume: consume-date heuristic failed, "
                            "falling back to stock parse_date"
                        )
                    return _original_parse_date(filename, text)

                _consumer_mod.parse_date = _heuristic_parse_date
                logger.info("reconsume: consumer date detection upgraded")
            except Exception:
                logger.exception(
                    "reconsume: could not upgrade consumer date detection (ignored)"
                )
