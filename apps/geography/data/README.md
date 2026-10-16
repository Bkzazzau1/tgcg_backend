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
| Collected with | `scripts/inec_locator.py`, one request per unit: Kaduna on 2026-10-11, Osun on 2026-10-11 and 12, Anambra, Bauchi, Benue, Borno, Delta, Kano, Katsina, Lagos, Oyo and Rivers on 2026-10-15 and 16, and Abia, Adamawa, Akwa Ibom, Edo, Enugu, Imo, Jigawa, Niger, Ogun and Plateau on 2026-10-16 (one collector per state, ten at once) |
| Checked with | `scripts/check_inec_locations.py`, against the register above and the Flutter app's LGA and ward boundaries |
| Coverage | **22 states, each complete: 128,853 units, 116,781 located (90.6%).** Other states are added by collecting them and re-running the check. See **Coverage** below. |

Columns: `code`, `latitude`, `longitude` (exactly as INEC wrote them), `qa`,
`fetched_at`.

**Coverage.** The check prints one table for the whole file; it is split by
state here. Replace the rows whenever LGAs are added.

| State | LGAs | Units | Located | No INEC location | Rejected | Not in the locator |
|---|---:|---:|---:|---:|---:|---:|
| Abia | 17 | 4,062 | 3,911 (96.3%) | 91 | 0 | 60 |
| Adamawa | 21 | 4,104 | 3,965 (96.6%) | 116 | 7 | 16 |
| Akwa Ibom | 31 | 4,353 | 4,292 (98.6%) | 51 | 10 | 0 |
| Anambra | 21 | 5,720 | 5,550 (97.0%) | 161 | 9 | 0 |
| Bauchi | 20 | 5,423 | 4,979 (91.8%) | 444 | 0 | 0 |
| Benue | 23 | 5,102 | 4,232 (82.9%) | 765 | 105 | 0 |
| Borno | 27 | 5,071 | 3,992 (78.7%) | 694 | 385 | 0 |
| Delta | 25 | 5,863 | 4,519 (77.1%) | 1,044 | 269 | 31 |
| Edo | 18 | 4,519 | 4,179 (92.5%) | 307 | 20 | 13 |
| Enugu | 17 | 4,145 | 4,017 (96.9%) | 89 | 39 | 0 |
| Imo | 27 | 4,758 | 4,219 (88.7%) | 531 | 8 | 0 |
| Jigawa | 27 | 4,522 | 3,995 (88.3%) | 512 | 15 | 0 |
| Kaduna | 23 | 8,012 | 7,770 (97.0%) | 225 | 17 | 0 |
| Kano | 44 | 11,222 | 8,078 (72.0%) | 2,637 | 500 | 7 |
| Katsina | 34 | 6,652 | 6,161 (92.6%) | 487 | 2 | 2 |
| Lagos | 20 | 13,325 | 12,389 (93.0%) | 924 | 12 | 0 |
| Niger | 25 | 4,950 | 4,325 (87.4%) | 500 | 125 | 0 |
| Ogun | 20 | 5,042 | 4,949 (98.2%) | 81 | 11 | 1 |
| Osun | 30 | 3,763 | 3,612 (96.0%) | 126 | 25 | 0 |
| Oyo | 33 | 6,390 | 6,029 (94.4%) | 286 | 73 | 2 |
| Plateau | 17 | 4,989 | 4,882 (97.9%) | 87 | 18 | 2 |
| Rivers | 23 | 6,866 | 6,736 (98.1%) | 83 | 47 | 0 |
| **Total** | **543** | **128,853** | **116,781 (90.6%)** | **10,241** | **1,697** | **134** |

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

**Anambra State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Aguata | 342 | 332 | 0 | 0 | 10 | 0 |
| Anambra East | 241 | 240 | 0 | 0 | 1 | 0 |
| Anambra West | 164 | 160 | 0 | 0 | 4 | 0 |
| Anaocha | 320 | 320 | 0 | 8 | 0 | 0 |
| Awka North | 150 | 150 | 0 | 0 | 0 | 0 |
| Awka South | 390 | 383 | 0 | 2 | 7 | 0 |
| Ayamelum | 191 | 188 | 0 | 0 | 3 | 0 |
| Dunukofia | 165 | 162 | 0 | 0 | 3 | 0 |
| Ekwusigo | 193 | 193 | 0 | 25 | 0 | 0 |
| Idemili North | 467 | 463 | 0 | 33 | 4 | 0 |
| Idemili South | 243 | 242 | 0 | 1 | 0 | 1 |
| Ihiala | 323 | 307 | 0 | 14 | 16 | 0 |
| Njikoka | 231 | 231 | 0 | 18 | 0 | 0 |
| Nnewi North | 318 | 310 | 0 | 0 | 8 | 0 |
| Nnewi South | 297 | 294 | 0 | 1 | 3 | 0 |
| Ogbaru | 383 | 364 | 0 | 0 | 17 | 2 |
| Onitsha North | 313 | 306 | 0 | 0 | 3 | 4 |
| Onitsha South | 321 | 299 | 0 | 1 | 20 | 2 |
| Orumba North | 253 | 247 | 0 | 6 | 6 | 0 |
| Orumba South | 208 | 206 | 0 | 6 | 2 | 0 |
| Oyi | 207 | 153 | 0 | 1 | 54 | 0 |
| **Total** | **5,720** | **5,550** | **0** | **116** | **161** | **9** |

