import zipfile, os

BASE = r"D:\nugo-standard-middleware"
OUT  = os.path.join(BASE, "function.zip")

EXCLUDE_FROM_PACKAGE = {"handler.py", "sf_client.py", "secrets_manager.py", "adapters"}

def add_dir(zf, src_dir, arc_prefix="", exclude_top=None):
    exclude_top = exclude_top or set()
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        top = rel_root.split(os.sep)[0]
        if top in exclude_top:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            if rel_root == "." and f in exclude_top:
                continue
            full = os.path.join(root, f)
            arc  = os.path.join(arc_prefix, os.path.relpath(full, src_dir)) if arc_prefix else os.path.relpath(full, src_dir)
            zf.write(full, arc)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
    # archivos raíz
    for f in ["handler.py", "sf_client.py", "secrets_manager.py"]:
        zf.write(os.path.join(BASE, f), f)
    # adapters/ tal cual (versión actualizada)
    add_dir(zf, os.path.join(BASE, "adapters"), "adapters")
    # package/ aplanado, sin sobreescribir los archivos propios
    add_dir(zf, os.path.join(BASE, "package"), "", exclude_top=EXCLUDE_FROM_PACKAGE)

print(f"ZIP creado: {OUT}")
