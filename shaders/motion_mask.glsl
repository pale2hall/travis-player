// travis-player motion mask — single hook with ping-pong save/bind
// theory: within one hook, BIND reads OLD value, SAVE writes NEW.
// the texture is a persistent ping-pong buffer between frames.

#define ALPHA_FLOOR 0.05
#define ACTIVITY_THRESHOLD 0.05
#define BLUR_R 2.0
#define DEBUG_MODE 1

//!HOOK OUTPUT
//!BIND HOOKED
//!BIND TRAVIS_PREV
//!SAVE TRAVIS_PREV
//!DESC travis: ping-pong frame diff

float luma(vec3 c) {
    return dot(c, vec3(0.2126, 0.7152, 0.0722));
}

vec4 hook() {
    vec4  curr = HOOKED_tex(HOOKED_pos);
    vec4  prev = TRAVIS_PREV_tex(HOOKED_pos);
    float diff = abs(luma(curr.rgb) - luma(prev.rgb));
    float act  = smoothstep(ACTIVITY_THRESHOLD * 0.5, ACTIVITY_THRESHOLD * 1.5, diff);

#if DEBUG_MODE == 1
    // heatmap visualization
    vec3 heat = mix(vec3(0.0, 0.2, 1.0), vec3(1.0, 0.1, 0.0), act);
    return vec4(mix(curr.rgb, heat, 0.7), 1.0);
#else
    return vec4(curr.rgb, mix(ALPHA_FLOOR, 1.0, act));
#endif
}
