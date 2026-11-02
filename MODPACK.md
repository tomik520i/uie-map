# Kontrola vlastního modpacku

**HOTOVO / OVĚŘENO:** inventář modových JARů, propojení s registrem světa a volitelná kontrola blokových PNG bez spuštění Minecraftu. **NEDOKONČENO:** automatické vytvoření renderovacího katalogu ze samotných JARů.

Příkaz můžete spustit nad svou složkou `mods` na Windows nebo Linuxu:

```sh
python uiemap.py modpack --mods /cesta/k/minecraft/mods
```

Vypíše JSON s názvy archivů, dostupnými údaji z `mcmod.info`, počtem kandidátů textur, jejich jmennými prostory a konflikty názvů mezi archivy. Pokud mod nemá čitelné `mcmod.info`, zůstane v inventáři pod názvem JARu; program si jeho identitu nevymýšlí.

Pro úplný seznam cest k texturám:

```sh
python uiemap.py modpack --mods /cesta/k/minecraft/mods --include-textures > modpack-inventory.json
```

Samotný nástroj pouze čte. Zápis do souboru v druhém příkladu provádí přesměrování výstupu vašeho shellu. Nepřepisuje JARy, neimportuje konfiguraci, nerozbaluje textury a nespouští Java kód. Prohlíží pouze `.jar` soubory přímo ve zvolené složce; vnořené archivy a podsložky zatím neprochází.

## Jak číst výsledek

| Pole | Význam |
| --- | --- |
| `state: inventory-complete` | Všechny vybrané archivy byly prohlédnuté. Neznamená hotový renderovací katalog. |
| `state: partial` a `errors` | Některý archiv se nepodařilo zkontrolovat nebo byl překročen limit. Ostatní dostupné výsledky zůstávají ve výstupu. |
| `mods` | Údaje deklarované archivem v `mcmod.info`; nemusí odpovídat úplnému seznamu modulů načtených Forge. |
| `textureCandidates` | PNG položky pod `assets/<namespace>/textures/`. Jejich obrazová data ani CRC se nekontrolují. |
| `blockTextureCandidates` | Podmnožina ve složce `blocks` nebo `block`; ještě nejde o mapování textury na konkrétní blok. |
| `textureConflicts` | Stejný název prostředku poskytuje více archivů. Nástroj neurčuje, který z nich má při načtení přednost. |
| `catalogReady: false` | Inventář ještě nelze použít jako katalog pro render. |

Návratový kód je `0` při dokončeném inventáři, `1` při částečném výsledku a `2` při chybě vstupních parametrů. Nečitelná metadata jednotlivého modu jsou uvedena ve `warnings`; sama nezakazují prohlédnout názvy jeho textur.

## Porovnání s bloky konkrétního světa

Volitelně přidejte kořen světa obsahující Forge `level.dat` (nikoli adresář `DIM-1`):

```sh
python uiemap.py modpack --mods /cesta/mods --world /cesta/world
python uiemap.py modpack --mods /cesta/mods --world /cesta/world --include-blocks > modpack-registry.json
```

Report doplní skutečná číselná ID a názvy registrovaných bloků, souhrn po jmenných prostorech a kandidátní textury se shodným názvem. Bez `--include-blocks` vrací jen souhrn. Položky registru neznamenají, že se všechny tyto bloky ve světě skutečně nacházejí; chunky tento příkaz neprochází. Z `level.dat` nezveřejňuje hráčská data ani jiné údaje mimo registr bloků.

`blocksWithNameCandidates` je počet bloků s texturou odpovídající přesnému názvu `namespace:blocks/jmeno` nebo `namespace:block/jmeno`. **Není to počet vykreslitelných ani ověřeně podporovaných bloků.** Přiřazení stěn, metadata a geometrie zůstávají neověřené (`mappingVerified: false`). Rozdíly velikosti písmen uvádí odděleně a automaticky je nepřiřazuje. Neúplný průchod archivy poznáte podle `archiveScanComplete: false`.

Chybějící shoda neznamená chybějící mod. Mod může používat jiný název pro prostředky, sdílet textury s jiným modulem nebo je získávat jinak. Základní textury Minecraftu také obvykle nejsou v adresáři `mods`.

