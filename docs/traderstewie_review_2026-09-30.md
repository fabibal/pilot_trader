# TraderStewie: másolhatóság, eredmények és a Pilot Trader pontossága

Vizsgálat dátuma: 2026-09-30. Az árfolyamteszt utolsó napja 2026-09-29.

TraderStewie nyilvános ötleteiben a helyi mintán látszik használható rövid távú részvényválasztás. A teljes kereskedési stratégiájának nyereségessége és másolhatósága viszont nem bizonyított ebből az adatból. A Pilot Trader jelenlegi találati aránya többféle eltérő eseményt kever, ezért nem alkalmas önmagában a kereskedő megítélésére vagy egy másoló rendszer engedélyezésére.

## Saját adat és módszer

Az elmentett `tweets_traderstewie.json` 200 sort, de csak **183 különböző posztazonosítót** tartalmaz, 2025-11-30 és 2026-06-02 között. A `trades.json` **92 különböző TraderStewie-eseményt** tartalmaz, 2025-11-30 és 2026-09-26 között. A két forrás között 54 különböző poszt egyezik. A források nem fedik le igazoltan a teljes nyilvános feedet.

A 92 eseményt az eredeti szöveg alapján, az árfolyamok letöltése előtt osztályoztam:

| Tartalom | Esemény |
|---|---:|
| Előremutató vételi ötlet / figyelőlistás setup | 47 |
| Előremutató short ötlet | 1 |
| Utólagos Top Pick eredmény | 18 |
| Utólagos oktatási trade-beszámoló | 2 |
| Megfigyelés, kommentár vagy más sikere | 23 |
| Retweet | 1 |

Ez **nem egy LLM pontossági mérés**: a megtartott jelnaplót osztályozza, és a visszatekintő beszámoló lehet helyes információ, csak nem követhető új belépés. Teljes recall-méréshez a feldolgozott, de elutasított posztok és az osztályozási döntések is szükségesek.

A követési teszt szabályai:

- Belépés a közzététel utáni első rendes amerikai piaci nyitás árán. Napközbeni posztnál ez a következő kereskedési nap; nyitás előtti posztnál az aznapi nyitás.
- Kiszállás az ötödik, illetve huszadik kereskedési nap záróján, a belépés napját első napnak számítva.
- Azonos részvényen egy adott tartási időn belül nem indítok újabb mintabeli ügyletet. Különböző részvények tartási időszakai átfedhetnek.
- A benchmark ugyanazon belépési és kilépési nap QQQ/SPY nyitó–záró árfolyamváltozása.
- A normál teszt nem használ stopot, célárat vagy a szerző kiszállási jelzéseit. Az árak Yahoo napi OHLC adatok, osztalékhozam nélkül. Költség és csúszás nincs levonva.

Ez a **nyilvános ötletek egységes követési próbája**, nem a privát ügyletek visszamásolása, és nem megvalósított portfólióteszt. A különböző tartási időket más minták is alkotják, ezért a két oszlop eltérése nem tisztán a tartási idő hatása.

## Mit mutatnak az árfolyamok?

| Mutató | 5 kereskedési nap | 20 kereskedési nap |
|---|---:|---:|
| Nem átfedő, értékelhető vételi ötlet | 40 | 31 |
| Nyereséges | 27 / 40, **67,5%** | 17 / 31, **54,8%** |
| Átlagos bruttó árfolyamhozam ötletként | **+4,07%** | +6,25% |
| Medián hozam | +2,69% | +3,18% |
| QQQ átlag ugyanazon időablakokban | +0,31% | +1,16% |
| QQQ-t felülteljesítő ötletek | 62,5% | 51,6% |
| Profit factor, egyenlő névleges méret mellett | 2,13 | 2,04 |
| Legrosszabb ügylet | **−20,46%** | **−38,28%** |
| Legjobb ügylet | +31,99% | +110,92% |
| Átlagos legnagyobb kedvezőtlen ármozgás belépés után | −7,58% | −13,31% |

A rövid távú eredmény biztató. A három legjobb ötlet kihagyásával az ötnapos átlag még +2,10%. Ugyanakkor a 40 eset kevés, több jel ugyanahhoz a részvényhez és piaci környezethez tartozik, és a minta már az eredeti jelfelismerés szűrésén is átment. A QQQ-val való összevetés nem korrigál a részvények nagyobb volatilitására, bétájára és szektorkockázatára, tehát nem bizonyít önálló alfát.

