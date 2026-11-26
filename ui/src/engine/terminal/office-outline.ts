import { Color, MeshBasicMaterial, ShaderMaterial } from 'three';

const BORDER_DISTANCE = `
    vec2 edge = min(office_uv, 1.0 - office_uv) / max(fwidth(office_uv), vec2(0.00001));
    float distance_to_edge = min(edge.x, edge.y);
`;

/** Face borders measured in drawing-buffer pixels so thin parts and desk tops agree.
 *  The opaque surface keeps a subtle neutral edge. Selection is a separate translucent
 *  pass with a wider core and soft falloff, drawn through occluders without filling the
 *  faces or covering label text. Its meshes inherit each part's pose and visibility;
 *  only hovered/selected rigs draw the extra pass. Geometry stays shared with the part.
 */
export function office_outline(color: number) {
    const strength = { value: 0 };
    const material = new MeshBasicMaterial({ vertexColors: true });
    material.name = 'office-outline';
    material.onBeforeCompile = (shader) => {
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
        `).replace('#include <opaque_fragment>', `
            ${BORDER_DISTANCE}
            float core = 1.0 - smoothstep(0.6, 1.5, distance_to_edge);
            outgoingLight = mix(outgoingLight, vec3(0.52), core * 0.16);
            #include <opaque_fragment>
        `);
    };
    material.customProgramCacheKey = () => 'office-outline-v2';
    const glow = new ShaderMaterial({
        name: 'office-outline-glow',
        transparent: true,
        depthTest: false,
        depthWrite: false,
        visible: false,
        uniforms: { strength, glow_color: { value: new Color(color) } },
        vertexShader: `
            varying vec2 office_uv;
            void main() {
                office_uv = uv;
                gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            }
        `,
        fragmentShader: `
            varying vec2 office_uv;
            uniform float strength;
            uniform vec3 glow_color;
            void main() {
                ${BORDER_DISTANCE}
                float core = 1.0 - smoothstep(1.2, 3.0, distance_to_edge);
                float halo = 1.0 - smoothstep(3.0, 7.0, distance_to_edge);
                float alpha = strength * (core * 0.55 + halo * 0.18);
                if (alpha < 0.01) discard;
                gl_FragColor = vec4(glow_color, alpha);
                #include <tonemapping_fragment>
                #include <colorspace_fragment>
            }
        `,
    });
    return { material, glow, strength };
}
