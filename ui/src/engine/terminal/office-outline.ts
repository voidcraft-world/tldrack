import { Color, MeshBasicMaterial } from 'three';

/** Face-border glow: a crisp inner edge with a soft falloff, measured in drawing-buffer
 *  pixels with derivatives so thin parts do not get wider bands than desk tops. Keep it
 *  in the opaque surface shader: furniture occludes it, screen text stays untouched,
 *  and a host needs neither lights nor a postprocessing pass. Neutral edges separate
 *  unselected parts; mint marks hover/selection. Each rig owns only its uniform/material,
 *  while all rigs share one compiled program and the scene's geometry.
 */
export function office_outline(color: number) {
    const strength = { value: 0 };
    const material = new MeshBasicMaterial({ vertexColors: true });
    material.name = 'office-outline';
    material.onBeforeCompile = (shader) => {
        shader.uniforms.office_outline_strength = strength;
        shader.uniforms.office_outline_color = { value: new Color(color) };
        shader.vertexShader = shader.vertexShader.replace('#include <common>', `
            #include <common>
            varying vec2 office_uv;
        `).replace('#include <uv_vertex>', `
            #include <uv_vertex>
            office_uv = uv;
        `);
        shader.fragmentShader = shader.fragmentShader.replace('#include <common>', `
            #include <common>
            varying vec2 office_uv;
            uniform float office_outline_strength;
            uniform vec3 office_outline_color;
        `).replace('#include <opaque_fragment>', `
            vec2 edge = min(office_uv, 1.0 - office_uv) / max(fwidth(office_uv), vec2(0.00001));
            float distance_to_edge = min(edge.x, edge.y);
            float core = 1.0 - smoothstep(0.6, 1.5, distance_to_edge);
            float glow = 1.0 - smoothstep(1.5, 4.0, distance_to_edge);
            vec3 edge_color = mix(vec3(0.52), office_outline_color, office_outline_strength);
            float ink = mix(core * 0.16, core * 0.86 + glow * 0.14, office_outline_strength);
            outgoingLight = mix(outgoingLight, edge_color, ink);
            #include <opaque_fragment>
        `);
    };
    material.customProgramCacheKey = () => 'office-outline-v1';
    return { material, strength };
}
