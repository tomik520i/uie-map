# Ověření zveřejněného prototypu

Stav k 12. říjnu 2026. Testy nejsou zárukou podpory všech modů ani potvrzením, že je celý plán dokončený.

- **75 základních Python testů prošlo** přímo v připravené kopii repozitáře: čtení světa, renderer, dlaždice a regresní testy barev.
- **JavaScript: 503 testů prošlo, 1 přeskočen**, celkem 504. Přeskočená kontrola vyžaduje neveřejná exportovaná data, která repozitář neobsahuje.
- **62 Linux testů kolizního exportu prošlo** v odděleném prostředí, včetně opakování výpočtu s čistou cache.
- **96 Linux testů retence a publikování prošlo** v odděleném prostředí.
- Úklid byl ověřen i na soukromých kopiích dvou skutečných datových párů se simulovaným stárnutím. Produkční soubory se při tomto ověření nemazaly.
- Dvě skutečné problémové dlaždice prošly opakovaným výpočtem a úplným ověřením výsledných dat. Následně se studenou cache prošel i celý původně odmítnutý region: 64/64 dlaždic se shodným hashem. To nenahrazuje dokončení exportu celé mapy.

## Důležité rozlišení

**Implementované a otestované neznamená nasazené.** Změna rendereru mění identitu generace a musí projít samostatným přepočtem a publikací. Oprava barev modovaných biomů a celosvětová kolizní retence jsou stále ve vývojové fázi. Obnova s čistou cache byla nasazena do soukromého celosvětového kolizního exportu; veřejné přepnutí těchto dat ještě neproběhlo.

Vybrané testy se spouštějí také přes GitHub Actions při změnách hlavní větve a v pull requestech. Aktuální výsledek je na kartě Actions; tento dokument nenahrazuje výsledek konkrétního běhu.

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
