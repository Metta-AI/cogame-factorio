// cogame-factorio replay viewer: the map canvas, raylib -> emscripten.
//
// This module is deliberately generic and dumb: it knows nothing about
// Factorio names, the replay JSON or steps. viewer/index.html
// (with viewer/replay_pack.js) parses the replay and pushes compact
// typed arrays into the exported viewer_set_* functions; this file owns
// the camera (drag to pan, wheel to zoom, fit), the tile grid, water,
// resource patches, entity rectangles, belts, pipes, the character
// marker and hover picking. All colours except fixed chrome come from
// JS-provided palettes indexed by kind id.
//
// Data contract (row-major, copied into wasm-owned buffers on each call;
// the JS buffers may be freed right after the call returns):
//
//   viewer_set_terrain(x0, y0, x1, y1,
//                      resources: int32 [kind, tx, ty, bucket] * n,
//                      water:     int32 [tx, ty, run_east] * n,
//                      trees:     int32 [tx, ty] * n)
//   viewer_set_step(entities: float32 [kind, cx, cy, w, h, dir, status] * n,
//                   belts:    float32 [cx, cy, dir] * n,
//                   pipes:    float32 [cx, cy] * n,
//                   has_character, char_x, char_y)
//   viewer_set_entity_palette / viewer_set_resource_palette(rgb u8 * n)
//
// Coordinates follow FLE: +x east, +y south; entity x,y is the centre;
// a w x h entity covers [cx - w/2, cx + w/2) x [cy - h/2, cy + h/2).
// Tile (tx, ty) covers [tx, tx+1) x [ty, ty+1). dir: 0 N, 2 E, 4 S,
// 6 W (FLE Direction), anything else = no direction notch.
//
// Status ids (outline tint): 0 none, 1 working, 2 idle, 3 problem.

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef VIEWER_HEADLESS
#include "raylib.h"
#endif

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#define ENT_STRIDE 7
#define BELT_STRIDE 3
#define PIPE_STRIDE 2
#define RES_STRIDE 4
#define WATER_STRIDE 3
#define TREE_STRIDE 2
#define MAX_KINDS 64

// ---- state ---------------------------------------------------------------

static int g_screen_w = 960, g_screen_h = 640;

// terrain
static int g_bounds[4] = {0, 0, 0, 0};
static int g_has_bounds = 0;
static int* g_res = NULL;   static int g_nres = 0;
static int* g_water = NULL; static int g_nwater = 0;
static int* g_trees = NULL; static int g_ntrees = 0;

// step
static float* g_ents = NULL;  static int g_nents = 0;
static float* g_belts = NULL; static int g_nbelts = 0;
static float* g_pipes = NULL; static int g_npipes = 0;
static int g_has_char = 0;
static float g_char_x = 0.f, g_char_y = 0.f;

// palettes (rgb per kind); defaults are neutral greys until JS pushes
static unsigned char g_ent_pal[MAX_KINDS * 3];
static int g_ent_pal_n = 0;
static unsigned char g_res_pal[MAX_KINDS * 3];
static int g_res_pal_n = 0;

// camera: world tile at screen centre + pixels per tile
static double g_cam_x = 0.0, g_cam_y = 0.0, g_scale = 12.0;
static const double SCALE_MIN = 0.75, SCALE_MAX = 96.0;
static int g_want_fit = 1;

// interaction
static int g_hover = -1;              // hovered entity row or -1
static double g_hover_wx = 0.0, g_hover_wy = 0.0;
static int g_mouse_inside = 0;
static int g_highlight = -1;          // JS-selected row to outline (optional)

// ---- helpers -------------------------------------------------------------

// Stage note: stamped before each risky phase (allocations, drawing) so
// that on a wasm abort (OOM etc.) JS can read where we were. Fixed
// buffer, NUL-terminated, exported via viewer_stage_ptr/len.
#define STAGE_NOTE_LEN 192
static char g_stage_note[STAGE_NOTE_LEN] = "init";
static void stage(const char* what) {
    size_t n = strlen(what);
    if (n >= STAGE_NOTE_LEN) n = STAGE_NOTE_LEN - 1;
    memcpy(g_stage_note, what, n);
    g_stage_note[n] = 0;
}

