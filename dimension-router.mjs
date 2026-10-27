import { readFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { validateMapView } from './web/map-view.mjs';

async function jsonFile(file, limit) {
  const info = await stat(file);
  if (!info.isFile() || info.size > limit) throw new Error('Invalid dimension configuration size');
  const bytes = await readFile(file);
  if (bytes.length > limit) throw new Error('Invalid dimension configuration size');
  return JSON.parse(bytes.toString('utf8'));
}

export async function createDimensionRouter(file, mainHandler, createHandler) {
  if (!file) return mainHandler;
  const config = await jsonFile(file, 65536);
  if (config?.format !== 'UIE_DIMENSION_VIEWS1' || !Array.isArray(config.views)
      || config.views.length > 32 || typeof config.mainTitle !== 'string'
      || !config.mainTitle.trim() || config.mainTitle.length > 200) throw new Error('Invalid dimension configuration');
  const children = new Map();
  for (const item of config.views) {
    if (!item || typeof item.key !== 'string' || !/^[a-z0-9][a-z0-9-]{0,47}$/.test(item.key)
        || item.key === 'main' || children.has(item.key)
        || typeof item.viewFile !== 'string' || !path.isAbsolute(item.viewFile)
        || typeof item.hiresStore !== 'string' || !path.isAbsolute(item.hiresStore)) throw new Error('Invalid dimension route');
    const view = validateMapView(await jsonFile(item.viewFile, 4096));
    // No inheritance of generation, players, caves or collision stores from the parent.
    const env = { UIE_MAP_VIEW_FILE: item.viewFile, UIE_HIRES_STORE: item.hiresStore };
    if (view.caves === true) {
      if (![item.caveGeometryStore,item.cavePairStore].every(v=>typeof v==='string' && path.isAbsolute(v)))
        throw new Error('Cave view requires both explicitly selected stores');
      env.UIE_CAVE_GEOMETRY_STORE=item.caveGeometryStore;
      env.UIE_CAVE_PAIR_STORE=item.cavePairStore;
    } else if (item.caveGeometryStore !== undefined || item.cavePairStore !== undefined) {
      throw new Error('Surface-only view cannot attach cave stores');
    }
    const handler = await createHandler(env);
    children.set(item.key, { handler, title: view.title, world: view.world, dimension: view.dimension });
  }
  return async (req, res) => {
    let url;
    try { url = new URL(req.url, 'http://localhost'); }
    catch { res.writeHead(400); res.end(); return; }
    const match = /^\/dimensions\/([a-z0-9-]+)(\/.*)?$/.exec(url.pathname);
    const selected = match ? children.get(match[1]) : null;
    if (url.pathname.startsWith('/dimensions/') && !selected) {
      res.writeHead(404, { 'Cache-Control': 'no-store' }); res.end('Unknown dimension'); return;
    }
    if (selected && !match[2]) {
      // Relative Location also works behind a reverse proxy mounted under /map/uie/.
      res.writeHead(308, { Location: `${match[1]}/${url.search}`, 'Cache-Control': 'no-store' }); res.end(); return;
    }
    const localPath = selected ? match[2] : url.pathname;
    if (localPath === '/data/dimensions.json') {
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        res.writeHead(405, { Allow: 'GET, HEAD' }); res.end(); return;
      }
      const entries = [{ key: 'main', title: config.mainTitle, href: selected ? '../../' : './' },
        ...[...children].map(([key, value]) => ({ key, title: value.title,
          href: selected ? `../${key}/` : `./dimensions/${key}/` }))];
      const body = JSON.stringify({ format: 'UIE_DIMENSION_MENU1', active: selected ? match[1] : 'main', entries });
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
        'Content-Length': Buffer.byteLength(body), 'X-Content-Type-Options': 'nosniff' });
      res.end(req.method === 'HEAD' ? undefined : body); return;
    }
    if (selected) req.url = localPath + url.search;
    return (selected?.handler ?? mainHandler)(req, res);
  };
}
