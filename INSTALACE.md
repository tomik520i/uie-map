# Instalace prototypu s připraveným katalogem

Tento postup pokrývá povrchový render a web pro vlastní Forge svět Minecraft 1.7.10. Potřebujete vlastní svět a připravený katalog, nebo existující export pro jeho sestavení. Automatické získání modelů ze samotných JARů zatím chybí. Klient Minecraftu při níže uvedených krocích neběží.

## 1. Prostředí a zdroje

Ověřovací prostředí používá Ubuntu, Python 3.12, Node.js 18; CI používá Node.js 22. Pro spuštění rendereru a plánovače je potřebný dostupný uživatelský systemd. Pythonové jádro nevyžaduje balíčky z pip, webový prohlížeč má Three.js přiložený. Git potřebujete pouze pro stažení/aktualizaci zdrojů; alternativou je zdrojový ZIP GitHubu.

```sh
git clone https://github.com/tomik520i/uie-map.git
cd uie-map
python3 -B uiemap.py --help
node --version
systemctl --user show --property=Version --value
```

Používejte svůj běžný uživatelský účet. Zdrojovou složku po instalaci timeru nepřesouvejte, protože služba odkazuje na konkrétní cestu.

## 2. Vstupy a konfigurace

Svět obsahuje `level.dat` a `region/*.mca`; jednotlivé dimenze mívají vlastní podadresáře `DIM…/region`. Render svět čte; výstupy musejí být v oddělené složce. Katalog musí obsahovat `catalog.json`, `atlas-layout.json`, `atlas.png`, `grass.png` a `foliage.png` pro odpovídající modpack.

Máte-li již exportované podklady včetně `assets.json`, vytvořte nový katalog:

```sh
python3 -B uiemap.py catalog --source /data/export --out /data/catalog-new --apply
```

Vzor najdete v [examples/config.json](examples/config.json). Zkopírujte jej do vlastní konfigurace a upravte cesty. Relativní cesty se vyhodnocují vůči umístění konfigurace, nikoli aktuálnímu terminálu. Pole `world.id` je vaše stabilní označení světa; neměňte ho pro již používané výstupy.

Dimenze se vybírají výslovně. Začněte jednou povrchovou dimenzí; `0` znamená hlavní svět. Další číselná dimenze například `42` standardně používá `DIM42`. Nether pod stropem a konfigurovaný kolizní export zatím podporované nejsou. Samostatný cave export z konfigurace už funguje; jeho webové připojení ještě čeká, viz podrobná konfigurace. [Podrobná konfigurace](KONFIGURACE.md) popisuje vlastní cesty a omezení.

## 3. Kontrola a první render

```sh
python3 -B uiemap.py check /data/config.json
python3 -B uiemap.py doctor /data/config.json
python3 -B uiemap.py render /data/config.json --dimension 0
python3 -B uiemap.py render /data/config.json --dimension 0 --apply
python3 -B uiemap.py status /data/config.json
```

Příkazy bez `--apply` nepouštějí render. Rezerva disku není odhad velikosti výstupu: renderer zastaví práci při jejím překročení. Délka prvního renderu závisí na rozsahu světa a modovaných modelech; [měření a odhady](KAPACITA.md) nejsou zárukou pro jiný svět.

## 4. Web a opakované aktualizace

```sh
python3 -B uiemap.py serve /data/config.json --main-dimension 0 --apply
```

Web běží v popředí; při vzorové konfiguraci otevřete na stejném stroji `http://127.0.0.1:8211/`. Pro přístup z jiného počítače nastavte vhodný host nebo vlastní reverse proxy. Příkaz sám nenastavuje firewall, veřejnou doménu ani TLS.

V dalším terminálu lze spustit jeden průchod, případně pravidelný timer:

```sh
python3 -B uiemap.py update /data/config.json --apply
python3 -B uiemap.py schedule /data/config.json --interval 900 --apply
```

Timer se vztahuje k dané konfiguraci; nepřidávejte další aktualizační službu ke stejnému produkčnímu store bez kontroly již běžící údržby. Zastavení a logy jsou popsány v [KONFIGURACE.md](KONFIGURACE.md). Přetrvání uživatelského systemd po odhlášení/rebootu je nastavení konkrétního stroje, které program automaticky nemění.

## Co ještě není instalačním postupem vyřešené

- Získání plného katalogu pro libovolný nový modpack a ověření jeho modelů.
- Jednotná retence všech dat, konfigurovaný export kolizí a webové připojení nového konfigurovaného cave exportu.
- Automatická instalace webu jako služby, reverse proxy a TLS.
- Ověření celého systému po skutečném restartu stroje.

Repozitář ani zdrojový ZIP neobsahují svět, textury hry/modů či produkční nastavení. Vlastní zdrojový kód zatím nemá přidělenou open-source licenci; licence přiloženého Three.js je v jeho adresáři. Jde o raný prototyp, nikoli hotový univerzální Minecraft plugin.
