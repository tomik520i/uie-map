# Náhled společné konfigurace

**HOTOVO:** nezapisující validace a rozdělení výstupních cest. **ČEKÁ:** napojení rendereru, publisheru, kolizí, jeskyní a webového serveru. Příkaz níže nic nespouští a nevytváří adresáře. Výsledek proto výslovně obsahuje `renderReady: false`.

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

ID světa je stabilní identifikátor zvolený uživatelem. **Nepoužívejte stejné ID pro dva různé světy se společným výstupem.** Trvalé ověření vazby existujícího úložiště na konkrétní vstup musí být součástí následné integrace rendereru; samotný náhled konfigurace stará data nezkoumá.

Validace odmítá překrytí výstupu se světem či katalogem včetně jejich nadřazených adresářů, duplicitní klíče, chybné typy a neznámá pole. Soubor má limit64KiB. Kontrola neověřuje verzi Minecraftu, katalog, dostupnou RAM, volný disk ani obsazení portu. Limity jsou zatím ověřené hodnoty budoucího nastavení; tento nástroj je nevynucuje při běhu rendereru.

`surface` a `caves` označují zamýšlený režim. Nether se stropem vyžaduje další práci; `surface` pro dimenzi-1 proto zobrazí varování a zvláštní režim `ceiling` ještě není podporovaný. Přijetí konfigurace neznamená dokončenou podporu renderu dimenze.

Ověřeno19Linux testy (konfigurace a rozpoznání dimenzí), na Windows17prošlo a2POSIXkontroly byly přeskočené. Skutečný CLI náhled na UIE ověřil tři oddělené dimenze a společný registr, aniž vytvořil výstupní adresář nebo spustil render.
