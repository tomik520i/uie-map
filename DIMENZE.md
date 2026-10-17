# Rozpoznání dimenzí — první část podpory

**HOTOVO:** samostatný nástroj pouze pro čtení adresářů. **ČEKÁ:** render jiné dimenze, oddělené fronty a přepínač dimenzí na webu. Tento příkaz nic nerenderuje, nekopíruje svět a nepřepisuje existující mapu.

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

Ověření:10testů prošlo na Linuxu včetně symlinků; na Windows8prošlo a2kontroly POSIXsymlinků byly přeskočené. Na UIE proběhlo skutečné čtení struktury světa bez spuštění renderu a bez zápisu do světa. Archiv Endu byl správně vynechaný. Vlastní renderer pro tyto dimenze zatím není zapojený.
