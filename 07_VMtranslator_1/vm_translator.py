#!/usr/bin/env python3
# vm_translator.py
#
# nand2tetris 第7章用 VM → Hack アセンブリ変換器（単一ファイル版）
# 対応:
#   - 算術コマンド: add, sub, neg, eq, gt, lt, and, or, not
#   - push/pop: argument, local, this, that, constant, static, temp, pointer

from __future__ import annotations
from enum import Enum, auto
from typing import List
import os
import sys


# ------------------------------------------------------------
# コマンド種別
# ------------------------------------------------------------

class CommandType(Enum):
    C_ARITHMETIC = auto()
    C_PUSH = auto()
    C_POP = auto()


# ------------------------------------------------------------
# Parser: .vm テキストを1コマンドずつ扱う
# ------------------------------------------------------------

class Parser:
    def __init__(self, text: str) -> None:
        # コメント / 空行を除去してコマンド配列にする
        lines = text.splitlines()
        self.commands: List[str] = []
        for raw in lines:
            # "//" 以降はコメントとして除去
            code = raw.split("//")[0].strip()
            if not code:
                continue
            self.commands.append(code)

        self.current: str | None = None
        self.index: int = -1

    def has_more_commands(self) -> bool:
        return self.index + 1 < len(self.commands)

    def advance(self) -> None:
        """次のコマンドに進める"""
        self.index += 1
        self.current = self.commands[self.index]

    def command_type(self) -> CommandType:
        assert self.current is not None
        parts = self.current.split()
        op = parts[0]
        if op == "push":
            return CommandType.C_PUSH
        if op == "pop":
            return CommandType.C_POP
        # それ以外は全部算術コマンドとして扱う
        return CommandType.C_ARITHMETIC

    def arg1(self) -> str:
        """算術コマンド: コマンド名
           push/pop: セグメント名"""
        assert self.current is not None
        ctype = self.command_type()
        parts = self.current.split()
        if ctype == CommandType.C_ARITHMETIC:
            return parts[0]
        else:
            return parts[1]

    def arg2(self) -> int:
        """push/pop のインデックス"""
        assert self.current is not None
        parts = self.current.split()
        return int(parts[2])


# ------------------------------------------------------------
# CodeWriter: VMコマンドを Hack アセンブリ文字列へ
# ------------------------------------------------------------

