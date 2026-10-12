# Ověření zveřejněného prototypu

Stav k 12. říjnu 2026. Testy nejsou zárukou podpory všech modů ani potvrzením, že je celý plán dokončený.

- **75 základních Python testů prošlo** přímo v připravené kopii repozitáře: čtení světa, renderer, dlaždice a regresní testy barev.
- **JavaScript: 503 testů prošlo, 1 přeskočen**, celkem 504. Přeskočená kontrola vyžaduje neveřejná exportovaná data, která repozitář neobsahuje.
- **56 Linux testů kolizního exportu prošlo** v odděleném prostředí, včetně opakování výpočtu s čistou cache.
- **96 Linux testů retence a publikování prošlo** v odděleném prostředí.
- Úklid byl ověřen i na soukromých kopiích dvou skutečných datových párů se simulovaným stárnutím. Produkční soubory se při tomto ověření nemazaly.
- Dvě skutečné problémové dlaždice prošly opakovaným výpočtem a úplným ověřením výsledných dat. To nenahrazuje dokončení exportu celé mapy.

## Důležité rozlišení

**Implementované a otestované neznamená nasazené.** Změna rendereru mění identitu generace a musí projít samostatným přepočtem a publikací. Zejména oprava barev modovaných biomů, celosvětová kolizní retence a obnova s čistou cache byly v době tohoto zápisu ve vývojové fázi.

## Spuštění kontrol

Základní Python testy:

```sh
python -m unittest test_reader test_offline_renderer test_world_hires test_colormap_determinism.ColorDeterminismTests
```

Webové testy (POSIX shell):

```sh
node --test test_*.mjs
```

Kolizní export (Linux):

```sh
python -m unittest test_collision_world_store test_collision_world_region test_collision_world_runner test_collision_pair_stage test_collision_region_probe
```

Retence (Linux):

```sh
python -m unittest test_collision_world_retention test_collision_world_store test_collision_world_gc test_collision_gc test_collision_retention
```

Provozní testy skutečného světa nejsou součástí těchto příkazů. Vyžadují vlastní vstupní data a izolované výstupy; testovací příkazy nemají nahrazovat kontrolu produkčního nasazení.
