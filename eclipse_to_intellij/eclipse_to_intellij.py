#!/usr/bin/env python3
"""
eclipse_to_intellij.py
======================
Converts an Eclipse .launch file + parent pom.xml into a fully configured
IntelliJ IDEA project.  When done, the user opens the root pom.xml in
IntelliJ and presses Run — nothing else required.

Usage
-----
    python eclipse_to_intellij.py \
        --pom   /path/to/parent/pom.xml   \
        --launch /path/to/MyApp.launch    \
        --jdk   17                         \
        --source 11                        \
        --target 11

Optional
--------
    --app-name   "My Application"   # override the run-config display name
    --native-dir native             # directory for DLLs/SOs (default: native)
    --deploy-dir target/deploy      # where Maven deposits native libs after build
    --encoding   UTF-8              # source file encoding (default: UTF-8)
    --dry-run                       # preview all changes without writing files

Requirements
------------
    Python 3.8+  (stdlib only — no pip installs needed)
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from textwrap import dedent
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

MAVEN_NS = "http://maven.apache.org/POM/4.0.0"

# IntelliJ language-level tokens by Java version string
LANG_LEVEL_MAP: Dict[str, str] = {
    "1.4": "JDK_1_4", "5": "JDK_1_5", "1.5": "JDK_1_5",
    "6": "JDK_1_6",   "1.6": "JDK_1_6",
    "7": "JDK_1_7",   "1.7": "JDK_1_7",
    "8": "JDK_1_8",   "1.8": "JDK_1_8",
    "9": "JDK_9",     "10": "JDK_10",   "11": "JDK_11",
    "12": "JDK_12",   "13": "JDK_13",   "14": "JDK_14",
    "15": "JDK_15",   "16": "JDK_16",   "17": "JDK_17",
    "18": "JDK_18",   "19": "JDK_19",   "20": "JDK_20",
    "21": "JDK_21",   "22": "JDK_22",   "23": "JDK_23",
}

# Eclipse launch-file attribute keys
ECLIPSE = {
    "main":        "org.eclipse.jdt.launching.MAIN_TYPE",
    "vm_args":     "org.eclipse.jdt.launching.VM_ARGUMENTS",
    "prog_args":   "org.eclipse.jdt.launching.PROGRAM_ARGUMENTS",
    "workdir":     "org.eclipse.jdt.launching.WORKING_DIRECTORY",
    "project":     "org.eclipse.jdt.launching.PROJECT_ATTR",
    "env_vars":    "org.eclipse.debug.core.environmentVariables",
    "stop_main":   "org.eclipse.jdt.launching.STOP_IN_MAIN",
    "vm_install":  "org.eclipse.jdt.launching.VM_INSTALL_NAME",
    "classpath":   "org.eclipse.jdt.launching.CLASSPATH",
    "def_cp":      "org.eclipse.jdt.launching.DEFAULT_CLASSPATH",
}

DIVIDER = "=" * 70
THIN    = "─" * 70

# Set from CLI args in main(); checked by all write operations.
DRY_RUN: bool = False


def _guarded_write_text(path: Path, content: str, encoding: str = "UTF-8") -> None:
    """Write a text file, or print what would be written in dry-run mode."""
    if DRY_RUN:
        _info(f"[DRY-RUN] Would write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)


def _guarded_write_bytes(path: Path, tree: ET.ElementTree) -> None:
    """Write an XML tree as bytes, or print what would be written in dry-run mode."""
    if DRY_RUN:
        _info(f"[DRY-RUN] Would write {path}")
        return
    with open(path, "wb") as fh:
        tree.write(fh, encoding="UTF-8", xml_declaration=True)


def _guarded_mkdir(path: Path) -> None:
    """Create a directory, or print what would be created in dry-run mode."""
    if DRY_RUN:
        if not path.exists():
            _info(f"[DRY-RUN] Would create directory {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def _guarded_backup(path: Path) -> None:
    """Back up a file, or print what would be backed up in dry-run mode."""
    backup = path.with_suffix(".xml.bak")
    if backup.exists():
        return
    if DRY_RUN:
        _info(f"[DRY-RUN] Would back up {path.name} → {backup.name}")
        return
    shutil.copy2(path, backup)


# ──────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Convert Eclipse launch config → IntelliJ IDEA run config "
                    "for a multi-module Maven project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--pom",        required=True,  help="Path to parent pom.xml")
    p.add_argument("--launch",     required=True,  help="Path to Eclipse .launch file")
    p.add_argument("--jdk",        required=True,  help="JDK version (e.g. 17, 21)")
    p.add_argument("--source",     required=True,  help="Maven compiler source (e.g. 11, 17)")
    p.add_argument("--target",     required=True,  help="Maven compiler target (e.g. 11, 17)")
    p.add_argument("--app-name",   default=None,   help="Run-config display name (default: launch file stem)")
    p.add_argument("--native-dir", default="native", help="Dir for DLLs/SOs relative to project root")
    p.add_argument("--deploy-dir", default=None,
                   help="Dir where Maven deposits native libs after build "
                        "(e.g. target/deploy). If set, build scripts will "
                        "copy *.dll/*.so/*.dylib from here into native-dir.")
    p.add_argument("--encoding",   default="UTF-8",  help="Source file encoding (default: UTF-8)")
    p.add_argument("--dry-run",    action="store_true",
                   help="Preview all changes without writing any files")
    return p.parse_args()


# ──────────────────────────────────────────────────────────────────────────────
# Eclipse launch file parser
# ──────────────────────────────────────────────────────────────────────────────

class EclipseLaunch:
    """
    Parses an Eclipse .launch XML file and exposes the fields that
    IntelliJ needs for an Application run configuration.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._tree = ET.parse(path)
        self._root = self._tree.getroot()

    # ── private helpers ────────────────────────────────────────────────────

    def _str(self, key: str) -> str:
        for el in self._root.findall("stringAttribute"):
            if el.get("key") == key:
                return el.get("value") or ""
        return ""

    def _bool(self, key: str) -> bool:
        for el in self._root.findall("booleanAttribute"):
            if el.get("key") == key:
                return el.get("value", "false").lower() == "true"
        return False

    def _map(self, key: str) -> Dict[str, str]:
        for el in self._root.findall("mapAttribute"):
            if el.get("key") == key:
                return {
                    e.get("key", ""): e.get("value", "")
                    for e in el.findall("mapEntry")
                }
        return {}

    def _list(self, key: str) -> List[str]:
        for el in self._root.findall("listAttribute"):
            if el.get("key") == key:
                return [e.get("value", "") for e in el.findall("listEntry")]
        return []

    # ── public properties ──────────────────────────────────────────────────

    @property
    def main_class(self) -> str:
        return self._str(ECLIPSE["main"])

    @property
    def vm_arguments(self) -> str:
        return self._str(ECLIPSE["vm_args"])

    @property
    def program_arguments(self) -> str:
        return self._str(ECLIPSE["prog_args"])

    @property
    def working_directory(self) -> str:
        return self._str(ECLIPSE["workdir"])

    @property
    def project_name(self) -> str:
        return self._str(ECLIPSE["project"])

    @property
    def env_vars(self) -> Dict[str, str]:
        return self._map(ECLIPSE["env_vars"])

    @property
    def stop_in_main(self) -> bool:
        return self._bool(ECLIPSE["stop_main"])


# ──────────────────────────────────────────────────────────────────────────────
# Maven POM handler
# ──────────────────────────────────────────────────────────────────────────────

