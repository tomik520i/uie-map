# Dimenze — rozpoznání, render a webový přepínač

**HOTOVO:** rozpoznání adresářů, oddělený render přes CLI a samostatný webový pohled. Ve veřejné ukázce je přepínač hlavního světa a skutečné DIM42. **ČEKÁ:** další dimenze, jejich blokové kolize a jeskyně, propojení s jednotnou konfigurací. Následující příkaz pro rozpoznání nic nerenderuje ani nezapisuje.

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

Ověřeno413regresními testy na Ubuntu a skutečným renderem/publikováním umělého světa: hlavní dimenze zachovala totožné bajty geometrie oproti původní cestě, jiná dimenze na stejných souřadnicích vytvořila odlišná data a chybná identita publisheru byla odmítnuta. Zdrojový svět zůstal nezměněný. To **nepotvrzuje** správný vzhled skutečného Netheru nebo modovaných dimenzí. Cave dávkování a kolizní export dalších dimenzí stále zbývají. Webový výběr popisuje další část tohoto dokumentu.


## Skutečná dimenze UIE: soukromé ověření19.10.

Renderer zpracoval všech10uložených oblastí `DIM42`,767chunků a161dlaždic bez chyb. Render a následné soukromé publikování trvaly dohromady přibližně62s při jednom pracovníku. Všech161geometrických souborů bylo zkontrolováno podle SHA-256; index má vlastní identitu dimenze42 a pokus o publikování poddimenzí0 byl odmítnut. Geometrická data mají přibližně1,72MB; tento malý svět není podklad pro odhad libovolné dimenze.

**HOTOVO / NASAZENO 19.10.:** kompletní povrch DIM42, samostatné zobrazení a přepínač na webu. Skutečný prohlížeč ověřil terén, létání, přepnutí hlavní svět → DIM42 → hlavní svět a odmítnutí chybné identity. **ČEKÁ:** blokové kolize a jeskynní režim dalších dimenzí. Chůze v DIM42 zatím používá přibližnou geometrii. Nejde o potvrzení věrných modelů všech modovaných bloků ani o podporu Netheru. Minecraft klient nebyl spuštěný.

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
