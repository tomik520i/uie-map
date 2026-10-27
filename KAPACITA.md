# Velikost mapy a doba výpočtu

Neexistuje pevný počet GB na Minecraft svět. Rozhoduje počet uložených chunků, členitost a geometrie bloků, povrch versus podzemí, kolize, komprese, počet dimenzí a uchovávané generace. Velikost vstupního ZIPu není spolehlivý odhad výstupu.

## Měření vývojové instalace

Měřeno 12. 10. 2026 přibližně 22:19 pomocí `du -s -B1` na samostatných výstupních adresářích. Jde o **fyzické obsazení adresářů za běžícího provozu**, včetně pracovních souborů a historie, nikoli čistou velikost aktuální publikované generace. Počty pokrytí byly čteny bezprostředně před měřením; nejde o atomický snímek.

| Sada | Pokrytí při kontrole | Obsazení |
| --- | --- | --- |
| Povrch hlavní dimenze | 56 782 dlaždic | 7 056 728 064 B ≈ 6,57 GiB |
| Jeskynní výstup | 507 / 983 regionů, 32 448 dlaždic | 10 250 715 136 B ≈ 9,55 GiB |
| Rozpracovaný kolizní výstup | 1 984 publikovaných dlaždic | 1 392 271 360 B ≈ 1,30 GiB |

Počet regionů zahrnuje nestejně husté oblasti. Kolizní adresář zahrnuje i rozpracované či odmítnuté výpočty: dělit jeho velikost pouze publikovanými dlaždicemi by dalo zavádějící sazbu. Tyto tři hodnoty rovněž nejsou celková spotřeba instalace: chybí zdrojový svět, katalog, zálohy, další kandidáti, logy a ostatní výstupy. Samostatná měření adresářů mohou opakovaně započítat stejná data sdílená hardlinky (například geometrii mezi povrchem a kolizemi), takže jejich součet může skutečné obsazení nadhodnocovat. Pro společný součet použijte jeden průchod `du` přes všechny příslušné adresáře bez volby `--count-links`.

## Orientační výpočet

Pro vrstvu a dimenzi měřte reprezentativní vzorek. Přibližná velikost aktuálních dat je:

`S ≈ součet přes dimenze a vrstvy (počet plánovaných dlaždic × průměrné bajty na dlaždici)`

Odděleně přidejte indexy, katalog, pracovní kopie, historii a rezervu. Pro bezpečné přepnutí generace mohou dočasně existovat stará i nová data zároveň.

Příklad pouze pro **povrch podobný této instalaci**: `6,57 GiB / 56 782 ≈ 121 KiB` obsazení na dlaždici. Lineární extrapolace stejné skladby a retence:

| Dlaždice povrchu | Orientační obsazení |
| --- | --- |
| 10 000 | 1,16 GiB |
| 50 000 | 5,79 GiB |
| 100 000 | 11,57 GiB |

Jde o odhad podle adresáře jedné instalace, nikoli garantovanou kompresi nebo požadavek pro jiný modpack. Jeskyně mají vlastní geometrii a nelze je odvodit touto povrchovou sazbou. Hrubá extrapolace aktuálního jeskynního adresáře `9,55 × 983 / 507 ≈ 18,5 GiB` předpokládá stejně náročný zbytek regionů a stejnou historii; může se výrazně lišit. Pro kolize zatím reprezentativní odhad nemáme.

## Odhad času a rezervy

`zbývající čas ≈ zbývající jednotky / skutečně dokončené jednotky za hodinu`

Měřte rychlost při stejném počtu pracovníků a souběžných úlohách; zahrňte opakované výpočty a publikaci. Dvojnásobek pracovníků nemusí znamenat dvojnásobnou rychlost. Nether či jiná dimenze mohou mít zcela jinou složitost než hlavní svět. První minuty po spuštění nejsou spolehlivý benchmark.

Pro volný disk plánujte `nová data + dočasné soubory + ponechané staré generace + provozní rezerva`; existující obsazení počítejte jen jednou. Read-only příkaz `capacity` je dostupný; úplný instalační průvodce a odhad všech vrstev zůstávají rozpracované.


## Automatický inventář a odhad geometrie (27. 10. 2026)

```sh
python uiemap.py capacity mapa.json
python uiemap.py capacity mapa.json --sample-store /cesta/k/publikovanemu/povrchu --generations 2
python uiemap.py capacity mapa.json --measure-output
```

Příkaz čte pouze hlavičky regionů vybraných dimenzí, spočítá uložené chunky a obsazené dlaždice 64×64 bloků. Nezapisuje do světa, nespouští renderer a nerezervuje disk. Kontroluje rozsahy sektorů, jejich překryvy a změnu souboru během čtení. Hlavička ale nepotvrzuje správnost NBT a jednotlivá čtení nejsou atomický snímek celého živého světa.

