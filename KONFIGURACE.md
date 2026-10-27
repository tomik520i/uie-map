# Společná konfigurace a spuštění povrchového renderu

**HOTOVO / OVĚŘENO:** nezapisující validace, oddělené výstupy, spuštění povrchového renderu a publisheru i sestavení webového katalogu a spuštění webu. **ČEKÁ:** kolize a jeskyně z této konfigurace, trvalá automatizace aktualizací a import katalogu z jiného modpacku. Příkaz `uiemap_config.py` níže nadále nic nespouští a nevytváří adresáře. Jeho výsledek proto výslovně obsahuje `renderReady: false`.

Příklad `uie-map.json` pro vlastní instalaci:

```json
{
  "format": "UIEMAP_CONFIG1",
  "world": {"id": "my-world", "path": "../minecraft/world"},
  "catalog": "./catalog",
  "out": "./output",
  "dimensions": [
    {"key": "0", "title": "Hlavní svět", "mode": "surface"},
    {"key": "-1", "title": "Nether", "mode": "caves"},
    {"key": "x:tropics", "path": "TROPICS", "title": "Tropy", "mode": "surface"}
  ],
  "limits": {"workers": 1, "memoryMiB": 2048, "minFreeGiB": 24},
  "web": {"host": "127.0.0.1", "port": 8211}
}
```

```sh
python uiemap_config.py uie-map.json
```

Svět a katalog musí existovat. Jejich relativní cesty i cesta výstupu se vyhodnocují vůči umístění konfiguračního souboru, nikoli aktuální pracovní složce. Cesta dimenze je relativní vůči světu. Rozpoznávání adresářů podrobněji popisuje [DIMENZE.md](DIMENZE.md).

Vybrané jsou pouze dimenze uvedené v konfiguraci. Každá dvojice ID světa a klíče dimenze dostane vlastní výstupní adresář odvozený z SHA-256. Uvnitř jsou oddělené cesty `surface`, `caves` a `collisions`. Názvy z uživatelské konfigurace se přímo nevkládají do cest. Popisek lze změnit bez změny tohoto rozdělení. Všechny dimenze sdílejí původní `level.dat`, ale mají vlastní adresář regionů.

ID světa je stabilní identifikátor zvolený uživatelem. **Nepoužívejte stejné ID pro dva různé světy se společným výstupem.** Experimentální CLI renderer již kontroluje trvalou vazbu úložiště na vstup, viz [DIMENZE.md](DIMENZE.md). Samotný náhled konfigurace stará data nezkoumá a renderer stále nespouští.

Validace odmítá překrytí výstupu se světem či katalogem včetně jejich nadřazených adresářů, duplicitní klíče, chybné typy a neznámá pole. Soubor má limit64KiB. Kontrola neověřuje verzi Minecraftu, katalog, dostupnou RAM, volný disk ani obsazení portu. Limity jsou zatím ověřené hodnoty budoucího nastavení; tento nástroj je nevynucuje při běhu rendereru.

`surface` a `caves` označují zamýšlený režim. Nether se stropem vyžaduje další práci; `surface` pro dimenzi-1 proto zobrazí varování a zvláštní režim `ceiling` ještě není podporovaný. Přijetí konfigurace neznamená dokončenou podporu renderu dimenze.

Ověřeno19Linux testy (konfigurace a rozpoznání dimenzí), na Windows17prošlo a2POSIXkontroly byly přeskočené. Skutečný CLI náhled na UIE ověřil tři oddělené dimenze a společný registr, aniž vytvořil výstupní adresář nebo spustil render.

## Spuštění jedné povrchové dimenze

Nejprve zkontrolujte plán konkrétní dimenze:

```sh
python uiemap_render.py uie-map.json --dimension 0
```

Bez `--apply` se nic nezapisuje. Plán zahrnuje zdroj světa, katalog, oddělený výstup, celý render vybrané dimenze a limity. Stávající výstup musí mít odpovídající trvalou vazbu; cizí nebo starý nepřiřazený adresář příkaz nepřevezme.

Na Linuxu s dostupným uživatelským systemd spustíte render a navazující publikování:

