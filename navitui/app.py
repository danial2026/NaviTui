"""NaviTui — the app.

Songs-first: one sidebar of ways-to-list-tracks (views + playlists), one big
tracks pane, cover + queue on the right. No tabs, no album browsing.

Cache-first everywhere: every pane renders from the last-known JSON cache
instantly, then a worker fetches fresh rows and swaps them in silently.
One 8fps heartbeat drives every animation (logo shimmer, visualizer,
progress pulse, marquee, spinners); each tick repaints only a few cells.
"""

from __future__ import annotations

import asyncio
import random
import time

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from ricekit import KitApp, icons, palette
from ricekit.modals import PickerModal
from ricekit.storage import AppDirs
from ricekit.widgets import NavList, Splitter

from navitui import anim, artcolor, config as configmod, player as playermod
from navitui.api import SubsonicClient, SubsonicError
from navitui.art import CoverArt
from navitui.integrations import DiscordPresence, ListenBrainz, Notifier
from navitui.models import Artist, Playlist, Song
from navitui.nowplaying import create_nowplaying
from navitui import mutations as mutations_mod
from navitui.mutations import MutationQueue
from navitui.palette import NaviTuiCommands
from navitui.playqueue import PlayQueue
from navitui.remote import Remote, build_snapshot
from navitui.screens import (
    AudioDeviceSwitcherModal,
    EqualizerModal,
    InputModal,
    NaviTuiHelpModal,
    OnboardingScreen,
    ServerSwitcherModal,
    StatsModal,
)
from navitui.stats import StatsStore
from navitui import stats as statsmod
from navitui.widgets import ClickList, Logo, NowPlaying, PAUSE_GLYPH, PLAY_GLYPH

# read once at import so the bindings table below can be built from it;
# remapping a key is an edit to player.toml + restart
CONFIG = configmod.load(AppDirs("navitui").config_file.parent)

# nf-fa-bookmark — as a \uXXXX escape (raw PUA glyphs don't survive patching)
BOOKMARK_GLYPH = "\uf02e"

def _kb(action_id: str, action: str, description: str = "", show: bool = False) -> Binding:
    return Binding(CONFIG["keybinds"][action_id], action, description, show=show)

VIEWS = [
    ("all-songs", "all tracks"),
    ("newest", "recently added"),
    ("recent", "recently played"),
    ("frequent", "most played"),
    ("starred", "starred"),
    ("shuffle-all", "shuffle everything"),
]
VIEW_LABELS = dict(VIEWS)

# playback speeds cycled by the speed action — 1.0 first so a fresh press
# reads as "no change yet" only after it wraps back
SPEED_STEPS = [1.0, 1.25, 1.5, 1.75, 2.0, 0.75]
# sleep-timer presets: minutes, or 0 for off, or -1 for "stop at end of the
# current track". Cycled by the sleep action; index 0 is always off.
SLEEP_PRESETS = [0, 15, 30, 45, 60, -1]

# Runtime streaming-quality presets (label, kbps cap, format). 0 kbps + "" is
# original/untranscoded. Cycled with the quality keybind; the chosen cap only
# affects streams started afterwards — a playing track keeps its URL.
QUALITY_PRESETS = [
    ("original", 0, ""),
    ("320 kbps", 320, "mp3"),
    ("192 kbps", 192, "mp3"),
    ("96 kbps", 96, "opus"),
]

# Keys and descriptions are kept short on purpose: NaviTuiHelpModal is a
# fixed-width box that pads the key column to the widest key, so a long key or
# description wraps and breaks the columns. Widest key here is "[count] j/k".
HELP_SECTIONS = [
    (
        "playback",
        [
            ("space", "play / pause"),
            ("enter", "play track / view / playlist"),
            ("n / b", "next / previous track"),
            ("← / →", "seek 5s  (shift: 30s)"),
            ("- / +", "volume down / up"),
            ("m", "mute"),
            (">", "cycle playback speed"),
            ("<", "sleep timer  (off→15→…→end)"),
            ("s", "toggle shuffle"),
            ("r", "cycle repeat  (off→all→one)"),
        ],
    ),
    (
        "queue",
        [
            ("a", "add to queue"),
            ("A", "play next"),
            ("x", "remove (in queue panel)"),
            ("ctrl+↑/↓", "move track up / down"),
            ("X", "clear queue"),
            ("ctrl+s", "save queue as a playlist"),
            ("", "played tracks dim — scroll up"),
        ],
    ),
    (
        "config & desktop",
        [
            ("N", "toggle notifications"),

            ("", "media keys via MPRIS / macOS / Windows"),
            ("", "config: player.toml — keybinds,"),
            ("", "replaygain, gapless, bitrate…"),
        ],
    ),
    (
        "library",
        [
            ("j/k/g/G", "move in lists"),
            ("[count] j/k", "repeat motion (3j = down 3)"),
            ("h / l", "previous / next panel"),
            ("d / D", "download track / view"),
            ("ctrl+d", "download whole library"),
            ("O", "offline mode"),
            ("Q", "cycle stream quality"),
        ],
    ),
    (
        "app",
        [
            ("t", "cycle kit themes"),
            ("T", "theme picker (live preview)"),
            ("ctrl+w", "listening stats / wrapped"),
            ("z", "zen / now-playing splash"),
            ("ctrl+e", "equalizer"),
            ("ctrl+o", "audio device"),
            ("ctrl+g", "switch server"),
            ("ctrl+p", "command palette"),
            ("?", "this help"),
            ("q", "quit"),
        ],
    ),
]

