"""
File: x86 implementation of the test case generator

Copyright (C) Microsoft Corporation
SPDX-License-Identifier: MIT
"""
import abc
import math
import re
import random
from typing import List, Dict, Set, Optional
from subprocess import run

from isa_loader import InstructionSet
from interfaces import TestCase, Operand, RegisterOperand, FlagsOperand, MemoryOperand, \
    ImmediateOperand, AgenOperand, LabelOperand, OT, Instruction, BasicBlock, InstructionSpec
from generator import ConfigurableGenerator, RandomGenerator, Pass, \
     parser_assert, Printer, GeneratorException, AsmParserException
from x86.x86_target_desc import X86TargetDesc
from config import CONF


class X86Generator(ConfigurableGenerator, abc.ABC):
    asm_prefixes = ["LOCK", "REX", "REP", "REPE", "REPNE"]
    asm_synonyms = {
        "JE": "JZ",
        "JNE": "JNZ",
        "JNAE": "JB",
        "JC": "JB",
        "JAE": "JNB",
        "JNC": "JNB",
        "JNA": "JBE",
        "JA": "JNBE",
        "JNGE": "JL",
        "JGE": "JNL",
        "JNG": "JLE",
        "JG": "JNLE",
        "JPE": "JP",
        "JPO": "JNP",
        "CMOVE": "CMOVZ",
        "CMOVNE": "CMOVNZ",
        "CMOVNAE": "CMOVB",
        "CMOVC": "CMOVB",
        "CMOVAE": "CMOVNB",
        "CMOVNC": "CMOVNB",
        "CMOVNA": "CMOVBE",
        "CMOVA": "CMOVNBE",
        "CMOVNGE": "CMOVL",
        "CMOVGE": "CMOVNL",
        "CMOVNG": "CMOVLE",
        "CMOVG": "CMOVNLE",
        "CMOVPE": "CMOVP",
        "CMOVPO": "CMOVNP",
        "SETE": "SETZ",
        "SETNE": "SETNZ",
        "SETNAE": "SETB",
        "SETC": "SETB",
        "SETAE": "SETNB",
        "SETNC": "SETNB",
        "SETNA": "SETBE",
        "SETA": "SETNBE",
        "SETNGE": "SETL",
        "SETGE": "SETNL",
        "SETNG": "SETLE",
        "SETG": "SETNLE",
        "SETPE": "SETP",
        "SETPO": "SETNP",
    }
    memory_sizes = {"BYTE": 8, "WORD": 16, "DWORD": 32, "QWORD": 64}

    def __init__(self, instruction_set: InstructionSet):
        super(X86Generator, self).__init__(instruction_set)
        self.passes = [
            X86SandboxPass(),
            X86PatchUndefinedFlagsPass(self.instruction_set, self),
            X86PatchUndefinedResultPass(),
        ]
        self.printer = X86Printer()
        self.target_desc = X86TargetDesc()

    def map_addresses(self, test_case: TestCase, bin_file: str) -> None:
        # get a list of relative instruction addresses
        dump = run(
            f"objdump --no-show-raw-insn -D -M intel -b binary -m i386:x86-64 {bin_file} "
            "| awk '/ [0-9a-f]+:/{print $1}'",
            shell=True,
            check=True,
            capture_output=True)
        address_list = [int(addr[:-1], 16) for addr in dump.stdout.decode().split("\n") if addr]

        # connect them with instructions in the test case
        address_map: Dict[int, Instruction] = {}
        counter = test_case.num_prologue_instructions
        for func in test_case.functions:
            for bb in func:
                for inst in list(bb) + bb.terminators:
                    address = address_list[counter]
                    address_map[address] = inst
                    counter += 1

        # map prologue and epilogue to dummy instructions
        for address in address_list:
            if address not in address_map:
                address_map[address] = Instruction("UNMAPPED", True)

        test_case.address_map = address_map

    def get_return_instruction(self) -> Instruction:
        return Instruction("RET", False, "", True)

    def get_unconditional_jump_instruction(self) -> Instruction:
        return Instruction("JMP", False, "UNCOND_BR", True)

    def parse_line(self, line: str, line_num: int,
                   instruction_map: Dict[str, List[InstructionSpec]]) -> Instruction:
        line = line.upper()

        # get name and possible specs
        words = line.split()
        name = ""
        specs: List[InstructionSpec] = []
        for word in words:
            if word in self.asm_prefixes:
                name += word + " "
                continue

            # fix jump name
            if word in self.asm_synonyms:
                key = name + self.asm_synonyms[word]
            else:
                key = name + word
            specs = instruction_map.get(key, [])
            name += word
            break
        if not specs:
            raise AsmParserException(line_num, f"Unknown instruction {line}")

        # instrumentation?
        is_instrumentation = line.endswith("# INSTRUMENTATION")

        # remove comments
        if "#" in line:
            line = re.search(r"(.*)#.*", line).group(1).strip()  # type: ignore

        # extract operands
        operands_raw = line.removeprefix(name).split(",")
        if operands_raw == [""]:  # no operands
            operands_raw = []
        else:  # clean the operands
            operands_raw = [o.strip() for o in operands_raw]

        # find a matching spec
        matching_specs = []
        for spec_candidate in specs:
            if len(spec_candidate.operands) != len(operands_raw):
                continue

            match = True
            for op_id, op_raw in enumerate(operands_raw):
                op_spec = spec_candidate.operands[op_id]
                if op_raw[0] == ".":  # match label
                    if op_spec.type != OT.LABEL:
                        match = False
                        break
                    continue
                elif "[" in op_raw:  # match address
                    if op_spec.type not in [OT.AGEN, OT.MEM]:
                        match = False
                        break
                    access_size = op_raw.split()[0]  # match address size
                    parser_assert(access_size in self.memory_sizes, line_num,
                                  f"Pointer size must be declared explicitly in {line}")
                    if op_spec.width != self.memory_sizes[access_size]:
                        match = False
                        break
                    continue
                # match immediate value
                elif re.match(r"^-?[0-9]+$", op_raw) or \
                        re.match(r"^-?0X[0-9ABCDEF]+$", op_raw) or \
                        re.match(r"^-?0B[01]+$", op_raw) or \
                        re.match(r"^-?[0-9]+\ *[+-]\ *[0-9]+$", op_raw):
                    if op_spec.type != OT.IMM:
                        match = False
                        break
                    continue
                elif op_spec.type == OT.REG:
                    if op_raw not in self.target_desc.registers[op_spec.width]:
                        match = False
                        break
                    continue
                else:
                    match = False
            if match:
                matching_specs.append(spec_candidate)
        parser_assert(
            len(matching_specs) != 0, line_num, f"Could not find a matching spec for {line}")

        # we might find several matches if the instruction has a magic operand value
        if len(matching_specs) > 1:
            magic_value_specs = list(filter(lambda x: (x.has_magic_value), matching_specs))
            if magic_value_specs:
                matching_specs = magic_value_specs

        # at this point we should have only one spec, but even if we don't, all of them should
        # be equivalent. Just pick the first
        spec: InstructionSpec = matching_specs[0]

        # generate a corresponding Instruction
        inst = Instruction.from_spec(spec, is_instrumentation)
        op: Operand
        for op_id, op_raw in enumerate(operands_raw):
            op_spec = spec.operands[op_id]
            if op_spec.type == OT.REG:
                op = RegisterOperand(op_raw, op_spec.width, op_spec.src, op_spec.dest)
            elif op_spec.type == OT.MEM:
                address_match = re.search(r'\[(.*)\]', op_raw)
                parser_assert(address_match is not None, line_num, "Invalid memory address")
                address = address_match.group(1)  # type: ignore
                op = MemoryOperand(address, op_spec.width, op_spec.src, op_spec.dest)
            elif op_spec.type == OT.IMM:
                op = ImmediateOperand(op_raw, op_spec.width)
            elif op_spec.type == OT.LABEL:
                assert spec.control_flow
                op = LabelOperand(op_raw)
            else:  # AGEN
                op = AgenOperand(op_raw, op_spec.width)
            inst.operands.append(op)

        for op_spec in spec.implicit_operands:
            op = self.generate_operand(op_spec, inst)
            inst.implicit_operands.append(op)

        return inst


