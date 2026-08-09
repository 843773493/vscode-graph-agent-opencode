Unicode true

!ifndef VERSION
  !define VERSION "0.1.0"
!endif
!ifndef INPUTDIR
  !error "INPUTDIR is required"
!endif
!ifndef OUTPUT
  !error "OUTPUT is required"
!endif

!include "MUI2.nsh"

Name "BoxTeam"
OutFile "${OUTPUT}"
InstallDir "$PROGRAMFILES64\BoxTeam"
InstallDirRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "InstallLocation"
RequestExecutionLevel admin
ShowInstDetails show
ShowUninstDetails show

Function .onInit
  SetRegView 64
FunctionEnd

Function un.onInit
  SetRegView 64
FunctionEnd

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "BoxTeam"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileDescription" "BoxTeam Windows Installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "BoxTeam"

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "BoxTeam" MainSection
  SetShellVarContext all
  SetOutPath "$INSTDIR"
  File /r "${INPUTDIR}/*"

  CreateDirectory "$SMPROGRAMS\BoxTeam"
  CreateShortCut "$SMPROGRAMS\BoxTeam\BoxTeam.lnk" "$INSTDIR\BoxTeam.exe"
  CreateShortCut "$SMPROGRAMS\BoxTeam\BoxTeam Doctor.lnk" "$INSTDIR\BoxTeamDoctor.exe" "--json"
  CreateShortCut "$DESKTOP\BoxTeam.lnk" "$INSTDIR\BoxTeam.exe"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "DisplayName" "BoxTeam"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "NoModify" 1
  WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext all
  Delete "$DESKTOP\BoxTeam.lnk"
  Delete "$SMPROGRAMS\BoxTeam\BoxTeam.lnk"
  Delete "$SMPROGRAMS\BoxTeam\BoxTeam Doctor.lnk"
  RMDir "$SMPROGRAMS\BoxTeam"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam"
  RMDir /r "$INSTDIR"
SectionEnd
