# Kontrola vlastního modpacku

**HOTOVO / OVĚŘENO:** inventář modových JARů bez spuštění Minecraftu. **NEDOKONČENO:** automatické vytvoření renderovacího katalogu ze samotných JARů.

Příkaz můžete spustit nad svou složkou `mods` na Windows nebo Linuxu:

```sh
python uiemap.py modpack --mods /cesta/k/minecraft/mods
```

Vypíše JSON s názvy archivů, dostupnými údaji z `mcmod.info`, počtem kandidátů textur, jejich jmennými prostory a konflikty názvů mezi archivy. Pokud mod nemá čitelné `mcmod.info`, zůstane v inventáři pod názvem JARu; program si jeho identitu nevymýšlí.

Pro úplný seznam cest k texturám:

```sh
python uiemap.py modpack --mods /cesta/k/minecraft/mods --include-textures > modpack-inventory.json
```

Samotný nástroj pouze čte. Zápis do souboru v druhém příkladu provádí přesměrování výstupu vašeho shellu. Nepřepisuje JARy, neimportuje konfiguraci, nerozbaluje textury a nespouští Java kód. Prohlíží pouze `.jar` soubory přímo ve zvolené složce; vnořené archivy a podsložky zatím neprochází.

## Jak číst výsledek

| Pole | Význam |
| --- | --- |
| `state: inventory-complete` | Všechny vybrané archivy byly prohlédnuté. Neznamená hotový renderovací katalog. |
| `state: partial` a `errors` | Některý archiv se nepodařilo zkontrolovat nebo byl překročen limit. Ostatní dostupné výsledky zůstávají ve výstupu. |
| `mods` | Údaje deklarované archivem v `mcmod.info`; nemusí odpovídat úplnému seznamu modulů načtených Forge. |
| `textureCandidates` | PNG položky pod `assets/<namespace>/textures/`. Jejich obrazová data ani CRC se nekontrolují. |
| `blockTextureCandidates` | Podmnožina ve složce `blocks` nebo `block`; ještě nejde o mapování textury na konkrétní blok. |
| `textureConflicts` | Stejný název prostředku poskytuje více archivů. Nástroj neurčuje, který z nich má při načtení přednost. |
| `catalogReady: false` | Inventář ještě nelze použít jako katalog pro render. |

Návratový kód je `0` při dokončeném inventáři, `1` při částečném výsledku a `2` při chybě vstupních parametrů. Nečitelná metadata jednotlivého modu jsou uvedena ve `warnings`; sama nezakazují prohlédnout názvy jeho textur.

## Co ještě chybí k importu

Z názvu PNG nejde obecně odvodit všechny bloky, jejich číselná ID ve světě, metadata, orientace ani přiřazení textur jednotlivým stěnám. Tyto informace a geometrie vlastních Java rendererů potřebují další mapování nebo adaptéry. Existující ověřená cesta je stále [import připraveného exportu do katalogu](KONFIGURACE.md).

Na UIE byl inventář ověřen 2. listopadu 2026 na **276 JAR souborech**, **73 707 kandidátech textur**, z toho **27 139 blokových**, se **4 konflikty názvů**. Jde o jednu konkrétní sadu modů; počty nevyjadřují počet podporovaných bloků. Testy pokrývají i poškozené archivy, limity, chybějící metadata, konflikty, nebezpečné cesty a CLI. Herní prostředky ani tento produkční inventář nejsou součástí repozitáře.