class X86LFENCEPass(Pass):

    def run_on_test_case(self, test_case: TestCase) -> None:
        for func in test_case.functions:
            for bb in func:
                insertion_points = []
                for instr in bb:
                    # make a copy to avoid infinite insertions
                    insertion_points.append(instr)

                for instr in insertion_points:
                    bb.insert_after(instr, Instruction("LFENCE", True))


class X86SandboxPass(Pass):
    mask_3bits = "0b111"
    bit_test_names = ["BT", "BTC", "BTR", "BTS", "LOCK BT", "LOCK BTC", "LOCK BTR", "LOCK BTS"]

    def __init__(self):
        super().__init__()
        input_memory_size = CONF.input_main_region_size + CONF.input_faulty_region_size
        mask_size = int(math.log(input_memory_size, 2)) - CONF.memory_access_zeroed_bits
        self.sandbox_address_mask = "0b" + "1" * mask_size + "0" * CONF.memory_access_zeroed_bits

    def run_on_test_case(self, test_case: TestCase) -> None:
        for func in test_case.functions:
            for bb in func:
                if bb == func.entry:
                    continue

                # collect all instructions that require sandboxing
                memory_instructions = []
                divisions = []
                bit_tests = []
                repeated_instructions = []
                corrupted_cf = []
                for inst in bb:
                    if inst.has_mem_operand(True):
                        memory_instructions.append(inst)
                    if inst.name in ["DIV", "REX DIV"]:
                        divisions.append(inst)
                    elif inst.name in self.bit_test_names:
                        bit_tests.append(inst)
                    elif "REP" in inst.name:
                        repeated_instructions.append(inst)
                    elif inst.category == "BASE-ROTATE" or inst.category == "BASE-SHIFT":
                        corrupted_cf.append(inst)

                # sandbox them
                for inst in memory_instructions:
                    self.sandbox_memory_access(inst, bb)

                for inst in divisions:  # must be after memory accesses
                    self.sandbox_division(inst, bb)

                for inst in bit_tests:
                    self.sandbox_bit_test(inst, bb)

                for inst in repeated_instructions:
                    self.sandbox_repeated_instruction(inst, bb)

                for inst in corrupted_cf:
                    self.sandbox_corrupted_cf(inst, bb)

    def sandbox_memory_access(self, instr: Instruction, parent: BasicBlock):
        """ Force the memory accesses into the page starting from R14 """
        mem_operands = instr.get_mem_operands()
        implicit_mem_operands = instr.get_implicit_mem_operands()
        if mem_operands and not implicit_mem_operands:
            assert len(mem_operands) == 1, f"Unexpected instruction format {instr.name}"
            mem_operand: Operand = mem_operands[0]
            address_reg = mem_operand.value
            imm_width = mem_operand.width if mem_operand.width <= 32 else 32
            apply_mask = Instruction("AND", True) \
                .add_op(RegisterOperand(address_reg, mem_operand.width, True, True)) \
                .add_op(ImmediateOperand(self.sandbox_address_mask, imm_width))
            parent.insert_before(instr, apply_mask)
            instr.get_mem_operands()[0].value = "R14 + " + address_reg
            return

        mem_operands = implicit_mem_operands
        if mem_operands:
            assert len(mem_operands) == 1, f"Unexpected instruction format {instr.name}"
            mem_operand = mem_operands[0]
            address_reg = mem_operand.value
            imm_width = mem_operand.width if mem_operand.width <= 32 else 32
            apply_mask = Instruction("AND", True) \
                .add_op(RegisterOperand(address_reg, mem_operand.width, True, True)) \
                .add_op(ImmediateOperand(self.sandbox_address_mask, imm_width))
            add_base = Instruction("ADD", True) \
                .add_op(RegisterOperand(address_reg, mem_operand.width, True, True)) \
                .add_op(RegisterOperand("R14", 64, True, False))
            parent.insert_before(instr, apply_mask)
            parent.insert_before(instr, add_base)
            return

        raise GeneratorException("Attempt to sandbox an instruction without memory operands")

    def sandbox_division(self, inst: Instruction, parent: BasicBlock):
        """
        We do not support handling of division faults so far, so we have to prevent them.
        Specifically, we need to prevent two types of faults:
        - division by zero
        - division overflow (i.e., quotient is larger than the destination register)
        For this, we ensure that the *D register (upper half of the dividend) is always
        less than the divisor with a bit trick like this ( D & divisor >> 1).

        The first corner case when it won't work is when the divisor is D. This case
        is impossible to resolve, as far as I can tell. We just give up.

        The second corner case is 8-bit division, when the divisor is the AX register alone.
        Here the instrumentation become too complicated, and we simply set AX to 1.
        """
        divisor = inst.operands[0]

        # make sure the divisor is not zero
        instrumentation = Instruction("OR", True).\
            add_op(divisor).\
            add_op(ImmediateOperand("1", 8))
        parent.insert_before(inst, instrumentation)

        # dividend in AX?
        if divisor.width == 8:
            if "RAX" not in divisor.value:
                instrumentation = Instruction("MOV", True).\
                    add_op(RegisterOperand("AX", 16, False, True)).\
                    add_op(ImmediateOperand("1", 16))
                parent.insert_before(inst, instrumentation)
                return
            else:
                # AX is both the dividend and the offset in memory.
                # Too complex (impossible?). Giving up
                parent.delete(inst)
                return

        # TODO: remove me - avoids a certain violation
        if divisor.width == 64 and CONF.x86_disable_div64:
            parent.delete(inst)
            return

        # divisor in D or in memory with RDX offset? Impossible case, give up
        if divisor.value in ["RDX", "EDX", "DX", "DH", "DL"] or "RDX" in divisor.value:
            parent.delete(inst)
            return

        # Normal case
        # D = (D & divisor) >> 1
        d_register = {64: "RDX", 32: "EDX", 16: "DX"}[divisor.width]
        instrumentation = Instruction("AND", True).\
            add_op(RegisterOperand(d_register, divisor.width, False, True)).\
            add_op(divisor)
        parent.insert_before(inst, instrumentation)
        instrumentation = Instruction("SHR", True).\
            add_op(RegisterOperand(d_register, divisor.width, False, True)).\
            add_op(ImmediateOperand("1", 8))
        parent.insert_before(inst, instrumentation)

    def sandbox_bit_test(self, inst: Instruction, parent: BasicBlock):
        """
        The address accessed by a BT* instruction is based on both of its operands.
        `sandbox_memory_access` take care of the first operand.
        This function ensures that the offset is always within a byte.
        """
        address = inst.operands[0]
        if isinstance(address, RegisterOperand):
            # this is a version that does not access memory
            # no need for sandboxing
            return

        offset = inst.operands[1]
        if isinstance(offset, ImmediateOperand):
            # The offset is an immediate
            # Simply replace it with a smaller value
            offset.value = str(random.randint(0, 7))
            return

        # The offset is in a register
        # Mask its upper bits to reduce the stored value to at most 7
        if address.value != offset.value:
            apply_mask = Instruction("AND", True) \
                .add_op(offset) \
                .add_op(ImmediateOperand(self.mask_3bits, 8))
            parent.insert_before(inst, apply_mask)
            return

        # Special case: offset and address use the same register
        # Sandboxing is impossible. Give up
        parent.delete(inst)

    def sandbox_repeated_instruction(self, inst: Instruction, parent: BasicBlock):
        apply_mask = Instruction("AND", True) \
            .add_op(RegisterOperand("RCX", 64, True, True)) \
            .add_op(ImmediateOperand("0xff", 8))
        add_base = Instruction("ADD", True) \
            .add_op(RegisterOperand("RCX", 64, True, True)) \
            .add_op(ImmediateOperand("1", 1))
        parent.insert_before(inst, apply_mask)
        parent.insert_before(inst, add_base)

    def sandbox_corrupted_cf(self, inst: Instruction, parent: BasicBlock):
        set_cf = Instruction("STC", True)
        parent.insert_after(inst, set_cf)

    @staticmethod
    def requires_sandbox(inst: InstructionSpec):
        if inst.has_mem_operand:
            return True
        if inst.name in ["DIV", "REX DIV"]:
            return True
        if inst.name in ["BT", "BTC", "BTR", "BTS", "LOCK BT", "LOCK BTC", "LOCK BTR", "LOCK BTS"]:
            return True
        if inst.category in ["BASE-SHIFT", "BASE-ROTATE"]:
            return True
        return False


