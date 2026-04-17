cmake_minimum_required(VERSION 3.14)
project(prad2evviewer VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

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
# Analysis tools + ROOT viewer (share a single ROOT dependency).
# Analysis is the *producer* of ROOT replay files; the viewer is the
# *consumer*.  They are always built together — a separate WITH_ROOT
# switch would let you compile the consumer without a producer, which is
# not useful.  Set -DBUILD_ANALYSIS=ON to enable both.
# ---------------------------------------------------------------------------
option(BUILD_ANALYSIS "Build analysis tools and ROOT data-source (requires ROOT)" OFF)

if(BUILD_ANALYSIS)
    find_package(ROOT 6.0 REQUIRED CONFIG COMPONENTS Core Tree RIO)
    include(${ROOT_USE_FILE})
    message(STATUS "ROOT: using ${ROOT_VERSION}")
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
# Python bindings (optional, uses pybind11 fetched via FetchContent)
# ---------------------------------------------------------------------------
option(BUILD_PYTHON "Build Python bindings (prad2py)" OFF)
if(BUILD_PYTHON)
    add_subdirectory(python)
endif()

# ---------------------------------------------------------------------------
# Qt standalone viewer + thin client (optional)
# ---------------------------------------------------------------------------
option(BUILD_GUI "Build Qt GUI applications" OFF)
if(BUILD_GUI)
    find_package(Qt6 QUIET COMPONENTS Widgets WebEngineWidgets)
    if(Qt6_FOUND)
        set(QT_VERSION_MAJOR 6)
        message(STATUS "Qt: using Qt6")
    else()
        find_package(Qt5 REQUIRED COMPONENTS Widgets WebEngineWidgets)
        set(QT_VERSION_MAJOR 5)
        message(STATUS "Qt: Qt6 not found, falling back to Qt5")
    endif()

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
