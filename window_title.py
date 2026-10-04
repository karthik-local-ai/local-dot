"""Read window titles for the one Edge process we launched, not other applications."""
import os


def owned_titles(process_id):
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.IsWindowVisible.restype = wintypes.BOOL
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextLengthW.restype = ctypes.c_int
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowTextW.restype = ctypes.c_int
    titles = []
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Never read titles from ordinary Edge/Chrome, editors or other applications.
        if pid.value == process_id and user.IsWindowVisible(hwnd):
            length = user.GetWindowTextLengthW(hwnd)
            if length:
                value = ctypes.create_unicode_buffer(length + 1)
                user.GetWindowTextW(hwnd, value, len(value))
                titles.append(value.value)
        return True
    user.EnumWindows(callback_type(visit), 0)
    return titles