static void* copy_buf(const void* src, size_t bytes) {
    if (bytes == 0) return NULL;
    void* p = malloc(bytes);   // -sABORTING_MALLOC: OOM aborts (JS onAbort)
    if (p) memcpy(p, src, bytes);
    return p;
}

// world <-> screen (screen px, origin top-left; +y down matches +y south)
static inline float sx(double wx) { return (float)((wx - g_cam_x) * g_scale + g_screen_w * 0.5); }
static inline float sy(double wy) { return (float)((wy - g_cam_y) * g_scale + g_screen_h * 0.5); }
static inline double wx_of(float px) { return (px - g_screen_w * 0.5) / g_scale + g_cam_x; }
static inline double wy_of(float py) { return (py - g_screen_h * 0.5) / g_scale + g_cam_y; }

// ---- exported API --------------------------------------------------------

#ifdef __EMSCRIPTEN__
#define VIEWER_EXPORT EMSCRIPTEN_KEEPALIVE
#else
#define VIEWER_EXPORT
#endif

VIEWER_EXPORT const char* viewer_stage_ptr(void) { return g_stage_note; }
VIEWER_EXPORT int viewer_stage_len(void) { return (int)strlen(g_stage_note); }

VIEWER_EXPORT void viewer_set_terrain(int x0, int y0, int x1, int y1,
                                      const int* resources, int nres,
                                      const int* water, int nwater,
                                      const int* trees, int ntrees) {
    stage("alloc terrain");
    g_bounds[0] = x0; g_bounds[1] = y0; g_bounds[2] = x1; g_bounds[3] = y1;
    g_has_bounds = (x1 > x0 && y1 > y0);
    free(g_res); free(g_water); free(g_trees);
    g_nres = nres > 0 ? nres : 0;
    g_nwater = nwater > 0 ? nwater : 0;
    g_ntrees = ntrees > 0 ? ntrees : 0;
    g_res = copy_buf(resources, (size_t)g_nres * RES_STRIDE * sizeof(int));
    g_water = copy_buf(water, (size_t)g_nwater * WATER_STRIDE * sizeof(int));
    g_trees = copy_buf(trees, (size_t)g_ntrees * TREE_STRIDE * sizeof(int));
    if (!g_res) g_nres = 0;
    if (!g_water) g_nwater = 0;
    if (!g_trees) g_ntrees = 0;
}

VIEWER_EXPORT void viewer_set_step(const float* entities, int nents,
                                   const float* belts, int nbelts,
                                   const float* pipes, int npipes,
                                   int has_character, float char_x, float char_y) {
    stage("alloc step");
    free(g_ents); free(g_belts); free(g_pipes);
    g_nents = nents > 0 ? nents : 0;
    g_nbelts = nbelts > 0 ? nbelts : 0;
    g_npipes = npipes > 0 ? npipes : 0;
    g_ents = copy_buf(entities, (size_t)g_nents * ENT_STRIDE * sizeof(float));
    g_belts = copy_buf(belts, (size_t)g_nbelts * BELT_STRIDE * sizeof(float));
    g_pipes = copy_buf(pipes, (size_t)g_npipes * PIPE_STRIDE * sizeof(float));
    if (!g_ents) g_nents = 0;
    if (!g_belts) g_nbelts = 0;
    if (!g_pipes) g_npipes = 0;
    stage("step ready");
    g_has_char = has_character ? 1 : 0;
    g_char_x = char_x; g_char_y = char_y;
    g_hover = -1;
    if (g_highlight >= g_nents) g_highlight = -1;
}

VIEWER_EXPORT void viewer_set_entity_palette(const unsigned char* rgb, int n) {
    if (n < 0) n = 0;
    if (n > MAX_KINDS) n = MAX_KINDS;
    memcpy(g_ent_pal, rgb, (size_t)n * 3);
    g_ent_pal_n = n;
}

