ENTRY(_start)

MEMORY
{
  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 16M
}

SECTIONS
{
  . = ORIGIN(RAM);

  .text : {
    *(.text.init)
    *(.text)
    *(.text.*)
  } > RAM

  .rodata : {
    *(.rodata)
    *(.rodata.*)
    *(.srodata)
    *(.srodata.*)
  } > RAM

  /* Constructor/destructor tables. Without these, libgloss's semihosting
     file-descriptor table is never initialized and every write() silently
     returns -1. */
  .preinit_array : {
    PROVIDE_HIDDEN(__preinit_array_start = .);
    KEEP(*(.preinit_array))
    PROVIDE_HIDDEN(__preinit_array_end = .);
  } > RAM

  .init_array : {
    PROVIDE_HIDDEN(__init_array_start = .);
    KEEP(*(SORT_BY_INIT_PRIORITY(.init_array.*)))
    KEEP(*(.init_array))
    PROVIDE_HIDDEN(__init_array_end = .);
  } > RAM

  .fini_array : {
    PROVIDE_HIDDEN(__fini_array_start = .);
    KEEP(*(SORT_BY_INIT_PRIORITY(.fini_array.*)))
    KEEP(*(.fini_array))
    PROVIDE_HIDDEN(__fini_array_end = .);
  } > RAM

  .data : {
    *(.data)
    *(.data.*)
    . = ALIGN(8);
    __global_pointer$ = . + 0x800;
    *(.sdata)
    *(.sdata.*)
  } > RAM

  .bss (NOLOAD) : {
    __bss_start = .;
    *(.sbss)
    *(.sbss.*)
    *(.bss)
    *(.bss.*)
    *(COMMON)
    __bss_end = .;
    _end = .;
  } > RAM
}