**Bauchi State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Alkaleri | 300 | 290 | 72 | 0 | 10 | 0 |
| Bauchi | 894 | 885 | 112 | 0 | 9 | 0 |
| Bogoro | 119 | 107 | 13 | 0 | 12 | 0 |
| Dambam | 163 | 149 | 34 | 0 | 14 | 0 |
| Darazo | 270 | 259 | 11 | 0 | 11 | 0 |
| Dass | 116 | 114 | 18 | 0 | 2 | 0 |
| Gamawa | 289 | 265 | 25 | 0 | 24 | 0 |
| Ganjuwa | 253 | 147 | 22 | 0 | 106 | 0 |
| Giade | 145 | 145 | 31 | 0 | 0 | 0 |
| Itas Gadau | 235 | 225 | 59 | 0 | 10 | 0 |
| Jama'are | 133 | 129 | 28 | 0 | 4 | 0 |
| Katagum | 364 | 360 | 135 | 1 | 4 | 0 |
| Kirfi | 154 | 151 | 37 | 1 | 3 | 0 |
| Misau | 256 | 254 | 33 | 0 | 2 | 0 |
| Ningi | 371 | 352 | 32 | 0 | 19 | 0 |
| Shira | 249 | 239 | 36 | 0 | 10 | 0 |
| Tafawa Balewa | 286 | 261 | 10 | 0 | 25 | 0 |
| Toro | 423 | 409 | 14 | 0 | 14 | 0 |
| Warji | 142 | 0 | 0 | 0 | 142 | 0 |
| Zaki | 261 | 238 | 41 | 0 | 23 | 0 |
| **Total** | **5,423** | **4,979** | **763** | **2** | **444** | **0** |

**Benue State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Ado | 157 | 146 | 0 | 11 | 7 | 4 |
| Agatu | 112 | 96 | 0 | 0 | 16 | 0 |
| Apa | 124 | 82 | 0 | 5 | 37 | 5 |
| Buruku | 240 | 236 | 0 | 13 | 4 | 0 |
| Gboko | 475 | 466 | 0 | 20 | 8 | 1 |
| Guma | 200 | 186 | 0 | 4 | 4 | 10 |
| Gwer East | 189 | 187 | 0 | 10 | 1 | 1 |
| Gwer West | 123 | 122 | 0 | 24 | 0 | 1 |
| Katsina Ala | 296 | 187 | 0 | 8 | 108 | 1 |
| Konshisha | 254 | 213 | 0 | 13 | 41 | 0 |
| Kwande | 339 | 89 | 0 | 0 | 250 | 0 |
| Logo | 184 | 123 | 0 | 2 | 60 | 1 |
| Makurdi | 562 | 549 | 0 | 14 | 3 | 10 |
| Obi | 120 | 118 | 0 | 18 | 1 | 1 |
| Ogbadibo | 142 | 117 | 0 | 1 | 25 | 0 |
| Ohimini | 87 | 70 | 0 | 4 | 17 | 0 |
| Oju | 204 | 157 | 0 | 0 | 47 | 0 |
| Okpokwu | 147 | 119 | 0 | 10 | 22 | 6 |
| Otukpo | 280 | 270 | 0 | 9 | 9 | 1 |
| Tarka | 96 | 90 | 0 | 2 | 0 | 6 |
| Ukum | 272 | 190 | 0 | 8 | 82 | 0 |
| Ushongo | 213 | 185 | 0 | 31 | 2 | 26 |
| Vandeikya | 286 | 234 | 0 | 27 | 21 | 31 |
| **Total** | **5,102** | **4,232** | **0** | **234** | **765** | **105** |

**Borno State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Abadam | 93 | 61 | 8 | 2 | 32 | 0 |
| Askira Uba | 260 | 224 | 10 | 0 | 36 | 0 |
| Bama | 318 | 220 | 73 | 10 | 50 | 48 |
| Bayo | 95 | 48 | 0 | 0 | 34 | 13 |
| Biu | 270 | 174 | 39 | 0 | 50 | 46 |
| Chibok | 118 | 94 | 0 | 0 | 17 | 7 |
| Damboa | 189 | 97 | 6 | 4 | 77 | 15 |
| Dikwa | 107 | 76 | 18 | 8 | 18 | 13 |
| Gubio | 121 | 107 | 10 | 1 | 14 | 0 |
| Guzamala | 87 | 72 | 13 | 2 | 15 | 0 |
| Gwoza | 304 | 252 | 24 | 0 | 35 | 17 |
| Hawul | 215 | 161 | 2 | 0 | 3 | 51 |
| Jere | 386 | 340 | 50 | 72 | 0 | 46 |
| Kaga | 109 | 93 | 2 | 0 | 10 | 6 |
| Kala Balge | 100 | 72 | 34 | 0 | 28 | 0 |
| Konduga | 230 | 209 | 44 | 40 | 5 | 16 |
| Kukawa | 131 | 74 | 23 | 0 | 56 | 1 |
| Kwaya Kusar | 105 | 76 | 3 | 0 | 3 | 26 |
| Mafa | 127 | 82 | 11 | 0 | 19 | 26 |
| Magumeri | 99 | 69 | 5 | 2 | 18 | 12 |
| Maiduguri | 886 | 860 | 115 | 64 | 5 | 21 |
| Marte | 135 | 109 | 23 | 2 | 26 | 0 |
| Mobbar | 98 | 61 | 22 | 0 | 37 | 0 |
| Monguno | 109 | 73 | 5 | 3 | 36 | 0 |
| Ngala | 141 | 86 | 21 | 0 | 55 | 0 |
| Nganzai | 94 | 85 | 0 | 4 | 9 | 0 |
| Shani | 144 | 117 | 25 | 0 | 6 | 21 |
| **Total** | **5,071** | **3,992** | **586** | **214** | **694** | **385** |

**Delta State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Aniocha North | 153 | 153 | 10 | 11 | 0 | 0 |
| Aniocha South | 190 | 165 | 16 | 2 | 25 | 0 |
| Bomadi | 192 | 79 | 10 | 8 | 93 | 20 |
| Burutu | 233 | 10 | 0 | 0 | 221 | 2 |
| Ethiope East | 220 | 220 | 183 | 1 | 0 | 0 |
| Ethiope West | 220 | 165 | 120 | 9 | 5 | 50 |
| Ika North East | 238 | 236 | 24 | 0 | 0 | 2 |
| Ika South | 169 | 159 | 40 | 29 | 8 | 0 |
| Isoko North | 264 | 248 | 8 | 3 | 1 | 15 |
| Isoko South | 257 | 255 | 5 | 0 | 2 | 0 |
| Ndokwa East | 158 | 154 | 6 | 4 | 4 | 0 |
| Ndokwa West | 203 | 199 | 59 | 7 | 3 | 1 |
| Okpe | 236 | 235 | 0 | 9 | 1 | 0 |
| Oshimili North | 165 | 165 | 4 | 5 | 0 | 0 |
| Oshimili South | 319 | 319 | 55 | 2 | 0 | 0 |
| Patani | 99 | 81 | 7 | 2 | 0 | 0 |
| Sapele | 268 | 119 | 6 | 5 | 61 | 88 |
| Udu | 265 | 263 | 106 | 4 | 2 | 0 |
| Ughelli North | 400 | 398 | 43 | 0 | 0 | 2 |
| Ughelli South | 244 | 155 | 46 | 1 | 77 | 1 |
| Ukwuani | 168 | 168 | 12 | 15 | 0 | 0 |
| Uvwie | 327 | 327 | 69 | 30 | 0 | 0 |
| Warri North | 192 | 0 | 0 | 0 | 143 | 49 |
| Warri South | 351 | 164 | 45 | 0 | 187 | 0 |
| Warri South West | 332 | 82 | 15 | 29 | 211 | 39 |
| **Total** | **5,863** | **4,519** | **889** | **176** | **1,044** | **269** |

