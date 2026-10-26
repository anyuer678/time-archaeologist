"""窗口截图（仅标准库：ctypes + GDI + 手写 PNG 编码器）。

用法：python scripts/screenshot_app.py <标题包含字串> <输出.png>
优先 PrintWindow（可捕获被遮挡窗口），失败退回屏幕 BitBlt。
"""
import ctypes
import struct
import sys
import zlib
import ctypes.wintypes as wt

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

PW_RENDERFULLCONTENT = 2
SRCCOPY = 0x00CC0020


def find_window(part: str) -> int:
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        n = user32.GetWindowTextLengthW(hwnd)
        if n and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if part in buf.value:
                hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else 0


class RECT(ctypes.Structure):
    _fields_ = [("L", ctypes.c_long), ("T", ctypes.c_long),
                ("R", ctypes.c_long), ("B", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


def write_png(path: str, w: int, h: int, rgb: bytes) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    stride = w * 3
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # 无过滤器
        raw += rgb[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                + chunk(b"IEND", b""))


def capture(hwnd: int, out: str) -> tuple[int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.R - r.L, r.B - r.T
    if w <= 0 or h <= 0:
        raise RuntimeError(f"bad rect {w}x{h}")

    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)

    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    if not ok:
        sdc = user32.GetDC(0)
        gdi32.BitBlt(mdc, 0, 0, w, h, sdc, r.L, r.T, SRCCOPY)
        user32.ReleaseDC(0, sdc)

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h          # 负高 = 自顶向下
    bi.biPlanes, bi.biBitCount = 1, 32
    bi.biCompression = 0                     # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)

    bgra = buf.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3] = bgra[2::4]                   # R
    rgb[1::3] = bgra[1::4]                   # G
    rgb[2::3] = bgra[0::4]                   # B
    write_png(out, w, h, bytes(rgb))

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    return w, h


def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else "时光考古机"
    out = sys.argv[2] if len(sys.argv) > 2 else "shots/app.png"
    hwnd = find_window(part)
    if not hwnd:
        print(f"window not found: {part}")
        return 1
    user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    import time
    time.sleep(0.6)
    w, h = capture(hwnd, out)
    print(f"saved {out} ({w}x{h})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
