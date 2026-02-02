cmake_minimum_required(VERSION 3.10)
project(udp_socketcan_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(udp_socketcan_bridge)
target_sources(udp_socketcan_bridge PRIVATE
    src/main.cpp
    src/bridge.cpp
    src/config.cpp
    src/protocol.cpp)
target_include_directories(udp_socketcan_bridge PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)

target_compile_options(udp_socketcan_bridge PRIVATE -fno-exceptions -fno-rtti -O2 -DNDEBUG)

target_link_libraries(udp_socketcan_bridge PRIVATE rt jsoncpp)

add_executable(udp_config_validator
    src/config_validator.cpp
    src/config.cpp)
target_include_directories(udp_config_validator PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_compile_options(udp_config_validator PRIVATE -fno-exceptions -fno-rtti -O2 -DNDEBUG)
target_link_libraries(udp_config_validator PRIVATE jsoncpp)

enable_testing()

add_executable(bridge_unit_tests
    tests/unit/bridge_unit_tests.cpp
    src/config.cpp
    src/protocol.cpp)
target_include_directories(bridge_unit_tests PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(bridge_unit_tests PRIVATE jsoncpp)
add_test(NAME bridge_unit_tests COMMAND bridge_unit_tests)
