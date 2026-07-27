"""NaviTui's animated widgets.

One shared 8fps heartbeat in the app calls `.tick()` on each of these; every
widget only repaints its own few cells, so the constant motion costs almost
nothing. Colors are read from `ricekit.palette` at render time so a theme
switch restyles every animation live.
"""

from __future__ import annotations

import math

from rich.text import Text
from textual.widgets import Static

from ricekit import icons, palette
from ricekit.widgets import NavList

from navitui import anim
from navitui.models import Song
from navitui.playqueue import Repeat

SHUFFLE_ICON = "\uf074"  # nf-fa-random
REPEAT_ICON = "\uf01e"  # nf-fa-repeat
PLAY_GLYPH = "\uf04b"  # nf-fa-play
PAUSE_GLYPH = "\uf04c"  # nf-fa-pause
SPEED_ICON = "\uf0e4"  # nf-fa-tachometer (playback speed)
SLEEP_ICON = "\uf186"  # nf-fa-moon_o (sleep timer)


class ClickList(NavList):
    """Single click highlights (previews), double click selects (acts).
    Keyboard enter still selects instantly — only the mouse path changes."""

    async def _on_click(self, event) -> None:
        clicked = event.style.meta.get("option")
        if clicked is not None and not self._options[clicked].disabled:
            self.highlighted = clicked
            if getattr(event, "chain", 1) >= 2:
                self.action_select()


class Logo(Static):
    """The NaviTui wordmark with a constant shimmer sweeping across it."""

    DEFAULT_CSS = """
    Logo { width: auto; height: 1; padding: 0 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._phase = 0.0

    def tick(self) -> None:
        self._phase += 0.55
        self.update(self.logo_text())

    def logo_text(self) -> Text:
        t = Text()
        t.append(anim.note(int(self._phase)) + " ", style=palette.mauve)
        t.append_text(anim.shimmer("NaviTui", self._phase, palette.mauve, palette.text))
        return t


class Visualizer(Static):
    """Standalone EQ bars (used in the onboarding screen for flair)."""

    DEFAULT_CSS = """
    Visualizer { width: auto; height: 1; }
    """

    def __init__(self, bars: int = 5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.model = anim.VizModel(bars)

    def tick(self) -> None:
        self.model.tick()
        self.update(self.model.render())


class NowPlaying(Static):
    """The two-line transport: viz + title marquee + star on top, smooth
    progress bar with times, volume gauge and mode toggles below.

    Click the bar to seek, click the gauge to set volume, click the
    shuffle/repeat glyphs to toggle them (they route through app actions).
    """

    DEFAULT_CSS = """
    NowPlaying {
        height: 3;
        padding: 0 1;
        background: transparent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.song: Song | None = None
        self.playing = False
        self.position = 0.0
        self.duration = 0.0
        self.volume = 100
        self.muted = False
        self.shuffle = False
        self.repeat = Repeat.OFF
        self.speed = 1.0
        self.sleep_label = ""
        self.viz = anim.VizModel(5, seed=7)
        self._tick = 0
        self._title_flash = 0
        self._vol_flash = 0
        self._speed_flash = 0
        self._bar_span: tuple[int, int] = (0, 0)
        self._gauge_span: tuple[int, int] = (0, 0)
        self._mode_spans: dict[str, tuple[int, int]] = {}

    # ── state from the app ────────────────────────────────────────────
    def set_song(self, song: Song | None) -> None:
        if song is not None and (self.song is None or song.id != self.song.id):
            self._title_flash = 12
        self.song = song
        if song is None:
            self.position = 0.0
            self.duration = 0.0

    def set_progress(self, position: float, duration: float) -> None:
        self.position = position
        if duration > 0:
            self.duration = duration

    def set_playing(self, playing: bool) -> None:
        self.playing = playing

    def flash_volume(self) -> None:
        self._vol_flash = 10

    def flash_speed(self) -> None:
        self._speed_flash = 10

    def tick(self, level: float | None = None) -> None:
        self._tick += 1
        self.viz.energy = 1.0 if self.playing else 0.0
        self.viz.tick(level if self.playing else None)
        if self._title_flash > 0:
            self._title_flash -= 1
        if self._vol_flash > 0:
            self._vol_flash -= 1
        if self._speed_flash > 0:
            self._speed_flash -= 1
        self.update(self._render_lines())

    # ── drawing ───────────────────────────────────────────────────────
    def _render_lines(self) -> Text:
        width = max(20, self.content_size.width)
        return Text("\n").join([self._line_top(width), self._line_bottom(width)])

    def _line_top(self, width: int) -> Text:
        line = Text()
        line.append_text(self.viz.render())
        line.append("  ")
        if self.song is None:
            line.append("nothing playing", style=palette.dim)
            line.append("  ·  press ", style=palette.vfaint)
            line.append("enter", style=palette.dim)
            line.append(" on a track", style=palette.vfaint)
            return line
        star = f" {icons.STAR}" if self.song.starred else ""
        flash = self._title_flash / 12
        title_color = anim.blend(palette.text, "#ffffff", 0.7 * flash)
        room = width - line.cell_len - len(star) - 1
        body = f"{self.song.title}  —  {self.song.artist} · {self.song.album}"
        line.append(anim.marquee(body, max(8, room), self._tick // 2), style=f"bold {title_color}")
        if star:
            line.append(star, style=palette.yellow)
        return line

    def _line_bottom(self, width: int) -> Text:
        elapsed = anim.fmt_time(self.position)
        total = anim.fmt_time(self.duration or (self.song.duration if self.song else 0))
        times = f" {elapsed} / {total} "

        right = Text("  ")
        if self.shuffle:
            right.append(f"{SHUFFLE_ICON} ", style=palette.peach)
        rep_style = palette.peach if self.repeat is not Repeat.OFF else ""
        if self.repeat is Repeat.ONE:
            right.append(f"{REPEAT_ICON}\u00b9 ", style=rep_style)
        elif self.repeat is Repeat.ALL:
            right.append(f"{REPEAT_ICON} ", style=rep_style)
        if abs(self.speed - 1.0) > 1e-3:
            right.append(f"{SPEED_ICON}{self.speed:g}x ", style=palette.mauve)
        if self.sleep_label:
            right.append(f"{SLEEP_ICON}{self.sleep_label} ", style=palette.lav)
        vol_frac = 0.0 if self.muted else self.volume / 100
        right.append("vol ", style=palette.vfaint)
        gauge = anim.mini_gauge(vol_frac, 4)
        if self._vol_flash > 0 and anim.can_blend():
            gauge.stylize(anim.blend(palette.lav, "#ffffff", self._vol_flash / 14))
        right.append_text(gauge)
        right.append(f" {self.volume:>2d}", style=palette.red if self.muted else palette.dim)

        bar_width = max(4, width - len(times) - right.cell_len)
        frac = self.position / self.duration if self.duration > 0 else 0.0
        pulse = (math.sin(self._tick * 0.55) + 1) / 2 if self.playing else 0.0
        line = Text()
        line.append_text(anim.smooth_bar(frac, bar_width, head_pulse=pulse))
        line.append(times, style=palette.vfaint)
        base = line.cell_len
        line.append_text(right)

        self._bar_span = (0, bar_width)
        return line

    # ── mouse ─────────────────────────────────────────────────────────
    def on_click(self, event) -> None:
        content = event.get_content_offset(self)
        if content is None:
            return
        x, y = content
        if y != 1:
            return
        b0, b1 = self._bar_span
        if b0 <= x < b1 and b1 > b0:
            self.app.seek_fraction((x - b0 + 0.5) / (b1 - b0))
            return
