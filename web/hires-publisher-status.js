// Operational progress only. The displayed index carries its own publication metadata.
const labels = {
  preparing: 'Příprava nové verze mapy', verifying: 'Kontrola souborů nové verze',
  published: 'Verze připravena k zobrazení', 'producer-busy': 'Čeká na renderer',
  'waiting-for-producer': 'Čeká na dokončená data rendereru',
  'waiting-for-complete-plan': 'Čeká na úplný renderovací plán',
  'low-disk': 'Publikování pozastaveno: málo místa na disku',
  error: 'Publikování narazilo na chybu; další běh to zkusí znovu',
};
const count = x => Number.isSafeInteger(x) && x >= 0 && x <= 100000;
export function describePublisherStatus(data, now = Date.now()) {
  const unknown = {tone:'muted', text:'Stav přípravy nové verze není k dispozici.'};
  if (!data || typeof data !== 'object' || Array.isArray(data) || data.format !== 'UIE_HIRES_PUBLISHER_STATUS1' ||
      typeof data.state !== 'string' || !Object.hasOwn(labels, data.state) || typeof data.updatedAt !== 'number' ||
      !Number.isFinite(data.updatedAt) || data.updatedAt <= 0 || !Number.isFinite(now)) return unknown;
  const age = now - data.updatedAt * 1000;
  if (!Number.isFinite(age) || age < -120000 || age > 120000) {
    return {tone:'warn', text:'Stav přípravy nové verze není čerstvý; běh nyní nelze potvrdit.'};
  }
  let text = labels[data.state];
  if (data.state === 'preparing' && count(data.captured) && count(data.remaining) &&
      data.captured + data.remaining > 0 && data.captured + data.remaining <= 100000) {
    const total = data.captured + data.remaining;
    text += `: ${data.captured.toLocaleString('cs-CZ')} / ${total.toLocaleString('cs-CZ')} dlaždic (${Math.floor(100*data.captured/total)} %). Poté následuje kontrola souborů`;
  }
  if (data.state === 'verifying' && count(data.verificationCachedTiles)) {
    text += `: ${data.verificationCachedTiles.toLocaleString('cs-CZ')} ověřovacích záznamů v paměti. Nejde o počet nově vykreslených dlaždic`;
  }
  return {tone:['error','low-disk'].includes(data.state)?'warn':'muted', text:text+'.'};
}
