# Údržba kolizních dat — vývojový nástroj

**Implementováno a ověřeno soukromě; automatický úklid celosvětového kolizního exportu není v ukázce aktivovaný.** Tento dokument není instalační návod pro celý produkt.

`collision_world_gc_cli.py` provede jeden omezený průchod údržby již připraveného kolizního úložiště. Výchozí režim pouze vypíše plán:

```sh
python collision_world_gc_cli.py --out /absolutni/cesta/ke/koliznimu/vystupu
```

Adresář musí mít platný vlastnický marker, profil světového GC, povolení GC a publikační žurnál. Samotný příkaz úklidu je nezakládá. Výstup není cesta k Minecraft světu ani přímo ke složce `store`.

Inicializaci nového světového profilu obstarává `collision_world_gc_init.py` pod stejným výhradním exportním zámkem. Výchozí kontrola nic neaktivuje; může založit pouze soubor zámku:

```sh
python collision_world_gc_init.py --out /absolutni/cesta/ke/koliznimu/vystupu
```

Teprve explicitní `--apply` zapíše žurnál chránící aktuální index, profil a nakonec povolení GC. Nic nemaže ani nenastavuje časovač. Zachovává dřívější platný žurnál a umožňuje dokončit přerušenou inicializaci. Již aktivní platný profil při opakování nemění. Starý profil GC či jeho ledger odmítne převádět; při běžícím exportu vrátí `busy`/kód4. Neplatný vstup vrací kód1.

Po aktivaci musí všechny další průchody světového exportéru používat `--allow-world-gc`. Starší exportér kompatibilní není. Inicializaci proto provádějte až při přechodu na kompatibilní verzi a mezi průchody. Vlastnický marker musí již pocházet z přípravy exportu; inicializační příkaz si cizí adresář nepřivlastňuje. Nízkou úroveň `enable_gc` nevolejte bez zámku a markery neupravujte ručně.

Skutečné odstranění vyžaduje přepínač `--apply`. I pak se zachovává publikační žurnál, ověření všech chráněných odkazů a minimální ochranné lhůty; jeden běh řeší nejvýše jednu fázi: staré indexy, potom nepoužívané páry, potom jejich objekty. Není to příkaz k rekurzivnímu mazání.

Výchozí omezení: nejvýše 50 kroků ověřování, 900 sekund kontrolovaných mezi kroky, 4096 čtení / 512 MiB na krok a nejvýše 100 odstraněných souborů / 256 MiB. Jeden krok může časovou hranici překročit; pro pevný limit doby procesu musí provozní služba stanovit vlastní timeout. Velká úložiště mohou vyčerpat ověřovací rozpočet bez mazání.

Exportér i údržba používají stejný výhradní zámek. Při běžícím exportu údržba skončí jako `busy`, bez čekací smyčky. První celosvětový export může držet zámek dlouho. Nový `collision_world_watch.py` uvolňuje exportní zámek mezi průchody; sám údržbu nespouští.

Exportér ve výchozím stavu nadále odmítá úložiště se zapnutým GC. Nový přepínač `collision_world_runner.py --allow-world-gc` dovoluje pouze **již řádně inicializovaný světový profil**; nezapíná úklid a nic nemaže. Neplatný žurnál či starý profil odmítne. Běžící vydání v ukázce se tímto kódem automaticky neaktualizuje.

| Návratový kód | Výsledek |
| --- | --- |
| 0 | Dokončený dry-run nebo jeden krok údržby |
| 1 | Odmítnutí, chyba nebo přerušené mazání; prohlédněte JSON včetně skutečně odstraněných cest |
| 2 | Neplatné argumenty |
| 3 | Vyčerpaný rozpočet ověřování; nedokončeno |
| 4 | Zámek drží jiný proces; nic se neprovedlo |

Ověření zahrnuje jednotkové testy a oddělené kopie skutečných dat: blokování zámkem, výchozí dry-run bez zápisu ledgeru a všechny tři fáze se simulovaným stárnutím. Živé soubory se při těchto testech nemazaly.

## Opakované aktualizace kolizí — nasazeno v ukázce

`collision_world_watch.py` opakuje celý ověřovaný průchod exportéru. Nezměněné oblasti využijí dřívější report, ale znovu se kontrolují jejich vstupy a publikované objekty. Proces uchovává ověřovací cache mezi průchody; při restartu se cache vždy ověří znovu.

```sh
python collision_world_watch.py \
  --world /absolutni/cesta/svet \
  --catalog-dir /absolutni/cesta/katalog \
  --hires-store /absolutni/cesta/povrch \
  --out /absolutni/cesta/kolize \
  --passes 0 --interval 900 --budget-gib 30 --min-free-gib 24
```

Výchozí `--passes 1` provede jediný průchod. Explicitní nula znamená nepřetržitý proces, který musí spravovat provozní služba. `--interval` je prodleva **po dokončení průchodu**, nikoli slib aktualizace každých 15 minut: velký svět může zabrat hodiny. Rychlá fronta změněných oblastí zůstává další práce. Zatím platí kontext prototypu `uie`, hlavní dimenze; univerzální konfigurace teprve vzniká.

Vlastní zámek brání druhému koordinátoru. Obsazený exportní zámek vede k čekání bez přepsání stavu probíhajícího exportu. Nedostatek disku proces zastaví; neprovádí automatické mazání. Chybové oblasti zůstávají v reportu a další průchod je může zkusit znovu. Částečný poslední průchod vrací kód2; druhý koordinátor kód3. Už aktivovaný světový GC vyžaduje `--allow-world-gc`, který ho sám nezapíná.

**Koordinátor je v ukázce nasazený od18.10.** Následný celý průchod932oblastmi byl ověřen bez chyb.19.10. byla nasazena optimalizace nezměněných oblastí; její celý běh ještě měříme.

Pokud již index obsahuje přesně stejné páry pro právě kontrolovanou oblast, `publish_region` ověří jen tyto páry, znovu zkontroluje ukazatel generace a nic nepublikuje. Výsledek výslovně uvádí `verificationScope: updates-only` a `published: false`: není to audit všech ostatních oblastí. Poškozený soubor dané oblasti je nadále odmítnut. Změněné páry nebo odstraňování odkazů procházejí původním úplným ověřením `publish_incremental` včetně publikačního žurnálu.

Optimalizace prošla91linuxovými testy, dvěma skutečnými průchody nad oddělenou kopií64dlaždicového vzorku a kontrolou64párů z úplného živého indexu bez zápisu. První ověření tohoto výběru trvalo přibližně1,09s, opakování s cache0,48s. Jde o měření jednoho výběru, nikoli odhad doby celého světa. Nasazení proběhlo mezi průchody; web ani herní server se nerestartovaly.
