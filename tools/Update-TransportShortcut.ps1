<#
Create/repair the project shortcut without launching Transport or changing pins.
No Python installation is required to create the shortcut.
#>
[CmdletBinding()]
param(
    [string]$ShortcutPath
)

$ErrorActionPreference = 'Stop'
$transportRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd([char[]]@('\', '/'))
$hashInput = $transportRoot.ToLowerInvariant()
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($hashInput))
} finally { $sha.Dispose() }
$rootHash = ([BitConverter]::ToString($hashBytes)).Replace('-', '').ToLowerInvariant().Substring(0, 16)
if (-not $ShortcutPath) {
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    if (-not $desktop) { throw 'Could not find the current user desktop directory.' }
    $ShortcutPath = Join-Path $desktop ("Transport Measurement (Source - " + $rootHash.Substring(0, 8) + ").lnk")
}
$transportLauncher = Join-Path $transportRoot 'tools\Start-Transport.ps1'
$transportIcon = Join-Path $transportRoot 'assets\transport.ico'
$powerShell = Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe'
$transportAppId = 'MyLab.TransportMeasurement.Source.' + $rootHash
$ShortcutPath = [System.IO.Path]::GetFullPath($ShortcutPath)
foreach ($requiredPath in @($transportLauncher, $transportIcon, $powerShell)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) { throw "Missing $requiredPath." }
}
if ([System.IO.Path]::GetExtension($ShortcutPath) -ine '.lnk') {
    throw 'ShortcutPath must end in .lnk.'
}
$directory = [System.IO.Path]::GetDirectoryName($ShortcutPath)
if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
    [System.IO.Directory]::CreateDirectory($directory) | Out-Null
}
$transportArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $transportLauncher + '"'
$transportIconLocation = $transportIcon + ',0'
$relaunchCommand = '"' + $powerShell + '" ' + $transportArguments
$displayName = 'Transport Measurement (Source)'
# IShellLinkW preserves Unicode paths; the Shell property store holds relaunch
# properties and AppUserModelID. No Python, registry edits or elevation needed.
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

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    public class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    public interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder path, int count, IntPtr data, uint flags);
        void GetIDList(out IntPtr list);
        void SetIDList(IntPtr list);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder value, int count);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder value, int count);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder value, int count);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string value);
        void GetHotkey(out short value);
        void SetHotkey(short value);
        void GetShowCmd(out int value);
        void SetShowCmd(int value);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder value, int count, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string value, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public static class Identity {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHGetPropertyStoreFromParsingName(
            string path, IntPtr bindContext, uint flags, ref Guid iid,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

        public static string[] ReadLink(string path) {
            object obj = new ShellLink();
            try {
                ((System.Runtime.InteropServices.ComTypes.IPersistFile)obj).Load(path, 0);
                IShellLinkW link = (IShellLinkW)obj;
                var target = new System.Text.StringBuilder(32768);
                var args = new System.Text.StringBuilder(32768);
                var work = new System.Text.StringBuilder(32768);
                var icon = new System.Text.StringBuilder(32768);
                var description = new System.Text.StringBuilder(32768);
                int iconIndex;
                link.GetPath(target, target.Capacity, IntPtr.Zero, 4);
                link.GetArguments(args, args.Capacity);
                link.GetWorkingDirectory(work, work.Capacity);
                link.GetIconLocation(icon, icon.Capacity, out iconIndex);
                link.GetDescription(description, description.Capacity);
                return new string[] { target.ToString(), args.ToString(), work.ToString(),
                    icon.ToString() + "," + iconIndex, description.ToString() };
            } finally { Marshal.ReleaseComObject(obj); }
        }
        public static void WriteLink(string path, string target, string args, string work, string icon, string description) {
            object obj = new ShellLink();
            try {
                IShellLinkW link = (IShellLinkW)obj;
                link.SetPath(target);
                link.SetArguments(args);
                link.SetWorkingDirectory(work);
                link.SetIconLocation(icon, 0);
                link.SetDescription(description);
                ((System.Runtime.InteropServices.ComTypes.IPersistFile)obj).Save(path, true);
            } finally { Marshal.ReleaseComObject(obj); }
        }

        public static void Set(string path, string appId, string command, string icon, string display) {
            IPropertyStore store = null;
            PropVariant value = new PropVariant();
            try {
                Guid iid = typeof(IPropertyStore).GUID;
                // GPS_READWRITE = 2
                SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store);
                string[] values = { command, icon, display, appId };
                uint[] ids = { 2, 3, 4, 5 };
                for (int i = 0; i < ids.Length; i++) {
                    PropertyKey key = new PropertyKey {
                        FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = ids[i]
                    };
                    value.Type = 31; // VT_LPWSTR
                    value.Pointer = Marshal.StringToCoTaskMemUni(values[i]);
                    try { store.SetValue(ref key, ref value); }
                    finally { Marshal.FreeCoTaskMem(value.Pointer); value.Pointer = IntPtr.Zero; }
                }
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

$transportExplorer = New-Object -ComObject Shell.Application
if (Test-Path -LiteralPath $ShortcutPath -PathType Leaf) {
    $transportLink = [TransportShortcutInterop.Identity]::ReadLink($ShortcutPath)
    $existingFolder = $transportExplorer.Namespace($directory)
    $existingItem = $existingFolder.ParseName([System.IO.Path]::GetFileName($ShortcutPath))
    if ($transportLink[0] -eq $powerShell -and
        $transportLink[1] -eq $transportArguments -and
        $transportLink[2] -eq $transportRoot -and
        $transportLink[3] -eq $transportIconLocation -and
        $transportLink[4] -eq $displayName -and
        $existingItem.ExtendedProperty('System.AppUserModel.ID') -eq $transportAppId -and
        $existingItem.ExtendedProperty('System.AppUserModel.RelaunchCommand') -eq $relaunchCommand -and
        $existingItem.ExtendedProperty('System.AppUserModel.RelaunchIconResource') -eq $transportIconLocation -and
        $existingItem.ExtendedProperty('System.AppUserModel.RelaunchDisplayNameResource') -eq $displayName) {
        Write-Output "Transport shortcut is already configured: $ShortcutPath"
        return
    }
}

[TransportShortcutInterop.Identity]::WriteLink($ShortcutPath, $powerShell, $transportArguments, $transportRoot, $transportIcon, $displayName)
[TransportShortcutInterop.Identity]::Set($ShortcutPath, $transportAppId, $relaunchCommand, $transportIconLocation, $displayName)
Write-Output "Updated Transport shortcut: $ShortcutPath"
