from typing import cast

from kotonoha.config import (
    DEFAULT_LYRICS_SOURCES,
    TRACK_OFFSET_CAP,
    VALID_LYRICS_SOURCES,
    Config,
    load_config,
    save_config,
    set_track_offset,
)


def test_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(port=30000, anchor_top=False, font_size=40, show_translation=False)
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.port == 30000
    assert loaded.anchor_top is False
    assert loaded.font_size == 40
    assert loaded.show_translation is False


def test_qqmusic_is_known_but_not_default():
    assert "qqmusic" in VALID_LYRICS_SOURCES
    assert "qqmusic" not in DEFAULT_LYRICS_SOURCES
    assert Config.from_dict({"lyrics_sources": ["qqmusic"]}).lyrics_sources == ["qqmusic"]


def test_screen_name_roundtrips(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(screen_name="DP-1"), path)
    assert load_config(path).screen_name == "DP-1"


def test_player_lock_roundtrips_and_clamps():
    assert Config().player_lock == ""
    assert Config(player_lock="org.mpris.MediaPlayer2.youtube").clamped().player_lock == (
        "org.mpris.MediaPlayer2.youtube"
    )
    assert Config.from_dict({"player_lock": 123}).player_lock == ""


def test_frost_panel_style_survives_clamp():
    assert Config(panel_style="frost").clamped().panel_style == "frost"
    assert Config(panel_style="bogus").clamped().panel_style == "pill"


def test_panel_accent_tint_roundtrips(tmp_path):
    path = tmp_path / "c.json"
    save_config(Config(panel_accent_tint=True), path)
    assert load_config(path).panel_accent_tint is True


def test_lyrics_script_clamps_unknown_to_off():
    assert Config(lyrics_script="zh-Hant").clamped().lyrics_script == "zh-Hant"
    assert Config(lyrics_script="bogus").clamped().lyrics_script == "off"


def test_current_line_only_roundtrips_and_coerces(tmp_path):
    path = tmp_path / "c.json"
    save_config(Config(current_line_only=True), path)
    assert load_config(path).current_line_only is True
    assert Config.from_dict({"current_line_only": 0}).current_line_only is False
    assert Config.from_dict({"current_line_only": 1}).current_line_only is True


