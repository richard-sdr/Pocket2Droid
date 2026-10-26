#!/usr/bin/env python3
"""Pocket2Droid APK builder — GitHub Actions / debug version.

Replaces android/build.py for the GHA build path. Differences from the
author's original:
  * No hardcoded /opt/pocket2droid-* paths; reads ANDROID_HOME from env
  * No seal_* hash checks or signing secrets
  * Auto-generates debug keystore
  * Skips LEGACY / BASELINE dependencies — source is complete in the repo
  * Assumes libp2dengine.so is already at android/build/libp2dengine.so
"""
import hashlib, os, shutil, subprocess, sys, zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANDROID_HOME = Path(os.environ["ANDROID_HOME"])
BUILD_TOOLS = max((ANDROID_HOME / "build-tools").iterdir())

ANDROID_JAR = ANDROID_HOME / "platforms/android-36/android.jar"
D8_JAR = BUILD_TOOLS / "lib/d8.jar"
AAPT2 = BUILD_TOOLS / "aapt2"
ZIPALIGN = BUILD_TOOLS / "zipalign"
APKSIGNER = BUILD_TOOLS / "apksigner"

BUILD = HERE / "build"
(BUILD / "classes").mkdir(parents=True, exist_ok=True)
(BUILD / "dex").mkdir(exist_ok=True)

SYSROOT = Path(os.environ.get("SYSROOT", ""))

def run(cmd, **kw):
    print("$", " ".join(map(str, cmd)))
    subprocess.run(cmd, check=True, **kw)

# --- 1. Compile Java sources ---
sources = sorted((HERE / "src").rglob("*.java"))
if not sources:
    print("ERROR: no .java sources found under", HERE / "src", file=sys.stderr)
    sys.exit(1)
print(f"Compiling {len(sources)} Java files")

run(["javac", "--release", "8", "-encoding", "UTF-8",
     "-cp", str(ANDROID_JAR), "-d", str(BUILD / "classes"),
     *map(str, sources)])
run(["jar", "cf", str(BUILD / "classes.jar"), "-C", str(BUILD / "classes"), "."])

# --- 2. Dex (class → dalvik bytecode) ---
dex_out = BUILD / "dex"
run(["java", "-cp", str(D8_JAR), "com.android.tools.r8.D8",
     "--min-api", "24", "--lib", str(ANDROID_JAR),
     "--output", str(dex_out), str(BUILD / "classes.jar")])

# --- 3. Compile resources ---
compiled_res = BUILD / "res.zip"
res_dir = HERE / "res"
run([str(AAPT2), "compile", "--dir", str(res_dir), "-o", str(compiled_res)])

# --- 4. Link resources into base APK ---
manifest = HERE / "AndroidManifest.xml"
linked = BUILD / "linked.apk"
run([str(AAPT2), "link", "-I", str(ANDROID_JAR),
     "--manifest", str(manifest), str(compiled_res),
     "-o", str(linked), "--auto-add-overlay", "--min-sdk-version", "24"])

# --- 5. Assemble final APK: res + dex + native so ---
unsigned = BUILD / "Pocket2Droid-UNSIGNED.apk"
shutil.copy2(linked, unsigned)

with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_STORED) as z:
    z.write(dex_out / "classes.dex", "classes.dex")

    # License / attribution assets (mirrors upstream release packaging)
    qemu_copying = HERE.parent / "qemu-android-exact/COPYING"
    if qemu_copying.exists():
        z.write(qemu_copying, "assets/QEMU-COPYING.txt")

    libslirp_notice = HERE / "patches/LIBSLIRP-NOTICE.txt"
    if libslirp_notice.exists():
        z.write(libslirp_notice, "assets/LIBSLIRP-NOTICE.txt")
        # The slirp COPYING comes from the slirp source tree; stub it if absent.
        slirp_copying = HERE.parent / "qemu-android-exact/subprojects/libslirp/COPYRIGHT"
        if slirp_copying.exists():
            z.write(slirp_copying, "assets/LIBSLIRP-COPYRIGHT.txt")
        else:
            z.writestr("assets/LIBSLIRP-COPYRIGHT.txt",
                       "libslirp - Copyright (c) 2004-2021 the QEMU team\n")

    # Guest-side network setup helper (pre-built in repo)
    net_setup_dir = HERE / "build/network-setup/production-helper"
    if (net_setup_dir / "autorun.exe").exists() and (net_setup_dir / "manifest.json").exists():
        z.write(net_setup_dir / "autorun.exe", "assets/network-setup/autorun.exe")
        z.write(net_setup_dir / "manifest.json", "assets/network-setup/manifest.json")

# Main engine SO (QEMU arm64)
engine = BUILD / "libp2dengine.so"
if not engine.exists():
    print(f"ERROR: native engine missing at {engine}", file=sys.stderr)
    sys.exit(1)

def add_executable_so(apk_path, src_path, zip_path):
    """Add a native shared library with executable permissions (0755)."""
    data = Path(src_path).read_bytes()
    with zipfile.ZipFile(apk_path, "a", zipfile.ZIP_STORED) as z:
        if zip_path in z.namelist():
            return
        info = zipfile.ZipInfo(zip_path, date_time=(2026, 10, 8, 12, 0, 0))
        info.external_attr = 0o100755 << 16
        z.writestr(info, data)