Az ötnapos nyerési arány egyszerű Wilson-féle 95%-os intervalluma 52–80%, de ez is független megfigyeléseket feltételez. Az átlag bizonytalansága nagy: egyszerű normálközelítéssel a 95%-os sáv kb. −0,19% és +8,33% között van. Ezek tájékoztató számok, nem megbízható szignifikanciatesztek az átfedő mintán.

Konkrét ötnapos példák:

| Ötlet | Első későbbi nyitás | Bruttó eredmény |
|---|---|---:|
| [AXTI](https://x.com/traderstewie/status/2041634386362429885) | 2026-04-08 | +31,99% |
| [APLD](https://x.com/traderstewie/status/2007976973965074435) | 2026-01-05 | +29,37% |
| [CDE](https://x.com/traderstewie/status/2011968694596018455) | 2026-01-16 | +23,45% |
| [QUBT](https://x.com/traderstewie/status/2061928607572996335) | 2026-06-03 | −20,46% |
| [AEHR](https://x.com/traderstewie/status/2054616414900932926) | 2026-05-14 | −19,95% |
| [MXL](https://x.com/traderstewie/status/2061831887677739260) | 2026-06-03 | −19,77% |

Az MXL jó példa a találati arány korlátjára: a dashboard június 30-án elért célárként értékeli, miközben az egységes ötnapos követés közel 20%-ot veszített volna. A két mérés eltérő kérdésre válaszol.

A 40 ügyletből 22 legalább 5%-ot, öt legalább 15%-ot ment a belépés ellen az ötnapos időablakban. Az audit által külön ráhelyezett 5%-os stop a nyerési arányt 40%-ra, az átlagot +2,11%-ra változtatta. Feltételezett 0,20 százalékpontos teljes ügyleti költséggel ez +1,91%. A 15%-os stop mellett az átlag +4,41%, a nyerési arány 67,5%. Ezek saját szabályok, nem TraderStewie stopjai; a stopár alatti nyitásnál a rosszabb nyitóárat használtam. A napi adatok nem modellezik az ajánlati könyvet, az aukciós teljesülést vagy a tényleges csúszást. A két stopváltozat nem optimalizált stratégia vagy bizonyíték a megfelelő stopméretre.

Egy teljes nappal későbbi belépés, az eredeti ötnapos kiszállást megtartva, +4,16%-os átlagot adott. Ebből nem következik, hogy a késés előnyös: más az expozíció időtartama, és nincs perces adat vagy eredeti jelmegérkezési idő.

## Mennyire lehet másolni?

A 92 gépi rekordból csak hat tartalmaz belépőárat, kilenc stopot, ötven célárat, és **egy sem tartalmaz belépőt, stopot és célárat együtt**. Ez a tárolt kinyerés teljessége, nem az összes eredeti kép végleges auditja. A hat belépőárból öt utólagos Top Pick beszámolóban szerepel; a hatodik, a HL $21 ára, a szövegben napi mélypont, nem igazolt kötés. A nyilvános feed ezért inkább ötletforrásként követhető.

A szolgáltatás a privát feedben és emailben ad kereskedési értesítéseket; a listaár **$199,99/hó**. A Top Pick kiválasztása és kezelése ilyen előzetes hozzáféréssel jobban követhető lehet, de ezt privát, időbélyegzett jelarchívum és tényleges teljesülések nélkül nem mértem meg. [AoT díjak és szolgáltatás](https://www.artoftrading.net/plans-pricing).

A publikált Top Pick eredmények: 2022 +115,74%; 2023 +131,86%; 2024 +37,46%; 2025 +10,19%. Ezek a szolgáltató saját közlései. A 2025-ös áttekintés nagy veszteségeket, hosszabb tartási időket és következő évre szóló szabályváltoztatást is leír. [2025-ös áttekintés](https://www.artoftrading.net/post/2025-top-pick-of-the-week-review).

A 2026-os eredmény a 38. hét után **+$7 131, azaz +71,31% a fix $10 000 pozíciómérethez képest**. A közlés szerint gyakori a részleges és korai kiszállás, a limitáras belépés, illetve az eredeti heti ötlet cseréje. Ezért egy mechanikus „hétfőn vétel, pénteken eladás” rendszer eltérő eredményt adhat. A nyilvános heti lista utólag jelenik meg. [2026-os heti napló](https://www.artoftrading.net/post/aot-top-pick-weekly-update-2026).

A stratégialeírás példája $100 000 fő számla mellett fix $10 000 heti pozícióval számol, és a max. heti veszteséghez 15%-os szabályt tárgyal. Ezzel a méretezéssel a +$7 131 a teljes számlára **+7,13% bruttó**, ha a többi tőkét nem fektetik be. Ez egyszerű átszámítás, nem az egész számla tényleges teljesítménye. [A stratégia szabályai](https://www.artoftrading.net/post/how-the-aot-top-pick-strategy-works).

A díj kis pozícióméretnél jelentős: a 2025-ös közölt eredmény fix $10 000 mérettel +$1 019, míg 12 hónap a mostani havi listaáron $2 399,88. Ha valaki kizárólag ezt az egy stratégiát használná, a különbség −$1 380,88 lenne, még kereskedési költség előtt. Ez illusztráció; a korábbi és a csomagár eltérhet, a szolgáltatás más elemeit nem értékeli.

A külön AoT alerts teljesítményoldal maga is hipotetikus számláról beszél, jutalék nélkül, és nem foglalja magában az összes ötletet vagy a Top Picket. A megvizsgált anyagokban nem találtam olyan független, teljes brókerszámla-auditot, amely a másolhatóságot igazolná. [AoT teljesítmény-módszertan](https://www.artoftrading.net/performance).

## A dashboard és a jelfelismerés hibái

A jelenlegi kód már külön kezeli az új `setup` és `review` rekordokat, és megőrzi a lezárt pozícióciklusokat. Ezek hasznos javítások. A fő probléma, hogy a régi rekordok és a friss kinyerés szemantikája továbbra sem elég szigorú.

**1. A win rate torzított, és nem a másoló hozama.** A szeptember 29-ig letöltött árakkal, a meglévő dashboard-szabályt visszajátszva 45 értékelt ciklusból 12 célártalálat, két stop és egy pozitív lezárás lett: **13/15 = 86,7%**. További 25 lejárt, két élő és három hibás árszintű eset nem szerepel az arányban. A 45-ből 39-nél nincs stop. A stop nélküli, lezáratlan veszteséges ötlet így rendszerint lejár, nem veszteségként számít. A felület ezt részben jelzi, de a nagy zöld win rate erősebb benyomást kelt. Ez az audit visszajátszása, nem a futó oldal képernyőjéről kiolvasott szám; a Yahoo korrekciói és a futó ár-cache eltérhetnek. Érintett: `resolver.py:132`, `dashboard.py:602`, `dashboard.py:645`, `dashboard.py:671`.

**2. Régi setupok tényleges pozíciónak látszanak.** A 92 eseményből 83-nál hiányzik az új `entry_status`. A `semantics()` sok ilyen rekordot `legacy` nyitásként kezel. Az 57 tárolt TraderStewie-rekordból 43 nyitott; ezek közül 41 `legacy`. A „keep an eye”, „new setup”, „looks ready” és a puszta célár nem tényleges kötés. Érintett: `signal_semantics.py:5`, `reconcile.py:128`.

**3. Friss téves holdingok is vannak.** A DELL „new all time highs” megfigyelése `confirmed/hold`, a NYMO makrokommentárja szintén `confirmed/hold`. A NYMO-t a dashboard saját szűrője elrejti, de a reconciliation pozícióként tárolja. A Top Pick eredményposztokból továbbra is lehet holding. Ez nem csak régi modellprobléma. Érintett: `monitor.py:128`, `monitor.py:149`, `reconcile.py:234`, `dashboard.py:191`.

**4. Egy irányos kép felülírhatja a helyes „nincs jel” döntést.** A `promote_with_chart()` bullish képből `buy`, bearish képből `sell/full` eseményt készít, de a `side`, `position_action`, `entry_status` mezőket nem hangolja hozzá. Reprodukáltam, hogy a végeredmény `buy / long / hold / confirmed / medium`, pedig a szöveges döntés eredetileg `none`. Egy bullish grafikon nem bizonyít megvett pozíciót, egy bearish grafikon nem bizonyít long eladást. Érintett: `monitor.py:603`, `monitor.py:729`.

**5. A napi gyertya tartalmazhat poszt előtti mozgást.** A belépő becslése gyakran az aznapi záró, miközben a resolver már az egész aznapi high/low értékből célártalálatot keres. Egy zárás után publikált jel így „nyerhet” a délelőtti high miatt. Ezt önálló reprodukció igazolta. Napközbeni explicit kiszállásnál is a napi zárót használja, nem a közölt teljesülést vagy a poszt utáni elérhető árat. Érintett: `dashboard.py:407`, `dashboard.py:427`, `dashboard.py:628`, `resolver.py:104`.

**6. Külön ötletek és későbbi célárak összecsúszhatnak.** A MU februári nyitási dátuma mellett a júniusi és júliusi célárak jelennek meg ugyanabban a ciklusban. Az AAOI januári $50 célját márciusi $115, majd egy áprilisi retweetből $103,25 váltja. A resolver a legutolsó célt visszamenőleg a ciklus kezdetétől használja. A ciklusarchívum az explicit close/reopen eseményeket kezeli, de az új, önálló setupok és időben változó árszintek teljes történetét nem. Érintett: `reconcile.py:201`, `reconcile.py:212`, `resolver.py:80`.

**7. Hiányzó kilépések és eredmények a mentett forrásban.** A nyers snapshotban megtalálható, de a jelnaplóban nincs: NVDA 2026-01-20 stop/−5,7%; MU 2026-03-03 veszteséges lezárás; AAOI 2026-03-18 „took all gains off”. Ezek saját thread-válaszok. A jelenlegi monitor már átengedi az influencer-válaszokat, ezért nem állítom, hogy most is a reply gate okozza a hiányt. A régi archívum még hiányos, és a teljes recall méréséhez elutasítási napló kell. Az AAOI márciusi trade így későbbi heti Top Pick recapokkal is összekeveredhet.

**8. A valódi és a becsült árak eredete elveszik.** A HL napi $21 mélypontját belépőnek tárolta, a DELL utólag közölt átlagos kiszállását célárnak. A képről kinyert szintek bekerülnek a stop/target mezőbe, mezőnkénti forrás és szöveges bizonyíték nélkül. Továbbá a schema egyetlen tickert ad vissza: a CRM/OKTA/CRWD többinstrumentumos posztból csak OKTA került az adatba. Érintett: `monitor.py:179`, `monitor.py:208`, `monitor.py:578`, `monitor.py:699`.

**9. A késleltetés mérhetően túl nagy egyes setupokhoz.** A telepített cron `0 0,4,8,12,16,20 * * *`: négyórás ütemezés. A dashboard 60 másodperces frissítése nem gyorsítja a jel érkezését. A múltbeli posztidőpontból számolt hozamot ezért a tényleges megfigyelési idő alapján számolt hozammal is össze kell vetni.

## Konkrét javítási sorrend

1. **Eseménytípus először, árkinyerés utána.** Külön osztály: ötlet, végrehajtott belépés, növelés, részleges zárás, teljes zárás, igazolt holding, utólagos beszámoló, kommentár. `confirmed` csak konkrét végrehajtási vagy aktuális tartási bizonyítékkal. Egy célár vagy egy new-high megjegyzés ehhez kevés. A nem kereskedési események iránya és pozícióművelete lehessen null.
2. **Képekből alapértelmezetten csak setup.** A bullish/bearish irány önmagában ne nyisson és ne zárjon holdingot. A chart promotion kezelje együtt az irányt és a műveletet. A kép fajtája is szerepeljen: árfolyamgrafikon, teljesítménytábla, egyéb. A stophoz/célhoz explicit felirat vagy egyértelmű jelölés és mezőszintű bizonyíték kelljen; MA, support és axis price önmagában ne legyen stop.
3. **Mezőnként forrás és validálás.** `entry`, `stop`, `targets`, `exit_fills` mellé eredet: szöveg/kép/becslés, bizonyítékrészlet és időpont. A célárrange első és második szintje maradjon külön; ne átlagoljuk automatikusan. Külön technikai invalidáció és tényleges stopmegbízás. Instrumentumazonosítás közös rétegben, több ticker külön rekordként.
4. **A másolható időpont legyen a mérés kezdete.** Tárolni kell a poszt publikálását, első megfigyelését és a kinyerés idejét. Külön eredmény az eredeti ötletnek és a ténylegesen megfigyelhető másolatnak. Napi adatból a következő nyitás konzervatív, reprodukálható alap; napközbeni követéshez finomabb árfolyam és végrehajtási modell szükséges. A feltételes belépést csak a trigger későbbi teljesülése aktiválja.
5. **A régieket külön jelölt adathalmazban újraosztályozni.** Először megőrzött eredetiből jelölt új ledger és összehasonlítás; a bizonytalan múlt ne legyen automatikusan confirmed. A 200 soros snapshot júniusban véget ér: a jelenlegi `--account traderstewie --backfill` lecserélheti a teljes későbbi eseménytörténetet erre, ezért ilyen közvetlen visszatöltést nem javaslok. Thread-kapcsolatokkal pótolni kell a hiányzó exit/update eseményeket.
6. **A dashboard mérje a különböző kérdéseket külön.** A fő mutatók: rögzített időablakú nettó hozam, medián, veszteségek, profit factor, időarányos benchmark, drawdown egy valódi tőke- és párhuzamospozíció-szabály mellett. A „célár a stop előtt” arány csak saját módszertannal feliratozott másodlagos mutató legyen. Mindig látszódjon a teljes jogosult minta, lezárt/lejárt/függő/ár nélküli eset, becsült belépők és stop nélküli hívások aránya. A nyitott holding mellett a „célár már elérve” ne számítson élő, másolható trade-nek.
7. **Változatlan kézi referencia-minta és regressziók.** Poszt + kép + thread alapján jelölt, ticker és időszak szerint elkülönített tanító/ellenőrző minta. Mérjük külön az eseménybesorolás precision/recall értékét, a mezők pontosságát, a megalapozatlan confirmed események arányát és a belépő/exit felismerést. Mentett modell- és promptverzió, valamint minden elutasítás oka kell. A jelenlegi 92 vegyes korú rekordból nem állapítható meg egy új modell összehasonlítható pontossága.

Először a téves confirmed és chart promotion problémát, majd a poszt előtti ármozgás kizárását és a régi adatok újraosztályozását javítanám. Egy drágább modell önmagában nem oldja meg az időkezelést, az összecsúszó ötleteket vagy a torz nevezőt.

## Ellenőrzés és megőrzött eredmények

A vizsgálat nem módosította az éles signal-, pozíció- vagy state-fájlokat, nem futtatott fizetős LLM-kiértékelést és nem változtatta meg a dashboard működését. A workspace-ben már meglévő módosításokat megőriztem.

- Az árfolyamlekérések minden kiválasztott vételi/short ötlethez sikerültek; a NYMO külön dashboard-diagnosztikai lekérése 404-et adott, mert indikátor.
- A dashboard `/`, `/_dash-layout`, `/_dash-dependencies` útvonalai Flask tesztkliensből HTTP 200-at adtak. Ez szerveroldali szerkezeti ellenőrzés, nem vizuális böngészőteszt.
- `tests/test_resolver.py`, `tests/test_cycles_and_queue.py`, `tests/test_reconcile.py`, `tests/test_dashboard.py`: **56 teszt átment**. A fent reprodukált fogalmi hibákra a meglévő tesztek nem adnak megfelelő garanciát.
- A chart promotion és a közzététel előtti high téves értékelése külön, fizetős API nélküli reprodukcióval igazolva.

A gitből kizárt helyi munkakönyvtár: `data/traderstewie_audit_2026-09-30/`. Tartalma: `analyze.py`, `dashboard_checks.py`, `classified_events.json`, `idea_returns.csv`, `summary.json`, `dashboard_summary.json`, `dashboard_resolutions.json`, letöltött Yahoo-válaszok és a heti Top Pick oldal mentett szövege. A `summary.json` az eredeti `trades.json` SHA-256 lenyomatát is tárolja. Új futásnál a script az akkori signal-fájlt olvassa; a vizsgált snapshot a `classified_events.json` fájlban marad meg.

Értékelésem: **jó jelölt ötletforrásnak és előre rögzített szabályokkal végzett következő papírtesztnek; teljes trade-másolásra a nyilvános adatok és a jelenlegi dashboard még nem adnak kellő bizonyítékot.**
