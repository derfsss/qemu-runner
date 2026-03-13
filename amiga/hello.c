/*
 * hello.c — simple test program for the automated workflow
 */

#include <stdio.h>

int main(int argc, char **argv)
{
    printf("Hello from AmigaOS 4 - v2!\n");
    printf("Test 1: basic output ... PASS\n");
    printf("Test 2: arguments argc=%d ... PASS\n", argc);
    printf("Test 3: math 6*7=%d ... %s\n", 6 * 7, (6 * 7 == 42) ? "PASS" : "FAIL");
    printf("Results: 3/3 passed\n");
    return 0;
}