class PomHandler:
    """
    Reads and modifies a Maven pom.xml.

    Handles projects that have the Maven namespace and those that don't.
    Creates a .bak backup before saving any modifications.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.project_root = path.parent
        ET.register_namespace("", MAVEN_NS)
        self._tree = ET.parse(str(path))
        self._root = self._tree.getroot()
        self._ns  = self._sniff_ns()

    # ── namespace helpers ──────────────────────────────────────────────────

    def _sniff_ns(self) -> str:
        tag = self._root.tag
        return tag[1 : tag.index("}")] if tag.startswith("{") else ""

    def _q(self, name: str) -> str:
        """Qualify a tag name with the namespace."""
        return f"{{{self._ns}}}{name}" if self._ns else name

    def _find(self, parent: ET.Element, *path_parts: str) -> Optional[ET.Element]:
        """Namespace-aware find along a sequence of tag names."""
        el = parent
        for part in path_parts:
            found = el.find(self._q(part))
            if found is None:
                return None
            el = found
        return el

    def _findall(self, parent: ET.Element, tag: str) -> List[ET.Element]:
        return parent.findall(self._q(tag))

    def _get_or_create(self, parent: ET.Element, tag: str) -> ET.Element:
        el = self._find(parent, tag)
        if el is None:
            el = ET.SubElement(parent, self._q(tag))
        return el

    # ── public readers ─────────────────────────────────────────────────────

    def artifact_id(self) -> str:
        el = self._find(self._root, "artifactId")
        return (el.text or "").strip() if el is not None else ""

    def modules(self) -> List[str]:
        """
        Collect all <module> entries from both the top-level <modules> block
        AND from any <profiles><profile><modules> blocks.  Maven allows
        modules to be declared inside profiles, and many real-world projects
        use this pattern (e.g. modules only built under a 'release' profile).
        Returns a deduplicated list preserving first-seen order.
        """
        result: List[str] = []
        seen: set = set()

        def _collect(parent: ET.Element) -> None:
            mods_el = self._find(parent, "modules")
            if mods_el is not None:
                for el in self._findall(mods_el, "module"):
                    mod = (el.text or "").strip()
                    if mod and mod not in seen:
                        result.append(mod)
                        seen.add(mod)

        # Top-level <modules>
        _collect(self._root)

        # <profiles><profile> → each may have its own <modules>
        profiles_el = self._find(self._root, "profiles")
        if profiles_el is not None:
            for profile_el in self._findall(profiles_el, "profile"):
                _collect(profile_el)

        return result

    def profile_module_counts(self) -> List[Tuple[str, int]]:
        """
        Return [(profile_id, module_count), ...] for profiles that declare
        <modules>.  Used for reporting only — does not affect module discovery.
        """
        result: List[Tuple[str, int]] = []
        profiles_el = self._find(self._root, "profiles")
        if profiles_el is None:
            return result
        for profile_el in self._findall(profiles_el, "profile"):
            id_el = self._find(profile_el, "id")
            pid = (id_el.text or "").strip() if id_el is not None else "(unnamed)"
            mods_el = self._find(profile_el, "modules")
            if mods_el is not None:
                count = len([
                    el for el in self._findall(mods_el, "module")
                    if (el.text or "").strip()
                ])
                if count:
                    result.append((pid, count))
        return result

    # ── plugin helpers ─────────────────────────────────────────────────────

    def _plugins_el(self) -> ET.Element:
        build = self._get_or_create(self._root, "build")
        return self._get_or_create(build, "plugins")

    def _find_plugin(self, plugins: ET.Element, artifact_id: str) -> Optional[ET.Element]:
        for plug in self._findall(plugins, "plugin"):
            aid = self._find(plug, "artifactId")
            if aid is not None and (aid.text or "").strip() == artifact_id:
                return plug
        return None

    def _get_or_create_plugin(
        self, plugins: ET.Element, group_id: str, artifact_id: str
    ) -> ET.Element:
        plug = self._find_plugin(plugins, artifact_id)
        if plug is None:
            plug = ET.SubElement(plugins, self._q("plugin"))
            ET.SubElement(plug, self._q("groupId")).text = group_id
            ET.SubElement(plug, self._q("artifactId")).text = artifact_id
        return plug

    def _set_version(self, plug: ET.Element, version: str) -> None:
        el = self._find(plug, "version")
        if el is None:
            # Insert after artifactId
            plug.insert(2, ET.Element(self._q("version")))
            el = self._find(plug, "version")
        if el is not None:
            el.text = version

    def _set_config(self, plug: ET.Element, **kwargs: str) -> None:
        cfg = self._get_or_create(plug, "configuration")
        for name, value in kwargs.items():
            el = self._find(cfg, name)
            if el is None:
                el = ET.SubElement(cfg, self._q(name))
            el.text = value

    # ── public modifiers ───────────────────────────────────────────────────

    def set_properties(self, source: str, target: str, encoding: str) -> None:
        """
        Inject / overwrite compiler and skip properties in <properties>.
        This is the highest-priority way to set these values so child
        modules inherit them even without explicit plugin config.
        """
        props = self._get_or_create(self._root, "properties")

        defs: Dict[str, str] = {
            "maven.compiler.source":           source,
            "maven.compiler.target":           target,
            "maven.compiler.encoding":         encoding,
            "project.build.sourceEncoding":    encoding,
            "project.reporting.outputEncoding": encoding,
            # Skip tests globally — two properties because different plugins
            # check different ones
            "maven.test.skip":                 "true",
            "skipTests":                       "true",
            # Skip javadoc
            "maven.javadoc.skip":              "true",
        }
        for name, value in defs.items():
            el = self._find(props, name)
            if el is None:
                el = ET.SubElement(props, self._q(name))
            el.text = value

    def configure_compiler_plugin(self, source: str, target: str, encoding: str) -> None:
        plugins = self._plugins_el()
        plug = self._get_or_create_plugin(
            plugins, "org.apache.maven.plugins", "maven-compiler-plugin"
        )
        self._set_version(plug, "3.13.0")
        self._set_config(
            plug,
            source=source,
            target=target,
            encoding=encoding,
            # Compile main sources, skip test compilation
            skipMain="false",
            skip="false",
        )

    def configure_surefire_plugin(self) -> None:
        plugins = self._plugins_el()
        plug = self._get_or_create_plugin(
            plugins, "org.apache.maven.plugins", "maven-surefire-plugin"
        )
        self._set_version(plug, "3.2.5")
        self._set_config(plug, skip="true", skipTests="true")

    def configure_failsafe_plugin(self) -> None:
        """Also skip maven-failsafe (integration tests)."""
        plugins = self._plugins_el()
        plug = self._get_or_create_plugin(
            plugins, "org.apache.maven.plugins", "maven-failsafe-plugin"
        )
        self._set_version(plug, "3.2.5")
        self._set_config(plug, skip="true", skipTests="true")

    def configure_javadoc_plugin(self) -> None:
        plugins = self._plugins_el()
        plug = self._get_or_create_plugin(
            plugins, "org.apache.maven.plugins", "maven-javadoc-plugin"
        )
        self._set_version(plug, "3.6.3")
        self._set_config(plug, skip="true")

    def configure_dependency_plugin(self) -> None:
        """
        Add an execution that copies all runtime deps to the project's
        lib/ folder.  IntelliJ sees this as an always-current dependency list.
        """
        plugins = self._plugins_el()
        plug = self._get_or_create_plugin(
            plugins, "org.apache.maven.plugins", "maven-dependency-plugin"
        )
        self._set_version(plug, "3.6.1")

        executions = self._get_or_create(plug, "executions")

        # Only add once
        for exec_el in self._findall(executions, "execution"):
            id_el = self._find(exec_el, "id")
            if id_el is not None and (id_el.text or "").strip() == "copy-deps":
                return

        exec_el = ET.SubElement(executions, self._q("execution"))
        ET.SubElement(exec_el, self._q("id")).text    = "copy-deps"
        ET.SubElement(exec_el, self._q("phase")).text = "package"

        goals_el = ET.SubElement(exec_el, self._q("goals"))
        ET.SubElement(goals_el, self._q("goal")).text = "copy-dependencies"

        cfg = ET.SubElement(exec_el, self._q("configuration"))
        ET.SubElement(cfg, self._q("outputDirectory")).text = "${project.basedir}/lib"
        ET.SubElement(cfg, self._q("overWriteIfNewer")).text = "true"
        ET.SubElement(cfg, self._q("includeScope")).text     = "runtime"
        ET.SubElement(cfg, self._q("overWriteReleases")).text = "false"
        ET.SubElement(cfg, self._q("overWriteSnapshots")).text = "true"

    # ── save ──────────────────────────────────────────────────────────────

    def save(self) -> None:
        """Back up original, then write pretty-printed XML."""
        _guarded_backup(self.path)
        self._pretty(self._root)
        _guarded_write_bytes(self.path, self._tree)

    def _pretty(self, el: ET.Element, level: int = 0) -> None:
        """In-place indentation for readable XML output."""
        pad  = "\n" + "    " * level
        pad1 = "\n" + "    " * (level + 1)
        if len(el):
            if not (el.text and el.text.strip()):
                el.text = pad1
            for child in el:
                self._pretty(child, level + 1)
            # last child tail
            if not (el[-1].tail and el[-1].tail.strip()):
                el[-1].tail = pad
        else:
            if not (el.text and el.text.strip()):
                el.text = ""
        if level > 0 and not (el.tail and el.tail.strip()):
            el.tail = pad


# ──────────────────────────────────────────────────────────────────────────────
# Module discovery helpers
# ──────────────────────────────────────────────────────────────────────────────

def collect_all_module_poms(handler: PomHandler) -> List[Path]:
    """
    Recursively walk the <modules> tree and return a flat list of
    every child pom.xml path found.
    """
    result: List[Path] = []

    def recurse(h: PomHandler) -> None:
        for rel in h.modules():
            child_pom = h.project_root / rel / "pom.xml"
            if child_pom.exists():
                result.append(child_pom)
                try:
                    recurse(PomHandler(child_pom))
                except Exception:
                    pass  # malformed child pom — skip recursion

    recurse(handler)
    return result


def _source_roots_for_module(pom_path: Path) -> List[Path]:
    """
    Return the list of Java source roots to search for a given module POM.
    Checks <build><sourceDirectory> first; always appends the Maven default
    src/main/java as a fallback.
    """
    module_dir = pom_path.parent
    roots: List[Path] = []
    try:
        h = PomHandler(pom_path)
        build = h._find(h._root, "build")
        if build is not None:
            src_dir_el = h._find(build, "sourceDirectory")
            if src_dir_el is not None and src_dir_el.text:
                custom = src_dir_el.text.strip()
                p = Path(custom)
                roots.append(p if p.is_absolute() else module_dir / p)
    except Exception:
        pass
    default = module_dir / "src" / "main" / "java"
    if default not in roots:
        roots.append(default)
    return roots


def find_launcher_module(
    handler: PomHandler, main_class: str
) -> Tuple[str, Path]:
    """
    Try to find the Maven module that contains the main class by looking for
    the corresponding .java source file.  Respects custom <sourceDirectory>
    declarations in each module POM.  Falls back to prompting the user.
    """
    project_root  = handler.project_root
    all_poms      = collect_all_module_poms(handler)
    main_rel_path = main_class.replace(".", "/") + ".java"

    for pom_path in all_poms:
        for src_root in _source_roots_for_module(pom_path):
            if (src_root / main_rel_path).exists():
                try:
                    art_id = PomHandler(pom_path).artifact_id()
                    if art_id:
                        print(f"  ✅ Main class found in module: {art_id}")
                        return art_id, pom_path.parent
                except Exception:
                    pass
                return pom_path.parent.name, pom_path.parent

    # Auto-detect failed — ask the user
    print(f"\n  ⚠️  Could not auto-detect module for: {main_class}")
    print("     Known modules:")
    # Show ALL discovered module POMs (including nested), not just top-level
    all_rels = [str(p.parent.relative_to(project_root)) for p in all_poms]
    for i, m in enumerate(all_rels):
        print(f"       [{i}] {m}")

    while True:
        answer = input(
            "\n  Enter module number from the list above, "
            "or type the artifactId directly: "
        ).strip()
        if answer.isdigit():
            idx = int(answer)
            if 0 <= idx < len(all_rels):
                mod_dir = project_root / all_rels[idx]
                mod_pom = mod_dir / "pom.xml"
                try:
                    art_id = PomHandler(mod_pom).artifact_id()
                    return art_id or mod_dir.name, mod_dir
                except Exception:
                    return mod_dir.name, mod_dir
        else:
            return answer, project_root


# ──────────────────────────────────────────────────────────────────────────────
# VM argument translator
# ──────────────────────────────────────────────────────────────────────────────

# Eclipse substitution variables → IntelliJ macros
#
# workspace_loc and project_loc entries use a callable replacement so we
# can strip the first path segment (the Eclipse project name) but keep
# any trailing sub-path.  E.g.
#   ${workspace_loc:/MyProject/config/app.xml} → $PROJECT_DIR$/config/app.xml
#   ${workspace_loc:/MyProject}                → $PROJECT_DIR$

_RE_WORKSPACE_LOC = re.compile(r"\$\{workspace_loc:/([^}/]+)(/[^}]*)?\}")
_RE_PROJECT_LOC   = re.compile(r"\$\{project_loc:([^}]*)\}")

_ECLIPSE_SIMPLE_MAP = [
    (re.compile(r"\$\{project_loc\}"),               "$PROJECT_DIR$"),
    (re.compile(r"\$\{workspace_loc\}"),             "$PROJECT_DIR$"),
    (re.compile(r"\$\{env_var:([^}]+)\}"),           r"$\1$"),   # best-effort
]


def translate_vm_args(raw: str, project_root: Path, native_dir: str) -> str:
    """
    1. Replace Eclipse variable tokens with IntelliJ $PROJECT_DIR$ macros.
    2. Ensure -Djava.library.path and -Djna.library.path both point to
       $PROJECT_DIR$/<native_dir>, appending if already present.
    """
    result = raw

    # workspace_loc: strip project name, keep sub-path
    result = _RE_WORKSPACE_LOC.sub(
        lambda m: "$PROJECT_DIR$" + (m.group(2) or ""), result
    )
    # project_loc with argument: always → $PROJECT_DIR$
    result = _RE_PROJECT_LOC.sub("$PROJECT_DIR$", result)
    # Simple / no-argument patterns
    for pattern, replacement in _ECLIPSE_SIMPLE_MAP:
        result = pattern.sub(replacement, result)

    native_path = f"$PROJECT_DIR$/{native_dir}"

    def inject_or_append(args: str, prop: str) -> str:
        pat = re.compile(rf"-D{re.escape(prop)}=(\S+)")
        m = pat.search(args)
        if m:
            existing = m.group(1)
            if native_path not in existing:
                new_val = f"{existing}:{native_path}"
                args = pat.sub(f"-D{prop}={new_val}", args)
        else:
            args += f" -D{prop}={native_path}"
        return args

    result = inject_or_append(result, "java.library.path")
    result = inject_or_append(result, "jna.library.path")

    return result.strip()


# ──────────────────────────────────────────────────────────────────────────────
# IntelliJ .idea/ file generators
# ──────────────────────────────────────────────────────────────────────────────

def _xml_esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


class IdeaGenerator:
    """Generates all files that belong in the .idea/ directory."""

    def __init__(
        self,
        project_root: Path,
        app_name:     str,
        jdk_version:  str,
        source:       str,
        target:       str,
        encoding:     str,
        native_dir:   str,
        launch:       EclipseLaunch,
    ) -> None:
        self.project_root = project_root
        self.idea         = project_root / ".idea"
        self.app_name     = app_name
        self.jdk_version  = jdk_version
        self.source       = source
        self.target       = target
        self.encoding     = encoding
        self.native_dir   = native_dir
        self.launch       = launch

        _guarded_mkdir(self.idea)
        _guarded_mkdir(self.idea / "runConfigurations")

    # ── run configuration ──────────────────────────────────────────────────

    def run_config(self, module_name: str) -> Path:
        vm_args = translate_vm_args(
            self.launch.vm_arguments, self.project_root, self.native_dir
        )
        work_dir = self._translate_workdir(self.launch.working_directory)
        prog_args = (self.launch.program_arguments or "").strip()
        env_block = self._env_block(self.launch.env_vars)
        safe      = re.sub(r"[^A-Za-z0-9_\-]", "_", self.app_name)

        xml = dedent(f"""\
            <component name="ProjectRunConfigurationManager">
              <configuration
                default="false"
                name="{_xml_esc(self.app_name)}"
                type="Application"
                factoryName="Application"
                singleton="true"
                nameIsGenerated="false">

                <!-- ── Main class ── -->
                <option name="MAIN_CLASS_NAME"
                        value="{_xml_esc(self.launch.main_class)}" />

                <!-- ── Classpath module (must match Maven artifactId) ── -->
                <option name="MODULE_NAME"
                        value="{_xml_esc(module_name)}" />

                <!-- ── JVM parameters (translated from Eclipse launch) ── -->
                <option name="VM_PARAMETERS"
                        value="{_xml_esc(vm_args)}" />

                <!-- ── Program arguments ── -->
                <option name="PROGRAM_PARAMETERS"
                        value="{_xml_esc(prog_args)}" />

                <!-- ── Working directory ── -->
                <option name="WORKING_DIRECTORY"
                        value="{_xml_esc(work_dir)}" />

                <!-- ── Use the project JDK (overridable per machine) ── -->
                <option name="ALTERNATIVE_JRE_PATH_ENABLED" value="false" />
                <option name="ALTERNATIVE_JRE_PATH" value="" />

                <!-- ── Do not include provided-scope jars on runtime CP ── -->
                <option name="INCLUDE_PROVIDED_SCOPE" value="false" />

                <!-- ── Environment variables ── -->
            {env_block}

                <!-- ── Before-launch: only compile (no tests, no javadoc) ── -->
                <method v="2">
                  <option name="Make" enabled="true" />
                </method>
              </configuration>
            </component>
        """)

        out = self.idea / "runConfigurations" / f"{safe}.xml"
        _guarded_write_text(out, xml)
        _ok(f"Run configuration       : {out}")
        return out

    def _translate_workdir(self, eclipse_dir: str) -> str:
        if not eclipse_dir:
            return "$PROJECT_DIR$"
        # Reuse the same sub-path-preserving patterns as translate_vm_args
        result = _RE_WORKSPACE_LOC.sub(
            lambda m: "$PROJECT_DIR$" + (m.group(2) or ""), eclipse_dir
        )
        result = _RE_PROJECT_LOC.sub("$PROJECT_DIR$", result)
        result = re.sub(r"\$\{project_loc\}", "$PROJECT_DIR$", result)
        return result or "$PROJECT_DIR$"

    def _env_block(self, env_vars: Dict[str, str]) -> str:
        if not env_vars:
            return "    <envs />"
        lines = ["    <envs>"]
        for k, v in env_vars.items():
            lines.append(f'      <env name="{_xml_esc(k)}" value="{_xml_esc(v)}" />')
        lines.append("    </envs>")
        return "\n".join(lines)

    # ── misc.xml ───────────────────────────────────────────────────────────

    def misc_xml(self) -> Path:
        lang = LANG_LEVEL_MAP.get(self.source, f"JDK_{self.source}")
        xml = dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project version="4">
              <component name="ProjectRootManager"
                         version="2"
                         languageLevel="{lang}"
                         project-jdk-name="{self.jdk_version}"
                         project-jdk-type="JavaSDK">
                <output url="file://$PROJECT_DIR$/target/classes" />
              </component>
            </project>
        """)
        out = self.idea / "misc.xml"
        _guarded_write_text(out, xml)
        _ok(f"misc.xml                : {out}")
        return out

    # ── compiler.xml ──────────────────────────────────────────────────────

    def compiler_xml(self) -> Path:
        xml = dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project version="4">
              <component name="CompilerConfiguration">
                <option name="DEFAULT_COMPILER" value="Javac" />
                <resourceExtensions />
                <wildcardResourcePatterns>
                  <entry name="!?*.java" />
                  <entry name="!?*.form" />
                  <entry name="!?*.class" />
                  <entry name="!?*.groovy" />
                  <entry name="!?*.scala" />
                  <entry name="!?*.kt" />
                </wildcardResourcePatterns>
                <annotationProcessing>
                  <profile name="Maven default annotation processors profile" enabled="true">
                    <sourceOutputDir name="target/generated-sources/annotations" />
                    <sourceTestOutputDir name="target/generated-test-sources/test-annotations" />
                    <outputRelativeToContentRoot value="true" />
                    <processorPath useClasspath="true" />
                  </profile>
                </annotationProcessing>
                <bytecodeTargetLevel target="{self.target}" />
              </component>
            </project>
        """)
        out = self.idea / "compiler.xml"
        _guarded_write_text(out, xml)
        _ok(f"compiler.xml            : {out}")
        return out

    # ── encodings.xml ─────────────────────────────────────────────────────

    def encodings_xml(self) -> Path:
        xml = dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project version="4">
              <component name="Encoding">
                <file url="PROJECT" charset="{self.encoding}" />
                <file url="file://$PROJECT_DIR$" charset="{self.encoding}" />
              </component>
            </project>
        """)
        out = self.idea / "encodings.xml"
        _guarded_write_text(out, xml)
        _ok(f"encodings.xml           : {out}")
        return out

    # ── .gitignore ────────────────────────────────────────────────────────

    def gitignore(self) -> Path:
        content = dedent("""\
            # ── IntelliJ default excludes ──────────────────────────────────────
            /shelf/
            /workspace.xml
            /tasks.xml
            /usage.statistics.xml
            /dictionaries/
            /sonarlint/
            /dataSources/
            /dataSources.local.xml
            /dynamic.classpath
            /uiDesigner.xml
            /.DS_Store

            # ── Commit these so configs travel with the repository ─────────────
            !runConfigurations/
            !modules.xml
            !misc.xml
            !compiler.xml
            !encodings.xml
            !jarRepositories.xml
            !vcs.xml
            !maven.xml
            !.gitignore
        """)
        out = self.idea / ".gitignore"
        _guarded_write_text(out, content)
        _ok(f".idea/.gitignore        : {out}")
        return out

    # ── vcs.xml ───────────────────────────────────────────────────────────

    def vcs_xml(self) -> Optional[Path]:
        if not (self.project_root / ".git").exists():
            return None
        xml = dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project version="4">
              <component name="VcsDirectoryMappings">
                <mapping directory="$PROJECT_DIR$" vcs="Git" />
              </component>
            </project>
        """)
        out = self.idea / "vcs.xml"
        _guarded_write_text(out, xml)
        _ok(f"vcs.xml                 : {out}")
        return out

    # ── modules.xml ───────────────────────────────────────────────────────

    def modules_xml(self, all_module_poms: List[Path]) -> Path:
        """
        Write .idea/modules.xml so IntelliJ discovers every submodule
        immediately on project open — before the first Maven sync runs.
        Without this file IntelliJ only shows the root aggregator POM in
        the Maven tool window.
        """
        def _iml_entry(dir_path: Path) -> str:
            rel = dir_path.relative_to(self.project_root)
            iml_name = dir_path.name + ".iml"
            rel_fwd = str(rel).replace("\\", "/")
            return (
                f'      <module'
                f' fileurl="file://$PROJECT_DIR$/{rel_fwd}/{iml_name}"'
                f' filepath="$PROJECT_DIR$/{rel_fwd}/{iml_name}" />'
            )

        lines: List[str] = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<project version="4">',
            '  <component name="ProjectModuleManager">',
            '    <modules>',
        ]

        # Root aggregator module entry
        root_name = self.project_root.name
        lines.append(
            f'      <module'
            f' fileurl="file://$PROJECT_DIR$/{root_name}.iml"'
            f' filepath="$PROJECT_DIR$/{root_name}.iml" />'
        )

        # One entry per discovered child module
        seen: set = {self.project_root}
        for pom_path in all_module_poms:
            mod_dir = pom_path.parent
            if mod_dir not in seen:
                seen.add(mod_dir)
                lines.append(_iml_entry(mod_dir))

        lines += [
            "    </modules>",
            "  </component>",
            "</project>",
            "",
        ]

        out = self.idea / "modules.xml"
        _guarded_write_text(out, "\n".join(lines))
        _ok(f"modules.xml             : {out}")
        return out

    # ── maven delegate settings ───────────────────────────────────────────

    def maven_settings_xml(self, maven_command: str) -> Path:
        """
        Write workspace.xml-style Maven runner settings so IntelliJ delegates
        build events to Maven with the user-supplied command / profiles.
        """
        # Extract -P <profiles> from the command if present
        profile_match = re.search(r"-P\s*(\S+)", maven_command)
        profiles_block = ""
        if profile_match:
            for prof in profile_match.group(1).split(","):
                profiles_block += f'        <profile name="{_xml_esc(prof.strip())}" />\n'

        xml = dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project version="4">
              <component name="MavenProjectsManager">
                <option name="importingSettings">
                  <MavenImportingSettings>
                    <option name="downloadDocsAutomatically" value="false" />
                    <option name="downloadSourcesAutomatically" value="true" />
                    <option name="createModuleForAggregatorPom" value="true" />
                    <option name="importAutomatically" value="false" />
                  </MavenImportingSettings>
                </option>
                <option name="generalSettings">
                  <MavenGeneralSettings>
                    <option name="workOffline" value="false" />
                    <option name="nonRecursive" value="false" />
                    <option name="printErrorStackTraces" value="true" />
                    <option name="usePluginRegistry" value="false" />
                    <option name="skipTests" value="true" />
                  </MavenGeneralSettings>
                </option>
              </component>
            </project>
        """)
        out = self.idea / "maven.xml"
        _guarded_write_text(out, xml)
        _ok(f"maven.xml               : {out}")
        return out