```sh
python uiemap_render.py uie-map.json --dimension 0 --apply
```

Příkaz čeká na dokončení a vypíše jméno své jednotky `uiemap-render-….service`. `memoryMiB` omezuje celou skupinu procesů včetně pracovníků. Rezerva `minFreeGiB` se kontroluje před startem a přibližně každou sekundu během práce; překročení rezervy ukončí jednotku a její potomky. Jde o hlídání volného místa, nikoli pevnou diskovou kvótu. Přerušení příkazu pomocí Ctrl+C zastaví jeho jednotku.

Změna konfiguračního souboru mezi plánem a startem pracovníka je odmítnuta. Publikování proběhne až po dokončeném plánu bez chyb; výsledný JSON musí obsahovat `published: true` a konkrétní `indexSha256`. Při chybě zůstávají rozpracovaná data pro opravu a další pokus. Web se automaticky nepřepíná ani nerestartuje; ruční připojení hotového výstupu popisuje [DIMENZE.md](DIMENZE.md).

Tento příkaz přijímá `surface` i `caves`, odmítá Nether `-1` kvůli nehotovému řešení stropu a nespouští další dimenze automaticky. Import katalogu z modpacku je samostatná nedokončená část. Na Windows je dostupný náhled, nikoli spuštění přes systemd.

Ověření19.10.: celý skutečný povrch DIM42 spuštěný přes `--apply`,161dlaždic a publikování za přibližně65s; všech161SHA ověřeno.28Linux testů PASS. Veřejná data ani služby tímto testem nebyly přepnuté.

## Jednotný příkaz: od konfigurace k webu

Předpoklady: Python3.12+, Node.js22+, pro render Linux s uživatelským systemd; vlastní svět Minecraft1.7.10 a odpovídající připravený katalog. Automatický import modpacku zatím není součástí tohoto postupu.

Pro první pokus ponechte v konfiguraci pouze jednu existující povrchovou dimenzi. Pro hlavní svět použijte `key: "0"`, `mode: "surface"`; nastavte existující cesty světa a katalogu a nový výstup mimo oba zdroje. Port webu musí být volný.

```sh
python uiemap.py check uie-map.json
python uiemap.py render uie-map.json --dimension 0
python uiemap.py render uie-map.json --dimension 0 --apply
python uiemap.py serve uie-map.json --main-dimension 0
python uiemap.py serve uie-map.json --main-dimension 0 --apply
```

`check` a oba příkazy bez `--apply` jsou nezapisující. `render --apply` skutečně zpracuje celou vybranou dimenzi a publikuje výsledek. `serve --apply` z dokončených výstupů vytvoří vlastní neměnný webový katalog a spustí Node v popředí. Otevřete adresu a port z pole `web`; host `127.0.0.1` je dostupný jen na daném stroji. Příkaz nepřepisuje PM2 ani běžící služby.

Pro více povrchových dimenzí nejprve dokončete `render --apply` pro každou dimenzi uvedenou v konfiguraci. `serve` vyžaduje publikované výstupy všech vybraných dimenzí; chybějící či neodpovídající data nezamění za hlavní svět. Výchozí dimenzi určuje `--main-dimension`, ostatní se objeví v přepínači. Katalog je ověřen podle trvalých vazeb a SHA indexů; web následně ověřuje vlastní přenosy. Výstupní cesty z konfigurace není nutné ručně kopírovat do proměnných prostředí.

Tento postup byl19.10. skutečně ověřen na odděleném kompletním exportu DIM42: generovaný web se otevřel, zobrazil terén a umožnil létání; žádná data hlavní mapy nepřebíral. Není to ještě ověření čisté instalace na jiném modpacku. Aktuálně už CLI nabízí plánovač, povrchovou retenci a export caves; propojení konfigurovaných caves s webem a kolizní export ještě nejsou hotové.


## Kontrola před spuštěním

`python -B uiemap.py doctor /cesta/config.json` vrátí JSON se samostatným výsledkem každé kontroly a návratový kód 0 při splnění kontrol, jinak 2. Nic nerenderuje, nevytváří výstup a nespouští služby. Ověřuje čitelnost souborů katalogu, načtení kompilovaného katalogu, registr Forge v level.dat, plán vybraných dimenzí, rezervu místa, dostupnost uživatelského systemd a Node.js 18+. Dotazy na verze mají časový limit 10 sekund.

