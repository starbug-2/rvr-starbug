#include "matmult.h"

/* Embench-IoT matmult-int's Multiply(), with one change: the dot product is
   summed in a local instead of in Res[i][j]. Because Res may alias A or B,
   Embench's form makes -O2 store Res on every inner iteration (8 instructions
   per step instead of 7), which would handicap the baseline. Results are
   identical. */
void matmult(const int32_t *A, const int32_t *B, int32_t *Res) {
    for (int i = 0; i < MATMULT_N; i++)
        for (int j = 0; j < MATMULT_N; j++) {
            int32_t sum = 0;
            for (int k = 0; k < MATMULT_N; k++)
                sum += A[i * MATMULT_N + k] * B[k * MATMULT_N + j];
            Res[i * MATMULT_N + j] = sum;
        }
}
