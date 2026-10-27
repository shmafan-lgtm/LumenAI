# -*- coding: utf-8 -*-
"""从 PE 文件里查导出函数名 → ordinal 的映射（开发期工具）。

Windows 的 gdiplus.dll 把一批函数只按序号导出（ordinal export），
按名字 getattr 会失败，只能用 dll[ordinal] 调用。
这个脚本直接从 PE 导出表里把「名字 → 序号」读出来。
"""
import struct
import os
import sys


def exports(path):
    d = open(path, 'rb').read()
    e = struct.unpack_from('<I', d, 0x3c)[0]
    assert d[e:e + 4] == b'PE\0\0'
    coff = e + 4
    nsec = struct.unpack_from('<H', d, coff + 2)[0]
    optsz = struct.unpack_from('<H', d, coff + 16)[0]
    opt = coff + 20
    magic = struct.unpack_from('<H', d, opt)[0]
    ddoff = opt + (112 if magic == 0x20b else 96)

    # 导出目录 RVA 在数据目录第 0 项
    edir_rva = struct.unpack_from('<I', d, ddoff)[0]

    sec0 = opt + optsz
    sections = []
    for i in range(nsec):
        o = sec0 + i * 40
        vsz, va, rsz, rp = struct.unpack_from('<IIII', d, o + 8)
        sections.append((va, vsz, rsz, rp))

    def r2o(rva):
        for va, vsz, rsz, rp in sections:
            if va <= rva < va + max(vsz, rsz):
                return rp + (rva - va)
        return None

    eo = r2o(edir_rva)
    if eo is None:
        return {}
    # IMAGE_EXPORT_DIRECTORY
    base = struct.unpack_from('<I', d, eo + 16)[0]
    nfunc = struct.unpack_from('<I', d, eo + 20)[0]
    nnames = struct.unpack_from('<I', d, eo + 24)[0]
    anames = struct.unpack_from('<I', d, eo + 32)[0]
    aords = struct.unpack_from('<I', d, eo + 36)[0]

    ntab = r2o(anames)
    otab = r2o(aords)
    if ntab is None or otab is None:
        return {}

    def cstr(off):
        end = d.index(b'\0', off)
        return d[off:end].decode('ascii', 'replace')

    out = {}
    for i in range(nnames):
        noff = struct.unpack_from('<I', d, ntab + i * 4)[0]
        no = r2o(noff)
        if no is None:
            continue
        nm = cstr(no)
        ordi = struct.unpack_from('<H', d, otab + i * 2)[0]
        out[nm] = base + ordi
    return out


if __name__ == '__main__':
    dll = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get('WINDIR', 'C:/Windows'), 'System32', 'gdiplus.dll')
    want = sys.argv[2:] or [
        'GdipCreateEncoderFromCLSID', 'GdipSetEncoderParameter',
        'GdipGetEncoderClsid', 'GdipCreateStreamOnHGlobal',
        'GdipStreamGetLength', 'GdipStreamRead', 'GdipStreamSeek',
        'GdipStreamReset', 'GdipDisposeStream',
    ]
    m = exports(dll)
    print('total named exports:', len(m))
    for w in want:
        print('  %-34s -> %s' % (w, m.get(w, 'NOT FOUND')))