VIEWER_EXPORT void viewer_set_resource_palette(const unsigned char* rgb, int n) {
    if (n < 0) n = 0;
    if (n > MAX_KINDS) n = MAX_KINDS;
    memcpy(g_res_pal, rgb, (size_t)n * 3);
    g_res_pal_n = n;
}

// Bounding box of the current step's content (entities, belts, pipes,
// character); falls back to the terrain bounds; returns 0 if nothing.
static int content_bounds(double* x0, double* y0, double* x1, double* y1) {
    double mnx = 1e30, mny = 1e30, mxx = -1e30, mxy = -1e30;
    int any = 0;
    for (int i = 0; i < g_nents; i++) {
        const float* e = g_ents + i * ENT_STRIDE;
        double hw = e[3] / 2.0, hh = e[4] / 2.0;
        if (e[1] - hw < mnx) mnx = e[1] - hw;
        if (e[1] + hw > mxx) mxx = e[1] + hw;
        if (e[2] - hh < mny) mny = e[2] - hh;
        if (e[2] + hh > mxy) mxy = e[2] + hh;
        any = 1;
    }
    for (int i = 0; i < g_nbelts; i++) {
        const float* b = g_belts + i * BELT_STRIDE;
        if (b[0] - .5 < mnx) mnx = b[0] - .5;
        if (b[0] + .5 > mxx) mxx = b[0] + .5;
        if (b[1] - .5 < mny) mny = b[1] - .5;
        if (b[1] + .5 > mxy) mxy = b[1] + .5;
        any = 1;
    }
    for (int i = 0; i < g_npipes; i++) {
        const float* p = g_pipes + i * PIPE_STRIDE;
        if (p[0] - .5 < mnx) mnx = p[0] - .5;
        if (p[0] + .5 > mxx) mxx = p[0] + .5;
        if (p[1] - .5 < mny) mny = p[1] - .5;
        if (p[1] + .5 > mxy) mxy = p[1] + .5;
        any = 1;
    }
    if (g_has_char) {
        if (g_char_x - .5 < mnx) mnx = g_char_x - .5;
        if (g_char_x + .5 > mxx) mxx = g_char_x + .5;
        if (g_char_y - .5 < mny) mny = g_char_y - .5;
        if (g_char_y + .5 > mxy) mxy = g_char_y + .5;
        any = 1;
    }
    if (any) {
        // a lone character / single entity would be a tiny box: pad to a
        // useful minimum window
        double w = mxx - mnx, h = mxy - mny;
        if (w < 24) { double c = (mnx + mxx) / 2; mnx = c - 12; mxx = c + 12; }
        if (h < 24) { double c = (mny + mxy) / 2; mny = c - 12; mxy = c + 12; }
        *x0 = mnx; *y0 = mny; *x1 = mxx; *y1 = mxy;
        return 1;
    }
    if (g_has_bounds) {
        *x0 = g_bounds[0]; *y0 = g_bounds[1]; *x1 = g_bounds[2]; *y1 = g_bounds[3];
        return 1;
    }
    return 0;
}

// Fit the camera to the current content (or terrain).
VIEWER_EXPORT void viewer_fit(void) {
    double x0, y0, x1, y1;
    if (!content_bounds(&x0, &y0, &x1, &y1)) {
        g_cam_x = 0; g_cam_y = 0; g_scale = 12.0;
        return;
    }
    double w = x1 - x0 + 4.0, h = y1 - y0 + 4.0;   // 2-tile margin
    double s = fmin(g_screen_w / w, g_screen_h / h);
    if (s < SCALE_MIN) s = SCALE_MIN;
    if (s > 32.0) s = 32.0;    // don't zoom a tiny base into a wall of pixels
    g_scale = s;
    g_cam_x = (x0 + x1) / 2.0;
    g_cam_y = (y0 + y1) / 2.0;
    g_want_fit = 0;
}

