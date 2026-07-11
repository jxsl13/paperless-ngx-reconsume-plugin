"""
Deterministic, language-independent date selection.

paperless' parse_date() takes the FIRST regex match in the OCR text — often
wrong (birth dates, footer dates, referenced years) and blind to formats its
DATE_REGEX misses (e.g. "October 2nd, 2022").

This module contains NO localized keywords whatsoever. Candidate extraction
uses generic letter/digit shapes, parsing is delegated to dateparser (which
knows month names in ~200 languages), and the winner is chosen purely by
structural evidence that works in any language and script:

  +20  ':' directly before the candidate  — label syntax ("Datum:", "Date:",
       "日付：", "Fecha:") without naming any label word
  +10  ',' directly before               — letter-head style "<City>, <date>"
  +20  position < 1200 chars (top of page 1), +10 extra < 400
  +10..20  the same calendar date repeats in the document
  -40  match embedded in a digit/spec blob (e.g. "AM4/1151/1150/1155")
  -10  partial date (month/year only, no explicit day) — valid but weaker
       evidence than a full date; never serves as the recency anchor
  -35  more than 6 years older than the newest clean candidate in the
       document (references to old contracts, laws, birth dates)

Partial dates (month + year, no day — "11.2016", "Oktober 2016") resolve to
the LAST day of that month via dateparser's PREFER_DAY_OF_MONTH="last",
which is calendar-aware (February, leap years: "Februar 2024" -> 2024-02-29).

Ties: higher score wins, then earlier position. No candidates -> None
(the document's date is left untouched).
"""

import calendar
import datetime
import logging
import re

logger = logging.getLogger("paperless.reconsume")

# Generic textual-date shapes, any language: "<day><suffix?> <particle?>
# <monthword> <year>" and "<monthword> <day><suffix?>, <year>".
# Day suffixes ("nd", "er", ".") and particles ("of", "de") are matched as
# generic 1-3 letter runs — no specific words. dateparser validates whether
# the letter run is actually a month name.
_L = r"[^\W\d_]"  # any unicode letter
TEXTUAL_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:{_L}{{1,2}}|\.)?(?:\s+{_L}{{1,3}})?\s+{_L}{{3,12}}\.?,?\s+\d{{4}}"
    rf"|{_L}{{3,12}}\.?\s+\d{{1,2}}(?:{_L}{{1,2}})?\s*,?\s+\d{{4}})\b",
    re.IGNORECASE | re.UNICODE,
)

# "2nd" -> "2", "1er" -> "1", "3º" -> "3": strip 1-2 letters glued to a
# 1-2 digit day so dateparser sees a clean number. Generic, not localized.
DAY_SUFFIX = re.compile(rf"\b(\d{{1,2}}){_L}{{1,2}}\b", re.UNICODE)

_MONTH_WORD = re.compile(rf"{_L}{{3,}}", re.UNICODE)

# Purely numeric month+year ("11.2016", "10/2016", "2016-11"). dateparser
# would misread these as day+year and fill in the CURRENT month — handle
# them explicitly and deterministically instead.
_NUM_MONTH_YEAR = re.compile(r"^\s*(?:(\d{1,2})\s*[./-]\s*(\d{4})|(\d{4})\s*[./-]\s*(\d{1,2}))\s*$")


def _parse_numeric_month_year(ds):
    """Return an aware datetime for MM.YYYY / YYYY-MM strings (last day of
    month, leap-year aware via calendar.monthrange), else None."""
    m = _NUM_MONTH_YEAR.match(ds)
    if not m:
        return None
    if m.group(1) is not None:
        month, year = int(m.group(1)), int(m.group(2))
    else:
        year, month = int(m.group(3)), int(m.group(4))
    if not 1 <= month <= 12:
        return None
    last = calendar.monthrange(year, month)[1]
    return datetime.datetime(year, month, last, tzinfo=datetime.timezone.utc)


def has_day(ds):
    """
    Does this date string contain an explicit day-of-month?
      "31.10.2016"        -> True   (two 1-2 digit groups: day + month)
      "2. Oktober 2022"   -> True   (1-2 digit group + month word)
      "October 2nd, 2022" -> True
      "11.2016", "10/2016"-> False  (single 1-2 digit group = the month)
      "Oktober 2016"      -> False  (month word + year only)
    Language-independent: only digit-group counting and letter runs.
    """
    small = [g for g in re.findall(r"\d+", ds) if len(g) <= 2]
    if len(small) >= 2:
        return True
    return len(small) >= 1 and _MONTH_WORD.search(ds) is not None