# ──────────────────────────────────────────────────────────────────────────────
# Native directory
# ──────────────────────────────────────────────────────────────────────────────

def ensure_native_dir(project_root: Path, native_dir: str) -> Path:
    native = project_root / native_dir
    _guarded_mkdir(native)
    readme = native / "README.md"
    if not readme.exists():
        _guarded_write_text(
            readme,
            dedent(f"""\
                # Native Libraries (`{native_dir}/`)

                Place **all** required native libraries here before running the app.

                | File | Platform | Description |
                |------|----------|-------------|
                | example.dll | Windows x64 | Replace with real entries |

                ## Why this directory exists

                The IntelliJ run configuration sets both:

                ```
                -Djava.library.path=$PROJECT_DIR$/{native_dir}
                -Djna.library.path=$PROJECT_DIR$/{native_dir}
                ```

                JNA uses its own lookup path independent of `java.library.path`.
                **Both must be set** or JNA will silently fail.

                ## Bitness

                Ensure every DLL/SO matches your JVM bitness (64-bit JVM → 64-bit libraries).
            """),
        )
    _ok(f"native dir              : {native}")
    return native


# ──────────────────────────────────────────────────────────────────────────────
# Maven build command prompt
# ──────────────────────────────────────────────────────────────────────────────

def prompt_maven_command(
    project_root: Path,
    native_dir: str,
    deploy_dir: Optional[str],
) -> str:
    """
    Ask the user for the full Maven command they use to build the project.
    This captures any -P profiles, -D properties, or other flags that live
    outside the pom.xml.  Saves both a .cmd (Windows) and .sh (Unix) script.

    If deploy_dir is set, the scripts also copy *.dll/*.so/*.dylib from the
    deployment directory into the native directory after the Maven build.
    """
    # ── Detect Maven Wrapper ──────────────────────────────────────────────
    has_mvnw = (project_root / "mvnw").exists() or (project_root / "mvnw.cmd").exists()
    default_mvn = "./mvnw" if has_mvnw else "mvn"
    default_cmd = f"{default_mvn} clean package -DskipTests"

    print()
    print(DIVIDER)
    print("  MAVEN BUILD COMMAND")
    print(THIN)
    if has_mvnw:
        _ok("Maven Wrapper detected — default will use ./mvnw")
    print("  Your project may require specific profiles, system properties,")
    print("  or environment variables to build successfully.")
    print()
    print("  Enter the FULL Maven command you normally use.  Examples:")
    print(f"    {default_mvn} clean install -P build-msi-local,Release")
    print(f"    {default_mvn} clean package -DskipTests")
    print()
    print("  Press ENTER to accept the default:")
    print(f"    {default_cmd}")
    print(DIVIDER)

    raw = input("  Command: ").strip()
    cmd = raw if raw else default_cmd

    # Guarantee -DskipTests is present
    if "skipTests" not in cmd and "maven.test.skip" not in cmd:
        cmd += " -DskipTests"

    # ── Build the native-copy block if deploy_dir is configured ───────────
    sh_copy_block = ""
    cmd_copy_block = ""
    if deploy_dir:
        sh_copy_block = dedent(f"""\

            # ── Stage native libraries from deployment directory ──────────────
            echo "Copying native libraries from {deploy_dir}/ to {native_dir}/ ..."
            mkdir -p "{native_dir}"
            find "{deploy_dir}" -type f \\( -iname "*.dll" -o -iname "*.so" -o -iname "*.dylib" \\) \\
                -exec cp -v {{}} "{native_dir}/" \\;
            echo "Native libraries staged."
        """)
        cmd_copy_block = dedent(f"""\

            REM ── Stage native libraries from deployment directory ─────────────
            echo Copying native libraries from {deploy_dir}\\ to {native_dir}\\ ...
            if not exist "{native_dir}" mkdir "{native_dir}"
            for /R "{deploy_dir}" %%F in (*.dll *.so *.dylib) do (
                echo   %%F
                copy /Y "%%F" "{native_dir}\\" >nul
            )
            echo Native libraries staged.
        """)

    # ── Windows batch script ───────────────────────────────────────────────
    # On Windows, ./mvnw → mvnw.cmd
    win_cmd = cmd.replace("./mvnw", "mvnw.cmd")
    bat = project_root / "build.cmd"
    _guarded_write_text(
        bat,
        dedent(f"""\
            @echo off
            REM ─────────────────────────────────────────────────────────────────
            REM  Maven build script — generated by eclipse_to_intellij.py
            REM  Run this ONCE from the project root before opening IntelliJ.
            REM ─────────────────────────────────────────────────────────────────
            cd /d "%~dp0"
            {win_cmd}
        """) + cmd_copy_block,
    )

    # ── Unix shell script ──────────────────────────────────────────────────
    sh = project_root / "build.sh"
    _guarded_write_text(
        sh,
        dedent(f"""\
            #!/usr/bin/env bash
            # ─────────────────────────────────────────────────────────────────
            #  Maven build script — generated by eclipse_to_intellij.py
            #  Run this ONCE from the project root before opening IntelliJ.
            # ─────────────────────────────────────────────────────────────────
            set -e
            cd "$(dirname "$0")"
            {cmd}
        """) + sh_copy_block,
    )
    if not DRY_RUN:
        try:
            sh.chmod(0o755)
        except Exception:
            pass

    _ok(f"build.cmd / build.sh    : {project_root}")
    return cmd


