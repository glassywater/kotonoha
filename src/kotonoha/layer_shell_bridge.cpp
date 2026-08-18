// Kotonoha Wayland layer-shell bridge.
//
// Compiled to libkoto-layer.so and loaded from Python via ctypes (see
// native.py / overlay.py). Promotes a QWindow to a wlr-layer-shell Overlay
// surface so the lyrics float above fullscreen apps, and exposes click-through
// (input region) + positioning control.
//
// IMPORTANT: make_overlay() must run BEFORE the window is shown, otherwise the
// surface already has an xdg-shell role and LayerShellQt refuses to convert it
// ("already has a shell integration"). overlay.py calls activate before show().
//
// Modelled on BiliHUD's bridge: anchor Top|Left and position the fixed-size
// surface with left/top margins (set_anchor_position), which is what makes the
// panel freely draggable.

#include <QWindow>
#include <LayerShellQt/Window>

#include <QMargins>

#include <QGuiApplication>
#include <qpa/qplatformnativeinterface.h>
#include <wayland-client.h>

#include <cstring>  // std::strcmp — used by the layer-shell probe, blur or not

#ifdef KOTONOHA_HAVE_BLUR
#include <cmath>
#include <map>

#include "blur-client-protocol.h"
#include "ext-background-effect-v1-client-protocol.h"


namespace {
    // Backdrop blur for the frosted-glass surfaces, over whichever protocol the
    // compositor speaks:
    //   * ext-background-effect-v1 — cross-desktop, implemented by KWin 6.7+ (which
    //     dropped the KDE-private protocol below) and by Mutter;
    //   * org_kde_kwin_blur — its predecessor, the only one Plasma <= 6.6 offers.
    // Both are bound lazily from the registry; a compositor with neither leaves
    // blur a no-op and the translucent fill renders unblurred.
    struct ext_background_effect_manager_v1* g_effect_manager = nullptr;
    uint32_t g_effect_caps = 0;  // from the capabilities event; blur may be absent
    struct org_kde_kwin_blur_manager* g_blur_manager = nullptr;
    // One effect/blur object PER surface, so several windows (the overlay pill AND
    // the settings window) can each be frosted independently without clobbering a
    // single shared object.
    std::map<struct wl_surface*, struct ext_background_effect_surface_v1*> g_effects;
    std::map<struct wl_surface*, struct org_kde_kwin_blur*> g_blurs;
    bool g_blur_probed = false;

    void effect_capabilities(void*, struct ext_background_effect_manager_v1*, uint32_t flags) {
        g_effect_caps = flags;
    }
    const struct ext_background_effect_manager_v1_listener kEffectListener = {effect_capabilities};

    void registry_global(void*, struct wl_registry* registry, uint32_t name,
                         const char* interface, uint32_t /*version*/) {
        if (std::strcmp(interface, ext_background_effect_manager_v1_interface.name) == 0) {
            g_effect_manager = static_cast<struct ext_background_effect_manager_v1*>(
                wl_registry_bind(registry, name, &ext_background_effect_manager_v1_interface, 1));
            // Listen here rather than after the roundtrip: the compositor sends
            // capabilities as soon as the global is bound, and an event delivered
            // to a proxy that has no listener yet is dropped.
            ext_background_effect_manager_v1_add_listener(g_effect_manager, &kEffectListener, nullptr);
        } else if (std::strcmp(interface, "org_kde_kwin_blur_manager") == 0) {
            g_blur_manager = static_cast<struct org_kde_kwin_blur_manager*>(
                wl_registry_bind(registry, name, &org_kde_kwin_blur_manager_interface, 1));
        }
    }
    void registry_global_remove(void*, struct wl_registry*, uint32_t) {}
    const struct wl_registry_listener kRegistryListener = {registry_global, registry_global_remove};

    struct wl_compositor* get_compositor(QPlatformNativeInterface* native) {
        struct wl_compositor* c = (struct wl_compositor*)native->nativeResourceForIntegration("compositor");
        if (!c) c = (struct wl_compositor*)native->nativeResourceForIntegration("wl_compositor");
        return c;
    }

