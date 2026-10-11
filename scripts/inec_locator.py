"""Collect polling unit coordinates from INEC's public Polling Unit Locator.

The locator (https://cvr.inecnigeria.org/pu) lists states, LGAs, wards and
polling units through /PublicApi/, and its GET DIRECTIONS form answers with a
Google Maps link to the unit. This script takes the same steps a voter does,
one request at a time, and writes one CSV row per polling unit.

It is polite and resumable:
- one request every --delay seconds (default 1), backing off on errors;
- units already in the output are skipped and unit lists are cached, so an
  interrupted run continues where it stopped.

Usage (from the backend folder):
    python scripts/inec_locator.py --out inec_locator.csv --lga 18-23 --limit 3 --samples samples
    python scripts/inec_locator.py --out inec_locator.csv --lga 18-23
    python scripts/inec_locator.py --out inec_locator.csv

Check the result with scripts/check_inec_locations.py before importing it.
"""

import argparse
import csv
import html
import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

BASE = "https://cvr.inecnigeria.org"
USER_AGENT = "Mozilla/5.0 (compatible; TGCG-polling-unit-locator/1.0)"
FIELDS = [
    "code",
    "name",
    "latitude",
    "longitude",
    "status",
    "maps_url",
    "inec_state_id",
    "inec_lga_id",
    "inec_ward_id",
    "inec_unit_id",
    "fetched_at",
]
# Rows with these statuses are not fetched again on a re-run.
FINISHED = {"ok", "no_location", "outside_nigeria"}
# Nigeria's extent with a margin.
LATITUDES = (3.5, 14.5)
LONGITUDES = (2.5, 15.0)
RETRY_CODES = {429, 500, 502, 503, 504}
# Waits between attempts when the locator fails: about 15 minutes in all.
RETRY_WAITS = (30, 60, 120, 240, 480)
# After a failure the collector slows down, up to one request every 4 s.
MAX_DELAY = 4.0

_OPTION = re.compile(r'<option value="(\d+)">\s*(\d+)\s*-\s*([^<]+)</option>')
_LABEL = re.compile(r"\s*(\d+)\s*-\s*(.+?)\s*$")
_MAPS_URL = re.compile(r"""https?://[^\s"'<>]*(?:google|goo\.gl)[^\s"'<>]*""", re.I)
_DECIMAL = re.compile(r"(-?\d{1,2}\.\d{3,})\s*,\s*\+?\s*(-?\d{1,3}\.\d{3,})")
_DMS = re.compile(
    r"(\d{1,2})°\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)[\"″]\s*([NS])\W*"
    r"(\d{1,3})°\s*(\d{1,2})['′]\s*(\d{1,2}(?:\.\d+)?)[\"″]\s*([EW])"
)


def in_nigeria(latitude, longitude):
    return LATITUDES[0] <= latitude <= LATITUDES[1] and LONGITUDES[0] <= longitude <= LONGITUDES[1]


_QUERY_POINT = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def _point(link):
    """(latitude, longitude) as written in a maps link, or None."""
    # The locator redirects to maps.google.com/?q=11.0642,7.716035636
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    for key in ("q", "query", "destination", "daddr", "ll"):
        for value in query.get(key, []):
            match = _QUERY_POINT.match(value)
            if match:
                return match.group(1), match.group(2)
    text = urllib.parse.unquote_plus(urllib.parse.unquote_plus(link))
    match = _DECIMAL.search(text)
    if match:
        return match.group(1), match.group(2)
    match = _DMS.search(text)
    if match:
        d1, m1, s1, ns, d2, m2, s2, ew = match.groups()
        latitude = int(d1) + int(m1) / 60 + float(s1) / 3600
        longitude = int(d2) + int(m2) / 60 + float(s2) / 3600
        latitude, longitude = (
            (-latitude if ns == "S" else latitude),
            (-longitude if ew == "W" else longitude),
        )
        return f"{latitude:.7f}", f"{longitude:.7f}"
    return None


def extract_location(location_header, body):
    """Find the unit's coordinates in a directions response.

    Looks at the redirect target first, then at Google Maps links in the page.
    Returns (latitude, longitude, maps_url) with the coordinates exactly as
    written (their decimal places show how precise they are), or None.
    """
    links = [location_header] if location_header else []
    links += _MAPS_URL.findall(html.unescape(body or ""))
    for link in links:
        point = _point(link)
        # 0,0 is how a missing location shows up in a maps link.
        if point and not (float(point[0]) == 0 and float(point[1]) == 0):
            return point[0], point[1], link
    return None


