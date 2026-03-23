# Specification: `eclipse_to_intellij.py`

## Purpose

A single-file Python 3.8+ CLI tool (stdlib only, zero pip dependencies) that converts an Eclipse `.launch` file and a multi-module Maven parent `pom.xml` into a fully configured IntelliJ IDEA project. When done, a developer clones the repo, runs one Maven build, opens the root `pom.xml` in IntelliJ, and clicks **Run** — nothing else required.

## CLI Interface

```
python eclipse_to_intellij.py \
    --pom    /path/to/parent/pom.xml \
    --launch /path/to/MyApp.launch   \
    --jdk    17                      \
    --source 11                      \
    --target 11
```

### Required arguments

| Flag | Type | Description |
|------|------|-------------|
| `--pom` | path | Path to the parent `pom.xml`. Its parent directory is the **project root**. |
| `--launch` | path | Path to an Eclipse `.launch` XML file. |
| `--jdk` | string | JDK version or name as registered in IntelliJ's SDK list (e.g. `17`, `temurin-21`). Used verbatim in `misc.xml` `project-jdk-name`. |
| `--source` | string | Maven compiler source level (e.g. `11`, `17`). |
| `--target` | string | Maven compiler target level (e.g. `11`, `17`). |

### Optional arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--app-name` | `.launch` filename stem | Display name for the IntelliJ run configuration. |
| `--native-dir` | `native` | Directory (relative to project root) for DLL/SO/DYLIB files. |
| `--deploy-dir` | `None` | Directory (relative to project root) where Maven deposits native libraries after build (e.g. `target/deploy`). If set, the generated build scripts will automatically copy `*.dll`/`*.so`/`*.dylib` from this directory into `native-dir` after the Maven build completes. |
| `--encoding` | `UTF-8` | Source file encoding. |
| `--dry-run` | off | Preview all changes to stdout without writing, creating, or modifying any files. |

Validate that `--pom` and `--launch` exist. Exit with error if not.

---

## Execution Steps

The script executes eight sequential steps. Print a `[N/8]` progress header before each.

---

### Step 1 — Parse the Eclipse `.launch` file

Eclipse `.launch` files are XML with a `<launchConfiguration>` root. Data is stored in typed attribute elements.

**Extract these fields:**

| Field | XML source | Eclipse attribute key |
|-------|-----------|----------------------|
| Main class (required) | `<stringAttribute>` | `org.eclipse.jdt.launching.MAIN_TYPE` |
| VM arguments | `<stringAttribute>` | `org.eclipse.jdt.launching.VM_ARGUMENTS` |
| Program arguments | `<stringAttribute>` | `org.eclipse.jdt.launching.PROGRAM_ARGUMENTS` |
| Working directory | `<stringAttribute>` | `org.eclipse.jdt.launching.WORKING_DIRECTORY` |
| Project name | `<stringAttribute>` | `org.eclipse.jdt.launching.PROJECT_ATTR` |
| Environment variables | `<mapAttribute>` containing `<mapEntry key=... value=...>` children | `org.eclipse.debug.core.environmentVariables` |
| Stop in main | `<booleanAttribute>` | `org.eclipse.jdt.launching.STOP_IN_MAIN` |

**Expose these as `@property` accessors** on a class (lazy reads from the parsed XML tree, not eagerly extracted in `__init__`).

If `main_class` is empty, print an error and `sys.exit(1)`.

Derive `app_name` with this priority chain: `--app-name` flag → `.launch` filename stem → Eclipse project name → simple class name (last segment of main class).

---

### Step 2 — Discover all Maven modules

The `modules()` method on `PomHandler` collects `<module>` entries from **two** locations:

1. **Top-level:** `<project><modules><module>` — the standard location.
2. **Inside profiles:** `<project><profiles><profile><modules><module>` — Maven allows modules to be scoped to a profile (e.g. modules only built under a `release` profile). Many real-world projects use this pattern.

Scan both locations. Deduplicate by module path, preserving first-seen order. A separate `profile_module_counts()` method returns `[(profile_id, count), ...]` for reporting — the caller in `main()` prints an info message like `Found 4 module(s) inside profile 'release'` once per profile that contributed modules.

