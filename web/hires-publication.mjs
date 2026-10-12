// Description of the immutable generation actually displayed by the client.
// The mutable producer status is a separate, potentially newer run.
export function describeHiresPublication(index) {
  if (!index?.immutable) {
    return { kind: 'waiting', label: 'Čekám na neměnnou generaci', tone: 'muted',
      finishedAtMs: null, note: 'Údaj se zobrazí po načtení neměnného indexu.' };
  }
  const source = index.source;
  if (!source) {
    return { kind: 'unknown', label: 'Rozsah zobrazené generace není ověřen', tone: 'warn',
      finishedAtMs: null, note: 'Starší neměnný index neobsahuje stav dokončení plánu ani čas svého renderu.' };
  }
  const stale = index.stale instanceof Set ? index.stale.size : source.staleTiles.length;
  if (source.planComplete) {
    return { kind: 'complete', label: 'Zobrazená generace má dokončený plán', tone: 'ok',
      finishedAtMs: source.producerFinishedAt * 1000,
      note: `Při renderu mělo všech ${source.plannedJobs} plánovaných regionů aktuální bezchybná data.` };
  }
  const details = [];
  if (source.notScheduled) details.push(`${source.notScheduled} změněných regionů nebylo naplánováno`);
  if (source.failedJobs) details.push(`${source.failedJobs} regionů mělo chyby (${source.failedTiles} dlaždic)`);
  if (stale) details.push(`${stale} dlaždic používá starší dobrou verzi`);
  return { kind: 'partial', label: 'Zobrazená generace má neúplný plán', tone: 'warn',
    finishedAtMs: source.producerFinishedAt * 1000,
    note: details.length ? `${details.join('; ')}. Rozsah celé mapy není potvrzen.`
      : 'Producent nepotvrdil úplnost plánu; rozsah celé mapy není potvrzen.' };
}

export function mayClaimWholeWorld(index) {
  return Boolean(index && (!index.immutable || index.source?.planComplete === true));
}
