import ctypes
import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

import pythoncom
import uiautomation as auto


# ============================================================
# Configuration
# ============================================================

POLL_INTERVAL = 0.05
WINDOW_CHECK_INTERVAL = 0.5

MAX_CONTROL_DEPTH = 15

# Application window defaults.
DEFAULT_WINDOW_WIDTH = 400
DEFAULT_WINDOW_HEIGHT = 600
DEFAULT_DARK_MODE = True

SETTINGS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "LiveTranscriber"
)
SETTINGS_FILE = os.path.join(
    SETTINGS_DIR,
    "settings.json"
)

# Selection animation.
SELECTION_ANIMATION_INTERVAL = 45
SELECTION_ANIMATION_STEPS = 24

SELECTION_COLORS = [
    "#5E81AC",
    "#88C0D0",
    "#A3BE8C",
    "#EBCB8B",
    "#D08770",
    "#BF616A",
    "#B48EAD",
]


# ============================================================
# Win32
# ============================================================

user32 = ctypes.windll.user32

VK_LWIN = 0x5B
VK_CONTROL = 0x11
VK_L = 0x4C

KEYEVENTF_KEYUP = 0x0002

SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

# Keep Live Captions alive for UI Automation, but place its
# top-level window outside the visible desktop.
OFFSCREEN_X = -32000
OFFSCREEN_Y = -32000


# ============================================================
# Text normalization
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    text = text.replace("\r", " ")
    text = text.replace("\n", " ")

    # Remove Windows private-use characters.
    # Live Captions exposes some toolbar icons this way.
    text = "".join(
        ch
        for ch in text
        if not (0xE000 <= ord(ch) <= 0xF8FF)
    )

    # Normalize whitespace.
    parts = text.split()

    return " ".join(parts)


# ============================================================
# Live Captions keyboard toggle
# ============================================================

def toggle_live_captions():

    user32.keybd_event(
        VK_LWIN,
        0,
        0,
        0
    )

    user32.keybd_event(
        VK_CONTROL,
        0,
        0,
        0
    )

    user32.keybd_event(
        VK_L,
        0,
        0,
        0
    )

    user32.keybd_event(
        VK_L,
        0,
        KEYEVENTF_KEYUP,
        0
    )

    user32.keybd_event(
        VK_CONTROL,
        0,
        KEYEVENTF_KEYUP,
        0
    )

    user32.keybd_event(
        VK_LWIN,
        0,
        KEYEVENTF_KEYUP,
        0
    )


# ============================================================
# Find Live Captions window
# ============================================================

def find_live_captions_window():

    found = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    CALLBACK = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p
    )

    def callback(hwnd, _):

        if not user32.IsWindowVisible(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)

        if length <= 0:
            return True

        buffer = ctypes.create_unicode_buffer(
            length + 1
        )

        user32.GetWindowTextW(
            hwnd,
            buffer,
            length + 1
        )

        title = buffer.value.lower()

        if (
            "live captions" not in title
            and
            "live caption" not in title
        ):
            return True

        rect = RECT()

        if user32.GetWindowRect(
            hwnd,
            ctypes.byref(rect)
        ):

            width = max(
                0,
                rect.right - rect.left
            )

            height = max(
                0,
                rect.bottom - rect.top
            )

            area = width * height

        else:
            area = 0

        found.append(
            (area, hwnd)
        )

        return True

    user32.EnumWindows(
        CALLBACK(callback),
        0
    )

    if not found:
        return None

    found.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return found[0][1]


# ============================================================
# Live Captions window positioning
# ============================================================

def get_window_position(hwnd):
    try:
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()

        if not user32.GetWindowRect(
            hwnd,
            ctypes.byref(rect)
        ):
            return None

        return (
            rect.left,
            rect.top
        )

    except Exception:
        return None


