#!/usr/bin/env python3
# vm_translator.py
#
# nand2tetris 第7〜8章用 VM → Hack アセンブリ変換器（単一ファイル版）
# 対応:
#   - 算術コマンド: add, sub, neg, eq, gt, lt, and, or, not
#   - push/pop: argument, local, this, that, constant, static, temp, pointer
#   - プログラムフロー: label, goto, if-goto
#   - 関数コール: function, call, return
#   - 複数 .vm ファイル + ブートストラップ(SP=256; call Sys.init)
#
# 使い方:
#   python vm_translator.py Foo.vm
#   python vm_translator.py path/to/Dir   # ディレクトリ内の .vm 全部をまとめて翻訳

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
    C_LABEL = auto()
    C_GOTO = auto()
    C_IF = auto()
    C_FUNCTION = auto()
    C_CALL = auto()
    C_RETURN = auto()


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
        if op == "label":
            return CommandType.C_LABEL
        if op == "goto":
            return CommandType.C_GOTO
        if op == "if-goto":
            return CommandType.C_IF
        if op == "function":
            return CommandType.C_FUNCTION
        if op == "call":
            return CommandType.C_CALL
        if op == "return":
            return CommandType.C_RETURN
        # それ以外は全部算術コマンドとして扱う
        return CommandType.C_ARITHMETIC

    def arg1(self) -> str:
        """算術コマンド: コマンド名
           push/pop: セグメント名
           label/goto/if-goto/function/call: シンボル名 or 関数名"""
        assert self.current is not None
        ctype = self.command_type()
        parts = self.current.split()

        if ctype == CommandType.C_ARITHMETIC:
            return parts[0]
        if ctype in (
            CommandType.C_PUSH,
            CommandType.C_POP,
            CommandType.C_LABEL,
            CommandType.C_GOTO,
            CommandType.C_IF,
            CommandType.C_FUNCTION,
            CommandType.C_CALL,
        ):
            return parts[1]
        if ctype == CommandType.C_RETURN:
            # return は引数なし（arg1 は使わない）
            raise ValueError("arg1() is not valid for C_RETURN")
        raise ValueError("Unknown command type in arg1()")

    def arg2(self) -> int:
        """push/pop/function/call のインデックス or 数値引数"""
        assert self.current is not None
        ctype = self.command_type()
        if ctype not in (
            CommandType.C_PUSH,
            CommandType.C_POP,
            CommandType.C_FUNCTION,
            CommandType.C_CALL,
        ):
            raise ValueError("arg2() is only valid for push/pop/function/call")
        parts = self.current.split()
        return int(parts[2])


# ------------------------------------------------------------
# CodeWriter: VMコマンドを Hack アセンブリ文字列へ
# ------------------------------------------------------------

