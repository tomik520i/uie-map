# UIE Map — 3D mapa pro Minecraft 1.7.10

**Raná experimentální verze (early development). Projekt je aktivně rozpracovaný; není to hotový univerzální plugin ani instalace na jedno kliknutí.**

🌍 **[Živá ukázka mapy UIE](https://mcinfo.opicebot.cz/map/uie/)**

UIE Map vytváří interaktivní webovou 3D mapu uloženého světa modovaného Minecraftu 1.7.10. Inspirací je způsob prohlížení světa v BlueMap, jde však o samostatný projekt. **Cílem je univerzálně konfigurovatelný renderer pro vlastní světy a modpacky; UIE je první ověřovací instalace.** Projekt není součástí BlueMap.

Směřujeme k samostatnému programu s konfigurací světa, výběrem více dimenzí, automatickými aktualizacemi a úplným návodem. Volitelný Minecraft mod může později doplnit napojení na server. Tyto části ještě nejsou dokončené: [stav a plán vývoje](ROADMAP.md), [naměřené velikosti a orientační výpočty](KAPACITA.md).

Jednotný příkaz `python uiemap.py` už nabízí kontrolu konfigurace, spuštění povrchového renderu a webu z hotových výstupů. Postup pro vlastní připravený katalog najdete v [KONFIGURACE.md](KONFIGURACE.md); stav jednotlivých dimenzí v [DIMENZE.md](DIMENZE.md). Import libovolného modpacku a úplná automatizace provozu stále čekají.

## Co už funguje

- Čtení Anvil regionů, Forge registru bloků a metadat, včetně NotEnoughIDs (`Blocks16`).
- Offline renderer v Pythonu: povrch, textury z připraveného katalogu, částečné tvary, průhlednost a listí.
- Webový prohlížeč s Three.js: zoom, myš, WASD/šipky, létání a chůze.
- Načítání detailu v okolí pohledu a zjednodušený vzdálený terén s omezením paměti.
- Neměnné publikované generace, průběžné aktualizace a stavové panely.
- Diagnostika nepodporovaných bloků včetně názvů, ID, metadat a souřadnic.
- Jeskynní pohled celé uložené hlavní dimenze: opravená generace byla 18. října ověřena a nasazena v ukázce (983 oblastí, 56 782 dlaždic).
- Opravený povrch a odpovídající bloková data kolizí celé hlavní dimenze: 18. října nasazeno po ověření všech 56 782 párů a zkoušce chůze na veřejném webu. Průběžné obnovování kolizí pracuje po celých průchodech světa.

**Pro běžný render ani prohlížení nemusí běžet Minecraft klient.** Renderer čte data světa a používá předem připravený katalog modelů a textur. Příprava katalogu pro jiný modpack je samostatná práce; automatický import libovolných modů zatím není hotový.

Současný katalog UIE vznikl jednorázovým exportem z klienta s exportérem. To je dosud ověřená cesta přípravy katalogu, nikoli zamýšlená podmínka trvale běžícího klienta. Další způsoby získání modelů a textur jsou součástí plánu importu modpacků.

## Co je rozpracované a jaká jsou omezení

- Podpora modovaných bloků je neúplná. Některé mají zjednodušený tvar či metadata; chybějící model může být fialový zástupný blok.
- Bloková data kolizí celé hlavní dimenze jsou nasazená. Tvary některých modovaných bloků zůstávají přibližné; mimo dostupná nebo odpovídající data může chůze používat přibližný terén.
- Celosvětové exporty jsou dlouhé dávkové úlohy. První úplné průchody povrchu, jeskyní a kolizí jsou dokončené. Regiony pocházejí z různých okamžiků uložení hry. Aktualizace kolizí zatím prochází celý svět; není to okamžitá fronta změn.
- Veřejná ukázka nabízí hlavní dimenzi a povrch DIM42 s přepínačem. Další dimenze, jejich blokové kolize a jeskynní režim zatím nejsou dokončené.
- Speciální a animované renderery strojů, entity a všechny možné modové kombinace nejsou věrně simulovány.
- Nová generace obsahuje opravy barev modovaných biomů a registru bloků. Experimentální části zdrojového kódu nemusí odpovídat nasazenému provozu.
- Opravena byla chyba náhradních názvů chybějících ID: souřadnice ve zprávě o chybě mohly přepsat jiný, známý blok v registru. Přepočítaný povrch, jeskyně i odpovídající kolize jsou již nasazené.
- Formáty dat a provozní rozhraní se mohou měnit. Obnova po skutečném restartu celého produkčního stroje zatím není kompletně ověřená.

## Jak je projekt sestavený

| Část | Hlavní soubory |
| --- | --- |
| Čtení světa a příprava dat | `reader.py`, `capture_snapshot.py`, `capture_batch.py` |
| Modely a offline renderer | `offline_catalog.py`, `offline_volume.py`, `offline_mesher.py`, `world_hires.py` |
| Publikování a retence povrchu | `hires_*.py` |
| Jeskyně a uzavírání řezu | `cave_*.py` |
| Kolizní data a jejich export | `collision_*.py` |
| HTTP server | `server.mjs`, `*-http.mjs` |
| Webový prohlížeč | `web/` |
| Automatické testy | `test_*.py`, `test_*.mjs` |

Starší adaptéry a experimentální cesty zůstávají ve zdrojích kvůli vývoji a testům. Jejich přítomnost neznamená, že jsou všechny součástí současného provozu.

Vývojový příkaz pro omezenou údržbu kolizních dat je popsán v [UDRZBA.md](UDRZBA.md). Automatický úklid celosvětových kolizí ještě není v ukázce aktivovaný.

## Lokální vývoj

Automatické testy na GitHubu používají Python 3.12 a Node.js 22. Pro produkční exporty se používá Linux; některé testy ověřují POSIX zámky a chování souborového systému. Pythonové jádro používá standardní knihovnu. Three.js je přiložen v `web/vendor/` včetně jeho licence.

Základní testy rendereru:

```sh
python -m unittest test_reader test_offline_renderer test_world_hires test_colormap_determinism.ColorDeterminismTests
```

JavaScriptové testy lze spustit v POSIX shellu:

```sh
node --test test_*.mjs
```

Webový server:

```sh
node server.mjs
```

Samotné spuštění serveru nevyrenderuje svět. Pro plnou mapu potřebujete vlastní svět, katalog a publikovaná data. Jejich cesty se předávají provozním nástrojům a serveru; jednotlivé Pythonové nástroje mají nápovědu `--help`. Univerzální instalační průvodce a ukázkový katalog jsou zatím nedokončené.

**Repozitář neobsahuje svět UIE, zálohy, hráčská data, exportované textury Minecraftu/modů, přístupové údaje ani produkční konfiguraci.** Textury a další herní prostředky si musí uživatel obstarat ze své instalace s ohledem na příslušné licence. Licence Three.js je v `web/vendor/LICENSE-three.txt`; vlastní kód zatím nemá přidělenou open-source licenci.

## Hlášení chyb

U problému prosím uveďte souřadnice, režim pohledu, prohlížeč a ideálně snímek obrazovky. U bloku pomůže jeho název z registru, ID a metadata. Rozlišujte chybějící model, nesprávnou texturu, problém kolizí a oblast, jejíž data ještě nejsou připravená.

Projekt vzniká postupně ve spolupráci autora s nástroji Codex a Claude Code. Změny se kontrolují testy a ověřováním skutečných exportů; uvedená omezení nejsou příslibem dokončené funkce.
