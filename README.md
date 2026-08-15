# Kotonoha

Kotonoha is a Linux desktop lyrics overlay. It reads the current track and playback position from any MPRIS player, then shows synchronized lyrics in a translucent Wayland overlay.

It works with browsers, Spotify, VLC, mpv, Cider, and other MPRIS-compatible players. Lyrics can come from Netease, lrclib, Kugou, or the optional Cider probe.

![Kotonoha lyrics overlay](screenshots/kotonoha-screenshot.png)

> **Icon credit:** Special thanks to [Zakkaus](https://github.com/Zakkaus) for designing Kotonoha's icon.

## Features

- Any MPRIS player through D-Bus; no player-specific plugin is required.
- Word-by-word karaoke highlighting, translation, and smooth playback interpolation.
- Multiple lyric sources with configurable order, matching, fallback, and local cache.
- Wayland layer-shell overlay with click-through mode, dragging, translucency, and blur.
- Settings and system tray controls for fonts, colors, position, opacity, icons, and language.

## Installation

### Release packages

Download the latest artifacts from [GitHub Releases](https://github.com/locez/kotonoha/releases).

- Debian/Ubuntu: `sudo apt install ./kotonoha_*.deb`
- Fedora: `sudo dnf install ./kotonoha-*.rpm`
- Arch Linux: `paru -S kotonoha-git`

For Gentoo, enable the [gentoo-zh overlay](https://github.com/gentoo-zh/overlay):

```bash
sudo eselect repository enable gentoo-zh
sudo emaint sync
sudo emerge --ask media-plugins/kotonoha::gentoo-zh
```

Start the installed application with:

```bash
kotonoha
```

### Linux wheel

The release wheel is for Linux x86_64 and still needs compatible system Qt, Wayland, and LayerShellQt runtime libraries. Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) first:

```bash
python3 -m venv .venv
uv pip install --python .venv/bin/python ./kotonoha-*-linux_x86_64.whl
.venv/bin/kotonoha
```

### From source

Install the system dependencies first. `uv sync` then builds Kotonoha's native Wayland bridge automatically.

```bash
# Arch
sudo pacman -S cmake qt6-base qt6-wayland layer-shell-qt

# Fedora
sudo dnf install cmake qt6-qtbase-devel layer-shell-qt-devel wayland-devel gcc-c++

# Debian/Ubuntu
sudo apt install cmake build-essential pkg-config qt6-base-dev qt6-base-private-dev qt6-wayland-dev libwayland-dev liblayershellqtinterface-dev

# Gentoo
sudo emerge -a dev-build/cmake kde-plasma/layer-shell-qt dev-qt/qtwayland
```

Then install and run Kotonoha:

```bash
git clone https://github.com/locez/kotonoha.git
cd kotonoha
uv sync
uv run kotonoha
```

## Before you start

- Floating above fullscreen requires a compositor that implements `wlr-layer-shell`, such as KDE/KWin or a wlroots-based compositor. GNOME/Mutter falls back to a normal top-most window.
- Browser players expose MPRIS through extensions such as [Plasma Browser Integration](https://github.com/KDE/plasma-browser-integration) and/or `playerctld`.

## Configuration

Open **Settings** from the tray. Under **Sources**, providers can be reordered or disabled. The default order is `netease -> lrclib -> kugou -> cider`.

**Prefer best match** is enabled by default: cached results and matching Cider snapshots are considered first, then network sources compete by match quality. Disable it for strict ordered fallback.

Settings also controls fonts, colors, opacity, position, translation, icons, panel style, and lyric effects.

**Furigana** (hiragana readings above Japanese kanji) is an opt-in display feature. It uses either the system `mecab` analyzer (preferred for packaged installs — add it via your package manager, e.g. `dnf install mecab`, and optionally drop a UniDic dictionary at `~/.local/share/kotonoha/unidic/` or set `KOTONOHA_UNIDIC_DIR`), or the optional Python `furigana` extra (`fugashi` + `unidic-lite`) as a fallback. When no analyzer/dictionary is available, lyrics simply don't show readings and the Settings screen prints an install hint.

## Cider plugin (optional)

The Cider integration is experimental and depends on Cider's internal APIs and Apple Music's TTML endpoint. Keep an external lyric source enabled as a fallback.

Install the plugin from a release ZIP:

```bash
install -d ~/.config/sh.cider.genten/plugins
unzip -o kotonoha-cider-lyrics-*.zip -d ~/.config/sh.cider.genten/plugins
```

Or build it from source:

```bash
cd plugins/cider/lyrics
pnpm install
pnpm build
install -d ~/.config/sh.cider.genten/plugins/dev.locez.kotonoha.cider.lyrics
cp dist/dev.locez.kotonoha.cider.lyrics/plugin.js \
  ~/.config/sh.cider.genten/plugins/dev.locez.kotonoha.cider.lyrics/plugin.js
cp dist/dev.locez.kotonoha.cider.lyrics/plugin.yml \
  ~/.config/sh.cider.genten/plugins/dev.locez.kotonoha.cider.lyrics/plugin.yml
```

Reload Cider after installing the plugin. `pnpm test` runs the plugin tests.