    // Approximate a rounded rectangle as a wl_region: a full-width middle band
    // plus one 1px strip per corner row inset to the arc. Without this the blur
    // is a sharp rectangle that overhangs the pill's rounded corners.
    void add_rounded_rect(struct wl_region* region, int x, int y, int w, int h, int radius) {
        int r = radius;
        if (r < 0) r = 0;
        if (r * 2 > w) r = w / 2;
        if (r * 2 > h) r = h / 2;
        if (r == 0) {
            wl_region_add(region, x, y, w, h);
            return;
        }
        wl_region_add(region, x, y + r, w, h - 2 * r);  // middle band, full width
        for (int i = 0; i < r; ++i) {
            int dy = r - i;  // vertical distance from the arc centre for this row
            int dx = r - static_cast<int>(std::sqrt(static_cast<double>(r * r - dy * dy)) + 0.5);
            int rw = w - 2 * dx;
            if (rw <= 0) continue;
            wl_region_add(region, x + dx, y + i, rw, 1);            // top row
            wl_region_add(region, x + dx, y + h - 1 - i, rw, 1);   // mirrored bottom row
        }
    }

    // True once the compositor advertises ext-background-effect-v1 AND reports that
    // it can actually blur: the capability is dynamic, and without it the effect
    // object would be created but never render.
    bool effect_blur_ready() {
        return g_effect_manager && (g_effect_caps & EXT_BACKGROUND_EFFECT_MANAGER_V1_CAPABILITY_BLUR);
    }

    // Bind whichever blur protocol this compositor offers. Returns true when at
    // least one is usable. Probed once; the result is cached.
    bool probe_blur(QPlatformNativeInterface* native) {
        if (!g_blur_probed) {
            g_blur_probed = true;
            struct wl_display* display = (struct wl_display*)native->nativeResourceForIntegration("wl_display");
            if (!display) display = (struct wl_display*)native->nativeResourceForIntegration("display");
            if (display) {
                struct wl_registry* registry = wl_display_get_registry(display);
                wl_registry_add_listener(registry, &kRegistryListener, nullptr);
                wl_display_roundtrip(display);  // process global advertisements so the binds land
                if (g_effect_manager) {
                    wl_display_roundtrip(display);  // and then the capabilities event
                }
                wl_registry_destroy(registry);  // the bound globals outlive it
            }
        }
        return effect_blur_ready() || g_blur_manager != nullptr;
    }
}  // namespace
#endif  // KOTONOHA_HAVE_BLUR


namespace {
    // One-shot probe (always compiled, unlike the blur namespace above): does THIS
    // compositor advertise zwlr_layer_shell_v1? Backs koto_has_layer_shell() so the
    // Python side can pick the top-most-window fallback on ANY layer-shell-less
    // Wayland session (GNOME/Mutter, Weston, Cinnamon) without hard-coding names.
    bool g_layer_shell_present = false;
    bool g_layer_shell_probed = false;

    void ls_registry_global(void* data, struct wl_registry*, uint32_t,
                            const char* interface, uint32_t /*version*/) {
        if (std::strcmp(interface, "zwlr_layer_shell_v1") == 0) {
            *static_cast<bool*>(data) = true;
        }
    }
    void ls_registry_global_remove(void*, struct wl_registry*, uint32_t) {}
    const struct wl_registry_listener kLayerShellRegistryListener = {
        ls_registry_global, ls_registry_global_remove};
}  // namespace


