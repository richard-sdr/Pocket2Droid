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

# Main engine SO (QEMU arm64)
engine = BUILD / "libp2dengine.so"
if not engine.exists():
    print(f"ERROR: native engine missing at {engine}", file=sys.stderr)
    sys.exit(1)

with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_STORED) as z:
    z.write(engine, "lib/arm64-v8a/libp2dengine.so")

# Storage helper JNI SO (required by StoragePublish class on first load)
storage_so = BUILD / "libp2dstorage.so"
if storage_so.exists():
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_STORED) as z:
        z.write(storage_so, "lib/arm64-v8a/libp2dstorage.so")
else:
    print(f"ERROR: native storage helper missing at {storage_so}", file=sys.stderr)
    sys.exit(1)

# If we have Termux-sysroot .so files, bundle them too.
# The Android linker needs them at runtime if QEMU was dynamically linked.
if SYSROOT:
    for termux_usr in [SYSROOT / "data/data/com.termux/files/usr", SYSROOT]:
        if not termux_usr.is_dir():
            continue
        libdir = termux_usr / "lib"
        if not libdir.is_dir():
            continue
        bundled = 0
        for so in sorted(libdir.glob("*.so*")):
            name = so.name
            if "libp2dengine" in name:
                continue
            # Only the ones we actually need — glib/pixman/slirp + their deps
            keep = any(k in name for k in [
                "libglib", "libpixman", "libslirp", "libxml2",
                "libiconv", "libffi", "liblz4", "libpcre",
                "libz", "libdl", "liblog",
            ])
            if not keep:
                continue
            zip_path = f"lib/arm64-v8a/{name}"
            with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_STORED) as z:
                # Skip if already inside
                if zip_path not in z.namelist():
                    z.write(so, zip_path)
                    bundled += 1
        if bundled:
            print(f"Bundled {bundled} Termux runtime .so files from {libdir}")
        break

# --- 6. zipalign ---
aligned = BUILD / "Pocket2Droid-ALIGNED.apk"
run([str(ZIPALIGN), "-f", "-P", "16", "4", str(unsigned), str(aligned)])

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
