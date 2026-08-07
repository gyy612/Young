; Inno Setup 6 script - wrap the portable EXE into an installer.
; File must be compiled with iscc on Windows.

#define MyAppName "ismolar Interpreter"
#define MyAppVersion "1.9.9"
#define MyAppExeName "ismolar-interpreter.exe"

[Setup]
AppId={{3F5C9B2E-7A1D-4E6A-9B2C-8D1E5F0A3C47}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=ismolar
DefaultDirName={autopf}\ismolar Interpreter
DefaultGroupName=ismolar Interpreter
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ismolar-setup-{#MyAppVersion}-win64
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\app_icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "..\dist\ismolar-interpreter.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\app_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\ismolar Interpreter"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"
Name: "{autodesktop}\ismolar Interpreter"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\app_icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch ismolar Interpreter"; Flags: nowait postinstall skipifsilent