// Fit to the whole captured terrain regardless of content.
VIEWER_EXPORT void viewer_fit_terrain(void) {
    if (!g_has_bounds) { viewer_fit(); return; }
    double w = g_bounds[2] - g_bounds[0] + 2.0, h = g_bounds[3] - g_bounds[1] + 2.0;
    double s = fmin(g_screen_w / w, g_screen_h / h);
    if (s < SCALE_MIN) s = SCALE_MIN;
    g_scale = s;
    g_cam_x = (g_bounds[0] + g_bounds[2]) / 2.0;
    g_cam_y = (g_bounds[1] + g_bounds[3]) / 2.0;
    g_want_fit = 0;
}

VIEWER_EXPORT void viewer_resize(int w, int h) {
    if (w < 64) w = 64;
    if (h < 64) h = 64;
    if (w == g_screen_w && h == g_screen_h) return;
    g_screen_w = w; g_screen_h = h;
#ifndef VIEWER_HEADLESS
    if (IsWindowReady()) SetWindowSize(w, h);
#endif
}

VIEWER_EXPORT void viewer_set_camera(double cx, double cy, double scale) {
    g_cam_x = cx; g_cam_y = cy;
    if (scale < SCALE_MIN) scale = SCALE_MIN;
    if (scale > SCALE_MAX) scale = SCALE_MAX;
    g_scale = scale;
    g_want_fit = 0;
}
VIEWER_EXPORT double viewer_camera_x(void) { return g_cam_x; }
VIEWER_EXPORT double viewer_camera_y(void) { return g_cam_y; }
VIEWER_EXPORT double viewer_camera_scale(void) { return g_scale; }

// Hover picking: JS polls these per frame and shows its own tooltip.
// viewer_pick(px, py) also lets JS query an arbitrary canvas position.
static int pick_at(double wx, double wy) {
    for (int i = g_nents - 1; i >= 0; i--) {   // last drawn = topmost
        const float* e = g_ents + i * ENT_STRIDE;
        double hw = e[3] / 2.0, hh = e[4] / 2.0;
        // small entities (poles, inserters) get a slightly larger hit box
        double slop = (g_scale < 8.0) ? 4.0 / g_scale : 0.0;
        if (wx >= e[1] - hw - slop && wx < e[1] + hw + slop &&
            wy >= e[2] - hh - slop && wy < e[2] + hh + slop)
            return i;
    }
    return -1;
}
VIEWER_EXPORT int viewer_pick(float px, float py) { return pick_at(wx_of(px), wy_of(py)); }
VIEWER_EXPORT int viewer_hover_entity(void) { return g_mouse_inside ? g_hover : -1; }
VIEWER_EXPORT double viewer_hover_x(void) { return g_hover_wx; }
VIEWER_EXPORT double viewer_hover_y(void) { return g_hover_wy; }
VIEWER_EXPORT int viewer_mouse_inside(void) { return g_mouse_inside; }
VIEWER_EXPORT void viewer_set_highlight(int row) { g_highlight = row; }

// ---- rendering (raylib; excluded from the headless node build) -----------
#ifndef VIEWER_HEADLESS

static Color kind_color(const unsigned char* pal, int n, int kind, unsigned char a) {
    if (kind < 0 || kind >= n) return (Color){150, 150, 160, a};
    return (Color){pal[kind * 3], pal[kind * 3 + 1], pal[kind * 3 + 2], a};
}

static Color status_color(int status) {
    switch (status) {
        case 1: return (Color){235, 235, 235, 200};   // working: neutral light rim
        case 2: return (Color){240, 200, 60, 255};    // idle: amber
        case 3: return (Color){245, 70, 60, 255};     // problem: red
        default: return (Color){20, 22, 26, 220};     // none: dark rim
    }
}


static void world_rect(double x0, double y0, double w, double h, Color c) {
    float px = sx(x0), py = sy(y0);
    float pw = (float)(w * g_scale), ph = (float)(h * g_scale);
    if (px + pw < 0 || py + ph < 0 || px > g_screen_w || py > g_screen_h) return;
    if (pw < 1.f) pw = 1.f;
    if (ph < 1.f) ph = 1.f;
    DrawRectangleV((Vector2){px, py}, (Vector2){pw, ph}, c);
}

