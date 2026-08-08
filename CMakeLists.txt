# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

set(WICAN_V210 1)
set(WICAN_V300 2)
set(WICAN_USB_V100 3)
set(WICAN_PRO 4)

set(WICAN_V210_STR "OBD")
set(WICAN_V300_STR "OBD")
set(WICAN_USB_V100_STR "USB")
set(WICAN_PRO_STR "OBD-PRO")

# Set these without quotes, as they're string literals
set(WICAN_V210_PREF "obd")
set(WICAN_V300_PREF "obd")
set(WICAN_USB_V100_PREF "usb")
set(WICAN_PRO_PREF "obd_pro")

add_definitions(-DWICAN_V210=${WICAN_V210})
add_definitions(-DWICAN_V300=${WICAN_V300})
add_definitions(-DWICAN_USB_V100=${WICAN_USB_V100})
add_definitions(-DWICAN_PRO=${WICAN_PRO})

add_definitions(-DWICAN_V210_STR="${WICAN_V210_STR}")
add_definitions(-DWICAN_V300_STR="${WICAN_V300_STR}")
add_definitions(-DWICAN_USB_V100_STR="${WICAN_USB_V100_STR}")
add_definitions(-DWICAN_PRO_STR="${WICAN_PRO_STR}")

execute_process(
    COMMAND git describe --tags --always --dirty
    WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
    OUTPUT_VARIABLE GIT_SHA
    OUTPUT_STRIP_TRAILING_WHITESPACE
    ERROR_QUIET
)

# git describe fails when the tree is built inside a container: the bind-mounted
# source is owned by a different uid than the container user, so git refuses it as
# "dubious ownership" and writes nothing to stdout. That failure was silent, and it
# left GIT_SHA empty - so the web UI's "Git ver" field showed nothing and the output
# binary was named with an empty suffix, making builds indistinguishable. Fall back
# to version.txt so a build always identifies itself.
if(NOT GIT_SHA)
    file(READ "${CMAKE_SOURCE_DIR}/version.txt" GIT_SHA)
    string(STRIP "${GIT_SHA}" GIT_SHA)
    message(STATUS "git describe unavailable; version from version.txt: ${GIT_SHA}")
endif()

set(GIT_SHA1 "${GIT_SHA}")
string(REPLACE "." "" GIT_SHA "${GIT_SHA}")

add_definitions(-DGIT_SHA="${GIT_SHA1}")

# Uncomment and use one of the following as needed
# set(HARDWARE_VER ${WICAN_V210})
# set(HARDWARE_VER ${WICAN_V300})
# set(HARDWARE_VER ${WICAN_USB_V100})
set(HARDWARE_VER ${WICAN_PRO})

add_definitions(-DHARDWARE_VER=${HARDWARE_VER})

set(PROJECT_PREF "wican-fw")

# Set HW_PREF based on HARDWARE_VER
if (HARDWARE_VER EQUAL ${WICAN_PRO})
    set(HW_PREF "${WICAN_PRO_PREF}")
    set(HW_VER "${WICAN_PRO_STR}")
elseif (HARDWARE_VER EQUAL ${WICAN_USB_V100})
    set(HW_PREF "${WICAN_USB_V100_PREF}")
    set(HW_VER "${WICAN_USB_V100_STR}")
elseif (HARDWARE_VER EQUAL ${WICAN_V210})
    set(HW_PREF "${WICAN_V210_PREF}")
    set(HW_VER "${WICAN_V210_STR}")
else()
    set(HW_PREF "${WICAN_V300_PREF}")
    set(HW_VER "${WICAN_V300_STR}")
endif()

add_definitions(-DHARDWARE_VERSION="${HW_VER}")

# Form the project binary name
set(PRJ_BIN "${PROJECT_PREF}_${HW_PREF}_${GIT_SHA}")
project("${PRJ_BIN}")

# WG split-tunnel: inject a destination-based IPv4 route hook into lwip,
# replacing the stock source-address hook. Same mechanism as Espressif's own
# examples/network/vlan_support example.
idf_component_get_property(lwip lwip COMPONENT_LIB)
target_compile_options(${lwip} PRIVATE "-I${CMAKE_SOURCE_DIR}/components/vpn_manager/include")
target_compile_definitions(${lwip} PRIVATE "-DESP_IDF_LWIP_HOOK_FILENAME=\"vpn_route_hook.h\"")
