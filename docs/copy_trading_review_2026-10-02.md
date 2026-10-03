# IncomeSharks és traderstewie: másolhatóság és dashboard-javítás — 2026-10-02

Árfolyamadat: Yahoo napi OHLC, 2026-10-02-ig lezárt sessionök. Minta: a
`positions.json` setup/confirmed ötletei (a dashboard copy testje), azonos
ticker átfedő ablakai kiszűrve. Belépés a poszt utáni első rendes nyitáson,
kiszállás az N-edik lezárt session záróján, 0,20% teljes költség levonva.
Benchmark: ugyanazon napok QQQ (részvény) / BTC (kripto) mozgása.

## Eredmény röviden

| | IncomeSharks | traderstewie |
|---|---:|---:|
| Időszak | 2026-05-29 – 09-30 | 2025-12-01 – 2026-09-29 |
| 5 napos ötlet (n) | 33 | 67 |
| Átlag nettó | +2,95% | +3,90% |
| Medián | −0,45% | +1,46% |
| Nyereséges | 48% | 60% |
| Átlag 95% bootstrap CI | −1,3 … +8,0 | +0,5 … +7,4 |
| Átlag a 2 legjobb nélkül | +0,3% | +2,6% |
| Nettó többlet a benchmarkhoz | +2,0 pp (CI −2,2 … +7,2) | +3,4 pp (CI +0,2 … +6,6) |
| Béta-korrigált többlet | +2,3 pp, medián 0,0 (CI −1,9 … +7,4) | +2,2 pp, medián +0,7 (CI −0,6 … +5,2) |

**IncomeSharks: nem másolható nyereségesen a meglévő adatból.** A medián
nulla körüli, a nyerési arány 50% alatti, és az átlagot egyetlen ötlet viszi:
`$M87 too` (MESSIER mikrokapitalizációjú mémcoin, +66,7%), egy idegen posztra
adott válaszból. Nélküle és a második legjobb nélkül az átlag +0,3%. A 10 és
20 napos ablak sem jobb (a két legjobb nélkül −0,6% ill. −3,0%). A feed 74%-a
kommentár; az ötletekhez 0 stop és 1 célár tartozik, sok poszt több éves
nézet („1 to 3 years ... back to $100”), nem kereskedési jel.

**traderstewie: gyenge, nem bizonyított előny.** Az 5 napos nyers eredmény
pozitív és a CI nem éri el a nullát, de:

- A választott részvények átlagos bétája **2,14** a QQQ-hoz. Bétára korrigálva
  az előny +2,2 pp, és a CI már a nullát is tartalmazza. A hozam nagy része
  egy erős piacon vett tőkeáttételes momentum-kitettség.
- **Időben elfogyott:** az első fél (dec–ápr, n=33) átlaga +6,4%, mediánja +2,0%,
  nyerési aránya 67%; a második félé (ápr–szept, n=34) +1,5%, +0,5%, 53%.
  Béta-korrigálva az első fél +4,5 pp, a második **0,0 pp** (CI −3,0 … +3,3).
  8 pozíciós tőkére vetítve az első fél +26,4% (QQQ −0,4%), a második +6,3%
  (QQQ +25,9%): az utolsó félévben a QQQ tartása messze jobb volt.
- Csak az 5 napos ablak szignifikáns: 1 nap +0,6% (t=1,4), 3 nap +1,9% (t=1,6),
  10 nap +4,2% de medián −1,9%, 20 nap +5,2% de medián +0,6% (t=1,2). Öt
  horizont tesztelése mellett egy határeset t=2,2 gyenge bizonyíték.
- Egyenlő méretű pozíciókkal, egyszerre legfeljebb 8 ötlettel (átlagosan 2,2
  nyitott) a tőke +32,7%-ot hozott volna, a QQQ ugyanezen időszakban +20,3%-ot.
  Az átlagos kitettség azonban csak ~27% volt, kétszeres bétával.
- Stop nélkül a legjobb: 8%-os stop rontott (átlag +3,2%, medián +0,5%), a
  12–15%-os semleges. A legrosszabb eset −23,7% (AXTI short).
- A 0,20% költség kis kapitalizációjú nevekre (AXTI, AEHR, LWLG, ONDS) és
  nyitási gapekre optimista.

**Követhetőség:** a monitor piaci órákban 15 percenként fut, így a következő
nyitás elérhető belépő. Mért élő felvételi idő eddig egy van (SOXL, 3 perc);
a többi `first_observed_at` a 2026-09-30-i újrafuttatás bélyege, nem késés.

**Javaslat:** traderstewie-t legfeljebb előre rögzített papírtesztben érdemes
követni (következő nyitás, 5. záró, egyenlő méret, max. 8 pozíció), kb. 40 új
ötletig. Ha 30 ötlet után a futó 5 napos átlag nem pozitív, vagy nem veri a
béta-korrigált QQQ-t, ne legyen éles. IncomeSharks-ot csak kommentárként.

## Dashboard-javítások

- A „Reported holdings and outcomes” blokk helyett: **Copy test** (egy mondatos
  magyarázat, darabszámok, ötletenkénti oszlopdiagram, futó ablakok élő
  árral, lezárt ötletek látható táblában) és **Trades the author reported**
  (a szerző saját vétel/tartás/részleges eladás/zárás posztjai szó szerint,
  árváltozással csak amíg tartja).
- A fejléckártya a copy test számait mutatja: átlag, nyereséges arány, medián,
  többlet a benchmarkhoz. Mellette „átlag a 2 legjobb nélkül”.
- Az All posts tábla: redundáns oszlopok (ACTION/ENTRY STATUS/EVENT, TP1)
  helyett TYPE és TARGETS, plusz a poszt szövege; a kommentár alapból rejtett.
- Hibák: `FETUSDT`/`BTC/USD`/`ETHUSD` tickerek az alapcoinra normalizálva; a
  „trade closed for 30%” poszt recap, nem új setup; a lezárt MU holding többé
  nem mutat +184%-ot a kiszállás utáni mozgásból; az újrafuttatás bélyegei nem
  számítanak felvételi késésnek; a táblák cellánkénti inline stílusa CSS
  osztály lett (traderstewie válasz 536 → 286 KB).
