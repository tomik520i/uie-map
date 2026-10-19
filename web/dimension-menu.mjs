export function validateDimensionMenu(doc) {
  if (doc?.format !== 'UIE_DIMENSION_MENU1' || typeof doc.active !== 'string'
      || !Array.isArray(doc.entries) || doc.entries.length < 1 || doc.entries.length > 33) throw new Error('Invalid dimension menu');
  const seen = new Set();
  for (const item of doc.entries) {
    if (!item || typeof item.key !== 'string' || !/^[a-z0-9][a-z0-9-]{0,47}$/.test(item.key)
        || seen.has(item.key) || typeof item.title !== 'string' || !item.title.trim() || item.title.length > 200
        || typeof item.href !== 'string') throw new Error('Invalid dimension entry');
    const expected = item.key === 'main' ? (doc.active === 'main' ? './' : '../../')
      : doc.active === 'main' ? `./dimensions/${item.key}/` : `../${item.key}/`;
    if (item.href !== expected) throw new Error('Invalid dimension destination');
    seen.add(item.key);
  }
  if (!seen.has('main') || !seen.has(doc.active)) throw new Error('Missing active dimension');
  return doc;
}

export async function mountDimensionMenu({ fetch, document, location, signal, url = './data/dimensions.json' }) {
  const response = await fetch(url, { cache: 'no-store', signal });
  if (response.status === 404) return;
  if (!response.ok) throw new Error('Dimension menu unavailable');
  const text = await response.text();
  if (text.length > 32768) throw new Error('Dimension menu too large');
  const menu = validateDimensionMenu(JSON.parse(text));
  const label = document.createElement('label'); label.textContent = 'Dimenze';
  label.className = 'note'; label.htmlFor = 'dimension-select';
  const select = document.createElement('select'); select.id = 'dimension-select';
  select.setAttribute('aria-label', 'Dimenze');
  select.style.cssText = 'display:block;width:100%;margin:8px 0;';
  for (const entry of menu.entries) {
    const option = document.createElement('option'); option.value = entry.href;
    option.textContent = entry.title; option.selected = entry.key === menu.active; select.append(option);
  }
  select.addEventListener('change', () => location.assign(select.value));
  const parent = document.querySelector('.panel-head'); parent.append(label, select);
}