class NaviTuiApp(KitApp):
    TITLE = "NaviTui"

    # our verb list on top of Textual's built-in system commands (never
    # instead of them), so the palette lists NaviTui's actions and the
    # standard theme/screenshot/quit system entries together
    COMMANDS = KitApp.COMMANDS | {NaviTuiCommands}

    BINDINGS = [
        _kb("play_pause", "play_pause", "play/pause", show=True),
        _kb("next_track", "next_track", "next", show=True),
        _kb("prev_track", "prev_track"),
        _kb("shuffle", "toggle_shuffle", "shuffle", show=True),
        _kb("repeat", "cycle_repeat", "repeat", show=True),
                _kb("seek_back", "seek(-5)"),
        _kb("seek_forward", "seek(5)"),
        _kb("seek_back_big", "seek(-30)"),
        _kb("seek_forward_big", "seek(30)"),
        _kb("volume_down", "volume(-5)"),
        _kb("volume_up", "volume(5)"),
        _kb("mute", "mute"),
        _kb("speed", "cycle_speed"),
        _kb("sleep_timer", "cycle_sleep"),
        _kb("enqueue", "enqueue(False)"),
        _kb("play_next", "enqueue(True)"),
        _kb("queue_remove", "queue_remove"),
        _kb("queue_clear", "queue_clear"),
        _kb("queue_move_up", "queue_move(-1)"),
        _kb("queue_move_down", "queue_move(1)"),
                                        _kb("download", "download"),
        _kb("download_view", "download_view"),
        _kb("download_all", "download_all"),
        _kb("offline_toggle", "toggle_offline"),
        _kb("quality_cycle", "cycle_quality"),

                                                        _kb("queue_save", "queue_save"),
        _kb("stats", "stats"),
                                                                _kb("notifications", "toggle_notifications"),
        _kb("panel_prev", "focus_panel(-1)"),
        _kb("panel_next", "focus_panel(1)"),
                _kb("theme_cycle", "cycle_kit_theme", "theme", show=True),
        _kb("theme_pick", "change_theme"),
        _kb("zen", "toggle_zen", "zen", show=True),
        _kb("equalizer", "show_equalizer"),
        _kb("audio_device", "switch_audio_device"),
        _kb("server_switch", "switch_server"),
                # explicit so it's remappable via player.toml; Textual would otherwise
        # auto-bind ctrl+p. Naming the binding `command_palette` also stops the
        # auto-bind from doubling up.
        _kb("command_palette", "command_palette", "palette"),
        _kb("help", "help", "help", show=True),
        _kb("quit", "quit", "quit", show=True),
    ]

    CSS = """
    #topbar { height: 1; padding: 0 1; }
    #topbar #status { width: 1fr; text-align: right; }

    #main { height: 1fr; }
    NavList { text-wrap: nowrap; text-overflow: ellipsis; }
    .panel {
        border: round $kit-border;
        padding: 0 1;
    }
    .panel:focus-within { border: round $kit-border-focus; }
    .panel NavList { height: 1fr; }
    #sidebar-panel { width: 26; }
    #tracks-panel { width: 1fr; }
    #pl-header { display: none; height: auto; padding: 0 1 1 1; }
    #pl-art { height: 8; width: 8; border: none; background: transparent; align: center middle; content-align: center middle; }
    #pl-art > .cover-image { width: auto; height: auto; max-width: 100%; max-height: 100%; }
    #pl-art > #pl-placeholder { width: 100%; height: 100%; content-align: center middle; }
    #pl-info { height: auto; width: 1fr; padding: 0 1; content-align: left middle; }
    #side { width: 34; }
    #art-panel {
        height: 40%; min-height: 10;
        border: none;
        background: transparent;
    }
    #queue-panel { height: 1fr; border: round $kit-border; }

    NowPlaying.playing { }

    #zen-info { display: none; }
    #zen-progress { display: none; }
    #zen-viz { display: none; }

    /* zen / now-playing splash: fullscreen centered cover + info */
    .zen #sidebar-panel, .zen #split1, .zen #tracks-panel,
    .zen #split2, .zen #queue-panel, .zen #topbar,
    .zen #now, .zen Footer { display: none; }
    .zen #main { align: center middle; }
    .zen #side {
        width: 1fr;
        height: 1fr;
        align: center middle;
        content-align: center middle;
        background: transparent;
    }
    .zen #art-panel {
        width: 48; height: 24;
        min-width: 48; min-height: 24;
        border: none;
        background: transparent;
        content-align: center middle;
    }
    .zen #zen-viz {
        display: block; height: 1; width: 100%;
        content-align: center middle;
    }
    .zen #zen-progress {
        display: block; height: 1; width: 100%;
        content-align: center middle;
    }
    .zen #zen-info {
        display: block; height: auto; width: 100%;
        background: transparent;
        content-align: center middle;
    }
    """

    def __init__(self, client: SubsonicClient | None = None, ao: str | None = None) -> None:
        super().__init__()
        self.dirs = AppDirs("navitui")
        # pending library writes to replay when the server comes back (or when
        # offline mode is switched off); survives restart via the JSON cache
        self.mutations = MutationQueue(
            lambda: self.dirs.read_cache(mutations_mod.CACHE_KEY),
            lambda data: self.dirs.write_cache(mutations_mod.CACHE_KEY, data),
        )
        self.client: SubsonicClient | None = client
        self._ao = ao
        # local, offline listening stats — one JSONL append per confirmed play
        self.stats = StatsStore(self.dirs.cache_dir)
        self.queue = PlayQueue()
        self.player = None
        self.view: str = "all-songs"  # sidebar view id (or "pl:<id>", or "artist:<id>")
        self._songs: list[Song] = []  # the full tracks-pane model
        self._playlists: list[Playlist] = []
        # vim repeat count: digits armed by the previous keystroke, consumed by
        # the next motion (see _handle_count). Never spans more than the very
        # next key — no timer keeps it alive.
        self._count = ""
        # playback bookkeeping
        self._scrobbled = False
        self._end_failures = 0
        self._resume_position = 0.0
        # sleep timer: an index into SLEEP_PRESETS. _sleep_deadline is a
        # monotonic timestamp (checked inside the heartbeat, no extra timer);
        # None means "off" or the special "stop at end of current track" mode.
        self._sleep_idx = 0
        self._sleep_deadline: float | None = None
        self._mutations = 0
        self._last_persist = 0.0
        self._queue_scrolled_to = -2
        self._zen = False
        self._offline = False  # play/browse only what's pinned; skip the network

        self._dl_total = 0
        self._dl_done = 0
        self._dl_failed = 0
        self._crossfade = max(0.0, float(CONFIG["crossfade"]))  # soft-fade seconds
        self._fade_base: int | None = None  # user volume captured across an active fade
        self._prefetched: str | None = None  # song id we last warmed, to dedup

    # ── layout ────────────────────────────────────────────────────────
    def compose(self):
        with Horizontal(id="topbar"):
            yield Logo(id="logo")
            yield Static(id="status")
        with Horizontal(id="main"):
            with Vertical(id="sidebar-panel", classes="panel"):
                yield ClickList(id="sidebar-list")
            yield Splitter("#sidebar-panel", on_resized=self._persist_width, id="split1")
            with Vertical(id="tracks-panel", classes="panel"):
                with Horizontal(id="pl-header"):
                    with Vertical(id="pl-art"):
                        yield Static("♪", id="pl-placeholder")
                    yield Static(id="pl-info")
                yield ClickList(id="tracks-list")
            yield Splitter("#side", invert=True, on_resized=self._persist_width, id="split2")
            with Vertical(id="side"):
                yield CoverArt(id="art-panel")
                yield Static(id="zen-viz")
                yield Static(id="zen-progress")
                yield Static(id="zen-info")
                with Vertical(id="queue-panel", classes="panel"):
                    yield ClickList(id="queue-list")
        yield NowPlaying(id="now")
        yield Footer()

    def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()  # for mpv-thread callbacks
        state = self.dirs.load_state()
        self.init_kit(theme=state.get("theme"))

        for selector, width in (state.get("widths") or {}).items():
            try:
                self.query_one(selector).styles.width = width
            except Exception:
                pass

        self.query_one("#art-panel", CoverArt).border_title = ""
        self.query_one("#queue-panel").border_title = ""
        saved_view = state.get("view", "all-songs")
        if (
            saved_view in VIEW_LABELS
            or saved_view.startswith("pl:")
        ):
            self.view = saved_view
        elif saved_view == "home":
            self.view = "all-songs"  # Home was turned off since last launch
        # offline mode is session-only: always start online so a stray `O`
        # toggle can't silently strand every future launch offline
        self._offline = False
        # radio state was here (removed)

        configmod.write_template(self.dirs.config_file.parent)
        self.notifier = Notifier(bool(CONFIG["notifications"]))
        self.discord = DiscordPresence(
            bool(CONFIG["discord_rich_presence"]), str(CONFIG["discord_app_id"])
        )
        self.listenbrainz = ListenBrainz(str(CONFIG["listenbrainz_token"]))
        self.mpris = create_nowplaying()  # MPRIS on linux, MPNowPlaying(mac)/SMTC(win) elsewhere
        self.remote = Remote()

        self.player = self._make_player()
        self.player.set_volume(int(state.get("volume", 80)))
        self._apply_audio_settings()  # restore saved EQ + output device
        now = self.query_one("#now", NowPlaying)
        now.volume = self.player.volume
        now.speed = self.player.set_speed(float(state.get("speed", 1.0)))

        # restore the queue exactly as it was left
        cached_queue = self.dirs.read_cache("queue")
        if cached_queue:
            self.queue = PlayQueue.from_dict(cached_queue)
            self._resume_position = float(cached_queue.get("position", 0.0))
            now.set_song(self.queue.current)
            now.set_progress(self._resume_position, self.queue.current.duration if self.queue.current else 0)
            now._title_flash = 0
        now.shuffle = self.queue.shuffle
        now.repeat = self.queue.repeat
        self._render_queue()

        self.set_interval(1 / 8, self._heartbeat)
        self.set_interval(180, self._auto_refresh)

        # local control API — transport + state work without a server
        # connection, so start it here (not in _start) regardless of onboarding
        self.run_worker(self._start_remote(), group="remote")

        if not playermod.MPV_AVAILABLE:
            self.notify(playermod.INSTALL_HINTS, severity="warning", timeout=15)

        if self.client is None:
            profiles = self._profiles()
            if profiles:
                active = self.dirs.load_state().get("active_profile", "")
                if active not in profiles:
                    active = next(iter(profiles))
                    self.dirs.save_state({"active_profile": active})
                creds = profiles[active]
                self.client = SubsonicClient(
                    creds["server"], creds["username"], creds["token"], creds["salt"],
                    art_dir=self.dirs.cache_dir / "art",
                    audio_dir=self.dirs.cache_dir / "audio",
                    max_bitrate=int(CONFIG["max_bitrate"]),
                    stream_format=str(CONFIG["stream_format"]),
                )
            else:
                self.push_screen(OnboardingScreen(), self._onboarded)
                return
        self._start()

    def _make_player(self):
        return playermod.create_player(
            self._mpv_position,
            self._mpv_track_end,
            ao=self._ao,
            replaygain=str(CONFIG["replaygain"]),
            gapless=str(CONFIG["gapless"]),
            replaygain_preamp=float(CONFIG["replaygain_preamp"]),
            replaygain_fallback=float(CONFIG["replaygain_fallback"]),
            audio_exclusive=bool(CONFIG["audio_exclusive"]),
            pipewire_buffer=int(CONFIG["pipewire_buffer"]),
        )

    def _eq_state(self) -> dict:
        st = self.dirs.load_state().get("equalizer")
        eq = dict(CONFIG["equalizer"])
        if isinstance(st, dict):
            for k in ("enabled", "preset", "bands"):
                if k in st:
                    eq[k] = st[k]
        return eq

    def _apply_audio_settings(self) -> None:
        """Push the saved EQ + output device onto the (local) player. No-ops
        cleanly on a null engine, which has neither."""
        player = self.player
        if player is None:
            return
        setter = getattr(player, "set_equalizer", None)
        eq = self._eq_state()
        if setter is not None and eq.get("enabled"):
            try:
                setter(list(eq.get("bands", [])))
            except Exception:
                pass
        saved = self.dirs.load_state().get("audio_device")
        lister = getattr(player, "get_audio_devices", None)
        apply = getattr(player, "set_audio_device", None)
        if saved and lister is not None and apply is not None:
            try:
                # smart-match the saved name across driver/bluetooth renames:
                # match on the distinctive tail (minus a dynamic dotted suffix)
                target = saved
                distinctive = saved.split("/", 1)[-1] if "/" in saved else saved
                base = distinctive.rsplit(".", 1)[0] if "." in distinctive else distinctive
                for dev in lister():
                    if base and base in dev.get("name", ""):
                        target = dev.get("name", "")
                        break
                apply(target)
            except Exception:
                pass

    # ── equalizer ─────────────────────────────────────────────────────
    def action_show_equalizer(self) -> None:
        if getattr(self.player, "set_equalizer", None) is None:
            self.notify("equalizer needs local playback (mpv)", timeout=3)
            return
        eq = self._eq_state()
        self.push_screen(
            EqualizerModal(bool(eq.get("enabled")), str(eq.get("preset", "flat")),
                           list(eq.get("bands", [0.0] * 10))),
            self._equalizer_saved,
        )

    def _equalizer_saved(self, result) -> None:
        if not result:
            return
        self.dirs.save_state({"equalizer": result})
        setter = getattr(self.player, "set_equalizer", None)
        if setter is not None:
            try:
                setter(result["bands"] if result.get("enabled") else [])
            except Exception:
                pass

    # ── output device ─────────────────────────────────────────────────
    def action_switch_audio_device(self) -> None:
        lister = getattr(self.player, "get_audio_devices", None)
        if lister is None:
            self.notify("device switching needs local playback (mpv)", timeout=3)
            return
        try:
            devices = lister()
        except Exception:
            devices = []
        active = ""
        getter = getattr(self.player, "get_current_audio_device", None)
        if getter is not None:
            try:
                active = getter()
            except Exception:
                active = ""
        self.push_screen(AudioDeviceSwitcherModal(devices, active), self._audio_device_picked)

    def _audio_device_picked(self, name) -> None:
        if not name:
            return
        apply = getattr(self.player, "set_audio_device", None)
        if apply is not None:
            try:
                apply(name)
            except Exception:
                pass
        self.dirs.save_state({"audio_device": name})
        self.notify(f"output → {name}", timeout=2)

    # ── multi-server switching ────────────────────────────────────────
    def _profiles(self) -> dict:
        """Saved Navidrome profiles, keyed by name, from [profiles.<name>]
        blocks in config.toml. Falls back to legacy flat keys (server, username,
        token, salt) at the top level, wrapped as profile "default"."""
        cfg = self.dirs.load_config()
        profiles = cfg.get("profiles")
        if isinstance(profiles, dict):
            return profiles
        if all(cfg.get(k) for k in ("server", "username", "token", "salt")):
            return {"default": {k: cfg[k] for k in ("server", "username", "token", "salt")}}
        return {}

    def action_switch_server(self) -> None:
        profiles = self._profiles()
        active = self.dirs.load_state().get("active_profile", "")
        self.push_screen(ServerSwitcherModal(profiles, active), self._server_picked)

    def _server_picked(self, name: str | None) -> None:
        if not name:
            return
        creds = self._profiles().get(name)
        if not isinstance(creds, dict) or not all(
            creds.get(k) for k in ("server", "username", "token", "salt")
        ):
            self.notify("that profile is missing credentials", severity="warning", timeout=4)
            return
        self.dirs.save_state({"active_profile": name})
        self.client = SubsonicClient(
            creds["server"], creds["username"], creds["token"], creds["salt"],
            art_dir=self.dirs.cache_dir / "art",
            audio_dir=self.dirs.cache_dir / "audio",
            max_bitrate=int(CONFIG["max_bitrate"]),
            stream_format=str(CONFIG["stream_format"]),
        )
        self._playlists = []
        self._render_status()
        self._render_sidebar()
        self._load_playlists()
        self._load_view(self.view)
        self.notify(f"switched to {name}", timeout=3)

    def _onboarded(self, config: dict | None) -> None:
        if not config:
            return
        creds = {k: config[k] for k in ("server", "username", "token", "salt")}
        self._save_profiles({"default": creds})
        self.dirs.save_state({"active_profile": "default"})
        self.client = SubsonicClient(
            creds["server"], creds["username"], creds["token"], creds["salt"],
            art_dir=self.dirs.cache_dir / "art",
            audio_dir=self.dirs.cache_dir / "audio",
            max_bitrate=int(CONFIG["max_bitrate"]),
            stream_format=str(CONFIG["stream_format"]),
        )
        self.notify("welcome to NaviTui ♪", timeout=4)
        self._start()

    def _save_profiles(self, profiles: dict) -> None:
        """Write a {name: {creds}} dict as [profiles.<name>] sections."""
        path = self.dirs.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for name, creds in profiles.items():
            lines.append(f"[profiles.{name}]")
            for k, v in creds.items():
                lines.append(f'{k} = "{v}"')
        path.write_text("\n".join(lines) + "\n")
        path.chmod(0o600)

    def _start(self) -> None:
        self._render_status()
        cached = self.dirs.read_cache("playlists")
        if cached:
            self._playlists = [Playlist.from_dict(p) for p in cached.get("playlists", [])]
        self._render_sidebar()
        sidebar = self.query_one("#sidebar-list", ClickList)
        sidebar.focus()
        self._highlight_view(self.view)
        self._load_playlists()
        self._flush_mutations()  # drain anything parked from a previous session
        self.run_worker(self._start_mpris(), group="mpris")

    async def _start_mpris(self) -> None:
        # dbus-fast runs on our own event loop, so media-key controls can
        # call app actions directly — no thread marshalling
        controls = {
            "play_pause": self.action_play_pause,
            "play": lambda: (self.player.set_paused(False) if self.player.active else self.action_play_pause()),
            "pause": lambda: self.player.set_paused(True),
            "stop": self._external_stop,
            "next": self.action_next_track,
            "prev": self.action_prev_track,
            "seek": self.player.seek,
            "set_position": lambda s: self.player.seek_to(s / max(1.0, self.player.duration)),
        }
        if await self.mpris.start(controls):
            self._announce()
        # notification action buttons ride the same event loop: an invoked
        # button calls straight back into an app action (falls back to a
        # buttonless notification when the dbus service isn't reachable).
        await self.notifier.start(
            {
                "previous": self.action_prev_track,
                "play-pause": self.action_play_pause,
                "next": self.action_next_track,
            }
        )

    async def _start_remote(self) -> None:
        # asyncio server on our own loop (like mpris): every handler drives an
        # existing app action. Handlers take an args dict, return a dict or
        # None. Wrapped so a startup failure can never stop the app.
        p = self.player

        def _seek(a: dict) -> None:
            if "to" in a:
                self.player.seek_to((float(a["to"]) / max(1.0, self.player.duration)))
            else:
                self.action_seek(float(a.get("delta", 0)))

        def _volume(a: dict) -> dict:
            if "set" in a:
                self.set_volume_fraction(max(0, min(130, int(a["set"]))) / 100)
            else:
                self.action_volume(int(a.get("delta", 0)))
            return {"volume": self.player.volume}

        controls = {
            "play_pause": lambda a: self.action_play_pause(),
            "play": lambda a: (p.set_paused(False) if p.active else self.action_play_pause()),
            "pause": lambda a: p.set_paused(True) or self._announce(),
            "stop": lambda a: self._external_stop(),
            "next": lambda a: self.action_next_track(),
            "prev": lambda a: self.action_prev_track(),
            "seek": _seek,
            "volume": _volume,
            "mute": lambda a: self.action_mute(),
            "shuffle": lambda a: self.action_toggle_shuffle(),
            "repeat": lambda a: self.action_cycle_repeat(),
            "enqueue": self._remote_enqueue,
        }
        try:
            ok = await self.remote.start(
                controls,
                self._remote_snapshot,
                self.dirs.cache_dir,
                token=str(CONFIG["remote_token"]),
                enabled=bool(CONFIG["remote_control"]),
            )
        except Exception:
            ok = False
        if ok:
            self.remote.publish(self._remote_snapshot())

    def _remote_snapshot(self) -> dict:
        active = bool(self.player and self.player.active)
        playing = active and not (self.player and self.player.paused)
        return build_snapshot(
            self.queue.current if active else None,
            self.queue.songs,
            self.queue.index,
            self.player.position if self.player else 0.0,
            self.player.volume if self.player else 0,
            bool(self.player and self.player.muted),
            playing,
            active,
            self.queue.shuffle,
            self.queue.repeat.value,
        )

    async def _remote_enqueue(self, a: dict) -> dict:
        """Queue a song by id (now or next)."""
        song_id = str(a.get("song_id", ""))
        if self.client is None or not song_id:
            return {"queued": False}
        song = next((s for s in self._songs if s.id == song_id), None)
        if song is None:
            song = next((s for s in self.queue.songs if s.id == song_id), None)
        if song is None:  # not on screen: resolve the id directly (getSong —
            song = await self.client.get_song(song_id)
        if song is None:
            return {"queued": False}
        if a.get("next"):
            self.queue.add_next([song])
        else:
            self.queue.add([song])
        self._render_queue()
        self._persist_queue()
        self._announce()
        return {"queued": True, "title": song.title}

    def _external_stop(self) -> None:
        self.player.stop()
        now = self.query_one("#now", NowPlaying)
        now.set_playing(False)
        self._announce()

    def _announce(self, track_change: bool = False) -> None:
        """Fan the player state out to MPRIS, Discord and (on track change)
        a desktop notification."""
        active = bool(self.player and self.player.active)
        song = self.queue.current if active else None
        playing = active and not self.player.paused
        art = None
        if song is not None and song.cover_art and self.client is not None:
            art = self.client.cached_art(song.cover_art)
        # each fan-out is isolated: one integration raising (e.g. a dbus
        # marshalling error) must never stop the others — notably it must not
        # swallow the track-change notification.
        try:
            self.mpris.update(
                song, playing,
                self.player.position if self.player else 0.0,
                self.player.volume if self.player else 100,
                str(art) if art else None,
            )
        except Exception:
            pass
        try:
            self.discord.track(
                song, playing,
                self.player.position if self.player else 0.0,
                float(song.duration) if song and song.duration else 0.0,
            )
        except Exception:
            pass
        if track_change and song is not None:
            try:
                self.notifier.track(song, art)
            except Exception:
                pass
        try:
            self.remote.publish(self._remote_snapshot())
        except Exception:
            pass

    def _render_status(self) -> None:
        if self.client is None:
            return
        host = self.client.server.split("://", 1)[-1]
        text = Text()
        if self._offline:
            text.append(f"{icons.PLUG} offline  ", style=palette.yellow)
        if self.client.max_bitrate:
            # signal glyph + cap, shown only while streaming is capped
            text.append(f"\uf012 {self.client.max_bitrate}k  ", style=palette.yellow)
        text.append(f"{self.client.username}@{host}", style=palette.dim)
        from navitui import __version__
        text.append(f"  \u00b7 v{__version__}", style=palette.vfaint)
        self.query_one("#status", Static).update(text)

    # ── the heartbeat (all constant animation) ────────────────────────
    def _heartbeat(self) -> None:
        try:
            self.query_one("#logo", Logo).tick()
            now = self.query_one("#now", NowPlaying)
            level = None
            if self.player is not None:
                poll = getattr(self.player, "poll", None)
                if poll is not None:
                    poll()
                now.set_playing(self.player.active and not self.player.paused)
                now.set_class(self.player.active, "playing")
                level = self.player.level
            # sleep-timer countdown: reuse this one heartbeat, no new timer.
            # Show mm:ss remaining and fire (pause) when the deadline passes.
            if self._sleep_deadline is not None:
                remaining = self._sleep_deadline - time.monotonic()
                if remaining <= 0:
                    self._sleep_fire()
                else:
                    now.sleep_label = anim.fmt_time(remaining)
            now.tick(level)
            if self._zen:
                self._render_zen_info()  # follow track changes in the splash
            groups = {w.group for w in self.workers if not w.is_finished}
            panel = self.query_one("#tracks-panel")
            spin = anim.spinner(int(now._tick))
            if "download" in groups:
                panel.border_subtitle = f"{spin} downloading {self._dl_done}/{self._dl_total}"
            elif groups & {"lib", "songs"}:
                panel.border_subtitle = f"{spin} refreshing"
            elif panel.border_subtitle and (
                "refreshing" in panel.border_subtitle or "downloading" in panel.border_subtitle
            ):
                count = self.query_one("#tracks-list", NavList).option_count
                panel.border_subtitle = str(count) if count else None
        except Exception:
            return  # shutdown race: the timer can fire while widgets unmount

    # ── sidebar ───────────────────────────────────────────────────────
    def _render_sidebar(self) -> None:
        ol = self.query_one("#sidebar-list", ClickList)
        highlighted_id = None
        if ol.highlighted is not None:
            highlighted_id = ol.get_option_at_index(ol.highlighted).id
        options: list[Option] = []
        for view_id, label in VIEWS:
            row = Text(no_wrap=True, overflow="ellipsis")
            if view_id == "starred":
                row.append(f" {icons.STAR}", style=palette.sub)
            elif view_id == "shuffle-all":
                row.append(" \uf074", style=palette.sub)  # nf-fa-random
            elif view_id == "newest":
                row.append(f" {icons.CLOCK}", style=palette.sub)
            elif view_id == "recent":
                row.append(f" {icons.CALENDAR}", style=palette.sub)
            elif view_id == "frequent":
                row.append(f" {icons.REFRESH}", style=palette.sub)
            else:
                row.append(f" {icons.LIST}", style=palette.sub)
            row.append(f" {label}", style=palette.text)
            options.append(Option(row, id=view_id))
        for p in self._playlists:
            row = Text(no_wrap=True, overflow="ellipsis")
            row.append(f" {icons.LIST}", style=palette.sub)
            row.append(f" {p.name}", style=palette.text)
            row.append(f" {p.song_count}\u2669", style=palette.vfaint)
            options.append(Option(row, id=f"pl:{p.id}"))
        new_row = Text(no_wrap=True)
        new_row.append(f" {icons.PLUS}", style=palette.sub)
        new_row.append(" new playlist", style=palette.sub)
        options.append(Option(new_row, id="pl-new"))
        had_focus = ol.has_focus
        ol.clear_options()
        ol.add_options(options)
        self._highlight_view(highlighted_id or self.view)
        if had_focus:
            ol.focus()

    def _highlight_view(self, view_id: str | None) -> None:
        if not view_id:
            return
        ol = self.query_one("#sidebar-list", ClickList)
        for i in range(ol.option_count):
            if ol.get_option_at_index(i).id == view_id:
                ol.highlighted = i
                return

    @on(OptionList.OptionHighlighted, "#sidebar-list")
    def _sidebar_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        oid = event.option.id
        if not oid or oid == "pl-new":
            return
        self.view = oid
        self.dirs.save_state({"view": oid})
        self._load_view(oid)

    @on(OptionList.OptionSelected, "#sidebar-list")
    def _sidebar_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if not oid:
            return
        if oid == "pl-new":
            self.push_screen(
                InputModal("new playlist", placeholder="name"),
                lambda name: self._create_empty_playlist(name) if name else None,
            )
        elif oid == "shuffle-all":
            self._shuffle_everything()
        elif self._songs:
            # enter on a view or playlist plays it from the top — but with
            # shuffle on, start somewhere random so it actually feels shuffled
            # (set_songs then keeps that pick first and randomises the rest)
            start = random.randrange(len(self._songs)) if self.queue.shuffle else 0
            self._play_songs(self._songs, start)

    @work(exclusive=True, group="lib")
    async def _load_playlists(self) -> None:
        try:
            playlists = await self.client.get_playlists()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache("playlists", {"playlists": [p.to_dict() for p in playlists]})
        self._playlists = playlists
        self._render_sidebar()

    @work(group="mutate")
    async def _create_empty_playlist(self, name: str) -> None:
        self._mutations += 1
        try:
            await self.client.create_playlist(name, [])
        except Exception as e:
            self.notify(f"couldn't create playlist: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(f"created \u201c{name}\u201d", timeout=3)
        self._load_playlists()

    # ── loading songs into the tracks pane ────────────────────────────
    @work(exclusive=True, group="songs")
    async def _load_view(self, view_id: str) -> None:
        await asyncio.sleep(0.12)
        title = self._tracks_title(view_id)

        if not view_id.startswith("pl:"):
            self._hide_pl_header()

        if view_id in ("all-songs", "shuffle-all"):
            cache_key, fetch = "all-songs", self.client.get_all_songs
        elif view_id in ("newest", "recent", "frequent"):
            cache_key = f"songview-{view_id}"

            async def fetch(v=view_id):
                return await self.client.get_songs_by_albums(v)
        elif view_id == "starred":
            cache_key = "starred-songs"

            async def fetch():
                return await self.client.get_starred()
        elif view_id.startswith("pl:"):
            pid = view_id.split(":", 1)[1]
            playlist = next((p for p in self._playlists if p.id == pid), None)
            cache_key = f"playlist-songs-{pid}"
            self._show_pl_header(playlist)
            self._load_pl_art(pid)

            async def fetch(p=pid):
                return await self.client.get_playlist_songs(p)
        else:
            return

        cached = self.dirs.read_cache(cache_key)
        if cached:
            self._show_songs([Song.from_dict(s) for s in cached.get("songs", [])], title)
        try:
            songs = await fetch()
        except Exception as e:
            self._connection_trouble(e)
            return
        self.dirs.write_cache(cache_key, {"songs": [s.to_dict() for s in songs]})
        if self.view == view_id:
            self._show_songs(songs, title)

    def _show_songs(self, songs: list[Song], title: str) -> None:
        self._songs = songs
        self.query_one("#tracks-panel").border_title = title
        self._fill("#tracks-list", [self._song_row(s, i) for i, s in enumerate(songs)])

    def _tracks_title(self, view_id: str) -> str:
        if view_id.startswith("pl:"):
            pid = view_id.split(":", 1)[1]
            playlist = next((p for p in self._playlists if p.id == pid), None)
            return playlist.name if playlist else "playlist"
        return VIEW_LABELS.get(view_id, "")

    # ── row rendering ─────────────────────────────────────────────────
    def _song_row(self, s: Song, index: int) -> Option:
        current = self.queue.current
        is_current = current is not None and s.id == current.id
        row = Text(no_wrap=True, overflow="ellipsis")
        if is_current:
            row.append(f" {PLAY_GLYPH}", style=palette.text)
        else:
            row.append("  ", style=palette.vfaint)
        row.append(f" {s.title}", style=f"bold {palette.text}" if is_current else palette.text)
        if self.client is not None and self.client.cached_stream(s.id):
            row.append(f" {icons.CHECK}", style=palette.text)
        if s.starred:
            row.append(f" {icons.STAR}", style=palette.text)
        row.append(f"  {s.artist}", style=palette.dim)
        row.append(f" · {anim.fmt_time(s.duration)}", style=palette.vfaint)
        return Option(row, id=f"trk-{index}")

    def _fill(self, selector: str, options: list[Option], subtitle_of: str | None = None) -> None:
        ol = self.query_one(selector, NavList)
        had_focus = ol.has_focus
        highlighted = ol.highlighted
        ol.clear_options()
        ol.add_options(options)
        if options:
            keep = highlighted if highlighted is not None and highlighted < len(options) else 0
            ol.highlighted = keep
        if subtitle_of:
            panel = self.query_one(subtitle_of)
            panel.border_subtitle = str(len(options)) if options else None
        if had_focus:
            ol.focus()

    # ── tracks pane ───────────────────────────────────────────────────
    def _tracks_view(self) -> list[Song]:
        return self._songs

    @on(OptionList.OptionHighlighted, "#tracks-list")
    def _track_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.player is not None and self.player.active:
            return  # while playing, the cover belongs to the current song
        view = self._tracks_view()
        i = event.option_index
        song = view[i] if i is not None and 0 <= i < len(view) else None
        if song is not None and song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")

    @on(OptionList.OptionSelected, "#tracks-list")
    def _track_selected(self, event: OptionList.OptionSelected) -> None:
        view = self._tracks_view()
        idx = event.option_index
        if idx is not None and 0 <= idx < len(view):
            self._play_songs(view, idx)

    @on(OptionList.OptionSelected, "#queue-list")
    def _queue_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.highlighted is not None:
            self._change_track(lambda i=event.option_list.highlighted: self.queue.jump(i))

    # ── type-to-filter (an explicit mode over the tracks pane) ─────────
    # The app binds many bare single letters to global actions, so we never
    # let typing narrow the list "ambiently". `\` opens filter mode; while it
    # is active `on_key` (dispatched before App._on_key, which resolves the
    # bindings) eats printable keys into the query and prevents the default
    # binding, so `s`/`f`/`a`/… type instead of shuffling/starring/queueing.
    # j/k/g/G/up/down/enter fall through untouched, so list navigation and
    # "play the highlighted match" keep working; esc restores the full list.

    # ── playback ──────────────────────────────────────────────────────
    def _shuffle_everything(self) -> None:
        if not self._songs:
            self.notify("still fetching the library — try again in a second", timeout=3)
            return
        if not self.queue.shuffle:
            self.queue.shuffle = True
            self.query_one("#now", NowPlaying).shuffle = True
            self.dirs.save_state({"shuffle": True})
        self._play_songs(self._songs, random.randrange(len(self._songs)))
        self.notify(f"shuffling all {len(self._songs)} tracks", timeout=3)

    def _play_songs(self, songs: list[Song], start: int) -> None:
        self._change_track(lambda: self.queue.set_songs(songs, start))

    def _stream_source(self, song: Song) -> str | None:
        """Where to play `song` from: a pinned local file if we have one,
        else the server stream URL. In offline mode a missing pin means the
        track is unplayable (None)."""
        local = self.client.cached_stream(song.id) if self.client else None
        if local is not None:
            return str(local)
        if self._offline:
            return None
        return self.client.stream_url(song.id) if self.client else None

    def _play_current(self, resume_at: float = 0.0) -> None:
        song = self.queue.current
        now = self.query_one("#now", NowPlaying)
        if song is None:
            self.player.stop()
            now.set_song(None)
            self._tint_from_art(None)
            self._render_queue()
            return
        source = self._stream_source(song)
        if source is None:
            # offline and not pinned — skip forward to the next playable track,
            # scanning the queue at most once so an all-unpinned queue stops
            # rather than spinning
            for _ in range(len(self.queue.songs)):
                nxt = self.queue.advance(natural=False)
                if nxt is None:
                    break
                source = self._stream_source(nxt)
                if source is not None:
                    song = nxt
                    break
            if source is None:
                self.notify("offline: nothing downloaded to play", timeout=3)
                self.player.stop()
                now.set_song(None)
                now.set_playing(False)
                self._render_queue()
                return
        set_duration = getattr(self.player, "set_duration", None)
        if set_duration is not None:
            set_duration(song.duration)
        self.player.play(source, start=resume_at)
        now.set_song(song)
        now.set_progress(resume_at, song.duration)
        self._scrobbled = False
        self._scrobble(song, False)
        if song.cover_art:
            self._load_art(song.cover_art, f"song-{song.id}")
        else:
            self._tint_from_art(None)
        self._render_queue()
        self._refresh_song_markers()
        self._persist_queue()
        self._announce(track_change=True)
        self._prefetch_next()

    def _prefetch_next(self) -> None:
        """Warm the next queued track's stream so its start is instant (and
        gapless is hardened). Peeks the queue without mutating it, dedups on the
        song id we last warmed, and skips anything already pinned or a case
        where prefetch can't help (offline mode, no client, no next track,
        repeat-one)."""
        if self.client is None or self._offline:
            return
        nxt = self.queue.peek_next()
        # peek_next returns `current` under repeat-one — nothing to warm there
        if nxt is None or (self.queue.current is not None and nxt.id == self.queue.current.id):
            return
        if nxt.id == self._prefetched or self.client.cached_stream(nxt.id):
            return
        self._prefetched = nxt.id
        self._warm_next(nxt)

    @work(exclusive=True, group="prefetch")
    async def _warm_next(self, song: Song) -> None:
        """Pin the next track to the audio cache off the UI thread. Reuses the
        offline-download path so a prefetched track is also available offline;
        stays silent (no notify) and swallows failures — it's pure speculation,
        and a miss just means the normal stream URL is used when it plays."""
        try:
            await self.client.download_song(song.id)
        except Exception:
            self._prefetched = None  # let a retry happen next time round
            return
        # the ✓ marker can now show for the freshly-pinned track
        if self.is_running:
            self._refresh_song_markers()

    def _refresh_song_markers(self) -> None:
        """Re-render the tracks pane so the ♪ marker (and stars, ✓, ratings)
        follow the player — over the filtered view when one is active."""
        self._fill("#tracks-list", [self._song_row(s, i) for i, s in enumerate(self._tracks_view())])

    def action_play_pause(self) -> None:
        if self.player.active:
            self.player.toggle_pause()
            self._announce()
        elif self.queue.current is not None:
            # resume a restored queue exactly where it left off
            self._play_current(resume_at=self._resume_position)
            self._resume_position = 0.0

    def _change_track(self, pick) -> None:
        """Manual track change (skip/prev/jump): `pick()` moves the queue and
        returns the new song. With crossfade on and audio already playing, do a
        short fade-out first, then load + fade-in; otherwise switch instantly.
        Natural EOF doesn't come through here — gapless owns that seam."""
        if self._crossfade > 0 and self.player.active:
            # a rapid re-skip cancels the in-flight fade mid-ramp, so the live
            # volume may be lowered; capture the *true* user volume once (here,
            # synchronously) and keep it in _fade_base so successive skips don't
            # ratchet it down. Cleared only when a fade lands cleanly.
            if self._fade_base is None:
                self._fade_base = self.player.volume
            self._crossfade_change(pick, self._fade_base)
            return
        song = pick()
        if song is not None:
            self._play_current()

    @work(exclusive=True, group="crossfade")
    async def _crossfade_change(self, pick, base: int) -> None:
        half = self._crossfade / 2
        await self.player.fade_out(half)
        song = pick()
        if song is None:
            self.player.set_volume(base)  # nothing to play — undo the fade
            self._fade_base = None
            return
        self._play_current()  # loads + starts the next track at low volume
        await self.player.fade_in(base, half)
        self._fade_base = None  # landed cleanly; a cancel skips this line

    def action_next_track(self) -> None:
        self._change_track(lambda: self.queue.advance(natural=False))

    def action_prev_track(self) -> None:
        if self.player.position > 4:
            self.player.seek_to(0.0)
            return
        self._change_track(self.queue.prev)

    def action_seek(self, seconds: int) -> None:
        self.player.seek(seconds)

    def seek_fraction(self, fraction: float) -> None:
        self.player.seek_to(fraction)

    def action_volume(self, delta: int) -> None:
        volume = self.player.set_volume(self.player.volume + delta)
        now = self.query_one("#now", NowPlaying)
        now.volume = volume
        now.flash_volume()
        self.dirs.save_state({"volume": volume})
        self._announce()

    def set_volume_fraction(self, fraction: float) -> None:
        self.action_volume(round(fraction * 100) - self.player.volume)

    def action_mute(self) -> None:
        now = self.query_one("#now", NowPlaying)
        now.muted = self.player.toggle_mute()
        now.flash_volume()

    # ── playback speed ────────────────────────────────────────────────
    def action_cycle_speed(self) -> None:
        """Step through common speeds — handy for podcasts and audiobooks."""
        current = self.player.speed
        idx = min(
            range(len(SPEED_STEPS)),
            key=lambda i: abs(SPEED_STEPS[i] - current),
        )
        speed = self.player.set_speed(SPEED_STEPS[(idx + 1) % len(SPEED_STEPS)])
        now = self.query_one("#now", NowPlaying)
        now.speed = speed
        now.flash_speed()
        self.dirs.save_state({"speed": speed})
        self.notify(f"speed {speed:g}x", timeout=1.5)

    # ── sleep timer ───────────────────────────────────────────────────
    def action_cycle_sleep(self) -> None:
        """Cycle off → 15 → 30 → 45 → 60 min → end of track → off. The
        deadline is checked inside the heartbeat; there is no extra timer."""
        self._sleep_idx = (self._sleep_idx + 1) % len(SLEEP_PRESETS)
        preset = SLEEP_PRESETS[self._sleep_idx]
        now = self.query_one("#now", NowPlaying)
        if preset == 0:
            self._sleep_deadline = None
            now.sleep_label = ""
            self.notify("sleep timer off", timeout=1.5)
        elif preset == -1:
            self._sleep_deadline = None  # fired from _on_track_end instead
            now.sleep_label = "end"
            self.notify("sleep: stopping at end of track", timeout=2)
        else:
            self._sleep_deadline = time.monotonic() + preset * 60
            now.sleep_label = anim.fmt_time(preset * 60)
            self.notify(f"sleep timer: {preset} min", timeout=2)

    def _sleep_fire(self) -> None:
        """Pause playback and clear the timer once a deadline passes."""
        self._sleep_idx = 0
        self._sleep_deadline = None
        now = self.query_one("#now", NowPlaying)
        now.sleep_label = ""
        if self.player is not None and self.player.active and not self.player.paused:
            self.player.set_paused(True)
            now.set_playing(False)
            self._announce()
        self.notify("sleep timer — paused", timeout=5)

    def action_toggle_shuffle(self) -> None:
        on_now = self.queue.toggle_shuffle()
        self.query_one("#now", NowPlaying).shuffle = on_now
        self._render_queue()
        self.dirs.save_state({"shuffle": on_now})
        self.notify(f"shuffle {'on' if on_now else 'off'}", timeout=1.5)

    def action_cycle_repeat(self) -> None:
        mode = self.queue.cycle_repeat()
        self.query_one("#now", NowPlaying).repeat = mode
        self.dirs.save_state({"repeat": mode.value})
        self.notify(f"repeat {mode.value}", timeout=1.5)

    # ── mpv thread callbacks ──────────────────────────────────────────
    # These arrive on mpv's event thread and must NEVER block: a blocking
    # call_from_thread here deadlocks against player.terminate() on quit
    # (the UI thread joins the event thread while the event thread waits
    # for the UI thread). call_soon_threadsafe just enqueues and returns.
    def _mpv_position(self, position: float, duration: float) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_position, position, duration)
        except Exception:
            pass  # loop gone — app shutting down

    def _mpv_track_end(self, failed: bool) -> None:
        try:
            self._loop.call_soon_threadsafe(self._on_track_end, failed)
        except Exception:
            pass

    def _on_position(self, position: float, duration: float) -> None:
        if not self.is_running:
            return
        now = self.query_one("#now", NowPlaying)
        now.set_progress(position, duration)
        self.mpris.set_position(position)
        if position > 3:
            self._end_failures = 0
        song = self.queue.current
        if song and not self._scrobbled and duration > 0:
            if position >= min(duration / 2, 240):
                self._scrobbled = True
                self._scrobble(song, True)
                # a play is now "counted" — mirror it into the local stats log
                # (cheap append; never blocks; matches the scrobble moment).
                # Private listening skips the local log too, not just the network.
                self.stats.log_play(song.id, song.title, song.artist)
        # crash-safe resume point, at most every 10s
        if position - self._last_persist >= 10 or position < self._last_persist:
            self._last_persist = position
            self._persist_queue(position)

    def _on_track_end(self, failed: bool) -> None:
        if not self.is_running:
            return
        if failed:
            self._end_failures += 1
            song = self.queue.current
            self.notify(
                f"stream failed: {song.title if song else '?'}",
                severity="warning",
                timeout=4,
            )
            if self._end_failures >= 3:
                self.notify("three failures in a row — stopping", severity="error")
                self.player.stop()
                self.query_one("#now", NowPlaying).set_playing(False)
                return
        # sleep timer set to "stop at end of track": let this one finish, then
        # stop rather than advancing, and clear the timer.
        if not failed and SLEEP_PRESETS[self._sleep_idx] == -1:
            self._sleep_idx = 0
            now = self.query_one("#now", NowPlaying)
            now.sleep_label = ""
            self.player.stop()
            now.set_playing(False)
            now.set_progress(0.0, 0.0)
            self._render_queue()
            self._announce()
            self.notify("sleep timer — stopped at end of track", timeout=5)
            return
        drained_seed = self.queue.current  # the track that just finished
        song = self.queue.advance(natural=not failed)
        if song is not None:
            self._play_current()
        else:
            self.player.stop()
            now = self.query_one("#now", NowPlaying)
            now.set_playing(False)
            now.set_progress(0.0, 0.0)
            self._render_queue()
            self._announce()

    # ── queue ─────────────────────────────────────────────────────────
    def _render_queue(self) -> None:
        panel = self.query_one("#queue-panel")
        options = []
        for i, song in enumerate(self.queue.songs):
            row = Text(no_wrap=True, overflow="ellipsis")
            if i < self.queue.index:
                row.append(f"{i + 1:>2d} ", style=palette.vfaint)
                row.append(song.title, style=palette.faint)
                row.append(f"  {song.artist}", style=palette.vfaint)
            elif i == self.queue.index:
                glyph = PLAY_GLYPH if (self.player and self.player.active and not self.player.paused) else PAUSE_GLYPH
                row.append(f"{glyph} ", style=palette.green)
                row.append(song.title, style=f"bold {palette.blue}")
                row.append(f"  {song.artist}", style=palette.dim)
            else:
                row.append(f"{i + 1:>2d} ", style=palette.vfaint)
                row.append(song.title, style=palette.text)
                row.append(f"  {song.artist}", style=palette.dim)
            options.append(Option(row, id=f"q{i}"))
        self._fill("#queue-list", options)
        ol = self.query_one("#queue-list", NavList)
        if options and 0 <= self.queue.index < len(options):
            ol.highlighted = self.queue.index
            if self.queue.index != self._queue_scrolled_to:
                self._queue_scrolled_to = self.queue.index
                index = self.queue.index
                self.call_after_refresh(
                    lambda: ol.scroll_to(y=index, animate=False)
                )
        upcoming = self.queue.songs[self.queue.index + 1 :] if self.queue.index >= 0 else self.queue.songs
        remaining = sum(s.duration for s in upcoming)
        panel.border_subtitle = (
            f"{len(upcoming)}\u2669 up next \u00b7 {anim.fmt_time(remaining)}" if self.queue.songs else None
        )

    def action_enqueue(self, play_next: bool) -> None:
        focused = self.focused
        if focused is None or focused.id != "tracks-list":
            return
        ol = self.query_one("#tracks-list", NavList)
        view = self._tracks_view()
        if ol.highlighted is None or ol.highlighted >= len(view):
            return
        song = view[ol.highlighted]
        if play_next:
            self.queue.add_next([song])
        else:
            self.queue.add([song])
        self._render_queue()
        self._persist_queue()
        self.notify(f"queued {'next: ' if play_next else ''}{song.title}", timeout=2)

    def action_queue_remove(self) -> None:
        focused = self.focused
        if focused is None or focused.id != "queue-list":
            return
        ol = self.query_one("#queue-list", NavList)
        if ol.highlighted is None:
            return
        was_current = ol.highlighted == self.queue.index
        self.queue.remove(ol.highlighted)
        if was_current:
            self._play_current()
        else:
            self._render_queue()
        self._persist_queue()

    def action_queue_clear(self) -> None:
        self.queue.clear()
        self.player.stop()
        now = self.query_one("#now", NowPlaying)
        now.set_song(None)
        now.set_playing(False)
        self._render_queue()
        self._persist_queue()
        self.notify("queue cleared", timeout=2)

    def _persist_queue(self, position: float | None = None) -> None:
        data = self.queue.to_dict()
        data["position"] = position if position is not None else (self.player.position if self.player else 0.0)
        self.dirs.write_cache("queue", data)

    # ── playlists ─────────────────────────────────────────────────────
    def _highlighted_song(self) -> Song | None:
        focused = self.focused
        if focused is not None and focused.id == "tracks-list":
            ol = self.query_one("#tracks-list", NavList)
            view = self._tracks_view()
            if ol.highlighted is not None and ol.highlighted < len(view):
                return view[ol.highlighted]
        elif focused is not None and focused.id == "queue-list":
            ol = self.query_one("#queue-list", NavList)
            if ol.highlighted is not None and ol.highlighted < len(self.queue.songs):
                return self.queue.songs[ol.highlighted]
        return None

    def action_queue_save(self) -> None:
        """Save the current play queue as a brand-new playlist."""
        if not self.queue.songs:
            self.notify("the queue is empty — nothing to save", timeout=2)
            return
        song_ids = [s.id for s in self.queue.songs]
        self.push_screen(
            InputModal("save queue as playlist", placeholder="name"),
            lambda name: self._queue_save(name, song_ids) if name else None,
        )

    @work(group="mutate")
    async def _queue_save(self, name: str, song_ids: list[str]) -> None:
        self._mutations += 1
        try:
            await self.client.create_playlist(name, song_ids)
        except Exception as e:
            self.notify(f"couldn't save queue: {e}", severity="error", timeout=5)
            return
        finally:
            self._mutations -= 1
        self.notify(f"saved “{name}” · {len(song_ids)} tracks", timeout=3)
        self._load_playlists()







    def action_toggle_notifications(self) -> None:
        on_now = self.notifier.toggle()
        self.notify(f"notifications {'on' if on_now else 'off'}", timeout=2)

    def action_queue_move(self, delta: int) -> None:
        focused = self.focused
        if focused is None or focused.id != "queue-list":
            return
        ol = self.query_one("#queue-list", NavList)
        if ol.highlighted is None:
            return
        new = self.queue.move(ol.highlighted, delta)
        if new is None:
            return
        self._render_queue()
        ol.highlighted = new
        self._persist_queue()

    # ── offline mutation queue ────────────────────────────────────────
    @staticmethod
    def _is_network_error(error: Exception) -> bool:
        """A connectivity failure (park the mutation) vs. a server rejection
        (a bad id — surface it, don't retry forever). Mirrors the split in
        `_connection_trouble`: SubsonicError is the server saying no."""
        return not isinstance(error, SubsonicError)

    def _note_queued(self) -> None:
        """Unobtrusive breadcrumb that a write is parked for later."""
        n = self.mutations.pending
        self.notify(f"offline — {n} change{'s' if n != 1 else ''} queued", timeout=2)

    @work(group="flush")
    async def _flush_mutations(self) -> None:
        """Replay parked stars/ratings/scrobbles once we look online again.
        Runs off the UI thread; drops each op on success, keeps the rest on the
        first network failure so order and intent are preserved."""
        if self.client is None or self._offline or not self.mutations.pending:
            return
        try:
            flushed = await self.mutations.flush(self.client, self._is_network_error)
        except Exception:
            return
        if flushed:
            self.notify(f"synced {flushed} queued change{'s' if flushed != 1 else ''}", timeout=2)



    # ── offline downloads ─────────────────────────────────────────────
    def action_download(self) -> None:
        """Pin the highlighted/playing track."""
        song = self._target_song()
        if song is None:
            self.notify("highlight a track to download", timeout=2)
            return
        if self.client is not None and self.client.cached_stream(song.id):
            self.notify(f"already downloaded: {song.title}", timeout=2)
            return
        self._download_songs([song], label=song.title)

    def action_download_view(self) -> None:
        """Pin every track in the current tracks pane / playlist."""
        if not self._songs:
            self.notify("nothing here to download", timeout=2)
            return
        title = self.query_one("#tracks-panel").border_title or "view"
        self._download_songs(list(self._songs), label=str(title))

    def action_download_all(self) -> None:
        """Pin the whole loaded library (the all-tracks cache)."""
        cached = self.dirs.read_cache("all-songs")
        songs = [Song.from_dict(s) for s in cached.get("songs", [])] if cached else list(self._songs)
        if not songs:
            self.notify("library not loaded yet — open 'all tracks' first", timeout=3)
            return
        self._download_songs(songs, label="library")

    @work(exclusive=True, group="download")
    async def _download_songs(self, songs: list[Song], label: str) -> None:
        """Download a batch of songs to the audio cache. Runs off the UI
        thread; progress rides the heartbeat spinner (group='download'),
        completion/failure is a notify. Already-pinned songs are skipped
        cheaply so re-runs are near-instant."""
        if self.client is None:
            return
        pending = [s for s in songs if not self.client.cached_stream(s.id)]
        if not pending:
            self.notify(f"{label}: already downloaded", timeout=2)
            return
        self._dl_total = len(pending)
        self._dl_done = 0
        self._dl_failed = 0
        if len(pending) > 1:
            self.notify(f"downloading {label} · {len(pending)} tracks", timeout=3)
        for song in pending:
            try:
                await self.client.download_song(song.id)
            except Exception:
                self._dl_failed += 1
            self._dl_done += 1
            # re-render so the ✓ appears as each track lands
            self._refresh_song_markers()
        ok = self._dl_done - self._dl_failed
        if self._dl_failed:
            self.notify(
                f"downloaded {ok}/{self._dl_total} · {self._dl_failed} failed",
                severity="warning", timeout=5,
            )
        else:
            self.notify(f"downloaded {label}" if ok == 1 else f"downloaded {ok} tracks", timeout=3)
        self._refresh_song_markers()

    def action_toggle_offline(self) -> None:
        self._offline = not self._offline  # session-only; not persisted
        self._render_status()
        self.notify(
            "offline mode — playing only downloaded tracks" if self._offline
            else "online mode",
            timeout=3,
        )
        if not self._offline:
            self._flush_mutations()  # back online: replay what we buffered

    def action_cycle_quality(self) -> None:
        """Step through the streaming-quality presets. Updates the cap used for
        the next stream; a currently-playing track is left untouched."""
        if self.client is None:
            return
        current = (int(self.client.max_bitrate), str(self.client.stream_format))
        idx = next(
            (i for i, (_, kb, fmt) in enumerate(QUALITY_PRESETS) if (kb, fmt) == current),
            -1,
        )
        label, kbps, fmt = QUALITY_PRESETS[(idx + 1) % len(QUALITY_PRESETS)]
        self.client.max_bitrate = kbps
        self.client.stream_format = fmt
        self._render_status()
        self.notify(f"\uf012 stream quality: {label}", timeout=2)

    @work(group="mutate")
    async def _scrobble(self, song: Song, submission: bool) -> None:
        # best-effort but still worth buffering so play counts catch up; stays
        # silent (background write, no user gesture to acknowledge)
        if self._offline:
            self.mutations.scrobble(song.id, submission)
            # ListenBrainz needs full track metadata the offline mutation queue
            # (keyed by id) can't reconstruct, so its listens don't buffer — a
            # missed listen is harmless, and never touching the network here
            # keeps offline mode truly offline.
            return
        try:
            await self.client.scrobble(song.id, submission)
        except Exception as e:
            if self._is_network_error(e):
                self.mutations.scrobble(song.id, submission)
        # mirror the scrobble to ListenBrainz when configured (no-op otherwise):
        # "playing_now" on track start, a counted listen at the submit threshold
        if self.listenbrainz.enabled:
            try:
                if submission:
                    await self.listenbrainz.submit(song)
                else:
                    await self.listenbrainz.now_playing(song)
            except Exception:
                pass  # opt-in extra; never let it disturb playback

    # ── art ───────────────────────────────────────────────────────────
    @work(exclusive=True, group="art")
    async def _load_art(self, cover_id: str, key: str) -> None:
        panel = self.query_one("#art-panel", CoverArt)
        try:
            path = await self.client.cover_art(cover_id)
        except Exception:
            panel.placeholder()
            self._tint_from_art(None)
            return
        panel.show(path, key)
        self._tint_from_art(path)

    @work(exclusive=True, group="art")
    async def _load_pl_art(self, pl_id: str) -> None:
        try:
            path = await self.client.cover_art(pl_id)
        except Exception:
            self._show_pl_placeholder()
            return
        def swap():
            el = self.query_one("#pl-art")
            try:
                from textual_image.widget import Image
                image = Image(str(path), classes="cover-image")
                el.remove_children()
                el.mount(image)
            except Exception:
                self._show_pl_placeholder()
        self.app.call_next(swap)

    def _show_pl_placeholder(self) -> None:
        def do():
            el = self.query_one("#pl-art")
            el.remove_children()
            el.mount(Static("♪", id="pl-placeholder"))
        self.app.call_next(do)

    def _show_pl_header(self, playlist: Playlist | None) -> None:
        el = self.query_one("#pl-header")
        el.styles.display = "block"
        info = self.query_one("#pl-info", Static)
        if playlist:
            dur = anim.fmt_time(playlist.duration)
            info.update(f"[bold]{playlist.name}[/]\n{playlist.song_count} tracks · {dur}")
        else:
            info.update("")

    def _hide_pl_header(self) -> None:
        self.query_one("#pl-header").styles.display = "none"

    def _sync_border_tint(self):
        from ricekit.palette import palette

        self.theme_variables["kit-border"] = palette.faint
        self.theme_variables["kit-border-focus"] = palette.blue
        self.theme_variables["kit-border-alt"] = palette.lav
        self.stylesheet.set_variables(self.theme_variables)
        self.stylesheet.reparse()
        self.stylesheet.update(self.screen, animate=False)

    def _tint_from_art(self, path: Path | None) -> None:
        """Live-tint the chrome with the cover's dominant color (or clear
        it). Off unless enabled + truecolor; any failure leaves it untinted."""
        if not CONFIG["art_theming"] or path is None:
            artcolor.set_tint(None)
            self.refresh_css()
            return
        try:
            artcolor.set_tint(artcolor.extract_vibrant(path))
            self._sync_border_tint()
            self._render_sidebar()
        except Exception:
            artcolor.set_tint(None)
            self.refresh_css()

    # ── misc actions ──────────────────────────────────────────────────
    def action_focus_panel(self, direction: int) -> None:
        lists = [
            self.query_one("#sidebar-list", NavList),
            self.query_one("#tracks-list", NavList),
            self.query_one("#queue-list", NavList),
        ]
        focused = self.focused
        try:
            i = lists.index(focused)
        except ValueError:
            i = 0 if direction > 0 else 1
            direction = 0 if direction > 0 else -1
        lists[(i + direction) % len(lists)].focus()


    def _auto_refresh(self) -> None:
        if self.client is None or self._mutations > 0:
            return
        if self.screen is not self.screen_stack[0]:
            return
        self._load_playlists()
        if not self.view.startswith(("artist:", "album:")):
            self._load_view(self.view)
        self._flush_mutations()

    def action_help(self) -> None:
        self.push_screen(NaviTuiHelpModal(HELP_SECTIONS, title="NaviTui · keys"))

    def action_stats(self) -> None:
        """Open the local listening-stats modal — reads the play log on open
        (cheap) and folds it into a summary; never touches the network."""
        import time

        summary = statsmod.summarize(self.stats.load(), time.time())
        self.push_screen(StatsModal(summary))

    # ── zen / now-playing splash ──────────────────────────────────────
    def action_toggle_zen(self) -> None:
        self._zen = not self._zen
        self.set_class(self._zen, "zen")
        if self._zen:
            self._render_zen_info()
            song = self.queue.current
            if song and song.cover_art:
                self._load_art(song.cover_art, f"zen-{song.id}")
        else:
            self.query_one("#tracks-list", ClickList).focus()

    def _render_zen_info(self) -> None:
        """The big title/artist/album block under the cover in zen mode."""
        song = self.queue.current
        info = self.query_one("#zen-info", Static)
        viz = self.query_one("#zen-viz", Static)
        prog = self.query_one("#zen-progress", Static)
        t = Text(justify="center")
        if song is None:
            t.append("nothing playing", style=palette.dim)
            info.update(t)
            viz.update(Text())
            prog.update(Text())
            return

        t.append(song.title, style=f"bold {palette.text}")
        if song.starred:
            t.append(f" {icons.STAR}", style=palette.text)
        t.append(f"\n{song.artist}", style=palette.sub)
        if song.album:
            t.append(f"\n{song.album}", style=palette.dim)

        # progress bar + time
        pos = self.player.position if self.player else 0.0
        dur = song.duration or 1.0
        frac = pos / dur if dur > 0 else 0.0
        p = Text(justify="center")
        p.append(f"{anim.fmt_time(pos)} ", style=palette.dim)
        p.append_text(anim.smooth_bar(frac, 20, head_pulse=0.3 if self.player and not self.player.paused else 0.0))
        p.append(f" {anim.fmt_time(dur)}", style=palette.dim)
        prog.update(p)

        # visualizer in zen
        v = self.query_one("#now", NowPlaying)
        vx = Text(justify="center")
        vx.append_text(v.viz.render())
        viz.update(vx)
        info.update(t)

    def on_kit_theme_changed(self) -> None:
        if not self.kit_theme_previewing:
            self.dirs.save_state({"theme": self.theme})
        # the palette was just rebuilt for the new theme — re-assert the
        # album tint on top (a no-op under the ANSI `system` theme)
        artcolor.reapply()
        self._sync_border_tint()
        self._render_status()
        if self.client is not None:
            self._render_sidebar()
            self._refresh_song_markers()
            self._render_queue()

    def _persist_width(self, selector: str, width: int | None) -> None:
        widths = self.dirs.load_state().get("widths", {})
        if width is None:
            widths.pop(selector, None)
        else:
            widths[selector] = width
        self.dirs.save_state({"widths": widths})

    def _connection_trouble(self, error: Exception) -> None:
        if isinstance(error, SubsonicError):
            self.notify(f"server error: {error}", severity="error", timeout=6)
        else:
            self.notify("offline — showing cached library", severity="warning", timeout=4)

    async def action_quit(self) -> None:
        self.mpris.stop()
        self.notifier.stop()
        self.remote.stop()
        self.discord.stop()
        await self.listenbrainz.close()
        if self.player is not None:
            self._persist_queue()
            self.player.terminate()
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
        self.exit()

def main() -> None:
    NaviTuiApp().run()

if __name__ == "__main__":
    main()