# ──────────────────────────────────────────────────────────────────────────────
# Child POM fixups
# ──────────────────────────────────────────────────────────────────────────────

def fixup_child_poms(
    module_poms: List[Path], source: str, target: str, encoding: str
) -> None:
    """
    Scan every child pom.xml.  If a child re-declares compiler source/target
    — either via <properties> or via maven-compiler-plugin <configuration> —
    it will override the parent.  Fix those.
    Also enforce test-skip wherever surefire/failsafe is declared.
    """
    for pom_path in module_poms:
        try:
            h = PomHandler(pom_path)
        except Exception as exc:
            _warn(f"Could not parse {pom_path}: {exc}")
            continue

        mod_label = pom_path.parent.name
        changed = False

        # ── fix <properties> overrides (maven.compiler.source/target) ─────
        props = h._find(h._root, "properties")
        if props is not None:
            prop_map = {
                "maven.compiler.source":  source,
                "maven.compiler.target":  target,
                "maven.compiler.encoding": encoding,
            }
            for prop_name, expected in prop_map.items():
                el = h._find(props, prop_name)
                if el is not None and el.text and el.text.strip() != expected:
                    old = el.text.strip()
                    el.text = expected
                    changed = True
                    _warn(f"  {mod_label}: <properties>/{prop_name} was {old} → {expected}")

        # ── fix compiler plugin overrides ──────────────────────────────────
        build = h._find(h._root, "build")
        if build is not None:
            plugins = h._find(build, "plugins")
        else:
            plugins = None

        if plugins is not None:
            compiler = h._find_plugin(plugins, "maven-compiler-plugin")
            if compiler is not None:
                cfg = h._find(compiler, "configuration")
                if cfg is not None:
                    for tag in ("source", "target", "encoding"):
                        el = h._find(cfg, tag)
                        if el is not None:
                            old = el.text
                            new = {"source": source, "target": target, "encoding": encoding}[tag]
                            if old != new:
                                el.text = new
                                changed = True

            # ── strip rogue surefire / failsafe ────────────────────────────
            for aid in ("maven-surefire-plugin", "maven-failsafe-plugin"):
                plug = h._find_plugin(plugins, aid)
                if plug is not None:
                    cfg = h._find(plug, "configuration")
                    if cfg is None:
                        cfg = ET.SubElement(plug, h._q("configuration"))
                    for skip_tag in ("skip", "skipTests"):
                        el = h._find(cfg, skip_tag)
                        if el is None:
                            el = ET.SubElement(cfg, h._q(skip_tag))
                        if el.text != "true":
                            el.text = "true"
                            changed = True

        if changed:
            h.save()
            _ok(f"  Fixed overrides in       : {mod_label}/pom.xml")

        # ── check for output directory customisations ──────────────────────
        if build is not None:
            out_dir = h._find(build, "outputDirectory")
            if out_dir is not None:
                _info(
                    f"  Custom <outputDirectory> in {mod_label}/pom.xml: "
                    f"{out_dir.text!r} — verify this matches IntelliJ module output."
                )

        # ── check for JPMS module-info.java ────────────────────────────────
        mod_info = pom_path.parent / "src" / "main" / "java" / "module-info.java"
        if mod_info.exists():
            _warn(
                f"  JPMS detected in {mod_label}. "
                f"You may need --add-reads / --add-opens in VM args."
            )


