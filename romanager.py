"""Applies the splash-screen fix to romanager.py.

Usage:  python patch_romanager.py            (patches romanager.py in this folder)
        python patch_romanager.py path\\to\\romanager.py
A backup is saved as romanager.py.bak before anything is changed.
"""
import shutil
import sys
from pathlib import Path

OLD = """        for w in self.winfo_children():
            w.destroy()
        self.toasts = []
"""
NEW = """        for w in self.winfo_children():
            if isinstance(w, tk.Toplevel):  # keep the splash (and any open dialogs) alive
                continue
            w.destroy()
        self.toasts = []
"""

path = Path(sys.argv[1] if len(sys.argv) > 1 else "romanager.py")
if not path.exists():
    sys.exit(f"File not found: {path}")

text = path.read_text(encoding="utf-8")
if "keep the splash (and any open dialogs) alive" in text:
    sys.exit("Already patched, nothing to do.")

norm = text.replace("\r\n", "\n")
if norm.count(OLD) != 1:
    sys.exit(f"Could not find the expected block exactly once (found {norm.count(OLD)}). "
             "Apply the edit by hand in App.build().")

shutil.copy2(path, path.with_name(path.name + ".bak"))
new_text = norm.replace(OLD, NEW)
if "\r\n" in text:
    new_text = new_text.replace("\n", "\r\n")
path.write_text(new_text, encoding="utf-8", newline="")
print(f"Patched {path} (backup: {path.name}.bak)")
