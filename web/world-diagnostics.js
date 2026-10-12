// Pure validation and Czech presentation of UIE_BLOCK_DIAGNOSTICS1. The caller owns fetching.
export const WORLD_DIAGNOSTICS_FORMAT = 'UIE_BLOCK_DIAGNOSTICS1';
const CLASSES = ['fallback', 'missingTexture', 'invisible', 'assumed'];
const FLAGS = new Set([...CLASSES, 'unknownId']);
const LABELS = {
  fallback: 'Náhradní model', missingTexture: 'Chybějící textura',
  invisible: 'Bez geometrie', assumed: 'Odhadnutá krychle',
};
const safeCount = value => Number.isSafeInteger(value) && value >= 0;
const hasCount = (obj, key) => safeCount(obj?.[key]);
const fail = message => { throw new TypeError(`Neplatná diagnostika: ${message}`); };
const countText = (value, lowerBound = false) => `${lowerBound ? '≥ ' : ''}${value.toLocaleString('cs-CZ')}`;

export function validateWorldDiagnostics(doc) {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc) || doc.format !== WORLD_DIAGNOSTICS_FORMAT
      || doc.version !== 1 || !['tile', 'job', 'world'].includes(doc.scope)) fail('formát');
  const coverage = doc.coverage;
  if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage)
      || !['tiles', 'expectedTiles', 'coreChunks', 'coreChunksExpected', 'voxels'].every(k => hasCount(coverage, k))
      || typeof coverage.visibleKnown !== 'boolean' || typeof coverage.complete !== 'boolean'
      || coverage.tiles > coverage.expectedTiles || coverage.coreChunks > coverage.coreChunksExpected) fail('pokrytí');
  if (doc.scope === 'world') {
    const jobs = doc.jobs;
    if (!jobs || !['planned', 'withDiagnostics', 'withoutDiagnostics', 'completeJobs'].every(k => hasCount(jobs, k))
        || jobs.withDiagnostics + jobs.withoutDiagnostics !== jobs.planned
        || jobs.completeJobs > jobs.withDiagnostics) fail('úlohy');
  }
  const classes = doc.classes;
  if (!classes || typeof classes !== 'object' || Array.isArray(classes)
      || Object.keys(classes).length !== CLASSES.length || !CLASSES.every(k => Object.hasOwn(classes, k))) fail('třídy');
  for (const key of CLASSES) {
    const item = classes[key];
    if (!hasCount(item, 'all') || !(item.visible === null || hasCount(item, 'visible'))
        || typeof item.lowerBound !== 'boolean') fail(`třída ${key}`);
  }
  if (!Array.isArray(doc.entries) || doc.entries.length > 500) fail('počet položek');
  for (const entry of doc.entries) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)
        || !Number.isInteger(entry.id) || entry.id < 0 || entry.id > 65535
        || !Number.isInteger(entry.meta) || entry.meta < 0 || entry.meta > 255
        || !(entry.name === null || (typeof entry.name === 'string' && entry.name.length > 0 && entry.name.length <= 200))
        || !CLASSES.includes(entry.class)
        || !Array.isArray(entry.flags) || entry.flags.length > FLAGS.size
        || new Set(entry.flags).size !== entry.flags.length || !entry.flags.every(flag => FLAGS.has(flag))
        || !entry.flags.includes(entry.class)
        || typeof entry.reason !== 'string' || entry.reason.length > 300
        || !hasCount(entry, 'all') || !(entry.visible === null || hasCount(entry, 'visible'))
        || typeof entry.lowerBound !== 'boolean'
        || !Array.isArray(entry.sample) || entry.sample.length !== 3
        || !entry.sample.every(Number.isInteger)
        || Math.abs(entry.sample[0]) > 30_000_000 || entry.sample[1] < 0 || entry.sample[1] > 255
        || Math.abs(entry.sample[2]) > 30_000_000) fail('položka');
  }
  const truncated = doc.truncated;
  if (!truncated || !['entries', 'all', 'inputParts', 'inputEntries', 'inputAll'].every(k => hasCount(truncated, k)))
    fail('zkrácení');
  return doc;
}

