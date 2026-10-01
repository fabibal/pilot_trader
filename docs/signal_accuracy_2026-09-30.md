# Jelfelismerés és dashboard javítása — 2026-09-30

A TraderStewie-auditban talált problémák alapján javítottam a feldolgozást, újraosztályoztam a helyi előzményeket, és élesítettem a dashboardot. A rendszer továbbra is nyilvános közléseket és hipotetikus árfolyamkövetést értékel; a `confirmed` besorolás explicit kötés- vagy holdingközlést jelent, nem független brókerigazolást.

## Jelfelismerés

- Külön eseménytípus: setup, entry, add, trim, exit, holding, recap, commentary, review. A figyelőlista, célár vagy bullish grafikon önmagában nem hoz létre végrehajtott pozíciót.
- A végrehajtás bizonyítéka az aktuális poszt szó szerinti részlete. A bizonyítékot az eredeti mondat szereplőjével és feltételes/negatív megfogalmazásával együtt ellenőrizzük. Egy levágott idézet nem tüntetheti el a „would have” vagy „someone else” részt.
- A heti/YTD Top Pick eredmények és az utólagos oktatási beszámolók nem nyitnak vagy zárnak élő holdingot.
- A „short interest”, „short squeeze” és „short-term oversold” nem jelenti az adott értékpapír shortolását. Az explicit short setup, short holding és short cover külön irányt és műveletet kap.
- Egy poszt több instrumentumából külön rekord készül. A deduplikáció instrumentumonként is működik, így egy tweet második tickere nem vész el.
- A belépő, stop, célár és kiszálló mellé forrás és bizonyíték kerül. A korábbi mélypont, MA, support, részvénymennyiség, teljes dollárbefektetés és százalékos stop nem válik automatikusan per-share árrá.
- A célárrange első szintjét használjuk; az átlagolás vagy a második szint elsődleges célként történő kiválasztása nem írja felül az egyértelmű forrást. A brókercél és a szerző saját célja külön szöveges bizonyíték alapján kezelhető.
- A chartok típusa elkülönül: árfolyamgrafikon, teljesítménytábla, egyéb. A kép csak explicit stop/TP feliratból adhat árszintet, és önmagában legfeljebb setupot hozhat létre.
- A NYMO/NAMO és más nem kereskedhető indikátorok közös szűrőt kaptak a tárolásban és a dashboardban.

## Saját válaszok és időbélyegek

TraderStewie is a `tweets_and_replies` feedet használja. A más szerzőtől származó válaszokat továbbra is kizárjuk. A saját korábbi threadből az instrumentum és az irány értelmezhető, az ott közölt belépő/célár és végrehajtás azonban nem öröklődik új eseményként.

Az élő vizsgálatban a korábbi thread alapján a modell „Si”, „Haba”, „Haha, nice” és „Nice find!” válaszokat is setupnak jelölt. Ezeket az aktuális szöveg vagy új árfolyamgrafikon hiányában kiszűrjük. A puszta „Below 220” vagy „$250” válasz címkézett árszint nélkül review, mert a saját root posztból nem derül ki biztosan, hogy stop, célár vagy más szint volt a kérdés.

Tároljuk a publikálást, az első rögzített megfigyelést, a kinyerést, a parent/conversation azonosítót, a modell nevét, a prompt és a feldolgozó verziójának lenyomatát. A korábban nem tárolt megfigyelési időt nem találjuk ki. A sikertelen elemzés megőrzi a payloadot újrapróbálásra; a sikeresen elutasított posztok döntései is megmaradnak. A prompt/feldolgozó változása után a korábbi elutasítások a konfigurált átfedési ablakban újraellenőrizhetők.

Az ütemezés piaci napokon **15 perc, 12–21 UTC között**. Éjszaka és hétvégén a ritkább ellenőrzés megmaradt. A crontab többi feladata változatlan. Ez sűrűbb megfigyelés, nem tickenkénti vagy azonnali végrehajtás.

## Pozíciók és árfolyamértékelés

Az önálló setupoknak külön identitása van; nem módosítják másik ügylet kezdő dátumát vagy célárát. Az explicit close/reopen ciklusokat továbbra is megőrizzük. A holdingok árszintváltozásai időbélyeges történetbe kerülnek, így későbbi célár nem érvényesül visszamenőleg. Az ismeretlen mértékű trim nem felezi meg automatikusan a pozíciót. A közölt részleges kiszállók csak ismert mennyiségekkel adnak súlyozott kiszállási árat; máskülönben a realizált eredmény nem árazható.

A dashboard elsődleges mérése **öt kereskedési napos ötletpróba**:

