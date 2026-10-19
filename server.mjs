// Executable entry: PM2 may load this module through its own wrapper.
import http from 'node:http';
import { createMapHandler } from './map-handler.mjs';
import { createDimensionRouter } from './dimension-router.mjs';

const port = Number(process.env.PORT ?? 8211);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid PORT');
const mainHandler = await createMapHandler(process.env);
const handler = await createDimensionRouter(process.env.UIE_DIMENSIONS_FILE, mainHandler, createMapHandler);
const server = http.createServer(handler);
server.requestTimeout = 30000;
server.headersTimeout = 15000;
server.listen(port, process.env.HOST ?? '0.0.0.0', () => console.log(`UIE map preview listening on ${port}`));

// Extra public port(s), all interfaces: the map takes over dynmap's 8123 (dynmap moved to 8124 in its
// configuration.txt, effective at the next game start). While the port is still taken it retries every minute.
// Default only for the LIVE instance (port 8211); probes and tests never grab it. UIE_EXTRA_PORTS="" disables it.
const extraPorts = (process.env.UIE_EXTRA_PORTS ?? (port === 8211 ? '8123' : ''))
  .split(',').map((v) => Number(v.trim())).filter((v) => Number.isInteger(v) && v > 0 && v < 65536 && v !== port);
const extraServers = extraPorts.map((extra) => {
  const handler = server.listeners('request')[0];
  const s = http.createServer(handler);
  s.requestTimeout = server.requestTimeout;
  s.headersTimeout = server.headersTimeout;
  const attempt = () => s.listen(extra, '0.0.0.0');
  s.on('listening', () => console.log(`UIE map preview also listening on ${extra}`));
  s.on('error', (err) => {
    if (err.code !== 'EADDRINUSE' && err.code !== 'EACCES') throw err;
    setTimeout(attempt, 60_000).unref();
  });
  attempt();
  return s;
});
for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => {
  for (const s of extraServers) s.close();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 5000).unref();
});
