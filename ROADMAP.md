# Směr projektu a stav vývoje

**Cíl: stáhnout program, nastavit vlastní svět Minecraft 1.7.10 a modpack, vybrat dimenze a nechat automaticky vytvářet webovou mapu.** UIE je první ověřovací svět, nikoli konečný rozsah produktu. Stav k 18. 10. 2026; níže uvedené plánované funkce zatím nejsou příslibem dostupné instalace.

## Nejprve dokončit současnou sadu

| Část | Stav |
| --- | --- |
| Povrch hlavního světa, web, ovládání, diagnostika, přírůstkové publikování | **HOTOVO / NASAZENO v ukázce** |
| Opravy determinismu barev a registru bloků | **HOTOVO SOUKROMĚ:** celý opravený povrch je přepočítaný a ověřený; veřejné přepnutí čeká na sladění navazujících dat |
| Jeskyně hlavního uloženého světa | **HOTOVO / NASAZENO:** opravená generace983/983oblastí a56782dlaždic; všechny páry ověřeny, veřejný browser prošel18.10. |
| Přesná chůze a kolize celého hlavního světa | **ROZDĚLANÉ:** soukromý export; celosvětové nasazení a průběžná údržba zbývají |
| Ověření obnovy po skutečném restartu hostitele | **ČEKÁ** |

„Celý svět“ v současném provozu znamená hlavní dimenzi. Neznamená všechny dimenze ani univerzální podporu každého modovaného bloku.

## Následující etapy

| Bod | Stav | Podmínka dokončení |
| --- | --- | --- |
| 12. Jednotná konfigurace vlastního světa | **ROZDĚLANÉ — VALIDACE HOTOVÁ** | Nezapisující náhled viz [KONFIGURACE.md](KONFIGURACE.md); napojení rendereru a webu čeká.  Svět, katalog, výstup, port, pracovníci, paměť a diskové rozpočty v jednom validovaném konfiguračním souboru; žádné povinné cesty UIE. |
| 13. Více dimenzí | **ROZDĚLANÉ — INVENTÁŘ HOTOVÝ** | Read-only rozpoznání adresářů je popsané v [DIMENZE.md](DIMENZE.md); zapojení do rendereru a webu ještě čeká.  Vyhledání kandidátů a explicitní výběr dimenzí; oddělené identity, souřadnice, výstupy, fronty, kolize a jeskyně; přepínač na webu. Nether i modované adresáře, archivované světy výchozím stavem nezařazovat. |
| 14. Vlastní modpack a katalog | **PLÁN — IMPLEMENTACE ČEKÁ** | Zdokumentovaný import prostředků uživatele, registry a metadata, rozšiřitelné adaptéry modelů a přehled podpory. Nepodporovaný blok musí být dohledatelný; nelze slibovat věrný automatický převod všech vlastních Java rendererů. |
| 15. Jednotný program a automatický provoz | **PLÁN — IMPLEMENTACE ČEKÁ** | Jednotné příkazy konfigurace, kontroly prostředí, renderu, aktualizací, stavu a webu; obnova fronty, rozpočty a retence. Běžný provoz bez Minecraft klienta. Nejprve samostatná aplikace; případný serverový mod jako volitelný adaptér až po ověření jádra. |
| 16. Distribuce a úplný návod | **PLÁN — IMPLEMENTACE ČEKÁ** | Instalace na čistém podporovaném systému, ukázková konfigurace bez tajných údajů, postup pro vlastní svět/modpack/dimenze, aktualizace, záloha a řešení chyb. Distribuční licence a práva k herním prostředkům musí být vyřešeny před vydáním balíčku. |
| 17. Přenositelnost a kapacitní plánování | **ROZDĚLANÁ DOKUMENTACE / IMPLEMENTACE ČEKÁ** | Test na druhém nezávislém světě a modpacku, více dimenzí se shodnými souřadnicemi bez kolizí, malý benchmark a odhad disku/času před plným renderem. |

Dokončení těchto etap se bude ověřovat skutečným použitím, nikoli jen existencí konfiguračního souboru. Do té doby je repozitář vývojový prototyp. Aktuální orientační výpočty jsou v [KAPACITA.md](KAPACITA.md).