1. Belépés a publikálás utáni első rendes nyitáson. Az ismert első megfigyelési időből külön próba készül.
2. Kiszállás az ötödik lezárult session záróján. A részleges mai gyertya nem kész eredmény.
3. Net eredmény: bruttó árfolyamváltozás mínusz feltételezett **0,20% teljes ügyleti költség**. Ez feltételezés, nem mért jutalék vagy csúszás.
4. Azonos ticker/irány átfedő ötletei és a még folyamatban lévő azonos próbák kiszűrve.
5. A benchmark ugyanazon napok direction-matched QQQ/BTC bruttó változása. Az eltérő volatilitásra és bétára nem korrigál.

A felület átlagot, pozitív ablakok arányát, mediánt, legrosszabb esetet, profit factort és teljes mintafedettséget mutat. A pending, hiányzó ár és bizonytalan instrumentum megmarad a számlálóban. A feltételes setupok próbája feltételezett belépés; nem állítja a trigger vagy a szerző saját trade-jének teljesülését, és nem portfólióhozam.

A célár/stop és a közölt kiszállás szerinti arány külön, másodlagos mérés. Csak explicit végrehajtási/holdingközlésű ciklusok szerepelnek benne, a hiányzó árak és a kizárások láthatók. Nem az összes setupot jelöljük tényleges ügyletnek.

A napi high/low nem használható a publikálás vagy megfigyelés előtti mozgásból. Napközbeni kiszállásnál vagy árszintmódosításnál a nem időben szétválasztható gyertyát a barrier-mérés kihagyja. A stopon túli nyitás rosszabb nyitóárát használja, és a közölt árakat a Yahoo split-adjusted ársorával azonos skálára igazítja. A régi 30%-os szabály nem cseréli le egy valódi közölt kötés árát egy másik napi záróra.

## Történeti javítás és megőrzés

Új, fizetős teljes backfill helyett a megőrzött szövegek konzervatív újraosztályozása és a lokális snapshotból hiányzó saját posztok visszavétele történt. A későbbi eseményeket a rövidebb snapshot nem törli.

Visszakerült a három fontos kilépés: NVDA 2026-01-20 stop/veszteség, MU 2026-03-03 veszteséges lezárás, AAOI 2026-03-18 teljes gains-off. A biztos nyilvános belépő nélküli kilépés review marad; hozzá nem találunk ki korábbi kötést. A régi chartból kapott, szövegben nem igazolt árakat nem használjuk biztos stopként/célárként.

A `scripts/review_signal_history.py` alapértelmezetten csak jelölt eredményt készít. `--apply` előtt az eredeti `trades.json` és `positions.json` másolata, valamint SHA-256 lenyomat és összesítés készül a gitből kizárt `data/signal_review/<timestamp>/` könyvtárban. A mostani `--backfill` is csak a valóban feldolgozott snapshot-ID-ket cseréli, nem az egész account történetét.

## Ellenőrzés

- Teljes helyi tesztcsomag: **174 teszt átment**.
- Tíz külön ellenőrzött élő modellkimenet a végső validátorral helyesen besorolva; ez referencia-minta, nem általános 100%-os pontossági állítás.
- Valós AEHR-chart: az új vision kimenet nem adott stopot a korábbi, kitalált 74,71 szint helyére. A képen EMA és korábbi árak voltak, stopfelirat nem.
- Az élő monitor mindkét accountot sikeresen feldolgozta. Az elutasítások, a saját thread-kapcsolatok és a verzióadatok naplózása működött.
- A futó dashboard tartalom-callbackjai a Consensus, a két trade feed és az összes kutatási nézet esetén ellenőrizve. A végső üzemeltetési összesítést az élő ellenőrzés után a helyi ellenőrzési napló is őrzi.

A végső éles TraderStewie-callback 67 lezárt próbát / 69 értékelhető ablakot mutatott, 29 átfedő ötlet kihagyásával. Az átlagos feltételezett nettó árfolyamváltozás +3,9%, a pozitív ablakok aránya 60%, a medián +1,5%, a legrosszabb eset −23,7% volt. A direction-matched benchmark átlagos bruttó változása +0,5%. Ezek az új, szélesebb automatikus mintára és a fenti ötnapos szabályra vonatkoznak, nem az eredeti kézi audit 40 vételi ötletének megismétlésére vagy a privát AoT ügyleteire. Az első rögzített megfigyelés szerinti próbák még nem értek be; a régi időbélyegek ismeretlensége külön látható.

Mind a 15 élő tartalom-callback HTTP 200-at adott, a container healthy állapotú volt. A végső monitorfutás mindkét accountot sikeresen ellenőrizte, 146 szöveges és egy vision hívással, elemzési hiba nélkül. Az ellenőrzések helyi HTTP eredménye: `data/signal_accuracy_v3_eval/live_dashboard_final.json`.

Az élő modellvizsgálatok helyi eredményei: `data/signal_accuracy_v3_eval/`. A megmaradt korlát a nyilvános feed teljessége: a lekérés átfedési ablaka korlátos, a privát AoT értesítéseket és a brókerkötéseket nem látja. Ezeket semmilyen új besorolás nem pótolja.
