# Procházení mapy

## Uložené oblasti

V panelu Navigace je nabídka **Uložené oblasti** a tlačítko **Zobrazit oblast**. Oblasti vznikají spojením sousedících publikovaných dlaždic. Umožňují najít i vzdálené ostrovy terénu, aniž by uživatel musel znát jejich souřadnice. Nabídka obsahuje nejvýše 200 největších oblastí; případné omezení je uvedeno vedle celkového počtu.

Souhrn uvádí všechny dlaždice publikace. Horní ukazatel „Okolí načteno“ naproti tomu počítá jen dlaždice kolem kamery. Jedna dlaždice může obsahovat neuložené chunky; spojení dlaždic proto neznamená souvislou krajinu bez děr.

Samostatná dimenze začíná v oblasti poblíž svého nakonfigurovaného středu. Po načtení povrchu se kamera zaměří na skutečný terén; u jediné malé dlaždice přiblížení odpovídá nalezenému povrchu. Ruční pohyb zruší čekající automatické zaměření. V jeskynním řezu se kamera automaticky nepřesouvá nad povrch.

## Výběr bloku

Krátké kliknutí v režimu Mapa vybere souřadnice a texturu plochy. U povrchu se následně načtou bloková data pro tuto oblast. Název bloku, číselné ID a metadata se ukážou pouze při shodě jejich verze s vykreslenou geometrií a atlasem. Chybějící či neodpovídající data zůstanou označená jako výběr samotné textury. Další kliknutí nebo zavření panelu zruší předchozí požadavek.

Výběr je dostupný také v samostatných dimenzích. Přesná bloková identita v jeskynním řezu zatím není připojená. Průhledné pixely a tvary přesahující svou buňku mohou způsobit výběr sousední buňky; údaj není důkaz přesnosti modovaného modelu.

## Aktuální terén

Novější ověřená světová publikace, která pokrývá všechny dlaždice starého detailu spawnu, vyřadí starý detail i jeho bloková data pro chůzi. Průběžná aktualizace jiných regionů nevrací říjnový detail na obrazovku. Pokud nová publikace dosud neobsahuje potřebné dlaždice nebo ji nelze ověřit, samotné stáří názvu souboru nestačí k přepnutí.

Poloha hráčů je poslední uložená poloha, nikoli živý pohyb. Podpora modovaných modelů a kolizí zůstává neúplná; diagnostika ukazuje konkrétní náhradní modely a jejich výskyty.

### Výběr při aktualizaci a v řezu

Změna řezu zavře předchozí detail. Pokud se během načítání změní vybraná geometrie, mapa požádá o nový výběr. Kliknutí na šedý uzávěr označí pomocnou plochu řezu; pro výběr stěny nebo podlahy uzávěry vypněte. Podzemní plochy zatím poskytují texturu a souřadnice, nikoli přesné ID a metadata. Dohledání povrchového bloku načítá pouze jednu blokovou dlaždici a nemění datové okolí pro chůzi.
