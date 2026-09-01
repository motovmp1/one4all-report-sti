# One4All Results Viewer — V1

A PySide6 desktop application for reviewing One4All XML test results.

Branded as **TRIDONIC — WE MANAGE LIGHT**, Version 1.6, powered by PT Team.

## Tools used and tested

The installer in this repository was built on Windows 11 x64 with:

- PowerShell 5.1 or newer;
- Python 3.14.6 x64;
- PySide6 6.11.1, installed from `requirements.txt`;
- PyInstaller 6.21.0, pinned in `packaging\requirements-build.txt`;
- Inno Setup 6.7.3, installed for the current Windows user;
- Windows Package Manager (`winget`) to install Python and Inno Setup.

Only the developer/build computer needs these tools. Colleagues who receive the
final Setup `.exe` do not need Python, pip, PySide6, PyInstaller, or Inno Setup.

### Changes made on the current build computer

During creation of the Version 1.6 installer:

- the existing Python 3.14.6 and project `.venv` were used;
- PyInstaller 6.21.0 and its build dependencies were installed inside `.venv`
  with `pip`;
- Inno Setup was installed or upgraded to 6.7.3 for the current Windows user
  with `winget`;
- `assets`, `build`, `dist`, and `release` build outputs were generated inside
  this project;
- no Python or pip installation is performed by the final end-user installer.

## Prepare a new build computer

Open PowerShell in the project directory. These commands perform the complete
one-time setup on a new Windows 11 computer.

### 1. Install Python 3.14 x64

```powershell
winget install --id Python.Python.3.14 -e --scope user
```

Close and reopen PowerShell, then confirm:

```powershell
py -3.14 --version
```

### 2. Create the project virtual environment

```powershell
cd "C:\path\to\one4all_report_sti_2gen"
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Activation is optional because all project commands can call the `.venv` Python
directly. If PowerShell allows script activation, use:

```powershell
.\.venv\Scripts\Activate.ps1
```

If activation is blocked, allow it only for the current PowerShell window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 3. Install Inno Setup on the build computer

```powershell
winget install --id JRSoftware.InnoSetup -e -s winget --scope user --accept-package-agreements --accept-source-agreements
```

Inno Setup creates the final installer wizard, shortcuts, and uninstaller. It is
not installed on end-user computers.

## Run from source

```powershell
.\.venv\Scripts\python.exe main.py
```

You can also launch the application by double-clicking `executar.bat`.

At startup, the application automatically opens the `ST-I_ Results` folder and
the `test_scope.xml` file next to `main.py` when available.

## Windows 11 installer

Share this file with users who do not have Python installed:

```text
release\Tridonic-One4All-Viewer-Setup-1.6.exe
```

The installer contains Python, PySide6, and all runtime dependencies. It installs
the application for the current Windows user, creates a Start Menu shortcut,
offers an optional desktop shortcut, and registers an uninstaller. Test results
and `test_scope.xml` are intentionally not bundled because users must select the
matching files from their own test campaign.

The current installer is not digitally signed, so Windows SmartScreen may show
an **Unknown publisher** warning. A company code-signing certificate is required
to remove that warning.

## Rebuild the Windows installer

From the project directory, run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_installer.ps1
```

The build script performs these steps automatically:

1. installs the pinned PyInstaller version into `.venv` when required;
2. generates `assets\app_icon.ico`;
3. packages Python, PySide6, and the application into `dist\One4AllViewer`;
4. asks Inno Setup to create the final Setup executable.

The finished installer is written to:

```text
release\Tridonic-One4All-Viewer-Setup-1.6.exe
```

Confirm that it exists and calculate its checksum with:

```powershell
Get-Item .\release\Tridonic-One4All-Viewer-Setup-1.6.exe
Get-FileHash .\release\Tridonic-One4All-Viewer-Setup-1.6.exe -Algorithm SHA256
```

To change the public version number, update it consistently in:

- `main.py` for the window and status-bar text;
- `packaging\version_info.txt` for Windows executable properties;
- `packaging\installer.iss` for the installer version and output filename.

