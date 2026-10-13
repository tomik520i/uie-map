# Údržba kolizních dat — vývojový nástroj

**Implementováno a ověřeno soukromě; automatický úklid celosvětového kolizního exportu není v ukázce aktivovaný.** Tento dokument není instalační návod pro celý produkt.

`collision_world_gc_cli.py` provede jeden omezený průchod údržby již připraveného kolizního úložiště. Výchozí režim pouze vypíše plán:

```sh
python collision_world_gc_cli.py --out /absolutni/cesta/ke/koliznimu/vystupu
```

Adresář musí mít platný vlastnický marker, profil světového GC, povolení GC a publikační žurnál. Příkaz je sám nezakládá; jejich bezpečná inicializace patří do budoucího provozního nasazení. Výstup není cesta k Minecraft světu ani přímo ke složce `store`.

Inicializace profilu musí proběhnout pod stejným výhradním exportním zámkem; samotná nízkoúrovňová funkce `enable_gc` jej nezískává. Markery se během běhu nesmějí ručně měnit. Jinak by vzniklo okno, ve kterém publikace není zaznamenaná v žurnálu. Automatický inicializační příkaz zatím není součástí tohoto nástroje.

Skutečné odstranění vyžaduje přepínač `--apply`. I pak se zachovává publikační žurnál, ověření všech chráněných odkazů a minimální ochranné lhůty; jeden běh řeší nejvýše jednu fázi: staré indexy, potom nepoužívané páry, potom jejich objekty. Není to příkaz k rekurzivnímu mazání.

Výchozí omezení: nejvýše 50 kroků ověřování, 900 sekund kontrolovaných mezi kroky, 4096 čtení / 512 MiB na krok a nejvýše 100 odstraněných souborů / 256 MiB. Jeden krok může časovou hranici překročit; pro pevný limit doby procesu musí provozní služba stanovit vlastní timeout. Velká úložiště mohou vyčerpat ověřovací rozpočet bez mazání.

Exportér i údržba používají stejný výhradní zámek. Při běžícím exportu údržba skončí jako `busy`, bez čekací smyčky. První celosvětový export může držet zámek dlouho; budoucí plánovač musí údržbě dát prostor mezi průchody.

Exportér ve výchozím stavu nadále odmítá úložiště se zapnutým GC. Nový přepínač `collision_world_runner.py --allow-world-gc` dovoluje pouze **již řádně inicializovaný světový profil**; nezapíná úklid a nic nemaže. Neplatný žurnál či starý profil odmítne. Běžící vydání v ukázce se tímto kódem automaticky neaktualizuje.

| Návratový kód | Výsledek |
| --- | --- |
| 0 | Dokončený dry-run nebo jeden krok údržby |
| 1 | Odmítnutí, chyba nebo přerušené mazání; prohlédněte JSON včetně skutečně odstraněných cest |
| 2 | Neplatné argumenty |
| 3 | Vyčerpaný rozpočet ověřování; nedokončeno |
| 4 | Zámek drží jiný proces; nic se neprovedlo |

Ověření zahrnuje jednotkové testy a oddělené kopie skutečných dat: blokování zámkem, výchozí dry-run bez zápisu ledgeru a všechny tři fáze se simulovaným stárnutím. Živé soubory se při těchto testech nemazaly.
