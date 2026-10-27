# Směr projektu a stav vývoje

**Cíl: stáhnout program, nastavit vlastní svět Minecraft 1.7.10 a modpack, vybrat dimenze a nechat automaticky vytvářet webovou mapu.** UIE je první ověřovací svět, nikoli konečný rozsah produktu. Stav k 27. 10. 2026; níže uvedené plánované funkce zatím nejsou příslibem dostupné instalace.

## Nejprve dokončit současnou sadu

| Část | Stav |
| --- | --- |
| Povrch hlavního světa, web, ovládání, diagnostika, přírůstkové publikování | **HOTOVO / NASAZENO v ukázce** |
| Opravy determinismu barev a registru bloků | **HOTOVO / NASAZENO:** celý opravený povrch je přepočítaný a zveřejněný18.10. spolu s odpovídajícími kolizemi |
| Jeskyně hlavního uloženého světa | **HOTOVO / NASAZENO:** opravená generace983/983oblastí a56782dlaždic; všechny páry ověřeny, veřejný browser prošel18.10. |
| Bloková data kolizí celého hlavního světa | **HOTOVO / NASAZENO:** všech56782párů nezávisle ověřeno; chůze na veřejném webu prošla. Tvary některých modů zůstávají přibližné. Opakované aktualizace spuštěny; rychlá změnová fronta a automatický úklid kolizí zbývají |
| Obnova mapy po skutečném restartu hostitele | **OVĚŘENO 26.10.:** web v PM2, publisher, watcher kolizí a časovače naběhly; veřejný prohlížeč, přepínač dimenzí a chůze ověřeny. Nejde o vyzkoušení všech scénářů pádu nebo obnovy hry. |

„Celý svět“ v současném provozu znamená hlavní dimenzi. Neznamená všechny dimenze ani univerzální podporu každého modovaného bloku.

## Následující etapy

| Bod | Stav | Podmínka dokončení |
| --- | --- | --- |
| 12. Jednotná konfigurace vlastního světa | **POVRCHOVÝ RENDER A WEB HOTOVÉ / OVĚŘENÉ; CELEK ROZDĚLANÝ** | [KONFIGURACE.md](KONFIGURACE.md): skutečný render, publikování, generovaný katalog a web DIM42 ověřené. Stav, sekvenční update a systemd timer ověřeny soukromě; cave render a update rovněž ověřeny na DIM42; webové připojení caves ověřeno soukromě 27.10.; konfigurovaný export kolizí, web kolizí a omezený cave companion GC ověřeny soukromě. Automatická obnova povrchu→kolizí NASAZENÁ na DIM42. Kolizní retence DIM42 NASAZENÁ; úklid jeskynní geometrie hlavního světa a DIM42 NASAZENÝ a ověřený skutečným mazáním starých indexů. |
| 13. Více dimenzí | **NASAZENO: hlavní svět, DIM42, DIM80, DIM81, DIM82, DIM83; ZBYTEK ROZDĚLANÝ** | Veřejná ukázka nabízí hlavní dimenzi a DIM42, DIM80, DIM81, DIM82, DIM83 včetně povrchu, blokových kolizí a jeskyní. Nově přidané dimenze obnovují dvě společné sekvenční 30min fronty s omezeným úklidem; DIM42 má své dosavadní časovače. K veřejnému připojení zbývá: DIM84, DIM85. DIM−112 neobsahuje bloky. Nether zatím není podporovaný. [Podrobnosti](DIMENZE.md). |
| 14. Vlastní modpack a katalog | **IMPORT EXISTUJÍCÍHO EXPORTU HOTOVÝ / OVĚŘENÝ; CELEK ROZDĚLANÝ** | PNG, atlas binding a nový katalog ověřeny na UIE; samotné JARy a jiný modpack čekají. Zdokumentovaný import prostředků uživatele, registry a metadata, rozšiřitelné adaptéry modelů a přehled podpory. Nepodporovaný blok musí být dohledatelný; nelze slibovat věrný automatický převod všech vlastních Java rendererů. |
| 15. Jednotný program a automatický provoz | **CLI HOTOVÉ / OVĚŘENÉ; AUTOMATIZACE ROZDĚLANÁ** | `python uiemap.py` sjednocuje check/doctor/catalog/status/services/capacity/render/update/schedule/gc/cave-gc/collisions/serve. Skutečný timer, update a opt-in omezená povrchová retence ověřeny soukromě. Omezený cave companion GC a jednorázový export kolizí ověřeny. Automatické aktualizace kolizí po povrchu NASAZENÉ na DIM42. Kolizní retence DIM42 NASAZENÁ; úplné nasazení služeb pro ostatní dimenze zbývá. Běžný provoz bez Minecraft klienta. |
| 16. Distribuce a úplný návod | **NÁVOD + ČISTÁ KOPIE ZDROJŮ OVĚŘENÉ; CELEK ROZDĚLANÝ** | [INSTALACE.md](INSTALACE.md) a [vzor](examples/config.json) hotové; 63 testů a CLI v čistě rozbaleném archivu ověřené. Nový operační systém zatím neověřen. Zbývá instalace na čistém podporovaném systému, postup pro vlastní svět/modpack/dimenze, aktualizace, záloha a řešení chyb. Distribuční licence a práva k herním prostředkům musí být vyřešeny před vydáním balíčku. |
| 17. Přenositelnost a kapacitní plánování | **INVENTÁŘ A ODHAD GEOMETRIE HOTOVÉ / OVĚŘENÉ; CELEK ROZDĚLANÝ** | Read-only `capacity` spočítá uložené chunky a scénář geometrie z explicitního vzorku. Ověřeno na Windows, Ubuntu a sedmi dalších dimenzích. Zbývá celková disková kapacita včetně kolizí a pracovních dat, časový odhad a test na druhém nezávislém světě a modpacku, více dimenzí se shodnými souřadnicemi bez kolizí, malý benchmark a odhad disku/času před plným renderem. |

Dokončení těchto etap se bude ověřovat skutečným použitím, nikoli jen existencí konfiguračního souboru. Do té doby je repozitář vývojový prototyp. Aktuální orientační výpočty jsou v [KAPACITA.md](KAPACITA.md).
