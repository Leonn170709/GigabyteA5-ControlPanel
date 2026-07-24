obj-m += a5ctl.o
KDIR ?= /lib/modules/$(shell uname -r)/build

# A module must be built by the same compiler as the kernel. CachyOS-style kernels are
# clang-built and need LLVM=1, Arch/Fedora/Ubuntu are gcc. Detect, don't hardcode either.
LLVM := $(shell grep -q '^CONFIG_CC_IS_CLANG=y' $(KDIR)/include/config/auto.conf 2>/dev/null && echo 1)
export LLVM

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
