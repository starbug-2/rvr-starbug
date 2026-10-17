/* Measurement harness for the starbug benchmarks.
 *
 * Replaces the original fir_main.c / dp_main.c, which printed all 8192 results.
 * That made ~77% of every measurement printf+semihosting, and forced a
 * "subtract the empty build" baseline that is invalid here (the empty build
 * prints "0" instead of real values, so it does far less printf work).
 *
 * Instead:
 *   - bracket the kernel call with csrr mcycle / minstret, so the kernel cost
 *     is measured directly and no baseline subtraction is needed;
 *   - reduce output to one checksum, so correctness is still verifiable but
 *     I/O cost is ~4 printfs instead of 8192.
 *
 * Build with -DBENCH_FIR, -DBENCH_DP or -DBENCH_MATMULT.
 *
 * A benchmark may also define BENCH_EXPECTED (a known-good output array, e.g.
 * from the benchmark suite it came from); the harness then prints
 * verify=PASS/FAIL in addition to the checksum.
 *
 * Only the low 32 bits of the counters are read. mcycleh/minstreth exist as
 * CSRs but riscv_top.cc:149-155 wires the counters to mcycle/minstret only.
 * 32 bits wraps at 4.29e9; these benchmarks run ~1e6-1e7, so there is headroom.
 */

#include <stdint.h>
#include <stdio.h>

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

/* Order-sensitive checksum: catches wrong values AND wrong ordering/length. */
static uint32_t checksum(const int32_t *p, uint64_t n) {
    uint32_t h = 2166136261u;
    for (uint64_t i = 0; i < n; i++) {
        h ^= (uint32_t)p[i];
        h *= 16777619u;
    }
    return h;
}

#if defined(BENCH_FIR)
#  include "fir_filter.h"
extern uint64_t n_samples, n_taps;
extern int32_t input_signal[], fir_coeffs[], fir_output[];
#  define BENCH_NAME   "fir"
#  define BENCH_N      n_samples
#  define BENCH_OUT    fir_output
#  define BENCH_CALL() fir_filter(input_signal, fir_coeffs, fir_output, n_samples, n_taps)
#elif defined(BENCH_DP)
#  include "dp.h"
extern uint64_t len;
extern int32_t input_1[], input_2[], output[];
#  define BENCH_NAME   "dp"
#  define BENCH_N      len
#  define BENCH_OUT    output
#  define BENCH_CALL() dot_product(input_1, input_2, output, len)
#elif defined(BENCH_MATMULT)
#  include "matmult.h"
extern int32_t matrix_a[], matrix_b[], matrix_res[], matrix_expected[];
#  define BENCH_NAME     "matmult"
#  define BENCH_N        (MATMULT_N * MATMULT_N)
#  define BENCH_OUT      matrix_res
#  define BENCH_EXPECTED matrix_expected
#  define BENCH_CALL()   matmult(matrix_a, matrix_b, matrix_res)
#else
#  error "define BENCH_FIR, BENCH_DP or BENCH_MATMULT"
#endif

int main(void) {
    /* Counters read immediately either side of the kernel. The "memory"
       clobber stops the compiler sinking/hoisting the call across them. */
    uint32_t c0 = rd_mcycle();
    uint32_t i0 = rd_minstret();

    BENCH_CALL();

    uint32_t i1 = rd_minstret();
    uint32_t c1 = rd_mcycle();

    /* Everything below is outside the measured region. */
    uint32_t sum = checksum(BENCH_OUT, BENCH_N);

    printf("bench=%s n=%lu\n", BENCH_NAME, (unsigned long)BENCH_N);
    printf("kernel_cycles=%lu\n", (unsigned long)(c1 - c0));
    printf("kernel_instructions=%lu\n", (unsigned long)(i1 - i0));
    printf("checksum=0x%08lx\n", (unsigned long)sum);
#ifdef BENCH_EXPECTED
    int ok = 1;
    for (uint64_t i = 0; i < (uint64_t)BENCH_N; i++)
        if (BENCH_OUT[i] != BENCH_EXPECTED[i]) { ok = 0; break; }
    printf("verify=%s\n", ok ? "PASS" : "FAIL");
#endif
    return 0;
}
