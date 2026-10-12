// First-person FLY view mode (no collisions, no gravity) switched against OrbitControls ("Mapa").
// No Three.js import: works on the passed camera/controls objects; movement comes from first-person-motion.js.
//
// createFirstPersonView({ camera, controls, canvas, window, document, motion, onChange, onModeChange, onMessage })
//   .mode                'orbit' | 'fly'        .locked   pointer currently locked to the canvas
//   .yaw, .pitch         radians (Euler 'YXZ', no roll)
//   .enterFly()          saves the orbit pose, freezes OrbitControls damping and disables OrbitControls.
//                        The camera does not move; its view direction is kept (roll removed).
//   .requestLock()       call ONLY from a user gesture (click). Unsupported or rejected pointer lock only shows a
//                        message; Map stays available. A lock that arrives after leaving Fly is released.
//   .step(dt) -> bool    moves the camera while locked, visible and focused; true when it moved.
//   .exitToMap()         releases the lock, restores the saved orbit pose (position, target, orientation, near)
//                        with damping flushed, re-enables OrbitControls and calls controls.update() once.
//   .dispose()
//   .enableDragLook() / .dragMode: explicit fallback without pointer lock. Canvas focus is required;
//   hold left mouse and drag to look. Escape, blur, hiding or focusing another control pauses it.
//   .walking / .setWalking(bool): with an optional `walker` (walk-physics.js) the camera walks with gravity and
//   collisions instead of flying; key G toggles it. The eye is `eyeHeight` above the walker's feet. An optional
//   walkStart(feet) -> feet | null picks where the body starts (e.g. on the terrain when the camera is high above).
// Callbacks: onChange(kind) with kind 'look' | 'move' | 'lock' | 'mode' | 'walk'; onModeChange(mode);
// onMessage(text | null, tone). The owner must not call controls.update() while mode === 'fly'.
// Keys are ignored and cleared while unlocked, hidden, on blur, with focus in a form field or with
// Ctrl/Meta/Alt held. Escape is handled by the browser (it ends pointer lock); nothing traps it.

export const FLY_NEAR = 0.05;
export const PITCH_LIMIT = Math.PI / 2 - 0.01;
export const LOOK_RADIANS_PER_PIXEL = 0.0025;

const editing = (target) => Boolean(target?.isContentEditable
  || target?.closest?.('input, textarea, select, [role="textbox"], [role="slider"]'));
const clampPitch = (value) => Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, value));
const wrapAngle = (value) => value - 2 * Math.PI * Math.floor((value + Math.PI) / (2 * Math.PI));

/** Cancel residual OrbitControls damping without moving the camera (three.js r186 instance fields). */
export function flushOrbitDamping(controls) {
  controls._sphericalDelta?.set(0, 0, 0);
  controls._panOffset?.set(0, 0, 0);
  if ('_scale' in controls) controls._scale = 1;
  if ('_performCursorZoom' in controls) controls._performCursorZoom = false;
}

