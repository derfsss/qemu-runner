CC = ppc-amigaos-gcc
CFLAGS = -O2 -Wall
LDFLAGS = -lauto

TARGETS = serialshell hello

all: $(TARGETS)

serialshell: serialshell.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

hello: hello.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGETS)

.PHONY: all clean