**Kano State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Ajingi | 175 | 167 | 18 | 0 | 8 | 0 |
| Albasu | 185 | 180 | 7 | 0 | 5 | 0 |
| Bagwai | 186 | 183 | 6 | 0 | 2 | 0 |
| Bebeji | 161 | 161 | 9 | 0 | 0 | 0 |
| Bichi | 277 | 188 | 3 | 1 | 89 | 0 |
| Bunkure | 183 | 134 | 8 | 0 | 49 | 0 |
| Dala | 668 | 664 | 117 | 4 | 4 | 0 |
| Danbata | 237 | 154 | 5 | 2 | 83 | 0 |
| Dawaki Kudu | 303 | 298 | 13 | 0 | 1 | 4 |
| Dawaki Tofa | 224 | 203 | 7 | 0 | 21 | 0 |
| Doguwa | 149 | 111 | 3 | 0 | 38 | 0 |
| Fagge | 521 | 358 | 55 | 0 | 163 | 0 |
| Gabasawa | 164 | 156 | 3 | 0 | 8 | 0 |
| Garko | 190 | 142 | 25 | 0 | 48 | 0 |
| Garun Malam | 151 | 133 | 5 | 0 | 18 | 0 |
| Gaya | 206 | 168 | 1 | 0 | 38 | 0 |
| Gezawa | 229 | 165 | 6 | 0 | 64 | 0 |
| Gwale | 450 | 243 | 103 | 0 | 207 | 0 |
| Gwarzo | 210 | 201 | 13 | 0 | 9 | 0 |
| Kabo | 188 | 155 | 3 | 0 | 33 | 0 |
| Kano Municipal | 630 | 470 | 141 | 0 | 160 | 0 |
| Karaye | 169 | 155 | 12 | 0 | 14 | 0 |
| Kibiya | 149 | 119 | 7 | 0 | 30 | 0 |
| Kiru | 246 | 184 | 7 | 0 | 62 | 0 |
| Kumbotso | 410 | 190 | 7 | 0 | 220 | 0 |
| Kunchi | 142 | 135 | 9 | 0 | 7 | 0 |
| Kura | 217 | 169 | 16 | 0 | 48 | 0 |
| Madobi | 171 | 31 | 0 | 0 | 44 | 96 |
| Makoda | 145 | 24 | 0 | 24 | 49 | 72 |
| Minjibir | 172 | 38 | 2 | 1 | 25 | 103 |
| Nasarawa | 817 | 483 | 46 | 0 | 334 | 0 |
| Rano | 165 | 163 | 11 | 1 | 2 | 0 |
| Rimin Gado | 136 | 124 | 10 | 0 | 12 | 0 |
| Rogo | 220 | 137 | 8 | 0 | 83 | 0 |
| Shanono | 151 | 149 | 3 | 0 | 2 | 0 |
| Sumaila | 221 | 220 | 7 | 0 | 1 | 0 |
| Takai | 251 | 188 | 26 | 0 | 63 | 0 |
| Tarauni | 425 | 218 | 32 | 1 | 207 | 0 |
| Tofa | 147 | 147 | 8 | 0 | 0 | 0 |
| Tsanyawa | 157 | 0 | 0 | 0 | 43 | 114 |
| Tudun Wada | 281 | 38 | 2 | 0 | 132 | 111 |
| Ungogo | 384 | 205 | 7 | 0 | 179 | 0 |
| Warawa | 143 | 111 | 36 | 0 | 32 | 0 |
| Wudil | 216 | 216 | 13 | 0 | 0 | 0 |
| **Total** | **11,222** | **8,078** | **820** | **34** | **2,637** | **500** |

**Katsina State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Bakori | 242 | 242 | 7 | 0 | 0 | 0 |
| Batagarawa | 186 | 184 | 23 | 0 | 1 | 1 |
| Batsari | 219 | 208 | 5 | 0 | 11 | 0 |
| Baure | 212 | 156 | 2 | 0 | 56 | 0 |
| Bindawa | 169 | 169 | 4 | 1 | 0 | 0 |
| Charanchi | 141 | 138 | 8 | 0 | 3 | 0 |
| Dan Musa | 165 | 153 | 9 | 0 | 12 | 0 |
| Dandume | 177 | 171 | 3 | 0 | 6 | 0 |
| Danja | 192 | 171 | 12 | 0 | 21 | 0 |
| Daura | 185 | 180 | 31 | 0 | 5 | 0 |
| Dutsi | 118 | 85 | 10 | 0 | 33 | 0 |
| Dutsin Ma | 209 | 206 | 16 | 0 | 3 | 0 |
| Faskari | 231 | 201 | 15 | 0 | 30 | 0 |
| Funtua | 298 | 265 | 15 | 0 | 33 | 0 |
| Ingawa | 170 | 166 | 8 | 0 | 4 | 0 |
| Jibia | 203 | 192 | 29 | 0 | 11 | 0 |
| Kafur | 270 | 184 | 4 | 0 | 86 | 0 |
| Kaita | 149 | 145 | 10 | 1 | 4 | 0 |
| Kankara | 256 | 239 | 5 | 0 | 17 | 0 |
| Kankia | 157 | 148 | 4 | 0 | 9 | 0 |
| Katsina | 501 | 476 | 74 | 0 | 25 | 0 |
| Kurfi | 140 | 138 | 4 | 0 | 2 | 0 |
| Kusada | 114 | 113 | 9 | 0 | 1 | 0 |
| Mai'Adua | 186 | 176 | 11 | 0 | 9 | 0 |
| Malumfashi | 325 | 323 | 10 | 0 | 1 | 0 |
| Mani | 207 | 206 | 3 | 1 | 1 | 0 |
| Mashi | 186 | 181 | 4 | 0 | 4 | 1 |
| Matazu | 132 | 132 | 2 | 0 | 0 | 0 |
| Musawa | 161 | 91 | 5 | 0 | 70 | 0 |
| Rimi | 178 | 175 | 3 | 0 | 3 | 0 |
| Sabuwa | 125 | 119 | 0 | 0 | 6 | 0 |
| Safana | 168 | 161 | 7 | 0 | 7 | 0 |
| Sandamu | 145 | 142 | 3 | 0 | 3 | 0 |
| Zango | 135 | 125 | 2 | 0 | 10 | 0 |
| **Total** | **6,652** | **6,161** | **357** | **3** | **487** | **2** |

