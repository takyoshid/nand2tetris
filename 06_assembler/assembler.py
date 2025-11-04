# assembler.py
# Nand2Tetris (Elements of Computing Systems) Chapter 6: Complete Assembler
# Usage:
#   python assembler.py input.asm              -> writes input.hack next to input
#   python assembler.py input.asm -o out.hack  -> writes to explicit output path
#
# Features:
# - Two-pass assembly (labels in pass 1, variables in pass 2 starting at RAM[16])
# - Predefined symbols (R0..R15, SP,LCL,ARG,THIS,THAT,SCREEN,KBD)
# - A- and C-instructions, whitespace/comments removal, robust errors with line numbers

import sys
import os
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

# -----------------------------
# Code tables (spec from Nand2Tetris)
# -----------------------------
DEST_TABLE: Dict[Optional[str], str] = {
    None:   "000",
    "M":    "001",
    "D":    "010",
    "MD":   "011",
    "A":    "100",
    "AM":   "101",
    "AD":   "110",
    "AMD":  "111",
}

JUMP_TABLE: Dict[Optional[str], str] = {
    None:   "000",
    "JGT":  "001",
    "JEQ":  "010",
    "JGE":  "011",
    "JLT":  "100",
    "JNE":  "101",
    "JLE":  "110",
    "JMP":  "111",
}

COMP_TABLE: Dict[str, str] = {
    # a=0
    "0":   "0101010",
    "1":   "0111111",
    "-1":  "0111010",
    "D":   "0001100",
    "A":   "0110000",
    "!D":  "0001101",
    "!A":  "0110001",
    "-D":  "0001111",
    "-A":  "0110011",
    "D+1": "0011111",
    "A+1": "0110111",
    "D-1": "0001110",
    "A-1": "0110010",
    "D+A": "0000010",
    "D-A": "0010011",
    "A-D": "0000111",
    "D&A": "0000000",
    "D|A": "0010101",
    # a=1 (replace A with M)
    "M":   "1110000",
    "!M":  "1110001",
    "-M":  "1110011",
    "M+1": "1110111",
    "M-1": "1110010",
    "D+M": "1000010",
    "D-M": "1010011",
    "M-D": "1000111",
    "D&M": "1000000",
    "D|M": "1010101",
}

PREDEFINED: Dict[str, int] = {
    "SP": 0, "LCL": 1, "ARG": 2, "THIS": 3, "THAT": 4,
    "SCREEN": 16384, "KBD": 24576,
    **{f"R{i}": i for i in range(16)}
}

# -----------------------------
# Errors
# -----------------------------
class AsmError(Exception):
    pass

@dataclass
class SourceLine:
    raw: str
    line_no: int  # 1-based in the original file

# -----------------------------
# Utility parsing helpers
# -----------------------------
def strip_comment_and_ws(line: str) -> str:
    # Remove inline comments and surrounding whitespace
    if "//" in line:
        line = line.split("//", 1)[0]
    return line.strip()

def is_label(line: str) -> bool:
    return line.startswith("(") and line.endswith(")") and len(line) >= 3

def parse_label(line: str) -> str:
    # (LOOP) -> LOOP
    return line[1:-1].strip()

def is_a_instruction(line: str) -> bool:
    return line.startswith("@")

def parse_a_symbol(line: str) -> str:
    # @value or @symbol -> 'value' or 'symbol'
    return line[1:].strip()

def parse_c_instruction(line: str) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Returns (dest, comp, jump)
    line could be: dest=comp;jump | comp;jump | dest=comp | comp
    """
    dest, compjump = None, line
    if "=" in line:
        dest, compjump = line.split("=", 1)
        dest = dest.strip() or None
    comp, jump = compjump, None
    if ";" in compjump:
        comp, jump = compjump.split(";", 1)
        jump = jump.strip() or None
    comp = comp.strip()
    return dest, comp, jump

def to_15bit_binary(n: int) -> str:
    if n < 0 or n > 32767:
        raise AsmError(f"Constant out of range for 15-bit A-instruction: {n}")
    return f"{n:015b}"

# -----------------------------
# Pass 1: Build symbol table with labels
# -----------------------------
def pass1_build_symbols(lines: List[SourceLine]) -> Dict[str, int]:
    symbols = dict(PREDEFINED)
    rom_addr = 0
    for s in lines:
        text = strip_comment_and_ws(s.raw)
        if not text:
            continue
        if is_label(text):
            label = parse_label(text)
            if not label or any(c.isspace() for c in label):
                raise AsmError(f"[line {s.line_no}] Invalid label syntax: {s.raw}")
            if label in symbols:
                # Re-definition check (labels shouldn’t overwrite predefined or previous labels)
                if symbols[label] != rom_addr:
                    raise AsmError(f"[line {s.line_no}] Label redefined: {label}")
            else:
                symbols[label] = rom_addr
        else:
            # Only actual instructions consume ROM addresses
            rom_addr += 1
    return symbols

# -----------------------------
# Pass 2: Translate to machine code
# -----------------------------
def pass2_translate(lines: List[SourceLine], symbols: Dict[str, int]) -> List[str]:
    out: List[str] = []
    next_var_addr = 16

    for s in lines:
        text = strip_comment_and_ws(s.raw)
        if not text or is_label(text):
            continue

        if is_a_instruction(text):
            token = parse_a_symbol(text)
            addr: int
            if token.isdigit():
                addr = int(token)
            else:
                if token not in symbols:
                    # allocate new variable address
                    addr = next_var_addr
                    symbols[token] = addr
                    next_var_addr += 1
                else:
                    addr = symbols[token]
            out.append("0" + to_15bit_binary(addr))
            continue

        # C-instruction
        dest, comp, jump = parse_c_instruction(text)
        if comp not in COMP_TABLE:
            raise AsmError(f"[line {s.line_no}] Invalid comp field: '{comp}' in: {s.raw}")
        if dest not in DEST_TABLE:
            raise AsmError(f"[line {s.line_no}] Invalid dest field: '{dest}' in: {s.raw}")
        if jump not in JUMP_TABLE:
            raise AsmError(f"[line {s.line_no}] Invalid jump field: '{jump}' in: {s.raw}")

        bits = "111" + COMP_TABLE[comp] + DEST_TABLE[dest] + JUMP_TABLE[jump]
        out.append(bits)
    return out

# -----------------------------
# Driver
# -----------------------------
def assemble(asm_text: str) -> List[str]:
    # Preserve original line numbers for good diagnostics
    lines = [SourceLine(raw=l.rstrip("\n"), line_no=i+1)
             for i, l in enumerate(asm_text.splitlines())]
    symbols = pass1_build_symbols(lines)
    machine = pass2_translate(lines, symbols)
    return machine

def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python assembler.py <input.asm> [-o output.hack]")
        sys.exit(1)
    in_path = argv[1]
    if not os.path.exists(in_path):
        print(f"Input not found: {in_path}")
        sys.exit(1)

    # Determine out path
    out_path = None
    if "-o" in argv:
        try:
            out_path = argv[argv.index("-o")+1]
        except Exception:
            print("Error: -o requires an output path")
            sys.exit(1)
    else:
        stem, _ = os.path.splitext(in_path)
        out_path = stem + ".hack"

    try:
        with open(in_path, "r", encoding="utf-8") as f:
            asm_text = f.read()
        hack_lines = assemble(asm_text)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for line in hack_lines:
                f.write(line + "\n")
        print(f"OK: wrote {out_path} ({len(hack_lines)} instructions)")
    except AsmError as e:
        print(f"Assembly error: {e}")
        sys.exit(2)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(3)

if __name__ == "__main__":
    main(sys.argv)