Starting from the parent POM, recursively walk the discovered module paths. For each `<module>` relative path, check if `{parent_dir}/{module}/pom.xml` exists. If it does, add it to a flat list and recurse into that child's own `<modules>` (which also scans profiles). If the child POM is malformed, silently skip recursion on it.

Return a `List[Path]` of every discovered child `pom.xml`.

**Critical: handle the Maven XML namespace.** POMs may or may not use `xmlns="http://maven.apache.org/POM/4.0.0"`. Sniff the namespace from the root element tag. All subsequent `find`/`findall` calls must use the detected namespace prefix. Build a helper that qualifies tag names: if namespace is present, `{ns}tagName`; otherwise bare `tagName`.

---

### Step 3 — Find the launcher module

Convert the main class FQCN to a relative path: `com.example.Foo` → `com/example/Foo.java`. Search every discovered module's `src/main/java/` tree for this file.

- **Single match:** use it automatically. Read the module's `<artifactId>` from its `pom.xml`.
- **Multiple matches:** list them and prompt the user to pick by number.
- **No matches:** list all known modules and prompt the user to pick by number or type an `artifactId` directly.

The interactive prompt must loop until valid input is received. Accepting a raw `artifactId` string (not a number) is a valid fallback.

---

### Step 4 — Prompt for the Maven build command and generate build scripts

The function signature is `prompt_maven_command(project_root, native_dir, deploy_dir)`.

**Maven Wrapper detection:** Before prompting, check if `mvnw` or `mvnw.cmd` exists at the project root. If found, use `./mvnw` as the default command prefix. Otherwise use `mvn`.

Print a banner showing example commands using the detected prefix. Prompt the user for their full Maven command. If they press Enter, use the default: `{prefix} clean package -DskipTests`.

**Auto-inject `-DskipTests`:** If the entered command contains neither `skipTests` nor `maven.test.skip`, append `-DskipTests`.

Generate two files at the project root:

**`build.sh`** (Unix):
```bash
#!/usr/bin/env bash
# (header comment: generated by eclipse_to_intellij.py)
set -e
cd "$(dirname "$0")"
{command}

# ── Stage native libraries from deployment directory (if --deploy-dir set) ──
echo "Copying native libraries from {deploy_dir}/ to {native_dir}/ ..."
mkdir -p "{native_dir}"
find "{deploy_dir}" -type f \( -iname "*.dll" -o -iname "*.so" -o -iname "*.dylib" \) \
    -exec cp -v {} "{native_dir}/" \;
echo "Native libraries staged."
```
Set executable permission `0o755`.

**`build.cmd`** (Windows):
```batch
@echo off
REM (header comment: generated by eclipse_to_intellij.py)
cd /d "%~dp0"
{command with ./mvnw replaced by mvnw.cmd}

REM ── Stage native libraries from deployment directory (if --deploy-dir set) ──
echo Copying native libraries from {deploy_dir}\ to {native_dir}\ ...
if not exist "{native_dir}" mkdir "{native_dir}"
for /R "{deploy_dir}" %%F in (*.dll *.so *.dylib) do (
    echo   %%F
    copy /Y "%%F" "{native_dir}\" >nul
)
echo Native libraries staged.
```

**If `--deploy-dir` is not set**, omit the native library copy blocks entirely. The build scripts will contain only the Maven command.

The native library copy block finds all `*.dll`, `*.so`, and `*.dylib` files anywhere under the deployment directory (recursively) and copies them flat into the native directory. This handles the case where Maven assembly/dependency plugins download DLLs from Nexus into `target/` subdirectories during the build — IntelliJ's Maven import does **not** execute build profiles or assembly plugins, so these DLLs must be staged before IntelliJ opens.

---

### Step 5 — Modify the parent POM