static void world_rect_lines(double x0, double y0, double w, double h, float thick, Color c) {
    float px = sx(x0), py = sy(y0);
    float pw = (float)(w * g_scale), ph = (float)(h * g_scale);
    if (px + pw < 0 || py + ph < 0 || px > g_screen_w || py > g_screen_h) return;
    DrawRectangleLinesEx((Rectangle){px, py, pw, ph}, thick, c);
}

// Small triangle on the `dir` side of the (cx,cy,w,h) box, pointing out.
static void draw_dir_notch(double cx, double cy, double w, double h, int dir, Color c) {
    if (g_scale < 4.0) return;
    double s = fmin(w, h) * 0.22;   // notch half-size in tiles
    if (s * g_scale < 1.5) s = 1.5 / g_scale;
    Vector2 a, b, t;
    switch (dir) {
        case 0: // north (up on screen)
            t = (Vector2){sx(cx), sy(cy - h / 2) + 1};
            a = (Vector2){sx(cx - s), sy(cy - h / 2 + s * 1.6)};
            b = (Vector2){sx(cx + s), sy(cy - h / 2 + s * 1.6)};
            break;
        case 2: // east
            t = (Vector2){sx(cx + w / 2) - 1, sy(cy)};
            a = (Vector2){sx(cx + w / 2 - s * 1.6), sy(cy + s)};
            b = (Vector2){sx(cx + w / 2 - s * 1.6), sy(cy - s)};
            break;
        case 4: // south
            t = (Vector2){sx(cx), sy(cy + h / 2) - 1};
            a = (Vector2){sx(cx + s), sy(cy + h / 2 - s * 1.6)};
            b = (Vector2){sx(cx - s), sy(cy + h / 2 - s * 1.6)};
            break;
        case 6: // west
            t = (Vector2){sx(cx - w / 2) + 1, sy(cy)};
            a = (Vector2){sx(cx - w / 2 + s * 1.6), sy(cy - s)};
            b = (Vector2){sx(cx - w / 2 + s * 1.6), sy(cy + s)};
            break;
        default: return;
    }
    // raylib wants counter-clockwise; order t,a,b is CCW for the cases above
    DrawTriangle(t, a, b, c);
}

// Belt chevron: a small "V" pointing along dir inside the tile.
static void draw_chevron(double cx, double cy, int dir, Color c) {
    if (g_scale < 5.0) return;
    double s = 0.28;
    float th = (float)fmax(1.0, g_scale * 0.09);
    Vector2 tip, l, r;
    switch (dir) {
        case 0: tip = (Vector2){sx(cx), sy(cy - s)}; l = (Vector2){sx(cx - s), sy(cy + s * 0.5)}; r = (Vector2){sx(cx + s), sy(cy + s * 0.5)}; break;
        case 2: tip = (Vector2){sx(cx + s), sy(cy)}; l = (Vector2){sx(cx - s * 0.5), sy(cy - s)}; r = (Vector2){sx(cx - s * 0.5), sy(cy + s)}; break;
        case 4: tip = (Vector2){sx(cx), sy(cy + s)}; l = (Vector2){sx(cx - s), sy(cy - s * 0.5)}; r = (Vector2){sx(cx + s), sy(cy - s * 0.5)}; break;
        case 6: tip = (Vector2){sx(cx - s), sy(cy)}; l = (Vector2){sx(cx + s * 0.5), sy(cy - s)}; r = (Vector2){sx(cx + s * 0.5), sy(cy + s)}; break;
        default: return;
    }
    DrawLineEx(l, tip, th, c);
    DrawLineEx(r, tip, th, c);
}


