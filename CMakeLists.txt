cmake_minimum_required(VERSION 3.14)
project(prad2evviewer VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Position-independent code is required for the static libraries (prad2dec,
# prad2det, prad2srv) to be linkable into shared objects — ACLiC macros
# (analysis/scripts/*.C+) and the pybind11 module both produce .so files.
# Enabling it globally is cheap and removes a class of build failures.
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# ---------------------------------------------------------------------------
# Compile-time paths (auto-discovered, overridable)
# ---------------------------------------------------------------------------
if(NOT DEFINED DATABASE_DIR)
    set(DATABASE_DIR "${CMAKE_SOURCE_DIR}/database")
endif()
get_filename_component(DATABASE_DIR "${DATABASE_DIR}" ABSOLUTE)

if(NOT DEFINED RESOURCE_DIR)
    set(RESOURCE_DIR "${CMAKE_SOURCE_DIR}/resources")
endif()
get_filename_component(RESOURCE_DIR "${RESOURCE_DIR}" ABSOLUTE)

# ---------------------------------------------------------------------------
# Decoder library (evio/et deps handled inside)
# ---------------------------------------------------------------------------
add_subdirectory(prad2dec)

# ---------------------------------------------------------------------------
# Analysis library (clustering, reconstruction)
# ---------------------------------------------------------------------------
add_subdirectory(prad2det)

# ---------------------------------------------------------------------------
# Web server dependencies (json, websocketpp, asio)
# ---------------------------------------------------------------------------
include(FetchContent)
set(FETCHCONTENT_QUIET OFF CACHE BOOL "" FORCE)

FetchContent_Declare(json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG        v3.11.3
    GIT_SHALLOW    TRUE
)
FetchContent_Declare(websocketpp
    GIT_REPOSITORY https://github.com/zaphoyd/websocketpp.git
    GIT_TAG        0.8.2
    GIT_SHALLOW    TRUE
)
FetchContent_Declare(asio
    GIT_REPOSITORY https://github.com/chriskohlhoff/asio.git
    GIT_TAG        asio-1-30-2
    GIT_SHALLOW    TRUE
)
FetchContent_MakeAvailable(json websocketpp asio)

if(NOT TARGET asio_standalone)
    add_library(asio_standalone INTERFACE)
    target_include_directories(asio_standalone INTERFACE ${asio_SOURCE_DIR}/asio/include)
    target_compile_definitions(asio_standalone INTERFACE ASIO_STANDALONE)
endif()

if(NOT TARGET websocketpp_lib)
    add_library(websocketpp_lib INTERFACE)
    target_include_directories(websocketpp_lib INTERFACE ${websocketpp_SOURCE_DIR})
    target_link_libraries(websocketpp_lib INTERFACE asio_standalone)
endif()

# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------
if(WITH_ET)
    add_executable(evc_test test/test_main.cpp)
    target_link_libraries(evc_test PRIVATE prad2dec)
    set_target_properties(evc_test PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

    add_executable(et_feeder test/et_feeder.cpp)
    target_link_libraries(et_feeder PRIVATE prad2dec)
    set_target_properties(et_feeder PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

    add_executable(evchan_test test/evchan_test.cpp)
    target_link_libraries(evchan_test PRIVATE prad2dec)
    set_target_properties(evchan_test PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
endif()

add_executable(evio_dump test/evio_dump.cpp)
target_link_libraries(evio_dump PRIVATE prad2dec nlohmann_json::nlohmann_json)
target_compile_definitions(evio_dump PRIVATE DATABASE_DIR="${DATABASE_DIR}")
set_target_properties(evio_dump PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

add_executable(ped_calc test/ped_calc.cpp)
target_link_libraries(ped_calc PRIVATE prad2dec nlohmann_json::nlohmann_json)
target_compile_definitions(ped_calc PRIVATE DATABASE_DIR="${DATABASE_DIR}")
set_target_properties(ped_calc PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

add_executable(gem_dump test/gem_dump.cpp)
target_link_libraries(gem_dump PRIVATE prad2dec prad2det nlohmann_json::nlohmann_json)
target_compile_definitions(gem_dump PRIVATE DATABASE_DIR="${DATABASE_DIR}")
set_target_properties(gem_dump PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

add_executable(ts_dump test/ts_dump.cpp)
target_link_libraries(ts_dump PRIVATE prad2dec nlohmann_json::nlohmann_json)
target_compile_definitions(ts_dump PRIVATE DATABASE_DIR="${DATABASE_DIR}")
set_target_properties(ts_dump PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

add_executable(livetime test/livetime.cpp)
target_link_libraries(livetime PRIVATE prad2dec nlohmann_json::nlohmann_json)
target_compile_definitions(livetime PRIVATE DATABASE_DIR="${DATABASE_DIR}")
set_target_properties(livetime PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

# ---------------------------------------------------------------------------
# Optional features — default ON with graceful fallback.
#
# Each feature probes for its prerequisites with find_package(... QUIET).
# If the prereqs are missing we turn the feature OFF automatically and
# print an actionable warning.  If the user explicitly passed
# -DBUILD_XXX=ON on the command line and the dependency is still missing,
# we fail with a clear message pointing at the fix.
#
# Core targets (prad2dec, prad2det, prad2srv, prad2_server, the dump
# tools) always build regardless of these options.
# ---------------------------------------------------------------------------

# Record which feature flags the user explicitly passed on the command line
# BEFORE option() sets defaults — otherwise the variables are always DEFINED
# once option() has run and we can't tell default from explicit.
foreach(_f BUILD_ANALYSIS BUILD_PYTHON BUILD_GUI)
    if(DEFINED ${_f})
        set(${_f}_EXPLICIT TRUE)
    else()
        set(${_f}_EXPLICIT FALSE)
    endif()
endforeach()

# ----- Analysis tools + ROOT viewer (shared ROOT dependency) ----------------
# Analysis is the producer of ROOT replay files; the ROOT data-source in
# the viewer is the consumer.  They are always enabled together.
option(BUILD_ANALYSIS "Build analysis tools and ROOT data-source (requires ROOT)" ON)

if(BUILD_ANALYSIS)
    find_package(ROOT 6.0 QUIET CONFIG COMPONENTS Core Tree RIO)
    if(ROOT_FOUND)
        include(${ROOT_USE_FILE})
        message(STATUS "ROOT: using ${ROOT_VERSION}")
    elseif(BUILD_ANALYSIS_EXPLICIT)
        message(FATAL_ERROR
            "BUILD_ANALYSIS=ON but ROOT was not found.\n"
            "Install ROOT 6.x (https://root.cern) and add its bin/ to PATH,\n"
            "or re-configure with -DBUILD_ANALYSIS=OFF to skip.")
    else()
        message(WARNING
            "ROOT not found — disabling BUILD_ANALYSIS. "
            "Core targets still build. Set -DBUILD_ANALYSIS=OFF to silence.")
        set(BUILD_ANALYSIS OFF CACHE BOOL "" FORCE)
    endif()
endif()

# Sources use ``#ifdef WITH_ROOT`` to guard ROOT-dependent blocks; expose
# that compile-definition wherever BUILD_ANALYSIS is on.
set(WITH_ROOT ${BUILD_ANALYSIS})

# ---------------------------------------------------------------------------
# Server library (shared between prad2_server and prad2evviewer)
# ---------------------------------------------------------------------------
find_package(Threads REQUIRED)

set(SRV_SOURCES src/viewer_server.cpp src/viewer_server_http.cpp src/viewer_server_et.cpp
    src/app_state.cpp src/app_state_init.cpp src/app_state_api.cpp src/app_state_filter.cpp
    src/data_source.cpp src/evio_data_source.cpp)
if(WITH_ROOT)
    list(APPEND SRV_SOURCES src/root_data_source.cpp)
endif()

add_library(prad2srv STATIC ${SRV_SOURCES})
target_include_directories(prad2srv PUBLIC ${CMAKE_SOURCE_DIR}/src)
target_link_libraries(prad2srv PUBLIC
    prad2dec
    prad2det
    nlohmann_json::nlohmann_json
    websocketpp_lib
    Threads::Threads
)
if(WITH_ET)
    target_compile_definitions(prad2srv PUBLIC WITH_ET)
endif()
if(WITH_ROOT)
    target_compile_definitions(prad2srv PUBLIC WITH_ROOT)
    target_link_libraries(prad2srv PUBLIC ROOT::Core ROOT::Tree ROOT::RIO)
endif()

# ---------------------------------------------------------------------------
# Unified server (replaces prad2_viewer + prad2_monitor)
# ---------------------------------------------------------------------------
add_executable(prad2_server src/prad2_server.cpp)
target_compile_definitions(prad2_server PRIVATE
    DATABASE_DIR="${DATABASE_DIR}"
    RESOURCE_DIR="${RESOURCE_DIR}"
)
target_link_libraries(prad2_server PRIVATE prad2srv)
set_target_properties(prad2_server PROPERTIES RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

# ---------------------------------------------------------------------------
# Analysis subdirectory (pulls in ROOT via the same find_package above).
# ---------------------------------------------------------------------------
if(BUILD_ANALYSIS)
    add_subdirectory(analysis)
endif()

# ---------------------------------------------------------------------------
# Python bindings (prad2py).  pybind11 itself is fetched via FetchContent;
# we only need a Python 3 interpreter + development headers on the host.
# ---------------------------------------------------------------------------
option(BUILD_PYTHON "Build Python bindings (prad2py)" ON)
if(BUILD_PYTHON)
    find_package(Python 3.7 QUIET COMPONENTS Interpreter Development)
    if(Python_FOUND AND Python_Development_FOUND)
        message(STATUS "Python: ${Python_EXECUTABLE} "
                       "(headers: ${Python_INCLUDE_DIRS})")
        add_subdirectory(python)
    elseif(BUILD_PYTHON_EXPLICIT)
        message(FATAL_ERROR
            "BUILD_PYTHON=ON but Python dev headers were not found.\n"
            "RHEL/Rocky:  sudo dnf install python3-devel\n"
            "Debian/Ubuntu:  sudo apt install python3-dev\n"
            "Or point CMake at a specific interpreter with "
            "-DPython_EXECUTABLE=$(which python3).\n"
            "Re-configure with -DBUILD_PYTHON=OFF to skip.")
    else()
        message(WARNING
            "Python dev headers not found — disabling BUILD_PYTHON. "
            "Core targets still build. "
            "Install python3-devel / python3-dev and re-configure, "
            "or set -DBUILD_PYTHON=OFF to silence.")
        set(BUILD_PYTHON OFF CACHE BOOL "" FORCE)
    endif()
endif()

# ---------------------------------------------------------------------------
# Qt standalone viewer + thin client.  Qt6 preferred, Qt5 as fallback.
# ---------------------------------------------------------------------------
option(BUILD_GUI "Build Qt GUI applications (prad2evviewer, prad2_client)" ON)
if(BUILD_GUI)
    find_package(Qt6 QUIET COMPONENTS Widgets WebEngineWidgets)
    if(Qt6_FOUND)
        set(QT_VERSION_MAJOR 6)
        message(STATUS "Qt: using Qt6")
    else()
        find_package(Qt5 QUIET COMPONENTS Widgets WebEngineWidgets)
        if(Qt5_FOUND)
            set(QT_VERSION_MAJOR 5)
            message(STATUS "Qt: Qt6 not found, falling back to Qt5")
        elseif(BUILD_GUI_EXPLICIT)
            message(FATAL_ERROR
                "BUILD_GUI=ON but neither Qt6 nor Qt5 was found.\n"
                "Install Qt6/5 Widgets + WebEngineWidgets "
                "(Qt6 recommended), or re-configure with "
                "-DBUILD_GUI=OFF to skip.")
        else()
            message(WARNING
                "Qt6/Qt5 not found — disabling BUILD_GUI. "
                "Core targets still build. "
                "Install Qt6 Widgets + WebEngineWidgets (or Qt5) and "
                "re-configure, or set -DBUILD_GUI=OFF to silence.")
            set(BUILD_GUI OFF CACHE BOOL "" FORCE)
        endif()
    endif()
endif()
if(BUILD_GUI)

    # Standalone viewer (embedded server + WebEngine)
    add_executable(prad2evviewer src/prad2evviewer.cpp)
    target_compile_definitions(prad2evviewer PRIVATE
        DATABASE_DIR="${DATABASE_DIR}"
        RESOURCE_DIR="${RESOURCE_DIR}"
    )
    target_link_libraries(prad2evviewer PRIVATE
        prad2srv Qt${QT_VERSION_MAJOR}::Widgets Qt${QT_VERSION_MAJOR}::WebEngineWidgets)
    set_target_properties(prad2evviewer PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
        AUTOMOC ON
    )

    # Thin remote client
    add_executable(prad2_client src/prad2_client.cpp)
    target_link_libraries(prad2_client PRIVATE
        Qt${QT_VERSION_MAJOR}::Widgets Qt${QT_VERSION_MAJOR}::WebEngineWidgets)
    set_target_properties(prad2_client PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin
    )
endif()

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
include(GNUInstallDirs)

install(TARGETS prad2_server evio_dump ped_calc gem_dump
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

if(WITH_ET)
    install(TARGETS evc_test et_feeder evchan_test
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

if(BUILD_GUI)
    install(TARGETS prad2evviewer prad2_client
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
endif()

install(DIRECTORY database/   DESTINATION ${CMAKE_INSTALL_DATADIR}/prad2evviewer/database)
install(DIRECTORY resources/  DESTINATION ${CMAKE_INSTALL_DATADIR}/prad2evviewer/resources)
install(FILES scripts/setup.sh scripts/setup.csh
    DESTINATION ${CMAKE_INSTALL_BINDIR})

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
message(STATUS "----------------------------------------------------")
message(STATUS "  ${PROJECT_NAME} ${PROJECT_VERSION}")
message(STATUS "  Build type   : ${CMAKE_BUILD_TYPE}")
message(STATUS "  Database     : ${DATABASE_DIR}")
message(STATUS "  Resources    : ${RESOURCE_DIR}")
message(STATUS "  ET support   : ${WITH_ET}")
message(STATUS "  Analysis+ROOT: ${BUILD_ANALYSIS}")
message(STATUS "  Python bind. : ${BUILD_PYTHON}")
message(STATUS "  Qt GUI       : ${BUILD_GUI}")
message(STATUS "----------------------------------------------------")
