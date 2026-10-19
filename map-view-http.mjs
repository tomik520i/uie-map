import { open } from 'node:fs/promises';
import { validateMapView } from './web/map-view.mjs';

export function createMapViewHandler(file) {
  return async (req, res, pathname) => {
    if (pathname !== '/data/map-view.json') return false;
    const reply = (status, text) => {
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Length': Buffer.byteLength(text) });
      res.end(req.method === 'HEAD' ? undefined : text);
      return true;
    };
    if (!file) return reply(404, '{"error":"view-not-configured"}');
    let handle;
    try {
      handle = await open(file, 'r');
      const info = await handle.stat();
      if (!info.isFile() || info.size > 4096) throw new Error('Invalid view file');
      const buffer = Buffer.alloc(4097);
      let length = 0;
      while (length < buffer.length) {
        const { bytesRead } = await handle.read(buffer, length, buffer.length - length, null);
        if (!bytesRead) break;
        length += bytesRead;
      }
      if (length > 4096) throw new Error('View file too large');
      const view = validateMapView(JSON.parse(buffer.subarray(0, length).toString('utf8')));
      return reply(200, JSON.stringify(view));
    } catch {
      return reply(503, '{"error":"view-unavailable"}');
    } finally { await handle?.close(); }
  };
}