extern "C" {
    // Qt ABI handshake for the ctypes loader (native.py): the version this bridge
    // was built against. The loader refuses a bridge built against a different Qt
    // minor than the running PyQt6 — the bridge links Qt QPA/private API, which
    // carries no cross-minor ABI guarantee.
    const char* koto_layer_qt_version() {
        return QT_VERSION_STR;
    }

    // 1 if the compositor advertises wlr-layer-shell, else 0. Lets the Python side
    // fall back to a top-most ordinary window on a layer-shell-less Wayland session
    // instead of silently no-op'ing every bridge call. Result cached after first.
    int koto_has_layer_shell() {
        if (g_layer_shell_probed) return g_layer_shell_present ? 1 : 0;
        g_layer_shell_probed = true;
        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return 0;
        struct wl_display* display = (struct wl_display*)native->nativeResourceForIntegration("wl_display");
        if (!display) display = (struct wl_display*)native->nativeResourceForIntegration("display");
        if (!display) return 0;
        struct wl_registry* registry = wl_display_get_registry(display);
        if (!registry) return 0;
        wl_registry_add_listener(registry, &kLayerShellRegistryListener, &g_layer_shell_present);
        wl_display_roundtrip(display);  // process the compositor's global advertisements
        wl_registry_destroy(registry);
        return g_layer_shell_present ? 1 : 0;
    }

    void make_overlay(void* window_ptr) {
        if (!window_ptr) return;

        QWindow* window = static_cast<QWindow*>(window_ptr);
        LayerShellQt::Window* ls_window = LayerShellQt::Window::get(window);

        if (ls_window) {
            ls_window->setLayer(LayerShellQt::Window::LayerOverlay);
            // -1 for no exclusive zone (fully ignored by tiling layout).
            ls_window->setExclusiveZone(-1);
            // Lyrics are passive; no keyboard focus.
            ls_window->setKeyboardInteractivity(LayerShellQt::Window::KeyboardInteractivityNone);
            // Anchor to the top-left corner; the surface keeps its requested size
            // and is positioned by left/top margins (set_anchor_position).
            ls_window->setAnchors(LayerShellQt::Window::Anchors(
                LayerShellQt::Window::AnchorTop | LayerShellQt::Window::AnchorLeft));
            ls_window->setScope("kotonoha");
        }
    }

    // Position the surface via left/top margins (x, y from the top-left anchor).
    void set_anchor_position(void* window_ptr, int x, int y) {
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);
        LayerShellQt::Window* ls_window = LayerShellQt::Window::get(window);

        if (ls_window) {
            QMargins margins;
            margins.setLeft(x);
            margins.setTop(y);
            margins.setRight(0);
            margins.setBottom(0);
            ls_window->setMargins(margins);
        }
        // Commit the surface right away so the move lands without waiting for the
        // next Qt repaint (reduces the dragging lag / "repainting in place" feel).
        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (native) {
            struct wl_surface* surface = (struct wl_surface*)native->nativeResourceForWindow("surface", window);
            if (surface) {
                wl_surface_commit(surface);
            }
        }
    }

    void set_passthrough(void* window_ptr, bool enabled) {
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);

        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return;

        struct wl_surface* surface = (struct wl_surface*)native->nativeResourceForWindow("surface", window);
        if (!surface) return;

        struct wl_compositor* compositor = (struct wl_compositor*)native->nativeResourceForIntegration("compositor");
        if (!compositor) {
            compositor = (struct wl_compositor*)native->nativeResourceForIntegration("wl_compositor");
        }

        if (surface && compositor) {
            if (enabled) {
                // Empty input region -> surface accepts no input (click-through).
                struct wl_region* region = wl_compositor_create_region(compositor);
                wl_surface_set_input_region(surface, region);
                wl_region_destroy(region);
            } else {
                // NULL input region -> infinite region (surface accepts all input).
                wl_surface_set_input_region(surface, nullptr);
            }
            wl_surface_commit(surface);
        }
    }

    // Restrict input to a single rectangle (surface coords). Used while unlocked
    // so only the visible pill catches clicks — the transparent area around it
    // stays click-through instead of the whole big band grabbing every click.
    void set_input_rect(void* window_ptr, int x, int y, int w, int h) {
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);

        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return;

        struct wl_surface* surface = (struct wl_surface*)native->nativeResourceForWindow("surface", window);
        if (!surface) return;

        struct wl_compositor* compositor = (struct wl_compositor*)native->nativeResourceForIntegration("compositor");
        if (!compositor) {
            compositor = (struct wl_compositor*)native->nativeResourceForIntegration("wl_compositor");
        }

        if (surface && compositor) {
            struct wl_region* region = wl_compositor_create_region(compositor);
            wl_region_add(region, x, y, w, h);
            wl_surface_set_input_region(surface, region);
            wl_region_destroy(region);
            wl_surface_commit(surface);
        }
    }

    // 1 when this compositor can blur a surface's backdrop over either protocol, so
    // the UI can gate the frosted-glass options on the real capability instead of
    // guessing from the desktop name. Result cached after the first probe.
    int koto_has_blur() {
#ifndef KOTONOHA_HAVE_BLUR
        return 0;  // built without blur
#else
        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return 0;
        return probe_blur(native) ? 1 : 0;
#endif  // KOTONOHA_HAVE_BLUR
    }

    // Ask the compositor to blur whatever is behind the pill rectangle (frosted
    // glass). No-op where no blur protocol exists; the translucent fill still
    // renders, so the panel just isn't blurred there.
    void set_blur_region(void* window_ptr, int x, int y, int w, int h, int radius) {
#ifndef KOTONOHA_HAVE_BLUR
        (void)window_ptr; (void)x; (void)y; (void)w; (void)h; (void)radius;  // built without blur
#else
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);
        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return;
        struct wl_surface* surface = (struct wl_surface*)native->nativeResourceForWindow("surface", window);
        if (!surface) return;
        if (!probe_blur(native)) return;

        struct wl_compositor* compositor = get_compositor(native);
        struct wl_region* region = nullptr;
        if (compositor) {
            region = wl_compositor_create_region(compositor);
            add_rounded_rect(region, x, y, w, h, radius);  // match the pill's rounded corners
        }

        if (effect_blur_ready()) {
            // Replace any previous effect for THIS surface (leave other windows
            // alone). Recreating rather than reusing also covers the surface being
            // destroyed and a new one allocated at the same address — the overlay
            // rebuilds its layer surface when it changes output, and the old effect
            // object is inert from then on.
            auto existing = g_effects.find(surface);
            if (existing != g_effects.end()) {
                ext_background_effect_surface_v1_destroy(existing->second);
                g_effects.erase(existing);
            }
            struct ext_background_effect_surface_v1* effect =
                ext_background_effect_manager_v1_get_background_effect(g_effect_manager, surface);
            g_effects[surface] = effect;  // keep it alive so the effect persists
            ext_background_effect_surface_v1_set_blur_region(effect, region);
        } else if (g_blur_manager) {
            auto existing = g_blurs.find(surface);
            if (existing != g_blurs.end()) {
                org_kde_kwin_blur_release(existing->second);
                g_blurs.erase(existing);
            }
            struct org_kde_kwin_blur* blur = org_kde_kwin_blur_manager_create(g_blur_manager, surface);
            g_blurs[surface] = blur;
            if (region) org_kde_kwin_blur_set_region(blur, region);
            org_kde_kwin_blur_commit(blur);
        }

        if (region) wl_region_destroy(region);
        wl_surface_commit(surface);
