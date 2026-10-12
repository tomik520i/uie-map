// A one-shot height correction for an explicit goto whose terrain was not loaded yet.
// No DOM or camera dependency: the caller applies delta equally to target and camera.
export function createGotoTerrain({ now = () => Date.now(), maxWaitMs = 30_000 } = {}) {
  if (typeof now !== 'function' || !Number.isFinite(maxWaitMs) || maxWaitMs <= 0) {
    throw new TypeError('invalid goto terrain clock or wait limit');
  }
  let pending = null;
  return {
    start({ key, x, z, targetY }) {
      if ((key !== null && typeof key !== 'string') || ![x, z, targetY].every(Number.isFinite)) {
        throw new TypeError('invalid pending goto');
      }
      pending = { key, x, z, targetY, expiresAt: now() + maxWaitMs };
    },
    cancel() { pending = null; },
    get pending() { return pending !== null; },
    resolve({ key, height, targetX, targetZ, targetY, orbit = true }) {
      const intent = pending;
      if (!intent || (intent.key !== null && key !== intent.key) || !Number.isFinite(height)) return null;
      pending = null;
      if (!orbit || now() > intent.expiresAt || ![targetX, targetZ, targetY].every(Number.isFinite)
          || Math.abs(targetX - intent.x) > 1e-3 || Math.abs(targetZ - intent.z) > 1e-3
          || Math.abs(targetY - intent.targetY) > 1e-3) return null;
      return height - targetY;
    },
  };
}
