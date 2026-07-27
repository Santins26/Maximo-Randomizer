"""
Tkinter GUI for the Maximo: Ghosts to Glory randomizer.

Two modes (tabs):
  - "Patch ISO": user just picks a Maximo ISO, hits Run, and the script
    extracts assets, randomizes them, and writes them back into the ISO.
    Backup is automatic.
  - "Folder mode": classic CLI behavior — pick a source folder of extracted
    PSX files and an output folder for randomized copies.

Usage:
  python -m randomizer.gui
  python randomizer_gui.py
"""
from __future__ import annotations
import io
import os
import random
import sys
import threading
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from .cli import cmd_randomize
from .iso_patcher import patch_iso
from . import assets


def _enable_dpi_awareness() -> None:
    """Tell Windows this process is DPI-aware BEFORE any Tk window exists."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _ui_scale(root: tk.Tk) -> float:
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        return 1.0
    return max(1.0, min(dpi / 96.0, 3.0))


def _load_image(path, max_size: tuple[int, int] | None = None):
    if path is None:
        return None
    path = Path(path)
    if not path.is_file():
        return None
    try:
        from PIL import Image, ImageTk
        img = Image.open(path).convert("RGBA")
        if max_size is not None:
            img.thumbnail(max_size, Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except ImportError:
        try:
            return tk.PhotoImage(file=str(path))
        except tk.TclError:
            return None
    except Exception:
        return None


class _StreamToWidget(io.TextIOBase):
    def __init__(self, widget: tk.Text, root: tk.Tk):
        self.widget = widget
        self.root = root

    def write(self, s: str) -> int:
        if not s:
            return 0
        self.root.after(0, self._append, s)
        return len(s)

    def _append(self, s: str) -> None:
        try:
            self.widget.configure(state=tk.NORMAL)
            self.widget.insert(tk.END, s)
            self.widget.see(tk.END)
            self.widget.configure(state=tk.DISABLED)
        except Exception:
            pass

    def flush(self) -> None:
        pass


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class ScrollableOptionsFrame(ttk.Frame):
    """A responsive container that ensures all options can be scrolled dynamically."""
    def __init__(self, container, bg_color, *args, **kwargs):
        super().__init__(container, *args, **kwargs)
        
        self.canvas = tk.Canvas(self, bg=bg_color, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas, style="MainViewport.TFrame")

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.scrollable_frame.bind("<Enter>", self._bind_mousewheel)
        self.scrollable_frame.bind("<Leave>", self._unbind_mousewheel)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event):
        if sys.platform == "win32":
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        elif sys.platform == "darwin":
            self.canvas.yview_scroll(int(-1 * event.delta), "units")

    def _bind_mousewheel(self, event):
        if sys.platform in ("win32", "darwin"):
            self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        else:
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _unbind_mousewheel(self, event):
        if sys.platform in ("win32", "darwin"):
            self.canvas.unbind_all("<MouseWheel>")
        else:
            self.canvas.unbind_all("<MouseWheel>")
            self.canvas.unbind_all("<Button-4>")
            self.canvas.unbind_all("<Button-5>")


class RandomizerApp:
    PAD = 8

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Maximo: Ghosts to Glory — Randomizer v7.3")
        self._scale = _ui_scale(root)
        try:
            root.tk.call("tk", "scaling", self._scale * 96.0 / 72.0)
        except tk.TclError:
            pass
        
        bw, bh = int(1040 * self._scale), int(880 * self._scale)
        root.geometry(f"{bw}x{bh}")
        root.minsize(int(940 * self._scale), int(680 * self._scale))

        if sys.platform == "win32":
            ico = assets.app_icon_path()
            if ico is not None:
                try:
                    root.iconbitmap(str(ico))
                except tk.TclError:
                    pass
                # iconbitmap() sets the title-bar icon, but PyInstaller
                # onefile builds have historically been unreliable about
                # propagating it to the TASKBAR/Alt-Tab icon on Windows (the
                # shell queries WM_GETICON, which iconbitmap doesn't always
                # populate correctly through Tcl/Tk's Windows glue).
                # root.iconphoto(True, ...) sets both the small and large
                # icon via a PhotoImage instead, which is the more reliable
                # mechanism for the taskbar specifically. Keep both calls --
                # iconbitmap for the title bar / window corner, iconphoto as
                # the robust fallback for taskbar/Alt-Tab.
                photo = _load_image(ico, max_size=(256, 256))
                if photo is not None:
                    try:
                        root.iconphoto(True, photo)
                        self._taskbar_icon_photo = photo  # keep a reference alive
                    except tk.TclError:
                        pass

        # State initialization
        cwd = Path.cwd()
        default_src = cwd / "game_files"
        default_out = cwd / "output"
        self.iso_var = tk.StringVar(value="")
        self.iso_output_var = tk.StringVar(value="")
        self.iso_backup_var = tk.BooleanVar(value=True)

        self.src_var = tk.StringVar(value=str(default_src) if default_src.is_dir() else "")
        self.out_var = tk.StringVar(value=str(default_out) if default_src.is_dir() else "")

        # Randomizer options
        self.seed_var = tk.StringVar(value="")
        self.items_var = tk.BooleanVar(value=True)
        self.chests_var = tk.BooleanVar(value=True)
        self.skills_var = tk.BooleanVar(value=True)
        self.columns_var = tk.BooleanVar(value=True)
        self.spawn_loc_var = tk.BooleanVar(value=False)
        self.gate_rando_var = tk.BooleanVar(value=False)
        self.gate_mode_var = tk.StringVar(value="isolated")
        self.damage_taken_var = tk.StringVar(value="normal")
        self.damage_dealt_var = tk.StringVar(value="normal")
        
        self.start_gold_var = tk.StringVar(value="")
        self.start_lives_var = tk.StringVar(value="")
        self.start_keys_var = tk.StringVar(value="")
        self.start_deathcoins_var = tk.StringVar(value="")
        self.sword_enchant_var = tk.StringVar(value="None")
        self.elemental_shield_var = tk.StringVar(value="None")
        self.randomize_start_inv_var = tk.BooleanVar(value=False)
        
        self.skill_vars = {k: tk.BooleanVar(value=False) for k in (
            "sword720", "double_slash", "mighty_blow", "masquerade", "sword_power",
            "projectile", "return_shield", "hover_shield", "increase_armor",
            "wide_shockwave", "damage_shockwave", "find_treasure", "smart_bomb",
            "increase_throw")}
        self.harder_mode_var = tk.BooleanVar(value=False)
        self.preserve_chests_var = tk.BooleanVar(value=False)
        self.preserve_iron_keys_var = tk.BooleanVar(value=False)
        self.randomize_levels_var = tk.BooleanVar(value=False)
        self.enemies_var = tk.BooleanVar(value=True)
        self.dup_bosses_var = tk.BooleanVar(value=False)
        # Cross-world enemies: inject foreign enemy blobs into every world's
        # BEF so any enemy can appear in any world (Bomb_Skeleton in Grave,
        # etc.). Off by default -- see catalog.py's CROSS_WORLD_SAFE_ENEMIES /
        # PRS_LOCKED_ENEMIES for which enemies are excluded from injection
        # (their meshes live in a world-specific .PRS and would crash the
        # game on map load if placed elsewhere).
        self.cross_world_var = tk.BooleanVar(value=False)
        
        self.boss_clones_grave_var = tk.IntVar(value=1)
        self.boss_clones_swamp_var = tk.IntVar(value=1)
        self.boss_clones_ice_var = tk.IntVar(value=1)
        self.boss_clones_under_var = tk.IntVar(value=1)
        self.boss_clones_castle_var = tk.IntVar(value=1)
        self.no_dark_knight_var = tk.BooleanVar(value=False)

        from .spawn_config import DEFAULT_CONFIG_FILENAME
        self.spawn_config_path_var = tk.StringVar(value="")
        _existing_cfg = cwd / DEFAULT_CONFIG_FILENAME
        if _existing_cfg.is_file():
            self.spawn_config_path_var.set(str(_existing_cfg))

        from .cli import ALL_WORLDS
        self._all_worlds = ALL_WORLDS
        self.all_worlds_var = tk.BooleanVar(value=True)
        self.world_vars = {w: tk.BooleanVar(value=True) for w in ALL_WORLDS}

        self._running = False
        self._setup_styles()
        self._build_ui()

    def _setup_styles(self) -> None:
        """Configure a crisp, highly accessible professional light theme for maximum readability."""
        self.style = ttk.Style()
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        bg_main = "#f8fafc"      # Slate-50
        bg_card = "#ffffff"      # Pure White
        fg_main = "#0f172a"      # Slate-900
        fg_muted = "#475569"     # Slate-600
        accent = "#2563eb"       # Blue-600
        accent_hover = "#1d4ed8" # Blue-700
        accent_dark = "#1e3a8a"  # Blue-900
        border_color = "#cbd5e1" # Slate-300

        self.root.configure(bg=bg_main)

        self.style.configure(".", background=bg_main, foreground=fg_main, font=("Segoe UI", 10))
        self.style.configure("TFrame", background=bg_main)
        self.style.configure("MainViewport.TFrame", background=bg_main)
        
        self.style.configure("TNotebook", background=bg_main, borderwidth=0)
        self.style.configure("TNotebook.Tab", background="#e2e8f0", foreground=fg_muted, padding=[20, 8], borderwidth=1, bordercolor=border_color)
        self.style.map("TNotebook.Tab", 
                       background=[("selected", bg_card), ("active", "#f1f5f9")], 
                       foreground=[("selected", accent), ("active", fg_main)])

        self.style.configure("TLabelframe", background=bg_main, bordercolor=border_color, borderwidth=1)
        self.style.configure("TLabelframe.Label", background=bg_main, foreground=accent_dark, font=("Segoe UI", 11, "bold"))

        self.style.configure("TEntry", fieldbackground=bg_card, bordercolor=border_color, foreground=fg_main, insertcolor=fg_main)
        
        self.style.configure("TCombobox", fieldbackground=bg_card, background="#f1f5f9", bordercolor=border_color, foreground=fg_main, seqcolor=fg_main)
        self.style.map("TCombobox", 
                       fieldbackground=[("readonly", bg_card)], 
                       foreground=[("readonly", fg_main)])
                       
        self.style.configure("TSpinbox", fieldbackground=bg_card, background="#f1f5f9", bordercolor=border_color, foreground=fg_main)
        self.style.map("TSpinbox", fieldbackground=[("readonly", bg_card)], foreground=[("readonly", fg_main)])

        self.style.configure("TButton", background=bg_card, foreground=fg_main, bordercolor=border_color, padding=[14, 6], anchor="center")
        self.style.map("TButton", background=[("active", "#f1f5f9"), ("disabled", "#e2e8f0")], foreground=[("disabled", "#94a3b8")])
        self.style.configure("Accent.TButton", background=accent, foreground="#ffffff", bordercolor=accent, font=("Segoe UI", 10, "bold"))
        self.style.map("Accent.TButton", background=[("active", accent_hover)], foreground=[("active", "#ffffff")])

        self.style.configure("TCheckbutton", background=bg_main, foreground=fg_main)
        self.style.map("TCheckbutton", background=[("active", bg_main)])
        self.style.configure("TRadiobutton", background=bg_main, foreground=fg_main)
        self.style.map("TRadiobutton", background=[("active", bg_main)])

    def _build_ui(self) -> None:
        top_region = ttk.Frame(self.root, padding=(self.PAD, self.PAD, self.PAD, 0))
        top_region.pack(side=tk.TOP, fill=tk.X)
        self._build_header(top_region)

        notebook_container = ttk.Frame(top_region)
        notebook_container.pack(fill=tk.X, pady=(self.PAD, 2))

        notebook = ttk.Notebook(notebook_container)
        notebook.pack(fill=tk.X)

        iso_tab = ttk.Frame(notebook, padding=self.PAD)
        notebook.add(iso_tab, text="  Patch ISO (Recommended)  ")
        self._build_iso_tab(iso_tab)

        folder_tab = ttk.Frame(notebook, padding=self.PAD)
        notebook.add(folder_tab, text="  Folder Mode (Advanced)  ")
        self._build_folder_tab(folder_tab)
        self._notebook = notebook

        bottom_region = ttk.Frame(self.root, padding=(self.PAD, 0, self.PAD, self.PAD))
        bottom_region.pack(side=tk.BOTTOM, fill=tk.X)
        
        self._build_actions_frame(bottom_region)
        self._build_log_frame(bottom_region)
        self._build_status_bar(bottom_region)

        middle_region = ttk.Frame(self.root, padding=(self.PAD, 4, self.PAD, 4))
        middle_region.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.scroll_viewport = ScrollableOptionsFrame(middle_region, bg_color="#f8fafc")
        self.scroll_viewport.pack(fill=tk.BOTH, expand=True)

        self._build_grid_options(self.scroll_viewport.scrollable_frame)

    def _build_header(self, parent: ttk.Frame) -> None:
        logo_path = assets.logo_path()
        header = ttk.Frame(parent)
        header.pack(fill=tk.X, pady=(0, 4))

        if logo_path is not None:
            # Bumped up from 50px tall / 300px wide -- at the old size the
            # logo occupied a small corner of the header, leaving a lot of
            # visibly empty space next to it. This is roughly 2.4x taller,
            # filling the header area properly.
            max_h = int(120 * self._scale)
            photo = _load_image(logo_path, max_size=(int(420 * self._scale), max_h))
            if photo is not None:
                self._logo_photo = photo
                ttk.Label(header, image=self._logo_photo).pack(side=tk.LEFT)
                # Credit label placed in the empty space toward the right of
                # the header (this is where the user circled in their
                # screenshot -- an empty area near the top-right, not
                # directly beside the logo). side=tk.RIGHT pushes it into
                # that space; the text itself stays left-justified rather
                # than centered or right-justified within its own label.
                ttk.Label(
                    header, text="by Santins", justify=tk.LEFT, anchor=tk.NW,
                    font=("Segoe UI", 14, "italic"), foreground="#475569"
                ).pack(side=tk.RIGHT, anchor=tk.N, padx=(0, 40), pady=(100, 0))
                return
        
        ttk.Label(header, text="MAXIMO: GHOSTS TO GLORY RANDOMIZER", font=("Segoe UI", 15, "bold"), foreground="#2563eb").pack(side=tk.LEFT)

    def _build_iso_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Maximo Disc Image:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.iso_var, width=70).grid(row=0, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_iso, width=12).grid(row=0, column=2)

        ttk.Label(parent, text="Output (Optional):").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.iso_output_var, width=70).grid(row=1, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_iso_output, width=12).grid(row=1, column=2)

        ttk.Checkbutton(parent, text="Create asset backup safely (.backup) prior to in-place patching execution", variable=self.iso_backup_var).grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(6, 2))

        info = "System Hint: Select a secure Maximo copy (.iso, .bin). If output entry path stays unconfigured, modifications overwrite source file layers directly."
        ttk.Label(parent, text=info, justify=tk.LEFT, foreground="#475569", font=("Segoe UI", 9)).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))
        parent.columnconfigure(1, weight=1)

    def _build_folder_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Source Folder:").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.src_var, width=70).grid(row=0, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_src, width=12).grid(row=0, column=2)

        ttk.Label(parent, text="Output Folder:").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(parent, textvariable=self.out_var, width=70).grid(row=1, column=1, sticky=tk.EW, padx=8)
        ttk.Button(parent, text="Browse...", command=self._browse_out, width=12).grid(row=1, column=2)
        parent.columnconfigure(1, weight=1)

    def _build_grid_options(self, parent: ttk.Frame) -> None:
        seed_row = ttk.Frame(parent)
        seed_row.pack(fill=tk.X, pady=(4, 10), padx=2)
        ttk.Label(seed_row, text="Seed Setup:", font=("Segoe UI", 11, "bold"), foreground="#1e3a8a").pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(seed_row, textvariable=self.seed_var, width=18).pack(side=tk.LEFT, padx=4)
        ttk.Label(seed_row, text="(Leave blank for random generation layout mechanics)", foreground="#475569", font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        ttk.Button(seed_row, text="Roll Random Seed", command=self._roll_seed).pack(side=tk.LEFT, padx=4)

        grid_body = ttk.Frame(parent)
        grid_body.pack(fill=tk.BOTH, expand=True)

        self._harder_lock = []

        left_col = ttk.Frame(grid_body)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(2, 6))

        f_rando = ttk.Labelframe(left_col, text=" Elements to Randomize ", padding=10)
        f_rando.pack(fill=tk.X, pady=(0, 8))
        
        for text, var in [("Enemies", self.enemies_var), ("Items", self.items_var), 
                          ("Chests", self.chests_var), ("Abilities & Skills", self.skills_var), 
                          ("Structures", self.columns_var)]:
            cb = ttk.Checkbutton(f_rando, text=text, variable=var)
            cb.pack(anchor=tk.W, pady=3)
            self._harder_lock.append((cb, "normal"))

        cb_cw = ttk.Checkbutton(
            f_rando, text="Cross-world enemies (any enemy in any world)",
            variable=self.cross_world_var)
        cb_cw.pack(anchor=tk.W, pady=3, padx=(16, 0))
        self._harder_lock.append((cb_cw, "normal"))

        f_worlds = ttk.Labelframe(left_col, text=" Included Map Regions ", padding=10)
        f_worlds.pack(fill=tk.X, pady=4)
        
        cb_aw = ttk.Checkbutton(f_worlds, text="All Available Worlds Included", variable=self.all_worlds_var, command=self._on_all_worlds_toggle)
        cb_aw.pack(anchor=tk.W, pady=4)
        self._harder_lock.append((cb_aw, "normal"))
        
        for w in self._all_worlds:
            wcb = ttk.Checkbutton(f_worlds, text=w.capitalize(), variable=self.world_vars[w], command=self._on_world_toggle)
            wcb.pack(anchor=tk.W, pady=3)
            self._harder_lock.append((wcb, "normal"))

        right_col = ttk.Frame(grid_body)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(6, 2))

        f_gameplay = ttk.Labelframe(right_col, text=" Combat & Balancing Parameters ", padding=10)
        f_gameplay.pack(fill=tk.X, pady=(0, 8))
        
        row_dt = ttk.Frame(f_gameplay)
        row_dt.pack(fill=tk.X, pady=4)
        ttk.Label(row_dt, text="Damage Taken", width=18).pack(side=tk.LEFT)
        cb_dt = ttk.Combobox(row_dt, textvariable=self.damage_taken_var, width=14, state="readonly", values=["0.25", "0.50", "normal", "2x", "4x"])
        cb_dt.pack(side=tk.LEFT)
        
        row_dd = ttk.Frame(f_gameplay)
        row_dd.pack(fill=tk.X, pady=4)
        ttk.Label(row_dd, text="Damage Dealt", width=18).pack(side=tk.LEFT)
        cb_dd = ttk.Combobox(row_dd, textvariable=self.damage_dealt_var, width=14, state="readonly", values=["0.25", "0.50", "normal", "2x", "4x"])
        cb_dd.pack(side=tk.LEFT)
        
        self._startinv_btn = ttk.Button(f_gameplay, text="Starting Inventory Config...", command=self._open_start_inventory)
        self._startinv_btn.pack(anchor=tk.W, pady=6)
        self._startinv_summary = ttk.Label(f_gameplay, text="", foreground="#475569", font=("Segoe UI", 9))
        self._startinv_summary.pack(anchor=tk.W, padx=2)

        # Spawn Rate Configuration Launchers
        spawn_row = ttk.Frame(f_gameplay)
        spawn_row.pack(fill=tk.X, pady=6)
        self._spawn_edit_btn = ttk.Button(spawn_row, text="Edit Spawn Rates...", command=self._open_spawn_editor)
        self._spawn_edit_btn.pack(side=tk.LEFT)
        self._spawn_clear_btn = ttk.Button(spawn_row, text="Use Stock Rates", command=self._clear_spawn_config)
        self._spawn_clear_btn.pack(side=tk.LEFT, padx=6)
        
        self._spawn_status = ttk.Label(f_gameplay, text="", foreground="#475569", font=("Segoe UI", 9))
        self._spawn_status.pack(anchor=tk.W, padx=2)
        
        cb_hm = ttk.Checkbutton(f_gameplay, text="Harder Mode", variable=self.harder_mode_var, command=self._sync_harder_mode)
        cb_hm.pack(anchor=tk.W, pady=4)
        self._harder_lock.extend([
            (cb_dt, "readonly"), (cb_dd, "readonly"), (self._startinv_btn, "normal"),
            (self._spawn_edit_btn, "normal"), (self._spawn_clear_btn, "normal")
        ])
        self._refresh_spawn_status()

        f_exp = ttk.Labelframe(right_col, text=" Custom Modifiers ", padding=10)
        f_exp.pack(fill=tk.X, pady=4)
        
        cb_sl = ttk.Checkbutton(f_exp, text="Randomize Maximo Entry Spawn Point", variable=self.spawn_loc_var)
        cb_sl.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_sl, "normal"))

        cb_dk = ttk.Checkbutton(f_exp, text="Exclude Dark Knight Entity (Prevents softlocks)", variable=self.no_dark_knight_var)
        cb_dk.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_dk, "normal"))

        cb_db = ttk.Checkbutton(f_exp, text="Duplicate Bosses", variable=self.dup_bosses_var)
        cb_db.pack(anchor=tk.W, pady=3)
        self._harder_lock.append((cb_db, "normal"))

        clones_frame = ttk.Frame(f_exp)
        clones_frame.pack(anchor=tk.W, padx=18, pady=4)
        from .items import BOSS_CLONE_MAX
        gmax = BOSS_CLONE_MAX.get("grave", 4)
        smax = BOSS_CLONE_MAX.get("swamp", 6)
        imax = BOSS_CLONE_MAX.get("ice", 8)
        umax = BOSS_CLONE_MAX.get("under", 10)
        cmax = BOSS_CLONE_MAX.get("castle", 8)
        _clone_specs = [
            ("Grave", self.boss_clones_grave_var, gmax),
            ("Swamp", self.boss_clones_swamp_var, smax),
            ("Ice", self.boss_clones_ice_var, imax),
            ("Under", self.boss_clones_under_var, umax),
            ("Castle", self.boss_clones_castle_var, cmax),
        ]
        for _ci, (_lbl, _var, _mx) in enumerate(_clone_specs):
            _r, _c = _ci // 3, (_ci % 3) * 2
            ttk.Label(clones_frame, text=f"{_lbl}:").grid(row=_r, column=_c, sticky=tk.W, padx=(0, 2), pady=3)
            _sp = ttk.Spinbox(clones_frame, from_=1, to=_mx, width=5, textvariable=_var)
            _sp.grid(row=_r, column=_c + 1, sticky=tk.W, padx=(0, 10), pady=3)
            self._harder_lock.append((_sp, "normal"))

        cb_gate = ttk.Checkbutton(f_exp, text="Randomize Gates", variable=self.gate_rando_var, command=self._sync_gate_opts)
        cb_gate.pack(anchor=tk.W, pady=(5, 3))
        self._harder_lock.append((cb_gate, "normal"))

        self._gate_sub_frame = ttk.Frame(f_exp)
        self._gate_sub_frame.pack(anchor=tk.W, padx=18, pady=2)
        
        rb_iso = ttk.Radiobutton(self._gate_sub_frame, text="Just gates randomizer", variable=self.gate_mode_var, value="isolated")
        rb_iso.grid(row=0, column=0, sticky=tk.W, pady=2)
        rb_pool = ttk.Radiobutton(self._gate_sub_frame, text="Randomize and add gates to the randomizer pool", variable=self.gate_mode_var, value="pool")
        rb_pool.grid(row=1, column=0, sticky=tk.W, pady=2)
        
        self._harder_lock.extend([(rb_iso, "normal"), (rb_pool, "normal")])
        self._sync_gate_opts()

        cb_pc = ttk.Checkbutton(f_exp, text="Preserve Original Chest Coordinates (Contents only)", variable=self.preserve_chests_var)
        cb_pc.pack(anchor=tk.W, pady=3)
        cb_pk = ttk.Checkbutton(f_exp, text="Preserve Iron Keys", variable=self.preserve_iron_keys_var)
        cb_pk.pack(anchor=tk.W, pady=3)
        cb_rl = ttk.Checkbutton(f_exp, text="Randomize Worlds", variable=self.randomize_levels_var)
        cb_rl.pack(anchor=tk.W, pady=3)
        
        for w in (cb_rl,):
            self._harder_lock.append((w, "normal"))

        grid_body.columnconfigure(0, weight=1, uniform="equal_cols")
        grid_body.columnconfigure(1, weight=1, uniform="equal_cols")
        self._refresh_startinv_summary()
        self._sync_harder_mode()

    def _sync_gate_opts(self) -> None:
        if not hasattr(self, "_gate_sub_frame"): return
        state = "normal" if self.gate_rando_var.get() and not self.harder_mode_var.get() else "disabled"
        for child in self._gate_sub_frame.winfo_children():
            try: child.configure(state=state)
            except Exception: pass

    def _sync_harder_mode(self) -> None:
        locked = bool(self.harder_mode_var.get())
        for widget, enabled_state in getattr(self, "_harder_lock", []):
            try: widget.configure(state=("disabled" if locked else enabled_state))
            except tk.TclError: pass
        self._sync_gate_opts()

    def _selected_skills(self) -> set[str]:
        return {k for k, v in self.skill_vars.items() if v.get()}

    def _sword_enchant_value(self) -> int | None:
        return {"None": None, "Fire": 1, "Ice": 2, "Sun": 3, "Armageddon": 4}.get(self.sword_enchant_var.get())

    def _elemental_shield_value(self) -> int | None:
        return {"None": None, "Wind": 1, "Magnetic": 2, "Lightning": 3}.get(self.elemental_shield_var.get())

    def _refresh_startinv_summary(self) -> None:
        if not hasattr(self, "_startinv_summary"): return
        if self.randomize_start_inv_var.get():
            txt = "Loadout Config Status: Randomized via Seed Layer"
        else:
            parts = []
            for label, var in (("gold", self.start_gold_var), ("lives", self.start_lives_var), ("keys", self.start_keys_var), ("death coins", self.start_deathcoins_var)):
                v = var.get().strip()
                if v: parts.append(f"{v} {label}")
            nsk = len(self._selected_skills())
            if nsk: parts.append(f"{nsk} skill{'s' if nsk != 1 else ''}")
            if self._sword_enchant_value() is not None: parts.append(f"{self.sword_enchant_var.get()} sword")
            if self._elemental_shield_value() is not None: parts.append(f"{self.elemental_shield_var.get()} shield")
            txt = f"Loadout Options Saved: {', '.join(parts)}" if parts else "Loadout Design Configuration: Standard Vanilla Defaults"
        self._startinv_summary.configure(text=txt)

    def _open_start_inventory(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Starting Loadout Layout Configuration")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#f8fafc")
        
        body = ttk.Frame(win, padding=16)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Configure Initial Currencies (Leave blank for game defaults):", font=("Segoe UI", 10, "bold"), foreground="#1e3a8a").grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 8))

        spins = []
        def add_spin(r, label, var, to):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky=tk.W, pady=3)
            sp = ttk.Spinbox(body, from_=0, to=to, width=10, textvariable=var)
            sp.grid(row=r, column=1, sticky=tk.W, padx=(4, 16), pady=3)
            spins.append(sp)
            return sp
            
        add_spin(1, "Initial Gold (Koins):", self.start_gold_var, 9999)
        add_spin(2, "Starting Lives:", self.start_lives_var, 99)
        add_spin(3, "Iron Keys:", self.start_keys_var, 9)
        add_spin(4, "Death Coins:", self.start_deathcoins_var, 99)
        
        ttk.Separator(body, orient="horizontal").grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=12)
        ttk.Label(body, text="Unlocked Passive Character Skills Matrix:", font=("Segoe UI", 10, "bold"), foreground="#1e3a8a").grid(row=6, column=0, columnspan=4, sticky=tk.W, pady=(0, 6))
            
        skill_cbs = []
        _labels = [("sword720", "Sword 720 Spin"), ("double_slash", "Double Slash"), 
                   ("mighty_blow", "Mighty Blow"), ("masquerade", "Masquerade"), 
                   ("sword_power", "Sword Power"), ("projectile", "Magic Bolt"), 
                   ("return_shield", "Return Shield"), ("hover_shield", "Hover Shield"), 
                   ("increase_armor", "Increase Armor"), ("wide_shockwave", "Wide Shockwave"), 
                   ("damage_shockwave", "Damage Shockwave"), ("find_treasure", "Find Treasure"), 
                   ("smart_bomb", "Smart Bomb"), ("increase_throw", "Increase Throw")]
        _cols = 3
        for i, (key, lbl) in enumerate(_labels):
            cb = ttk.Checkbutton(body, text=lbl, variable=self.skill_vars[key])
            cb.grid(row=7 + i // _cols, column=i % _cols, sticky=tk.W, padx=(4, 10), pady=3)
            skill_cbs.append(cb)

        sw_row = 7 + (len(_labels) + _cols - 1) // _cols
        ttk.Label(body, text="Sword Modification:").grid(row=sw_row, column=0, sticky=tk.W, pady=(10, 4))
        sword_cb = ttk.Combobox(body, textvariable=self.sword_enchant_var, width=16, state="readonly", values=["None", "Fire", "Ice", "Sun", "Armageddon"])
        sword_cb.grid(row=sw_row, column=1, columnspan=2, sticky=tk.W, pady=(10, 4))

        sh_row = sw_row + 1
        ttk.Label(body, text="Shield Infusion:").grid(row=sh_row, column=0, sticky=tk.W, pady=4)
        shield_cb = ttk.Combobox(body, textvariable=self.elemental_shield_var, width=16, state="readonly", values=["None", "Wind", "Magnetic", "Lightning"])
        shield_cb.grid(row=sh_row, column=1, columnspan=2, sticky=tk.W, pady=4)

        sep_row = sh_row + 1
        ttk.Separator(body, orient="horizontal").grid(row=sep_row, column=0, columnspan=4, sticky=tk.EW, pady=12)

        def _sync_rand():
            dis = bool(self.randomize_start_inv_var.get())
            for w in spins + skill_cbs:
                try: w.configure(state=("disabled" if dis else "normal"))
                except Exception: pass
            try:
                sword_cb.configure(state=("disabled" if dis else "readonly"))
                shield_cb.configure(state=("disabled" if dis else "readonly"))
            except tk.TclError: pass

        rand_cb = ttk.Checkbutton(body, text="Randomize start equipment configurations dynamically via core logic seed", variable=self.randomize_start_inv_var, command=_sync_rand)
        rand_cb.grid(row=sep_row + 1, column=0, columnspan=4, sticky=tk.W, pady=2)

        btns = ttk.Frame(body)
        btns.grid(row=sep_row + 2, column=0, columnspan=4, sticky=tk.E, pady=(16, 0))

        def _clear():
            for var in (self.start_gold_var, self.start_lives_var, self.start_keys_var, self.start_deathcoins_var): var.set("")
            for v in self.skill_vars.values(): v.set(False)
            self.sword_enchant_var.set("None")
            self.elemental_shield_var.set("None")
            self.randomize_start_inv_var.set(False)
            _sync_rand()

        ttk.Button(btns, text="Reset Fields", command=_clear).pack(side=tk.LEFT, padx=(0, 8))
        def _close():
            self._refresh_startinv_summary()
            win.destroy()
        ttk.Button(btns, text="Apply Changes", command=_close, style="Accent.TButton").pack(side=tk.LEFT)
        _sync_rand()
        win.protocol("WM_DELETE_WINDOW", _close)
        win.grab_set()

    def _refresh_spawn_status(self) -> None:
        path = self.spawn_config_path_var.get().strip()
        if path:
            self._spawn_status.configure(text=f"Custom Configuration Active: {Path(path).name}", foreground="#16a34a")
        else:
            self._spawn_status.configure(text="Using Standard Stock Spawn Tuning Matrix", foreground="#475569")

    def _clear_spawn_config(self) -> None:
        self.spawn_config_path_var.set("")
        self._refresh_spawn_status()

    def _open_spawn_editor(self) -> None:
        from .spawn_config import SpawnConfig, load_spawn_config, DEFAULT_CONFIG_FILENAME
        path = self.spawn_config_path_var.get().strip()
        config = load_spawn_config(path) or SpawnConfig.default()
        default_save = path or str(Path.cwd() / DEFAULT_CONFIG_FILENAME)

        def _on_save(saved_path: str) -> None:
            self.spawn_config_path_var.set(saved_path)
            self._refresh_spawn_status()

        SpawnRateEditor(self.root, config, default_save, _on_save)

    @staticmethod
    def _parse_inv(var: tk.StringVar) -> int | None:
        txt = var.get().strip()
        if not txt:
            return None
        try:
            return max(0, int(txt))
        except ValueError:
            return None

    def _build_actions_frame(self, parent: ttk.Frame) -> None:
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=(4, 6))
        self.run_btn = ttk.Button(f, text="Execute Randomizer Process", command=self._run_clicked, style="Accent.TButton")
        self.run_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(f, text="Open Target Folder", command=self._open_output).pack(side=tk.LEFT, padx=4)
        ttk.Button(f, text="Clear Console Log", command=self._clear_log).pack(side=tk.LEFT, padx=4)

    def _build_log_frame(self, parent: ttk.Frame) -> None:
        f = ttk.Labelframe(parent, text=" Processing Terminal Logs ", padding=6)
        f.pack(fill=tk.BOTH, expand=True, pady=2)
        
        sb = ttk.Scrollbar(f, orient=tk.VERTICAL)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # A moderate explicit height (bigger than the previous fixed 7 lines,
        # which was cramped enough that important diagnostic output like "ELF
        # patch: damage-taken patch NOT applied (reason: ...)" could scroll out
        # of view almost immediately) balanced against leaving room for the
        # options panel above. fill+expand still let it grow if the window is
        # resized taller.
        self.log_text = tk.Text(f, height=12, wrap=tk.WORD, font=("Consolas", 10), bg="#ffffff", fg="#0f172a", bd=1, relief="solid", highlightthickness=0, padx=8, pady=8)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        self.log_text.configure(yscrollcommand=sb.set)
        sb.configure(command=self.log_text.yview)

    def _build_status_bar(self, parent: ttk.Frame) -> None:
        self.status_var = tk.StringVar(value="Pipeline Ready.")
        sb = ttk.Label(parent, textvariable=self.status_var, anchor=tk.W, font=("Segoe UI", 9), foreground="#475569")
        sb.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))

    def _browse_iso(self) -> None:
        f = filedialog.askopenfilename(filetypes=[("Disc Images", "*.iso *.bin *.cue"), ("All Files", "*.*")])
        if f: 
            self.iso_var.set(f)
            p = Path(f)
            auto_output = p.parent / f"{p.stem}_randomized{p.suffix}"
            self.iso_output_var.set(str(auto_output))

    def _browse_iso_output(self) -> None:
        initial_dir = None
        initial_file = "Maximo - Ghosts to Glory (USA)_randomized.bin"
        current_out = self.iso_output_var.get().strip()
        current_in = self.iso_var.get().strip()
        
        if current_out:
            p = Path(current_out)
            initial_dir = str(p.parent)
            initial_file = p.name
        elif current_in:
            p = Path(current_in)
            initial_dir = str(p.parent)
            initial_file = f"{p.stem}_randomized{p.suffix}"

        f = filedialog.asksaveasfilename(
            initialdir=initial_dir,
            initialfile=initial_file,
            filetypes=[("Disc Images", "*.iso *.bin"), ("All Files", "*.*")]
        )
        if f: self.iso_output_var.set(f)

    def _browse_src(self) -> None:
        d = filedialog.askdirectory()
        if d: self.src_var.set(d)

    def _browse_out(self) -> None:
        d = filedialog.askdirectory()
        if d: self.out_var.set(d)

    def _roll_seed(self) -> None:
        self.seed_var.set(str(random.randint(100000, 999999)))

    def _on_all_worlds_toggle(self) -> None:
        val = bool(self.all_worlds_var.get())
        for w in self._all_worlds: self.world_vars[w].set(val)

    def _on_world_toggle(self) -> None:
        all_sel = all(self.world_vars[w].get() for w in self._all_worlds)
        self.all_worlds_var.set(all_sel)

    def _clear_log(self) -> None:
        try:
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.delete("1.0", tk.END)
            self.log_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    def _open_output(self) -> None:
        out_str = self.iso_output_var.get().strip()
        if self._notebook.index(self._notebook.select()) == 1:
            out_str = self.out_var.get().strip()
        if not out_str: out_str = self.iso_var.get().strip()
        p = Path(out_str)
        if p.is_file(): p = p.parent
        if p.is_dir():
            import subprocess
            if sys.platform == "win32": os.startfile(p)
            elif sys.platform == "darwin": subprocess.Popen(["open", str(p)])
            else: subprocess.Popen(["xdg-open", str(p)])
        else:
            messagebox.showerror("Destination Error", "The designated output path directory does not exist yet.")

    def _run_clicked(self) -> None:
        if self._running: return
        self._running = True
        self.run_btn.configure(state=tk.DISABLED)
        self.status_var.set("Compiling asset randomizer distributions parameters...")
        self._clear_log()
        threading.Thread(target=self._worker_thread, daemon=True).start()

    def _worker_thread(self) -> None:
        success = False
        err_msg = ""
        stream = _StreamToWidget(self.log_text, self.root)

        with redirect_stdout(stream), redirect_stderr(stream):
            try:
                active_tab = self._notebook.index(self._notebook.select())
                seed_str = self.seed_var.get().strip()
                seed_val = int(seed_str) if seed_str.isdigit() else random.randint(100000, 999999)
                w_list = [w for w in self._all_worlds if self.world_vars[w].get()]

                gold_val = self._parse_inv(self.start_gold_var)
                lives_val = self._parse_inv(self.start_lives_var)
                keys_val = self._parse_inv(self.start_keys_var)
                coins_val = self._parse_inv(self.start_deathcoins_var)
                skills_set = self._selected_skills()
                gate_m = self.gate_mode_var.get() if self.gate_rando_var.get() else None

                src_folder_str = self.src_var.get().strip()
                out_folder_str = self.out_var.get().strip()

                # Build a comprehensive options lookup block
                args = _Args(
                    seed=seed_val, items=bool(self.items_var.get()), chests=bool(self.chests_var.get()),
                    skills=bool(self.skills_var.get()), columns=bool(self.columns_var.get()),
                    spawn_location=bool(self.spawn_loc_var.get()), gate_randomizer=bool(self.gate_rando_var.get()),
                    gate_mode=gate_m, gen_tier=True, damage_taken=self.damage_taken_var.get(),
                    damage_dealt=self.damage_dealt_var.get(), start_gold=gold_val,
                    start_lives=lives_val, start_keys=keys_val, start_deathcoins=coins_val,
                    sword_enchant=self._sword_enchant_value(), elemental_shield=self._elemental_shield_value(),
                    randomize_start_inv=bool(self.randomize_start_inv_var.get()), starting_skills=skills_set,
                    start_skills=skills_set, harder_mode=bool(self.harder_mode_var.get()),
                    preserve_chests=bool(self.preserve_chests_var.get()), preserve_iron_keys=bool(self.preserve_iron_keys_var.get()),
                    randomize_levels=bool(self.randomize_levels_var.get()), randomize_levels_cross=bool(self.randomize_levels_var.get()),
                    enemies=bool(self.enemies_var.get()), no_enemies=not bool(self.enemies_var.get()),
                    duplicate_bosses=bool(self.dup_bosses_var.get()), boss_clones_grave=int(self.boss_clones_grave_var.get()),
                    boss_clones_swamp=int(self.boss_clones_swamp_var.get()), boss_clones_ice=int(self.boss_clones_ice_var.get()),
                    boss_clones_under=int(self.boss_clones_under_var.get()), boss_clones_castle=int(self.boss_clones_castle_var.get()),
                    exclude_dark_knight=bool(self.no_dark_knight_var.get()), cross_world_enemies=bool(self.cross_world_var.get()),
                    cross_world=bool(self.cross_world_var.get()), worlds=w_list, psx_folder=src_folder_str, output=out_folder_str,
                    spawn_config_path=self.spawn_config_path_var.get().strip() or None,
                    backup=bool(self.iso_backup_var.get()), make_backup=bool(self.iso_backup_var.get())
                )

                if active_tab == 0:
                    iso_in = self.iso_var.get().strip()
                    iso_out = self.iso_output_var.get().strip() or None
                    if not iso_in: raise ValueError("Source Maximo disc image target (.iso / .bin) is required.")
                    print(f"--- Launching ISO Patcher Module (Seed: {seed_val}) ---")

                    # Call patch_iso with EXPLICIT, named keyword arguments that
                    # match its real signature (iso_patcher.py) exactly. A prior
                    # version of this code built the kwargs dict by introspecting
                    # patch_iso's signature at runtime and guessing which local
                    # variable/attribute to use per parameter name (substring
                    # matching like "'damage' in param and 'taken' in param").
                    # That's fragile, unauditable, and silently drops/misroutes
                    # any parameter whose name doesn't match its heuristics or
                    # doesn't exist on `args` -- exactly the kind of bug class
                    # that can make "damage" / "starting skills" / other options
                    # silently no-op with no visible error. Explicit kwargs make
                    # every value's destination unambiguous and IDE/linter-
                    # checkable.
                    patch_iso(
                        iso_path=iso_in,
                        seed=seed_val,
                        items=bool(self.items_var.get()),
                        chests=bool(self.chests_var.get()),
                        skills=bool(self.skills_var.get()),
                        columns=bool(self.columns_var.get()),
                        spawn_location=bool(self.spawn_loc_var.get()),
                        gen_tier=True,
                        gate_mode=gate_m,
                        damage_taken=self.damage_taken_var.get(),
                        damage_dealt=self.damage_dealt_var.get(),
                        start_gold=gold_val,
                        start_lives=lives_val,
                        start_keys=keys_val,
                        start_deathcoins=coins_val,
                        sword_enchant=self._sword_enchant_value(),
                        elemental_shield=self._elemental_shield_value(),
                        start_skills=skills_set,
                        randomize_start_inv=bool(self.randomize_start_inv_var.get()),
                        randomize_levels=bool(self.randomize_levels_var.get()),
                        randomize_levels_cross=bool(self.randomize_levels_var.get()),
                        harder_mode=bool(self.harder_mode_var.get()),
                        preserve_chests=bool(self.preserve_chests_var.get()),
                        preserve_iron_keys=bool(self.preserve_iron_keys_var.get()),
                        spawn_config_path=self.spawn_config_path_var.get().strip() or None,
                        enemies=bool(self.enemies_var.get()),
                        cross_world=bool(self.cross_world_var.get()),
                        worlds=(set(w_list) if set(w_list) != set(self._all_worlds) else None),
                        duplicate_bosses=bool(self.dup_bosses_var.get()),
                        exclude_dark_knight=bool(self.no_dark_knight_var.get()),
                        boss_clones_grave=int(self.boss_clones_grave_var.get()),
                        boss_clones_swamp=int(self.boss_clones_swamp_var.get()),
                        boss_clones_ice=int(self.boss_clones_ice_var.get()),
                        boss_clones_under=int(self.boss_clones_under_var.get()),
                        boss_clones_castle=int(self.boss_clones_castle_var.get()),
                        output_iso=iso_out,
                        backup=bool(self.iso_backup_var.get()),
                        log=print,
                    )
                else:
                    if not src_folder_str or not out_folder_str: raise ValueError("Both Source and Output folder paths require operational entries.")
                    print(f"--- Launching Directory Architecture Shuffler (Seed: {seed_val}) ---")
                    # cmd_randomize(args) takes a SINGLE namespace argument (see
                    # cli.py) -- it reads args.psx_folder / args.output itself.
                    # A prior version of this code called
                    # cmd_randomize(src_folder_str, out_folder_str, args) with
                    # three positional arguments, which raises a hard TypeError
                    # every time Folder Mode was used.
                    args.worlds = ",".join(sorted(w_list)) if set(w_list) != set(self._all_worlds) else "all"
                    args.no_enemies = not bool(self.enemies_var.get())
                    cmd_randomize(args)
                success = True
            except Exception as e:
                import traceback
                traceback.print_exc()
                err_msg = str(e)

        self.root.after(0, self._worker_done, success, err_msg)

    def _worker_done(self, success: bool, err_msg: str) -> None:
        self._running = False
        self.run_btn.configure(state=tk.NORMAL)
        if success:
            self.status_var.set("Pipeline Finished: Operations successfully logged.")
            messagebox.showinfo("Execution Complete", "Randomization successfully!")
        else:
            self.status_var.set("Pipeline Error: Runtime exception aborted file serialization.")
            messagebox.showerror("Execution Fault", f"Process failure encountered:\n\n{err_msg}")


class SpawnRateEditor:
    """Modal-ish editor for the per-tag / per-world spawn weights."""
    def __init__(self, parent: tk.Tk, config, default_save_path: str, on_save):
        self.config = config
        self.on_save = on_save
        self.default_save_path = default_save_path
        self._rows = {}
        self._direct_keys = set()

        self.win = tk.Toplevel(parent)
        self.win.title("Spawn Rate Configuration")
        self.win.configure(bg="#f8fafc")
        self.win.transient(parent)
        
        try: scale = getattr(parent, "_scale", 1.0)
        except Exception: scale = 1.0
            
        self.win.geometry(f"{int(680 * scale)}x{int(720 * scale)}")
        self.win.minsize(int(580 * scale), int(500 * scale))
        self._build()
        self.win.lift()
        self.win.focus_force()
        self.win.grab_set()

    def _build(self) -> None:
        info = ttk.Label(
            self.win, justify=tk.LEFT, wraplength=600, foreground="#475569", font=("Segoe UI", 9),
            text=("Set each entry's spawn weight (0-100). 'Share' shows the "
                  "entry's chance within its group. Uncheck an entry (or set 0) "
                  "to stop it appearing. Enemies are split per world, and each "
                  "tier-eligible enemy has editable class levels (Lv 1/2/3)."),
        )
        info.pack(fill=tk.X, padx=12, pady=(12, 6))

        self._nb_container = ttk.Frame(self.win)
        self._nb_container.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        btns = ttk.Frame(self.win, padding=(12, 12))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        
        ttk.Button(btns, text="Save Settings", command=self._save, style="Accent.TButton").pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Save As...", command=self._save_as).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Cancel", command=self.win.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="Reset Defaults", command=self._reset).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Enable All Tabs", command=lambda: self._set_all_enabled(True)).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="Disable All Tabs", command=lambda: self._set_all_enabled(False)).pack(side=tk.LEFT, padx=4)

        self._build_tabs()

    def _build_tabs(self) -> None:
        from .spawn_config import ITEM_TYPES, STRUCTURE_TYPES, WORLDS, SPECIAL_NAMES
        
        for child in self._nb_container.winfo_children():
            child.destroy()
        self._rows = {}
        self._direct_keys = set()

        nb = ttk.Notebook(self._nb_container)
        nb.pack(fill=tk.BOTH, expand=True)

        self._build_category_tab(nb, "Items", ("items",), ITEM_TYPES, self.config.items)
        self._build_category_tab(nb, "Structures", ("structures",), STRUCTURE_TYPES, self.config.structures)
        self._build_category_tab(nb, "Mimics/Wizards", ("specials",), SPECIAL_NAMES, self.config.specials, direct_pct=True)

        enemies_tab = ttk.Frame(nb)
        nb.add(enemies_tab, text="Enemies")
        sub = ttk.Notebook(enemies_tab)
        sub.pack(fill=tk.BOTH, expand=True)
        for world in WORLDS:
            self._build_enemy_world_tab(sub, world)

    def _scrollable(self, notebook, title, toolbar_key=None):
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)
        
        if toolbar_key is not None:
            bar = ttk.Frame(outer, padding=4)
            bar.pack(fill=tk.X, side=tk.TOP)
            ttk.Button(bar, text="Enable This Tab", width=16, command=lambda k=toolbar_key: self._set_key_enabled(k, True)).pack(side=tk.LEFT, padx=2)
            ttk.Button(bar, text="Disable This Tab", width=16, command=lambda k=toolbar_key: self._set_key_enabled(k, False)).pack(side=tk.LEFT, padx=2)
        
        canvas = tk.Canvas(outer, highlightthickness=0, bg="#ffffff")
        scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        inner = ttk.Frame(canvas, style="MainViewport.TFrame")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        def _on_mousewheel(event):
            if sys.platform == "win32": canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            elif sys.platform == "darwin": canvas.yview_scroll(int(-1 * event.delta), "units")
        inner.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        inner.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _set_key_enabled(self, key, value: bool) -> None:
        for w_var, en_var, pct in self._rows.get(key, []):
            if en_var is not None: en_var.set(value)

    def _set_all_enabled(self, value: bool) -> None:
        for key, rows in self._rows.items():
            for w_var, en_var, pct in rows:
                if en_var is not None: en_var.set(value)

    def _header(self, inner, r: int, on_col: bool = True, value_label: str = "Share") -> None:
        if on_col: ttk.Label(inner, text="On", font=("Segoe UI", 10, "bold")).grid(row=r, column=0, padx=6, pady=4)
        ttk.Label(inner, text="Asset Target Name", font=("Segoe UI", 10, "bold"), anchor=tk.W).grid(row=r, column=1, sticky=tk.W, padx=6, pady=4)
        ttk.Label(inner, text="Weight", font=("Segoe UI", 10, "bold")).grid(row=r, column=2, padx=6, pady=4)
        ttk.Label(inner, text=value_label, font=("Segoe UI", 10, "bold")).grid(row=r, column=3, padx=6, pady=4)

    def _add_enable_row(self, inner, r, name, store, tid, key) -> None:
        ent = store[tid]
        en_var = tk.BooleanVar(value=bool(ent.get("enabled", True)))
        w_var = tk.DoubleVar(value=float(ent.get("weight", 0)))
        pct = ttk.Label(inner, text="", width=8)

        def _cb(*_a, tid=tid, en_var=en_var, w_var=w_var, store=store, key=key):
            try: v = float(w_var.get())
            except (tk.TclError, ValueError): return
            store[tid]["weight"] = max(0.0, min(100.0, v))
            store[tid]["enabled"] = bool(en_var.get())
            self._refresh_pct(key)

        en_var.trace_add("write", _cb)
        w_var.trace_add("write", _cb)

        cb = ttk.Checkbutton(inner, variable=en_var)
        cb.grid(row=r, column=0, padx=6, pady=2)
        ttk.Label(inner, text=name, anchor=tk.W).grid(row=r, column=1, sticky=tk.W, padx=6, pady=2)
        sp = ttk.Spinbox(inner, from_=0, to=100, increment=1, width=6, textvariable=w_var)
        sp.grid(row=r, column=2, padx=6, pady=2)
        pct.grid(row=r, column=3, padx=6, pady=2)

        if key not in self._rows: self._rows[key] = []
        self._rows[key].append((w_var, en_var, pct))

    def _build_category_tab(self, notebook, title, keys, types_list, store, direct_pct=False):
        key = keys[0]
        inner = self._scrollable(notebook, title, toolbar_key=key)
        self._header(inner, 0, value_label="%" if direct_pct else "Share")

        if direct_pct: self._direct_keys.add(key)
        # BUG FIX: ITEM_TYPES / STRUCTURE_TYPES / SPECIAL_NAMES are all
        # {type_id: display_name} dicts (see spawn_config.py). Iterating them
        # directly ("for tid in types_list") yields the dict's KEYS (the raw
        # numeric type IDs), not their names -- so every row was labeled with
        # a stringified integer (e.g. "43") instead of the real item name
        # (e.g. "Gold key"). Using .items() gets the actual display name.
        for r, (tid, display_name) in enumerate(types_list.items(), start=1):
            if tid not in store:
                continue
            self._add_enable_row(inner, r, display_name, store, tid, key)
        self._refresh_pct(key)

    def _build_enemy_world_tab(self, notebook, world):
        from .spawn_config import world_enemy_types
        inner = self._scrollable(notebook, world.capitalize(), toolbar_key=world)
        self._header(inner, 0, value_label="Share")

        # BUG FIX: world_enemy_types(world) returns a {type_id: display_name}
        # dict too -- same issue as _build_category_tab above. Use the dict's
        # value (the real enemy name) instead of str(enemy_id).
        enemies = world_enemy_types(world)
        store = self.config.enemies[world]
        r = 1
        for enemy_id, display_name in enemies.items():
            if enemy_id in store:
                self._add_enable_row(inner, r, display_name, store, enemy_id, world)
                r += 1
            else:
                for k in sorted(store.keys()):
                    if k == enemy_id or (isinstance(k, str) and k.startswith(f"{enemy_id}_")):
                        name = str(k).replace("_", " ").capitalize()
                        self._add_enable_row(inner, r, name, store, k, world)
                        r += 1
        self._refresh_pct(world)

    def _refresh_pct(self, key) -> None:
        rows = self._rows.get(key, [])
        if not rows: return
            
        if key in self._direct_keys:
            for w_var, en_var, pct_label in rows:
                pct_label.configure(text=f"{w_var.get():.1f}%" if en_var.get() else "0.0%")
            return

        total = sum(w_var.get() for w_var, en_var, _ in rows if en_var.get())
        for w_var, en_var, pct_label in rows:
            if en_var.get() and total > 0:
                share = (w_var.get() / total) * 100.0
                pct_label.configure(text=f"{share:.1f}%")
            else:
                pct_label.configure(text="0.0%")

    def _save(self) -> None:
        try:
            self.config.save(self.default_save_path)
            if self.on_save: self.on_save(self.default_save_path)
            self.win.destroy()
        except Exception as e:
            messagebox.showerror("Save Error", f"Failed to save spawn configuration maps:\n{e}")

    def _save_as(self) -> None:
        f = filedialog.asksaveasfilename(
            initialdir=str(Path(self.default_save_path).parent),
            initialfile=str(Path(self.default_save_path).name),
            filetypes=[("JSON files", "*.json"), ("All Files", "*.*")],
            defaultextension=".json"
        )
        if f:
            try:
                self.config.save(f)
                if self.on_save: self.on_save(f)
                self.win.destroy()
            except Exception as e:
                messagebox.showerror("Save Error", f"Failed to serialize file:\n{e}")

    def _reset(self) -> None:
        from .spawn_config import SpawnConfig
        self.config = SpawnConfig.default()
        self._build_tabs()


def main() -> None:
    _enable_dpi_awareness()
    root = tk.Tk()
    
    def _log_callback_exception(exc, val, tb):
        import traceback
        try:
            log_dir = (Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent)
            with open(log_dir / "gui_errors.log", "a", encoding="utf-8") as f:
                f.write("\n--- Tk callback exception ---\n")
                traceback.print_exception(exc, val, tb, file=f)
        except Exception: pass
    root.report_callback_exception = _log_callback_exception

    app = RandomizerApp(root)
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = getattr(app, "_scale", 1.0)
    w, h = int(1040 * scale), int(880 * scale)
    w, h = min(w, sw - 40), min(h, sh - 80)
    x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
    root.geometry(f"{w}x{h}+{x}+{y}")
    root.mainloop()


if __name__ == "__main__":
    main()