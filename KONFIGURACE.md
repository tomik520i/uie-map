# Společná konfigurace a spuštění povrchového renderu

**HOTOVO:** nezapisující validace a rozdělení výstupních cest. Nový příkaz `uiemap_render.py` propojuje vybranou povrchovou dimenzi s rendererem a publisherem. **ČEKÁ:** automatické nastavení webového katalogu, kolize a jeskyně z této konfigurace. Příkaz `uiemap_config.py` níže nadále nic nespouští a nevytváří adresáře. Jeho výsledek proto výslovně obsahuje `renderReady: false`.

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

Tento příkaz zatím přijímá pouze `surface`, odmítá Nether `-1` kvůli nehotovému řešení stropu a nespouští další dimenze automaticky. Import katalogu z modpacku je samostatná nedokončená část. Na Windows je dostupný náhled, nikoli spuštění přes systemd.

Ověření19.10.: celý skutečný povrch DIM42 spuštěný přes `--apply`,161dlaždic a publikování za přibližně65s; všech161SHA ověřeno.28Linux testů PASS. Veřejná data ani služby tímto testem nebyly přepnuté.