def test_typography_and_panel_size_roundtrip(tmp_path):
    path = tmp_path / "c.json"
    cfg = Config(
        font_family="Noto Sans CJK SC",
        context_font_size=18, translation_font_size=11,
        panel_width_mode="fixed", panel_width=880,
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.font_family == "Noto Sans CJK SC"
    assert loaded.context_font_size == 18
    assert loaded.translation_font_size == 11
    assert loaded.panel_width_mode == "fixed"
    assert loaded.panel_width == 880


def test_typography_and_panel_size_defaults_and_clamps():
    # New keys default sanely and coerce out-of-range values.
    assert Config().panel_width_mode == "fit"
    assert Config(context_font_size=1).clamped().context_font_size == 8
    assert Config(panel_width=99999).clamped().panel_width == 2400
    assert Config(panel_width_mode="bogus").clamped().panel_width_mode == "fit"


def test_all_font_sizes_clamp_to_the_spin_box_range():
    # All three sizes clamp to 8..120 — the same range the Appearance spin boxes
    # offer — so opening Settings and pressing Apply can never truncate them.
    assert Config(font_size=999).clamped().font_size == 120
    assert Config(context_font_size=200).clamped().context_font_size == 120
    assert Config(translation_font_size=200).clamped().translation_font_size == 120


def test_effects_defaults_clamp_and_roundtrip(tmp_path):
    # Calm defaults: animations on, glow / word-pop off.
    assert Config().fx_animate is True
    assert Config().fx_glow is False
    assert Config().fx_word_pop is False
    assert Config().fx_intensity == "subtle"
    assert Config(fx_intensity="expressive").clamped().fx_intensity == "expressive"
    assert Config(fx_intensity="bogus").clamped().fx_intensity == "subtle"
    # Line-change transition: "rise" default, known values kept, junk falls back.
    assert Config().fx_transition == "rise"
    assert Config(fx_transition="zoom").clamped().fx_transition == "zoom"
    assert Config(fx_transition="bogus").clamped().fx_transition == "rise"
    # Fuzzy matching: on by default, coerced to bool.
    assert Config().fuzzy_match is True
    assert Config(fuzzy_match=cast(bool, 0)).clamped().fuzzy_match is False
    # Settings-window opacity: a touch see-through by default, clamped to 0.0..1.0.
    assert Config().settings_opacity == 0.95
    assert Config(settings_opacity=2.0).clamped().settings_opacity == 1.0
    assert Config(settings_opacity=-0.5).clamped().settings_opacity == 0.0
    path = tmp_path / "c.json"
    save_config(Config(fx_animate=False, fx_glow=False, fx_word_pop=False, fx_intensity="expressive"), path)
    loaded = load_config(path)
    assert not loaded.fx_animate and not loaded.fx_glow and not loaded.fx_word_pop
    assert loaded.fx_intensity == "expressive"


def test_theme_and_white_panel_clamp_and_roundtrip(tmp_path):
    assert Config().theme == "auto"
    assert Config(theme="light").clamped().theme == "light"
    assert Config(theme="bogus").clamped().theme == "auto"
    assert Config(panel_style="white").clamped().panel_style == "white"
    path = tmp_path / "c.json"
    save_config(Config(theme="dark", panel_style="white"), path)
    loaded = load_config(path)
    assert loaded.theme == "dark"
    assert loaded.panel_style == "white"


def test_frost_window_defaults_and_roundtrips(tmp_path):
    assert Config().frost_window is True
    assert Config(frost_window=cast(bool, 0)).clamped().frost_window is False  # coerced to bool
    path = tmp_path / "c.json"
    save_config(Config(frost_window=False), path)
    assert load_config(path).frost_window is False


def test_frost_opacity_and_full_transparency(tmp_path):
    path = tmp_path / "c.json"
    save_config(Config(opacity=0.0, frost_opacity=0.35), path)
    loaded = load_config(path)
    assert loaded.opacity == 0.0  # black panel may now be fully transparent
    assert loaded.frost_opacity == 0.35


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.json")
    assert cfg == Config()


def test_invalid_json_returns_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    assert load_config(path) == Config()


def test_unknown_keys_ignored_and_defaults_filled():
    cfg = Config.from_dict({"port": 40000, "totally_unknown": 5})
    assert cfg.port == 40000
    assert cfg.karaoke is True  # default preserved


def test_clamping():
    assert Config(port=99999).clamped().port == 65535  # clamped to max, not reset
    assert Config(opacity=5.0).clamped().opacity == 1.0
    assert Config(opacity=-1.0).clamped().opacity == 0.0  # 0..1 now (fully transparent allowed)
    assert Config(opacity=0.0).clamped().opacity == 0.0
    assert Config(font_size=1).clamped().font_size == 8
    assert Config(panel_style="weird").clamped().panel_style == "pill"


def test_from_dict_non_dict():
    assert Config.from_dict("nope") == Config()
    assert Config.from_dict(None) == Config()


def test_cache_enabled_defaults_true_and_roundtrips(tmp_path):
    assert Config().cache_enabled is True
    path = tmp_path / "config.json"
    save_config(Config(cache_enabled=False), path)
    assert load_config(path).cache_enabled is False


def test_cache_enabled_is_clamped_to_bool():
    assert Config.from_dict({"cache_enabled": 0}).cache_enabled is False
    assert Config.from_dict({"cache_enabled": 1}).cache_enabled is True


def test_icon_name_roundtrips_and_rejects_paths(tmp_path):
    path = tmp_path / "config.json"
    save_config(Config(icon_name="leaf-pink.svg"), path)
    assert load_config(path).icon_name == "leaf-pink.svg"
    assert Config.from_dict({"icon_name": "../outside.svg"}).icon_name == "default"


def test_every_lyric_source_has_a_display_name_in_every_language():
    # Adding a source without its string leaves the settings list showing the raw
    # key, e.g. "src.qqmusic". This is the guard for that.
    from kotonoha.strings import STRINGS

    missing = []
    for source in VALID_LYRICS_SOURCES:
        entry = STRINGS.get(f"src.{source}")
        if entry is None:
            missing.append(f"src.{source} (no entry)")
            continue
        for language in ("en", "zh-Hans", "zh-Hant", "ja"):
            if not entry.get(language):
                missing.append(f"src.{source} [{language}]")
    assert not missing, f"lyric sources without a display name: {missing}"


def test_track_offsets_roundtrip_and_evict_oldest(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config()
    for index in range(TRACK_OFFSET_CAP + 1):
        set_track_offset(cfg, f"track-{index}", index)
    save_config(cfg, path)
    loaded = load_config(path)
    assert len(loaded.track_offsets) == TRACK_OFFSET_CAP
    assert "track-0" not in loaded.track_offsets
    assert loaded.track_offsets["track-100"] == 100


def test_track_without_offset_keeps_global_lead_only():
    cfg = Config(lead_ms=120)
    assert cfg.track_offsets.get("missing", 0) == 0
