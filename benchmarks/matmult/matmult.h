#ifndef MATMULT_H
#define MATMULT_H

#include <stdint.h>

/* Matrix size, fixed to match Embench-IoT matmult-int (UPPERLIMIT). The
   hand-scheduled .S variants hardcode this; change all of them together. */
#define MATMULT_N 20

/* Res = A * B for row-major MATMULT_N x MATMULT_N int32 matrices. */
extern void matmult(const int32_t *A, const int32_t *B, int32_t *Res);

#endif // MATMULT_H