static void handle_input(void) {
    Vector2 m = GetMousePosition();
    g_mouse_inside = (m.x >= 0 && m.y >= 0 && m.x < g_screen_w && m.y < g_screen_h);

    float wheel = GetMouseWheelMove();
    if (wheel != 0.f && g_mouse_inside) {
        if (wheel > 3.f) wheel = 3.f;
        if (wheel < -3.f) wheel = -3.f;
        double before_x = wx_of(m.x), before_y = wy_of(m.y);
        double ns = g_scale * pow(1.15, wheel);
        if (ns < SCALE_MIN) ns = SCALE_MIN;
        if (ns > SCALE_MAX) ns = SCALE_MAX;
        g_scale = ns;
        // keep the world point under the cursor fixed
        g_cam_x = before_x - (m.x - g_screen_w * 0.5) / g_scale;
        g_cam_y = before_y - (m.y - g_screen_h * 0.5) / g_scale;
        g_want_fit = 0;
    }
    if (IsMouseButtonDown(MOUSE_BUTTON_LEFT) || IsMouseButtonDown(MOUSE_BUTTON_MIDDLE)) {
        Vector2 d = GetMouseDelta();
        if (d.x != 0.f || d.y != 0.f) {
            g_cam_x -= d.x / g_scale;
            g_cam_y -= d.y / g_scale;
            g_want_fit = 0;
        }
    }
    g_hover_wx = wx_of(m.x);
    g_hover_wy = wy_of(m.y);
    g_hover = g_mouse_inside ? pick_at(g_hover_wx, g_hover_wy) : -1;
}

static void draw_terrain(void) {
    // captured area: a slightly lighter ground plate
    if (g_has_bounds) {
        world_rect(g_bounds[0], g_bounds[1], g_bounds[2] - g_bounds[0],
                   g_bounds[3] - g_bounds[1], (Color){34, 44, 38, 255});
    }
    // water runs
    for (int i = 0; i < g_nwater; i++) {
        const int* w = g_water + i * WATER_STRIDE;
        world_rect(w[0], w[1], w[2], 1, (Color){38, 92, 140, 255});
    }
    // resource tiles tinted by kind, alpha by amount bucket
    for (int i = 0; i < g_nres; i++) {
        const int* r = g_res + i * RES_STRIDE;
        int bucket = r[3] < 0 ? 0 : (r[3] > 7 ? 7 : r[3]);
        unsigned char a = (unsigned char)(150 + bucket * 15);   // 150..255 by amount
        world_rect(r[1], r[2], 1, 1, kind_color(g_res_pal, g_res_pal_n, r[0], a));
    }
    // trees: small dark-green blobs
    if (g_scale >= 2.0) {
        for (int i = 0; i < g_ntrees; i++) {
            const int* t = g_trees + i * TREE_STRIDE;
            world_rect(t[0] + 0.2, t[1] + 0.2, 0.6, 0.6, (Color){52, 110, 58, 230});
        }
    } else {
        for (int i = 0; i < g_ntrees; i++) {
            const int* t = g_trees + i * TREE_STRIDE;
            world_rect(t[0], t[1], 1, 1, (Color){52, 110, 58, 160});
        }
    }
}

static void draw_grid(void) {
    if (g_scale < 6.0) return;
    Color c = (Color){255, 255, 255, (unsigned char)(g_scale < 12.0 ? 10 : 18)};
    double x0 = floor(wx_of(0)), x1 = ceil(wx_of((float)g_screen_w));
    double y0 = floor(wy_of(0)), y1 = ceil(wy_of((float)g_screen_h));
    for (double x = x0; x <= x1; x += 1.0) {
        float px = sx(x);
        DrawLine((int)px, 0, (int)px, g_screen_h, c);
    }
    for (double y = y0; y <= y1; y += 1.0) {
        float py = sy(y);
        DrawLine(0, (int)py, g_screen_w, (int)py, c);
    }
    // chunk lines (32 tiles) a bit brighter, always
    Color cc = (Color){255, 255, 255, 34};
    for (double x = floor(x0 / 32) * 32; x <= x1; x += 32.0) {
        float px = sx(x); DrawLine((int)px, 0, (int)px, g_screen_h, cc);
    }
    for (double y = floor(y0 / 32) * 32; y <= y1; y += 32.0) {
        float py = sy(y); DrawLine(0, (int)py, g_screen_w, (int)py, cc);
    }
    // origin axes
    Color ax = (Color){255, 255, 255, 60};
    DrawLine((int)sx(0), 0, (int)sx(0), g_screen_h, ax);
    DrawLine(0, (int)sy(0), g_screen_w, (int)sy(0), ax);
}