**Lagos State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Agege | 696 | 696 | 0 | 1 | 0 | 0 |
| Ajeromi Ifelodun | 734 | 734 | 0 | 0 | 0 | 0 |
| Alimosho | 1,545 | 1,442 | 0 | 0 | 101 | 2 |
| Amuwo Odofin | 637 | 637 | 0 | 5 | 0 | 0 |
| Apapa | 374 | 219 | 0 | 0 | 155 | 0 |
| Badagry | 412 | 356 | 0 | 0 | 56 | 0 |
| Epe | 374 | 350 | 0 | 8 | 18 | 6 |
| Eti Osa | 725 | 576 | 0 | 1 | 148 | 1 |
| Ibeju Lekki | 227 | 143 | 0 | 2 | 82 | 2 |
| Ifako Ijaye | 761 | 582 | 0 | 0 | 179 | 0 |
| Ikeja | 667 | 642 | 0 | 0 | 25 | 0 |
| Ikorodu | 682 | 658 | 0 | 0 | 24 | 0 |
| Kosofe | 899 | 885 | 0 | 1 | 14 | 0 |
| Lagos Island | 389 | 389 | 0 | 0 | 0 | 0 |
| Lagos Mainland | 493 | 492 | 0 | 0 | 1 | 0 |
| Mushin | 865 | 861 | 0 | 0 | 3 | 1 |
| Ojo | 582 | 576 | 0 | 8 | 6 | 0 |
| Oshodi Isolo | 890 | 778 | 0 | 1 | 112 | 0 |
| Somolu | 616 | 616 | 0 | 0 | 0 | 0 |
| Surulere | 757 | 757 | 0 | 0 | 0 | 0 |
| **Total** | **13,325** | **12,389** | **0** | **27** | **924** | **12** |

**Oyo State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Afijio | 95 | 92 | 9 | 23 | 3 | 0 |
| Akinyele | 278 | 273 | 13 | 21 | 1 | 4 |
| Atiba | 168 | 164 | 19 | 22 | 0 | 4 |
| Atisbo | 152 | 144 | 21 | 21 | 0 | 8 |
| Egbeda | 294 | 272 | 61 | 32 | 20 | 2 |
| Ibadan North | 512 | 493 | 0 | 0 | 18 | 1 |
| Ibadan North East | 336 | 334 | 0 | 0 | 0 | 2 |
| Ibadan North West | 240 | 212 | 0 | 0 | 28 | 0 |
| Ibadan South East | 352 | 267 | 0 | 0 | 85 | 0 |
| Ibadan South West | 473 | 471 | 35 | 62 | 2 | 0 |
| Ibarapa Central | 140 | 132 | 8 | 6 | 0 | 8 |
| Ibarapa East | 148 | 147 | 8 | 1 | 1 | 0 |
| Ibarapa North | 148 | 147 | 43 | 0 | 1 | 0 |
| Ido | 196 | 136 | 12 | 2 | 59 | 1 |
| Irepo | 101 | 93 | 48 | 0 | 7 | 0 |
| Iseyin | 229 | 228 | 28 | 0 | 1 | 0 |
| Itesiwaju | 97 | 89 | 6 | 2 | 8 | 0 |
| Iwajowa | 120 | 114 | 10 | 0 | 6 | 0 |
| Kajola | 132 | 131 | 9 | 1 | 0 | 1 |
| Lagelu | 225 | 224 | 8 | 0 | 1 | 0 |
| Ogbomoso North | 188 | 188 | 26 | 0 | 0 | 0 |
| Ogbomoso South | 183 | 183 | 29 | 2 | 0 | 0 |
| Ogo Oluwa | 95 | 90 | 0 | 1 | 5 | 0 |
| Olorunsogo | 68 | 68 | 6 | 0 | 0 | 0 |
| Oluyole | 229 | 225 | 5 | 22 | 4 | 0 |
| Ona Ara | 150 | 138 | 4 | 3 | 7 | 5 |
| Orelope | 93 | 79 | 5 | 1 | 14 | 0 |
| Ori Ire | 168 | 156 | 0 | 6 | 1 | 11 |
| Oyo East | 149 | 128 | 9 | 27 | 2 | 19 |
| Oyo West | 139 | 132 | 29 | 5 | 0 | 7 |
| Saki East | 105 | 103 | 0 | 19 | 2 | 0 |
| Saki West | 228 | 221 | 29 | 2 | 7 | 0 |
| Surulere | 159 | 155 | 9 | 17 | 3 | 0 |
| **Total** | **6,390** | **6,029** | **489** | **298** | **286** | **73** |

