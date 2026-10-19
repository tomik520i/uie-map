# Rozpoznání dimenzí — první část podpory

**HOTOVO:** rozpoznání adresářů a experimentální oddělený render přes CLI, ověřený na umělém světě. **ČEKÁ:** ověření skutečných modovaných dimenzí, oddělené fronty a přepínač dimenzí na webu. Následující příkaz pro rozpoznání nic nerenderuje ani nezapisuje.

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

Ověřeno413regresními testy na Ubuntu a skutečným renderem/publikováním umělého světa: hlavní dimenze zachovala totožné bajty geometrie oproti původní cestě, jiná dimenze na stejných souřadnicích vytvořila odlišná data a chybná identita publisheru byla odmítnuta. Zdrojový svět zůstal nezměněný. To **nepotvrzuje** správný vzhled skutečného Netheru nebo modovaných dimenzí. Cave dávkování, kolizní export, webový výběr dimenzí a propojení s jednotnou konfigurací zatím nejsou přizpůsobené.


## Skutečná dimenze UIE: soukromé ověření19.10.

Renderer zpracoval všech10uložených oblastí `DIM42`,767chunků a161dlaždic bez chyb. Render a následné soukromé publikování trvaly dohromady přibližně62s při jednom pracovníku. Všech161geometrických souborů bylo zkontrolováno podle SHA-256; index má vlastní identitu dimenze42 a pokus o publikování poddimenzí0 byl odmítnut. Geometrická data mají přibližně1,72MB; tento malý svět není podklad pro odhad libovolné dimenze.

**HOTOVO SOUKROMĚ:** čtení skutečné další dimenze, kompletní render jejího uloženého obsahu a oddělené publikování. **ČEKÁ:** vizuální kontrola této dimenze, přepínač na webu, kolize a jeskynní režim dalších dimenzí. Nejde o potvrzení věrných modelů všech modovaných bloků ani o podporu Netheru. Veřejná mapa při testu zůstala beze změny a Minecraft klient nebyl spuštěný.