**Back up first.** Before any write, copy the original to `pom.xml.bak` (but only the first time — don't overwrite an existing backup).

Use `xml.etree.ElementTree`. Register the Maven namespace with `ET.register_namespace("", MAVEN_NS)` so output doesn't sprout `ns0:` prefixes.

#### 5a. Set `<properties>`

Create `<properties>` if absent. Set or overwrite these children:

| Property | Value |
|----------|-------|
| `maven.compiler.source` | `{source}` |
| `maven.compiler.target` | `{target}` |
| `maven.compiler.encoding` | `{encoding}` |
| `project.build.sourceEncoding` | `{encoding}` |
| `project.reporting.outputEncoding` | `{encoding}` |
| `maven.test.skip` | `true` |
| `skipTests` | `true` |
| `maven.javadoc.skip` | `true` |

#### 5b. Configure plugins

For each plugin below: find it in `<build><plugins>` by `<artifactId>`. If absent, create the `<plugin>` element with the correct `<groupId>` and `<artifactId>`. Set `<version>`. Set `<configuration>` children.

**maven-compiler-plugin** (version `3.13.0`):

| Config key | Value |
|------------|-------|
| `source` | `{source}` |
| `target` | `{target}` |
| `encoding` | `{encoding}` |
| `skipMain` | `false` |
| `skip` | `false` |

**maven-surefire-plugin** (version `3.2.5`):

| Config key | Value |
|------------|-------|
| `skip` | `true` |
| `skipTests` | `true` |

**maven-failsafe-plugin** (version `3.2.5`):

| Config key | Value |
|------------|-------|
| `skip` | `true` |
| `skipTests` | `true` |

**maven-javadoc-plugin** (version `3.6.3`):

| Config key | Value |
|------------|-------|
| `skip` | `true` |

**maven-dependency-plugin** (version `3.6.1`):

Add an `<execution>` with `<id>copy-deps</id>` (skip if one already exists with that id):

```xml
<execution>
    <id>copy-deps</id>
    <phase>package</phase>
    <goals><goal>copy-dependencies</goal></goals>
    <configuration>
        <outputDirectory>${project.basedir}/lib</outputDirectory>
        <overWriteIfNewer>true</overWriteIfNewer>
        <includeScope>runtime</includeScope>
        <overWriteReleases>false</overWriteReleases>
        <overWriteSnapshots>true</overWriteSnapshots>
    </configuration>
</execution>
```

#### 5c. Save

Pretty-print the XML tree in-place (4-space indent) before writing. Write as bytes with `encoding="UTF-8"` and `xml_declaration=True`.

---

### Step 6 — Fix child POM overrides

Iterate every child `pom.xml` from Step 2. For each, check three categories of overrides. Track a `changed` boolean per POM and only save once at the end if anything was modified.

#### 6a. `<properties>` overrides

Check if the child's `<properties>` contains `maven.compiler.source`, `maven.compiler.target`, or `maven.compiler.encoding`. If any of these exist and their text differs from the expected value, overwrite them. Print a warning for each: `"{module}: <properties>/{prop} was {old} → {new}"`.

#### 6b. Compiler plugin `<configuration>` overrides

If the child has `<build><plugins>` containing `maven-compiler-plugin`, check its `<configuration>` for `<source>`, `<target>`, `<encoding>`. Correct any that differ.

#### 6c. Surefire / Failsafe enforcement

If the child declares `maven-surefire-plugin` or `maven-failsafe-plugin`, ensure their `<configuration>` contains both `<skip>true</skip>` and `<skipTests>true</skipTests>`. Create the `<configuration>` element and/or the skip elements if they don't exist.

#### 6d. Informational warnings (no modification)

- If the child has a custom `<outputDirectory>` in `<build>`, print an info message (IntelliJ module output may not match).
- If `src/main/java/module-info.java` exists in the module, print a JPMS warning about `--add-reads` / `--add-opens`.

---

### Step 7 — Purge Eclipse workspace files

Eclipse generates `.classpath`, `.project`, and `.settings/` inside each project/module directory. These are workspace state derived from the POM. When present alongside a Maven `pom.xml`, they interfere with IntelliJ's Maven import — IntelliJ tries to reconcile both models and the result is unreliable. Since the POM is the sole source of truth for compilation, packaging, and deployment, these files are dead weight.

**Scan and delete:** Walk the project root and every module directory discovered in Step 2. For each, check for `.classpath` (file), `.project` (file), and `.settings/` (directory). Delete every one found. For directories, use `shutil.rmtree`. Print the count and relative paths of everything deleted.

**Update root `.gitignore`:** After deletion, read the root `.gitignore` (or treat as empty if it doesn't exist). Check whether each of `.classpath`, `.project`, `.settings/` appears in the file. For any that are missing, append them in a labeled block:

```gitignore

# Eclipse workspace files (generated — do not commit)
.classpath
.project
.settings/
```

Only append the patterns that are actually missing. If all three are already present, do nothing.

Store the target filenames as a constant: `ECLIPSE_ARTIFACTS = [".classpath", ".project", ".settings"]`.

---

### Step 8 — Generate `.idea/` files

Create the `.idea/` and `.idea/runConfigurations/` directories. Generate these files:

#### 8a. Run configuration: `.idea/runConfigurations/{SafeName}.xml`

Sanitize the app name for the filename: replace any character not in `[A-Za-z0-9_-]` with `_`.

**Eclipse variable translation** must happen before writing VM args and working directory. The translation rules are:

| Eclipse pattern | IntelliJ replacement | Notes |
|----------------|---------------------|-------|
| `${workspace_loc:/ProjectName/sub/path}` | `$PROJECT_DIR$/sub/path` | Strip the project name segment, **keep the trailing sub-path** |
| `${workspace_loc:/ProjectName}` | `$PROJECT_DIR$` | No sub-path |
| `${workspace_loc}` | `$PROJECT_DIR$` | Bare form |
| `${project_loc:...}` | `$PROJECT_DIR$` | Any variant |
| `${project_loc}` | `$PROJECT_DIR$` | Bare form |
| `${env_var:NAME}` | `$NAME$` | Best-effort; IntelliJ may not expand all of these |

**CRITICAL sub-path detail:** The `workspace_loc` regex must capture the optional trailing path after the project name. Pattern: `\$\{workspace_loc:/([^}/]+)(/[^}]*)?\}`. Group 1 = project name (discarded). Group 2 = optional sub-path (kept). Use a lambda replacement, not a static string.

**Native library path injection** into VM args:

For both `-Djava.library.path` and `-Djna.library.path`:
- If the flag **already exists** in the VM args, **append** `:{native_path}` to its existing value (preserving whatever Eclipse had). Do NOT replace.
- If the flag **does not exist**, append ` -D{prop}={native_path}` to the end.

Where `native_path` = `$PROJECT_DIR$/{native_dir}`.

**Run config XML structure:**

```xml
<component name="ProjectRunConfigurationManager">
  <configuration
    default="false"
    name="{app_name}"
    type="Application"
    factoryName="Application"
    singleton="true"
    nameIsGenerated="false">

    <option name="MAIN_CLASS_NAME" value="{main_class}" />
    <option name="MODULE_NAME" value="{launcher_artifact_id}" />
    <option name="VM_PARAMETERS" value="{translated_vm_args}" />
    <option name="PROGRAM_PARAMETERS" value="{program_args}" />
    <option name="WORKING_DIRECTORY" value="{translated_working_dir}" />
    <option name="ALTERNATIVE_JRE_PATH_ENABLED" value="false" />
    <option name="ALTERNATIVE_JRE_PATH" value="" />
    <option name="INCLUDE_PROVIDED_SCOPE" value="false" />

    <envs>
      <env name="{key}" value="{value}" />
      <!-- one per env var; use <envs /> if empty -->
    </envs>

    <method v="2">
      <option name="Make" enabled="true" />
    </method>
  </configuration>
</component>
```

**Important attributes:**
- `nameIsGenerated="false"` — prevents IntelliJ from auto-renaming.
- `ALTERNATIVE_JRE_PATH_ENABLED=false` — forces use of project JDK.
- `PROGRAM_PARAMETERS` — always present even if empty.
- All attribute values must be XML-escaped (`&`, `<`, `>`, `"`).

#### 8b. `misc.xml` — Project SDK and language level

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="ProjectRootManager"
             version="2"
             languageLevel="{lang_level}"
             project-jdk-name="{jdk}"
             project-jdk-type="JavaSDK">
    <output url="file://$PROJECT_DIR$/target/classes" />
  </component>
</project>
```

**`languageLevel`** must use IntelliJ's token format, which varies by Java version:

| Source version | IntelliJ token |
|---------------|---------------|
| `1.4` | `JDK_1_4` |
| `5` or `1.5` | `JDK_1_5` |
| `6` or `1.6` | `JDK_1_6` |
| `7` or `1.7` | `JDK_1_7` |
| `8` or `1.8` | `JDK_1_8` |
| `9` | `JDK_9` |
| `10` | `JDK_10` |
| `11` through `23` | `JDK_11` through `JDK_23` |

Store this as a lookup map. If the source version isn't in the map, fall back to `JDK_{source}`.

#### 8c. `compiler.xml` — Bytecode target and annotation processing

```xml
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
    <bytecodeTargetLevel target="{target}" />
  </component>
</project>
```

#### 8d. `encodings.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="Encoding">
    <file url="PROJECT" charset="{encoding}" />
    <file url="file://$PROJECT_DIR$" charset="{encoding}" />
  </component>
</project>
```

Both `PROJECT` and `file://$PROJECT_DIR$` entries are required — IntelliJ checks both scopes.

#### 8e. `.gitignore`

```gitignore
# ── IntelliJ default excludes ──
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

# ── Commit these so configs travel with the repository ──
!runConfigurations/
!misc.xml
!compiler.xml
!encodings.xml
!jarRepositories.xml
!vcs.xml
!maven.xml
!.gitignore
```

#### 8f. `vcs.xml` — only if `.git/` exists at project root

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project version="4">
  <component name="VcsDirectoryMappings">
    <mapping directory="$PROJECT_DIR$" vcs="Git" />
  </component>
</project>
```

#### 8g. `maven.xml` — Maven import and runner settings

Extract `-P profile1,profile2` from the Maven command (if present). Split on commas.

```xml
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
```

#### 8h. Native directory and README

Create `{project_root}/{native_dir}/` if it doesn't exist. Write a `README.md` inside it (only if the README doesn't already exist) that documents: what to place here, both JVM flags (`-Djava.library.path` and `-Djna.library.path`), why both are needed (JNA has an independent lookup), and the bitness requirement.

---

## Dry-Run Mode

When `--dry-run` is set, the script must:

- **Never** write, create, modify, or delete any file or directory.
- **Never** create backups.
- **Never** `chmod`.
- Print `[DRY-RUN] Would write {path}`, `[DRY-RUN] Would create directory {path}`, `[DRY-RUN] Would back up {file}`, `[DRY-RUN] Would delete {path}`, or `[DRY-RUN] Would append Eclipse patterns to {path}` for every operation that would have occurred.
- Still perform all in-memory XML parsing and modification (to detect and report child POM overrides, etc).
- Still prompt the user for the Maven command (the prompt is informational, not a side effect).

Implement this with a global `DRY_RUN` boolean and guarded wrapper functions for every I/O operation: write-text, write-XML-bytes, mkdir, backup, chmod, delete, and gitignore-append.

---

## Post-Execution Output

After all eight steps, print three sections:

### Section 1: "SETUP COMPLETE" — 8 numbered steps

1. Copy DLLs / native libraries into the native directory (document both `-Djava.library.path` and `-Djna.library.path`, bitness requirement).
2. Run the Maven build once before opening IntelliJ (reference `build.cmd` / `build.sh`).
3. Register the JDK in IntelliJ (File → Project Structure → SDKs, name must match exactly).
4. Open the project (File → Open → select root `pom.xml` → "Open as Project").
5. Activate Maven profiles if the build command uses `-P`.
6. Wait for indexing and Maven sync.
7. Verify the run configuration appears in the top-right dropdown.
8. Click ▶ Run.

### Section 2: "WHAT WAS MODIFIED / CREATED"

List every file touched, grouped by: parent POM changes, child POM changes, `.idea/` files, project root files, and a "Deleted" group noting that `.classpath`, `.project`, `.settings/` were removed from the root and all modules, with patterns added to root `.gitignore` to prevent re-commit.

### Section 3: "THINGS TO DOUBLE-CHECK" — 8 titled warnings

1. **JDK name must match exactly** — `misc.xml` `project-jdk-name` must match the SDK name in IntelliJ's list.
2. **DLL / JVM bitness** — 64-bit JVM requires 64-bit DLLs; JNA fails silently on mismatch.
3. **Eclipse variable substitution** — scan original (pre-translation) VM args + program args for remaining `${...}` patterns and list any that weren't translated.
4. **Maven profiles in `settings.xml`** — profiles from `~/.m2/settings.xml` aren't visible to IntelliJ automatically.
5. **`--add-opens` / `--add-exports`** — needed if the app reflects on JDK internals.
6. **Annotation processors** — `compiler.xml` enables classpath-based processing; non-default processor paths need manual config.
7. **Maven Wrapper** — note whether auto-detected; remind to verify if `mvnw` was added after the script ran.
8. **JPMS** — if any module has `module-info.java`, `--add-reads` / `--add-opens` may be needed.

---

## Architecture and Design Requirements

### Class: `EclipseLaunch`

- Constructor takes a file path string, parses XML once.
- Private helpers: `_str(key)`, `_bool(key)`, `_map(key)`, `_list(key)` — iterate the appropriate `*Attribute` elements.
- Public fields exposed as `@property` accessors, not eagerly extracted.

### Class: `PomHandler`

- Constructor takes a `Path`, parses XML, sniffs namespace from root tag.
- `_q(name)` — qualifies a tag with detected namespace.
- `_find(parent, *path_parts)` — namespace-aware chained find.
- `_findall(parent, tag)` — namespace-aware findall.
- `_get_or_create(parent, tag)` — find or create a child element.
- `artifact_id()` → `str` — read `<artifactId>`.
- `modules()` → `List[str]` — collect all `<module>` entries from both the top-level `<modules>` block **and** from `<profiles><profile><modules>` blocks. Deduplicate by module path, preserve first-seen order. Does **not** print info messages — that is the caller's responsibility.
- `profile_module_counts()` → `List[Tuple[str, int]]` — return `[(profile_id, count), ...]` for profiles that declare `<modules>`. Used for reporting in `main()` only.
- `_plugins_el()` — ensure `<build><plugins>` exists and return it.
- `_find_plugin(plugins, artifact_id)` — locate a plugin by artifact ID.
- `_get_or_create_plugin(plugins, group_id, artifact_id)` — find or create.
- `_set_version(plugin, version)` — set `<version>`, inserting after `<artifactId>` if absent.
- `_set_config(plugin, **kwargs)` — set children of `<configuration>`.
- `save()` — backup, pretty-print (4-space indent), write as UTF-8 bytes with XML declaration.
- `_pretty(el, level)` — recursive in-place indentation.

### Class: `IdeaGenerator`

- Constructor takes all config values + the `EclipseLaunch` instance. Creates `.idea/` and `.idea/runConfigurations/` directories.
- One method per `.idea/` file: `run_config(module_name)`, `misc_xml()`, `compiler_xml()`, `encodings_xml()`, `gitignore()`, `vcs_xml()`, `maven_settings_xml(maven_command)`.
- Each method generates XML/text via string templates (not ElementTree), writes via the guarded write helper, and returns the output `Path`.
- `_translate_workdir(eclipse_dir)` — translate Eclipse working directory variable, reusing the same sub-path-preserving regex patterns as `translate_vm_args()`.
- `_env_block(env_vars)` — generate `<envs>` XML fragment; return `<envs />` if empty.

### Standalone function: `translate_vm_args(raw, project_root, native_dir)`

1. Apply Eclipse variable translation (sub-path-preserving `workspace_loc`, then `project_loc`, then simple patterns).
2. For `java.library.path` and `jna.library.path`: if already present in args, **append** native path with `:` separator; if absent, append as new flag.
3. Return stripped result.

### Standalone function: `fixup_child_poms(module_poms, source, target, encoding)`

Loop each child POM. Check `<properties>`, compiler plugin `<configuration>`, surefire/failsafe. Track `changed` boolean. Save once per POM if changed. Print informational warnings for custom `<outputDirectory>` and JPMS `module-info.java`.

### Standalone function: `purge_eclipse_files(project_root, module_poms)`

Scan project root and every module directory for `.classpath`, `.project`, `.settings/`. Delete files with `Path.unlink()`, directories with `shutil.rmtree()`. Collect relative paths of everything deleted and print a summary. Then call `_update_root_gitignore()`.

### Standalone function: `_update_root_gitignore(project_root)`

Read the root `.gitignore` (or empty string if absent). Check which of `.classpath`, `.project`, `.settings/` are missing. Append a labeled block with only the missing patterns. Do nothing if all three are already present.

### Constant: `ECLIPSE_ARTIFACTS`

```python
ECLIPSE_ARTIFACTS = [".classpath", ".project", ".settings"]
```

### Console helpers

Four one-liner functions for consistent output:
- `_ok(msg)` — prints `✅`
- `_warn(msg)` — prints `⚠️`
- `_info(msg)` — prints `ℹ️`
- `_err(msg)` — prints `❌`

---

## Constants

### `LANG_LEVEL_MAP`

```python
{
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
```

### `ECLIPSE` — attribute key lookup

```python
{
    "main":       "org.eclipse.jdt.launching.MAIN_TYPE",
    "vm_args":    "org.eclipse.jdt.launching.VM_ARGUMENTS",
    "prog_args":  "org.eclipse.jdt.launching.PROGRAM_ARGUMENTS",
    "workdir":    "org.eclipse.jdt.launching.WORKING_DIRECTORY",
    "project":    "org.eclipse.jdt.launching.PROJECT_ATTR",
    "env_vars":   "org.eclipse.debug.core.environmentVariables",
    "stop_main":  "org.eclipse.jdt.launching.STOP_IN_MAIN",
    "vm_install": "org.eclipse.jdt.launching.VM_INSTALL_NAME",
    "classpath":  "org.eclipse.jdt.launching.CLASSPATH",
    "def_cp":     "org.eclipse.jdt.launching.DEFAULT_CLASSPATH",
}
```

### Maven namespace

```python
MAVEN_NS = "http://maven.apache.org/POM/4.0.0"
```

---

## Edge Cases and Pitfalls

1. **POMs without a namespace.** Some projects omit `xmlns` from their `pom.xml`. The namespace sniffing logic must handle both cases. If the root tag has no `{...}` prefix, use bare tag names for all lookups.

2. **Multiple `save()` calls on the same POM.** The backup (`pom.xml.bak`) must only be created once — the first time. Subsequent saves overwrite the POM but not the backup.

3. **`_set_version` insertion position.** When creating a `<version>` element on a new plugin, insert it at index 2 (after `<groupId>` and `<artifactId>`), not appended at the end.

4. **Encoding of POM writes.** POM files are written as **bytes** (`wb` mode) with `encoding="UTF-8"` and `xml_declaration=True`. This is important because `ElementTree.write()` in text mode doesn't produce the XML declaration correctly on all Python versions.

5. **`.idea/` files are written as text** (not XML trees). Use string templates with `textwrap.dedent`. This avoids ElementTree reformatting concerns and keeps the output readable.

6. **XML escaping.** All values injected into XML attribute positions (`value="..."`) must escape `&`, `<`, `>`, `"`.

7. **Interactive prompt resilience.** The launcher module prompt must handle: non-integer input (treat as raw artifactId), out-of-range integers (re-prompt), and the edge case where the POM for a selected module is unparseable (fall back to directory name).

8. **`workspace_loc` with only a project name.** `${workspace_loc:/MyProject}` (no trailing path) must map to `$PROJECT_DIR$`, not `$PROJECT_DIR$/`. The regex's group 2 is optional and may be `None`.

9. **Modules declared only inside profiles.** A parent POM may have zero top-level `<modules>` and all modules scoped under one or more `<profiles><profile><modules>` blocks. The script must still discover them. A module may also appear in both a top-level `<modules>` and a profile — deduplicate by path.

10. **Modules in multiple profiles.** The same module path might appear in two different profiles (e.g. `release` and `ci`). Deduplication ensures it only appears once in the result list.

11. **`--deploy-dir` not set.** The native library copy block must be omitted entirely from the build scripts — not generated with an empty path. The instructions must tell the user to place DLLs manually.

12. **`--deploy-dir` points to a directory that doesn't exist yet.** The build scripts will create it via `mkdir -p` (Unix) / `if not exist ... mkdir` (Windows) at runtime, so this is not an error at migration time. The script does not validate that the deployment directory exists.

---

## Test Scenarios

### Scenario A — Profile-scoped modules with deployment directory

Create a minimal project at `/tmp/test-project/`:

```
test-project/
├── pom.xml                          (parent with <modules> INSIDE a <profile id="release">:
│                                      common, connector, comp, btec)
├── .git/                            (empty dir — triggers vcs.xml generation)
├── common/
│   └── pom.xml
├── connector/
│   └── pom.xml
├── comp/
│   └── pom.xml
├── btec/
│   ├── pom.xml
│   └── src/main/java/com/example/launcher/MainClass.java
├── target/deploy/                   (simulated Maven output with DLLs)
│   ├── mylib.dll
│   └── dependency.dll
└── MyApp.launch                     (workspace_loc with sub-path in VM args)
```

Run with: `--deploy-dir target/deploy`

Expected outcomes:
1. Step 2 reports `Found 4 module(s) inside profile 'release'` — exactly once.
2. Step 3 auto-detects `btec` as the launcher.
3. Step 4 generates `build.sh` and `build.cmd` that contain both the Maven command and a native library copy block that recursively finds `*.dll`/`*.so`/`*.dylib` in `target/deploy/` and copies them to `native/`.
4. With `--dry-run`: zero files are created, modified, or deleted.

### Scenario B — Standard layout with Eclipse cleanup

Create a minimal project at `/tmp/test-project/`:

```
test-project/
├── pom.xml                          (parent with top-level <modules>: module-a, module-launcher)
├── .git/                            (empty dir — triggers vcs.xml generation)
├── .gitignore                       (pre-existing, e.g. "target/\n*.class")
├── .classpath                       (Eclipse workspace file — should be deleted)
├── .project                         (Eclipse workspace file — should be deleted)
├── .settings/                       (Eclipse workspace dir — should be deleted)
│   └── org.eclipse.jdt.core.prefs
├── module-a/
│   ├── pom.xml                      (<properties> with maven.compiler.source=11, target=11)
│   ├── .classpath                   (Eclipse workspace file — should be deleted)
│   ├── .project                     (Eclipse workspace file — should be deleted)
│   ├── .settings/                   (Eclipse workspace dir — should be deleted)
│   │   └── org.eclipse.jdt.core.prefs
│   └── src/main/java/com/example/   (empty)
├── module-launcher/
│   ├── pom.xml                      (no overrides)
│   ├── .classpath                   (Eclipse workspace file — should be deleted)
│   ├── .project                     (Eclipse workspace file — should be deleted)
│   └── src/main/java/com/example/launcher/MainClass.java
└── MyApp.launch                     (workspace_loc with sub-path in VM args)
```

Run without `--deploy-dir`.

Expected outcomes:
1. Step 2 finds modules at the top level (no profile message).
2. Step 3 auto-detects `module-launcher` as the launcher.
3. Step 5 injects all properties and plugins into parent POM.
4. Step 6 detects `module-a`'s `<properties>` override (`11` → `{source}`) and corrects it.
5. Step 7 deletes all `.classpath`, `.project`, and `.settings/` files/directories from the root and both modules (8 artifacts total). Appends `.classpath`, `.project`, `.settings/` to the root `.gitignore`.
6. Step 8 generates all `.idea/` files. The run config's `VM_PARAMETERS` has the sub-path preserved in any translated `workspace_loc` variables. Both `-Djava.library.path` and `-Djna.library.path` point to `$PROJECT_DIR$/native`.
7. `.idea/.gitignore` whitelists `!vcs.xml` and `!maven.xml`.
8. `build.sh` and `build.cmd` contain only the Maven command (no native copy block, since `--deploy-dir` was not set).
9. With `--dry-run`: zero files are created, modified, or deleted. `[DRY-RUN] Would delete` messages appear for each Eclipse artifact. All Eclipse files remain on disk afterward.