**Rivers State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Abua/Odual | 214 | 208 | 0 | 0 | 6 | 0 |
| Ahoada East | 176 | 176 | 0 | 0 | 0 | 0 |
| Ahoada West | 171 | 154 | 0 | 23 | 17 | 0 |
| Akuku Toru | 244 | 244 | 0 | 3 | 0 | 0 |
| Andoni | 265 | 253 | 0 | 1 | 12 | 0 |
| Asari Toru | 251 | 251 | 0 | 0 | 0 | 0 |
| Bonny | 165 | 158 | 0 | 13 | 1 | 6 |
| Degema | 178 | 139 | 0 | 42 | 0 | 39 |
| Eleme | 233 | 231 | 0 | 5 | 2 | 0 |
| Emohua | 280 | 280 | 0 | 0 | 0 | 0 |
| Etche | 271 | 263 | 0 | 0 | 8 | 0 |
| Gokana | 260 | 260 | 0 | 0 | 0 | 0 |
| Ikwerre | 312 | 305 | 0 | 1 | 7 | 0 |
| Khana | 359 | 358 | 0 | 0 | 1 | 0 |
| Obio/Akpor | 1,211 | 1,211 | 0 | 0 | 0 | 0 |
| Ogba/Egbema/Ndoni | 335 | 328 | 0 | 21 | 5 | 2 |
| Ogu/Bolo | 131 | 131 | 0 | 3 | 0 | 0 |
| Okrika | 170 | 170 | 0 | 0 | 0 | 0 |
| Omuma | 167 | 167 | 0 | 0 | 0 | 0 |
| Opobo/Nkoro | 154 | 150 | 0 | 0 | 4 | 0 |
| Oyigbo | 217 | 217 | 0 | 0 | 0 | 0 |
| Port Harcourt | 961 | 941 | 0 | 3 | 20 | 0 |
| Tai | 141 | 141 | 0 | 0 | 0 | 0 |
| **Total** | **6,866** | **6,736** | **0** | **115** | **83** | **47** |

**Abia State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Aba North | 503 | 501 | 201 | 24 | 2 | 0 |
| Aba South | 518 | 493 | 233 | 3 | 25 | 0 |
| Arochukwu | 183 | 167 | 62 | 0 | 5 | 0 |
| Bende | 214 | 211 | 62 | 26 | 3 | 0 |
| Ikwuano | 143 | 138 | 44 | 2 | 1 | 0 |
| Isiala Ngwa North | 185 | 177 | 44 | 9 | 1 | 0 |
| Isiala Ngwa South | 194 | 183 | 62 | 15 | 10 | 0 |
| Isuikwuato | 149 | 149 | 44 | 10 | 0 | 0 |
| Obingwa | 312 | 302 | 46 | 10 | 0 | 0 |
| Ohafia | 282 | 270 | 15 | 0 | 2 | 0 |
| Osisioma | 301 | 280 | 154 | 0 | 12 | 0 |
| Ugwunagbo | 160 | 159 | 48 | 6 | 1 | 0 |
| Ukwa East | 85 | 81 | 7 | 0 | 4 | 0 |
| Ukwa West | 129 | 113 | 41 | 2 | 16 | 0 |
| Umu Nneochi | 153 | 145 | 16 | 0 | 8 | 0 |
| Umuahia North | 378 | 370 | 181 | 0 | 0 | 0 |
| Umuahia South | 173 | 172 | 21 | 0 | 1 | 0 |
| **Total** | **4,062** | **3,911** | **1,281** | **107** | **91** | **0** |

**Adamawa State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Demsa | 177 | 174 | 12 | 0 | 3 | 0 |
| Fufore | 274 | 268 | 8 | 1 | 6 | 0 |
| Ganye | 198 | 181 | 4 | 0 | 1 | 0 |
| Girei | 152 | 147 | 7 | 6 | 1 | 4 |
| Gombi | 180 | 177 | 4 | 0 | 3 | 0 |
| Guyuk | 150 | 137 | 2 | 0 | 13 | 0 |
| Hong | 225 | 219 | 24 | 3 | 3 | 3 |
| Jada | 215 | 200 | 13 | 1 | 15 | 0 |
| Lamurde | 152 | 151 | 24 | 0 | 1 | 0 |
| Madagali | 160 | 149 | 5 | 0 | 11 | 0 |
| Maiha | 122 | 119 | 9 | 0 | 3 | 0 |
| Mayo Belwa | 206 | 199 | 1 | 1 | 7 | 0 |
| Michika | 223 | 216 | 9 | 0 | 7 | 0 |
| Mubi North | 276 | 272 | 9 | 0 | 4 | 0 |
| Mubi South | 143 | 130 | 4 | 0 | 13 | 0 |
| Numan | 170 | 162 | 13 | 0 | 8 | 0 |
| Shelleng | 124 | 114 | 2 | 0 | 10 | 0 |
| Song | 207 | 207 | 6 | 1 | 0 | 0 |
| Toungo | 101 | 95 | 4 | 0 | 6 | 0 |
| Yola North | 377 | 376 | 15 | 0 | 1 | 0 |
| Yola South | 272 | 272 | 11 | 0 | 0 | 0 |
| **Total** | **4,104** | **3,965** | **186** | **13** | **116** | **7** |

**Akwa Ibom State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Abak | 186 | 184 | 0 | 8 | 2 | 0 |
| Eastern Obolo | 57 | 57 | 0 | 0 | 0 | 0 |
| Eket | 203 | 203 | 0 | 0 | 0 | 0 |
| Esit Eket | 95 | 95 | 0 | 0 | 0 | 0 |
| Essien Udim | 219 | 215 | 0 | 2 | 4 | 0 |
| Etim Ekpo | 111 | 110 | 0 | 0 | 1 | 0 |
| Etinan | 146 | 145 | 0 | 3 | 1 | 0 |
| Ibeno | 71 | 68 | 0 | 0 | 3 | 0 |
| Ibesikpo Asutan | 180 | 180 | 0 | 0 | 0 | 0 |
| Ibiono Ibom | 199 | 198 | 0 | 6 | 1 | 0 |
| Ika | 79 | 77 | 0 | 0 | 2 | 0 |
| Ikono | 168 | 168 | 0 | 32 | 0 | 0 |
| Ikot Abasi | 111 | 111 | 0 | 0 | 0 | 0 |
| Ikot Ekpene | 171 | 157 | 0 | 0 | 14 | 0 |
| Ini | 113 | 109 | 0 | 6 | 4 | 0 |
| Itu | 140 | 136 | 0 | 0 | 4 | 0 |
| Mbo | 100 | 94 | 0 | 3 | 6 | 0 |
| Mkpat Enin | 145 | 144 | 0 | 4 | 1 | 0 |
| Nsit Atai | 85 | 85 | 0 | 0 | 0 | 0 |
| Nsit Ibom | 114 | 111 | 0 | 0 | 3 | 0 |
| Nsit Ubium | 117 | 117 | 0 | 4 | 0 | 0 |
| Obot Akara | 105 | 105 | 0 | 0 | 0 | 0 |
| Okobo | 112 | 112 | 0 | 2 | 0 | 0 |
| Onna | 142 | 142 | 0 | 12 | 0 | 0 |
| Oron | 105 | 100 | 0 | 0 | 5 | 0 |
| Oruk Anam | 221 | 220 | 0 | 3 | 0 | 1 |
| Udung Uko | 61 | 61 | 0 | 0 | 0 | 0 |
| Ukanafun | 134 | 134 | 0 | 3 | 0 | 0 |
| Uruan | 166 | 166 | 0 | 2 | 0 | 0 |
| Urue-Offong/Oruko | 73 | 73 | 0 | 0 | 0 | 0 |
| Uyo | 424 | 415 | 0 | 12 | 0 | 9 |
| **Total** | **4,353** | **4,292** | **0** | **102** | **51** | **10** |

