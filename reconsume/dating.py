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
  +10  ',' plus one short word (<=4 letters) before — "<City>, den <date>",
       "<City>, le <date>" — structural: comma + tiny particle, no word list
  +20  position < 1200 chars (top of page 1), +10 extra < 400
  +35  paragraph isolation — the date sits in its own blank-line-delimited
       block with little other content
  +20  line isolation — the date is (nearly) alone on its text line while
       its paragraph is busy (a label word directly precedes it, OCR
       column-merging notwithstanding — "Alsenstr. 7 Datum 09.04.2024")
  -10  paragraph has a lot of OTHER content besides the date (deeply
       embedded in running prose)
  +10..20  the same calendar date repeats — counting DISTINCT CLUSTERS of
       occurrences (positions within ~200 chars of each other collapse
       into one cluster), so N adjacent table rows restating the same
       date count as ONE piece of evidence, not N
  +30  a non-repeated candidate that occurs BEFORE the first occurrence of
       a clearly dominant, repeated OTHER date value — defends a letter's
       own one-off dateline against a restated deadline/effective date
  -40  match embedded in a digit/spec blob ("AM4/1151/1150/1155"), glued to
       an identifier ("ISA-25.11.2017"), or inside a barcode/reference
       block ("*13.05.26*" — bracketed by '*' or similar glue punctuation)
  -15  day-of-month is 1 in a full date — "zum 01.10.2016" is typically an
       effective/cut-off day, not an issue date (structural prior)
  -10  partial date (month/year only, no explicit day)
  -25  additionally for a word+year match whose "word" validated as NOT a
       month (a bare year wearing a date-shaped disguise, e.g. a coverage-
       period tag "für 2023" repeated as a page watermark) — weaker
       evidence than genuine month/day precision
  -35  more than 6 years older than the newest clean candidate in the
       document (references to old contracts, laws)
  -25  additionally when more than 15 years older — birth-date territory

FILENAME dates: user-named files carry strong intent. Structured filename
dates are extracted deterministically as YMD (a leading 4-digit year defines
the order — structural, not locale-dependent):
    "2019.03.04_Shop.pdf"  -> 2019-03-04   (same separator both times)
    "BAföG_2018_04-1.pdf"  -> 2018-04-30   (year+month; "-1" copy suffix
                                            is NOT mistaken for a day)
    "Abrechnung_08_2019"   -> 2019-08-31   (month+year)
    "scan_20250725_140140" -> weak (YYYYMMDD_HHMMSS = scanner timestamp)
A strong filename date competes with base score 50 (partial -10); a weak
scanner timestamp only with score 1 (pure last resort). Additionally, any
text candidate CONFIRMED by a strong filename date (same day, or same
year+month for a partial filename date) gets +35 — the user's own naming
is the best available ground truth.

Partial dates (month + year, no day — "11.2016", "Oktober 2016", "Aug/2019")
resolve to the LAST day of that month, calendar-aware (February, leap years:
"Februar 2024" -> 2024-02-29).

