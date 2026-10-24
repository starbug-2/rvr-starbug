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

#include <cstdint>
#include <string>

#include "absl/log/check.h"
#include "absl/log/log.h"
#include "mpact/sim/generic/instruction.h"
#include "mpact/sim/generic/program_error.h"
#include "mpact/sim/generic/type_helpers.h"
#include "mpact/sim/util/memory/memory_interface.h"
#include "riscv/riscv32_vliw_decoder.h"
#include "riscv/riscv32_vliw_encoding.h"
#include "riscv/riscv32g_enums.h"
#include "riscv/riscv_state.h"

namespace mpact {
namespace sim {
namespace riscv {

using ::mpact::sim::generic::operator*;  // NOLINT: is used below (clang error).

namespace {

// STARBUG2 predicate: creg 1-7 selects a register from the state's predicate
// list (x9-x15 by default, --starbug_pred_regs); z = 0 enables the operation
// when the register is nonzero, z = 1 when it is zero.
class StarbugPredicate : public generic::PredicateOperandInterface {
 public:
  StarbugPredicate(RiscVState* state, const std::string& reg, bool z)
      : reg_(state->GetRegister<RV32Register>(reg).first),
        z_(z),
        name_(std::string(z ? "!" : "") + reg) {}
  bool Value() override { return (reg_->data_buffer()->Get<uint32_t>(0) != 0) != z_; }
  std::string AsString() const override { return name_; }

 private:
  RV32Register* reg_;
  bool z_;
  std::string name_;
};

// Header layout (custom-0, bits 6:0 = 0001011):
//   31:16  creg/z nibble per operation 0-3 (creg in the upper 3 bits)
//   15:13  4-way form: thermometer count, ops = 1 + leading ones (p0 p1 p2)
//          wide form:  binary count, ops = value + 1 (1-8)
//   12     p3, spare
//   11:8   reserved
//   7      W: wide form; a second word follows whose bits 31:16 hold the
//          creg/z nibbles of operations 4-7 (its bits 6:0 are also custom-0)
constexpr uint32_t kStarbugOpcode = 0b000'1011;

int StarbugOpCount(uint32_t hdr) {
  if ((hdr >> 7) & 1) return static_cast<int>((hdr >> 13) & 7) + 1;
  int p0 = (hdr >> 15) & 1, p1 = (hdr >> 14) & 1, p2 = (hdr >> 13) & 1;
  if (!p0) return (p1 || p2) ? -1 : 1;   // non-thermometer patterns are illegal
  if (!p1) return p2 ? -1 : 2;
  return p2 ? 4 : 3;
}

}  // namespace

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
  riscv_encoding_->ParseInstruction(iword);

  // Call the isa decoder to obtain a new instruction object for the instruction
  // word that was parsed above.
  auto* instruction = riscv_isa_->Decode(address, riscv_encoding_);

  // STARBUG2 execute-packet header: attach the packet's operations.
  if (instruction->opcode() == *isa32::OpcodeEnum::kStarbugHdr &&
      (iword & 0x7f) == kStarbugOpcode) {
    uint32_t words[2] = {iword, 0};
    uint64_t offset = 4;
    if ((iword >> 7) & 1) {
      memory_->Load(address + 4, inst_db_, nullptr, nullptr);
      words[1] = inst_db_->Get<uint32_t>(0);
      offset = 8;
    }
    int count = StarbugOpCount(iword);
    if (count < 0) {
      instruction->set_semantic_function([this](generic::Instruction* inst) {
        state_->Trap(/*is_interrupt*/ false, 0, *ExceptionCode::kIllegalInstruction,
                     inst->address(), inst);
      });
      return instruction;
    }
    for (int i = 0; i < count; i++) {
      generic::Instruction* child = DecodeInstruction(address + offset);
      uint32_t nibble = (words[i / 4] >> (28 - 4 * (i % 4))) & 0xf;
      int creg = nibble >> 1;
      bool z = nibble & 1;
      const auto& regs = state_->starbug_pred_regs();
      if (creg != 0 && creg <= static_cast<int>(regs.size())) {
        child->SetPredicate(new StarbugPredicate(state_, regs[creg - 1], z));
      }
      instruction->AppendChild(child);
      offset += child->size();
    }
    instruction->set_size(offset);
    return instruction;
  }

  // Search for VLIW START Hint Inst
  LOG(INFO) << "opcode detected: " << instruction->opcode() << " name " << isa32::kOpcodeNames[instruction->opcode()];
  if (instruction->opcode() == *isa32::OpcodeEnum::kCliHint) {
    uint32_t vliw_inst = instruction->Source(1)->AsUint32(0);
    LOG(INFO) << "HINT detected. Expecting " << vliw_inst << " VLIW inst.";

    uint64_t offset = 2; // Start offset by 2 bytes of the HINT
    uint8_t instructions_left = vliw_inst;
    while (instructions_left > 0) {
      generic::Instruction *child = DecodeInstruction(address + offset);
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
