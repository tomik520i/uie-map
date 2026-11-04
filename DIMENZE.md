# Dimenze — rozpoznání, render a webový přepínač

**Aktuální stav:** Veřejná ukázka nabízí hlavní dimenzi a DIM42, DIM80, DIM81, DIM82, DIM83, DIM84, DIM85 včetně povrchu, blokových kolizí a jeskyní. Nově přidané dimenze obnovují dvě společné sekvenční 30min fronty s omezeným úklidem; DIM42 má své dosavadní časovače. Všech šest vybraných neprázdných modovaných dimenzí je zveřejněných. DIM−112 neobsahuje bloky. Nether je od 3. 11. **HOTOVÝ / VEŘEJNĚ NASAZENÝ**: všech 39 uložených regionových souborů, 990 povrchových dlaždic a odpovídající kolizní i jeskynní data prošly auditem. Výchozí řez ve výšce 96 otevře pohled pod strop. Dvě samostatné 30min aktualizační fronty jsou zapnuté; dokončené automatické cykly povrchu, kolizí, jeskyní i jejich úklidu potvrzeny 4. 11.; další aktualizace běží pravidelně.

```sh
python world_layout.py /cesta/k/vlastnimu/svetu
```

Výsledek obsahuje `dimensions` a `warnings`. U každé dimenze uvádí klíč, relativní cestu, adresář regionů, počet souborů regionů a zda adresář existuje. Počet regionů není počet chunků ani odhad velikosti hotové mapy. Prázdná dimenze je rozpoznaná, ale nemusí mít žádná uložená data.

Automaticky rozpoznává hlavní svět (`0`) a kanonické názvy `DIM-1`, `DIM1`, `DIM42` apod. Archivované názvy jako `DIM1-smazano`, nekanonické `DIM01` a jiné neznámé varianty **nezařazuje** a uvede je mezi varováními. Neskenuje libovolné podadresáře jako další světy.

Nestandardní adresář vyžaduje explicitní mapování:

```sh
python world_layout.py /cesta/k/svetu --dimension x:tropics=TROPICS
```

Cesta označuje kořen dimenze, pod kterým je `region`, nikoli samotný adresář `region`. `x:tropics` je uživatelem zvolený stabilní klíč, nikoli automaticky zjištěné číselné ID modu. Mapování neznamená výběr k renderování.

Nástroj odmítne duplicitní klíče/cesty, absolutní cesty, `..`, symlinky či junctions uvnitř procházených cest a odkazy místo souborů regionů. Zápis cesty v mapování používá `/` i na Windows. Procházení má konečný limit počtu položek. Kontrola je okamžitý přehled adresářů; nenahrazuje stabilní kopii vstupů rendereru, ověření verze světa ani kontrolu jeho registru bloků.

Ověření rozpoznání:10testů prošlo na Linuxu včetně symlinků; na Windows8prošlo a2kontroly POSIXsymlinků byly přeskočené. Na UIE proběhlo skutečné čtení struktury světa bez zápisu do světa. Archiv Endu byl správně vynechaný.

## Experimentální render přes CLI (Linux)

Renderer přijímá `--world-id`, `--dimension-key` a pro nestandardní adresáře také `--dimension-path`. `--world` stále označuje kořen celého světa se společným `level.dat`. Každá dimenze musí mít nový, vlastní výstup mimo svět a katalog. Například pro explicitně vybranou modovanou dimenzi:

```sh
python world_hires.py --world /data/world --catalog-dir /data/catalog \
  --out /data/maps/example-dim42 --world-id example --dimension-key 42 \
  --workers 1 --max-jobs 1 --prepare-immutable
```

Příklad zpracuje nejvýše jednu oblast; není to spuštění automatického renderu celé dimenze. Publikování čeká na dokončení celého plánu dimenze. Pro dokončení všech jejích oblastí vynechte `--max-jobs 1`; rozpočet a prostředky ověřte předem. Publikování musí použít stejné ID světa, dimenzi a režim. Úložiště si při prvním běhu uloží trvalou vazbu na vstupní cesty; pozdější záměnu zdroje nebo publikování pod jinou identitou odmítne. Starý neprázdný výstup nelze tímto způsobem automaticky převzít. Přesun vstupů zatím vyžaduje samostatný migrační postup, který není implementovaný.