def parse_states(page):
    block = re.search(r'id="SearchStateId"[^>]*>(.*?)</select>', page, re.S)
    if not block:
        raise SystemExit("The locator page has changed: no state list found.")
    return [
        (int(value), int(number), html.unescape(name).strip())
        for value, number, name in _OPTION.findall(block.group(1))
    ]


def parse_choices(body):
    """A /PublicApi/ list: [{"0": "--SELECT--", "selected": "0", "347": "01 - BIRNIN GWARI"}]."""
    data = json.loads(body)
    items = data if isinstance(data, list) else [data]
    merged = {}
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
    choices = []
    for key, label in merged.items():
        if not key.isdigit() or key == "0" or not isinstance(label, str):
            continue
        match = _LABEL.match(html.unescape(label))
        if match:
            choices.append((int(key), int(match.group(1)), match.group(2)))
    return sorted(choices, key=lambda choice: choice[1])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Locator:
    """Talks to the locator one request at a time."""

    def __init__(self, delay, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.delay = delay
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), _NoRedirect()
        )
        self.sleep = sleep
        self.clock = clock
        self._last = None

    def _pace(self):
        if self._last is not None:
            pause = self._last + self.delay - self.clock()
            if pause > 0:
                self.sleep(pause)
        self._last = self.clock()

    def request(self, path, *, query=None, form=None, xhr=False):
        """Returns (status, headers, text). Redirects are returned, not followed."""
        url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"User-Agent": USER_AGENT, "Referer": BASE + "/pu"}
        if xhr:
            headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
        if form is not None:
            headers["Origin"] = BASE
        reason = ""
        for attempt in range(len(RETRY_WAITS) + 1):
            self._pace()
            try:
                # The URL always starts with the fixed https BASE.
                request = urllib.request.Request(url, data, headers)  # noqa: S310
                with self.opener.open(request, timeout=40) as r:
                    return r.status, r.headers, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                if 300 <= error.code < 400:
                    return error.code, error.headers, error.read().decode("utf-8", "replace")
                if error.code not in RETRY_CODES:
                    raise SystemExit(
                        f"The locator refused {path} (HTTP {error.code}). Stopping."
                    ) from None
                reason = f"HTTP {error.code}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                reason = str(getattr(error, "reason", "") or error) or type(error).__name__
            if attempt == len(RETRY_WAITS):
                break
            # The locator is struggling: wait, then carry on more slowly.
            wait = RETRY_WAITS[attempt]
            self.delay = min(self.delay * 2, MAX_DELAY)
            print(
                f"{path}: {reason}. Waiting {wait} s, then one request every {self.delay:g} s.",
                flush=True,
            )
            self.sleep(wait)
        raise SystemExit(f"{path} kept failing for about 15 minutes ({reason}). Re-run to resume.")

    def choices(self, kind, parent_field, parent_id):
        _, _, body = self.request(
            f"/PublicApi/{kind}/1/Search",
            query={f"data[Search][{parent_field}]": parent_id},
            xhr=True,
        )
        return parse_choices(body)

    def directions(self, state_id, lga_id, ward_id, unit_id):
        return self.request(
            "/pu_locator/index",
            form={
                "_method": "POST",
                "data[Search][state_id]": state_id,
                "data[Search][local_government_id]": lga_id,
                "data[Search][registration_area_id]": ward_id,
                "data[Search][polling_unit_id]": unit_id,
            },
        )


def read_finished(path, retry_missing=False):
    """Codes not to fetch again, and codes INEC gave no location for last time."""
    if not os.path.exists(path):
        return set(), set()
    with open(path, encoding="utf-8", newline="") as handle:
        latest = {row["code"]: row["status"] for row in csv.DictReader(handle)}
    missing = {code for code, status in latest.items() if status == "no_location"}
    finished = {code for code, status in latest.items() if status in FINISHED}
    return (finished - missing if retry_missing else finished), missing