# ──────────────────────────────────────────────────────────────────────────────
# Eclipse workspace file cleanup
# ──────────────────────────────────────────────────────────────────────────────

ECLIPSE_ARTIFACTS = [".classpath", ".project", ".settings"]


def purge_eclipse_files(project_root: Path, module_poms: List[Path]) -> None:
    """
    Delete Eclipse workspace files (.classpath, .project, .settings/) from
    the project root and every module directory.  These are derived state —
    the POM is the source of truth — and their presence can interfere with
    IntelliJ's Maven import.
    """
    dirs_to_scan = [project_root] + [p.parent for p in module_poms]
    deleted: List[str] = []

    for d in dirs_to_scan:
        for name in ECLIPSE_ARTIFACTS:
            target = d / name
            if target.is_file():
                if DRY_RUN:
                    _info(f"[DRY-RUN] Would delete {target}")
                else:
                    target.unlink()
                deleted.append(str(target.relative_to(project_root)))
            elif target.is_dir():
                if DRY_RUN:
                    _info(f"[DRY-RUN] Would delete {target}/")
                else:
                    shutil.rmtree(target)
                deleted.append(str(target.relative_to(project_root)) + "/")

    if deleted:
        _ok(f"Deleted {len(deleted)} Eclipse artifact(s): {', '.join(deleted)}")
    else:
        _info("No Eclipse workspace files found — nothing to clean")

    # Ensure these patterns are in the root .gitignore so they don't come back
    _update_root_gitignore(project_root)


