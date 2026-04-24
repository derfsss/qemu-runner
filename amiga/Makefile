CC = ppc-amigaos-gcc
CFLAGS = -O2 -Wall
LDFLAGS = -lauto

# Build date stamped into the $VER cookie (DD.MM.YYYY, Amiga Version format).
BUILD_DATE := $(shell date +%d.%m.%Y)
SERIALSHELL_CFLAGS = -DSERIALSHELL_DATE=\"$(BUILD_DATE)\"

# AmiUpdate integration — only pulled in when WITH_AMIUPDATE=1.
# The `serialshell-full` target sets this via recursive make.
AMIUPDATE_DIR ?= ../../../../AmiUpdateIntegration
WITH_AMIUPDATE ?= 0

ifeq ($(WITH_AMIUPDATE),1)
include $(AMIUPDATE_DIR)/amiupdate.mk
SERIALSHELL_EXTRA_OBJS   = $(AMIUPDATE_OBJ)
SERIALSHELL_EXTRA_CFLAGS = $(AMIUPDATE_CFLAGS) -DSERIALSHELL_AMIUPDATE
endif

# Distribution
DIST_DIR = dist
DIST_NAME = SerialShell
VERSION = 1.3

.PHONY: all serialshell-full clean dist dist-lha

# Default target: standalone serialshell (no AmiUpdate dependency) + hello.
all: serialshell hello

serialshell: serialshell.c $(SERIALSHELL_EXTRA_OBJS)
	$(CC) $(CFLAGS) $(SERIALSHELL_CFLAGS) $(SERIALSHELL_EXTRA_CFLAGS) -o $@ $^ $(LDFLAGS)

# Build serialshell with AmiUpdate integration (requires AmiUpdateIntegration
# checkout at $(AMIUPDATE_DIR)). Re-invokes make with WITH_AMIUPDATE=1 so the
# conditional include above kicks in.
serialshell-full:
	$(MAKE) WITH_AMIUPDATE=1 serialshell

hello: hello.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

# Distribution requires the AmiUpdate-integrated build.
dist: serialshell-full
	rm -rf $(DIST_DIR)/$(DIST_NAME)
	mkdir -p $(DIST_DIR)/$(DIST_NAME)
	cp serialshell $(DIST_DIR)/$(DIST_NAME)/SerialShell
	cp $(DIST_DIR)/SerialShell.readme $(DIST_DIR)/$(DIST_NAME)/
	cp $(DIST_DIR)/AutoInstall $(DIST_DIR)/$(DIST_NAME)/
	cp $(DIST_DIR)/SerialShell.readme $(DIST_DIR)/readme.txt

dist-lha: dist
	cd $(DIST_DIR) && lha ao5q $(DIST_NAME)_v$(VERSION).lha $(DIST_NAME) readme.txt
	@ls -la $(DIST_DIR)/$(DIST_NAME)_v$(VERSION).lha

clean:
	rm -f serialshell hello $(AMIUPDATE_OBJ)
	rm -rf $(DIST_DIR)/$(DIST_NAME) $(DIST_DIR)/*.lha $(DIST_DIR)/readme.txt
