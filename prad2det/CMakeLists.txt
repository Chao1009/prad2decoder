# prad2det/CMakeLists.txt — static library: libprad2det.a
#
# Analysis library: HyCal clustering, event reconstruction, hit matching.
# Depends on nlohmann/json for configuration loading.

cmake_minimum_required(VERSION 3.14)

# If built standalone, set up project and standards
if(NOT CMAKE_PROJECT_NAME)
    project(prad2det VERSION 1.0.0 LANGUAGES CXX)
    set(CMAKE_CXX_STANDARD 17)
    set(CMAKE_CXX_STANDARD_REQUIRED ON)
endif()

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
include(FetchContent)
set(FETCHCONTENT_QUIET OFF CACHE BOOL "" FORCE)

# nlohmann/json (header-only) — reuses top-level fetch if already available
if(NOT TARGET nlohmann_json::nlohmann_json)
    FetchContent_Declare(json
        GIT_REPOSITORY https://github.com/nlohmann/json.git
        GIT_TAG        v3.11.3
        GIT_SHALLOW    TRUE
    )
    FetchContent_MakeAvailable(json)
endif()

# ---------------------------------------------------------------------------
# Library target
# ---------------------------------------------------------------------------
add_library(prad2det STATIC
    src/HyCalSystem.cpp
    src/HyCalCluster.cpp
    src/EpicsStore.cpp
)

target_include_directories(prad2det
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include/prad2det>
)

target_link_libraries(prad2det PUBLIC nlohmann_json::nlohmann_json)

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------
include(GNUInstallDirs)

install(TARGETS prad2det ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
install(DIRECTORY include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/prad2det)
