Unicode true

!ifndef VERSION
  !error "VERSION is required"
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
  SetShellVarContext all
  SetRegView 64
  ReadRegStr $INSTDIR HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeam" "InstallLocation"
  StrCmp $INSTDIR "" 0 +2
    Abort
FunctionEnd

Function un.RemoveRuntimeTree
  Exch $0
  Push $1
  Push $2
  FindFirst $1 $2 "$0\*"
  loop:
    StrCmp $2 "" done
    StrCmp $2 "." next
    StrCmp $2 ".." next
    IfFileExists "$0\$2\*" 0 file
      Push "$0\$2"
      Call un.RemoveRuntimeTree
      RMDir "$0\$2"
      Goto next
    file:
      SetFileAttributes "$0\$2" NORMAL
      Delete "$0\$2"
    next:
      FindNext $1 $2
      Goto loop
  done:
  FindClose $1
  Pop $2
  Pop $1
  Pop $0
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
  File /r "${INPUTDIR}\*"

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
  Push "$INSTDIR\launcher"
  Call un.RemoveRuntimeTree
  RMDir "$INSTDIR\launcher"
  Push "$INSTDIR\runtime"
  Call un.RemoveRuntimeTree
  RMDir "$INSTDIR\runtime"
  Delete "$INSTDIR\boxteam-doctor.cmd"
  Delete "$INSTDIR\boxteam.cmd"
  Delete "$INSTDIR\BoxTeam.exe"
  Delete "$INSTDIR\BoxTeamDoctor.exe"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
