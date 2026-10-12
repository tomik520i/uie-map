// Keep MSAA edge samples inside each textured triangle, rather than extrapolating
// atlas UVs into a neighbouring sprite. WebGL2 GLSL ES 3 supports centroid in/out.
export function useCentroidAtlasSampling(material, chunks) {
  material.onBeforeCompile = (shader) => {
    for (const [stage, chunk] of [['vertexShader', 'uv_pars_vertex'], ['fragmentShader', 'uv_pars_fragment']]) {
      const source = chunks[chunk];
      if (!source.includes('varying vec2 vMapUv;')) throw new Error('Atlas shader contract changed');
      shader[stage] = shader[stage].replace(`#include <${chunk}>`,
        source.replace('varying vec2 vMapUv;', 'centroid varying vec2 vMapUv;'));
    }
  };
  material.customProgramCacheKey = () => 'uie-centroid-atlas-v1';
  return material;
}