def harvest(locator, out, *, state=None, lga=None, limit=None, samples=None, retry_missing=False):
    """Walk the locator and append one row per polling unit to [out]."""
    finished, known_missing = read_finished(out, retry_missing)
    cache_path = Path(str(out) + ".lists.json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    saved_samples = 0
    fetched = located = 0
    # Units fetched for the first time, for the format-change guard below.
    fresh = fresh_located = 0

    def choices(kind, parent_field, parent_id):
        key = f"{'units' if kind == 'pus' else kind}/{parent_id}"
        if key not in cache:
            cache[key] = locator.choices(kind, parent_field, parent_id)
        return [tuple(item) for item in cache[key]]

    def save_cache():
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_path)

    new_file = not os.path.exists(out)
    _, _, page = locator.request("/pu")
    with open(out, "a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        if new_file:
            writer.writeheader()
        try:
            for state_id, state_no, _ in parse_states(page):
                if state is not None and state_no != state:
                    continue
                for lga_id, lga_no, lga_name in choices("lgas", "state_id", state_id):
                    if lga is not None and lga_no != lga:
                        continue
                    wards = choices("wards", "local_government_id", lga_id)
                    for ward_id, ward_no, ward_name in wards:
                        units = choices("pus", "registration_area_id", ward_id)
                        save_cache()
                        ward_code = f"{state_no:02d}-{lga_no:02d}-{ward_no:02d}"
                        ward_found = ward_tried = 0
                        for unit_id, unit_no, unit_name in units:
                            code = f"{ward_code}-{unit_no:03d}"
                            if code in finished:
                                continue
                            if limit is not None and fetched >= limit:
                                return fetched, located
                            status, headers, body = locator.directions(
                                state_id, lga_id, ward_id, unit_id
                            )
                            if samples is not None and saved_samples < 3:
                                Path(samples).mkdir(parents=True, exist_ok=True)
                                Path(samples, f"{code}.txt").write_text(
                                    f"HTTP {status}\n{headers}\n{body}", encoding="utf-8"
                                )
                                saved_samples += 1
                            found = extract_location(headers.get("Location"), body)
                            row = {
                                "code": code,
                                "name": unit_name,
                                "inec_state_id": state_id,
                                "inec_lga_id": lga_id,
                                "inec_ward_id": ward_id,
                                "inec_unit_id": unit_id,
                                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "status": "no_location",
                                # Kept so a missing location can be explained later.
                                "maps_url": headers.get("Location") or f"HTTP {status}",
                            }
                            if found:
                                latitude, longitude, link = found
                                inside = in_nigeria(float(latitude), float(longitude))
                                row.update(
                                    latitude=latitude,
                                    longitude=longitude,
                                    maps_url=link,
                                    status="ok" if inside else "outside_nigeria",
                                )
                            writer.writerow(row)
                            handle.flush()
                            finished.add(code)
                            fetched += 1
                            ward_tried += 1
                            if row["status"] == "ok":
                                located += 1
                                ward_found += 1
                            # Guard against a format change: stop early rather than walk
                            # the whole country finding nothing. Units retried because
                            # they had no location before do not count.
                            if code not in known_missing:
                                fresh += 1
                                fresh_located += row["status"] == "ok"
                                if fresh == 10 and fresh_located == 0:
                                    raise SystemExit(
                                        "No coordinates in the first 10 new units. The locator "
                                        "may have changed; check the --samples folder."
                                    )
                        if ward_tried:
                            print(
                                f"{ward_code} {lga_name} / {ward_name}: "
                                f"{ward_found} of {ward_tried} located",
                                flush=True,
                            )
        except KeyboardInterrupt:
            print("Interrupted. Re-run the same command to resume.")
        finally:
            save_cache()
    return fetched, located


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="CSV file to write (appended to on resume)")
    parser.add_argument("--state", type=int, help="INEC state number, e.g. 18 for Kaduna")
    parser.add_argument("--lga", help="INEC LGA code, e.g. 18-23 for Zaria")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--limit", type=int, help="stop after this many units")
    parser.add_argument("--samples", help="folder for the first raw directions responses")
    parser.add_argument(
        "--retry-missing", action="store_true", help="fetch no_location units again"
    )
    args = parser.parse_args(argv)
    state, lga = args.state, None
    if args.lga:
        state_part, lga_part = args.lga.split("-")
        state, lga = int(state_part), int(lga_part)
    fetched, located = harvest(
        Locator(args.delay),
        args.out,
        state=state,
        lga=lga,
        limit=args.limit,
        samples=args.samples,
        retry_missing=args.retry_missing,
    )
    print(f"Done: {fetched} units fetched, {located} with coordinates.")


if __name__ == "__main__":
    main()