`prerequisitesReady` znamená splněné uvedené předpoklady. Nejde o potvrzení správnosti všech regionů, obrazových souborů, modovaných modelů, Minecraft verze, volného portu ani dostupnosti všech cgroup controllerů. Automatický import jiného modpacku ještě není implementovaný. Kontrola vyhodnocuje render i web; na Windows proto oznámí chybějící Linux systemd i při dostupném Node. Žádný Minecraft klient není potřeba.


### Příprava přenosného katalogu (rozpracované)

`python -B catalog_import_plan.py --assets assets.json --layout atlas-layout.json` pouze kontroluje existující export metadat, bez zápisu. Zkontroluje šest stran, metadata 0–15, barvy včetně Java ARGB a UV včetně zrcadlení. Vypíše SHA vstupů a názvy textur mimo registrovaný atlas; takové názvy mohou být dynamické varianty Chisel s vlastními UV, automaticky se nezahazují. Ověřeno na exportu UIE: 6300 bloků a 100800 variant. PNG, shoda atlasu s exportem, vytvoření katalogu a import nového modpacku zatím čekají. Tento příkaz není dokončený importér.


### Vytvoření katalogu z existujícího exportu

Tento krok je hotový a ověřený na podkladech UIE. Nevytahuje data ze samotných mod JARů; potřebuje již připravené `assets.json`, `atlas-layout.json`, `atlas.png`, `grass.png` a `foliage.png` v jedné složce. Minecraft klient se při importu nespouští.

```sh
python -B uiemap.py catalog --source /data/export --out /data/catalog-new
python -B uiemap.py catalog --source /data/export --out /data/catalog-new --apply
```

První příkaz je pouze kontrola metadat. Druhý zkopíruje podklady do pracovní složky, ověří vazbu SHA a rozměrů atlasu na layout, CRC PNG, úplnost omezeného zlib streamu a filtry řádků. Podporuje 8bit RGB/RGBA/grey PNG bez prokládání; colormapy musí mít 256 × 256 pixelů. Vytvoří `catalog.json` a `package.json` s SHA všech vstupů a kompilovaného katalogu. Dokončený adresář zpřístupní přejmenováním na novou cílovou cestu. Rodič cíle musí existovat; existující katalog nepřepíše. Při běžné chybě vlastní pracovní adresář uklidí. Po tvrdém pádu může zůstat `.import-lock` a `.catalog-*`; před jejich ručním úklidem ověřte, že import neběží.

V konfiguraci nastavte `catalog` na nový adresář a spusťte `doctor`. Import neprokazuje, že assets a atlas pocházejí ze stejného modpacku, ani správnost všech modelů. To stále vyžaduje odpovídající zdrojový export a render vzorku. Ověření UIE: kompilovaný katalog má přesně stejné bajty i identity jako původní; 6300 bloků, 26607 položek tabulky textur včetně dynamických UV.


## Stav všech vybraných dimenzí

`python -B uiemap.py status /cesta/config.json` přečte stav bez renderování, zápisu nebo spouštění služeb. Každá dimenze má zvlášť `publication` a `render`: nově selhávající výpočet nesmaže informaci o posledním platném publikovaném indexu. Kontrolují se binding světa/dimenze, SHA indexu a formát odkazů na dlaždice; obsah všech dlaždic se znovu nečte (`payloadsVerified: false`).

- `published`: existuje platný publikovaný index; není to potvrzení aktuálnosti vůči právě běžícímu světu.
- `unpublished`: chybí publikovaný ukazatel. `not-started`: pro dimenzi ještě neexistuje výstupní adresář.
- `running-reported`: soubor uvádí běh a heartbeat není starší než 120 sekund. Proces se tím nepotvrzuje (`processVerified: false`).
- `stale-running-record`: údaj o běhu má chybějící, starý nebo budoucí heartbeat. Samotný příkaz proces nezabíjí ani nerestartuje.
- `done` je zaznamenaný konec výpočtu. Sledujte také `planComplete` a `hasFailures`; dokončení procesu samo neznamená úspěšný celý svět.
- `error`: metadata jsou nečitelná, poškozená nebo patří jinému světu/dimenzi. Ostatní dimenze se zobrazí dál. Návratový kód je 2 při chybě metadat, jinak 0; zaznamenané neúspěšné render úlohy jsou samostatně v `hasFailures`.