class CodeWriter:
    def __init__(self) -> None:
        self.lines: List[str] = []  # 出力するアセンブリ行
        self.filename: str = ""     # static 用
        self.label_count: int = 0   # 比較演算用ラベル番号

    def set_file_name(self, filename: str) -> None:
        """static セグメント名に使うファイル名（拡張子なし）"""
        self.filename = filename

    # ---------- 算術コマンド ----------

    def write_arithmetic(self, op: str) -> None:
        self.lines.append(f"// {op}")
        if op in ("add", "sub", "and", "or"):
            self._write_binary_op(op)
        elif op in ("neg", "not"):
            self._write_unary_op(op)
        elif op in ("eq", "gt", "lt"):
            self._write_compare(op)
        else:
            raise ValueError(f"Unknown arithmetic op: {op}")

    def _write_binary_op(self, op: str) -> None:
        """
        二項演算: スタックトップ2つを x,y として
          add: x+y
          sub: x-y
          and: x & y
          or : x | y
        結果は元の x の位置に書き戻し、スタックサイズは -1。
        """
        # y = *--SP, x = *(SP-1)
        self.lines += [
            "@SP",
            "AM=M-1",   # SP--, A=SP, M=*SP (y)
            "D=M",      # D = y
            "A=A-1",    # A = SP-1 (x)
        ]
        if op == "add":
            self.lines.append("M=M+D")   # x = x + y
        elif op == "sub":
            self.lines.append("M=M-D")   # x = x - y
        elif op == "and":
            self.lines.append("M=M&D")   # x = x & y
        elif op == "or":
            self.lines.append("M=M|D")   # x = x | y

    def _write_unary_op(self, op: str) -> None:
        """
        単項演算: スタックトップ1つに対して
          neg: -x
          not: !x
        結果は同じ位置に上書き。
        """
        self.lines += [
            "@SP",
            "A=M-1",  # top
        ]
        if op == "neg":
            self.lines.append("M=-M")
        elif op == "not":
            self.lines.append("M=!M")

    def _write_compare(self, op: str) -> None:
        """
        比較演算:
          eq: x == y ? -1 : 0
          gt: x >  y ? -1 : 0
          lt: x <  y ? -1 : 0
        スタックトップ2つ (x,y) → 1つ (true:-1, false:0)
        """
        true_label = f"{op.upper()}_TRUE_{self.label_count}"
        end_label = f"{op.upper()}_END_{self.label_count}"
        self.label_count += 1

        jump = {
            "eq": "JEQ",
            "gt": "JGT",
            "lt": "JLT",
        }[op]

        self.lines += [
            "@SP",
            "AM=M-1",   # SP--, A=SP, M=*SP (y)
            "D=M",      # D = y
            "A=A-1",    # A = SP-1 (x)
            "D=M-D",    # D = x - y
            f"@{true_label}",
            f"D;{jump}",   # 条件成立 → true_label
            # false の場合
            "@SP",
            "A=M-1",       # A = SP-1 (x の位置)
            "M=0",         # false = 0
            f"@{end_label}",
            "0;JMP",
            # true の場合
            f"({true_label})",
            "@SP",
            "A=M-1",
            "M=-1",        # true = -1
            f"({end_label})",
        ]

    # ---------- push / pop ----------

    def write_push_pop(self, ctype: CommandType, segment: str, index: int) -> None:
        cmd = "push" if ctype == CommandType.C_PUSH else "pop"
        self.lines.append(f"// {cmd} {segment} {index}")
        if ctype == CommandType.C_PUSH:
            self._write_push(segment, index)
        else:
            self._write_pop(segment, index)

    def _write_push(self, segment: str, index: int) -> None:
        # D レジスタに「pushする値」を入れてから、汎用 push シーケンス
        if segment == "constant":
            # ただの即値
            self.lines += [
                f"@{index}",
                "D=A",
            ]
        elif segment in ("local", "argument", "this", "that"):
            base = {
                "local": "LCL",
                "argument": "ARG",
                "this": "THIS",
                "that": "THAT",
            }[segment]
            self.lines += [
                f"@{base}",
                "D=M",
                f"@{index}",
                "A=D+A",  # base + index
                "D=M",
            ]
        elif segment == "temp":
            # temp i → 5 + i (R5〜R12)
            addr = 5 + index
            self.lines += [
                f"@{addr}",
                "D=M",
            ]
        elif segment == "pointer":
            # pointer 0 → THIS, pointer 1 → THAT
            if index == 0:
                self.lines += ["@THIS"]
            elif index == 1:
                self.lines += ["@THAT"]
            else:
                raise ValueError("pointer index must be 0 or 1")
            self.lines.append("D=M")
        elif segment == "static":
            # FileName.index というシンボル名で表現
            symbol = f"{self.filename}.{index}"
            self.lines += [
                f"@{symbol}",
                "D=M",
            ]
        else:
            raise ValueError(f"Unknown segment: {segment}")

        # 汎用 push: *SP = D; SP++
        self.lines += [
            "@SP",
            "A=M",
            "M=D",
            "@SP",
            "M=M+1",
        ]

    def _write_pop(self, segment: str, index: int) -> None:
        if segment in ("local", "argument", "this", "that"):
            base = {
                "local": "LCL",
                "argument": "ARG",
                "this": "THIS",
                "that": "THAT",
            }[segment]
            # アドレス計算結果を R13 に保存
            self.lines += [
                f"@{base}",
                "D=M",
                f"@{index}",
                "D=D+A",
                "@R13",
                "M=D",   # R13 = base + index
            ]
            # *--SP を取り出して [R13] へ
            self.lines += [
                "@SP",
                "AM=M-1",
                "D=M",
                "@R13",
                "A=M",
                "M=D",
            ]
        elif segment == "temp":
            addr = 5 + index   # temp i → 5 + i
            self.lines += [
                "@SP",
                "AM=M-1",
                "D=M",
                f"@{addr}",
                "M=D",
            ]
        elif segment == "pointer":
            # pointer 0/1
            self.lines += [
                "@SP",
                "AM=M-1",
                "D=M",
            ]
            if index == 0:
                self.lines.append("@THIS")
            elif index == 1:
                self.lines.append("@THAT")
            else:
                raise ValueError("pointer index must be 0 or 1")
            self.lines.append("M=D")
        elif segment == "static":
            symbol = f"{self.filename}.{index}"
            self.lines += [
                "@SP",
                "AM=M-1",
                "D=M",
                f"@{symbol}",
                "M=D",
            ]
        else:
            raise ValueError(f"Unknown segment for pop: {segment}")


# ------------------------------------------------------------
# VM 全体を翻訳するドライバ関数
# ------------------------------------------------------------

def translate_vm(in_path: str) -> List[str]:
    with open(in_path, "r", encoding="utf-8") as f:
        vm_text = f.read()

    parser = Parser(vm_text)
    writer = CodeWriter()

    # static 用のファイル名 (拡張子なし)
    filename = os.path.splitext(os.path.basename(in_path))[0]
    writer.set_file_name(filename)

    # スタックマシンのコマンドを1つずつ処理
    while parser.has_more_commands():
        parser.advance()
        ctype = parser.command_type()
        if ctype == CommandType.C_ARITHMETIC:
            op = parser.arg1()
            writer.write_arithmetic(op)
        elif ctype in (CommandType.C_PUSH, CommandType.C_POP):
            segment = parser.arg1()
            index = parser.arg2()
            writer.write_push_pop(ctype, segment, index)
        else:
            raise ValueError("Unexpected command type")

    return writer.lines


# ------------------------------------------------------------
# main
# ------------------------------------------------------------

def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python vm_translator.py Program.vm")
        sys.exit(1)

    in_path = argv[1]
    if not os.path.exists(in_path):
        print(f"Input not found: {in_path}")
        sys.exit(1)

    asm_lines = translate_vm(in_path)
    out_path = os.path.splitext(in_path)[0] + ".asm"

    with open(out_path, "w", encoding="utf-8") as f:
        for line in asm_lines:
            f.write(line + "\n")

    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main(sys.argv)