static void draw_step(void) {
    // pipes: small squares
    Color pipe_c = (Color){118, 140, 160, 255};
    for (int i = 0; i < g_npipes; i++) {
        const float* p = g_pipes + i * PIPE_STRIDE;
        world_rect(p[0] - 0.3, p[1] - 0.3, 0.6, 0.6, pipe_c);
    }
    // belts: dark tile + chevron
    Color belt_bg = (Color){96, 84, 40, 255};
    Color belt_fg = (Color){246, 214, 96, 255};
    for (int i = 0; i < g_nbelts; i++) {
        const float* b = g_belts + i * BELT_STRIDE;
        world_rect(b[0] - 0.5 + 0.08, b[1] - 0.5 + 0.08, 0.84, 0.84, belt_bg);
        draw_chevron(b[0], b[1], (int)b[2], belt_fg);
    }
    // entities
    for (int i = 0; i < g_nents; i++) {
        const float* e = g_ents + i * ENT_STRIDE;
        int kind = (int)e[0];
        double cx = e[1], cy = e[2], w = e[3], h = e[4];
        int dir = (int)e[5], status = (int)e[6];
        double x0 = cx - w / 2.0, y0 = cy - h / 2.0;
        double inset = 0.06;
        Color fill = kind_color(g_ent_pal, g_ent_pal_n, kind, 235);
        world_rect(x0 + inset, y0 + inset, w - 2 * inset, h - 2 * inset, fill);
        float th = (float)fmax(1.0, fmin(3.0, g_scale * 0.12));
        world_rect_lines(x0 + inset, y0 + inset, w - 2 * inset, h - 2 * inset, th,
                         status_color(status));
        draw_dir_notch(cx, cy, w, h, dir, (Color){12, 12, 14, 230});
        if (i == g_hover || i == g_highlight) {
            world_rect_lines(x0 - 0.15, y0 - 0.15, w + 0.3, h + 0.3,
                             (float)fmax(2.0, g_scale * 0.15), (Color){255, 255, 255, 255});
        }
    }
    // character marker
    if (g_has_char) {
        float px = sx(g_char_x), py = sy(g_char_y);
        float r = (float)fmax(4.0, g_scale * 0.42);
        DrawCircleV((Vector2){px, py}, r + 2.f, (Color){10, 10, 12, 220});
        DrawCircleV((Vector2){px, py}, r, (Color){80, 230, 150, 255});
        DrawCircleV((Vector2){px, py}, r * 0.4f, (Color){10, 40, 24, 255});
    }
}

static int g_frames = 0;

static void frame(void) {
    if (g_want_fit && (g_nents || g_nbelts || g_npipes || g_has_char || g_has_bounds)) {
        viewer_fit();
    }
    handle_input();
    stage("draw");
    BeginDrawing();
    ClearBackground((Color){18, 22, 26, 255});
    draw_terrain();
    draw_grid();
    draw_step();
    EndDrawing();
    g_frames++;
    stage("idle");
}

// Frames rendered so far; JS dismisses its loading plate on the first one.
VIEWER_EXPORT int viewer_frame_count(void) { return g_frames; }

int main(void) {
    stage("init window");
    SetTraceLogLevel(LOG_WARNING);
    InitWindow(g_screen_w, g_screen_h, "cogame-factorio replay");
#ifdef __EMSCRIPTEN__
    // 0 fps == requestAnimationFrame; main returns and the runtime stays
    // alive (EXIT_RUNTIME=0) for the viewer_* exports.
    emscripten_set_main_loop(frame, 0, 0);
#else
    SetTargetFPS(60);
    while (!WindowShouldClose()) frame();
    CloseWindow();
#endif
    return 0;
}
#else  // VIEWER_HEADLESS: no raylib, no main loop; the data/camera/pick API
       // above is what viewer/smoke.cjs exercises under node.
VIEWER_EXPORT int viewer_frame_count(void) { return 0; }
#endif  // VIEWER_HEADLESS
