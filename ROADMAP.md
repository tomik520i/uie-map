# Směr projektu a stav vývoje

**Cíl: stáhnout program, nastavit vlastní svět Minecraft 1.7.10 a modpack, vybrat dimenze a nechat automaticky vytvářet webovou mapu.** UIE je první ověřovací svět, nikoli konečný rozsah produktu. Stav k 26. 10. 2026; níže uvedené plánované funkce zatím nejsou příslibem dostupné instalace.

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
| 12. Jednotná konfigurace vlastního světa | **POVRCHOVÝ RENDER A WEB HOTOVÉ / OVĚŘENÉ; CELEK ROZDĚLANÝ** | [KONFIGURACE.md](KONFIGURACE.md): skutečný render, publikování, generovaný katalog a web DIM42 ověřené. Stav, sekvenční update a systemd timer ověřeny soukromě; cave render a update rovněž ověřeny na DIM42; konfigurované kolize a webové připojení caves zbývají. |
| 13. Více dimenzí | **PŘEPÍNAČ A DIM42 POVRCH HOTOVO / NASAZENO; ZBYTEK ROZDĚLANÝ** | [DIMENZE.md](DIMENZE.md): skutečná DIM42 kompletně vykreslená, webový přepínač ověřený veřejně. DIM42 caves export ověřen soukromě; jeho web, další dimenze a jejich kolize ještě čekají. |
| 14. Vlastní modpack a katalog | **IMPORT EXISTUJÍCÍHO EXPORTU HOTOVÝ / OVĚŘENÝ; CELEK ROZDĚLANÝ** | PNG, atlas binding a nový katalog ověřeny na UIE; samotné JARy a jiný modpack čekají. Zdokumentovaný import prostředků uživatele, registry a metadata, rozšiřitelné adaptéry modelů a přehled podpory. Nepodporovaný blok musí být dohledatelný; nelze slibovat věrný automatický převod všech vlastních Java rendererů. |
| 15. Jednotný program a automatický provoz | **CLI HOTOVÉ / OVĚŘENÉ; AUTOMATIZACE ROZDĚLANÁ** | `python uiemap.py` sjednocuje check/doctor/catalog/status/services/render/update/schedule/gc/serve. Skutečný timer, update a opt-in omezená povrchová retence ověřeny soukromě. Konfigurovaná údržba caves/kolizí a úplné nasazení služeb zbývají. Běžný provoz bez Minecraft klienta. |
| 16. Distribuce a úplný návod | **NÁVOD + ČISTÁ KOPIE ZDROJŮ OVĚŘENÉ; CELEK ROZDĚLANÝ** | [INSTALACE.md](INSTALACE.md) a [vzor](examples/config.json) hotové; 63 testů a CLI v čistě rozbaleném archivu ověřené. Nový operační systém zatím neověřen. Zbývá instalace na čistém podporovaném systému, postup pro vlastní svět/modpack/dimenze, aktualizace, záloha a řešení chyb. Distribuční licence a práva k herním prostředkům musí být vyřešeny před vydáním balíčku. |
| 17. Přenositelnost a kapacitní plánování | **ROZDĚLANÁ DOKUMENTACE / IMPLEMENTACE ČEKÁ** | Test na druhém nezávislém světě a modpacku, více dimenzí se shodnými souřadnicemi bez kolizí, malý benchmark a odhad disku/času před plným renderem. |

Dokončení těchto etap se bude ověřovat skutečným použitím, nikoli jen existencí konfiguračního souboru. Do té doby je repozitář vývojový prototyp. Aktuální orientační výpočty jsou v [KAPACITA.md](KAPACITA.md).