#endif  // KOTONOHA_HAVE_BLUR
    }

    void clear_blur(void* window_ptr) {
#ifndef KOTONOHA_HAVE_BLUR
        (void)window_ptr;  // built without the blur protocol
#else
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);
        QPlatformNativeInterface* native = QGuiApplication::platformNativeInterface();
        if (!native) return;
        struct wl_surface* surface = (struct wl_surface*)native->nativeResourceForWindow("surface", window);
        if (!surface) return;
        // No capability check before the erase. An object already created must be
        // destroyed whatever the compositor reports now: withdrawing the blur
        // capability makes probe_blur() false, and returning here would strand the
        // proxy in the map for the life of the process.

        // Destroying the effect object drops its regions on the next commit.
        auto effect = g_effects.find(surface);
        if (effect != g_effects.end()) {
            ext_background_effect_surface_v1_destroy(effect->second);
            g_effects.erase(effect);
        }
        auto existing = g_blurs.find(surface);
        if (existing != g_blurs.end()) {
            org_kde_kwin_blur_release(existing->second);
            g_blurs.erase(existing);
        }
        if (g_blur_manager) org_kde_kwin_blur_manager_unset(g_blur_manager, surface);
        wl_surface_commit(surface);
#endif  // KOTONOHA_HAVE_BLUR
    }

    // How many compositor-side blur objects this process is holding. Exported so a
    // test can assert that repeated surface rebuilds do not accumulate them: the
    // objects are keyed by wl_surface, and a rebuilt surface gets a new address, so
    // one left behind can never be found again.
    int koto_blur_object_count() {
#ifndef KOTONOHA_HAVE_BLUR
        return 0;  // built without blur
#else
        return static_cast<int>(g_effects.size() + g_blurs.size());
#endif  // KOTONOHA_HAVE_BLUR
    }

    void set_keyboard_interactivity(void* window_ptr, bool enabled) {
        if (!window_ptr) return;
        QWindow* window = static_cast<QWindow*>(window_ptr);
        LayerShellQt::Window* ls_window = LayerShellQt::Window::get(window);

        if (ls_window) {
            if (enabled) {
                ls_window->setKeyboardInteractivity(LayerShellQt::Window::KeyboardInteractivityOnDemand);
            } else {
                ls_window->setKeyboardInteractivity(LayerShellQt::Window::KeyboardInteractivityNone);
            }
        }
    }
}