class X86PatchUndefinedFlagsPass(Pass):
    """
    Some instructions have undefined effect on FLAGS (e.g., SHL may or may not overwrite OF).
    This causes a mismatch between Model execution and Executor, if the undefined behavior
    is implemented differently. It leads to false positives.
    To prevent them, we analyse the test cases in search for the cases where an instruction
    with undefined flags is followed by an instruction that uses this flag. We then
    insert another random instruction in-between, such that this
    instruction overwrites the undefined flag.

    I.e., we replace
        SHL eax, eax  // undefined OF
        JNO .label    // uses OF
    with
        SHL eax, eax
        ADD ebx, ecx  // random instruction that overwrites OF
        JNO .label
    """
    patch_candidates: List[InstructionSpec]

    def __init__(self, instruction_set: InstructionSet, generator: ConfigurableGenerator):
        super().__init__()
        self.instruction_set = instruction_set
        self.generator = generator

        self.patch_candidates = []
        for instruction_spec in instruction_set.instructions:
            # we don't want to change the control flow
            if instruction_spec.control_flow:
                continue

            # check if the instruction is safe to use on its own
            if X86SandboxPass.requires_sandbox(instruction_spec):
                continue

            # check if it overwrites flags and if creates new dependencies
            has_read = False
            has_write = False
            for op in instruction_spec.operands + instruction_spec.implicit_operands:
                if op.type == OT.FLAGS:
                    for f in op.values:
                        if f in ['r', 'r/w', 'r/cw']:
                            has_read = True
                        elif f in ['w']:
                            has_write = True
            if not has_read and has_write:
                self.patch_candidates.append(instruction_spec)

        if not self.patch_candidates:
            raise GeneratorException("The instruction set is insufficient to patch undef flags")

    def run_on_test_case(self, test_case: TestCase) -> None:
        for func in test_case.functions:
            for bb in func:
                # get a list of all instructions in the BB
                all_instructions: List[Instruction] = []
                for inst in bb:
                    all_instructions.append(inst)
                if len(bb.terminators) == 2:  # include conditional terminators
                    all_instructions.append(bb.terminators[0])

                # keep track of the FLAGS dependencies as we iterate over instructions
                flags_to_set: Set[str] = set()

                # walk the list in inverse order
                while all_instructions:
                    inst = all_instructions.pop()
                    flags: Optional[FlagsOperand] = inst.get_flags_operand()
                    if not flags:
                        continue

                    # fix undefined flags by adding another instruction in-between
                    undef_flags = [i for i in flags.get_undef_flags() if i in flags_to_set]
                    if undef_flags:
                        patches = self.find_flags_patch(undef_flags, flags_to_set)
                        for patch in patches:
                            bb.insert_after(inst, patch)
                            patch.is_instrumentation = True
                            # remove the flags overwritten by the patch
                            for f in patch.get_flags_operand().get_write_flags():  # type: ignore
                                flags_to_set.discard(f)

                    # remove the flags overwritten by the instruction
                    for f in flags.get_write_flags():
                        flags_to_set.discard(f)

                    # add new flag dependencies
                    for f in flags.get_read_flags():
                        flags_to_set.add(f)

                # make sure that we do not have undefined flags when we enter the BB
                if flags_to_set:
                    patches = self.find_flags_patch(list(flags_to_set), flags_to_set)
                    for patch in patches:
                        bb.insert_before(bb.get_first(), patch)
                        patch.is_instrumentation = True

    def find_flags_patch(self, undef_flags, flags_to_set) -> List[Instruction]:
        """
        Find an instruction that would overwrite the undefined flags
        """
        patches: List[Instruction] = []
        for instruction_spec in self.patch_candidates:
            patch = self.generator.generate_instruction(instruction_spec)
            patch_flags = patch.get_flags_operand()
            assert patch_flags
            new_undef_flags = [i for i in patch_flags.get_undef_flags() if i in flags_to_set]
            not_patched_flags = [i for i in undef_flags if i not in patch_flags.get_write_flags()]

            if not new_undef_flags and not_patched_flags != undef_flags:
                patches.append(patch)
                undef_flags = not_patched_flags
                if not undef_flags:
                    break

        if undef_flags:
            raise GeneratorException(f"Could not find an instruction to patch flags {undef_flags}")

        return patches