Pokud znáte správné přiřazení, lze ho zadat v samostatném JSON souboru, například:

```json
{"ExampleMod": "exampleassets"}
```

```sh
python uiemap.py modpack --mods /cesta/mods --world /cesta/world --namespace-map namespaces.json --include-blocks
```

Nahraďte ukázkové názvy skutečnými názvy své instalace. Levá strana musí existovat v registru; překlepy, duplicitní klíče a neplatné hodnoty příkaz odmítne. Report zachová původní názvy bloků a ukáže `textureNamespace` a `namespaceMappingExplicit`. Ani ručně přiřazený jmenný prostor sám nepotvrzuje model nebo správnost textury.

Ověření na UIE: **6 300 registrovaných bloků ve 163 jmenných prostorech**, z toho **314 s přesným kandidátem podle názvu** bez dodatečného přiřazení. Zbytek není seznam nepodporovaných bloků; současný připravený katalog používá další informace, které tento inventář nenahrazuje.

## Co ještě chybí k importu

### Kontrola obrazových dat

Výchozí inventář zkoumá jen názvy textur. Pro kontrolu samotných blokových PNG použijte:

```sh
python uiemap.py modpack --mods /cesta/mods --verify-block-textures > textures-check.json
```

Lze kombinovat s `--world`, `--include-blocks`, `--namespace-map` a `--include-textures`. Program nic nerozbaluje na disk a neprovádí Java kód. Kontroluje ZIP CRC, PNG CRC, strukturu, délku rozbalených dat, řádkové filtry a u paletových obrázků také existenci odkazovaných barev. Podporuje neprokládané PNG: šedé 1/2/4/8/16bitové, paletové 1/2/4/8bitové, RGB i varianty s alfa kanálem 8/16bitové. Prokládané PNG zatím hlásí odděleně jako `unsupported`, nikoli jako poškozené.

Souhrn `blockTextureValidation` obsahuje `checked`, `verified`, `invalid`, `unsupported`, `limited` a `complete`. U každého archivu je seznam `issues` s přesnou cestou, názvem prostředku a důvodem. S `--include-textures` jsou u úspěšných kontrol také rozměry, barevný typ, bitová hloubka a SHA-256. Stejná textura z více JARů se ověřuje v každém poskytovateli; pořadí modů se neodhaduje.

`invalid` znamená odmítnutí přísnou PNG/ZIP kontrolou. Může jít i o jiný formát s příponou `.png` nebo přebytečné bajty za koncem PNG, které jiný prohlížeč toleruje. Neznamená to automaticky rozbitý mod. `unsupported` znamená omezení kontrolního nástroje. Při kterékoli neověřené položce je stav `partial` a návratový kód `1`; podrobnosti zůstanou ve výstupu.

Limity jsou 4 MiB uložených PNG dat a 4 194 304 pixelů na obrázek, celkem 512 MiB deklarovaných PNG dat na jeden inventář. Nadlimitní položky se přeskočí s uvedeným důvodem. Kontrolují se pouze složky `blocks` a `block`, nikoli položky GUI či předmětů; proto obecné pole `texturePayloadsVerified` zůstává `false`. Přesnější výsledek je v `blockTextureValidation`.

Přítomnost souboru `.png.mcmeta` se eviduje, jeho obsah ani animace se neinterpretují. `staticSquareCandidate` znamená pouze čtvercový ověřený obrázek bez tohoto souboru. **Nepotvrzuje přiřazení ke stěně bloku, skutečně statickou texturu, model ani hotový katalog.** Kontrola zatím nekonvertuje pixely do atlasu a neověřuje ostatní doprovodná PNG metadata.

Skutečná kontrola UIE: 27 139 blokových PNG kandidátů z 276 JARů za přibližně 12,4 sekundy. Ověřeno 27 066; 54 nevyhovělo přísné kontrole, 14 bylo prokládaných a 5 přesáhlo limity. Soukromý report obsahuje přesné názvy, do veřejného repozitáře se herní prostředky neukládají. Související sada: 41 testů na Ubuntu PASS.

### Explicitní pravidla bloků a metadat

