# Ověření zveřejněného prototypu

Stav k 18. říjnu 2026. Testy nejsou zárukou podpory všech modů ani potvrzením, že je celý plán dokončený.

- **75 základních Python testů prošlo** přímo v připravené kopii repozitáře: čtení světa, renderer, dlaždice a regresní testy barev.
- **JavaScript: 503 testů prošlo, 1 přeskočen**, celkem 504. Přeskočená kontrola vyžaduje neveřejná exportovaná data, která repozitář neobsahuje.
- **62 Linux testů kolizního exportu prošlo** v odděleném prostředí, včetně opakování výpočtu s čistou cache.
- **96 Linux testů retence a publikování prošlo** v odděleném prostředí.
- **6 testů porovnávání geometrie, UV a barev prošlo** na Windows i Linuxu; nástroj pouze diagnostikuje rozdíly a nenahrazuje kontrolu publikace.
- **135 Linux testů společné sady údržby prošlo**, včetně runneru, publikace, retence, příkazu GC, zámků mezi procesy a explicitního přijetí připraveného profilu. Jde o překrývající se sadu s výše uvedenými testy, počty se nesčítají.
- Po přidání inicializace profilu prošla tato sada **143 Linux testy**. Na kopiích dvou skutečných párů prošly také inicializační dry-run, odmítnutí při zámku, explicitní aktivace, opakování a všechny tři fáze úklidu se simulovaným stárnutím. Živá úložiště zůstala beze změny.
- **90 testů čtečky, rendereru a registrů prošlo** 13. října přímo v nové zmrazené serverové generaci. Zahrnují 15 regresních kontrol parseru chybějících ID. Původní parser selhal ve třech ze šesti prvních regresních testů: omylem přepisoval známá ID hodnotou souřadnice Y.
- Na stabilní kopii skutečné dlaždice oprava zachovala ID portálu a reprodukovala správnou texturu. Vyžaduje nový přepočet, stará zveřejněná data tím nejsou opravena.
- **6 dalších integračních regresí registru prošlo na Windows i Linuxu.** Používají skutečné sestavení Volume ze syntetických chunků a porovnávají názvy použitých bloků i celé bajty kolizních dat před a po zpracování neznámých ID. Netestují vykreslení všech modů.
- **64 skutečných kolizních párů opravené generace prošlo úplnou kontrolou** vůči soukromému povrchovému vzorku. Opakovaný běh obnovil hotovou oblast bez nového renderování; pokus spojit nový export se starou generací byl odmítnut před kopírováním světa.
- **82 Linux testů jeskynní přípravy prošlo** a skutečná dlaždice prošla ověřením geometrie i dat pro řez terénem. Jde o překrývající se sadu zahrnující registry; není to dokončený render celého podzemí.
- **Rozšířená jeskynní sada: 207 Linux testů prošlo** v konečné připravené verzi, včetně skutečné cesty publisheru, obnovy po přerušené publikaci, plánovače, retence a nastavitelných diskových limitů. Odhalila a pomohla odstranit nesoulad starého publisheru v původní přípravě. Tento počet zahrnuje dřívější jeskynní sady.
- Příkaz údržby na kopiích skutečných dat ověřil návrat `busy` při zámku drženém jiným procesem a výchozí dry-run bez zápisu evidence mazání.
- Úklid byl ověřen i na soukromých kopiích dvou skutečných datových párů se simulovaným stárnutím. Produkční soubory se při tomto ověření nemazaly.
- Dvě skutečné problémové dlaždice prošly opakovaným výpočtem a úplným ověřením výsledných dat. Následně se studenou cache prošel i celý původně odmítnutý region: 64/64 dlaždic se shodným hashem. To nenahrazuje dokončení exportu celé mapy.

## Důležité rozlišení

**Implementované a otestované neznamená nasazené.** Změna rendereru mění identitu generace a musí projít samostatným přepočtem a publikací. Povrch s opravou barev a registru je dokončený soukromě, opravené jeskyně byly18.10.zveřejněny; celosvětová kolizní retence není aktivovaná. Starý soukromý kolizní export byl zastaven, jeho data zachována. Nový export musí vzniknout do samostatného adresáře proti opravenému povrchu; veřejné přepnutí ještě neproběhlo.

Vybrané testy se spouštějí také přes GitHub Actions při změnách hlavní větve a v pull requestech. Aktuální výsledek je na kartě Actions; tento dokument nenahrazuje výsledek konkrétního běhu.

## Spuštění kontrol

Základní Python testy:

```sh
python -m unittest test_reader test_offline_renderer test_world_hires test_colormap_determinism.ColorDeterminismTests
```

Webové testy (POSIX shell):

```sh
node --test test_*.mjs
```

Kolizní export (Linux):

```sh
python -m unittest test_collision_world_store test_collision_world_region test_collision_world_runner test_collision_pair_stage test_collision_region_probe
```

Retence (Linux):

```sh
python -m unittest test_collision_world_retention test_collision_world_store test_collision_world_gc test_collision_gc test_collision_retention
```

Provozní testy skutečného světa nejsou součástí těchto příkazů. Vyžadují vlastní vstupní data a izolované výstupy; testovací příkazy nemají nahrazovat kontrolu produkčního nasazení.


## Dokončené podzemí — 18. října

- 13 Linux testů nezávislého ověřovače a datových párů prošlo, včetně poškozených dat, neúplného pokrytí, změny ukazatele generace a omezené dekomprese.
- Všech56 782 skutečných jeskynních párů ve983oblastech prošlo úplným ověřením kontrolních součtů, formátu, souřadnic a rekonstrukce indexu. Kontrola trvala přibližně8minut a neměnila data.
- Soukromý i veřejný prohlížeč ověřil načítání3oblastí, řezy30/31, uvolňování paměti, přepínání povrch/podzemí a nulový počet chyb JavaScriptu. Výchozí povrch nestahuje jeskynní data.
- Dokončená mapa stále představuje regionální snímky pořízené během renderu, nikoli současný snímek celé běžící hry. Ostatní dimenze tím nejsou dokončené.

```sh
python -m unittest test_cave_release_verify test_cave_pair test_cave_pair_index
```