**Edo State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Akoko Edo | 241 | 141 | 0 | 0 | 100 | 0 |
| Egor | 436 | 434 | 0 | 2 | 1 | 1 |
| Esan Central | 117 | 114 | 0 | 2 | 3 | 0 |
| Esan North East | 172 | 171 | 0 | 1 | 1 | 0 |
| Esan South East | 160 | 156 | 0 | 0 | 4 | 0 |
| Esan West | 200 | 114 | 0 | 0 | 86 | 0 |
| Etsako Central | 104 | 84 | 0 | 0 | 19 | 1 |
| Etsako East | 166 | 163 | 0 | 0 | 3 | 0 |
| Etsako West | 322 | 300 | 0 | 0 | 21 | 1 |
| Igueben | 95 | 95 | 0 | 0 | 0 | 0 |
| Ikpoba Okha | 641 | 621 | 0 | 18 | 19 | 1 |
| Oredo | 619 | 583 | 0 | 17 | 20 | 3 |
| Orhionmwon | 265 | 264 | 0 | 1 | 1 | 0 |
| Ovia North East | 292 | 278 | 0 | 14 | 3 | 11 |
| Ovia South West | 195 | 185 | 0 | 0 | 9 | 1 |
| Owan East | 189 | 187 | 0 | 0 | 2 | 0 |
| Owan West | 151 | 150 | 0 | 1 | 1 | 0 |
| Uhunmwode | 154 | 139 | 0 | 0 | 14 | 1 |
| **Total** | **4,519** | **4,179** | **0** | **56** | **307** | **20** |

**Enugu State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Aninri | 153 | 149 | 67 | 0 | 3 | 1 |
| Awgu | 240 | 240 | 6 | 0 | 0 | 0 |
| Enugu East | 386 | 378 | 112 | 29 | 3 | 5 |
| Enugu North | 393 | 386 | 104 | 10 | 7 | 0 |
| Enugu South | 321 | 317 | 66 | 2 | 2 | 2 |
| Ezeagu | 180 | 178 | 44 | 0 | 2 | 0 |
| Igbo Etiti | 201 | 197 | 75 | 3 | 0 | 4 |
| Igbo Eze North | 266 | 232 | 71 | 0 | 34 | 0 |
| Igbo Eze South | 213 | 190 | 88 | 1 | 3 | 20 |
| Isi Uzo | 159 | 154 | 43 | 6 | 5 | 0 |
| Nkanu East | 164 | 164 | 26 | 1 | 0 | 0 |
| Nkanu West | 207 | 207 | 43 | 28 | 0 | 0 |
| Nsukka | 397 | 383 | 122 | 20 | 10 | 4 |
| Oji River | 175 | 162 | 47 | 0 | 13 | 0 |
| Udenu | 265 | 261 | 65 | 1 | 3 | 1 |
| Udi | 279 | 276 | 41 | 0 | 1 | 2 |
| Uzo Uwani | 146 | 143 | 20 | 0 | 3 | 0 |
| **Total** | **4,145** | **4,017** | **1,040** | **101** | **89** | **39** |

**Imo State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Aboh Mbaise | 212 | 209 | 0 | 1 | 3 | 0 |
| Ahiazu Mbaise | 185 | 184 | 0 | 4 | 0 | 1 |
| Ehime Mbano | 154 | 154 | 0 | 1 | 0 | 0 |
| Ezinihitte Mbaise | 175 | 174 | 0 | 9 | 1 | 0 |
| Ideato North | 196 | 188 | 0 | 6 | 7 | 1 |
| Ideato South | 152 | 135 | 0 | 48 | 17 | 0 |
| Ihitte Uboma | 117 | 117 | 0 | 7 | 0 | 0 |
| Ikeduru | 227 | 162 | 0 | 11 | 65 | 0 |
| Isiala Mbano | 196 | 190 | 0 | 12 | 5 | 1 |
| Isu | 123 | 122 | 0 | 0 | 1 | 0 |
| Mbaitoli | 287 | 284 | 0 | 23 | 3 | 0 |
| Ngor Okpala | 193 | 0 | 0 | 0 | 193 | 0 |
| Njaba | 134 | 134 | 0 | 8 | 0 | 0 |
| Nkwerre | 131 | 109 | 0 | 43 | 21 | 1 |
| Nwangele | 113 | 100 | 0 | 64 | 13 | 0 |
| Obowo | 128 | 127 | 0 | 5 | 1 | 0 |
| Oguta | 184 | 180 | 0 | 0 | 4 | 0 |
| Ohaji Egbema | 210 | 193 | 0 | 0 | 17 | 0 |
| Okigwe | 154 | 152 | 0 | 1 | 2 | 0 |
| Onuimo | 97 | 97 | 0 | 24 | 0 | 0 |
| Orlu | 209 | 195 | 0 | 2 | 13 | 1 |
| Orsu | 137 | 95 | 0 | 0 | 41 | 1 |
| Oru East | 171 | 170 | 0 | 8 | 1 | 0 |
| Oru West | 147 | 138 | 0 | 0 | 9 | 0 |
| Owerri Municipal | 239 | 133 | 0 | 0 | 106 | 0 |
| Owerri North | 236 | 234 | 0 | 7 | 2 | 0 |
| Owerri West | 251 | 243 | 0 | 6 | 6 | 2 |
| **Total** | **4,758** | **4,219** | **0** | **290** | **531** | **8** |