class CodeWriter:
    def __init__(self) -> None:
        self.lines: List[str] = []     # 出力するアセンブリ行
        self.filename: str = ""        # static 用
        self.label_count: int = 0      # 比較演算用ラベル番号
        self.call_count: int = 0       # call 用リターンラベル番号
        self.current_function: str = ""  # 関数名（ラベルのスコープ用）

    def set_file_name(self, filename: str) -> None:
        """static セグメント名に使うファイル名（拡張子なし）"""
        self.filename = filename

    # ---------- ブートストラップ ----------

    def write_init(self) -> None:
        """
        SP=256 にセットし、Sys.init を呼び出す。
        ディレクトリ入力時など、プログラムのエントリポイントを決めるために使う。
        """
        self.lines += [
            "// bootstrap: SP=256; call Sys.init",
            "@256",
            "D=A",
            "@SP",
            "M=D",
        ]
        # 引数0で Sys.init を call
        self.write_call("Sys.init", 0)

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
            "A=A-1",    # A = SP-1 (x), M = x
        ]

        if op == "add":
            # D = x + y （D+M を使う）
            self.lines += [
                "D=D+M",  # D = y + x
                "M=D",    # M = x + y
            ]
        elif op == "sub":
            # x - y = x + (-y)
            self.lines += [
                "D=-D",   # D = -y
                "D=D+M",  # D = -y + x = x - y
                "M=D",
            ]
        elif op == "and":
            self.lines += [
                "D=D&M",  # y & x
                "M=D",
            ]
        elif op == "or":
            self.lines += [
                "D=D|M",  # y | x
                "M=D",
            ]

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
        true_label = f"{op.UPPER()}_TRUE_{self.label_count}"
        end_label = f"{op.UPPER()}_END_{self.label_count}"
        self.label_count += 1

        jump = {
            "eq": "JEQ",
            "gt": "JGT",
            "lt": "JLT",
        }[op]

        # x - y を D に入れてから判定する（D+M だけを使う形に統一）
        self.lines += [
            "@SP",
            "AM=M-1",   # SP--, A=SP, M=*SP (y)
            "D=M",      # D = y
            "A=A-1",    # A = SP-1 (x), M = x
            "D=-D",     # D = -y
            "D=D+M",    # D = -y + x = x - y
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

    # ---------- プログラムフロー: label / goto / if-goto ----------

    def _scoped_label(self, label: str) -> str:
        """
        関数内のラベルは functionName$label のように名前空間を切る
        """
        if self.current_function:
            return f"{self.current_function}${label}"
        return label

    def write_label(self, label: str) -> None:
        scoped = self._scoped_label(label)
        self.lines.append(f"// label {label}")
        self.lines.append(f"({scoped})")

    def write_goto(self, label: str) -> None:
        scoped = self._scoped_label(label)
        self.lines.append(f"// goto {label}")
        self.lines += [
            f"@{scoped}",
            "0;JMP",
        ]

    def write_if(self, label: str) -> None:
        scoped = self._scoped_label(label)
        self.lines.append(f"// if-goto {label}")
        # if pop()!=0 goto label
        self.lines += [
            "@SP",
            "AM=M-1",
            "D=M",
            f"@{scoped}",
            "D;JNE",
        ]

    # ---------- 関数定義 / call / return ----------

    def write_function(self, name: str, num_locals: int) -> None:
        """
        function f k
          → (f) ラベル定義
          → ローカル変数k個を0で初期化(push constant 0)
        """
        self.current_function = name  # 以降の label は f$XXX になる
        self.lines.append(f"// function {name} {num_locals}")
        self.lines.append(f"({name})")

        for _ in range(num_locals):
            self._write_push("constant", 0)

    def write_call(self, name: str, num_args: int) -> None:
        """
        call f n
          1. push return-address
          2. push LCL, ARG, THIS, THAT
          3. ARG = SP - 5 - n
          4. LCL = SP
          5. goto f
          6. (return-address)
        """
        return_label = f"{name}$ret.{self.call_count}"
        self.call_count += 1

        self.lines.append(f"// call {name} {num_args}")

        # 1. push return-address
        self.lines += [
            f"@{return_label}",
            "D=A",
            "@SP",
            "A=M",
            "M=D",
            "@SP",
            "M=M+1",
        ]

        # 2. push LCL, ARG, THIS, THAT
        for seg in ("LCL", "ARG", "THIS", "THAT"):
            self.lines += [
                f"@{seg}",
                "D=M",
                "@SP",
                "A=M",
                "M=D",
                "@SP",
                "M=M+1",
            ]

        # 3. ARG = SP - 5 - num_args
        self.lines += [
            "@SP",
            "D=M",
            "@5",
            "D=D-A",
            f"@{num_args}",
            "D=D-A",
            "@ARG",
            "M=D",
        ]

        # 4. LCL = SP
        self.lines += [
            "@SP",
            "D=M",
            "@LCL",
            "M=D",
        ]

        # 5. goto f
        self.lines += [
            f"@{name}",
            "0;JMP",
        ]

        # 6. (return-address)
        self.lines.append(f"({return_label})")

    def write_return(self) -> None:
        """
        return
          FRAME = LCL
          RET = *(FRAME-5)
          *ARG = pop()
          SP = ARG + 1
          THAT = *(FRAME-1)
          THIS = *(FRAME-2)
          ARG  = *(FRAME-3)
          LCL  = *(FRAME-4)
          goto RET
        """
        self.lines.append("// return")

        # FRAME = LCL (R13に保存)
        self.lines += [
            "@LCL",
            "D=M",
            "@R13",
            "M=D",
        ]

        # RET = *(FRAME-5) (R14に保存)
        self.lines += [
            "@5",
            "A=D-A",
            "D=M",
            "@R14",
            "M=D",
        ]

        # *ARG = pop()
        self.lines += [
            "@SP",
            "AM=M-1",
            "D=M",
            "@ARG",
            "A=M",
            "M=D",
        ]

        # SP = ARG + 1
        self.lines += [
            "@ARG",
            "D=M+1",
            "@SP",
            "M=D",
        ]

        # THAT = *(FRAME-1)
        self._restore_segment_from_frame("THAT", 1)
        # THIS = *(FRAME-2)
        self._restore_segment_from_frame("THIS", 2)
        # ARG = *(FRAME-3)
        self._restore_segment_from_frame("ARG", 3)
        # LCL = *(FRAME-4)
        self._restore_segment_from_frame("LCL", 4)

        # goto RET
        self.lines += [
            "@R14",
            "A=M",
            "0;JMP",
        ]

    def _restore_segment_from_frame(self, seg: str, offset: int) -> None:
        """
        FRAME は R13 に入っている前提
          seg = *(FRAME - offset)
        """
        self.lines += [
            "@R13",
            "D=M",
            f"@{offset}",
            "A=D-A",
            "D=M",
            f"@{seg}",
            "M=D",
        ]


# ------------------------------------------------------------
# VM 1ファイル分を翻訳するドライバ関数
# ------------------------------------------------------------

def translate_vm_text(vm_text: str, filename: str, writer: CodeWriter) -> None:
    """
    vm_text: 1つの .vm ファイルの中身
    filename: 拡張子なしファイル名 (static 用)
    writer: 出力先 CodeWriter（複数ファイルで共有）
    """
    parser = Parser(vm_text)
    writer.set_file_name(filename)

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

        elif ctype == CommandType.C_LABEL:
            label = parser.arg1()
            writer.write_label(label)

        elif ctype == CommandType.C_GOTO:
            label = parser.arg1()
            writer.write_goto(label)

        elif ctype == CommandType.C_IF:
            label = parser.arg1()
            writer.write_if(label)

        elif ctype == CommandType.C_FUNCTION:
            name = parser.arg1()
            n_locals = parser.arg2()
            writer.write_function(name, n_locals)

        elif ctype == CommandType.C_CALL:
            name = parser.arg1()
            n_args = parser.arg2()
            writer.write_call(name, n_args)

        elif ctype == CommandType.C_RETURN:
            writer.write_return()

        else:
            raise ValueError("Unexpected command type")


# ------------------------------------------------------------
# main
# ------------------------------------------------------------

def main(argv: List[str]) -> None:
    if len(argv) < 2:
        print("Usage: python vm_translator.py Program.vm | Directory")
        sys.exit(1)

    in_path = argv[1]
    if not os.path.exists(in_path):
        print(f"Input not found: {in_path}")
        sys.exit(1)

    writer = CodeWriter()
    vm_files: List[str] = []

    if os.path.isdir(in_path):
        # ディレクトリ → 中の .vm を全部集めて翻訳
        for name in sorted(os.listdir(in_path)):
            if name.lower().endswith(".vm"):
                vm_files.append(os.path.join(in_path, name))

        if not vm_files:
            print(f"No .vm files found in directory: {in_path}")
            sys.exit(1)

        # 出力: Dir/Dir.asm
        base = os.path.basename(os.path.normpath(in_path))
        out_path = os.path.join(in_path, base + ".asm")

        # ブートストラップ（第8章の仕様）
        writer.write_init()

    else:
        # 単一 .vm ファイル
        if not in_path.lower().endswith(".vm"):
            print("Input must be a .vm file or a directory containing .vm files.")
            sys.exit(1)

        vm_files.append(in_path)
        out_path = os.path.splitext(in_path)[0] + ".asm"

        # 単一ファイルの場合は、第7章互換を優先してブートストラップしない。
        # （必要ならテスト時に writer.write_init() をここで呼ぶ感じ）

    # 各 .vm ファイルを翻訳
    for vm_path in vm_files:
        with open(vm_path, "r", encoding="utf-8") as f:
            vm_text = f.read()
        filename = os.path.splitext(os.path.basename(vm_path))[0]
        writer.lines.append(f"// === {filename}.vm ===")
        translate_vm_text(vm_text, filename, writer)

    # .asm に書き出し
    with open(out_path, "w", encoding="utf-8") as f:
        for line in writer.lines:
            f.write(line + "\n")

    print(f"Wrote: {out_path}")


if __name__ == "__main__":
    main(sys.argv)