export function createFirstPersonView({ camera, controls, canvas, window: win, document: doc, motion,
  onChange = () => {}, onModeChange = () => {}, onMessage = () => {}, near = FLY_NEAR,
  sensitivity = LOOK_RADIANS_PER_PIXEL, walker = null, eyeHeight = 1.62, walkStart = null } = {}) {
  let mode = 'orbit';
  let walking = false;    // walk with collisions (walker) instead of free flight; only inside 'fly' mode
  let saved = null;
  let yaw = 0;
  let pitch = 0;
  let lockSeq = 0;
  let wantLock = false;   // true only between an explicit requestLock() and unlock/blur/hide/exit
  let pending = false;    // a requestPointerLock() call has not produced a lock change yet
  let disposed = false;
  let dragMode = false;
  let drag = null;
  let pauseShown = false;
  const endDrag = () => {
    const id = drag?.id;
    drag = null;
    if (id !== undefined && canvas.hasPointerCapture?.(id)) canvas.releasePointerCapture(id);
  };

  const locked = () => doc.pointerLockElement === canvas;
  const focused = () => typeof doc.hasFocus !== 'function' || doc.hasFocus();
  // Single guard for movement, keys and mouse look.
  const canControl = () => !disposed && mode === 'fly' && (locked() || (dragMode && doc.activeElement === canvas)) && !doc.hidden && focused()
    && !editing(doc.activeElement);
  const applyRotation = () => {
    camera.rotation.set(pitch, yaw, 0, 'YXZ');
    camera.updateMatrixWorld();
  };
  const releaseLock = () => {
    if (locked() && typeof doc.exitPointerLock === 'function') doc.exitPointerLock();
  };
  const denied = (detail) => {
    pauseShown = false;
    onMessage(`Prohlížeč zamčení myši odmítl${detail ? ` (${detail})` : ''}. `
      + 'Zkuste tažení myší, nebo se vraťte na Mapu.', 'warn');
  };
  const pausedMessage = () => `${walking ? 'Chůze je pozastavená' : 'Létání je pozastavené'}, myš je uvolněná.`;
  const dragMessage = () => walking
    ? 'Rozhlížení: držte levé tlačítko a táhněte. WASD chodí, mezerník skáče, Esc pozastaví.'
    : 'Rozhlížení: držte levé tlačítko a táhněte. WASD létá, Esc pozastaví.';

  function enterFly() {
    if (disposed || mode === 'fly') return;
    flushOrbitDamping(controls);
    controls.enabled = false;
    saved = { position: camera.position.clone(), target: controls.target.clone(),
      quaternion: camera.quaternion.clone(), near: camera.near };
    const euler = camera.rotation.clone();
    euler.setFromQuaternion(camera.quaternion, 'YXZ');
    yaw = euler.y;
    pitch = clampPitch(euler.x);
    applyRotation();
    camera.near = near;
    camera.updateProjectionMatrix();
    mode = 'fly';
    pauseShown = false;
    motion.clear();
    onModeChange('fly');
    onMessage(null);
    onChange('mode');
  }

  function requestLock() {
    if (disposed || mode !== 'fly' || locked()) return;
    if (typeof canvas.requestPointerLock !== 'function') {
      pauseShown = false;
      onMessage('Tento prohlížeč nepodporuje zamčení myši. Použijte tažení myší nebo Mapu.', 'warn');
      return;
    }
    const seq = ++lockSeq;
    wantLock = true;
    pending = true;
    let result;
    try {
      result = canvas.requestPointerLock();
    } catch (err) {
      wantLock = pending = false;
      denied(err?.message);
      return;
    }
    if (result && typeof result.then === 'function') {
      result.then(() => {
        if (seq !== lockSeq || mode !== 'fly' || !wantLock || disposed) releaseLock();
      }, (err) => {
        if (seq !== lockSeq) return;
        wantLock = pending = false;
        if (mode === 'fly' && !disposed) denied(err?.message);
      });
    }
  }

  /** Blur or hidden tab: stop, forget pending requests and give the mouse back. */
  function suspend() {
    motion.clear();
    dragMode = false;
    endDrag();
    lockSeq += 1;
    wantLock = false;
    releaseLock();
    if (mode === 'fly') {
      pauseShown = true;
      onMessage(pausedMessage(), 'info');
      onChange('lock');
    }
  }

  function enableDragLook() {
    if (disposed || mode !== 'fly') return;
    suspend();
    dragMode = true;
    pauseShown = false;
    canvas.focus?.({ preventScroll: true });
    onMessage(dragMessage(), 'info');
    onChange('lock');
  }

  function step(dt) {
    if (mode !== 'fly') return false;
    if (!canControl()) {
      motion.clear();
      return false;
    }
    if (walking) {
      const moved = walker.step(Number.isFinite(dt) ? dt : 0, { ...motion.input(), yaw });
      if (!moved) return false;
      const p = walker.position;
      camera.position.set(p.x, p.y + eyeHeight, p.z);
      camera.updateMatrixWorld();
      onChange('move');
      return true;
    }
    const d = motion.step(Number.isFinite(dt) ? dt : 0, yaw, pitch);
    if (!d.x && !d.y && !d.z) return false;
    camera.position.x += d.x;
    camera.position.y += d.y;
    camera.position.z += d.z;
    camera.updateMatrixWorld();
    onChange('move');
    return true;
  }

  /** Switch between free flight and walking with collisions (needs a walker). The camera stays where it is. */
  function setWalking(on) {
    const next = Boolean(on) && walker !== null && mode === 'fly';
    if (next === walking) return;
    walking = next;
    motion.clear();
    if (walking) {
      const eye = { x: camera.position.x, y: camera.position.y - eyeHeight, z: camera.position.z };
      walker.place(walkStart?.(eye) ?? eye);
      const p = walker.position;
      camera.position.set(p.x, p.y + eyeHeight, p.z);
      camera.updateMatrixWorld();
    }
    if (dragMode) onMessage(dragMessage(), 'info');
    else if (pauseShown) onMessage(pausedMessage(), 'info');
    onChange('walk');
  }

  function exitToMap() {
    if (disposed || mode !== 'fly') return;
    walking = false;
    mode = 'orbit';
    pauseShown = false;
    dragMode = false;
    endDrag();
    lockSeq += 1;  // any pending lock request is now stale
    wantLock = false;
    motion.clear();
    releaseLock();
    camera.position.copy(saved.position);
    controls.target.copy(saved.target);
    camera.quaternion.copy(saved.quaternion);
    camera.near = saved.near;
    camera.updateProjectionMatrix();
    saved = null;
    flushOrbitDamping(controls);
    controls.enabled = true;
    controls.update();
    onMessage(null);
    onModeChange('orbit');
    onChange('mode');
  }

  const onLockChange = () => {
    pending = false;
    if (locked()) {
      // A lock nobody asked for any more (late grant after exit, blur or a hidden tab) is released at once.
      if (mode !== 'fly' || !wantLock || doc.hidden || !focused()) { releaseLock(); return; }
      dragMode = false;
      endDrag();
      pauseShown = false;
      onMessage(null);
    } else {
      wantLock = false;
      motion.clear();
      if (mode === 'fly') {
        pauseShown = true;
        onMessage(pausedMessage(), 'info');
      }
    }
    onChange('lock');
  };
  const onLockError = () => {
    pending = false;
    wantLock = false;
    if (mode === 'fly') denied(null);
  };
  const onMouseMove = (event) => {
    if (!canControl()) return;
    const dx = event.movementX;
    const dy = event.movementY;
    if (!Number.isFinite(dx) || !Number.isFinite(dy) || (!dx && !dy)) return;
    yaw = wrapAngle(yaw - dx * sensitivity);
    pitch = clampPitch(pitch - dy * sensitivity);
    applyRotation();
    onChange('look');
  };
  const onKeyDown = (event) => {
    if (mode !== 'fly') return;
    if (event.code === 'Escape' && dragMode) { suspend(); return; }
    if (!canControl() || editing(event.target) || event.ctrlKey || event.metaKey || event.altKey) {
      motion.clear();
      return;
    }
    if (event.code === 'KeyG' && walker && !event.repeat) { setWalking(!walking); event.preventDefault(); return; }
    if (motion.handleKey(event.code, true)) event.preventDefault();
  };
  const onKeyUp = (event) => { if (mode === 'fly') motion.handleKey(event.code, false); };
  const clearKeys = () => {
    motion.clear();
    if (dragMode && doc.activeElement !== canvas) suspend();
  };
  const onVisibility = () => { if (doc.hidden) suspend(); else motion.clear(); };
  const onLockedMouseMove = (event) => { if (locked()) onMouseMove(event); };
  const onDragStart = (event) => {
    if (!dragMode || event.button !== 0 || event.isPrimary === false) return;
    canvas.focus?.({ preventScroll: true });
    if (!canControl()) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY };
    canvas.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  };
  const onDragMove = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    onMouseMove({ movementX: event.clientX - drag.x, movementY: event.clientY - drag.y });
    drag.x = event.clientX; drag.y = event.clientY;
  };
  const onDragEnd = (event) => { if (drag?.id === event.pointerId) drag = null; };

  doc.addEventListener('pointerlockchange', onLockChange);
  doc.addEventListener('pointerlockerror', onLockError);
  doc.addEventListener('mousemove', onLockedMouseMove);
  canvas.addEventListener('pointerdown', onDragStart);
  canvas.addEventListener('pointermove', onDragMove);
  canvas.addEventListener('pointerup', onDragEnd);
  canvas.addEventListener('pointercancel', onDragEnd);
  canvas.addEventListener('lostpointercapture', onDragEnd);
  doc.addEventListener('visibilitychange', onVisibility);
  doc.addEventListener('focusin', clearKeys);
  win.addEventListener('keydown', onKeyDown);
  win.addEventListener('keyup', onKeyUp);
  win.addEventListener('blur', suspend);

  return {
    get mode() { return mode; },
    get locked() { return locked(); },
    get dragMode() { return dragMode; },
    get walking() { return walking; },
    setWalking,
    get yaw() { return yaw; },
    get pitch() { return pitch; },
    enterFly,
    requestLock,
    enableDragLook,
    step,
    exitToMap,
    dispose() {
      if (disposed) return;
      if (mode === 'fly') exitToMap();
      disposed = true;
      wantLock = false;
      releaseLock();
      doc.removeEventListener('pointerlockchange', onLockChange);
      doc.removeEventListener('pointerlockerror', onLockError);
      doc.removeEventListener('mousemove', onLockedMouseMove);
      canvas.removeEventListener('pointerdown', onDragStart);
      canvas.removeEventListener('pointermove', onDragMove);
      canvas.removeEventListener('pointerup', onDragEnd);
      canvas.removeEventListener('pointercancel', onDragEnd);
      canvas.removeEventListener('lostpointercapture', onDragEnd);
      doc.removeEventListener('visibilitychange', onVisibility);
      doc.removeEventListener('focusin', clearKeys);
      win.removeEventListener('keydown', onKeyDown);
      win.removeEventListener('keyup', onKeyUp);
      win.removeEventListener('blur', suspend);
      // A request still in flight (also without a promise) must not leave the mouse locked afterwards.
      if (pending) doc.addEventListener('pointerlockchange', releaseLock, { once: true });
    },
  };
}
