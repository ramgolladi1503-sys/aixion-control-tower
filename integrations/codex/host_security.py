"""Host-specific file and credential security for the Codex adapter.

Windows validation is intentionally fail-closed: only an explicit DACL for
the current user, LocalSystem, and the privileged Builtin Administrators
group is accepted. POSIX keeps the existing
owner/mode-bit policy. This module uses only the Python standard library.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO


class HostSecurityError(Exception):
    """A file, ACL, reparse point, or credential could not be trusted."""


def default_config_path(home: Path) -> Path:
    return workspace_config_dir(home).parent / "codex.json"


def workspace_config_dir(home: Path) -> Path:
    if os.name == "nt":
        if home.resolve() != Path.home().resolve():
            root = home / "AppData" / "Local"
        else:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise HostSecurityError("Windows LOCALAPPDATA is unavailable")
            root = Path(local_app_data)
        return root / "Aixion" / "ControlTower" / "Codex" / "workspaces"
    return home / ".config" / "aixion" / "workspaces"


def _is_reparse(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def ensure_no_reparse_path(path: Path) -> None:
    """Reject symlinks/reparse points in every existing path component."""
    if os.name != "nt":
        current = path
        while True:
            try:
                if _is_reparse(current.lstat()):
                    raise HostSecurityError(f"Path contains a symlink: {current}")
            except FileNotFoundError:
                pass
            if current == current.parent:
                return
            current = current.parent

    absolute = Path(os.path.abspath(path))
    if absolute.anchor.startswith("\\\\"):
        raise HostSecurityError("UNC paths are not supported for protected Codex files")
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(metadata):
            raise HostSecurityError(f"Path contains a Windows reparse point: {current}")


def open_private_file(path: Path, *, require_private: bool,
                      reject_shared_write: bool) -> BinaryIO:
    """Open one regular file without following links and validate its owner/ACL."""
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise HostSecurityError(f"File must be a regular file owned by this user: {path}")
            mode = stat.S_IMODE(metadata.st_mode)
            if require_private and mode & 0o077:
                raise HostSecurityError(f"File must exclude group and other users: {path}")
            if reject_shared_write and mode & 0o022:
                raise HostSecurityError(f"File must not be group- or other-writable: {path}")
            return os.fdopen(descriptor, "rb")
        except Exception:
            if "descriptor" in locals():
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise

    return _windows_open_private_file(
        path, require_private=require_private, reject_shared_write=reject_shared_write
    )


def validate_directory(path: Path, *, reject_shared_write: bool = True) -> None:
    """Require a real user-owned directory with a trusted host ACL."""
    ensure_no_reparse_path(path)
    if os.name != "nt":
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise HostSecurityError(f"Directory must be owned by this user: {path}")
        if reject_shared_write and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise HostSecurityError(f"Directory must not be group- or other-writable: {path}")
        return
    _windows_validate_path(path, expect_directory=True)


def ensure_private_directory(path: Path) -> None:
    """Create, then protect, a directory; existing unsafe ACLs are rejected."""
    ensure_no_reparse_path(path)
    if os.name != "nt":
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        if path.lstat().st_uid != os.getuid() or not stat.S_ISDIR(path.lstat().st_mode):
            raise HostSecurityError(f"Directory must be owned by this user: {path}")
        os.chmod(path, 0o700)
        return
    created = False
    try:
        path.mkdir(parents=False)
        created = True
    except FileExistsError:
        pass
    if created:
        _windows_set_private_acl_path(path, directory=True)
    _windows_validate_path(path, expect_directory=True)


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Atomically replace a protected file after securing the temporary file."""
    if os.name != "nt":
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or parent.st_uid != os.getuid():
            raise HostSecurityError(f"Managed parent must be owned by this user: {path.parent}")
        if os.path.lexists(path):
            with open_private_file(
                path, require_private=(stat.S_IMODE(mode) & 0o077 == 0),
                reject_shared_write=True,
            ):
                pass
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temporary)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return

    ensure_no_reparse_path(path.parent)
    validate_directory(path.parent)
    if os.path.lexists(path):
        with open_private_file(
            path, require_private=(stat.S_IMODE(mode) & 0o077 == 0),
            reject_shared_write=True,
        ):
            pass
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    descriptor_open = True
    try:
        _windows_set_private_acl_path(temp_path)
        output = os.fdopen(fd, "wb")
        descriptor_open = False
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
        with open_private_file(path, require_private=True, reject_shared_write=True):
            pass
    finally:
        if descriptor_open:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


