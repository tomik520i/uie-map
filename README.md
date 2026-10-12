# UIE Map — 3D mapa pro Minecraft 1.7.10

**Raná experimentální verze (early development). Projekt je aktivně rozpracovaný; není to hotový univerzální plugin ani instalace na jedno kliknutí.**

🌍 **[Živá ukázka mapy UIE](https://mcinfo.opicebot.cz/map/uie/)**

UIE Map vytváří interaktivní webovou 3D mapu uloženého světa modovaného Minecraftu 1.7.10. Inspirací je způsob prohlížení světa v BlueMap, jde však o samostatnou implementaci zaměřenou na starší Minecraft a konkrétní modpack. Projekt není součástí BlueMap.

## Co už funguje

- Čtení Anvil regionů, Forge registru bloků a metadat, včetně podporovaného rozšířeného kódování ID.
- Offline renderer v Pythonu: povrch, textury z připraveného katalogu, částečné tvary, průhlednost a listí.
- Webový prohlížeč s Three.js: zoom, myš, WASD/šipky, létání a chůze.
- Načítání detailu v okolí pohledu a zjednodušený vzdálený terén s omezením paměti.
- Neměnné publikované generace, průběžné aktualizace a stavové panely.
- Diagnostika nepodporovaných bloků včetně názvů, ID, metadat a souřadnic.
- Jeskynní pohled a postupný render podzemí celého uloženého světa.

**Pro běžný render ani prohlížení nemusí běžet Minecraft klient.** Renderer čte data světa a používá předem připravený katalog modelů a textur. Příprava katalogu pro jiný modpack je samostatná práce; automatický import libovolných modů zatím není hotový.

## Co je rozpracované a jaká jsou omezení

- Podpora modovaných bloků je neúplná. Některé mají zjednodušený tvar či metadata; chybějící model může být fialový zástupný blok.
- Přesné blokové kolize se připravují pro celý svět. Mimo dostupná data může chůze používat přibližný terén.
- Celosvětové jeskynní a kolizní exporty jsou dlouhé dávkové úlohy; stav ukázky se postupně mění.
- Speciální a animované renderery strojů, entity a všechny možné modové kombinace nejsou věrně simulovány.
- Oprava barev modovaných biomů je ve zdrojích, ale vyžaduje novou generaci dat. Zdrojový kód v tomto repozitáři proto nemusí přesně odpovídat nasazené ukázce.
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

## Lokální vývoj

Potřebujete Python 3.10+ a Node.js 18+. Pro produkční exporty se používá Linux; některé testy ověřují POSIX zámky a chování souborového systému. Pythonové jádro používá standardní knihovnu. Three.js je přiložen v `web/vendor/` včetně jeho licence.

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
