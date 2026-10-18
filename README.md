# UIE Map — 3D mapa pro Minecraft 1.7.10

**Raná experimentální verze (early development). Projekt je aktivně rozpracovaný; není to hotový univerzální plugin ani instalace na jedno kliknutí.**

🌍 **[Živá ukázka mapy UIE](https://mcinfo.opicebot.cz/map/uie/)**

UIE Map vytváří interaktivní webovou 3D mapu uloženého světa modovaného Minecraftu 1.7.10. Inspirací je způsob prohlížení světa v BlueMap, jde však o samostatný projekt. **Cílem je univerzálně konfigurovatelný renderer pro vlastní světy a modpacky; UIE je první ověřovací instalace.** Projekt není součástí BlueMap.

Směřujeme k samostatnému programu s konfigurací světa, výběrem více dimenzí, automatickými aktualizacemi a úplným návodem. Volitelný Minecraft mod může později doplnit napojení na server. Tyto části ještě nejsou dokončené: [stav a plán vývoje](ROADMAP.md), [naměřené velikosti a orientační výpočty](KAPACITA.md).

První samostatné nástroje pro jiné instalace už umožňují [rozpoznat dimenze](DIMENZE.md) a [zkontrolovat společnou konfiguraci](KONFIGURACE.md). Zatím jde o nezapisující náhled; napojení konfigurace na render a web čeká.

## Co už funguje

- Čtení Anvil regionů, Forge registru bloků a metadat, včetně NotEnoughIDs (`Blocks16`).
- Offline renderer v Pythonu: povrch, textury z připraveného katalogu, částečné tvary, průhlednost a listí.
- Webový prohlížeč s Three.js: zoom, myš, WASD/šipky, létání a chůze.
- Načítání detailu v okolí pohledu a zjednodušený vzdálený terén s omezením paměti.
- Neměnné publikované generace, průběžné aktualizace a stavové panely.
- Diagnostika nepodporovaných bloků včetně názvů, ID, metadat a souřadnic.
- Jeskynní pohled celé uložené hlavní dimenze: opravená generace byla 18. října ověřena a nasazena v ukázce (983 oblastí, 56 782 dlaždic).

**Pro běžný render ani prohlížení nemusí běžet Minecraft klient.** Renderer čte data světa a používá předem připravený katalog modelů a textur. Příprava katalogu pro jiný modpack je samostatná práce; automatický import libovolných modů zatím není hotový.

Současný katalog UIE vznikl jednorázovým exportem z klienta s exportérem. To je dosud ověřená cesta přípravy katalogu, nikoli zamýšlená podmínka trvale běžícího klienta. Další způsoby získání modelů a textur jsou součástí plánu importu modpacků.

## Co je rozpracované a jaká jsou omezení

- Podpora modovaných bloků je neúplná. Některé mají zjednodušený tvar či metadata; chybějící model může být fialový zástupný blok.
- Kolize odvozené ze skutečných blokových dat se připravují pro celý svět. Tvary některých modovaných bloků zůstávají přibližné; mimo dostupná data může chůze používat přibližný terén.
- Celosvětové exporty jsou dlouhé dávkové úlohy. Jeskynní průchod je dokončený, kolizní export pokračuje. Regiony pocházejí z různých okamžiků uložení hry.
- Současný celosvětový export pokrývá hlavní dimenzi. Automatická správa dalších dimenzí a jejich přepínání na webu jsou plánované.
- Speciální a animované renderery strojů, entity a všechny možné modové kombinace nejsou věrně simulovány.
- Oprava barev modovaných biomů je ve zdrojích, ale vyžaduje novou generaci dat. Zdrojový kód v tomto repozitáři proto nemusí přesně odpovídat nasazené ukázce.
- Opravena byla také chyba náhradních názvů chybějících ID: souřadnice ve zprávě o chybě mohly přepsat jiný, známý blok v registru. Opravené jeskyně jsou nasazené; nový povrch je dokončený soukromě a čeká na sladěné přepnutí s navazujícími daty. Dosavadní veřejný povrch ještě může být ovlivněný.
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