Word+year matches whose "month word" is not actually a month (e.g. "für
2016") are detected by parsing twice with two different RELATIVE_BASE dates:
if the resulting month differs, dateparser merely filled in the base month —
the candidate degrades deterministically to Dec 31 of that year, tagged as
weaker evidence (see -25 above) rather than silently competing at full
strength.

Year-only fallback: if NO month/day candidate parses anywhere, standalone
past years resolve to Dec 31 of that year ("Steuerbescheinigung 2019" ->
2019-12-31), picked with the same structural scoring. The current year is
excluded (its Dec 31 lies in the future).

Ties: higher score wins; filename candidates win ties against text
candidates; then earlier position. No candidates -> None (the document's
date is left untouched).
"""

import calendar
import datetime
import logging
import re

logger = logging.getLogger("paperless.reconsume")

# Generic textual-date shapes, any language: "<day><suffix?> <particle?>
# <monthword> <year>" and "<monthword> <day><suffix?>, <year>" and
# "<monthword>[./-]<year>" ("Aug/2019"). Day suffixes ("nd", "er", ".") and
# particles ("of", "de") are matched as generic 1-3 letter runs — no
# specific words. dateparser validates whether the letter run is a month.
_L = r"[^\W\d_]"  # any unicode letter
TEXTUAL_DATE = re.compile(
    rf"\b(?:\d{{1,2}}(?:{_L}{{1,2}}|\.)?(?:\s+{_L}{{1,3}})?\s+{_L}{{3,12}}\.?,?\s+\d{{4}}"
    rf"|{_L}{{3,12}}\.?\s+\d{{1,2}}(?:{_L}{{1,2}})?\s*,?\s+\d{{4}}"
    rf"|{_L}{{3,12}}\.?\s*[./-]\s*\d{{4}})(?!\d)",
    re.IGNORECASE | re.UNICODE,
)

# "2nd" -> "2", "1er" -> "1", "3º" -> "3": strip 1-2 letters glued to a
# 1-2 digit day so dateparser sees a clean number. Generic, not localized.
DAY_SUFFIX = re.compile(rf"\b(\d{{1,2}}){_L}{{1,2}}\b", re.UNICODE)

_MONTH_WORD = re.compile(rf"{_L}{{3,}}", re.UNICODE)

# ", den " / ", le " / ", on " — comma followed by ONE short particle
# (1-4 letters, any language) right before the date. Purely structural.
COMMA_WORD = re.compile(rf",\s*{_L}{{1,4}}\s*$", re.UNICODE)

# Purely numeric month+year ("11.2016", "10/2016", "2016-11"). dateparser
# would misread these as day+year and fill in the CURRENT month — handle
# them explicitly and deterministically instead.
_NUM_MONTH_YEAR = re.compile(
    r"^\s*(?:(\d{1,2})\s*[./-]\s*(\d{4})|(\d{4})\s*[./-]\s*(\d{1,2}))\s*$"
)

# --- structured filename dates (YMD by structure: 4-digit year leads) ------
# Same separator twice = full date; mixed separators would treat copy
# suffixes ("2018_04-1") as days.
FN_FULL = re.compile(r"(?<!\d)(\d{4})([._-])(\d{1,2})\2(\d{1,2})(?!\d)")
FN_COMPACT = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
# year+month / month+year; a following TWO-digit group would be the day
# (then FN_FULL should have matched) — a single digit is a copy suffix.
FN_YM = re.compile(r"(?<!\d)(\d{4})[._-](\d{1,2})(?![._-]?\d\d)(?!\d)")
FN_MY = re.compile(r"(?<!\d)(\d{1,2})[._-](\d{4})(?!\d)")
# YYYYMMDD followed by _HHMMSS = scanner timestamp, weak evidence
_SCAN_TAIL = re.compile(r"[._-]\d{6}(?!\d)")

# Punctuation that "glues" a date into a barcode/reference block even when
# not touching it directly (asterisk-fenced codes: "*13.05.26*"). Checked
# on the immediate neighbour char, same spirit as the digit/slash check.
_GLUE_CHARS = set("*#|")

# How close two occurrences of the SAME date value must be (in chars) to
# count as the same repetition "cluster" rather than independent evidence.
_CLUSTER_GAP = 200


def _last_day(y, m):
    return calendar.monthrange(y, m)[1]


def _today():
    try:
        from django.utils import timezone

        return timezone.localdate()
    except Exception:
        return datetime.date.today()


def filename_date_candidates(filename):
    """
    Deterministic, structural date extraction from the file name.
    Returns [(date, strong: bool, partial: bool)]. A leading 4-digit year
    fixes the order to YMD — no locale involved.
    """
    out = []
    today = _today()
    fn = filename or ""
    for m in FN_FULL.finditer(fn):
        y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
        try:
            dd = datetime.date(y, mo, d)
        except ValueError:
            continue
        if y > 1900 and dd <= today:
            out.append((dd, True, False))
    for m in FN_COMPACT.finditer(fn):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dd = datetime.date(y, mo, d)
        except ValueError:
            continue
        if not (y > 1900 and dd <= today):
            continue
        strong = not _SCAN_TAIL.match(fn[m.end():])
        out.append((dd, strong, False))
    if not out:  # month-precision only when no full date is present
        for m in FN_YM.finditer(fn):
            y, mo = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and y > 1900:
                dd = datetime.date(y, mo, _last_day(y, mo))
                if dd <= today:
                    out.append((dd, True, True))
        for m in FN_MY.finditer(fn):
            mo, y = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and y > 1900:
                dd = datetime.date(y, mo, _last_day(y, mo))
                if dd <= today:
                    out.append((dd, True, True))
    return out


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
    last = _last_day(year, month)
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


def _paragraph_extra_len(source, start, end):
    """
    Chars of "other" content in the blank-line-delimited paragraph
    containing [start:end), besides the match itself. Purely structural.
    """
    para_start = source.rfind("\n\n", 0, start)
    para_start = 0 if para_start == -1 else para_start + 2
    para_end = source.find("\n\n", end)
    para_end = len(source) if para_end == -1 else para_end
    paragraph_len = len(source[para_start:para_end].strip())
    return max(0, paragraph_len - (end - start))


def _line_extra_len(source, start, end):
    """Chars of other content on the text LINE containing the match."""
    ls = source.rfind("\n", 0, start) + 1
    le = source.find("\n", end)
    le = len(source) if le == -1 else le
    return max(0, len(source[ls:le].strip()) - (end - start))


def _candidates(text, parse_one):
    for m in _iter_matches(text):
        d = parse_one(m.group(0))
        if d is not None:
            start, end = m.start(), m.end()
            prev_c = text[start - 1] if start > 0 else " "
            prev2_c = text[start - 2] if start > 1 else " "
            next_c = text[end] if end < len(text) else " "
            # embedded in a digit/spec blob ("AM4/1151/1150/1155"), glued
            # to an identifier ("ISA-25.11.2017"), or barcode-fenced
            # ("*13.05.26*")
            noisy = (
                prev_c.isdigit()
                or prev_c == "/"
                or next_c.isdigit()
                or next_c == "/"
                or prev_c in _GLUE_CHARS
                or next_c in _GLUE_CHARS
                or (prev_c in "-._" and prev2_c.isalnum())
            )
            yield {
                "date": d,
                "pos": start,
                "end": end,
                "noisy": noisy,
                "partial": not has_day(m.group(0)),
                "year_guess": bool(getattr(parse_one, "last_year_guess", False)),
                "para_extra": _paragraph_extra_len(text, start, end),
                "line_extra": _line_extra_len(text, start, end),
                "before8": text[max(0, start - 8): start],
                "before20": text[max(0, start - 20): start],
            }


# Standalone 4-digit years ("Steuerbescheinigung 2019"). Only used as the
# LAST fallback tier when no month/day candidate parses anywhere.
# Excluded when part of a longer number, a date ("31.12.2019", "2019-05")
# or an amount ("2019,50") — i.e. when a digit follows the separator.
# "Zinsbescheinigung_2021.pdf" stays valid (".p" is not ".<digit>").
YEAR_ONLY = re.compile(r"(?<![\d.,/-])((?:19|20)\d{2})(?!\d)(?![.,/-]\d)")


def _year_only_candidates(text):
    """
    Fallback tier: every standalone past year resolves to Dec 31 of that
    year ("2019" -> 2019-12-31). Years whose Dec 31 lies in the future
    (i.e. the current year) are excluded — only past years qualify.
    """
    today = _today()
    for m in YEAR_ONLY.finditer(text):
        d = datetime.date(int(m.group(1)), 12, 31)
        if d.year <= 1900 or d > today:
            continue
        yield {
            "date": d,
            "pos": m.start(),
            "end": m.end(),
            "noisy": False,
            "partial": True,
            "year_guess": False,
            "para_extra": _paragraph_extra_len(text, m.start(), m.end()),
            "line_extra": _line_extra_len(text, m.start(), m.end()),
            "before8": text[max(0, m.start() - 8): m.start()],
            "before20": text[max(0, m.start() - 20): m.start()],
        }


def _clusters(positions, gap=_CLUSTER_GAP):
    """Group sorted positions into clusters where consecutive gaps < gap.
    Returns list of cluster-start positions (the first/earliest pos of
    each cluster)."""
    if not positions:
        return []
    positions = sorted(positions)
    clusters = [[positions[0]]]
    for p in positions[1:]:
        if p - clusters[-1][-1] < gap:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    return [c[0] for c in clusters]


def best_date(filename, text, parse_one):
    """
    Return the most plausible issue date (datetime.date) or None.

    Tier 1: text candidates (month precision or better) + structured
    filename dates. Tier 2 (only if tier 1 is empty): standalone past
    years -> Dec 31.
    """
    text = text or ""
    cands = []
    for c in _candidates(text, parse_one):
        d = c["date"]
        if isinstance(d, datetime.datetime):
            c["date"] = d.date()
        cands.append(c)

    fcands = filename_date_candidates(filename)

    if not cands and not fcands:
        # fallback tier: standalone past years -> Dec 31 of that year
        cands = list(_year_only_candidates(text))
        if not cands and filename:
            cands = list(_year_only_candidates(filename))
            for c in cands:
                c["pos"] = 0
    if not cands and not fcands:
        return None

    # Recency anchor: all non-noisy candidates (incl. partial) + filename
    anchor = [c["date"] for c in cands if not c["noisy"]]
    anchor += [d for d, _s, _p in fcands]
    newest = max(anchor) if anchor else max(c["date"] for c in cands)

    # Repetition counts distinct CLUSTERS of clean occurrences, not raw
    # count — adjacent table rows restating one value are one piece of
    # evidence, not N. Also locate each value's earliest cluster position,
    # needed for the "precedes the dominant repeat" bonus below.
    positions_by_date = {}
    for c in cands:
        if not c["noisy"]:
            positions_by_date.setdefault(c["date"], []).append(c["pos"])
    clusters_by_date = {d: _clusters(ps) for d, ps in positions_by_date.items()}
    cluster_count = {d: len(cs) for d, cs in clusters_by_date.items()}
    first_pos = {d: min(ps) for d, ps in positions_by_date.items()}

    # The dominant repeated value: highest cluster count, if it actually
    # repeats (>=2 clusters). Ties are not resolved here — any qualifying
    # dominant value is enough to grant the "precedes it" bonus below.
    dominant = None
    if cluster_count:
        best_d, best_n = max(cluster_count.items(), key=lambda kv: kv[1])
        if best_n >= 2:
            dominant = best_d

    strong_full = {d for d, s, p in fcands if s and not p}
    strong_month = {(d.year, d.month) for d, s, p in fcands if s and p}

    scored = []
    for c in cands:
        d = c["date"]
        s = 0
        if c["noisy"]:
            s -= 40
        b8 = c["before8"].rstrip()
        labeled = False
        if b8.endswith(":") or b8.endswith("："):
            s += 20
            labeled = True
        elif b8.endswith(","):
            s += 10
            labeled = True
        elif COMMA_WORD.search(c["before20"]):
            s += 10
            labeled = True
        if c["pos"] < 1200:
            s += 20
        if c["pos"] < 400:
            s += 10
        if c["para_extra"] <= 40:
            s += 35
        elif c["para_extra"] >= 300:
            s -= 10
        if not labeled and c["para_extra"] > 40 and c["line_extra"] <= 25:
            s += 20  # dateline alone on its line inside a busy block —
            # only when colon/comma didn't already flag a label (those
            # signals are redundant with line isolation, not additive)
        s += min(20, 10 * (cluster_count.get(d, 1) - 1))
        if (
            dominant is not None
            and d != dominant
            and cluster_count.get(d, 1) == 1
            and c["pos"] < first_pos[dominant]
        ):
            s += 40  # one-off dateline ahead of a restated deadline
        if c["partial"]:
            s -= 10
            if c["year_guess"]:
                s -= 25  # bare year in date-shaped disguise
        elif d.day == 1:
            s -= 15  # "zum 01.10." — effective-date prior
        age = (newest - d).days
        if age > 6 * 365:
            s -= 35
            if age > 15 * 365:
                s -= 60  # birth-date territory — crush it decisively so
                # it can never outscore even a weak (year-guess) candidate
                # when no better alternative exists in the document
        # user's own file naming confirms this date: exact day for full
        # filename dates, year+month for month-precision filename dates
        if d in strong_full or (d.year, d.month) in strong_month:
            s += 35
        scored.append((s, 0, -c["pos"], d))

    for d, strong, partial in fcands:
        s = 50 if strong else 1
        if partial:
            s -= 10
        if (newest - d).days > 6 * 365:
            s -= 35
        # filename candidates win ties against text candidates
        scored.append((s, 1, 0, d))

    scored.sort(reverse=True)
    top = scored[0]
    logger.debug(
        "reconsume dating: %d text + %d filename candidates, winner %s (score %d)",
        len(cands), len(fcands), top[3], top[0],
    )
    return top[3]


def paperless_parse_one():
    """
    Single-date parser using paperless' configured settings, with a
    language-independent fallback: if the configured locales cannot parse
    a candidate (e.g. an English month name in a German setup), dateparser
    retries with full auto-detection (~200 languages). Deterministic.

    Dates without an explicit day (month/year only) resolve to the LAST day
    of that month — dateparser's PREFER_DAY_OF_MONTH="last" is calendar-aware
    (February and leap years included).

    Word+year strings whose word is NOT a month name (dateparser would
    silently fill in a month) are detected via two parses with different
    RELATIVE_BASE dates and degrade to Dec 31 of the year. The returned
    closure exposes whether the LAST call degraded via the
    `last_year_guess` attribute, so `_candidates()` can tag the resulting
    candidate as weaker (bare-year) evidence.
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

    def _dp_settings(prefer_day, relative_base=None):
        s = {
            "DATE_ORDER": settings.DATE_ORDER,
            "PREFER_DAY_OF_MONTH": prefer_day,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": settings.TIME_ZONE,
        }
        if relative_base is not None:
            s["RELATIVE_BASE"] = relative_base
        return s

    def _dp_locale(ds, prefer_day, relative_base=None):
        """Parse with the CONFIGURED languages only."""
        try:
            return dateparser.parse(
                ds, settings=_dp_settings(prefer_day, relative_base),
                locales=languages,
            )
        except Exception:
            return None

    def _dp_auto(ds, prefer_day, relative_base=None):
        """Parse with full locale auto-detection (~200 languages)."""
        try:
            return dateparser.parse(
                ds, settings=_dp_settings(prefer_day, relative_base),
            )
        except Exception:
            return None

    def _year_end_of(ds):
        ym = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", ds)
        if ym:
            return datetime.datetime(
                int(ym.group(1)), 12, 31, tzinfo=datetime.timezone.utc,
            )
        return None

    def parse_one(ds):
        parse_one.last_year_guess = False
        # numeric MM.YYYY / YYYY-MM: deterministic month-end, bypassing
        # dateparser's current-month filling
        d = _parse_numeric_month_year(ds)
        if d is None:
            day_known = has_day(ds)
            prefer_day = "first" if day_known else "last"
            ds_clean = DAY_SUFFIX.sub(r"\1", ds)
            d_locale = _dp_locale(ds_clean, prefer_day)
            d = d_locale if d_locale is not None else (
                _dp_auto(ds_clean, prefer_day) if languages else None
            )
            if languages is None and d is None:
                d = _dp_auto(ds_clean, prefer_day)
            if (
                d is not None
                and not day_known
                and _MONTH_WORD.search(ds)
            ):
                # month-word validation, two structural checks:
                # (a) only the 200-language auto-fallback could read the
                #     word — in a configured-language document a partial
                #     month word the configured language cannot parse is
                #     almost never a real month ("für" reads as a month in
                #     SOME locale). Degrade to Dec 31 of the year.
                # (b) parse twice against different RELATIVE_BASE dates;
                #     if the month follows the base, dateparser merely
                #     filled it in ("Jahr 2020" -> current month, which
                #     would even be non-deterministic across months).
                degraded = False
                if languages and d_locale is None:
                    yr_end = _year_end_of(ds)
                    if yr_end is not None:
                        d = yr_end
                        degraded = True
                else:
                    b1 = datetime.datetime(2000, 1, 15)
                    b2 = datetime.datetime(2000, 6, 15)
                    d1 = _dp_locale(ds_clean, prefer_day, b1)
                    d2 = _dp_locale(ds_clean, prefer_day, b2)
                    if d1 is not None and d2 is not None and d1.month != d2.month:
                        yr_end = _year_end_of(ds)
                        if yr_end is not None:
                            d = yr_end
                            degraded = True
                parse_one.last_year_guess = degraded
        if d is None or d.year <= 1900 or d > now or d.date() in ignore:
            return None
        return d

    parse_one.last_year_guess = False
    return parse_one
