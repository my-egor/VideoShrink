Unicode true
ManifestDPIAware true
RequestExecutionLevel user

!include "MUI2.nsh"

!define APP_NAME "VideoShrink"
!define APP_VERSION "1.1.6"
!define APP_PUBLISHER "VideoShrink"
!define APP_UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\VideoShrink"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "dist\VideoShrink_Setup_1.1.6.exe"
InstallDir "$LOCALAPPDATA\Programs\VideoShrink"
InstallDirRegKey HKCU "Software\VideoShrink" "InstallDir"
ShowInstDetails show
ShowUnInstDetails show
SetCompressor /FINAL zlib

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "VideoShrink" SEC_APP
  SetOutPath "$INSTDIR"
  File /r "dist\VideoShrink\*.*"

  FileOpen $0 "$INSTDIR\installed.json" w
  FileWrite $0 "{}"
  FileClose $0

  WriteRegStr HKCU "Software\VideoShrink" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${APP_UNINSTALL_KEY}" "DisplayName" "${APP_NAME} ${APP_VERSION}"
  WriteRegStr HKCU "${APP_UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_UNINSTALL_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${APP_UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\VideoShrink"
  CreateShortcut "$SMPROGRAMS\VideoShrink\VideoShrink.lnk" "$INSTDIR\VideoShrink.exe"
  CreateShortcut "$SMPROGRAMS\VideoShrink\Папка для видео.lnk" "$INSTDIR\VideoShrink.exe" "--open-input"
  CreateShortcut "$DESKTOP\VideoShrink.lnk" "$INSTDIR\VideoShrink.exe"
  CreateShortcut "$DESKTOP\Папка для видео.lnk" "$INSTDIR\VideoShrink.exe" "--open-input"
SectionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\VideoShrink\VideoShrink.lnk"
  Delete "$SMPROGRAMS\VideoShrink\Папка для видео.lnk"
  RMDir "$SMPROGRAMS\VideoShrink"
  Delete "$DESKTOP\VideoShrink.lnk"
  Delete "$DESKTOP\Папка для видео.lnk"
  DeleteRegKey HKCU "${APP_UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\VideoShrink"
  RMDir /r "$INSTDIR"
SectionEnd
