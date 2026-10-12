# Geography data

## nigeria_geography.json

Zones, states, senatorial districts and LGAs. It is generated from the Flutter
client's `GeographyRegistry` with `dart run tool/export_geography.dart`.

## inec_polling_units.csv.gz

All 176,846 INEC polling units in 8,809 wards, mapped onto the canonical LGA
codes.

| | |
|---|---|
| Upstream | [saidiadegoke/nigeria-inec-geo](https://github.com/saidiadegoke/nigeria-inec-geo), a transcription of INEC's public polling unit directory |
| Upstream commit | `3cf617721aea519eef960681d3d86a4fad4325da` (August 2026) |
| Retrieved via | [paul1029-ife/polling-unit-finder](https://github.com/paul1029-ife/polling-unit-finder), `data/polling-units.csv`, on 2026-10-10 |
| SHA-256 of source CSV | `28667fd3a5574eaae0436f98d4b693811886c895edb40f90cb754a2107a29484` (matches the published provenance) |
| Licence | MIT for the compilation. The underlying data is public INEC information. |
| Official directory | https://inecnigeria.org/polling-units/ |

**Checks performed:**

- 176,846 unique polling unit codes, 8,809 wards, 774 LGAs and 37 states/FCT.
- No blank names.
- All 774 LGAs map one-to-one onto the canonical catalogue. Spelling
  differences were reviewed by hand, for example `S/BIRNI` to Sabon Birni,
  `AIYEKIRE` to Gbonyin, and `KOGI . K. K.` to Koton Karfe.
- An independent INEC list for Zaria LGA (Kaduna) agrees on all 584
  polling unit codes and on every ward name and count.

**Codes** follow INEC's printed format with hyphens:

- LGA: `18-23` (Zaria)
- Ward: `18-23-01`
- Polling unit: `18-23-01-001`

**Not included:** registered voters and coordinates. The upstream dataset
has none, because the available geocodes were unreliable. Add them per polling
unit from verified sources. The loader never overwrites these fields.

**To refresh**, replace the file (same columns) and run
`uv run python manage.py load_polling_units`.

## inec_locations.csv.gz

Polling unit coordinates from INEC's public Polling Unit Locator, applied by
`uv run python manage.py load_inec_locations`.

| | |
|---|---|
| Source | INEC Polling Unit Locator, https://cvr.inecnigeria.org/pu. Its GET DIRECTIONS button redirects to a Google Maps link holding the unit's coordinates. |
| Collected with | `scripts/inec_locator.py`, one request per unit: Kaduna on 2026-10-11, Osun on 2026-10-11 and 12 |
| Checked with | `scripts/check_inec_locations.py`, against the register above and the Flutter app's LGA and ward boundaries |
| Coverage | **All of Kaduna State: 23 LGAs, 8,012 units, 7,770 located (97.0%). All of Osun State: 30 LGAs, 3,763 units, 3,612 located (96.0%).** Other states are added by collecting them and re-running the check. See **Coverage** below. |

Columns: `code`, `latitude`, `longitude` (exactly as INEC wrote them), `qa`,
`fetched_at`.

**Coverage.** The check prints one table for the whole file; it is split by
state here. Replace the rows whenever LGAs are added.

**Kaduna State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Birnin Gwari | 263 | 256 | 18 | 1 | 5 | 2 |
| Chikun | 543 | 538 | 53 | 4 | 5 | 0 |
| Giwa | 262 | 258 | 26 | 1 | 4 | 0 |
| Igabi | 600 | 599 | 33 | 3 | 0 | 1 |
| Ikara | 251 | 238 | 16 | 0 | 13 | 0 |
| Jaba | 132 | 128 | 9 | 0 | 4 | 0 |
| Jema'a | 325 | 319 | 14 | 1 | 6 | 0 |
| Kachia | 317 | 301 | 3 | 1 | 5 | 11 |
| Kaduna North | 704 | 703 | 37 | 0 | 1 | 0 |
| Kaduna South | 796 | 796 | 182 | 3 | 0 | 0 |
| Kagarko | 222 | 209 | 19 | 0 | 13 | 0 |
| Kajuru | 188 | 180 | 7 | 1 | 7 | 1 |
| Kaura | 186 | 182 | 8 | 0 | 4 | 0 |
| Kauru | 243 | 235 | 7 | 0 | 8 | 0 |
| Kubau | 310 | 306 | 35 | 0 | 4 | 0 |
| Kudan | 193 | 183 | 2 | 0 | 10 | 0 |
| Lere | 457 | 407 | 43 | 1 | 50 | 0 |
| Makarfi | 208 | 201 | 11 | 0 | 7 | 0 |
| Sabon Gari | 398 | 398 | 28 | 0 | 0 | 0 |
| Sanga | 195 | 130 | 4 | 0 | 65 | 0 |
| Soba | 298 | 287 | 4 | 0 | 11 | 0 |
| Zangon Kataf | 337 | 332 | 36 | 1 | 3 | 2 |
| Zaria | 584 | 584 | 41 | 0 | 0 | 0 |
| **Total** | **8,012** | **7,770** | **636** | **17** | **225** | **17** |

**Osun State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Atakumosa East | 77 | 58 | 2 | 13 | 14 | 5 |
| Atakumosa West | 76 | 75 | 5 | 5 | 1 | 0 |
| Ayedaade | 150 | 146 | 11 | 1 | 4 | 0 |
| Ayedire | 67 | 66 | 14 | 1 | 1 | 0 |
| Boluwaduro | 74 | 67 | 14 | 3 | 7 | 0 |
| Boripe | 112 | 108 | 26 | 2 | 1 | 3 |
| Ede North | 139 | 139 | 29 | 3 | 0 | 0 |
| Ede South | 101 | 89 | 8 | 13 | 12 | 0 |
| Egbedore | 90 | 89 | 4 | 1 | 1 | 0 |
| Ejigbo | 133 | 133 | 6 | 3 | 0 | 0 |
| Ife Central | 215 | 215 | 42 | 0 | 0 | 0 |
| Ife East | 200 | 196 | 22 | 11 | 2 | 2 |
| Ife North | 111 | 109 | 16 | 8 | 0 | 2 |
| Ife South | 131 | 130 | 10 | 5 | 1 | 0 |
| Ifedayo | 63 | 53 | 9 | 4 | 9 | 1 |
| Ifelodun | 145 | 145 | 20 | 4 | 0 | 0 |
| Ila | 124 | 124 | 22 | 0 | 0 | 0 |
| Ilesa East | 132 | 132 | 29 | 13 | 0 | 0 |
| Ilesa West | 121 | 121 | 20 | 0 | 0 | 0 |
| Irepodun | 133 | 133 | 40 | 0 | 0 | 0 |
| Irewole | 138 | 131 | 24 | 9 | 0 | 7 |
| Isokan | 95 | 83 | 18 | 3 | 7 | 5 |
| Iwo | 170 | 153 | 28 | 0 | 17 | 0 |
| Obokun | 100 | 100 | 5 | 1 | 0 | 0 |
| Odo Otin | 121 | 115 | 14 | 0 | 6 | 0 |
| Ola Oluwa | 62 | 58 | 2 | 3 | 4 | 0 |
| Olorunda | 193 | 184 | 3 | 114 | 9 | 0 |
| Oriade | 123 | 114 | 9 | 2 | 9 | 0 |
| Orolu | 85 | 82 | 6 | 10 | 3 | 0 |
| Osogbo | 282 | 264 | 44 | 2 | 18 | 0 |
| **Total** | **3,763** | **3,612** | **502** | **234** | **126** | **25** |

- **Located** units are imported. **Outside own ward** counts located units
  that fall just outside their ward on GRID3's operational ward map, whose
  lines do not always follow INEC's. They are kept (`qa = ok_outside_ward`).
- **In next LGA** counts located units whose point lies more than 1 km
  outside their LGA but inside a neighbouring LGA of the same state, no more
  than 5 km beyond their own (`qa = ok_neighbouring_lga`). Border towns are
  often drawn on the wrong side of the line: 112 of Olorunda's 114 lie in
  Osogbo LGA as the app's boundaries draw it, in the part of Osogbo town that
  INEC counts in Olorunda. Half of these points are within about 2 km of
  their own LGA. They are imported with a note (`location_note`) that the
  app shows with the directions, asking whoever goes there to check the
  point; a coordinator's pin clears it.
- **No INEC location:** the locator has no coordinates for these units and
  only offers a Google Maps search by name. Coordinators pin them.
- **Rejected:** INEC's point is more than 1 km outside the unit's LGA and
  not just inside a neighbouring LGA of the same state. Of the 42 (17 in
  Kaduna, 25 in Osun), 39 are more than 5 km out and 3 fall in a neighbouring
  state. These units stay unmapped until a coordinator pins them.
- No spot is shared by units of three or more wards. A few pairs share a spot,
  as two units at one school would.
- Spot check: unit `18-23-01-014` (UNG. ASKA I/ L.E.A. ANGUWAN KAHU) matches
  the location seen in the locator by hand (11.0539, 7.712955).

**Precision.** The locator drops trailing zeros, so `11.064` means `11.0640`.
Across Kaduna, 4,758 latitudes had 4 decimal places, 485 had 3, 54 had 2, 7 had
1 and 1 had none: the 10:1 pattern that dropped zeros produce. Others have 5 to
9 places. Four places is at most about 6 m off, so the loader treats every
coordinate as at least that precise, stores the margin as the unit's
`location_accuracy_m`, and allows it to a coordinator's confirming pin.

**Rejected by the check** (`qa` values that are not imported):
`outside_lga` (more than 1 km outside the unit's LGA, and not within 5 km
inside a neighbouring LGA of the same state), `shared_spot` (one spot
claimed by units of three or more wards, a placeholder rather than a
building), `not_in_register`, `no_location` and `outside_nigeria`.

**Not the 2022 public file.** The `mykeels/inec-polling-units` file, scraped in
2022, has coordinates for 67% of units, but most are town or LGA centres:
nationally 24% of its points are outside their own LGA and 59% outside their
own ward. The locator's current data is far better, as the coverage table shows.

**To extend**, collect more units, check them (the check merges them into this
file and keeps the LGAs already here), and reload. `--complete-only` takes just
the LGAs whose units have all been collected, so it is safe to run while the
collector is still working:

```bash
python scripts/inec_locator.py --out inec_locator.csv --state 18
uv run --with shapely python scripts/check_inec_locations.py \
    --locations inec_locator.csv --geo ../TheGameChangerGroup/assets/geo --complete-only
uv run python manage.py load_inec_locations
```
