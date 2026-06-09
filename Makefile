MODULE_TOPDIR = $(shell grass --config path)

PGM = r.in.prism

include $(MODULE_TOPDIR)/include/Make/Script.make

default: script
