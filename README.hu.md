# tuple-screen-extractor

[English](README.md) | **Magyar**

Minden megosztott képernyő-képkocka kinyerése egy [Tuple](https://tuple.app)
hívásból JPEG-ként — időbélyeges indexszel, és opcionálisan lejátszható
videóval együtt.

A Tuple **Capture** funkciója (macOS 3.3.0+) helyben rögzíti a megosztott
képernyőket, de nem videofájlként tárolja őket, és az alkalmazás nem kínál
visszajátszást. A képkockák titkosított, szabadalmaztatott formátumban élnek a
Tuple helyi index-adatbázisában, és megtekinthető kép kinyerésének egyetlen
támogatott módja a `tuple` CLI, ami egyszerre egy képkockát ad. Ez az eszköz
ezt az egy-képkockás API-t teljes, pontos, képernyőmegosztónkénti
képkockasorozattá alakítja.

Minden helyben marad: az eszköz csak a gépeden lévő fájlokat olvassa, és csak a
gépeden futó Tuple démonnal beszél. Semmit nem küld sehová.

## Követelmények

- **macOS** — a Capture (és a helyi archívuma) kizárólag macOS-es Tuple
  funkció.
- **Tuple alkalmazás** futva és bejelentkezve, Capture-rel rögzített hívásokkal.
- **`tuple` CLI** telepítve (alkalmazás oldalsáv → Local History → *Install
  Tuple CLI*).
- **Python 3.9+**, kizárólag standard könyvtár (3.14-gyel fejlesztve).
- **ffmpeg** (opcionális) — csak a `--video` kapcsolóhoz. `brew install ffmpeg`.

## Gyors kezdés

```sh
# nézd meg, mi kerülne kinyerésre, mielőtt bármihez hozzányúlnál
./tuple_extract_screens.py <hivas-azonosito> --dry-run

# 15 másodperces ablak kinyerése egy elhangzott mondat körül
./tuple_extract_screens.py <hivas-azonosito> --since 01:04:15 --until 01:04:30 --video

# gyors áttekintés: 5 másodpercenként egy képkocka az egész hívásból
./tuple_extract_screens.py <hivas-azonosito> --every 5

# szó szerint minden tárolt képkocka, minden megosztótól (hatalmas lehet — lásd lejjebb)
./tuple_extract_screens.py <hivas-azonosito>
```

A `<hivas-azonosito>` a `tuple capture list` kimenetéből származó azonosító
(vagy annak bármilyen egyedi előtagja):

```sh
$ tuple capture list
 Date             Title Call                                  Segments Participants
 2026-09-17 13:55       a1b2c3d4-0000-4f07-a545-b3177728788b 2809     Kovács Á., Nagy B.
```

## Kimeneti felépítés

```
<kimenet>/
  frames/
    user-163400-kovacs-a/
      segment-01/
        000001_20260917T055950.911Z.jpg   # <sorszam>_<UTC idopont>.jpg, teljes felbontás
        ...
    user-145137-nagy-b/
      segment-02/
        ...
  manifest.jsonl        # képkockánként egy JSON rekord (a hiteles index)
  frames.csv            # ugyanaz, táblázatkezelőkhöz lapítva
  summary.json          # darabszámok, hibák, szegmensenkénti statisztika, futási idő
  user-163400-kovacs-a-segment-01.mp4   # csak --video esetén
  user-145137-nagy-b-segment-02.mp4
```

Egy „szegmens” az az időszak, amíg egy résztvevő folyamatosan osztja a
képernyőjét. Ha a hívás közben cserélődik a megosztó, minden megosztóhoz külön
szegmens tartozik; a képkockák és videók szegmensenként különülnek el, mert egy
képkocka csak egy konkrét megosztóhoz viszonyítva létezik.

Egy manifest-sor:

```json
{"annotations_overlay": "none", "app": "Zen", "bytes": 327736,
 "call_id": "a1b2c3d4-...", "cli_segment_id": "...", "drift_ms": 0,
 "file": "frames/user-163400-kovacs-a/segment-01/000001_20260917T055950.911Z.jpg",
 "frame_time": "2026-09-17T05:59:50.911Z", "height": 1440,
 "recording_uuid": "...", "requested_ms": 1789624790911,
 "requested_time": "2026-09-17T05:59:50.911Z", "segment": 1,
 "segment_uuid": "...", "url": null, "user": "Kovács Á.", "user_id": 163400,
 "width": 2560, "window_title": "…pull request cím…"}
```

Az `app`, `window_title` és `url` mezők azt mutatják, melyik
alkalmazás/ablakcím/URL volt épp látható a megosztó képernyőjén abban a
pillanatban (a Tuple shared-content eseményeiből rekonstruálva) — hasznos,
amikor „azt a képet” keresed, ahol a deploy dashboard fent volt.

## Hogyan működik

### Amit a Tuple valójában tárol

A Capture egy helyi archívumot ír ide:
`~/Library/Application Support/app.tuple.app/index.db` (SQLite, WAL mód;
a Tuple a saját séma-diagramját is odateszi mellé `index-schema.mmd` néven).
A fontos táblák:

| Tábla | Tartalma |
|---|---|
| `calls` | hívásazonosító, kezdés/vége, cím |
| `recording_sessions` | hívásonként egy sor Capture ki/be kapcsolási szakaszonként |
| `screen_share_segments` | résztvevőnként egy sor képernyőmegosztási szakaszonként: `user_id`, pontos `started_at`/`ended_at` |
| `screen_share_frames` | minden tárolt képkocka: `share_segment_id`, `ts_ms` (epoch ms), `key_frame_ts_ms`, és egy **titkosított, szabadalmaztatott `data` blob** |
| `shared_content` + `events` | melyik app/ablakcím/URL volt épp a megosztó képernyőjén |
| `annotation_events` | képernyőre rajzolt vonások, megosztási szegmensenként |

Tuple 3.3.5 alapján mérve: a képkockák **~8 fps** sebességgel (120–125
miliszekundumonként egy) kerülnek tárolásra, **teljes kijelző-felbontásban**,
időszakos kulcsképkockákkal és a köztük lévő delta-képkockákkal. Egy 3,5 órás,
egymegosztós hívásban 99 966 képkocka volt.

### Miért kell az adatbázis (csak metaadat)

A képkocka-blobok titkosítottak, és a Tuple-on kívül nem dekódolhatók, ezért a
kinyerés azt jelenti, hogy a CLI-vel rendereltetünk egyesével:

```sh
tuple --format json screen --at 2026-09-17T05:59:50.911Z --call a1b2c3d4 --user 163400 -o frame.jpg
# → frame.jpg (JPEG, a rajzolt jelölésekkel összefűzve)
# → nyugta a stdout-ra: {"frame_time": "2026-09-17T05:59:50.911Z", "sharer_user_id": 163400,
#                        "width": 2560, "height": 1440, "annotations_overlay": "none", ...}
```

Az adatbázisra kizárólag arra van szükség, hogy megmondja, **mely időpontokban
van képkocka, és melyik felhasználó osztotta** (a `--user` azért kötelező,
mert a képkockák megosztónként léteznek; aki T időpontban beszél, gyakran nem
az, akinek a képernyőjén az érdekes tartalom van). Az eszköz `mode=ro` módban,
`PRAGMA query_only` mellett nyitja meg az adatbázist, és kizárólag a `ts_ms`,
időzítési és felhasználói oszlopokat kérdezi le — a `data` blobot soha nem
olvassa vagy dekódolja. Maga a kinyerés teljesen a támogatott CLI-n át zajlik.
(A `--source cli` adatbázismentes tartalék-út: a `tuple capture show`
eseményeiből építi újra a megosztási időszakokat, és szondálépcsővel járja
végig őket, a nyugta `frame_time` mezője alapján deduplikálva; ez kihagyhat
olyan képkockákat, amelyek a lépésköznél sűrűbben készültek.)

Mivel az inventory pontos, a kinyerés is az: **tárolt képkockánként pontosan
egy szonda, nulla találgatás, nulla duplikátum**. Az adatbázissal
ellenőrizve egy teljes szegmensen: 1332 tárolt képkocka → 1332 kinyert
képkocka, 1332 különböző `frame_time`, 0 hiányzó, 0 felesleges, 0 szondahiba.

### Videó összefűzés

A `--video` ffmpeg concat demuxerrel fűzi össze a szegmens képkockáit, és
minden képkockának a **következő tárolt képkockáig eltelt valós időtartamot**
adja (40 ms-os padló, 10 s-es plafon) konstans fps helyett — a videó valós
sebességgel játszik még ott is, ahol a Tuple tárolási üteme ingadozik.
`-fps_mode vfr` kódolással (régebbi ffmpeg-en `-vsync vfr` tartalék),
H.264/yuv420p.

### Folytatás megszakítás után

Minden kiírt képkocka bekerül a `manifest.jsonl`-be. Újrafuttatáskor (Ctrl-C
után, vagy szélesebb időablakkal) a már a manifestben szereplő képkockákat az
eszköz kihagyja, így a kinyerés olcsón újrapróbálható és biztonságosan
megszakítható.

## Kapcsolók

| Kapcsoló | Alapértelmezés | Jelentés |
|---|---|---|
| `call` | — | hívásazonosító vagy egyedi előtag (`tuple capture list` alapján) |
| `-o, --out` | `./<hivas8>-screens` | kimeneti könyvtár |
| `--source {auto,db,cli}` | `auto` | `db`: pontos képkocka-inventory az index-adatbázisból (csak metaadat). `cli`: szondálépcsős bejárás capture-eseményekből, adatbázis nélkül. `auto` visszaesik `cli`-re, ha az adatbázis nem nyitható meg. |
| `--db` | `~/Library/Application Support/app.tuple.app/index.db` | az index-adatbázis elérési útja |
| `--every N` | 0 (minden tárolt képkocka) | N másodpercenként legfeljebb egy képkocka |
| `--stride N` | 1 | minden N-edik tárolt képkocka |
| `--since` / `--until` | egész hívás | RFC3339 időbélyeg, vagy `HH:MM[:SS]` **a hívás kezdetétől számítva** |
| `--sharer UID` | minden megosztó | csak ez a felhasználó-azonosító (ismételhető; azonosítókat a dry run ad) |
| `--limit-frames N` | 0 (nincs limit) | szegmensenként legfeljebb N képkocka (gyors mintavétel) |
| `--jobs N` | 4 | párhuzamos szondák; a Tuple démon ennél a pontnál telítődik |
| `--exact` | ki | pontos időpont kérése (ne hozzon befejezetlen rajzjelölést időben előre) |
| `--video` | ki | szegmensenként mp4 összefűzés (ffmpeg kell) |
| `--video-fps` | 8 | tartalék fps, ha a képkocka-távolság ismeretlen |
| `--no-resume` | ki | a manifestben már szereplő képkockák újrakinyerése |
| `--dry-run` | ki | terv kiírása (szegmensenkénti darabszámok, méretek, időbecslés) és kilépés |
| `--quiet` | ki | letiltja a darabonkénti haladásjelzést |
| `--tuple-bin` / `--host` / `--env` | `tuple` | CLI helye / alkalmazás-socket / Tuple környezet |

`--dry-run` példa (anonimizálva):

```
call    a1b2c3d4-0000-4f07-a545-b3177728788b
window  2026-09-17T05:55:35.792Z -> 2026-09-17T09:26:02.182Z
source  db (exact frame inventory)
plan    2 segment(s), 101298 frame(s) to extract; ~31.7 min at 4 probes in parallel
  seg 01  user 163400  Kovács Á.   05:56:15 -> 05:59:53  stored 1332   extract 1332    ~0.7 GB
  seg 02  user 145137  Nagy B.     05:59:54 -> 09:26:01  stored 99966  extract 99966   ~50.0 GB
```

## Teljesítmény (Tuple 3.3.5 alapján, M-es sorozatú Macen mérve)

| Szegmens típusa | Szonda/mp (--jobs 4) | Képkockaméret | Megjegyzés |
|---|---|---|---|
| a saját képernyőd | ~14 (démon-plafon; több job nem segít) | ~0,6 MB | 2560×1440 |
| távoli résztvevő képernyője | ~1,2 | ~0,35 MB | 3840×2160; a delta-lánc dekódolás ~10× lassabb |

Tehát: **tárolt képkockánként ~1 szondával** számolj; egy teljes, 100 ezer
képkockás hívás ~55 GB, és ~2 órától (saját képernyő) ~20 óráig (távoli
megosztó) bármi lehet. A gyakorlatban az `--every 1`–`--every 5` (másodpercenként
vagy öt másodpercenként egy képkocka) percenkénti feladattá alakítja a
kezelhetetlent, és általában ennyi elég; teljes sűrűség csak forenzikus
ablakokhoz kell, `--since/--until` segítségével.

## Adatvédelem és biztonság

- A képkockán **mindent** lát, ami a megosztott képernyőn volt — kód, láthatóan
  hagyott jelszavak, privát üzenetek. Kezeld a kinyerést úgy, mint magát a
  hívást. Ne commitolj kinyeréseket repóba; tedd a kimeneti könyvtárat a
  `.gitignore`-ba.
- A Tuple minden résztvevőt értesít, amikor a Capture bekapcsol; ez az eszköz
  azon nem változtat. Képkockák kinyerése és megosztása előtt kérj hozzájárulást.
- Az adatbázis-hozzáférés szigorúan írásvédett, és soha nem nyúl a
  képkocka-blobokhoz; az eszköz nem tudja módosítani a Tuple archívumát. Ha
  egyáltalán nem akarod az adatbázishoz nyúlni, ott a `--source cli`.

## Hibaelhárítás

- **`nothing to extract: no shared-screen frames match the filters`** — a
  hívást nem rögzítették, senki nem osztott képernyőt a kiválasztott
  tartományban, vagy a `--since/--until` a híváson kívülre esik. Ellenőrizd
  `--dry-run`-nal és `tuple capture list`-tel.
- **Pár `probe_failures: {"no-recording": N}` a `summary.json`-ban** —
  `--source cli` esetén ez az időszakok szélein várható: ezek a szondák a
  tárolt tartományokon kívülre estek. `--source db`-vel üresnek kell lennie.
- **`ffmpeg failed: Unrecognized option 'vsync'`** — az ffmpeg ≥ 8 átnevezte a
  `-vsync` kapcsolót; az eszköz először `-fps_mode vfr`-rel próbál és
  visszaesik, így ha ezt látod, az ffmpeg-ed régebbi, mint amire a tartalék
  számít; frissíts (`brew upgrade ffmpeg`) vagy hagyd el a `--video`-t.
- **~10× lassabb a kinyerés, ha más képernyője volt** — ezt a Tuple démon
  távoli delta-lánc dekódolása okozza, nem ez az eszköz; az `--every` a
  barátod.

## Kilépési kódok

`0` siker (beleértve a „folytatáskor már nincs teendő” esetet is), `1` hiba
vagy nincs találat, `130` Ctrl-C megszakítás (onnan folytatható, ahol
megállt).

## Köszönetnyilvánítás

Ezt a projektet a Kimi K3 és Qwen3.8 Flash Next LLM-ek generálták — beleértve a
Tuple tárolási formátumának felderítését, magát a kinyerő eszközt és ezt a
dokumentációt is — emberi irányítás mellett, és minden viselkedést élő Tuple
3.3.5 telepítésen ellenőriztünk.
