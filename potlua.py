import os
import sys
import shutil
import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "potlua"
AUTHOR = "Hrazhub"


def steam_root():
    if winreg is not None:
        for hive, path, names in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath",)),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ("InstallPath",)),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", ("InstallPath",)),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    for name in names:
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                        except OSError:
                            continue
                        if value and os.path.isdir(value):
                            return os.path.normpath(value)
            except OSError:
                continue
    for candidate in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(candidate):
            return candidate
    return None


def tokenize(text):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n:
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt))
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            tokens.append("".join(buf))
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n{}"':
            j += 1
        tokens.append(text[i:j])
        i = j
    return tokens


def parse_vdf(text):
    tokens = tokenize(text)
    pos = 0

    def parse_block():
        nonlocal pos
        result = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return result
            if tok == "{":
                pos += 1
                continue
            key = tok
            pos += 1
            if pos < len(tokens) and tokens[pos] == "{":
                pos += 1
                result[key] = parse_block()
            elif pos < len(tokens) and tokens[pos] != "}":
                result[key] = tokens[pos]
                pos += 1
            else:
                result[key] = ""
        return result

    return parse_block()


def get_ci(mapping, *keys):
    if not isinstance(mapping, dict):
        return ""
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return ""


def discover_libraries(root):
    libs = []
    lf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(lf):
        try:
            with open(lf, encoding="utf-8", errors="replace") as handle:
                data = parse_vdf(handle.read())
        except OSError:
            data = {}
        folders = get_ci(data, "libraryfolders", "LibraryFolders")
        if isinstance(folders, dict):
            for value in folders.values():
                if isinstance(value, dict):
                    path = get_ci(value, "path")
                    if path:
                        libs.append(path)
                elif isinstance(value, str) and os.path.isdir(value):
                    libs.append(value)
    libs.insert(0, root)
    seen = set()
    out = []
    for path in libs:
        try:
            real = os.path.normcase(os.path.abspath(path))
        except (OSError, ValueError):
            continue
        if real in seen or not os.path.isdir(path):
            continue
        seen.add(real)
        out.append(os.path.normpath(path))
    return out


