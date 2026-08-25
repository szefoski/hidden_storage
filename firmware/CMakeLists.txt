cmake_minimum_required(VERSION 3.20.0)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(hidden_sd)

target_sources(app PRIVATE
  src/main.c
  src/rtt_proto.c
  src/sd_block.c
)
target_include_directories(app PRIVATE src)
