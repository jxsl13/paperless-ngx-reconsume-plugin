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
        #
        # The row's task_id is PREFIXED ("reconsume:<celery id>") so it never
        # matches a real celery task id — paperless' own task_postrun_handler
        # updates any row whose task_id matches the finished task and would
        # otherwise clobber ours (IntegrityError on state=None crashes).

        def _row_id(task_id):
            return f"reconsume:{task_id}"

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

                    create_pending_task_row(_row_id(task_id), doc_id)
            except Exception:
                logger.exception("reconsume: publish hook failed (ignored)")

        @task_prerun.connect(weak=False)
        def _on_reprocess_started(sender=None, task_id=None, **_):
            try:
                if sender is None or getattr(sender, "name", None) != REPROCESS_TASK:
                    return
                from reconsume.tasks import mark_task_row_started

                mark_task_row_started(_row_id(task_id))
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
                    fail_task_row(
                        _row_id(task_id),
                        f"reprocess did not complete (state {state}) — "
                        "typically killed mid-OCR (worker restart/shutdown); "
                        "run reprocess again",
                    )
                    return
                if doc_id is None:
                    return
                logger.info(
                    "reconsume: reprocess of document %s finished, "
                    "running full consume steps",
                    doc_id,
                )
                # Run the follow-up INLINE (~1s), right here in the same
                # worker slot. Dispatching it with .delay() would put it at
                # the BACK of the celery queue — behind every still-queued
                # reprocess task — so during bulk runs no document would get
                # its date/matching/row-close until the whole OCR queue has
                # drained.
                try:
                    full_consume_steps(
                        document_id=doc_id, task_row_id=_row_id(task_id)
                    )
                except Exception:
                    logger.exception(
                        "reconsume: inline follow-up failed for %s", doc_id
                    )
            except Exception:
                # Never let the hook break the worker.
                logger.exception("reconsume: hook failed (ignored)")

        # --- reliability: interrupted reprocess tasks auto-resume ---------
        # paperless' celery defaults ack tasks BEFORE execution, so a task
        # killed mid-OCR (worker restart, OOM, crash) is simply lost and
        # never retried. With acks_late + reject_on_worker_lost the broker
        # redelivers it: immediately on a clean shutdown, or after the
        # redis visibility timeout (default 1h) on a hard kill. Both the
        # reprocess task and our follow-up are idempotent, so redelivery
        # is safe. prefetch=1 stops one worker from hoarding (and losing)
        # a batch of queued tasks. Kill switch:
        # RECONSUME_RELIABLE_REPROCESS=false.
        if os.getenv("RECONSUME_RELIABLE_REPROCESS", "true").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            try:
                from celery import current_app

                current_app.conf.worker_prefetch_multiplier = 1

                def _harden_tasks(app):
                    hardened = []
                    for name in (
                        REPROCESS_TASK,
                        "reconsume.tasks.full_consume_steps",
                    ):
                        t = app.tasks.get(name)
                        if t is not None:
                            t.acks_late = True
                            t.reject_on_worker_lost = True
                            hardened.append(name.rsplit(".", 1)[-1])
                    if hardened:
                        logger.info(
                            "reconsume: reliability (acks_late) enabled for %s",
                            ", ".join(hardened),
                        )

                _harden_tasks(current_app)

                @current_app.on_after_finalize.connect(weak=False)
                def _harden_late(sender=None, **_):
                    _harden_tasks(sender or current_app)
            except Exception:
                logger.exception(
                    "reconsume: could not enable reliability (ignored)"
                )

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
