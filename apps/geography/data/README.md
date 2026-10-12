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
| Coverage | **All of Kaduna State: 23 LGAs, 8,012 units, 7,753 located (96.8%). All of Osun State: 30 LGAs, 3,763 units, 3,378 located (89.8%).** Other states are added by collecting them and re-running the check. See **Coverage** below. |

Columns: `code`, `latitude`, `longitude` (exactly as INEC wrote them), `qa`,
`fetched_at`.

**Coverage.** The check prints one table for the whole file; it is split by
state here. Replace the rows whenever LGAs are added.

**Kaduna State** (complete)

| LGA | Units | Located | Outside own ward | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|
| Birnin Gwari | 263 | 255 | 18 | 5 | 3 |
| Chikun | 543 | 534 | 53 | 5 | 4 |
| Giwa | 262 | 257 | 26 | 4 | 1 |
| Igabi | 600 | 596 | 33 | 0 | 4 |
| Ikara | 251 | 238 | 16 | 13 | 0 |
| Jaba | 132 | 128 | 9 | 4 | 0 |
| Jema'a | 325 | 318 | 14 | 6 | 1 |
| Kachia | 317 | 300 | 3 | 5 | 12 |
| Kaduna North | 704 | 703 | 37 | 1 | 0 |
| Kaduna South | 796 | 793 | 182 | 0 | 3 |
| Kagarko | 222 | 209 | 19 | 13 | 0 |
| Kajuru | 188 | 179 | 7 | 7 | 2 |
| Kaura | 186 | 182 | 8 | 4 | 0 |
| Kauru | 243 | 235 | 7 | 8 | 0 |
| Kubau | 310 | 306 | 35 | 4 | 0 |
| Kudan | 193 | 183 | 2 | 10 | 0 |
| Lere | 457 | 406 | 43 | 50 | 1 |
| Makarfi | 208 | 201 | 11 | 7 | 0 |
| Sabon Gari | 398 | 398 | 28 | 0 | 0 |
| Sanga | 195 | 130 | 4 | 65 | 0 |
| Soba | 298 | 287 | 4 | 11 | 0 |
| Zangon Kataf | 337 | 331 | 36 | 3 | 3 |
| Zaria | 584 | 584 | 41 | 0 | 0 |
| **Total** | **8,012** | **7,753** | **636** | **225** | **34** |

**Osun State** (complete)

| LGA | Units | Located | Outside own ward | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|
| Atakumosa East | 77 | 45 | 2 | 14 | 18 |
| Atakumosa West | 76 | 70 | 5 | 1 | 5 |
| Ayedaade | 150 | 145 | 11 | 4 | 1 |
| Ayedire | 67 | 65 | 14 | 1 | 1 |
| Boluwaduro | 74 | 64 | 14 | 7 | 3 |
| Boripe | 112 | 106 | 26 | 1 | 5 |
| Ede North | 139 | 136 | 29 | 0 | 3 |
| Ede South | 101 | 76 | 8 | 12 | 13 |
| Egbedore | 90 | 88 | 4 | 1 | 1 |
| Ejigbo | 133 | 130 | 6 | 0 | 3 |
| Ife Central | 215 | 215 | 42 | 0 | 0 |
| Ife East | 200 | 185 | 22 | 2 | 13 |
| Ife North | 111 | 101 | 16 | 0 | 10 |
| Ife South | 131 | 125 | 10 | 1 | 5 |
| Ifedayo | 63 | 49 | 9 | 9 | 5 |
| Ifelodun | 145 | 141 | 20 | 0 | 4 |
| Ila | 124 | 124 | 22 | 0 | 0 |
| Ilesa East | 132 | 119 | 29 | 0 | 13 |
| Ilesa West | 121 | 121 | 20 | 0 | 0 |
| Irepodun | 133 | 133 | 40 | 0 | 0 |
| Irewole | 138 | 122 | 24 | 0 | 16 |
| Isokan | 95 | 80 | 18 | 7 | 8 |
| Iwo | 170 | 153 | 28 | 17 | 0 |
| Obokun | 100 | 99 | 5 | 0 | 1 |
| Odo Otin | 121 | 115 | 14 | 6 | 0 |
| Ola Oluwa | 62 | 55 | 2 | 4 | 3 |
| Olorunda | 193 | 70 | 3 | 9 | 114 |
| Oriade | 123 | 112 | 9 | 9 | 2 |
| Orolu | 85 | 72 | 6 | 3 | 10 |
| Osogbo | 282 | 262 | 44 | 18 | 2 |
| **Total** | **3,763** | **3,378** | **502** | **126** | **259** |

Osun's rejections (6.9% of units, against 0.4% in Kaduna) look like
boundary detail rather than wrong points: 256 of the 259 fall inside a
neighbouring Osun LGA, half of them less than 2.1 km beyond the unit's own
LGA, and whole wards fall together. Olorunda is most of it: 112 of its 114
rejected points lie in Osogbo LGA as drawn in the app's boundaries, across
almost all of wards 01 to 07, the part of Osogbo town that INEC counts in
Olorunda. The 1 km rule is unchanged; coordinators pin these units.

- **Located** units are imported. **Outside own ward** counts located units
  that fall just outside their ward on GRID3's operational ward map, whose
  lines do not always follow INEC's. They are kept (`qa = ok_outside_ward`).
- **No INEC location:** the locator has no coordinates for these units and
  only offers a Google Maps search by name. Coordinators pin them.
- **Rejected:** INEC's point is more than 1 km outside the unit's LGA. These
  units stay unmapped until a coordinator pins them.
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
`outside_lga` (more than 1 km outside the unit's LGA), `shared_spot` (one spot
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