Volitelný `--measure-output` navíc změří existující výstupy povrchu, kolizí a jeskyní vybraných dimenzí. Uvede logické bajty souborů a přidělené diskové bloky; hardlinky počítá pouze jednou i napříč vrstvami. Součet jednotlivých řádků může sdílené soubory započítat vícekrát, proto používejte `combinedUniqueFileBytes` a `combinedAllocatedFileBytes`. Na platformě bez údaje o přidělených blocích vrací druhá hodnota `null`. Měření zahrnuje přítomnou historii a pracovní soubory uvnitř výstupů, ale ne adresářová metadata, zdrojový svět, externí katalog, zálohy ani budoucí rezervu. Odmítne odkazy a nečitelné položky místo tichého podhodnocení. Scan má limit 500 000 položek a není atomickým snímkem živých dat.

Bez `--sample-store` nevymýšlí sazbu. S publikovaným vzorkem stejného režimu načte ověřený index a změří velikosti odkazované geometrie. Neprovádí úplné hashování payloadů (`payloadHashesVerified: false`). Výsledný JSON uvádí scénář jedné generace, zvoleného počtu generací bez sdílení a dvojnásobné hustoty. **Dvojnásobek není garantovaná horní mez.** Pro jeskyně použijte konfiguraci v režimu `caves` a jeskynní vzorek; povrchový vzorek je odmítnut.

Nezahrnuje atlas, metadata, kolize, uzávěry řezů, pracovní soubory, zálohy ani provozní rezervu. Čas a RAM z velikosti geometrie neodhaduje. Nether je v reportu výslovně nepodporovaný; ostatní neprázdné dimenze potřebují skutečné ověření renderu. Úspěšný inventář není potvrzení podpory modpacku.

Aktuální příklad sedmi dalších dimenzí vývojového světa:

| Dimenze | Regiony | Uložené chunky | Obsazené dlaždice |
| --- | ---: | ---: | ---: |
| 82 | 1 | 1 | 1 |
| -112 | 9 | 695 | 157 |
| 80 | 15 | 1689 | 138 |
| 81 | 12 | 523 | 48 |
| 83 | 10 | 749 | 161 |
| 84 | 10 | 758 | 161 |
| 85 | 12 | 1012 | 82 |

Celkem 748 obsazených dlaždic. Změřená aktuální hlavní dimenze má přibližně 118.0 KiB povrchové a 279.1 KiB jeskynní geometrie na dlaždici. Přenos této hustoty na další dimenze dává **86.2 MiB povrchu + 203.9 MiB jeskyní**, dohromady 290.1 MiB pro jednu generaci. Dvě celé nesdílené generace při dvojnásobné hustotě by měly 1160.2 MiB samotné geometrie. Jde o scénář, který se teprve porovnává s jejich skutečným renderem; není to naměřený konečný výstup těchto dimenzí.


### Proč samotná geometrie nestačí: skutečná DIM82

Po prvním automatickém updatu má jediná dlaždice DIM82 29 269 B povrchové a 29 442 B jeskynní geometrie. Celé tři výstupní adresáře ale společně zabírají **56 209 408 B ≈ 53,6 MiB přidělených souborových bloků**. Toto měření započítalo každý inode jen jednou i napříč vrstvami; adresářová metadata, společný vstupní katalog a zálohy nezahrnuje. Obsahuje přítomné indexy, atlasy, pracovní data a historii. Jde o živé, neatomické měření po dokončeném updatu.

U maličké dimenze převládá pevná režie, zejména kopie atlasů. Násobení 53,6 MiB počtem dlaždic by bylo stejně chybné jako prohlášení, že celá dimenze potřebuje jen 59 kB. Pro kapacitní plán je nutné oddělit pevnou režii každé dimenze/vrstvy, proměnlivou geometrii a provozní rezervu. DIM−112 navíc prokazatelně neobsahuje žádné bloky, přesto má obsazené záznamy chunků; odhad z pouhých hlaviček ji proto nadhodnocuje.

### Porovnání odhadu se skutečným dalším renderem

| Povrch | Dlaždice | Odhad z hustoty hlavního světa | Změřená publikovaná geometrie | Skutečnost / odhad |
| --- | ---: | ---: | ---: | ---: |
| DIM82 | 1 | 120 824 B | 29 269 B | 0,24× |
| DIM80 | 138 | 16 673 642 B | 33 560 071 B | 2,01× |

DIM80 dokončila všech 15 regionů za přibližně 416 sekund při jednom pracovníku. Bylo nezávisle ověřeno všech 138 souborů geometrie a soukromý webový náhled. Kolize a jeskyně v okamžiku tohoto měření ještě běžely/čekaly; čas neoznačuje dokončení všech vrstev. Rozdíl hustoty dokládá, proč ani dvojnásobný scénář není horní mez. Pro další plánování používejte pokud možno vzorek stejné dimenze, ne jen hlavního světa.
