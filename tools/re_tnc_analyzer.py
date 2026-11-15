#!/usr/bin/env python3
"""
NET-51TNC & Embedded .NET Binary Analysis Tool
Extracts metadata, CIL bytecode instructions, string heaps, and EEPROM maps
from .NET PE binaries without requiring Windows or Visual Studio.
"""

import sys
import os
import argparse
from typing import List, Any, Tuple

try:
    import dnfile
    from dncil.cil.body.reader import read_method_body_from_bytes
    from dncil.clr.token import Token, StringToken
except ImportError as e:
    print(f"Error: Missing reverse engineering dependencies: {e}")
    print("Install via: pip install --user --break-system-packages dnfile pefile dncil")
    sys.exit(1)


class DotNetAnalyzer:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.pe = dnfile.dnPE(file_path)
        self.us_data = b""
        if b"#US" in self.pe.net.metadata.streams:
            s = self.pe.net.metadata.streams[b"#US"]
            self.us_data = self.pe.get_data(s.rva, s.sizeof())

    def get_user_string(self, offset: int) -> str:
        """Parse length-prefixed UTF-16LE user string from #US stream."""
        if not self.us_data or offset >= len(self.us_data):
            return ""
        b0 = self.us_data[offset]
        if b0 < 0x80:
            length = b0
            idx = offset + 1
        elif b0 < 0xC0:
            length = ((b0 & 0x3F) << 8) | self.us_data[offset + 1]
            idx = offset + 2
        else:
            length = (
                ((b0 & 0x1F) << 24)
                | (self.us_data[offset + 1] << 16)
                | (self.us_data[offset + 2] << 8)
                | self.us_data[offset + 3]
            )
            idx = offset + 4
        if length <= 1:
            return ""
        # Trailing 1-byte flag in ECMA-335 is stripped
        return self.us_data[idx : idx + length - 1].decode("utf-16le", errors="replace")

    def list_methods(self) -> List[Tuple[str, int, int]]:
        """List all method names, RVAs, and approximate sizes."""
        methods = []
        for row in self.pe.net.mdtables.MethodDef:
            name = str(row.Name)
            rva = row.Rva
            methods.append((name, rva, row.row_index))
        return methods

    def disassemble_method(self, method_name: str, max_bytes: int = 16384) -> List[Any]:
        """Disassemble method body to CIL instructions."""
        for row in self.pe.net.mdtables.MethodDef:
            if str(row.Name) == method_name and row.Rva > 0:
                data = self.pe.get_data(row.Rva, max_bytes)
                body = read_method_body_from_bytes(data)
                return body.instructions
        return []

    def extract_strings_from_method(self, method_name: str) -> List[str]:
        """Extract all string constants referenced via ldstr in a method."""
        instructions = self.disassemble_method(method_name)
        strings = []
        for ins in instructions:
            if ins.opcode.name == "ldstr" and isinstance(
                ins.operand, (Token, StringToken)
            ):
                s = self.get_user_string(ins.operand.value & 0x00FFFFFF)
                if s and s not in strings:
                    strings.append(s)
        return strings

    def trace_eeprom_writes(
        self, method_name: str = "Button6_Click"
    ) -> List[Tuple[int, str]]:
        """Trace array assignments (stelem.i1) to map byte offsets."""
        instructions = self.disassemble_method(method_name)
        writes = []
        # Pattern tracking: ldc.i4 <offset>, ..., stelem.i1
        last_offset = None
        for i, ins in enumerate(instructions):
            if ins.opcode.name in (
                "ldc.i4",
                "ldc.i4.s",
                "ldc.i4.0",
                "ldc.i4.1",
                "ldc.i4.2",
                "ldc.i4.3",
                "ldc.i4.4",
                "ldc.i4.5",
                "ldc.i4.6",
                "ldc.i4.7",
                "ldc.i4.8",
                "ldc.i4.m1",
            ):
                val = 0
                if "." in ins.opcode.name and ins.opcode.name.split(".")[-1].isdigit():
                    val = int(ins.opcode.name.split(".")[-1])
                elif ins.opcode.name == "ldc.i4.m1":
                    val = -1
                elif ins.operand is not None:
                    val = ins.operand
                last_offset = val
            elif ins.opcode.name in ("stelem.i1", "stelem.i2", "stelem.i4"):
                if last_offset is not None:
                    writes.append((last_offset, ins.opcode.name))
        return writes


def main():
    parser = argparse.ArgumentParser(
        description="Analyze .NET PE binaries and extract protocol/EEPROM details"
    )
    parser.add_argument("binary", help="Path to .NET .exe or .dll file")
    parser.add_argument(
        "--list-methods", action="store_true", help="List all defined methods"
    )
    parser.add_argument(
        "--disasm", metavar="METHOD", help="Disassemble specified method"
    )
    parser.add_argument(
        "--strings", metavar="METHOD", help="Extract strings from specified method"
    )
    parser.add_argument(
        "--trace-eeprom",
        action="store_true",
        help="Trace EEPROM array stores in Button6_Click",
    )

    args = parser.parse_args()
    if not os.path.exists(args.binary):
        print(f"File not found: {args.binary}")
        sys.exit(1)

    analyzer = DotNetAnalyzer(args.binary)
    print(f"Loaded: {os.path.basename(args.binary)}")

    if args.list_methods:
        methods = analyzer.list_methods()
        print(f"Total methods: {len(methods)}")
        for name, rva, idx in methods:
            if not name.startswith(("get_", "set_", "add_", "remove_")):
                print(f"  [0x{rva:08X}] #{idx:03d} {name}")

    if args.strings:
        strings = analyzer.extract_strings_from_method(args.strings)
        print(f"Strings referenced in {args.strings}:")
        for s in strings:
            print(f"  {repr(s)}")

    if args.disasm:
        instructions = analyzer.disassemble_method(args.disasm)
        print(f"Disassembly for {args.disasm} ({len(instructions)} instructions):")
        for ins in instructions:
            op_str = f" {ins.operand}" if ins.operand is not None else ""
            print(f"  0x{ins.offset:04X}: {ins.opcode.name}{op_str}")

    if args.trace_eeprom:
        writes = analyzer.trace_eeprom_writes("Button6_Click")
        print(f"Traced {len(writes)} EEPROM array store operations:")
        for off, op in writes[:30]:
            print(f"  Offset 0x{off:03X} ({off:3d}): {op}")
        if len(writes) > 30:
            print(f"  ... ({len(writes) - 30} more writes)")


if __name__ == "__main__":
    main()
