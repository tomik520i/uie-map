// Shared V2 context validation for the immutable HTTP endpoint and browser client.
export function validHiresContext(value) {
  const keys=['world','dimension','renderer','caves','layoutSha256'];
  if(!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).length!==keys.length
      || !keys.every(k=>Object.hasOwn(value,k)))return false;
  for(const key of ['world','dimension','renderer']) {
    const text=value[key];
    if(typeof text!=='string' || [...text].length<1 || [...text].length>128 || text.trim()!==text
        || /[^\x20-\x7e]/.test(text))return false;
  }
  return typeof value.caves==='boolean' && typeof value.layoutSha256==='string'
    && /^[0-9a-f]{64}$/.test(value.layoutSha256);
}

export function sameHiresWorld(a,b) {
  return validHiresContext(a) && validHiresContext(b) && a.world===b.world && a.dimension===b.dimension;
}