def _iter_matches(text):
    """All date-shaped spans: paperless' DATE_REGEX + generic textual dates."""
    from documents.parsers import DATE_REGEX

    taken = []
    for m in re.finditer(DATE_REGEX, text):
        taken.append((m.start(), m.end()))
        yield m
    for m in TEXTUAL_DATE.finditer(text):
        s, e = m.start(), m.end()
        if any(s < te and ts < e for ts, te in taken):
            continue  # overlaps a DATE_REGEX match
        yield m


def _candidates(text, parse_one):
    for m in _iter_matches(text):
        d = parse_one(m.group(0))
        if d is not None:
            prev_c = text[m.start() - 1] if m.start() > 0 else " "
            next_c = text[m.end()] if m.end() < len(text) else " "
            # embedded in a longer digit/spec blob ("AM4/1151/1150/1155")
            noisy = (
                prev_c.isdigit() or prev_c == "/" or next_c.isdigit() or next_c == "/"
            )
            yield d, m.start(), noisy, not has_day(m.group(0))


def best_date(filename, text, parse_one):
    """Return the most plausible issue date (datetime.date) or None."""
    text = text or ""
    cands = []
    for d, pos, noisy, partial in _candidates(text, parse_one):
        if isinstance(d, datetime.datetime):
            d = d.date()
        cands.append((d, pos, noisy, partial))
    # filename candidates count like very early text (paperless checks them too)
    if filename:
        for d, _, noisy, partial in _candidates(filename, parse_one):
            if isinstance(d, datetime.datetime):
                d = d.date()
            cands.append((d, 0, noisy, partial))
    if not cands:
        return None

    # Partial (month/year-only) and digit-noise matches never serve as the
    # recency anchor — they are weaker evidence.
    clean = [d for d, _, noisy, partial in cands if not partial and not noisy]
    newest = max(clean) if clean else max(d for d, _, _n, _p in cands)
    freq = {}
    for d, _, _n, _p in cands:
        freq[d] = freq.get(d, 0) + 1

    scored = []
    for d, pos, noisy, partial in cands:
        before = text[max(0, pos - 8) : pos].rstrip()
        score = 0
        if noisy:
            score -= 40
        # punctuation context — language- and script-independent
        if before.endswith(":") or before.endswith("："):
            score += 20
        elif before.endswith(","):
            score += 10
        if pos < 1200:
            score += 20
        if pos < 400:
            score += 10
        score += min(20, 10 * (freq[d] - 1))
        if partial:
            score -= 10
        if (newest - d).days > 6 * 365:
            score -= 35
        scored.append((score, -pos, d))

    scored.sort(reverse=True)
    top_score, neg_pos, top_date = scored[0]
    logger.debug(
        "reconsume dating: %d candidates, winner %s (score %d, pos %d)",
        len(cands), top_date, top_score, -neg_pos,
    )
    return top_date


def paperless_parse_one():
    """
    Single-date parser using paperless' configured settings, with a
    language-independent fallback: if the configured locales cannot parse
    a candidate (e.g. an English month name in a German setup), dateparser
    retries with full auto-detection (~200 languages). Deterministic.

    Dates without an explicit day (month/year only) resolve to the LAST day
    of that month — dateparser's PREFER_DAY_OF_MONTH="last" is calendar-aware
    (February and leap years included).
    """
    import dateparser
    from django.conf import settings
    from django.utils import timezone

    try:
        from documents.parsers import ocr_to_dateparser_languages
        from paperless.config import OcrConfig

        languages = settings.DATE_PARSER_LANGUAGES or ocr_to_dateparser_languages(
            OcrConfig().language,
        )
    except Exception:
        languages = None

    ignore = getattr(settings, "IGNORE_DATES", ())
    now = timezone.now()

    def _dp_settings(prefer_day):
        return {
            "DATE_ORDER": settings.DATE_ORDER,
            "PREFER_DAY_OF_MONTH": prefer_day,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": settings.TIME_ZONE,
        }

    def parse_one(ds):
        # numeric MM.YYYY / YYYY-MM: deterministic month-end, bypassing
        # dateparser's current-month filling
        d = _parse_numeric_month_year(ds)
        if d is None:
            # month/year-only dates get the last day of the month
            prefer_day = "first" if has_day(ds) else "last"
            dp_settings = _dp_settings(prefer_day)
            ds = DAY_SUFFIX.sub(r"\1", ds)
            try:
                d = dateparser.parse(ds, settings=dp_settings, locales=languages)
                if d is None and languages:
                    # language-independent fallback: auto-detect locale
                    d = dateparser.parse(ds, settings=dp_settings)
            except Exception:
                return None
        if d is None or d.year <= 1900 or d > now or d.date() in ignore:
            return None
        return d

    return parse_one
