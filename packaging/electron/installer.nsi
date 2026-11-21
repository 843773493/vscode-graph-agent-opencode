Unicode true
!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "FileFunc.nsh"

Name "BoxTeam Desktop"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Programs\BoxTeam Desktop"
InstallDirRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "BoxTeam Desktop"
VIAddVersionKey "FileDescription" "BoxTeam Desktop Installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "BoxTeam"

Var Gateway
Var GatewayInput

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom GatewayPage GatewayLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  ${GetParameters} $0
  ${GetOptions} $0 "/GATEWAY=" $Gateway
  IfSilent 0 done
  StrCmp $Gateway "" 0 done
    SetErrorLevel 2
    Quit
  done:
FunctionEnd

Function GatewayPage
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 30u "请输入已有 Workspace Gateway 的 HTTP(S) 地址。此安装包仅包含桌面客户端，不启动 Gateway 或工作区后端。"
  Pop $0
  ${NSD_CreateText} 0 40u 100% 14u "$Gateway"
  Pop $GatewayInput
  nsDialogs::Show
FunctionEnd

Function GatewayLeave
  ${NSD_GetText} $GatewayInput $Gateway
  StrCmp $Gateway "" 0 done
    MessageBox MB_ICONSTOP "请填写 Gateway 地址。"
    Abort
  done:
FunctionEnd

Section "BoxTeam Desktop"
  SetShellVarContext current
  SetOutPath "$INSTDIR"
  File /r "${INPUTDIR}\*"
  CreateDirectory "$SMPROGRAMS\BoxTeam Desktop"
  CreateShortCut "$SMPROGRAMS\BoxTeam Desktop\BoxTeam.lnk" "$INSTDIR\boxteam.exe" '--gateway="$Gateway"'
  CreateShortCut "$DESKTOP\BoxTeam Desktop.lnk" "$INSTDIR\boxteam.exe" '--gateway="$Gateway"'
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "DisplayName" "BoxTeam Desktop"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop" "NoRepair" 1
SectionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\BoxTeam Desktop.lnk"
  Delete "$SMPROGRAMS\BoxTeam Desktop\BoxTeam.lnk"
  RMDir "$SMPROGRAMS\BoxTeam Desktop"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\BoxTeamDesktop"
  RMDir /r "$INSTDIR"
SectionEnd
