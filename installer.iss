#define AppName "VideoShrink"
#define AppVersion "1.1.6"
#define AppPublisher "VideoShrink"
#define BuildDir "dist\VideoShrink"

[Setup]
AppId={{5A60CE1D-59B4-4C78-8891-3D0D0E8E9A02}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\VideoShrink
DefaultGroupName=VideoShrink
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=VideoShrink_Setup_1.1.6
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\VideoShrink.exe

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autodesktop}\VideoShrink"; Filename: "{app}\VideoShrink.exe"
Name: "{autodesktop}\Папка для видео"; Filename: "{app}\VideoShrink.exe"; Parameters: "--open-input"; IconFilename: "{sys}\shell32.dll"; IconIndex: 3
Name: "{group}\VideoShrink"; Filename: "{app}\VideoShrink.exe"
Name: "{group}\Папка для видео"; Filename: "{app}\VideoShrink.exe"; Parameters: "--open-input"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  Marker: String;
begin
  if CurStep = ssPostInstall then
  begin
    Marker := ExpandConstant('{app}\installed.json');
    SaveStringToFile(Marker, '{}', False);
  end;
end;

[Run]
Filename: "{app}\VideoShrink.exe"; Description: "Запустить VideoShrink"; Flags: postinstall nowait skipifsilent