`total` a `done` počítají úlohy daného běhu; `plannedJobs` všechny oblasti plánu a `skippedUnchanged` převzaté nezměněné oblasti. Stav je okamžitý pohled na metadata, nikoli atomický snímek všech běžících procesů.


## Jeden aktualizační průchod všech vybraných dimenzí

```sh
python -B uiemap.py update /cesta/config.json
python -B uiemap.py update /cesta/config.json --apply
```

Bez `--apply` se pouze připraví plán. S `--apply` se dimenze z konfigurace aktualizují postupně, vždy přes existující Linux systemd renderer s jeho limity RAM, pracovníků a rezervy disku. Nezměněné oblasti umí renderer převzít. Všechny dimenze se předem zkontrolují; Nether je zatím odmítnutý; caves se publikují spolu s daty řezu. Konfigurace je připnutá hashem a při změně se další práce zastaví.

První selhání průchod zastaví; výsledný JSON obsahuje dokončené výsledky a dosud nezpracované dimenze. Nestačí návratový kód 0: vyžaduje se potvrzení publikace pro správný svět, dimenzi, adresář a hash indexu. Návratový kód příkazu je 2 při neúspěchu, 0 při úspěchu nebo platném náhledu. Souhrn se zobrazí až po skončení průchodu; během běhu lze v druhém terminálu použít `uiemap.py status CONFIG`.

Příkaz neinstaluje timer, nespouští další dimenze souběžně a nemění konfiguraci webu. Publikuje ale do nakonfigurovaných výstupů, takže web již připojený k těmto store může nová data převzít. Pravidelné plánování a jednotná retence jsou další rozpracované části.


## Pravidelné aktualizace přes uživatelský systemd

```sh
python -B uiemap.py schedule /cesta/config.json --interval 900
python -B uiemap.py schedule /cesta/config.json --interval 900 --apply
```

Bez `--apply` získáte pouze obsah jednotek a jejich název. S `--apply` se na Linuxu zapíší do `~/.config/systemd/user` a aktivuje se timer. První průchod začne za 30 sekund; další za uvedený interval od dokončení předchozího průchodu, nikoli od jeho začátku. Interval je 60–86400 sekund. Stejná systemd služba neběží dvakrát souběžně; ručně spuštěné aktualizace jsou samostatné procesy a používají existující zámky rendereru.

Jméno `uiemap-update-<hash>` je součástí výsledného JSON. Dosadíte ho do následujících příkazů:

```sh
systemctl --user status uiemap-update-<hash>.timer
journalctl --user -u uiemap-update-<hash>.service
systemctl --user disable --now uiemap-update-<hash>.timer
systemctl --user stop uiemap-update-<hash>.service
```

Vypnutí timeru brání dalším spuštěním; samostatné zastavení služby přeruší aktivní průchod. Služba posílá SIGINT procesům průchodu, aby renderer mohl ukončit vlastní omezenou systemd úlohu. Přehled renderu zůstává v `uiemap.py status CONFIG`; souhrn průchodu a chyby jsou v journalu.

Konfigurace a zdrojový adresář programu musí zůstat na stejných cestách. Existující jednotky s odlišným obsahem se nepřepisují; při změně intervalu nejprve vypněte timer, zastavte službu a odstraňte pouze tuto dvojici jednotek. Potom lze instalaci opakovat. Pokud aktivace selže, mohou zůstat již zapsané jednotky; výsledek se nehlásí jako úspěch.

Příkaz nezapíná uživatelské lingering oprávnění a nepotvrzuje běh po rebootu nebo odhlášení. To závisí na nastavení uživatelského systemd na konkrétním serveru. Ověřen byl skutečný timer, jeden celý aktualizační průchod a vypnutí/úklid soukromé testovací služby; restart serveru se kvůli hře neprováděl.