def read_credential(service: str, account: str) -> str:
    """Read a host credential; unsupported or absent credential stores fail closed."""
    if os.name != "nt":
        raise HostSecurityError("Windows Credential Manager is unavailable on this host")
    return _windows_read_credential(service, account)


def store_credential(service: str, account: str, secret: str) -> None:
    if os.name != "nt":
        raise HostSecurityError("Windows Credential Manager is unavailable on this host")
    _windows_store_credential(service, account, secret)


def delete_credential(service: str, account: str) -> bool:
    if os.name != "nt":
        raise HostSecurityError("Windows Credential Manager is unavailable on this host")
    return _windows_delete_credential(service, account)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _credui = ctypes.WinDLL("Advapi32", use_last_error=True)
    _CRED = _credui
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _GENERIC_READ = 0x80000000
    _FILE_READ_ATTRIBUTES = 0x80
    _READ_CONTROL = 0x00020000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
    _OPEN_EXISTING = 3
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _CRED_TYPE_GENERIC = 1
    _CRED_PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168
    _INHERIT_ONLY_ACE = 0x08
    _KNOWN_ACE_FLAGS = 0x1F

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", _FILETIME),
            ("ftLastAccessTime", _FILETIME), ("ftLastWriteTime", _FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
        ]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_byte), ("AceFlags", ctypes.c_byte),
                    ("AceSize", wintypes.WORD)]

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", _FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                      ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    _kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _advapi32.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _advapi32.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_int]
    _advapi32.GetAclInformation.restype = wintypes.BOOL
    _advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetAce.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                     ctypes.POINTER(ctypes.c_void_p),
                                                     ctypes.POINTER(wintypes.BOOL)]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p]
    _advapi32.SetSecurityInfo.restype = wintypes.DWORD
    _CRED.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    _CRED.CredWriteW.restype = wintypes.BOOL
    _CRED.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    _CRED.CredReadW.restype = wintypes.BOOL
    _CRED.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _CRED.CredDeleteW.restype = wintypes.BOOL
    _CRED.CredFree.argtypes = [ctypes.c_void_p]
    _CRED.CredFree.restype = None
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _win_error(operation: str, code: int | None = None) -> HostSecurityError:
        return HostSecurityError(f"Windows security operation failed: {operation} ({code or ctypes.get_last_error()})")

    def _current_user_sid() -> str:
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
            raise _win_error("open current-user token")
        try:
            required = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
            if required.value < ctypes.sizeof(ctypes.c_void_p):
                raise _win_error("size current-user SID")
            buffer = ctypes.create_string_buffer(required.value)
            if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, required, ctypes.byref(required)):
                raise _win_error("read current-user SID")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_text = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
                raise _win_error("format current-user SID")
            try:
                return sid_text.value
            finally:
                _kernel32.LocalFree(sid_text)
        finally:
            _kernel32.CloseHandle(token)

    def _open_handle(path: Path, *, directory: bool = False, read_data: bool = False,
                     write_dac: bool = False, write_owner: bool = False):
        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        access = (_GENERIC_READ if read_data else _FILE_READ_ATTRIBUTES | _READ_CONTROL)
        if write_dac:
            access |= 0x00040000  # WRITE_DAC
        if write_owner:
            access |= 0x00080000  # WRITE_OWNER
        handle = _kernel32.CreateFileW(
            str(path), access, _FILE_SHARE_ALL, None, _OPEN_EXISTING, flags, None
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise _win_error(f"open {path}")
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            _kernel32.CloseHandle(handle)
            raise _win_error("inspect file attributes")
        attrs = info.dwFileAttributes
        if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
            _kernel32.CloseHandle(handle)
            raise HostSecurityError(f"Windows reparse points are not allowed: {path}")
        if bool(attrs & _FILE_ATTRIBUTE_DIRECTORY) != directory:
            _kernel32.CloseHandle(handle)
            raise HostSecurityError(f"Unexpected file type: {path}")
        return handle

    def _security_descriptor(handle):
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        status = _advapi32.GetSecurityInfo(
            handle, _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
        )
        if status != 0:
            raise _win_error("read owner and DACL", status)
        return owner, dacl, descriptor

    def _sid_string(sid_pointer) -> str:
        sid_text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text)):
            raise _win_error("read ACL principal")
        try:
            return sid_text.value
        finally:
            _kernel32.LocalFree(sid_text)

    def _validate_handle_acl(handle, path: Path) -> None:
        owner, dacl, descriptor = _security_descriptor(handle)
        try:
            current_sid = _current_user_sid()
            if not owner or _sid_string(owner) != current_sid:
                raise HostSecurityError(f"Windows file owner is not the current user: {path}")
            if not dacl:
                raise HostSecurityError(f"Windows file has a null or missing DACL: {path}")
            size_info = _ACL_SIZE_INFORMATION()
            if not _advapi32.GetAclInformation(
                dacl, ctypes.byref(size_info), ctypes.sizeof(size_info), 2
            ):
                raise _win_error("inspect DACL")
            principals: set[str] = set()
            for index in range(size_info.AceCount):
                ace = ctypes.c_void_p()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                    raise _win_error("read DACL entry")
                header = _ACE_HEADER.from_address(ace.value)
                if (header.AceType != 0 or header.AceFlags & ~_KNOWN_ACE_FLAGS
                        or header.AceSize < 12):
                    raise HostSecurityError(f"Windows file has an unrecognized ACE: {path}")
                sid_pointer = ace.value + ctypes.sizeof(_ACE_HEADER) + ctypes.sizeof(wintypes.DWORD)
                sid = _sid_string(sid_pointer)
                if sid not in {current_sid, "S-1-5-18", "S-1-5-32-544"}:
                    raise HostSecurityError(f"Windows DACL grants access to a broad or unknown principal: {path}")
                if not header.AceFlags & _INHERIT_ONLY_ACE:
                    principals.add(sid)
            if current_sid not in principals:
                raise HostSecurityError(f"Windows DACL does not grant access to the current user: {path}")
        finally:
            _kernel32.LocalFree(descriptor)

    def _windows_validate_path(path: Path, *, expect_directory: bool) -> None:
        ensure_no_reparse_path(path)
        handle = _open_handle(path, directory=expect_directory)
        try:
            _validate_handle_acl(handle, path)
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_open_private_file(path: Path, *, require_private: bool,
                                   reject_shared_write: bool) -> BinaryIO:
        del require_private, reject_shared_write  # Windows enforces one strict DACL policy.
        ensure_no_reparse_path(path)
        handle = _open_handle(path, read_data=True)
        try:
            _validate_handle_acl(handle, path)
            descriptor = msvcrt.open_osfhandle(
                int(handle), os.O_RDONLY | os.O_BINARY
            )
            handle = None
            return os.fdopen(descriptor, "rb")
        finally:
            if handle is not None:
                _kernel32.CloseHandle(handle)

    def _windows_set_private_acl(handle, *, directory: bool = False) -> None:
        del directory
        user_sid = _current_user_sid()
        sddl = f"D:P(A;;FA;;;{user_sid})(A;;FA;;;SY)(A;;FA;;;BA)"
        descriptor = ctypes.c_void_p()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise _win_error("build protected file DACL")
        try:
            dacl = ctypes.c_void_p()
            dacl_present = wintypes.BOOL()
            dacl_defaulted = wintypes.BOOL()
            if not _advapi32.GetSecurityDescriptorDacl(
                descriptor, ctypes.byref(dacl_present), ctypes.byref(dacl), ctypes.byref(dacl_defaulted)
            ) or not dacl_present.value or not dacl:
                raise _win_error("read protected DACL")
            status = _advapi32.SetSecurityInfo(
                handle, _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None,
            )
            if status != 0:
                raise _win_error("set protected DACL", status)
        finally:
            _kernel32.LocalFree(descriptor)

    def _windows_set_private_acl_path(path: Path, *, directory: bool = False) -> None:
        handle = _open_handle(path, directory=directory, write_dac=True, write_owner=True)
        try:
            _windows_set_current_user_owner(handle)
            _windows_set_private_acl(handle, directory=directory)
            _validate_handle_acl(handle, path)
        finally:
            _kernel32.CloseHandle(handle)

    def _windows_set_current_user_owner(handle) -> None:
        """Make newly created managed objects owned by the interactive user."""
        sid = ctypes.c_void_p()
        if not _advapi32.ConvertStringSidToSidW(_current_user_sid(), ctypes.byref(sid)):
            raise _win_error("format current-user owner SID")
        try:
            status = _advapi32.SetSecurityInfo(
                handle, _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION,
                sid, None, None, None,
            )
            if status != 0:
                raise _win_error("set current-user owner", status)
        finally:
            _kernel32.LocalFree(sid)

    def _credential_target(service: str, account: str) -> str:
        if not service or not account or any(char in "\x00\r\n" for char in service + account):
            raise HostSecurityError("Credential service or account is invalid")
        target = f"{service}/{account}"
        if len(target.encode("utf-16-le")) > 5120:
            raise HostSecurityError("Credential target is too long")
        return target

    def _windows_store_credential(service: str, account: str, secret: str) -> None:
        if not secret or "\x00" in secret:
            raise HostSecurityError("Credential value is empty or invalid")
        blob = bytearray(secret.encode("utf-8"))
        if len(blob) > 8192:
            raise HostSecurityError("Credential value exceeds the supported limit")
        blob_buffer = None
        try:
            blob_buffer = ctypes.create_string_buffer(len(blob))
            ctypes.memmove(
                blob_buffer,
                (ctypes.c_char * len(blob)).from_buffer(blob),
                len(blob),
            )
            record = _CREDENTIALW(
                Flags=0, Type=_CRED_TYPE_GENERIC, TargetName=_credential_target(service, account),
                Comment="Aixion Control Tower Codex connector token", LastWritten=_FILETIME(),
                CredentialBlobSize=len(blob), CredentialBlob=ctypes.cast(blob_buffer, ctypes.c_void_p),
                Persist=_CRED_PERSIST_LOCAL_MACHINE, AttributeCount=0, Attributes=None,
                TargetAlias=None, UserName=account,
            )
            if not _CRED.CredWriteW(ctypes.byref(record), 0):
                raise _win_error("store Windows Credential Manager secret")
        finally:
            if blob_buffer is not None:
                ctypes.memset(blob_buffer, 0, len(blob))
            if blob:
                ctypes.memset((ctypes.c_char * len(blob)).from_buffer(blob), 0, len(blob))

    def _windows_read_credential(service: str, account: str) -> str:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        target = _credential_target(service, account)
        if not _CRED.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            raise HostSecurityError("Agent token is missing from Windows Credential Manager")
        try:
            record = pointer.contents
            if record.Type != _CRED_TYPE_GENERIC or record.TargetName != target:
                raise HostSecurityError("Windows Credential Manager returned a mismatched credential")
            size = int(record.CredentialBlobSize)
            if not record.CredentialBlob or size < 1 or size > 8192:
                raise HostSecurityError("Windows Credential Manager returned an invalid credential")
            raw = ctypes.string_at(record.CredentialBlob, size)
            try:
                value = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise HostSecurityError("Windows Credential Manager secret is invalid") from exc
            if not value.strip():
                raise HostSecurityError("Windows Credential Manager secret is empty")
            return value.strip()
        finally:
            try:
                record = pointer.contents
                if record.CredentialBlob and record.CredentialBlobSize:
                    ctypes.memset(record.CredentialBlob, 0, record.CredentialBlobSize)
            finally:
                _CRED.CredFree(pointer)

    def _windows_delete_credential(service: str, account: str) -> bool:
        if _CRED.CredDeleteW(_credential_target(service, account), _CRED_TYPE_GENERIC, 0):
            return True
        code = ctypes.get_last_error()
        if code == _ERROR_NOT_FOUND:
            return False
        raise _win_error("delete Windows Credential Manager secret", code)