Ověřeno413regresními testy na Ubuntu a skutečným renderem/publikováním umělého světa: hlavní dimenze zachovala totožné bajty geometrie oproti původní cestě, jiná dimenze na stejných souřadnicích vytvořila odlišná data a chybná identita publisheru byla odmítnuta. Zdrojový svět zůstal nezměněný. To **nepotvrzuje** správný vzhled skutečného Netheru nebo modovaných dimenzí. Cave dávkování a kolizní export byly následně ověřeny na DIM42; nejde o ověření všech dimenzí. Webový výběr popisuje další část tohoto dokumentu.


## Skutečná dimenze UIE: soukromé ověření19.10.

Renderer zpracoval všech10uložených oblastí `DIM42`,767chunků a161dlaždic bez chyb. Render a následné soukromé publikování trvaly dohromady přibližně62s při jednom pracovníku. Všech161geometrických souborů bylo zkontrolováno podle SHA-256; index má vlastní identitu dimenze42 a pokus o publikování poddimenzí0 byl odmítnut. Geometrická data mají přibližně1,72MB; tento malý svět není podklad pro odhad libovolné dimenze.

**HOTOVO / NASAZENO 19.10.:** kompletní povrch DIM42, samostatné zobrazení a přepínač na webu. Skutečný prohlížeč ověřil terén, létání, přepnutí hlavní svět → DIM42 → hlavní svět a odmítnutí chybné identity. **HOTOVO / NASAZENO27.10.:** jeskynní režim DIM42,161párů a30min aktualizace. **HOTOVO / NASAZENO27.10.:**161párů blokových kolizí a30min obnova povrchu→kolizí. Chůze načítá bloková data; modované tvary mohou být přibližné. **Zbývající dimenze:** viz aktuální přehled níže. Nejde o potvrzení věrných modelů všech modovaných bloků ani o podporu Netheru. Minecraft klient nebyl spuštěný.

## Nastavení samostatného webu a přepínače

Jde zatím o ruční konfiguraci prototypu. Pro každou dimenzi připravte vlastní dokončený a neměnně publikovaný surface store a soubor `view.json`:

```json
{
  "format": "UIE_MAP_VIEW1",
  "world": "example",
  "dimension": "42",
  "title": "Moje dimenze 42",
  "bounds": { "min": [-64, 0, -64], "max": [64, 256, 64] },
  "focus": [32, 80, 32]
}
```

Hranice a ohnisko upravte podle skutečného exportu. `world` a `dimension` musí přesně odpovídat publikovanému indexu; podporované jsou také explicitní klíče typu `x:tropics`. Tento formát nepředstírá detailní export spawnu a neobsahuje smyšlené statistiky bloků. V této verzi připojuje pouze povrch.

Samostatná instance používá `UIE_MAP_VIEW_FILE=/data/maps/dim42/view.json` a `UIE_HIRES_STORE=/data/maps/dim42/out`; nesmí mít `UIE_GENERATION_STORE`. Pro přepínač v existujícím hlavním webu nastavte `UIE_DIMENSIONS_FILE` na katalog:

```json
{
  "format": "UIE_DIMENSION_VIEWS1",
  "mainTitle": "Hlavní svět",
  "views": [{
    "key": "dim42",
    "viewFile": "/data/maps/dim42/view.json",
    "hiresStore": "/data/maps/dim42/out"
  }]
}
```

Katalog se načítá při startu webového serveru. Hlavní nastavení zůstává v jeho dosavadních proměnných. Další dimenze je dostupná na relativní adrese `dimensions/dim42/`; odkazy fungují i za proxy pod `/map/uie/`. Přepnutí načítá novou stránku, takže nepřenáší staré kolize ani geometrii. Každý další pohled má oddělené nastavení bez dědění dat hlavního světa. Neznámá dimenze vrátí404, poškozený explicitní bootstrap vrátí chybu místo zobrazení jiného světa. Katalog nepublikuje lokální cesty.

