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
        from celery.signals import task_postrun

        @task_postrun.connect(weak=False)
        def _after_reprocess(sender=None, state=None, args=None, kwargs=None, **_):
            try:
                if sender is None or getattr(sender, "name", None) != REPROCESS_TASK:
                    return
                if state != states.SUCCESS:
                    return
                doc_id = None
                if kwargs:
                    doc_id = kwargs.get("document_id")
                if doc_id is None and args:
                    doc_id = args[0]
                if doc_id is None:
                    return
                from reconsume.tasks import full_consume_steps

                full_consume_steps.delay(document_id=doc_id)
                logger.info(
                    "reconsume: reprocess of document %s finished, "
                    "queued full consume steps",
                    doc_id,
                )
            except Exception:
                # Never let the hook break the worker.
                logger.exception("reconsume: hook failed (ignored)")