def read_games(libraries):
    games = []
    for lib in libraries:
        steamapps = os.path.join(lib, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        try:
            entries = os.listdir(steamapps)
        except OSError:
            continue
        for filename in entries:
            lower = filename.lower()
            if not lower.startswith("appmanifest_") or not lower.endswith(".acf"):
                continue
            manifest = os.path.join(steamapps, filename)
            try:
                with open(manifest, encoding="utf-8", errors="replace") as handle:
                    data = parse_vdf(handle.read())
            except OSError:
                continue
            state = get_ci(data, "AppState", "appstate")
            appid = get_ci(state, "appid") or filename[12:-4]
            name = get_ci(state, "name") or "App {}".format(appid)
            try:
                size = int(get_ci(state, "SizeOnDisk") or 0)
            except (TypeError, ValueError):
                size = 0
            try:
                last = int(get_ci(state, "LastUpdated", "LastPlayed") or 0)
            except (TypeError, ValueError):
                last = 0
            try:
                flags = int(get_ci(state, "StateFlags") or 0)
            except (TypeError, ValueError):
                flags = 0
            installdir = get_ci(state, "installdir")
            install_path = os.path.join(steamapps, "common", installdir) if installdir else ""
            games.append({
                "appid": str(appid),
                "name": str(name),
                "size": size,
                "last": last,
                "flags": flags,
                "library": lib,
                "manifest": manifest,
                "install_path": install_path,
                "integrity": "?",
                "findings": [],
            })
    games.sort(key=lambda g: g["name"].lower())
    return games


def human_size(num):
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < step or unit == "TB":
            if unit == "B":
                return "{} B".format(int(num))
            return "{:.2f} {}".format(num, unit)
        num /= step
    return "{} B".format(int(num))


def state_text(flags):
    if flags == 4:
        return "Installed"
    if flags & 4:
        return "Installed (partial)"
    if flags & 2:
        return "Update needed"
    if flags & 1:
        return "Uninstalled"
    return "Unknown"


def when_text(ts):
    if not ts:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "-"


EMULATOR_MARKERS = (
    "goldberg",
    "smartsteamemu",
    "onlinefix",
    "creamapi",
    "smokeapi",
    "coldclientloader",
    "steam_settings",
)

GROUP_MARKERS = {
    "codex",
    "rune",
    "skidrow",
    "plaza",
    "empress",
    "cpy",
    "reloaded",
    "flt",
    "tenoke",
    "razor1911",
    "dodi",
    "fitgirl",
    "unsteam",
}


def scan_game(game):
    path = game.get("install_path") or ""
    findings = []
    if not path or not os.path.isdir(path):
        return "Unknown", findings
    apis = []
    files_seen = 0
    for dirpath, dirs, files in os.walk(path, onerror=lambda _e: None):
        depth = dirpath[len(path):].count(os.sep)
        if depth >= 2:
            dirs[:] = []
        else:
            for folder in list(dirs):
                low = folder.lower()
                if any(marker in low for marker in EMULATOR_MARKERS):
                    findings.append(("high", "Folder matches emulator marker: {}".format(folder)))
                elif low in GROUP_MARKERS:
                    findings.append(("medium", "Folder matches release group: {}".format(folder)))
        for name in files:
            files_seen += 1
            low = name.lower()
            full = os.path.join(dirpath, name)
            if low in ("steam_api.dll", "steam_api64.dll"):
                apis.append(full)
            if low == "steam_appid.txt":
                findings.append(("medium", "steam_appid.txt present (may bypass ownership check)"))
                continue
            if any(marker in low for marker in EMULATOR_MARKERS):
                findings.append(("high", "File matches emulator marker: {}".format(name)))
            elif low.split(".")[0] in GROUP_MARKERS:
                findings.append(("medium", "File matches release group: {}".format(name)))
    for api in apis:
        try:
            with open(api, "rb") as handle:
                data = handle.read(4 * 1024 * 1024).lower()
        except OSError:
            continue
        label = os.path.basename(api)
        if b"goldberg" in data or b"steamemu" in data:
            findings.append(("high", "{} appears to be an emulator build".format(label)))
        elif b"valve" not in data:
            findings.append(("medium", "{} is not a recognized Valve build".format(label)))
    findings = list(dict.fromkeys(findings))
    if files_seen == 0:
        return "Unknown", findings
    if any(severity == "high" for severity, _ in findings):
        return "Suspicious", findings
    if findings:
        return "Modified", findings
    return "No indicators", findings


class PotluaApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("{} - Steam Library Manager  (by {})".format(APP_NAME, AUTHOR))
        self.geometry("1080x640")
        self.minsize(860, 480)
        self.games = []
        self.visible = []
        self.sort_key = "name"
        self.sort_desc = False
        self.steam_path = None
        self.libraries = []
        self._build_style()
        self._build_ui()
        self.refresh()

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        bg = "#1e2228"
        panel = "#262b33"
        fg = "#e6e9ef"
        accent = "#4f8cff"
        self.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground=panel)
        style.configure("TFrame", background=bg)
        style.configure("Panel.TFrame", background=panel)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Muted.TLabel", background=bg, foreground="#9aa4b2")
        style.configure("TButton", background=panel, foreground=fg, padding=(10, 4))
        style.map("TButton", background=[("active", accent)], foreground=[("active", "#ffffff")])
        style.configure("TEntry", fieldbackground=panel, foreground=fg, insertcolor=fg)
        style.configure("Treeview", background=panel, fieldbackground=panel, foreground=fg, rowheight=24, borderwidth=0)
        style.configure("Treeview.Heading", background="#333a45", foreground=fg, relief="flat", padding=(6, 4))
        style.map("Treeview", background=[("selected", accent)], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#3d4552")])

    def _build_ui(self):
        toolbar = ttk.Frame(self, padding=(10, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Search").pack(side="left")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(toolbar, textvariable=self.search_var, width=36)
        entry.pack(side="left", padx=(6, 12))
        entry.bind("<KeyRelease>", lambda _e: self.apply_filter())
        ttk.Button(toolbar, text="Refresh", command=self.refresh).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Backup Manifests", command=self.backup_manifests).pack(side="left")
        self.path_label = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.path_label.pack(side="right")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        left = ttk.Frame(body)
        columns = ("name", "appid", "size", "status", "integrity", "last")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        headings = {
            "name": "Name",
            "appid": "AppID",
            "size": "Size",
            "status": "Status",
            "integrity": "Integrity",
            "last": "Last Updated",
        }
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self.sort_by(c))
        self.tree.column("name", width=300, anchor="w")
        self.tree.column("appid", width=80, anchor="e")
        self.tree.column("size", width=95, anchor="e")
        self.tree.column("status", width=125, anchor="w")
        self.tree.column("integrity", width=120, anchor="w")
        self.tree.column("last", width=135, anchor="w")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.show_details())
        self.tree.bind("<Double-1>", lambda _e: self.launch())

        right = ttk.Frame(body, padding=(12, 8))
        self.detail = tk.Text(right, bg="#262b33", fg="#e6e9ef", bd=0, relief="flat", wrap="word", padx=10, pady=10)
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")

        actions = ttk.Frame(self, padding=(10, 0, 10, 10))
        actions.pack(fill="x")
        ttk.Button(actions, text="Launch", command=self.launch).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Verify Files", command=self.verify).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Integrity Scan", command=self.scan_all).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Open Install Folder", command=self.open_folder).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Open Manifest", command=self.open_manifest).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Copy AppID", command=self.copy_appid).pack(side="left")

        body.add(left, weight=3)
        body.add(right, weight=2)

        self.status = ttk.Label(self, text="", padding=(10, 4), style="Muted.TLabel")
        self.status.pack(fill="x", side="bottom")

    def refresh(self):
        self.steam_path = steam_root()
        if not self.steam_path:
            self.games = []
            self.libraries = []
            self.status.configure(text="Steam installation not found.")
            self.path_label.configure(text="not found")
            messagebox.showwarning(APP_NAME, "Steam installation was not found on this system.")
        else:
            self.libraries = discover_libraries(self.steam_path)
            self.games = read_games(self.libraries)
            total = sum(g["size"] for g in self.games)
            self.status.configure(
                text="{} games across {} libraries - {}".format(len(self.games), len(self.libraries), human_size(total))
            )
            self.path_label.configure(text=self.steam_path)
        self.apply_filter()

    def apply_filter(self):
        needle = self.search_var.get().strip().lower()
        self.visible = [g for g in self.games if needle in g["name"].lower() or needle in g["appid"]]
        self.render()

    def sort_by(self, key):
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            self.sort_desc = False
        self.render()

    def render(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        key = self.sort_key
        reverse = self.sort_desc
        self.visible.sort(key=lambda g: g[key].lower() if key == "name" else g[key], reverse=reverse)
        for game in self.visible:
            self.tree.insert(
                "",
                "end",
                iid=game["appid"],
                values=(
                    game["name"],
                    game["appid"],
                    human_size(game["size"]),
                    state_text(game["flags"]),
                    game["integrity"],
                    when_text(game["last"]),
                ),
            )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", "Select a game to see details.\n")
        self.detail.configure(state="disabled")

    def selected(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "Select a game first.")
            return None
        appid = selection[0]
        for game in self.games:
            if game["appid"] == appid:
                return game
        return None

    def show_details(self):
        game = None
        selection = self.tree.selection()
        if selection:
            for entry in self.games:
                if entry["appid"] == selection[0]:
                    game = entry
                    break
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if not game:
            self.detail.insert("end", "Select a game to see details.\n")
            self.detail.configure(state="disabled")
            return
        if game["integrity"] == "?":
            game["integrity"], game["findings"] = scan_game(game)
            if self.tree.exists(game["appid"]):
                values = list(self.tree.item(game["appid"], "values"))
                values[4] = game["integrity"]
                self.tree.item(game["appid"], values=values)
        lines = [
            ("Name", game["name"]),
            ("AppID", game["appid"]),
            ("Status", state_text(game["flags"])),
            ("Integrity", game["integrity"]),
            ("Size on disk", human_size(game["size"])),
            ("Last updated", when_text(game["last"])),
            ("Library", game["library"]),
            ("Install folder", game["install_path"] or "-"),
            ("Manifest", game["manifest"]),
        ]
        for label, value in lines:
            self.detail.insert("end", "{}\n".format(label), ("label",))
            self.detail.insert("end", "{}\n\n".format(value))
        self.detail.tag_configure("label", foreground="#9aa4b2")
        if game["findings"]:
            self.detail.insert("end", "Findings\n", ("label",))
            for severity, message in game["findings"]:
                self.detail.insert("end", "  [{}] {}\n".format(severity, message), (severity,))
            self.detail.tag_configure("high", foreground="#ff6b6b")
            self.detail.tag_configure("medium", foreground="#f0b429")
        elif game["integrity"] != "Unknown":
            self.detail.insert("end", "No crack or tamper indicators found.\n", ("ok",))
            self.detail.tag_configure("ok", foreground="#57c785")
        self.detail.configure(state="disabled")

    def launch(self):
        game = self.selected()
        if game:
            os.startfile("steam://rungameid/{}".format(game["appid"]))

    def verify(self):
        game = self.selected()
        if game:
            os.startfile("steam://validate/{}".format(game["appid"]))
            self.status.configure(text="Requested file verification for {}.".format(game["name"]))

    def open_folder(self):
        game = self.selected()
        if not game:
            return
        target = game["install_path"] if os.path.isdir(game["install_path"]) else os.path.join(game["library"], "steamapps")
        if os.path.isdir(target):
            os.startfile(target)

    def open_manifest(self):
        game = self.selected()
        if game and os.path.isfile(game["manifest"]):
            os.startfile(game["manifest"])

    def copy_appid(self):
        game = self.selected()
        if game:
            self.clipboard_clear()
            self.clipboard_append(game["appid"])
            self.status.configure(text="Copied AppID {}.".format(game["appid"]))

    def scan_all(self):
        if not self.games:
            messagebox.showinfo(APP_NAME, "No games to scan.")
            return
        self.status.configure(text="Scanning {} games...".format(len(self.games)))
        self.update_idletasks()
        counts = {}
        for game in self.games:
            game["integrity"], game["findings"] = scan_game(game)
            counts[game["integrity"]] = counts.get(game["integrity"], 0) + 1
            if self.tree.exists(game["appid"]):
                values = list(self.tree.item(game["appid"], "values"))
                values[4] = game["integrity"]
                self.tree.item(game["appid"], values=values)
        parts = ", ".join("{} {}".format(count, label) for label, count in sorted(counts.items()))
        self.status.configure(text="Integrity scan complete: " + parts)
        self.show_details()

    def backup_manifests(self):
        if not self.games:
            messagebox.showinfo(APP_NAME, "Nothing to back up.")
            return
        target = filedialog.askdirectory(title="Choose backup folder")
        if not target:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(target, "potlua_manifests_{}".format(stamp))
        try:
            os.makedirs(dest, exist_ok=True)
            copied = 0
            for game in self.games:
                if os.path.isfile(game["manifest"]):
                    shutil.copy2(game["manifest"], os.path.join(dest, os.path.basename(game["manifest"])))
                    copied += 1
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.status.configure(text="Backed up {} manifests to {}".format(copied, dest))
        messagebox.showinfo(APP_NAME, "Backed up {} manifests to:\n{}".format(copied, dest))


def main():
    app = PotluaApp()
    app.mainloop()


if __name__ == "__main__":
    main()
