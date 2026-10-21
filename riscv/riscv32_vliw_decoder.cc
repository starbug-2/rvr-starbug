// Copyright 2023 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "riscv/riscv32_vliw_decoder.h"

#include <array>
#include <cstdint>
#include <string>

#include "absl/log/check.h"
#include "absl/log/log.h"
#include "absl/strings/str_cat.h"
#include "mpact/sim/generic/instruction.h"
#include "mpact/sim/generic/program_error.h"
#include "mpact/sim/generic/type_helpers.h"
#include "mpact/sim/util/memory/memory_interface.h"
#include "riscv/riscv32_vliw_decoder.h"
#include "riscv/riscv32_vliw_encoding.h"
#include "riscv/riscv32g_enums.h"
#include "riscv/riscv_register.h"
#include "riscv/riscv_state.h"

namespace mpact {
namespace sim {
namespace riscv {

using ::mpact::sim::generic::operator*;  // NOLINT: is used below (clang error).

namespace {

StarbugPacketHistogram &ExecutedPackets() {
  static StarbugPacketHistogram histogram;
  return histogram;
}

// creg 0, z 0 is unconditional. creg 1..7 selects x9..x15. z 0 tests nonzero
// and z 1 tests zero. creg 0, z 1 is the reserved encoding and does not fire.
bool PredicateFires(RiscVState *state, uint32_t nibble) {
  const unsigned creg = (nibble >> 1) & 0x7;
  const bool test_zero = (nibble & 0x1) != 0;
  if (creg == 0)
    return !test_zero;
  auto *reg =
      state->GetRegister<RV32Register>(absl::StrCat("x", 8 + creg)).first;
  const uint32_t value = reg->data_buffer()->Get<uint32_t>(0);
  return test_zero ? value == 0 : value != 0;
}

}  // namespace

StarbugPacketHistogram StarbugExecutedPacketHistogram() {
  return ExecutedPackets();
}

RiscV32Decoder::RiscV32Decoder(RiscVState* state, util::MemoryInterface* memory)
    : state_(state), memory_(memory) {
  // Get a handle to the internal error in the program error controller.
  decode_error_ = state->program_error_controller()->GetProgramError(
      generic::ProgramErrorController::kInternalErrorName);

  // Need a data buffer to load instructions from memory. Allocate a single
  // buffer that can be reused for each instruction word.
  inst_db_ = state_->db_factory()->Allocate<uint32_t>(1);
  // Allocate the isa factory class, the top level isa decoder instance, and
  // the encoding parser.
  riscv_isa_factory_ = new RV32IsaFactory();
  riscv_isa_ = new isa32::RiscV32GInstructionSet(state, riscv_isa_factory_);
  riscv_encoding_ = new isa32::RiscV32GEncoding(state);
  decode_error_ = state->program_error_controller()->GetProgramError(
      generic::ProgramErrorController::kInternalErrorName);
}

RiscV32Decoder::~RiscV32Decoder() {
  delete riscv_isa_;
  delete riscv_isa_factory_;
  delete riscv_encoding_;
  inst_db_->DecRef();
}

generic::Instruction* RiscV32Decoder::DecodeInstruction(uint64_t address) {
  return DecodeInstructionAt(address, /*allow_packet=*/true);
}

generic::Instruction* RiscV32Decoder::DecodeExecutePacket(uint64_t address,
                                                          uint32_t iword) {
  // Nibbles are in C6x order: bits 31:28, 27:24, 23:20, 19:16.
  const std::array<uint32_t, 4> preds = {(iword >> 28) & 0xf, (iword >> 24) & 0xf,
                                          (iword >> 20) & 0xf, (iword >> 16) & 0xf};
  // p0 is bit 15, p1 bit 14, p2 bit 13. An operation is in the packet only
  // while the parallel bits form a prefix, so parallel 7/6/4/0 is 4/3/2/1 ops.
  const unsigned parallel = (iword >> 13) & 0x7;
  unsigned nops = 1;
  if ((parallel & 0x4) != 0) {
    nops = 2;
    if ((parallel & 0x2) != 0) {
      nops = 3;
      if ((parallel & 0x1) != 0)
        nops = 4;
    }
  }

  auto *packet = new generic::Instruction(0, state_);
  packet->set_opcode(*isa32::OpcodeEnum::kNone);
  packet->set_address(address);
  packet->SetDisassemblyString("sb2.header");

  uint64_t offset = 4;
  unsigned appended = 0;
  for (unsigned i = 0; i < nops; ++i) {
    generic::Instruction *child =
        DecodeInstructionAt(address + offset, /*allow_packet=*/false);
    packet->AppendChild(child);
    ++appended;
    if (child->size() == 0)
      break;
    offset += child->size();
  }
  packet->set_size(offset);

  RiscVState *state = state_;
  packet->set_semantic_function(
      [preds, appended, state](generic::Instruction *inst) {
        // Every operation reads its sources, including the predicate, at the
        // start of the cycle. A predicate that fails writes nothing. The
        // header and its operations are one fetch, so the top of the simulator
        // charges one cycle for the packet.
        bool fire[4] = {false, false, false, false};
        for (unsigned i = 0; i < appended && i < 4; ++i)
          fire[i] = PredicateFires(state, preds[i]);
        if (appended >= 1 && appended <= 4)
          ExecutedPackets().width[appended]++;
        generic::Instruction *child = inst->child();
        unsigned i = 0;
        while (child != nullptr && i < appended) {
          if (fire[i])
            child->Execute();
          child = child->next();
          ++i;
        }
      });
  return packet;
}

generic::Instruction* RiscV32Decoder::DecodeInstructionAt(uint64_t address,
                                                          bool allow_packet) {
  // First check that the address is aligned properly. If not, create and return
  // an instruction object that will raise an exception.
  if (address & 0x1) {
    auto* inst = new generic::Instruction(0, state_);
    inst->set_size(1);
    inst->SetDisassemblyString("Misaligned instruction address");
    inst->set_opcode(*isa32::OpcodeEnum::kNone);
    inst->set_address(address);
    inst->set_semantic_function([this](generic::Instruction* inst) {
      state_->Trap(/*is_interrupt*/ false, inst->address(),
                   *ExceptionCode::kInstructionAddressMisaligned,
                   inst->address() ^ 0x1, inst);
    });
    return inst;
  }

  // If the address is greater than the max address, return an instruction
  // that will raise an exception.
  if (address > state_->max_physical_address()) {
    auto* inst = new generic::Instruction(0, state_);
    inst->set_size(0);
    inst->SetDisassemblyString("Instruction access fault");
    inst->set_opcode(*isa32::OpcodeEnum::kNone);
    inst->set_address(address);
    inst->set_semantic_function([this](generic::Instruction* inst) {
      state_->Trap(/*is_interrupt*/ false, inst->address(),
                   *ExceptionCode::kInstructionAccessFault, inst->address(),
                   nullptr);
    });
    return inst;
  }

  // Read the instruction word from memory and parse it in the encoding parser.
  memory_->Load(address, inst_db_, nullptr, nullptr);
  uint32_t iword = inst_db_->Get<uint32_t>(0);

  // Custom-0 execute-packet header, major opcode 0001011. The ISA decoder has
  // no encoding for it and would trap. A header plus the operations selected
  // by p0..p2 is one instruction, so the simulator charges one cycle.
  if (allow_packet && (iword & 0x7f) == 0x0b)
    return DecodeExecutePacket(address, iword);

  riscv_encoding_->ParseInstruction(iword);

  // Call the isa decoder to obtain a new instruction object for the instruction
  // word that was parsed above.
  auto* instruction = riscv_isa_->Decode(address, riscv_encoding_);

  // Search for VLIW START Hint Inst
  LOG(INFO) << "opcode detected: " << instruction->opcode() << " name " << isa32::kOpcodeNames[instruction->opcode()];
  if (instruction->opcode() == *isa32::OpcodeEnum::kCliHint) {
    uint32_t vliw_inst = instruction->Source(1)->AsUint32(0);
    LOG(INFO) << "HINT detected. Expecting " << vliw_inst << " VLIW inst.";

    uint64_t offset = 2; // Start offset by 2 bytes of the HINT
    uint8_t instructions_left = vliw_inst;
    while (instructions_left > 0) {
      generic::Instruction *child =
          DecodeInstructionAt(address + offset, /*allow_packet=*/true);
      LOG(INFO) << " -> appending child opcode: " << child->opcode() << " name " << isa32::kOpcodeNames[child->opcode()];
      instruction->AppendChild(child);
      offset += child->size();
      instructions_left--;
    }
    LOG(INFO) << "Resizing parent inst to: " << offset;
    instruction->set_size(offset);
    LOG(INFO) << "Returning parent inst:" << instruction->opcode() << " name " << isa32::kOpcodeNames[instruction->opcode()];
  }
  return instruction;
}

}  // namespace riscv
}  // namespace sim
}  // namespace mpact
