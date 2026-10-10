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