## Omezený úklid nepoužívaných povrchových dat

```sh
python -B uiemap.py gc /data/config.json --dimension 0
python -B uiemap.py gc /data/config.json --dimension 0 --max-files 32 --max-bytes 16777216 --apply
```

Výchozí režim nic nemaže, ale může vytvořit soubor zámku rendereru. Vybere se právě jedna dimenze uvedená v konfiguraci a ověří se její binding. Příkaz používá existující retenční plán pod exkluzivním zámkem: chrání publikované indexy, jejich odkazy, chráněnou historii a rozpracovanou přípravu. Při obsazeném rendereru vrátí `producer-busy` bez úklidu.

Jeden průchod odstraní nejvýše 128 souborů / 64 MiB logických velikostí; menší limity lze nastavit. Logické bajty nejsou zárukou stejného uvolnění disku kvůli hardlinkům. Mažou se pouze nepoužívané obsahově adresované soubory v adresářích immutable store. Svět, zdrojový katalog, staré klientské artefakty a legacy mutable tiles se tím neuklízejí. Chyba po části mazání může zanechat částečně dokončenou dávku; další pokus vždy vytváří nový plán.

Úklid po úspěšné publikaci lze výslovně zapnout:

```sh
python -B uiemap.py update /data/config.json --gc --apply
python -B uiemap.py schedule /data/config.json --interval 900 --gc --apply
```

Bez `--gc` automatická aktualizace nic neuklízí. S přepínačem proběhne jedna výchozí omezená dávka pro každou úspěšně publikovanou dimenzi. Při chybě publikace se úklid neprovede. Při chybě úklidu se průchod zastaví, ale výsledek ponechá potvrzení úspěšné publikace; ostatní dimenze jsou v `pending`. Obsazený renderer znamená odložený úklid, nikoli smazání dat za běhu. Změněná konfigurace proti plánu je odmítnutá.

Ověřeno na soukromém store DIM42: skutečné odstranění jednoho nepoužívaného indexu, nezměněný current ukazatel a všech 163 ověřovaných souborů aktuální mapy. Ověřen také timer s aktualizací a následným GC. Konfigurovaný úklid kolizí a jeskyní není touto povrchovou funkcí dokončený.


## Jeskynní export z konfigurace

Pro vybranou dimenzi nastavte `"mode": "caves"`. Stejné příkazy `render` a `update` pak vytvoří geometrii i companion data pro uzavírání řezu v samostatném podadresáři `caves`; povrchový store se nepřepisuje. Používají se stejné limity RAM a volného disku. Nether `-1` zatím není podporovaný ani touto cestou.

```sh
python -B uiemap.py render /data/cave-config.json --dimension 42 --apply
python -B uiemap.py update /data/cave-config.json --apply
python -B uiemap.py status /data/cave-config.json
```

Úspěšný výsledek obsahuje `mode: caves`, `indexSha256` i `capIndexSha256`. Samotná publikace geometrie se za úspěšný jeskynní výsledek nepovažuje. Ve `status` znamená `awaiting-caps`, že companion index chybí nebo neodpovídá geometrii; opakovaný běh může dokončit přerušenou publikaci. Proces zatím nepřepíná žádný web.

V jedné konfiguraci může být klíč dimenze uveden jen jednou. Chcete-li pro stejnou dimenzi povrch i caves, použijte dvě konfigurace se stejným world ID, cestami a rozdílným mode. Neplánujte je bez rozmyslu souběžně: každá služba má vlastní limit paměti. `update --gc` a `schedule --gc` odmítnou konfiguraci s caves před spuštěním, protože společný konfigurovaný úklid geometrie a cap dat ještě není připojený.

Ověřeno 24.10. na celé soukromé DIM42: 10 oblastí, 161 dvojic, nulové chyby, přibližně 95 s prvního renderu. Všechny dvojice geometrie a rozbalených cap dat prošly nezávislou kontrolou hashů. Ověřen i navazující skutečný `update`. Webové připojení je od 27. 10. ověřené soukromě: `serve` přijímá povrchovou konfiguraci a volitelný jeskynní protějšek. Veřejná ukázka tím nebyla přepnutá.