def _update_root_gitignore(project_root: Path) -> None:
    """Append Eclipse artifact patterns to the root .gitignore if missing."""
    gi_path = project_root / ".gitignore"
    patterns = [".classpath", ".project", ".settings/"]

    existing = ""
    if gi_path.exists():
        existing = gi_path.read_text(encoding="utf-8")

    missing = [p for p in patterns if p not in existing]
    if not missing:
        return

    block = (
        "\n# Eclipse workspace files (generated — do not commit)\n"
        + "\n".join(missing)
        + "\n"
    )

    if DRY_RUN:
        _info(f"[DRY-RUN] Would append Eclipse patterns to {gi_path}")
        return

    with open(gi_path, "a", encoding="utf-8") as f:
        f.write(block)
    _ok(f"Added {', '.join(missing)} to root .gitignore")


# ──────────────────────────────────────────────────────────────────────────────
# Console helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ok(msg: str)   -> None: print(f"  ✅  {msg}")
def _warn(msg: str) -> None: print(f"  ⚠️   {msg}")
def _info(msg: str) -> None: print(f"  ℹ️   {msg}")
def _err(msg: str)  -> None: print(f"  ❌  {msg}")


# ──────────────────────────────────────────────────────────────────────────────
# Final step-by-step instructions
# ──────────────────────────────────────────────────────────────────────────────