**Jigawa State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Auyo | 137 | 136 | 1 | 0 | 0 | 1 |
| Babura | 214 | 199 | 13 | 0 | 14 | 1 |
| Birnin Kudu | 322 | 303 | 30 | 0 | 19 | 0 |
| Birniwa | 148 | 146 | 1 | 0 | 2 | 0 |
| Buji | 117 | 113 | 9 | 0 | 4 | 0 |
| Dutse | 267 | 253 | 9 | 0 | 14 | 0 |
| Gagarawa | 89 | 89 | 22 | 0 | 0 | 0 |
| Garki | 172 | 163 | 10 | 1 | 4 | 5 |
| Gumel | 103 | 101 | 25 | 0 | 2 | 0 |
| Guri | 113 | 108 | 2 | 0 | 5 | 0 |
| Gwaram | 298 | 266 | 47 | 0 | 32 | 0 |
| Gwiwa | 112 | 103 | 8 | 1 | 9 | 0 |
| Hadejia | 138 | 136 | 53 | 0 | 2 | 0 |
| Jahun | 232 | 0 | 0 | 0 | 232 | 0 |
| Kafin Hausa | 239 | 227 | 12 | 0 | 6 | 6 |
| Kaugama | 161 | 159 | 2 | 0 | 2 | 0 |
| Kazaure | 144 | 139 | 11 | 0 | 5 | 0 |
| Kirika Samma | 149 | 139 | 8 | 0 | 10 | 0 |
| Kiyawa | 188 | 175 | 3 | 1 | 13 | 0 |
| Maigatari | 161 | 161 | 5 | 0 | 0 | 0 |
| Malam Madori | 154 | 151 | 6 | 0 | 2 | 1 |
| Miga | 113 | 110 | 10 | 0 | 3 | 0 |
| Ringim | 240 | 149 | 3 | 0 | 91 | 0 |
| Roni | 110 | 85 | 3 | 1 | 24 | 1 |
| Sule Tankarkar | 167 | 165 | 3 | 0 | 2 | 0 |
| Taura | 157 | 143 | 17 | 0 | 14 | 0 |
| Yankwashi | 77 | 76 | 1 | 0 | 1 | 0 |
| **Total** | **4,522** | **3,995** | **314** | **4** | **512** | **15** |

**Niger State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Agaie | 164 | 162 | 42 | 1 | 2 | 0 |
| Agwara | 70 | 70 | 5 | 0 | 0 | 0 |
| Bida | 270 | 207 | 28 | 0 | 63 | 0 |
| Borgu | 208 | 191 | 10 | 1 | 17 | 0 |
| Bosso | 249 | 245 | 20 | 6 | 1 | 3 |
| Chanchaga | 379 | 373 | 30 | 2 | 5 | 1 |
| Edatti | 119 | 118 | 1 | 0 | 0 | 1 |
| Gbako | 156 | 139 | 4 | 15 | 0 | 17 |
| Gurara | 150 | 92 | 13 | 2 | 48 | 10 |
| Katcha | 149 | 142 | 4 | 1 | 6 | 1 |
| Kontagora | 248 | 210 | 50 | 3 | 37 | 1 |
| Lapai | 193 | 184 | 21 | 1 | 5 | 4 |
| Lavun | 223 | 179 | 8 | 33 | 5 | 39 |
| Magama | 189 | 176 | 10 | 0 | 8 | 5 |
| Mariga | 214 | 183 | 12 | 1 | 28 | 3 |
| Mashegu | 205 | 196 | 6 | 12 | 8 | 1 |
| Mokwa | 187 | 161 | 10 | 15 | 4 | 22 |
| Munya | 119 | 100 | 4 | 0 | 19 | 0 |
| Paikoro | 232 | 179 | 31 | 3 | 48 | 5 |
| Rafi | 219 | 147 | 31 | 0 | 72 | 0 |
| Rijau | 178 | 168 | 19 | 0 | 9 | 1 |
| Shiroro | 257 | 176 | 10 | 5 | 73 | 8 |
| Suleja | 277 | 259 | 18 | 21 | 17 | 1 |
| Tafa | 171 | 160 | 47 | 0 | 11 | 0 |
| Wushishi | 124 | 108 | 2 | 2 | 14 | 2 |
| **Total** | **4,950** | **4,325** | **436** | **124** | **500** | **125** |

**Ogun State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Abeokuta North | 338 | 335 | 34 | 1 | 3 | 0 |
| Abeokuta South | 445 | 434 | 131 | 15 | 8 | 3 |
| Ado-Odo/Ota | 655 | 654 | 83 | 1 | 1 | 0 |
| Ewekoro | 188 | 187 | 17 | 1 | 1 | 0 |
| Ifo | 448 | 445 | 82 | 0 | 2 | 0 |
| Ijebu East | 154 | 153 | 22 | 3 | 1 | 0 |
| Ijebu North | 268 | 258 | 51 | 4 | 5 | 5 |
| Ijebu North East | 111 | 110 | 4 | 32 | 0 | 1 |
| Ijebu Ode | 161 | 161 | 28 | 1 | 0 | 0 |
| Ikenne | 126 | 123 | 20 | 0 | 3 | 0 |
| Imeko Afon | 132 | 119 | 11 | 1 | 13 | 0 |
| Ipokia | 262 | 260 | 7 | 0 | 2 | 0 |
| Obafemi Owode | 329 | 326 | 11 | 2 | 3 | 0 |
| Odeda | 182 | 180 | 6 | 17 | 2 | 0 |
| Odogbolu | 178 | 174 | 10 | 21 | 4 | 0 |
| Ogun Waterside | 126 | 93 | 12 | 0 | 31 | 2 |
| Remo North | 132 | 131 | 7 | 1 | 1 | 0 |
| Sagamu | 299 | 299 | 80 | 0 | 0 | 0 |
| Yewa North | 251 | 251 | 65 | 5 | 0 | 0 |
| Yewa South | 257 | 256 | 16 | 0 | 1 | 0 |
| **Total** | **5,042** | **4,949** | **697** | **105** | **81** | **11** |

**Plateau State** (complete)