Vstupem serveru zůstává `node server.mjs`; tento soubor je kompatibilní i s importem z PM2 wrapperu. `map-handler.mjs` je interní factory bez spouštění portu při importu.





<!-- CURRENT EXPANSION START -->
## Aktuální nasazení dalších dimenzí (3. 11.)

Veřejná ukázka nabízí hlavní dimenzi a DIM42, DIM80, DIM81, DIM82, DIM83, DIM84, DIM85 včetně povrchu, blokových kolizí a jeskyní. Nově přidané dimenze obnovují dvě společné sekvenční 30min fronty s omezeným úklidem; DIM42 má své dosavadní časovače. Všech šest vybraných neprázdných modovaných dimenzí je zveřejněných. DIM−112 neobsahuje bloky. Nether je od 3. 11. **HOTOVÝ / VEŘEJNĚ NASAZENÝ**: všech 39 uložených regionových souborů, 990 povrchových dlaždic a odpovídající kolizní i jeskynní data prošly auditem. Výchozí řez ve výšce 96 otevře pohled pod strop. Dvě samostatné 30min aktualizační fronty jsou zapnuté; dokončené automatické cykly povrchu, kolizí, jeskyní i jejich úklidu potvrzeny 4. 11.; další aktualizace běží pravidelně.

| Dimenze | Veřejných dlaždic | Povrch / kolize / jeskyně |
| --- | ---: | --- |
| −1 (Nether) | 990 | **HOTOVO / NASAZENO** |
| 42 | 161 | **HOTOVO / NASAZENO** |
| 80 | 138 | **HOTOVO / NASAZENO** |
| 81 | 48 | **HOTOVO / NASAZENO** |
| 82 | 1 | **HOTOVO / NASAZENO** |
| 83 | 161 | **HOTOVO / NASAZENO** |
| 84 | 161 | **HOTOVO / NASAZENO** |
| 85 | 82 | **HOTOVO / NASAZENO** |

Před zveřejněním proběhlo nezávislé ověření geometrie, kolizních dat a jeskynních párů. Prohlížeč ověřil přepnutí, úplné načtení potřebných řezů a návrat na hlavní mapu. Skupinové aktualizace zpracovávají dimenze postupně, takže počet nových dimenzí nezvyšuje počet současně spuštěných rendererů v každé frontě. Povrchová a jeskynní fronta ale mohou běžet souběžně; nejde o globální limit všech služeb na serveru. Staré samostatné časovače DIM80/82 byly vypnuté.

Mapa vykresluje uložené chunky; negeneruje neprozkoumaný terén podle seedu. Proto mohou například DIM81 nebo DIM83 vypadat jako oddělené ostrůvky či sloupce, i když je celý uložený rozsah hotový. DIM82 obsahuje jediný uložený chunk. Jeskynní režim zpřístupňuje existující podzemní geometrii; u mělké ploché dimenze jako DIM83 nemusí řez nad povrchem vzhled vůbec změnit.

<!-- CURRENT EXPANSION END -->