def print_instructions(
    project_root: Path,
    app_name:     str,
    jdk_version:  str,
    native_dir:   str,
    deploy_dir:   Optional[str],
    source:       str,
    target:       str,
    launch:       EclipseLaunch,
    module_name:  str,
) -> None:

    safe_config = re.sub(r"[^A-Za-z0-9_\-]", "_", app_name)

    remaining_eclipse_vars = re.findall(
        r"\$\{[^}]+\}", launch.vm_arguments + launch.program_arguments
    )

    print(f"\n\n{DIVIDER}")
    print("  SETUP COMPLETE")
    print(DIVIDER)

    if deploy_dir:
        copy_note = (
            f"build.sh / build.cmd will run Maven and then automatically copy\n"
            f"    •   native libraries from {deploy_dir}/ into {native_dir}/."
        )
    else:
        copy_note = (
            f"You must manually place all native libraries in {native_dir}/\n"
            f"    •   before running the app.  Re-run with --deploy-dir to automate this."
        )

    steps = [
        (
            "Run the build script ONCE before opening IntelliJ",
            [
                f"Terminal → cd {project_root}",
                "Windows : build.cmd",
                "Linux / macOS : ./build.sh",
                "This downloads all dependencies, compiles all modules,",
                "  copies all runtime JARs to each module's lib/,",
                f"  and stages native libraries for IntelliJ.",
                copy_note,
                "⚠️  Do NOT skip — IntelliJ needs compiled output to resolve symbols.",
            ],
        ),
        (
            f"Register JDK {jdk_version} in IntelliJ (one-time, per developer machine)",
            [
                "File → Project Structure → Platform Settings → SDKs",
                f"Click + → Add JDK → browse to your JDK {jdk_version} home directory.",
                f"Set the name to exactly: {jdk_version}",
                "  (misc.xml references this name — a typo breaks the project JDK.)",
                "Click OK / Apply.",
            ],
        ),
        (
            "Open the project",
            [
                "IntelliJ → File → Open",
                f"Select: {project_root / 'pom.xml'}",
                "Choose 'Open as Project'.",
                "When prompted 'Trust project?' → Trust.",
                "When the Maven import dialog appears → confirm import of ALL modules.",
                "⚠️  If IntelliJ does NOT show a Maven import dialog automatically:",
                "     View → Tool Windows → Maven → click 'Reload All Maven Projects' ↻",
                "     Maven sync is what creates .iml files and marks source roots.",
                "     Without it, src/main/java will NOT be recognised as Sources Root",
                "     and you will see red imports everywhere.",
            ],
        ),
        (
            "Activate Maven profiles (if your build.cmd uses -P ...)",
            [
                "View → Tool Windows → Maven",
                "In the Maven panel → expand your project → Profiles",
                "Check every profile that appears in your build.cmd / build.sh.",
                "Then click the 'Reload All Maven Projects' ↻ button.",
                "Skip this step if your build uses no -P profiles.",
            ],
        ),
        (
            "Verify source roots after Maven sync completes",
            [
                "In the Project panel, every module's src/main/java should appear",
                "  with a BLUE folder icon — that means it is marked as Sources Root.",
                "If any folder still shows a plain (non-blue) icon:",
                "  Right-click the folder → Mark Directory as → Sources Root.",
                "Alternatively: File → Project Structure → Modules → select the",
                "  module → Sources tab → mark src/main/java as Sources.",
                "⚠️  Plain/red folder icons after sync mean Maven sync did not finish",
                "     successfully. Check View → Tool Windows → Maven → sync log.",
                "     Common causes: missing profiles, unresolvable dependencies,",
                "     or a child POM referencing a parent version not yet in .m2/.",
            ],
        ),
        (
            "Wait for background indexing to finish",
            [
                "Watch the bottom status bar — it shows indexing progress.",
                "Symbol resolution errors disappear once indexing completes.",
                "This may take several minutes on first load.",
                "Do NOT attempt to run the app until the status bar is clear.",
            ],
        ),
        (
            "Verify the run configuration",
            [
                f"Top-right dropdown should show: '{app_name}'",
                f"If missing: Run → Edit Configurations → Application → '{app_name}'",
                f"File on disk: .idea/runConfigurations/{safe_config}.xml",
                f"Module: {module_name}",
                f"Main class: {launch.main_class}",
            ],
        ),
        (
            "Click ▶ Run",
            [
                "The application launches with all JVM parameters,",
                "  environment variables, and native library paths pre-set.",
                "No further configuration should be required.",
            ],
        ),
    ]

    for i, (title, details) in enumerate(steps, 1):
        print(f"\n  Step {i}: {title}")
        print(f"  {THIN[:len(title) + 9]}")
        for d in details:
            print(f"    • {d}")

    # ── Summary of changes ─────────────────────────────────────────────────
    print(f"\n\n{DIVIDER}")
    print("  WHAT WAS MODIFIED / CREATED")
    print(DIVIDER)
    print(f"""
  parent pom.xml (backed up as pom.xml.bak)
    • <properties>: compiler source={source}, target={target}, UTF-8 encoding
    • <properties>: maven.test.skip=true, skipTests=true, maven.javadoc.skip=true
    • maven-compiler-plugin {source}/{target}, no test compilation
    • maven-surefire-plugin: skip=true (unit tests)
    • maven-failsafe-plugin: skip=true (integration tests)
    • maven-javadoc-plugin:  skip=true
    • maven-dependency-plugin: copies all runtime JARs → each module's lib/

  child pom.xml files (each backed up as pom.xml.bak)
    • Compiler source/target corrected where child overrode parent
    • Test-skip enforced wherever surefire/failsafe was declared

  .idea/ files (created or overwritten)
    • runConfigurations/{safe_config}.xml  ← the run config
    • modules.xml       ← full submodule list (all modules visible on first open)
    • misc.xml          ← JDK {jdk_version}, language level
    • compiler.xml      ← bytecode target {target}, annotation processors
    • encodings.xml     ← UTF-8 project-wide
    • vcs.xml           ← Git mapping (if .git present)
    • maven.xml         ← Maven import settings (no auto-import, skip tests)
    • .gitignore        ← runConfigurations/ committed; workspace.xml excluded

  Project root
    • {native_dir}/          ← place all DLLs/SOs here
    • {native_dir}/README.md ← instructions for native libs
    • build.cmd         ← Windows build script
    • build.sh          ← Unix build script

  Deleted (Eclipse workspace state — POM is the source of truth)
    • .classpath, .project, .settings/ (from root and all modules, if present)
    • Patterns added to root .gitignore to prevent re-commit
""")

    # ── Warnings ──────────────────────────────────────────────────────────
    print(DIVIDER)
    print("  THINGS TO DOUBLE-CHECK")
    print(DIVIDER)

    warnings = [
        (
            "JDK name must match exactly",
            f"misc.xml expects the SDK named '{jdk_version}' in IntelliJ's SDK list. "
            f"If developers use a different name (e.g. 'openjdk-{jdk_version}') "
            f"the project JDK will be 'unknown'. Standardise the name across the team."
        ),
        (
            "DLL / JVM bitness",
            "Every native library must be the same bitness as the JVM. "
            "A 64-bit JVM with a 32-bit DLL produces a silent load failure via JNA."
        ),
        (
            "Eclipse variable substitution",
            "VM args were translated but some Eclipse variables may remain. "
            + (f"Still unresolved: {remaining_eclipse_vars}" if remaining_eclipse_vars
               else "None detected — looks clean.")
        ),
        (
            "Maven profiles in settings.xml",
            "Profiles activated in ~/.m2/settings.xml or via activeProfiles are NOT "
            "visible to IntelliJ automatically. Open the Maven panel and activate them manually."
        ),
        (
            "--add-opens / --add-exports (Java 9+)",
            "If the app uses reflection on JDK internals, ensure every required "
            "--add-opens / --add-exports line is in the VM_PARAMETERS block of "
            f".idea/runConfigurations/{safe_config}.xml."
        ),
        (
            "Annotation processors (Lombok, MapStruct, etc.)",
            "compiler.xml enables annotation processing via classpath. If a module "
            "uses a processor from a non-default path, add it in "
            "File → Settings → Build → Compiler → Annotation Processors."
        ),
        (
            "Maven Wrapper (mvnw)",
            "If the project uses mvnw, the script auto-detected it and the "
            "build scripts already use ./mvnw.  Verify build.cmd / build.sh "
            "if you added mvnw after this script ran."
        ),
        (
            "JPMS (module-info.java)",
            "If any module has a module-info.java, every --add-reads / --add-opens "
            "needed by the launcher must also be in the run config VM parameters."
        ),
    ]

    for title, detail in warnings:
        print(f"\n  ⚠️  {title}")
        print(f"     {detail}")

    print(f"\n\n{DIVIDER}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global DRY_RUN
    args = parse_args()
    DRY_RUN = args.dry_run

    print(f"\n{DIVIDER}")
    print("  Eclipse → IntelliJ IDEA  |  Multi-Module Maven Migration")
    print(DIVIDER)
    if DRY_RUN:
        print("  *** DRY RUN — no files will be modified ***")

    # ── Validate paths ─────────────────────────────────────────────────────
    pom_path    = Path(args.pom).resolve()
    launch_path = Path(args.launch).resolve()

    for p, label in [(pom_path, "POM"), (launch_path, "Launch file")]:
        if not p.exists():
            _err(f"{label} not found: {p}")
            sys.exit(1)

    project_root = pom_path.parent

    print(f"\n  Project root : {project_root}")
    print(f"  Parent POM   : {pom_path.name}")
    print(f"  Launch file  : {launch_path.name}")
    print(f"  JDK          : {args.jdk}")
    print(f"  Source       : {args.source}   Target: {args.target}")
    print(f"  Encoding     : {args.encoding}")
    print(f"  Native dir   : {args.native_dir}")
    if args.deploy_dir:
        print(f"  Deploy dir   : {args.deploy_dir}")
    else:
        print(f"  Deploy dir   : (not set — DLLs must be placed in {args.native_dir}/ manually)")

    # ── Step 1: Parse Eclipse launch file ─────────────────────────────────
    print(f"\n[1/8] Parsing Eclipse launch file …")
    launch = EclipseLaunch(str(launch_path))

    if not launch.main_class:
        _err(
            "No MAIN_TYPE found in launch file.\n"
            "       Expected a <stringAttribute key='org.eclipse.jdt.launching.MAIN_TYPE' …>"
        )
        sys.exit(1)

    app_name = (
        args.app_name
        or launch_path.stem
        or launch.project_name
        or launch.main_class.split(".")[-1]
    )

    _ok(f"Main class   : {launch.main_class}")
    _ok(f"App name     : {app_name}")
    vm_preview = launch.vm_arguments[:100] + ("…" if len(launch.vm_arguments) > 100 else "")
    _ok(f"VM args      : {vm_preview or '(none)'}")
    _ok(f"Prog args    : {launch.program_arguments or '(none)'}")
    _ok(f"Working dir  : {launch.working_directory or '(not set → $PROJECT_DIR$)'}")
    _ok(f"Env vars     : {len(launch.env_vars)} variable(s)")

    # ── Step 2: Discover modules ───────────────────────────────────────────
    print(f"\n[2/8] Discovering Maven modules …")
    parent = PomHandler(pom_path)
    top_level = parent.modules()
    all_poms  = collect_all_module_poms(parent)

    # Report where modules were found
    profile_counts = parent.profile_module_counts()
    for pid, cnt in profile_counts:
        _info(f"Found {cnt} module(s) inside profile '{pid}'")

    _ok(f"Top-level modules : {len(top_level)} → {', '.join(top_level)}")
    _ok(f"Total child POMs  : {len(all_poms)} (including nested)")

    # ── Step 3: Find launcher module ───────────────────────────────────────
    print(f"\n[3/8] Locating launcher module …")
    module_name, module_dir = find_launcher_module(parent, launch.main_class)

    # ── Step 4: Prompt for Maven command ──────────────────────────────────
    print(f"\n[4/8] Maven build command …")
    maven_command = prompt_maven_command(project_root, args.native_dir, args.deploy_dir)

    # ── Step 5: Modify parent POM ─────────────────────────────────────────
    print(f"\n[5/8] Modifying parent pom.xml …")
    parent.set_properties(args.source, args.target, args.encoding)
    parent.configure_compiler_plugin(args.source, args.target, args.encoding)
    parent.configure_surefire_plugin()
    parent.configure_failsafe_plugin()
    parent.configure_javadoc_plugin()
    parent.configure_dependency_plugin()
    parent.save()
    _ok(f"Saved + backup : pom.xml.bak")

    # ── Step 6: Fixup child POMs ──────────────────────────────────────────
    print(f"\n[6/8] Checking {len(all_poms)} child POM(s) for overrides …")
    fixup_child_poms(all_poms, args.source, args.target, args.encoding)

    # ── Step 7: Purge Eclipse workspace files ─────────────────────────────
    print(f"\n[7/8] Cleaning Eclipse workspace files …")
    purge_eclipse_files(project_root, all_poms)

    # ── Step 8: Generate .idea/ files ─────────────────────────────────────
    print(f"\n[8/8] Generating .idea/ configuration …")
    ensure_native_dir(project_root, args.native_dir)

    gen = IdeaGenerator(
        project_root=project_root,
        app_name=app_name,
        jdk_version=args.jdk,
        source=args.source,
        target=args.target,
        encoding=args.encoding,
        native_dir=args.native_dir,
        launch=launch,
    )

    gen.misc_xml()
    gen.compiler_xml()
    gen.encodings_xml()
    gen.gitignore()
    gen.vcs_xml()
    gen.maven_settings_xml(maven_command)
    gen.modules_xml(all_poms)
    gen.run_config(module_name)

    # ── Print human-readable instructions ─────────────────────────────────
    print_instructions(
        project_root=project_root,
        app_name=app_name,
        jdk_version=args.jdk,
        native_dir=args.native_dir,
        deploy_dir=args.deploy_dir,
        source=args.source,
        target=args.target,
        launch=launch,
        module_name=module_name,
    )


if __name__ == "__main__":
    main()