Then run `build_installer.ps1` again. The script rebuilds the application and
installer; no manual PyInstaller or Inno Setup command is required.

## Build troubleshooting

- **`.venv was not found`**: run the virtual-environment commands from the
  preparation section.
- **`Inno Setup 6 was not found`**: run the `winget install` command from step 3,
  then reopen PowerShell.
- **PowerShell script execution is disabled**: use the full
  `powershell -NoProfile -ExecutionPolicy Bypass -File ...` build command above.
- **PyInstaller cannot download packages**: verify internet/proxy access on the
  build computer. End-user computers do not download packages.
- **Windows shows Unknown publisher**: the installer is not digitally signed.
  A company code-signing certificate is needed to remove that warning.

## V1 features

- two independent result modes: open one XML file or load an entire folder;
- recursive, dynamic XML discovery in folder mode;
- native folder-only selection avoids enumerating result files while browsing network drives;
- two-phase XML loading: lightweight summaries populate the dashboard first,
  while complete step details are parsed in the background only when opened;
- overview summaries read only XML metadata boundaries, use up to four concurrent
  reads, and cache unchanged files for faster network refreshes;
- completed scans remain visible when filesystem changes queue another refresh;
- Passed, Failed, Skipped, Aborted, Draft, and Unknown states;
- combined search, status, and dynamic Function Block number filters;
- Function Block choices are generated only from the result IDs currently loaded;
- a scope-results toggle switches between the legacy-compatible view (only IDs
  selected in `test_scope.xml`) and every XML currently present in Results;
- static, aligned **QA member** and **QA comment** columns in the overview table;
- QA assignments and comments are read from and saved to `Relationships.xml`
  beside the selected scope, using the legacy `Tester` and `Comment` attributes;
- existing legacy QA values populate the member selector dynamically, and new
  values are persisted when assigned to a test;
- `One4All_QA_data.xml` and `Test_report_data.xml` are not read, created, or modified;
- result-step comments remain only in the detailed Steps table and are never
  copied into the initial overview;
- inside a test's detail **Overview**, QA member and QA comment each have their own
  nearby **Edit**, **Save**, **Cancel**, and **Remove** actions; clicking the displayed
  member or comment also opens its editor, and QA editing provides **+ Add QA**;
- generated meeting reports list the QA members assigned to the latest scoped results
  in the **Configuration / Test environment** panel;
- mapped network drives such as `Z:` and UNC paths show a persistent English banner
  with indeterminate discovery and determinate XML-reading progress;
- scope/QA loading, result/detail scanning, QA writes, and PDF input reads run outside
  the UI thread; failed network operations offer **Retry** after checking VPN access;
- a one-second pulsing active-filter warning with one-click filter clearing;
- automatic totals and percentages;
- responsive Distribution panel for standard and high-DPI Windows displays,
  including 4K screens at 250% scaling;
- comparison against the exact tests selected in Test Manager's `test_scope.xml`;
- an in-app **Scope help** notice explains that the scope and results must belong
  to the same test campaign/project directory to keep statistics valid;
- duplicated variants in the scope XML are consolidated by unique test ID;
- 50 stable Test Manager groups remain visible at all times;
- groups without selected scope tests remain disabled in gray;
- the active scope can be unloaded with **Clear scope**;
- result and scope paths are hidden by default behind a **Show paths** toggle;
- active groups show their selected-test count and Passed percentage;
- yellow hover summaries show exact Passed, Failed, Skipped, Aborted, Draft,
  Unknown, Completed, and Missing counts for every group and for the full scope;
- compact static progress panel with no scroll bars;
- result state detected from the XML filename suffix;
- the latest execution determines the state when an ID has several runs;
- test details open in closable tabs within the same window;
- full step-by-step execution table with PASS/FAIL row highlighting;
- responsive step columns, multiline messages, and full-value tooltips;
- UTF-8 and legacy Windows-1252 XML decoding without replacement characters;
- overview, all steps, failed evaluations, and original XML views;
- automatic folder monitoring and manual refresh.
- immediate window display followed by deferred background XML loading;
- file-by-file loading progress for folders and single XML files.