def move_live_captions_offscreen(hwnd):

    try:

        if not hwnd or not user32.IsWindow(hwnd):
            return False

        return bool(
            user32.SetWindowPos(
                hwnd,
                0,
                OFFSCREEN_X,
                OFFSCREEN_Y,
                0,
                0,
                SWP_NOSIZE
                |
                SWP_NOACTIVATE
                |
                SWP_SHOWWINDOW
            )
        )

    except Exception:
        return False


def restore_live_captions_position(hwnd, position):
    try:
        if (
            not hwnd
            or not user32.IsWindow(hwnd)
            or not position
        ):
            return False

        x, y = position

        return bool(
            user32.SetWindowPos(
                hwnd,
                0,
                x,
                y,
                0,
                0,
                SWP_NOSIZE
                |
                SWP_NOACTIVATE
                |
                SWP_SHOWWINDOW
            )
        )

    except Exception:
        return False


# ============================================================
# Ensure Live Captions exists
# ============================================================

def launch_live_captions_app():
    """Launch Live Captions through the normal Windows app shell."""

    try:
        # Windows assigns the Live Captions app an AUMID.  Resolve it
        # from the Start menu so this works even when the package ID
        # differs between Windows builds.
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$app = Get-StartApps | "
                    "Where-Object { $_.Name -eq 'Live Captions' } | "
                    "Select-Object -First 1; "
                    "if ($app) { $app.AppID }"
                )
            ],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5
        )

        app_id = result.stdout.strip()

        if not app_id:
            return False

        subprocess.Popen(
            [
                "explorer.exe",
                f"shell:AppsFolder\\{app_id}"
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )

        return True

    except Exception:
        return False


def ensure_live_captions():

    hwnd = find_live_captions_window()

    if hwnd:
        return hwnd

    # Start Live Captions as its normal Windows app.
    # Keep the keyboard shortcut as a fallback for Windows builds
    # where Live Captions is not exposed through Get-StartApps.
    if not launch_live_captions_app():
        toggle_live_captions()

    deadline = time.monotonic() + 10

    while time.monotonic() < deadline:

        hwnd = find_live_captions_window()

        if hwnd:
            return hwnd

        time.sleep(0.2)

    return None


# ============================================================
# UI Automation helpers
# ============================================================

def get_root_rect(root):

    try:

        rect = root.BoundingRectangle

        return (
            rect.left,
            rect.top,
            rect.right,
            rect.bottom
        )

    except Exception:
        return None


def control_is_inside_root(
    control,
    root_rect
):

    try:

        rect = control.BoundingRectangle

        if not rect:
            return False

        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 30 or height <= 5:
            return False

        root_left, root_top, root_right, root_bottom = (
            root_rect
        )

        overlap_width = (
            min(rect.right, root_right)
            -
            max(rect.left, root_left)
        )

        overlap_height = (
            min(rect.bottom, root_bottom)
            -
            max(rect.top, root_top)
        )

        return (
            overlap_width > 30
            and
            overlap_height > 5
        )

    except Exception:
        return False


# ============================================================
# Discover Live Captions text controls
# ============================================================

def discover_caption_controls(hwnd):

    try:

        root = auto.ControlFromHandle(
            hwnd
        )

        if not root:
            return []

        root_rect = get_root_rect(root)

        if not root_rect:
            return []

        controls = []

        def walk(control, depth):

            if depth > MAX_CONTROL_DEPTH:
                return

            try:

                if (
                    control.ControlType
                    ==
                    auto.ControlType.TextControl
                ):

                    name = normalize_text(
                        control.Name
                    )

                    if (
                        name
                        and
                        control_is_inside_root(
                            control,
                            root_rect
                        )
                    ):

                        rect = control.BoundingRectangle

                        controls.append({
                            "control": control,
                            "name": name,
                            "left": rect.left,
                            "top": rect.top,
                            "width": (
                                rect.right
                                -
                                rect.left
                            ),
                            "height": (
                                rect.bottom
                                -
                                rect.top
                            )
                        })

            except Exception:
                pass

            try:

                children = control.GetChildren()

                for child in children:

                    walk(
                        child,
                        depth + 1
                    )

            except Exception:
                pass

        walk(root, 0)

        # ----------------------------------------------------
        # Remove duplicate UIA entries.
        # ----------------------------------------------------

        unique = []

        seen = set()

        for item in controls:

            key = (
                item["name"].lower(),
                round(item["top"] / 5),
                round(item["left"] / 5)
            )

            if key in seen:
                continue

            seen.add(key)

            unique.append(item)

        unique.sort(
            key=lambda item: (
                item["top"],
                item["left"]
            )
        )

        return unique

    except Exception:
        return []


# ============================================================
# Read current Live Captions transcript
# ============================================================

def read_caption(controls):

    candidates = []

    for item in controls:

        try:

            text = normalize_text(
                item["control"].Name
            )

            if not text:
                continue

            # Ignore icon-only controls.
            if all(
                0xE000 <= ord(ch) <= 0xF8FF
                for ch in text
            ):
                continue

            candidates.append({
                "text": text,
                "top": item["top"],
                "left": item["left"],
                "width": item["width"],
                "height": item["height"]
            })

        except Exception:
            continue

    if not candidates:
        return ""

    # --------------------------------------------------------
    # Exact duplicate controls.
    # --------------------------------------------------------

    unique = []

    seen = set()

    for item in candidates:

        key = (
            item["text"].lower(),
            round(item["top"] / 5),
            round(item["left"] / 5)
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(item)

    candidates = unique

    # --------------------------------------------------------
    # Remove controls whose entire text is contained in a
    # longer control.
    # --------------------------------------------------------

    filtered = []

    for candidate in candidates:

        candidate_text = candidate["text"].lower()

        contained = False

        for other in candidates:

            if candidate is other:
                continue

            other_text = other["text"].lower()

            if (
                len(other_text)
                >
                len(candidate_text)
                and
                candidate_text in other_text
            ):

                contained = True
                break

        if not contained:
            filtered.append(candidate)

    if not filtered:
        return ""

    # --------------------------------------------------------
    # Choose the longest actual caption.
    # --------------------------------------------------------

    filtered.sort(
        key=lambda item: (
            len(item["text"].split()),
            len(item["text"]),
            item["width"]
        ),
        reverse=True
    )

    return filtered[0]["text"]


# ============================================================
# Transcription worker
# ============================================================

class Transcriber(threading.Thread):

    def __init__(self, on_text):

        super().__init__(
            daemon=True
        )

        self.on_text = on_text

        self.running = True

        self.hwnd = None

        self.controls = []

        self.last_window_check = 0

        self.last_control_refresh = 0

        self.failed_reads = 0

        self.last_text = None

        self.last_offscreen_move = 0

        # Original visible position of Live Captions.
        self.original_position = None

    def stop(self):

        self.running = False

    def set_window(self, hwnd):

        if hwnd == self.hwnd:
            return

        self.hwnd = hwnd

        self.controls = []

        self.failed_reads = 0

        self.last_control_refresh = 0
        self.last_offscreen_move = 0

        # Capture the position before we move Live Captions off-screen.
        self.original_position = get_window_position(
            hwnd
        )

        if hwnd:
            move_live_captions_offscreen(
                hwnd
            )

    def restore_window(self):
        if not self.hwnd or not self.original_position:
            return

        restore_live_captions_position(
            self.hwnd,
            self.original_position
        )


    def refresh_controls(self):

        if not self.hwnd:
            return

        controls = discover_caption_controls(
            self.hwnd
        )

        if controls:

            self.controls = controls

            self.last_control_refresh = (
                time.monotonic()
            )

            self.failed_reads = 0

    def run(self):

        pythoncom.CoInitialize()

        try:

            while self.running:

                now = time.monotonic()

                # ------------------------------------------------
                # Find Live Captions.
                # ------------------------------------------------

                if (
                    self.hwnd is None
                    or
                    now - self.last_window_check
                    >= WINDOW_CHECK_INTERVAL
                ):

                    self.last_window_check = now

                    hwnd = (
                        find_live_captions_window()
                    )

                    if not hwnd:

                        hwnd = (
                            ensure_live_captions()
                        )

                    if hwnd:

                        self.set_window(
                            hwnd
                        )

                # ------------------------------------------------
                # Keep Live Captions off-screen.
                #
                # Windows Live Captions may reposition itself,
                # so periodically move the top-level window back
                # outside the visible desktop.
                # ------------------------------------------------

                if (
                    self.hwnd
                    and
                    now - self.last_offscreen_move >= 0.5
                ):

                    move_live_captions_offscreen(
                        self.hwnd
                    )

                    self.last_offscreen_move = now

                # ------------------------------------------------
                # Discover UIA controls.
                # ------------------------------------------------

                if not self.controls:

                    self.refresh_controls()

                # ------------------------------------------------
                # Read current transcript.
                # ------------------------------------------------

                caption = ""

                if self.controls:

                    try:

                        caption = read_caption(
                            self.controls
                        )

                    except Exception:

                        caption = ""

                if caption:

                    self.failed_reads = 0

                    # Live Captions snapshot is authoritative.
                    if caption != self.last_text:

                        self.last_text = caption

                        self.on_text(
                            caption
                        )

                else:

                    self.failed_reads += 1

                # ------------------------------------------------
                # Refresh UIA controls after repeated failures.
                # ------------------------------------------------

                if (
                    self.failed_reads >= 20
                    and
                    now
                    -
                    self.last_control_refresh
                    >= 1.0
                ):

                    self.controls = []

                    self.refresh_controls()

                time.sleep(
                    POLL_INTERVAL
                )

        finally:

            pythoncom.CoUninitialize()


# ============================================================
# GUI
# ============================================================

class Application:

    def __init__(self):

        self.root = tk.Tk()

        self.root.title(
            "Live Transcriber"
        )

        self.root.minsize(
            300,
            300
        )

        # Restore the user's previous window geometry and theme.
        # On the first launch, use a centered 400x600 window and
        # dark mode.
        settings = self.load_settings()

        self.dark = settings.get(
            "dark_mode",
            DEFAULT_DARK_MODE
        )

        self.apply_initial_geometry(
            settings.get("window")
        )

        # ----------------------------------------------------
        # Selection state.
        #
        # True while the user is selecting or while the
        # selection animation is running.
        # ----------------------------------------------------

        self.selection_active = False

        self.selection_animation_running = False

        self.build_ui()

        self.apply_theme()

        self.transcriber = Transcriber(
            self.set_transcript
        )

        self.transcriber.start()

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.close
        )

    # ========================================================
    # Persistent settings
    # ========================================================

    def load_settings(self):

        try:

            with open(
                SETTINGS_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                settings = json.load(file)

            if not isinstance(settings, dict):
                return {}

            if not isinstance(
                settings.get("dark_mode"),
                bool
            ):
                settings.pop("dark_mode", None)

            window = settings.get("window")

            if isinstance(window, dict):

                required = (
                    "width",
                    "height",
                    "x",
                    "y"
                )

                if not all(
                    isinstance(window.get(key), int)
                    for key in required
                ):
                    settings.pop("window", None)

                elif (
                    window["width"] < 300
                    or window["height"] < 300
                ):
                    settings.pop("window", None)

            else:
                settings.pop("window", None)

            return settings

        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError
        ):
            return {}

    def save_settings(self):

        try:

            self.root.update_idletasks()

            settings = {
                "dark_mode": bool(self.dark),
                "window": {
                    "width": int(self.root.winfo_width()),
                    "height": int(self.root.winfo_height()),
                    "x": int(self.root.winfo_x()),
                    "y": int(self.root.winfo_y())
                }
            }

            os.makedirs(
                SETTINGS_DIR,
                exist_ok=True
            )

            temp_file = SETTINGS_FILE + ".tmp"

            with open(
                temp_file,
                "w",
                encoding="utf-8"
            ) as file:

                json.dump(
                    settings,
                    file,
                    indent=2
                )

            os.replace(
                temp_file,
                SETTINGS_FILE
            )

        except (
            OSError,
            ValueError,
            TypeError
        ):
            pass

    def apply_initial_geometry(self, window):

        if isinstance(window, dict):

            width = window.get("width")
            height = window.get("height")
            x = window.get("x")
            y = window.get("y")

            if all(
                isinstance(value, int)
                for value in (width, height, x, y)
            ) and width >= 300 and height >= 300:

                self.root.geometry(
                    f"{width}x{height}+{x}+{y}"
                )

                return

        # First launch: exactly 400x600, centered on the primary screen.
        width = DEFAULT_WINDOW_WIDTH
        height = DEFAULT_WINDOW_HEIGHT

        self.root.geometry(
            f"{width}x{height}"
        )

        self.root.update_idletasks()

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        x = max(
            0,
            (screen_width - width) // 2
        )

        y = max(
            0,
            (screen_height - height) // 2
        )

        self.root.geometry(
            f"{width}x{height}+{x}+{y}"
        )

    # ========================================================
    # Build UI
    # ========================================================

    def build_ui(self):

        self.main = ttk.Frame(
            self.root
        )

        self.main.pack(
            fill="both",
            expand=True,
            padx=12,
            pady=12
        )

        # ----------------------------------------------------
        # Header
        # ----------------------------------------------------

        self.top = ttk.Frame(
            self.main
        )

        self.top.pack(
            fill="x",
            pady=(0, 8)
        )

        self.title_label = ttk.Label(
            self.top,
            text="Live Transcriber",
            font=(
                "Segoe UI",
                14,
                "bold"
            )
        )

        self.title_label.pack(
            side="left"
        )

        self.theme_button = ttk.Button(
            self.top,
            text="Dark Mode",
            command=self.toggle_theme
        )

        self.theme_button.pack(
            side="right"
        )

        # ----------------------------------------------------
        # Transcript area
        # ----------------------------------------------------

        self.text_frame = ttk.Frame(
            self.main
        )

        self.text_frame.pack(
            fill="both",
            expand=True
        )

        self.text = tk.Text(
            self.text_frame,
            wrap="word",
            undo=False,
            borderwidth=0,
            highlightthickness=0,
            padx=12,
            pady=12,
            font=(
                "Segoe UI",
                11
            )
        )

        # Mouse selection.
        self.text.bind(
            "<ButtonPress-1>",
            self.begin_selection
        )

        self.text.bind(
            "<ButtonRelease-1>",
            self.end_selection
        )

        # Keyboard copy.
        self.text.bind(
            "<Control-c>",
            self.copy_selection
        )

        self.scrollbar = ttk.Scrollbar(
            self.text_frame,
            orient="vertical",
            command=self.text.yview
        )

        self.text.configure(
            yscrollcommand=self.scrollbar.set
        )

        self.text.pack(
            side="left",
            fill="both",
            expand=True
        )

        self.scrollbar.pack(
            side="right",
            fill="y"
        )

    # ========================================================
    # Begin selection
    # ========================================================

    def begin_selection(self, event=None):

        # Freeze transcript replacement immediately.
        self.selection_active = True

        # Remove any previous animation tag.
        try:

            self.text.tag_remove(
                "copy_flash",
                "1.0",
                "end"
            )

        except tk.TclError:
            pass

    # ========================================================
    # End selection
    # ========================================================

    def end_selection(self, event=None):

        try:

            # Wait one event-loop cycle so Tkinter has finished
            # updating sel.first / sel.last.
            self.root.after(
                1,
                self.finish_selection
            )

        except tk.TclError:
            pass

    # ========================================================
    # Finish selection
    # ========================================================

    def finish_selection(self):

        try:

            start = self.text.index(
                "sel.first"
            )

            end = self.text.index(
                "sel.last"
            )

            selected_text = self.text.get(
                start,
                end
            )

            if not selected_text:
                self.selection_active = False
                return

            # ------------------------------------------------
            # Copy immediately.
            # ------------------------------------------------

            self.root.clipboard_clear()

            self.root.clipboard_append(
                selected_text
            )

            self.root.update()

            # ------------------------------------------------
            # Remove Tk's normal selection highlight.
            #
            # We keep the range ourselves so we can animate it.
            # ------------------------------------------------

            self.text.tag_remove(
                "sel",
                "1.0",
                "end"
            )

            # ------------------------------------------------
            # Start visual confirmation animation.
            # ------------------------------------------------

            self.animate_copy(
                start,
                end,
                0,
                0
            )

        except tk.TclError:

            self.selection_active = False

    # ========================================================
    # Animate copied selection
    # ========================================================

    def animate_copy(
        self,
        start,
        end,
        step,
        color_index
    ):

        try:

            # ------------------------------------------------
            # Animation finished.
            # ------------------------------------------------

            if step >= SELECTION_ANIMATION_STEPS:

                self.text.tag_remove(
                    "copy_flash",
                    start,
                    end
                )

                self.selection_animation_running = False
                self.selection_active = False

                return

            self.selection_animation_running = True

            # ------------------------------------------------
            # Configure the animation tag.
            # ------------------------------------------------

            self.text.tag_configure(
                "copy_flash",
                background=SELECTION_COLORS[
                    color_index
                    %
                    len(SELECTION_COLORS)
                ],
                foreground="#FFFFFF"
            )

            self.text.tag_add(
                "copy_flash",
                start,
                end
            )

            # ------------------------------------------------
            # Move through the colors.
            # ------------------------------------------------

            next_color = (
                color_index + 1
            )

            # ------------------------------------------------
            # Last few frames gradually remove the highlight.
            #
            # Tkinter doesn't provide alpha blending for Text
            # tags, so we simulate a fade by switching through
            # increasingly subtle colors.
            # ------------------------------------------------

            fade_start = (
                SELECTION_ANIMATION_STEPS - 8
            )

            if step >= fade_start:

                fade_colors = [
                    "#D9D9D9",
                    "#E3E3E3",
                    "#EAEAEA",
                    "#EFEFEF",
                    "#F3F3F3",
                    "#F7F7F7",
                    "#FAFAFA",
                    "#FFFFFF",
                ]

                if self.dark:

                    fade_colors = [
                        "#4A4A4A",
                        "#454545",
                        "#414141",
                        "#3D3D3D",
                        "#393939",
                        "#353535",
                        "#313131",
                        "#2D2D2D",
                    ]

                fade_index = (
                    step - fade_start
                )

                if fade_index >= len(fade_colors):

                    fade_index = (
                        len(fade_colors) - 1
                    )

                self.text.tag_configure(
                    "copy_flash",
                    background=fade_colors[
                        fade_index
                    ],
                    foreground=(
                        "#202124"
                        if not self.dark
                        else "#E8EAED"
                    )
                )

            # ------------------------------------------------
            # Schedule next frame.
            # ------------------------------------------------

            self.root.after(
                SELECTION_ANIMATION_INTERVAL,
                lambda: self.animate_copy(
                    start,
                    end,
                    step + 1,
                    next_color
                )
            )

        except tk.TclError:

            self.selection_animation_running = False
            self.selection_active = False

    # ========================================================
    # Copy selected transcript text
    # ========================================================

    def copy_selection(self, event=None):

        try:

            start = self.text.index(
                "sel.first"
            )

            end = self.text.index(
                "sel.last"
            )

            selected_text = self.text.get(
                start,
                end
            )

            if not selected_text:
                return "break"

            self.root.clipboard_clear()

            self.root.clipboard_append(
                selected_text
            )

            self.root.update()

            # Remove native selection.
            self.text.tag_remove(
                "sel",
                "1.0",
                "end"
            )

            # Start the same visual confirmation.
            self.selection_active = True

            self.animate_copy(
                start,
                end,
                0,
                0
            )

        except tk.TclError:
            pass

        return "break"

    # ========================================================
    # Theme
    # ========================================================

    def apply_theme(self):

        if self.dark:

            background = "#202124"
            foreground = "#E8EAED"
            text_background = "#292A2D"
            button_background = "#3C4043"
            button_foreground = "#FFFFFF"
            trough = "#202124"
            slider = "#5F6368"

        else:

            background = "#F5F5F5"
            foreground = "#202124"
            text_background = "#FFFFFF"
            button_background = "#E8EAED"
            button_foreground = "#202124"
            trough = "#F5F5F5"
            slider = "#BDBDBD"

        self.root.configure(
            background=background
        )

        style = ttk.Style(
            self.root
        )

        style.theme_use(
            "clam"
        )

        style.configure(
            "TFrame",
            background=background
        )

        style.configure(
            "TLabel",
            background=background,
            foreground=foreground
        )

        style.configure(
            "TButton",
            background=button_background,
            foreground=button_foreground,
            borderwidth=0,
            padding=(12, 7)
        )

        style.map(
            "TButton",
            background=[
                (
                    "active",
                    button_background
                ),
                (
                    "pressed",
                    button_background
                )
            ],
            foreground=[
                (
                    "active",
                    button_foreground
                ),
                (
                    "pressed",
                    button_foreground
                )
            ]
        )

        style.configure(
            "Vertical.TScrollbar",
            background=slider,
            troughcolor=trough,
            bordercolor=background,
            arrowcolor=foreground
        )

        self.text.configure(
            background=text_background,
            foreground=foreground,
            insertbackground=foreground,
            selectbackground="#666666",
            selectforeground="#FFFFFF"
        )

        self.theme_button.configure(
            text=(
                "Light Mode"
                if self.dark
                else "Dark Mode"
            )
        )

    def toggle_theme(self):

        self.dark = not self.dark

        self.apply_theme()

        # Remember the theme immediately, so a later launch
        # uses the user's most recent choice even if the app
        # is closed unexpectedly afterward.
        self.save_settings()

    # ========================================================
    # Replace transcript
    # ========================================================

    def set_transcript(self, text):

        if not text:
            return

        def update():

            try:

                # ------------------------------------------------
                # Do absolutely nothing while the user has a
                # selection or while its copy animation is running.
                # ------------------------------------------------

                if self.selection_active:
                    return

                current = self.text.get(
                    "1.0",
                    "end-1c"
                )

                if current == text:
                    return

                # ------------------------------------------------
                # Replace, don't append.
                # ------------------------------------------------

                self.text.delete(
                    "1.0",
                    "end"
                )

                self.text.insert(
                    "1.0",
                    text
                )

                self.text.see(
                    "end"
                )

            except tk.TclError:
                pass

        try:

            self.root.after(
                0,
                update
            )

        except tk.TclError:
            pass

    # ========================================================
    # Shutdown
    # ========================================================

    def close(self):

        # Persist the final window position, size, and theme.
        self.save_settings()

        if hasattr(
            self,
            "transcriber"
        ):

            self.transcriber.stop()

            # Put Live Captions back exactly where it was
            # before this application moved it off-screen.
            self.transcriber.restore_window()

        self.root.destroy()

    # ========================================================
    # Run
    # ========================================================

    def run(self):

        self.root.mainloop()


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    app = Application()

    app.run()