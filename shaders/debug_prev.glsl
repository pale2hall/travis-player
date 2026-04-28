// debug shader: shows TRAVIS_PREV directly
// if you see live video → save+bind both work but get same-frame value (need workaround)
// if you see ghost/lag → TRAVIS_PREV holds previous frame correctly
// if you see static/black → save isn't persisting

//!HOOK OUTPUT
//!BIND HOOKED
//!BIND TRAVIS_PREV
//!DESC debug: show TRAVIS_PREV

vec4 hook() {
    // split screen: left half = current frame, right half = TRAVIS_PREV
    if (HOOKED_pos.x < 0.5) {
        return HOOKED_tex(HOOKED_pos);
    } else {
        return TRAVIS_PREV_tex(HOOKED_pos);
    }
}

//!HOOK OUTPUT
//!BIND HOOKED
//!SAVE TRAVIS_PREV
//!DESC debug: save frame

vec4 hook() {
    return HOOKED_tex(HOOKED_pos);
}
