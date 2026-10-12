// Ground-plane panning through OrbitControls.pan(). No dependency on Three.js.
const DIRECTIONS = new Map([
  ['KeyW', [0, 1]], ['ArrowUp', [0, 1]], ['KeyS', [0, -1]], ['ArrowDown', [0, -1]],
  ['KeyA', [1, 0]], ['ArrowLeft', [1, 0]], ['KeyD', [-1, 0]], ['ArrowRight', [-1, 0]],
]);

export function createKeyboardPan(controls, eventTarget, document, enabled = () => true) {
  const held = new Set();
  const clear = () => held.clear();
  const editing = (target) => Boolean(target?.isContentEditable
    || target?.closest?.('input, textarea, select, [role="textbox"], [role="slider"]'));
  const down = (event) => {
    if (!enabled() || editing(event.target) || editing(document.activeElement) || document.hidden
      || event.ctrlKey || event.metaKey || event.altKey) {
      clear();
      return;
    }
    if (!DIRECTIONS.has(event.code)) return;
    event.preventDefault();
    const direction = DIRECTIONS.get(event.code);
    const alreadyHeld = [...held].some((code) => DIRECTIONS.get(code)[0] === direction[0]
      && DIRECTIONS.get(code)[1] === direction[1]);
    held.add(event.code);
    // A short tap may begin and end between animation frames. Give it one step;
    // auto-repeat and duplicate aliases must not add extra steps to held movement.
    if (!alreadyHeld) controls.pan(direction[0] * 4, direction[1] * 4);
  };
  const up = (event) => held.delete(event.code);
  eventTarget.addEventListener('keydown', down);
  eventTarget.addEventListener('keyup', up);
  eventTarget.addEventListener('blur', clear);
  document.addEventListener('visibilitychange', clear);
  document.addEventListener('focusin', clear);
  return {
    update(dt) {
      if (!enabled() || editing(document.activeElement) || document.hidden) { clear(); return; }
      if (!Number.isFinite(dt) || dt <= 0) return;
      let x = 0, y = 0;
      // W + Up must not double the speed. Opposing directions cancel.
      const directions = new Set([...held].map((code) => DIRECTIONS.get(code).join(',')));
      for (const direction of directions) {
        const [dx, dy] = direction.split(',').map(Number);
        x += dx; y += dy;
      }
      const length = Math.hypot(x, y);
      if (!length) return;
      const distance = 240 * Math.min(dt, 0.05) / length;
      controls.pan(x * distance, y * distance);
    },
    dispose() {
      clear();
      eventTarget.removeEventListener('keydown', down);
      eventTarget.removeEventListener('keyup', up);
      eventTarget.removeEventListener('blur', clear);
      document.removeEventListener('visibilitychange', clear);
      document.removeEventListener('focusin', clear);
    },
  };
}
