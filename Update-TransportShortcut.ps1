<#
Create/repair the project shortcut without launching Transport or changing pins.
Run Transport_App.bat first to install the project environment.
#>
[CmdletBinding()]
param(
    [string]$ShortcutPath = (Join-Path $PSScriptRoot 'Transport_App - Shortcut.lnk')
)

$ErrorActionPreference = 'Stop'
$transportPython = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$transportEntry = Join-Path $PSScriptRoot 'transport_UI.py'
$transportIcon = Join-Path $PSScriptRoot 'assets\transport.ico'
# This identity must match app/app_identity.py for Windows taskbar grouping.
$transportAppId = 'MyLab.TransportMeasurement'
$ShortcutPath = [System.IO.Path]::GetFullPath($ShortcutPath)

foreach ($requiredPath in @($transportPython, $transportEntry, $transportIcon)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Missing $requiredPath. Run Transport_App.bat to set up the project first."
    }
}
if ([System.IO.Path]::GetExtension($ShortcutPath) -ne '.lnk') {
    throw 'ShortcutPath must end in .lnk.'
}

$transportShell = New-Object -ComObject WScript.Shell
$transportLink = $transportShell.CreateShortcut($ShortcutPath)
$transportArguments = '"' + $transportEntry + '"'
$transportIconLocation = $transportIcon + ',0'
$transportExplorer = New-Object -ComObject Shell.Application
if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
    $existingFolder = $transportExplorer.Namespace([System.IO.Path]::GetDirectoryName($ShortcutPath))
    $existingItem = $existingFolder.ParseName([System.IO.Path]::GetFileName($ShortcutPath))
    if ($transportLink.TargetPath -eq $transportPython -and
        $transportLink.Arguments -eq $transportArguments -and
        $transportLink.WorkingDirectory -eq $PSScriptRoot -and
        $transportLink.IconLocation -eq $transportIconLocation -and
        $existingItem.ExtendedProperty('System.AppUserModel.ID') -eq $transportAppId) {
        Write-Output "Transport shortcut is already configured: $ShortcutPath"
        return
    }
}

# WScript.Shell handles ordinary link fields; the Shell property store handles
# AppUserModelID. No extra Python package, registry edits or administrator rights.
if (-not ('TransportShortcutInterop.Identity' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace TransportShortcutInterop {
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey {
        public Guid FormatId;
        public uint Id;
    }

    // PROPVARIANT has an 8-byte header and a union of two pointer-sized fields.
    [StructLayout(LayoutKind.Sequential)]
    public struct PropVariant {
        public ushort Type, Reserved1, Reserved2, Reserved3;
        public IntPtr Pointer, Padding;
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    public static class Identity {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHGetPropertyStoreFromParsingName(
            string path, IntPtr bindContext, uint flags, ref Guid iid,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

        public static void Set(string path, string appId) {
            IPropertyStore store = null;
            PropVariant value = new PropVariant();
            try {
                Guid iid = typeof(IPropertyStore).GUID;
                // GPS_READWRITE = 2
                SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store);
                PropertyKey key = new PropertyKey {
                    FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5
                };
                value.Type = 31; // VT_LPWSTR
                value.Pointer = Marshal.StringToCoTaskMemUni(appId);
                store.SetValue(ref key, ref value);
                store.Commit();
            } finally {
                if (value.Pointer != IntPtr.Zero) Marshal.FreeCoTaskMem(value.Pointer);
                if (store != null) Marshal.ReleaseComObject(store);
            }
        }
    }
}
'@
}

$transportLink.TargetPath = $transportPython
$transportLink.Arguments = $transportArguments
$transportLink.WorkingDirectory = $PSScriptRoot
$transportLink.IconLocation = $transportIconLocation
$transportLink.Description = 'Transport Measurement'
$transportLink.Save()
[TransportShortcutInterop.Identity]::Set($ShortcutPath, $transportAppId)
Write-Output "Updated Transport shortcut: $ShortcutPath"