Pro mod s vlastnostmi bloků závislými na dimenzi lze nově vybrat samostatný katalog, viz [konfigurace katalogů](KONFIGURACE.md#vlastní-katalog-pro-jednotlivou-dimenzi). Nejde o automatickou opravu všech modovaných bloků.

DIM84 používá od 29. 10. samostatný profil šesti krystalových bloků OreSpawn, jejichž vlastnost neprůhlednosti závisí na dimenzi. Povrch, kolize a jeskyně byly přepočítány společně a všech 161 dlaždic a párů nezávisle ověřeno. Atlas zůstává původní: krystalové textury samy obsahují průhledné pixely a z dálky mohou působit zrnitě. Přesná shoda se spuštěnou hrou zatím nebyla ověřena.

Veřejný prohlížeč ověřil přepínání a jeskynní řezy i chůzi na jednom místě. Od 1. 11. se chůze v prokazatelně obsazené nebo nenačtené pozici nezapne a ponechá létání s vysvětlením. Pokus zároveň vyžádá dostupná bloková data; po načtení lze přepnutí zopakovat. Pokud postavu uzavřou až později načtená data, vrátí se ovládání na létání. Od 2. 11. se při vstupu z obsazeného místa hledá také blízká volná pozice: nejvýše dva bloky ve 3D, s načtenými daty, pevnou podlahou a místem pro celé tělo. Hledání respektuje půlbloky, odmítá kapaliny a neprobíhá během běžné chůze. Pokud v tomto malém okolí nic nenajde, zůstane létání. Přesnost závisí na dostupných kolizních tvarech modů. Tento test nepotvrzuje správnost všech modovaných kolizních tvarů.

## Nether: pohled pod stropem

[Veřejný Nether](https://mcinfo.opicebot.cz/map/uie/dimensions/d-1bad6b8cf97131fceab8/) se otevře s řezem96. Výšku lze změnit ovladačem nebo URL `?cut=48`; `?cut=off` ukáže povrch bedrockového stropu. Šedé plochy jsou uzávěry řezu pevnou hmotou, nikoli chybějící textura krajiny. Prohlížeč načítá jen okolí kamery a postupně uvolňuje vzdálená data.

Soukromý i veřejný prohlížeč ověřil načtení podzemí a uzávěrů, řez48, vypnutí řezu a návrat do hlavního světa bez přenesení Nether nastavení. Blokové kolize byly ověřené i přes skutečné veřejné HTTP požadavky při vypnutém řezu. Při zapnutém jeskynním řezu používá pohyb přibližné kolize geometrie; přesná shoda všech modovaných tvarů ani chůze celou dimenzí ověřená není. Polohy hráčů pro Nether nejsou připojené.

Původní render i samostatný audit všech990párů dokončeny bez chyb. Audit ověřil publikované soubory a stabilní ukazatele, nikoli shodu s živým světem v libovolném pozdějším okamžiku; k tomu slouží průběžné přepočty. Aktualizační interval se počítá od dokončení běhu; běh jedné služby se nepřekrývá sám se sebou. Povrchová a jeskynní fronta mohou běžet současně. Herní server ani Minecraft klient nejsou při této publikaci restartované/spouštěné.


## DIM84 — 4. 11. 2026: HOTOVO / VEŘEJNĚ NASAZENO

Oprava 6 055 svislých CrystalTreeLog (metadata 0) je veřejná. Všech 758 uložených chunků v 10 regionových souborech bylo přepočítáno: 161 povrchových dlaždic, 161 kolizních párů a 161 jeskynních párů. Atlas zůstal shodný; pokrytí původní mapy zachováno. Audit všech objektů, hashů a vazeb PASS. Soukromý i veřejný browser: povrch, jeskyně Y48, uzávěry 16/16, návrat na povrch PASS; konzole bez chyb. Veřejná diagnostika: fallback 0, chybějící textura 0, 4 Thaumcraft:blockAiry bez geometrie. Krátké létání ověřeno, úplný regresní test chůze tím není nahrazen.

Nové samostatné 30min fronty DIM84: povrch + kolize + úklid a jeskyně + úklid. Obě enabled/active, první automatické běhy complete=true a exit 0. Ostatních pět rozšířených dimenzí ponecháno na původním runtime. Herní procesy nezměněny; klient nebyl spuštěn.

Celý první přepočet: povrch 231,06 s, kolize 319,66 s, jeskyně 291,91 s, celkem 14,04 min. Po prvním update je 90,44 MiB unikátně alokovaných výstupních souborů (87,68 MiB logicky). Bez katalogu, kódu, zdrojového světa, staré publikace a adresářových metadat. Jde o měření této uložené oblasti, nikoli obecný odhad dalších světů.

ČEKÁ: ověření průhlednosti CrystalStone/Grass, vodorovné varianty kmene, přesná ID v jeskyních. Univerzální import a distribuce zůstávají rozpracované.