Příkaz `modpack-rules` připraví ověřený plán vazeb pro konkrétní svět. **Ještě nevytváří atlas ani katalog použitelný pro render.** U každého pravidla přesně uvedete registry název, metadata, šest stěn a poskytovatele textury. Žádná jiná metadata se nezdědí z první varianty.

```sh
python uiemap.py modpack-rules --mods /cesta/mods --world /cesta/world --rules rules.json > bindings.json
```

Ukázková struktura `rules.json` (názvy i SHA nahraďte skutečnými údaji):

```json
{
  "format": "UIEMAP_MODPACK_RULES1",
  "textures": {
    "stone": {
      "archive": "ExampleMod.jar",
      "entry": "assets/example/textures/blocks/stone.png",
      "sha256": "SEM_PATRI_64_ZNAKU_SHA256_SKUTECNEHO_PNG"
    }
  },
  "blocks": [{
    "name": "ExampleMod:stone",
    "metadata": [0, 2],
    "model": "cube",
    "opacity": "solid",
    "faces": ["stone", "stone", "stone", "stone", "stone", "stone"],
    "evidence": "Zde popište, jak jste ověřili tvar a přiřazení stěn."
  }]
}
```

SHA získáte z inventáře s `--verify-block-textures --include-textures`. Pořadí stěn je **dole, nahoře, sever, jih, západ, východ** (D U N S W E). Textury lze sdílet mezi pravidly; každá deklarovaná textura musí být použitá. Rozdílné stěny odkazují na rozdílné aliasy v `textures`.

První verze přijímá pouze explicitně deklarované kostky s neprůhledností `solid` nebo vyřezávanou průhledností `cutout`. Podporuje jen ověřené čtvercové PNG bez souboru `.mcmeta`. Rozměry, SHA a PNG data se znovu kontrolují při každém spuštění. Konflikt mezi JARy řeší vaše konkrétní volba `archive`; program nepředpokládá pořadí načítání modů. Pravidlo nemůže odkazovat mimo složku modů.

Číslo bloku se vždy vezme z registru zvoleného světa. Neznámé názvy, duplicitní JSON klíče, překrývající se metadata, chybějící stěny, změněné SHA či nepodporované PNG příkaz odmítne návratovým kódem `2`. Výstup uvádí `registrySha256` a deterministický `bindingSha256`, takže vazby pro různé registry nebo různé textury nelze zaměnit. Změna pořadí uvedených metadat nemění význam ani tento otisk.

`state: bindings-validated` znamená ověřené odkazy, existenci v registru a vybraná PNG data. **Neznamená nezávisle ověřenou podobu bloku:** `tier: user-declared`, `modelSemanticsVerified: false`, `catalogReady: false`. Poznámka `evidence` je tvrzení autora pravidla. Program ji neprověřuje proti Java rendereru. Výstup navíc určuje `unlistedMetadataPolicy: fallback`; navazující adaptér nesmí toto omezení obejít.

Ověřeno 51 souvisejícími testy na Ubuntu a technickou vazbou na skutečný registr a PNG UIE. Tento skutečný vzorek ověřil přenos ID a přiřazení zdroje, **nikoli správnost zvoleného tvaru**; produkční katalog se neměnil. Příkaz pouze čte, zápis `bindings.json` v ukázce provádí shell.

### Zbývající práce

Z názvu PNG nejde obecně odvodit všechny bloky, jejich číselná ID ve světě, metadata, orientace ani přiřazení textur jednotlivým stěnám. Tyto informace a geometrie vlastních Java rendererů potřebují další mapování nebo adaptéry. Existující ověřená cesta je stále [import připraveného exportu do katalogu](KONFIGURACE.md).

Na UIE byl inventář ověřen 2. listopadu 2026 na **276 JAR souborech**, **73 707 kandidátech textur**, z toho **27 139 blokových**, se **4 konflikty názvů**. Jde o jednu konkrétní sadu modů; počty nevyjadřují počet podporovaných bloků. Testy pokrývají i poškozené archivy, limity, chybějící metadata, konflikty, nebezpečné cesty a CLI. Herní prostředky ani tento produkční inventář nejsou součástí repozitáře.
