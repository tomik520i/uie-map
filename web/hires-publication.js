// Describes the displayed generation independently of the renderer's live status.
export function describeHiresPublication(index, formatTime) {
  if (!index?.immutable) return null;
  const source = index.source;
  if (!source) return {title:'UIE · mapa', text:'Zobrazená mapa: tato verze neposkytuje ověřený čas ani úplnost renderovacího plánu.'};
  const completion = source.planComplete ? 'renderovací plán dokončen' : 'renderovací plán neúplný';
  return {title:source.planComplete?'UIE · mapa světa':'UIE · částečná mapa',
    text:`Zobrazená mapa: ${formatTime(source.producerFinishedAt * 1000)} · ${completion} (${source.plannedJobs} regionů v plánu). `+
      `Starší dlaždice: ${source.staleTiles.length}; odložené regiony: ${source.notScheduled}; chyby regionů/dlaždic: ${source.failedJobs}/${source.failedTiles}. `+
      'Čas označuje dokončení zdrojového běhu. Běh rendereru níže může být novější.'};
}
