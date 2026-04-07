CC = ppc-amigaos-gcc
CFLAGS = -O2 -Wall
LDFLAGS = -lauto

# AmiUpdate integration
AMIUPDATE_DIR = ../../../../AmiUpdateIntegration
include $(AMIUPDATE_DIR)/amiupdate.mk

# Distribution
DIST_DIR = dist
DIST_NAME = SerialShell
VERSION = 1.0

TARGETS = serialshell hello

all: $(TARGETS)

serialshell: serialshell.c $(AMIUPDATE_OBJ)
	$(CC) $(CFLAGS) $(AMIUPDATE_CFLAGS) -o $@ $^ $(LDFLAGS)

hello: hello.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

dist: serialshell
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
	rm -f $(TARGETS) $(AMIUPDATE_OBJ)
	rm -rf $(DIST_DIR)/$(DIST_NAME) $(DIST_DIR)/*.lha $(DIST_DIR)/readme.txt

.PHONY: all clean dist dist-lha