add_executable_so(unsigned, engine, "lib/arm64-v8a/libp2dengine.so")

# Storage helper JNI SO (required by StoragePublish class on first load)
storage_so = BUILD / "libp2dstorage.so"
if storage_so.exists():
    add_executable_so(unsigned, storage_so, "lib/arm64-v8a/libp2dstorage.so")
else:
    print(f"ERROR: native storage helper missing at {storage_so}", file=sys.stderr)
    sys.exit(1)

# If we have Termux-sysroot .so files, bundle them too.
# The Android linker needs them at runtime if QEMU was dynamically linked.
# We install each real library under its SONAME so the dynamic linker finds it.
SONAME_KEEP = [
    "libglib", "libpixman", "libslirp", "libxml2",
    "libiconv", "libffi", "liblz4", "libpcre",
    "libz", "libandroid-support", "libdl", "liblog",
]

# Map a real file name to the canonical SONAME the Android linker looks for.
# This is a safety net in case readelf is unavailable in the CI image.
FALLBACK_SONAME = {
    "libz.so": "libz.so.1",
    "libz.so.1": "libz.so.1",
    "libglib-2.0.so": "libglib-2.0.so.0",
    "libglib-2.0.so.0": "libglib-2.0.so.0",
    "libpixman-1.so": "libpixman-1.so.0",
    "libpixman-1.so.0": "libpixman-1.so.0",
    "libslirp.so": "libslirp.so.0",
    "libslirp.so.0": "libslirp.so.0",
    "libxml2.so": "libxml2.so.2",
    "libxml2.so.2": "libxml2.so.2",
    "libpcre2-8.so": "libpcre2-8.so.0",
    "libpcre2-8.so.0": "libpcre2-8.so.0",
    "liblz4.so": "liblz4.so.1",
    "liblz4.so.1": "liblz4.so.1",
    "libiconv.so": "libiconv.so",
    "libandroid-support.so": "libandroid-support.so",
    "libffi.so": "libffi.so",
    "libffi.so.8": "libffi.so.8",
}

def soname_of(path):
    try:
        out = subprocess.check_output(["readelf", "-d", str(path)], text=True,
                                      stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "SONAME" in line:
                # line looks like: 0x000000000000000e (SONAME) Library soname: [libfoo.so.0]
                start = line.find("[")
                end = line.find("]", start)
                if start != -1 and end != -1:
                    return line[start + 1:end]
    except Exception:
        pass
    return None

def canonical_soname(path):
    """Return the SONAME the Android linker will search for."""
    # Prefer the ELF header, fall back to filename-derived mapping.
    soname = soname_of(path)
    if soname:
        return soname
    return FALLBACK_SONAME.get(path.name, path.name)

if SYSROOT:
    for termux_usr in [SYSROOT / "data/data/com.termux/files/usr", SYSROOT]:
        if not termux_usr.is_dir():
            continue
        libdir = termux_usr / "lib"
        if not libdir.is_dir():
            continue

        # Collect real files only; skip symlinks. Map SONAME -> best real file.
        soname_files = {}
        for so in libdir.glob("*.so*"):
            if so.is_symlink():
                continue
            if "libp2dengine" in so.name:
                continue
            if not any(k in so.name for k in SONAME_KEEP):
                continue
            soname = canonical_soname(so)
            # Prefer the most version-specific real file for a given SONAME.
            existing = soname_files.get(soname)
            if existing is None or len(so.name) > len(existing.name):
                soname_files[soname] = so

        bundled = 0
        for soname, so in sorted(soname_files.items()):
            zip_path = f"lib/arm64-v8a/{soname}"
            add_executable_so(unsigned, so, zip_path)
            bundled += 1
        if bundled:
            print(f"Bundled {bundled} Termux runtime .so files from {libdir}")
            print("   " + " ".join(sorted(soname_files.keys())))
        break

# --- 6. zipalign ---
aligned = BUILD / "Pocket2Droid-ALIGNED.apk"
# -p is required: page-aligns .so files to 4 KiB, otherwise Android refuses to
# load native libraries directly from the APK (extractNativeLibs=true).
run([str(ZIPALIGN), "-f", "-p", "4", str(unsigned), str(aligned)])

# --- 7. Debug-sign ---
keystore = Path.home() / ".android/debug.keystore"
if not keystore.exists():
    print("Generating debug keystore...")
    keystore.parent.mkdir(parents=True, exist_ok=True)
    run(["keytool", "-genkeypair", "-v", "-keystore", str(keystore),
         "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048",
         "-validity", "10000",
         "-storepass", "android", "-keypass", "android",
         "-dname", "CN=Android Debug,O=Android,C=US"])

signed = BUILD / "Pocket2Droid-DEBUG.apk"
run([str(APKSIGNER), "sign",
     "--ks", str(keystore), "--ks-pass", "pass:android",
     "--out", str(signed), str(aligned)])
run([str(APKSIGNER), "verify", str(signed)])

sha = hashlib.sha256(signed.read_bytes()).hexdigest()
size_mb = signed.stat().st_size / 1024 / 1024
print()
print("=" * 50)
print(f"✅ Pocket2Droid DEBUG APK built successfully!")
print(f"   Path:    {signed}")
print(f"   Size:    {size_mb:.1f} MB")
print(f"   SHA256:  {sha}")
print("=" * 50)
