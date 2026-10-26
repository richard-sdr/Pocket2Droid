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
import hashlib, os, re, shutil, subprocess, sys, zipfile
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

# Storage helper JNI SO (required by StoragePublish class on first load)
storage_so = BUILD / "libp2dstorage.so"
if storage_so.exists():
    add_executable_so(unsigned, storage_so, "lib/arm64-v8a/libp2dstorage.so")
else:
    print(f"ERROR: native storage helper missing at {storage_so}", file=sys.stderr)
    sys.exit(1)

# Termux dependencies use SONAMEs with version suffixes (e.g. libglib-2.0.so.0).
# Android's package manager only extracts libraries whose names match lib*.so,
# so we rename them to NDK-friendly forms and rewrite DT_NEEDED/SONAME entries
# with patchelf. The names mirror the upstream release's libp2d_* convention.
# Map from original SONAME/symlink-name -> NDK-friendly APK filename.
# We include both versioned and unversioned names because Termux packages
# expose libraries as symlinks, and the linker may record either form as
# DT_NEEDED depending on which symlink was used at link time.
NDK_NAMES = {
    "libglib-2.0.so.0":      "libp2d_glib_2_0.so",
    "libglib-2.0.so":        "libp2d_glib_2_0.so",
    "libpixman-1.so.0":      "libp2d_pixman_1.so",
    "libpixman-1.so":        "libp2d_pixman_1.so",
    "libslirp.so.0":         "libp2d_slirp.so",
    "libslirp.so":           "libp2d_slirp.so",
    "libxml2.so.2":          "libp2d_xml2.so",
    "libxml2.so.16":         "libp2d_xml2.so",
    "libxml2.so":            "libp2d_xml2.so",
    "libz.so.1":             "libp2d_z.so",
    "libz.so":               "libp2d_z.so",
    "libandroid-support.so": "libp2d_android_support.so",
    "liblz4.so.1":           "libp2d_lz4.so",
    "liblz4.so":             "libp2d_lz4.so",
    "libpcre2-8.so.0":       "libp2d_pcre2_8.so",
    "libpcre2-8.so":         "libp2d_pcre2_8.so",
    "libiconv.so":           "libp2d_iconv.so",
    "libffi.so.8":           "libp2d_ffi.so",
    "libffi.so":             "libp2d_ffi.so",
}

# Keywords used to filter the sysroot for libraries we care about.
REQUIRED_KEYS = {
    "libglib", "libpixman", "libslirp", "libxml2",
    "libiconv", "libffi", "liblz4", "libpcre",
    "libz", "libandroid-support",
}

def readelf_sonames(path):
    """Return the list of DT_NEEDED library names for an ELF shared object."""
    try:
        out = subprocess.check_output(["readelf", "-d", str(path)], text=True,
                                      stderr=subprocess.DEVNULL)
        needed = []
        for line in out.splitlines():
            if "(NEEDED)" in line and "Shared library:" in line:
                m = re.search(r"\[([^\]]+)\]", line)
                if m:
                    needed.append(m.group(1))
        return needed
    except Exception as e:
        print(f"WARN: readelf failed for {path}: {e}", file=sys.stderr)
        return []

def rewrite_so_dependencies(src_path, new_name):
    """Copy a shared library, rewrite its SONAME and DT_NEEDED to NDK names.

    Returns the path to the rewritten file inside BUILD/.
    """
    needed = readelf_sonames(src_path)
    replace_args = []
    for old in needed:
        if old in NDK_NAMES:
            new = NDK_NAMES[old]
            if old != new:
                replace_args.extend(["--replace-needed", old, new])

    out = BUILD / new_name
    if out.resolve() == Path(src_path).resolve():
        # Source is already in BUILD/ under the target name; work on a temp copy.
        tmp = BUILD / (new_name + ".patchelf")
        shutil.copy2(src_path, tmp)
        src_path = tmp
    shutil.copy2(src_path, out)
    cmd = ["patchelf", "--set-soname", new_name, *replace_args, str(out)]
    if replace_args:
        print("$", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return out

# Locate Termux sysroot libs and map canonical SONAME -> real file.
soname_files = {}
if SYSROOT:
    for termux_usr in [SYSROOT / "data/data/com.termux/files/usr", SYSROOT]:
        if not termux_usr.is_dir():
            continue
        libdir = termux_usr / "lib"
        if not libdir.is_dir():
            continue

        def soname_of(path):
            try:
                out = subprocess.check_output(["readelf", "-d", str(path)],
                                              text=True, stderr=subprocess.DEVNULL)
                for line in out.splitlines():
                    if "SONAME" in line:
                        m = re.search(r"\[([^\]]+)\]", line)
                        if m:
                            return m.group(1)
            except Exception:
                pass
            return None

        for so in sorted(libdir.glob("*.so*")):
            if so.is_symlink():
                continue
            if "libp2dengine" in so.name:
                continue
            if not any(k in so.name for k in REQUIRED_KEYS):
                continue
            soname = soname_of(so)
            # Fallback: if readelf is missing, derive a canonical name from
            # the filename by stripping the last version component.
            if not soname:
                soname = so.name
                if ".so." in soname:
                    soname = soname.rsplit(".", 1)[0] + ".so"
            if not soname:
                continue
            existing = soname_files.get(soname)
            if existing is None or len(so.name) > len(existing.name):
                soname_files[soname] = so
        break

# Patch engine to reference the NDK-friendly names (engine keeps its own name).
patched_engine = rewrite_so_dependencies(engine, "libp2dengine.so")
add_executable_so(unsigned, patched_engine, "lib/arm64-v8a/libp2dengine.so")

# Bundle dependencies under NDK-friendly names so Android extracts them.
bundled = 0
for soname, so in sorted(soname_files.items()):
    ndk_name = NDK_NAMES.get(soname)
    if not ndk_name:
        print(f"WARN: no NDK-friendly mapping for {soname}; skipping", file=sys.stderr)
        continue
    patched = rewrite_so_dependencies(so, ndk_name)
    zip_path = f"lib/arm64-v8a/{ndk_name}"
    add_executable_so(unsigned, patched, zip_path)
    bundled += 1
if bundled:
    print(f"Bundled {bundled} Termux runtime .so files")
    print("   " + " ".join(sorted(NDK_NAMES.get(s, s) for s in soname_files)))

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
