; Compile a verified, complete PyInstaller bundle; no hardware drivers are bundled.
; ISCC /DAppVersion=1.2.3 /DBundleDir=C:\build\dist\Transport /DOutputDir=C:\build\dist /DManifestFile=C:\build\manifest.json tools\Transport.iss
; Require the release version explicitly rather than embedding a stale date/version.
#ifndef AppVersion
  #error AppVersion must be supplied with /DAppVersion=...
#endif
#ifndef BundleDir
  #define BundleDir "..\dist\Transport"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef ManifestFile
  #error ManifestFile must be supplied with /DManifestFile=...
#endif
#if !FileExists(ManifestFile)
  #error ManifestFile must refer to the generated bundle file manifest
#endif
#if !FileExists(BundleDir + "\Transport.exe")
  #error BundleDir must contain Transport.exe
#endif
#if !FileExists(BundleDir + "\TransportWorker.exe")
  #error BundleDir must contain TransportWorker.exe
#endif

[Setup]
AppId={code:InstallAppId}
AppName=Transport Measurement
AppVersion={#AppVersion}
AppPublisher=Transport Measurement
AppPublisherURL=https://github.com/ylylyl98/Transport_Pyside6
DefaultDirName={localappdata}\Programs\Transport Measurement
DefaultGroupName={code:InstallGroupName}
UsePreviousGroup=no
UsePreviousLanguage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Transport-Setup-windows-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\transport.ico
UninstallDisplayIcon={app}\Transport.exe
CloseApplications=no
RestartApplications=no
RestartIfNeededByRun=no
UninstallLogMode=append
ChangesEnvironment=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ManifestFile}"; DestDir: "{app}"; DestName: "installer-manifest.json"; Flags: ignoreversion

[Icons]
Name: "{group}\Transport Measurement"; Filename: "{app}\Transport.exe"; WorkingDir: "{app}"
Name: "{group}\Uninstall Transport Measurement"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{code:InstallGroupName}"; Filename: "{app}\Transport.exe"; WorkingDir: "{app}"; Tasks: desktopicon

; Deliberately no [Run], [InstallDelete], or [UninstallDelete]: never launch
; measurement automatically or recursively delete settings/data/unknown files.
; Upgrades overlay the existing bundle and append the uninstall history. Files
; retired by later releases remain until uninstall; unknown files are retained.
[Code]
function SmokeToken: String;
var
  I: Integer;
begin
  { Verification must never share a real installation's registry/shortcuts. }
  Result := ExpandConstant('{param:SMOKETEST|}');
  if Result = '' then
    exit;
  if Length(Result) <> 32 then
    RaiseException('SMOKETEST must be a 32-character hexadecimal isolation token.');
  for I := 1 to Length(Result) do
    if Pos(Lowercase(Copy(Result, I, 1)), '0123456789abcdef') = 0 then
      RaiseException('SMOKETEST must be a 32-character hexadecimal isolation token.');
end;

function InstallAppId(Param: String): String;
begin
  Result := '{B60282B6-C896-4950-9670-97801409F229}';
  if SmokeToken <> '' then
    { Keep below Inno's key-length limit so the verification token is retained. }
    Result := 'TransportSmoke-' + SmokeToken;
end;

function InstallGroupName(Param: String): String;
begin
  Result := 'Transport Measurement';
  if SmokeToken <> '' then
    Result := Result + ' Smoke ' + SmokeToken;
end;

function ProcessSafetyError: String;
var
  Locator, Services, Processes: Variant;
begin
  Result := '';
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Services := Locator.ConnectServer('', 'root\CIMV2');
    { Query all sessions and locations, including portable copies. Do not ask
      Restart Manager or WMI to terminate or close anything. }
    Processes := Services.ExecQuery(
      'SELECT ProcessId FROM Win32_Process WHERE Name = ''Transport.exe'' OR Name = ''TransportWorker.exe''');
    if Processes.Count > 0 then
      Result := 'Transport.exe or TransportWorker.exe is running. Close Transport normally after safely stopping measurements, then retry.';
  except
    { Inability to inspect processes is not evidence that instruments are idle. }
    Log('Process safety check failed: ' + GetExceptionMessage);
    Result := 'Cannot verify whether Transport is running (Windows WMI is unavailable). Installation or removal has been blocked. Restore WMI and retry.';
  end;
end;

function InitializeSetup: Boolean;
var
  Error: String;
begin
  Error := ProcessSafetyError;
  Result := Error = '';
  if not Result then
  begin
    Log(Error);
    if not WizardSilent then
      MsgBox(Error, mbError, MB_OK);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { Runs in silent mode too, immediately before any installation changes. }
  Result := ProcessSafetyError;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Error: String;
begin
  if CurStep = ssInstall then
  begin
    Error := ProcessSafetyError;
    if Error <> '' then
      RaiseException(Error);
  end;
end;

function InitializeUninstall: Boolean;
var
  Error: String;
begin
  Error := ProcessSafetyError;
  Result := Error = '';
  if not Result then
  begin
    Log(Error);
    if not UninstallSilent then
      MsgBox(Error, mbError, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Error: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Error := ProcessSafetyError;
    if Error <> '' then
      RaiseException(Error);
  end;
end;
