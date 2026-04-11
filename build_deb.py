import tarfile
import os
import io
import subprocess
import time

VERSION = "1.0.0"
DEB_NAME = f"anilix_{VERSION}_all.deb"

# 1. Create control files
control_content = f"""Package: anilix
Version: {VERSION}
Architecture: all
Maintainer: Abhinav Rajpati
Depends: curl, python3, python3-venv, mpv
Description: Anilix Terminal Anime Interface
"""

postinst_content = """#!/bin/bash
set -e
cd /opt/anilix

# Install uv globally if not available
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    # Make uv available globally for all users just in case
    ln -sf $(which uv) /usr/local/bin/uv || true
fi

# We use uv to sync dependencies into the venv it automatically manages.
uv sync

# Ensure the .venv and all files are world-readable/executable so the normal user can run it
chmod -R a+rX /opt/anilix
exit 0
"""

wrapper_script = """#!/bin/bash
cd /opt/anilix
if [ ! -f ".venv/bin/python" ]; then
    echo "Error: Virtual environment not found at /opt/anilix/.venv"
    echo "The installation script might have failed to run 'uv sync'."
    exit 1
fi
exec .venv/bin/python anilix.py "$@"
"""

def reset_tarinfo(tarinfo):
    tarinfo.uid = 0
    tarinfo.gid = 0
    tarinfo.uname = "root"
    tarinfo.gname = "root"
    return tarinfo

def add_content(tar, name, content, mode=0o644, type=tarfile.REGTYPE):
    content_bytes = content.encode('utf-8')
    t = tarfile.TarInfo(name=name)
    t.size = len(content_bytes)
    t.mode = mode
    t.type = type
    t.mtime = int(time.time())
    t = reset_tarinfo(t)
    tar.addfile(t, io.BytesIO(content_bytes))

def add_dir(tar, name, mode=0o755):
    t = tarfile.TarInfo(name=name)
    t.type = tarfile.DIRTYPE
    t.mode = mode
    t.mtime = int(time.time())
    t = reset_tarinfo(t)
    tar.addfile(t)

print("Creating control.tar.gz...")
with tarfile.open("control.tar.gz", "w:gz", format=tarfile.GNU_FORMAT) as tar:
    # Explicitly create root dir if required, though dpkg handles missing dirs.
    add_content(tar, "control", control_content, mode=0o644)
    add_content(tar, "postinst", postinst_content, mode=0o755)

print("Creating data.tar.gz...")
with tarfile.open("data.tar.gz", "w:gz", format=tarfile.GNU_FORMAT) as tar:
    # Explicitly add directories for dpkg
    add_dir(tar, "opt")
    add_dir(tar, "opt/anilix")
    add_dir(tar, "usr")
    add_dir(tar, "usr/bin")

    files_to_pack = [
        "anilix.py",
        "anilix_server.py",
        "pyproject.toml",
        "uv.lock",
        "README.md",
    ]
    
    # Add files to /opt/anilix/
    for f in files_to_pack:
        if os.path.exists(f):
            print(f"Adding {f} to opt/anilix/{f}")
            def filter_reset(tarinfo):
                tarinfo.name = f"opt/anilix/{f}"
                return reset_tarinfo(tarinfo)
            tar.add(f, filter=filter_reset)
        else:
            print(f"Warning: {f} not found!")
            
    # Add wrapper script to /usr/bin/anilix
    print("Adding wrapper to usr/bin/anilix")
    add_content(tar, "usr/bin/anilix", wrapper_script, mode=0o755)

print("Creating debian-binary...")
with open("debian-binary", "w") as f:
    f.write("2.0\n")

print(f"Archiving into {DEB_NAME} (pure Python)...")
if os.path.exists(DEB_NAME):
    os.remove(DEB_NAME)

def pad(string, length):
    string = str(string) if not isinstance(string, bytes) else string.decode('ascii')
    return (string + " " * length)[:length].encode('ascii')

try:
    with open(DEB_NAME, 'wb') as out:
        out.write(b"!<arch>\n")
        
        for name in ["debian-binary", "control.tar.gz", "data.tar.gz"]:
            stat = os.stat(name)
            # GNU ar format appends '/' to the filename
            ar_name = f"{name}/"
            # 16-sys name, 12 timestamp, 6 uid, 6 gid, 8 mode, 10 size, 2 end
            # Header is exactly 60 bytes
            header = pad(ar_name, 16) + \
                     pad(int(stat.st_mtime), 12) + \
                     pad("0", 6) + \
                     pad("0", 6) + \
                     pad("100644", 8) + \
                     pad(stat.st_size, 10) + \
                     b"`\n"
            
            assert len(header) == 60, f"Header length must be 60, got {len(header)}"
            out.write(header)
            
            with open(name, 'rb') as f:
                content = f.read()
                out.write(content)
                if len(content) % 2 != 0:
                    out.write(b"\n")
    print(f"Successfully created {DEB_NAME}!")
except Exception as e:
    print(f"Failed to create ar archive: {e}")

# Cleanup
os.remove("debian-binary")
os.remove("control.tar.gz")
os.remove("data.tar.gz")

print(f"Finished. You can now use '{DEB_NAME}' to install via apt.")
