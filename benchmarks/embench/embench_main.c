/* Driver for running unmodified Embench-IoT benchmarks on the MPACT simulators.
 *
 * Replaces Embench's support/main.c + board files. Same call sequence
 * (initialise_benchmark, warm_caches, benchmark, verify_benchmark), but the
 * start/stop triggers read mcycle/minstret so the timed region is reported
 * directly, and the results are printed over semihosting in the same
 * key=value format as harness/bench_main.c.
 */

#include <stdint.h>
#include <stdio.h>
#include "support.h"

static uint32_t c0, c1, i0, i1;

static inline uint32_t rd_mcycle(void) {
    uint32_t v;
    __asm__ volatile("csrr %0, mcycle" : "=r"(v) : : "memory");
    return v;
}

static inline uint32_t rd_minstret(void) {
    uint32_t v;
    __asm__ volatile("csrr %0, minstret" : "=r"(v) : : "memory");
    return v;
}

void initialise_board(void) {}
void start_trigger(void) { c0 = rd_mcycle(); i0 = rd_minstret(); }
void stop_trigger(void)  { i1 = rd_minstret(); c1 = rd_mcycle(); }

int main(void) {
    volatile int result;
    initialise_board();
    initialise_benchmark();
    warm_caches(WARMUP_HEAT);
    start_trigger();
    result = benchmark();
    stop_trigger();
    int correct = verify_benchmark(result);
    printf("kernel_cycles=%lu\n", (unsigned long)(c1 - c0));
    printf("kernel_instructions=%lu\n", (unsigned long)(i1 - i0));
    printf("verify=%s\n", correct ? "PASS" : "FAIL");
    return !correct;
}
