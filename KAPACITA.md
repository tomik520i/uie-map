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

Pro volný disk plánujte `nová data + dočasné soubory + ponechané staré generace + provozní rezerva`; existující obsazení počítejte jen jednou. Automatické měření vzorku a kapacitní odhad v budoucím jednotném programu jsou **plánované**, nikoli již dostupný instalační průvodce.