export function describeWorldDiagnostics(input) {
  const doc = validateWorldDiagnostics(input);
  const { coverage, jobs, truncated } = doc;
  const measured = coverage.tiles > 0 || coverage.coreChunks > 0;
  const unknown = !coverage.complete || (doc.scope === 'world' && jobs.withoutDiagnostics > 0);
  const coverageText = doc.scope === 'world'
    ? `Regiony s diagnostikou: ${jobs.withDiagnostics} z ${jobs.planned}; bez diagnostiky: ${jobs.withoutDiagnostics}. Úplně zkontrolované regiony: ${jobs.completeJobs}. Dlaždice v těchto záznamech: ${coverage.tiles} z ${coverage.expectedTiles}.`
    : `Dlaždice: ${coverage.tiles} z ${coverage.expectedTiles}; načtené oblasti: ${coverage.coreChunks} z ${coverage.coreChunksExpected}.`;
  const explanation = 'Počty zahrnují všechny zjištěné bloky v měřených oblastech včetně podzemí. Viditelné počty jsou známé jen pro náhradní modely. Dodatečná kontrola dat může být novější než zobrazená geometrie; přesnou shodu se starým renderem nelze doložit.';
  const emptyText = !doc.entries.length
    ? (unknown || !measured ? 'Stav je neznámý: některé oblasti ještě nemají diagnostiku.'
      : 'V měřených oblastech nebyl zjištěn žádný problémový blok.')
    : null;
  const classRows = CLASSES.map(key => {
    const value = doc.classes[key];
    return { key, label: LABELS[key], all: countText(value.all, value.lowerBound),
      visible: value.visible === null ? 'neznámé' : countText(value.visible, value.lowerBound) };
  });
  const entries = doc.entries.map(entry => ({
    name: entry.name ?? 'Neznámý název', id: entry.id, meta: entry.meta,
    classKey: entry.class, classLabel: LABELS[entry.class], flags: [...entry.flags], reason: entry.reason,
    all: countText(entry.all, entry.lowerBound),
    visible: entry.visible === null ? 'neznámé' : countText(entry.visible, entry.lowerBound),
    sample: [...entry.sample], sampleText: `${entry.sample[0]}, ${entry.sample[1]}, ${entry.sample[2]}`,
  }));
  const omitted = truncated.entries + truncated.inputEntries;
  return { title: 'Diagnostika bloků', coverageText, explanation, emptyText,
    unknownText: unknown ? 'Část světa je neznámá; nulové počty platí pouze pro změřené oblasti. Po změně světa může počet zkontrolovaných regionů klesnout: zastaralá data čekají na novou kontrolu.' : null,
    truncatedText: omitted ? `Seznam je zkrácen; v této souhrnné tabulce a jejích vstupech bylo vynecháno ${countText(omitted)} záznamů. Stejný typ bloku se v nich může opakovat.` : null,
    classRows, entries };
}

// Filters only the already bounded, possibly truncated entry list. Class totals and coverage are untouched.
export function filterWorldDiagnosticsEntries(entries, { classKey = 'all', query = '' } = {}) {
  if (!Array.isArray(entries) || entries.length > 500 || (classKey !== 'all' && !CLASSES.includes(classKey))) {
    throw new TypeError('Neplatný filtr diagnostiky');
  }
  const needle = String(query).trim().toLocaleLowerCase('cs-CZ').slice(0, 120);
  return entries.filter(entry => {
    if (classKey !== 'all' && entry.classKey !== classKey) return false;
    if (!needle) return true;
    return [entry.name, String(entry.id), String(entry.meta), `${entry.id}:${entry.meta}`]
      .some(value => value.toLocaleLowerCase('cs-CZ').includes(needle));
  });
}

// Optional DOM helper. Uses only the supplied container; all untrusted strings are inserted as text.
export function renderWorldDiagnostics(container, doc, onLocate = () => {}) {
  const model = describeWorldDiagnostics(doc);
  const owner = container.ownerDocument;
  container.replaceChildren();
  container.style.overflowWrap = 'anywhere';
  const add = (parent, tag, value) => {
    const node = owner.createElement(tag);
    node.textContent = value;
    parent.append(node);
    return node;
  };
  add(container, 'h3', model.title);
  add(container, 'p', model.coverageText);
  add(container, 'p', model.explanation);
  if (model.unknownText) add(container, 'p', model.unknownText);
  if (model.emptyText) add(container, 'p', model.emptyText);
  const summary = add(container, 'div', '');
  for (const row of model.classRows) add(summary, 'p', `${row.label}: všechny ${row.all}; viditelné ${row.visible}`);
  if (model.truncatedText) add(container, 'p', model.truncatedText);
  add(container, 'p', 'Filtr mění jen zobrazený, případně zkrácený seznam. Souhrnné počty výše platí pro celý změřený rozsah.');
  const controls = owner.createElement('div');
  const classLabel = add(controls, 'label', 'Třída ');
  const classSelect = owner.createElement('select');
  for (const [key, label] of [['all', 'Všechny třídy'], ...CLASSES.map(key => [key, LABELS[key]])]) {
    const option = owner.createElement('option');
    option.value = key;
    option.textContent = label;
    classSelect.append(option);
  }
  classLabel.append(classSelect);
  const searchLabel = add(controls, 'label', ' Hledat název / ID / meta ');
  const search = owner.createElement('input');
  search.type = 'search';
  search.maxLength = 120;
  search.style.maxWidth = '100%';
  searchLabel.append(search);
  container.append(controls);
  const filterResult = add(container, 'p', '');
  const list = owner.createElement('div');
  list.style.maxHeight = '350px';
  list.style.overflowY = 'auto';
  container.append(list);
  function showFiltered() {
    const shown = filterWorldDiagnosticsEntries(model.entries, { classKey: classSelect.value, query: search.value });
    list.replaceChildren();
    filterResult.textContent = `${shown.length} z ${model.entries.length} záznamů v načteném seznamu.`
      + (shown.length === 0 && model.entries.length > 0 ? ' Filtru neodpovídá žádná položka v načteném seznamu.' : '');
    for (const entry of shown) {
      const item = owner.createElement('details');
      add(item, 'summary', `${entry.name} (ID ${entry.id}, meta ${entry.meta}) · ${entry.classLabel} · ${entry.all}`);
      add(item, 'p', `Všechny: ${entry.all}; viditelné: ${entry.visible}. ${entry.reason}`);
      add(item, 'p', `Příklad: ${entry.sampleText}`);
      const button = add(item, 'button', 'Najít na mapě');
      button.type = 'button';
      button.addEventListener('click', () => onLocate(...entry.sample));
      list.append(item);
    }
  }
  classSelect.addEventListener('change', showFiltered);
  search.addEventListener('input', showFiltered);
  showFiltered();
  return model;
}