## Kontrola služby po restartu

`python -B uiemap.py services /data/config.json` přečte skutečný stav uživatelského systemd timeru a služby vytvořených příkazem `schedule` pro tuto cestu konfigurace. Načtení konfigurace ani existence světa nejsou potřebné: službu lze dohledat i při poškozeném vstupu. Příkaz nic nespouští, nezastavuje a nepřepisuje.

Výstup rozlišuje `not-installed`, skutečný `active`/`inactive`/`failed` stav a `error` při nedostupném systemd nebo timeoutu. Neaktivní oneshot služba je mezi běhy normální; sledujte také timer a `Result`/`ExecMainStatus`. Návratový kód 2 znamená chybu čtení, nepodporovanou platformu nebo stav `failed`. Chybějící jednotky nejsou vydávány za běžící službu. Příkaz pokrývá pouze jednotky dané konfigurace, nikoli starší ručně instalované produkční služby nebo Minecraft server. Stav renderovaných souborů poskytuje samostatný příkaz `status`.


### Připojení jeskyní k vlastnímu webu

```sh
python uiemap.py serve surface.json --main-dimension 42 --cave-config caves.json
python uiemap.py serve surface.json --main-dimension 42 --cave-config caves.json --apply
```

Oba exporty musí být předem publikované. `surface.json` obsahuje povrchové dimenze; `caves.json` jejich vybrané protějšky s `mode: "caves"`. Identita i cesta světa a vybraných dimenzí musí souhlasit. Jeskynní konfigurace může obsahovat jen podmnožinu povrchových dimenzí. Kontrolují se vazby úložišť a souhlas indexu geometrie s indexem uzávěrů řezu. Neúplné nebo nesouvisející indexy spuštění odmítnou.

Výchozí web načítá povrch; zapnutí výškového řezu načte podzemí a uzávěry okolních dlaždic. Vypnutí vrací povrch. Funguje také odkaz `?cut=48`. Připojené jeskyně jsou označené novým bootstrap formátem `UIE_MAP_VIEW2`; staré povrchové soubory `UIE_MAP_VIEW1` zůstávají podporované. Původní konfigurace `UIEMAP_CONFIG1` se nemění. Blokové kolize ani automatická údržba cave dat tím nejsou doplněné.

Ověřeno skutečným prohlížečem na DIM42: povrch → jeskyně → povrch, přímý odkaz s řezem, načtení geometrie i uzávěrů z indexu 161 párů; 8 okolních párů načteno. Výchozí povrch nenačítal cave payloady. Nejde o nově nasazené jeskyně DIM42 ve veřejné ukázce.


## Kolize a omezený úklid jeskyní (27.10.2026)

Jednorázový export blokových kolizí pro již publikovaný povrch:

```sh
python uiemap.py collisions config.json --dimension 42
python uiemap.py collisions config.json --dimension 42 --apply
python uiemap.py serve config.json --main-dimension 42 --collisions --apply
```

Bez `--apply` se vypíše plán. Export používá registry kořenového světa a regiony vybrané dimenze. Web s `--collisions` vyžaduje odpovídající export pro každou vybranou dimenzi; cizí nebo zastaralé párování odmítne. Lze kombinovat s `--cave-config caves.json`. Export a chůze byly ověřeny na DIM42; speciální modované kolizní tvary zůstávají přibližné. Samotný příkaz `collisions` je jednorázový; pravidelný provoz zapíná `schedule --collisions` níže.

```sh
python uiemap.py cave-gc caves.json --dimension 42
python uiemap.py cave-gc caves.json --dimension 42 --apply --max-files 64 --max-bytes 16777216
```

Tento omezený úklid spravuje pouze doprovodné cave indexy, páry, uzávěry a aliasy. Neuklízí kořenovou geometrii; existující povrchové `gc` ji nenahrazuje. Bez `--apply` nemaže soubory (může vytvořit soubor zámku). Skutečné odstranění jednoho nepotřebného aliasu a následná integrita všech publikovaných párů byly ověřeny na oddělené kopii.

