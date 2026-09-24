#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=x.y.z
#endif
#ifndef SourceDir
  #error SourceDir must be supplied with /DSourceDir=path
#endif
#ifndef OutputDir
  #error OutputDir must be supplied with /DOutputDir=path
#endif
#ifndef TargetArch
  #error TargetArch must be supplied with /DTargetArch=x64 or arm64
#endif
#ifndef IconFile
  #error IconFile must be supplied
#endif
#ifndef LicenseFile
  #error LicenseFile must be supplied
#endif

[Setup]
AppId={{3F5E6BD9-95D5-5E4F-9C3A-DF93D4314D07}
AppName=Argus
AppVersion={#AppVersion}
AppPublisher=Varun S V
AppPublisherURL=https://github.com/Varun-SV/Argus
AppSupportURL=https://github.com/Varun-SV/Argus/issues
DefaultDirName={autopf}\Argus
DefaultGroupName=Argus
DisableProgramGroupPage=yes
LicenseFile={#LicenseFile}
OutputDir={#OutputDir}
OutputBaseFilename=Argus-{#AppVersion}-windows-{#TargetArch}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
UninstallDisplayIcon={app}\Argus\Argus.exe
SetupIconFile={#IconFile}
#if TargetArch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
#else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Argus"; Filename: "{app}\Argus\Argus.exe"
Name: "{autodesktop}\Argus"; Filename: "{app}\Argus\Argus.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Argus\Argus.exe"; Description: "Launch Argus"; Flags: nowait postinstall skipifsilent