| LGA | Units | Located | Outside own ward | In next LGA | No INEC location | Rejected |
|---|---:|---:|---:|---:|---:|---:|
| Barkin Ladi | 236 | 199 | 0 | 0 | 37 | 0 |
| Bassa | 296 | 289 | 0 | 2 | 6 | 1 |
| Bokkos | 236 | 236 | 0 | 9 | 0 | 0 |
| Jos East | 133 | 116 | 0 | 3 | 7 | 10 |
| Jos North | 913 | 913 | 0 | 0 | 0 | 0 |
| Jos South | 542 | 542 | 0 | 0 | 0 | 0 |
| Kanam | 285 | 262 | 0 | 5 | 22 | 1 |
| Kanke | 185 | 178 | 0 | 4 | 2 | 5 |
| Langtang North | 225 | 225 | 0 | 0 | 0 | 0 |
| Langtang South | 127 | 124 | 0 | 9 | 2 | 1 |
| Mangu | 409 | 408 | 0 | 1 | 1 | 0 |
| Mikang | 111 | 111 | 0 | 1 | 0 | 0 |
| Pankshin | 260 | 260 | 0 | 5 | 0 | 0 |
| Qua'an Pan | 279 | 277 | 0 | 3 | 2 | 0 |
| Riyom | 146 | 145 | 0 | 5 | 1 | 0 |
| Shendam | 329 | 329 | 0 | 1 | 0 | 0 |
| Wase | 277 | 268 | 0 | 2 | 7 | 0 |
| **Total** | **4,989** | **4,882** | **0** | **50** | **87** | **18** |

- **Located** units are imported. **Outside own ward** counts located units
  that fall just outside their ward on GRID3's operational ward map, whose
  lines do not always follow INEC's. They are kept (`qa = ok_outside_ward`).
- **In next LGA** counts located units whose point lies more than 1 km
  outside their LGA but inside a neighbouring LGA of the same state, no more
  than 5 km beyond their own (`qa = ok_neighbouring_lga`). Border towns are
  often drawn on the wrong side of the line: 112 of Olorunda's 114 lie in
  Osogbo LGA as the app's boundaries draw it, in the part of Osogbo town that
  INEC counts in Olorunda. In Imo, Nwangele and Nkwerre mirror each other: 55
  of Nwangele's 64 lie in Nkwerre and 42 of Nkwerre's 43 in Nwangele, all
  within about 4 km. Half of these points are within about 2 km of
  their own LGA. They are imported with a note (`location_note`) that the
  app shows with the directions, asking whoever goes there to check the
  point; a coordinator's pin clears it.
- **No INEC location:** the locator has no coordinates for these units and
  only offers a Google Maps search by name. Coordinators pin them. Whole
  areas can be missing: all of Warji (Bauchi), Ngor Okpala (Imo) and Jahun
  (Jigawa), most of Burutu and Warri North (Delta), much of Kano city
  (Nasarawa, Kumbotso, Gwale, Tarauni), and large parts of Owerri Municipal
  (Imo), Ringim (Jigawa), Akoko Edo and Esan West (Edo).
- **Rejected:** INEC's point is more than 1 km outside the unit's LGA and
  not just inside a neighbouring LGA of the same state. These units stay
  unmapped until a coordinator pins them. In Kaduna and Osun, 39 of the 42
  are more than 5 km out and 3 fall in a neighbouring state. Where later
  states have many:
  - **Kano** (496 of its 500): INEC has mixed up five LGAs. Tsanyawa's units
    have points in Tudun Wada, about 100 km to the south, and Tudun Wada's
    in Tsanyawa; Madobi's lie in Makoda, Makoda's in Minjibir and
    Minjibir's in Madobi. The unit names match the register, and the app's
    boundaries sit where these LGAs are, so the points belong to other units.
  - **Delta:** 138 points of Sapele and Ethiope West lie 14 to 19 km beyond
    their LGAs (median), in Ethiope East and Okpe; Warri North's 49 lie 35 to
    90 km away.
  - **Borno** (385): points lie from about 5 km to over 200 km beyond their
    own LGA, scattered over the state. INEC has relocated many Borno units
    because of the insurgency, so a few may be real relocated sites.
  - **Benue:** 26 Ushongo points lie just inside Kwande, about 7.5 km out,
    past the 5 km allowed for border towns; Vandeikya's lie 66 to 100 km away.
  - **Niger** (125): Lavun's 39 and Gbako's 17 lie mostly in Bida LGA, a
    median 14 and 10 km beyond their own; Mokwa's 22 lie about 50 km away,
    in Katcha and Lavun.
  - **Enugu:** 20 of Igbo-Eze South's points lie in Enugu city (Enugu North),
    about 45 km away.
- **Not in the locator:** a few register units are missing from the
  locator's own lists (60 in Abia, 31 in Delta, 16 in Adamawa, 13 in Edo, 7
  in Kano, 2 each in Katsina, Oyo and Plateau, and 1 in Ogun), so there was
  nothing to fetch: in Oredo (Edo), the locator lists 32 units in Ibiwe/
  Iwegie/Ugbague ward and the register 45. They count under Units only.
- Only one spot is claimed by units of three or more wards: four Dawaki Kudu
  (Kano) units from four wards, rejected as `shared_spot`. A few pairs share
  a spot, as two units at one school would.
- States without published ward boundaries (Akwa Ibom, Anambra, Benue, Edo,
  Imo, Lagos, Plateau, Rivers) show 0 under Outside own ward: there is nothing
  to compare with.
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
collector is still working. Once the collector has finished, check its file
without it: the locator lists fewer units than the register in a few wards
(see Not in the locator), and with the option such an LGA is left out whole.

```bash
python scripts/inec_locator.py --out inec_locator.csv --state 18
uv run --with shapely python scripts/check_inec_locations.py \
    --locations inec_locator.csv --geo ../TheGameChangerGroup/assets/geo --complete-only
uv run python manage.py load_inec_locations
```

Several states can be collected at once: one collector per state, each with
its own `--out` file (for example `--out inec_locator.csv.KN --state 19`), then
each file checked in turn. Ten ran side by side on 2026-10-15 and ten more on
2026-10-16 with only a few retried server errors; when this side's connection
dropped for three hours, they waited and carried on.