Veřejná ukázka od 27.10. obsahuje i jeskyně DIM42 s aktualizací po 30 minutách. Blokové kolize DIM42 jsou veřejné a povrch s kolizemi má samostatnou30min pravidelnou obnovu; první skutečný běh časovače prošel.


### Společné aktualizace povrchu a kolizí

```sh
python uiemap.py update config.json --collisions
python uiemap.py update config.json --collisions --apply
python uiemap.py schedule config.json --collisions --interval 1800 --apply
```

Volba vyžaduje povrchové dimenze. Každou zpracuje sekvenčně: publikování povrchu, odpovídající kolize, případný opt-in povrchový úklid. Nestačí nulový návratový kód; oba kroky musí potvrdit správný svět, dimenzi a cílové uložiště. Chyba kolizí zastaví další dimenze a zachová potvrzení úspěšného povrchu. Nejde o atomické přepnutí obou dat: během přepočtu klient používá jen kolize odpovídající načtené geometrii, jinak jeho běžný náhradní režim. Jeskynní aktualizace mají vlastní konfiguraci a časovač. Existující odlišnou systemd jednotku příkaz nepřepisuje.

Na veřejné DIM42 ověřen první automatický průchod za přibližně9s při nezměněných10oblastech. Toto není odhad přepočtu změněného světa. Interval se počítá od dokončení; user systemd musí zůstat dostupný i po odhlášení.


### Omezená kolizní retence (opt-in)

```sh
python uiemap.py collisions config.json --dimension 42 --gc --apply
python uiemap.py update config.json --collisions --collision-gc --apply
python uiemap.py schedule config.json --collisions --collision-gc --interval 1800 --apply
```

Po úspěšném exportu se ověří vlastnictví výstupu, inicializuje evidence chráněných generací a provede nejvýše jeden omezený úklid:64souborů/16MiB, nejvýše50ověřovacích kroků a120s mezi kroky. Aktuální generace, její data a ochranné lhůty zůstávají chráněné. Nenulová chyba úklidu znamená neúspěšnou aktualizaci; publikovaná data se tím nevracejí zpět.

Po zapnutí profilu pokračujte s tímto přepínačem: starý exporter bez podpory údržby takové uložiště úmyslně odmítá. Existující odlišnou systemd jednotku příkaz nepřepisuje. Povrchové `--gc` je samostatná funkce. Tento režim nemaže svět, katalog ani klientské prostředky.

Na DIM42 ověřena inicializace, opakovaný export s aktivním profilem i produkční společný běh. První průchod nesmazal žádné soubory, protože nebyly způsobilé; všech161publikovaných párů následně nezávisle prošlo kontrolou. Automatická retence DIM42 je nasazená, nikoli tím automaticky retence všech ostatních dimenzí.


### Úklid jeskynní geometrie

```sh
python uiemap.py cave-gc caves.json --dimension 42 --geometry
python uiemap.py cave-gc caves.json --dimension 42 --geometry --apply
python uiemap.py update caves.json --cave-gc --apply
python uiemap.py schedule caves.json --cave-gc --interval 1800 --apply
```

Bez `--geometry` zůstává `cave-gc` úklidem doprovodných dat. S touto volbou spravuje root geometrii,indexy,atlasy a rozložení; všechny stále existující jeskynní indexy a páry chrání své odkazy. Poškozená metadata zastaví plán. Automatické `--cave-gc` po publikaci nejprve uklízí doprovodná data, potom znovu vyhodnotí ochranu a uklízí geometrii. Každá fáze má limit64souborů/16MiB; jde o logické velikosti, hardlinky mohou znamenat menší skutečnou úsporu. Svět,katalog a klientské prostředky se nemažou.

Ověřeno na oddělené DIM42 a nasazeno pro hlavní veřejný svět i DIM42. První dva hlavní průchody každý odstranily tři staré jeskynní indexy a tři odpovídající geometrické indexy, aktuální publikace zůstaly stejné. Provoz hlavního úklidu dosáhl přibližně384MiB paměti. Existující odlišnou systemd jednotku generátor nepřepisuje.