class X86PatchUndefinedResultPass(Pass):

    def run_on_test_case(self, test_case: TestCase) -> None:
        for func in test_case.functions:
            for bb in func:
                if bb == func.entry:
                    continue

                # collect all instructions that require patching
                bit_scan = []
                for inst in bb:
                    if inst.name in ["BSF", "BSR"]:
                        bit_scan.append(inst)

                # patch them
                for inst in bit_scan:
                    self.patch_bit_scan(inst, bb)

    @staticmethod
    def patch_bit_scan(inst: Instruction, parent: BasicBlock):
        """
        Bit Scan instructions give an undefined result when the source operand is zero.
        To avoid it, set the most significant bit.
        """
        source = inst.operands[1]
        mask = bin(1 << (source.width - 1))
        mask_size = source.width
        if source.width in [64, 32]:
            mask = "0b1000000000000000000000000000000"
            mask_size = 32
        apply_mask = Instruction("OR", True) \
            .add_op(source) \
            .add_op(ImmediateOperand(mask, mask_size))
        parent.insert_before(inst, apply_mask)


class X86Printer(Printer):
    memory_prefixes = {8: "byte ptr", 16: "word ptr", 32: "dword ptr", 64: "qword ptr", 512: ""}
    prologue_template = [
        ".intel_syntax noprefix\n",
        "MFENCE # instrumentation\n",
        ".test_case_enter:\n",
    ]
    epilogue_template = [
        ".test_case_exit:\n",
        "MFENCE # instrumentation\n",
    ]

    def print(self, test_case: TestCase, outfile: str) -> None:
        with open(outfile, "w") as f:
            # print prologue
            for line in self.prologue_template:
                f.write(line)

            # print the test case
            for func in test_case.functions:
                f.write(f"{func.name}:\n")
                for bb in func:
                    self.print_basic_block(bb, f)

            # print epilogue
            for line in self.epilogue_template:
                f.write(line)

        for i in self.prologue_template:
            if i[0] != ".":
                test_case.num_prologue_instructions += 1

    def print_basic_block(self, bb: BasicBlock, file):
        file.write(f"{bb.name}:\n")
        for inst in bb:
            file.write(self.instruction_to_str(inst) + "\n")
        for inst in bb.terminators:
            file.write(self.instruction_to_str(inst) + "\n")

    def instruction_to_str(self, inst: Instruction):
        operands = ", ".join([self.operand_to_str(op) for op in inst.operands])
        comment = "# instrumentation" if inst.is_instrumentation else ""
        return f"{inst.name} {operands} {comment}"

    def operand_to_str(self, op: Operand) -> str:
        if isinstance(op, MemoryOperand) or isinstance(op, AgenOperand):
            prefix = self.memory_prefixes[op.width]
            return f"{prefix} [{op.value}]"

        return op.value


class X86RandomGenerator(X86Generator, RandomGenerator):

    def __init__(self, instruction_set: InstructionSet):
        super().__init__(instruction_set)
